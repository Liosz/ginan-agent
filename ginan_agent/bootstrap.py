"""启动引导 —— 用户双击 run.bat 之后跑的就是这个。

============================ 它解决什么问题 ============================
让用户**双击就能跑，不用敲任何命令**。

为此它要在用户看到界面之前把这三件事办完：
    ① 确认 Python 版本够
    ② 确认依赖装了 —— 没装就自动装
    ③ 启动应用

============================ ⚠️ 只用标准库 ============================
这一条是硬约束，不是风格偏好：

    **它的职责就是"把依赖装上"，所以它自己不能依赖任何第三方包。**

如果 bootstrap 需要 `import requests` 才能告诉你"缺少 requests"，
那就成了一个死循环 —— 用户永远到不了能装依赖的那一步。

============================ ⚠️ 中文为什么在这里而不在 .bat 里 ============================
因为 `cmd.exe` 用系统 ANSI 代码页读 `.bat` 文件（中文 Windows 是 GBK），
而 Python 源码是 UTF-8 —— 中文注释会被错解成命令，整个脚本直接死掉。

而且**换编码也解决不了**：存成 GBK 就在非中文的 Windows 上坏。
没有一种编码是到处都能用的。

所以：`.bat` 保持纯 ASCII，**所有中文由 Python 输出**。
Python 处理 UTF-8 没有问题。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# =============================================================================
# 依赖清单
# =============================================================================
#
# ⚠️⚠️ **只有一份，在 `ginan_agent/deps.py`。**
#
#    这里原来**又写了一遍**（当时是三处各一份：requirements.txt /
#    这个文件 / ginan/environment.py），而三份都漏了 fastapi、uvicorn、
#    sse_starlette、httpx —— 于是"双击就能跑"在干净机器上必然崩。
#    详见 `deps.py` 的文件头。
#
# ⚠️ 两种导入方式都要能走通，这不是洁癖：
#      · `run.bat` 是 `python ginan_agent\bootstrap.py` 直接跑的 ——
#        那时 `sys.path[0]` 是 `ginan_agent/` 本身，只能 `from deps import`
#      · 而 `scripts/check_launcher.py` 是 `from ginan_agent.bootstrap import` ——
#        那时只能 `from .deps import`
#    两种都是真实用法，所以两种都留着。
try:                                    # 作为包的一部分被导入时
    from .deps import DEPS
except ImportError:                     # 被当成脚本直接跑时（run.bat 那条）
    from deps import DEPS  # type: ignore[no-redef]

MIN_PYTHON = (3, 10)


# =============================================================================
# 输出
# =============================================================================


def _setup_stdout() -> None:
    """让中文能在 Windows 控制台正常显示。

    ⚠️ Windows 控制台默认不是 UTF-8，直接 print 中文会抛
       UnicodeEncodeError 或者显示成乱码。

       （这里踩过一次：在自检脚本里 print 带 ✓ 的字符串，
         报 'gbk' codec can't encode character '\\u2713'。）
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def title(text: str) -> None:
    say()
    say("  " + "=" * 58)
    say(f"   {text}")
    say("  " + "=" * 58)
    say()


def step(n: int, total: int, text: str) -> None:
    say(f"  [{n}/{total}] {text}")


def ok(text: str) -> None:
    say(f"        ✓ {text}")


def warn(text: str) -> None:
    say(f"        ! {text}")


def fail_block(title_text: str, lines: list[str]) -> None:
    """打印一个"出错了"的方块。

    ⚠️ 每一条错误信息都必须包含**用户能做的下一步**。
      只说"失败了"等于没说 —— 用户不知道该怎么办，
       只能来问你，而你可能几个月后才看到消息。
    """
    say()
    say("  " + "=" * 58)
    say(f"   ✗ {title_text}")
    say("  " + "=" * 58)
    say()
    for line in lines:
        say(f"    {line}")
    say()


# =============================================================================
# 检查
# =============================================================================


def check_python() -> bool:
    v = sys.version_info
    if v >= MIN_PYTHON:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True

    fail_block("Python 版本太低", [
        f"当前版本：{v.major}.{v.minor}.{v.micro}",
        f"需要版本：{MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高",
        "",
        "这个程序用了一些较新的 Python 特性，旧版本跑不了。",
        "",
        "怎么升级：",
        "  1. 到 https://www.python.org/downloads/ 下载新版",
        "  2. 安装时勾选「Add Python to PATH」",
        "  3. 重新双击 run.bat",
    ])
    return False


def check_data_dir(root: Path) -> bool:
    """确认软件目录下能写数据。

    ⚠️ 为什么要在启动时**提前**查，而不是等真正要写的时候再报错？

       因为第一次写数据可能发生在"下载几百 MB 到一半"的时候 ——
       那时候失败，用户已经等了很久，而且报错点离真正的根因很远。

       **能提前知道的失败，就不要留给用户去撞。**

    ⚠️ 为什么这里**自己拼路径**，不 import `ginan_agent.core.session`？

       因为 bootstrap 是**只用标准库**的（见文件头那条硬约束）——
       而 import 那个包会连带把 openai 等第三方依赖拉进来。
       在"依赖还没装"的时刻 import 一个需要依赖的模块，就是死循环。

       代价是 `userdata` 这个名字在这里重复了一次。
       **这个重复是有意的**：宁可重复一个字符串，
       也不能破坏"启动器不依赖任何第三方包"这条硬约束。

       （自检里有一条守着这两个名字一致，改一处漏一处会被测出来。）
    """
    target = root / "userdata"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".writable"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        fail_block("软件目录不可写，无法保存数据", [
            f"数据目录：{target}",
            f"系统给的原因：{exc}",
            "",
            "这个程序会把**所有**数据都放在自己的文件夹里 ——",
            "包括你填的 AI 密钥、下载的观测数据、对话历史。",
            "所以这个文件夹必须是可写的。",
            "",
            "怎么办（任选一个）：",
            "  1. 把整个软件文件夹挪到你有写权限的地方",
            "     （比如「文档」或桌面，别放在 C:\\Program Files 下面）",
            "  2. 右键软件文件夹 → 属性 → 取消勾选「只读」",
            "  3. 如果它在 U 盘或网络盘上，确认没被写保护",
        ])
        return False

    ok("数据目录可写")
    return True


def missing_deps() -> list[str]:
    """哪些依赖没装。返回的是**包名**（给 pip 用的）。"""
    missing = []
    for pkg, mod in DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def install(packages: list[str], root: Path) -> bool:
    """装依赖。成功返回 True。

    ⚠️ 优先用 requirements.txt 而不是自己拼包名 ——
       requirements.txt 是**唯一的内容源**，将来加了依赖只需改那一处。

       （注意：requirements.txt 里有注释掉的可选依赖，
         所以这里只在"全部缺失"时用整份文件；
         部分缺失时精确装缺的那几个，避免把可选的也装上。）
    """
    req = root / "requirements.txt"
    all_missing = len(packages) == len(DEPS)

    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check",
        "-r", str(req),
    ] if (all_missing and req.is_file()) else [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", *packages,
    ]

    say()
    say(f"        正在安装 {'全部依赖' if all_missing else '、'.join(packages)} ...")
    say("        （第一次运行需要下载，约 1-2 分钟）")
    say()

    try:
        # ⚠️ 不捕获输出，让它直接流到控制台。
        #    因为下载要一两分钟，用户需要看到"正在动" ——
        #    如果这里静默等待，用户会以为卡死了然后关掉窗口。
        result = subprocess.run(cmd, cwd=str(root))
    except OSError as exc:
        fail_block("无法启动 pip", [
            f"系统给的原因：{exc}",
            "",
            "可能是 Python 安装不完整（缺少 pip 模块）。",
            "试试重新运行 Python 安装程序，选 Modify 然后确保勾上 pip。",
        ])
        return False

    if result.returncode != 0:
        fail_block("依赖安装失败", [
            "最常见的原因是网络问题（下载不到软件包）。",
            "",
            "可以试试：",
            "  1. 检查网络连接",
            "  2. 如果平时用代理上网，先打开代理再重新双击",
            "  3. 手动安装：在这个文件夹里打开命令行，运行",
            "         pip install -r requirements.txt",
            "",
            "如果还是不行，把上面的错误信息发给开发者。",
        ])
        return False

    still = missing_deps()
    if still:
        fail_block("安装了但导入仍然失败", [
            f"仍然找不到：{'、'.join(still)}",
            "",
            "这种情况比较少见，通常是因为：",
            "  · 系统里有多个 Python，pip 装到了另一个里面",
            "  · 或者安装过程被中断了",
            "",
            "试试手动安装看看完整报错：",
            f"    {sys.executable} -m pip install {' '.join(still)}",
        ])
        return False

    return True


# =============================================================================
# 启动
# =============================================================================


# 用户主动终止进程时，Windows 给的退出码。
#
# ⚠️ 这两个数看着像乱码，但它们**是有名字的正常状态**，
#    不是错误。必须识别出来，否则"用户关掉窗口"会被报成"程序崩溃"。
#
#    踩过一次：用户关掉窗口，启动器显示一大块
#    "✗ 程序异常退出" —— 用户会以为程序坏了，
#    而实际上他就是正常退出的。
#
#    > **正常操作被报成错误，比不报错还糟** ——
#    > 它会让用户对程序的可靠性失去信任。
CTRL_C_EXIT = 0xC000013A          # STATUS_CONTROL_C_EXIT（Ctrl+C 或关窗口）
CTRL_BREAK_EXIT = 0xC000013B      # STATUS_CONTROL_BREAK


def _is_user_quit(rc: int) -> bool:
    """这个退出码是不是"用户主动退出"而不是"程序出错"。

    ⚠️ 判断要宽一点：Windows 上关窗口的退出码在不同情况下不完全一样，
       可能是有符号/无符号的同一串数字。
       宁可把少数真错误当成正常退出（用户看到平静的"已退出"），
       也不要把正常退出报成崩溃（用户以为坏了）。
    """
    unsigned = rc & 0xFFFFFFFF if rc < 0 else rc
    return unsigned in (CTRL_C_EXIT, CTRL_BREAK_EXIT) or rc in (0, 130, -2, -15)


def launch(root: Path, argv: list[str]) -> int:
    """启动应用。

    ⚠️ 用 `-m ginan_agent` 而不是直接调 main() ——
       这样和用户手动敲命令的行为**完全一致**，
       不会出现"双击和命令行跑出不同结果"的诡异问题。
    """
    say("  " + "=" * 58)
    say("   启动中……")
    say()
    say("   服务起来后浏览器会自动打开。")
    say("   关掉这个窗口就是退出程序。")
    say("  " + "=" * 58)
    say()

    try:
        # ⚠️ `-u` = 不要缓冲输出。
        #
        #    不加的话，Python 在**输出不是终端**时会把 stdout 攒在缓冲区里
        #    （比如输出被重定向到文件、或被上层程序捕获）。
        #    表现是：应用其实已经启动了，但用户看不见任何提示，
        #    等几秒没反应就把窗口关掉了 —— 然后以为程序坏了。
        #
        #    加 -u 的代价可以忽略，换来的是"启动过程始终可见"。
        rc = subprocess.call(
            [sys.executable, "-u", "-m", "ginan_agent", *argv],
            cwd=str(root),
        )
    except KeyboardInterrupt:
        # Ctrl+C 打在父进程上（子进程也会收到，但我们先捕到了）
        rc = 0

    if _is_user_quit(rc):
        # ⚠️ 正常退出要**明确说出来**，而不是什么都不显示。
        #    否则用户看到窗口一闪而过，不确定是"正常结束"还是"崩了"。
        say()
        say("  已退出。")
        say()
        return 0

    return rc


# =============================================================================
# 主入口
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    argv = list(argv if argv is not None else sys.argv[1:])
    root = Path(__file__).resolve().parent.parent

    title("Ginan Agent")
    say("   AI 驱动的 GNSS 精密定位工作台")
    say()

    # ---- ① Python 版本 ----
    step(1, 4, "检查 Python")
    if not check_python():
        return 1

    # ---- ② 数据目录 ----
    # ⚠️ 放在装依赖**之前** —— 它是几毫秒的事，而装依赖要一两分钟。
    #    能立刻发现的问题，不该让用户等两分钟才看到。
    step(2, 4, "检查数据目录")
    if not check_data_dir(root):
        return 1

    # ---- ③ 依赖 ----
    step(3, 4, "检查运行依赖")
    missing = missing_deps()
    if missing:
        warn(f"缺少 {len(missing)} 个：{'、'.join(missing)}")
        if not install(missing, root):
            return 1
        ok("全部就绪")
    else:
        ok("全部就绪")

    # ---- ④ 启动 ----
    step(4, 4, "启动")
    rc = launch(root, argv)

    if rc != 0:
        fail_block("程序异常退出", [
            "上面应该有错误信息。如果看不懂：",
            "",
            "  1. 先跑一次环境自检（同样只输出信息，不改任何东西）：",
            f"         {sys.executable} scripts\\check_all.py",
            "  2. 把输出发给开发者",
        ])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
