"""自检：路径解析 —— 必须按用户解析，不能硬编码。

============================ 为什么需要这个测试 ============================
开发机上路径长这样：

    C:\\Users\\<某个用户名>\\AppData\\Local\\GinanAgent\\

而别人的机器上是：

    C:\\Users\\张三\\AppData\\Local\\GinanAgent\\
    C:\\Users\\wangwu\\AppData\\Local\\GinanAgent\\
    /home/lisi/.local/share/ginan-agent/

⚠️ **开发机上跑得通，什么都证明不了。**
   硬编码成 `C:\\Users\\<开发机的用户名>\\...` 在开发机上**完全正常** ——
   因为那台机器上那个路径确实存在。
   问题只在别人机器上才暴露，而那时候项目可能已经发出去了。

所以这个测试要主动**假装换个用户**，看路径跟不跟着变。

============================ 两类检查 ============================
  ① 行为检查：改 LOCALAPPDATA / 改平台 → 路径要跟着变
  ② 静态检查：全仓库扫一遍，确保没有硬编码的绝对路径

第 ② 条是在防**将来的人**（包括三个月后的我）图省事写死一个路径。
"""

from __future__ import annotations

import importlib
import os
import re
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
# ① 行为：换用户 → 路径要变
# =============================================================================


def _reload_session():
    """重新导入 core.session，让环境变量的改动生效。

    ⚠️ 因为 default_app_root() 是在**调用时**读环境变量的，
    所以其实不重载也行 —— 但重载一次能保证我们测的是"新起的进程会怎样"，
    更接近真实情况。
    """
    import ginan_agent.core.session as S

    importlib.reload(S)
    return S


def test_single_location() -> None:
    section("① 数据目录只有一个位置：软件目录下的 userdata/")

    # ⚠️⚠️ 这一节改过两次，值得把来龙去脉记下来 ——
    #    因为它连着推翻了两版设计。
    #
    #    第一版：数据放系统用户目录（%LOCALAPPDATA%\GinanAgent）
    #            理由：项目目录可能只读、怕几百 MB 的东西被提交进 git
    #
    #    第二版：三层优先级（环境变量 → 软件目录 → 系统目录）
    #            理由：用户说"我的东西散在系统各处，找不到"
    #
    #    第三版（现在）：只有软件目录下的 userdata/
    #            理由：用户把要求说得更直白了 ——
    #              "这个软件真的只是把所有的东西都存到自己软件的安装目录下"
    #
    #    ⚠️ 第二版错在把"用户要什么"理解成了"给用户一堆选择"。
    #       用户要的是**一个确定的地方**，不是一套优先级规则。
    #       多出来的环境变量和退路，对他来说只是"有时候在 A、有时候在 B" ——
    #       而那正是他要摆脱的困惑。
    #
    #    > 改设计时断言要跟着改，但**更要紧的是搞清楚为什么改**，
    #    > 否则下一版还会绕着同一个弯子转。
    S = _reload_session()
    project = ROOT.resolve()
    root = S.default_app_root().resolve()

    check(
        root == project / S.DATA_DIR_NAME,
        f"数据目录 = 软件目录下的 {S.DATA_DIR_NAME}/",
    )

    # ⚠️ 环境变量**不再**能改它。
    #    这条是防"退路偷偷长回来" —— 将来任何人想加回
    #    "可以用环境变量指定数据目录"，这条会立刻红。
    original = os.environ.get("GINAN_AGENT_HOME")
    try:
        os.environ["GINAN_AGENT_HOME"] = str(ROOT / ".." / "别的地方")
        S = _reload_session()
        got = S.default_app_root().resolve()
        check(
            got == project / S.DATA_DIR_NAME,
            "★ 环境变量改不了它（已经没有'退路'这种东西了）",
        )
    finally:
        if original is None:
            os.environ.pop("GINAN_AGENT_HOME", None)
        else:
            os.environ["GINAN_AGENT_HOME"] = original
        _reload_session()

    # ⚠️ 连**词**都不该再出现 —— 留着旧概念的名字，下一个人会以为它还有用。
    src = (ROOT / "ginan_agent" / "core" / "session.py").read_text(encoding="utf-8")
    for word in ("PORTABLE", "GINAN_AGENT_HOME", "_system_root", "LOCALAPPDATA"):
        check(word not in src, f"session.py 里不再出现 {word}")


def test_unwritable_fails_loudly() -> None:
    section("①b 软件目录写不了 → 明确报错，**绝不偷偷换地方存**")

    # ⚠️⚠️ 这是"别把自己弄坏了"那条要求的核心断言。
    #
    #    用户的原话："只是让软件操作的时候别把自己弄坏了"。
    #
    #    最容易犯的错**不是崩溃，而是静默换一个地方存**：
    #    用户以为数据在软件目录里，实际在别处 ——
    #    等他要备份、要整个搬走的时候，才发现那里是空的。
    #    **静默换地方比直接报错糟得多。**
    S = _reload_session()
    project = ROOT.resolve()

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        fake_project = Path(td) / "ginan-agent"
        fake_project.mkdir()

        # ⚠️ 造"写不了"的方式：用一个**同名普通文件**占住目录位置。
        #    为什么不用权限位？因为 Windows 上 chmod 基本不起作用，
        #    而"目录位置被文件占住"在任何平台都必然失败 ——
        #    测试要造的是**必然**，不是"通常"。
        (fake_project / S.DATA_DIR_NAME).write_text("我是文件", encoding="utf-8")

        original_fn = S.default_app_root
        try:
            S.default_app_root = lambda: fake_project / S.DATA_DIR_NAME

            raised = None
            try:
                S.ensure_app_root()
            except S.AppRootNotWritable as exc:
                raised = exc

            check(raised is not None, "★ 写不了时抛出 AppRootNotWritable")

            msg = str(raised) if raised else ""
            check("不可写" in msg, "错误信息说清了是什么问题")
            check(
                str(fake_project / S.DATA_DIR_NAME) in msg,
                "错误信息里有具体路径（用户得知道是哪个文件夹）",
            )
            check(
                "怎么办" in msg,
                "★ 错误信息给了**下一步怎么做**（只说'失败'等于没说）",
            )
        finally:
            S.default_app_root = original_fn

    # ⚠️ 顺带守住"写不了时**没有**别的去处"：
    #    如果将来有人加回退路，上面那条会仍然绿（因为它是直接调
    #    ensure_app_root），但这条会红 —— 它检查的是**源码里没有退路**。
    check(
        not hasattr(S, "_system_root"),
        "★ 没有 _system_root 这样的退路函数",
    )
    check(
        not hasattr(S, "app_root_source"),
        "没有'来源说明'了 —— 只有一个来源，不需要说明是哪个",
    )


def test_data_dir_is_safe() -> None:
    section("③ 运行时数据在项目里 → 必须被 gitignore")

    # ⚠️⚠️ 这条断言**改过**，因为设计变了，值得记下来。
    #
    #    原来断言的是："数据目录不能在项目里"。
    #    理由：下载的几百 MB 会被 git 看见。
    #
    #    但用户提了一个更重要的点：
    #
    #        "我的东西散在系统各处，找不到。"
    #
    #    对实际使用来说，`%LOCALAPPDATA%\\GinanAgent` 是个要专门去记的
    #    路径，而"软件文件夹里那个 userdata"一眼就能看到。
    #    **能找到，比"理论上更规范"重要。**
    #
    #    ============================ 不变量也跟着变了 ============================
    #    从"不在项目里" 变成 **"在项目里的话，必须被 gitignore"**。
    #
    #    这才是真正要守住的东西 —— **位置是设计选择，不进版本库是硬要求。**
    #
    #    > 改设计时别只改代码，**也要回头看看哪条断言表达了旧设计的意图**。
    #    > 否则测试会红，而你会以为代码坏了。
    S = _reload_session()
    root = S.default_app_root().resolve()
    project = ROOT.resolve()

    check(
        root == project / S.DATA_DIR_NAME,
        f"数据目录就在项目内（{root.relative_to(project)}）—— 这是刻意的",
    )

    gitignore = ROOT / ".gitignore"
    check(gitignore.is_file(), ".gitignore 存在")

    text = gitignore.read_text(encoding="utf-8")
    rel = root.relative_to(project).as_posix()

    # ⚠️ gitignore 里的路径是**相对仓库根**的，所以要按相对路径找。
    #    写成绝对路径的话永远匹配不上。
    check(
        f"{rel}/" in text or f"/{rel}/" in text or rel in text,
        f"★ {rel}/ 在 .gitignore 里（否则几百 MB 数据 + API 密钥会被提交！）",
    )

    # ⚠️ 顺手确认 git 真的认为它被忽略了 —— 光有那行字不算数，
    #    路径的写法可能不匹配（gitignore 的规则很微妙）。
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "check-ignore", "-q", str(root)],
            cwd=str(ROOT), capture_output=True, timeout=10,
        )
        check(
            proc.returncode == 0,
            "★ git 确实认为它被忽略了（不是只有那一行字）",
        )
    except Exception:  # noqa: BLE001
        check(True, "（跳过 git check-ignore —— 不是 git 仓库或没有 git）")


def test_bootstrap_dir_name_matches() -> None:
    section("③b 启动器里的目录名和代码里的一致")

    # ⚠️ bootstrap.py 是**只用标准库**的，所以它没法 import core.session
    #    去拿 DATA_DIR_NAME —— 只能自己把字符串再写一遍。
    #
    #    那个重复是**有意的**（见 bootstrap.check_data_dir 的说明）：
    #    宁可重复一个字符串，也不能让启动器依赖第三方包。
    #
    #    ⚠️ 但重复就有不一致的风险：改了名字只改一处，启动器会去检查
    #       一个**根本不存在**的目录 —— 而且它还会"成功"（mkdir 会建出来）。
    #       那才是最难查的：两个 userdata 目录，程序只认其中一个。
    #
    #    所以这条断言把两边对上。
    S = _reload_session()
    src = (ROOT / "ginan_agent" / "bootstrap.py").read_text(encoding="utf-8")

    check(
        f'"{S.DATA_DIR_NAME}"' in src,
        f"★ bootstrap.py 里的目录名和 DATA_DIR_NAME 一致（{S.DATA_DIR_NAME}）",
    )


# =============================================================================
# ② 静态：全仓库扫硬编码路径
# =============================================================================


# 匹配形如 C:\Users\xxx 或 /home/xxx 的字面量
HARDCODED = re.compile(
    r"""["'](?:[A-Za-z]:[\\/](?:Users|Documents|Desktop)|/home/[a-z_][\w.-]*/|/Users/[A-Za-z])""",
)

# 扫描范围：自己的代码和文档
SCAN_SUFFIXES = {".py", ".js", ".mjs", ".html", ".css", ".md"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "build", "dist"}


def test_no_hardcoded_paths() -> None:
    section("④ 全仓库扫描：没有硬编码的用户路径")

    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            # 允许测试文件里出现（它们本来就要造假的路径来做断言）
            if path.name == "check_paths.py":
                continue
            if HARDCODED.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}  {line.strip()[:70]}")

    if hits:
        for h in hits:
            print(f"      {h}")
    check(
        not hits,
        f"没有硬编码的用户路径（扫了 {len(SCAN_SUFFIXES)} 类文件）",
    )


def test_docs_use_placeholders() -> None:
    section("⑤ 文档里的路径示例要用占位符")

    # ⚠️ 文档里的"示例路径"如果长得太真实（比如带开发者的盘符和目录名），
    #    读者会以为那就是**固定**的，照着抄然后发现跑不通。
    #    用 <你的...> 或 ... 这种明显的占位符，意图才清楚。
    doc = ROOT / "docs" / "ginan-facts.md"
    if not doc.exists():
        check(True, "（文档不存在，跳过）")
        return

    text = doc.read_text(encoding="utf-8")

    # ⚠️ 正则必须排除 URL。
    #
    #    踩过一次：原来写的是 `[A-Za-z]:[\\/]...`，
    #    结果 `https://igs.bkg.bund.de/...` 里的 `s://` 被当成了盘符 ——
    #    于是文档里 6 个正常的网址全被报成"硬编码路径"。
    #
    #    那个负向后顾 `(?<![A-Za-z])` 是关键：
    #    它要求冒号前面**不是字母**。
    #    `https:` 的冒号前面是 `p`（字母）→ 排除 ✓
    #    `E:/Code` 的冒号前面是行首或空格 → 保留 ✓
    suspicious = re.findall(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s`\"']{25,}", text)

    if suspicious:
        for s in suspicious:
            print(f"      {s[:80]}")
    check(
        not suspicious,
        f"文档里没有又长又完整的绝对路径（找到 {len(suspicious)} 处）",
    )


# =============================================================================
# ⑥ 打包白名单
# =============================================================================


def test_packinclude() -> None:
    """守卫 `.packinclude` —— 发布时不能把不该带的东西带出去。

    ⚠️ 这条守卫比它看起来重要。

    `.packinclude` 是**白名单**，所以正常情况下"忘了加"不会出事。
    但"手滑加了一行"会 —— 而且后果很严重：

      · 加上 `ginan-main/` 或 `ginan-windows-x64/`
        → 再分发了含 GPL-3 组件的二进制 → **整个项目的许可证被污染**
        （详见 THIRD_PARTY.md 的 ADR-001）

      · 加上 `dev/` 或 `docs/`
        → 用户拿到一堆开发文档，里面全是"我们踩过哪些坑"
        → 看起来很不专业，而且那是给维护者看的

    而这两种错误**都不会导致程序跑不起来** —— 所以没有测试的话，
    一直不会有人发现。
    """
    section("⑥ 打包白名单里没有不该带的东西")

    pack = ROOT / ".packinclude"
    if not pack.exists():
        check(False, ".packinclude 存在")
        return

    # 只取非注释、非空行 —— 那才是真正的条目
    entries = [
        ln.strip()
        for ln in pack.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

    check(bool(entries), f"白名单非空（{len(entries)} 条）")

    # ---- 绝不允许出现的 ----
    forbidden = {
        "ginan-main/": "Ginan 源码 —— 内含 GPL-3 组件，再分发会污染许可证",
        "ginan-windows-x64/": "Ginan 二进制 —— 同上",
        "ginan-linux-x64/": "Ginan 二进制 —— 同上",
        "ginan/": "可能指向 Ginan 二进制目录",
        "dev/": "开发文档 —— 用户不需要看我们踩过哪些坑",
        "docs/": "开发文档 —— 同上",
        "scripts/": "自检脚本 —— 开发时用的",
        "tests/": "测试与假对象 —— 开发时用的",
        "_scratch/": "实验用的临时目录",
    }

    bad = [e for e in entries if e.rstrip("/") + "/" in forbidden]
    if bad:
        for b in bad:
            print(f"      ⚠️ {b}  —— {forbidden[b.rstrip('/') + '/']}")
    check(not bad, "白名单里没有 Ginan 二进制 / 开发文档 / 测试")

    # ---- 许可证相关的文件必须在 ----
    must = {"LICENSE", "THIRD_PARTY.md"}
    missing = must - set(entries)
    check(
        not missing,
        f"许可证相关文件在白名单里（缺：{missing or '无'}）",
    )

    # ---- 代码本体必须在 ----
    check("ginan_agent/" in entries, "代码本体在白名单里")


# =============================================================================
# ⑦ 项目可搬移
# =============================================================================


def test_cwd_independent() -> None:
    """所有项目内资源，都不能受"当前工作目录"影响。

    ⚠️ 这是最容易被忽略的一类问题，因为**开发机上永远测不出来**：

       开发时在项目目录里敲命令 → CWD = 项目目录 → 什么都对
       用户从开始菜单/别处启动   → CWD 是别的地方 → 静默失效

    而且失效往往是**静默的** —— 比如 `load_dotenv()` 读不到 .env 时
    不报错，只是"没配置 API Key"，用户根本想不到是路径问题。
    """
    section("⑦ 项目内资源不受当前工作目录影响")

    import os
    import tempfile

    from ginan_agent import locations
    from ginan_agent.ginan import paths as gpaths

    original = os.getcwd()
    try:
        before = {
            "project_root": locations.project_root(),
            "frontend_dir": locations.frontend_dir(),
            "data_dir": gpaths.data_dir(),
            "app_root": gpaths.app_root(),
        }

        # 换到任意一个目录 —— 一个都不该变
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            after = {
                "project_root": locations.project_root(),
                "frontend_dir": locations.frontend_dir(),
                "data_dir": gpaths.data_dir(),
                "app_root": gpaths.app_root(),
            }
            # ⚠️ 必须在**退出 with 之前**回到原来的目录。
            #
            #    因为 Windows 不允许删除"当前工作目录" ——
            #    临时目录清理时会报 WinError 32（另一个程序正在使用此文件）。
            #    Linux 没这个限制，所以这类代码在 Linux 上写、在 Windows 上跑
            #    就会莫名其妙地失败。
            os.chdir(original)

        for key in before:
            check(
                before[key] == after[key],
                f"{key} 在换了 CWD 之后没变（{before[key]}）",
            )
    finally:
        os.chdir(original)


def test_resources_exist() -> None:
    """项目内被代码引用的资源，必须真的存在。

    ⚠️ 这条看似废话，但它抓的是"改了目录结构却忘了改引用" ——
       比如把 frontend/ 挪了位置，代码还在老地方找。
       那种错误不会在 import 时报，只在**用户打开界面时**才暴露。
    """
    section("⑧ 被引用的项目内资源都存在")

    from ginan_agent import locations

    check(locations.project_root().is_dir(), "project_root 是个目录")
    check(
        (locations.project_root() / "ginan_agent").is_dir(),
        "project_root 下确实有 ginan_agent 包",
    )
    check(locations.frontend_dir().is_dir(), "前端目录存在")
    for f in ("index.html", "app.js", "styles.css", "markdown.js"):
        check((locations.frontend_dir() / f).is_file(), f"前端资源 {f} 存在")
    check(
        locations.dotenv_path().name == ".env",
        f".env 的路径算得对（{locations.dotenv_path()}）",
    )


def test_no_bare_dotenv() -> None:
    """静态检查：不许出现不带参数的 `load_dotenv()`。

    ⚠️ 裸的 `load_dotenv()` 从**当前工作目录**往上找 .env ——
       用户换个目录启动就失效，而且**静默失效**。

       这个静态检查的意义在于：它是唯一能在"还没出事"时发现问题的办法 ——
       因为功能测试通常都在项目目录里跑，测不出这个。

    ⚠️ 必须用 **AST 解析**，不能用文本匹配。
       踩过一次：用正则 `load_dotenv\\(\\s*\\)` 扫源码，
       结果把**文档字符串里提到这个写法的地方**也当成了违规
       （我们的注释里正好在解释"为什么不能这么写"）。

       文本匹配看的是"文件里有没有这段字符"，
       而我们要问的是"**代码里有没有这个调用**" —— 这是两个问题，
       只有 AST 能回答后者。

       （同理，它也自动忽略了注释、字符串、以及带参数的正确调用。）
    """
    section("⑨ 没有裸的 load_dotenv() 调用")

    import ast

    hits: list[str] = []

    for path in (ROOT / "ginan_agent").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute)
                else ""
            )
            # 裸调用 = 名字对，且既无位置参数也无关键字参数
            if name == "load_dotenv" and not node.args and not node.keywords:
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    if hits:
        for h in hits:
            print(f"      {h}")
    check(
        not hits,
        "没有裸的 load_dotenv()（都用 locations.load_project_dotenv）",
    )


# =============================================================================
# ⑩ 保护 Ginan
# =============================================================================


def test_ginan_protection() -> None:
    """⚠️ 绝不让程序往 Ginan 的安装目录里写东西。

    用户明确提过这条要求（"记得程序不要破坏 ginan"）。
    而且这不是多虑 —— 有个**真实的**写入路径：

        `pea` 的 RTS 输出文件名是 `<CONFIG>_<RECEIVER>.rts`（没有目录前缀），
        所以它写在**当前工作目录**里。
        如果 CWD 设成 Ginan 目录，就会直接写进去。

    所以要在真正执行前拦一道，而不是靠"我一直很小心"。
    """
    section("⑩ 保护 Ginan：不往它的安装目录里写")

    import os
    import tempfile

    from ginan_agent.ginan import paths as gp

    # =====================================================================
    # ⚠️⚠️ 这一节原来断言的是「**这台机器上**有 Ginan 安装目录」：
    #
    #         installs = gp.ginan_install_dirs()
    #         check(bool(installs), f"认出了 {len(installs)} 个 …")
    #         if not installs:
    #             return
    #
    #     那是**依赖机器**的断言，后果有两层，第二层更糟：
    #
    #       ① 没装 Ginan 的机器上，这一节**整个跳过** ——
    #          下面那几条边界断言一次都没跑
    #       ② 它不是"跳过"，是**报红** —— 于是**一个刚 clone 下来的
    #          仓库连自己的自检都过不了**
    #
    #     实测（2026-09-22，干净 clone 到另一个目录）：
    #         `check_paths.py` → 「认出了 0 个 Ginan 安装目录」✗
    #     同一次运行里 ⑩b 是**绿的** —— 因为 ⑩b 自己造了夹具。差别就在这儿。
    #
    #     > .gitignore 里早就写着这条最怕什么：
    #     > 「测试在本地过、别人 clone 下来就崩」是最难查的一类问题
    #     > （因为本地永远是对的）。**这一条就是那个形状。**
    #
    #     👉 改成**自己造一个** Ginan 安装布局，和 ⑩b 用同一个口子：
    #        `GINAN_AGENT_PEA` 是 `_pea_candidates()` 的第一优先级。
    #        这样这一节在**任何**机器上都是真的在测东西。
    # =====================================================================
    old = os.environ.get("GINAN_AGENT_PEA")
    with tempfile.TemporaryDirectory(prefix="ginan_") as td:
        root = Path(td)
        # 真的发行包就是这个形状：<安装>/bin/pea
        install = root / "ginan"
        pea = install / "bin" / gp.PEA_NAME
        pea.parent.mkdir(parents=True, exist_ok=True)
        pea.write_bytes(b"MZ")        # 内容无所谓 —— 没人会去执行它
        os.environ["GINAN_AGENT_PEA"] = str(pea)
        try:
            installs = gp.ginan_install_dirs()
            check(
                install.resolve() in {p.resolve() for p in installs},
                f"★★ 认出自己造的那个安装目录（共认出 {len(installs)} 个）",
            )

            # ---- 里面的要被认出来 ----
            check(gp.is_inside_ginan(install)[0], "安装目录本身被认出来")
            check(
                gp.is_inside_ginan(install / "exampleConfigs")[0],
                "安装目录的子目录被认出来",
            )
            check(
                gp.is_inside_ginan(install / "a" / "b" / "c")[0],
                "深层子目录也被认出来",
            )

            # ---- 外面不能被误判 ----
            check(not gp.is_inside_ginan(root)[0], "安装目录的上一层不算在里面")
            check(
                not gp.is_inside_ginan(root / "ginan-dist")[0],
                "名字像但不在里面 —— 也不该被误判",
            )

            # ---- ⚠️ 边界：字符串前缀会误判的那种 ----
            #
            #    如果用 `str(path).startswith(str(install))` 来判断，
            #    `ginan-backup` 会被误判成在 `ginan` 里面。
            #    （用户的备份目录、另一个版本 —— 这些是正常位置，不该被拦。）
            #
            #    正确的做法是**路径层级**判断（Path.is_relative_to）。
            #
            # ⚠️ 例子取 `install` 的兄弟 —— 而 `install` 就是**最外层的**
            #    那个安装目录（`pea.parent` 是 `install/bin`，比它更深）。
            #    踩过一次：用 `installs[0]` 的话可能取到深层那个，
            #    它的兄弟**确实还在 `install` 里面** —— 断言写反了，
            #    红的是测试而不是代码。
            sibling = install.parent / (install.name + "-backup")
            check(
                not gp.is_inside_ginan(sibling)[0],
                f"{install.name} 的兄弟目录 {sibling.name} 不算在里面"
                f"（字符串前缀会误判）",
            )

            # ---- 守卫真的会抛异常 ----
            try:
                gp.guard_not_in_ginan(install, "测试")
                check(False, "守卫应该在 Ginan 目录上抛异常")
            except ValueError as exc:
                msg = str(exc)
                check(True, "守卫在 Ginan 目录上抛了异常")
                check("怎么办" in msg, "错误信息里给出了'怎么办'")
                check(str(install)[:20] in msg, "错误信息里指明了是哪个目录")
        finally:
            if old is None:
                os.environ.pop("GINAN_AGENT_PEA", None)
            else:
                os.environ["GINAN_AGENT_PEA"] = old

    # ---- 正常位置要放行 ----
    #
    # ⚠️ 放在 `GINAN_AGENT_PEA` 还原**之后** —— 这一条要证明的是
    #    "随便一个临时目录不会被误拦"，和上面那个造出来的安装无关。
    with tempfile.TemporaryDirectory() as td:
        try:
            gp.guard_not_in_ginan(Path(td), "测试")
            check(True, "正常位置放行")
        except ValueError:
            check(False, "正常位置被误拦了！")


# =============================================================================
# 主程序
# =============================================================================


def test_ginan_protection_does_not_overreach() -> None:
    section("⑩b ★★ 「保护 Ginan」不能把**基地之外**的地方也一起圈进去")

    # ⚠️⚠️ 上面 ⑩ 测的是"该保护的保住了"。这一条测**反面**：
    #    "不该保护的别乱保护" —— 而后者更致命。
    #
    #    原来的判据是从 pea 的位置**无条件往上取两层**：
    #
    #        found.append(pea.parent)
    #        found.append(pea.parent.parent)      # ← 只要不是盘根就加
    #
    #    ⚠️ pea 落在浅目录时，那个"上一层"会把整块地方圈进来。
    #       实测（pea 放在 `D:\pea.exe`）：`D:\` 成了"Ginan 安装目录" ——
    #       **整块盘上的所有运行都起不来**，报的是
    #       「拒绝把工作目录设在 Ginan 的安装目录里」，
    #       而用户根本没装过 Ginan 到那儿。
    #
    #    ⚠️ 更贴近现实的一条：`paths.py` 的候选表里有 `<base>/bin/pea.exe`，
    #       一旦命中，`<base>` 自己就成了受保护目录，
    #       而工作目录 `userdata/runs/…` **就在它下面** → 所有运行都起不来。
    #
    #    👉 判据：**基地本身 → 保护；基地之外 → 一律不保护。**
    import os as _os
    import tempfile as _tempfile

    from ginan_agent.ginan import paths

    old = _os.environ.get("GINAN_AGENT_PEA")
    try:
        # (pea 的相对位置, 描述, 受保护的那一层是 base 的哪个子目录)
        #
        # ⚠️ 第三种布局里，**安装目录是 `base/ginan`，不是 `base`** ——
        #    我第一版断言写的是"base 一定受保护"，
        #    于是第三种布局**红了**。那是**我的判据错了**：
        #    `base` 是安装目录的**上一层**，保护它就是过度保护
        #    （而下一条断言恰好就是在测"别过度保护"）。
        for layout, desc, protected in [
            ("pea.exe", "pea 直接躺在 base 下", ""),
            ("bin/pea.exe", "pea 在 <base>/bin 下", ""),
            ("ginan/bin/pea.exe", "pea 在 <base>/ginan/bin 下", "ginan"),
        ]:
            base = Path(_tempfile.mkdtemp(prefix="pea_"))
            pea = base / layout
            pea.parent.mkdir(parents=True, exist_ok=True)
            pea.write_bytes(b"MZ")
            _os.environ["GINAN_AGENT_PEA"] = str(pea)

            install = base / protected if protected else base
            check(
                paths.is_inside_ginan(install / "workdir")[0],
                f"★ 安装目录本身仍然受保护：{install.name}/workdir（{desc}）",
            )
            # ⚠️ 这一条才是这个 bug 的判据 —— **安装目录之外**的地方
            outside = base.parent / "别的地方"
            check(
                not paths.is_inside_ginan(outside)[0],
                f"★★ 安装目录**之外**不受影响（{desc}）"
                "—— 原来 pea 在浅目录时会把整个上层目录圈进来，"
                "于是**所有运行都起不来**",
            )
            if protected:
                check(
                    not paths.is_inside_ginan(base / "workdir")[0],
                    f"★★ 安装目录的**上一层**不该被保护（{desc}）"
                    "—— 那一层不是 Ginan 的地盘",
                )
    finally:
        if old is None:
            _os.environ.pop("GINAN_AGENT_PEA", None)
        else:
            _os.environ["GINAN_AGENT_PEA"] = old


def test_doc_links_resolve() -> None:
    section("⑪ ★★ 文档里的相对链接必须指向**存在的文件**")

    # =====================================================================
    # ⚠️⚠️ 这一条是从一次**真实的发布事故**里长出来的。
    #
    #     `docs/concepts.md` 是一份**文档索引**，它里面有一整段专门警告：
    #
    #         「一份**指向不存在文件**的索引，比没有索引更糟：
    #           它让人以为"想看 ADR 就去那儿"，于是**不会去别处找**。」
    #
    #     ⚠️ 而那段警告自己就错过了同类的第二次和第三次：
    #
    #        ① `decisions.md` 那一行标着 ✅ 现在 —— 而磁盘上没有这个文件
    #        ② 后来 ADR 改指内部笔记 `dev/00-项目全貌.md` 第八节；
    #           再后来准备公开发布，`dev/` 不随仓库走 ——
    #           **14 处链接同时变成死链**，其中 4 处是 ADR 的"正主"
    #
    #     👉 共同点：**没有任何东西会去点那些链接**。
    #        文档里的链接和代码不同 —— 代码写错了测试会红，
    #        而链接写错了，直到一个读者撞上去之前都是"绿的"。
    #
    #     > 所以这条守卫做一件很朴素的事：**把每个相对链接当成文件路径，
    #     > 看看它到底在不在。**
    #
    #    ⚠️ 只查**本仓库里**的相对链接：
    #       · 跳过 http/https（外网的，查不了也不该查）
    #       · 跳过纯锚点 `#xxx`（同页跳转）
    #       · 锚点后缀要去掉 —— `file.md#某节` 要按 `file.md` 去找
    #       · 跳过 `dev/` 下的文档：那是内部笔记，**不随仓库发布**，
    #         它引用的东西在发布版里本来就不存在
    # =====================================================================
    docs = sorted(
        [ROOT / "README.md"]
        + [p for p in (ROOT / "docs").rglob("*.md") if p.is_file()]
    )
    check(len(docs) >= 3, f"找到了 {len(docs)} 份文档")

    broken: list[str] = []
    checked = 0
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in re.finditer(r"\]\(([^)\s]+)\)", text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            checked += 1
            # ⚠️ 链接是相对**当前文档**的
            resolved = (doc.parent / path_part).resolve()
            if not resolved.exists():
                rel = doc.relative_to(ROOT).as_posix()
                broken.append(f"{rel} → {target}")

    check(
        not broken,
        f"★ {checked} 个相对链接全部指向存在的文件"
        + (f"　**坏掉的 {len(broken)} 个**：\n      " + "\n      ".join(broken)
           if broken else ""),
    )

    # ⚠️ 顺带把"索引指向哪里"这件事钉一下：ADR 必须在仓库里能找到。
    #    （上面那条只保证链接不坏 —— 但把所有 ADR 链接删光也能过。
    #      这一条堵住那个缺口。）
    dec = ROOT / "docs" / "decisions.md"
    check(dec.is_file(), "★★ docs/decisions.md 在（ADR 得有个家）")
    if dec.is_file():
        body = dec.read_text(encoding="utf-8")
        for n in ("ADR-001", "ADR-002", "ADR-003", "ADR-004"):
            check(f"## {n}" in body, f"★ {n} 在 decisions.md 里")
        concepts = (ROOT / "docs" / "concepts.md").read_text(encoding="utf-8")
        check(
            "decisions.md" in concepts,
            "★★ 而且 concepts.md（索引）指向了它",
        )


def main() -> int:
    print("=" * 60)
    print("路径解析自检 —— 必须按用户走，不能硬编码")
    print("=" * 60)
    print("⚠️ 开发机上跑得通什么都证明不了 —— 所以这里主动'假装换个用户'。")

    test_single_location()
    test_unwritable_fails_loudly()
    test_data_dir_is_safe()
    test_bootstrap_dir_name_matches()
    test_no_hardcoded_paths()
    test_docs_use_placeholders()
    test_packinclude()
    test_cwd_independent()
    test_resources_exist()
    test_no_bare_dotenv()
    test_ginan_protection()
    test_ginan_protection_does_not_overreach()
    test_doc_links_resolve()

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
