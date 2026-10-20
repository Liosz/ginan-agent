"""Ginan 领域层 —— 目录布局与可执行文件定位。

============================ 这一层的定位 ============================
`ginan_agent/ginan/` 里全是**纯函数**：
  · 不 import core（不知道 agent、不知道 LLM 存在）
  · 不 import agent（不知道工具是怎么被调用的）

所以它们能被**独立测试**，将来加 MCP server 也只是再加一层适配。

============================ 目录布局 ============================
所有运行时数据放在**软件目录下的 `userdata/`**：

    <软件目录>/userdata/                 （见 core.session.default_app_root）
    ├── data/           ← 所有输入文件，**平铺**
    │                     下载来的 + 用户从 inbox 搬过来的
    ├── runs/
    │   └── <run_id>/
    │       ├── <config_id>.yaml 本次运行用的配置（名字来自测站+日期）
    │       ├── <RECEIVER>_<CONFIG>.POS / .TRACE / _smoothed.POS
    │       └── <日期>.log        pea 的完整原始输出
    ├── inbox/          ← 第四条路：用户手动把文件丢这里
    ├── sessions/       ← 对话历史（由 core.session 管）
    ├── ginan/          ← 放 pea（用户在收件箱之外的第二条路）
    └── logs/

# ⚠️ 上面这段必须照着**磁盘上真实的目录**写。
#    原来写的是 `config.yaml` / `outputs/` / `repro/` —— 三个都不对：
#    实测 `userdata/runs/<run_id>/` 里是 `<config_id>.yaml`、`.POS`、
#    `.TRACE`、`.log` **直接摊在 run 目录下**，没有 outputs/，也没有 repro/
#    （复现包至今没做，`dev/` 里挂着）。
#    **一份对不上的目录说明，会让下一个人去错地方找东西。**

============================ 为什么是"平铺" ============================
Ginan 的 `inputs_root` 是个**前缀**：它把每个输入文件名拼在这个前缀后面。
所以只要所有输入文件都在同一个目录里，一行配置就够了：

    inputs_root:                 <app>/data/
    gnss_observations_root:      <app>/data/      ← 同一个目录

不需要给每类文件建子目录，也不需要软链接。

============================ ⚠️ 子路径：这里原来写错了 ============================
本文件原来写着「前提是输入清单里**不能出现子路径**（比如 `tables/gpt_25.grd`）」——
**那句话是错的**，2026-09-20 做「完整」档时查实的：

    `inputs_root` 是**字符串前缀拼接**，不是"目录里找文件"。
    所以 `tables/gpt_25.grd` 拼出来就是 `<app>/data/tables/gpt_25.grd` ——
    **只要文件真的在那个子目录里，就能找到。**

证据有两处，都是 Ginan 自己的：
  · `src/cpp/common/debug.cpp:1873`
        `string filename = acsConfig.inputs_root + "tables/opoleloadcoefcmcor.txt";`
  · 官方 `default_config.yaml` 里就是这么写的
        `gpt2grid_files: [tables/gpt_25.grd]`

所以现在的规则是**两条并存**的：

    产品文件（按天变的）  →  平铺在  <app>/data/
    模型文件（一次性的）  →  放在    <app>/data/tables/

两边互不干扰：`inputs_root` 对前者就是目录本身，对后者多拼一段 `tables/`。

⚠️ 教训记在这儿：那句注释当时是**照着"全都是平铺"这个前提推的**，
   而不是量出来的。而它躺在模块开头、写着"⚠️ 前提"，看起来像一条
   已验证的约束 —— **一个没量过的结论，写成警告比不写还糟。**
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 复用 core 里的"用户数据目录"计算 —— 两个平台判断不该写两遍
from ..core.session import default_app_root

# =============================================================================
# 目录
# =============================================================================

# 找 pea 时可执行文件的文件名（Windows 带 .exe）
PEA_NAME = "pea.exe" if sys.platform == "win32" else "pea"


def app_root() -> Path:
    """应用数据根目录。"""
    return default_app_root()


def data_dir() -> Path:
    """所有输入文件放这里（平铺）。

    下载来的、用户手动搬来的，都在这一层。
    Ginan 的 `inputs_root` 和 `gnss_observations_root` 都指这儿。
    """
    return app_root() / "data"


def tables_dir() -> Path:
    """模型文件（`tables/`）放这里 —— 在 data/ **里面**，不是旁边。

    ⚠️ 位置不是随便定的：Ginan 的配置里引用它们用的是 `tables/gpt_25.grd`
       这种带前缀的写法，而解析方式是 `inputs_root + "tables/..."`。
       `inputs_root` 指着 `data/`，所以它们必须在 `data/tables/`。
       放在别处（比如 `<app>/tables/`）会**找不到，而且报错指不到这儿**。

    ⚠️ 这是**一次性**的东西：下过一次就一直用，不随日期变。
    """
    return data_dir() / "tables"


def runs_dir() -> Path:
    """每次运行一个子目录。"""
    return app_root() / "runs"


def run_dir(run_id: str) -> Path:
    """某次运行的目录。"""
    return runs_dir() / run_id


def inbox_dir() -> Path:
    """用户手动投放文件的目录（第四条路）。

    ⚠️ 这个目录可以**配置成网盘同步目录** ——
    这样用户在另一台机器上下载的文件会自动同步过来。

    ⚠️⚠️ 优先级：环境变量 → **设置里的 inbox_dir** → 默认位置。

        `Settings.inbox_dir` 这个设置项**存了很久、却从来没有人读它** ——
        界面上能填、能保存进 config.json、然后就躺在那里。
        用户改了"收件箱位置"，程序照样去默认的地方找 ——
        而现象是"我明明放进去了，它说没有"，离病因很远。

        👉 设置项存了不读 = 一个**假的**功能。要么读，要么别提供。

    ⚠️ 环境变量保留在最高优先级：自检脚本靠它把收件箱指到临时目录，
       不能让用户设置意外盖掉它。

    ⚠️ 相对路径按**软件目录**解析，不按 CWD ——
       用户填 `收件箱` 的意思是"软件目录下的收件箱"，
       而 CWD 取决于他从哪双击的程序。
    """
    configured = os.environ.get("GINAN_AGENT_INBOX", "").strip()
    if not configured:
        try:
            from ..settings import load_settings

            configured = (load_settings().inbox_dir or "").strip()
        except Exception:  # noqa: BLE001
            # ⚠️ 设置读不出来不该让"找收件箱"失败 —— 退回默认位置。
            #    设置文件损坏在 settings.load_settings 里已经有兜底，
            #    这里再兜一层是因为收件箱是**用户数据的入口**，不能因为
            #    一个配置问题就完全找不到。
            configured = ""

    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute():
            p = app_root() / p
        return p.resolve()
    return app_root() / "inbox"


def logs_dir() -> Path:
    return app_root() / "logs"


def ensure_dirs() -> None:
    """把该有的目录都建出来。**在任何写操作之前调用一次即可。**

    ⚠️ 注意这个函数**不会**被任何模块在导入时调用 ——
    导入即建目录是一种副作用，会让"我只是 import 一下"就改磁盘。
    （同一个教训在 core/loop.py 里也出现过：
      构造 Agent 曾经会落盘，后来去掉了。）

    ⚠️ 收件箱那个 mkdir **单独兜底**：它现在可能是用户填的网盘路径，
       打错字、或者网盘没挂载，都会让 mkdir 失败。
       而"收件箱建不出来"绝不该让**整个程序起不来** ——
       数据目录、运行目录都还能用，用户至少能跑通下载那条路。
    """
    for d in (data_dir(), runs_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)

    try:
        inbox_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        # 留个痕：启动横幅会打印收件箱路径，用户能看出它不对
        pass


# =============================================================================
# 找 pea
# =============================================================================
#
# 按优先级依次尝试，第一个找到的为准。
#
# ⚠️ 顺序是刻意的：**越明确的来源越靠前**。
#    环境变量是用户明确指定的，最优先；
#    开发目录只是"顺手找找"，放最后 ——
#    否则用户自己装了一个 pea，却被项目里那份旧的抢走了。


def find_pea() -> Path | None:
    """找到 pea 可执行文件，找不到返回 None。

    ⚠️ 返回 None 而不是抛异常 —— 因为"没装 Ginan"是一种**正常状态**，
    不是错误。界面应该显示"还没装，点这里装"，而不是崩掉。
    """
    for candidate in _pea_candidates():
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def _pea_candidates():
    """按优先级列出所有可能的 pea 位置。"""
    # ① 环境变量显式指定
    explicit = os.environ.get("GINAN_AGENT_PEA", "").strip()
    if explicit:
        yield Path(explicit).expanduser()

    # ② 应用数据目录里（将来"一键下载 Ginan"装到这里）
    yield app_root() / "ginan" / "bin" / PEA_NAME
    yield app_root() / "ginan" / PEA_NAME

    # ③ 系统 PATH
    found = shutil.which("pea")
    if found:
        yield Path(found)

    # ④ 开发时的常见位置 —— 从本文件往上找几层
    #
    # 典型布局（本项目的开发/实验环境就是这样）：
    #
    #     test06_ginanagent/
    #     ├── ginan-dist/               ← Ginan 本体都放这里
    #     │   ├── source/               源码
    #     │   ├── windows-x64/pea.exe   二进制
    #     │   └── linux-x64/pea
    #     ├── scratch/                  实验区
    #     └── ginan-agent/
    #         └── ginan_agent/ginan/paths.py   ← 本文件
    #
    # 从本文件往上走 4 层就到 test06_ginanagent/。
    #
    # ⚠️ 同时保留**旧的目录名**（ginan-windows-x64 之类）。
    #    因为别人克隆代码后，可能按老文档把 Ginan 放在老位置 ——
    #    多列几个候选的代价只是几次 is_file() 检查，而少列一个
    #    的代价是"找不到 pea"这种很难自己诊断的问题。
    here = Path(__file__).resolve()
    for up in range(1, 6):
        try:
            base = here.parents[up]
        except IndexError:
            break
        for sub in (
            "ginan-dist/windows-x64",   # ← 现在的布局
            "ginan-dist/linux-x64",
            "ginan-windows-x64",        # ← 旧布局，保持兼容
            "ginan-linux-x64",
            "bin",
            "ginan/bin",
        ):
            yield base / sub / PEA_NAME


# =============================================================================
# ⚠️ 保护 Ginan：绝不写入它的安装目录
# =============================================================================
#
# 这是一条**硬约束**，不是"最好别这么做"：
#
#   · Ginan 是别人写的软件，我们只是调用它。往人家的目录里写东西是不对的。
#   · 用户可能是从发行版或者共享目录里用的，那里的写入权限可能不属于他。
#   · 一旦写进去，用户重装/升级 Ginan 时会困惑（"这些文件哪来的？"）。
#   · 最坏情况是**覆盖掉 Ginan 自己的文件** —— 那就把用户的工具弄坏了，
#     而且他多半不会想到是我们干的。
#
# ⚠️ 现实风险在哪？三个具体的：
#
#   ① `pea` 的 RTS 输出文件名是 `<CONFIG>_<RECEIVER>.rts`（**没有目录前缀**），
#      所以它会写在**当前工作目录**里 —— CWD 设错就写进 Ginan 目录了。
#
#   ② `outputs_root` 如果配成相对路径，会相对 CWD 解析。
#
#   ③ 我们的工作目录如果恰好设在 Ginan 下面（比如调试时图省事）。
#
# 所以要在**真正执行前**拦一道，而不是靠"我一直很小心"。


def ginan_install_dirs() -> list[Path]:
    """认出哪些目录是 Ginan 的安装目录（受保护，不可写）。

    ⚠️ 用几个**特征文件**来认，而不是只看目录名 ——
       用户可能把 Ginan 解压成一个随便什么名字的文件夹。
    """
    found: list[Path] = []

    # 从 pea 的位置往回推
    pea = find_pea()
    if pea is not None:
        # pea 通常在 <ginan>/bin/ 或 <ginan>/ 里
        found.append(pea.parent)
        # ⚠️⚠️ **父目录的父目录，只有在父目录真的叫 bin 时才算。**
        #
        #    原来这里是无条件往上再取一层（只排除了盘根）：
        #
        #        found.append(pea.parent.parent)
        #
        #    ⚠️ 后果：pea 落在**浅目录**时，那个"上一层"会把整块盘圈进来 ——
        #       实测（把 pea 放在临时目录里）：
        #
        #           ginan_install_dirs() → ['…\\shallow_xxx', '…\\AppData\\Local\\Temp', …]
        #           is_inside_ginan(<那个临时目录>/workdir) → True
        #           run_pea(...) → ValueError: 拒绝把工作目录设在 Ginan 的安装目录里
        #
        #       而 `D:\pea.exe` / `~/pea` 更直接：**整个 D 盘 / 整个家目录**
        #       都成了"Ginan 安装目录"，于是**所有运行都起不来**。
        #
        #    ⚠️ 而这不是假想 —— 候选表里就有 `<base>/bin/pea.exe` 和
        #       `<base>/ginan/bin/pea.exe`，一旦命中，`<base>` 自己就成了
        #       受保护目录，而工作目录 `userdata/runs/…` 就在它下面。
        #
        #    👉 判据：**再往上一层，只有当那一层是 bin 目录时才成立**。
        #       那是 Ginan 发行包的真实布局，也是这个函数原本想覆盖的情况。
        if pea.parent.name.lower() == "bin" and pea.parent.parent != pea.parent:
            found.append(pea.parent.parent)

    # 开发时的布局：ginan-dist/{source,windows-x64,linux-x64}
    here = Path(__file__).resolve()
    for up in range(1, 6):
        try:
            base = here.parents[up]
        except IndexError:
            break
        dist = base / "ginan-dist"
        if dist.is_dir():
            found.append(dist)
            for sub in ("source", "windows-x64", "linux-x64"):
                if (dist / sub).is_dir():
                    found.append(dist / sub)
        src = base / "ginan-main"           # 旧布局
        if src.is_dir():
            found.append(src)

    # 去重，并只保留真实存在的
    seen: set[Path] = set()
    out: list[Path] = []
    for p in found:
        try:
            p = p.resolve()
        except OSError:
            continue
        if p not in seen and p.is_dir():
            seen.add(p)
            out.append(p)
    return out


def is_inside_ginan(path: Path) -> tuple[bool, Path | None]:
    """这个路径在 Ginan 的安装目录里吗。

    返回 (是否在里面, 是哪个安装目录)。

    ⚠️ 用 `Path.is_relative_to` 做**路径层级**判断，不是字符串前缀匹配。
       字符串匹配会把 `C:/ginan-backup/` 误判成在 `C:/ginan/` 里面。
    """
    try:
        target = Path(path).resolve()
    except OSError:
        return False, None

    for install in ginan_install_dirs():
        try:
            if target == install or target.is_relative_to(install):
                return True, install
        except (ValueError, OSError):
            continue
    return False, None


def guard_not_in_ginan(path: Path, what: str = "输出目录") -> None:
    """确认这个路径不在 Ginan 目录里，否则抛异常。

    ⚠️ 这里**故意抛异常**而不是返回 False。
       因为这是个"绝不该发生"的情况 —— 静默返回 False 的话，
       调用方可能就这么放着不管，然后 Ginan 目录被写脏。
       **宁可让程序停下来，也不要悄悄破坏用户的工具。**

    ⚠️ 错误信息要说清"怎么修"，因为触发它的多半是我们自己写错了路径，
       而看到这条错误的人可能是三个月后的我。
    """
    inside, install = is_inside_ginan(path)
    if not inside:
        return

    raise ValueError(
        f"拒绝把{what}设在 Ginan 的安装目录里。\n"
        f"  想用的位置：{path}\n"
        f"  落进了：    {install}\n"
        f"\n"
        f"Ginan 是别人写的软件，我们只调用它，不往里写东西 ——\n"
        f"  写入可能覆盖它自己的文件，或者在下一次升级时造成困惑。\n"
        f"\n"
        f"怎么办：把工作目录设在别处。默认位置是：\n"
        f"  {app_root() / 'runs'}\n"
    )


def pea_version(pea: Path | None = None) -> str:
    """跑一下 `pea --help` 抓版本横幅。

    Ginan 启动时第一行就是版本，形如：
        PEA starting... (HEAD v4.1.4-5033996 from 2026-09-16 17:56:53 +1000)

    拿不到就返回空字符串 —— 版本号是**锦上添花**，
    不该因为它读不到就让整个环境检查失败。
    """
    import subprocess

    exe = pea or find_pea()
    if exe is None:
        return ""

    try:
        # ⚠️ 超时必须有。pea --help 本身很快，但如果可执行文件损坏或被
        #    杀毒软件拦下，可能会挂住 —— 环境检查不能因此卡死。
        out = subprocess.run(
            [str(exe), "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("PEA starting"):
            return line
    return (out.stdout or "").splitlines()[0].strip() if out.stdout else ""
