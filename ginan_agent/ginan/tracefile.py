"""Ginan 领域层 —— 读 `.TRACE` 里的**残差**，算成能画图的东西。

============================ 残差是什么、为什么值得看 ============================
位置解出来是"一个数"，而残差是**这个数后面有多少万次观测在撑着**。
用户问"结果可信吗"，`.POS` 只能回答"稳不稳"；残差能回答
"**数据本身质量如何**"：有没有系统性偏差、哪颗卫星/哪个频点特别差、
有没有整段被剔除的时段。

============================ ⚠️⚠️ 残差不是白来的（实测过的代价）============================
Ginan 里残差行受**两个**开关管，缺一个都拿不到：

    outputs.trace.level: 5             ← 级别不够，**连块头都不写**（实测：1、3 级 0 行）
    outputs.trace.output_residuals: true
    outputs.trace.output_network: true ← **PPP/RTS 的残差在这一份里**
    network_filename: ...

⚠️ 最后一条是这次查出来的：只开 `output_receivers` 的话，拿到的是
   `RESIDUALS/SPP`（预处理阶段的**码**残差），PPP 和 RTS 那两段**根本不写** ——
   而 Ginan 自己的 `plot_trace_res.py` 用的正是 PPP/RTS
   （它的默认是"用平滑后的残差，更准"）。

实测（ALIC 2026-09-02，60 个历元，同一份配置只改 trace）：

    level 1 + output_residuals     0 行        （7.0 MB）
    level 3 + output_residuals     0 行        （22.1 MB）
    level 5 + output_residuals     4236 行     （32.5 MB）← 只有 SPP
    level 5 + 网络 trace           18460 行    （6.4 MB） ← PPP + RTS 都在，**而且更小**

⚠️ **整天**的实测（ALIC 2026-09-02，2880 历元，网络那份）：

    `NET_*.TRACE`            144 MB   39.0 万行 → 去中间迭代后 **17.7 万条**
    `NET_*_smoothed.TRACE`    52 MB   （RTS，`iter = -1`，sigma 整列是 0）
    整个重跑                  148 秒

    ⇒ 一天 **约 200 MB**、两分半钟。（60 历元那份按比例外推是 300 MB ——
      **外推偏高**：文件有固定开销，历元越多单位成本越低。）

⚠️ 而**级别不影响解算结果**：同一份配置 level 1 跑一遍、level 5 跑一遍，
   `.POS` **逐字节相同**（实测，sha256 一致）。所以"开残差"是**纯观测**动作 ——
   这一点很重要：它意味着诊断看到的残差**就是那份解**的残差，不是另一次解算的。

============================ 三种残差块 ============================
    RESIDUALS/SPP     预处理阶段（码，单点定位）
    RESIDUALS/PPP     PPP 滤波（码 + 相位）—— **多次后验迭代**，同一测量出现好几行
    RESIDUALS/RTS     反向平滑（`iter = -1`，一次一条）← **默认用这个**

⚠️ PPP 那块要**只取最后一次迭代**（`keep_last_iteration`）：前面几次是
   滤波器迭代过程中的中间值，混在一起会把"残差"算大。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from pathlib import Path

# `%\t 0\t2026-09-02 00:00:00.01\t   CODE_MEAS\t G15\tALIC\tL1W-L2W\t -2.08213896\t…`
#
# ⚠️ 字段之间是**制表符**，而字段内部还有空格对齐（` G15`、`   NONE`）——
#    所以按 `\t` 切、每段再 strip()。别写成按空白切：那样卫星号会和
#    后面的字段粘在一起，而且症状是"数字看起来像对的"。
_LINE = re.compile(
    r"^%\s*(?P<iter>-?\d+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"(?P<meas>CODE_MEAS|PHAS_MEAS)\s+"
    r"(?P<sat>\S+)\s+(?P<recv>\S+)\s+(?P<sig>\S+)\s+"
    r"(?P<prefit>\S+)\s+(?P<postfit>\S+)\s+(?P<sigma>\S+)"
)

# 块头/块尾：`+RESIDUALS/PPP`、`-RESIDUALS/RTS`
_BLOCK = re.compile(r"^[+-]RESIDUALS(?P<suffix>\S*)\s*$")

# 界面上那两个类型名 —— 一个人话，一个给机器
MEAS_LABEL = {"CODE_MEAS": "码（伪距）", "PHAS_MEAS": "相位（载波）"}

# 一个站一天大概多少条 —— 用来在"要不要跑诊断"之前给用户一个数量级
TYPICAL_PER_DAY = 120_000


@dataclass(frozen=True)
class Resid:
    """一条残差。"""

    t: dt.datetime
    iteration: int          # RTS 那份是 -1
    meas: str               # CODE_MEAS / PHAS_MEAS
    sat: str
    recv: str
    sig: str
    prefit: float
    postfit: float
    sigma: float

    @property
    def normalized(self) -> float:
        """归一化残差 = 后验残差 / 形式误差。

        ⚠️ 这是**唯一能跨卫星/跨频点比较**的量：原始残差里
           低仰角的本来就大（形式误差也大），直接比会冤枉它。
           一个正常的解，它应该大致落在 ±1 里，而且近似正态。
        """
        return self.postfit / self.sigma if self.sigma else float("nan")


@dataclass
class ResidFile:
    """一份 `.TRACE` 解析出来的东西。

    ⚠️ `points` 是**保留下来**的那些（PPP 那份已经去过中间迭代），
       `n_lines` 是文件里真实的行数 —— 两个数不一样时要说清楚，
       不然"我们只用了 1/4 的数据"这件事没人知道。
    """

    path: Path
    points: list[Resid]
    n_lines: int = 0
    n_bad: int = 0                     # 看不懂的行
    blocks: tuple[str, ...] = ()
    # ⚠️ **原始条数**。落盘再读回来时 `points` 只剩抽过样的那些，
    #    所以总条数不能拿 `len(points)` 当 —— 否则界面上会把
    #    "抽稀后的 2000"说成"一共 2000 条观测"（差两个数量级）。
    total: int = 0

    @property
    def count(self) -> int:
        return self.total or len(self.points)

    @property
    def kinds(self) -> list[str]:
        return sorted({p.meas for p in self.points})

    def by_kind(self, meas: str) -> list[Resid]:
        return [p for p in self.points if p.meas == meas]


def _f(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse(path: Path, *, keep_last_iteration: bool = True,
          limit: int | None = None) -> ResidFile:
    """读一份 `.TRACE` 里的残差行。

    ⚠️ **流式**：逐行读、只留需要的。一天的 RTS 残差约 12 万条
       （十几 MB 文本）没问题，但 PPP 那份带中间迭代能到百万行 ——
       所以中间迭代**边读边丢**，不攒到最后再筛。

    ⚠️ `limit` 是给"先看看"用的（解析到够了就停），
       但**报出去的必须是"读了多少行"而不是"留了多少条"** ——
       否则界面上会写着一个偏小的数，而没人知道那是抽样。
    """
    path = Path(path)
    pts: list[Resid] = []
    keep: dict[tuple, Resid] = {}      # PPP 那份用：同一测量只留最后一次迭代
    n_lines = n_bad = 0
    blocks: list[str] = []
    suffix = ""

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(("+", "-")):
                m = _BLOCK.match(line.rstrip("\n"))
                if m:
                    suffix = m.group("suffix")
                    if line[0] == "+":
                        blocks.append(suffix)
                continue
            if not line.startswith("%"):
                continue
            m = _LINE.match(line)
            if not m:
                n_bad += 1
                continue
            n_lines += 1
            g = m.groupdict()
            try:
                t = dt.datetime.fromisoformat(g["date"] + " " + g["time"])
            except ValueError:
                n_bad += 1
                continue
            r = Resid(
                t=t, iteration=int(g["iter"]), meas=g["meas"],
                sat=g["sat"], recv=g["recv"], sig=g["sig"],
                prefit=_f(g["prefit"]), postfit=_f(g["postfit"]),
                sigma=_f(g["sigma"]),
            )
            if keep_last_iteration and r.iteration >= 0:
                key = (r.t, r.meas, r.sat, r.sig)
                old = keep.get(key)
                if old is None or r.iteration > old.iteration:
                    keep[key] = r
            else:
                pts.append(r)
            if limit is not None and n_lines >= limit:
                break

    # ⚠️ **合并**，不是替换 —— 一个文件里可能同时有"带迭代号的"和
    #    `iter = -1`（RTS）两种行。写成 `pts = list(keep.values())` 的话，
    #    那批 -1 的行会被**整批丢掉**，而且不报错、数字看着还挺正常。
    #    （守卫抓到的：夹具里 CODE 有迭代号、PHAS 是 -1，PHAS 全没了。）
    if keep:
        pts = list(keep.values()) + pts
    pts.sort(key=lambda t2: (t2.t, t2.sat, t2.sig))
    # ⚠️ 去重：同一个块每个历元都写一遍（一天 2880 次），
    #    全列出来的话"这份文件里有哪几种残差"这个问题就没法一眼看出来了。
    seen: list[str] = []
    for b in blocks:
        if b not in seen:
            seen.append(b)
    return ResidFile(path=path, points=pts, n_lines=n_lines, n_bad=n_bad,
                     blocks=tuple(seen))


# =============================================================================
# 统计
# =============================================================================


def _stats(vals: list[float]) -> dict:
    """均值 / 标准差 / RMS / 最大绝对值（都不是加权 —— 残差的每个值等价）。"""
    xs = [v for v in vals if math.isfinite(v)]
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": float("nan"), "std": float("nan"),
                "rms": float("nan"), "max_abs": float("nan")}
    mean = sum(xs) / n
    var = sum((v - mean) ** 2 for v in xs) / n
    return {
        "n": n,
        "mean": mean,
        "std": math.sqrt(var),
        "rms": math.sqrt(sum(v * v for v in xs) / n),
        "max_abs": max(abs(v) for v in xs),
    }


def summarize(rf: ResidFile, *, bin_minutes: int = 30) -> dict:
    """把一份残差算成"能画图也能说人话"的东西。

    只做**一遍**遍历能做完的事：按测量类型、按信号、按卫星，
    再加上分时间窗的序列（图要的是"随时间"）。
    """
    out: dict = {
        "file": rf.path.name,
        "count": rf.count,
        "n_lines": rf.n_lines,
        "n_bad": rf.n_bad,
        "blocks": list(rf.blocks),
        "kinds": {},
        "time": {},
    }
    if rf.count == 0:
        return out

    times = [p.t for p in rf.points]
    t0, t1 = min(times), max(times)
    out["time"] = {"start": t0.isoformat(sep=" ", timespec="seconds"),
                   "end": t1.isoformat(sep=" ", timespec="seconds")}

    # ---- 按测量类型 ----
    for meas in rf.kinds:
        group = rf.by_kind(meas)
        out["kinds"][meas] = {
            "label": MEAS_LABEL.get(meas, meas),
            "postfit": _stats([p.postfit for p in group]),
            "prefit": _stats([p.prefit for p in group]),
            "normalized": _stats([p.normalized for p in group]),
            # 归一化残差超过 3 的占比 —— 判"有没有异常"的那把尺子
            "frac_over_3sigma": (
                sum(1 for p in group if abs(p.normalized) > 3) / len(group)
            ),
        }
        # ⚠️⚠️ **RMS 那一份 `.TRACE` 里 `sigma` 整列是 0**（实测：prefit 也是 0，
        #    只有 postfit 是真的）—— 于是归一化残差**整列算不出来**。
        #    不说的话，界面上会显示"0 条，超过 3σ 的占 0.00%"，
        #    看着像"完美"，其实是"没有这一列"。
        out["kinds"][meas]["normalized_available"] = bool(
            group and all(p.sigma > 0 for p in group))
        # ⚠️ 实测：相位那组里约 2% 的后验残差**恰好是 0**（不是四舍五入 ——
        #    就是 0.00000000）。成因我们**没有查**（很可能是"定义该卫星模糊度的
        #    那一条观测"，但那是推测）。不报出来的话，直方图上那根尖峰会
        #    被当成"残差特别好"，而它其实可能是"根本没有残差"。
        out["kinds"][meas]["frac_near_zero"] = (
            sum(1 for p in group if abs(p.postfit) < 1e-5) / len(group))

    # ---- 按信号 / 按卫星（跨测量类型分开，因为码和相位的量级差两个数量级）----
    for meas in rf.kinds:
        group = rf.by_kind(meas)
        by_sig: dict[str, list[Resid]] = {}
        by_sat: dict[str, list[Resid]] = {}
        for p in group:
            by_sig.setdefault(p.sig, []).append(p)
            by_sat.setdefault(p.sat, []).append(p)
        out.setdefault("by_signal", {})[meas] = {
            k: _stats([p.postfit for p in v]) for k, v in sorted(by_sig.items())
        }
        out.setdefault("by_satellite", {})[meas] = {
            k: _stats([p.postfit for p in v]) for k, v in sorted(by_sat.items())
        }

    # ---- 分窗序列（画"随时间"用）----
    step = max(1, int(bin_minutes * 60))
    bins: dict[int, list[Resid]] = {}
    base = t0.timestamp()
    for p in rf.points:
        bins.setdefault(int((p.t.timestamp() - base) // step), []).append(p)
    series: dict[str, dict] = {}
    for meas in rf.kinds:
        xs, means, rms, ns = [], [], [], []
        for b in sorted(bins):
            group = [p for p in bins[b] if p.meas == meas]
            if not group:
                continue
            st = _stats([p.postfit for p in group])
            xs.append(round(b * bin_minutes, 1))     # 相对起点，分钟
            means.append(st["mean"])
            rms.append(st["rms"])
            ns.append(st["n"])
        series[meas] = {"t_minutes": xs, "mean": means, "rms": rms, "n": ns}
    out["series"] = series
    out["bin_minutes"] = bin_minutes
    return out


def verdict(summary: dict) -> dict:
    """人话结论 —— **先看条件，再报数字**（同 `posfile.verdict` 的规矩）。"""
    if not summary.get("count"):
        return {
            "headline": "这份 .TRACE 里没有残差行。",
            "detail": (
                "残差要有两样东西才会写出来：`outputs.trace.level: 5` 和 "
                "`output_residuals: true`（PPP/RTS 那两段还要 `output_network: true`）。"
                "少一样都是**静默的**：文件照常写，只是没有残差行。"
            ),
            "quality": "unknown",
        }

    parts = []
    worst = None
    usable = 0
    for meas, d in summary["kinds"].items():
        nz = d["normalized"]
        frac = d["frac_over_3sigma"]
        if d.get("normalized_available"):
            usable += 1
            parts.append(
                f"{d['label']} {nz['n']} 条：归一化残差 "
                f"平均 {nz['mean']:+.2f}、标准差 {nz['std']:.2f}，"
                f"超过 3σ 的占 {frac * 100:.2f}%"
            )
            if worst is None or frac > worst[1]:
                worst = (d["label"], frac)
        else:
            # ⚠️ 这一类的 `sigma` 是 0（平滑解那份 .TRACE 就是这样）——
            #    那就算不出归一化残差。**必须说出来**，不能显示成"0 条，0.00%"。
            parts.append(
                f"{d['label']} {d['postfit']['n']} 条："
                "这份 .TRACE 里没有形式误差（σ=0），归一化残差算不出来 —— "
                f"原始后验残差 RMS {d['postfit']['rms']:.4f}"
            )

    if usable == 0:
        return {
            "headline": "读到了残差，但**算不出归一化残差**。",
            "detail": "；".join(parts) + "。",
            "note": (
                "平滑解（RTS）那份 `.TRACE` 里 `sigma` 整列是 0 —— 想看归一化残差"
                "（残差 ÷ 形式误差）要读 **PPP** 那一份（`NET_*.TRACE`，不是 "
                "`..._smoothed.TRACE`）。"
            ),
            "quality": "unknown",
        }

    frac = worst[1] if worst else 0.0
    if frac <= 0.005:
        head = "残差正常 —— 几乎没有超出 3σ 的观测。"
        quality = "good"
    elif frac <= 0.02:
        head = f"残差基本正常，但{worst[0]}有 {frac * 100:.2f}% 超出 3σ。"
        quality = "fair"
    else:
        head = (f"{worst[0]}有 {frac * 100:.2f}% 的观测超出 3σ "
                "—— 这一路的数据值得看一眼。")
        quality = "poor"

    return {
        "headline": head,
        "detail": "；".join(parts) + "。",
        # ⚠️ 说清这不是"准不准"：残差小只说明"观测和模型自洽"。
        "note": (
            "残差是「观测值减掉模型算出来的值」—— 它小，说明观测和所选的模型"
            "自洽，不等于位置一定准（那要用权威坐标去比）。"
        ),
        "quality": quality,
    }


# =============================================================================
# 落盘 / 读回（复现包那一类小文件）
# =============================================================================
#
# ⚠️ 为什么要"派生一份小的"：那份 `.TRACE` 是**几百 MB**（一天 ~300 MB），
#    而画图只需要**抽过样的点**（几千个）+ 统计量。
#    派生文件几百 KB —— 用户可以放心把大文件删掉，图还在。
#
# ⚠️ 但**原始的 `.TRACE` 不删**：它是"这次残差是怎么来的"的唯一证据，
#    而删不删该由用户决定（同 `repro` 的规矩）。

BUNDLE_NAME = "residual_summary.json"


def bundle(rf: ResidFile, summary: dict, verdict_: dict,
           *, sampled: dict | None = None) -> dict:
    """把残差压成一个小文件的内容。

    ⚠️ `sampled` 由画图那边给（`charts._resid_sample`）—— 抽稀的判据
       （"超限的点永远保住"）属于画图，不属于解析。
    """
    return {
        "format_version": 1,
        "file": rf.path.name,
        "count": rf.count,
        "n_lines": rf.n_lines,
        "n_bad": rf.n_bad,
        "blocks": list(rf.blocks),
        "summary": summary,
        "verdict": verdict_,
        "points": sampled or {},
    }


def unbundle(data: dict):
    """读回 —— 返回 `(ResidFile, summary, verdict)`。

    ⚠️ 读回来的 `ResidFile.points` 只有**抽过样的那些**，
       所以 `count` 用 bundle 里记的原始条数（不是 `len(points)`）——
       否则界面上会把"抽稀后的 2000"当成"一共 2000 条观测"。
    """
    pts: list[Resid] = []
    for kind, rows in (data.get("points") or {}).items():
        for r in rows:
            pts.append(Resid(
                t=dt.datetime.fromisoformat(r["t"]),
                iteration=-1, meas=kind, sat=r["sat"], recv=r.get("recv", ""),
                sig=r["sig"], prefit=r.get("prefit", float("nan")),
                postfit=r["postfit"], sigma=r.get("sigma", float("nan")),
            ))
    pts.sort(key=lambda p: p.t)
    rf = ResidFile(path=Path(data.get("file", "?")), points=pts,
                   n_lines=int(data.get("n_lines") or 0),
                   n_bad=int(data.get("n_bad") or 0),
                   blocks=tuple(data.get("blocks") or ()),
                   total=int(data.get("count") or len(pts)))
    return rf, data.get("summary") or {}, data.get("verdict") or {}
