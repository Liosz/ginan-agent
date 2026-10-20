"""Ginan 领域层 —— 读 SP3 轨道文件，以及**修 Ginan 在 Windows 上写坏的那个标签**。

============================ 这个模块为什么存在 ============================
直接原因是实测撞出来的一个**静默**失败（2026-09-27）：

    Ginan 写出来的 SP3，**第 2 个历元起的行首是 `E*` 而不是 `*`**。
    ⇒ Ginan **自己的**读取器只认 `*`，于是它读自己的产物**只读到 1 个历元**。

实测：`brdc2sp3` 跑出的一天（96 历元、半径 26,473 km、**内容是对的**）
拿去和 IGS 精密轨道比对：

    直接比                     1 个公共历元，残差 1,800,000 米（垃圾）
    把 95 个 `E*` 修成 `*`     96 个公共历元，3D 残差 **1.32 米** ← 广播星历该有的量级

⚠️ **这不是新发现的缺陷。** `docs/ginan-facts.md` 七点九早记着它 ——
   根因是 `sp3Write.cpp:294` 的 `seekp(-4)` 没算 Windows 文本模式的 `\\r\\n`。
   但 app 里**一处都没有修它**（全项目搜 `E*`：0 命中），
   所以 ① 的验收是**手工**修了文件才比出来的
   （`ginan-testdata-202506/orbitcheck/cmp3.yaml` 用的是 `ours_fixed.SP3`）。

⇒ 这就是"任何要读**自己产物**的功能都缺的那一步"。

⚠️ **修的是文件副本，不是去改 Ginan。**
   用户对上游缺陷的指示是"不管"，这里遵守 —— 但 app 能控制的这一侧必须补上，
   否则"读自己的产物"这件事根本不成立。

============================ 只做体检，不做完整解析 ============================
这个模块**不解析卫星位置**。没有任何调用方要那些数（比对是 Ginan 自己做的），
凭空写一个位置解析器就是造一件没人穿的衣服。

它回答的是三个**真实**问题：
    · 这份 SP3 Ginan **读得懂吗**（历元标签坏没坏）
    · 它覆盖了什么时段、多久一个历元、多少颗星
    · 两套轨道**差多少**（解析 `compare_orbits` 的输出）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .posfile import Verdict

# =============================================================================
# 修历元标签
# =============================================================================

# 坏掉的历元标签：**行首**的 `E*`。
#
# ⚠️⚠️ **行首锚（`(?m)^`）是免费的保险，但要说清它保的是什么。**
#
#     实测（2026-09-27，对 30287 行的一份真产物）：
#
#         `E\*`（无锚）命中 95 处，`(?m)^E\*`（有锚）也是 95 处 —— **差 0**。
#
#     ⇒ 也就是说，**在我们产出的文件上，锚不锚结果一样**。
#        （数据行是 `PG01`/`VE01`/`PE02` 那种形状，`E` 后面跟的是数字不是 `*`。）
#
#     ⚠️ 所以不能声称"不锚就会毁掉数据行" —— 那是**推**出来的，不是量出来的。
#
#     真正需要锚的地方是 **SP3 的 `/* … */` 注释行**（格式里是自由文本，
#     我们自己的产物里就有 4 行）。一个 `E*` 出现在注释中间时，
#     无锚的写法会把它改成 `*` —— 那是**静默改坏一份别人的文件**。
#
#     ⇒ 守卫 ⑲d 的 (b) 用的夹具里**刻意放了一条带 `E*` 的注释行**，
#       否则那条守卫在真产物上**永远变不了红**，等于没有。
_BROKEN_EPOCH = re.compile(r"(?m)^E\*")

# 正常的历元标签，用来数"Ginan 真正读得到几个历元"。
_EPOCH_TAG = re.compile(r"(?m)^\*")


def repair_epoch_tags(text: str) -> tuple[str, int]:
    """把行首的 `E*` 换回 `*`。返回 `(新文本, 改了几个)`。

    ⚠️ 纯字符串进、纯字符串出 —— **不碰文件系统**。
       写到哪里由调用方决定，通常是**副本**而不是原产物
       （原产物是用户的，我们没有理由改它）。
    """
    out, n = _BROKEN_EPOCH.subn("*", text)
    return out, n


def repair_file(src: Path, dst: Path) -> int:
    """读 `src`、修好、写到 `dst`。返回改了几个标签。

    ⚠️ `src == dst` 是**允许的**（原地修），但调用方要想清楚 ——
       `do_compare_orbits` 用的就是副本，见那里的说明。

    ⚠️⚠️ **`newline=""`，两头都要。**
       不加的话 Python 会把 `\\r\\n` 翻译成 `\\n`（universal newlines），
       于是这个函数会**把整份文件的行尾都换掉** —— 而它声称做的
       只是"改几个历元标签"。一个改 95 个字节的函数悄悄重写了每一行，
       是那种最难看出来的改动。

       ⚠️ 而且**我们的产物恰恰就是 CRLF** 的（`cat -A` 实测：`#dV2019…^M$`）
           —— 这不是假想的边角情况，是这批文件的常态。

    ⚠️ `errors="replace"`：产物可能被中途杀掉写了一半，
       "读不出来"不该让整个流程崩掉（和 `posfile.parse` 同一个原则）。
    """
    src, dst = Path(src), Path(dst)

    # ⚠️ 读和写**分开报错**，因为它们是两件事。
    #
    #    实测踩过：目标目录不存在时抛的是
    #    `No such file or directory: '<dst>'`，而调用方看到的是
    #    "读不了 xxx.sp3" —— **怪在源文件头上，而源文件好好的**。
    #    一个把根因指错方向的报错，比没有报错更费时间。
    try:
        with src.open("r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
    except OSError as e:
        raise OSError(f"读不了 {src}：{e}") from e

    fixed, n = repair_epoch_tags(text)

    # ⚠️ 目标目录不存在就建 —— 和 `runner` 写日志那里同一个做法
    #    （`log_path.parent.mkdir(parents=True, exist_ok=True)`）。
    #    调用方给的是一个"要写到哪"的路径，不该还要它先保证目录在。
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("w", encoding="utf-8", newline="") as f:
            f.write(fixed)
    except OSError as e:
        raise OSError(f"写不了 {dst}：{e}") from e

    return n


# =============================================================================
# 体检
# =============================================================================


@dataclass
class Sp3File:
    """一份 SP3 的**体检结果** —— 不是它的全部内容（见模块 docstring）。"""

    origin: Path
    size: int = 0
    start: datetime | None = None
    """第一个历元的时刻（从 `#d` 行读）。"""
    interval_s: float | None = None
    """历元间隔（秒，从 `##` 行读）。"""
    declared_epochs: int | None = None
    """**头部声称**的历元数（`#d` 行最后一个字段）。

    ⚠️ 它可能和 `written_epochs` 对不上 —— 那就说明文件被写坏了或写了一半。
       实测撞到过一次反向的情况：头部写 `1.00000000` 而实际是 10 秒一个
       （见 `ginan-facts.md` ③ 那一节）。
    """
    systems: str = ""
    """出现的系统字母，如 `"GERC"`（从 `+` 行读）。"""
    ok_epochs: int = 0
    """行首是 `*` 的历元数 —— **Ginan 真正读得到的就这么多**。"""
    broken_epochs: int = 0
    """行首是 `E*` 的历元数 —— 在 Windows 上产出的文件里，这就是"读不到的那些"。"""

    @property
    def written_epochs(self) -> int:
        """文件里**实际写出来**的历元数（好的 + 坏的）。"""
        return self.ok_epochs + self.broken_epochs

    @property
    def damaged(self) -> bool:
        """这份产物是不是被那个 CRLF 缺陷写坏了。"""
        return self.broken_epochs > 0


# SP3 第一行的**结构**。
#
# ⚠️⚠️ 别拿"把所有数字抠出来"当解析 —— 我第一版就是那么写的，
#     而它在真文件上**算错了历元数**：
#
#         #dV2019  7 18  0  0  0.00000000      96 ORBIT igb14 FIT  GAA
#                                                          ^^^^^
#         `igb14`（参考框架名）里的 `14` 被当成了"最后一个数" ⇒ 历元数 = 14
#
#     这类错**不会报错**，只会给出一个看着合理的错数字。
#
# ⚠️ 三个生产者的头**形状不一样**（都是实测的原文）：
#
#     Ginan  产出   `#dV2019  7 18  0  0  0.00000000      96 ORBIT igb14 FIT  GAA`
#     TUG    真值   `#dP2019 07 18 00 00 00.00000000     289 RAW   IGSR3 FIT  TUG`
#     IGS    快速   `#cP2025  6 15  0  0  0.00000000      96 ORBIT IGb20 HLM  IGS`
#
#     ⇒ 年月日时分的**位数不固定**（` 7` vs `07`），所以用 `\d{1,2}`；
#       版本字母是 `a`–`d`；第 4 个字符是 `P`（位置）或 `V`（位置+速度）——
#       ⚠️ **Ginan 产的是 `V`**（它配了 `output_velocities: true`）。
_SP3_HEAD = re.compile(
    r"^#(?P<ver>[a-d])(?P<kind>[PV])"
    r"\s*(?P<year>\d{4})\s+(?P<mon>\d{1,2})\s+(?P<day>\d{1,2})"
    r"\s+(?P<hh>\d{1,2})\s+(?P<mi>\d{1,2})\s+(?P<ss>[\d.]+)"
    r"\s+(?P<n>\d+)(?=\s)"
)

# 第二行 `##`：`<GPS周> <周内秒> <历元间隔> <MJD> <小数日>`。
# ⚠️ 同样写成结构式而不是抠数字 —— 理由见上面那一段。
_SP3_HEAD2 = re.compile(
    r"^##\s+(?P<week>\d+)\s+(?P<tow>[\d.]+)\s+(?P<interval>[\d.]+)"
)


def read(path: Path) -> Sp3File:
    """读一份 SP3 的体检信息。**永不抛异常** —— 失败时返回一份空的。

    ⚠️ 只扫头部 + 数标签，**不解析位置**（见模块 docstring）。
       所以它能用在很大的文件上：84 MB 的 POD 产物也只是几秒的事。

    ⚠️ `newline=""` 和 `repair_file` 一致 —— 见那里的说明
       （否则行尾被悄悄翻译掉，"改了几个标签"和"改了多少字节"就对不上了）。
    """
    path = Path(path)
    result = Sp3File(origin=path)

    try:
        result.size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()
    except OSError:
        return result

    lines = text.splitlines()

    # ---- 第 1 行：起始时刻 + 声明的历元数 ----
    m = _SP3_HEAD.match(lines[0]) if lines else None
    if m:
        try:
            result.start = datetime(
                int(m.group("year")), int(m.group("mon")), int(m.group("day")),
                int(m.group("hh")), int(m.group("mi")), int(float(m.group("ss"))),
            )
        except (ValueError, TypeError):
            result.start = None
        try:
            result.declared_epochs = int(m.group("n"))
        except (ValueError, TypeError):
            result.declared_epochs = None

    # ---- 第 2 行 `##`：历元间隔 ----
    for line in lines[:3]:
        if line.startswith("##"):
            m2 = _SP3_HEAD2.match(line)
            if m2:
                try:
                    result.interval_s = float(m2.group("interval"))
                except (ValueError, TypeError):
                    result.interval_s = None
            break

    # ---- `+` 行：卫星列表 ----
    letters: list[str] = []
    for line in lines[:60]:
        if not line.startswith("+"):
            continue
        for ch in line[1:]:
            if ch.isalpha() and ch not in letters:
                letters.append(ch)
    result.systems = "".join(sorted(letters))

    # ---- 数历元标签 ----
    result.ok_epochs = len(_EPOCH_TAG.findall(text))
    result.broken_epochs = len(_BROKEN_EPOCH.findall(text))

    return result


# =============================================================================
# 比对结果（`compare_orbits` 的输出）
# =============================================================================


@dataclass
class Rms:
    """一个方向上、Helmert 转换**前后**的 RMS（米）。"""

    before: float
    after: float
    entries: int


@dataclass
class OrbitComparison:
    """一次轨道比对的结果。

    ⚠️ `compare_orbits` 做的是**Helmert 七参数转换**：先算两套轨道的整体
       平移/旋转/尺度差并扣掉，剩下的才算"形状上的差异"。

       所以：
           before = 扣之前（含坐标系/基准的系统差）
           after  = 扣之后（**这才是"两条轨道差多少"**）
    """

    files: tuple[str, str] = ("", "")
    agg: dict[str, Rms] = field(default_factory=dict)
    """汇总行。键是方向：`3D` / `N` / `R` / `T`。"""
    per_sat: dict[str, Rms] = field(default_factory=dict)
    """逐卫星的 `3D` 行。"""

    @property
    def epochs(self) -> int:
        """公共历元数。

        ⚠️ 逐星那些行的 `entries` 就是它，取最大值 —— 汇总行的 `entries`
           是"卫星数 × 历元数"，不是历元数。
        """
        if not self.per_sat:
            return 0
        return max(r.entries for r in self.per_sat.values())

    @property
    def satellites(self) -> int:
        return len(self.per_sat)

    @property
    def rms_3d(self) -> float | None:
        """★ **要报告的那个数**：扣掉整体差之后的三维 RMS（米）。"""
        r = self.agg.get("3D")
        return r.after if r else None


# `& Agg-All    - from:     1.327282 to:     1.319809 in 3D over  2976 entries`
#
# ⚠️ 这是**实测**的格式（`compare.cpp:1017` 的 `tracepdeex` 打出来的），
#    不是照规范推的。字段之间是**空格对齐**的，所以用 `\s+` 而不是定宽切。
_ROW = re.compile(
    r"^&\s+(\S+)\s+-\s+from:\s*([-\d.]+)\s+to:\s*([-\d.]+)"
    r"\s+in\s+([A-Z0-9]+)\s+over\s+(\d+)\s+entries",
    re.MULTILINE,
)

# 汇总行的名字。实测有两个，内容一样：
#     `Agg-All`  —— 两套轨道里**都有**的卫星
#     `Agg-Used` —— 进了最小约束解的那些
# ⚠️ 取 `Agg-All` 是因为它更全（`Agg-Used` 可能少几颗）。
_AGG_NAMES = ("Agg-All", "Agg-Used")


def parse_comparison(stdout: str) -> OrbitComparison:
    """解析 `compare_orbits` 打在 stdout 上的 `MINCON STATISTICS` 块。

    ⚠️ **只认 `/TOTAL` 那一块。** 逐历元还有 `/2019-07-18 00:00:00` 那种块，
       它们的前缀也是 `MINCON STATISTICS` —— 一起收进来的话，
       逐星的那些行会被后面某个历元的数覆盖掉，而**看不出错**。

    ⚠️ 永不抛异常：解析不出任何一行就返回一份空的（`epochs == 0`）。
    """
    out = OrbitComparison()

    # ---- 先截出 `/TOTAL` 那一块 ----
    #
    # 实测的形状（`minimumConstraints.cpp:996` 的 `Block`）：
    #
    #     +MINCON STATISTICS/TOTAL
    #     # Position RMS changed in meters:
    #     & Agg-All  - from: … to: … in 3D over …
    #     …
    #     -MINCON STATISTICS/TOTAL
    start = stdout.find("+MINCON STATISTICS/TOTAL")
    if start < 0:
        return out
    end = stdout.find("-MINCON STATISTICS/TOTAL", start)
    block = stdout[start : end if end > 0 else len(stdout)]

    # ---- 文件名（`Comparing files:` 后面那两行）----
    #
    # ⚠️ 这一段在 `/TOTAL` 块**外面**（`compare.cpp:64` 是每个文件对开头就打的），
    #    所以要**在整份 stdout 里找**，不是在截出来的块里找。
    cf = stdout.find("Comparing files:")
    if cf >= 0:
        tail = stdout[cf:].splitlines()[1:3]
        if len(tail) == 2:
            out.files = (tail[0].strip(), tail[1].strip())

    for m in _ROW.finditer(block):
        name, before, after, comp, entries = m.groups()
        rms = Rms(float(before), float(after), int(entries))
        if name in _AGG_NAMES:
            # ⚠️ 先到先得 —— `Agg-All` 排在 `Agg-Used` 前面，别让后者盖掉前者。
            out.agg.setdefault(comp, rms)
        else:
            out.per_sat[name] = rms

    return out


# =============================================================================
# 人话结论
# =============================================================================

# 差异分级的阈值（米，Helmert 转换**之后**的 3D RMS）。
#
# ⚠️ 这些是**领域经验值**，标的是"两条轨道差多少"，不是"谁更准"：
#
#     0.05 米以下  —— 两条精密轨道之间的正常差异（IGS 各家产品之间就是这个量级）
#     0.5 米以下   —— 很接近，通常一方是另一方的解算结果
#     5 米以下     —— 米级。广播星历 vs 精密轨道就在这一档（实测 1.32 米）
#     5 米以上     —— 大了。要么两边不是同一天的，要么有一方解算发散
LEVELS = [
    (0.05, "good", "同一水平"),
    (0.50, "good", "非常接近"),
    (5.00, "fair", "米级差异"),
    (float("inf"), "poor", "差异很大"),
]


def verdict(cmp: OrbitComparison) -> Verdict:
    """给一次轨道比对下结论。

    ⚠️ 措辞上守着 `posfile.verdict` 那条原则：**说清楚不确定性**。

       不要写 "轨道精度 1.3 米"（听起来像在说"我们算得准"），
       要写 "两条轨道相差约 1.3 米" —— 因为这只是**两条轨道的差异**，
       "谁更准"这个比法**答不出来**（两条都可能是错的，只是错得一样）。
    """
    if cmp.epochs == 0 or cmp.rms_3d is None:
        return Verdict(
            headline="没能从比对结果里读出数据",
            quality="unknown",
            details=["`compare_orbits` 没有输出可解析的统计块。"],
            caveats=[
                "可能是两份 SP3 里**没有任何重叠的卫星或历元** —— "
                "比如它们不是同一天、或者星座不重合。"
            ],
        )

    rms = cmp.rms_3d
    quality, label = "unknown", ""
    for limit, q, lab in LEVELS:
        if rms < limit:
            quality, label = q, lab
            break

    headline = f"两条轨道相差约 {rms:.2f} 米（{label}）"

    details = [
        f"比的是 {cmp.satellites} 颗卫星、{cmp.epochs} 个公共历元",
        f"扣掉整体差异前是 {cmp.agg['3D'].before:.2f} 米 —— "
        f"两者的差主要是坐标系/基准的系统差，不是轨道形状的差",
    ]
    for comp, name in (("N", "切向"), ("R", "径向"), ("T", "法向")):
        r = cmp.agg.get(comp)
        if r:
            details.append(f"{comp}（{name}方向）{r.after:.2f} 米")

    caveats: list[str] = []

    # ⚠️⚠️ **先拦"公共历元太少"这一种 —— 它有一个具体的已知原因。**
    #
    #     实测撞到过：产物 SP3 的历元标签被 CRLF 缺陷写成 `E*`，
    #     Ginan 只读到第 1 个历元 ⇒ 这里就会看到 `1 个历元` 加一个巨大的数。
    #     不说的话，用户会以为"我的轨道差了 1800 公里"。
    if cmp.epochs <= 1:
        headline = "两条轨道几乎没有重叠的历元，这个比对**没有意义**"
        quality = "unknown"
        caveats.append(
            "⚠️ 最常见的原因是**产物 SP3 的历元标签坏了**："
            "Ginan 在 Windows 上把第 2 个历元起的行首写成 `E*` 而不是 `*`，"
            "于是它自己只读得到一个历元。先用修复功能处理那份产物再比。"
        )
        return Verdict(
            headline=headline, quality=quality, details=details, caveats=caveats
        )

    if rms > 5.0:
        caveats.append(
            "差异这么大时，先确认两件事：两份文件是**同一天**的吗？"
            "其中一份是**解算发散**时产出的吗？"
        )
    else:
        caveats.append(
            "⚠️ 这是**两条轨道的差异**，不是任何一方的精度 —— "
            "两条都可能有同样的偏差，而那样比出来会很接近。"
        )

    return Verdict(headline=headline, quality=quality, details=details, caveats=caveats)
