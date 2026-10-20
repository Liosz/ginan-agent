"""核心层 —— 与具体领域无关的 agent 内核。

这一包是**与领域无关的 agent 内核**，其中 6 处是**为桌面应用专门做的**改造：

  ① tools.py  —— 工具执行不再读全局注册表（原来传自定义工具集不生效）
  ② tools.py  —— 新增 ToolContext，长任务能报进度、能被取消
  ③ loop.py   —— 新增工具执行前后的钩子，可以拒绝调用
  ④ loop.py   —— 构造 Agent 不再有落盘副作用
  ⑤ provider.py —— 配置变成可注入对象，支持代理和自定义 Key
  ⑥ session.py —— 新增 SessionStore（多会话），去掉核心层里唯一的 print()

============================ 分层铁律 ============================
六层的依赖**只能单向向下**：

    cli / server / desktop  ← 界面层（本包不含，见 ginan_agent/server 等）
            ↓
         loop.py   ④ 循环层
            ↓
      provider.py  ② 模型层     tools.py  ③ 工具层
            ↓                        ↓
        session.py ⑤ 会话层      types.py  ① 契约层

**types.py 不 import 本包任何东西** —— 这是铁律中的铁律。
它是"尺寸标准"，规定了东西长什么样，但自己不参与拼装。

============================ 和领域层的关系 ============================
core/ 完全**不知道 Ginan 是什么**。它只知道"有工具、有模型、有循环"。

Ginan 相关的全部知识在 ginan_agent/ginan/ 里，那一层是纯函数、
不 import 本包 —— 所以它能被独立测试，将来也能被 MCP server 复用。
两边唯一的接触点是 ginan_agent/agent/ginan_tools.py。
"""

from .ask import QuestionBroker
from .loop import Agent
from .provider import Provider, ProviderConfig
from .session import (
    AppRootNotWritable,
    Session,
    SessionMeta,
    SessionStore,
    default_app_root,
    ensure_app_root,
)
from .tools import Tool, ToolContext, ToolDecision, ToolRegistry
from .types import AgentEvent, Message, Reply, ToolCall

__all__ = [
    # 循环层
    "Agent",
    # 模型层
    "Provider",
    "ProviderConfig",
    # 工具层
    "Tool",
    "ToolRegistry",
    "ToolContext",
    "ToolDecision",
    # 提问通道（工具阻塞等用户回答）
    "QuestionBroker",
    # 会话层
    "Session",
    "SessionStore",
    "SessionMeta",
    "default_app_root",
    "ensure_app_root",
    "AppRootNotWritable",
    # 契约层
    "AgentEvent",
    "Message",
    "Reply",
    "ToolCall",
]
