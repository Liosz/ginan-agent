"""③ 工具层 —— 工具的注册、查表、校验、执行。

============================ 职责 ================================
一个工具箱 + 一本说明书：
  - 登记"我们有哪些工具"
  - 每个工具的参数要求是什么（JSON Schema）
  - 根据模型的要求，找到对应工具并执行
  - 校验参数是否合法，捕获执行中的异常

============================ 边界（坚决不做） ============================
- 不知道模型是谁、对话到第几轮
- 不允许 print()！工具应该 return 结果，而不是打印

关于"不允许 print"这条：
    工具如果自己 print，结果就只能给人看。
    而 return 出去，结果就能被：CLI 显示 / 写进日志 / 被测试断言 / 送给模型
    行内 print 是把"数据"降级成了"屏幕上的像素"。
"""

from __future__ import annotations

import inspect
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .types import AgentEvent

# =============================================================================
# 数据结构：一个"工具"包含什么
# =============================================================================


@dataclass
class Tool:
    """一个工具 = 元信息 + 参数说明 + 实现函数。

    @dataclass 是装饰器，Python 会自动帮我们生成 __init__ 方法。
    不加它就得手写：
        def __init__(self, name, description, parameters, function):
            self.name = name
            ...
    加了它，下面这四行声明就够了。

    四个基本字段：
        name        —— 工具名。模型用它来"点名"要调用哪个工具
        description —— 给模型看的说明。写得越清楚，模型用得越准
        parameters  —— JSON Schema，描述这个工具要什么参数
        function    —— 真正干活的函数（注意：是函数本身，不是调用结果）

    三个新增字段（Ginan 项目引入），都有默认值，写起来不用管：

        wants_context —— 这个工具要不要拿到"运行上下文"（ToolContext）。

            为什么需要？因为 Ginan 的工具是**长任务** —— 下载几百 MB、
            跑十几分钟的解算。这类工具需要两样东西：
              ① 中途往外报告进度（否则用户盯着转圈不知道死了没）
              ② 能被取消（否则用户只能强杀进程）

            标了 True 的工具，函数签名第一个参数会收到一个 ToolContext，
            里面有 cancel 事件和 emit 回调。其余工具完全不受影响。
            **默认 False 是刻意的**：绝大多数工具不需要这些，
            不该被迫知道"我可能被取消"这种事。

        dangerous —— 这个工具会不会产生**不可逆的副作用**。

            起子进程、写文件、下载数据都算。界面层可以据此加确认弹窗。
            （注意：拦截逻辑在循环层的钩子里，不在这一层 ——
              工具层只负责**声明**，不负责**决定**。）

        max_result_chars —— 这个工具的返回值最多允许多少字符。

            为什么需要？因为工具结果要**塞回对话历史**送给模型。
            一个 GB 级的 .TRACE 文件如果原样返回，会瞬间撑爆上下文窗口。
            超长时按行边界截断，并附上"完整输出在哪"的提示。
            （8_000 是经验值：约 2000 个汉字，不会挤占太多窗口。）
    """

    name: str
    description: str
    parameters: dict[str, Any]
    # Callable[..., str] 的意思是：
    #   "一个可以被调用的东西，接受任意参数，返回字符串"
    # 这里的 ... 表示"参数是什么不重要"，因为我们靠 ** 动态展开
    function: Callable[..., str]
    wants_context: bool = False
    dangerous: bool = False
    max_result_chars: int = 8_000


# =============================================================================
# 工具 1：获取当前时间
# =============================================================================
#
# 为什么选它当第一个工具？
#   1. 无参数 —— 先跑通最简单的路径，不用处理参数解析
#   2. 无副作用 —— 不会改坏任何东西
#   3. 必然成功 —— 出问题时可以确定"不是工具的锅"
#   4. 模型真的不知道现在几点 —— 它的知识有截止日期
#
# 第 4 条最关键：这给了模型一个**真实的、非要调工具不可**的理由。


def get_current_time() -> str:
    """获取当前的日期和时间。

    注意这个函数完全不知道自己会被 AI 调用 ——
    它就是个普通函数，可以被任何代码调用。
    这个"无知"很重要：工具应该能被独立测试。
    """
    now = datetime.now()  # 拿一个 datetime 对象
    # strftime 把时间对象转成字符串，%Y 是四位年，%m 是两位月，以此类推。
    # 完整对照表：https://docs.python.org/zh-cn/3/library/datetime.html#strftime-and-strptime-format-codes
    return now.strftime("%Y-%m-%d %H:%M:%S")


# =============================================================================
# 工具 2：计算器
# =============================================================================
#
# 为什么选它当第二个工具？
#   1. 有参数 —— 可以演示参数解析和 ** 展开
#   2. 演示"结果可验证"的价值
#   3. 天然带一个安全问题 —— 正好用来讲"白名单校验"
#
# ⚠️ 关于第 2 点，一个被实测推翻的常见说法：
#     网上常说"大模型做算术经常出错"。我实测了 4 个算式
#     （包括 1746382 × 62851 这种 12 位数乘法），deepseek-flash
#     在**不给工具**的情况下全部算对。
#
#     所以"模型算不准"是 2023 年前后的过时经验，不能作为选它的理由。
#
#     真正的理由是：**它算对了也验证不了。**
#     模型没有验算机制，这次对不代表下次对，换个模型/版本可能就错。
#     而工具的结果是**可复现、可验证**的 ——
#     这才是数值场景必须用工具的根据，而不是"模型算不准"。
#
#     结论：要给一个工具的正当理由，应该是
#       「模型不可能知道」（实时/私有信息）
#       「模型不可能做到」（有副作用的操作）
#     而不是「模型可能做错」。
#
#     详见 docs/03-notes.md 的「认知修正 #1」。


def calculate(expression: str) -> str:
    """计算一个数学表达式。

    ⚠️ 关于安全：这里用了 eval()，这是 Python 里最危险的函数之一 ——
    它会把字符串当代码执行。如果直接对用户输入用 eval，
    别人可以输入 __import__('os').system('rm -rf /') 删掉你的文件。

    我们的防护是**白名单**：先用正则检查，只允许数字和运算符通过。
    不在白名单里的一律拒绝。

    为什么用白名单而不是黑名单（"禁止 os、import、eval 这些词"）？
        因为黑名单永远列不全。攻击者总有你没想到的写法。
        白名单反过来：只放行已知安全的东西，其他全挡。
        安全领域的基本原则：**白名单优于黑名单**。

    ⚠️ 注意：这个实现是教学版，对我们的场景够安全。
       生产环境应该用专门的表达式解析库（如 simpleeval），而不是 eval。
    """
    # re.fullmatch 要求整个字符串完全匹配（不是部分匹配）。
    # 字符组 [...] 里列出允许出现的字符：
    #   0-9   数字
    #   + - * / ( ) .  运算符和小数点、括号
    #   空格  允许用户写 "1 + 2"
    if not re.fullmatch(r"[0-9+\-*/(). ]+", expression):
        # f-string 里的 !r 表示用 repr() 显示 —— 会带上引号，
        # 这样如果用户输入里有空格或特殊字符，一眼能看出来。
        return f"错误：表达式只能包含数字和 + - * / ( ) . ，收到的是 {expression!r}"

    try:
        # eval 的完整签名是 eval(源码, 全局变量, 局部变量)
        # 传 {"__builtins__": {}} 会**清空所有内置函数** ——
        # 没有 open、没有 import、没有 exec。
        # 这是第二道防线（第一道是上面的白名单）。
        result = eval(expression, {"__builtins__": {}}, {})
    except ZeroDivisionError:
        return "错误：除数不能为零"
    except Exception as exc:
        return f"错误：无法计算（{type(exc).__name__}: {exc}）"

    return str(result)


# =============================================================================
# ⚠️ 这里原本是两个模块级全局变量 ALL_TOOLS / TOOLS_BY_NAME
# =============================================================================
#
# ⚠️⚠️ 这里曾经是模块级全局注册表：
#
#     ALL_TOOLS: list[Tool] = [ ... ]
#     TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in ALL_TOOLS}
#
# 而 Agent 的构造函数**同时**接受一个 tools 参数：
#
#     Agent(provider, prompt, tools=[我的工具])
#
# 看起来是"你可以只挂自己那套工具"，但执行时走的是全局注册表 ——
# 于是产生了一个**极其隐蔽的 bug**：
#
#     模型看得到你的工具（因为我们把 tools 参数发给了它）
#     但工具一旦被调用，execute() 去全局表里查 → 查不到
#     → 返回"没有名为 xxx 的工具"
#
# 症状是"AI 明明知道有这个工具，却总说工具不存在"。
# 这种"声明处和执行处各说各话"的 bug 特别难查，
# 因为两边单独看都对。
#
# ============================ 怎么修 ============================
# **删掉全局变量，把它变成一个类。**
#
# 全局变量的根本问题是：一个进程里只能有一份，且谁都能改。
# 而我们需要的是"每个 Agent 有自己那份"。
#
# 这正是「把隐式全局状态变成显式对象」的经典手法 ——
# 同样的思路在 session.py 里也用过（工作目录从隐式变成显式参数）。
#
# ⚠️ 光删掉还不够，必须留一个**回归测试**（scripts/check_registry.py），
#    断言"传自定义工具集后，它真的能被执行"。
#    没有测试保护的修复，下次重构就会悄悄退回去。


# =============================================================================
# 工具上下文 —— 长任务需要的"操作把手"
# =============================================================================


@dataclass
class ToolContext:
    """工具执行时拿到的运行时上下文。

    只有声明了 `wants_context=True` 的工具才会收到它。
    普通工具（比如算个数、取个时间）完全不需要知道它存在。

    两个核心字段，各自解决一个真实问题：

    ============================ cancel：怎么取消 ============================
    一个可以被外部设置的线程事件标志。

    ⚠️ 为什么不能用 gen.close() 取消？

    因为循环层是个生成器。取消的那一刻，它很可能正卡在
    `stream.__next__()` 里等网络。而对一个**正在执行**的生成器调 close()：

        ValueError: generator already executing

    所以取消必须是**协作式**的：外部设标志，生成器在下次 yield 时自己检查。
    因为 agent 每个文本碎片、每次工具调用之间都会 yield，
    检查间隔足够短，用户感觉是"秒取消"。

    ⚠️ 但光靠协作检查**打断不了子进程**。
    一个跑 10 分钟的 pea 进程不会因为你检查了标志就停下 ——
    长任务工具必须把这个标志一路传到底，真的去 Popen.terminate()。

    ============================ emit：怎么报进度 ============================
    一个往外报告进度的普通回调。

    ⚠️ 为什么是回调，而不是让工具也变成生成器（yield 出去）？

    因为那样工具就得**懂生成器、懂事件流、懂谁在消费** ——
    而工具层的第一条铁律是"它不该知道这些"。

    做成普通回调后，工具里只写：

        ctx.progress("正在下载 CLK 文件", fraction=0.52)

    它完全不知道这句话会变成 SSE 事件推到浏览器、
    还是变成 MCP 的 progress notification。
    **这个无知让同一个工具能原封不动地复用到别的宿主里。**
    """

    call_id: str
    name: str
    cancel: threading.Event
    emit: Callable[[AgentEvent], None]
    run_dir: Path | None = None

    # 提问通道 —— 只有"有界面"的宿主才会给（见 ask_user 的说明）。
    ask: "object | None" = None

    @classmethod
    def bare(cls, name: str = "") -> "ToolContext":
        """造一个"什么都没有"的上下文。

        用途：单独测试某个工具时，不必先搭出一整套循环。
        取消标志永远是"未取消"，进度报出去就掉进虚空。

        ⚠️ 正常运行时**不该走到这里** —— 循环层总是传一个真的进来。
        它存在的意义是让工具**永远不用判断 ctx 是不是 None**，
        少一个分支就少一处可能写错的地方。
        """
        return cls(
            call_id="",
            name=name,
            cancel=threading.Event(),
            emit=lambda _event: None,
        )

    def ask_user(
        self,
        question: str,
        options: list[dict],
        allow_text: bool = True,
        timeout: float | None = None,
    ) -> str | None:
        """问用户一个问题，**阻塞**等他回答。

        返回：
          · 用户选的/写的字符串
          · `None` —— 要么这个宿主**没有界面**（命令行、单测），
                      要么用户中止了这一轮

        ⚠️⚠️ **区分这两种 None 是调用方的责任** —— 但做法很简单：
           工具只要返回一句"没法问，请你直接说 / 用户中止了"，
           模型自己会决定下一步。**别在这里抛异常**：
           工具抛异常会把整轮对话打断，而"问不出来"不是错误，
           只是"这条路走不通，换一条"。

        ⚠️ 为什么返回 None 而不是抛 `NotImplementedError`：
           `ToolContext.bare()` 那条原则是"**工具永远不用判断 ctx 是不是 None**"。
           如果这里抛异常，每个想提问的工具都得写 try/except ——
           那等于把"有没有界面"这件事泄漏到了每个工具里。
           统一返回 None，工具只判断一个**有文档的返回值**。
        """
        if self.ask is None:
            return None
        return self.ask.ask(          # type: ignore[attr-defined]
            call_id=self.call_id,
            question=question,
            options=options,
            allow_text=allow_text,
            cancel=self.cancel,
            emit=self.emit,
            timeout=timeout,
        )

    @property
    def cancelled(self) -> bool:
        """外部有没有要求取消。长任务的循环里该经常检查这个。"""
        return self.cancel.is_set()

    def progress(
        self,
        message: str,
        fraction: float | None = None,
        **data: Any,
    ) -> None:
        """报一句进度。

        fraction 传 None（默认）表示"不知道总量" —— 界面该显示转圈而不是百分比。
        额外的关键字参数会被放进 data 里，给需要展示细节的前端用。
        """
        event: dict[str, Any] = {
            "type": "tool_progress",
            "name": self.name,
            "call_id": self.call_id,
            "fraction": fraction,
            "message": message,
        }
        if data:
            event["data"] = data
        self.emit(event)  # type: ignore[arg-type]


# =============================================================================
# 工具决策 —— 钩子的返回值
# =============================================================================


@dataclass
class ToolDecision:
    """"这次工具调用要不要执行"的答复。

    ⚠️ 为什么不用一个裸布尔值？

    因为拒绝时**必须**带上理由。理由会被原样回给模型，
    让它知道"用户为什么不让我做这件事"，从而换个做法。

    如果只返回 False，模型收到的是"被拒绝了"但不知道为什么 ——
    它很可能**原封不动再试一次**，于是用户被同一个弹窗骚扰三遍。

    带理由的拒绝，才是一次**有信息量的**拒绝。
    （这又是"错误即数据"的同一个思路：
      把它当成一种需要传达的信息，而不是一个需要中断的异常。）
    """

    deny: bool = False
    reason: str = ""

    @classmethod
    def allow(cls) -> "ToolDecision":
        """放行。"""
        return cls()

    @classmethod
    def reject(cls, reason: str) -> "ToolDecision":
        """拒绝，并说明原因。"""
        return cls(deny=True, reason=reason)


# =============================================================================
# 工具注册表 —— 一个 Agent 一份
# =============================================================================


class ToolRegistry:
    """一个工具集，及其查表、说明生成、执行。

    每个 Agent 持有自己的一个实例。这就是修掉上面那个 bug 的关键 ——
    "有哪些工具"从进程级的全局状态，变成了对象级的实例状态。

    设计上刻意保持**不可变**：构造后不再增删工具。
    需要不同工具集就构造不同的注册表，而不是相互修改。
    """

    def __init__(self, tools: list[Tool]) -> None:
        self._tools: list[Tool] = list(tools)
        # 字典推导式：{键: 值 for 元素 in 列表}
        # 读作："对于每个工具 t，生成一条 t.name: t 的记录"
        self._by_name: dict[str, Tool] = {t.name: t for t in self._tools}

        # 重名是个静默的灾难：后一个会覆盖前一个，
        # 而模型调用时永远只会调到后一个，前一个成了"隐形的工具"。
        # 与其将来花几小时查"为什么这个工具没反应"，不如现在就炸。
        if len(self._by_name) != len(self._tools):
            names = [t.name for t in self._tools]
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"工具名重复：{sorted(dupes)}")

    # -------------------------------------------------------------------------
    # 只读视图
    # -------------------------------------------------------------------------

    @property
    def tools(self) -> list[Tool]:
        """当前注册的所有工具（返回副本，防止外部乱改）。"""
        return list(self._tools)

    def names(self) -> list[str]:
        """所有工具名。报错时用来提示"可用的有哪些"。"""
        return list(self._by_name)

    def get(self, name: str) -> Tool | None:
        """按名字取工具，没有就返回 None。"""
        return self._by_name.get(name)

    # -------------------------------------------------------------------------
    # 生成给模型看的说明
    # -------------------------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        """把 Tool 对象转成 API 要求的格式。

        为什么要转换？因为 Tool 里有个 function 字段（Python 函数），
        这个东西**没法序列化成 JSON**，也不可能发给模型。

        API 只想要 name / description / parameters 三样。
        所以这里做一次"脱敏"：把实现藏起来，只交出去说明书。

        这就是**两层数据**的思想：
            内部结构（Tool）   —— 我们自己用，字段丰富
            传输结构（schema） —— 发出去，字段精简、必须能序列化
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools
        ]

    # -------------------------------------------------------------------------
    # 执行
    # -------------------------------------------------------------------------

    def execute(
        self,
        name: str,
        arguments_json: str,
        *,
        ctx: ToolContext | None = None,
    ) -> str:
        """执行一个工具调用，返回结果字符串。

        这个方法是"模型世界"和"Python 世界"之间的关口，所以检查得比较严。

        参数：
            name           —— 工具名，模型给的
            arguments_json —— 参数，模型给的**JSON 字符串**
                              ⚠️ 是字符串不是字典！必须先 json.loads 解析
            ctx            —— 运行时上下文。**由调用方构造好传进来**。
                              只有声明了 wants_context=True 的工具会收到它。

        ⚠️ 为什么 ctx 是从外面传进来的，而不是在这里构造？

        因为构造它需要的三样东西 —— `call_id`、`cancel`、`emit` ——
        **只有循环层知道**：
            · call_id  来自模型回复里的那次工具调用
            · cancel   是这一轮的总开关
            · emit     要接到界面的事件队列上

        如果这里自己造一个，就必然造出一个**缺了 call_id 的空壳**：
        工具报出去的进度事件找不到自己的归属，前端也分不清
        哪条进度属于哪次调用（模型一次可以要求调好几个工具）。

        > 这也是「声明处与执行处不一致」那个老毛病的轻量版：
        > **同一个东西在两处构造，就一定有一处是错的。**
        > 解法不是让两处对齐，而是让它只在一处出现。

        ⚠️ 关键设计：这个方法**永远不抛异常**，出错时返回错误信息字符串。

        为什么？因为报错信息要**发给模型看**，让它知道哪里错了、然后改正。
        如果这里抛异常，程序就崩了，模型永远得不到反馈，
        用户看到的就是"程序挂了"而不是"AI 说它参数写错了，重试了一次"。

        这个模式叫「错误即数据」：把错误当成一种正常结果传递，而不是异常中断。
        """
        # ---------- ① 工具存在吗？ ----------
        tool = self._by_name.get(name)
        if tool is None:
            available = ", ".join(self._by_name)
            return ToolText(
                f"错误：没有名为 {name!r} 的工具。可用的工具有：{available}",
                ok=False,
            )

        # ---------- ② 参数是合法 JSON 吗？ ----------
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            return ToolText(
                f"错误：参数不是合法的 JSON（{exc}）。收到的内容：{arguments_json!r}",
                ok=False,
            )

        # ---------- ③ 参数是个"对象"吗？ ----------
        # JSON 的顶层可以是对象 {}、数组 []、字符串、数字……
        # 但工具的**参数必须是一个对象**，因为我们要用 ** 展开成关键字参数。
        # 如果是数组，** 展开会报错。
        if not isinstance(arguments, dict):
            return ToolText(
                f"错误：参数应该是一个 JSON 对象（花括号），"
                f"收到的是 {type(arguments).__name__}",
                ok=False,
            )

        # ---------- ④ 需要上下文的工具，把上下文塞进去 ----------
        #
        # 这就是 wants_context 的兑现处。注意参数名固定叫 ctx，
        # 工具的签名要写成 def mytool(ctx, 其他参数...) ——
        # 位置参数而不是关键字参数，避免和模型给的参数名撞车。
        #
        # ⚠️ ctx 是调用方传进来的，这里只负责塞进去，绝不自己构造。
        #    没传时给一个"什么都没有"的兜底，让工具不用判断 None。
        if tool.wants_context:
            arguments = {"ctx": ctx or ToolContext.bare(name=name), **arguments}

        # ---------- ⑤ 执行 ----------
        #
        # ⚠️⚠️ 绑定参数和**调用函数体**要分开判 —— 否则函数体里自己抛的
        #     `TypeError` 会被报成"参数不匹配"，而那是**发给模型看的**，
        #    模型的行为就是"那我改改参数再试" —— 参数本来就是对的，
        #    于是它会一直重试。
        try:
            inspect.signature(tool.function).bind(**arguments)
        except TypeError as exc:
            expected = ", ".join(tool.parameters.get("properties", {}))
            return ToolText(
                f"错误：参数不匹配（{exc}）。{tool.name} 需要的参数是：{expected}",
                ok=False,
            )

        try:
            raw = tool.function(**arguments)
        except Exception as exc:
            # 兜底：工具内部出了别的错
            return ToolText(
                f"错误：工具执行失败（{type(exc).__name__}: {exc}）", ok=False
            )

        # ---------- ⑥ 结果必须是文本 ----------
        #
        # ⚠️⚠️ 这里原来直接把它交给 `_truncate`，而 `_truncate` 第一行是
        #    `len(text)` —— 工具**忘了 return**（返回 None）时它抛 TypeError。
        #
        #    而那句 TypeError 是在上面那两个 except **之外**抛的，
        #    于是它一路穿出 `execute()`、穿出循环层 ——
        #    而此时"带 tool_calls 的 assistant 消息"已经落盘、
        #    "tool 回复"还没补上 → 历史里留下**孤儿 tool_call** →
        #    **之后每一次请求都会被 API 拒掉**，对话等于废了。
        #
        #    👉 所以"工具返回了不是字符串的东西"必须**当成一种普通的
        #       工具失败**报出去（错误即数据），而不是让异常逃出去。
        if not isinstance(raw, str):
            return ToolText(
                f"错误：工具 {tool.name} 返回了 {type(raw).__name__}，"
                f"而工具只能返回字符串。"
                f"（这多半是工具自己忘了写 return —— 请把它当作工具故障，"
                f"不要重试。）",
                ok=False,
            )

        # ---------- ⑦ 截断超长结果 ----------
        return ToolText(_truncate(raw, tool.max_result_chars, tool.name), ok=True)


# =============================================================================
# 工具结果 —— 一个字符串，但**自带"这次成功了吗"**
# =============================================================================


class ToolText(str):
    """工具返回的文本。**它就是一个 `str`**，只是多带一个 `ok`。

    ============================ ⚠️ 为什么需要它 ============================
    原来 `execute()` 只返回一个字符串，五处失败都靠 `"错误："` 这个前缀
    来区分。于是**循环层根本不知道这次调用失败了**：

        yield {"type": "tool_end", ..., "status": "cancelled" if 取消 else "ok"}

    它只有"取消了没有"这一个维度。**"工具报错了"这个维度不存在** ——
    所以一个抛了 `RuntimeError("下载失败")` 的工具，在界面上是
    **绿勾 + 「完成」**，错误文本只是塞在卡片正文里。

    ⚠️ 这正是本仓库反复说的那句话的反面：
       **「一个说'完成了'的失败，比说'失败了'糟得多」**
       （`app.js` 和 `types.py` 里都写着这句）。
       `not_run` / `cancelled` / `denied` 三种都做到了，唯独
       **最常见的"工具报错"**没做到。

    ============================ 为什么是 str 的子类 ============================
    ⚠️ 因为**状态必须在"决定失败"的那一处产生**，不能再让下游去猜。

       可下游（循环层、二十来条自检断言）用的都是字符串操作：
       `startswith("错误")`、`== "来自B"`、`json.loads(...)`、`len(...)`。
       换成一个新的结果对象 → 这二十处全要改，而且**每改一处就多一次
       可能改错的机会**。

       `str` 的子类两边都满足：字符串那一面**完全不变**，
       需要状态的地方读 `.ok` 就行。

    ⚠️ 它**不是** `str + bool` 的拼装玩具：`ok` 只在 `execute()` 里被赋值，
       而且只在一个地方赋值（失败那五个分支 + 成功那一个分支）。
       这就是"让它只在一处出现"。
    """

    ok: bool

    def __new__(cls, text: str, ok: bool = True) -> "ToolText":
        obj = super().__new__(cls, text)
        obj.ok = ok
        return obj


# =============================================================================
# 超长结果截断
# =============================================================================
#
# ⚠️ 为什么必须有这个？因为工具结果要**塞回对话历史**，每轮都重发。
#
# Ginan 的 .TRACE 文件可以到 GB 级。如果某个工具不小心把它读进来返回，
# 后果不是"慢"，而是**上下文窗口瞬间爆掉、对话直接不可用**。
#
# 所以每个工具声明自己能返回多长，超了就在这里截断。
# 一定要在**这一层**做，而不是指望每个工具作者都记得自己截 ——
# 安全网要放在必经之路上。
#
# ⚠️ 按行边界截断，不是按字符数硬切。
# 硬切会把最后一行切成半截，读者（模型）会以为文件就这样。


def _truncate(text: str, limit: int, tool_name: str) -> str:
    """把超长文本按行边界截断，并说明被截了多少。"""
    if len(text) <= limit:
        return text

    kept = text[:limit]
    # rfind 找最后一个换行 —— 保证不切断行
    cut = kept.rfind("\n")
    if cut > 0:
        kept = kept[:cut]

    dropped = len(text) - len(kept)
    # ⚠️ 必须告诉模型"这不是全部" ——
    # 否则它会基于残缺数据下结论，而且完全不知道自己漏了东西。
    return (
        f"{kept}\n"
        f"… [输出被截断：还有 {dropped:,} 个字符未显示。"
        f"工具 {tool_name} 的完整输出请从文件读取，或缩小查询范围后重试。]"
    )


# =============================================================================
# 默认工具集 —— 给自检脚本和最小示例用
# =============================================================================
#
# ⚠️ 注意这里是个**函数**，不是模块级变量。
# 每次调用都返回一个全新的注册表，互不影响 ——
# 这正是不再需要全局变量的意义。


def default_registry() -> ToolRegistry:
    """构造一个只有演示工具的注册表（取时间 + 计算器）。

    Ginan Agent 实际运行时用的是 ginan_agent.agent.ginan_tools 里那套；
    这两个留在这里是为了让核心层能独立自检 ——
    不需要 Ginan、不需要网络、不需要 API Key。
    """
    return ToolRegistry(
        [
            Tool(
                name="get_current_time",
                # description 是**写给模型看的**，不是写给人看的。
                # 说清楚"什么时候该用它"比说清楚"它是什么"更有用。
                description=(
                    "获取当前的日期和时间。"
                    "当用户问现在几点、今天几号、今天星期几时使用。"
                ),
                parameters={
                    "type": "object",
                    "properties": {},  # 没有参数，所以是空字典
                    "required": [],
                },
                function=get_current_time,  # ⚠️ 注意：没有括号！传的是函数本身
            ),
            Tool(
                name="calculate",
                description=(
                    "计算数学表达式并返回精确结果。"
                    "当需要做算术运算时使用，不要自己心算 —— 你容易算错。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "要计算的数学表达式，例如 '(1+2)*3' 或 '100/7'",
                        }
                    },
                    "required": ["expression"],  # expression 是必填的
                },
                function=calculate,
            ),
        ]
    )


# =============================================================================
# 说明：原来这里的 tool_schemas() 和 execute() 两个模块级函数
# =============================================================================
#
# 它们已经被收进上面的 ToolRegistry 类里，成为两个方法：
#
#     tool_schemas(tools)          →  registry.schemas()
#     execute(name, arguments)     →  registry.execute(name, arguments, ...)
#
# 为什么必须搬进类里？因为它们原来读的是模块级全局 TOOLS_BY_NAME ——
# 而那个全局变量正是"传进来的工具集不生效"这个 bug 的根源。
#
# 函数体本身**几乎没变**（错误即数据、白名单校验、JSON 检查全保留了）。
# 变的只有一件事：**它从哪儿查工具**。
#
# 这本身就说明了一个道理：
#   **很多"重构"不是重写逻辑，而是换掉逻辑所依赖的状态来源。**
#   从"进程级全局"换成"对象级实例"，行为没变，正确性变了。
