"""Ginan 领域层 —— 网络会话与代理。

============================ 为什么单独一个模块 ============================
因为**所有**对外请求都必须走同一个会话。理由有三个，每个都是真实的坑：

  ① **代理配置只有一处**。如果每个模块自己 requests.get()，
     那代理设置就得在十个地方分别记得传 —— 漏一个就是"为什么这个能下那个不能"。

  ② **连接复用**。requests 的 Session 会复用 TCP 连接。
     我们要下载几十个文件，每个都重新握手会慢很多。

  ③ **统一的超时和重试策略**。数据源在境外，超时是常态而非异常。

============================ 代理的三种状态 ============================
    auto    跟随系统环境变量（HTTP_PROXY / HTTPS_PROXY）
            适合"我用的是全局代理/TUN 模式"的用户 —— 他们什么都不用填
    manual  显式指定代理地址
            适合"我的代理是个本地端口"的用户
    none    直连

⚠️ 为什么不"自动检测系统代理"？
   在 Windows 上确实能读注册表拿到系统代理设置，但：
     · 那是 IE/Edge 的设置，不一定是 curl/Python 走的
     · 很多代理软件用的是 TUN 模式，根本没有"设置"可读
     · 读到了也可能和实际情况不符
   所以：**让用户明确选**，比猜准得多。猜错的代价是"明明配了代理却还是连不上"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 默认超时（秒）。
#
# ⚠️ 这个数字是权衡的结果：
#   太短 → 境外源正常波动就被判为"不可达"，用户白开代理
#   太长 → 网络不通时要等很久才有反馈，界面像卡死
#
# 10 秒是经验值：够境外的慢速响应，又不至于让人等得不耐烦。
# ⚠️ 注意"探测"和"下载"用的超时**不一样** ——
#    探测只是打个招呼，10 秒足够；
#    下载可能要传几十 MB，得给长得多的超时。
DEFAULT_PROBE_TIMEOUT = 10.0
DEFAULT_DOWNLOAD_TIMEOUT = 120.0

# 下载时的分块大小（字节）。1 MB 一块，兼顾内存和进度粒度。
CHUNK_SIZE = 1024 * 1024

ProxyMode = Literal["auto", "manual", "none"]


@dataclass
class ProxyConfig:
    """代理设置。

    ⚠️ 这是**给用户看的**配置，所以默认值选了 auto ——
    最省事：用全局代理的人什么都不用填。
    """

    mode: ProxyMode = "auto"
    http: str = ""
    https: str = ""

    def to_requests(self) -> dict[str, str] | None:
        """转成 requests 库要的格式。

        返回 None 表示"别管，让 requests 自己按环境变量处理"。
        """
        if self.mode == "none":
            return {}          # 空字典 = 显式不用代理
        if self.mode == "manual":
            out: dict[str, str] = {}
            if self.http:
                out["http"] = self.http
            if self.https:
                out["https"] = self.https
            # 只填了一个时，另一个沿用它 —— 大多数代理两者用同一个地址
            if "http" in out and "https" not in out:
                out["https"] = out["http"]
            if "https" in out and "http" not in out:
                out["http"] = out["https"]
            return out or None
        return None            # auto

    def describe(self) -> str:
        """一句人话描述，显示在界面上。"""
        if self.mode == "none":
            return "直连（不使用代理）"
        if self.mode == "manual":
            addr = self.https or self.http or "（未填写地址）"
            return f"手动代理：{addr}"
        return "跟随系统代理设置"


def build_session(proxy: ProxyConfig | None = None):
    """造一个配置好的 requests 会话。

    ⚠️ 整个项目里**只应该有这一个地方**创建 requests.Session。
    其他模块都接收一个 session 参数 ——
    这样"代理设置有没有生效"就只需要检查一处。
    """
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()

    proxies = (proxy or ProxyConfig()).to_requests()
    if proxies is not None:
        session.proxies.update(proxies)
        if not proxies:
            # 显式直连：连环境变量也不认
            session.trust_env = False

    # 一个礼貌的 User-Agent。
    #
    # ⚠️ 有些数据源会拦默认的 python-requests UA。
    #    带上项目名和版本，既不容易被拦，也方便对方在日志里认出我们
    #    （万一我们的请求太多，对方能查到是谁，而不是直接封 IP）。
    session.headers["User-Agent"] = (
        "ginan-agent/0.1 (+https://github.com/) GNSS research tool"
    )

    # 重试：只重试连接类错误，不重试 404 这类"问了也白问"的。
    #
    # ⚠️ 一个真实的坑：很多数据源对**不存在的文件**返回的不是 404，
    #    而是 200 + 一个 HTML 错误页。所以状态码重试策略得保守一点，
    #    别把"文件不存在"当成"网络抖动"重试三次。
    adapter = HTTPAdapter(max_retries=0, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def head_ok(
    session,
    url: str,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> tuple[bool, int, str]:
    """探测一个 URL 通不通。

    返回 (是否通, 耗时毫秒, 人话说明)。

    ⚠️ 探测用 Range 请求只取头几个字节，不是 GET 整个文件。
       否则探一次 61 MB 的天线文件，用户要等很久。
       服务器普遍支持 Range；不支持时会退化成整个下载 ——
       所以超时设短一点，别真的把大文件拖下来。

    ⚠️ 判断"通"不能只看状态码。见 judge_data：
       CDDIS 认证失败返回的是 **200 + 登录页**，不是 401。
    """
    import time

    start = time.monotonic()
    try:
        resp = session.get(
            url,
            headers={"Range": "bytes=0-1023"},
            timeout=timeout,
            stream=True,        # 不把响应体读进内存
            allow_redirects=True,
        )

        # ⚠️⚠️ 必须在 close() **之前**把头部内容读出来！
        #
        # 这里踩过一次坑：原来的写法是先 resp.close() 再交给 judge_data 判断，
        # 结果 close 之后 iter_content 什么都读不到 ——
        # 于是"响应体里没有 Earthdata Login 字样"，CDDIS 被判为"正常"。
        #
        # 假阳性比假阴性更糟：用户以为 CDDIS 能用，等到真下载时才发现要账号，
        # 而那时候他已经在界面上看到过一次绿勾了，只会更困惑。
        #
        # 教训：**判断需要的数据，要在释放资源之前取出来。**
        status = resp.status_code
        try:
            head = next(resp.iter_content(2048), b"") or b""
        except Exception:  # noqa: BLE001
            head = b""
        finally:
            resp.close()    # 拿到就断开，别真的下载
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.monotonic() - start) * 1000)
        return False, elapsed, _explain_exception(exc)

    elapsed = int((time.monotonic() - start) * 1000)
    ok, detail = judge_data(status, head)
    return ok, elapsed, detail


def judge_data(status: int, head: bytes) -> tuple[bool, str]:
    """根据状态码和响应体开头，判断这个源到底能不能用。

    ⚠️ 这一段是本模块**最容易被低估**的地方。

    状态码本身几乎说明不了问题：
      · 200 可能是"文件在"，也可能是"请先登录"的登录页
      · 206 是 Range 请求的正常响应（部分内容）
      · 404 可能是"这个日期的文件还没出"，不代表源不通

    所以要看**响应体的特征**。这里的规则来自实测（见 docs/ginan-facts.md）：
      · CDDIS 未认证时返回 **200** + 标题为 "Earthdata Login" 的 HTML

    ⚠️ 参数是 (状态码, 响应体开头) 而不是 response 对象 ——
       因为调用方已经把连接关了。传值不传对象，
       就不可能再犯"关了连接还去读"的错误。
    """
    code = status

    if code in (200, 206):
        lowered = head[:2048].lower()
        if b"earthdata login" in lowered:
            return False, "需要登录（返回了 Earthdata 登录页，不是数据）"
        if b"<!doctype html" in lowered or b"<html" in lowered:
            return False, "返回的是网页而不是数据文件（可能路径变了或被拦截）"
        return True, f"正常（HTTP {code}）"

    if code in (401, 403):
        return False, f"需要认证（HTTP {code}）"
    if code == 404:
        # ⚠️ 404 有两种含义，这里必须区分：
        #    源通了但这天的文件还没发布 ≠ 源不通
        return True, "源可达，但指定的文件不存在（可能是日期太新，产品还没发布）"
    return False, f"异常状态码 HTTP {code}"


def judge_reachable(status: int, head: bytes) -> tuple[bool, str]:
    """宽松判断：**只要服务器应答了**就算可达。

    ⚠️ 和 `judge_data` 的区别就在这里，别弄混：

        judge_data        "这些字节是不是一个可用的数据文件"
        judge_reachable   "这台服务器回我话了吗"

    混用的后果实测过：给 GitHub 首页配 `judge_data`，
    于是它被判为**不通** —— 而它明明 200 正常应答。

    这个判断里 **404 也算可达** —— 服务器活着，只是路径不对。
    真正不可达的只有：超时、连不上、DNS 解析失败。

    ⚠️ 它和 `judge_data` 一样接收 (状态码, 响应体开头) 而不是 response 对象，
       这样调用方不可能犯"关了连接还去读"的错误。
    """
    if 200 <= status < 400:
        # 登录页要单独识别 —— 它虽然是 2xx，但拿不到数据
        lowered = head[:2048].lower()
        if b"earthdata login" in lowered:
            return False, "需要登录（返回了 Earthdata 登录页）"
        return True, f"可达（HTTP {status}）"
    if status in (401, 403):
        return False, f"可达但需认证（HTTP {status}）"
    if 400 <= status < 500:
        # ⚠️ 404 是"路径不对"，不是"服务器不可达" —— 这个区别很重要，
        #    因为前者换个路径就行，后者才需要代理。
        return True, f"可达（HTTP {status}，路径可能不对）"
    if status >= 500:
        return True, f"可达但服务端出错（HTTP {status}）"
    return False, f"异常状态码 {status}"


def probe_url(
    session,
    url: str,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    *,
    expect_data: bool = True,
) -> tuple[bool, int, str]:
    """探测一个 URL。

    ⚠️ 只保留这一个探测入口，`head_ok` 是它的 `expect_data=True` 特例。
       两个入口会让调用方选错，而选错的后果是误报
       （"GitHub 不通"这种）。

    参数：
        expect_data=True   要求拿到**真实数据文件**（SP3/ATX 之类）
        expect_data=False  只要服务器应答即可（首页、目录列表）
    """
    import time

    start = time.monotonic()
    try:
        resp = session.get(
            url,
            headers={"Range": "bytes=0-1023"},
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        )
        # ⚠️ 必须在 close() **之前**把头部读出来 —— 见 head_ok 里那段说明
        status = resp.status_code
        try:
            head = next(resp.iter_content(2048), b"") or b""
        except Exception:  # noqa: BLE001
            head = b""
        finally:
            resp.close()
    except Exception as exc:  # noqa: BLE001
        return False, int((time.monotonic() - start) * 1000), _explain_exception(exc)

    ms = int((time.monotonic() - start) * 1000)
    judge = judge_data if expect_data else judge_reachable
    ok, detail = judge(status, head)
    return ok, ms, detail


def _explain_exception(exc: Exception) -> str:
    """把异常翻译成人话。

    ⚠️ 用户看到的应该是"连不上，可能需要代理"，
       而不是 "ProxyError: HTTPSConnectionPool(host=...)"。

    这一层翻译是**新手体验的关键** ——
    原始异常对懂技术的人是信息，对不懂的人是噪音。
    """
    name = type(exc).__name__
    text = str(exc).lower()

    if "timeout" in name.lower() or "timed out" in text:
        return "超时 —— 连不上，这种情况通常需要开启代理"
    if "proxy" in text:
        return f"代理设置有问题（{name}）—— 检查代理地址填对了没"
    if "name or service not known" in text or "getaddrinfo" in text:
        return "域名解析失败 —— 可能需要开启代理，或者检查网络"
    if "ssl" in text or "certificate" in text:
        return f"SSL 证书问题（{name}）—— 可能是代理拦截了 HTTPS"
    if "connection" in text:
        return "连接被拒绝或中断 —— 可能需要开启代理"
    return f"{name}: {exc}"
