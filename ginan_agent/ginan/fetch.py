"""Ginan 领域层 —— 下载：从多个源把一个文件弄到手。

============================ 这个模块的四个难点 ============================
每一个都是实际会出问题的，不是理论顾虑：

  ① **多源回退**。我们普查过的源里，有些时好时坏。
     一个源失败不该让整个任务失败 —— 换下一个。

  ② **断点续传**。实测澳大利亚 GA 的下载速度约 90 KB/s，
     一个 50 MB 的 RINEX 要 10 分钟。中间断了从头再来是不能接受的。

  ③ **进度上报**。10 分钟的下载如果界面只是转圈，用户会以为死了。
     所以边下边报，而且要报**字节数**和**预估剩余时间**。

  ④ **下载后重新识别内容**。⚠️ 这条最反直觉。
     文件名可能是错的（实测：GA 返回的 URL 写着 .crx.gz，内容是明文）。
     所以下完必须**按内容重新判断**，然后按真实类型命名。

============================ 和第四条路的关系 ============================
这个模块负责"自动下载"（路径①②③）。
用户手动提供文件走的是 inbox.py（路径④）——
两条路最终产出**同样形状的结果**，所以调用方不需要区分。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import crinex, identify, lzw, net, paths, sources
from .catalog import SOURCES, FilePlan, RunPlan, plan_tables

# 下载时的分块大小。
#
# ⚠️ 1 MB 是权衡：太小则进度回调过于频繁（每次都更新界面反而卡），
#    太大则进度条一跳一大格，看起来像卡住。
CHUNK = 1024 * 1024

# `Content-Range: bytes 54165504-72363936/72363937` → (start, end, size)
#
# ⚠️ 用 `.match` 而不是 `.search`：这个头**必须从头就是这个格式**，
#    前面多一个词就说明我们对它的理解是错的，那就该当成解析失败。
_RE_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)")

# 单个文件最多从几个源尝试。
#
# ⚠️ 设上限是因为：所有源都失败时，用户已经等了很久，
#    再多试一个只是让他多等。
#
#    代价与收益：每多试一个源，最坏情况就多一次超时（约 10~30 秒）。
#    但那只在**前面全挂**时才发生 —— 而那时候多一次尝试正是你想要的。
#
# ⚠️ 这个数**跟着源的数量涨过两次**（3 → 4 → 5），两次都是同一个原因：
#    **排在上限之外的源永远不会被尝试，等于白写。**
#    加源的时候必须一起看这个数，否则新源是"配了但不用"。
#
#    ⚠️ **顺序 = 优先级**，见 catalog.plan_run 里那张排序说明。
#
#    ⚠️ 2026-09-20 再次改成 5：加了武大 FTP 之后**镜像**正好是 5 个
#       （武大HTTP / 武大FTP / SIO / BKG / CDDIS）。
#       这个数只数**镜像** —— 备选产品（ESA / iGMAS）走的是另一条路，
#       而且要先问用户，不在这儿。
MAX_SOURCES_PER_FILE = 5


# =============================================================================
# 结果
# =============================================================================


@dataclass
class DownloadResult:
    """一个文件的下载结果。"""

    filename: str
    ok: bool = False
    source: str = ""          # 实际用的是哪个源
    path: Path | None = None
    size: int = 0
    sha256: str = ""
    detail: str = ""
    skipped: bool = False     # 已经在缓存里，没重新下
    # ⚠️ 这是**计划里的那一类**（sp3/clk/erp/atx/nav/obs/snx/bia/table），
    #    不是 identify 认出来的那一类 —— 两套词表不一样，见 `_decide_name`
    #    下面那段说明。下游全按这个词表查，所以必须是计划的值。
    kind: str = ""
    real_name: str = ""       # 按真实内容定的文件名（可能与请求的不同）

    @property
    def short(self) -> str:
        if self.skipped:
            return "已在缓存里"
        if not self.ok:
            return f"失败：{self.detail}"
        mb = self.size / (1024 * 1024)
        return f"{mb:.1f} MB，来自 {SOURCES[self.source].label if self.source in SOURCES else self.source}"


@dataclass
class FetchReport:
    """一次运行的全部下载结果。"""

    results: list[DownloadResult] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> list[DownloadResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[DownloadResult]:
        return [r for r in self.results if not r.ok]

    @property
    def total_bytes(self) -> int:
        return sum(r.size for r in self.results if r.ok and not r.skipped)

    @property
    def all_ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        done = len(self.ok)
        total = len(self.results)
        mb = self.total_bytes / (1024 * 1024)
        parts = [f"{done}/{total} 个文件"]
        if self.total_bytes:
            parts.append(f"下载 {mb:.1f} MB")
        if self.failed:
            parts.append(f"失败 {len(self.failed)} 个")
        return "，".join(parts)


# =============================================================================
# 单文件下载
# =============================================================================


def _sha256(path: Path, limit: int | None = None) -> str:
    """算文件的 SHA-256。

    ⚠️ 分块算，不一次读进内存 —— 一个 61 MB 的文件没必要占 61 MB 内存。

    参数 limit 用于校验**部分内容**（断点续传后只剩前半段时用）。
    """
    h = hashlib.sha256()
    read = 0
    with path.open("rb") as f:
        while True:
            if limit is not None and read >= limit:
                break
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
    return h.hexdigest()


def _session_for(source_key: str, session, proxy):
    """给某个源挑合适的会话。

    ⚠️ CDDIS 需要**特殊的认证流程**（跨主机跳转，且 requests 默认会
       在跨主机时剥掉凭据）。其他源用普通会话就行。

       这个判断必须在这里做，而不是让调用方自己想起来 ——
       "忘了给 CDDIS 用特殊会话"的症状是"下载失败，拿到登录页"，
       看起来像凭据问题，实际上代码问题。**极难排查。**
    """
    if session is not None:
        return session, False          # 调用方指定了，尊重它
    if source_key == "cddis":
        from .credentials import cddis_session

        return cddis_session(proxy=proxy), True
    return net.build_session(proxy), True


def _resolve_query_url(
    url: str,
    *,
    session,
    day=None,
    timeout: float = net.DEFAULT_PROBE_TIMEOUT,
) -> tuple[str, str]:
    """把"查询接口"的 URL 解析成**真正的文件地址**。

    返回 `(真实URL, 错误说明)`。不是查询接口就原样返回、错误为空。

    ============================ 现在有**两个**这样的源 ============================
        ① GA（澳大利亚）—— 值是 `http://data.gnss.ga.gov.au/api/...`
        ② 武大（武汉大学）—— 值是 `lookup://whu/<接口>/<文件名>`

    ⚠️ 武大那条是后来加的，**没有另起一套机制** —— 就挂在这个函数里。
       因为"有些源给的不是文件地址，是查询接口"**是同一件事**，
       写成两处的话，迟早有一处会被改漏（而这正是这个项目反复踩的坑）。

    ⚠️ 武大需要**日期**才能查（它按天索引）—— 所以多传了一个 `day`。

    ============================ GA 是两步的 ============================
    澳大利亚 GA 的观测数据**不能直接下**：

        ① 查 API → 返回一段 JSON，每个元素里有 `fileLocation`
        ② 下载那个 `fileLocation`（一个**预签名**的 S3 地址）

    ⚠️⚠️ 这个分支原来**根本没写** —— 计划里给的是查询 URL，
       下载器把它当普通文件下，于是拿到一段 JSON，然后被识别环节
       判为"内容对不上"。

       实测现象（2026-09-18）：
           要的是 obs，拿到的是 认不出来
           （开头是 '[{"siteId":"alic","fileType":"obs",...'）

    ⚠️ 而它**能藏这么久**，是因为这条路被两件事先后挡着：
         ① 先是网络超时（没开代理时 GA 连不上）
         ② 后来用户自己的本地文件直接命中了 —— **压根没走到下载**

       > **一个从没被走到的分支，等于没写过。**
       > 这也是为什么"换个数据源实测一遍"比"多加几个测试"更能发现问题。

    ⚠️ API 会返回**多条**：实测同一份 01D 观测数据返回了 2 条，
       字段看起来**完全一样**（同样的 fileType / filePeriod / fileSize）。
       这里取**第一条** —— 按经验它是最新的那份。
       （多出来的那条很可能是同一天的不同处理版本。）
    """
    # ⚠️ 按**URL 特征**判断，不按源的名字判断 ——
    #    这样将来换了源 key、或者别人拿这个 URL 单独来调，
    #    判断依然成立。

    # ---- 武大：按"待解析"标记判断（它压根不是 URL）----
    if sources.is_lookup(url):
        real = sources.resolve(url, day, session=session)
        if real is None:
            # ⚠️ 查不到 **不是异常**，是一条"这个源这次不行"——
            #    调用方会记下错误、接着试下一个源。见下面 docstring 末尾那句。
            return "", "没查到（可能是网络不通，或该文件不在它的库里）"
        return real, ""

    if "data.gnss.ga.gov.au/api/" not in url:
        return url, ""

    try:
        resp = session.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return "", f"查询接口连不上（{net._explain_exception(exc)}）"

    # ⚠️⚠️ **404 不是"服务器坏了"，是"这个源没有这个站"。**
    #
    #    实测（2026-09-23，逐个查 71 个 IGS 站名、日期 2025-06-15）：
    #
    #        56 个有数据 ✓
    #        13 个返回 **HTTP 404** —— BRUX / JFNG / LHAZ / MGUE / OHI3 /
    #                                   PAPE / SEY2 / TIDB / WETT / ALRT …
    #
    #    GA 的 API 对**不认识的站号**回 404，**不是**回空数组。
    #    所以下面那条精心写的「这个源上没有该站该天的数据」**根本不会触发**
    #    —— 用户看到的是 `查询接口返回 HTTP 404`，而 404 读起来像
    #    「接口地址写错了 / 服务器出问题了」，于是会去查网络、查 URL。
    #    **而真正的事实是「这个源没有这个站」** —— 该做的是换个站或者换个源。
    #
    #    ⚠️ 这是「错误信息离根因很远」的又一个实例，和之前那次
    #       「广播星历把测站参数传了进去 → 必定失败、而报错看起来像网络问题」
    #       是**同一个形状**：错的不是网络，是把现象归错了类。
    if resp.status_code == 404:
        return "", (
            "这个源没有收录这个站"
            "（GA 收录的是全球站，但不是整个 IGS 网 —— 换个站，或者自己准备观测文件）"
        )
    if resp.status_code != 200:
        return "", f"查询接口返回 HTTP {resp.status_code}"

    try:
        entries = resp.json()
    except Exception:  # noqa: BLE001
        return "", "查询接口返回的不是 JSON（可能是被代理/网关拦了）"

    if not entries:
        # ⚠️ 这个说法要准确：不是"下载失败"，是**这个源上就没有**。
        #    两者的处理方式完全不同（一个是重试，一个是换源）。
        return "", "这个源上没有该站该天的数据"

    loc = (entries[0] or {}).get("fileLocation")
    if not loc:
        return "", "查询接口没有给出文件地址（fileLocation）"

    return str(loc), ""


def _station_from_filename(name: str) -> str:
    """从计划里的文件名反推测站。

    ⚠️ 计划里观测数据的文件名是占位的 `{STATION}_UNKNOWN.rnx` ——
       因为真实文件名要下载时才知道（GA 返回的是预签名 URL）。

       所以这里取第一个下划线前的部分。这不是"猜文件名"，
       而是**读我们自己写进去的占位符**。
    """
    head = (name or "").split("_")[0].strip().upper()
    return head if 2 <= len(head) <= 4 else ""


def _station_prefix_matches(path: Path, want: str) -> bool:
    """文件名看起来是不是这个站的数据。

    ⚠️ 规则刻意收得很紧 —— 因为这是**猜**，而猜错的代价不是报错，
       而是"**用了别的站的数据，算出一个看起来完全正常的结果**"。

       ① 站号必须在**前 4 位** —— 那是 RINEX 的惯例位置
          （`ALIC00AUS_R_20262450000_01D_30S_MO.rnx`）。
       ② 第 5 位**不能是字母** —— 否则 `DCMSX.rnx` 会被当成 `DCMS`。

            ✓ dcms260101000000.rnx     "dcms" + "2…"（数字，后面是日期）
            ✓ DCMS_R_202512310000_…    "DCMS" + "_…"（分隔符）
            ✓ DCMS.rnx                 正好 4 个字符
            ✗ DCMSX.rnx                "DCMS" + "X"（字母）→ 拒绝

       ⚠️ 故意**不用"包含"**（`want in name`）——
          那会把 `2026_DCMS_backup.rnx` 这种也匹配上，
          而名字里出现站号的原因可能完全不同（备份标记、注释……）。
    """
    stem = path.stem.upper()
    if not stem.startswith(want):
        return False
    rest = stem[len(want):]
    return not (rest and rest[0].isalpha())


def find_local_obs(
    station: str, day: date, dest_dir: Path | None = None
) -> Path | None:
    """数据目录里已经有这个站**这一天**的观测数据了吗。

    ⚠️ **这一条是为了"用户自己的测站"这个场景。**

       自建站的观测数据只在用户自己手里 —— GA 的 API 上没有、
       IGS 的数据中心也没有。如果 `plan_run` 的取数计划里写着
       "从 GA 下载 ALIC 的数据"，而用户要处理的是他自己的站，
       那一步**必然失败**，而且失败原因（404）看起来像网络问题。

       所以：**本地有就不用下。** 用户把文件放进来（收件箱或手动），
       流程就该认出来。

    ============================ 两级匹配 ============================

        ① **文件头里的测站名** —— 权威。头里写的就是事实。
        ② **文件名的站号前缀** —— 兜底。只在①一个都没匹配上时才用。

    ⚠️ 为什么需要②：用户自己的数据**头部常常是空的** ——
       自建站的采集软件不一定填 MARKER NAME。

       实测（一份自建站数据，`dcms260101000000.rnx`）：
       MARKER NAME 是 60 个空格，
       站名、天线、接收机**三个字段全空**。
       只有①的话这类文件永远找不到 —— 而现象是
       "程序说没看到你的数据"，然后跑去网上找一个不存在的站、失败，
       排查方向被带偏。

    ⚠️⚠️ 但②是**猜**，所以有两个约束：

        · 规则收得很紧（见 `_station_prefix_matches`）
        · **头里明确写了别的站时，名字不能推翻它** ——
          头是权威的。名字只在"头空着"的时候才作数。

    ============================ ⚠️⚠️ 为什么要传 `day` ============================
    **一个测站有多天的观测，而"有没有这个站的观测"这句话没有唯一答案。**

    原来这个函数**只收站名、不收日期** —— 于是它返回"随便哪一个"。
    实测（2026-09-21，用户那份三天的数据）：

        认领之后 data/ 里躺着：ALIC.rnx、ALIC_2.rnx、ALIC_3.rnx
                               ↑09-03    ↑09-02     ↑09-01
        find_local_obs("ALIC")          → ALIC.rnx   ← **09-03 那份**

    后果**不是报错，是静默算错**：给"ALIC 09-02"做计划时，
    配置里被写成 `ALIC.rnx`（09-03 的观测），而轨道/钟差是 09-02 的
    —— 配置看着完全正常，跑出来的是**拿错日子算的解**，
    **没有任何地方会告诉用户**。

    > 这是本项目最不能接受的那一类：**静默给错东西**。
    > （比"下载失败"糟得多 —— 那次至少会有人来问。）
    >
    > ⚠️ 而且它一直藏着：这个函数被写出来时，
    > `find_local_obs` 的测试夹具**只有一个文件**，
    > 而"一个站只有一天"是**唯一不会暴露这个 bug 的情形**。

    👉 所以 `day` 是**必填的**，不给默认值 ——
       "这个站的观测有没有"这个问题**本身就不完整**，
       逼调用方说清是哪一天，比让他拿到一个随机答案好。
    """
    from . import rinex as rinex_mod

    dest_dir = dest_dir or paths.data_dir()
    if not dest_dir.is_dir():
        return None

    want = station.strip().upper()[:4]
    if len(want) < 2:
        # ⚠️ 站号太短就别猜了 —— 一两个字母的"站号"几乎必然误匹配。
        return None

    # ⚠️ 每个文件只读一次头（inspect 只读前 64 KB，很便宜，
    #    但没必要为两轮匹配读两遍）。
    #
    # ⚠️ 第三格是"这份**证明得了**它覆盖 `day` 吗" —— 见下面那段。
    candidates: list[tuple[Path, str, bool]] = []
    for p in sorted(dest_dir.iterdir()):
        if not p.is_file():
            continue
        # ⚠️⚠️ **按内容认，不按扩展名。**
        #
        #    这里原来写的是 `p.suffix.lower() not in (".rnx", ".obs")` ——
        #    和 `agent/ginan_tools.py` 里 `_obs_in` 当初那一行**一字不差**。
        #
        #    那一处已经被当成真 bug 修掉了（用户那份 `alic0600.22o`
        #    是 RINEX 2 的标准短名，完全正常，却因为后缀不是 `.rnx`
        #    被滤掉），修的时候还专门写下这句话：
        #
        #        **一个原则只要有一处不照做，那一处就是全部。**
        #
        #    👉 **这一处就是"那一处"。** 同一个 bug 的第二份拷贝，
        #       而它的后果**更重**：
        #
        #         它把「① 文件头里的测站名（权威）」整个挡在扩展名过滤**后面** ——
        #         用户那份 `.22o` 永远不会被 inspect → "本地已经有就不用下载"
        #         落空 → 程序转去网上找 → 404 → **看起来像网络问题**。
        #
        #        （`check_files.py` 的 ④d 给这个函数喂的输入**全是 `.rnx`**，
        #          一次都没喂过 `.22o` —— 所以它一直没被发现。）
        #
        # ⚠️⚠️ 判据从 `kind != "obs"` 改成 `not usable` —— **又一个
        #     "认得出"和"能用"分家的例子**（前面已经有 HTML 和 CrinEX 两次）。
        #
        #     CrinEX **也是 `kind="obs"`**（它的头部就是明文 RINEX），
        #     所以光判 kind 会把它当成"本地已经有一份了"**直接交出去** ——
        #     然后 Ginan 拿它去解算，报一个和真正原因毫无关系的错。
        #
        #     比"下载失败"糟得多：**这一次连下载都没发生**，
        #     用户看到的是"已存在，跳过下载"，一切正常。
        ident = identify.identify(p)
        if ident.kind != "obs" or not ident.usable:
            continue
        info = rinex_mod.inspect(p)
        marker = (info.marker or "").upper()[:4]

        # ⚠️⚠️ **日期必须"证明得了"，不能靠猜。**
        #
        #     判据取自**文件头里的观测时段**（`TIME OF FIRST/LAST OBS`）：
        #     这一天必须落在 `[第一天, 最后一天]` 里面。
        #
        #     ⚠️ 读不出时段的（头坏了、被截断了）一律**不算命中** ——
        #        哪怕它名字看着像。理由是不对称的：
        #
        #          漏掉（判成没有）→ 程序去找下载 → 找不到就如实说
        #                            "缺这一天的观测"（**响的**）
        #          误收（判成有）  → 拿错日子的数据去算（**静默的**）
        #
        #        两者都错，但只有前者会有人来问。
        first, last = info.first_obs, info.last_obs
        covers = bool(first and last and first.date() <= day <= last.date())
        candidates.append((p, marker, covers))

    # ⚠️ 日期这一关**先过**，后面两轮匹配都只能在过了关的里面挑。
    dated = [c for c in candidates if c[2]]

    # ---- ① 文件头里的测站名（权威）----
    for p, marker, _ in dated:
        if marker and marker == want:
            return p

    # ---- ② 文件名的站号前缀（兜底）----
    #
    # ⚠️ 只对**头里没写站名**的文件兜底。
    #    头里写了别的站 → 名字说了不算 —— 那是拿名字去推翻事实。
    for p, marker, _ in dated:
        if not marker and _station_prefix_matches(p, want):
            return p

    return None


def _inspect_local_obs(path: Path) -> str:
    """认领一份本地观测数据时，顺手看看它**能不能用**。

    返回一段要接在 detail 后面的说明（没问题就返回空串）。

    ⚠️⚠️ 为什么要在这里说，而不是等跑完再说？

       因为"找到了"和"能用"是**两个**问题，而第二个如果不说，
       用户会一路跑到结果出来才发现不对 —— 那时候错误已经变成
       一个看起来正常的数字了。

       **早说的成本是几行字，晚说的成本是用户的一次错误结论。**

    ⚠️ 特别是**天线型号为空**：那会让 Ginan 不做相位中心改正，
       结果偏几厘米，而**看起来完全正常、不报错**。
       这是本项目最想避免的那类失败。

    ⚠️ 这一层是"提前提醒"，不是"最终防线" ——
       最终防线在诊断层（PEA 自己也会警告）。
       **两层都要有**：这里能让用户免于白等一次运行，
       那里能兜住所有我们没预料到的情况。
    """
    from . import rinex as rinex_mod

    info = rinex_mod.inspect(path)
    missing = []
    if not (info.marker or "").strip():
        missing.append("站名")
    if not (getattr(info, "antenna", "") or "").strip():
        missing.append("天线型号")

    if not missing:
        return ""

    return (
        f"。⚠️ **但这份数据的头部缺「{'、'.join(missing)}」** —— "
        "跑之前要先处理：\n"
        "  · 先用 `inspect_rinex` 看它到底能不能用\n"
        "  · 缺天线型号要**问用户**（填错或不填会让结果偏几厘米，"
        "而且**看起来完全正常**）\n"
        "  · 补完用 `repair_rinex` 写出新文件（不会改原文件）"
    )


def _local(dest_dir: Path, name: str) -> Path:
    """把一个"计划里的文件名"落到 `dest_dir` 下的真实路径。

    ⚠️ **这是唯一该被用来拼下载路径的地方。** 两条理由，都是踩出来的：

      ① **空名字会让 `dest_dir / ""` 等于目录本身** —— 于是"删掉那个文件"
         变成了"删掉那个目录"，报错里只有一个临时目录路径，
         **看不出和文件名有关系**（见 catalog.py 里那段）。

      ② 「完整」档的模型文件名**带子目录**（`tables/gpt_25.grd`）。
         直接拼出来的父目录不存在 —— 而失败会发生在**下载完成之后**，
         也就是白白下完 17 MB 才发现写不进去。

    所以这里一次做三件事：挡掉不合法的名字、建好父目录、返回完整路径。
    """
    if not name or name.endswith(("/", "\\")):
        raise ValueError(f"文件名不合法（空的或只有目录）：{name!r}")

    p = Path(name)
    # ⚠️⚠️ 光判 `is_absolute()` **不够** —— Windows 上 `/abs/x.grd` 会被判成
    #     "不是绝对路径"（它有根但没有盘符），于是 `is_absolute()` 是 False、
    #     `p.drive` 也是空。看起来合法。
    #
    #     但拼接时它**照样会甩掉左边**：`dest_dir / "/abs/x.grd"` 的结果是
    #     `\abs\x.grd`（挂到当前盘的根上）—— 文件就落到 dest_dir 外面去了。
    #
    #     👉 所以还要看 `p.root`：非空 = 有根 = 能甩掉左边 = 拒绝。
    #        （这条是写测试时才发现的，`is_absolute()` 那行原本"看起来够了"。）
    if p.is_absolute() or p.drive or p.root or ".." in p.parts:
        raise ValueError(f"文件名不合法（绝对路径或往上越界）：{name!r}")

    full = dest_dir / p
    full.parent.mkdir(parents=True, exist_ok=True)
    return full


def download_one(
    item: FilePlan,
    dest_dir: Path,
    day: date | None = None,
    *,
    session=None,
    ctx=None,
    proxy=None,
    timeout: float = net.DEFAULT_DOWNLOAD_TIMEOUT,
) -> DownloadResult:
    """下载一个文件，多源依次尝试。

    ⚠️ `day` 是"**这份文件是哪一天的**" —— 观测数据要靠它在本地找
       "**这一天的**"（见 `find_local_obs`：只给站名会拿到别的日子，
       然后**静默算错**）。

       ⚠️ 只有"下模型文件"那条路会不给日期（`fetch_tables` → 那些文件
          不属于任何一天）。那条路里**没有观测项**，所以 `None` 传到这里
          不会带来任何猜测 —— 见下面那个 `day is not None`。

    ⚠️ **永不抛异常** —— 返回带错误说明的结果。
       因为批量下载时，一个文件失败不该让整批崩掉；
       用户需要看到的是"哪些成了、哪些没成、没成的为什么"。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # ---- ⓪ 名字先过一遍 ----
    # ⚠️ 放在最前面：不合法的名字**一个字节都不该下**。
    #    等下载完才发现写不下去，用户已经白等了几分钟。
    try:
        target = _local(dest_dir, item.filename)
    except ValueError as e:
        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind,
            detail=str(e),
        )

    # ---- ① 先看本地有没有 ----
    #
    # ⚠️ 一次性文件（天线表等）下载后长期有效，**不该让用户等第二遍**。
    #    61 MB 的文件下两次是很实在的浪费。
    if item.one_time:
        # ⚠️ 只认**名字正好对上**的（外加一个还没解压的压缩后缀）。
        #
        #    这里原来是个 `glob(filename + "*")` —— 太宽了：
        #    它会把 `xxx.part`（下载中途的临时文件）也当成"已经有了"，
        #    于是**永远跳过下载**，用户拿到一个残文件，而且不报任何错。
        #    一次性文件里 `real_name` 就等于 `item.filename`（见 _decide_name），
        #    所以精确匹配是对的，宽匹配才是错的。
        for existing in target.parent.glob(target.name + "*"):
            if not existing.is_file() or existing.stat().st_size <= 0:
                continue
            if existing.name != target.name and not existing.name.endswith((".gz", ".Z")):
                continue
            return DownloadResult(
                filename=item.filename, ok=True, skipped=True,
                path=existing, size=existing.stat().st_size,
                source="缓存", detail="已存在，跳过下载",
                kind=item.kind, real_name=existing.name,
            )

    # ⚠️ 观测数据：按**测站名 + 日期**找本地已有的文件。
    #
    #    这一条是给"用户自己的测站"用的 —— 自建站的数据只在用户手里，
    #    数据源上根本没有。不检查的话会去下载然后 404，
    #    而那个报错看起来像网络问题，把排查方向带偏。
    #
    #    ⚠️⚠️ **`day` 不能省。** 一个站有多天的观测，只给站名的话
    #        "有没有"没有唯一答案 —— 实测它会把 09-03 那份交出去当
    #        09-02 的用（见 `find_local_obs` 的说明）。
    if item.kind == "obs":
        station = _station_from_filename(item.filename)
        # ⚠️⚠️ `day is not None` 是**必须的条件，不是保险** ——
        #     不认日期的"本地已经有"就是**随手挑一天**。
        #     没有日期时**不查本地**，让它走下面的正常下载路径：
        #     那样查不到会**明确失败**，而不是拿别的日子顶上。
        if station and day is not None:
            local = find_local_obs(station, day, dest_dir)
            if local is not None:
                detail = f"已存在（{local.name}），跳过下载"
                detail += _inspect_local_obs(local)
                return DownloadResult(
                    filename=item.filename, ok=True, skipped=True,
                    path=local, size=local.stat().st_size,
                    source="本地", detail=detail,
                    kind="obs", real_name=local.name,
                )

    if not item.urls:
        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind,
            detail="没有任何已知的下载来源",
        )

    # ⚠️⚠️ 这个局部变量**原来叫 `sources`** —— 正好盖住了上面
    #     `from . import … sources` 那个模块。
    #
    #     后果：这个函数里任何 `sources.xxx()` 都会打到**这个列表**上，
    #     报 `AttributeError: 'list' object has no attribute …`。
    #
    #     ⚠️ 它是**加 iGMAS 限流时才暴露的** ——
    #        在那之前这个函数压根不需要用到 `sources` 模块，
    #        所以遮蔽一直存在、但一直没被踩到。
    #
    #        比"改坏"更值得记的是这一点：**遮蔽不会自己报错，
    #        它等到第一个真正要用那个名字的人。** 而那个人可能是很久以后。
    #
    #     > 这就是为什么它现在叫 `source_keys` ——
    #     > **别用和模块同名的局部变量**，哪怕当下不冲突。
    # ⚠️⚠️ **失败的结果也必须带上 `kind`。**
    #
    #     第一版只给成功的结果填了 kind（那是在识别之后才有的），
    #     失败的一律是空字符串。于是调用方（`do_fetch_data`）拿不到
    #     "到底哪一类没下来" —— 它只好去问用户：
    #
    #         "有 3 个文件所有来源都没下到（），要用备选产品吗？"
    #                                    ↑ 空的
    #
    #     ⚠️ kind 是**计划里就有的**，和"下载有没有成功"无关。
    #        失败路径少填一个字段，看起来只是"少点信息"，
    #        实际上让下游那句问话**没法问清楚**。
    source_keys = list(item.urls)[:MAX_SOURCES_PER_FILE]
    errors: list[str] = []
    made: list = []          # 我们自己建的会话，用完要关

    try:
        # ---- ① 先把每个源解析成**真地址**，并各准备一个会话 ----
        #
        # ⚠️ 原来"解析 + 下载"是**在同一个循环里**做的 —— 那样就没法测速：
        #    要比较两个源，得先同时拿到两个真地址。
        #    对只有一条路的文件（绝大多数），这一步和以前**完全一样**，
        #    只是把循环拆成了两段。
        cands: list[tuple[str, str, str, object]] = []   # (src, label, url, sess)
        for src in source_keys:
            url = item.urls[src]
            label = SOURCES[src].label if src in SOURCES else src

            # ⚠️⚠️ iGMAS **必须限流**：实测连续下载几乎无缝时，
            #     第二次会返回 `HTTP 304 + 0 字节` —— 看着像"内容没变化"，
            #     其实是"请勿频繁点击"。间隔 ≥1 秒就稳定成功。
            #
            #     ⚠️ 这个约束是**服务器那边**的，所以它既在这里生效，
            #        也在 `sources` 里生效（那边发检索请求）——
            #        两处用的是**同一个时间戳**（`sources.igmas_throttle`）。
            if "igmas.org" in url:
                sources.igmas_throttle()

            # ⚠️ 每个源可能要不同的会话 —— CDDIS 需要特殊认证流程。
            #    见 _session_for 的说明。
            sess, ours = _session_for(src, session, proxy)
            if ours:
                made.append(sess)

            # ⚠️⚠️ **有些源给的不是文件地址，是查询接口**（GA 就是）——
            #     必须先解析出真正的文件地址，否则下回来的是 JSON。
            #     见 _resolve_query_url 的说明。
            real, resolve_err = _resolve_query_url(url, session=sess, day=item.day)
            if resolve_err:
                errors.append(f"{label}：{resolve_err}")
                continue
            cands.append((src, label, real, sess))

        # ---- ② 多个源就测速，按**实测快的在前**排 ----
        #
        # ⚠️⚠️ 这一段的由来（用户点的）：「不是谁在前，是测一下速，用最快的」。
        #
        #    背景：同一份观测数据实测差 **44 倍** ——
        #        武大 1305 KB/s（整条链 9.4 秒）
        #        GA     22 KB/s（5 MB 要 3.8 分钟）
        #    根因是 GA 的观测下载走 AWS S3 `ap-southeast-2`，从这边就是慢。
        #
        #    ⚠️ 而这件事**换个用户/换个地区就会反过来** ——
        #      所以"谁排第一"写死在代码里**必然是错的**：
        #      写死武大，欧美的用户就吃亏；写死 GA，这边就慢 44 倍。
        #      **只能测。**
        if len(cands) > 1 and ctx is not None:
            ctx.progress(f"正在给 {item.filename} 测速（{len(cands)} 个源）", fraction=None)
        if len(cands) > 1:
            cands = _rank_by_speed(cands, ctx=ctx, timeout=timeout)

        for i, (src, label, url, sess) in enumerate(cands):
            if ctx is not None:
                ctx.progress(
                    f"从 {label} 下载 {item.filename}（{i+1}/{len(cands)}）",
                    fraction=None,
                )
                if ctx.cancelled:
                    return DownloadResult(
                        filename=item.filename, ok=False, kind=item.kind, detail="已取消"
                    )

            result = _try_download(
                item, url, dest_dir, source=src, session=sess,
                ctx=ctx, timeout=timeout,
            )
            # ⚠️ CDDIS 的下载必须走 cddis_get（自己跟跳转）——
            #    见 credentials.cddis_get 里那段说明：
            #    requests 的 allow_redirects 会在跨主机时剥掉凭据，
            #    所以 CDDIS 的下载**一定会**拿到登录页。
            #    这和"登录测试"是同一个问题，两处必须用同一套逻辑。
            if result.ok:
                return result

            errors.append(f"{label}：{result.detail}")

        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind,
            detail="所有来源都失败了 —— " + "；".join(errors),
            source="",
        )
    finally:
        for s in made:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


# 测速时每个源最多取多少字节。
#
# ⚠️ 取多大是有取舍的：
#     太小（<32 KB）—— 量到的主要是**建连和首包**，不是带宽，会乱排
#     太大        —— 白下的流量多（这份流量是要**扔掉**的，见下）
#   256 KB 在实测里够分清"1305 KB/s 和 22 KB/s"这种量级的差别 —— 而那正是
#   它要解决的那一类问题（差 44 倍），不是要精确排名。
SPEED_PROBE_BYTES = 256 * 1024

# 量到这么多字节才算一次有效测速。
#
# ⚠️⚠️ **判据是"拿到了多少字节"，不是"花了多少秒" —— 这一点我第一版写反了。**
#
#    第一版写的是：`dt < 1.0 秒 ⇒ 结果不可信 ⇒ 返回 None`。
#    本意是"窗口太短会不可信"，但**对快的源恰好反了**：
#    实测武大 **0.8 秒就把 256 KB 下完了**（≈320 KB/s），
#    而那条判据把它判成 None —— **把最快的源扔了**。
#    于是排序里只剩慢的能被量出来，GA（22 KB/s）反而排到了前面。
#
#    > **一个把"太快"当成"测不准"的判据，会让测速系统专门挑慢的用。**
#
#    ⇒ 换成字节数下限：**拿到够多的字节就是一次有效测量**，
#      快慢都算。时间只用来做**上限**（太慢就放弃，返回 None）。
SPEED_PROBE_MIN_BYTES = 64 * 1024

# 一个源最多给它这么久来凑够上面的字节数，超了就判"太慢"（返回 None）。
#
# ⚠️ 来源慢**不是错** —— 它只是排在后面。所以这里返回 None 是
#    "速度未知"，调用方会**保留这个源**（见 `_rank_by_speed`），
#    而不是把它踢掉。
SPEED_PROBE_GIVE_UP_SECONDS = 8.0


def _probe_one(url: str, source: str, sess, timeout: float) -> float | None:
    """量一个真地址的速度，返回 **KB/s**；量不了返回 None。

    ⚠️⚠️ **这不是下载，是取样。** 取到的字节**直接扔掉**（不落盘、不碰
       `.part`）—— 所以它和断点续传是两条互不干扰的路。

    ⚠️ 只测**能安全直接 GET 的** http(s)：
       · CDDIS 要 `cddis_get` 跟跳转（凭据在跨主机时会掉），这里不测；
       · FTP 走不了 requests。
       两类都返回 None ⇒ 交给调用方按"速度未知"处理（排在测出来的后面）。
    """
    if not url.startswith(("http://", "https://")):
        return None
    if source == "cddis":
        return None
    if "igmas.org" in url:
        # ⚠️ iGMAS 有限流：**多打一次就多一次被限的风险**，
        #    而它本来就是个"备选产品"源。不值得为它冒这个险。
        return None

    import time as _t

    headers = {"Range": f"bytes=0-{SPEED_PROBE_BYTES - 1}"}
    t0 = _t.monotonic()
    got = 0
    try:
        with sess.get(url, timeout=min(timeout, 20), stream=True,
                      headers=headers, allow_redirects=True) as resp:
            if resp.status_code not in (200, 206):
                return None
            for chunk in resp.iter_content(CHUNK):
                got += len(chunk)
                if got >= SPEED_PROBE_BYTES:
                    break
                # ⚠️ **只有"太慢"才放弃。** 快不是问题 —— 见
                #    `SPEED_PROBE_MIN_BYTES` 那段（第一版把快当成了测不准）。
                if _t.monotonic() - t0 > SPEED_PROBE_GIVE_UP_SECONDS:
                    return None
    except Exception:  # noqa: BLE001
        return None

    dt = _t.monotonic() - t0
    # ⚠️ 判据是**字节数**，不是秒数。
    if got < SPEED_PROBE_MIN_BYTES or dt <= 0:
        return None
    return got / 1024.0 / dt


def _rank_by_speed(cands, *, ctx=None, timeout: float):
    """按**实测速度**把候选源重排：快的在前。

    ⚠️ 测不出来的排**在测出来的后面**、但**仍然保留**（按原顺序）——
       测速失败不等于源不可用（超时、限流、HEAD 被禁都可能），
       把它踢掉就等于"因为量不到而放弃一个本来能用的源"。

    ⚠️ 一个都没测出来就**原样返回** —— 排序是优化，不是前提。
    """
    scored: list[tuple[float, int, tuple]] = []
    measured: set[int] = set()
    for i, c in enumerate(cands):
        kbps = _probe_one(c[2], c[0], c[3], timeout)
        if kbps is not None:
            scored.append((kbps, i, c))
            measured.add(i)

    if not scored:
        return cands

    scored.sort(key=lambda x: (-x[0], x[1]))       # 快的在前；同速按原顺序
    ranked = [c for _, _, c in scored]
    ranked += [c for i, c in enumerate(cands) if i not in measured]

    if ctx is not None and len(scored) > 1:
        best = scored[0]
        ctx.progress(
            f"测速结果：{best[2][1]} 最快（{best[0]:.0f} KB/s），按这个顺序下",
            fraction=None,
        )
    return ranked


def _try_download(
    item: FilePlan,
    url: str,
    dest_dir: Path,
    *,
    source: str,
    session,
    ctx,
    timeout: float,
) -> DownloadResult:
    """从**一个**源下载。失败返回 ok=False 的结果。"""
    import requests

    sess = session or net.build_session()
    part = _local(dest_dir, item.filename + ".part")

    # ---- 断点续传：已经下了一部分就接着下 ----
    headers = {}
    already = part.stat().st_size if part.exists() else 0
    if already:
        headers["Range"] = f"bytes={already}-"

    try:
        if url.startswith("ftp://"):
            # ⚠️ FTP 走不了 requests（它只认 http/https）——
            #    所以这里塞一个**形状一样的假响应**进去，
            #    让下面那一大段（状态码判断 / 落盘 / 进度 / 取消）**原样复用**。
            #
            #    👉 不这么做的话，整段下载逻辑要照抄一份给 FTP，
            #       而"两份一样的代码"迟早会只改一份。
            from . import ftp as _ftp

            resp = _ftp.open(url, already=already, timeout=timeout)
        elif source == "cddis":
            # ⚠️ CDDIS 必须走这条 —— 见 credentials.cddis_get 的说明
            from .credentials import cddis_get

            resp = cddis_get(sess, url, timeout=timeout)
        else:
            # ⚠️ 断点续传要用 headers 里的 Range
            resp = sess.get(url, timeout=timeout, stream=True,
                            headers=headers, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind, source=source,
            detail=net._explain_exception(exc),
        )

    # ---- 状态检查 ----
    #
    # ⚠️ 状态码要看清，尤其是 206：那表示"服务器接受了断点续传"，
    #    响应体是**从续传点开始**的部分内容，不是整个文件。
    #    把 206 当 200 处理会把文件写坏，而且**看起来是成功的**（文件大小不对但不报错）。
    #
    # ⚠️ 先初始化为 0 —— 200 且没有 Content-Length 时它就该保持 0
    #    （那种情况下面会跳过完整性检查，**总比抛 NameError 好**）。
    total = 0

    if resp.status_code == 200 and already:
        # 服务器不支持续传，会从头发 —— 那之前的 .part 必须丢掉重来
        already = 0
        _unlink_quietly(part)
    elif resp.status_code == 206:
        # ⚠️⚠️ **206 要核对它到底从哪开始发的。**
        #
        #     实测（2026-09-24，`EGM2008.gfc`）：`.part` 最后是
        #     **78,655,393** 字节，而文件只有 **72,363,937** ——
        #     **正好多了 6 MiB**（= 它当时续传的起点）。
        #
        #     算术上和这件事完全吻合：**服务端收了 Range、也回了 206，
        #     但发的是整份文件**。而下面算总大小的写法是
        #
        #         total = Content-Length + already
        #
        #     —— 服务端发的既然已经是整份，这个加法就**把 total 也一起放大了**，
        #     于是"多出来 6 MiB"这件事在完整性检查里**看不出来**
        #     （written 和 total 同时多了 6 MiB，两边仍然相等）。
        #
        #     ⚠️ 这条**没有实测复现过**（这台机器上直连时服务端是对的，
        #        206 的 Content-Range 完全正常）。但它是唯一能同时解释
        #        "正好多 6 MiB"和"检查通过了"的机制。
        #
        #     👉 不管上面那个机制成不成立，**下面两件事本身就该做**：
        #        ① 总大小的**权威来源是 `Content-Range` 的第三段**，
        #           不是"我们猜的起点 + 响应长度"
        #        ② 服务端从**别的位置**发的数据，必须当场拒绝 ——
        #           拼到 .part 后面只会得到一个坏文件
        cr = resp.headers.get("Content-Range", "")
        _m = _RE_CONTENT_RANGE.match(cr.strip())
        if _m is None:
            resp.close()
            return DownloadResult(
                filename=item.filename, ok=False, kind=item.kind, source=source,
                detail=(
                    f"服务端回了 206 却没给可解析的 Content-Range（{cr!r}）—— "
                    "续传不可信，已放弃（重试会从头下）"
                ),
            )
        start, _end, size = int(_m.group(1)), int(_m.group(2)), int(_m.group(3))
        if start != already:
            # ⚠️ 不删 `.part` —— 它是好的，坏的是这次响应
            resp.close()
            return DownloadResult(
                filename=item.filename, ok=False, kind=item.kind, source=source,
                detail=(
                    f"续传位置不对：请求从 {already} 开始，服务端却从 {start} 发"
                    "（多半是把整份又发了一遍）。已放弃这份响应，重试即可"
                ),
            )
        total = size        # ← 权威值
    elif resp.status_code not in (200, 206):
        resp.close()
        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind, source=source,
            detail=f"HTTP {resp.status_code}",
        )

    # ---- 总大小（用于算进度）----
    #
    # ⚠️ 206 那条路**已经在上面把 total 定死了**（从 Content-Range 取），
    #    这里只处理 200 和"没有 Content-Length"的情况。
    if not total:
        clen = resp.headers.get("Content-Length")
        if clen and clen.isdigit():
            total = int(clen) + already

    # ---- 边下边写边报进度 ----
    written = already
    t0 = time.monotonic()
    try:
        mode = "ab" if already else "wb"
        with part.open(mode) as f:
            for chunk in resp.iter_content(CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)

                if ctx is not None:
                    if ctx.cancelled:
                        resp.close()
                        return DownloadResult(
                            filename=item.filename, ok=False, kind=item.kind, source=source,
                            detail=f"已取消（已下载 {written/(1024*1024):.1f} MB 保留在 .part 里）",
                        )
                    elapsed = time.monotonic() - t0
                    frac = (written / total) if total else None
                    speed = written / elapsed / 1024 if elapsed > 0 else 0
                    eta = ""
                    if total and speed > 0:
                        left = (total - written) / 1024 / speed
                        eta = f"，约剩 {left:.0f} 秒"
                    ctx.progress(
                        f"{item.filename}  "
                        f"{written/(1024*1024):.1f}"
                        + (f"/{total/(1024*1024):.1f}" if total else "")
                        + f" MB  {speed:.0f} KB/s{eta}",
                        fraction=frac,
                        bytes=written,
                    )
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 这里**不删 .part** —— 保留断点，下次能接着下。
        #    网络抖动是常态，保留进度能让用户重试的成本低得多。
        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind, source=source,
            detail=f"传输中断（{type(exc).__name__}）—— 已下载的部分保留，可重试",
        )
    finally:
        resp.close()

    # ---- 完整性检查 ----
    #
    # ⚠️ 这条检查很关键：**下载 "成功" 不等于文件完整**。
    #    常见的坑是服务器返回了一个 HTML 错误页但状态码是 200，
    #    或者连接被中间切断但没报错。
    # ⚠️⚠️ **两个方向都要查 —— 原来只查 `written < total`。**
    #
    #    实测（2026-09-24，`EGM2008.gfc`）：`.part` 下到了
    #    **78,655,393** 字节，而服务端说这个文件是 **72,363,937** ——
    #    **整整多了 6 MiB（= 6 × CHUNK）**。
    #
    #    ⚠️ 它**通过了**这道检查（因为 78655393 > 72363937，不满足 `<`），
    #       然后被交给 `_gunzip` —— 在那里炸成一个
    #       `zlib.error: invalid code lengths set`。
    #
    #    👉 那条错误信息**完全指不到真因**：读到它的人会去查 gzip、
    #       查源站、查文件格式，而真相是"这个文件被写多了 6 MB"。
    #
    #    **一道"完整性检查"如果漏掉一个方向，它挡不住它存在的理由。**
    if total and written != total:
        over = written > total
        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind, source=source,
            detail=(
                f"大小不对：得到 {written} 字节，应有 {total} 字节"
                + (
                    "（**多了** —— 续传时服务端多半把整份又发了一遍。"
                    "删掉 .part 重下）"
                    if over
                    else "（少了 —— 可重试续传）"
                )
            ),
        )

    # ---- 切掉服务器追加在末尾的垃圾（只有武大会有）----
    #
    # ⚠️⚠️ 必须在**识别和解压之前**做。
    #
    #     武大的响应是「完整的 gzip 流 + ~19 KB 的 ThinkPHP 调试 HTML」。
    #     识别那一步没事（它只看开头几 KB），但紧接着的 `_gunzip` 会抛
    #     `BadGzipFile` —— 于是**每一个武大的文件都是"下载成功、解压失败"**。
    #
    #     ⚠️ 判据是那个 HTML 标记，**不是接口返回的 size 字段**（它会过期）。
    #        详见 sources.sanitize 的说明。
    if sources.sanitize(source, part):
        # 切了就重算一遍已写入的字节数 —— 后面报告里那个数字要如实。
        try:
            written = part.stat().st_size
        except OSError:
            pass

    # ---- 按内容识别（不信文件名）----
    #
    # ⚠️ 识别发生在**解压之前**，用的是 identify 内部的嗅探 ——
    #    它自己会流式解压前几 KB 来判断，所以压缩态也能认出是什么。
    ident = identify.identify(part)

    # ---- 解压（如果需要）----
    #
    # ⚠️⚠️ 这一步曾经漏掉，而且**漏得非常隐蔽**。
    #
    #    症状：BKG 上的是 `.gz` 压缩文件，下载器原样存成了
    #    `IGS0OPSRAP_..._ORB.SP3`（没有 .gz 后缀）。
    #    然后识别、校验和、落盘**全部成功** —— 因为 identify 内部会解压看内容，
    #    所以它认得出是 SP3，报的"一切正常"。
    #
    #    但磁盘上那个文件**仍然是 gzip 压缩的**。
    #    Ginan 读它只会得到乱码，报一个和真正原因毫不相干的错。
    #
    #    > 教训：**"看起来成功"和"真的能用"是两回事。**
    #    > 识别通过只证明"这是 SP3 的压缩包"，不证明"这里有一个可用的 SP3"。
    #
    # 所以：识别之后必须把它真的解开。
    plain = part
    if ident.compression == "gzip":
        plain = _local(dest_dir, item.filename + ".plain")
        try:
            _gunzip(part, plain, source=source, kind=item.kind)
        # ⚠️⚠️ **不能只接 `OSError`。**
        #
        #    `_gunzip` 对非 iGMAS 的源会**原样抛出**它在 gzip 库里接到的异常，
        #    而那个异常最常见的是 **`EOFError`** ——
        #    `EOFError` 不是 `OSError` 的子类！
        #
        #    实测（截断的 gzip、源是 bkg）：
        #        ★★ EOFError: Compressed file ended before the
        #           end-of-stream marker was reached
        #           是 OSError 的子类吗？ False   ← 这句 except 接不住
        #
        #    而 `download_one` 函数体顶层**没有兜底 except**，
        #    `fetch_plan` 更是**一个 try 都没有** ——
        #    于是它一路穿到工具层：**整批下载中止**，
        #    用户拿到 traceback 而不是"哪些成了、哪些没成"。
        #
        #    ⚠️ 触发条件并不苛刻：只要某个源的响应没有 `Content-Length`
        #       （分块传输）又被中途切断，就是这条。
        #
        #    👉 判据：**"这一个文件解不开"是这一批里的一件事，不是这批的结局。**
        #       所以这里接 `Exception`，把它变成一条失败记录。
        except Exception as exc:  # noqa: BLE001
            _unlink_quietly(part, plain)
            return DownloadResult(
                filename=item.filename, ok=False, kind=item.kind, source=source,
                detail=(
                    f"解压失败（{type(exc).__name__}）：{exc}"
                    "—— 文件多半是传了一半，重试通常能好"
                ),
            )
        _unlink_quietly(part)
        # 解压后重新识别一次 —— 现在看的才是真实内容
        ident = identify.identify(plain)

    # ⚠️ Unix compress（.Z）—— 老日期（GPS 周 2237 之前）的 IGS 产品
    #    在 CDDIS 上就是这个格式。见 catalog.py 里 `_cddis_urls` 的说明。
    #
    # ⚠️⚠️ **这是"下载器不解压"那个坑第二次出现，形状一模一样：**
    #      上一次是 gzip —— 识别内部会解压看内容，所以"认出来了"，
    #      而磁盘上那个文件**仍然是压缩的**，Ginan 读它只会得到乱码。
    #      这一次是 .Z —— 多了一道更糟的：识别**根本认不出 .Z**
    #      （魔数 0x1f9d 不在嗅探表里），于是白名单把文件**删掉**，
    #      用户看到的是"下载失败"。
    #
    #    👉 `identify.sniff_bytes` 里加 .Z 只是让它**能认**，
    #       真正让它**能用**的是下面这几行。
    #       **"认出来"和"能用"永远是两件事。**
    if ident.compression == "z":
        plain = _local(dest_dir, item.filename + ".plain")
        try:
            lzw.unlzw_stream(part, plain)
        # ⚠️ 和上面 gzip 那条同一个理由：**接 `Exception`，不只接已知的那两个**。
        #    `.Z` 是 1980 年代的格式，真实文件里的畸形比 gzip 多得多，
        #    而"解不开"永远只该是**这一个文件**的事。
        except Exception as exc:  # noqa: BLE001
            _unlink_quietly(part, plain)
            return DownloadResult(
                filename=item.filename, ok=False, kind=item.kind, source=source,
                detail=(
                    f"解压 .Z 失败（{type(exc).__name__}）：{exc}"
                    "—— 文件多半是传了一半，重试通常能好"
                ),
            )
        _unlink_quietly(part)
        ident = identify.identify(plain)

    # ---- 交叉验证：要的类型 vs 实际拿到的类型 ----
    #
    # ⚠️⚠️ 这里**不能加 `ident.known and` 那个短路**。
    #
    #    踩过一次，而且后果很严重：
    #    原来的写法是 `if ident.known and item.kind in (...)` ——
    #    意思是"认得出类型时才做校验"。
    #
    #    于是 **CDDIS 的登录页 HTML**（识别为 unknown）直接跳过了校验，
    #    被当成 SP3 文件**保存了下来**，大小、校验和、落盘全部"成功"。
    #
    #    这比"下载失败"糟得多：
    #      下载失败 → 用户立刻知道，可以重试或换源
    #      存了个垃圾 → 用户以为成功了，直到跑 Ginan 才炸，
    #                   而那时的报错和真正原因（认证没过）毫无关系
    #
    #    正确的逻辑应该是**默认拒绝**：拿到了预期之外的东西就报错，
    #    而"认不出是什么"本身就是"预期之外"。
    #
    #    > 校验要写白名单（"是我要的才放行"），不能写黑名单
    #    > （"能识别的才检查"）—— 后者会把所有未知情况都放过去。
    if item.kind in ("atx", "sp3", "clk", "erp", "nav", "obs"):
        if not ident.known or ident.kind != item.kind:
            _unlink_quietly(plain)
            got = ident.kind if ident.known else "认不出来"
            # 网页单独给一个更直白的说明 —— 它九成是认证问题
            hint = ""
            if ident.kind == "html":
                hint = "。这通常是**认证没过**（比如 CDDIS 需要账号），或者路径不对"
            return DownloadResult(
                filename=item.filename, ok=False, kind=item.kind, source=source,
                detail=(
                    f"内容对不上：要的是 {item.kind}，拿到的是 {got}"
                    f"（{ident.evidence}）{hint}"
                ),
            )

    # ---- CrinEX：**认得出来，等下定名之后解码** ----
    #
    # ⚠️ 它**过得了上面那道校验** —— `kind` 确实是 `obs`，marker 也读得到，
    #    因为 CrinEX 的头部本来就是明文 RINEX。
    #
    #    Ginan 自己文档写着这个要求
    #    （`ginan-dist/source/src/cpp/common/acsConfigDocs.cpp:20`）：
    #
    #        The data should be uncompressed rinex (gunzipped,
    #        and not in hatanaka format)
    #
    # ⚠️ 这里**曾经是拒绝**（`ok=False` + "我们解不开这个编码"）。
    #    现在 `ginan/crinex.py` 能解了，所以改成**解码之后落盘** ——
    #    见下面 `final` 那一段。
    #
    # ⚠️ 为什么**不能**照原样存成 `.crx`：
    #    它不匹配配置里的 `ALIC*.rnx`，于是**静默不参与解算** ——
    #    用户会看到"下载成功"，然后拿到一个星级很差的解，
    #    而原因（观测数据根本没进去）离现象非常远。
    #    **下载失败会立刻被看到，静默不参与不会。**
    #
    # ⚠️ 这是**同一个形状第三次出现**（gzip 一次、`.Z` 一次，
    #    两处都写过长注释）。这次最隐蔽：残留内容开头是**纯文本**，人眼看不出来。

    # ---- 定名 ----
    #
    # ⚠️⚠️ 这一段必须**兜住 ValueError**，因为它拼的名字有一部分来自
    #     **文件内容**（观测数据的名字取文件头里的 MARKER NAME）——
    #     而那是**别人给的数据**，不是我们能控制的。
    #
    #     实测两种：
    #       · 标签写在第 1 列的 RINEX（不合规但真实存在）→
    #         名字变成 `MARKER.rnx`，`ok=True`、没有任何警告，
    #         而配置随后引用这个错名字
    #       · 标签里带 `:`（Windows 非法字符）→ `C:ALIC00AUS.rnx` →
    #         `_local` **正确地**拒绝了 —— 但拒绝的结果是
    #         **异常穿出 `download_one`、穿出 `fetch_plan`**，
    #         整批下载中止，用户拿到一个 traceback 而不是
    #         "这一个文件没成、别的继续"。
    #
    #     ⚠️ 文件名不合法的正确结局是**这一个文件失败**，
    #        而不是整个任务崩掉 —— 它和"内容对不上"是同一类问题。
    try:
        real_name = _decide_name(item, ident, plain)
        final = _local(dest_dir, real_name)
    except ValueError as exc:
        _unlink_quietly(plain)
        return DownloadResult(
            filename=item.filename, ok=False, kind=item.kind, source=source,
            detail=(
                f"按内容定出来的文件名不可用（{exc}）—— "
                "多半是观测文件头里的测站名有问题"
            ),
        )
    if final.exists():
        final.unlink()          # Windows 上 replace 到已存在的文件会失败

    # ⚠️⚠️ CrinEX 要**解码之后再落盘** —— 只是改名的话，
    #    磁盘上会出现一个名叫 `ALIC.rnx`、内容是 Hatanaka 编码的文件，
    #    而 Ginan 读到一半才报错，报错还和真正原因（编码没解）离得很远。
    #
    # ⚠️ 这里传 `compression=""`：外层压缩**上面已经剥过了**（`plain`
    #    就是解开之后的）。再传一次 gzip 会去解一份已经不压缩的东西。
    if ident.crinex:
        try:
            crinex.convert_to_rnx(plain, final, "")
        except Exception as exc:  # noqa: BLE001
            _unlink_quietly(plain)
            _unlink_quietly(final)
            return DownloadResult(
                filename=item.filename, ok=False, kind=item.kind,
                source=source,
                detail=(
                    f"CrinEX 解码失败（{exc}）—— 拿到的是 Hatanaka 编码的"
                    "观测数据，我们解不开这一份。换一个源试试"
                ),
            )
        _unlink_quietly(plain)
    else:
        plain.replace(final)

    return DownloadResult(
        filename=item.filename, ok=True, source=source, path=final,
        size=final.stat().st_size, sha256=_sha256(final),
        # ⚠️⚠️ 这里填的是**计划里的那一类**，不是 identify 认出来的那一类。
        #
        #    原来写的是 `kind=ident.kind or item.kind`，那是个真 bug：
        #
        #      · `ident.kind` **永远不会是空字符串**（认不出来时它是
        #        `"unknown"`），所以 `or item.kind` 那个兜底**永远不生效**
        #      · 于是 `kind` 变成了 identify 的**另一套词表**：
        #
        #            计划 kind     identify 认成     结果里存的
        #            snx           sinex            sinex      ✗
        #            bia           unknown          unknown    ✗
        #            table         unknown          unknown    ✗
        #            sp3/clk/erp/atx/nav/obs         同名       ✓
        #
        #      · 而下游（`workspace.names_of()`、配置生成）是按**计划的
        #        词表**去查的 —— 于是 `snx`/`bia` 这两类**明明下成功了，
        #        却被报成"下载失败"**，配置里那两栏永远是空的。
        #
        #    ⚠️ 那两类存在的**全部理由**就是消掉 Ginan 的两族警告
        #       （见 config.py 里对应那段注释）—— 等于白下，还不报错。
        #
        #    👉 判据：**"我们要的"和"我们认出来的"是两件事，不能共用一个字段。**
        #       下游要的是"这是哪一类文件"（计划的语义），
        #       而 identify 的答案是用来**校验**的（内容对不对得上），
        #       校验已经在上面做完了。
        kind=item.kind, real_name=real_name,
        detail=f"已保存为 {real_name}",
    )


def _unlink_quietly(*paths: Path) -> None:
    """删临时文件，**失败就算了**。

    ⚠️⚠️ 为什么不能让异常冒出来 —— 这是实测踩出来的（2026-09-24）：

        一个坏掉的 `.part`（比正常大了 6 MiB）让 `_gunzip` 抛 `zlib.error`，
        走到错误处理里，而 `part.unlink()` **自己又抛了**
        `PermissionError: [WinError 32] 另一个程序正在使用此文件`。

        ⇒ 用户和日志看到的是一条**文件被占用**的 traceback，
          而真因（下下来的文件是坏的）**一个字都没露出来**。

    ⚠️ Windows 上"删不掉"是**常态而不是意外**（杀毒软件、索引服务、
       另一个进程刚好开着它）。**清理失败不该改变这条记录说什么。**
       文件留着最坏也就是占点磁盘 —— 下次会续传或重下。

    ⚠️ `missing_ok=True` 只挡 `FileNotFoundError`，
       **挡不住 `PermissionError`** —— 这是两回事，别混成一个。
    """
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _gunzip(src: Path, dst: Path, *, source: str = "", kind: str = "") -> None:
    """流式解压 gzip。

    ⚠️ 用流式而不是 `gzip.decompress(src.read_bytes())`：
       天线文件解压后是 61 MB，没必要一次占那么多内存。

    ⚠️ 先写到 .tmp 再改名 —— 解压中途失败时不会留下半个文件
       冒充成完整的（那种文件 Ginan 会读到一半才报错）。

    ⚠️⚠️ **只有 iGMAS 允许"流没有正常结束"** —— 而且是有条件的：

        实测它的响应里，gzip 的**结束标记被服务器截掉了**
        （解压器报 `EOFError: Compressed file ended before the end-of-stream
        marker was reached`），但**数据本身是完整的** —— 解压出来 96 个历元、
        从 00:00 到 23:45、末尾有 SP3 自己的 `EOF`。

        ⚠️ 这有个代价：**标准 gzip 的 CRC 校验用不上了**，
           也就是说"传输中途被切断"原本靠它兜底，现在兜不住。
           → 所以 `_gunzip_tolerant` **额外要求解压结果以 `EOF` 结尾**，
             那是 SP3 格式自带的完整性标志，能顶上一部分。

        **换掉一层校验，就要补一层。**
    """
    import gzip
    import shutil as _shutil

    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        with gzip.open(src, "rb") as fin, tmp.open("wb") as fout:
            _shutil.copyfileobj(fin, fout, CHUNK)
    except (OSError, EOFError, gzip.BadGzipFile):
        _unlink_quietly(tmp)
        if source != "igmas":
            raise
        _gunzip_tolerant(src, dst, kind=kind)
        return
    tmp.replace(dst)


def _gunzip_tolerant(src: Path, dst: Path, kind: str = "") -> None:
    """解压一个**缺少结束标记**的 gzip 流；SP3 还要求内容以 `EOF` 结尾。

    ⚠️ 那个 `EOF` 检查**只对 SP3 生效**（参数传进来的 `kind`）——
       钟差和 ERP 都没有这种结尾标记，对它们查等于**误杀**。
       详见里面那段说明。

    ⚠️ 只给 iGMAS 用。别的源出现这种情况一律当**下载被截断**处理 ——
       那正是标准 gzip 的 CRC 要防的事，不该被"容错"放过。

    ⚠️ `zlib.decompressobj(16 + MAX_WBITS)` 是"按 gzip 头解压"的用法：
       16 是"带 gzip 头"的标志位。它流式工作，遇到流末尾就停，
       **不会因为缺少尾部 8 字节而报错**。
    """
    import zlib

    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        written = 0
        with src.open("rb") as fin, tmp.open("wb") as fout:
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                out = d.decompress(chunk)
                if out:
                    fout.write(out)
                    written += len(out)
            fout.write(d.flush())

        # ---- 补上的那道校验 ----
        size = tmp.stat().st_size
        if size == 0:
            raise OSError("解压出来是空的")
        with tmp.open("rb") as f:
            f.seek(max(0, size - 256))
            tail = f.read()
        # ⚠️⚠️ **只有 SP3 以 `EOF` 结尾。**
        #
        #     第一版**对所有类型都查 EOF** —— 于是 iGMAS 的钟差文件
        #     被**误杀**：它是 RINEX 时钟文件，末尾是最后一条观测记录，
        #     **本来就没有 EOF**。报错说"文件多半是传了一半"，
        #     而文件是完整的。
        #
        #     > 一条针对某一种格式的检查，**不能顺手用在所有格式上**。
        #     > 假阳性和假阴性一样有害 —— 而假阳性更难查，
        #     > 因为它把"好的"说成"坏的"，人只会去怀疑网络。
        if kind == "sp3" and b"EOF" not in tail:
            raise OSError(
                "解压结果末尾没有 EOF —— 文件多半是**传了一半**"
                "（iGMAS 的 gzip 没有结束标记，所以这里用 SP3 自己的 EOF 兜底）"
            )
    except Exception:
        _unlink_quietly(tmp)
        raise
    tmp.replace(dst)


# Windows 上**文件名里不能出现**的字符。冒号尤其要命：
# `C:xxx` 会被当成"盘符 + 相对路径"，`_local` 会拒绝，而拒绝的表现是整批崩。
#
# ⚠️ 顺带把路径分隔符也列进来 —— 从文件里读出来的值里带 `/` 或 `\`
#    同样是在拼路径（`../../etc/passwd` 那种）。
_ILLEGAL_IN_NAME = '<>:"/\\|?*'


def _safe_filename(raw: str, *, limit: int = 64) -> str:
    """把一段**从文件内容里读出来的**文本变成能当文件名的东西。

    ⚠️ 只保留"看得出是什么"的部分：字母数字、下划线、点、连字符。
       中文等其它字符统一换成 `_` —— 文件名是给 Ginan 和操作系统看的，
       不是给人看的（人看的是界面上的结论）。

    ⚠️ 返回空串表示**清洗完什么都不剩**，调用方必须处理这种情况。
       绝不返回一个"看起来能用其实不能用"的名字。
    """
    out = []
    for ch in raw.strip():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in "._-":
            out.append(ch)
        else:
            out.append("_")

    name = "".join(out).strip("._-")
    # ⚠️ Windows 上以点结尾的名字会被静默截断，`con` / `nul` 这类是保留名。
    #    这里只处理"看着就不对"的，不去枚举保留名 —— 那些正常不会出现。
    if not name or name in (".", ".."):
        return ""
    return name[:limit]


def _decide_name(item: FilePlan, ident: identify.FileIdentity, path: Path) -> str:
    """决定文件最终叫什么名字。

    ⚠️ 三种情况：

      ① 请求的名字就是对的 → 用它（比如 SP3/CLK 的名字很标准）
      ② 内容已经被识别、且有权威名字 → 用**内容里的**名字
         （RINEX 的测站名从文件头读，不从文件名猜 —— GinanUI 踩过这个坑）
      ③ 内容认不出来 → 用请求的名字兜底，但**不要加错误的扩展名**
    """
    # 观测数据：用文件头里的测站名，这是权威来源
    #
    # ⚠️⚠️ 但"权威"不等于"干净" —— 这个名字是**从别人的文件里读出来的**，
    #     而我们拿它去拼磁盘路径。所以要**先清洗再用**。
    #
    #     实测（清洗之前）两种坏结果：
    #       · 标签写在第 1 列的 RINEX（不合规，但真实存在）→
    #         `_extract_marker` 取到的是**标签本身** → 文件叫 `MARKER.rnx`，
    #         而且 `ok=True`、没有任何警告 —— 配置随后引用这个错名字
    #       · 标签里带 `:` → `C:ALIC00AUS.rnx` → `_local` 拒绝 →
    #         异常穿出去，**整批下载中止**
    # ⚠️ 但**数据集来的文件不适用** —— 见 `catalog.FilePlan.keep_name`。
    #
    #    那条规则治的是"名字是**别人给的**（浏览器下的、采集软件写的）"，
    #    而数据集的文件名是**我们自己照官方清单定的**，而且名字带信息
    #    （`HOB200AUS_S_20253150000_…` 里的时间）。
    #    SBAS 那 112 个观测只来自 4 个站 —— 按默认规则会变成
    #    `HOB2.rnx`、`HOB2_2.rnx`……，**一个时间信息都不剩**。
    if item.kind == "obs" and ident.marker and not item.keep_name:
        safe = _safe_filename(ident.marker)
        if safe:
            return f"{safe}.rnx"
        # ⚠️ 清洗完什么都不剩 → 说明那个"测站名"根本不是测站名。
        #    这时**退回请求里的名字**（计划里的），不要硬拼一个空的出来 ——
        #    空名字会让 `dest_dir / ""` 等于目录本身。
        #    （见 `_local` 的说明：那个坑踩过。）

    # 广播星历：**原样保留请求的文件名**。
    #
    # ⚠️⚠️ 这里原来会"把占位名换掉"，写的是：
    #
    #         f"BRDC_{item.filename.split('_')[-1] or 'nav'}.rnx"
    #
    #     那是**计划里还用占位名**的年代留下的。而计划现在给的是
    #     **真实的 IGS 文件名**（`BRDC00IGS_R_20260010000_01D_MN.rnx`）——
    #     于是按 `_` 拆开取最后一段，得到的是 `MN.rnx`，拼出来：
    #
    #         BRDC_MN.rnx.rnx          ← 名字被毁掉了
    #
    #     ⚠️ 这个 bug **一直被 bug① 挡着没暴露** ——
    #        广播星历从来就没下载成功过（源配错了），
    #        所以这一行根本没机会跑。等 ① 修好、真的下下来了，它才现形。
    #
    #        **一个 bug 会掩盖另一个 bug。** 修完一个，要回头看它下游
    #        有没有从来没被走到过的代码。
    #
    #     广播星历的文件名和 SP3/CLK 一样是 **IGS 标准命名**，
    #     必须原样保留 —— 配置里会引用它，改了就找不到。
    if item.kind == "nav":
        name = item.filename
        return name if name.endswith(".rnx") else f"{name}.rnx"

    # 有扩展名的保留（SP3/CLK/ERP 的名字是 IGS 标准命名，必须原样保留 ——
    # Ginan 的配置里会引用它们，改了就找不到）
    return item.filename


# =============================================================================
# 批量下载
# =============================================================================


def missing_tables(
    dest_dir: Path | None = None, workflow: str | None = None
) -> list[FilePlan]:
    """模型文件里，本地**还没有**的那些。

    ⚠️ `workflow` 决定要不要把**工作流特有**的那批也算进来（POD 的重力场）。
       不给就只算「完整」档那批通用模型 —— 见 `catalog.plan_tables`。

    ⚠️ 这是**一次性**文件，下过一次就一直用 —— 所以正常情况这里返回**空列表**，
       调用方不用做任何事。第一次是 10 个 / 17 MB（POD 另加 ~72 MB）。

    ⚠️ 判断标准是"文件在且非空"。不用比大小、不用比哈希：
       这些文件是静态模型，下下来是什么就是什么；而**校验太严**会带来
       另一个麻烦 —— 哪天官方换了版本，我们会一直认为"没下过"，
       然后每次运行都重下。
    """
    dest = dest_dir or paths.data_dir()
    out: list[FilePlan] = []
    for item in plan_tables(workflow):
        p = dest / item.filename
        if not (p.is_file() and p.stat().st_size > 0):
            out.append(item)
    return out


def fetch_tables(
    dest_dir: Path | None = None,
    *,
    workflow: str | None = None,
    ctx=None,
    session=None,
    proxy: net.ProxyConfig | None = None,
) -> FetchReport:
    """把缺的模型文件补齐（不缺就什么都不做）。

    ⚠️ `workflow` 决定要不要连**工作流特有**的那批一起下（POD 的重力场，
       约 72 MB）。不传就只下「完整」档那批通用模型 —— 见 `missing_tables`。

    ⚠️ 默认目录是 `paths.data_dir()` 而不是 `paths.tables_dir()` ——
       因为 `FilePlan.filename` 里**已经带了 `tables/` 前缀**
       （Ginan 的配置就是这么引用它们的）。多拼一层会拼成 `data/tables/tables/`。
    """
    dest = dest_dir or paths.data_dir()
    todo = missing_tables(dest, workflow)
    if not todo:
        return FetchReport()

    # ⚠️ 传 `plan=None` + `items=` —— 模型文件不属于任何一天，
    #    也谈不上"哪个测站的计划"。硬造一个假的 RunPlan 只为了让签名过得去，
    #    是在制造一个**看起来有意义的空壳**（它的 day 是 None，
    #    哪天有人从 plan 里读日期，就会拿到一个静默的 None）。
    return fetch_plan(
        None, dest, ctx=ctx, session=session, proxy=proxy, items=todo
    )


def fetch_plan(
    plan: RunPlan | None,
    dest_dir: Path,
    *,
    ctx=None,
    session=None,
    proxy: net.ProxyConfig | None = None,
    only_kinds: list[str] | None = None,
    items: list[FilePlan] | None = None,
) -> FetchReport:
    """按计划下载全部文件。

    ⚠️ 这里是**串行**的，不是并发。
       虽然并发能快很多，但：
         · 对数据中心的服务器不礼貌（可能被限速甚至封 IP）
         · 进度报告会乱（好几个文件同时报，用户看不懂）
         · 断点续传的判断复杂好几倍

       文件名带"精密"两个字的东西，用户对"快"的期待本来就不高，
       对"别出错"的期待很高。

    ⚠️ 单个文件失败**不中断整批** —— 继续下其余的。
       因为最后可能就差那一两个文件，让用户看到"还差什么"比"什么都没有"有用得多。
    """
    t0 = time.monotonic()
    report = FetchReport()

    # ⚠️ `items` 是给**备选产品**和**模型文件**用的：它让调用方换一批文件来下，
    #    而不是改 `plan.files`（那份是"镜像"的清单，不能动）。
    #    见 ginan_tools.do_fetch_data —— 备选那条路要用户点头之后才走。
    #
    # ⚠️ 给了 `items` 就不需要 `plan`（模型文件不属于任何一天，
    #    没有"取数计划"这个东西）—— 所以 plan 可以为 None。
    #    两个都没给才是调用方写错了。
    if items is None:
        if plan is None:
            raise ValueError("fetch_plan 至少要给 plan 或 items 之一")
        items = list(plan.files)
    else:
        items = list(items)

    # ⚠️ 日期只能从**计划**里来。`items=` 那条路（模型文件）没有日期，
    #    也**不能假造一个** —— 见 `fetch_tables` 里那段说明。
    day = plan.day if plan is not None else None
    if only_kinds:
        items = [f for f in items if f.kind in only_kinds]

    # ⚠️⚠️ **这里不要统一建一个会话传下去。**
    #
    #    原来写的是 `sess = session or net.build_session(proxy)`，
    #    然后把同一个会话喂给每个 `download_one` ——
    #    而 `download_one` 里"这个源要不要特殊会话"的判断是：
    #
    #         if session is not None:
    #             return session, False        ← 早返回
    #
    #    ⚠️ `session` 永远不是 None（上面那行保证了），所以那个分支
    #       **永远提前返回** —— CDDIS 那条分支**一次都没执行过**。
    #
    #    后果：`credentials.ScopedBasicAuth` / `cddis_session` 里
    #    那套"跨主机跳转要重新带凭据"的设计**全都是死代码**，
    #    下载 CDDIS 只会拿到一个登录页 ——
    #    而那看起来像"账号不对"，实际是"代码从来没带上账号"。
    #    **极难排查。**
    #
    #    👉 会话该在**每个源**那儿按需建。代价是每个源多一次
    #       `requests.Session()`（很便宜，不建连接就只是个小对象），
    #       换来的是"需要认证的源真的带上认证"。
    #
    #    ⚠️ 调用方**显式**传进来的会话仍然照传（测试用的假会话靠它）。
    for i, item in enumerate(items):
        # ⚠️ download_one 会按需自己建会话（比如 CDDIS 需要特殊认证），
        #    所以这里的 sess 只是"默认会话"，不一定被用上。
        if ctx is not None:
            if ctx.cancelled:
                report.results.append(
                    DownloadResult(filename=item.filename, ok=False, kind=item.kind, detail="已取消")
                )
                continue
            ctx.progress(
                f"[{i+1}/{len(items)}] {item.label}",
                fraction=i / len(items),
            )

        # ⚠️ `day` 必须传下去 —— 观测数据要靠它区分"哪一天的"。
        #    为 None 时说明调用方没给计划（`items=` 那条路），
        #    那时观测项本来也不在清单里（它只用来下产品）。
        report.results.append(
            download_one(
                item, dest_dir, day, session=session, ctx=ctx, proxy=proxy
            )
        )

    report.elapsed = time.monotonic() - t0

    if ctx is not None:
        ctx.progress(report.summary(), fraction=1.0)

    return report


def fetch_report_json(report: FetchReport) -> str:
    """给模型看的结果摘要。

    ⚠️ 只给摘要，不给每个文件的完整路径和校验和 ——
       那些对模型决策没用，只会占满上下文。
       详细的在复现包（repro/manifest.json）里。
    """
    import json

    return json.dumps(
        {
            "summary": report.summary(),
            "all_ok": report.all_ok,
            "elapsed_seconds": round(report.elapsed, 1),
            "files": [
                {
                    "requested": r.filename,
                    "saved_as": r.real_name,
                    "ok": r.ok,
                    "source": r.source,
                    "size_mb": round(r.size / (1024 * 1024), 2) if r.ok else 0,
                    "detail": r.detail,
                }
                for r in report.results
            ],
            "failed": [r.filename for r in report.failed],
        },
        ensure_ascii=False,
        indent=2,
    )
