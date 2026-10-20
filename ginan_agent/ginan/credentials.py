"""Ginan 领域层 —— 数据源凭据（主要是 CDDIS / Earthdata）。

============================ 为什么需要这个 ============================
CDDIS 上有 IGS 的最终产品（轨道、钟差、天线文件）。
它不需要付费，但**需要注册一个免费账号**。

问题在于认证机制又老又挑剔：

  · 用 HTTP Basic 认证，凭据存在 `.netrc` 文件里
  · ⚠️ **Windows 上文件名要带下划线**：`_netrc`
     （不带下划线的 `.netrc` 在某些 Python/库版本里读不到）
  · ⚠️ 而且 Windows 上**两个名字都写**最保险 ——
     GinanUI 的注释原话是 "Windows is not consistent"，
     这是踩过坑的人才会写的话
  · 权限要收紧（Unix 上 0600），否则 curl 之类的工具会拒绝使用

============================ ⚠️ 一个不能重复的错误 ============================
GinanUI 的 `save_earthdata_credentials()` **把整个 .netrc 覆盖掉**，
用户在其他地方配的凭据（比如公司内网的）会**被无声地抹掉**。

这是典型的"为了完成一个任务，破坏了用户别的东西"。

所以这里必须做**读-改-写**（read-merge-write）：
    读出现有的全部条目 → 只替换 CDDIS 那两条 → 写回去

> 原则：**修改一个共享资源时，只动你要动的那部分。**
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

# =============================================================================
# .netrc 的位置
# =============================================================================

# ⚠️ CDDIS 的认证会认两个域名，两个都要配：
#
#     urs.earthdata.nasa.gov   —— 登录入口
#     cddis.nasa.gov           —— 实际的数据服务器
#
#    只配一个的话，某些请求路径会认证失败 ——
#    而报错看起来像"密码错了"，非常误导。
NETRC_MACHINES = ("urs.earthdata.nasa.gov", "cddis.nasa.gov")


def netrc_paths() -> list[Path]:
    """该往哪几个位置写 .netrc —— **软件目录里**。

    ⚠️ Windows 上返回**两个**：`.netrc` 和 `_netrc`。
       因为"Windows 不一致" —— 不同的工具/库读的文件名不一样，
       两个都写最保险。代价只是多写一个小文件。

    ============================ ⚠️⚠️ 为什么不在家目录 ============================
    用户的原话：**"凭据怎么在 c 盘，要在软件目录内"**

    而启动横幅上写的承诺是：
        "这个程序会把**所有**数据都放在自己的文件夹里 ——
         包括你填的 AI 密钥、下载的观测数据、对话历史。"

    `config.json`（API 密钥）一直在 `userdata/` 里，**只有凭据跑到了 C 盘** ——
    承诺和事实对不上。用户去找的时候会很困惑，"我的东西到底在哪"。

    ⚠️ 这是个**有代价的取舍，不是白捡的**：
       原来放家目录的理由是"curl、gnasanalysis、Ginan 自己
       都会去读 `~/.netrc`"。搬进软件目录之后，
       **那些外部工具就读不到了。**

       而 CDDIS 的消费者**只有我们自己**（`cddis_session` / `cddis_get`，
       它们都走 `read_netrc`），所以这个代价我们付得起。
       但如果哪天要让外部的 curl 用同一份凭据，就得重新想。

    ⚠️ 家目录那份**不会被删**（见 `migrate_legacy`）——
       那还是用户的文件，我们只是不再往那儿写。
    """
    root = _app_root()
    if sys.platform == "win32":
        return [root / "_netrc", root / ".netrc"]
    return [root / ".netrc"]


def _legacy_paths() -> list[Path]:
    """老位置（家目录）。**只读不写** —— 为了兼容已经配好的用户。

    ⚠️ 不写 `~` 而用 `Path.home()` —— `~` 展开依赖 HOME 环境变量，
       在某些 Windows 环境下它没设或指向别处。
    """
    home = Path.home()
    if sys.platform == "win32":
        return [home / "_netrc", home / ".netrc"]
    return [home / ".netrc"]


def _app_root() -> Path:
    """软件自己的数据目录。

    ⚠️ 放在函数里 import 是为了避免 `ginan.paths` 和本模块的循环依赖
       （`paths` 会在需要时反向 import 设置，链条已经够绕了）。
    """
    from . import paths

    return paths.app_root()


# =============================================================================
# 读写
# =============================================================================


def read_netrc(path: Path) -> list[tuple[str, dict[str, str]]]:
    """读出 .netrc 里的全部条目。

    返回 [(machine, {"login":..., "password":...}), ...]

    ⚠️ 自己解析而不是用标准库的 `netrc` 模块，因为：
       · 标准库那个只给"按域名查"，不给"列出全部" —— 而我们要做读-改-写
       · 损坏的行会直接抛异常，而我们要能容忍（用户手动编辑过）

    格式（token 用空白分隔）：
        machine cddis.nasa.gov
        login myuser
        password mypass

        machine 别的服务器
        login xxx
    """
    entries: list[tuple[str, dict[str, str]]] = []
    if not path.is_file():
        return entries

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entries

    current: str | None = None
    fields: dict[str, str] = {}

    def flush() -> None:
        if current is not None:
            entries.append((current, dict(fields)))

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        key = parts[0].lower()

        # ⚠️ `default` 必须**在长度检查之前**处理。
        #
        #    踩过一次：原来的写法是先 `if len(parts) < 2: continue`，
        #    而 `default` 孤零零一个词、只有一个 part ——
        #    于是它被当成"格式不对的行"跳过，**根本没被解析**。
        #
        #    后果很隐蔽：读的时候丢掉，写回去的时候自然也没有了。
        #    用户 .netrc 里的兜底配置会被**静默删除**。
        #    而这正是我写这个模块时想避免的那类错误。
        #
        #    > 检查顺序也是逻辑的一部分。先做通用校验、再做特殊情况，
        #    > 很容易把特殊情况误杀。
        if key == "default":
            flush()
            current = "__default__"
            fields = {}
            continue

        if len(parts) < 2:
            continue

        if key == "machine":
            flush()
            current = parts[1]
            fields = {}
        elif key in ("login", "password", "account") and current is not None:
            fields[key] = parts[1]

    flush()
    return entries


def _render_netrc(entries: list[tuple[str, dict[str, str]]]) -> str:
    """把条目渲染回 .netrc 文本。"""
    out: list[str] = []
    for machine, fields in entries:
        if machine == "__default__":
            out.append("default")
        else:
            out.append(f"machine {machine}")
        if "login" in fields:
            out.append(f"    login {fields['login']}")
        if "password" in fields:
            out.append(f"    password {fields['password']}")
        if "account" in fields:
            out.append(f"    account {fields['account']}")
        out.append("")
    return "\n".join(out)


def write_credentials(
    user: str,
    password: str,
    paths: list[Path] | None = None,
) -> list[Path]:
    """把 CDDIS 凭据写进 .netrc。返回实际写入的文件列表。

    ⚠️⚠️ 这是**读-改-写**，不是覆盖。
       GinanUI 的实现直接构造一份新内容写进去，
       用户在其他地方配的凭据会被无声抹掉。
       这是"为了完成一个任务，破坏了用户别的东西"。

    ⚠️ 同时写多个路径（Windows 上是 _netrc 和 .netrc）——
       见 netrc_paths 的说明。

    参数 paths 用于**覆盖写入位置**。默认写真实的家目录；
    测试时指定一个临时路径，就不会碰用户的真文件。
    """
    written: list[Path] = []
    targets = paths if paths is not None else netrc_paths()

    for path in targets:
        # ① 先读出**现有的全部条目**
        existing = read_netrc(path)

        # ② 只把 CDDIS 相关的替换掉，其余原样保留
        keep = [
            (m, f) for m, f in existing
            if m not in NETRC_MACHINES and m != "__default__"
        ]
        # 兜底条目要**移到最后**（如果有的话）—— 它必须排在所有 machine 之后
        default = [(m, f) for m, f in existing if m == "__default__"]

        add = [
            (m, {"login": user, "password": password})
            for m in NETRC_MACHINES
        ]

        text = _render_netrc(keep + add + default)

        # ③ 写回
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            continue

        _restrict(path)
        written.append(path)

    return written


def _restrict(path: Path) -> None:
    """收紧文件权限。

    ⚠️ 在 Unix 上这是**必须的**，不只是好习惯：
       `curl` 和某些库会**拒绝使用**权限过松的 .netrc
       （防止别的用户读到你的密码）。
       用户会看到认证失败，但原因是"文件权限"，报错完全看不出来。

    ⚠️ Windows 上 chmod 基本不起作用（NTFS 权限模型不同），
       所以失败要**静默忽略** —— 不该因为收紧权限失败就让保存整个失败。
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:
        pass


def read_paths() -> list[Path]:
    """**读取**凭据时按顺序看哪几个文件。

    ⚠️ 软件目录优先，然后才是家目录 —— **写新、读旧**。

       不这么排的话：用户升级之后，凭据还在 C 盘那份里，
       而程序只看软件目录 → 判定"没配过" → 让他重新填一遍。
       明明东西还在，却要重填，是最让人恼火的那种"升级"。

    ⚠️ 顺序不能反：反了的话，用户在新位置改的密码会被旧位置盖掉，
       而现象是"我明明改了，怎么还是认证失败"。
    """
    seen: list[Path] = []
    for p in netrc_paths() + _legacy_paths():
        if p not in seen:
            seen.append(p)
    return seen


def has_credentials() -> bool:
    """现在有没有可用的 CDDIS 凭据。"""
    for path in read_paths():
        for machine, fields in read_netrc(path):
            if machine in NETRC_MACHINES and fields.get("login") and fields.get("password"):
                return True
    return False


def current_user() -> str:
    """读出现有凭据里的用户名（给界面回显用，**不返回密码**）。"""
    for path in read_paths():
        for machine, fields in read_netrc(path):
            if machine in NETRC_MACHINES:
                return fields.get("login", "")
    return ""


def migrate_legacy_into(
    targets: list[Path], sources: list[Path]
) -> list[Path]:
    """搬家逻辑本体（位置可注入，方便测试）。

    ⚠️ 参数化的理由和 `write_credentials(paths=...)` 一样：
       测试要能用临时目录，**绝不能碰用户真实的凭据文件**。
       首次运行的一次性迁移逻辑，一旦测错就是"把人家密码搞丢了"。
    """
    # 目标位置已经有凭据了？什么都不做。
    #
    # ⚠️ 这一条是**最重要的**：用户可能已经在新位置配过了，
    #    而家目录那份是个过期副本。搬过去就等于
    #    **把他刚改好的密码盖回旧的** —— 现象是"我明明改了，
    #    怎么反而认证失败了"，而且他根本想不到是程序干的。
    for path in targets:
        for machine, fields in read_netrc(path):
            if machine in NETRC_MACHINES and fields.get("login"):
                return []

    user = ""
    password = ""
    for path in sources:
        for machine, fields in read_netrc(path):
            if machine in NETRC_MACHINES and fields.get("login") and fields.get("password"):
                user = fields["login"]
                password = fields["password"]
                break
        if user:
            break

    if not user:
        return []

    try:
        return write_credentials(user, password, paths=targets)
    except OSError:
        return []


def migrate_legacy() -> list[Path]:
    """把家目录里的凭据**搬一份**到软件目录（老用户升级用）。

    ⚠️ **绝不删家目录那份。** 那是用户自己机器上的文件，
       而且可能还有别的工具在用。我们只是复制一份过来。

    ⚠️ 这个函数**有副作用**（会写文件），所以**不能**放在
       `has_credentials()` / `current_user()` 里 —— getter 不该改磁盘。
       调用点只有一处：程序启动时（见 `__main__.py`）。

    ⚠️ 搬不过来（或不需要搬）都不影响功能 —— **读取**那条路
       （`read_paths`）仍然会看到家目录那份。
       所以这个函数失败是**静默安全**的。
    """
    return migrate_legacy_into(netrc_paths(), _legacy_paths())


# =============================================================================
# ⚠️⚠️ CDDIS 的认证流程 —— 这里是最容易搞错的地方
# =============================================================================
#
# 实测出来的完整流程（4 跳）：
#
#     ① GET https://cddis.nasa.gov/archive/.../xxx
#            → 302 跳到 urs.earthdata.nasa.gov/oauth/authorize?...
#     ② GET https://urs.earthdata.nasa.gov/oauth/authorize?...
#            ← **这一步需要带凭据！** 通过后才返回 code
#            → 302 跳回 cddis.nasa.gov/proxyauth?code=xxx
#     ③ GET https://cddis.nasa.gov/proxyauth?code=xxx
#            ← CDDIS 用 code 换一个叫 ProxyAuth 的 cookie
#            → 302 跳回最初的地址
#     ④ GET 最初的地址（带上 ProxyAuth cookie）
#            → 200，拿到真实数据
#
# ⚠️⚠️ 关键在第 ② 步：**CDDIS 和 Earthdata 是两台不同的主机**。
#
#     而 `requests` 在跨主机跳转时会**主动剥掉 Authorization 头** ——
#     这是它的安全设计（防止把凭据泄露给第三方主机）。
#
#     也就是说：**用 requests 的默认行为，凭据永远到不了 Earthdata。**
#
#     症状极具误导性：最终拿到的是登录页，看起来像"密码错了"或
#     "账号没注册"，而实际上凭据完全正确。
#
#     这个 bug 不只影响登录测试 —— **所有 CDDIS 下载都会失败**。
#
#     （GinanUI 用的是 `session.auth = get_netrc_auth()`，
#       同样会被剥掉 —— 这条路它多半也没真正走通过。）
#
# 解法：自己实现一个认证类，**每一跳都重新带上凭据**，
#       但**只对白名单里的主机**带 —— 不能无脑发给所有跳转目标。


# 允许发送凭据的主机白名单。
#
# ⚠️ 这个白名单是**必须的**，不是保守。
#    因为我们要在跨主机跳转时带凭据 ——
#    如果无条件带，一个恶意的跳转目标就能骗到用户的密码。
#
#     只发给做认证这件事本身需要的主机，其余一律不带。
ALLOWED_AUTH_HOSTS = ("cddis.nasa.gov", "urs.earthdata.nasa.gov")


def _basic_header(user: str, password: str) -> str:
    import base64

    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class ScopedBasicAuth:
    """只在白名单主机上带凭据的 Basic 认证。

    ⚠️ 为什么不用 `requests.auth.HTTPBasicAuth`？

       它自己不管主机 —— 而 requests 会在**跨主机跳转时把它剥掉**。
       我们要的恰恰相反：**跨主机那一跳必须带**。

    ⚠️ 为什么不用 `session.auth = ...`？

       同样会被 requests 的 `rebuild_auth` 剥掉，行为完全一样。

    所以要实现成 `AuthBase` 的子类 —— `__call__` 会在**每一跳**被调用，
    我们在这个时机按主机决定加不加。
    """

    def __init__(self, user: str, password: str) -> None:
        self.user = user
        self.password = password

    def __call__(self, r):
        from urllib.parse import urlparse

        host = (urlparse(r.url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in ALLOWED_AUTH_HOSTS):
            r.headers["Authorization"] = _basic_header(self.user, self.password)
        return r


def cddis_session(
    user: str | None = None,
    password: str | None = None,
    proxy=None,
):
    """造一个能通过 CDDIS 认证的 requests 会话。

    ⚠️ **所有访问 CDDIS 的请求都必须用它**，包括下载。

       "认证只是登录时测一下"是错的 —— 每次下载都要走一遍那 4 跳。
       Ginan 自己下载时不走 Python，所以不受影响；
       但凡是我们下的 CDDIS 文件，都得用这个会话。

    凭据不传时，从 `.netrc` 里读。
    """
    from .net import build_session

    session = build_session(proxy)

    if user is None or password is None:
        found = _find_credentials()
        if found:
            user, password = found

    if user and password:
        session.auth = ScopedBasicAuth(user, password)

    return session


def _find_credentials() -> tuple[str, str] | None:
    """从 .netrc 里找出 CDDIS 的凭据。

    ⚠️ 用 `read_paths()` 不是 `netrc_paths()` —— **写新、读旧**。
       只读新位置的话，升级后还没搬过来的用户会突然"认证失败"，
       而他什么都没改。
    """
    for path in read_paths():
        for machine, fields in read_netrc(path):
            if machine in NETRC_MACHINES:
                u, p = fields.get("login"), fields.get("password")
                if u and p:
                    return u, p
    return None


# 跳转链最多跟几跳 —— 防止配置错误导致无限循环
MAX_REDIRECTS = 8


def cddis_get(session, url: str, *, timeout: float = 60.0, stream: bool = True):
    """带认证地跟随 CDDIS 的跳转链。**这是访问 CDDIS 的唯一正确方式。**

    ⚠️⚠️ 绝对不能改用 `allow_redirects=True`。

       试过了，不行。原因在 requests 的源码里：

           resolve_redirects() 里，跟随跳转时做的是
               prepared_request = req.copy()      ← 复制上一跳的请求
               self.rebuild_auth(prepared_request, resp)

           而 rebuild_auth 的逻辑是：

               if 主机变了:
                   del headers["Authorization"]   ← 删掉
                   ← 而且**不会重新加上**！
                     因为 copy 出来的请求不经过 prepare_auth，
                     我们那个 ScopedBasicAuth.__call__ 根本没机会跑。

       所以哪怕自己实现了认证类，开了 allow_redirects 也会在第 ② 跳被剥掉 ——
       而第 ② 跳恰恰是唯一需要凭据的一跳。

    这个函数做的事就是：**自己跟跳转，每一跳都重新构造请求**，
    这样 ScopedBasicAuth 每次都有机会加上凭据。

    返回最后一次响应（调用方负责 close）。
    """
    from urllib.parse import urljoin

    resp = session.get(url, timeout=timeout, stream=stream,
                       allow_redirects=False)

    hops = 0
    while resp.is_redirect and hops < MAX_REDIRECTS:
        hops += 1
        # ⚠️ Location 可能是相对路径 —— 必须 urljoin，
        #    直接当绝对地址用会拼出错误的 URL（而且报错莫名其妙）
        nxt = urljoin(resp.url, resp.headers["Location"])
        resp.close()
        resp = session.get(nxt, timeout=timeout, stream=stream,
                           allow_redirects=False)

    return resp


# =============================================================================
# 验证
# =============================================================================


@dataclass
class CredentialTest:
    ok: bool
    detail: str
    suggestion: str = ""


# 用一个"必须登录才能访问、而且确定存在"的地址来验证。
#
# ⚠️⚠️ 这个地址换过一次，换的原因值得记下来：
#
#    原来用的是 `https://cddis.nasa.gov/archive/00readme/` ——
#    参考了 GinanUI 的做法（它用这个地址探测连接）。
#
#    但实测发现：**这个路径现在返回 404 了**。
#    而 CDDIS 的 404 也是一个 HTML 页面 ——
#    于是"路径不存在"被报成了"认证失败"，
#    用户拿着**完全正确的凭据**反复检查密码。
#
#    > "照抄别人的做法"也要验证。那个做法在写的时候是对的，
#    > 但路径会失效，而失效的表现是**指向完全错误的方向**。
#
# 现在的选择标准是三条：
#    ① 确定存在（顶层目录，不会随日期消失）
#    ② 确定需要认证（整个 /archive/ 都在认证保护下）
#    ③ 内容小（目录列表，KB 级）
#
# 实测：`/archive/gnss/` 带凭据返回 200，不带凭据跳登录页。
TEST_URL = "https://cddis.nasa.gov/archive/gnss/"


def test_credentials(
    user: str,
    password: str,
    timeout: float = 20.0,
    proxy=None,
) -> CredentialTest:
    """**当场**用这对凭据发一个真实请求，看能不能过。

    ⚠️ 这个"当场测试"是整个流程里最关键的一步。

       不测的话，用户填完保存、看到"已保存"、以为成了 ——
       直到几十分钟后下载失败才发现密码打错了。
       而那时的报错是"下载失败"，完全联想不到是凭据问题。

       有了它，用户**填完立刻知道对不对**。

    ⚠️ 不能用"状态码是不是 200"来判断 —— CDDIS 未认证时返回的是
       **200 + 登录页**，不是 401。要看响应体里有没有登录页的特征。
    """
    if not user or not password:
        return CredentialTest(False, "用户名或密码是空的")

    # ⚠️ 必须用 cddis_session，不能用 requests.get(auth=...)。
    #
    #    因为 CDDIS 的认证要跨主机跳转（cddis → earthdata → cddis），
    #    而 requests 在跨主机时会把 Authorization 剥掉 ——
    #    凭据永远到不了该到的地方，报错却是"拿到登录页"（看起来像密码错）。
    #
    #    详见本文件上方那段流程说明。
    session = cddis_session(user=user, password=password, proxy=proxy)

    try:
        # ⚠️ 必须用 cddis_get（自己跟跳转），不能用 allow_redirects
        resp = cddis_get(session, TEST_URL, timeout=timeout)
        try:
            head = next(resp.iter_content(4096), b"") or b""
            status = resp.status_code
        finally:
            resp.close()
    except Exception as exc:  # noqa: BLE001
        from .net import _explain_exception

        return CredentialTest(
            False,
            f"连不上：{_explain_exception(exc)}",
            "如果平时需要代理才能访问国外网站，先在设置里配置代理。",
        )
    finally:
        session.close()

    return judge_credentials(status, head)


def judge_credentials(status: int, head: bytes) -> CredentialTest:
    """根据响应判断凭据有没有通过。

    ⚠️ 单独抽出来是为了**能测**。

       原来这段逻辑内联在 test_credentials 里，测试只能用猴补丁
       替换 requests.get 来测 —— 而一旦实现改成走 session，
       补丁就失效了，测试静默变成"永远通过"。

       **把判断逻辑和网络请求分开，是让判断逻辑可测的前提。**

    ⚠️ 判断依据是**响应体内容**，不是状态码。

       CDDIS 未认证时返回的是 **200 + 登录页**，不是 401。
       只看状态码会把"密码错了"判成"认证通过" ——
       用户填错密码却看到绿勾，是最坏的一种失败。
    """
    lowered = head[:4096].lower()

    if b"earthdata login" in lowered or b"<title>earthdata" in lowered:
        return CredentialTest(
            False,
            "认证没有通过（服务器还是返回了登录页）",
            "检查用户名和密码。注意 Earthdata 的用户名通常**不是邮箱**，"
            "而是注册时填的那个用户名。",
        )

    # ⚠️ 404 要单独区分 —— 它是"路径不对"，**和账号无关**。
    #
    #    实测踩过：CDDIS 的 404 页面也是 HTML，和登录页长得几乎一样，
    #    于是把"文件名拼错了"报成了"认证没过"，
    #    让排查方向完全跑偏。
    if b"404" in lowered and b"not found" in lowered:
        return CredentialTest(
            False,
            "认证可能没问题，但服务器返回了 404（找不到这个地址）",
            "这一般不是账号问题 —— 可能是测试用的路径变了。"
            "可以试试直接下载数据，能下就说明凭据是好的。",
        )

    if status >= 400:
        return CredentialTest(
            False,
            f"服务器返回 HTTP {status}",
            "如果反复出现，可能是 CDDIS 服务临时不可用，过一会儿再试。",
        )

    # 拿到的不是登录页、也没有报错 —— 说明认证过了
    return CredentialTest(True, "认证通过，CDDIS 可以访问")


# =============================================================================
# 2026-09-29：这条链**第一次被完整走通**，以及踩到的那个陷阱
# =============================================================================
#
# ============================ 一、认证确实是通的 ============================
# 在此之前，本文件顶部的 4 跳说明是**照着 requests 的行为推出来的**，
# 没有一次真实的成功记录（没账号）。这天拿到账号之后实测：
#
#     GET /archive/gnss/                      → 200（目录列表，14,448 字节）
#     GET .../products/2199/igr21992.erp.Z    → 200 + `1f 9d`（真 .Z 数据）
#     GET .../products/2438/IGS0OPSRAP_*.SP3.gz → 200 + `1f 8b`（96,771 字节）
#
# ⇒ **`ScopedBasicAuth` + `cddis_get` 这一套是对的**，而且它是**自足的**：
#   把 `trust_env` 关掉（不许 requests 去读任何 .netrc）之后仍然能下。
#   也就是说用户把凭据交给我们之后，这条路不依赖机器上的别的文件。
#
# ============================ 二、四种引导结局都对 ============================
# 用编造的凭据实测（免得动到真凭据），判据是界面那个「保存并验证」按钮
# 真正调用的 `judge_credentials()`：
#
#     ① 完全没有凭据      → 200 + 登录页  → 判失败「认证没有通过」  ✅
#     ② 用户名密码都编的  → 401           → 判失败「HTTP 401」      ✅
#     ③ 真用户名 + 错密码 → 401           → 判失败「HTTP 401」      ✅
#     ④ 真凭据            → 200 + 真数据  → 判通过                  ✅
#
# ⚠️ ②③ 拿到的是 **401**，不是"200 + 某个错误页"。
#   这一条值得记：`judge_credentials()` 最后那条分支是**兜底为真**
#   （"既不是登录页、也不是 404、状态码又 <400 → 判通过"），
#   本来担心"别的错误页会被判成绿勾"。实测下来 Earthdata 对错密码
#   返回的是规范的 401，所以那个兜底没有被触发。
#   **但它是"兜底"，不是"证据"** —— 以后再加源时别指望它。
#
# ============================ 三、⚠️⚠️ 那个陷阱：requests 会自己去读 .netrc ============
# 踩的过程：我想量"CDDIS 的老数据要不要账号"，就造了个**不带认证**的会话去下，
# 拿到 200 + 真数据，差一点写下「老数据不用账号」。
#
# 用 `curl`（真匿名）一对照，拿到的是 **302**。两边对不上，才查出原因：
#
#     `requests` 在 `trust_env=True`（默认）时会自己去找 `~/.netrc` / `~/_netrc`，
#     做法是 `Session.prepare_request` → `get_netrc_auth()`。
#     而且**跟跳转时 `rebuild_auth()` 还会再补一次** ——
#     这正好解释了为什么"内建的 netrc 流程能过 CDDIS，而显式设的 auth 不能"。
#
# ⇒ 两条实践含义，都很容易咬人：
#
#   ① **"匿名测一下"在这个项目里是个陷阱。**
#      只要家目录里有一份 .netrc，任何"匿名"探针都是假的。
#      要真匿名：`session.trust_env = False`。
#      （`scripts/check_network.py` 里那条守卫就是照这个写的。）
#
#   ② **家目录那份 .netrc 一直在，而且 requests 只认它。**
#      本程序写的凭据在软件目录（`netrc_paths()`），requests **不认那个位置**。
#      所以凡是"拿普通会话去碰 CDDIS"的地方，都会退化成
#      "只有这台机器**恰好**有份老 .netrc 时才通"。
#      —— `ginan/probe.py` 里那条探测就是这么坏了很久，
#         而本机恰好有那份老文件，所以看着一直是好的。
#
#      ⚠️ 这也意味着：**用户把家目录那份删掉，某些地方会立刻坏**，
#         而下载不受影响（下载走 `cddis_session`，是自足的）。
#         想让某个地方也自足，就用 `cddis_session()` / `cddis_get()`，
#         别用 `net.build_session()` 直接碰 cddis。

