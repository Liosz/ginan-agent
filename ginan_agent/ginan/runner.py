"""Ginan 领域层 —— 运行 pea。

============================ 这个模块要处理的事 ============================
跑一个外部进程看起来很简单（`subprocess.run` 一行），但**做好**需要处理：

  ① 输出可能很大，而且要**边跑边读**（不能等它结束再一次读）
  ② 用户要能**中途取消**，而且是真的杀掉进程，不是不看
  ③ 要能报进度（解析 "Processed epoch" 行）
  ④ 环境变量要对（OPENBLAS 那个坑）
  ⑤ 失败时要能读懂输出（diagnostics.py 的职责）

============================ ⚠️ 一个照抄来的错误 ============================
GinanUI 的实现是把 stdout 和 stderr **分开读**，stdout 边读边显示，
stderr 攒进一个列表、**等进程结束才一起打印**。

后果：所有错误信息出现在最后，和它相关的上下文早就滚走了。
用户看到的是「先是一堆处理日志，最后突然冒出来一段错误」——
而那段错误说的文件，可能八百行前就已经"正在读取"了。

正确做法：`stderr=subprocess.STDOUT` 合并成一条流。
**顺序本身就是信息。**
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import diagnostics as diag
from .environment import pea_env, process_cpu_seconds


# 默认超时（秒）。一小时。
#
# ⚠️ 为什么这么大？
#    一次完整的 PPP 处理（比如一天的数据）可能要十几分钟，
#    而 POD（定轨）更久。
#    超时设小了会在正常处理的中途把进程杀掉 ——
#    用户看到的是"跑一半没了"，比失败还难理解。
#
#    这个超时的作用是**防止无限挂起**（比如那个 OpenBLAS 的坑），
#    不是用来限制正常处理的。
DEFAULT_TIMEOUT = 3600.0

# 取消后等多久才强杀。
#
# ⚠️ 先 terminate（给它自己收尾的机会：关闭输出文件、清理临时文件），
#    等一会儿再 kill。**立刻 kill 会让输出文件处于半截状态** ——
#    而用户可能已经跑了几十分钟，那些中间结果是有价值的。
GRACE_SECONDS = 5.0


@dataclass
class RunHandle:
    """一次运行的"身份"和位置。"""

    run_id: str
    work_dir: Path
    config_path: Path
    log_path: Path

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id,
            "work_dir": str(self.work_dir),
            "config": str(self.config_path),
            "log": str(self.log_path),
        }


@dataclass
class RunResult:
    """一次运行的结果。"""

    ok: bool
    exit_code: int
    duration: float
    handle: RunHandle
    diagnosis: diag.Diagnosis = field(default_factory=diag.Diagnosis)
    cancelled: bool = False
    timed_out: bool = False
    pos_files: list[Path] = field(default_factory=list)

    def output(self) -> str:
        """这次运行 pea **打出来的全部文字**。

        ⚠️ 它从**日志文件**读，不是从内存 —— `run_pea` 把子进程的每一行
           直接写进 `handle.log_path`（文件只开一次，理由见那里的说明），
           内存里**没有**留一份。所以这个方法是真的读盘，不是取一个字段。

        ⚠️ 为什么会需要它：有些 Ginan 功能**把结果打在 stdout 上**、
           不写任何产物文件 —— `compare_orbits` 就是（它连配置校验都不走，
           直接 `exit()`）。那种功能只能从这里拿结果。

        ⚠️ 读不出来时返回空串，**不抛异常** —— 和 `posfile.parse` 同一个原则：
           "日志读不到"不该让整个流程崩掉。
        """
        try:
            return self.handle.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @property
    def headline(self) -> str:
        """一句话结论。

        ⚠️⚠️ 这里**必须结合退出码**，不能只看诊断。

           踩过一次：跑的进程崩了（退出码 2），但因为输出里有
           "PEA finished"、而崩溃信息又没被任何规则识别，
           诊断给出的结论是"处理正常完成" ——
           **和事实完全相反**。

           一个说"完成了"但实际失败了的结论，比没有结论更糟：
           用户会拿着坏结果去分析。

           所以：**退出码和文字，两个都要看，以退出码为准。**
        """
        if self.cancelled:
            return "已取消。"
        if self.timed_out:
            return f"超时（超过 {DEFAULT_TIMEOUT/60:.0f} 分钟），已停止。"

        if self.ok:
            return self.diagnosis.headline

        # ---- 失败的情况 ----
        if self.diagnosis.errors:
            return self.diagnosis.headline

        # 失败了，但没能从输出里识别出原因 —— 如实说
        if self.exit_code != 0:
            return (
                f"处理失败了（退出码 {self.exit_code}），"
                "但没能从输出里识别出具体原因。"
            )
        return self.diagnosis.headline

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "ok": self.ok,
                "headline": self.headline,
                "exit_code": self.exit_code,
                "duration_seconds": round(self.duration, 1),
                "cancelled": self.cancelled,
                "timed_out": self.timed_out,
                "run": self.handle.to_json(),
                "outputs": [str(p) for p in self.pos_files],
                "diagnosis": json.loads(self.diagnosis.to_json()),
            },
            ensure_ascii=False,
            indent=2,
        )


# =============================================================================
# 运行
# =============================================================================


def _is_python_script(exe: Path) -> bool:
    """这个"可执行文件"其实是个 Python 脚本吗。

    ⚠️ 需要这个判断是为了**假 Ginan**（tests/fakes/pea.py）——
       它是个 .py，得用 python 解释器跑。

       用假 Ginan 的价值：整条流程（读输出、报进度、解析诊断）
       都能在**没有真 Ginan、不下载任何数据、不联网**的情况下测。
    """
    return exe.suffix.lower() == ".py"


# ---- 并行体检的三个阈值（见 `run_pea` 里那一段）----
#
# ⚠️ 是模块级常量，**为了能被守卫测**：守卫拿假 pea 把窗口缩到零点几秒就能验。
CORES_MIN_SECONDS = 40.0
"""至少量这么久（秒）—— 短窗口的读数不可信（I/O、启动开销会带偏）。"""

CORES_MIN_EPOCHS = 5
"""这期间至少跨过这么多个历元 —— 光有时间不够，得有真实的处理量。"""

CORES_MIN_CPU = 4
"""机器少于这么多核就不提这件事：4 核以下，1 个核是正常的。"""

CORES_SUSPICIOUS = 1.5
"""低于这个核数才认为"并行可能没生效"。1.0 是纯单核；留 0.5 的余量，
因为进程会有 I/O 等待，实测占用会略低于真实并行度。"""


def _popen(cmd: list[str], work_dir: Path):
    """起进程。

    ⚠️ Windows 上要开一个新的进程组 —— 否则取消时
       terminate 只杀 pea 自己，它可能已经起了一堆子进程。
    """
    kwargs: dict = {
        "cwd": str(work_dir),
        "env": pea_env(),          # ← OPENBLAS 那个坑在这里堵上
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,   # ← 合并，见模块开头的说明
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",       # ⚠️ Ginan 的输出里可能有非 UTF-8 字节
        "bufsize": 1,              # 行缓冲 —— 否则进度会攒着不出来
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _terminate(proc: subprocess.Popen) -> None:
    """终止进程**及其子进程**。

    ⚠️ `proc.terminate()` 在 Windows 上只杀那一个进程。
       pea 可能起了子进程（比如它内部的下载器），
       那些会变成孤儿继续跑 —— 用户看到"取消了但 CPU 还在满"。

       `taskkill /T` 会连带子树一起杀。

    ⚠️ 全程吞异常：进程可能已经自己退出了（那就没有"终止失败"这回事）。
       在这里抛异常会把"用户点了取消"变成一个错误弹窗。
    """
    if proc.poll() is not None:
        return

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass

    try:
        proc.wait(timeout=GRACE_SECONDS)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def find_pea_for_run(explicit: Path | None = None) -> Path | None:
    """找出该跑哪个 pea。

    ⚠️ 支持 `GINAN_AGENT_FAKE_PEA` 环境变量指向一个假 Ginan。
       这让界面和整条流程能在**离线状态**下开发和演示 ——
       不用等真数据下载，也不用担心把真结果搞坏。
    """
    fake = os.environ.get("GINAN_AGENT_FAKE_PEA", "").strip()
    if fake:
        p = Path(fake)
        return p if p.is_file() else None

    if explicit is not None:
        return Path(explicit) if Path(explicit).is_file() else None

    from . import paths

    return paths.find_pea()


def _not_started(reason: str, what_to_do: str) -> "diag.Diagnosis":
    """把"**根本没跑起来**"翻译成一条说人话的诊断。

    ⚠️⚠️ 原来这两处都是 `diag.diagnose("无法启动 Ginan：…")` ——
       而 `diagnose` 是**从 pea 的输出里认模式**的，喂给它一句我们自己
       编的话，它一条都认不出来，于是结论是笼统的那句默认话：

           「处理的结束状态不明确（没有看到正常的结束标志）。」

       ⚠️ 实测两个都是这个结论：
           diagnose("拒绝执行：…")     → blocking=0 errors=0
           diagnose("无法启动 Ginan：…") → blocking=0 errors=0

       于是用户看到"状态不明确"，而真正的原因是"**程序压根没让它跑**" ——
       两件事的下一步动作完全不同。

    ⚠️ 判据：**自己的话要自己翻译**，不要喂给一个"认别人话"的函数。
    """
    return diag.Diagnosis(diagnostics=[
        diag.Diagnostic(
            severity="error",
            kind="not_started",
            raw=reason,
            what=reason,
            what_to_do=what_to_do,
        )
    ])


def run_pea(
    config_path: Path,
    work_dir: Path,
    *,
    pea: Path | None = None,
    ctx=None,
    timeout: float = DEFAULT_TIMEOUT,
    max_epochs: int | None = None,
    dry_run: bool = False,
) -> RunResult:
    """跑一次 pea。

    ⚠️ **永不抛异常** —— 所有失败都变成 RunResult 里的字段。
       因为这是最外层的一步，崩掉的话用户看到的是"程序挂了"，
       而我们需要给他的是"哪里出了问题、怎么办"。
    """
    config_path = Path(config_path).resolve()
    work_dir = Path(work_dir).resolve()

    # ⚠️⚠️ 这里是**最后一道防线**：绝不让 Ginan 的工作目录落在它自己的安装目录里。
    #
    #    为什么这里最危险？因为 pea 的 RTS 输出文件名是
    #    `<CONFIG>_<RECEIVER>.rts`（**没有目录前缀**）—— 它写在**当前工作目录**里。
    #    另外 `outputs_root` 若是相对路径，也相对 CWD 解析。
    #
    #    所以 CWD 设错 = 直接往 Ginan 目录里写文件。
    #
    #    （实测确认过：跑完之后 `work_dir` 里会多出
    #      `xxx_Network.rts_forward` 之类的文件。）
    #
    # 见 paths.guard_not_in_ginan 的说明 —— 那里还讲了为什么用抛异常而不是返回 False。
    from . import paths as _paths

    work_dir.mkdir(parents=True, exist_ok=True)

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    log_path = work_dir / f"{run_id}.log"
    handle = RunHandle(
        run_id=run_id, work_dir=work_dir,
        config_path=config_path, log_path=log_path,
    )

    # ⚠️⚠️ `guard_not_in_ginan` 是**故意抛 ValueError** 的
    #    （那里的注释解释得很清楚："宁可让程序停下来"），
    #    而这个函数的文档写着「**永不抛异常** —— 所有失败都变成 RunResult 里的字段」。
    #
    #    两个设计各自都说得通，合在一起就是：**`run_pea` 在它明确承诺
    #    不抛的那一种情况下抛了**，而调用方（`do_run_processing`）没有 try ——
    #    用户拿到一个 traceback，而不是一条能读的结论。
    #
    #    👉 兜住它，翻译成 RunResult。**"拒绝执行"也是一种运行结果。**
    #
    #    ⚠️ 位置很要紧：必须放在 `handle` 建好**之后** ——
    #       RunResult 要它，而它要 run_id（那是"这一次运行的编号"）。
    #       我第一版把这段放在最前面，结果 `UnboundLocalError: handle`
    #       ——**我修的那个 bug 是"抛异常"，而我的修法自己抛了另一个异常。**
    try:
        _paths.guard_not_in_ginan(work_dir, "工作目录")
        _paths.guard_not_in_ginan(config_path, "配置文件")
    except ValueError as exc:
        return RunResult(
            ok=False, exit_code=-1, duration=0.0, handle=handle,
            diagnosis=_not_started(
            f"拒绝执行：{exc}",
            "工作目录不能在 Ginan 的安装目录里。把工作目录改到别处，"
            "或者把 pea 挪出这个目录。",
        ),
        )

    exe = find_pea_for_run(pea)
    if exe is None:
        return RunResult(
            ok=False, exit_code=-1, duration=0.0, handle=handle,
            diagnosis=diag.diagnose(""),
        )

    cmd = [str(exe), "--config", str(config_path)]
    if _is_python_script(exe):
        cmd = [sys.executable, "-u", *cmd]
    if dry_run:
        cmd.append("--dry-run")
    if max_epochs is not None:
        cmd += ["-n", str(max_epochs)]

    # ⚠️ 不传 -q（那会隐藏 info），更不传 -Q（那会隐藏 **warning**）。
    #
    #    sanity checker 的警告是 warning 级的 —— 用 -Q 就看不到了。
    #    而"配置被静默改写"恰恰是我们要抓的东西。

    if ctx is not None:
        ctx.progress("正在启动 Ginan…", fraction=None)

    t0 = time.monotonic()
    cancelled = False
    timed_out = False
    lines: list[str] = []
    epochs = 0

    # ---- 并行体检 ----
    #
    # ⚠️⚠️ **这一条是用户点的**：「处理中如果发现只有一个 cpu 工作的情况
    #    记得中断检查」。这个坑在本项目上真的发生过，而且很贵 ——
    #    `pea_env()` 曾经设了 `OPENBLAS_NUM_THREADS=1`，把 Ginan 从 20 核
    #    压成 1 核，一整轮 POD 慢 1.6~1.7 倍，**而没人量过它的代价**。
    #    （`docs/ginan-facts.md` 七点五 ③）
    #
    # ⚠️⚠️ **但这里只"报告"，不"替它决定"，更不打断那一轮。**
    #      · 不打断：一轮 POD 要跑几个小时，光凭"核数少"就杀掉它是**猜**——
    #        有些活本来就该是单核的（小站网的 PPP）。
    #      · 不设环境变量：那是库和 Ginan 的事，不是 app 的事
    #        （见 `environment.pea_env()` 那一段）。发现并说出来，就够了。
    #
    # ⚠️ **判据是「占用核数」，不是「线程数」** —— 数线程会看到 40 个，
    #    而实际只有 1 个核在干活。见 `process_cpu_seconds` 的说明。
    cpu_base: tuple[float, float, int] | None = None   # (累计 CPU 秒, 墙钟, 历元号)
    cores_reported = False

    # ⚠️ 上面那个判断用的是**模块级常量**，不是为了好看 —— 是为了**能被守卫测**。
    #    守卫拿假 pea 跑一遍、把窗口缩到零点几秒，几秒钟就能验完；
    #    写死在循环里的字面量是测不动的（只能靠真跑 40 秒）。
    #    见 `check_toolchain` 的 ⑦f。

    try:
        proc = _popen(cmd, work_dir)
    except OSError as exc:
        return RunResult(
            ok=False, exit_code=-1, duration=0.0, handle=handle,
            diagnosis=_not_started(
                f"无法启动 Ginan：{exc}",
                "多半是 pea 那个文件不在、或者没有可执行权限。"
                "用 `check_environment` 看看它认到的是哪个路径。",
            ),
        )

    # ---- 边读边报进度 ----
    #
    # ⚠️ 逐行读，不用 communicate()。
    #    communicate() 会等进程结束才返回 ——
    #    那样用户在整个处理期间看不到任何进度，
    #    而一次处理可能十几分钟。**「卡住了」和「在跑」的观感差别巨大。**
    #
    # ⚠️⚠️ 但"直接 `for line in proc.stdout`"有个致命问题：
    #    **那是阻塞读**，子进程不输出时它会一直卡在那一行上。
    #
    #    于是循环体里的**取消**和**超时**两个检查**都没有机会执行** ——
    #    实测（子进程只 sleep 8 秒、什么都不打印；timeout=1.0；
    #          0.5 秒时按下取消）：
    #
    #        耗时 8.0 秒  cancelled=False  timed_out=False  ok=True
    #
    #    ⚠️ 而那个超时在 `run_pea` 的文档里写的用途**正是**
    #       「防止无限挂起（比如那个 OpenBLAS 的坑）」——
    #       OpenBLAS 挂起恰好就是**一声不吭**。
    #       **它挡不住它声称要挡的那件事。**
    #
    #    👉 修法：**读数交给一条单独的线程**，主循环只从队列里取；
    #       取不到就等一小会儿，然后**回去再查一次取消和超时**。
    #       这样"检查的节奏"和"子进程有没有输出"彻底解耦。
    #
    #    ⚠️ 为什么不用 select：Windows 上 SelectSelector 只能管**套接字**，
    #       管不了管道；而 Ginan 在 Windows 上才是主要平台。
    # ⚠️⚠️ 日志文件**只开一次**，不要每行一次。
    #
    #    原来写的是 `with log_path.open("a", ...)` 放在逐行的循环体里 ——
    #    真日志（`userdata/runs/…/*.log`）是 **84492 行**，
    #    实测每行 open/close 要 **8.16 秒**，只开一次是 **0.02 秒**。
    #
    #    ⚠️ 而那 8 秒是在**读子进程输出的同一个循环里**花的 ——
    #       等于每次整天运行都白白压着管道 8 秒，
    #       这段时间里进度条不动、日志也不动。
    #
    #    ⚠️ 打开失败不该让整个处理挂掉（日志是辅助，不是主产物），
    #       所以失败就设成 None，下面写的时候跳过。
    log_f = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = log_path.open("a", encoding="utf-8")
    except OSError:
        log_f = None

    try:
        assert proc.stdout is not None
        line_q: queue.Queue[str | None] = queue.Queue()

        def _pump() -> None:
            """把子进程的每一行推进队列。读到 EOF 就推一个 None 收尾。"""
            try:
                for raw in proc.stdout:      # type: ignore[union-attr]
                    line_q.put(raw)
            except Exception:  # noqa: BLE001
                pass
            finally:
                line_q.put(None)

        threading.Thread(target=_pump, daemon=True, name="pea-stdout").start()

        while True:
            # ⚠️ **先查取消和超时** —— 这一步不看子进程有没有说话。
            if ctx is not None and ctx.cancelled:
                cancelled = True
                _terminate(proc)
                break
            if time.monotonic() - t0 > timeout:
                timed_out = True
                _terminate(proc)
                break

            try:
                line = line_q.get(timeout=0.2)
            except queue.Empty:
                continue                     # 0.2 秒没动静，回去再查一遍

            if line is None:                 # 子进程的输出流到头了
                break

            lines.append(line)
            clean = line.rstrip()

            # 写进日志文件（追加式，崩了也能看到跑到哪了）
            #
            # ⚠️ 只开**一次**文件。原来这里是每行一次 open/close ——
            #    真日志 84492 行，实测 8.16 秒，而只开一次是 0.02 秒。
            #    那 8 秒是在**读子进程输出的同一个循环里**花的。
            #    见下面 log_f 的说明。
            if log_f is not None:
                try:
                    log_f.write(line)
                except OSError:
                    pass

            # 进度：Ginan 每处理一个历元打印一行
            #
            # ⚠️⚠️ 只认**前向**那种（带 `#`）。真 pea 一个历元打印**两遍**：
            #
            #        前向解： Processed epoch #1234 - GPS time: 2022 03 01 ...
            #        RTS 反向：Processed epoch - 2022-03-01 00:01:00.01 ...
            #
            #    原来判的是子串 `"Processed epoch" in clean` —— **两种都算**，
            #    于是计数**正好翻倍**。
            #
            #    实测（`-n 60` 的试跑）：
            #        fraction = epochs / max_epochs = 118 / 60 ≈ 1.97
            #      → 前端直接 `Math.round(f*100)+'%'` → **width:197%**，
            #        字面写着"已完成 5760 个历元"，而真实是 2880。
            #
            #    ⚠️ 而且同一个数在 `diagnostics.py` 里**另算了一遍**
            #       （那边只认 `-` 那种，于是少 1）—— 两处对不上。
            #       见 diagnostics._EPOCH 那段说明。
            if "Processed epoch #" in clean:
                epochs += 1
                if ctx is not None and (epochs % 10 == 0 or epochs == 1):
                    ctx.progress(
                        f"处理中… 已完成 {epochs} 个历元",
                        fraction=(epochs / max_epochs) if max_epochs else None,
                    )

                # ---- 并行体检：**只报一次，不打断**（见上面那段说明）----
                if ctx is not None and not cores_reported:
                    cpu_now = process_cpu_seconds(proc)
                    if cpu_now is None:
                        # 这个平台量不了 —— 记成"报过了"，别每个历元重试
                        cores_reported = True
                    elif cpu_base is None:
                        if epochs >= 2:      # 等它进了并行区再取基线
                            cpu_base = (cpu_now, time.monotonic(), epochs)
                    else:
                        dt = time.monotonic() - cpu_base[1]
                        # ⚠️ 样本要够长：**40 秒以上，而且中间跨了至少 5 个历元**。
                        #    短窗口量出来的"核数"会被启动开销和 I/O 等待带偏
                        #    （这也正是本项目在"判卡住"上踩过的那个坑：
                        #     短窗口的读数不可信，见 `ginan-facts` 七点五 ③）。
                        if dt >= CORES_MIN_SECONDS and epochs - cpu_base[2] >= CORES_MIN_EPOCHS:
                            cores = (cpu_now - cpu_base[0]) / dt
                            n_cpu = os.cpu_count() or 1
                            # ⚠️ 只在**这台机器本来有得并行**时才说 ——
                            #    4 核以下的机器上 1 个核是正常的。
                            if cores < CORES_SUSPICIOUS and n_cpu >= CORES_MIN_CPU:
                                ctx.progress(
                                    f"⚠️ 这一轮只用了约 {cores:.1f} 个核"
                                    f"（这台机器有 {n_cpu} 个）—— 并行可能没生效。"
                                    "常见原因是环境变量里设了 "
                                    "OPENBLAS_NUM_THREADS / OMP_NUM_THREADS；"
                                    "本程序不会替你改，跑完值得看一眼。",
                                    fraction=None,
                                )
                            cores_reported = True

        if not cancelled and not timed_out:
            proc.wait(timeout=30)

    except Exception as exc:  # noqa: BLE001
        _terminate(proc)
        lines.append(f"\n[读取输出时出错：{type(exc).__name__}: {exc}]\n")
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        if log_f is not None:
            try:
                log_f.close()
            except OSError:
                pass

    duration = time.monotonic() - t0
    output = "".join(lines)
    diagnosis = diag.diagnose(output)
    exit_code = proc.returncode if proc.returncode is not None else -1

    # ---- 找产物 ----
    pos_files: list[Path] = []
    try:
        pos_files = sorted(work_dir.glob("*.POS")) + sorted(work_dir.glob("*.pos"))
    except OSError:
        pass

    ok = (exit_code == 0) and not cancelled and not timed_out
    # ⚠️ 退出码 0 还不够 —— 必须也**没有 error 级诊断**。
    #    实测过：Ginan 会因为缺文件打印 error 然后... 其实退出码是 1。
    #    但 sanity checker 的 warning 是 0 退出的，所以两个条件都要看。
    if ok and diagnosis.errors:
        ok = False

    result = RunResult(
        ok=ok,
        exit_code=exit_code,
        duration=duration,
        handle=handle,
        diagnosis=diagnosis,
        cancelled=cancelled,
        timed_out=timed_out,
        pos_files=pos_files,
    )

    # ⚠️ 报进度时要用 **result.headline**，不能用 diagnosis.headline。
    #
    #    两者不一样：诊断只看输出文字，而 headline 还看了退出码。
    #    踩过：取消时 diagnosis 说"结束状态不明确"，
    #    而 result 正确地说了"已取消" —— 界面收到的却是前者。
    if ctx is not None:
        ctx.progress(result.headline, fraction=1.0)

    return result
