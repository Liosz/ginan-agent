"""Ginan 领域层 —— 读懂 RINEX 观测文件。

============================ 这个模块解决什么问题 ============================
用户拿着一份自己的观测数据来问"能用这个测吗"。

**不能直接说"能"** —— 有几个硬性前提，不满足的话跑出来的结果是错的，
而报错信息会指向完全无关的方向。

四个前提：

  ① **双频**。Ginan 的 PPP 用无电离层组合（两个频率相减），
     单频接收机做不了。而这是个**静默**的失败：
     配置能跑、程序不报错、结果是错的。

  ② **天线型号在 ATX 文件里**。Ginan 要查天线的相位中心偏差。
     自建站用的天线如果不在 IGS 的标准表里，会报"找不到天线" ——
     而且报错方式很难懂。

  ③ **日期在数据源覆盖范围内**。BKG 只保留近几年的产品。

  ④ **观测时段够长**。PPP 要 30 分钟以上才收敛。

这个模块的职责就是把这几条**在跑之前**说清楚。

============================ 为什么自己解析而不用库 ============================
现成的库（georinex 之类）会拖进 xarray、pandas 一大堆依赖 ——
而我们只需要读文件**开头那几十行**。

RINEX 的头部是**定宽格式**，字段位置是标准规定的，自己读几十行就够。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 头部最多读多少行。
#
# ⚠️ RINEX 头部通常 30~80 行，但有些接收机会塞进几十行 COMMENT。
#    给 200 行的余量。超过基本可以认为文件有问题。
MAX_HEADER_LINES = 2000

# ⚠️⚠️ 这个数**原来写的是 200，而那是个 bug** —— 它把完好的数据判成了有问题。
#
#     实测（用户 2026-09-20 丢进收件箱的一箱 IGS 数据）：
#         `AUCK00NZL_R_20262450000_01D_30S_MO.rnx` 的头部有 **290 行**，
#         而 `TIME OF FIRST OBS` 在**第 288 行** —— 被 `[:MAX_HEADER_LINES]`
#         这行切片砍掉了。
#
#     后果链条：
#         `first_obs = None` → `inspect()` 报「文件头里没有起始时间」
#         → **一份完好的观测数据被判成用不了**。
#         （那一箱里 5 个站中招：AUCK、WARK，以及 ALGO/MIZU/ULAB 的 RINEX 3 那份。）
#
#     ⚠️ 为什么 200 会不够：**RINEX 3 的头部长度不是固定的**。
#        多系统的站每个系统一段 `SYS / # / OBS TYPES`，
#        再加上几十行 `SYS / PHASE SHIFT`、`GLONASS SLOT / FRQ #` ——
#        7 个系统轻松两三百行。**这是一个会随数据变化的量，不是常数。**
#
#     ⚠️ 而且解析循环里**本来就有** `if "END OF HEADER" in line: break` ——
#        头部的结束**由终止符说了算**。是这行切片让循环永远到不了那儿。
#
#     👉 现在这个值只是**挡"畸形文件根本没有 END OF HEADER"的保险**，
#        不是"头部有多长"的假设。真正的边界是 `HEADER_BYTES`
#        （读进来的就 64 KB，一行至少 60 字符，装不下 2000 行）。
#
#     > 一个"我以为够大"的常数，遇到真实数据时就是一次静默截断。

# 读文件头时一次读多少字节。
#
# ⚠️ 为什么是 64 KB 而不是按行读：RINEX 的头部长度**不固定** ——
#    观测类型多、多系统的站，头部能有几百行（实测 ALIC 的 RINEX 3 头部
#    光 `SYS / # / OBS TYPES` 就占了十几行 × 7 个系统）。
#    一次读一块再按行切，比逐行读盘简单，也不容易在长行上出错。
#
# ⚠️ 给 `.gz` 用时要注意：这 64 KB 是**解压之后**的 64 KB。
#    见 `inspect()` 的 `raw` 参数。
HEADER_BYTES = 65536

# 频率编号的映射。
#
# ⚠️ 观测码有**两种写法**，长度不一样：
#
#      RINEX 3    `C1C`  —— 伪距(C) / 频段 1 / 跟踪模式 C
#      RINEX 2    `C1`   —— 只有前两段，**没有跟踪模式**
#
#    我们要的是**频段号**（数字那一位），判断有没有两个不同频段。
#
#    L1/L2/L5 是 GPS 的；L1/L5/L7/L8 是 Galileo 的；
#    L1/L2/L3 是 GLONASS 的；L2/L6/L7 是 BeiDou 的。
#    这里只取数字，具体是哪个系统由前面的字母决定。
#
# ⚠️⚠️ 最后那一位用 `\w?`（**零或一个**）而不是 `\w`（一个）——
#    这是一条真 bug 的修复：
#
#        RINEX 2 的观测码是**两位**（`C1 L1 D1 P2 L2 D2`），
#        而正则要求三位 → `L1` 匹配不上 → `bands` 永远是空集 →
#        `dual_frequency` 判成 False →
#        用户拿一份**完全正常的双频 RINEX 2 文件**，被告知
#        「这份数据是**单频**的，做不了精密单点定位」。
#
#    ⚠️ 而这不是冷门格式：测站短名（`alic0600.22o`）就是 RINEX 2，
#       也就是这个产品的用户最可能拿到的第一种文件。
#
# ⚠️ 为什么只认 `C` 和 `L`，不认 `P` / `D` / `S`：
#    · `C` 伪距、`L` 相位 —— **PPP 要的就是这两个**
#    · `D` 多普勒是速度、`S` 信噪比是质量指标，都不是观测值本身
#    · `P` 是 RINEX 2 里的 P 码伪距，和 `C` 同一个频段
#      （有 `P2` 就一定有 `L2`，否则根本没有相位可用）
#    把 `D`/`S` 也算进来的话，一个只有信噪比没有观测值的文件
#    会被判成"双频可用" —— 那是**更糟**的错。
_OBS_CODE = re.compile(r"^[CL](?P<band>\d)(?P<attr>\w?)$")


@dataclass
class Constellation:
    """一个卫星系统的观测情况。"""

    system: str               # G / E / R / C / J
    codes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        # ⚠️ `I` 是 IRNSS/NavIC（印度区域导航系统）—— 实测的 ALIC 文件里就有。
        #    漏掉它的话会显示成"系统 I"，用户不知道那是什么。
        #    （RINEX 3.04 的系统码：G/E/R/C/J/S/I）
        return {
            "G": "GPS", "E": "Galileo", "R": "GLONASS",
            "C": "BeiDou", "J": "QZSS", "S": "SBAS", "I": "IRNSS",
        }.get(self.system, f"未知系统（{self.system}）")

    @property
    def bands(self) -> set[str]:
        """有哪些频段。"""
        out = set()
        for c in self.codes:
            m = _OBS_CODE.match(c)
            if m:
                out.add(m.group("band"))
        return out

    @property
    def has_phase(self) -> bool:
        """有没有相位观测。

        ⚠️ 只有伪距没有相位的话，做不了精密定位 —— 精度会差两个量级。
        """
        return any(c.startswith("L") for c in self.codes)

    @property
    def dual_frequency(self) -> bool:
        return len(self.bands) >= 2


@dataclass
class RinexInfo:
    """一份 RINEX 文件的头部信息。"""

    path: Path | None = None
    version: float = 0.0
    file_type: str = ""            # OBSERVATION DATA / NAVIGATION DATA
    marker: str = ""
    marker_number: str = ""
    receiver: str = ""
    antenna: str = ""
    antenna_delta: tuple[float, float, float] = (0.0, 0.0, 0.0)
    approx_xyz: tuple[float, float, float] | None = None
    interval: float = 0.0
    first_obs: datetime | None = None
    last_obs: datetime | None = None
    constellations: dict[str, Constellation] = field(default_factory=dict)

    # ---- 问题分三类，因为**该做什么完全不同** ----
    #
    # ⚠️ 一开始只有一个 problems 列表，结果用户看到一堆红字
    #    不知道哪些是"没救了"、哪些是"补一下就行的"。
    #
    #    分清楚之后，agent 才能自己动手修能修的那部分，
    #    只把真正需要人知道的抛回去。

    # 致命：数据本身不满足前提，做什么都没用
    problems: list[str] = field(default_factory=list)
    # 能自动补：信息缺了但有可靠的来源（比如从文件名推站号）
    fixable: list[str] = field(default_factory=list)
    # 需要用户提供：是物理设备信息，只有他知道
    needs_input: list[str] = field(default_factory=list)
    # 解析状态（记录上一个出现的系统码，用于拼接换行的 OBS TYPES）
    _last_system: str = ""

    @property
    def ok(self) -> bool:
        """致命问题一个都没有 —— 注意这不代表"能直接跑"。

        ⚠️ 「能用」和「能直接跑」是两回事：
           有些信息缺了可以补（补完就能跑），
           有些只有用户知道（得问他）。

           判断"能不能直接开跑"要用 `ready`。
        """
        return not self.problems

    @property
    def ready(self) -> bool:
        """补完能补的之后，还有没有卡住的地方。"""
        return self.ok and not self.needs_input

    @property
    def duration_minutes(self) -> float:
        if not (self.first_obs and self.last_obs):
            return 0.0
        return (self.last_obs - self.first_obs).total_seconds() / 60

    @property
    def dual_frequency(self) -> bool:
        """至少有一个系统是双频的。

        ⚠️ 只要有**一个**系统双频就能做 PPP ——
           比如接收机同时收 GPS(L1/L2) 和 Galileo(L1/L5)，
           用任一个都行。
        """
        return any(c.dual_frequency for c in self.constellations.values())

    @property
    def usable_systems(self) -> list[str]:
        """能做 PPP 的系统（双频 + 有相位）。"""
        return [
            c.name for c in self.constellations.values()
            if c.dual_frequency and c.has_phase
        ]


# =============================================================================
# 解析
# =============================================================================


def inspect(path: Path, raw: bytes | None = None) -> RinexInfo:
    """读一份 RINEX 文件的头部。

    ⚠️ **永不抛异常** —— 返回的 RinexInfo 里 problems 字段记着问题。
       用户的数据可能格式不对、可能是压缩的、可能根本不是 RINEX。
       这些都不该让程序崩，而该变成"这个文件为什么不能用"的说明。

    ⚠️⚠️ `raw` 是**给"文件带着压缩、而调用方想透过压缩看内容"的场景**用的。
       传了就**不再读盘**，直接把这段字节当"文件内容"解析。

       谁需要它：**收件箱**。收件箱里的文件**天生带压缩**（IGS 分发 RINEX 3
       的标准形式就是 `.gz`），而这里下面那段 gzip 检查是给**数据目录**
       写的 —— 那里的文件本来就该是解开过的，所以遇到 gz 说"需要先解压"
       是对的。

       ⚠️ 但收件箱的语义完全不同：**那个 gz 是正常的**，
          `inbox.claim()` 会在搬进数据目录的路上解压它。
          拿"需要先解压"去判它，等于把一份好数据报成"用不了" ——
          而且**报得很像那么回事**，用户会信。

       > 同一个字节序列，在"数据目录"里是问题，在"收件箱"里是常态。
       > 判据没错，**位置错了**。

       调用方要自己把压缩解开再传进来（`identify.sniff_bytes()` 就是干这个的，
       它同时把 gzip / `.Z` 都处理了）。
    """
    path = Path(path)
    info = RinexInfo(path=path)

    if raw is None:
        if not path.is_file():
            info.problems.append("文件不存在")
            return info

        try:
            raw = path.open("rb").read(HEADER_BYTES)
        except OSError as exc:
            info.problems.append(f"读不了这个文件：{exc}")
            return info

    # ---- 压缩的文件 ----
    #
    # ⚠️ 用户从数据站下载的经常是 .gz 或 Hatanaka 压缩的。
    #    直接当文本读会得到乱码，然后报"看不懂这个格式" ——
    #    而真正的问题是"需要先解压"。
    if raw[:2] == b"\x1f\x8b":
        info.problems.append(
            "这是 gzip 压缩的文件，需要先解压。"
            "（放进收件箱由程序处理，或自己用 7-Zip 解压）"
        )
        return info

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()[:MAX_HEADER_LINES]

    if not lines:
        info.problems.append("文件是空的")
        return info

    _parse_version(info, lines[0])
    if info.file_type and "OBSERVATION" not in info.file_type.upper():
        info.problems.append(
            f"这不是观测数据文件（文件头写的是 {info.file_type}）。"
            "PPP 需要的是 OBSERVATION DATA。"
        )
        return info

    for line in lines:
        _parse_line(info, line)
        if "END OF HEADER" in line:
            break

    # ⚠️ 把**整段 text**（不只是头部）传进去 ——
    #    因为时长和采样间隔可能要从观测数据里推出来（文件头里没有）。
    _finalize(info, text)
    return info


def _parse_version(info: RinexInfo, first: str) -> None:
    try:
        info.version = float(first[:9].strip())
    except ValueError:
        info.problems.append("看不懂文件头 —— 这似乎不是 RINEX 文件")
        return
    info.file_type = first[20:40].strip()


def _parse_line(info: RinexInfo, line: str) -> None:
    """按标签解析一行头部。

    ⚠️ RINEX 的头部是**定宽**格式：标签固定在第 60 列之后，
       数据在左边。所以先看右边是什么标签，再从左边取值。
    """
    if len(line) < 60:
        return
    label = line[60:].strip()

    if label == "MARKER NAME":
        info.marker = line[:60].strip().split()[0] if line[:60].strip() else ""

    elif label == "MARKER NUMBER":
        info.marker_number = line[:60].strip()

    elif label == "REC # / TYPE / VERS":
        info.receiver = line[20:40].strip()

    elif label == "ANT # / TYPE":
        info.antenna = line[20:40].strip()

    elif label == "APPROX POSITION XYZ":
        info.approx_xyz = _floats(line[:60], 3)

    elif label == "ANTENNA: DELTA H/E/N":
        # ⚠️ RINEX 里的顺序是 H/E/N，而 Ginan 的配置要 E/N/U ——
        #    存的时候转一下，别在后面各处各转一次。
        h, e, n = _floats(line[:60], 3)
        info.antenna_delta = (e, n, h)

    elif label == "INTERVAL":
        vals = _floats(line[:60], 1)
        info.interval = vals[0] if vals else 0.0

    elif label in ("TIME OF FIRST OBS", "TIME OF LAST OBS"):
        m = re.match(r"\s*(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+([\d.]+)",
                     line[:60])
        if m:
            y, mo, d, hh, mm, ss = m.groups()
            try:
                t = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(float(ss)))
            except ValueError:
                return
            if label == "TIME OF FIRST OBS":
                info.first_obs = t
            else:
                info.last_obs = t

    elif label == "SYS / # / OBS TYPES":
        # RINEX 3：`G   22 C1C L1C D1C ...` —— 每个系统一行，
        # ⚠️ 但**一行放不下时会换行继续**，续行的系统码位置是空的：
        #
        #     G   22 C1C L1C D1C S1C ... L2L D2L     ← 系统名和码
        #            S2L C5Q L5Q D5Q S5Q ...          ← 续行，系统名位置是空格
        #
        # 踩过：一开始只处理有系统名的行，**续行的观测码全丢了**。
        # 对双频判断影响不大（主要频段都在第一行），
        # 但如果某个频段恰好只出现在续行，就会误判成单频 ——
        # 而"单频"会被报成"不能做 PPP"。**一个误判会直接劝退用户。**
        sysid = line[0:1].strip()
        codes = line[7:60].split()
        if sysid:
            info._last_system = sysid
            c = info.constellations.setdefault(sysid, Constellation(system=sysid))
            c.codes.extend(codes)
        elif codes and getattr(info, "_last_system", None):
            # 续行 —— 接到上一次的系统上
            info.constellations[info._last_system].codes.extend(codes)

    elif label == "# / TYPES OF OBSERV":
        # RINEX 2：没有系统区分，默认是 GPS
        codes = line[6:60].split()
        if codes and codes[0].isdigit():
            codes = codes[1:]
        c = info.constellations.setdefault("G", Constellation(system="G"))
        c.codes.extend(codes)


def _floats(text: str, n: int) -> tuple:
    out: list[float] = []
    for tok in text.split():
        try:
            out.append(float(tok))
        except ValueError:
            continue
        if len(out) >= n:
            break
    while len(out) < n:
        out.append(0.0)
    return tuple(out[:n])


def _finalize(info: RinexInfo, text: str) -> None:
    """解析完之后检查那些**硬性前提**。"""
    # ---- 先补上文件头里可能没有的东西 ----
    #
    # ⚠️ `TIME OF LAST OBS` 和 `INTERVAL` 是**可选**的头部字段，
    #    很多接收机不写。实测的 ALIC 文件（Septentrio 产）两个都没有。
    #
    #    不能因为缺了就报"数据有问题" —— 它们可以从**数据本身**推出来。
    _fill_from_epochs(info, text)

    # ⚠️ 缺站点名是**可以自动补的** —— 文件名里有站号
    #    （这是他那个转换工具的通病：只转观测数据，元数据全空）
    if not info.marker:
        guess = infer_marker_from_filename(info.path) if info.path else ""
        if guess:
            info.fixable.append(
                f"文件头里没有站点名。从文件名推测是 {guess!r} —— "
                "可以自动补上（补之前会确认）。"
            )
        else:
            info.needs_input.append(
                "文件头里没有站点名（MARKER NAME），"
                "文件名里也看不出站号。需要知道这是哪个站。"
            )

    # ⚠️ 天线和接收机型号是**物理设备信息**，推不出来，只能问用户
    if not info.antenna:
        info.needs_input.append(
            "文件头里没有天线型号（ANT # / TYPE）。"
            "Ginan 要用它查相位中心偏差 —— 缺了就做不了改正。"
            "（需要看天线上的标签或设备清单）"
        )
    if not info.receiver:
        info.fixable.append(
            "文件头里没有接收机型号。这个不影响解算，可以不填。"
        )

    if not info.constellations:
        info.problems.append("文件头里没有列出观测类型")
    elif not info.dual_frequency:
        # ⚠️ 这条最要紧 —— 单频做不了 PPP，而且是**静默失败**：
        #    配置能跑、不报错、结果是错的。
        bands = {c.name: sorted(c.bands) for c in info.constellations.values()}
        info.problems.append(
            "这份数据是**单频**的，做不了精密单点定位（PPP 需要至少两个频率）。"
            f"（各系统的频段：{bands}）\n"
            "PPP 用两个频率相减来消掉电离层延迟，只有一个频率就没有这个条件。"
            "单频数据只能用广播星历做普通定位（精度米级）。"
        )

    for c in info.constellations.values():
        if not c.has_phase:
            info.problems.append(
                f"{c.name} 只有伪距观测、没有相位观测。"
                "相位观测是厘米级精度的来源，没有它只能做到米级。"
            )

    if info.first_obs is None:
        info.problems.append(
            "文件头里没有起始时间（TIME OF FIRST OBS）。"
            "没有它就不知道这份数据覆盖哪一天。"
        )


# 历元行：`> 2026 09 02 00 00  0.0000000  0 62`（RINEX 3）
#          或 ` 26  9  2  0  0  0.0000000  0 12`（RINEX 2）
_EPOCH3 = re.compile(
    r"^>\s*(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+([\d.]+)"
)
_EPOCH2 = re.compile(
    r"^\s*(\d{2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+([\d.]+)\s+\d"
)


def _fill_from_epochs(info: RinexInfo, text: str) -> None:
    """从观测数据的历元行里补出时长和采样间隔。

    ⚠️ 这两个值**必须补**：
       · 时长 —— 用户最需要知道的"这份数据够不够跑 PPP"
       · 间隔 —— Ginan 配置里的 epoch_interval 应该和它一致

       而且**不能靠读整个文件**（48 MB）。开头的 64 KB 足够
       算出采样间隔，文件末尾 8 KB 足够找到最后一个历元。
    """
    epochs: list[datetime] = []

    def take(line: str) -> None:
        for pat in (_EPOCH3, _EPOCH2):
            m = pat.match(line)
            if not m:
                continue
            y, mo, d, hh, mm, ss = m.groups()
            try:
                year = int(y)
                # ⚠️ RINEX 2 的年份是两位数：80 以后是 1900 年代，
                #    80 以前是 2000 年代。（和 rinex_extractor 里同一套规则）
                if pat is _EPOCH2:
                    year = 2000 + year if year < 80 else 1900 + year
                epochs.append(
                    datetime(year, int(mo), int(d), int(hh), int(mm), int(float(ss)))
                )
            except ValueError:
                pass
            return

    # 开头能拿到的历元 —— 用来算采样间隔
    for line in text.splitlines():
        if line.startswith(">"):
            take(line)
        if len(epochs) >= 12:
            break

    # ---- 采样间隔 ----
    if info.interval <= 0 and len(epochs) >= 2:
        deltas = [
            (epochs[i + 1] - epochs[i]).total_seconds()
            for i in range(len(epochs) - 1)
        ]
        deltas = [d for d in deltas if d > 0]
        if deltas:
            # 取中位数 —— 有数据缺口时，平均会被拉偏
            deltas.sort()
            info.interval = deltas[len(deltas) // 2]

    # ---- 结束时间 ----
    if info.last_obs is None and info.path is not None:
        info.last_obs = _find_last_epoch(info.path)


# 读文件末尾时，从多大开始、每次翻几倍、最多读到多大。
#
# ⚠️ 为什么要"逐步扩大"而不是固定一个大数？
#
#    踩过：一开始固定读 16 KB，结果**一个历元行都没找到** ——
#    那 16 KB 里全是观测数据行。
#
#    因为一个历元块可能很大：20 多颗卫星 × 10 多种观测，
#    按 RINEX 的排版一行放 4 个观测值，一个历元就要十几 KB。
#    所以 16 KB 可能连**一个**历元都装不下。
#
#    固定的"够大"要么浪费（大多数文件用不着），要么还是不够（总有人超）。
#    逐步扩大两个好处都占：**常见情况只读一次小的，极端情况才读大的。**
_TAIL_START = 64 * 1024
_TAIL_MAX = 4 * 1024 * 1024


def _find_last_epoch(path: Path) -> datetime | None:
    """找到文件里最后一个历元的时间。

    ⚠️ 从后往前读，读到就停 —— 不读整个文件（可能几十 MB）。

    ⚠️ 读到的第一行可能是**从中间截断的半行**，解析时匹配不上就跳过。
       所以不能假设"读到的第一行是完整的"。
    """
    size = _TAIL_START
    while True:
        try:
            with path.open("rb") as f:
                f.seek(0, 2)
                total = f.tell()
                if total == 0:
                    return None
                start = max(0, total - size)
                f.seek(start)
                raw = f.read(size)
        except OSError:
            return None

        text = raw.decode("utf-8", errors="replace")
        last: datetime | None = None
        for line in text.splitlines():
            for pat in (_EPOCH3, _EPOCH2):
                m = pat.match(line)
                if not m:
                    continue
                y, mo, d, hh, mm, ss = m.groups()
                try:
                    year = int(y)
                    if pat is _EPOCH2:
                        year = 2000 + year if year < 80 else 1900 + year
                    last = datetime(year, int(mo), int(d), int(hh), int(mm),
                                    int(float(ss)))
                except ValueError:
                    pass

        if last is not None:
            return last

        # 读到头了还没有，说明整个文件里都没有历元行
        if start == 0:
            return None

        # 还不够大就翻倍再试
        if size >= _TAIL_MAX:
            return None
        size *= 4


# =============================================================================
# 修补头部 —— 让"转换工具偷懒"的文件能用
# =============================================================================
#
# ⚠️ 这个功能解决一个很常见的现实问题：
#
#    很多设备厂商的转换工具（实测遇到的：SINOGNSS 的 RinexConvert）
#    只把**观测数据**转出来，**元数据全空着**：
#
#        MARKER NAME          ← 空
#        REC # / TYPE / VERS  ← 空
#        ANT # / TYPE         ← 空
#
#    而 Ginan 需要这些 —— 尤其是天线型号（要查相位中心偏差）。
#
#    用户的反应通常是"那我重新导出一遍"，
#    但**转换工具就是不给**，再导一遍还是空的。
#
#    其实 RINEX 的头部是**纯文本、定宽**的，补几行就行。
#    这个模块就是干这个的。


# RINEX 头部每行固定 80 列：内容在 0-59，标签在 60-79。
_LABEL_COL = 60
_LINE_WIDTH = 80


def _header_line(content: str, label: str) -> str:
    """拼一行合法的 RINEX 头部。

    ⚠️ 内容**超长时要截断**，不能让它盖掉标签 ——
       否则 Ginan 读到的是一行乱码标签的行，然后"找不到 MARKER NAME"。
       而那个报错完全看不出是"你在头部写太长了"。
    """
    content = (content or "")[:_LABEL_COL].ljust(_LABEL_COL)
    return (content + label.ljust(_LINE_WIDTH - _LABEL_COL))[:_LINE_WIDTH] + "\n"


def infer_marker_from_filename(path: Path) -> str:
    """从文件名推站点名。

    ⚠️ 这是**推测**，不是权威来源 —— 所以调用方要告知用户。

       但对这种"转换工具没写 MARKER NAME"的文件，文件名反而是
       唯一可靠的线索了：`dcms260101000000.26O` 里的 `dcms` 就是站号。

    ⚠️ 只在 RINEX 2 风格的文件名上推（`ssssdddf.yyo`）：
       前 4 个字符是站号。别的命名不要瞎猜。
    """
    stem = path.stem.upper()
    # RINEX 2 短名：4 位站号 + 3 位年积日 + 1 位序号 + 年份 + 类型
    if re.fullmatch(r"[A-Z0-9]{4}\d{3}[0-9A-Z]\.?\d{0,2}[A-Z]?", stem):
        return stem[:4]
    # 9 位站号（RINEX 3 风格）
    m = re.match(r"^([A-Z0-9]{4})", stem)
    return m.group(1) if m else ""


def repair(
    src: Path,
    dst: Path,
    *,
    marker: str = "",
    receiver: str = "",
    antenna: str = "",
    antenna_delta: tuple[float, float, float] | None = None,
) -> tuple[bool, str]:
    """写一份**修好头部**的新文件。返回 (成功, 说明)。

    ⚠️⚠️ **绝不覆盖原文件。**

       用户的原始数据是**不可再生的** —— 那是他扛着接收机在野外测出来的。
       而且我们只是补几个头部字段，改坏了得不偿失。

       所以永远是"读 src，写 dst"，dst 是数据目录里的新文件。

    ⚠️ 只改头部，**观测数据部分原样按字节拷过去**。
       42 MB 的文件不能"读进来改改再写出去" ——
       既慢又可能因为编码转换把二进制内容弄坏
       （观测数据里有非 ASCII 字节）。
    """
    src = Path(src)
    dst = Path(dst)

    if not src.is_file():
        return False, "源文件不存在"

    try:
        raw_head = src.open("rb").read(512 * 1024)
    except OSError as exc:
        return False, f"读不了：{exc}"

    text = raw_head.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)

    # 找到头部结束的位置（按**字节**算，因为后面要按字节拷）
    header_end = -1
    for i, ln in enumerate(lines):
        if "END OF HEADER" in ln:
            header_end = sum(len(x.encode("utf-8", "replace")) for x in lines[:i + 1])
            header_lines = lines[:i]
            break
    if header_end < 0:
        return False, "没找到 END OF HEADER —— 这不像一份完整的 RINEX 文件"

    # ---- 改头部 ----
    wants = {
        "MARKER NAME": _header_line(marker, "MARKER NAME") if marker else None,
        "REC # / TYPE / VERS": (
            _header_line(" " * 20 + (receiver or "")[:20], "REC # / TYPE / VERS")
            if receiver else None
        ),
        "ANT # / TYPE": (
            _header_line(" " * 20 + (antenna or "")[:20], "ANT # / TYPE")
            if antenna else None
        ),
        "ANTENNA: DELTA H/E/N": (
            _header_line(
                f"{antenna_delta[2]:14.4f}{antenna_delta[0]:14.4f}"
                f"{antenna_delta[1]:14.4f}",
                "ANTENNA: DELTA H/E/N",
            )
            if antenna_delta else None
        ),
    }

    out: list[str] = []
    replaced: set[str] = set()
    for ln in header_lines:
        label = ln[60:].strip() if len(ln) > 60 else ""
        if label in wants:
            new = wants[label]
            if new is not None:
                out.append(new)
                replaced.add(label)
            else:
                out.append(ln)      # 没给值就不动
        else:
            out.append(ln)

    # 没找到的字段（转换工具可能整行都不写）→ 追加进去
    for label, new in wants.items():
        if new is not None and label not in replaced:
            out.append(new)

    new_header = "".join(out) + _header_line("", "END OF HEADER")

    # ---- 写：新头部 + 原样的数据部分 ----
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as fin, dst.open("wb") as fout:
            fin.seek(header_end)
            fout.write(new_header.encode("utf-8"))
            # ⚠️ 分块拷 —— 42 MB 不一次读进内存
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
    except OSError as exc:
        return False, f"写不了：{exc}"

    return True, f"已写出修补后的文件：{dst.name}"


# =============================================================================
# 结论
# =============================================================================


def verdict(info: RinexInfo) -> dict:
    """给这份数据一个"能不能用"的结论。

    ⚠️ 结论要分三层，因为**该做什么完全不同**：

        problems       彻底不行（单频）—— 只能换数据
        fixable        缺信息但能补（站号能从文件名推）—— **我可以自己修**
        needs_input    缺的是物理设备信息（天线型号）—— 只能问用户

       「能用」「补完能用」「得先问」是三件事，
       混成一句"有问题"会让用户不知道该干嘛。
    """
    if not info.ok:
        return {
            "usable": False,
            "ready_to_run": False,
            "headline": "这份数据做不了 PPP。",
            "reasons": info.problems,
            "can_auto_fix": [],
            "need_from_user": [],
        }

    minutes = info.duration_minutes
    notes: list[str] = []

    if minutes < 30:
        notes.append(
            f"观测时长只有 {minutes:.0f} 分钟。PPP 至少要 30 分钟才收敛，"
            "建议用一整天的数据。"
        )
    elif minutes < 120:
        notes.append(
            f"观测时长 {minutes:.0f} 分钟 —— 够跑，但不如整天数据可靠。"
        )

    # ---- 结论的文字 ----
    #
    # ⚠️ 没准备好时**不能只说"可以用"**。
    #    用户会以为马上就能跑，然后在跑的时候撞到"找不到天线"。
    if info.ready:
        headline = (
            f"可以用。测站 {info.marker}，"
            f"{'、'.join(info.usable_systems) or '（无）'} 多频，"
            f"时长 {minutes:.0f} 分钟。"
        )
    elif info.needs_input:
        headline = (
            f"数据本身没问题（{'、'.join(info.usable_systems) or '（无）'} 多频、"
            f"时长 {minutes:.0f} 分钟），"
            f"但还缺 {len(info.needs_input)} 项信息需要你提供。"
        )
    else:
        headline = (
            f"可以用（{'、'.join(info.usable_systems) or '（无）'} 多频、"
            f"时长 {minutes:.0f} 分钟），但有些信息要补一下。"
        )

    return {
        "usable": True,
        "ready_to_run": info.ready,
        "headline": headline,
        "station": info.marker,
        "systems": info.usable_systems,
        "start": info.first_obs.isoformat() if info.first_obs else None,
        "end": info.last_obs.isoformat() if info.last_obs else None,
        "duration_minutes": round(minutes, 1),
        "interval_seconds": info.interval,
        "antenna": info.antenna,
        "notes": notes,
        # ⚠️ 这两项是给 agent 看的"行动指引"
        "can_auto_fix": info.fixable,
        "need_from_user": info.needs_input,
    }


def to_json(info: RinexInfo) -> str:
    import json

    return json.dumps(
        {
            "file": str(info.path.name) if info.path else "",
            "version": info.version,
            "marker": info.marker,
            "marker_number": info.marker_number,
            "receiver": info.receiver,
            "antenna": info.antenna,
            "approximate_position": list(info.approx_xyz) if info.approx_xyz else None,
            "start": info.first_obs.isoformat() if info.first_obs else None,
            "end": info.last_obs.isoformat() if info.last_obs else None,
            "duration_minutes": round(info.duration_minutes, 1),
            "interval_seconds": info.interval,
            "constellations": {
                c.name: {
                    "bands": sorted(c.bands),
                    "dual_frequency": c.dual_frequency,
                    "has_phase": c.has_phase,
                }
                for c in info.constellations.values()
            },
            "verdict": verdict(info),
            "hint": (
                "如果 verdict.can_auto_fix 非空，调用 repair_rinex 就能补上；"
                "如果 verdict.need_from_user 非空，那些只能问用户 —— "
                "天线型号是物理设备信息，推不出来。"
                "（可以用 list_antennas 帮用户从表里找）"
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
