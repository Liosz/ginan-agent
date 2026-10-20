"""Ginan 领域层 —— 「这一大堆文件，能算什么」。

============================ 它回答什么问题 ============================
用户往收件箱丢了一大堆文件（几十上百个，混着观测 / 导航 / 产品），
然后问：**「这些能算什么？」**

而 `prompts.py` **早就要求模型回答这个**：

    → 然后说清楚：这份数据**能算什么**、**还缺什么**、
      缺的**能不能自动下**。这才叫"给建议"。

—— 但**当时没有任何工具能回答它**。

============================ ⚠️ 为什么必须在这里聚合 ============================
模型手上那两块信息是**分开的，而且都不全**：

    `scan_inbox`      有 kind，**没有站名、没有时间**
    `list_local_obs`  有站名、有时间，但**把非 obs 全丢了**
                      （`ginan_tools.py` 里那一行 `if ident.kind != "obs"`）
                      —— 而 SP3 / CLK / ERP / ATX 恰恰是"能不能算"的关键

两边**无处可 join**。模型只能把上百行摆成一张表在脑子里心算 ——
而工具输出有 **8000 字符上限**，那么多行**会被截断**，
于是它看到的还是残的。

    > 一个只能返回"我找到了什么"的工具，说不出"我还缺什么"。

所以这个模块做两件事：

    ① **聚合** —— 按站、按天，而不是一个一个文件
    ② 把"能不能算"的三个要素 **join 到一起**：
       观测（做什么）× 时间（哪一天）× 辅助文件（缺不缺）

============================ ⚠️ 它不做什么 ============================
**不猜、不保证、不替用户决定。**

   · 一个 `(站, 天)` 只说明"**这一天有这个站的观测**" ——
     **不等于**这个解能跑好。观测质量、天线型号、多路径都不在这里判。
   · ⚠️ 它**不会说"这份数据能算出厘米级"**。
     准不准要拿权威坐标（IGS 的 SINEX 解）去比，而这个模块手里
     只有文件名和文件头。**它说得出的是：有什么、缺什么、哪些用不了。**
   · ⚠️ 它也**不替用户挑**先算哪一个 —— 那是用户的事。

============================ 分层 ============================
**领域层纯函数**：不进网络、不写盘、不 import `core`。
输入是 `inbox.InboxItem` 的列表，输出是 `Pile`（可以直接 `to_json`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import identify, rinex
from .inbox import InboxItem

# =============================================================================
# 常量
# =============================================================================

# 一次解算**必须有**的辅助文件。
#
# ⚠️ 这份清单来自 `catalog.KINDS`，但**不等于**它 ——
#    `KINDS` 是"我们认识哪些类型"，这里是"**算一个 PPP 解至少要哪些**"。
#    多出来的（`bia` 码偏差、`table` 模型文件）是**锦上添花**，
#    缺了照样能跑，只是精度或改正项少一点。
REQUIRED_KINDS: tuple[str, ...] = ("sp3", "clk", "erp", "atx")

# 缺了会**明显**影响结果、但不至于跑不起来的。
OPTIONAL_KINDS: tuple[str, ...] = ("nav", "snx", "bia")

# 观测时长门槛（分钟）。
#
# ⚠️ 这是个**粗糙**的门槛，只用来把"明显太短"的挑出来 ——
#    PPP 静态解的收敛通常要几十分钟，而日解是 24 小时。
#    **不拿它下"够不够好"的结论**，只说"这个偏短，你心里有数"。
#
#    （为什么是 120：比它短的，解算结果里"还没收敛"那一段占比会很大，
#      而用户看到的重复性数字会被那一段拖坏。这是个经验值，不是定理。）
SHORT_OBS_MINUTES = 120

# 输出里最多列几个候选站。
#
# ⚠️ 工具输出有 8000 字符上限（`core/tools.py`）。上百个站全列出来
#    会被**截断**，而截断之后模型看到的是"前几个站" ——
#    它会以为只有那么多，**比不列还糟**。
#
# 👉 所以超了就**明说"还有 N 个没列"**，而不是让输出被悄悄砍掉。
MAX_STATIONS = 30


# =============================================================================
# 数据类型
# =============================================================================


@dataclass
class ObsFile:
    """一个观测文件的摘要。"""

    path: Path
    station: str            # 4 位站号。**来自文件头**，不是文件名
    day: date | None
    duration_minutes: float
    encoding: str           # "RINEX3" / "RINEX2" / "CrinEX"
    usable: bool            # 能不能直接喂给 Ginan
    problems: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class StationDays:
    """一个测站：有哪些天、每份文件什么情况。"""

    station: str
    days: list[date] = field(default_factory=list)
    files: list[ObsFile] = field(default_factory=list)

    @property
    def usable_files(self) -> list[ObsFile]:
        return [f for f in self.files if f.usable]

    @property
    def usable_days(self) -> list[date]:
        return sorted({f.day for f in self.usable_files if f.day})

    @property
    def days_text(self) -> str:
        return "、".join(d.strftime("%m-%d") for d in self.usable_days)


@dataclass
class Pile:
    """一堆文件的**聚合**结果。"""

    obs: list[ObsFile] = field(default_factory=list)

    # kind → 文件。**包括非 obs**（这正是 `list_local_obs` 缺的那一块）
    aux: dict[str, list[InboxItem]] = field(default_factory=dict)

    unknown: list[InboxItem] = field(default_factory=list)
    unusable: list[InboxItem] = field(default_factory=list)

    # 太深没扫到的文件数（来自 `inbox.ScanResult`）
    too_deep_files: int = 0

    # ---- 派生 ----

    @property
    def stations(self) -> dict[str, StationDays]:
        out: dict[str, StationDays] = {}
        for f in self.obs:
            st = out.setdefault(f.station, StationDays(f.station))
            st.files.append(f)
            if f.usable and f.day and f.day not in st.days:
                st.days.append(f.day)
        for st in out.values():
            st.days.sort()
        return out

    @property
    def candidates(self) -> list[tuple[str, date]]:
        """★ 所有 `(站, 天)` 候选 —— **接 `plan_run(station, day)` 的入口**。

        ⚠️ 这是这个模块最核心的输出。`catalog.plan_run` 的签名就是
           `(station, day)`，所以模型拿到这个列表就能直接往下走。

        ⚠️ **只算"能用"的观测。** CrinEX 那份虽然也是 obs，
           但它喂不进 Ginan —— 把它算进候选，等于承诺一个跑不了的东西。
        """
        pairs: list[tuple[str, date]] = []
        for st in self.stations.values():
            for d in st.usable_days:
                pairs.append((st.station, d))
        return sorted(pairs)

    @property
    def day_span(self) -> list[date]:
        return sorted({d for _, d in self.candidates})

    @property
    def missing_kinds(self) -> list[str]:
        """缺哪些**必需**的辅助文件。

        ⚠️ 判据是"收件箱里有没有这一类文件"，**不是**"数据目录里有没有" ——
           数据目录里可能已经有程序早先下好的。
           所以这句话的措辞必须是"**收件箱里没有**"，不能是"你没有"。
        """
        return [k for k in REQUIRED_KINDS if not self.aux.get(k)]

    @property
    def optional_missing(self) -> list[str]:
        return [k for k in OPTIONAL_KINDS if not self.aux.get(k)]


# =============================================================================
# 从文件名读日期 —— 只在文件头读不出来时用
# =============================================================================

# RINEX 3：`ALIC00AUS_R_20262440000_01D_30S_MO.rnx` → 2026 年 244 日
_R3_DAY = re.compile(r"_R_(\d{4})(\d{3})\d{4}_")
# RINEX 2：`alic2440.26o` → 26 年 244 日
_R2_DAY = re.compile(r"^[a-z]{4}(\d{3})\d\.(\d{2})[a-z]", re.IGNORECASE)


def _day_from_name(name: str) -> date | None:
    """从文件名里读日期。**兜底用**，权威来源是文件头。

    ⚠️ 为什么需要它：**CrinEX 的头里读不出观测时间** ——
       实测 `rinex.inspect()` 对 `.crx` 返回 `first_obs=None`
       （它认为"这不是观测数据文件"，所以没有历元可读）。
       而我们要能告诉用户"CrinEX 那份对应的是哪天，
       同站同天有一份 .rnx 可以用" —— 那就得从名字拿日期。

    ⚠️ 但**日期绝不能当成站名那样信** ——
       站名有权威来源（文件头 MARKER NAME），日期没有。
       所以这里读出来的，只在报告里当"**这个名字指向哪天**"用，
       而"有没有这一天的数据"仍然以能读出来的那份为准。
    """
    m = _R3_DAY.search(name)
    if m:
        return _doy(int(m.group(1)), int(m.group(2)))
    m = _R2_DAY.match(name)
    if m:
        yy = int(m.group(2))
        # ⚠️ 两位年份的世纪：RINEX 2 是 1980~2079 的约定（GPS 纪元起算）
        return _doy(1900 + yy if yy >= 80 else 2000 + yy, int(m.group(1)))
    return None


def _doy(year: int, doy: int) -> date | None:
    """年积日 → 日期。越界返回 None（**不抛异常**）。"""
    try:
        return date(year, 1, 1) + timedelta(days=doy - 1)
    except (ValueError, OverflowError):
        return None


# =============================================================================
# 主函数
# =============================================================================


def analyze(items: list[InboxItem], *, too_deep_files: int = 0) -> Pile:
    """把一堆收件箱条目聚合成一份 `Pile`。

    ⚠️ **不抛异常。** 收件箱是用户随手丢东西的地方，
       一个读不了的文件不该让整次分析失败 —— 那正是
       `inbox.scan()` 那条原则（**批量操作里的单个失败，
       要变成一条记录，而不是一次中断**）。
    """
    pile = Pile(too_deep_files=too_deep_files)

    for it in items:
        kind = it.identity.kind

        # ---- 认不出来的 ----
        if not it.identity.is_data:
            pile.unknown.append(it)
            continue

        # ---- 观测：要读内容才知道时长和问题 ----
        #
        # ⚠️ CrinEX **也走这一条** —— 它只是"包了一层编码"，
        #    解出来之后就是普通 RINEX（认领时会自动解，见 `crinex.py`）。
        #    `_obs_file` 里对它单独处理：**只解头部**，不解整个文件。
        #
        #    ⚠️ 这里原来走的是 `pile.unusable`（"认得出、是数据、但用不了"）。
        #       那在**当时**是对的 —— 没有解码器。现在有了，所以它是可用的。
        if kind == "obs":
            f = _obs_file(it)
            pile.obs.append(f)
            # ⚠️ **只有真的解不开**（CRINEX 1.0、文件坏了）才进这一类。
            #    那才是"认得出、是数据、但用不了"。
            if it.identity.crinex and not f.usable:
                pile.unusable.append(it)
            continue

        # ---- 辅助文件（sp3/clk/erp/atx/nav/snx/bia/table）----
        pile.aux.setdefault(kind, []).append(it)

    return pile


def _crinex_header_text(path: Path) -> str | None:
    """读出（可能带压缩的）CrinEX 文件的**头部明文**。读不出来返回 None。

    ⚠️ 只读前面一小块 —— `identify.sniff_bytes` 会顺手把 gzip / `.Z`
       解开。头部在 CrinEX 里是明文复制过去的，几 KB 就够，
       **不需要把整个文件解出来**（那要好几秒）。

    ⚠️ 兜底的情况（`gzip?` / `z?`）要说出来 —— 见 `_obs_file` 里那段。
    """
    from . import identify as _ident
    from . import crinex as _crinex

    head, compression, _ = _ident.sniff_bytes(path, limit=rinex.HEADER_BYTES)
    if compression in ("gzip?", "z?"):
        return None
    try:
        return _crinex.decode_header(head.decode("latin-1"))
    except Exception:  # noqa: BLE001
        # ⚠️ 解不开就是解不开 —— **不猜**。CRINEX 1.0 走的就是这里
        #    （本程序只支持 3.0，见 crinex.py 的说明）。
        return None


def _obs_file(it: InboxItem) -> ObsFile:
    """把一个收件箱条目变成一个 `ObsFile`。"""
    name = it.path.name
    day = _day_from_name(name)

    # ⚠️ 站名的**权威来源是文件头**，不是文件名。
    #    从文件名截前 4 个字符是 GinanUI 踩过的坑：遇到非标准命名
    #    会**静默**得到错误结果（见 docs/ginan-facts.md）。
    station = (it.identity.marker or "")[:4].upper()

    # ⚠️⚠️ CrinEX **要先解出头部**才能判 —— 直接 `inspect` 原文件不行。

    #    它的头部（`COMPACT RINEX FORMAT`）会让 `inspect` 认为
    #    "这不是观测数据文件"，于是**读不出站名和观测时间**，
    #    一份好数据会被报成用不了。

    #    👉 **只解头部**，不解整个文件 —— 头在 CrinEX 里本来就是明文复制
    #       的（只多两行自述行），所以这一步是**常数时间**。
    #       而解一份 50 MB 的日观测要 5 秒，48 份就是四分钟：
    #       一个交互式工具不能这样。

    #    ⚠️ 代价：**观测时长拿不到**（那要靠数据段的历元算）。
    #       所以下面 `duration_minutes` 是 0 —— 调用方别把它当"时长是 0"，
    #       那是"不知道"。`SHORT_OBS_MINUTES` 那条判断用 `if f.duration_minutes`
    #       挡住了 0，所以不会误报"偏短"。
    if it.identity.crinex:
        header_text = _crinex_header_text(it.path)
        if header_text is None:
            return ObsFile(
                path=it.path,
                station=station or _station_from_name(name) or "????",
                day=day,
                duration_minutes=0.0,
                encoding="CrinEX",
                usable=False,
                problems=[
                    "CrinEX 解码失败 —— 这一份我们解不开"
                    "（可能是 CRINEX 1.0 / RINEX 2 的老格式，或者文件坏了）"
                ],
            )
        ident = it.identity
        info = rinex.inspect(it.path, raw=header_text.encode("latin-1"))
        problems = list(info.problems) + list(info.needs_input)
        if not station:
            station = (info.marker or "")[:4].upper()
        if info.first_obs:
            day = info.first_obs.date()
        return ObsFile(
            path=it.path,
            station=station or _station_from_name(name) or "????",
            day=day,
            duration_minutes=0.0,
            encoding="CrinEX",
            # ⚠️⚠️ 这里**不能**用 `ident.usable` —— 它对 CrinEX 天然是 False
            #    （那个属性的定义就是"不能**直接**喂给 Ginan"，
            #      见 `identify.FileIdentity.usable` 里那段）。
            #
            #    而这里问的是**另一个问题**："解出来之后能不能用" ——
            #    我们刚把头部解出来了，所以答案由 `info.ready` 说了算，
            #    判据和普通 RINEX **完全一样**。
            #
            #    （第一版就是写成了 `ident.usable and info.ready`，
            #      于是 48 个 CrinEX 一个都没变成可用 —— 而候选数
            #      看不出来，因为它们都有 `.rnx` 双胞胎顶着。
            #      要不是"用不了"那一栏还写着 48，这个错会被漏掉。）
            usable=ident.is_data and info.ready,
            problems=problems,
        )

    # ⚠️⚠️ **透过压缩读** —— 收件箱里的文件天生带压缩，
    #    而 `inspect` 里那段 gzip 检查是给**数据目录**写的
    #    （那里的文件本来就该是解开过的）。
    #    不看穿的话，一份 .gz 好数据会被报成"需要先解压 = 用不了"。
    head, compression, _ = identify.sniff_bytes(
        it.path, limit=rinex.HEADER_BYTES
    )

    # ⚠️⚠️ **解不开的压缩包不能拿去当 RINEX 解析。**
    #
    #    `sniff_bytes` 有个兜底分支：gzip 解不动时**返回压缩字节本身**，
    #    类型标成 `"gzip?"`。那个返回值对 `identify` 是**正确的信号**
    #    （"这是个 gzip，我读不动"），但拿它来解析 RINEX 就是灾难 ——
    #    我们会把压缩字节当成文本，得到"认不出来"，
    #    然后把一份好数据报成"用不了"。
    #
    #    ⚠️ 什么时候会走到那儿：`sniff_bytes` 只喂前 `limit` 个**压缩**字节，
    #       想解出 `limit` 个**解压后**字节 —— 所以**压缩比低于 1** 时它就解不够。
    #
    #    📏 **实测（2026-09-20，用户那一箱 146 个 .gz）：触发 0 次。**
    #       RINEX 的压缩比是 **2.7 ~ 3.7 倍**，够用得很。
    #
    #    ⚠️ 而且这条分支**今天走不到**：`analyze()` 只对
    #       `identity.is_data` 的条目调 `_obs_file`，而一个解不开的压缩包
    #       在 `identify` 那一步就已经被归成 `unknown` 了。
    #
    #    👉 所以它是一条**防御分支**：万一 `sniff_bytes` 的行为变了、
    #       或者 `analyze()` 的输入换了个来源，这里要能**说出真话**，
    #       而不是把压缩字节当 RINEX 解析、报一句"认不出来"
    #       （那会让用户去查自己的数据，而真正的问题是文件坏了）。
    #
    #    📏 它**有守卫**（`check_files.py` ⑦m）—— 直接调 `_obs_file` 喂一个
    #       坏压缩包。**没有守卫的防御分支就是没人读的字段**，
    #       这个仓库为那件事吃过不止一次亏。
    if compression in ("gzip?", "z?"):
        return ObsFile(
            path=it.path,
            station=station or _station_from_name(name) or "????",
            day=day,
            duration_minutes=0.0,
            encoding=_encoding(it, name),
            usable=False,
            problems=[
                f"压缩包解不开（{compression}）—— 可能坏了或没传完，重新下一份再试"
            ],
        )

    info = None
    try:
        info = rinex.inspect(it.path, raw=head)
    except Exception:  # noqa: BLE001
        # ⚠️ 读不动就是读不动 —— **不猜**。见下面 `problems` 的处理。
        pass

    problems: list[str] = []
    duration = 0.0
    usable = it.identity.usable

    if info is not None:
        problems = list(info.problems) + list(info.needs_input)
        duration = info.duration_minutes
        if info.first_obs:
            # 文件头里的时间是**权威的**，盖过文件名
            day = info.first_obs.date()
        # ⚠️ `ok` 是"没有致命问题"，`ready` 是"补完能补的之后还能跑"。
        #    判断能不能开跑要用 `ready` —— 这是 `RinexInfo` 自己写着的话。
        usable = usable and info.ready
    elif usable:
        # 读不动内容，但识别层说它是数据 —— 不敢说能用
        problems = ["读不出内容（可能是压缩态或文件损坏）"]
        usable = False

    if not station:
        # ⚠️ 头部没有站名时的**兜底**，而且**只在兜底时才用文件名**。
        #    自建站的数据头部常常是空的（实测：某份自建站数据的
        #    MARKER NAME 是 60 个空格）。
        guess = _station_from_name(name)
        if guess:
            station = guess
            problems = problems + [f"文件头里没有站名，按文件名当成 {guess}（请确认）"]

    return ObsFile(
        path=it.path,
        station=station or "????",
        day=day,
        duration_minutes=duration,
        encoding=_encoding(it, name),
        usable=usable,
        problems=problems,
    )


def _station_from_name(name: str) -> str:
    """从文件名猜站号。**只在文件头没有站名时才调用。**

    ⚠️ 只认两种**标准的**命名，不硬凑：
        RINEX 3：`ALIC00AUS_R_...`
        RINEX 2：`alic2440.26o`

       猜不出来的返回空串 —— 一个空站号比一个**编出来的**站号好。
       （编错的后果是配置引用一个不存在的站，而 Ginan 会报一个
         和真正原因毫无关系的错。）
    """
    m = re.match(r"^([A-Za-z0-9]{4})00[A-Za-z]{3}_[RS]_", name)
    if m:
        return m.group(1).upper()
    m = re.match(r"^([a-z]{4})\d{3}\d\.\d{2}[a-z]", name, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return ""


def _encoding(it: InboxItem, name: str) -> str:
    """这份观测数据是什么编码。给人看的说法。"""
    if it.identity.crinex:
        return "CrinEX"
    low = name.lower()
    if re.search(r"\.\d{2}[on]$", low) or re.match(r"^[a-z]{4}\d{3}\d\.\d{2}[a-z]", low):
        return "RINEX2"
    return "RINEX3"


# =============================================================================
# 输出
# =============================================================================


def to_json(pile: Pile) -> str:
    """给模型看的摘要 —— **聚合过的**。

    ⚠️⚠️ 这个函数的**唯一**约束是"别被截断"。
       工具输出上限是 8000 字符（`core/tools.py`），
       而一个 194 个文件的收件箱如果逐个文件列出来，轻松超。

       而被截断的后果不是"少看一点" —— 是模型**以为就这么多**。
       它会对着前十几个文件给用户下结论。**比不列还糟。**

       👉 所以：按站聚合、超了就明说"还有 N 个没列"，
          绝不交给下游去偷偷砍掉。

    ⚠️ **不给 `indent`** —— 实测同一份数据：
           `indent=2` → 8984 字符（**超了**）
           紧凑       → 约 3000 字符
       而 `indent` 唯一的用处是让人看 —— 但这份输出是**给模型看的**，
       用户看到的是模型复述出来的话，不是这坨 JSON。
       （界面上的工具卡是折叠的，展开看原始 JSON 是排错行为。）
    """
    import json

    return json.dumps(_as_dict(pile), ensure_ascii=False, separators=(",", ":"))


def _as_dict(pile: Pile) -> dict:
    stations = pile.stations
    cands = pile.candidates

    # ---- 观测 ----
    listed = sorted(stations.values(), key=lambda s: (-len(s.usable_days), s.station))
    shown = listed[:MAX_STATIONS]
    all_days = pile.day_span

    by_station = []
    for s in shown:
        row: dict = {"站": s.station, "天数": len(s.usable_days)}
        # ⚠️ 只在"**这个站的日子和全体不一样**"时才列日期。
        #    实测那一箱 23 个站里 22 个都是完整 3 天 —— 每个都列一遍
        #    纯属重复，而重复的代价是**把别的信息挤出 8000 字符上限**。
        if set(s.usable_days) != set(all_days):
            row["日期"] = [d.isoformat() for d in s.usable_days]
        row["编码"] = sorted({f.encoding for f in s.usable_files})
        short = [
            f.name for f in s.usable_files
            if f.duration_minutes and f.duration_minutes < SHORT_OBS_MINUTES
        ]
        if short:
            row["偏短"] = short
        probs = sorted({p for f in s.usable_files for p in f.problems})
        if probs:
            row["问题"] = probs[:3]
        by_station.append(row)

    obs_block: dict = {
        "测站数": len(stations),
        "有可用观测的站数": sum(1 for s in stations.values() if s.usable_days),
        "候选数": len(cands),
        "日期": [d.isoformat() for d in all_days],
        "按站": by_station,
    }
    if len(listed) > MAX_STATIONS:
        # ⚠️ **明说漏了**，别让下游截断
        obs_block["未列出"] = (
            f"还有 {len(listed) - MAX_STATIONS} 个站，"
            f"按可用天数排序后排在后面"
        )

    # ---- 用不了的 ----
    #
    # ⚠️ 这里**不逐个文件列**，而是先回答用户真正要问的那个问题：
    #    「**这些我是不是白下了？**」
    #
    #    实测那一箱：48 个 `.crx.gz`，**每一个都有同站同天的 `.rnx`** ——
    #    也就是说**一个都没白下**。这个结论比 48 行文件名有用得多，
    #    而且只占 40 个字符。
    unusable_block = None
    if pile.unusable:
        # 同站同天有没有能用的替代品
        usable_index = {(f.station, f.day) for f in pile.obs if f.usable}
        with_alt: list[tuple] = []
        without: list[str] = []
        for u in pile.unusable:
            st = (u.identity.marker or "")[:4].upper()
            d = _day_from_name(u.path.name)
            if st and (st, d) in usable_index:
                with_alt.append((u, st, d))
            else:
                without.append(u.path.name)

        unusable_block = {
            "数量": len(pile.unusable),
            # ⚠️ 这句原来是"CrinEX 编码 —— Ginan 读不了这个格式"。
            #    **现在不准确了**：能解开的 CrinEX 一律可用（认领时自动解），
            #    走到这里的是**解不开的那一小撮**。说清是哪一小撮，
            #    用户才知道要不要去换。
            "为什么": (
                "CrinEX 编码，而且这一份**解不开**"
                "（多半是 CRINEX 1.0 / RINEX 2 的老格式，或者文件坏了）"
            ),
            "有替代品的": len(with_alt),
            "例子": [
                {
                    "文件": u.path.name,
                    "改用": next(
                        f.name for f in pile.obs
                        if f.usable and f.station == st and f.day == d
                    ),
                }
                for u, st, d in with_alt[:3]
            ],
            "怎么办": (
                "这些**不用管** —— 同站同天都有一份能用的，用那份就行"
                if not without
                else f"其中 {len(without)} 个**没有**替代品，"
                     "得先转成 RINEX（我们没有解码器），或者换个源重新拿："
                     + "、".join(without[:3])
            ),
        }

    # ---- 重复（同站同天多份能用的）----
    #
    # ⚠️ 这里要分**两种**重复，它们的处理完全不同：
    #
    #     ① **内容相同**（`X.rnx` 和 `X.rnx.gz`）→ 留一份就行，随便哪份
    #     ② **内容不同**（RINEX 3 的 `_MO.rnx` 和 RINEX 2 的 `.26o`）
    #        → 是同一份观测的**两个版本**，得挑一个（一般挑 RINEX 3）
    #
    #    不说清是哪一种的话，"同站同天有两份"这句提示**没法照着做** ——
    #    用户不知道是该删一个还是该挑一个。
    #
    # ⚠️ 判据直接复用 `inbox._digest()` —— 它算的是"**解压之后**的样子"，
    #    所以 `.rnx` 和 `.rnx.gz` 会给出**同一个指纹**。
    #    这正是它当初被修成那样的原因（见那个函数的说明）。
    from .inbox import _digest

    dupes = []
    for s in stations.values():
        seen: dict[date, list[ObsFile]] = {}
        for f in s.usable_files:
            if f.day:
                seen.setdefault(f.day, []).append(f)
        for d, fs in sorted(seen.items()):
            if len(fs) > 1:
                digests = {_digest(f.path) for f in fs}
                same = len(digests) == 1 and None not in digests
                row: dict = {
                    "站": s.station, "日": d.isoformat(),
                    "份数": len(fs),
                    "文件": [f.name for f in fs],
                    "内容": "完全相同（留一份就行）" if same
                            else "不同（同一份观测的不同版本，挑一个用）",
                }
                if not same:
                    # 有 RINEX 3 就建议用 RINEX 3 —— 它是更新的格式
                    r3 = [f.name for f in fs if f.encoding == "RINEX3"]
                    if r3:
                        row["建议用"] = r3[0]
                dupes.append(row)

    # ---- 缺什么 ----
    #
    # ⚠️ 分成两句话，**不是四行** ——
    #    第一版每个 kind 一行、每行都带着同一句"能自动下吗"，
    #    4 个必需项就把那句 40 字的话重复了 4 遍。
    #    重复的代价是**把别的信息挤出 8000 字符上限**，
    #    而"能不能下"对这几个类型来说**本来就是同一个答案**。
    # ⚠️⚠️ 措辞是**判据的一部分**，不是修饰。
    #
    #     `missing_kinds` 自己那段说明写着：
    #        「判据是"收件箱里有没有这一类文件"，**不是**"数据目录里有没有"
    #          ……所以这句话的措辞必须是"**收件箱里没有**"，不能是"你没有"。」
    #
    #     而这里原来写的是 `"还缺（必需）"` / `"必需（缺了做不了 PPP）"` ——
    #     **两句都是"你没有"**。规则写下了，输出没照做。
    #
    #     实测代价（用户 2026-09-21 那一箱）：他的 `data/` 里
    #     `igs20.atx` 天天都在、245 天的 SP3/CLK/ERP/OSB 也都在，
    #     而这句话说的是"缺 sp3、clk、erp、atx" ——
    #     照着它去下，会把已有的东西再下一次；
    #     更糟的是，**问"能不能算"的人会得到"缺四样"这个错答案**。
    #
    #     👉 两处都改成"收件箱里没有"，并且**明说数据目录可能已有**、
    #        怎么核对。措辞和判据对齐之后，这句话才既对又有用。
    missing = None
    if pile.missing_kinds or pile.optional_missing:
        missing = {
            "必需（收件箱里没有这几类就做不了 PPP）": pile.missing_kinds,
            "可选（缺了也能跑，精度差一点）": pile.optional_missing,
            "⚠️这只是收件箱": (
                "**数据目录里可能早就有了**（程序之前下过的、或者上一轮认领的）——"
                "这一类不随日期变（如天线表），或者是**按天**的（如 SP3/CLK/ERP）。"
                "所以要先核对再用：`catalog.plan_run(站, 天)` 给出那一天**究竟**"
                "要哪些文件，再拿 `list_local_data` 看本地有什么。"
                "**不要照着这一行就直接下。**"
            ),
            "能自动下吗": (
                "能 —— 用 plan_run 生成取数计划，再 fetch_data 下载。"
                "下载会跳过本地已有的。"
                "⚠️ 但计划要指定**站和天**（`catalog.plan_run(station, day)`）。"
                "⚠️ 观测数据只有**一个源**（澳大利亚 GA）。"
                "它收录的是**全球**站，不止澳洲 —— 实测 2025-06-15 那天"
                "在 71 个 IGS 站名里查到 56 个（美洲、欧洲、非洲、亚洲、"
                "大洋洲、南极都有）。"
                "但**不是整个 IGS 网**：查不到的站会明确说「这个源没有收录这个站」，"
                "那种情况下只能自己准备观测文件丢进收件箱。"
            ),
        }

    out: dict = {}
    out["一句话"] = _headline(pile, stations, cands)
    out["观测"] = obs_block
    if unusable_block:
        out["用不了"] = unusable_block
    if dupes:
        out["重复"] = dupes[:12]
        if len(dupes) > 12:
            out["重复"].append({"还有": f"{len(dupes) - 12} 个同站同天多份的情况"})
    out["辅助文件"] = {
        "收件箱里已有": {k: len(v) for k, v in sorted(pile.aux.items())},
        # ⚠️ 键名也必须说清是**收件箱**里的缺失 —— 见上面那段说明。
        "收件箱里没有（必需）": pile.missing_kinds,
    }
    if missing:
        out["缺什么"] = missing
    if pile.unknown:
        out["认不出"] = {
            "数量": len(pile.unknown),
            "例子": [u.path.name for u in pile.unknown[:5]],
        }
    if pile.too_deep_files:
        # ⚠️ 和 `inbox` 那边一样：**看不到的东西必须自己说出来**
        out["没扫到"] = (
            f"还有 {pile.too_deep_files} 个文件在太深的子目录里没看，"
            f"把它们挪到浅一点的地方再扫一次"
        )
    return out


def _headline(pile: Pile, stations: dict, cands: list) -> str:
    """一句话总结 —— 给模型一个**能直接复述给用户**的开头。

    ⚠️ 这句话只陈述**数得出来的事实**，不做质量判断。
       数得出来的只有"有几个 (站, 天) 有观测"；"能算得很准"是**承诺**。

    ⚠️ 原来这里写的是"**可以做 N 个日解**"（见 `Pile.candidates` 的说明）。**
       那是个**过头话**：候选只说"这一天有这个站的观测"，
       而**能不能真跑起来还要看那一天的精密产品在不在**
       —— 模块开头那段「它不做什么」自己就写着这一条。

       实测代价（用户 2026-09-21 那一箱）：头一句说"可以做 66 个日解"，
       下一节又说"缺 sp3/clk/erp/atx" —— **一句话自相矛盾**，
       而读它的人（模型）会把哪一句复述给用户，全看运气。

       👉 改成陈述"有多少个 (站, 天) **有观测**" —— 这是数得出来的，
          而且不会和下一节打架。
    """
    usable_st = [s for s in stations.values() if s.usable_days]
    if not cands:
        if pile.obs:
            return "有观测数据，但**没有一个能用**（看看下面「用不了」那一节）"
        return "收件箱里没有观测数据（只有辅助文件或认不出来的东西）"

    days = pile.day_span
    span = (
        f"{days[0].isoformat()} 到 {days[-1].isoformat()}"
        if len(days) > 1 else days[0].isoformat()
    )
    return (
        f"{len(usable_st)} 个测站、{len(days)} 天（{span}），"
        f"一共 {len(cands)} 个 (站, 天) 有观测"
        f"（能不能跑起来还要看那几天的精密产品在不在，见下面「缺什么」）"
    )
