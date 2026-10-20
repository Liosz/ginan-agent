"""④ 循环层 —— agent 的心脏 ★

================================ 职责 ================================
把模型层和工具层粘起来，掌握唯一的决策流程。

这是整个项目**唯一**知道"agent 在干什么"的地方。
去掉它，provider.py 只是个翻译官，tools.py 只是个工具箱 ——
是这一层让它们合起来变成了 agent。

============================ 边界（坚决不做） ============================
- 不碰 input() / print() ← 界面层的事（这条保证了以后能换 Web 界面）
- 不直接发 HTTP ← 模型层的事
- 不关心某个工具具体怎么实现 ← 工具层的事

============================= 循环长什么样 =============================
这是本项目的全部魔法，真的只有这么多：

    消息列表 = [系统提示, 用户说的话]

    一直重复（直到模型不再要工具）:
        回复 = 问模型(消息列表, 工具说明书)     ← 边问边把文字吐给外面
        消息列表.追加(回复的原始消息)          ← ① 记下来

        if 回复里没有工具调用:                  ← ② 判断
            报告最终答案，结束                   ←    没有 → 结束

        for 每个工具调用 in 回复的工具调用列表:   ← ③ 执行
            结果 = 执行工具(名字, 参数)
            报告结果
            消息列表.追加(工具结果消息)          ← ④ 回填

四步：记下来 → 判断 → 执行 → 回填。就这么简单。
所有看着复杂的 agent 框架，内核都是这四步。

    ⚠️ 真正的循环逻辑就那么几十行；一个循环实现里**绝大部分代码**
       是流式处理、并行工具执行、异常路径、以及"取消要能一路传到底"。
    那些都是"让它更稳"，不是"让它成为 agent"。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Iterator

from .provider import Provider, estimate_messages_tokens
from .session import Session
from .ask import QuestionBroker
from .tools import Tool, ToolContext, ToolDecision, ToolRegistry
from .types import (
    AgentEvent,
    Message,
    system_message,
    tool_result_message,
    user_message,
)

# =============================================================================
# 上下文压缩策略（阶段 4B）
# =============================================================================
#
# ============================ 两个阈值，不是一个 ============================
#
# 压缩的触发点（COMPACT_AT）和压缩的目标（COMPACT_TO）**必须不同**。
#
# 为什么？假设只用一个阈值：到 75% 就压，压到 74%。
#
#     75% → 压 → 74% → 聊一句 → 75% → 压 → 74% → ...
#                       ↑ 每次都在压缩！每次都要额外调用一次模型！
#
# 两个阈值就解决了：
#
#     75% → 压 → 35% → 可以聊很久才需要再压
#
# 这个模式叫「迟滞（hysteresis）」—— **开和关的阈值不同**。
# 生活里的例子：空调不是"28 度开、28 度关"（会疯狂启停），
# 而是"28 度开、26 度关"。
#
# ⚠️ 迟滞还有一个附带好处：它对 Prompt Caching 更友好。
#    压低一次之后，历史前缀会稳定很长时间，缓存可以连续命中。
#    （压缩会破坏缓存 —— 见 docs/04-walkthrough.md 4B 概念 5）
#
COMPACT_AT_RATIO = 0.75  # 用到窗口的 75% 就压缩
COMPACT_TO_RATIO = 0.35  # 一次压到 35%，留出充足余量

# 至少保留最近几条消息不压缩。
#
# 为什么要保留？
#   · 最近的对话是**当前语境**，丢了就接不上话
#   · 用户刚说的话、刚给的约束，必须原样在
# 经验值：8 条大致覆盖最近 3~4 轮问答。
COMPACT_KEEP_RECENT = 8

# =============================================================================
# 摘要用的 system prompt
# =============================================================================
#
# ⚠️ 关键：**压缩要用一个完全不同的 system prompt**，不能用 agent 自己的。
#
# 因为 agent 的提示词是"你是一个简洁的助手……"——
# 模型会**尝试回答问题**，而不是做摘要。
#
# 调用同一个模型做不同的事情时，要给它**不同的身份和任务**。
# 这是"一个模型多种用途"的基本做法。
#
# 这段提示词的四条要求，每条都有理由：
#   ① 说清要保留什么   —— 不说的话，模型会按自己的判断丢东西
#   ② "不要添加原文没有的信息" —— 摘要最危险的失败模式是**编造**。
#      模型会把"像是有过"的内容写进去，而下游没人能发现。
#   ③ "第三人称"       —— 避免让摘要看起来像用户或助手说的话
#   ④ 长度限制          —— 没有限制的摘要可能比原文还长，那就白压了
#
COMPACT_SYSTEM_PROMPT = (
    "你的任务是把一段对话记录压缩成简明的摘要。\n"
    "\n"
    "必须保留：\n"
    "- 用户提到的关键事实（姓名、数字、偏好、约束）\n"
    "- 已经做出的决定，以及决定的原因\n"
    "- 未完成的事项、待办、悬而未决的问题\n"
    "- 调用过哪些工具、得到了什么关键结果\n"
    "\n"
    "必须避免：\n"
    "- 不要添加原文里没有的信息。宁可漏，不可编。\n"
    "- 不要评价、不要推测、不要补充建议。\n"
    "\n"
    "用第三人称陈述（'用户询问了……'、'助手回答了……'）。\n"
    "控制在 300 字以内。只输出摘要本身，不要任何前言后语。"
)

# ⚠️⚠️ **这里原来是一个步数上限（`DEFAULT_MAX_STEPS = 10`），现在没有了。**
#
# 记下为什么删掉 —— 因为它删得有代价，理由必须立得住。
#
# ============================ 它在那次真实使用里做了什么 ============================
# 用户跑的那一轮真实的活：
#
#     扫收件箱 → 看本地有什么 → 读 RINEX 头 → 做计划 → 测数据源
#     → 问用户（下不下）→ 下载（第一次，两个文件失败）
#     → 再测数据源 → 问用户（重试吗）→ 下载（第二次，6/6 全成功）
#     → ✂️ **被切在这里**
#
# **下载刚刚成功，就差最后一句总结。** 而那正好是第 10 次模型请求。
# 用户看到的是：干了一堆活，然后什么都不显示。
#
# ⚠️ 关键在"一步"到底是什么：不是"一条消息"，而是
#    **"模型要一次工具 → 工具跑完 → 再问它一次"**这样一个完整来回。
#    做一件真事的来回数，比直觉多得多：
#
#        · 每一步都可能失败要重试（那次下载就重试了两回）
#        · 每次要用户拍板都占一个来回
#        · `probe_data_sources` 要跑几十秒，但它只算一步
#
#    用户的原话很直接，而且他是对的：**"日常使用肯定远超十条"**。
#
# ============================ 那上界呢？ ============================
# 结论：**主循环不该有步数上限**，该有上限的是**时间**（由用户按停止按钮），
# 而不是"跑了多少步"。一个正常的任务本来就可能几十步。
# 也就是说：上界由**用户**给，不由程序猜。
#
# 这个取舍在我们这里同样成立，理由有三条：
#
#   ① 每一轮都是**用户发起的** —— 不点发送，什么都不会跑。
#      "无人看管跑一夜"这个最坏情况压根不成立。
#   ② 每一步**都看得见**（工具卡片一条条往下走），而且随时能点「停止」。
#      取消标志在**每轮开头**都查一次，所以停止是真的有效。
#   ③ 任何我拍脑袋定的数字，迟早会再切错一次 ——
#      而它切错的地方必然是"活干到最后一步"，那正是最不能出错的时候。
#
# 👉 **上界从此是用户，不是常数。** 见 run() 里的取消检查。
#
# ⚠️ 别再把它加回来。真要加，先回答：凭什么认为它比用户的判断更准？


def _discard(_event: AgentEvent) -> None:
    """什么都不做的 emit。

    当调用方没传 emit（命令行、测试、纯内存用法）时用它兜底。
    这样工具里的 `ctx.progress(...)` 永远不需要判断"有没有人在听" ——
    **让调用方适配工具，而不是让工具适配调用方。**
    """
    return None


class Agent:
    """agent 本体。

    它持有对话历史 —— 这正是阶段 1 缺失的那块拼图。
    阶段 1 的 cli.py 每次只发一条 user 消息，所以模型"失忆"。
    现在历史由 Agent 持有并逐轮累积，模型就能记住上下文了。

    ⚠️ 为什么历史由循环层持有，而不是界面层？
       因为"记住什么、记住多少、什么时候该忘"是**业务逻辑**。
       界面层只管显示，不应该管状态。
       这个边界让将来的 Web 界面能复用同一套逻辑，一行都不用改。
    """

    def __init__(
        self,
        provider: Provider,
        system_prompt: str,
        tools: list[Tool],
        session: Session | None = None,
        on_before_tool_call: Callable[[str, str, ToolContext], ToolDecision]
        | None = None,
        cancel: threading.Event | None = None,
        run_dir: Path | None = None,
        ask: QuestionBroker | None = None,
    ) -> None:
        """构造一个 agent。

        ⚠️ 注意这里**不做任何磁盘操作** —— 构造一个 Agent 必须是免费的。

        如果 __init__ 里就去写历史文件（比如"文件是空的就先写一条系统提示"），
        那"仅仅构造一个 Agent"就会在目录里创建文件夹和文件。
        对命令行程序无所谓（你启动它本来就是要用它）；
        但桌面应用会**在渲染窗口时就构造 Agent** ——
        用户开一次程序、什么都不做、关掉，磁盘上就多了个文件夹。
        这类"看一眼就有副作用"的行为在 GUI 里是明确的 bug。

        所以：构造只准备内存状态，真正的读写在第一次 run() 时才发生。
        （scripts/check_core.py 里有一条断言专门守着这个性质：
          "构造一个 Agent 不创建任何文件"。没有测试守着的约束迟早会退化。）

        参数：
            on_before_tool_call —— 工具执行前的钩子，可以拒绝这次调用。
                                   返回 ToolDecision.allow() 或 ToolDecision.reject(理由)。
            cancel              —— 外部取消标志。设上之后，循环会在下一个事件边界停下。
            run_dir             —— 本次运行的工作目录，会传给需要它的长任务工具。
        """
        self._provider = provider
        self._registry = ToolRegistry(tools)
        self._system_prompt = system_prompt
        self._session = session
        self._on_before_tool_call = on_before_tool_call
        self._cancel = cancel or threading.Event()
        self._run_dir = run_dir
        # ⚠️ None 表示"这个宿主没有界面"（命令行、单测）——
        #    那时 `ask_user` 工具会如实告诉模型"没法问，请你直接说"，
        #    而不是傻等一个永远不来的回答。
        self._ask = ask

        # 上次调用模型时，历史里有多少条消息。
        # 用来算"从那之后新增了哪些" —— 配合 Provider 的真实 token 数做估算。
        self._sent_message_count = 0

        # 累计的缓存命中和未命中 token 数。
        #
        # 为什么要**累计**，而不只看最近一次？
        # 因为单次数字会剧烈波动（第一轮必然全不命中，之后才命中）。
        # 累计值才能反映"这段对话整体的缓存效率" ——
        # 而这正是判断"压缩值不值"所需要的依据。
        self._total_cache_hit = 0
        self._total_cache_miss = 0

        # 历史**还没**从磁盘读 —— 推迟到第一次 run()。
        self._loaded = False
        self._needs_initial_write = False
        self._messages: list[Message] = []

    # -------------------------------------------------------------------------
    # 懒加载：把"读磁盘"推迟到真正要用的那一刻
    # -------------------------------------------------------------------------

    def _load(self) -> None:
        """第一次真正要用历史时才从磁盘读。

        ⚠️ 为什么要有 _needs_initial_write 这个标志？

        因为历史文件应该**始终是一份完整可用的消息列表**（见 session.py 的说明）。
        如果第一次对话时我们只是"内存里有系统提示、文件里空着"，
        然后直接追加用户消息，文件里就会缺少系统提示 ——
        下次恢复时读回来的是"没有系统提示的历史"，agent 的人格就丢了。

        但如果我们**立刻**补写系统提示，就退回到"构造即落盘"的老问题。

        解法：推迟到**第一条消息真正要写入时**，先把已存在的补上，再写新的。
        这样既没有构造副作用，文件也始终完整。
        """
        if self._loaded:
            return
        self._loaded = True

        if self._session is not None:
            restored = self._session.read_all()
            if restored:
                self._messages = restored
            else:
                # 文件不存在或为空 —— 这是这个目录的第一次对话
                self._messages = [system_message(self._system_prompt)]
                self._needs_initial_write = True
        else:
            # 没有 session —— 纯内存模式
            #
            # ⚠️ 为什么保留这个模式？两个用处：
            #   ① 测试：不需要碰磁盘就能测历史管理逻辑
            #   ② GUI 的"预览"场景：只想让 agent 说句话，不留痕迹
            #
            # 历史的第一条永远是系统提示。
            # 它不会被清除（reset 时会重建），因为它定义了 agent 的"人格"。
            self._messages = [system_message(self._system_prompt)]

    # -------------------------------------------------------------------------
    # 记录：把"加进内存"和"写进文件"绑成一件事
    # -------------------------------------------------------------------------

    def _record(self, message: Message) -> None:
        """把消息加进历史，并持久化。

        ⚠️ 为什么要有这个方法，而不是到处写 self._messages.append(...)？

        因为"加进内存"和"写进文件"**必须同时发生**。
        如果散在五个地方各写各的，迟早会漏掉一处 ——
        于是内存里有、文件里没有，恢复会话时就丢消息，
        而且这种 bug **极难发现**（因为当前会话看起来完全正常）。

        把它包成一个方法，就只有一处需要维护。
        **把"必须同时发生的操作"绑在一起，是不变量（invariant）的守护方式。**

        这个方法名以 _ 开头，是"内部使用"的约定 ——
        外部不需要知道消息是怎么存的。
        """
        self._load()
        self._messages.append(message)

        if self._session is None:
            return

        # ⚠️ 第一次落盘时，先把之前就在内存里的消息补上。
        # 否则文件里会缺系统提示 —— 下次恢复时 agent 就"失忆"了自己的身份。
        # 详见 _load() 里关于 _needs_initial_write 的说明。
        #
        # ⚠️ 注意切片是 [1:-1]，**首尾都要排除**：
        #       [0]     是系统提示 —— clear() 已经写过了
        #       [-1]    是刚刚追加的这条 —— 下面单独写
        #    写成 [:-1] 会把系统提示写两遍，而且**症状很隐蔽**：
        #    文件看起来能用，只是每次恢复时历史开头多一条 system 消息。
        if self._needs_initial_write:
            self._needs_initial_write = False
            self._session.clear(self._system_prompt)
            for earlier in self._messages[1:-1]:
                self._session.append(earlier)

        self._session.append(message)

    # -------------------------------------------------------------------------
    # 历史管理
    # -------------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        """查看当前历史（返回副本，防止外部乱改）。

        ⚠️ 为什么返回副本而不是原来的列表？
        因为如果我们直接 `return self._messages`，外部就能这样干：
            agent.messages.clear()          ← 把历史清空了！
            agent.messages.append(假的)      ← 伪造历史！
        返回 `list(self._messages)` 复制一份，外部怎么改都不影响内部。

        这类"防止外部破坏内部状态"的做法，是**封装**思想的一部分。
        （注意：这是浅拷贝，只复制列表本身，里面的字典还是共享的。
          对我们的场景够用了。）
        """
        self._load()
        return list(self._messages)

    @property
    def turn_count(self) -> int:
        """已经聊了几轮（不含系统提示）。"""
        self._load()
        return len(self._messages) - 1

    @property
    def cancelled(self) -> bool:
        """外部有没有要求取消。"""
        return self._cancel.is_set()

    def begin_turn(self, cancel: threading.Event | None = None) -> None:
        """开始新的一轮 —— 重置**只属于这一轮**的状态。

        ⚠️⚠️ 这个方法的存在是因为一个真实的 bug，值得记下来：

           服务端原来**每一轮都重建 Agent**。当时的理由是
           "取消标志绑在构造时，复用会把上一轮取消过的状态带进下一轮"。

           **但那句话只考虑了取消标志，忘了 Agent 还持有对话历史。**
           重建 = 历史清空。而当时又没接 session（没落盘），
           于是每轮都是全新的对话 ——

           用户说一句，agent 答一句，**下一句它就不记得上一句了**。

           用户的原话是："没有上下文吗，我说完就忘了。"

        ============================ 根子在哪 ============================
        **把生命周期不同的东西绑在了一起。**

            对话历史  —— 属于「会话」，跨轮、跨重启
            取消标志  —— 属于「这一轮」，每轮必须重置

        正确的做法不是重建（那会把会话也毁掉），
        而是**显式地只重置该重置的那部分**。

        > 遇到"复用会带上旧状态"时，先问一句：
        > **是这东西本来就该是每轮新的，还是我把它的生命周期搞错了？**
        > 重建整个对象是最省事的解法，也最容易连带毁掉不该毁的东西。
        """
        self._cancel = cancel if cancel is not None else threading.Event()

    def cancel(self) -> None:
        """请求取消当前这一轮。

        ⚠️ 这是**请求**，不是"立刻停"。

        为什么不能真的立刻停？因为循环可能正卡在网络请求里。
        从另一个线程强行把生成器关掉会抛
            ValueError: generator already executing
        所以这里只是升起一面旗子，循环在下一个事件边界看到它才停。

        因为 agent 每个文本碎片、每次工具调用之间都会检查，
        用户感受到的仍然是"秒取消"。

        ⚠️ 长任务的**子进程**要靠 ToolContext.cancel 才能真的杀掉 ——
        光靠循环检查打断不了一个跑 10 分钟的外部程序。
        """
        self._cancel.set()

    def context_usage(self) -> dict[str, int]:
        """当前上下文用量 —— 给界面显示用。

        暴露这个数据是刻意的：**上下文用量是隐式状态**，
        用户看不到就会对"为什么突然变慢/变贵/开始压缩"毫无头绪。
        （同一条原则见第 4 讲：凡是影响行为的隐式状态都要显式说出来。）
        """
        return {
            "tokens": self._estimate_tokens(),
            # 窗口大小来自 Provider 的配置，不再是一个模块级常量 ——
            # 换模型时只改一处，不会出现"顶层说 64k、循环层还按旧值算"的不一致。
            "window": self._provider.config.context_window,
            # 最近一次调用的缓存情况
            "cache_hit": self._provider.last_cache_hit_tokens,
            "cache_miss": self._provider.last_cache_miss_tokens,
            # 本次会话累计 —— 这个数字才反映"整体缓存效率"
            "total_cache_hit": self._total_cache_hit,
            "total_cache_miss": self._total_cache_miss,
        }

    @property
    def session(self) -> Session | None:
        """当前的会话对象（可能为 None，表示纯内存模式）。"""
        return self._session

    def reset(self) -> None:
        """清空对话历史，只保留系统提示。

        ⚠️ 如果接了会话文件，**文件也要一起清空** ——
        否则内存清空了、文件里还留着旧历史，
        下次恢复时旧对话又回来了。
        这种"两边不一致"的 bug 特别难查，所以必须一起处理。

        ⚠️ 注意 reset 之后 _needs_initial_write 要清掉 ——
        因为 clear() 已经写了系统提示，文件是完整的，不需要再补。
        """
        self._load()
        self._messages = [system_message(self._system_prompt)]
        self._needs_initial_write = False
        self._cancel.clear()
        if self._session is not None:
            self._session.clear(self._system_prompt)

    # -------------------------------------------------------------------------
    # 上下文预算与压缩
    # -------------------------------------------------------------------------

    def _estimate_tokens(self) -> int:
        """估算当前历史占多少 token。

        ============================ 组合策略 ============================
        **上次的真实值 + 之后新增内容的估算**

        为什么这样组合？（见 docs/04-walkthrough.md 4B 概念 2）

        三种方法各有取舍：
            调 tokenizer 精确算 → 准，但要引入依赖，且各家 tokenizer 不同
            用 API 的 usage     → 真实值，但**事后才知道**（发出去才有）
            按字符数估算        → 免费、随时可用，但不准

        组合起来就取长补短了：
            真实值给出基准（包含了工具说明书、系统提示等我们算不准的部分）
            估算补上"从那之后又新增的消息"

        > 通用手法：**估算给出够用的近似，真实值周期性校准它。**
        > 缓存、监控、限流都在用这个思路。
        """
        if self._provider.last_prompt_tokens is None:
            # 还没调用过模型 —— 只能纯估算
            return estimate_messages_tokens(self._messages)

        # 上次调用时发了多少条，之后新增的就是这些
        new_messages = self._messages[self._sent_message_count:]
        return self._provider.last_prompt_tokens + estimate_messages_tokens(new_messages)

    def _should_compact(self) -> bool:
        """现在该不该压缩？"""
        # 消息太少就没必要压 —— 压缩本身要额外花一次调用的钱
        if len(self._messages) <= COMPACT_KEEP_RECENT + 1:
            return False

        threshold = int(self._provider.config.context_window * COMPACT_AT_RATIO)
        return self._estimate_tokens() >= threshold

    @staticmethod
    def _find_cut_point(messages: list[Message], keep_recent: int) -> int:
        """算出"从哪里开始的消息要保留"。

        ⚠️ 这是压缩里**最容易写错**的一处，所以单独抽成函数方便测试。

        ============================ 为什么不能直接切 ============================
        工具调用在历史里是**成对**出现的：

            assistant(tool_calls=[...])   ← 发起调用
            tool(tool_call_id=...)        ← 返回结果

        如果按"保留最近 N 条"直接切，很可能切在这一对**中间**：

            [要被压掉的] ... assistant(tool_calls)
            [要保留的]   tool(结果) ...      ← 没有父级的孤儿！

        API 会直接报错：你给了一条 tool 消息，但前面没有对应的调用请求。

        ============================ 解决 ============================
        把切点往后挪，直到落在一段 **user 消息** 上。
        user 消息是"回合的自然边界" —— 一个回合从用户说话开始，
        工具调用不可能跨越这个边界。所以切在这儿一定安全。

        ⚠️ 返回 1 表示"一条都不保留"（只有系统提示留在外面）——
        这个值在极端情况下会出现，调用方要能处理。
        """
        keep_from = max(1, len(messages) - keep_recent)
        while keep_from < len(messages) and messages[keep_from]["role"] != "user":
            keep_from += 1
        return keep_from

    @staticmethod
    def _render_for_summary(messages: list[Message]) -> str:
        """把消息列表渲染成纯文本，喂给摘要模型。

        ⚠️ 为什么不直接把 messages 原样发过去？
        因为摘要模型不需要"对话"格式 —— 它只是要读一段记录。
        渲染成纯文本能：
          · 省掉每条消息的格式开销（role 字段等）
          · 让工具调用也变成可读的文字
          · 让模型更容易把它当成"要处理的材料"而不是"正在进行对话"
        """
        lines: list[str] = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content") or ""

            tool_calls = msg.get("tool_calls")
            if tool_calls:
                names = "、".join(c["function"]["name"] for c in tool_calls)
                content = f"{content}（调用了工具：{names}）".strip()

            if content:
                lines.append(f"[{role}] {content}")

        return "\n\n".join(lines)

    def compact_now(self, keep_recent: int | None = None) -> Iterator[AgentEvent]:
        """手动触发一次压缩 —— **不管当前用量多少**。

        存在的理由有两个：
          ① 用户主动想"清一清"，不想等到自动触发
          ② **测试**：不然要聊几十轮才攒够可压缩的内容

        参数 keep_recent 可以覆盖默认的"保留最近几条"，
        调小一点就能在短对话里演示压缩。
        """
        yield from self._compact(keep_recent)

    def compact_threshold(self) -> int:
        """自动压缩的触发线（token 数）。显示给用户看。"""
        return int(self._provider.config.context_window * COMPACT_AT_RATIO)

    def _compact(self, keep_recent: int | None = None) -> Iterator[AgentEvent]:
        """压缩历史：把老对话总结成一段摘要。

        这是个生成器 —— 因为摘要要调模型，可能花几秒，
        期间要往外报告进度（否则用户以为卡死了）。
        """
        # ⚠️⚠️ **先把历史从磁盘读进来**，否则这个方法在"冷"的 Agent 上是
        #     一次**静默空操作** —— 什么也不做，也不报错，连一条事件都没有。
        #
        #     实测（不调这一行时）：新建一个 Agent、直接 `compact_now()`，
        #     拿到的 `[]`（零个事件），历史文件一个字节都没变。
        #
        #     ⚠️ 而 `compact_now` 的 docstring 写着它的两个用途是
        #        「用户主动想清一清」和「**测试**」——
        #        测试那条**必然**是在一个刚构造的 Agent 上调的，
        #        所以它十有八九一直在测空气。
        #
        #     ⚠️ `run()` 里有这一行（所以自动压缩那条路是安全的），
        #        而 `_compact` 被 `compact_now` 直接调到时就漏了。
        #        **懒加载这种东西，只要有一处忘了调，那一处就是错的。**
        self._load()

        keep = COMPACT_KEEP_RECENT if keep_recent is None else keep_recent
        total = len(self._messages)
        tokens_before = self._estimate_tokens()

        # ---------- 决定压缩到哪一刀 ----------
        keep_from = self._find_cut_point(self._messages, keep)

        old_messages = self._messages[1:keep_from]   # 第 0 条是系统提示，永不压缩
        recent = self._messages[keep_from:]

        if not old_messages:
            return  # 没有可压的（比如切点一下子走到了末尾）

        yield {
            "type": "compact_start",
            "message_count": len(old_messages),
            "tokens_before": tokens_before,
        }

        # ---------- 调用模型做摘要 ----------
        transcript = self._render_for_summary(old_messages)

        summary_parts: list[str] = []
        for event in self._provider.chat_stream(
            [
                system_message(COMPACT_SYSTEM_PROMPT),
                user_message(f"请压缩下面这段对话记录：\n\n{transcript}"),
            ]
        ):
            # 我们只要文本。这里**不往外转发** delta 事件 ——
            # 摘要过程不该显示在对话里（用户不需要逐字看摘要生成）。
            if event["type"] == "text_delta":
                summary_parts.append(event["text"])

        summary = "".join(summary_parts).strip()

        # ---------- 重组历史 ----------
        #
        # ⚠️ 用 system 角色而不是 user/assistant。
        # 因为摘要描述的是"之前的背景"，它既不是用户说的话，
        # 也不是助手说的话。硬塞进这两个角色里都是**撒谎**。
        # system 角色最诚实：这是给模型的背景信息。
        summary_message: Message = {
            "role": "system",
            "content": f"以下是更早对话的摘要（原文已压缩）：\n\n{summary}",
        }

        self._messages = [self._messages[0], summary_message, *recent]

        # ⚠️ 必须让 Provider 忘掉上次的用量 —— 那个数字对应的是**旧的历史**。
        # 历史刚刚被换掉，旧数字已经没有意义了。
        # 不清掉的话，下一次估算会基于一个错误的高基数，
        # 导致刚压完就立刻又想压（明明已经空出大半空间）。
        self._provider.last_prompt_tokens = None

        tokens_after = self._estimate_tokens()

        # 同步到磁盘：历史被改写了，内存和文件必须一致
        # （这是 _record() 守护的那个不变量，这里因为要整体替换，
        #   所以单独处理一次）
        if self._session is not None:
            self._session.clear(self._system_prompt)
            # ⚠️⚠️ **从 [1:] 开始** —— `_messages[0]` 就是系统提示，
            #     而 `clear(self._system_prompt)` 已经把它写成第一行了。
            #
            #     写成 `self._messages` 的话，系统提示被**写两遍**。
            #     实测（修之前，压一次）：
            #
            #         ['system', 'system', 'system', 'user', 'assistant']
            #              ↑[0]      ↑[1]      ↑[2] 是摘要（这条该有）
            #                        两行一字不差
            #
            #     ⚠️ 后果不是"多占一点 token"，是**指令被重复发给模型两遍**，
            #        而且**写进了磁盘**（重启后还在），每次请求都带着。
            #
            #     ⚠️ 而同一件事在 `_record()` 里是**写对的**（`self._messages[1:-1]`）
            #        —— 又是一处"一个原则只要有一处不照做，那一处就是全部"。
            for message in self._messages[1:]:
                self._session.append(message)

        yield {
            "type": "compact_end",
            "tokens_after": tokens_after,
            "summary": summary,
        }

    # -------------------------------------------------------------------------
    # 核心：跑一轮
    # -------------------------------------------------------------------------

    def run(
        self,
        user_input: str,
        emit: Callable[[AgentEvent], None] | None = None,
    ) -> Iterator[AgentEvent]:
        """处理一条用户输入，**流式**产出事件。

        ============================ 这是个生成器 ============================
        注意函数体里有 `yield`，所以它是**生成器函数**。

        调用它不会立刻执行任何代码，只是返回一个生成器对象。
        真正开始跑是在你遍历它的时候：

            for event in agent.run("现在几点"):
                #              ↑ 每拿到一个事件就处理一次
                ...

        ============================ 事件有四种 ============================
            text_delta  —— 模型吐出了一小段文字（可能几十上百个）
            tool_start  —— 即将执行工具
            tool_end    —— 工具执行完毕
            turn_end    —— 一轮彻底结束，附带最终答案

        消费方靠 `event["type"]` 区分。见 types.py 的 AgentEvent。

        ============================ 为什么要改成生成器 ============================
        阶段 2 用的是**回调**，阶段 3 加了流式后崩了，两个原因：

          ① 参数爆炸：多一件事就多一个回调参数
          ② 技术死结：Python 里函数内部**不能 yield 出去**，
             回调没法把数据"冒泡"给最外层的渲染代码 ——
             而流式输出恰恰需要"一小片一小片地往外送"

        改成生成器后，每层只负责"产出事件"，不关心谁在消费。

        ============================ ⚠️ 一个生成器特有的坑 ============================
        **函数体在第一次遍历之前不会执行。**

            gen = agent.run("你好")     # ← 这里什么都没发生！
            gen = agent.run("你好")     # ← 又什么都没发生，而且用户消息
                                        #    没被加进历史

        只有开始 `for event in gen` 时，代码才真正跑起来。
        所以「调用 run()」和「遍历 run() 返回的东西」是两件事。

        ============================ 关于 emit 参数 ============================
        `emit` 是一个**旁路出口**，给长任务工具报进度用的。

        为什么进度不能像别的事件一样 yield 出去？

        因为工具执行时，这个生成器**正在执行中**，卡在工具函数里面。
        生成器没法"一边执行一边往外 yield" —— 那是生成器的基本限制。
        所以工具只能通过一个回调，把进度送到别处去。

        ⚠️ 那个"别处"应该汇聚到和事件流**同一个消费者**那里。

        比如桌面应用的做法：
            TurnRunner 从 run() 收事件   →  推进 SSE 队列
            emit 回调被工具调用          →  也推进同一个 SSE 队列
        前端只看到一个 SSE 流，分不出哪条是 yield 出来的、哪条是 emit 出来的。
        **两条路汇聚到一个出口，消费者就不需要知道有两条路。**

        传 None（默认）时，进度事件会被丢弃 —— 命令行/测试场景不需要它们。
        """
        # ⓪ **先报一句"我收到了"。**
        #
        # ⚠️ 必须放在**最前面**，在写历史之前 ——
        #    因为用户从"按下发送"到"看到第一个字"之间，
        #    原来**一个信号都没有**。而那个空档是模型首字延迟 + 网络往返，
        #    几秒到几十秒都可能。用户只能盯着一个没反应的界面猜。
        #
        #    现在这一条事件让前端立刻能显示"正在思考…"。
        yield {"type": "turn_start"}

        # ① 把用户这句话记进历史（内存 + 磁盘）
        self._load()
        self._record(user_message(user_input))

        # ② 进入循环。
        #
        # ⚠️ 这里原来写的是 `for _step in range(max_steps)`，上限 10 ——
        #    而它在一次真实使用里**正好卡在最后一步**（下载刚成功、还没总结），
        #    用户看到的是"干了一堆活然后什么都不显示"。详见文件上方那段说明。
        #
        #    现在是**没有步数上限**的循环。上界由谁给？**用户** ——
        #    见下面每轮开头的取消检查。
        #
        # ⚠️ 所以**取消检查是这个循环唯一的出口条件**（除了模型自己不要工具了）。
        #    删掉它，这个循环就真的出不来了。它不是"顺便检查一下"，
        #    它是**上界本身**。
        while True:
            # ---------- 取消检查（每轮开头一次） ----------
            # 用户点了"停止"就不要再发起新的模型请求了 ——
            # 否则取消之后还会看到一整轮新的回答，很困惑。
            if self._cancel.is_set():
                yield {
                    "type": "turn_end",
                    "text": (
                        "（已停止。之前完成的步骤都保留在历史里，"
                        "可以直接接着说别的。）"
                    ),
                }
                return

            # ---------- 检查上下文预算，必要时先压缩 ----------
            #
            # ⚠️ 为什么放在"问模型"之前，而不是之后？
            # 因为我们要避免**这次请求本身就超限**。
            # 放在之后的话，这次调用已经发出去了，该爆还是爆。
            #
            # 而且这里用的是 `yield from` —— 压缩过程会产出
            # compact_start / compact_end 事件，直接转发给界面。
            if self._should_compact():
                yield from self._compact()

            # ---------- 问模型（并转发它吐出的文字） ----------
            #
            # ⚠️ 这一行是阶段 3 的核心语法：`yield from`
            #
            # 它的意思是：
            #   "把 provider.chat_stream(...) 产出的所有东西**原样转发**出去，
            #    等它结束时，把它的 **return 值**接住赋给 reply。"
            #
            # 一行顶三行：
            #     for event in provider.chat_stream(...): yield event   ← 转发
            #     reply = ...                                            ← 接返回值
            #
            # 这就是"整个链路是生成器"的威力 —— 文本碎片从模型一路
            # 冒泡到终端，中间不需要任何缓存或回调。
            #
            # 发出去之前先记下历史条数 —— 调用返回后 provider 会告诉我们
            # 这次用了多少 token，配合这个条数就能算出"之后新增了多少"。
            self._sent_message_count = len(self._messages)

            reply = yield from self._provider.chat_stream(
                self._messages,
                tools=self._registry.schemas(),
            )

            # 累计缓存统计 —— 在压缩前后对比，就能看出压缩的代价
            self._total_cache_hit += reply.cache_hit_tokens
            self._total_cache_miss += reply.cache_miss_tokens

            # ---------- 记下来 ----------
            # ⚠️ 这一行看起来不起眼，但它是 agent 能工作的关键之一。
            #
            # 为什么必须存回历史？两个原因：
            #   1. 如果模型要求调工具，下一轮请求里**必须有这条 assistant 消息**，
            #      否则 API 会报错 —— "你回了一条 tool 消息，但前面没有对应的调用请求"
            #   2. 就算模型没要求调工具，存下来对话才能连续
            #
            # 存的是 reply.raw（原始消息）而不是我们自己拼的 ——
            # 因为我们不知道 API 期望的完整格式（可能还有我们没注意的字段），
            # 原样奉还最安全。
            self._record(reply.raw)

            # ---------- 判断：要不要继续 ----------
            # 这是个"否命题"，值得多看一眼：
            #   不是"如果该继续就继续"，而是"**如果不该继续就结束**"。
            #   因为"结束"是唯一的出口，默认行为是继续 —— 这正是循环的含义。
            #
            # 判断依据很简单：模型还要调工具吗？
            #   不要 → 它给出答案了，循环结束
            #   还要 → 说明它觉得信息不够，我们得去执行
            if not reply.tool_calls:
                # 报告最终答案。
                # 注意：文字内容其实已经通过 text_delta 事件逐个吐过了，
                # 这里的 text 是**完整版**，给不需要逐字渲染的消费方用
                # （比如自动化测试、脚本调用）。
                yield {"type": "turn_end", "text": reply.text}
                return

            # =================================================================
            # ⚠️ 截断防护（阶段 3B）
            # =================================================================
            #
            # 问题：如果模型因为撞到输出上限（max_tokens）被截断，
            # 它生成的工具调用参数**可能是残缺的**：
            #
            #     {"expression": "1234 * 56      ← JSON 没写完
            #
            # 更麻烦的是：被截断的参数**有可能恰好是合法 JSON**，
            # 于是它能通过我们工具层的所有校验，被执行 ——
            # 得到一个静默的错误结果，谁都不知道出了问题。
            #
            # 处理办法：
            # **把这一批工具调用全部判为失败**，告诉模型"你被截断了，重发"。
            #
            # 宁可失败，不可执行可疑指令。
            #
            if reply.finish_reason == "length":
                truncation_notice = (
                    "错误：上一次回复因为超出输出长度上限被截断，"
                    "工具调用的参数可能不完整，因此**没有执行**。"
                    "请重新发起这个工具调用，并确保参数完整。"
                )
                for call in reply.tool_calls:
                    name = call["function"]["name"]
                    # ⚠️ `status="not_run"` —— 这两条事件是"告诉界面有这么
                    #    一次调用"，**不是"它跑完了"**。
                    #    不带这个标记的话前端会画一个绿勾"完成"，
                    #    而工具根本没执行。**说"完成了"的失败比说"失败了"糟。**
                    yield {
                        "type": "tool_start", "name": name,
                        "call_id": call["id"],
                        "arguments": call["function"]["arguments"],
                    }
                    yield {
                        "type": "tool_end", "name": name,
                        "call_id": call["id"],
                        "result": truncation_notice, "status": "not_run",
                    }
                    # ⚠️ 注意：即使工具没被执行，也**必须**回一条结果消息。
                    # 因为"模型要求调 N 个工具，就必须收到 N 条回复"是硬性要求，
                    # 少一条 API 会直接报错。
                    self._record(tool_result_message(call["id"], truncation_notice))

                # 回到循环开头，让模型重发
                continue

            # ---------- 执行 + 回填 ----------
            # 模型可能要一次调多个工具，所以这里是个循环。
            # 每个调用都要**回一条结果**，一条都不能少。
            for idx, call in enumerate(reply.tool_calls):
                # call 的形状是 ToolCall（见 types.py）：
                #   {"id": "...", "type": "function",
                #    "function": {"name": "...", "arguments": "JSON字符串"}}
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]

                # ---------- 取消检查（每个工具之前） ----------
                # 至少保证"还没开始的工具不再开始"。
                # 已经跑起来的那个要靠它自己的 ToolContext.cancel 去响应。
                if self._cancel.is_set():
                    # ⚠️⚠️ 这里**绝对不能直接 return**！
                    #
                    # 走到这一步时，带 tool_calls 的 assistant 消息**已经记进历史了**
                    # （上面那句 self._record(reply.raw)）。
                    #
                    # 如果这时候一走了之，历史里就留下了：
                    #     assistant(tool_calls=[A, B, C])   ← 声称要调 3 个工具
                    #     （什么回复都没有）                  ← 孤儿！
                    #
                    # 下次请求会被 API 直接拒掉：
                    #     "每个 tool_call 必须有对应的 tool 消息"
                    #
                    # 而且这个 bug **只在用户中途点"停止"时才复现** ——
                    # 平时测试完全正常，一取消就坏，是最难查的那一类。
                    #
                    # 所以：把这一批里**还没处理的每一个**都补一条说明再走。
                    cancel_notice = (
                        "已停止：用户中止了本次运行，这个工具没有执行。"
                        "如果还需要它，请在用户确认后重新调用。"
                    )
                    for pending in reply.tool_calls[idx:]:
                        # ⚠️ `status="cancelled"` —— 用户点了停止，
                        #    工具**没有执行**。不带标记的话前端画绿勾。
                        yield {
                            "type": "tool_end",
                            "name": pending["function"]["name"],
                            "call_id": pending["id"],
                            "result": cancel_notice,
                            "status": "cancelled",
                        }
                        self._record(
                            tool_result_message(pending["id"], cancel_notice)
                        )

                    yield {
                        "type": "turn_end",
                        "text": (
                            "（已停止。之前完成的步骤都保留在历史里，"
                            "可以直接接着说别的。）"
                        ),
                    }
                    return

                # ---------- 构造运行时上下文 ----------
                # 这个对象会被传给声明了 wants_context=True 的工具。
                # 普通工具完全感知不到它存在。
                ctx = ToolContext(
                    call_id=call["id"],
                    name=name,
                    cancel=self._cancel,
                    emit=emit or _discard,
                    run_dir=self._run_dir,
                    ask=self._ask,
                )

                # ---------- 钩子：这次调用要不要真的执行？ ----------
                #
                # ⚠️⚠️ 这里是整个钩子机制最容易写错的地方：
                #      被拒绝时**仍然必须记一条 tool 消息**。
                #
                # 因为 API 的硬性要求是"每个 tool_call 恰好一条回复"，
                # 少一条直接报错。所以拒绝**不是** "continue 跳过"，
                # 而是"回一条说明为什么没执行"。
                #
                # 写错的症状很隐蔽：平时完全正常，只在用户点"拒绝"时才崩。
                if self._on_before_tool_call is not None:
                    decision = self._on_before_tool_call(name, arguments, ctx)
                    if decision.deny:
                        # ⚠️⚠️ **先补一条 tool_start，再发 tool_denied。**
                        #
                        #    原来这里只发 `tool_denied` + `tool_end`，
                        #    而前端是"在 tool_start 时建卡片、后面的事件去更新它"。
                        #    卡片根本没建出来 → **拒绝这件事在界面上什么都不显示**。
                        #
                        #    用户看到的：点了"拒绝"，什么都没发生。
                        #    而实际上工具确实被拦下了。
                        yield {
                            "type": "tool_start", "name": name,
                            "call_id": call["id"], "arguments": arguments,
                        }
                        yield {
                            "type": "tool_denied",
                            "name": name,
                            "call_id": call["id"],
                            "reason": decision.reason,
                        }
                        denial = (
                            "用户拒绝了这次工具调用，它没有被执行。\n"
                            f"原因：{decision.reason}\n"
                            "请换一种不需要用户批准的做法，"
                            "或者停下来把情况告诉用户、问他希望怎么做。"
                        )
                        # 虽然是"没执行"，但 tool_end 和 tool 消息一个都不能少，
                        # 否则要么界面缺一块，要么下一轮请求直接被 API 拒掉。
                        yield {
                            "type": "tool_end", "name": name,
                            "call_id": call["id"],
                            "result": denial, "status": "denied",
                        }
                        self._record(tool_result_message(call["id"], denial))
                        continue

                yield {
                    "type": "tool_start", "name": name,
                    "call_id": call["id"], "arguments": arguments,
                }

                # 真正执行。注意 execute() **永远不抛异常**：
                # 出错时它返回错误字符串，我们照样把它发给模型看。
                # 这样模型能知道"我参数写错了"并重试 —— 这就是"错误即数据"。
                result = self._registry.execute(name, arguments, ctx=ctx)

                # ⚠️⚠️ 状态有**两个**维度，缺一不可：
                #
                #      ① 用户取消了吗
                #      ② 工具自己失败了吗   ← 这个原来是缺的
                #
                #    原来这里只写 `"cancelled" if 取消 else "ok"` ——
                #    于是**抛了异常的工具在界面上是绿勾 +「完成」**，
                #    错误文本只是塞在卡片正文里。
                #
                #    ⚠️ 这正是本文件上面和 app.js 里反复强调那句的反面：
                #       「一个说'完成了'的失败，比说'失败了'糟得多」。
                #       not_run / cancelled / denied 都做到了，唯独
                #       **最常见的"工具报错"**没做到。
                #
                #    `result.ok` 是 `ToolText` 带的 —— 状态在**决定失败的
                #    那一处**（tools.execute）产生，这里只是读出来，不猜。
                if self._cancel.is_set():
                    status = "cancelled"
                elif not result.ok:
                    status = "failed"
                else:
                    status = "ok"

                yield {
                    "type": "tool_end", "name": name,
                    "call_id": call["id"],
                    "result": result,
                    "status": status,
                }

                # 把结果塞回历史。
                # ⚠️ call["id"] 必须原样带上 —— 这是"哪个申请的回复"的对应关系。
                self._record(tool_result_message(call["id"], result))

            # 执行完，回到循环开头，把新历史再问一遍模型。
            #
            # ⚠️ 这里原来是 `for _step in range(max_steps)` 的循环尾，
            #    底下还有一段"安全阀触发"的代码 —— 循环撞上限时告诉用户
            #    "已达到最大步数，说一句继续就能接着做"。
            #    现在没有上限了，那段代码**永远走不到**，所以删掉。
            #    （删掉的不是"保护"，是"一个会切错地方的常数"。）
