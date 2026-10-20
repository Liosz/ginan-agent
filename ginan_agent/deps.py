"""依赖清单 —— **唯一的一份**。

============================ 为什么要有这个文件 ============================
在它出现之前，同一份清单在**三个地方**各写了一遍：

    requirements.txt                    pip 装的
    ginan_agent/bootstrap.py            「双击就能跑」自己检查/安装用的
    ginan_agent/ginan/environment.py    界面上「环境自检」显示的那一栏

⚠️⚠️ 三份的结果是：**它们不一致，而且没有任何东西发现。**

    产品代码实际 import 了 **8** 个第三方包，
    而这三份清单**都只有 5 个** ——
    `fastapi` / `uvicorn` / `sse_starlette` / `httpx` 一个都没列。

    后果不是"少装一个可选包"，而是：

        用户在一台干净机器上双击 `run.bat`
        → 启动器检查依赖 → 打印「全部就绪」
        → 主程序**第一行** `ImportError: No module named 'fastapi'`

    **README 里那条「五分钟跑起来」，从来没有在干净机器上成立过。**

⚠️⚠️ 而 `bootstrap.py` 里原来还写着一条注释：

        「将来可以用脚本从 requirements.txt 解析出来，
          但现在只有 5 个，手写反而更清楚、更不容易出错。」

    那句话本身就是这个 bug。
    **「现在只有 5 个」是当时的真话，但没有任何东西在它变成 8 个的时候
      提醒任何人。** 这正是这个仓库反复记的那条 ——
    「现在没问题」不是一种保证，**只有守卫才是**。

所以现在只有这一份，另外两处都从这里 import；
并且 `scripts/check_launcher.py` 会用 AST 扫全仓库，断言
「**代码里真的 import 的第三方包** ⊆ **这张表**」——
那才是真正该守的东西（守"清单和代码一致"，不是守"清单里有什么"）。

============================ 包名 ≠ 导入名 ============================
必须分开写。`python-dotenv` 装完之后是 `import dotenv`，
`pyyaml` 装完是 `import yaml`，`sse-starlette` 装完是 `import sse_starlette`。

只写一个的话，检查逻辑必然是错的 —— 它会去 `import python-dotenv`，
那个名字永远找不到，于是**每个用户都被要求装一遍已经装好的东西**。
"""

from __future__ import annotations

# (包名, 导入名)
#
# ⚠️ 顺序按用途分组，不按字母 —— 这份表是给人读的。
DEPS: list[tuple[str, str]] = [
    # ---- LLM ----
    ("openai", "openai"),
    # ⚠️ `httpx` 在 `core/provider.py` 里被**直接** import（拼代理客户端用）。
    #    它本来是 openai 的传递依赖，装 openai 时会顺带装上 ——
    #    但「碰巧装了」和「声明了」是两件事，而这份清单的意义就是声明。
    #    实测：把它从表里去掉，那条 import 就会让守卫报红（这是对的）。
    ("httpx", "httpx"),
    ("python-dotenv", "dotenv"),

    # ---- 数据与配置 ----
    ("requests", "requests"),
    ("pyyaml", "yaml"),

    # ---- 本地服务 ----
    #
    # ⚠️⚠️ 这三个原来是**注释掉的**，标着「M1 之后按需启用」。
    #    但 M1 就是这套服务本身 —— `server/app.py` 在**模块顶层**就
    #    `from fastapi import ...`，也就是"跑起来的第一个动作"。
    #    它们不是可选路径，是启动的必要条件。
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("sse-starlette", "sse_starlette"),

    # ---- 结果作图（2026-09-29 加的）----
    #
    # ⚠️ 这一组和上面那组是**同一个故事的两半**，值得看完：
    #
    #    `numpy` 原来在这张表里，理由是"解析 .POS 并算 RMS"，
    #    但那时全仓库**一处都没 import 它**（`posfile.py` 用的是标准库的
    #    `math` / `statistics`）—— 于是它被删掉了，删得对：
    #    **留着它的代价不是磁盘，是误导**。
    #
    #    现在它回来了，因为真的用得上了（`series.py`）：算**加权**统计量和
    #    LOWESS 平滑。⚠️ 这不是"当初删错了" —— 是**条件变了**：
    #    当时没有调用者，现在有。
    #
    #    👉 这条也是给下一个人的提醒：这张表要跟着**事实**走，不跟着**计划**走。
    #       计划里的依赖等到真写代码那天再进来（下面 requirements.txt 里
    #       那句被注释掉的 plotly 就是这么躺了很久的）。
    ("numpy", "numpy"),
    ("plotly", "plotly"),
]
