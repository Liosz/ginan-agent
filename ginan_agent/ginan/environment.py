"""Ginan 领域层 —— 环境检查：这台机器现在能不能跑。

============================ 这个模块的定位 ============================
它回答一个问题：**"我现在能开始干活了吗？如果不能，缺什么？"**

它**不**检查网络（那是 probe.py 的事）。两者分开是因为：
  · 环境检查是**本地的**，瞬间出结果，永远能跑
  · 网络检查要联网，可能要等好几秒，还可能失败

分开之后，界面可以先秒出环境结果，再慢慢跑网络检查。

============================ 新手体验的关键 ============================
每一项检查都带 **状态 + 人话说明 + 该做什么**。

不是：
    ✓ pea
    ✗ OPENBLAS_NUM_THREADS

而是：
    ✓ Ginan 已就绪（v4.1.4）
    ⚠ 未设置线程限制 —— 不设置的话，处理时可能出现"OpenBLAS 检测到
      OpenMP 循环，程序可能挂起"的警告。程序会自动帮你设置，不用管。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from . import paths

Status = Literal["ok", "warn", "fail", "info"]


@dataclass
class CheckItem:
    """一项检查的结果。"""

    key: str
    label: str          # 短名字，界面上显示
    status: Status
    detail: str         # 具体是什么情况（可以带数字）
    advice: str = ""    # 不正常时该做什么（**这一步最关键**）

    @property
    def blocks_work(self) -> bool:
        """这一项会不会让人**完全没法干活**。

        ⚠️ 区分"致命"和"警告"很重要 ——
        如果所有问题都显示成一样严重，用户就分不清该先解决哪个。

        比如"没设置线程环境变量"只是可能变慢，
        而"找不到 pea"是彻底没法跑。
        """
        return self.status == "fail"


@dataclass
class EnvironmentReport:
    """环境检查的完整结果。"""

    items: list[CheckItem] = field(default_factory=list)
    pea_path: str | None = None
    pea_version: str = ""
    # 各个目录的位置。
    #
    # ⚠️ 这是**数据**，不是**检查项** —— 分开是刻意的。
    #    目录路径没有"是否正常"可言，把它塞进 items 会让
    #    检查列表里混进一堆永远显示为 info 的条目，稀释真正要看的信号。
    #    （收件箱就是个例子，它原来在 items 里，和侧栏的面板重复了。）
    paths: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """能开始干活吗。"""
        return not any(i.blocks_work for i in self.items)

    @property
    def problems(self) -> list[CheckItem]:
        return [i for i in self.items if i.status in ("fail", "warn")]

    @property
    def verdict(self) -> str:
        """一句话总结。"""
        fails = [i for i in self.items if i.status == "fail"]
        warns = [i for i in self.items if i.status == "warn"]
        if fails:
            return f"还不能开始 —— 有 {len(fails)} 个必须解决的问题。"
        if warns:
            return f"可以开始，但有 {len(warns)} 项建议处理。"
        return "一切就绪，可以开始处理数据。"

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "ready": self.ready,
                "verdict": self.verdict,
                "pea_path": self.pea_path,
                "pea_version": self.pea_version,
                "paths": self.paths,
                "items": [
                    {**asdict(i), "blocks_work": i.blocks_work} for i in self.items
                ],
            },
            ensure_ascii=False,
            indent=2,
        )


# =============================================================================
# 检查项
# =============================================================================


def _check_pea() -> tuple[CheckItem, Path | None, str]:
    """找 pea 可执行文件。这是**唯一一个致命项** —— 没有它就什么都做不了。"""
    pea = paths.find_pea()
    if pea is None:
        return (
            CheckItem(
                key="pea",
                label="Ginan 主程序",
                status="fail",
                detail="没有找到 pea 可执行文件",
                advice=(
                    "需要先准备 Ginan 本体。三条路：\n"
                    "  ① 程序自动下载（推荐，见设置里的「下载 Ginan」）\n"
                    "  ② 自己从 https://github.com/GeoscienceAustralia/ginan/releases "
                    "下载，解压后把 pea 的路径填进设置\n"
                    "  ③ 用 Docker 版 Ginan\n"
                    "⚠️ 我们不随程序分发 Ginan —— 它内含 GPL-3 组件，"
                    "分发会产生许可证问题（详见 THIRD_PARTY.md）"
                ),
            ),
            None,
            "",
        )

    version = paths.pea_version(pea)
    return (
        CheckItem(
            key="pea",
            label="Ginan 主程序",
            status="ok",
            detail=f"已找到：{pea}",
            advice="",
        ),
        pea,
        version,
    )


def _check_dirs() -> CheckItem:
    """检查数据目录能不能写。"""
    try:
        paths.ensure_dirs()
        probe = paths.data_dir() / ".writable-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckItem(
            key="dirs",
            label="数据目录",
            status="fail",
            detail=f"不能写入 {paths.data_dir()}（{exc}）",
            advice="检查磁盘是否已满，或者这个位置是否有写入权限。",
        )
    return CheckItem(
        key="dirs",
        label="数据目录",
        status="ok",
        detail=str(paths.data_dir()),
    )


def _check_disk() -> CheckItem:
    """检查剩余磁盘空间。**通用**的一道，不针对某个工作流。

    ⚠️ 阈值定在 2 GB。为什么？
    一次完整的 PPP 运行要下 ~55 MB 产品 + 用户自己的观测数据，
    加上输出和缓存，几百 MB 是常态。
    再考虑到 .TRACE 可能很大（Windows 上还有 2.1 GB 的单文件上限），
    2 GB 以下就值得提醒了。

    ⚠️⚠️ **但 2 GB 对 POD 远远不够 —— 这一点必须说清，别让这道检查
       看起来像是在担保 POD 也能跑。**

        实测（2026-09-25）：一整天 POD 的 RTS 状态文件 **51 MB/历元**
        ⇒ 288 历元约 **15 GB**（46 站），87 站那轮约 **18 GB**。

        ⇒ 磁盘剩 10 GB 的用户**会通过这一道**，然后在跑到一半时磁盘满。

    ⚠️ 所以 POD 的磁盘判据**不在这里**，在 `do_run_processing` ——
       **只有那一步知道是哪个工作流**，也只有那一步知道真正要写多少。
       取值在 `workflows.Workflow.disk_need_gb`（POD = 25 GB）。

    ⚠️ 两道检查的分工：这一道回答"**这台机器还能不能干活**"，
       那一道回答"**这一件具体的活能不能干**"。别把后者挪到这儿来 ——
       环境检查跑在用户选工作流**之前**，那时还不知道要干什么。
    """
    try:
        usage = shutil.disk_usage(paths.app_root().anchor or paths.app_root())
    except OSError:
        return CheckItem(
            key="disk", label="磁盘空间", status="info", detail="读不到磁盘信息"
        )

    free_gb = usage.free / (1024**3)
    if free_gb < 2:
        return CheckItem(
            key="disk",
            label="磁盘空间",
            status="warn",
            detail=f"剩余 {free_gb:.1f} GB",
            advice=(
                "Ginan 处理会产生较大的输出文件（.TRACE 可能上百 MB 甚至更多）。"
                "建议留出至少 2 GB。"
            ),
        )
    return CheckItem(
        key="disk",
        label="磁盘空间",
        status="ok",
        detail=f"剩余 {free_gb:.1f} GB",
    )


def _check_deps() -> CheckItem:
    """检查关键依赖装齐了没。

    ⚠️ 清单从 `ginan_agent/deps.py` 来，**不在这里另写一份**。

       这里原来自己抄了 5 个包，和 bootstrap、requirements.txt 三方
       各写各的 —— 而三份都漏了 fastapi / uvicorn / sse_starlette / httpx。
       直接后果：界面上这一栏显示「全部就位」，而服务端根本起不来；
       它的建议「pip install -r requirements.txt」**照做也修不好**，
       因为那三个包当时在 requirements.txt 里是注释掉的。
    """
    from ..deps import DEPS

    missing: list[str] = []
    for pkg, mod in DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    if missing:
        return CheckItem(
            key="deps",
            label="Python 依赖",
            status="fail",
            detail=f"缺少：{'、'.join(missing)}",
            advice="在项目目录运行：pip install -r requirements.txt",
        )
    return CheckItem(
        key="deps", label="Python 依赖", status="ok", detail="全部就位"
    )


def _check_api_key(env_name: str = "DEEPSEEK_API_KEY") -> CheckItem:
    """检查大模型的 API Key。

    ⚠️ 这一项是 **fail 而不是 warn**，因为它确实致命 ——
    没有 Key，agent 完全不能工作（但 Ginan 本身还能手动跑）。

    措辞上把它说成"还没配置"而不是"错误" ——
    对第一次打开程序的用户来说，这是个**正常的待办事项**，不是故障。

    ============================ ⚠️ 两个来源都要看 ============================
    密钥有**两个**地方可以配：

        ① 界面「设置」里填的    → 存在 config.json   ← 用户最可能用的
        ② 环境变量 / .env       → 开发时用的

    ⚠️ 踩过一次：这个检查原来**只读环境变量**。
    结果是用户明明在设置界面填好了、agent 也能正常跑，
    但侧栏一直红着说"还没配置" ——
    **功能能用而状态说不能用**，这是最容易让人怀疑"是不是坏了"的那类不一致。

    教训：凡是"检查某个东西在不在"的函数，
    必须和"真正用那个东西"的函数**看同一个来源**。
    否则两边迟早会分叉。
    """
    # ---- 来源 ①：界面里配的（优先）----
    #
    # ⚠️ 这里 import settings 有点"越层"（settings 是应用级的，ginan 是领域层）。
    #    但可以让步：对用户来说，"程序检查我的环境"本来就是一件应用级的事，
    #    他看不出来也不关心内部是怎么分层的。
    #    刻意维持分层的代价是"每个调用方都要记得把 Key 传进来"，
    #    而只要有一个调用方忘了，这个 bug 就又回来了。
    try:
        from ..settings import load_settings

        s = load_settings()
        if s.has_usable_key():
            return CheckItem(
                key="api_key",
                label="AI 模型密钥",
                status="ok",
                detail=f"已配置（{s.masked_key()}）",
            )
    except Exception:  # noqa: BLE001
        # 设置读不出来不该让环境检查整个失败 —— 继续看环境变量那条路
        pass

    # ---- 来源 ②：环境变量 ----
    #
    # ⚠️ 用显式路径读 .env（见 locations.dotenv_path 的说明）——
    #    裸的 load_dotenv() 从当前工作目录往上找，换个启动目录就失效。
    from ..locations import load_project_dotenv

    load_project_dotenv()
    if os.environ.get(env_name, "").strip():
        return CheckItem(
            key="api_key",
            label="AI 模型密钥",
            status="ok",
            detail="已配置（来自环境变量）",
        )

    return CheckItem(
        key="api_key",
        label="AI 模型密钥",
        status="fail",
        detail="还没配置",
        advice=(
            "程序需要调用 AI 模型来理解你的需求。\n"
            "点右上角「设置」填一个即可 —— 推荐 DeepSeek，国内可直连、有免费额度。\n"
            "密钥只存在你自己的电脑上，不会上传。"
        ),
    )


# ⚠️ 这里原来还有一个 _check_inbox()，已经删掉。
#
#    它把"收件箱"当成环境检查的一项列出来 —— 但那是不对的，两个原因：
#
#      ① **它不是"检查"**。收件箱没有"正常/异常"之分，
#         它是个**功能入口**，该有自己的位置。
#
#      ② **它和侧栏的收件箱面板重复了** —— 同一个东西在两处出现，
#         用户会疑惑"哪个是真的、以哪个为准"。
#         （用户原话："收件箱，你写重复了"。）
#
#    现在收件箱只在侧栏有一个专属面板。它的路径通过
#    EnvironmentReport.paths 提供给界面，而不是伪装成一条检查项。


# =============================================================================
# 主入口
# =============================================================================


def check_environment(api_key_env: str = "DEEPSEEK_API_KEY") -> EnvironmentReport:
    """跑一遍全部环境检查。

    ⚠️ 这个函数**不联网、不抛异常、永远是秒回**。
    任何一项出问题都变成结果里的一个条目，而不是中断整个检查 ——
    因为用户需要看到**完整的问题清单**，而不是"修一个报一个"。
    """
    report = EnvironmentReport()

    pea_item, pea_path, version = _check_pea()
    report.items.append(pea_item)
    report.pea_path = str(pea_path) if pea_path else None
    report.pea_version = version

    report.items.append(_check_deps())
    report.items.append(_check_dirs())
    report.items.append(_check_disk())
    # ⚠️ 这里**曾经有一项 `_check_threads()`**（检查 `OPENBLAS_NUM_THREADS` 那类
    #    环境变量，并建议用户设上）。2026-09-25 删掉了，两个理由：
    #
    #    ① **它建议的事是错的**：设 `OPENBLAS_NUM_THREADS=1` 会把并行砍到
    #       只剩 1 个核（实测 21 线程只占 1.01 核，而不设是 40 线程占 8.32 核）。
    #    ② **用户根本不该关心这件事**：他的机器有多少核、该开多少线程，
    #       是 Ginan 和它下面那层库的事。用户看到这一项只会多一个要想的问题。
    #
    #    完整实测和理由见 `pea_env()` 的说明。
    report.items.append(_check_api_key(api_key_env))

    # 目录位置 —— 是**数据**不是检查项（见 EnvironmentReport.paths 的说明）
    report.paths = {
        "data": str(paths.data_dir()),
        "inbox": str(paths.inbox_dir()),
        "runs": str(paths.runs_dir()),
    }

    return report


def pea_env() -> dict[str, str]:
    """跑 Ginan 时该设置的环境变量。

    ⚠️⚠️ **它现在一个线程相关的变量都不设 —— 这是刻意的，不是漏了。**

        原来这里设：

            env.setdefault("OPENBLAS_NUM_THREADS", "1")
            env.setdefault("GOTO_NUM_THREADS", "1")

        理由是「OpenBLAS 和 OpenMP 嵌套会挂起，按住多余的那个 BLAS 池」。
        理由本身不算错，但**没人量过它的代价**。实测（2026-09-25）：

        | | 进程线程数 | **实际占用核数** | 每历元【处理】 |
        |---|---|---|---|
        | 设了（1 线程） | 21 | **1.01** | 20.6 / 19.7 / 18.6 / 18.4 → 69.6 / 77.3 / 79.5 秒 |
        | 不设 | 40 | **8.32** | **12 / 11 / 12 / 12 → 43 / 43 / 44 / 45 秒** |

        ⚠️ 两条都是**同一份 87 站配置、同一天数据、同一台机器**（i5-14600KF，
           20 个逻辑核），差异只有那一个环境变量。**不设快 1.6~1.7 倍。**

        ⚠️ 注意 21 线程那一行：Ginan 自己的 20 个 OMP 线程**是建了的**
           —— 建了却没干活，所以 20 个核在旁边看着 1 个核跑。
           用户的话说得最准：**「别出现一个核心干活、多核围观」**。

        ⚠️ 而且不设的那一轮**没有出现 OpenBLAS 的挂起警告**。
           Ginan 内部本来就有 `ScopedOpenBlasThreadLimit`
           （`ginan-dist/source/src/cpp/common/blasThreading.hpp`），
           只在它自己的 OpenMP 分块循环里限制 BLAS —— 那个保护**精准得多**，
           而原来那一刀把它连同全部并行一起砍掉了。

    ⚠️ **不要再往这里加线程相关的变量。**
       用户的机器有多少核、该开多少线程，是**库和 Ginan 的事**，不是 app 的事。
       连「按核数自动设置」也不要 —— 那是 app 替库去做库自己更清楚的决定，
       而用户换一台机器，这个决定就可能又错了。

    ⚠️ 保留 `os.environ.copy()`：用户**自己**设了这些变量，那是他的选择，照传。
    """
    return os.environ.copy()


def process_cpu_seconds(proc) -> float | None:
    """`proc` 这个子进程**累计**吃掉了多少 CPU 秒。拿不到就返回 `None`。

    ⚠️ **为什么不用 `psutil`**：这个仓库对依赖很克制（`deps.py` 是唯一出处，
       `check_launcher.py` 会断言两边一致），而这一件事用标准库就能做完 ——
       为了一个自检功能引一个新依赖不划算。

    ⚠️ **为什么是「累计 CPU 秒」而不是「百分比」**：
       判据要的是**占用核数** = 两次采样的 CPU 秒之差 ÷ 两次的墙钟之差。
       单看一个百分比会被"刚启动那一下"骗到（那时还没进并行区）。

    ⚠️⚠️ **判据是核数，不是线程数。** 这一点踩过坑：数线程会看到 40 个
       （Ginan 的 OMP 池 20 + OpenBLAS 的池 20），而实际只有 1 个核在干活。
       详见 `docs/ginan-facts.md` 七点五 ③。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD),
                            ("dwHighDateTime", wintypes.DWORD)]

            def _ft(ft: _FILETIME) -> int:
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            k = ctypes.windll.kernel32
            creation, exit_, kernel, user = (_FILETIME() for _ in range(4))
            # ⚠️ `proc._handle` 是 Popen 建进程时拿到的句柄（有 QUERY 权限）。
            #    它在 CPython 的 Windows 实现里是个公开用法的私有属性 ——
            #    拿不到就算拿不到，返回 None，绝不抛。
            handle = getattr(proc, "_handle", None)
            if handle is None:
                return None
            ok = k.GetProcessTimes(
                wintypes.HANDLE(int(handle)),
                ctypes.byref(creation), ctypes.byref(exit_),
                ctypes.byref(kernel), ctypes.byref(user),
            )
            if not ok:
                return None
            # FILETIME 的单位是 100 纳秒
            return (_ft(kernel) + _ft(user)) / 1e7
        except Exception:  # noqa: BLE001
            return None

    # ---- POSIX：读 /proc，拿不到就没有 ----
    try:
        pid = proc.pid
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])       # utime + stime
        return ticks / os.sysconf("SC_CLK_TCK")
    except Exception:  # noqa: BLE001
        return None
