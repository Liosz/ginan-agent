"""自检：网络层 —— 判断逻辑、代理配置、取数计划。

⚠️ 这个脚本**完全不联网**。

它测的是"给你一个响应，你判得对不对"，而不是"你能不能连上"。
两者要分开：

    · "响应判断"是**纯逻辑**，应该 100% 可测、可复现
    · "能不能连上"取决于网络，不该进单元测试

混在一起的话，测试就变成了"我今天的网通不通" —— 毫无意义。

============================ 一条重要的守卫 ============================
第一个测试盯的是一个真实踩过的坑：

    假阳性 —— 把"需要登录"判成了"正常"

那次的成因是"先关了连接，再去读响应体"。假阳性比假阴性更糟：
用户看到绿勾以为能用，真下载时才发现要账号，只会更困惑。

所以这里用假响应把它钉死。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ginan_agent.ginan import catalog  # noqa: E402
from ginan_agent.ginan import fetch  # noqa: E402
from ginan_agent.ginan import net  # noqa: E402
from ginan_agent.ginan import sources  # noqa: E402

PASS = 0
FAIL = 0


def check(cond: bool, label: str) -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}   ← 失败")
    return cond


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 60)


# =============================================================================
# 假响应 —— 模仿 requests 的 response 对象
# =============================================================================

# CDDIS 未认证时返回的真实特征：HTTP 200 + 一个标题为 Earthdata Login 的 HTML
LOGIN_PAGE = (
    b"<!DOCTYPE html>\n<html lang=\"en\"><head>"
    b"<title>Earthdata Login</title></head><body>..."
)


class FakeResponse:
    """够用的假响应。只实现 head_ok 会用到的那几个成员。"""

    def __init__(self, status: int, body: bytes = b"", chunk: int = 2048) -> None:
        self.status_code = status
        self._body = body
        self._chunk = chunk
        self.closed = False
        # 记录 iter_content 是不是在 close 之后才被调用 —— 这正是那个 bug
        self.iterated_after_close = False

    def iter_content(self, size: int):
        if self.closed:
            self.iterated_after_close = True
            return
        yield self._body[:size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self._response = response
        self.last_url = ""
        self.last_headers: dict = {}

    def get(self, url, headers=None, **kwargs):
        self.last_url = url
        self.last_headers = headers or {}
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# =============================================================================
# ① 响应判断 —— 那个假阳性的守卫
# =============================================================================


def test_probe_uses_the_same_endpoint_as_download() -> None:
    section("⑨ ★★ 体检探的地址，必须和**下载用的**是同一个")

    # ⚠️⚠️ 守的是一条**过期的记录**，而且它已经害过人。
    #
    #    同一口桶，两个主机名：
    #
    #        peanpod.s3.ap-southeast-2.amazonaws.com   区域端点
    #        peanpod.s3.amazonaws.com                  全球端点
    #
    #    ⚠️ 实测区域端点**不通**（超时 12 秒，走代理也不通），
    #       而全球端点**直连 200、1.9 秒**。
    #
    #    下载用的是全球端点（`catalog.GINAN_S3_BASE`），
    #    而 `probe.py` 里**又手写了一遍**、写的是区域端点 ——
    #
    #    后果：**唯一真正能下模型文件的源，在体检里被报成 ✗**，
    #    还劝用户"通常需要开启代理"。用户于是不去试那个能用的源。
    #
    #    👉 这正是本项目那条「**过期的记录比没有记录更糟**」：
    #       `catalog.py` 里那条错记录改了，`probe.py` 这份**拷贝**没改。
    #       而"有两份拷贝"本身就是成因。
    #
    # ⚠️ 判据要盯**两个地址必须是同一个主机**，而不是"字符串里没写
    #    ap-southeast-2" —— 后者改个写法就绕过去了。
    import re as _re

    from ginan_agent.ginan import catalog, probe

    base_host = catalog.GINAN_S3_BASE.split("/")[2]
    targets = [t for t in probe.PROBE_TARGETS if t[0] == "ginan_s3"]
    check(bool(targets), "清单里有 ginan_s3 这一项（否则测的是空气）")
    for t in targets:
        url = t[3]
        host = url.split("/")[2]
        check(
            host == base_host,
            f"★★ 体检 URL 的主机 = 下载用的主机（{host} vs {base_host}）"
            "—— 不一样的话，唯一能下的源会被体检报成不通",
        )
        check(
            url.startswith(catalog.GINAN_S3_BASE),
            f"★ 而且它是**从那个常量拼出来的**（{url[:52]}…）"
            "—— 手写第二遍就会再次分叉",
        )
        check(
            not _re.search(r"ap-southeast-2", url),
            "★ 不是那个**实测不通**的区域端点",
        )

    # ⚠️ 全清单扫一遍：不许再有"手写的 peanpod 主机名"。
    #    这次的成因就是"另一处又写了一遍"，所以判据要覆盖**整个文件**。
    #
    # ⚠️⚠️ 这里**必须查 AST，不能查源码文本** —— 这个仓库已经踩过三次：
    #     被查的那段源码里**引用了当初那个 bug**（讲解它为什么是 bug），
    #     于是按关键词查会命中**自己的说明**，守卫被自己的文档绊倒。
    #
    #     我第一版就是 `re.findall(r"https?://peanpod[^\s\"']*", src)` ——
    #     **立刻红了**，而它抓到的是我自己刚写的那两行注释。
    #     反过来更糟：想绕过它，改一下注释就行 ——
    #     **一条能靠改注释绕过的守卫，等于没有守卫。**
    import ast as _ast

    tree = _ast.parse(
        (ROOT / "ginan_agent" / "ginan" / "probe.py").read_text(encoding="utf-8")
    )
    literals = [
        node.value
        for node in _ast.walk(tree)
        if isinstance(node, _ast.Constant)
        and isinstance(node.value, str)
        and "peanpod" in node.value
    ]
    check(
        not literals,
        f"★★ probe.py 的**代码**里没有手写的 peanpod 地址"
        f"（找到 {literals}）—— 有的话迟早和 catalog 分叉",
    )


def test_judge_matrix() -> None:
    section("① 响应判断：状态码 + 响应体 → 能不能用")

    cases = [
        # (状态码, 响应体开头, 期望可用, 期望说明里含什么, 说明)
        (206, b"#cP2026 9 2 0 0 0.00000000", True, "正常",
         "206 部分内容（Range 请求的正常响应）→ 可用"),
        (200, b"     1.4            M   ANTEX VERSION", True, "正常",
         "200 + 真实数据 → 可用"),
        (200, LOGIN_PAGE, False, "需要登录",
         "⚠️ 200 + 登录页 → **不可用**（这就是那个假阳性）"),
        (200, b"<!DOCTYPE html><html>Not Found", False, "网页",
         "200 + 普通错误网页 → 不可用"),
        (401, b"", False, "需要认证", "401 → 需要认证"),
        (403, b"", False, "需要认证", "403 → 需要认证"),
        (404, b"", True, "不存在",
         "404 → **源是可达的**，只是这个文件没有（比如日期太新还没发布）"),
        (500, b"", False, "500", "500 → 异常"),
    ]

    for status, body, want_ok, want_text, label in cases:
        ok, detail = net.judge_data(status, body)
        good = ok == want_ok and want_text in detail
        check(good, f"{label}   → 实际：{detail}")


def test_head_ok_reads_before_close() -> None:
    section("② head_ok 必须在关闭连接**之前**读完判断所需的内容")

    # ⚠️ 这是那个假阳性的**直接回归测试**。
    #
    #    错误的写法：resp.close() 之后才去 iter_content → 读到空 → 判成"正常"
    #    这个断言会抓住它。
    resp = FakeResponse(200, LOGIN_PAGE)
    session = FakeSession(resp)
    ok, ms, detail = net.head_ok(session, "https://example.invalid/x")

    check(resp.closed, "用完之后连接被关掉了（不能泄漏）")
    check(
        not resp.iterated_after_close,
        "判断所需的响应体是在 close **之前**读的（假阳性的根源）",
    )
    check(not ok, "登录页被判为不可用")
    check("需要登录" in detail, f"说明里指出了需要登录（实际：{detail}）")


def test_head_ok_exception_handling() -> None:
    section("③ 网络异常要翻译成人话，不能把原始异常丢给用户")

    cases = [
        (TimeoutError("timed out"), "代理"),
        (OSError("getaddrinfo failed"), "解析"),
        (Exception("ProxyError: bad proxy"), "代理"),
    ]
    for exc, want in cases:
        ok, ms, detail = net.head_ok(FakeSession(exc), "https://example.invalid/x")
        good = (not ok) and want in detail
        check(good, f"{type(exc).__name__:14} → {detail}")


# =============================================================================
# ④ 代理配置
# =============================================================================


def test_proxy_config() -> None:
    section("④ 代理三种模式")

    # auto：返回 None，表示"让 requests 自己按环境变量处理"
    auto = net.ProxyConfig(mode="auto")
    check(auto.to_requests() is None, "auto 模式交给环境变量")
    check("系统代理" in auto.describe(), f"描述可读：{auto.describe()}")

    # none：返回空字典，表示"显式不用代理"
    none = net.ProxyConfig(mode="none")
    check(none.to_requests() == {}, "none 模式返回空字典（显式直连）")

    # manual：两个都填
    both = net.ProxyConfig(mode="manual", http="http://127.0.0.1:7890",
                           https="http://127.0.0.1:7890")
    check(
        both.to_requests() == {"http": "http://127.0.0.1:7890",
                               "https": "http://127.0.0.1:7890"},
        "manual 两个地址都填 → 原样使用",
    )

    # manual：只填一个 → 另一个沿用它（大多数代理两者同址）
    only_https = net.ProxyConfig(mode="manual", https="http://127.0.0.1:7890")
    got = only_https.to_requests()
    check(
        got is not None and got.get("http") == got.get("https"),
        "manual 只填 https → http 沿用它（省得用户填两遍）",
    )

    # manual 但什么都没填 → None，相当于 auto，不该崩
    empty = net.ProxyConfig(mode="manual")
    check(empty.to_requests() is None, "manual 但没填地址 → 退回默认行为，不崩")


def test_session_build() -> None:
    section("⑤ 会话构建")

    s = net.build_session(net.ProxyConfig(mode="none"))
    check("ginan-agent" in s.headers.get("User-Agent", ""), "带上了项目自己的 UA")
    check(s.trust_env is False, "显式直连时连环境变量也不认")

    s2 = net.build_session(net.ProxyConfig(mode="auto"))
    check(s2.trust_env is True, "auto 模式尊重环境变量")
    s.close()
    s2.close()


# =============================================================================
# ⑥ 取数计划
# =============================================================================


def test_plan_run_multi() -> None:
    section("⑥a ★★ 多站：观测按站分叉，产品只有一份；而且**不许说成网络解**")

    # ⚠️⚠️ 这一节盯的是"把单站代码搬到多站上"最典型的那个错误：
    #     **把全球统一的产品也按站下一份**。
    #
    #     SP3/CLK/ERP/ATX/导航 是**全球统一、与测站无关**的 ——
    #     三个站要的是**同一份** SP3，不是三份。
    #     按站各下一份的话：HTTP 请求数翻三倍、磁盘多两倍，
    #     而**结果一模一样**（同一个文件下载三遍），
    #     用户看到的只是"怎么这么慢、怎么这么大"。
    #
    #     ⚠️ 这个形状这个仓库已经踩过一次：广播星历曾经被
    #        `ga_rinex_url(station, day, "nav")` 当成"每站各一份"去要，
    #        而那是个**全球文件**（见 catalog.py 里 nav 那一段）。
    d = date(2026, 9, 2)
    one = catalog.plan_run("ALIC", d)
    many = catalog.plan_run(["ALIC", "DARW", "CEDU"], d)

    # ---- ① 观测：一个站一项 ----
    obs1 = [f for f in one.files if f.kind == "obs"]
    obs3 = [f for f in many.files if f.kind == "obs"]
    check(len(obs1) == 1, f"单站有 1 个观测项（实际 {len(obs1)}）")
    check(
        len(obs3) == 3,
        f"★★★ 三个站有**三个**观测项（实际 {len(obs3)}）"
        "—— 只造一个的话，配置里就只有第一个站在算，另两个白列了",
    )
    check(
        {f.filename.split("_")[0] for f in obs3} == {"ALIC", "DARW", "CEDU"},
        f"★★ 每个站的观测项都在：{[f.filename for f in obs3]}",
    )

    # ---- ② 产品：**还是那一份**（这才是这一节的重点）----
    prod1 = [f for f in one.files if f.kind != "obs"]
    prod3 = [f for f in many.files if f.kind != "obs"]
    check(
        len(prod3) == len(prod1),
        f"★★★ 产品项数**没变**（单站 {len(prod1)} → 三站 {len(prod3)}）"
        "—— 它们是全球统一的，多站要的是同一份",
    )
    check(
        [f.filename for f in prod3] == [f.filename for f in prod1],
        "★★ 而且就是同一批文件名（不是按站重造了一套）",
    )

    # ---- ③ 去重：同一个站写两遍只算一次 ----
    dup = catalog.plan_run(["alic", "ALIC", "darw"], d)
    check(
        dup.stations == ["ALIC", "DARW"],
        f"★★ 重复的站去掉、**保持给的顺序**（实际 {dup.stations}）",
    )
    check(
        len([f for f in dup.files if f.kind == "obs"]) == 2,
        "★★ 而且观测项也跟着只有两个（不然同一个站会被算两遍）",
    )

    # ---- ④ ★★ 措辞：**不许说成"网络解"** ----
    #
    # ⚠️⚠️ 这一条是**实测**逼出来的，不是抠字眼。
    #
    #     我一开始把多站叫"网络解（多站联合平差）"。然后真跑了一遍：
    #     三个站（ALIC/DARW/CEDU）整天，和**逐站单独跑**比 ——
    #     **重复性、偏移、收敛历元逐位相同**。
    #
    #     原因不是 bug：这份配置里**轨道和钟差是固定输入**（SP3/CLK），
    #     每个接收机的待估参数都不依赖别的站，数学上本来就该一样。
    #
    #     ⚠️ 要测站之间真的互相约束，得估**共同参数** ——
    #        那是 POD（估卫星轨道/钟差）和电离层建模做的事。
    #
    #     > 叫它"网络解"就是**说了一件没做到的事**。
    d = many.describe()
    check(
        "网络解" not in d and "联合平差" not in d,
        f"★★★ 描述里**不**说「网络解 / 联合平差」（{d[:44]}…）"
        "—— 实测各站结果与单站逐位相同，那是个没做到的承诺",
    )
    check(
        "多站" in d or "一次" in d,
        f"★★ 而是说清「多站、一次算完」（{d[:44]}…）",
    )
    check(
        "网络解" in one.describe() or "多站" not in one.describe(),
        "★ 单站时当然也不说多站",
    )
    check(
        catalog.RunPlan.__dataclass_fields__["stations"].type is not None,
        "站是一个**列表**（单站就是只有一项的列表，不为单站单开一个字段）",
    )

    # ---- ⑤ 一个站都不给 → 明确报错，不是造一份空计划 ----
    try:
        catalog.plan_run([], d)
        check(False, "空站列表应该报错")
    except ValueError as exc:
        check("测站" in str(exc), f"★ 空站列表报错且说得清（{exc}）")


def test_plan_run() -> None:
    section("⑥ 取数计划：算得对、多源齐全、纯函数")

    d = date(2026, 9, 2)   # 实测用过的那一天
    plan = catalog.plan_run("alic", d)

    check(plan.stations == ["ALIC"], "站号被规范化成大写")
    check(plan.day == d, "日期正确")

    # GPS 周和年积日 —— 实测确认过 2026-09-02 是第 2434 周第 245 天
    check(catalog.gps_week(d) == 2434, f"GPS 周计算正确（{catalog.gps_week(d)}）")
    check(catalog.year_doy(d) == "2026245", f"年积日格式正确（{catalog.year_doy(d)}）")

    kinds = {f.kind for f in plan.files}
    # ⚠️ 2026-09-20 加了 "snx"（卫星元数据）和 "bia"（码偏差）——
    #    为的是消掉 Ginan 每次都在打的那两族警告。
    #    ⚠️ 这是**集合**断言（不是"正好这几个"），所以它变红=有人加了新类型，
    #    那件事本身值得被看见 —— 新类型要配到配置里才算真的接上。
    check(
        kinds == {"atx", "sp3", "clk", "erp", "obs", "nav", "snx", "bia"},
        f"文件种类齐全：{sorted(kinds)}",
    )

    # ---- 多源覆盖 ----
    #
    # ⚠️ 这里要区分两类文件，不能一刀切要求"都有多个源"：
    #
    #     全球统一（atx/sp3/clk/erp/**nav**）—— 各大数据中心都有副本，
    #        所以**必须**配多个源，这是"网络不代表所有人"的落地点。
    #
    #     测站专属（**只有 obs**）—— 只存在于运营它的机构那里。
    #        ALIC 是澳站，所以只有 GA 有。这不是设计缺陷而是数据的物理归属。
    #        （见 catalog.plan_run 里那段说明）
    #
    # ⚠️⚠️ **`nav` 曾经被错分到"测站专属"那一类，这里和代码犯的是同一个错。**
    #
    #     广播星历是卫星播发的，**全球任何地方收到的是同一份** ——
    #     它和测站毫无关系。把它当成测站专属，于是给它配了
    #     `ga_rinex_url(station, ...)`，而非 GA 的测站查询返回空数组，
    #     那一项**必定下载失败**。
    #
    #     ⚠️ 这个分类错误在**测试和代码里同时存在** ——
    #        所以测试永远抓不到它。**测试如果照着错误的心智模型写，
    #        就提供不了任何保护，只会给那个错误盖章。**
    #
    #     👉 真正的不变量在下面 test_global_files_ignore_station()：
    #        **全球文件的 URL 不能随测站变。**
    GLOBAL_KINDS = {"atx", "sp3", "clk", "erp", "nav"}
    globals_ = [f for f in plan.files if f.kind in GLOBAL_KINDS]
    multi = [f for f in globals_ if len(f.urls) >= 2]
    check(
        len(multi) == len(globals_),
        f"全球统一的文件都配了 ≥2 个源（{len(multi)}/{len(globals_)}）",
    )

    station_specific = [f for f in plan.files if f.kind == "obs"]
    check(
        all(len(f.urls) >= 1 for f in station_specific),
        f"测站专属文件（观测数据）至少有一个源（{len(station_specific)} 个）",
    )

    # ---- 一次性文件要标记出来 ----
    atx = next(f for f in plan.files if f.kind == "atx")
    check(atx.one_time, "天线文件被标记为一次性（缓存后不必再下）")
    sp3 = next(f for f in plan.files if f.kind == "sp3")
    check(not sp3.one_time, "轨道文件不是一次性的（每天都要新的）")

    # ---- URL 里不能有占位符没替换 ----
    for f in plan.files:
        for src, url in f.urls.items():
            check(
                "{" not in url and "}" not in url,
                f"{f.kind}/{src} 的 URL 没有残留占位符",
            )

    # ---- ⚠️ `urls` 的值要么是 http(s) URL，要么是**登记过的**待解析标记 ----
    #
    # ⚠️ 这条是 2026-09-20 加的，动机很具体：
    #    武大的值是 `lookup://…`，**它不是一个 URL**。
    #    而上面那条"没有占位符"的检查**拦不住**它 —— 它本来就没有花括号。
    #
    #    一个不是 URL 的东西混在 `urls` 里，最危险的后果是：
    #    有人后来写个新函数遍历 `urls` 直接去 GET，**得到的是一个
    #    根本不是地址的字符串**。所以这里把规矩写死：
    #
    #        要么 http(s)://…        要么 lookup://<已登记的源>/…
    #
    #    ⚠️ 第二类还必须**在 `NEEDS_LOOKUP` 里登记过** —— 光有前缀不算，
    #       否则随便什么字符串加个前缀就能混进来。
    # ⚠️⚠️ 这条守卫**当天就抓到了一次**，值得记下来：
    #     写完它几个小时后加「武大 FTP」，`ftp://` 的值当场被它拦下。
    #     **守卫的价值不在于它今天全绿，而在于它会在你越界时说话。**
    #
    #     被拦下之后要做的**不是删掉守卫**，而是判断"新的这一类算不算越界"：
    #     FTP 是一个真能 GET（能下载）的地址，和 http 同类 —— 所以放行。
    #     而 `lookup://` 不是地址（它要先查一次才知道地址）—— 所以它要
    #     **单独登记**，光有前缀不算。
    bad_scheme: list[str] = []
    for f in plan.files:
        for src, url in f.urls.items():
            if url.startswith(("http://", "https://", "ftp://")):
                continue
            if sources.is_lookup(url) and src in sources.NEEDS_LOOKUP:
                continue
            bad_scheme.append(f"{f.kind}/{src} = {url[:50]}")

    for b in bad_scheme:
        print(f"      ⚠️ 既不是 http URL 也没登记：{b}")
    check(
        not bad_scheme,
        "★ `urls` 的值要么是 http(s) URL，要么是**登记过的**待解析源",
    )

    # ---- 待解析的源，必须都带了日期 ----
    #
    # ⚠️ 武大按天索引，**没有日期就查不到**（`sources.resolve` 会返回 None）。
    #    而它失败的方式是**静默降级到下一个源** —— 也就是"一直用慢的那个"，
    #    看起来一切正常。所以这条必须测。
    for f in plan.files:
        if any(sources.is_lookup(u) for u in f.urls.values()):
            check(
                f.day == plan.day,
                f"{f.kind} 有待解析的源，且带了正确日期（{f.day}）",
            )

    # ---- 待解析的名字必须是**远端的文件名**，不是落盘名 ----
    #
    # ⚠️⚠️ 这条是一个**真实踩过的坑**的形状：
    #
    #    武大库里存的是 `…_ORB.SP3.gz`（远端名），
    #    而计划里的 `filename` 是 `…_ORB.SP3`（落盘名，压缩后缀要去掉）。
    #
    #    第一版写成 `lookup_token("whu", "jmxl", names[kind])` —— **少一个 .gz**，
    #    于是**永远查不到**，然后**静默降级到 BKG**：
    #    文件下下来了、内容也对、测试全绿 —— 只是**一直是慢的那个源**。
    #
    #    > **降级如果是静默的，就没人会发现它在降级。**
    #
    #    ⚠️ 这就是为什么这条断言值钱：它不看"能不能下"，只看
    #       "有没有可能查得中"。**一个永远查不中的 token 和没有 token 一样，
    #       但它看起来是有的。**
    for f in plan.files:
        for src, u in f.urls.items():
            if not sources.is_lookup(u):
                continue
            remote = u.rsplit("/", 1)[-1]

            # ⚠️⚠️ **观测数据是例外，而且是"另一种索引方式"，不是"放宽要求"。**
            #
            #    上面那条断言的前提是「**文件名事先知道**」—— 产品就是这样。
            #    而观测**不是**：它的清单 `obsList` **必须带站号**才能查
            #    （不带就是「请至少选择一个站」），返回的才是文件名。
            #    ⇒ 所以 token 里带的是**站号**，不是文件名。
            #
            #    ⚠️ 但它**照样要能被查中**（这正是上一条断言的价值）——
            #       只是判据换成了"看起来像个站号"：
            #       4 个大写字母。**别因为换了个判据就把它放过。**
            if f.kind == "obs" and src == "whu":
                check(
                    len(remote) == 4 and remote.isupper() and remote.isalpha(),
                    f"★ obs/whu 里带的是**站号**（观测按站索引，文件名事先不知道）：{remote!r}",
                )
                continue

            check(
                remote.endswith((".gz", ".Z")),
                f"★ {f.kind}/{src} 里带的是远端名（带压缩后缀）：{remote}",
            )

    # ---- 人话用途必须存在（新手看不懂在下载什么就不信任工具）----
    for f in plan.files:
        check(bool(f.purpose) and len(f.purpose) > 10,
              f"{f.kind} 有人话用途说明")

    check(plan.missing_from_all_sources == [], "没有'一个源都不知道去哪拿'的文件")


def test_old_date_naming() -> None:
    section("⑦ ★ 老日期用旧式短名 + .Z，新日期一点不能变")

    # ⚠️⚠️ 这一条来自一次**真实的运行失败**：
    #
    #    用户拿 2022-03-01 的数据来跑，三个精密产品文件
    #    （轨道/钟差/地球自转）在两个源上都 404 ——
    #    换了三次代理、连 CDDIS 都通了，还是 404。
    #
    #    根因不是网络：**IGS 在 GPS 周 2237 换过命名规则。**
    #    那天之前是短名 `igr21992.sp3.Z`，之后才是
    #    `IGS0OPSRAP_20220600000_01D_15M_ORB.SP3.gz`。
    #    而程序只会拼新式的。
    #
    #    （顺带：BKG 整个没有 2022 那周 —— 它的存档有洞。
    #      所以老日期只有 CDDIS 一条路，而 CDDIS 要账号。）
    old = date(2022, 3, 1)
    check(catalog.gps_week(old) == 2199, f"2022-03-01 是第 2199 周（{catalog.gps_week(old)}）")
    check(
        catalog.gps_dow(old) == 2,
        f"★ 星期几算对了（{catalog.gps_dow(old)}）—— 周二 = 2，"
        "用 ISO 的 weekday() 会整体差一天",
    )

    plan = catalog.plan_run("ALIC", old)
    prods = {f.kind: f for f in plan.files if f.kind in ("sp3", "clk", "erp")}

    check(len(prods) == 3, "三类精密产品都在计划里")
    sp3 = prods.get("sp3")
    if sp3:
        check(
            sp3.filename == "igr21992.sp3",
            f"★ 名字是**解压后**的短名（{sp3.filename}）—— "
            "不能带 .Z，否则配置里会引用一个磁盘上不存在的名字",
        )
        check(
            any(u.endswith("/2199/igr21992.sp3.Z") for u in sp3.urls.values()),
            f"★ URL 是旧式短名 + .Z（{list(sp3.urls.values())}）",
        )
        check(
            "bkg" not in sp3.urls,
            "★ 没有配 BKG —— 它根本没有那天，配了只会白等超时，"
            "而且它的 404 会被读成「产品还没发布」",
        )

    # ---- ⚠️ 反过度修正：新日期必须**一点没变** ----
    #
    #    改老路径时最容易顺手把新路径弄坏，而新路径才是日常用的那条。
    new = catalog.plan_run("ALIC", date(2026, 9, 2))
    nd = {f.kind: f for f in new.files if f.kind in ("sp3", "clk", "erp")}
    nsp3 = nd.get("sp3")
    if nsp3:
        check(
            nsp3.filename == "IGS0OPSRAP_20262450000_01D_15M_ORB.SP3",
            f"★ 新日期的名字没变（{nsp3.filename}）",
        )
        # ⚠️⚠️ 这一条**改过**（2026-09-20），改的是判据不是结论 —— 说明一下：
        #
        #    原来写的是 `set(nsp3.urls) == {"bkg", "cddis"}` ——
        #    那是**当时的状态快照**，不是不变量。所以加源时它必然红，
        #    而它红了以后有两种反应：
        #
        #        ❌ 把新源删掉，让测试变绿       —— 守卫反过来挡住了功能
        #        ✅ 问一句"我当初想守的是什么"   —— 是**"新日期不能坏"**
        #
        #    真正的不变量是这三条（下面各一条断言）：
        #      · 名字没变（还是新式长名）
        #      · **仍然是多源**（有备份，不是独苗）
        #      · 后缀还是 `.gz`（没被老日期的 `.Z` 逻辑污染）
        #
        #    ⚠️ **"至少 2 个"才是那个不变量，"正好 2 个"不是。**
        #       今天有 4 个源是好事，不是回归。
        check(
            len(nsp3.urls) >= 2,
            f"★ 新日期仍然是**多源**（{sorted(nsp3.urls)}）—— "
            "只有一个源的话，它一挂就整个卡住",
        )
        check(
            all(
                u.endswith(".gz") or sources.is_lookup(u)
                for u in nsp3.urls.values()
            ),
            "★ 新日期仍然是 .gz（或待解析的源），没被改成 .Z",
        )


def test_global_files_ignore_station() -> None:
    section("⑥b ★ 全球统一的文件，URL 不能随测站变")

    # ⚠️⚠️ 这是 bug① 的回归守卫。它为什么存在，值得说清楚。
    #
    #    原来 `plan_run` 给广播星历配的是 `ga_rinex_url(station, ...)` ——
    #    把测站传了进去。而广播星历是**卫星播发的，全球任何地方收到的
    #    是同一份**，和测站毫无关系。
    #
    #    后果：GA 的 API 对**没有收录的测站**返回 **HTTP 404，响应体是 `[]`**
    #    （2026-09-23 复核：DCMS / BRUX / JFNG，以及瞎编的 ZZZZ 全都一样），
    #    于是这一项**必定下载失败**。而失败信息看起来像"GA 超时" ——
    #    指向网络问题，离真正的根因（参数传错）非常远。
    #
    #    ⚠️ 这条记录原来写的是「返回**空数组**（实测 DCMS → `[]`）」——
    #       **只记了响应体，漏了状态码**。这个漏项后来害了一次：
    #       `fetch._resolve_query_url` 先判 `status_code != 200` 就返回了，
    #       **永远走不到**下面那句精心写的「这个源上没有该站该天的数据」——
    #       于是"这个源没收录这个站"被报成 `查询接口返回 HTTP 404`，
    #       而 404 读起来像"接口地址写错了"。
    #       已修（见 test_ga_missing_station），此处一并把记录补全。
    #
    #    ⚠️ 这个 bug 之所以活了很久，是因为**测试也把 nav 当成了测站专属**
    #       （见 test_plan_run 里那段注释）—— 代码和测试共享同一个误解，
    #       于是测试给这个错误**盖了章**。
    #
    #    👉 所以这条断言**换了个角度问问题**：
    #       不问"配了几个源"（那要靠分类），而问"**换个测站，URL 变不变**"。
    #       这个角度不依赖任何分类假设，也就不可能和代码犯同一个错。
    a = catalog.plan_run("ALIC", date(2026, 9, 2))    # 澳站（GA 自己家的站）
    b = catalog.plan_run("DCMS", date(2026, 9, 2))    # 中国站（GA 根本没有）

    ua = {f.kind: dict(f.urls) for f in a.files}
    ub = {f.kind: dict(f.urls) for f in b.files}

    # ---- 全球统一的：URL 必须**逐字相同** ----
    for kind in ("nav", "sp3", "clk", "erp", "atx"):
        if kind not in ua:
            continue
        check(
            ua[kind] == ub[kind],
            f"★ {kind:3} 的 URL 不随测站变（它是全球统一的）",
        )

    # ---- 测站专属的：必须**不同** ----
    # ⚠️ 反过来也要测：如果一个"测站专属"的东西换了站还一样，
    #    那说明测站压根没被用上（另一个方向的错）。
    check(
        ua["obs"] != ub["obs"],
        "观测数据的 URL **随测站变**（它是测站专属的）",
    )

    # ---- 更直接的证据：广播星历的 URL 里不该出现测站号 ----
    nav_urls = " ".join(ua["nav"].values()).upper()
    check("ALIC" not in nav_urls, "★ 广播星历的 URL 里不含测站号")
    check("BRDC" in nav_urls, "广播星历用的是 BRDC（全球合并产品）")


def test_plan_is_pure() -> None:
    section("⑦ 取数计划是纯函数（不联网、不写盘）")

    import tempfile
    from pathlib import Path as P

    with tempfile.TemporaryDirectory() as tmp:
        before = set(P(tmp).rglob("*"))
        catalog.plan_run("ALIC", date(2026, 9, 2))
        after = set(P(tmp).rglob("*"))
        check(before == after, "造计划没有产生任何文件")

    # 同样的输入必须得到同样的输出
    a = catalog.plan_run("ALIC", date(2026, 9, 2))
    b = catalog.plan_run("ALIC", date(2026, 9, 2))
    check(
        [f.filename for f in a.files] == [f.filename for f in b.files],
        "同样输入得到同样输出（确定性）",
    )


def test_alternative_filename_extraction() -> None:
    section("⑩ ★ 从下载地址里取文件名：两种形状都要取对")

    # ⚠️⚠️ 这条守着两个**实测踩过的**坑，都出在同一个函数上：
    #
    #     ① **iGMAS 的文件名不在路径里，在查询参数里**
    #
    #            ESA：  …/gnss-products/2434/ESA0OPSFIN_….SP3.gz
    #                   ↑ 路径最后一段就是文件名
    #            iGMAS： …/downItem/?fid=/products/products/1078/b1cb2a/isc10783.sp3.Z
    #                   ↑ 路径最后一段是**空的**，名字在 `fid=` 里
    #
    #        只取路径的话，iGMAS 会得到**空字符串** —— 而空名字在路径拼接里
    #        **一点提示都没有**：`dest_dir / ""` 就是目录本身，
    #        于是下载器去操作那个目录，报一个只有临时路径的 PermissionError。
    #
    #     ② **名字必须来自"那个源自己的地址"**
    #
    #        第一版把多个源的地址合进一个 FilePlan，文件名取第一个源的 ——
    #        结果 iGMAS 没有钟差、落到了 ESA，却仍然存成 iGMAS 的名字：
    #        **名字说是 iGMAS 的，内容其实是 ESA 的。**
    #        配置按文件名引用文件，于是"到底跑的谁的产品"在磁盘上看不出来。
    #        → 修法见 `catalog.alternative_files`：**按源分组**。
    from ginan_agent.ginan import catalog

    cases = [
        (
            "ESA：文件名在路径里",
            "https://navigation-office.esa.int/products/gnss-products/2434"
            "/ESA0OPSFIN_20262450000_01D_05M_ORB.SP3.gz",
            # ⚠️ 返回的是**带后缀**的名字 —— 去后缀是调用方（alternative_files）的事
            "ESA0OPSFIN_20262450000_01D_05M_ORB.SP3.gz",
        ),
        (
            "iGMAS：文件名在查询参数里",
            "http://www.igmas.org/Product/TreePage/downItem/"
            "?fid=/products/products/1078/b1cb2a/isc10783.sp3.Z",
            "isc10783.sp3.Z",
        ),
        (
            "武大：路径最后一段是动词 download，不是文件名 → 如实返回空",
            "http://www.igs.gnsswhu.cn/index.php/igs/download?year=2026&id=310281",
            "",
        ),
    ]

    for label, url, want in cases:
        got = catalog._name_from_url(url)
        check(got == want, f"★ {label} → {got!r}")

    # ⚠️ 取不出名字时**必须返回空**，绝不能瞎编一个 ——
    #    调用方（alternative_files）看到空就会拒绝，而不是拿它去拼路径。
    check(
        catalog._name_from_url("http://example.com/") == "",
        "★ 取不出名字时返回空（不编）—— 调用方据此拒绝，而不是去写目录",
    )


def test_plan_json() -> None:
    section("⑧ 计划能转成给模型看的 JSON")

    import json as _json

    plan = catalog.plan_run("ALIC", date(2026, 9, 2))
    raw = catalog.plan_to_json(plan)
    data = _json.loads(raw)

    check(data["stations"] == ["ALIC"], "JSON 里带站号（列表，单站也是一项）")
    check(data["gps_week"] == 2434, "JSON 里带 GPS 周")
    check(len(data["files"]) == len(plan.files), "文件数量一致")

    first = data["files"][0]
    check("purpose" in first, "每个文件带人话用途")
    check("available_from" in first, "每个文件带'能从哪些源拿'")
    check(
        all("needs_account" in s for s in first["available_from"]),
        "每个源标明'需不需要账号'（决定界面要不要引导注册）",
    )


# =============================================================================
# 主程序
# =============================================================================


def test_ga_missing_station() -> None:
    section("⑫ GA 没收录的站 —— 必须说「没有这个站」，不能报成 HTTP 404")

    # ⚠️⚠️ 这个守卫盯的是一个**已经活了一段时间**的错误信息。
    #
    #    2026-09-23 实测（逐个查 71 个 IGS 站名，日期 2025-06-15）：
    #
    #        56 个有数据 ✓
    #        13 个 —— BRUX / JFNG / LHAZ / MGUE / OHI3 / PAPE / SEY2 /
    #                 TIDB / WETT / ALRT …，以及**瞎编的** ZZZZ / ABCD
    #                 → 全部返回 **HTTP 404，响应体是 `[]`**
    #
    #    ⚠️ 注意这两半：**状态码是 404，体是 `[]`**。
    #       仓库里原来的记录只写了后半句（"返回空数组，实测 DCMS → `[]`"），
    #       而 `fetch._resolve_query_url` 是**先判状态码**的：
    #
    #           if resp.status_code != 200:
    #               return "", f"查询接口返回 HTTP {resp.status_code}"
    #           entries = resp.json()
    #           if not entries:
    #               return "", "这个源上没有该站该天的数据"   ← 走不到这儿
    #
    #       于是"这个源没收录这个站"被报成 `查询接口返回 HTTP 404`。
    #       **404 读起来像"接口地址写错了 / 服务器坏了"**，
    #       用户会去查网络 —— 而该做的是换个站、或者自己准备观测文件。
    #
    #    👉 这是本项目反复记的那条「**后端从不返回某个键，调用方一直在读**」
    #       的又一个变体：这里不是键，是**分支**。

    class _Resp:
        def __init__(self, status: int, body: bytes) -> None:
            self.status_code = status
            self._body = body

        def json(self):
            import json as _json

            return _json.loads(self._body.decode())

    class _Sess:
        def __init__(self, resp) -> None:
            self._resp = resp
            self.last_url = ""

        def get(self, url, **kw):
            self.last_url = url
            return self._resp

    ga = catalog.ga_rinex_url("BRUX", date(2025, 6, 15), "obs")

    # ---- ① 404 + 空数组 → 必须说"这个源没有收录这个站" ----
    sess = _Sess(_Resp(404, b"[]"))
    real, err = fetch._resolve_query_url(ga, session=sess)
    check(real == "", "没解析出地址（不能拿一个假地址去下载）")
    check(
        "没有收录" in err or "没有" in err,
        f"说明里指出了「这个站没有收录」（实际：{err!r}）",
    )
    check(
        "HTTP 404" not in err,
        f"**不能**把它报成裸的 HTTP 404（那会把人的注意力引向网络，实际：{err!r}）",
    )

    # ---- ② 200 + 空数组 → 仍然是"这个源上没有该站该天的数据" ----
    sess = _Sess(_Resp(200, b"[]"))
    real, err = fetch._resolve_query_url(ga, session=sess)
    check(
        "没有该站该天" in err or "没有" in err,
        f"200 + 空数组说的是「该站该天没有数据」（实际：{err!r}）",
    )
    check("404" not in err, "这一条不该出现 404 字样")

    # ---- ③ 200 + 有记录 → 拿到地址 ----
    sess = _Sess(_Resp(200, b'[{"fileLocation": "https://example.invalid/a.rnx"}]'))
    real, err = fetch._resolve_query_url(ga, session=sess)
    check(real.endswith("a.rnx") and err == "", f"正常情况能拿到地址（实际：{real!r} {err!r}）")


class _InstantSession:
    """一个"瞬间就把 256 KB 给完"的假会话 —— 用来验"快的源不被判成测不准"。"""

    def get(self, url, **kw):  # noqa: ARG002
        class _R:
            status_code = 206

            def iter_content(self, n):
                for _ in range(300):
                    yield b"x" * 1024

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()


def test_sources_are_ranked_by_measured_speed() -> None:
    section("⑯ ★★ 多个源时**测速**，按最快的那条走（不是写死谁在前）")

    # ⚠️⚠️ 由来（用户点的）：「不是谁在前，是测一下速，用最快的」。
    #
    #    背景是实测出来的一个 44 倍差距（同一份观测，2026-09-25 WUH2）：
    #        武大  1305 KB/s   →  整条链 9.4 秒
    #        GA      22 KB/s   →  5 MB 要 3.8 分钟
    #    根因是 GA 的观测下载走 AWS S3 `ap-southeast-2`，从这边就是慢。
    #
    #    ⚠️⚠️ **而写死顺序必然是错的**：换个用户、换个地区就可能反过来。
    #       第一版我写的就是"武大排第一"，还给它配了条守卫 ——
    #       那条守卫守的是**我拍的顺序**，不是"用最快的"。
    #       ⇒ 换成下面这些：守**行为**，不守顺序。
    from ginan_agent.ginan import fetch

    real_probe = fetch._probe_one
    try:
        # ---- ① 快的排前面（**故意让"排在后面的"更快**）----
        speeds = {"slow": 10.0, "fast": 900.0, "mid": 100.0}

        def fake(url, source, sess, timeout):  # noqa: ARG001
            return speeds.get(url)

        fetch._probe_one = fake  # type: ignore[assignment]
        cands = [("slow", "慢源", "slow", None),
                 ("fast", "快源", "fast", None),
                 ("mid", "中源", "mid", None)]
        ranked = fetch._rank_by_speed(cands, ctx=None, timeout=30)
        order = [c[1] for c in ranked]
        check(order == ["快源", "中源", "慢源"],
              f"★★ 按实测速度重排（拿到 {order}）—— 原来传入的顺序是 慢/快/中")

        # ---- ② 测不出来的**必须保留**，排在测出来的后面 ----
        #
        # ⚠️ 这一条是防"排序把源弄丢了"：测速失败 ≠ 源不可用
        #    （超时、限流、HEAD 被禁都可能）。丢掉它就等于
        #    "因为量不到而放弃一个本来能用的源"。
        def fake_some(url, source, sess, timeout):  # noqa: ARG001
            return 500.0 if url in ("a", "c") else None

        fetch._probe_one = fake_some  # type: ignore[assignment]
        cands2 = [("a", "A", "a", None), ("b", "B", "b", None), ("c", "C", "c", None)]
        ranked2 = [c[1] for c in fetch._rank_by_speed(cands2, ctx=None, timeout=30)]
        check(set(ranked2) == {"A", "B", "C"}, f"★★ 一个源都没丢（拿到 {ranked2}）")
        check(ranked2[-1] == "B", f"★★ 测不出来的排最后（拿到 {ranked2}）")

        # ---- ③ **一个都测不出来就原样返回** ----
        #
        # ⚠️ 这条是"排序是优化、不是前提"：测速全失败时，
        #    下载**必须照旧能走**，不能因为量不到就什么都不干。
        fetch._probe_one = lambda *a, **k: None  # type: ignore[assignment]
        cands3 = [("x", "X", "x", None), ("y", "Y", "y", None)]
        ranked3 = [c[1] for c in fetch._rank_by_speed(cands3, ctx=None, timeout=30)]
        check(ranked3 == ["X", "Y"], f"★★ 全测不出来 → 原顺序不动（拿到 {ranked3}）")
    finally:
        fetch._probe_one = real_probe  # type: ignore[assignment]

    # ---- ④ 有些源**根本不该去测** ----
    #
    # ⚠️ 测速是**额外打一次请求**，对下面这几类是错的：
    #    · CDDIS：下载要跟着跳转带凭据，直接 GET 会拿到登录页
    #    · FTP：requests 不认
    #    · iGMAS：**有限流**（连着打会被回 304+0 字节），不该为测速多打
    # ⚠️⚠️ **夹具必须带一个"会被调用就会成功"的会话** ——
    #    第一版传的是 `sess=None`，于是"被策略拦下"和"一调用就崩"
    #    **返回的都是 None**，两种情况的读数一模一样：
    #    把 FTP/CDDIS 那两条策略**删掉**，守卫**照样全绿**（实测）。
    #    ⇒ 夹具不贴真实，这条守卫就没有牙齿。
    called: list[str] = []

    class _WouldSucceed:
        """真去 GET 就会"成功"的假会话 —— 所以拿到 None **只能**是没去测。"""

        def get(self, url, **kw):  # noqa: ARG002
            called.append(url)

            class _R:
                status_code = 206

                def iter_content(self, n):
                    # 够快、够多：真被调用的话一定量得出来
                    for _ in range(300):
                        yield b"x" * 1024

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return _R()

    # ---- ⑤ ⚠️⚠️ **快的源不许被判成"测不准"** ----
    #
    #    这是我自己写出来又改掉的一个**反向 bug**，值得单独守：
    #
    #    第一版写的是「量到的窗口短于 1 秒 ⇒ 不可信 ⇒ 返回 None」。
    #    本意是"短窗口不可信"，但**对快的源恰好反了** ——
    #    实测武大 **0.8 秒就把 256 KB 下完了**（≈1300 KB/s），
    #    而那条判据把它判成 None，**把最快的源扔了**；
    #    于是排序里只剩慢的能被量出来，GA（22 KB/s）反而排到了前面。
    #
    #    > **一个把"太快"当成"测不准"的判据，会让测速系统专门挑慢的用。**
    #
    #    ⚠️ 判据必须是**字节数**（拿到了就是一次有效测量），不是秒数。
    instant = _InstantSession()
    v = real_probe("https://fast.example/x.gz", "bkg", instant, 10)
    check(v is not None, f"★★ 秒传的源也要量得出来（拿到 {v}）—— 别把'快'判成'测不准'")
    check(v is not None and v > 1000, f"★★ 而且量出来是**快**（拿到 {v}）")

    for url, src, why in (
        ("ftp://example.org/a.gz", "bkg", "FTP"),
        ("https://cddis.nasa.gov/x.gz", "cddis", "CDDIS"),
        ("https://igmas.org/x.gz", "igmas", "iGMAS（限流）"),
    ):
        called.clear()
        got = real_probe(url, src, _WouldSucceed(), 10)
        check(got is None, f"★ 不去测 {why}（拿到 {got}）")
        check(not called, f"★ 而且是**压根没发那一次请求**（发了 {called}）")


def test_aiub_is_an_alternative_not_a_mirror() -> None:
    section("⑰ ★★ AIUB 给的是**另一个分析中心**的解算 —— 不许混进镜像里")

    # ⚠️⚠️ 这条守的是一个**静默改结果**的失败：
    #
    #    `urls`（镜像）里的几条**必须是同一个产品**（`IGS0OPSRAP_…`），
    #    下哪条都一样。而 AIUB 给的是 `COD0MGXFIN_…` ——
    #    **CODE 分析中心自己的 MGEX 最终解**。
    #
    #    把它写进 `urls` 的话：镜像全挂时它会顶上，
    #    **而磁盘上跑的是谁的产品、配置里引用的是哪个名字，
    #    都不会有任何提示** —— 结果变了，用户不知道。
    #
    #    ⇒ 备选那条路（`plan.alternatives`）存在的全部意义就是这个：
    #      「不是镜像，是另一个分析中心的解算结果」+ **要用户点头**。
    from datetime import date

    from ginan_agent.ginan import catalog

    day = date(2026, 9, 19)
    plan = catalog.plan_run(["ALIC"], day)

    # ---- ① 镜像里不许出现 aiub ----
    bad = [
        f"{f.kind}/{src}"
        for f in plan.files
        for src in f.urls
        if src == "aiub"
    ]
    check(not bad, f"★★ AIUB **不在**镜像（`urls`）里（混进去了：{bad}）")

    # ---- ② 备选里必须有，而且 sp3/clk/erp 三类都要 ----
    for kind in ("sp3", "clk", "erp"):
        alts = plan.alternatives.get(kind) or {}
        for src in ("aiub", "gssc"):
            check(src in alts, f"★ {kind} 的备选里有 {src}（拿到 {list(alts)}）")

    # ---- ③ 路径要**拼得像真的** ----
    #
    # ⚠️ 采样率那一段**每种产品都不一样**（05M / 30S / 12H），
    #    是从 AIUB 自己的目录列表里读出来的，不是按规律推的。
    #    这里把实测下载成功过的那三个名字钉住 —— 改错了就是**永远 404**，
    #    而 404 会被读成"这个源没有那天数据"（静默降级到下一个源）。
    urls = catalog._aiub_urls(day)
    expect = {
        "sp3": "COD0MGXFIN_20262620000_01D_05M_ORB.SP3.gz",
        "clk": "COD0MGXFIN_20262620000_01D_30S_CLK.CLK.gz",
        "erp": "COD0MGXFIN_20262620000_01D_12H_ERP.ERP.gz",
    }
    for kind, want in expect.items():
        got = urls.get(kind, "")
        check(got.endswith("/" + want),
              f"★★ {kind} 的名字是实测下载过的那个（拿到 …{got[-40:]}）")

    # ---- ④ **名字和内容必须配套**：备选的落盘名来自真 URL ----
    #
    # ⚠️ 这条是"名字说是 IGS 的、内容其实是 CODE 的"那个形状的守卫。
    #    AIUB 的 URL 末尾就是 `COD0MGXFIN_…`，所以落盘名也必须是它。
    # ⚠️⚠️ **这一步必须把备选限制成只有 aiub** —— `alternative_files` 是
    #    **联网函数**（它要去问 iGMAS/ESA 那一周的真文件名）。
    #
    #    第一版我没限制，于是这条守卫**悄悄发了两次网络请求**
    #    （拿到 `isr10806.sp3` 和 `ESA0OPSFIN_…`）——
    #    而 `check_all.py` 开头写着「**不联网**、不调用模型、不产生任何费用」。
    #    ⇒ 一条偷偷联网的守卫，会在断网的机器上、或者 CI 里变成红。
    only_aiub = catalog.plan_run(["ALIC"], day)
    only_aiub.alternatives = {
        k: {"aiub": v["aiub"]}
        for k, v in only_aiub.alternatives.items() if "aiub" in v
    }
    groups, _notes = catalog.alternative_files(only_aiub, ["sp3"])
    names = [f.filename for fs in groups.values() for f in fs]
    check(
        any(n.startswith("COD0MGXFIN_") for n in names),
        f"★★ 备选落盘用的是**它自己的名字**（拿到 {names}）"
        "—— 不许把它存成 IGS0OPSRAP_…（那样磁盘上就看不出来是谁的产品了）",
    )



def test_noaa_cors_observation_source() -> None:
    section("⑱ ★★ NOAA 的 CORS 观测：只认明文那一份，老日期不给")

    # ⚠️⚠️ NOAA 同一天挂**两种**文件，而只有一种我们能用：
    #
    #        2024/300   .o.gz -> **404**   .d.gz -> 200
    #        2024/365   .o.gz -> 200      .d.gz -> 200     （实测）
    #        2025/001   .o.gz -> 200      .d.gz -> 200
    #
    #    `.d.gz` 是 **Hatanaka 压缩的 RINEX 2**，而本程序的 `crinex.py`
    #    **只支持 3.0** —— 给它那种文件，下游解不开。
    #    ⇒ 宁可说「这个源没有」，也不能给一份用不了的。
    from datetime import date

    from ginan_agent.ginan import catalog

    # ---- ① 新日期：有 noaa，而且拼出来是 `.o.gz` ----
    new_day = date(2026, 9, 27)
    urls = catalog.obs_urls_for("AB02", new_day)
    check("noaa" in urls, f"★ 新日期有 noaa（拿到 {list(urls)}）")
    noaa = urls.get("noaa", "")
    # ⚠️ 判据是 **类型位**：RINEX 2 短名是 `站号+年积日+序号.年+类型`，
    #    `o` = 明文观测，`d` = Hatanaka 压缩。**`o.gz` 结尾**才是我们要的。
    #    ⚠️ 我第一版写成了 `.o.gz`（多一个点）—— 而真名是 `ab022700.26o.gz`，
    #       `.` 属于年字段。**这条断言当场把它拦下了**（守卫抓到我自己的错）。
    check(noaa.endswith("o.gz") and not noaa.endswith("d.gz"),
          f"★★ 拼出来的是**明文**那一份（拿到 …{noaa[-14:]}）")
    check("/ab02/" in noaa, f"★★ 站号**小写**（拿到 {noaa}）—— 它的目录和小写站号一致")
    check("/2026/270/" in noaa, f"★ 路径里有年和年积日（拿到 {noaa}）")

    # ---- ② 老日期：**不给** ----
    old_day = date(2024, 1, 1)
    urls_old = catalog.obs_urls_for("AB02", old_day)
    check("noaa" not in urls_old,
          f"★★ 2025 前的日期**不给 noaa**（拿到 {list(urls_old)}）"
          " —— 那时只有 Hatanaka 的 .d.gz，本程序解不开")

    # ---- ③ 但其它源照旧 —— 别把这条当成「整类文件都不能下」 ----
    check("whu" in urls_old and "ga" in urls_old,
          f"★ 老日期仍然给 whu/ga（拿到 {list(urls_old)}）")


def test_nrcan_only_for_old_dates() -> None:
    section("⑲ ★★ NRCan 只补**老日期** —— 新长名那种没验过，不许给")

    # ⚠️ 加它的理由很具体：**老日期的 `igr*` 原来只有 CDDIS 一个源，
    #    而 CDDIS 要账号**。NRCan 不要账号。
    #
    # ⚠️ 但它**只有老式短名**（实测 `igr20624.sp3.Z` 能下、96 历元、头部 IGS）——
    #    新长名那种它有没有，**没验过**。
    #    而"没验过的东西不写进来"是这个仓库的规矩。
    from datetime import date

    from ginan_agent.ginan import catalog

    # ---- ① 老日期：有 nrcan，而且是短名 + `.Z` ----
    old_day = date(2022, 3, 1)
    sp3_old = [f for f in catalog.plan_run(["ALIC"], old_day).files if f.kind == "sp3"][0]
    check("nrcan" in sp3_old.urls, f"★ 老日期有 nrcan（拿到 {list(sp3_old.urls)}）")
    u = sp3_old.urls.get("nrcan", "")
    check(u.endswith(".Z"), f"★★ 是 `.Z`（拿到 …{u[-20:]}）—— 实测下的就是这种")
    check("/2199/" in u, f"★ 路径里是 GPS 周（拿到 {u}）")

    # ---- ② 新日期：**不给** ----
    new_day = date(2026, 9, 19)
    sp3_new = [f for f in catalog.plan_run(["ALIC"], new_day).files if f.kind == "sp3"][0]
    check("nrcan" not in sp3_new.urls,
          f"★★ 新日期**不给** nrcan（拿到 {list(sp3_new.urls)}）"
          " —— 新长名那种它有没有没验过")

    # ---- ③ 它是个**镜像**（IGS 的产品），不是别的 AC 的 ----
    #
    # ⚠️ 这条区分要紧：AIUB/GSSC 给的是 `COD0MGXFIN`（另一个 AC 的解算）
    #    ⇒ 只能进 `alternatives`。而 NRCan 给的就是 `igr*`（IGS 自己那份）
    #    ⇒ 可以进 `urls`（镜像）。**把两者搞混，就会静默换掉结果。**
    check(sp3_old.filename == "igr21992.sp3",
          f"★ 它的落盘名和 IGS 那份一致（拿到 {sp3_old.filename}）—— 所以是镜像")

def _fake_response(status: int, body: bytes):
    """一个够用的假响应 —— 只实现 `_probe_authed` 会碰的那几个成员。"""

    class _R:
        def __init__(self) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}
            self.closed = False

        def iter_content(self, n: int = 1):
            if body:
                yield body

        def close(self) -> None:
            self.closed = True

    return _R()


# CDDIS 上**实测存在**的那一个（2026-09-29，认证会话下过 385 字节的真 .Z 数据）。
#
# ⚠️ 写死是有意的 —— 这就是一条**测量记录**，和 `_POD_SECONDS_PER_EPOCH` 同一个性质。
#    改它之前先重新量一次，别照着"看起来更合理"改。
_CDDIS_PROBE_MEASURED = (
    "https://cddis.nasa.gov/archive/gnss/products/2199/igr21992.erp.Z"
)


def test_cddis_probe_goes_through_the_authenticated_path() -> None:
    section("⑳a ★★ 要账号的源必须走认证那条路")

    # ⚠️⚠️ 守的是一个**用户看得见、但谁都没发现**的坏法：
    #
    #    侧栏那个「NASA CDDIS」，原来是用**普通 GET** 探的
    #    （`net.probe_url` → `session.get(allow_redirects=True)`）。
    #    而 CDDIS 要**跨主机跳转**（cddis → urs.earthdata.nasa.gov → cddis），
    #    requests 在跨主机时会把 Authorization **剥掉** —— 显式设的 auth
    #    根本带不住（这正是 `credentials.cddis_get` 存在的唯一理由）。
    #
    #    后果：**用户填完凭据、界面上「保存并验证」都通过了，
    #    侧栏仍然显示「需要登录」，而且永远不变。**
    #    app.js 里还写着注释期待它变绿 —— 它绿不了。
    #
    #    ⚠️⚠️ 而本机一直看着是**好的** —— 因为家目录里恰好留着一份老的
    #        `C:\\Users\\<用户>\\_netrc`，而 requests 会自己去读它。
    #        这个 bug 是这样被盖住的。（见 credentials.py 末尾那第三条。）
    #
    # ⇒ 所以判据不是"字符串里写了 cddis_get"，而是**真的走没走那条路**。
    #    这里把两条路都换成假的，看调用发生在哪一边。
    from ginan_agent.ginan import credentials, probe

    calls: dict = {"cddis_get": 0, "sessions": 0, "probe_url": [], "status": 200,
                   "body": b"", "url": None}

    class _FakeSession:
        """⚠️ 必须有 `close()`。

        ⚠️ 第一版这里返回的是 `object()` —— 于是 `_probe_authed` 里的
           `finally: session.close()` 抛 AttributeError，异常被 `probe_all`
           吞成"探测过程出错"（reachable=False）。
           结果：**"未登录 → 判为不可用"那一条是假通过的** ——
           它通过的原因是"抛异常了"，不是"判对了"。
        """

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def fake_session(proxy=None, user=None, password=None):
        calls["sessions"] += 1
        return _FakeSession()

    def fake_get(session, url, timeout=60.0, stream=True):
        calls["cddis_get"] += 1
        calls["url"] = url
        if calls["body"] is None:
            raise TimeoutError("假超时")
        return _fake_response(calls["status"], calls["body"])

    def fake_probe_url(session, url, timeout=0.0, expect_data=True):
        calls["probe_url"].append(url)
        return True, 1, "不该走到这里（普通 GET 带不住 CDDIS 的凭据）"

    real = (credentials.cddis_session, credentials.cddis_get, net.probe_url)
    credentials.cddis_session = fake_session
    credentials.cddis_get = fake_get
    net.probe_url = fake_probe_url
    try:
        # ① 没登录：200 + 登录页 —— 必须是"需要登录"，而且要带注册指引
        calls.update(status=200,
                     body=b"<html><head><title>Earthdata Login</title></head>")
        s = probe.probe_all(only=["cddis"], timeout=5.0).sources[0]
        check(not s.reachable, "拿到登录页 → 判为不可用（不是绿勾）")
        check(s.icon == "auth", "★ 而且是 auth 态（该去注册），不是 fail（该查网络）")
        check("earthdata" in s.advice.lower(), "★ 给了可操作的建议（去注册的那个链接）")

        # ② 登录了：200 + 真数据（.Z 的魔数）—— 必须是 ok
        calls.update(status=200, body=b"\x1f\x9d\x90" + b"\x00" * 64)
        s = probe.probe_all(only=["cddis"], timeout=5.0).sources[0]
        check(s.reachable, "拿到真数据 → 判为可用")
        check(s.icon == "ok", "★ icon 是 ok，而 advice 是空的（没事要做）")
        check(s.advice == "", "通过的时候不给建议（给了反而像出问题）")

        # ③ 连不上：不可用
        calls.update(body=None)
        s = probe.probe_all(only=["cddis"], timeout=5.0).sources[0]
        check(not s.reachable, "没问到 → 判为不可用")

        # ④ ★ 决定性的一条：全程**一次都没走普通 GET**
        check(calls["cddis_get"] >= 3, f"★ 走的是 cddis_get（{calls['cddis_get']} 次）")
        check(calls["probe_url"] == [],
              "★★ 一次都没走普通 GET —— 走了的话凭据带不住，"
              "用户填对了也永远显示『需要登录』")
        check(calls["sessions"] >= 3, "每次都新开一个 CDDIS 会话（用完就关）")
        check(calls["url"] == _CDDIS_PROBE_MEASURED,
              "探的就是清单里那一个地址")

        # ⑤ 对照：别的源**必须**还走普通 GET
        #    （不然这条"改用认证通路"就变成了"所有源都套一层 CDDIS 认证"）
        calls["probe_url"].clear()
        calls["cddis_get"] = 0
        probe.probe_all(only=["bkg"], timeout=5.0)
        check(len(calls["probe_url"]) == 1, "★ 对照：bkg 仍然走普通 GET")
        check(calls["cddis_get"] == 0, "★ 对照：bkg 不走 CDDIS 那条路")
    finally:
        credentials.cddis_session, credentials.cddis_get, net.probe_url = real


def test_cddis_probe_url_is_one_we_measured() -> None:
    section("⑳b ★ CDDIS 的探测地址必须是**实测存在**的那个")

    # ⚠️ 守的是一次**猜出来的路径活成了事实**：
    #
    #    原来的探测地址是 `gnss/products/rcvantex/igs20.atx.gz` ——
    #    注释里老老实实写着"没验证过"，然后就这么用了很久。
    #    有了账号一量：文件 404、**目录整个不存在**、父目录 1828 项里
    #    没有任何含 atx/antex/rcv 的项。
    #
    #    而它造成的**不是**"少一个源"，是"**绿灯**"：
    #    `net.judge_data` 故意把 404 判成"可达"（"源通了但今天的文件还没发布"），
    #    于是侧栏显示 ok、细节写着"文件不存在"、advice 空的 ——
    #    用户看不到任何可做的事，而那个解释**永远是错的**
    #    （不是"日期太新"，是这个地址从来不存在）。
    from ginan_agent.ginan import probe

    targets = [t for t in probe.PROBE_TARGETS if t[0] == "cddis"]
    check(len(targets) == 1, "清单里正好有一个 cddis（多了少了都要重看这段）")
    if not targets:
        return
    t = targets[0]
    url, needs_auth, expect_data = t[3], t[4], t[5]
    check(url == _CDDIS_PROBE_MEASURED,
          f"★ 就是 2026-09-29 实测下过数据的那一个（{url.rsplit('/', 1)[-1]}）")
    check(needs_auth is True,
          "★ 带『要账号』标记 —— 没这个标记就会退回普通 GET（见 ⑳a）")
    check(expect_data is True,
          "★ 要求的是**数据文件**（「只要服务器应答就算通」会把登录页判成通）")
    check("rcvantex" not in url, "★ 不是那条实测 404 的死路径")
    check("igs20.atx" not in url, "★ 也不是那个根本不存在的天线文件")
    check(url.startswith("https://cddis.nasa.gov/archive/"), "在 CDDIS 的 archive 树里")


def test_atx_has_no_dead_cddis_source() -> None:
    section("⑳c ★ 实测不存在的那个天线地址，不许回来")
    import ast

    # ⚠️ 判断"要不要留一个源"的标准是**实测能不能下**，不是"文档上有没有"：
    #    天线表那边已经有两个实测能下的源（BKG 真下过 61 MB、IGS 官网 206 可达），
    #    CDDIS 那一栏是纯负债 —— 而**一个错的源比没有源更糟**：
    #    它会让下一个人以为"CDDIS 上有天线文件"，然后去查"为什么下不动"。
    check(not hasattr(catalog, "CDDIS_ATX_URL"), "catalog 里没有 CDDIS_ATX_URL 了")
    check(
        list(catalog.atx_plan().urls) == ["bkg", "igs"],
        f"天线表只剩两个实测能下的源（实际 {list(catalog.atx_plan().urls)}）",
    )

    # ⚠️⚠️ 这一段**必须查 AST 的赋值语句，不能查源码文本**：
    #    `catalog.py` 里那两处注释/文档正写着这个死地址（讲解它为什么被删），
    #    按关键词查会命中**我自己的说明**。这个仓库已经踩过三次这个坑
    #    （见 ⑨ 里那条"守卫被自己的文档绊倒"）。
    #
    # ⚠️ 而且只查**模块级的赋值**，不查所有字符串字面量 ——
    #    文档字符串本来就该提到它（那是这段历史的记录）。
    tree = ast.parse(Path(catalog.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for value in ast.walk(node.value):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if "rcvantex" in value.value:
                    offenders.append(node.lineno)
    check(not offenders,
          f"★ 没有任何模块级常量指回那条死路径（命中行号：{offenders}）")


def main() -> int:
    print("=" * 60)
    print("网络层自检 —— 判断逻辑 / 代理 / 取数计划")
    print("=" * 60)
    print("⚠️ 这个脚本完全不联网，测的是判断逻辑而不是连通性。")

    test_probe_uses_the_same_endpoint_as_download()
    test_judge_matrix()
    test_head_ok_reads_before_close()
    test_head_ok_exception_handling()
    test_proxy_config()
    test_session_build()
    test_plan_run()
    test_plan_run_multi()
    test_old_date_naming()
    test_global_files_ignore_station()
    test_plan_is_pure()
    test_plan_json()
    test_alternative_filename_extraction()
    test_ga_missing_station()
    test_sources_are_ranked_by_measured_speed()
    test_aiub_is_an_alternative_not_a_mirror()
    test_noaa_cors_observation_source()
    test_nrcan_only_for_old_dates()
    test_cddis_probe_goes_through_the_authenticated_path()
    test_cddis_probe_url_is_one_we_measured()
    test_atx_has_no_dead_cddis_source()

    print()
    print("=" * 60)
    total = PASS + FAIL
    if FAIL == 0:
        print(f"全部通过 ({PASS}/{total})")
    else:
        print(f"有失败：{FAIL} 项未通过（通过 {PASS}/{total}）")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
