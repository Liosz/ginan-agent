"""Ginan 领域层 —— 运行目录：一次解算在磁盘上留下的**全部**东西。

============================ 它是什么 ============================
`userdata/runs/<时间戳>-<4位>/` —— 一次任务一个目录（见 `Workspace.work_dir`）。
里面有四类东西：

    · **记录**   配置 `.yaml`、日志 `.log`、复现包 `repro/manifest.json`
    · **结果**   `.POS` / `.SP3` / `.CLK` / `.OBX`
    · **中间产物** `Filter-*.rts_forward`（RTS 滤波状态）、每站一个的 `.TRACE`

⚠️ 实测（2026-10-02，当时堆了 41 次运行）：合计 **14 GB**，其中

        `.rts_forward`   6.86 GB   ← 8 个文件
        `.TRACE`         6.15 GB   ← 614 个文件
        其余（记录+结果） 0.12 GB   ← 1646 个文件

    ——**99% 是中间产物**。而它们是求解器自己的状态输出：
    Ginan 写完之后，这个程序（和 Ginan 自己）**都不会再读它们**。
    （残差那几张图是**按需重跑**出来的，不是读这些旧文件 ——
      见 `docs/ginan-facts.md` 七点三十。）

============================ ⚠️ 它曾经"只进不出" ============================
在那之前，**没有任何代码删过它**：`runs_dir()` 在全项目只有两种用法 ——
"建目录"（`workspace.work_dir`）和"报路径"（给用户看 / 打开文件夹）。
12 天堆了 14 GB。

而用户在界面上被告知的是：

    新任务 → ⚠️ 会删掉：… 任务状态（计划 / 配置 / **运行记录**）

**那句话是假的。** 收件箱删了、观测数据删了、对话删了 ——
运行目录一个字节都没少。用户 2026-10-02 问的正是这个：

    "每次点新任务，之前解算的产生的中间产物，没用的东西都删除了吗"

👉 所以现在 `clear_runs()` 是**唯一**删它的地方，由 `Hub.reset()` 调用 ——
   "会删掉"那张清单从这一刻起是真的。

============================ 为什么是"整个删" ============================
两条路都想过：**只删中间文件**（保住结果，多留 0.12 GB）vs **整个删**。
选了整个删，两个理由：

  · **用户是这么定的。** 把两条路摆给他看，他选的是「整个运行目录都删」。
  · **"剩一点"比"全删"更难解释。** 如果清完 `runs/` 里还剩 41 个目录、
    每个里面躺着一个 `repro/manifest.json`，用户看到的又是
    「我点了新任务，里边还有东西」——**那正是他 2026-09-21 报过的那个 bug**
    （当时是收件箱里剩了一个删不掉的文件）。

⚠️ 复现包（`repro/manifest.json`）也跟着走：它的用途是"让**这次的**结果
   可复现"，而结果本身都删了 —— 留一份指向已经不存在的文件的 manifest，
   只会让人以为东西还在。

⚠️ 但这个决定有代价，**要如实说**：删完就**真的没了**，
   要再看到那份结果，只能重跑（配置和输入还在数据目录里，
   `docs/ginan-facts.md` 那套复现步骤仍然成立）。

============================ 它和 `clear_inbox` / `clear_observations` 的关系 ============================
三个都是"「新任务」要清掉的东西"，但清的是三个地方：

    收件箱    ← `inbox.clear_inbox()`          用户的投放区
    数据目录   ← `inbox.clear_observations()`   当前任务的作业材料（观测）
    运行目录   ← 这里                           上一次解算的全部产物

`ClearResult` 和 `unlink_with_retry` 从 `inbox.py` import 过来，**不另起一套**：
那两样东西（"删了什么/留了什么的说法"和"Windows 上删不掉要重试"）
是同一件事，写两份迟早对不上 —— 而那份说法是**要拿给用户看的**。
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .inbox import ClearResult, unlink_with_retry


def _size_of(p: Path) -> int:
    """一个文件多少字节。读不到就当 0 —— **不该让整次清理失败**。

    ⚠️ 它只影响"报给用户的那句话里写几 GB"，不影响删不删得掉。
       所以量不准可以忍，抛出去不行。
    """
    try:
        return p.stat().st_size
    except OSError:
        return 0


def clear_runs(root: Path | None = None) -> ClearResult:
    """把**运行目录整个清空**（`userdata/runs/` 下所有东西）。

    参数 `root` 只给测试用 —— 生产路径永远是 `paths.runs_dir()`。

    ⚠️ 这是**删用户文件**，所以判据收得紧：

        · 只删 `runs_dir()` **里面**的东西，目录本身留着
          （它归 `paths.ensure_dirs()` 管，而且删了下次还得建）
        · 每个删掉的都记进 `deleted` / `deleted_dirs`，**由调用方报出去**
        · 删不掉的**带着原因**进 `errors` —— 见 `unlink_with_retry`：
          Windows 上临时占用会让 `unlink()` 失败，而那种占用几秒钟就自己好了。
          **报出来是必须的**（用户能再点一次），静默才是不能接受的。
    """
    root = root if root is not None else paths.runs_dir()

    result = ClearResult(
        deleted_noun="文件",
        kept_noun="没删的",
        nothing_text="没有运行记录",
    )
    if not root.is_dir():
        return result

    for entry in sorted(root.iterdir()):
        if entry.is_file():
            # 正常不会走到这儿（运行目录里只有目录），
            # 但用户手放一个文件进去也不该让整次清理失败。
            size = _size_of(entry)
            ok, why = unlink_with_retry(entry)
            if ok:
                result.deleted.append(entry)
                result.bytes += size
            else:
                result.errors.append(f"{entry.name} —— {why}")
            continue

        if not entry.is_dir():
            continue                      # 链接之类：不碰

        # 先把里面的文件删掉，再删空目录 —— 顺序反了 rmdir 会失败。
        dirs: list[Path] = []
        stack = [entry]
        while stack:
            d = stack.pop()
            dirs.append(d)
            try:
                children = list(d.iterdir())
            except OSError as exc:
                result.errors.append(f"{entry.name}/：读不了（{exc}）")
                break
            for c in children:
                if c.is_dir():
                    stack.append(c)
                elif c.is_file():
                    size = _size_of(c)
                    ok, why = unlink_with_retry(c)
                    if ok:
                        result.deleted.append(c)
                        result.bytes += size
                    else:
                        result.errors.append(f"{c.name} —— {why}")

        # 深的先删 —— 父目录非空时 rmdir 会失败。
        #
        # ⚠️ **只有最外层那个进 `deleted_dirs`。**
        #    里面的子目录照样删，但不单独计数 —— 因为那个数字是**说给用户听的**
        #    （`summary` 里写着"N 个空目录"）：对用户有意义的是
        #    "清掉了几次运行"，不是"连着几层子目录一共撤了几个壳子"。
        #    （实测：一个运行目录里通常还有一两层，全算上会让 41 变成 45 左右。）
        for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
                if d == entry:
                    result.deleted_dirs.append(d)
            except OSError as exc:
                # 里面还有删不掉的文件 → 目录留着，原因报出去
                result.errors.append(f"{d.name}/：撤不掉（{exc}）")

    return result
