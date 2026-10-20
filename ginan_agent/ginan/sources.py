"""有些源没法用"一个 URL"表示 —— 它们的下载地址只能在**下载时**查。

============================ 为什么需要这一层 ============================
`catalog.plan_run` 是**纯函数**（不联网、不写盘），它把"要什么、从哪拿"
算出来，把 URL 直接写进 `FilePlan.urls`。

对大多数源这是成立的 —— BKG / IGS / CDDIS / GA 的文件地址**只跟文件名有关**，
事先拼得出来。

⚠️ 但**武汉大学 IGS 数据中心**不是这样。它 2024-06 改版之后：

    · 目录树关掉了 —— 老式的 `/pub/gnss/products/{周}/xxx.sp3.gz` 全部 404
      （实测 `/pub/`、`/pub/gnss/`、`/pub/gnss/products/` 全是 ThinkPHP 404 页）
    · 下载改成两步：

        POST http://www.igs.gnsswhu.cn/index.php/igs/jmxlList   精密产品
        POST http://www.igs.gnsswhu.cn/index.php/igs/gbxlList   广播星历
             → {"rows":[{id, year, filename, size, path}, ...]}
        GET  http://www.igs.gnsswhu.cn/index.php/igs/download?year={year}&id={id}

    · `id` 是**数据库行号**，事先猜不出来。

👉 所以它的地址只能在下载那一刻查。这就是这个模块存在的全部理由。

============================ 为什么不在计划阶段查 ============================
`plan_run` 的文档里明写着"**纯函数 —— 不联网、不写盘**"，
而 `check_network.py` 整套离线测试都建立在这条性质上。

把网络请求塞进计划阶段，那条性质就没了 —— 换来的只是"少写一层"。

> **计划是"打算做什么"，下载是"真的去做"。**
> 需要联网的事情，属于后者。

============================ 为什么值得为它写这么多 ============================
因为**快**。同一台机器、同一个会话、同一个文件（2026-09-02 的 CLK）实测：

        武汉大学   1792 ~ 3165 KB/s
        德国 BKG     47 ~   60 KB/s        ← 原来的唯一主力源

差 **20~30 倍**。用户的原话就是"下载源太少了"。

⚠️ 而且它顺带解决了另一个问题：**老日期（2022）现在只有 CDDIS 一个源，
   而 CDDIS 要账号**。武大有老式短名（`igr21992.sp3.Z`，实测可下），
   于是老日期**不用账号也能下**了。

============================ ⚠️ 它有三个坑，每个都实测过 ============================
① **每个响应尾部追加了 ~19 KB 的 ThinkPHP 调试 HTML**（`<div id="think_page_trace">`）。

   文件本身是完整的 gzip 流，垃圾挂在后面。直接 `gzip.open()` 会抛
   `BadGzipFile` —— 也就是说：**下载"成功"了，然后每一个文件都解压失败。**

   👉 必须按那个标记截断。**不能按接口返回的 `size` 字段截** ——
      它是**过期的**（实测 ALIC 观测那条写 6,267,332，真实只有 6,259,787）。
      按它截会切进 HTML 里。见 `strip_trailer()`。

② **文件不存在时返回 200 + 0 字节**，不是 404。

   👉 这个已经被现有的**内容校验**兜住了（`fetch.py` 里"认不出来 = 失败"），
      0 字节过不了那一关。**不是我们新加的防御，是原来就有的。**

③ **`year` 参数传错不报错，会静默给你另一个文件**（实测：2026 的 id 配 year=2024
   → 返回了一个加拿大站的观测文件）。

   👉 所以 `year` 必须原样用列表行里那个，而且**必须有内容校验兜底** ——
      回到 ②，同一道关。

============================ 已知它**没有**的 ============================
`igs20.atx`（天线表）、`igs_satellite_metadata.snx`、`sat_yaw_bias_rate.snx`
—— 枚举了整整一周的全部文件，一个 `.atx` 都没有。**不是路径没找到，是库里没有。**

这几样仍然只能从 IGS 官网 / BKG 取。**别在这上面浪费时间重查一遍。**
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

# 解析标记。
#
# ⚠️ 用一个**显眼的**前缀，而不是"直接把文件名塞进 urls 里"——
#    因为 `urls` 的语义是"源 key → URL"，
#    塞个不是 URL 的东西进去，读代码的人会以为它是个坏 URL。
#    加个前缀至少让它**看起来就不像 URL**。
LOOKUP_PREFIX = "lookup://"

# 哪些源需要运行时解析。
#
# ⚠️ 这张表要**短**。每加一个，就多一处联网的失败点。
#    能事先拼出 URL 的源，一律不要写进来。
NEEDS_LOOKUP: frozenset[str] = frozenset({"whu", "esa", "igmas"})


# =============================================================================
# 武汉大学 IGS 数据中心
# =============================================================================

# ⚠️ 三个域名细节，全是实测踩出来的：
#
#   · 必须带 `www.` —— 不带 www 的 `igs.gnsswhu.cn` DNS 能解析但连接直接失败
#   · 必须用 **http** —— https 的证书**已过期**（certificate has expired）
#   · 路径是 `/index.php/igs/...`，不是老文档里的 `/pub/gnss/...`
WHU_BASE = "http://www.igs.gnsswhu.cn/index.php/igs"

# 尾部调试垃圾的起始标记
WHU_JUNK_MARKER = b'<div id="think_page_trace"'

# 截断时只扫文件末尾这么多字节 —— 垃圾恒在末尾（~19 KB），
# 没必要为了找它把 60 MB 的天线文件整个读一遍。
TAIL_SCAN = 64 * 1024

# 两种文件清单。**分开的不是"分类 tidy"，是因为它们是两个不同的接口** ——
# 精密产品查 jmxlList，广播星历查 gbxlList。查错了就是 0 条。
WHU_LISTS = ("jmxl", "gbxl")

# (清单类型, 日期) → {文件名: (id, year)}
#
# ⚠️ 缓存按**天**存，不是按文件存 —— 一次 POST 就能拿到那一天的全部文件，
#    而一天有 3000 多条。所以算 sp3/clk/erp 三个文件只需要 1 次请求。
_whu_index: dict[tuple[str, date], dict[str, tuple[int, int]]] = {}


def lookup_token(source: str, list_type: str, filename: str) -> str:
    """造一个"待解析"的占位值，放进 `FilePlan.urls`。

    形如：`lookup://whu/jmxl/IGS0OPSRAP_20262450000_01D_15M_ORB.SP3.gz`
    """
    return f"{LOOKUP_PREFIX}{source}/{list_type}/{filename}"


def is_lookup(value: str) -> bool:
    """这个值是不是"待解析"（而不是一个能直接 GET 的 URL）。"""
    return value.startswith(LOOKUP_PREFIX)


def resolve(
    value: str,
    day: date | None,
    session=None,
) -> str | None:
    """把一个"待解析"的值变成一个**真的能 GET 的 URL**。

    返回 None 表示"这次没查到"（网络不通、文件不在库里…）——
    ⚠️ **不要抛异常**：调用方（fetch）的策略是"这个源不行就换下一个"，
       而抛异常会把那条正常的降级路径变成崩溃。
    """
    if not is_lookup(value):
        return value

    # ⚠️ 格式是 `lookup://<源>/<剩下的一整段>`，**剩下那段由各个源自己解释**：
    #
    #        lookup://whu/jmxl/IGS0OPSRAP_…SP3.gz     ← whu 要 <清单接口>/<文件名>
    #        lookup://esa/sp3                          ← esa 只要 <类型>
    #
    #    ⚠️ 所以只能按**第一个**斜杠切。"先切成三段再分发"的话，
    #       ESA 那种两段的值会被判成格式错误 —— 而它是对的。
    body = value[len(LOOKUP_PREFIX):]
    if "/" not in body:
        return None
    source, rest = body.split("/", 1)

    if day is None:
        # 没有日期就查不了 —— 这两种源都按天索引
        return None

    try:
        if source == "whu":
            # ⚠️ 两种情况，**先判 obs**：
            #
            #     lookup://whu/obs/ALIC          ← 观测：按**站号**查，文件名事先不知道
            #     lookup://whu/jmxl/IGS0OPS…SP3.gz ← 产品：按**文件名**查
            #
            #   分叉的理由不"整齐"，是**接口不一样**：产品那个
            #   `jmxlList` 一次返回一天三千条、按文件名索引；
            #   而观测那个 `obsList` **必须带站号**（不带就是
            #   「请至少选择一个站」），返回的是站→文件。
            if rest.startswith("obs/"):
                station = rest[len("obs/"):].strip()
                row = _whu_obs_find(station, day, session)
                if row is None:
                    return None
                file_id, year = row
                return f"{WHU_BASE}/download?year={year}&id={file_id}"

            # rest = "<清单接口>/<文件名>"
            if "/" not in rest:
                return None
            list_type, filename = rest.split("/", 1)
            row = _whu_find(list_type, filename, day, session)
            if row is None:
                return None
            file_id, year = row
            # ⚠️ `year` **必须**用列表行里的那个。
            #    传错的话它不报错，会静默返回**另一个文件**（实测踩过）。
            return f"{WHU_BASE}/download?year={year}&id={file_id}"

        if source == "esa":
            return _esa_resolve(rest, day, session)

        if source == "igmas":
            return _igmas_resolve(rest, day, session)

        return None
    except Exception:  # noqa: BLE001
        # ⚠️ 网络层的任何异常都在这里消化掉 —— 见上面 docstring。
        return None


# (日期) → {站号: (id, year)}
#
# ⚠️ 和产品的那个索引**分开缓存**，因为键不一样：产品按文件名、观测按站号。
_whu_obs_index: dict[date, dict[str, tuple[int, int]]] = {}

# 观测清单里，哪些文件名是**我们能用的**。
#
# ⚠️⚠️ 这个判据是量出来的，不是想出来的。武大同一个站会挂**好几种**名字，
#     而它们的格式**不一样**（2026-09-29 实测 26 个站）：
#
#         ALIC00AUS_R_20262680000_01D_30S_MO.crx.gz   ← RINEX 3 + CrinEX ✓
#         bjfs2680.26d.Z / chan2680.26d.gz            ← **RINEX 2** + Hatanaka ✗
#
#     而本程序的 `crinex.py` **只支持 3.0**（它自己的说明里写着）。
#     拿 RINEX 2 那份去解码，最好的结果是报错，最坏的是**解出一半**。
#     ⇒ 所以只认**长名 + `.crx.gz`**，认不出来就当这个站没有。
#
# ⚠️ 实测覆盖率（26 个站的样本，`gs=d`）：**21 个站有，其中 19 个是长名**。
#     只有 BJFS / CHAN 两个站是"只有老式短名"—— 它们会被判成"没有"，
#     这是**对的**：宁可说没有，也不能给一份下游用不了的文件。
_WHU_OBS_OK = ".crx.gz"


def _whu_obs_find(
    station: str,
    day: date,
    session,
) -> tuple[int, int] | None:
    """在武大的**观测**清单里查一个站，返回 (id, year)。查不到返回 None。"""
    st = station.strip()
    if not st:
        return None

    index = _whu_obs_index.get(day)
    if index is None:
        index = {}
        _whu_obs_index[day] = index

    if st in index:
        return index[st]

    if session is None:
        from . import net

        session = net.build_session()

    iso = day.isoformat()
    payload = {
        "data": {
            "type": "obs",
            # ⚠️ `gs=d` 是"日文件"。另一个取值 `o` 实测 26 个站里只有 2 个有，
            #    而且全是老式短名（RINEX 2）—— 没有用。
            "gs": "d",
            # ⚠️ 站号**必须**给，而且是**数组**：不给就是
            #    「请至少选择一个站」，给字符串则当没给（两种都实测过）。
            "site": [st],
            "filename": "",
            "start_time": iso,
            "end_time": iso,
        },
        "start": 0,
        "limit": 50,
    }

    try:
        resp = session.post(f"{WHU_BASE}/obsList", json=payload, timeout=40)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    try:
        rows = resp.json().get("rows", [])
    except Exception:  # noqa: BLE001
        return None

    for r in rows:
        name = str(r.get("filename") or "")
        fid, year = r.get("id"), r.get("year")
        # ⚠️ 只看**长名那种**，见 `_WHU_OBS_OK` 的说明。
        if not name.endswith(_WHU_OBS_OK):
            continue
        if isinstance(fid, int) and isinstance(year, int):
            index[st] = (fid, year)
            return index[st]

    # 查到了但都不能用（比如只有 RINEX 2 那种）—— 记成"没有"，
    # 免得同一个站被反复查。⚠️ 缓存的是"这天没有**能用的**"，不是"这天没有文件"。
    return None


def _whu_find(
    list_type: str,
    filename: str,
    day: date,
    session,
) -> tuple[int, int] | None:
    """在武大的文件清单里找一个文件，返回 (id, year)。"""
    index = _whu_index_for(list_type, day, session)
    if index is None:
        return None
    return index.get(filename)


def _whu_index_for(
    list_type: str,
    day: date,
    session,
) -> dict[str, tuple[int, int]] | None:
    """取某天某类文件的 文件名 → (id, year) 索引（带缓存）。"""
    key = (list_type, day)
    if key in _whu_index:
        return _whu_index[key]

    if session is None:
        from . import net

        session = net.build_session()

    iso = day.isoformat()
    payload = {
        "data": {
            "type": list_type,
            "filename": "",
            # ⚠️ 检索窗口**硬上限 365 天**，而且带 filename 过滤 + 宽窗口
            #    会让服务端 SQL 拼坏（HTTP 500）。所以这里只查**一天**。
            "start_time": iso,
            "end_time": iso,
        },
        "start": 0,
        "limit": 5000,          # 实测一天 ~3000 条
    }

    resp = session.post(f"{WHU_BASE}/{list_type}List", json=payload, timeout=40)
    if resp.status_code != 200:
        return None

    # ⚠️ Content-Type 是 text/html，内容是 JSON。**别按 Content-Type 判断。**
    try:
        rows = resp.json().get("rows", [])
    except Exception:  # noqa: BLE001
        return None

    index: dict[str, tuple[int, int]] = {}
    for r in rows:
        name = r.get("filename")
        fid, year = r.get("id"), r.get("year")
        if name and isinstance(fid, int) and isinstance(year, int):
            index[name] = (fid, year)

    _whu_index[key] = index
    return index


# =============================================================================
# 欧洲空间局 ESOC（备选产品）
# =============================================================================

# ⚠️ 目录名是 `gnss-products`（**带连字符**）。`gnss_products` 是 404 —— 实测。
ESA_BASE = "https://navigation-office.esa.int/products/gnss-products"

# ⚠️ 一个**只解析一次**的完整目录，不是猜文件名。
#
#     为什么不能拼文件名：它的前缀**随年代变**，实测：
#         2026 年：ESA0**OPS**FIN_20262450000_01D_05M_ORB.SP3.gz
#         2022 年：ESA0**MGN**FIN_20220580000_01D_05M_ORB.SP3.gz
#     （OPS = 常规运行，MGN = MGEX）
#     写一套"哪年到哪年用哪个前缀"的规则，迟早会错 ——
#     而错了的后果是**静默换掉另一个产品**。
#
#     👉 列目录 = **问服务器要真名**，不用猜。
#        顺带还对重命名免疫（它改名我们也不用改代码）。
#
# ⚠️ 它的 SP3 是 **05M**（5 分钟），不是 IGS 那种 15M；
#    钟差是 **30S**。所以**不能拿 IGS 的文件名去拼**。
_ESA_KIND_SUFFIX = {
    "sp3": "_ORB.SP3.gz",
    "clk": "_CLK.CLK.gz",
    "erp": "_ERP.ERP.gz",
}
_esa_index: dict[date, dict[str, str]] = {}


def _esa_index_for(day: date, session) -> dict[str, str]:
    """列出 ESA 那一周的目录，找出**这一天的** three 类产品。

    返回 {kind: 完整 URL}。找不到的 kind 不在里面。
    """
    if day in _esa_index:
        return _esa_index[day]

    if session is None:
        from . import net

        session = net.build_session()

    import re

    week = _gps_week(day)
    listing = f"{ESA_BASE}/{week}/"
    resp = session.get(listing, timeout=40)
    if resp.status_code != 200:
        return {}

    # ⚠️ ESA 的文件名里带年积日（`20262450000`）。
    #    用**年积日 + 0000** 去匹配，而不是"日期相近就行" ——
    #    一周里有 7 天，匹配松了会拿到**前一天**的轨道。
    yd = f"{day.year}{day.timetuple().tm_yday:03d}"
    needle = f"_{yd}0000_"

    found: dict[str, str] = {}
    for href in re.findall(r'href="([^"?/][^"]*)"', resp.text):
        if not href.startswith("ESA") or needle not in href:
            continue
        for kind, suffix in _ESA_KIND_SUFFIX.items():
            if href.endswith(suffix) and kind not in found:
                found[kind] = listing + href

    _esa_index[day] = found
    return found


def _esa_resolve(rest: str, day: date, session) -> str | None:
    """`lookup://esa/<kind>/<任意>` → ESA 那个产品的真实地址。"""
    kind = rest.split("/", 1)[0]
    if kind not in _ESA_KIND_SUFFIX or day is None:
        return None
    return _esa_index_for(day, session).get(kind)


def _gps_week(d: date) -> int:
    """GPS 周。

    ⚠️ **不在这里重写一遍算法**，而是调 catalog 那个。
       我第一版就是自己抄了一遍（还写了句"两边必须一致"）——
       那正是这个项目最反对的东西：**同一件事有两个实现，
       迟早只改一边**。而 GPS 周算错一天，取回来的就是**另一天**的轨道。

    ⚠️ import 写在函数里，因为 catalog 在模块级 `from . import sources`
       —— 模块级再反向 import 就成环了。函数内的延迟 import 断开这个环，
       而且**只有一份实现**。
    """
    from .catalog import gps_week

    return gps_week(d)


# =============================================================================
# iGMAS 国际GNSS监测评估系统（备选产品）
# =============================================================================

# ⚠️ 只能走 **http**（https 连不上），而且**不带 www 的域名 DNS 都解析不了**。
IGMAS_BASE = "http://www.igmas.org"

# ⚠️ 北斗周：纪元是 **2006-01-01**（不是 GPS 的 1980-01-06）。
#    实测核对过：2026-09-02 → 周 1078 第 3 天 → 它库里的 `isc10783.sp3.Z`
#    解压后首行是 `#cP2026  9  2 …` ✓
_BEIDOU_EPOCH = date(2006, 1, 1)

# 产品档位，**按质量排序**：最终 → 快速 → 超快。
#
# ⚠️ 这个顺序是"能用哪个就用哪个"，**每类文件各自选**：
#    于是可能出现在 sp3 用最终、clk 用快速的情况 —— 而这是**看得见的**，
#    因为档位就写在文件名里（`isc10783.sp3` / `isr10785.clk`），
#    它会一路进到配置和结论里。
#
# ⚠️ 没有做"三类必须同一档"的约束：那需要在三次独立的 resolve 之间
#    共享状态，而收益只是"好看一点"。
_IGMAS_TIERS = ("isc", "isr", "isu")

_igmas_cache: dict[tuple[str, int], dict[str, str]] = {}

# ⚠️ 限流：实测连续下载**几乎无缝**时，第二次会返回 `HTTP 304 + 0 字节`
#    （看着像"没变化"，其实是"请勿频繁点击"）。间隔 ≥1 秒就稳定成功。
#    这个时间戳是模块级的，因为限流是**服务器那边**的约束，
#    和在哪一次调用无关。
_igmas_last_hit = 0.0
IGMAS_MIN_GAP = 1.2


def igmas_throttle() -> None:
    """让 iGMAS 的请求之间至少隔 `IGMAS_MIN_GAP` 秒。

    ⚠️ 下载那一步不经过这个模块（它走 fetch）—— 所以 `fetch` 那边
       也要在发请求前调一次这个函数。**限流是服务器的约束，不是某个函数的。**
    """
    global _igmas_last_hit
    import time

    gap = time.monotonic() - _igmas_last_hit
    if gap < IGMAS_MIN_GAP:
        time.sleep(IGMAS_MIN_GAP - gap)
    _igmas_last_hit = time.monotonic()


def _beidou_week_dow(day: date) -> tuple[int, int]:
    delta = (day - _BEIDOU_EPOCH).days
    return delta // 7, delta % 7


def _igmas_list(ptype: str, week: int, session) -> set[str]:
    """检索一周的某一类产品，返回 `(目录)文件名` 那样的字符串集合。

    ⚠️ 它返回的是 **HTML 表格**，不是 JSON —— 只能从 `<td>` 里把
       `(b1cb2a)isc10783.sp3.Z` 这种字符串抠出来。
       这是这个源**最脆**的地方：它改一次页面结构，这里就失效。
       （所以它是"备选里的备选"，不是主力。）
    """
    key = (ptype, week)
    if key in _igmas_cache:
        return _igmas_cache[key]

    if session is None:
        from . import net

        session = net.build_session()

    import re

    igmas_throttle()
    resp = session.post(
        f"{IGMAS_BASE}/Product/Search/search/",
        data={
            "cate_id": "38",          # 实测：38 = 产品类
            "findby": "0",            # 按北斗周检索
            "pweek": str(week),
            "ptype": ptype,
            "pname": "",
        },
        timeout=40,
        headers={"Referer": f"{IGMAS_BASE}/Product/Search/index"},
    )
    if resp.status_code != 200:
        return set()

    # ⚠️ 编码要显式定：页面是 GBK 系的，靠 apparent_encoding 判
    resp.encoding = resp.apparent_encoding or "utf-8"

    found: set[str] = set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.S):
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S):
            text = re.sub(r"<[^>]+>", "", cell).strip()
            m = re.match(r"\((\w+)\)(\S+)", text)
            if m:
                found.add(f"{m.group(1)}/{m.group(2)}")

    _igmas_cache[key] = found
    return found


def _igmas_resolve(rest: str, day: date, session) -> str | None:
    """`lookup://igmas/<kind>/` → 它库里的下载地址。

    ⚠️ 路径中间的**那层 AC 目录**（`b1cb2a` 这种）**不能省、也不能猜** ——
       它只存在于检索结果里。去掉就 404。
    """
    kind = rest.split("/", 1)[0]
    if kind not in ("sp3", "clk", "erp") or day is None:
        return None

    week, dow = _beidou_week_dow(day)
    rows = _igmas_list(kind, week, session)
    if not rows:
        return None

    for tier in _IGMAS_TIERS:
        want = f"{tier}{week}{dow}.{kind}.Z"
        for row in rows:
            acdir, name = row.split("/", 1)
            if name == want:
                return (
                    f"{IGMAS_BASE}/Product/TreePage/downItem/"
                    f"?fid=/products/products/{week}/{acdir}/{name}"
                )
    return None


def sanitize(source: str, path: Path) -> bool:
    """把服务器附加的**前后垃圾**去掉（就地改文件）。

    ⚠️ 目前处理两种，各属于一个源：

        武大：**尾部**追加 ~19 KB 的 ThinkPHP 调试 HTML  → 见 strip_trailer
        iGMAS：**开头**有一个 UTF-8 BOM（`ef bb bf`）

    ⚠️⚠️ BOM 必须去 —— 不去的话识别环节**认不出是 gzip**
        （`sniff_bytes` 看的是头两字节 `1f 8b`，而前面挡着三个字节），
        于是文件被丢弃，现象是"下载失败"，而文件其实是好的。

    ⚠️ 一个函数管两件事，是因为**调用方只需要知道"把这个源的文件收拾干净"**，
       不需要知道每家脏在哪。加第三个源时改这里，不改调用点。

    ⚠️ 返回值是"**有没有动过这个文件**" —— 调用方靠它决定要不要重算字节数。
       我第一版把它写成返回 None，于是调用点那句 `is not None` 永远为假：
       文件切了、但报告里的字节数还是切之前的。**这种错不会报错，只会让数字不对。**
    """
    if source == "whu":
        return strip_trailer("whu", path)

    if source == "igmas":
        try:
            with path.open("r+b") as f:
                if f.read(3) == b"\xef\xbb\xbf":
                    rest = f.read()
                    f.seek(0)
                    f.write(rest)
                    f.truncate()
                    return True
        except OSError:
            pass

    return False


def strip_trailer(source: str, path: Path) -> bool:
    """把服务器追加在文件末尾的调试信息切掉。返回是否真的切了。

    ⚠️⚠️ 只有武汉大学需要这个（见模块开头"坑①"）。

        它的响应是：`<完整的 gzip 流><19 KB 的 ThinkPHP 调试 HTML>`
        状态码 200、头 8 字节是 gzip magic、Content-Type 是 text/html。

        **不切的话，每一个文件都会"下载成功"然后解压失败** ——
        这是最难查的那种失败：下载那一步的报告是绿的。

    ⚠️ 判据是**那个标记**，不是接口返回的 `size` 字段 ——
       实测那个字段**会过期**（写 6,267,332，真实 6,259,787），
       按它截会切进 HTML 里，得到一个**看起来更合理**的坏文件。

    ⚠️ 只扫末尾 64 KB。垃圾恒在末尾，没必要为了找它把整个文件读一遍
       （天线文件 61 MB，它没有，但别的源将来可能也有这个毛病）。
    """
    if source != "whu":
        return False

    try:
        size = path.stat().st_size
        if size == 0:
            return False

        start = max(0, size - TAIL_SCAN)
        with path.open("r+b") as f:
            f.seek(start)
            tail = f.read()
            cut = tail.find(WHU_JUNK_MARKER)
            if cut < 0:
                return False
            f.truncate(start + cut)
        return True
    except OSError:
        return False


def reset_cache() -> None:
    """清掉清单缓存。

    ⚠️ 给自检用的。真实运行里没必要清 —— 同一天的文件清单不会变。
    """
    _whu_index.clear()
    _esa_index.clear()
    _igmas_cache.clear()
