"""⑤ 会话层 —— 每个工作目录一份对话历史。

================================ 职责 ================================
  - 把消息列表存到磁盘（JSONL 格式）
  - 下次在同目录启动时读回来

============================ 边界（坚决不做） ============================
- 不修改消息内容（存什么读什么）
- 不参与任何决策（不决定"该不该压缩"，只负责存取）
- 不理解消息的语义（它只管序列化，不管里面写了什么）

============================ 两种模型，服务两种场景 ============================
这个模块里有**两套**存储模型。别把它们混起来看：

  ① `Session(workdir)` —— **工作目录就是身份**

        在 E:/项目A 启动  →  读写 E:/项目A/.ginan-agent/history.jsonl
        在 E:/项目B 启动  →  读写 E:/项目B/.ginan-agent/history.jsonl

     每个目录有它自己那一份对话历史，互不干扰。
     换个目录启动，自然就是全新的对话 —— 因为那里还没有历史文件。

     这个模型的两个好处：
        ① 简单：不需要"会话列表""选会话""会话 ID"这一整套概念
        ② 直觉：历史就在你的工作目录里，看得见、摸得着、删得掉

     想重置？两种方式：
        - 在程序里输入 reset
        - 直接删掉 .ginan-agent 文件夹

  ② `SessionStore` —— **侧边栏里列着好几个会话**（桌面应用用的）

     见文件末尾那一段。它存在的理由，正是 ① 在桌面场景下不成立。

============================ 和 Claude Code 的异同 ============================
行为模型是一样的：**按目录区分对话，在哪个目录启动就接上那个目录的历史。**

但存储位置不同：
    Claude Code  存在全局目录（~/.claude/projects/<按路径编码的名字>/）
    我们         存在工作目录里（<工作目录>/.ginan-agent/）

两种都能实现"按目录区分"，差别在于：
    存工作目录   → 看得见、搬走文件夹历史跟着走、重置就是删文件夹
                   代价：工作目录里多一个 .ginan-agent（git 仓库要加进 .gitignore）
    存全局目录   → 工作目录保持干净
                   代价：看不见摸不着、文件夹改名或移动历史就丢了

我们选了前者，因为"历史属于这个文件夹"是更好理解的心智模型。

============================ 一条边界 ============================
本模块**只做存取**：不判断"该不该压缩"、不改消息内容、不碰 print。

会话管理属于"工程复杂度"，把它关在这一层里，上面的循环层才干净。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .types import Message

# =============================================================================
# 路径约定
# =============================================================================

# 配置目录的名字。前面那个点表示"隐藏目录"（Unix 传统，Windows 也认）。
# 用 .ginan-agent 而不是 sessions，是为了：
#   ① 一看就知道是哪个程序创建的
#   ② 和 .git / .venv / .vscode 一样，是"工具自己用的目录"这种约定
CONFIG_DIR_NAME = ".ginan-agent"
HISTORY_FILE_NAME = "history.jsonl"

# 本应用所有运行时数据的目录名 —— 软件目录下的这个子目录。
#
# ⚠️ 叫 `userdata/` 而不是 `data/` —— 因为项目里已经有一个 `data/`
#    的概念（Ginan 的输入数据目录，在它下面）。两个都叫 data 会混乱。
#
# ⚠️ 这个名字**必须**在 .gitignore 里。用 `userdata/` 这种一眼能看出
#    是"运行时产生的东西"的名字，比 `data/` 更不容易被误提交。
DATA_DIR_NAME = "userdata"

META_FILE_NAME = "meta.json"


def history_path(workdir: Path) -> Path:
    """算出某个工作目录的历史文件路径。

    抽成独立函数而不是塞进类里，是因为**路径规则**可能在多个地方被用到
    （比如将来做"列出所有有历史的目录"这种功能）。
    规则收在一处，改的时候不会漏。
    """
    return workdir / CONFIG_DIR_NAME / HISTORY_FILE_NAME


def default_app_root() -> Path:
    """本应用所有运行时数据的根目录 —— **软件目录下的 `userdata/`**。

    ============================ 只有一个位置 ============================
    软件放在哪，数据就在哪。**没有"模式"、没有优先级、没有退路。**

    ⚠️ 这里原来有三层优先级（环境变量 → 软件目录 → 系统目录）。
       去掉它们是因为用户把要求说得很直白：

           "这个软件真的只是把所有的东西都存到自己软件的安装目录下，
            只是让软件操作的时候别把自己弄坏了。"

       对用户来说，**一个软件 = 一个文件夹**。要备份、要搬走、要删干净，
       都只涉及一个地方。任何"有时候在 A、有时候在 B"的设计都会让他
       找不到自己的东西 —— 而这种困惑**不会以报错的形式出现**，
       只会让他以为数据丢了。

    ⚠️ **为什么不是"相对路径"？**
       相对路径依赖于**进程的当前工作目录** —— 而桌面应用没有
       "当前目录"这个概念（从开始菜单启动时 CWD 可能是任何地方）。
       所以这里算的是**从软件位置出发的绝对路径**，效果一样但可靠得多。

    ⚠️ 和 `locations.py` 里的两个概念分清：
           `project_root()`      ← 软件**代码**在哪
           `default_app_root()`  ← 它下面的 `userdata/`，数据在这
    """
    # locations 是"项目在哪"的唯一出处 —— 见那个模块的说明。
    # ⚠️ 它对 core 是**下层**（只依赖标准库），所以 core import 它不违反分层。
    from ..locations import project_root

    return project_root() / DATA_DIR_NAME


class AppRootNotWritable(RuntimeError):
    """数据目录写不了。

    单独定义一个异常类型，是为了让启动器能**专门**捕获它
    并打印一段"怎么办" —— 而不是混在通用错误里。
    """


def ensure_app_root() -> Path:
    """确保数据目录存在且可写，返回它。写不了就**明确报错**。

    ============================ 写不了就报错，不换地方 ============================
    用户的要求是"别把自己弄坏了"。

    ⚠️ **偷偷换一个地方存，就是把自己弄坏了。**
       用户以为数据在软件目录里，实际在别处 —— 等他要备份、
       要整个搬走的时候才发现那里是空的。**静默换地方比直接报错糟得多。**

       所以这里的选择是：**明确失败**，并且告诉他怎么办。

    ⚠️ **真的写一次来探测**，不是只看权限位。
       权限位在很多情况下不准（ACL、只读挂载、网络盘……），
       而"写一个探针文件再删掉"是唯一可靠的判断。

    ⚠️ 探测文件叫 `.writable` —— 万一因为崩溃留了下来，
       这个名字一眼能看出它是什么，而不是像 `tmp` 那样让人不敢删。
    """
    root = default_app_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".writable"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise AppRootNotWritable(
            f"软件目录不可写，无法保存数据。\n"
            f"    数据目录：{root}\n"
            f"    系统给的原因：{exc}\n"
            f"\n"
            f"  怎么办（任选一个）：\n"
            f"    1. 把整个软件文件夹挪到一个你有写权限的地方\n"
            f"       （比如「文档」或桌面，别放在 C:\\Program Files 下面）\n"
            f"    2. 右键软件文件夹 → 属性 → 取消勾选「只读」\n"
            f"    3. 如果它在 U 盘或网络盘上，确认没被写保护"
        ) from exc
    return root


# =============================================================================
# Session —— 一个工作目录的历史
# =============================================================================


class Session:
    """一个工作目录的对话历史。

    存储格式：JSONL（每行一个完整的 JSON 对象 = 一条消息）

    ============================ 为什么是 JSONL ============================
    因为我们的访问模式是"**只追加，从不修改中间**"：
        每轮对话结束 → 往后追加几条消息

    JSONL（JSON Lines）完美匹配这个模式：
      - 追加是 O(1)：打开文件、写一行、关掉
      - 崩溃只丢最后一行，不会毁掉整个历史
      - 可以用普通文本工具查看（grep / tail）

    对比之下：
        单体 JSON：每存一条都要"全量读 → 改 → 全量写"，O(n)，而且崩溃全丢
        SQLite：能力过剩，还要引入 schema 和 SQL 两套新概念

    > 通用原则：**选数据结构时，先问"我怎么访问它"。**
    """

    def __init__(
        self,
        workdir: Path | None = None,
        path: Path | None = None,
    ) -> None:
        """两种构造方式，二选一：

        · `Session(workdir)` —— 按"工作目录"定位历史
          适合命令行工具（cd 到哪，就是哪份历史）。
          默认取 `Path.cwd()`，也就是**进程启动时的目录**。

        · `Session(path=...)` —— 直接指定历史文件的绝对路径
          桌面应用需要这个：会话不再按目录区分，而是有一份全局的会话列表
          （见下面的 SessionStore）。这时"工作目录"没有意义。

        ⚠️ 注意 `Path.cwd()` 取的是**进程启动时的目录**，
        而不是这个文件所在的目录。这正是命令行场景要的：
        在哪启动，就用哪的历史。
        """
        # 读文件时发现了几行损坏。初始为 0，由 read_all() 更新。
        # 之所以放在实例上而不是返回值里，是为了不改变 read_all 的签名
        # （list[Message] 是很多调用方期待的形状）。
        self.last_skipped_count: int = 0

        if path is not None:
            self.path = Path(path)
            self.workdir = self.path.parent
        else:
            self.workdir = (workdir or Path.cwd()).resolve()
            self.path = history_path(self.workdir)

    # -------------------------------------------------------------------------
    # 读取
    # -------------------------------------------------------------------------

    def read_all(
        self,
        on_corrupt: Callable[[int], None] | None = None,
    ) -> list[Message]:
        """读出全部消息。文件不存在或为空就返回空列表。

        参数 on_corrupt（可选）：发现有损坏行时会被调用，参数是损坏行数。

        ⚠️ 关键设计：**容忍损坏的行**。

        写入可能被打断（断电、Ctrl+C、磁盘满），
        导致最后一行只写了一半，不是合法 JSON：
            {"role": "assistant", "content": "现在是凌晨
                                                  ← 断了

        如果这里直接抛异常，整段历史都读不出来 ——
        为了**一条**坏消息丢掉**整个**对话，代价完全不成比例。

        所以：跳过坏行，继续读剩下的。
        丢一条消息的代价 << 丢掉整个对话的代价。

        ⚠️ 但**不能不吭声** —— 损坏时必须报出来。
        优雅降级 ≠ 静默失败：降级是"继续工作但告诉你出了问题"。

        ⚠️ 注意这里**不用 print()** —— 出了问题就记下来，不往屏幕上吐。

        因为这是核心层，而核心层的第一条铁律就是"不碰输入输出"。
        对命令行程序，print 恰好能被用户看到；
        但对桌面应用，这个模块跑在一个**没有 stdout 的窗口进程**里 ——
        print 出去的东西掉进虚空，用户永远看不到，
        而代码看起来"已经处理了警告"。这是**假的安全感**。

        正确做法是把它变成**返回值**，让调用方决定怎么呈现：
            命令行 → 打一行字
            GUI    → 在对话里显示一条系统提示
            测试   → 断言它确实被报出来了

        同一个信息，三种呈现 —— 这才是分层该有的样子。
        """
        if not self.path.exists():
            return []

        messages: list[Message] = []
        skipped = 0

        # 逐行读，而不是一次性 read_text().splitlines()
        # 好处：文件很大时不用一次全读进内存
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue  # 跳过空行

                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    skipped += 1  # 坏行：跳过，继续

        if skipped:
            # 记在实例上，调用方随时可以查
            self.last_skipped_count = skipped
            if on_corrupt is not None:
                on_corrupt(skipped)

        return messages

    # -------------------------------------------------------------------------
    # 写入
    # -------------------------------------------------------------------------

    def append(self, message: Message) -> None:
        """往历史文件末尾追加一条消息。

        ⚠️ 为什么用追加（"a" 模式）而不是覆盖（"w"）？
        因为覆盖会把已有的内容全删掉。追加只往后加，前面的历史保持不变。

        ⚠️ 为什么要 flush()？
        因为 Python 为了效率会把写入攒在缓冲区里，攒够了才真正落盘。
        如果程序在这之前崩溃，缓冲区里的内容就丢了。
        每写完一条就强制刷新 —— 用一点性能换可靠性，值得。
        """
        # 第一次写入时目录还不存在，先建出来
        # （mkdir 的 exist_ok=True 表示"已存在就别报错"，让这个调用可以重复执行）
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # ensure_ascii=False 让中文按原样写入，而不是转成 \uXXXX 转义。
        # 好处：文件可以直接用文本编辑器看，也可以 grep 中文。
        line = json.dumps(message, ensure_ascii=False)

        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    # -------------------------------------------------------------------------
    # 清空
    # -------------------------------------------------------------------------

    def clear(self, system_prompt: str) -> None:
        """清空历史，只留下系统提示。

        ⚠️ 为什么保留系统提示而不是彻底清空文件？
        因为"历史文件"应该始终是一份**完整可用**的消息列表。
        如果清成空文件，下次读回来是 []，还得靠 Agent 那边补系统提示 ——
        这就把"文件应该长什么样"的知识分散到了两处。

        留一行系统提示，文件自己就是完整的。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            f.write(
                json.dumps({"role": "system", "content": system_prompt},
                           ensure_ascii=False) + "\n"
            )

    # -------------------------------------------------------------------------
    # 启动前检查
    # -------------------------------------------------------------------------

    def prepare(self) -> None:
        """确认这个目录能写历史；不能写就抛出**说人话**的错误。

        ⚠️ 为什么需要这一步？

        因为如果用户指向一个只读目录（C:\\Windows、C:\\Program Files……），
        不做检查的话会在**第一次写入时**炸出一堆 traceback：

            PermissionError: [WinError 5] 拒绝访问。: 'C:\\Windows\\.ginan-agent'
              File "...", line 182, in append
                self.path.parent.mkdir(parents=True, exist_ok=True)

        用户看到的是"Python 崩了"，而不是"这个目录不能写，请换一个"。

        ============================ 两个设计要点 ============================

        ① **尽早失败（fail fast）**
           在启动时就检查，而不是等用户输入完一句话才炸。
           越早发现问题，用户损失越小、越容易理解发生了什么。

        ② **用探针文件，不碰真正的历史文件**
           直接试写 history.jsonl 也行，但那样即使用户只是启动看一眼就退出，
           也会留下一个空的历史文件 —— 下次启动就会显示"已恢复（0 条消息）"，
           语义上不对。
           用一个临时探针文件，测完就删，不产生副作用。
        """
        probe = self.path.parent / ".writable-probe"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 真正地写一次 —— 这比只检查目录权限更准
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"这个目录不能写：{self.workdir}\n"
                f"\n"
                f"  想创建的位置：{self.path.parent}\n"
                f"  系统给的原因：{exc}\n"
                f"\n"
                f"常见情况：\n"
                f"  · 指向了系统保护目录，比如 C:\\Windows、C:\\Program Files\n"
                f"  · 当前用户对这个目录没有写入权限\n"
                f"  · 目录所在磁盘是只读的，或者已经满了\n"
                f"\n"
                f"怎么办：换一个有写入权限的目录。\n"
                f"  例如：run D:\\我的项目\n"
            ) from exc

    # -------------------------------------------------------------------------
    # 元信息
    # -------------------------------------------------------------------------

    def exists(self) -> bool:
        """这个目录有没有历史。"""
        return self.path.exists()

    def count(self) -> int:
        """历史里有多少条消息（用于启动时显示）。

        ⚠️ 为什么单独写一个方法，而不是让调用方 `len(session.read_all())`？
        因为**列表显示只需要一个数字，不需要把整个历史解析出来**。
        文件可能有几百条消息，为了显示"12 条"而全量解析是浪费。

        这里只数非空行 —— 不解析 JSON，快得多。
        （代价是坏行也会被算进去。但对"显示个大概"这个用途，够准了。）
        """
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())


# =============================================================================
# 会话元信息
# =============================================================================


def _now() -> str:
    """当前时间，ISO 格式（**毫秒**精度）。

    用 ISO 字符串而不是时间戳，是为了 meta.json **人能直接读** ——
    调试的时候打开一看就知道是什么时候的会话，不用去换算。

    ⚠️ 为什么是毫秒而不是秒？

    因为会话列表要按"最近更新"排序，而**秒精度会让同一秒内发生的事无法区分**：

        创建一个会话 → 立刻再建一个   → 两个 updated 完全相同 → 排序随机
        改一下标题   → 立刻跑一次任务  → 同上

    对用户来说就是"我刚动过的那个会话居然没排到最前面"。
    毫秒精度把这种同分情况从"日常"降到"几乎不可能"。

    （注意：这只是把问题变小，没有根除 —— 真正要保证顺序时，
      不应该依赖时间戳，而应该显式指定。见 touch() 的 when 参数。）
    """
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass
class SessionMeta:
    """一个会话的元信息。

    ⚠️ 为什么要把"元信息"和"消息历史"分成两个文件？

    因为它们的**读写频率完全不同**：

        meta.json      —— 列出会话列表时要读**全部**会话的元信息
        history.jsonl  —— 只有真正打开某个会话时才读

    如果混在一个文件里，"显示会话列表"就得把每个会话的完整历史都解析一遍。
    20 个会话、每个几百条消息，那就是几万条 JSON —— 只为了显示 20 行标题。

    **按访问模式拆分存储，是比"看起来整齐"更重要的原则。**
    （同一个思路在别处也出现过：Session.count() 只数行数不解析 JSON。）
    """

    id: str
    title: str = ""
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    # 这个会话关联的运行目录（Ginan 每跑一次会有一个工作目录）
    workspace: str | None = None
    # 这个会话里产生过哪些运行（run_id 列表），用于历史页
    run_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "workspace": self.workspace,
            "run_ids": list(self.run_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionMeta":
        """从字典还原。

        ⚠️ 用 data.get(k, 默认值) 而不是 data[k] ——
        这样将来给 SessionMeta 加字段时，**旧文件仍然读得出来**。
        直接下标取值的话，老版本的 meta.json 会因为缺字段而整个读失败。

        这类"向前兼容"的写法，是存储格式演进时的必需品。
        """
        return cls(
            id=data["id"],  # id 是唯一不能缺的，缺了就没意义
            title=data.get("title", ""),
            created=data.get("created", ""),
            updated=data.get("updated", ""),
            workspace=data.get("workspace"),
            run_ids=list(data.get("run_ids") or []),
        )


# =============================================================================
# 会话仓库 —— 桌面应用用的"多个会话"
# =============================================================================
#
# ⚠️ 这一块是**以前做过、后来删掉、现在又加回来**的东西。
#
# 早期版本做过一套多会话（带 ID、能列表、能切换），后来**全删了**，
# 改成"一个工作目录 = 一个对话"。理由是：命令行场景下，
# cd 到不同目录天然就是"切换上下文"，再搞一套会话 ID 纯属概念负担。
#
# 那个决定对**命令行**是对的。但桌面应用不成立 ——
# 因为桌面应用**没有"当前目录"这个概念**（见 default_app_root 的说明）。
# 用户找不到"换个目录就是新对话"这个入口，他需要的是侧边栏里列着
# 自己所有的对话，点哪个进哪个。
#
# 所以这不是"把删掉的东西加回来"，而是**同一个问题在另一个场景下的
# 不同答案**。存储模型要服务于使用模型。
#
# （这也是一个好例子：架构决定是有**适用范围**的。
#   当初写"我们不要多会话"时如果补上一句"如果将来做 GUI 则另说"，
#   今天就不用重新推导一遍了。）


class SessionStore:
    """管理多个会话：创建、列出、打开、删除。

    磁盘布局：

        <app_root>/sessions/<会话id>/meta.json       ← 元信息
        <app_root>/sessions/<会话id>/history.jsonl   ← 消息历史
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_app_root()).resolve()
        self.sessions_dir = self.root / "sessions"

    # -------------------------------------------------------------------------
    # 路径
    # -------------------------------------------------------------------------

    def dir_for(self, session_id: str) -> Path:
        """某个会话的目录。"""
        return self.sessions_dir / session_id

    def meta_path(self, session_id: str) -> Path:
        return self.dir_for(session_id) / META_FILE_NAME

    # -------------------------------------------------------------------------
    # 创建 / 打开
    # -------------------------------------------------------------------------

    def create(
        self,
        title: str = "",
        workspace: str | None = None,
    ) -> SessionMeta:
        """新建一个会话。

        ⚠️ 用 uuid 而不是"第 1 个、第 2 个"这种序号做 ID。

        因为序号会**冲突**：删掉第 2 个之后新建的又叫第 2 个，
        于是旧的文件、旧的日志、旧的 run 记录全都对不上了。
        uuid 保证 ID 永不复用，代价只是长一点 —— 而它本来也是给人看的
        （界面上显示的是 title，不是 id）。
        """
        session_id = uuid.uuid4().hex[:16]
        meta = SessionMeta(
            id=session_id,
            title=title or "新对话",
            workspace=workspace,
        )
        d = self.dir_for(session_id)
        d.mkdir(parents=True, exist_ok=True)
        self._write_meta(meta)
        return meta

    def session(self, session_id: str) -> Session:
        """拿到某个会话的 Session 对象（用于读写消息历史）。

        ⚠️ 直接把 history.jsonl 的**完整路径**传进去（path= 而不是 workdir=）——
        因为桌面应用的会话不在"工作目录"里，
        用 workdir 那套会算出一个错误的位置。
        """
        return Session(path=self.dir_for(session_id) / HISTORY_FILE_NAME)

    def meta(self, session_id: str) -> SessionMeta | None:
        """读一个会话的元信息。不存在或损坏就返回 None。"""
        p = self.meta_path(session_id)
        if not p.exists():
            return None
        try:
            return SessionMeta.from_dict(
                json.loads(p.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, KeyError, OSError):
            # 元信息坏了不该让整个列表打不开 —— 返回 None，
            # 让调用方决定是跳过它还是显示成"损坏的会话"。
            return None

    # -------------------------------------------------------------------------
    # 列出
    # -------------------------------------------------------------------------

    def list_all(self) -> list[SessionMeta]:
        """列出所有会话，**最近更新的排在前面**。

        ⚠️ 用 meta.json 的**修改时间**兜底排序，而不是只用 updated 字段 ——
        因为如果哪天写 meta 的时候崩了，updated 字段可能是空的，
        而"按空字符串排序"会让那个会话莫名其妙沉到底部。
        文件系统的 mtime 总是存在的。
        """
        if not self.sessions_dir.exists():
            return []

        out: list[SessionMeta] = []
        for child in self.sessions_dir.iterdir():
            if not child.is_dir():
                continue
            m = self.meta(child.name)
            if m is not None:
                out.append(m)

        # 排序键：优先 updated 字段，空的话退回文件 mtime
        def sort_key(m: SessionMeta) -> str:
            if m.updated:
                return m.updated
            try:
                return datetime.fromtimestamp(
                    self.meta_path(m.id).stat().st_mtime
                ).isoformat(timespec="seconds")
            except OSError:
                return ""

        out.sort(key=sort_key, reverse=True)
        return out

    # -------------------------------------------------------------------------
    # 更新 / 删除
    # -------------------------------------------------------------------------

    def touch(
        self,
        session_id: str,
        title: str | None = None,
        workspace: str | None = None,
        run_id: str | None = None,
        when: str | None = None,
    ) -> SessionMeta | None:
        """更新一个会话的元信息，顺带刷新 updated 时间。

        ⚠️ 注意 run_id 是**追加**而不是替换 ——
        一个会话可以产生多次运行（用户改了参数重跑），
        历史页要能看到全部，只留最后一次会丢掉信息。
        同时用去重避免同一个 run_id 被记两遍。

        参数 when：显式指定 updated 时间，不传就用当前时间。

        ⚠️ 这个参数是为**测试**而加的，但它解决的是个真问题：
        "验证排序逻辑"不该依赖"两次调用之间时间真的变了"。
        那种测试在同一毫秒内跑完就会随机失败 ——
        而且失败信息看起来像被测代码错了，实际是测试自己不稳。

        > 原则：**要测顺序，就显式造出顺序**，不要指望时钟配合你。
        """
        m = self.meta(session_id)
        if m is None:
            return None

        if title is not None:
            m.title = title
        if workspace is not None:
            m.workspace = workspace
        if run_id is not None and run_id not in m.run_ids:
            m.run_ids.append(run_id)

        m.updated = when or _now()
        self._write_meta(m)
        return m

    def delete(self, session_id: str) -> bool:
        """删掉一个会话（连同它的历史）。删掉了返回 True。

        ⚠️ 只删会话目录，**不删**这个会话产生的运行目录。
        因为运行目录里是用户花了几十分钟下载的数据和跑出来的结果，
        会话只是"聊过什么"的记录。把对话删了顺手把人家的数据也删了，
        是典型的"删除超出预期范围"。
        """
        d = self.dir_for(session_id)
        if not d.exists():
            return False
        for child in d.iterdir():
            if child.is_file():
                child.unlink()
        d.rmdir()
        return True

    # -------------------------------------------------------------------------
    # 内部
    # -------------------------------------------------------------------------

    def _write_meta(self, meta: SessionMeta) -> None:
        """写 meta.json。

        ⚠️ 先写临时文件再 rename —— 这是一个**原子写入**模式。

        如果直接覆盖写 meta.json，而在写到一半时断电/崩溃，
        文件就成了半截 JSON，这个会话的元信息就永久损坏了
        （连标题都读不出来，用户会以为对话丢了）。

        rename 在文件系统层面是原子的：要么看到旧文件，要么看到新文件，
        不存在"看到半个"的中间状态。

        > 凡是"覆盖写一个已经存在的配置文件"，都该考虑这个模式。
        """
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        d = self.dir_for(meta.id)
        d.mkdir(parents=True, exist_ok=True)

        target = self.meta_path(meta.id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(target)
