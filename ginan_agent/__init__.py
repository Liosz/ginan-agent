"""Ginan Agent —— 用自然语言驱动 Ginan 做 GNSS 分析的桌面应用。

给完全不懂 GNSS 的人用的。用户说人话，agent 负责查环境、下数据、
生成配置、跑 pea、诊断失败、解读结果。

================================ 顶层包结构 ================================

    ginan_agent/
    ├── core/       agent 内核（与领域无关）
    ├── ginan/      领域层：Ginan 的全部知识，纯函数，不依赖 core
    ├── agent/      把 core 和 ginan 接起来（唯一同时懂两边的层）
    ├── server/     本地 HTTP + SSE 服务
    ├── desktop/    pywebview 原生窗口
    └── frontend/   HTML/CSS/JS 界面

============================ 为什么核心层可以整个换掉 ============================
core/ 里没有任何一行代码知道"Ginan"这个词。
它只知道：有工具、有模型、有循环。

所以将来要把这套内核拿去做别的领域（比如换个工具集做别的科学软件），
复制 core/ 就行，不用动 ginan/。

⚠️ 这里刻意**不 import 子模块**。
   因为 import ginan_agent 会连带触发一大堆依赖（openai、requests 等），
   而 `import ginan_agent.ginan.rinex` 这种只想用一个小功能的场景
   不该被迫加载整个应用。
   需要什么就 import 什么 —— 这是 Python 包该有的样子。
"""

__version__ = "0.1.0"
