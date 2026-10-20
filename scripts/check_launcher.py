"""自检：启动器 —— 「双击就能跑」这条路的守卫。

跑法：python scripts/check_launcher.py

============================ 为什么这些规则需要测试 ============================
因为这个模块的**失败方式**全都是"静默"的：

  · `.bat` 里混进一个中文字符 → cmd 把它当命令执行 → 脚本死掉
    而且**开发时不一定发现** —— 取决于你用什么编辑器打开、保存成什么编码。

  · bootstrap 里 import 了第三方包 → 用户没装依赖时它自己先崩 →
    死循环：想装依赖的东西，需要依赖才能跑。

  · 把"用户主动关窗口"判成"程序崩溃" → 用户以为坏了。

这三条我都实际踩过（`.bat` 那条还踩了两次 —— 写了警告之后又犯）。
**光靠注释提醒是不够的，必须有东西拦住。**
"""

from __future__ import annotations

import ast
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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
# ① run.bat 必须是纯 ASCII
# =============================================================================


def test_bat_is_ascii() -> None:
    """⚠️ 这条规则我违反了两次，所以必须用测试钉死。

    为什么这么严格：
      cmd.exe 用**系统 ANSI 代码页**读 .bat 文件。
      中文 Windows 是 GBK。而 .bat 文件是 UTF-8。
      → cmd 把 UTF-8 字节按 GBK 解码 → 得到乱码
      → **乱码被当成命令执行** → 一个中文注释变成一堆"找不到命令"

      而且换编码解决不了：存成 GBK 就在英文 Windows 上坏。
      **没有任何一种编码在所有系统上都能用。**

      所以唯一的解法是 .bat 里一个非 ASCII 字符都不能有。

    ⚠️ 这条规则**只适用于 .bat**。

       踩过一次：一开始把 run.sh 也一起检查了，于是它因为带中文而"失败"。
       但 .sh 根本没有这个问题 —— Linux/macOS 的 shell 本来就是按 UTF-8
       处理脚本的，中文完全正常。

       **规则套错了地方，就会逼着人去做没必要的牺牲**
       （把 .sh 也改成英文）或者把测试关掉。两个都更糟。
    """
    section("① run.bat 是纯 ASCII（.sh 不受此限）")

    checks = [
        ("run.bat", True, "cmd.exe 按 ANSI 代码页读，中文会被当命令执行"),
        ("run.sh", False, "shell 按 UTF-8 处理，中文没问题"),
    ]

    for name, must_be_ascii, why in checks:
        path = ROOT / name
        if not path.exists():
            check(False, f"{name} 存在")
            continue

        data = path.read_bytes()
        bad = [(i, b) for i, b in enumerate(data) if b > 127]

        if not must_be_ascii:
            # 不要求 ASCII，但仍检查一条：不能有 CRLF
            # （.sh 带 CRLF 在 Linux 上会报 "bad interpreter"）
            check(
                b"\r\n" not in data,
                f"{name} 用的是 LF 换行（CRLF 会让 Linux 报 bad interpreter）",
            )
            continue

        if bad:
            print(f"      ⚠️ {name} 里有 {len(bad)} 个非 ASCII 字节：")
            for i, b in bad[:5]:
                lineno = data[:i].count(b"\n") + 1
                ctx = data[max(0, i - 20):i + 20].decode("utf-8", "replace")
                print(f"          第 {lineno} 行  0x{b:02x}  ...{ctx}...")
            print(f"      → {why}")
            print("      → 中文要放在 Python 里输出，不能写进 .bat")
        check(not bad, f"{name} 全部是 ASCII（{len(data)} 字节）")


# =============================================================================
# ② bootstrap 只能用标准库
# =============================================================================


def test_bootstrap_stdlib_only() -> None:
    """⚠️ 硬约束，不是风格偏好。

    bootstrap 的职责就是"把依赖装上"。
    如果它自己 `import requests` 才能告诉你"缺少 requests"，
    那就成了**死循环** —— 用户永远到不了能装依赖的那一步。

    判断方法：解析 AST，看它 import 了什么。
    只有标准库和本项目自己的模块是允许的。
    """
    section("② bootstrap 只用标准库")

    path = ROOT / "ginan_agent" / "bootstrap.py"
    if not path.exists():
        check(False, "bootstrap.py 存在")
        return

    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # 相对导入（本项目内部）
                continue
            if node.module:
                imported.add(node.module.split(".")[0])

    # 标准库里的名字 —— 用 sys.stdlib_module_names（Python 3.10+）
    #
    # ⚠️ 除了标准库，还要允许**我们自己的模块**：
    #    bootstrap 现在会 `from deps import DEPS`（那份唯一的依赖清单，
    #    见 ginan_agent/deps.py）。用脚本相对名导入是因为 run.bat 就是这么
    #    跑它的 —— `sys.path[0]` 是 `ginan_agent/`，不是项目根。
    #    判据和 ②b 用**同一个函数**，免得两处对"什么是自己人"各有一套说法。
    stdlib = set(sys.stdlib_module_names)
    allowed = stdlib | _own_top_level_names()

    third_party = sorted(imported - allowed)

    if third_party:
        print(f"      ⚠️ 引入了第三方包：{third_party}")
        print("      → 缺依赖时 bootstrap 自己就会崩，用户到不了装依赖那一步")
    check(
        not third_party,
        f"没有引入第三方包（引入了 {len(imported)} 个模块，全是标准库）",
    )


# =============================================================================
# ②b 依赖清单必须覆盖**代码里真的 import 的东西**
# =============================================================================


# 我们自己的顶层包 —— 不是第三方依赖。
#
# ⚠️ 除了包名，还要算上 `ginan_agent/` 下**直接躺着的模块**：
#    `bootstrap.py` 被 run.bat 当脚本跑时，`sys.path[0]` 是 `ginan_agent/`，
#    所以它是 `from deps import DEPS` 而不是 `from .deps import DEPS`。
#    不把这些名字算进来的话，守卫会把**我们自己的模块**报成缺依赖
#    —— 我第一版就是这么写的，它立刻把 `deps` 报了出来。
def _own_top_level_names() -> set[str]:
    names = {"ginan_agent", "tests"}
    names |= {p.stem for p in (ROOT / "ginan_agent").glob("*.py")}
    names |= {p.name for p in (ROOT / "ginan_agent").iterdir() if p.is_dir()}
    names |= {p.name for p in (ROOT / "tests").iterdir() if p.is_dir()}
    return names


def _third_party_imports() -> dict[str, str]:
    """扫全仓库，找出所有第三方包的**导入名**。

    返回 {导入名: "第一个出现的位置"}。

    ⚠️ 用 AST 而不是正则：注释和字符串里出现的 `import xxx` 不算，
       而正则分不清 —— 这个仓库注释特别多，正则会被自己的注释骗到
       （这条在别的守卫上已经踩过一次）。
    """
    import ast

    stdlib = set(sys.stdlib_module_names)
    ours = _own_top_level_names()
    found: dict[str, str] = {}

    for root in ("ginan_agent", "scripts", "tests"):
        for p in sorted((ROOT / root).rglob("*.py")):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level or not node.module:
                        continue          # 相对导入 = 我们自己的代码
                    names = [node.module]
                for full in names:
                    top = full.split(".")[0]
                    if top in stdlib or top in ours:
                        continue
                    found.setdefault(top, f"{p.relative_to(ROOT).as_posix()}:{node.lineno}")

    return found


def test_deps_cover_imports() -> None:
    """⚠️⚠️ 这条守的是**当初真的出过的事故**。

    ── 事故本身 ──
    产品代码实际 import 了 8 个第三方包，而依赖清单（当时在
    requirements.txt / bootstrap.py / environment.py **三处**各写一遍）
    都只有 5 个 —— `fastapi` / `uvicorn` / `sse_starlette` / `httpx`
    一个都没列。

    后果不是"少装一个可选包"：

        用户在干净机器上双击 run.bat
        → 启动器检查依赖 → 打印「全部就绪」
        → 主程序**第一行** ImportError: No module named 'fastapi'

    `server/app.py` 是在**模块顶层** import fastapi 的 ——
    那不是一个可选路径，是"跑起来的第一个动作"。

    ── 为什么原来的守卫拦不住 ──
    原来这一节（②）只断言 **bootstrap 自己没有 import 第三方包** ——
    那是**反方向**的一条规则，而且是它自己那一层。
    没有任何东西问过："清单里的东西，够不够跑起来？"

    ⚠️ 所以这条查的是**集合关系**，不是"清单里写了什么"：
          代码真的 import 的  ⊆  依赖清单
       并且反过来也查：清单里的每一项都真的有人 import。
       多一个的代价不是磁盘，是**误导**（`numpy` 就是被这条揪出来的：
       清单说它用来"解析 .POS 算 RMS"，而结果解析用的是标准库）。
    """
    section("②b ★★ 依赖清单要覆盖**代码里真的 import 的包**")

    from ginan_agent.deps import DEPS

    by_import = {mod: pkg for pkg, mod in DEPS}
    actual = _third_party_imports()

    missing = sorted(m for m in actual if m not in by_import)
    if missing:
        print(f"      ⚠️ 这些包被 import 了，但清单里没有：")
        for m in missing:
            print(f"           {m:16s} {actual[m]}")
        print("      → 干净机器上装完清单里的东西**还是跑不起来**")
        print("      → 补进 ginan_agent/deps.py（并同步 requirements.txt）")
    check(
        not missing,
        f"★ 代码里 import 的第三方包**全在清单里**（扫到 "
        f"{len(actual)} 个导入名 / 清单 {len(DEPS)} 项）",
    )

    unused = sorted(m for m in by_import if m not in actual)
    if unused:
        print(f"      ⚠️ 清单里有、但全仓库没有一处 import：{unused}")
        print("      → 留着的代价是误导下一个读清单的人")
    check(not unused, "★ 清单里也没有**没人用**的项（多一个也是错）")

    # ---- requirements.txt 和 deps.py 必须说同一件事 ----
    #
    # ⚠️ 这两份**必须**一致，但它们是给不同东西看的：
    #    deps.py 给启动器和守卫读，requirements.txt 给 pip 读。
    #    所以要有一条断言盯着它们别分叉。
    req_path = ROOT / "requirements.txt"
    if not req_path.exists():
        check(False, "requirements.txt 存在")
        return

    listed: set[str] = set()
    for line in req_path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()      # 丢掉注释
        if not line:
            continue
        for sep in (">=", "==", "<=", "~=", ">", "<", "["):
            line = line.split(sep, 1)[0]
        listed.add(line.strip().lower())

    not_listed = sorted(p for p in by_import.values() if p.lower() not in listed)
    if not_listed:
        print(f"      ⚠️ deps.py 有、requirements.txt 里没有（或被注释掉了）：{not_listed}")
    check(
        not not_listed,
        f"★ requirements.txt 和 deps.py 说的是同一份清单"
        f"（{len(listed)} 行有效依赖）",
    )


# =============================================================================
# ③ 用户主动退出不能被当成出错
# =============================================================================


def test_user_quit_is_not_error() -> None:
    """⚠️ 「正常操作被报成错误」比不报错还糟 —— 它让用户失去信任。

    踩过一次：用户关掉窗口，启动器显示一大块「✗ 程序异常退出」。
    可他就是正常退出的。

    ⚠️ 这个测试要连**大的负数**也覆盖 ——
       Windows 上关窗口的退出码在不同情况下有无符号两种表示。
    """
    section("③ 用户主动退出被识别为正常")

    from ginan_agent.bootstrap import _is_user_quit

    cases = [
        # (退出码, 期望, 说明)
        (0, True, "正常返回"),
        (0xC000013A, True, "STATUS_CONTROL_C_EXIT（关窗口）"),
        (0xC000013B, True, "STATUS_CONTROL_BREAK"),
        (-2, True, "KeyboardInterrupt"),
        (130, True, "128+SIGINT（常见于 shell）"),
        (1, False, "一般错误"),
        (2, False, "一般错误"),
    ]

    for rc, expect, label in cases:
        check(
            _is_user_quit(rc) is expect,
            f"退出码 {rc}（{label}）→ {'正常' if expect else '异常'}",
        )

    # ⚠️ 有符号形式也要能认出来 —— 同一串数字的另一半表示
    signed = -0x3FFFFEC6            # 0xC000013A 的补码形式
    check(_is_user_quit(signed), f"有符号形式 {signed} 也被认出来")


# =============================================================================
# ④ 启动路径上没有硬编码
# =============================================================================


def test_launcher_paths() -> None:
    section("④ 启动器的路径不依赖当前目录")

    bat = (ROOT / "run.bat").read_text(encoding="utf-8", errors="replace")

    # ⚠️ `cd /d "%~dp0"` 是让"双击"能工作的关键。
    #    双击时的工作目录是**不确定的**（通常还是用户上次的目录），
    #    不切过来就会在错误的地方找文件。
    check('cd /d "%~dp0"' in bat, 'run.bat 会切到脚本所在目录（cd /d "%~dp0"）')

    check(
        "ginan_agent\\bootstrap.py" in bat or "ginan_agent/bootstrap.py" in bat,
        "run.bat 调用的是 bootstrap.py（而不是直接跑主程序）",
    )

    # bootstrap 里应该用 __file__ 定位项目根，而不是 cwd
    bs = (ROOT / "ginan_agent" / "bootstrap.py").read_text(encoding="utf-8")
    check("__file__" in bs, "bootstrap.py 用 __file__ 定位项目根（不依赖 CWD）")
    check(
        "Path.cwd()" not in bs,
        "bootstrap.py 没有用 Path.cwd()（那取决于用户在哪双击）",
    )


# =============================================================================
# ⑤ 按 Ctrl+C 要安静
# =============================================================================


def test_quiet_shutdown_filter() -> None:
    section("⑤a ★ 退出时的假 ERROR 要滤掉 —— 但**真错误不能滤**")

    # ⚠️⚠️ 用户的原话（他贴了一屏红色堆栈）：
    #
    #        ERROR:    Cancel 1 running task(s), timeout graceful shutdown exceeded
    #        ERROR:    Exception in ASGI application
    #        Traceback ... 几十行 ...
    #        asyncio.exceptions.CancelledError
    #
    #    **那看着就是程序崩了**，而它是一次完全正常的退出。
    #
    #    成因：退出时正好有个**慢请求**在跑（比如"探测数据源"要挨个连
    #    11 个源，最坏几十秒）。优雅退出等不了 → 把它取消 →
    #    而 uvicorn 把"被取消"**当成异常记成 ERROR、还打完整堆栈**。
    #
    #    ⚠️ 可"被取消"不是故障，是**用户按了退出**的必然结果。
    #       把它报成 ERROR 是"**把正常操作说成错误**" ——
    #       这个项目在 `bootstrap._is_user_quit()` 那里已经踩过一次
    #       （关窗口被报成"程序异常退出"）。
    import asyncio
    import logging

    from ginan_agent import __main__ as m

    f = m._QuietShutdown()

    def rec(msg: str, exc_info=None) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.error", logging.ERROR, "", 0,
                                 msg, None, exc_info)

    check(
        not f.filter(rec("Cancel 1 running task(s), "
                         "timeout graceful shutdown exceeded")),
        "★ 「Cancel N running task(s)…」被丢掉",
    )

    try:
        raise asyncio.CancelledError("取消")
    except asyncio.CancelledError:
        ei = sys.exc_info()
    check(
        not f.filter(rec("Exception in ASGI application\n", ei)),
        "★ CancelledError 那条堆栈被丢掉",
    )

    # ⚠️⚠️ **反过来的那一条才是关键** ——
    #     过滤器写宽了会把**真错误**一起藏起来，那比一屏红字糟得多：
    #     用户再也看不到真正的故障了。
    try:
        raise ValueError("这是真错误")
    except ValueError:
        ei2 = sys.exc_info()
    check(
        f.filter(rec("Exception in ASGI application\n", ei2)),
        "★★ **别的异常照常放行**（写宽了会把真故障也藏掉）",
    )
    check(
        f.filter(rec("启动失败：端口被占用")),
        "普通的错误日志照常放行",
    )

    # 判据看的是**异常类型**，不是消息文案 —— 文案会随 uvicorn 版本变
    src = __import__("inspect").getsource(m._QuietShutdown.filter)
    check(
        "exc_info" in src and "CancelledError" in src,
        "★ 判据是 exc_info 里的**异常类型**（文案会随 uvicorn 版本变）",
    )


def test_capture_signals_is_disabled() -> None:
    section("⑤b ★ 不让 uvicorn 接管信号（它会退出前再抛一次）")

    # ⚠️ uvicorn 的 `capture_signals()` 除了设 `should_exit`（对的），
    #    还会在退出前 `signal.raise_signal()` **把信号再抛一次** ——
    #    那个 SIGINT 是在**事件循环里**抛的，会取消正在跑的请求。
    #
    # ⚠️ 但它**只是半个原因**：实测光去掉它，堆栈照打
    #    （另一半是那个日志过滤器）。两条都要。
    import contextlib

    from ginan_agent import __main__ as m
    from ginan_agent.server.app import create_app

    server = m.make_server(create_app(), 8899)
    check(
        server.capture_signals is contextlib.nullcontext,
        "★ make_server 把 capture_signals 换成了空上下文",
    )
    check(
        server.config.timeout_graceful_shutdown is not None,
        "★ 而且设了退出超时（不然 `/api/events` 那条长连接会一直等）",
    )

    # 自己接的信号 handler：第一次设 should_exit，第二次 force_exit
    class Fake:
        should_exit = False
        force_exit = False

    fake = Fake()
    m.install_quit_handler(fake)
    handler = signal.getsignal(signal.SIGINT)
    check(callable(handler), "★ 装上了自己的 SIGINT handler")

    handler(signal.SIGINT, None)
    check(fake.should_exit and not fake.force_exit,
          "第一次 Ctrl+C → 优雅退出")
    handler(signal.SIGINT, None)
    check(fake.force_exit,
          "★ 第二次 Ctrl+C → 立刻退（有些连接不肯关，得留条强硬的路）")


def test_ctrl_c_leaves_no_traceback() -> None:
    section("⑤c ★★ 端到端：退出时**一个字符都不该打到 stderr**")

    # ⚠️⚠️ 这条是行为的，不是查源码的 —— 上面 ⑤a/⑤b 查的是机制，
    #     这一条查的是**产物**：真起一个服务、真发一个慢请求、
    #     真走一遍退出流程，然后看 stderr。
    #
    #     ⚠️ 为什么必须这样：这一节的结论是拿"旧写法 vs 新写法"各跑一遍
    #        比出来的 —— 旧的那边 **4677 个字符的堆栈**，新的这边 **0**。
    #        只看"代码里有没有那个过滤器"是得不出这个结论的。
    #
    #     ⚠️ 它依赖"探测数据源"这个请求够慢（实测最慢的源要 12 秒才超时），
    #        所以在 4 秒后退出时它一定还在跑。
    #        网络特别快的话这条测的会少一些，但**不会误报失败**。
    import subprocess
    import tempfile

    child = r'''
import contextlib, os, sys, threading, time, urllib.request
sys.path.insert(0, r"{root}")
import uvicorn
from ginan_agent.server.app import create_app
from ginan_agent import __main__ as m

fd = os.open(r"{log}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
os.dup2(fd, 2); os.dup2(fd, 1)          # fd 级重定向，logging 也跑不掉

app = create_app()
server = m.make_server(app, {port})      # ← 产品代码那条路
m.install_quit_handler(server)
ready = threading.Event()

def probe():
    ready.wait(6)
    try:
        with urllib.request.urlopen("http://127.0.0.1:{port}/", timeout=5) as r:
            html = r.read().decode("utf-8", "replace")
        tok = html.split('ginan-token" content="')[1].split('"')[0]
        req = urllib.request.Request("http://127.0.0.1:{port}/api/probe",
                                     headers={{"X-Token": tok}})
        urllib.request.urlopen(req, timeout=60).read()
    except Exception:
        pass

def stop():
    ready.set()
    time.sleep(4)                        # 让 probe 真的跑起来
    server.should_exit = True            # = 用户按 Ctrl+C 之后走的那条路

threading.Thread(target=probe, daemon=True).start()
threading.Thread(target=stop, daemon=True).start()
try:
    server.run()
except KeyboardInterrupt:
    pass
'''

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "stderr.txt"
        script = Path(td) / "child.py"
        script.write_text(
            child.format(root=ROOT, log=log, port=8897), encoding="utf-8"
        )
        try:
            subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                           capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            check(False, "子进程超时（自己没能退出？）")
            return

        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""

    bad = [w for w in ("Traceback", "KeyboardInterrupt", "CancelledError",
                       "ERROR") if w in text]
    for w in bad:
        print(f"      ⚠️ stderr 里出现了 {w}")
    check(
        not bad,
        f"★★ 退出时 stderr 是干净的（实测 {len(text)} 字符）",
    )


# =============================================================================
# 主程序
# =============================================================================


def main() -> int:
    print("=" * 60)
    print("启动器自检 —— 「双击就能跑」这条路")
    print("=" * 60)
    print("⚠️ 这个模块的失败方式都是静默的，所以需要测试拦住。")

    test_bat_is_ascii()
    test_bootstrap_stdlib_only()
    test_deps_cover_imports()
    test_user_quit_is_not_error()
    test_launcher_paths()
    test_quiet_shutdown_filter()
    test_capture_signals_is_disabled()
    test_ctrl_c_leaves_no_traceback()

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
