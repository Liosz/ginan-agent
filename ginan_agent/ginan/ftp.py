"""匿名 FTP 下载 —— 只为了让 `requests` 够不着的那条路也能走。

============================ 为什么需要这个文件 ============================
`fetch.py` 的整条下载链路是围着 `requests` 写的：拿一个响应对象，
然后 `iter_content()` 一小块一小块地写盘、报进度、响应取消。

而**武汉大学的匿名 FTP**（`ftp://igs.gnsswhu.cn/…`）是一条有价值的独立通道：

    · 路径**可以直接拼**（`/pub/gps/products/{周}/{文件名}`）——
      不用两步查询
    · **没有尾部那 19 KB 调试垃圾**（HTTP 那条有，见 sources.strip_trailer）
    · 实测 255 KB/s，比 BKG 还快 4~5 倍

    代价是比 HTTP 慢 5~10 倍（HTTP 实测 1.3~3 MB/s）。
    但 2024 年那次改版把 HTTP 的目录树整个关掉了 ——
    **说明这两条路会被分别改坏**，都留着才有意义。

⚠️ 而 `requests` **不支持 ftp://**（`InvalidSchema`）。

============================ 怎么接进来的 ============================
不去改 `fetch.py` 的主流程，而是**在这里造一个和 requests 响应形状一样的对象**：

        resp.status_code      200 / 206
        resp.headers          （FTP 没有头，给个空 dict）
        resp.iter_content(n)  一块一块地吐字节
        resp.close()

`fetch._try_download` 拿到它之后**完全不知道自己在走 FTP** ——
状态码判断、断点续传、落盘、进度、取消，那一大段一个字都不用改。

> **让新东西去适配老的形状，比让老代码为新的东西开岔路要好。**
> 开岔路的写法，迟早会变成"两份差不多的代码，只改了一份"。

============================ ⚠️ 用标准库，不加依赖 ============================
`ftplib` 是 Python 自带的。这个项目对加依赖很克制
（连 LZW 解压都是自己写的，见 lzw.py）。
"""

from __future__ import annotations

import ftplib
from urllib.parse import unquote, urlparse

# 分块大小 —— 和 fetch.CHUNK 保持一致就行，这里是独立的一份，
# 免得两个模块互相 import 成环。
CHUNK = 64 * 1024


class FtpResponse:
    """长得像 `requests.Response` 的 FTP 响应。

    ⚠️ 只实现 `fetch._try_download` 真正用到的那几个成员。
       **不要去实现没用到的东西** —— 那是照着想象写，猜错的地方没人会发现。
    """

    def __init__(self, ftp: ftplib.FTP, path: str, already: int = 0) -> None:
        self._ftp = ftp
        self._path = path
        self._already = already
        self._conn = None
        self._done = False

        # ⚠️ 断点续传：FTP 用 `REST` 命令（`retrbinary` 的 `rest=` 参数）。
        #    服务器支持时它返回 206（和 HTTP 的语义一样），
        #    不支持时**从头开始** —— 而 fetch 那边的判断是
        #    `status_code == 200 and already` → 丢掉 .part 重来。
        #    所以这两种情况各自都能被正确处理。
        self.status_code = 206 if already else 200

        # FTP 没有响应头。给个空 dict 而不是 None ——
        # fetch 那边会 `resp.headers.get("Content-Length")`。
        self.headers: dict[str, str] = {}

    def iter_content(self, chunk_size: int = CHUNK):
        """一块一块地吐字节。第一次取值时才真的去连。

        ⚠️ **数据连接是在这里建的，不在 `__init__` 里** ——
           连接失败要变成"这一条下载失败"（`fetch` 那边接的是
           `except Exception` → 一个 ok=False 的结果），
           而不是在构造对象时就抛出去。

        ⚠️ 续传用 `transfercmd(..., rest=N)`（FTP 的 `REST` 命令），
           **必须和 RETR 一起发** —— 分两步写（先 RETR 再重开一个带 REST 的）
           会白建一条连接。
        """
        if self._conn is None:
            self._conn = self._ftp.transfercmd(
                f"RETR {self._path}", rest=self._already or None
            )

        while True:
            data = self._conn.recv(chunk_size)
            if not data:
                break
            yield data
        self._done = True

    def close(self) -> None:
        """收尾。**绝不能抛异常** —— 它是在 finally 里被调的。"""
        for closer in (self._conn, self._ftp):
            if closer is None:
                continue
            try:
                if closer is self._conn and self._done:
                    # 正常读完 → 告诉服务器数据连接结束
                    self._ftp.voidresp()
                closer.close()
            except Exception:  # noqa: BLE001
                pass


def open(url: str, *, already: int = 0, timeout: float = 30) -> FtpResponse:  # noqa: A001
    """打开一个 `ftp://` 地址，返回一个**形状像 requests 响应**的对象。

    ⚠️ 函数名 `open` 会遮蔽内置的 —— 是刻意的：调用处写成
       `ftp.open(url)` 读起来就是"打开这个地址"。
       （`fetch.py` 里是 `from . import ftp as _ftp` 再 `_ftp.open(...)`，
         不会和内置的撞。）
    """
    u = urlparse(url)
    if u.scheme != "ftp":
        raise ValueError(f"不是 ftp 地址：{url}")

    ftp = ftplib.FTP()
    ftp.connect(u.hostname or "", u.port or 21, timeout=timeout)
    # ⚠️ 匿名登录：用户名 `anonymous`，密码惯例上填个邮箱。
    #    武大那台不校验，填什么都行。
    ftp.login("anonymous", "ginan-agent@localhost")
    # 二进制模式 —— 不设的话文本模式下换行会被改写，**文件就坏了**。
    ftp.voidcmd("TYPE I")

    return FtpResponse(ftp, unquote(u.path), already=already)


def probe(url: str, timeout: float = 15) -> tuple[bool, str]:
    """只连一下、不下载 —— 给探测用的。

    返回 (通不通, 说明)。**不抛异常**：探测层的约定是"每个源自己报告"。
    """
    u = urlparse(url)
    ftp = None
    try:
        ftp = ftplib.FTP()
        ftp.connect(u.hostname or "", u.port or 21, timeout=timeout)
        ftp.login("anonymous", "ginan-agent@localhost")
        ftp.voidcmd("TYPE I")
        # ⚠️ 用 SIZE 确认那个文件**真的在** —— 只连上服务器证明不了什么。
        #    和 HTTP 那边"必须是真的数据文件、不能只看 200"是同一个原则。
        size = ftp.size(unquote(u.path))
        return True, f"可达（{size} 字节）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"
    finally:
        if ftp is not None:
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass
