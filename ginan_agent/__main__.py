"""启动器 —— 起本地服务，开窗口（或浏览器）。

跑法：
    python -m ginan_agent            # 起服务并打开浏览器
    python -m ginan_agent --no-open  # 只起服务，自己手动开
    python -m ginan_agent --port 8899

============================ 关于端口 ============================
用**随机空闲端口**而不是写死 8000 之类的。理由：

  · 写死的端口可能被别的程序占用，用户看到的是"启动失败：端口被占用" ——
    一个他完全不知道该怎么办的错误
  · 端口固定的本地服务更容易被别的东西扫到
  · 反正我们会自动打开浏览器，用户不需要记端口号

============================ 为什么先开浏览器，不用 pywebview ============================
pywebview 会带来平台依赖（Windows 需要 WebView2、Linux 需要 GTK/Qt）。
先把服务和界面做扎实，窗口壳是一个**约 40 行的附加件** ——
而且两边的界面**字节级相同**（都是同一个页面），
所以推迟它不损失任何东西。

⚠️ 注意不能用 file:// 打开 index.html。
   因为 SSE（EventSource）需要一个真实的 HTTP 来源，
   file:// 下浏览器会拒绝。所以服务是必须的，两种模式都要有。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import socket
import sys
import threading
import time
import webbrowser


def _free_port(preferred: int = 0) -> int:
    """找一个能用的端口。

    ⚠️ 用"绑定 0 端口让系统分配"的做法，而不是自己随机猜一个再试 ——
    猜的那个可能在"猜完"和"真正绑定"之间被别人抢走（竞态）。
    让系统直接分配则不存在这个窗口期。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
        except OSError as exc:
            # ⚠️⚠️ 指定端口被占用时，原来是**一坨裸 traceback**，
            #    而启动器只会说一句"程序异常退出"——
            #    **错误信息里没有"用户能做的下一步"**，这违反本项目自己的规矩。
            #
            #    ⚠️ 默认那条路（自动挑端口）不会走到这儿；
            #      只有用户显式 `--port` 时才可能。
            raise SystemExit(
                f"\n  端口 {preferred} 用不了：{exc}\n"
                f"  换个端口：python -m ginan_agent --port {preferred + 1}\n"
                f"  或者干脆不指定（默认会自动挑一个空闲的）"
                f"——去掉 --port 就行。\n"
            ) from None
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ginan-agent",
        description="AI 驱动的 GNSS 精密定位工作台",
    )
    parser.add_argument("--port", type=int, default=0,
                        help="监听端口（默认自动挑选一个空闲的）")
    parser.add_argument("--no-open", action="store_true",
                        help="不要自动打开浏览器")
    parser.add_argument("--no-browser", action="store_true",
                        help=argparse.SUPPRESS)  # 兼容旧写法
    args = parser.parse_args(argv)

    from .ginan import paths
    from .server.app import create_app

    # ⚠️ 目录在用之前先建出来。不这么做的话，第一次运行时
    #    "检查环境"会因为目录不存在而报一个很吓人的错 ——
    #    而实际上它只是还没建而已。
    paths.ensure_dirs()

    # ⚠️ 老用户升级：凭据以前存在家目录（C:\Users\xxx\_netrc），
    #    现在改存软件目录了。搬一份过来，**但不删旧的**。
    #
    #    放在这里（启动时一次）而不是 `has_credentials()` 里 ——
    #    那是个 getter，不该写磁盘。
    #
    #    失败也不影响启动：读取那条路仍然会看到家目录那份。
    try:
        from .ginan import credentials

        moved = credentials.migrate_legacy()
        if moved:
            print(f"  凭据已搬到软件目录：{moved[0]}")
    except OSError:
        pass

    port = _free_port(args.port)
    app = create_app()
    url = f"http://127.0.0.1:{port}/"

    # ⚠️ 显式 flush —— 见 bootstrap.py 里的说明。
    #    不加的话，输出被重定向时这些提示会卡在缓冲区里，
    #    用户看不到"界面地址"这一行，就不知道浏览器该开哪个网址。
    for line in (
        "",
        "  Ginan Agent",
        "  " + "─" * 46,
        f"  界面地址   {url}",
        f"  数据目录   {paths.data_dir()}",
        f"  收件箱     {paths.inbox_dir()}",
        "  " + "─" * 46,
        "  按 Ctrl+C 退出",
        "",
    ):
        print(line, flush=True)

    # 等服务真的起来再开浏览器 —— 否则会打开一个"无法连接"的页面，
    # 用户的第一印象就是"这东西坏了"。
    if not (args.no_open or args.no_browser):
        def _open_when_ready() -> None:
            for _ in range(50):          # 最多等 5 秒
                time.sleep(0.1)
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        webbrowser.open(url)
                        return
                except OSError:
                    continue

        threading.Thread(target=_open_when_ready, daemon=True).start()

    # =========================================================================
    # ⚠️⚠️ 为什么不用 `uvicorn.run()` —— Ctrl+C 会打出一大坨 traceback
    # =========================================================================
    #
    # 用户按 Ctrl+C 之后看到的是一屏红色堆栈。**那看着就像程序崩了**，
    # 而它其实是一次完全正常的退出。
    #
    # ⚠️⚠️ 成因有**两个**，而且是实测比出来的 —— 我一开始只找到一个，
    #      修完发现堆栈照打（A/B：旧 4677 字符，新还是 4677 字符）。
    #
    #   ① **uvicorn 退出前会把信号再抛一次**
    #      `Server.capture_signals()` 除了设 `should_exit`（这一步是对的），
    #      还会 `signal.raise_signal()` 把 SIGINT **在事件循环里**重抛一遍。
    #      → 正在跑的请求被取消。
    #
    #   ② **uvicorn 把"请求被取消"当成 ERROR，还打完整堆栈**
    #      退出时正好有慢请求在跑（"探测数据源"要挨个连 11 个源，
    #      最坏几十秒）。优雅退出等不了 → 取消它 → 于是：
    #
    #          ERROR:    Cancel 1 running task(s), timeout graceful shutdown exceeded
    #          ERROR:    Exception in ASGI application
    #          Traceback ... 几十行 ... CancelledError
    #
    #      ⚠️ 可"被取消"不是故障，是**用户按了退出的必然结果**。
    #
    # 👉 所以两条都要治（`make_server()` 里）：
    #      ① 不让 uvicorn 接管信号，我们自己接、只设 `should_exit`
    #      ② 把②那两族假 ERROR 从日志里滤掉
    #
    # ⚠️ 光治① 不够，光治② 也不够 —— 这个"两个都要"是**跑出来**的，
    #    不是想出来的。见 scripts/check_launcher.py ⑤c 那条端到端测试。
    #
    # ⚠️ 另外 `except KeyboardInterrupt: pass` 也拦不住 ——
    #    堆栈在我们接到异常**之前**就已经打出来了。
    server = make_server(app, port)
    install_quit_handler(server)
    try:
        server.run()
    finally:
        # 恢复默认处理，免得影响同一进程里后面的东西
        signal.signal(signal.SIGINT, signal.default_int_handler)

    return 0


class _QuietShutdown(logging.Filter):
    """退出时，把两族**根本不是错误**的日志丢掉。

    ============================ 为什么要这个 ============================
    ⚠️⚠️ 用户按 Ctrl+C，屏幕上是一屏红色堆栈 —— **那看着就是程序崩了**，
        而它是一次完全正常的退出。实测抓到的原话：

            ERROR:    Cancel 1 running task(s), timeout graceful shutdown exceeded
            ERROR:    Exception in ASGI application
            Traceback (most recent call last):
              ...几十行...
            asyncio.exceptions.CancelledError

        成因：退出时正好有个**慢请求**在跑（比如"探测数据源"，它要挨个
        连 11 个源，最坏几十秒）。优雅退出等不了那么久 → 把它取消 →
        而 uvicorn 把"被取消"**当成异常记成 ERROR，还打完整堆栈**。

    ⚠️ 可"被取消"不是故障，是**用户按了退出**的必然结果。
       把它报成 ERROR 是**把正常操作说成错误** ——
       这个项目在 `bootstrap._is_user_quit()` 那里已经踩过一次同样的坑
       （关窗口被报成"程序异常退出"，用户以为坏了）。

    ⚠️ 判据收得很紧，**只丢这两族**，别的照常打：
        ① 那一行 "Cancel N running task(s)…"
        ② **异常类型是 CancelledError** 的记录

       不按消息文案匹配（文案会随 uvicorn 版本变），
       而是看 `exc_info` 里的**异常类型** —— 那个才是不变量。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()

        # ① "Cancel N running task(s), timeout graceful shutdown exceeded"
        if "running task" in msg and "shutdown" in msg:
            return False

        # ② 被取消的请求。⚠️ 看类型，不看文案。
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, asyncio.CancelledError):
            return False

        return True


def make_server(app, port: int):
    """把 uvicorn 的服务对象配好（但**不启动**）。

    ⚠️ 抽出来是为了能被测试 —— 见 `scripts/check_launcher.py` 里"Ctrl+C 要安静"
    那一节。它要能自己造一个 server、自己按流程停掉，然后断言**没打堆栈**。
    """
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",     # ⚠️ 只绑本机。这个服务能起子进程、能读写磁盘，
                              #    绝不能监听 0.0.0.0。
        port=port,
        log_level="warning",  # 默认的 info 太吵，会盖住我们自己的输出
        access_log=False,
        # ⚠️ 收工最多等这么久，不等就一直等。
        #    `/api/events` 是一条**长连接**（SSE），不设上限的话
        #    优雅退出会一直等它 —— 用户按了 Ctrl+C 却像卡住了。
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)

    # ⚠️⚠️ 关掉 uvicorn 对信号的接管。
    #
    #    它的 `capture_signals()` 除了设 `should_exit`（这一步是对的），
    #    还会在退出前**把收到的信号再抛一次**（`signal.raise_signal`）——
    #    那个 SIGINT 是在**事件循环里**抛的，会把正在跑的请求取消掉。
    #
    #    ⚠️ 但这一条**只是半个原因**（实测：光去掉它，堆栈照打）。
    #       另一半见下面那个日志过滤器。
    #
    #    换成一个空的上下文管理器就行 —— 它在该位置只被当作 `with` 用。
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    # ⚠️⚠️ 顺序不能反：`config.load()` 会跑 `logging.config.dictConfig()`，
    #    而那一步**会把 logger 上的 filter 清掉**。
    #    所以必须**先** load、**再**装过滤器。
    #
    #    （`load()` 自己是幂等的 —— `if not self.config.loaded` ——
    #      所以在这里提前调一次，`server.run()` 里那次就成了空操作。）
    config.load()  # noqa: B018
    silence_shutdown_noise()

    return server


def silence_shutdown_noise() -> None:
    """给 uvicorn 的 logger 装上那个过滤器。

    ⚠️ 必须在 `config.load()` **之后**调用 —— 那一步会跑
       `logging.config.dictConfig()`，把 logger 上的 filter 清掉。
       所以在 `server.run()` 之前那一刻装。
    """
    lg = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _QuietShutdown) for f in lg.filters):
        lg.addFilter(_QuietShutdown())


def install_quit_handler(server) -> None:
    """把 Ctrl+C 接到 `should_exit` 上 —— **只设标志，不抛异常**。

    ⚠️ 第二次 Ctrl+C = 立刻退（有些连接不肯关，得留一条强硬的路）。
       这和 uvicorn 自己 `handle_exit` 的语义一致 ——
       **自己接管信号，就得把那套语义也接过来，不能只接一半。**
    """

    def _on_sigint(signum, frame) -> None:  # noqa: ARG001
        if server.should_exit:
            server.force_exit = True
        else:
            server.should_exit = True
            # ⚠️ 说一句。什么都不显示的话，用户看不出"它在退"还是"它卡住了"——
            #    尤其当有请求正在跑，优雅退出要等一小会儿的时候。
            print("\n  正在退出……", flush=True)

    signal.signal(signal.SIGINT, _on_sigint)


if __name__ == "__main__":
    sys.exit(main())
