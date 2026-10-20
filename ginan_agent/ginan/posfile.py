"""Ginan 领域层 —— 解析 .POS 定位结果，并给出人话结论。

============================ 这个模块为什么重要 ============================
它是**产品差异化的落点**。

GinanUI 的现状是：跑完之后给用户一堆 `.pos` 文件和 Plotly 图，
然后**什么都不说**。用户得自己知道"什么样的残差算好"、
"收敛了没有"、"这个精度正常不正常"。

而我们的用户是**完全不懂 GNSS 的人**。给他一个数字表格等于没给。

所以这里的重点不是"解析"，而是**结论**：
    不是 "RMS(N)=0.0123 m"
    而是 "定位精度约 1.2 厘米，对静态测量来说这是正常水平。"

============================ .POS 的格式 ============================
PBO Station Position Time Series（Ginan 用的就是它）。

    开头若干行说明 + 一行以 `*` 打头的列名
    之后每行一个历元，**24 个空白分隔的字段**

⚠️ 两个容易踩的地方：
   ① 数据行**前面有一个空格** —— 用 `line.startswith("2026")` 判断会漏掉全部数据
   ② 字段要**按位置取**（这是定宽格式的变体），不能按名字

字段顺序（实测确认）：
    0 时间   1 十进制年
    2-4  X Y Z           5-7  Sx Sy Sz        8-10 Rxy Rxz Ryz
    11-13 纬度 经度 高程
    14-16 dN dE dU       17-19 Sn Se Su       20-22 Rne Rnu Reu
    23 "ginan"
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 收敛判据：连续多少个历元的解稳定在阈值内，才算收敛。
#
# ⚠️ 这两个数不是随便定的：
#   · PPP 的模糊度通常要 15~30 分钟才收敛（取决于卫星几何）
#   · 30 秒采样下 20 个历元 = 10 分钟，是"开始稳定"的量级
#   · 阈值 10 厘米是"厘米级定位"的粗线 —— 定得太严会永远判不收敛
CONVERGE_EPOCHS = 20
CONVERGE_THRESHOLD_M = 0.10

# 「一份位置时间序列至少得有几个不同的位置」。
#
# ⚠️ 这是 `repeatability_is_meaningful()` 的**绝对下限**，配合"2% 比率"一起用。
#    为什么不能只用比率：历元少的时候 2% 不足一个位置，判据就失去分辨力
#    （60 个历元时 1 个位置算 1.7%，能拦住；40 个历元时同一个毛病算 2.5%，拦不住）。
#
# ⚠️ 取 3 的理由：1 个位置是常数，2 个位置是一步阶跃，都不构成"序列"。
#    实测前向解最低也有 **33.8%**（整天）—— 离 3 差着两个数量级，不会误伤。
MIN_DISTINCT_POSITIONS = 3


@dataclass
class PosEpoch:
    """一个历元的定位结果。"""

    time: datetime
    x: float
    y: float
    z: float
    lat: float
    lon: float
    height: float
    dn: float
    de: float
    du: float
    sn: float
    se: float
    su: float

    @property
    def horizontal(self) -> float:
        """水平方向偏差（N/E 合成）。"""
        return math.hypot(self.dn, self.de)

    @property
    def three_d(self) -> float:
        """三维偏差。"""
        return math.sqrt(self.dn ** 2 + self.de ** 2 + self.du ** 2)


@dataclass
class PosFile:
    """一份 .POS 文件的全部内容。"""

    station: str = ""
    reference_xyz: tuple[float, float, float] | None = None
    epochs: list[PosEpoch] = field(default_factory=list)
    origin: Path | None = None

    # "forward" / "smoothed" / "unknown"
    #
    # ⚠️ 按**文件名**判定，不是按内容猜的。
    #    这是 Ginan 的对外契约：它的常量 `SMOOTHED_SUFFIX = "_smoothed"`
    #    （src/cpp/common/constants.hpp:40）由 `insertSuffix`
    #    （src/cpp/pea/outputs.cpp:220-233）**插在扩展名之前**，
    #    所以产物是 `<RECEIVER>_<CONFIG>.POS` 和
    #    `<RECEIVER>_<CONFIG>_smoothed.POS` 两个文件。
    solution: str = "unknown"

    @property
    def count(self) -> int:
        return len(self.epochs)

    # -------------------------------------------------------------------------
    # 这份解算的形态
    # -------------------------------------------------------------------------

    def distinct_positions(self) -> int:
        """有多少个**不同的**位置（四舍五入到毫米）。

        ⚠️ 用来识别"整天被压成了一个点"的产物。
           实测（ALIC 2022-03-01 整天，2880 历元，`userdata/runs/` 里那几份）：
               前向解    974 个不同位置（33.8%）
               平滑解      2 个不同位置（是**两段常数**，不是常数）

           ⚠️ 原来这里写的是「前向解 2864 / 平滑解 7」——那是**另一次运行**
              （2026-09-02）的数，而那份产物**磁盘上已经没有了**。
              两个数现在都无法复核。上面这组是能复现的。

        ⚠️ 用 N/E/U 三列，不用 X/Y/Z —— 它们信息等价（刚体变换），
           但**这个模块里所有的指标都基于 N/E/U**（重复性、rms、收敛）。
           跟它们保持一致，就不会出现"看这个说它在动、看那个说它不动"。
        """
        seen = {(round(e.dn, 3), round(e.de, 3), round(e.du, 3))
                for e in self.epochs}
        return len(seen)

    def repeatability_is_meaningful(self) -> bool:
        """「重复性」这个指标对这份文件**有没有意义**。

        ============================ ⚠️ 为什么需要这个判断 ============================
        RTS 反向平滑对**静态站**会让位置长时间停在同一个值上，
        于是"相对自身平均位置的离散程度"退化成一个**常数器的产物** ——
        它不是"特别准"，而是**这个指标在这份文件上根本没定义**。

        ⚠️⚠️ 而且它**不稳定**，这条比"等于 0"更要命。
           实测同一个站同一天（ALIC 2022-03-01）的三次运行，
           平滑解算出来的"重复性"是 **0.0 / 2.7 / 2.9 厘米** ——
           差别只来自"那天被切成了几段常数"，
           以及 `converged_from()` 恰好从哪一段开始取。
           **一个会在 0.0 和 2.9 之间跳的数，不是指标。**

        ⚠️ 它还会和前向解的数**并排显示** ——
           两个矛盾的结论摆在一起，**没意义的那个看起来更好**。

        ============================ 判据 ============================
        通用的（"不同位置数相对历元数太少"），**不是** `if 文件名带 _smoothed`。
        任何被压成常数的序列都该被拦下，不管它从哪来 ——
        RTS 只是目前已知的那一个成因。

        实测（ALIC 2022-03-01，四份真跑出来的文件）：

            文件                  历元   不同位置   占比
            前向解（整天）        2880      974     33.8%
            前向解（半小时）        60       60    100%
            平滑解（整天）        2880        2      0.07%
            平滑解（半小时）        60        1       1.7%

        ⚠️ 2% 这条线**两边都有很大余量**（0.07% ←→ 33.8%），不是卡着边界定的。

        ⚠️⚠️ 但**光有比率不够** —— 历元少的时候 2% 不足一个位置，
           比率检验就没有分辨力了：
               60 个历元 × 2% = 1.2，"只有 1 个位置"是 1 > 1.2 = **假**；
               可 40 个历元 × 2% = 0.8，同一个毛病就变成 1 > 0.8 = **真**，漏掉。
           所以再加一条**绝对下限**：位置时间序列至少得有 3 个不同的位置。

        ⚠️⚠️ 这里原来写的是 `if self.count < 100: return True` ——
           它想解决的正是"小样本不可判"这同一个问题，
           但取的是"**放过**"：
           于是**半小时那份平滑解**（60 历元、1 个不同的位置）
           照样被报成「重复性约 0.0 厘米（厘米级）· 质量 good」——
           就是本函数要拦下的那个东西。**实测复现过。**
        """
        if self.count < 4:
            # 少于 4 个历元谈不上"序列"，什么结论都下不了。
            # ⚠️ 这一条**不是**原来那个 `< 100` —— 那个把 60 历元的也一起放过了。
            #    这里只放过"根本不成序列"的，剩下的交给下面的判据。
            #    文件太短本来就有专门的提示（时长那条 caveat）。
            return True
        return self.distinct_positions() > max(
            MIN_DISTINCT_POSITIONS, self.count * 0.02
        )

    # -------------------------------------------------------------------------
    # 统计
    # -------------------------------------------------------------------------

    def self_reference(self) -> tuple[float, float, float]:
        """拿"时间序列末尾的中位数"当自己的参考位置。

        ⚠️⚠️ **为什么不能用 .POS 表头那个 Reference position。**

           那个位置是**观测文件头里写的近似坐标**，可能是很多年前的旧值。

           实测（2026-09-19，ALIC 2026-09-02）：

               ALIC 每年移动 6.7 厘米，而文件头里的坐标是 2020 年前后的 ——
               到观测日已经过了 6.67 年，参考位置**差了 45 厘米**。

               后果不是"数字有点偏"，而是**连锁的**：

                 · `converged_from()` 永远判不收敛
                   （偏差恒大于 10 厘米的阈值）→ 全部历元都被当成未收敛
                 · `rms()` 被那 45 厘米**整体平移**主导
                 · 结论报出"定位偏差 45.8 厘米（米级水平）"
                   **而那 45 厘米几乎全是参考位置自己的错** ——
                   拿 IGc20 权威坐标一比，解算本身只差几厘米

               👉 **一份几厘米的解算，被报成了 46 厘米。**

               ⚠️ 那个"几厘米"**取决于时段有多长**，不要说死：
                  实测同一套配置，5 小时的数据是 3.0 厘米，
                  整天 24 小时是 8.3 厘米（都是离权威坐标）。
                  **先看条件，再报数字。**

           ⚠️ 而"整体平移"正是这个错误的特点：它污染"偏差"，
              **不影响"离散程度"**。所以要判定收敛和重复性，
              就得拿**解算自己的**位置作参考。

        ⚠️ 为什么取**末尾 25% 的中位数**而不是平均值或最后一个历元：

           · 末尾 —— 那是最可能已经收敛的部分
           · 25%  —— 样本够多，中位数才稳
           · 中位数 —— 不受个别跳点影响（平均值会被拉偏）
        """
        n = self.count
        if n == 0:
            return (0.0, 0.0, 0.0)

        tail = self.epochs[max(0, n - max(10, n // 4)):]

        def median(vals: list[float]) -> float:
            s = sorted(vals)
            m = len(s) // 2
            return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2

        return (
            median([e.dn for e in tail]),
            median([e.de for e in tail]),
            median([e.du for e in tail]),
        )

    def _dev_from_self(self, e: PosEpoch, ref: tuple[float, float, float]) -> float:
        """这个历元离**自身参考位置**有多远（三维，米）。"""
        return math.sqrt(
            (e.dn - ref[0]) ** 2 + (e.de - ref[1]) ** 2 + (e.du - ref[2]) ** 2
        )

    def converged_from(self) -> int:
        """算出从第几个历元开始收敛了。返回索引（没收敛就是 -1）。

        ⚠️ 为什么要单独算"收敛"？

           因为**未收敛的历元会严重拉高 RMS**。
           PPP 刚启动时位置偏差可能有几米，跑半小时后稳定到几厘米。
           如果把全部历元一起算 RMS，得到的是"平均下来一米" ——
           一个既不反映起始状态、也不反映最终精度的**没有意义的数字**。

           GinanUI 没做这一步，所以它给的 RMS 多半是偏大的。

        判据：从某个历元起，**连续 CONVERGE_EPOCHS 个历元**的三维偏差
              都在 CONVERGE_THRESHOLD_M 以内。

        ⚠️⚠️ 偏差是**相对自身参考位置**算的，不是相对文件里那个 ——
           理由见 `self_reference()`。用文件里那个的话，
           参考位置过期多少，就有多少历元被判成"没收敛"。
        """
        n = self.count
        if n < CONVERGE_EPOCHS:
            return -1

        ref = self.self_reference()
        for i in range(0, n - CONVERGE_EPOCHS + 1):
            window = self.epochs[i:i + CONVERGE_EPOCHS]
            if all(self._dev_from_self(e, ref) <= CONVERGE_THRESHOLD_M
                   for e in window):
                return i
        return -1

    def converged_epochs(self) -> list[PosEpoch]:
        """收敛之后的历元。没判出收敛就返回全部。"""
        i = self.converged_from()
        return self.epochs[i:] if i >= 0 else list(self.epochs)

    def repeatability(self) -> dict[str, float]:
        """收敛之后，相对**解算自身位置**的离散程度（N/E/U）。

        ⚠️⚠️ 这是那个**不依赖参考位置**的指标，结论应该以它为主。

           参考位置过期只造成**整体平移** —— 平移污染"偏差"，
           但**不影响"离散程度"**。所以重复性反映的才是解算自身稳不稳。

           实测（ALIC 2026-09-02 整天 2880 历元）：
               离文件里的参考位置 约 45 厘米（那是 2020 年的旧坐标）
               离权威 IGc20 坐标   8.3 厘米（开了固体潮是 4.0）
               而重复性            3.3 厘米（开了固体潮是 2.8）

           ⚠️ 注意"离参考位置 45 厘米"和"离权威坐标 8 厘米"是**同一次解算** ——
              差的那 40 厘米是**参考位置自己的错**，不是解算的错。

           ⚠️ **三个数字说的是三件事**，普通用户分不清，所以措辞要小心：

               重复性    —— "这次解稳不稳"        ← 程序能算准
               偏移      —— "离文件里那个位置多远" ← 参考位置可能是旧的
               绝对精度  —— "离真实坐标多近"       ← **程序不知道**（除非有权威坐标）
        """
        eps = self.converged_epochs()
        if not eps:
            return {"n": 0.0, "e": 0.0, "u": 0.0, "3d": 0.0, "count": 0}

        def scatter(f) -> float:
            vals = [f(e) for e in eps]
            mean = sum(vals) / len(vals)
            return math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))

        rn = scatter(lambda e: e.dn)
        re = scatter(lambda e: e.de)
        ru = scatter(lambda e: e.du)
        return {
            "n": rn, "e": re, "u": ru,
            "3d": math.sqrt(rn ** 2 + re ** 2 + ru ** 2),
            "count": len(eps),
        }

    def rms(self) -> dict[str, float]:
        """收敛之后的 RMS（N/E/U 三个方向）。

        ⚠️ 口径是"相对 .POS 表头那个 Reference position" ——
           也就是**观测文件头里写的近似坐标**。

        ⚠️⚠️ **它可能是很旧的**（实测 ALIC 那份是 2020 年前后的，
           而观测日是 2026 年 —— 站每年移动 6.7 厘米，差了 45 厘米）。

           所以这个数字**不能当成精度**：参考位置过期多少，
           它就虚高多少。真实精度得靠外部权威坐标，或者看 `repeatability()`。
        """
        eps = self.converged_epochs()
        if not eps:
            return {"n": 0.0, "e": 0.0, "u": 0.0, "3d": 0.0, "count": 0}

        def rms3(f) -> float:
            return math.sqrt(sum(f(e) ** 2 for e in eps) / len(eps))

        rn = rms3(lambda e: e.dn)
        re = rms3(lambda e: e.de)
        ru = rms3(lambda e: e.du)
        return {
            "n": rn, "e": re, "u": ru,
            "3d": math.sqrt(rn ** 2 + re ** 2 + ru ** 2),
            "count": len(eps),
        }

    def duration_minutes(self) -> float:
        if self.count < 2:
            return 0.0
        return (self.epochs[-1].time - self.epochs[0].time).total_seconds() / 60

    def sampling_interval_seconds(self) -> float:
        """采样间隔（秒）—— 取前几个历元间隔的**中位数**。

        ⚠️ 为什么不能写死 30 秒：本程序里有 **1 Hz** 的工作流
           （`rtk_example`，实测 5 分钟 / 300 历元）。
           写死 30 秒的话，"第 20 个历元"会被报成 10 分钟，
           而它其实是 **20 秒** —— 差 30 倍。

        ⚠️ 取中位数、且只看前 50 个：中位数不受个别跳点影响；
           而"收敛"是开头那一段的事，用开头那段估间隔最贴近。
        """
        if self.count < 2:
            return 0.0
        deltas = [
            (self.epochs[i + 1].time - self.epochs[i].time).total_seconds()
            for i in range(min(50, self.count - 1))
        ]
        if not deltas:
            return 0.0
        return statistics.median(deltas)

    def solution_rate(self) -> float:
        """有解的比例。

        ⚠️ 这个数怎么算的要说清楚：
           我们**只知道有解的历元**（没解的历元根本不会出现在 .POS 里）。
           所以用"时间跨度 ÷ 采样间隔"估算应该有多个历元，再和实际数量比。

           这是个估算 —— 结论里不能说得太肯定。
        """
        if self.count < 2:
            return 1.0
        span = (self.epochs[-1].time - self.epochs[0].time).total_seconds()
        interval = self.sampling_interval_seconds()
        if interval <= 0:
            return 1.0
        expected = span / interval + 1
        return min(1.0, self.count / expected)


# =============================================================================
# 解析
# =============================================================================


def parse(path: Path) -> PosFile:
    """读一份 .POS 文件。

    ⚠️ **永不抛异常** —— 返回一个可能为空的 PosFile。
       因为结果文件可能损坏、可能只写了半截（用户中途取消），
       而"读不出来"不该让整个流程崩掉。
    """
    path = Path(path)

    # ⚠️ 按文件名判定"这份是前向解还是平滑解" —— 见 PosFile.solution 的说明。
    #    `_smoothed` 插在扩展名**之前**，所以判据是**去掉扩展名的词干**结尾。
    stem = path.stem.lower()
    solution = "smoothed" if stem.endswith("_smoothed") else "forward"

    result = PosFile(origin=path, solution=solution)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    for line in text.splitlines():
        # ---- 表头信息 ----
        if "4-character ID" in line:
            parts = line.split(":")
            if len(parts) > 1:
                result.station = parts[1].strip()
            continue

        if "XYZ Reference position" in line:
            nums = _numbers(line)
            if len(nums) >= 3:
                result.reference_xyz = (nums[0], nums[1], nums[2])
            continue

        # 表头行、说明行、空行都跳过
        stripped = line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("PBO"):
            continue
        if stripped[0] not in "0123456789-":     # 数据行以日期开头
            continue

        parts = stripped.split()
        if len(parts) < 24:
            continue

        try:
            result.epochs.append(
                PosEpoch(
                    time=datetime.strptime(parts[0][:19], "%Y-%m-%dT%H:%M:%S"),
                    x=float(parts[2]), y=float(parts[3]), z=float(parts[4]),
                    lat=float(parts[11]), lon=float(parts[12]),
                    height=float(parts[13]),
                    dn=float(parts[14]), de=float(parts[15]), du=float(parts[16]),
                    sn=float(parts[17]), se=float(parts[18]), su=float(parts[19]),
                )
            )
        except (ValueError, IndexError):
            # ⚠️ 单行坏掉就跳过，不要让整个文件读不出来。
            #    半个结果也比没有强 —— 用户至少能看到收敛过程。
            continue

    return result


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for tok in text.replace(":", " ").split():
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


# =============================================================================
# 结论 —— 这个模块最重要的部分
# =============================================================================


@dataclass
class Verdict:
    """人话结论。"""

    headline: str           # 一句话结论
    quality: str            # good / fair / poor / unknown
    details: list[str] = field(default_factory=list)   # 补充说明
    caveats: list[str] = field(default_factory=list)   # 需要说明的局限

    @property
    def icon(self) -> str:
        return {"good": "ok", "fair": "warn", "poor": "fail"}.get(self.quality, "info")


# 精度分级的阈值（米）。
#
# ⚠️ 这些是**领域经验值**，来自静态 PPP 的常见水平：
#    厘米级  —— 数据质量好、收敛充分（正常水平）
#    分米级  —— 数据较短、或有系统误差
#    米级    —— 没收敛，或者数据有问题
#
# ⚠️ 但"正常"取决于**观测时长**。10 分钟的数据做到厘米级是运气，
#    24 小时的数据做到米级是失败。所以结论里必须带上时长。
LEVELS = [
    (0.03, "good", "厘米级"),
    (0.20, "fair", "分米级"),
    (float("inf"), "poor", "米级"),
]

# 「短到**不该判好坏**」的时长下限（分钟）。
#
# ⚠️ 这不是"再短就不好"的意思 —— 是**短到这个程度，上面那张表根本不适用**。
#
#    PPP 的模糊度要 15~30 分钟才收敛，所以十几分钟的解**必然**是米级：
#    那个米级反映的是"还没收敛"，不是"这次解得差"。
#    分级表却会把这种解报成「质量 poor」——
#    而 `quality` 正是界面上的颜色和图标（`Verdict.icon`），
#    也是模型"直接转述给用户"的那句话。
#
#    ⚠️ 实测（`ginan-testdata-*/cheap3/ppp_span/ALIC_pppspan.POS`，
#       真跑出来的 30 个历元 / 14 分钟）：结论是
#           「重复性约 95.2 厘米（米级）」· quality = poor
#       ——对一份只有 14 分钟的数据，这句话把"数据不够"说成了"解得差"。
#
#    这和"平滑解的重复性没定义"（见 `repeatability_is_meaningful`）是
#    **同一类问题**：指标在这份数据上不成立，那就别报它 —— 待遇也一样
#    （`quality = "unknown"`，界面上是中性图标而不是红叉）。
#
# ⚠️ 取 30 分钟的出处：和下面那条时长 caveat、以及收敛那句提示是**同一个数**
#    （"PPP 一般需要 30 分钟以上才能收敛"）。**三处必须是同一个数** ——
#    所以这里是唯一的定义处。
MIN_GRADABLE_MINUTES = 30.0


def verdict(pf: PosFile) -> Verdict:
    """给一份定位结果下结论。

    ⚠️ 写这个函数时的原则：**说清楚不确定性**。

       不要写 "精度 1.2 厘米"（听起来像保证），
       要写 "相对参考位置的偏差约 1.2 厘米"。

       普通用户分不清"绝对精度"和"相对参考位置的重复性"，
       而我们也不该假装我们知道得更多。
    """
    if pf.count == 0:
        return Verdict(
            headline="没有解算结果",
            quality="unknown",
            details=["结果文件里没有有效的历元数据。"],
            caveats=["可能是处理中断了，或者观测数据不足以解算。"],
        )

    # =========================================================================
    # ⚠️⚠️ 先把"重复性没意义"这一种拦下来，再走原来的路
    # =========================================================================
    #
    #  RTS 平滑会把位置压成**常数**（实测：不是"一个位置"，是**两段常数** ——
    #  整天那份 2880 个历元里只有 2 个不同的位置，分界在 03:47 前后）。
    #  于是"相对自身平均位置的离散程度"退化成一个**常数器的产物**。
    #
    #  ⚠️⚠️ 而它**不是恒等于 0** —— 实测同一个站同一天三次运行，
    #     算出来是 **0.0 / 2.7 / 2.9 厘米**，差别只来自"那天被切成几段常数"
    #     和 `converged_from()` 从哪一段开始取。
    #     **一个会在 0.0 和 2.9 之间跳的数，不是指标。**
    #
    #  ⚠️ 这段注释原来写的是「收敛成**同一个位置**，于是离散程度恒为 0」——
    #     **两个说法后来都被实测推翻了**（见下面 caveats 里那段纠正文）。
    #     留着它会让下一个读代码的人按错的前提去改这里。
    #
    #  照常报的话，用户看到的是"重复性约 0.0 厘米（厘米级）· 质量 good" ——
    #  一句**听着像满分、实际什么都没说**的话。
    #
    #  ⚠️ 而且它还会和前向解的 6.0 厘米**并排出现**，两个结论互相矛盾，
    #     而**没意义的那个看起来更好**。
    if not pf.repeatability_is_meaningful():
        n = pf.distinct_positions()
        eps = pf.epochs
        cx = sum(e.x for e in eps) / len(eps)
        cy = sum(e.y for e in eps) / len(eps)
        cz = sum(e.z for e in eps) / len(eps)
        offset3d = None
        if pf.reference_xyz:
            dx, dy, dz = (cx - pf.reference_xyz[0], cy - pf.reference_xyz[1],
                          cz - pf.reference_xyz[2])
            offset3d = math.sqrt(dx * dx + dy * dy + dz * dz)

        details = [
            f"这份是平滑解（文件名末尾是 _smoothed）。",
            f"{pf.count} 个历元里只有 {n} 个不同的位置 —— "
            "绝大多数历元写着同一个坐标。",
        ]
        if offset3d is not None:
            details.append(
                f"与观测文件头里那个参考位置相差 {offset3d * 100:.1f} 厘米。"
            )
        return Verdict(
            headline="平滑解：位置被压成了常数",
            quality="unknown",
            details=details,
            caveats=[
                # ⚠️⚠️ 这里**不能写死一个数**。
                #
                #    原来写的是「这里的「重复性」**恒等于 0**」——
                #    而实测三份整天平滑解，两份算出来是 **2.7 / 2.9 厘米**，
                #    一份是 0.0。差别只来自"那天被切成几段常数"和
                #    `converged_from()` 从哪段开始取。
                #    写死的那个 0 对两份文件是**假话**，
                #    而这句假话正是用户读到的关于"0.0"的解释。
                #
                #    改成说清**这个数是什么**，不说它等于几 ——
                #    这样无论它算出 0.0 还是 2.9，这句话都是对的。
                "⚠️ 这份文件里的「重复性」不是精度 —— "
                "它只反映「平滑解吐出了几个不同的位置」，不反映定位好坏。"
                "实测同一个站同一天的三次运行，它算出来是 0.0 / 2.7 / 2.9 厘米 —— "
                "它甚至不是个稳定的数，所以别拿它比大小。",
                "平滑解通常比前向解更接近真实位置（它用了全天数据回头修），"
                "但「更准多少」要靠权威坐标去比，程序自己不知道。",
                "想知道这次解算稳不稳，请看前向解那一份的重复性。",
            ],
        )

    rms = pf.rms()             # 相对**文件里那个**参考位置（可能过期）
    rep = pf.repeatability()   # 相对**解算自身**（不依赖参考位置）
    n3d = rep["3d"]
    offset3d = rms["3d"]
    minutes = pf.duration_minutes()
    conv = pf.converged_from()

    # ⚠️ 分级用**重复性**，不用"离参考位置多远" ——
    #    后者会被过期的参考位置整体拉高（实测：一份 3 厘米的解算
    #    因为参考位置是 6 年前的旧值，被算成 45 厘米）。
    for threshold, quality, label in LEVELS:
        if n3d < threshold:
            break

    # ⚠️⚠️ **太短的解算不参与分级** —— 见 `MIN_GRADABLE_MINUTES` 里那段。
    #
    #    这里要**先**把它判成 unknown，再往下走：下面收敛那段会在
    #    "没收敛"时把 good 降成 poor，那条逻辑对"本来就没分级"的应该无害。
    too_short = minutes < MIN_GRADABLE_MINUTES
    if too_short:
        quality = "unknown"

    details = [
        f"有效解算 {pf.count} 个历元，时间跨度 {minutes:.0f} 分钟。",
        f"重复性 {n3d * 100:.1f} 厘米"
        f"（水平 {math.hypot(rep['n'], rep['e']) * 100:.1f}，"
        f"高程 {rep['u'] * 100:.1f}）—— 这是相对解算自身平均位置的离散程度。",
    ]

    caveats: list[str] = []

    # ---- 收敛情况 ----
    #
    # ⚠️⚠️ `converged_from()` 返回 -1 有**两种完全不同的原因**，
    #    而它们要说的话不一样（这里原来只描述了一种，于是另一种成了假话）：
    #
    #      ① 历元数 < CONVERGE_EPOCHS —— **根本没去判断**
    #         实测踩过：`ALIC_g1_o4.POS` 只有 8 个历元，走的就是这条，
    #         却被告知"误差一直在 10 厘米以上波动" —— 我们压根没看过。
    #      ② 真的没找到连续 CONVERGE_EPOCHS 个历元稳定在阈值内
    if conv < 0:
        if pf.count < CONVERGE_EPOCHS:
            caveats.append(
                f"数据太少，还不够判断有没有收敛 —— "
                f"至少要 {CONVERGE_EPOCHS} 个历元才谈得上「连续稳定」。"
            )
        else:
            caveats.append(
                f"这段时间里没找到连续 {CONVERGE_EPOCHS} 个历元稳定在 "
                f"{CONVERGE_THRESHOLD_M*100:.0f} 厘米以内 —— "
                f"通常是数据还没稳下来，PPP 一般需要 "
                f"{MIN_GRADABLE_MINUTES:.0f} 分钟以上才能收敛。"
            )
            # ⚠️ 只有**真的判过**并且没通过，才把 good 降级。
            #    第 ① 种情况我们什么都没测出来，降级等于凭空扣分。
            quality = "poor" if quality == "good" else quality
    else:
        # ⚠️ "第几个历元"要换算成分钟，而**间隔必须从数据里量** ——
        #    写死 30 秒的话，1 Hz 的数据会被报成 30 倍的时间。
        step = pf.sampling_interval_seconds()
        conv_sec = conv * step if step > 0 else 0.0
        conv_min = conv_sec / 60
        if conv_sec < 60:
            conv_text = f"{conv_sec:.0f} 秒"
        else:
            conv_text = f"{conv_min:.0f} 分钟"
        details.append(
            f"大约从第 {conv} 个历元（约 {conv_text}）开始稳定。"
        )

    # ---- 时长 ----
    #
    # ⚠️ 时长是**解读精度的前提**。同样的数字，不同时长含义完全不同。
    # ⚠️ 时长怎么写要看数据量：只有 1 个历元时 `duration_minutes()` 按定义是 0，
    #    写成"只有 0 分钟"读起来像程序算错了。
    if pf.count < 2:
        span_text = f"{pf.count} 个历元"
    elif minutes < 1:
        span_text = "不到 1 分钟"
    else:
        span_text = f"{minutes:.0f} 分钟"

    if too_short:
        caveats.append(
            f"数据太短（只有 {span_text}），所以这次不判断精度好坏 —— "
            f"PPP 要 {MIN_GRADABLE_MINUTES:.0f} 分钟以上才收敛，"
            "比这更短的数据算出来的数字反映的是「还没稳下来」，不是「解得差」。"
        )
        caveats.append(
            "时间越长结果越可靠 —— 要得到可用于科研的精度，建议至少跑一整天。"
        )

    # ---- 数据完整性 ----
    rate = pf.solution_rate()
    if rate < 0.9:
        details.append(f"约有 {rate*100:.0f}% 的历元解算成功。")
        caveats.append(
            "有些历元没有解 —— 可能是卫星信号被遮挡，或者数据有中断。"
        )

    # ---- ⚠️ 相对文件里那个参考位置的偏移 ----
    #
    # ⚠️⚠️ **它不是精度**，而且很容易误导 —— 所以单独说、并且说清楚。
    #
    #    实测：ALIC 那份解算离文件里的参考位置约 45 厘米，
    #    而离 IGc20 权威坐标只有几厘米 ——
    #    因为文件头里那个坐标是**6 年前的**，而站每年移动 6.7 厘米。
    details.append(
        f"与观测文件头里那个参考位置相差 {offset3d * 100:.1f} 厘米。"
    )
    caveats.append(
        "⚠️ 上面那个「与文件里的参考位置相差」，不是精度 —— "
        "文件头里的近似坐标可能是很多年前的，测站每年都在移动。"
        "想知道真实精度，要用权威坐标（比如 IGS 的 SINEX 解）去比。"
    )

    # ⚠️ 太短的时候**不能把那个分级词写进结论** —— 见 `MIN_GRADABLE_MINUTES`。
    #    「重复性约 95.2 厘米（米级）」对一份 14 分钟的数据是句错话：
    #    它把"还没收敛"说成了"解得差"。而 headline 正是模型
    #    "直接转述给用户"的那一句，所以错在这里代价最大。
    if too_short:
        headline = f"数据太短（{span_text}），还判不出精度"
    else:
        headline = f"重复性约 {n3d * 100:.1f} 厘米（{label}）"

    return Verdict(
        headline=headline,
        quality=quality,
        details=details,
        caveats=caveats,
    )


def plot_series(pf: PosFile, *, max_points: int = 600) -> dict:
    """给界面**画图**用的序列：时间轴 + N/E/U 三条（单位厘米）。

    ============================ ⚠️ 参考系选的是哪一个 ============================
    用 `self_reference()`（末尾 25% 的中位数），**不是**文件头那个参考位置。

    ⚠️ 这不是随手挑的，两个参考系画出来的图是**两回事**：

        用文件头的参考位置 → 整条曲线被抬高几十厘米（实测 ALIC 那份差 45 厘米，
                             因为文件头里的坐标是 6 年前的），
                             图上看不出"收敛"，只看到一条挂在半空的线
        用解算自身的位置   → 曲线从"偏离最终位置 1 米"降下来、十几分钟后贴住 0 ——
                             **这才是这张图要回答的问题**（"稳住了没有"）

    ============================ 关于 y 轴范围 ============================
    上限取 `|偏差|` 的 99.5 百分位，**不是最大值** —— 一个跳点不该把整张图压扁。
    但**也不裁到"只看着地那段"**：开头那 1 米正是这张图最该给人看的东西
    （"它一开始飘着，后来稳住了"）。裁掉开头等于把结论藏起来。

    ⚠️ 降采样用**等间隔抽**、并**保最后一个点** ——
       最后一个点就是"最终位置"，它的值恒等于 0（定义如此），
       抽掉它图会看起来像"没收敛到 0"。
    """
    n = pf.count
    if n == 0:
        return {"t_minutes": [], "n": [], "e": [], "u": [],
                "y_limit_cm": 0.0, "count": 0, "plotted": 0}

    # ⚠️⚠️ 被压成常数的解**不画** —— 和它的「重复性」是同一个毛病：
    #    指标在这份数据上没定义。
    #
    #    实测：半小时那份平滑解，30 个历元的高程全是 **0.00** ——
    #    画出来是一根压在 0 上的直线，看着"完美稳定"，实际什么都没说。
    #    而它还会和前向解那张"从 1 米降下来"的图并排显示，
    #    **没意义的那个看起来更好**（这正是 `repeatability_is_meaningful`
    #    要拦的同一个东西，只是换了一张图）。
    #
    #    ⇒ 不画，并且**把为什么说出来** —— 让前端有能力显示一句人话，
    #      而不是画一张空图让用户自己琢磨。
    if not pf.repeatability_is_meaningful():
        return {
            "t_minutes": [], "n": [], "e": [], "u": [],
            "y_limit_cm": 0.0, "count": n, "plotted": 0,
            "why_not": (
                "这份解的位置被压成了常数（绝大多数历元写着同一个坐标），"
                "画出来会是一根直线 —— 那看着像「完美稳定」，其实什么都没说。"
                "想看稳定性请看前向解那一张。"
            ),
            "headline": verdict(pf).headline,
            "quality": verdict(pf).quality,
            "converged_minutes": None,
        }

    ref = pf.self_reference()

    def dev_cm(dn: float, de: float, du: float) -> tuple[float, float, float]:
        return ((dn - ref[0]) * 100, (de - ref[1]) * 100, (du - ref[2]) * 100)

    stride = max(1, -(-n // max(1, max_points)))     # 向上取整
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)

    t0 = pf.epochs[0].time
    t = [(pf.epochs[i].time - t0).total_seconds() / 60 for i in idx]
    ns, es, us = [], [], []
    for i in idx:
        a, b, c = dev_cm(pf.epochs[i].dn, pf.epochs[i].de, pf.epochs[i].du)
        ns.append(a)
        es.append(b)
        us.append(c)

    # 99.5 百分位（取绝对值）
    vals = sorted(abs(v) for v in (ns + es + us))
    y_limit = vals[min(len(vals) - 1, int(len(vals) * 0.995))] if vals else 0.0

    conv = pf.converged_from()
    conv_min = None
    if conv >= 0:
        step = pf.sampling_interval_seconds()
        conv_min = (pf.epochs[conv].time - t0).total_seconds() / 60 if step else None

    v = verdict(pf)
    return {
        "t_minutes": [round(x, 2) for x in t],
        "n": [round(x, 2) for x in ns],
        "e": [round(x, 2) for x in es],
        "u": [round(x, 2) for x in us],
        "y_limit_cm": round(max(y_limit, 0.1), 2),
        "count": n,
        "plotted": len(idx),
        "converged_minutes": round(conv_min, 1) if conv_min is not None else None,
        "headline": v.headline,
        "quality": v.quality,
    }


def summarize_json(pf: PosFile) -> str:
    """给模型/界面看的结构化摘要。"""
    import json

    v = verdict(pf)
    rms = pf.rms()
    return json.dumps(
        {
            "station": pf.station,
            # ⚠️⚠️ 这个键**必须在这里**：`ginan_tools.do_summarize_results`
            #    靠它判断"这次是不是同时产出了前向解和平滑解"，
            #    好把那句"两个数字不能直接比"的说明带上。
            #
            #    ⚠️ 原来**没有这个键** —— 于是那边 `.get("solution")` 永远是
            #       None、`{"forward","smoothed"} ⊆ kinds` 永远不成立，
            #       整块 comparison 说明**一次都没发出去过**，
            #       也就一次都没到过用户眼前。实测复现（发了两次结果，
            #       comparison 是空的）。
            "solution": pf.solution,
            "epochs": pf.count,
            "duration_minutes": round(pf.duration_minutes(), 1),
            "converged_from_epoch": pf.converged_from(),
            "solution_rate": round(pf.solution_rate(), 3),
            "rms_cm": {
                "north": round(rms["n"] * 100, 2),
                "east": round(rms["e"] * 100, 2),
                "up": round(rms["u"] * 100, 2),
                "horizontal": round(math.hypot(rms["n"], rms["e"]) * 100, 2),
                "three_d": round(rms["3d"] * 100, 2),
            },
            "verdict": {
                "headline": v.headline,
                "quality": v.quality,
                "details": v.details,
                "caveats": v.caveats,
            },
        },
        ensure_ascii=False,
        indent=2,
    )
