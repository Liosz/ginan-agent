"""Ginan 领域层 —— 网络体检：哪些数据源现在能用。

============================ 这个模块的产品价值 ============================
它是把「**装不上**」变成「**知道哪里不通**」的那一步。

没有它，用户看到的是：
    ✗ 下载失败

有这个，用户看到的是：
    ✓ 澳大利亚 GA        正常（1.2 秒）
    ✓ 德国 BKG           正常（0.8 秒）
    ✗ NASA CDDIS         需要登录（返回了登录页，不是数据）
                          → 到 urs.earthdata.nasa.gov 注册一个免费账号

**同一个失败，两种体验。** 前者用户只能来问你，后者用户自己能解决。

============================ 并发探测 ============================
三个源串行探测、每个超时 10 秒 = 最坏 30 秒。
界面卡 30 秒和死机没区别。

所以用线程池并发。三个源各探各的，总耗时约等于最慢的那个。

⚠️ 注意并发探测和"并发下载"是两回事：
   探测是"打个招呼"，随便并发；
   下载要考虑对方服务器压力，得限流（见 fetch.py）。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from . import catalog, net

# =============================================================================
# 每个源拿什么来探测
# =============================================================================
#
# ⚠️ 探测用的 URL 要满足三个条件：
#      ① 稳定存在（不能是某个特定日期的文件，那会因为"还没发布"而误报）
#      ② 小（用 Range 只取 1KB，但服务器可能不支持 Range）
#      ③ 是**真实数据**而不是网页
#         （judge_data 会把 HTML 判为"这不是数据文件"，这是刻意的 ——
#           登录页、错误页都是 HTML）
#
# 所以每个源都挑一个"任何时候都该有的数据文件"。

# =============================================================================
# 探测目标
# =============================================================================
#
# ⚠️ 这份清单的**组织方式**比内容更重要，改过一版。
#
#    第一版只列了 3 个源（BKG / GA / CDDIS），界面显示"2/3 可用" ——
#    用户看到的第一反应是"就这几个？"。
#
#    问题不在数量，在**视角**：
#      按"服务器"列 → 用户不知道这跟自己要干的活有什么关系
#      按"用途"列   → 用户一眼看出"我需要的那些文件，哪些能拿到"
#
#    所以每一项都带 `purpose`，界面按用途分组显示。
#
# ⚠️ 清单里的 URL 全部来自**实测普查**（scripts/survey_sources.py），
#    不是猜的。加新条目之前先跑一遍普查确认它真的能用。

# (key, 用途分组, 给人看的名字, 探测 URL, 需不需要账号, 是否要求真实数据文件)
PROBE_TARGETS: list[tuple[str, str, str, str, bool, bool]] = [
    # ---- 精密产品：轨道 / 钟差 / 天线文件。PPP 的命根子 ----
    #
    # ⚠️ 前两个是 2026-09-20 加的，**都真实下载成功过**（不是"看着应该能用"）。
    #
    # ⚠️ 武大的探测 URL 是**首页**（`expect_data=False`）——
    #    因为它下载文件要先 POST 查清单，一个 GET 探不出"能不能下"。
    #    首页 200 只证明"服务器活着"。**真正的证据是实测下载**：
    #    同一天同一个文件它 1792~3165 KB/s，而 BKG 只有 47~60 KB/s。
    #
    # ⚠️ SIO 用一个**固定的老文件**探测，这是有意的：
    #    固定日期不会过期，任何时候探都是同一个结论；
    #    用"今天的文件"会让探测结果随日期漂移（今天有、明天 404）。
    ("whu", "精密产品", "武汉大学 IGS 数据中心",
     "http://www.igs.gnsswhu.cn/index.php", False, False),
    ("sio", "精密产品", "美国 SIO / Scripps",
     "https://garner.ucsd.edu/pub/products/2434"
     "/IGS0OPSRAP_20262450000_01D_15M_ORB.SP3.gz", False, True),
    ("bkg", "精密产品", "德国 BKG",
     "https://igs.bkg.bund.de/root_ftp/IGS/station/general/igs20.atx", False, True),
    ("igs", "精密产品", "IGS 官方分发点",
     "https://files.igs.org/pub/station/general/igs20.atx", False, True),
    # ⚠️⚠️ CDDIS 这个探测地址**换过一次**，换的原因值得记下来（2026-09-29 实测）。
    #
    #    原来用的是 `gnss/products/rcvantex/igs20.atx.gz` —— 那是**猜**出来的路径
    #    （见 catalog.CDDIS_ATX_URL 上那句"没验证过"）。有了账号之后一量：
    #
    #        · 那个文件           → 404
    #        · 它的目录 `rcvantex/` → 404（**整个目录不存在了**）
    #        · 父目录清单（1828 项）里没有任何含 atx/antex/rcv 的项
    #
    #    后果比"少一个源"严重得多：`net.judge_data` **故意**把 404 判成"可达"
    #    （"源通了但今天的文件还没发布"），于是侧栏显示成 **ok（绿）**、
    #    细节写着"文件不存在"、advice 是空的 —— 用户**没有任何可操作的信息**。
    #    而本地机器恰好留着一份老的 `~/_netrc`，把这件事盖住了很久。
    #
    #    换成现在的地址，选它的三条理由：
    #      ① **固定日期**（2022-03-01）—— 和 SIO 那条同一个道理：永远不会过期，
    #         任何时候探都是同一个结论（用"今天的文件"会让结论随日期漂移）
    #      ② **385 字节**，实测存在（`igr21992.erp.Z`，真 .Z 数据）
    #      ③ **必须认证** —— 没凭据时它会跳登录页，正好触发「需要登录」那条引导
    ("cddis", "精密产品", "NASA CDDIS",
     "https://cddis.nasa.gov/archive/gnss/products/2199/igr21992.erp.Z", True, True),

    # ---- 观测数据：测站自己的数据 ----
    ("ga", "观测数据", "澳大利亚 GA",
     "https://data.gnss.ga.gov.au/api/v1/rinexFiles"
     "?stationId=ALIC&fileType=obs&rinexVersion=3&filePeriod=01D"
     "&decompress=true&tenantId=default"
     "&startDate=2026-01-01T00:00:00Z&endDate=2026-01-02T00:00:00Z", False, True),

    # ---- 模型文件：tables/ 里的那些静态模型 ----
    #
    # ⚠️ 这一组是"锦上添花"：没有它们精度略降，但分析照样能跑。
    #
    # ⚠️⚠️ 这里原来还写着「**有代理才能用**的典型」—— **那句话是错的**，
    #    而且错得有害：它让每个用户以为必须挂代理，于是根本不去试。
    #    实测（见 catalog.py）：区域端点不通，但**全球端点直连 200、1.9 秒**。
    #    「过期的记录比没有记录更糟」—— 这条错的记录害过人一次。
    #
    # ⚠️ 后 6 个的探测 URL 是**目录页或首页**（它们的数据路径结构不标准，
    #    实测猜不出来），所以最后一位是 False —— 只要服务器应答就算通。
    #    这也是为什么这个字段必须存在：用"必须是数据文件"去测一个首页，
    #    会把正常应答的服务器判成不通（踩过）。
    # ⚠️⚠️ 这个 URL **从 catalog 的常量拼出来，不要再手写一遍**。
    #
    #    原来这里写的是**区域端点**：
    #        https://peanpod.s3.ap-southeast-2.amazonaws.com/aux/...
    #    而下载用的（`catalog.GINAN_S3_BASE`）是**全球端点**：
    #        https://peanpod.s3.amazonaws.com/aux/...
    #
    #    ⚠️ 同一个桶，只是主机名不同 —— 而区域端点**实测不通**
    #       （`catalog.py` 里记着：超时 12 秒，走代理也不通）。
    #
    #    后果：**唯一真正能下模型文件的源，在体检里被报成 ✗**，
    #    还按 `advice` 劝用户"通常需要开启代理" ——
    #    而它其实直连就行（实测 200，1.9 秒）。
    #
    #    👉 这正是本项目那条「**过期的记录比没有记录更糟**」的又一次现形：
    #       `catalog.py` 里那条错记录改了，`probe.py` 这份**拷贝**没改。
    #       而两份拷贝的存在本身就是成因 —— 所以这里直接引用常量，
    #       让"两处不一致"变成**写不出来**的事。
    ("ginan_s3", "模型文件", "Ginan 官方数据仓",
     f"{catalog.GINAN_S3_BASE}/products/tables/gpt_25.grd.gz",
     False, True),
    ("iers", "模型文件", "IERS 地球定向",
     "https://datacenter.iers.org/data/latestVersion/", False, False),
    ("vmf", "模型文件", "TU Wien 对流层",
     "https://vmf.geo.tuwien.ac.at/trop_products/GRID/", False, False),
    ("ncei", "模型文件", "NOAA 地磁模型",
     "https://www.ncei.noaa.gov/pub/data/", False, False),
    ("icgem", "模型文件", "ICGEM 重力场",
     "http://icgem.gfz-potsdam.de/", False, False),
    ("jpl", "模型文件", "JPL 行星历表",
     "https://ssd.jpl.nasa.gov/", False, False),
    ("onsala", "模型文件", "Onsala 海潮负荷",
     "http://holt.oso.chalmers.se/loading/", False, False),
]

# 用途分组的显示顺序 —— 按"对跑通一次分析的重要性"排，不是字母序
PURPOSE_ORDER = ["精密产品", "观测数据", "模型文件"]

# 每个用途组的说明 —— 让用户知道"这一类是干什么的"
PURPOSE_HELP: dict[str, str] = {
    "精密产品": "卫星的精确位置和钟差。定位精度主要靠它。",
    "观测数据": "你的测站接收到的信号。这是要处理的对象。",
    # ⚠️ 原来写的是"缺了精度略降，但能跑" —— 那是个**没测过的量级**。
    #    这些文件是给"完整"档用的；"基础"档压根不引用它们，
    #    所以这一组探不通时，用户最需要知道的是"**那我受影响吗**"。
    "模型文件": "海潮、对流层等改正模型，只有「完整」档会用到。"
                "用「基础」档的话，这一组不通也不影响。",
}

# 每种用途需要**几个**可用源才算"够用"
#
# ⚠️ 为什么不是"全绿才好"？
#    因为多源本来就是互替关系 —— 精密产品有 BKG 一个能用就够了，
#    其余只是备份。要求全绿会让用户误以为"还有问题要修"。
#
#    模型文件那组例外：它们的用途各不相同（潮汐/对流层/地磁…），
#    不是互替的，所以按"至少有一个"算。
ENOUGH_PER_PURPOSE: dict[str, int] = {
    "精密产品": 1,
    "观测数据": 1,
    "模型文件": 1,
}


# =============================================================================
# 结果
# =============================================================================


@dataclass
class SourceProbe:
    """一个源的体检结果。"""

    key: str
    label: str
    purpose: str          # 用途分组（精密产品 / 观测数据 / 模型文件）
    reachable: bool
    latency_ms: int
    detail: str
    requires_auth: bool
    note: str = ""

    @property
    def icon(self) -> str:
        """给界面用的图标 —— 三种状态，不是一个布尔值。

        ⚠️ 为什么区分"不通"和"要账号"？
        因为**用户该做的事完全不同**：
            不通     → 开代理
            要账号   → 去注册
        都显示成一个红叉的话，用户不知道该干嘛。
        """
        if self.reachable:
            return "ok"
        if self.requires_auth:
            return "auth"
        return "fail"

    @property
    def advice(self) -> str:
        """不通时，告诉用户该做什么。

        ⚠️ 这是整个模块最重要的一个字段。
        一句可操作的指引，顶得上一小时的排查。
        """
        if self.reachable:
            return ""
        if self.requires_auth:
            return (
                "这个源需要免费账号。到 https://urs.earthdata.nasa.gov/users/new "
                "注册后，在设置里填入用户名和密码即可。"
            )
        if self.purpose == "模型文件":
            # ⚠️ 模型文件不通**不是问题** —— 说清楚这一点。
            #    否则用户会以为"有东西坏了"，急着去修。
            return (
                "连不上。这一组是精度增强用的 —— "
                "连不上不代表不能干活，只是结果精度会略低。"
                "想拿到的话通常需要开启代理。"
            )
        return (
            "连不上。如果其他源也连不上，通常需要开启代理；"
            "在设置里可以填代理地址。"
        )


@dataclass
class PurposeGroup:
    """按用途分的一组源。

    ⚠️ 分组是为了回答用户真正关心的问题：
       **"我要的这类文件，能拿到吗？"**
       而不是"这 17 台服务器有几台能 ping 通"。
    """

    purpose: str
    help: str
    sources: list[SourceProbe] = field(default_factory=list)

    @property
    def usable(self) -> list[SourceProbe]:
        return [s for s in self.sources if s.reachable]

    @property
    def enough(self) -> bool:
        """这一组够用吗。

        ⚠️ 判断标准是"有没有可用的"，不是"是不是全绿"。
           多源本来就是互替关系 —— 有备份是好事，
           但只有主源能用也**完全没问题**。

           要求全绿会导致：用户看到几个红的，以为还有问题要修，
           而实际上他马上就能开始干活。**制造不必要的焦虑也是一种 bug。**
        """
        return len(self.usable) >= ENOUGH_PER_PURPOSE.get(self.purpose, 1)

    @property
    def icon(self) -> str:
        if self.enough:
            return "ok"
        # 全组都不通 —— 才值得标黄
        return "auth" if any(s.requires_auth for s in self.sources) else "fail"

    @property
    def summary(self) -> str:
        n = len(self.usable)
        total = len(self.sources)
        if n == total:
            return f"{n}/{total} 可用"
        if n:
            return f"{n}/{total} 可用（够用）"
        return "一个都不通"


@dataclass
class ProbeReport:
    """一次网络体检的完整结果。"""

    sources: list[SourceProbe] = field(default_factory=list)
    proxy_description: str = ""
    # ⚠️ 被用户中止了吗。
    #
    #    为什么要单独记这个？因为**被中止的探测结果是不全的** ——
    #    如果不说，调用方会把"只探了 3 个源"当成"只有 3 个源"，
    #    然后得出"其他源都不通"的错误结论。
    #
    #    半截的结果必须标明自己是半截的。
    cancelled: bool = False

    @property
    def usable(self) -> list[SourceProbe]:
        return [s for s in self.sources if s.reachable]

    @property
    def unusable(self) -> list[SourceProbe]:
        return [s for s in self.sources if not s.reachable]

    def groups(self) -> list[PurposeGroup]:
        """按用途分组，顺序按 PURPOSE_ORDER。"""
        by: dict[str, list[SourceProbe]] = {}
        for s in self.sources:
            by.setdefault(s.purpose, []).append(s)
        return [
            PurposeGroup(purpose=p, help=PURPOSE_HELP.get(p, ""), sources=by[p])
            for p in PURPOSE_ORDER
            if p in by
        ]

    @property
    def verdict(self) -> str:
        """一句话总结。

        ⚠️ 措辞的重点是**"能不能干活"**，不是"几台通几台不通"。

           反面："2/3 个数据源可用" —— 用户不知道这意味着什么
           正面："可以开始处理数据" / "还差关键的一类，需要开代理"
        """
        if not self.sources:
            return "还没检查"

        # ⚠️ 被中止时**不能说"拿不到"** —— 那会被当成"这些源不行"。
        #    实际只是"还没测完"。
        if self.cancelled:
            return (
                f"检查被中止了，只测了 {len(self.sources)} 个源，"
                "结果不完整。"
            )

        groups = self.groups()
        blocked = [g for g in groups if not g.enough]
        if not blocked:
            return "数据都能拿到，可以开始处理。"

        names = "、".join(g.purpose for g in blocked)
        return f"{names}拿不到，需要开代理。"

    @property
    def ok_for_work(self) -> bool:
        """现在能不能真的开始干活。"""
        return all(g.enough for g in self.groups())

    def to_json(self) -> str:
        return json.dumps(
            {
                "verdict": self.verdict,
                "ok_for_work": self.ok_for_work,
                "proxy": self.proxy_description,
                "groups": [
                    {
                        "purpose": g.purpose,
                        "help": g.help,
                        "icon": g.icon,
                        "enough": g.enough,
                        "summary": g.summary,
                        "sources": [
                            {**asdict(s), "icon": s.icon, "advice": s.advice}
                            for s in g.sources
                        ],
                    }
                    for g in self.groups()
                ],
                "sources": [
                    {**asdict(s), "icon": s.icon, "advice": s.advice}
                    for s in self.sources
                ],
            },
            ensure_ascii=False,
            indent=2,
        )


# =============================================================================
# 体检
# =============================================================================


def _probe_authed(
    url: str,
    proxy: net.ProxyConfig | None,
    timeout: float,
) -> tuple[bool, int, str]:
    """探一个**要账号**的源 —— 走 `cddis_get`，不走普通 GET。

    ⚠️ 为什么必须单独一条路，见 `probe_one` 里那段（一句话：跨主机跳转时
       requests 会把 Authorization 剥掉，只有自己跟跳转才带得住）。

    ⚠️ 判断逻辑**复用** `net.judge_data` —— 不在这里重写一遍。
       两处判断迟早会漂移，而漂移的方向是"探测说能下、下载却失败"。

    返回 `(是否可用, 耗时毫秒, 人话说明)` —— 和 `net.probe_url` 同一个形状，
    所以调用点可以互换。
    """
    import time

    # 局部 import：`credentials` 会反向 import `net`，
    # 放模块顶部会把这条依赖变成环（同 `fetch._session_for` 的做法）。
    from .credentials import cddis_get, cddis_session

    start = time.monotonic()
    session = cddis_session(proxy=proxy)
    try:
        resp = cddis_get(session, url, timeout=timeout)
        # ⚠️ 必须在 close() **之前**把判断要用的字节读出来 ——
        #    关掉之后 iter_content 什么都读不到，于是"响应体里没有登录页字样"，
        #    认证失败的源会被判成**正常**。这个坑在 net.head_ok 里记过一次。
        try:
            status = resp.status_code
            head = next(resp.iter_content(2048), b"") or b""
        finally:
            resp.close()
    except Exception as exc:  # noqa: BLE001
        return False, int((time.monotonic() - start) * 1000), net._explain_exception(exc)
    finally:
        session.close()

    ok, detail = net.judge_data(status, head)
    return ok, int((time.monotonic() - start) * 1000), detail


def probe_all(
    proxy: net.ProxyConfig | None = None,
    timeout: float = net.DEFAULT_PROBE_TIMEOUT,
    only: list[str] | None = None,
    cancelled=None,
    on_progress=None,
) -> ProbeReport:
    """探测所有数据源。

    参数：
        only        只探其中几个（测试用，或者用户只想复查某一个）
        cancelled   可调用对象，返回 True 表示该中止了
        on_progress 可调用对象，每探完一个源调一次（用于报进度）

    ⚠️⚠️ 支持取消，这一点很重要，原因值得记下来：

       一个工具如果在执行途中完全不理会取消，用户点"停止"之后
       会**看起来毫无反应** —— 而"点了没反应"和"程序卡死了"
       在用户眼里是一回事。

       实测：11 个源、每个超时 20 秒，最坏要等 40 秒才轮到循环层
       检查取消标志。用户早就以为程序坏了。

       **取消的响应速度是可靠性的一部分**，不是锦上添花。
    """
    targets = [
        t for t in PROBE_TARGETS if not only or t[0] in only
    ]
    session = net.build_session(proxy)
    report = ProbeReport(
        proxy_description=(proxy or net.ProxyConfig()).describe()
    )

    def probe_one(t: tuple) -> SourceProbe:
        key, purpose, label, url, needs_auth, expect_data = t
        if needs_auth:
            # ⚠️ 要账号的源**不能用普通 GET 探** —— 这不是保守，是实测：
            #
            #   信任环境（requests 自己去读家目录 `~/_netrc`）时，探针会"碰巧"通；
            #   而用户在界面里填的凭据写在 `userdata/_netrc`，**requests 不认那个位置**。
            #   实测 `trust_env=False` 之后，连 `cddis_session()` 配 `probe_url`
            #   （`allow_redirects=True`）都只能拿到登录页。
            #
            #   原因就是 `credentials.cddis_get` 里那段：CDDIS 要**跨主机跳转**
            #   （cddis → urs.earthdata.nasa.gov → cddis），而 requests 在跨主机时
            #   会把 Authorization **剥掉**；它自己的 netrc 流程会重新补上，
            #   我们显式设的 auth 不会。
            #
            #   ⇒ 所以这条路必须自己跟跳转。
            #
            #   ⚠️ 这一条修不好会怎样（实测）：探测永远显示"需要登录"，
            #      用户**填完并且验证通过**之后它还是"需要登录" ——
            #      因为探的这条路根本没带上那份凭据。（本机被一份老的
            #      `C:\Users\<用户>\_netrc` 盖住了，所以看着是好的。）
            ok, ms, detail = _probe_authed(url, proxy, timeout)
        else:
            ok, ms, detail = net.probe_url(
                session, url, timeout, expect_data=expect_data
            )
        return SourceProbe(
            key=key,
            label=label,
            purpose=purpose,
            reachable=ok,
            latency_ms=ms,
            detail=detail,
            requires_auth=needs_auth,
        )

    # ⚠️ 并发探测，否则 11 个源串行最坏要等两分钟。
    #
    #    ⚠️ 但并发数要**克制**：这里限到 6，不是"有多少源开多少线程"。
    #       因为其中有几台是同一个机构的不同服务，
    #       一次打太多请求是不礼貌的，而且可能被当成攻击而封掉 IP。
    #       （每条请求已经带了 Range 只要 1KB，但礼貌还是要讲。）
    done = 0
    results: list[SourceProbe] = []
    order = {t[0]: i for i, t in enumerate(targets)}

    from concurrent.futures import as_completed

    # ⚠️⚠️ 这里**故意不用 `with ThreadPoolExecutor(...)`**。
    #
    #    踩过一次：用了 with，取消之后**还是等了 32 秒**。
    #
    #    因为 `with` 退出时会调 `shutdown(wait=True)` ——
    #    **等所有正在跑的线程结束**。而 `future.cancel()`
    #    只能取消"还没开始"的任务，**已经在跑的那个取消不掉**。
    #
    #    所以"取消"看起来生效了（我们的循环确实退出了），
    #    但函数迟迟不返回 —— 用户那边还是没反应。
    #
    #    换成 `shutdown(wait=False)`：立刻返回，不等在飞的那几个。
    #    它们的线程会在后台自己超时结束，结果被丢弃。
    #    **让函数立刻返回，比回收那几个线程重要得多。**
    pool = ThreadPoolExecutor(max_workers=min(6, max(1, len(targets))))
    try:
        futures = {pool.submit(probe_one, t): t for t in targets}

        # ⚠️ 用 as_completed 而不是 pool.map ——
        #    map 要等**全部**完成才返回，中间拿不到任何进度，
        #    也没机会检查取消。
        for fut in as_completed(futures):
            if cancelled is not None and cancelled():
                for f in futures:
                    f.cancel()          # 取消还没开始的
                report.cancelled = True
                break
            try:
                results.append(fut.result())
            except Exception:  # noqa: BLE001
                # ⚠️ 单个源探测失败不该让整批失败 ——
                #    记一条"探测出错"，其余照常。
                t = futures[fut]
                results.append(SourceProbe(
                    key=t[0], label=t[2], purpose=t[1],
                    reachable=False, latency_ms=0,
                    detail="探测过程出错", requires_auth=t[4],
                ))
            done += 1
            if on_progress is not None:
                on_progress(done, len(targets))
    finally:
        # ⚠️ wait=False —— 见上面的说明。不等在飞的那几个。
        pool.shutdown(wait=False)

    # 保持和 targets 一致的顺序，不然界面上的顺序会随机变
    results.sort(key=lambda s: order.get(s.key, 999))
    report.sources = results

    session.close()
    return report
