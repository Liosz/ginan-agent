"""本地服务 —— HTTP + SSE。

============================ 架构：三条线程 ============================
   主线程        pywebview 窗口（将来）
   服务线程      uvicorn，跑在 127.0.0.1 的随机端口
   工作线程      跑 agent.run()，一轮一个

为什么必须有工作线程？

因为 agent 的内核是**全同步**的：它阻塞在 HTTP 请求上、
阻塞在 time.sleep 的重试里、阻塞在工具执行中。
如果直接在服务线程里跑，整个服务会卡住 —— 连"我在干活"这个事件都发不出去。

⚠️ 但事件**不能**从工作线程直接 yield 给 SSE 流。
   生成器是单线程的东西，跨线程 yield 会炸。

   解法：工作线程把事件推进一个 **queue.Queue**（它本身就是线程安全的），
   SSE 那边从队列里取。两条线程通过队列解耦。

============================ 两条产出路径汇聚到一个出口 ============================
    agent.run()  yield 出来的事件  ─┐
                                    ├─→  同一个 queue  →  SSE  →  浏览器
    ToolContext.emit 回调推的事件  ─┘

工具报的进度（tool_progress）走的是 emit 那条路，因为它是在
生成器**正在执行中**产生的，没法 yield 出来（见 core/loop.py 的说明）。

但两条路都进同一个队列，所以前端只看到一条事件流，
分不出哪条是 yield 的、哪条是 emit 的。**消费者不需要知道有两条路。**

============================ 安全 ============================
这个服务能起子进程、能下载文件、能读写用户的磁盘。
任何用户访问的网页都可能去戳它（DNS rebinding / CSRF）。

所以三道防护：
  ① 只绑 127.0.0.1 —— 外网根本连不到
  ② 每次启动生成随机 token，所有接口都要校验
  ③ 校验 Host 头，防 DNS rebinding

⚠️ 绝不能省。这不是"本地服务可以随便"的场景 ——
   一个能起子进程的本地 HTTP 服务，是攻击面很大的东西。
"""

from __future__ import annotations

import asyncio
import json
import queue
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agent.ginan_tools import build_ginan_tools
from ..agent.prompts import SYSTEM_PROMPT

# ⚠️ **模块级导入**，不要挪进 `Hub.__init__` 里。
#
#    原来只在 `__init__` 内部 import 了一次，于是 `Workspace` 只是那个
#    函数的**局部名字** —— `Hub.reset()` 里写 `Workspace()` 直接
#    `NameError`，而它在 HTTP 层表现为 **500**。
#
#    👉 `/api/reset`（界面上的「新任务」）**一直是坏的**。
#       用户点它只会看到一个报错，然后前端照样 reload 页面 ——
#       看起来像"重置了"，其实什么都没发生。
#
#    **在函数里 import，作用域就只到那个函数为止** ——
#    这个坑在同一个文件里还踩过一次（见 `_guard` 那条注释）。
from ..agent.workspace import Workspace
from ..core import Agent, Provider, ProviderConfig, QuestionBroker
from ..ginan import corrections as gcorrections
from ..ginan import config as gconfig
from ..ginan import environment, paths, probe
from ..ginan.net import ProxyConfig

# 前端资源目录
#
# ⚠️ 从 locations 取，不在本地再算一次 ——
#    "项目在哪"只该有一处定义。两处各算各的，迟早会不一致。
from ..locations import frontend_dir as _frontend_dir

FRONTEND_DIR = _frontend_dir()

# 队列里的"这一轮结束了"标记
#
# ⚠️ 用哨兵值而不是 "None 表示结束"，因为 None 也可能是合法的
#    队列内容（虽然现在不是）。哨兵能让"结束"这件事**显式**。
#    用一个谁也造不出来的对象，杜绝了"某个真事件长得像哨兵"的可能。
TURN_DONE = object()

# 用户点过「获取列表」之后，最近一次查到的模型名。
#
# ⚠️⚠️ **只活在这次运行的内存里，不落盘。**
#    理由是 `settings.py` 里那条仍然成立：存下来的列表会变成"上次的"，
#    服务端换了模型它还是旧的，反而误导。
#    它要解决的只是"关掉设置再打开，下拉框别空着"。
#
# ⚠️ 它不能放在 `Settings` 里 —— `load_settings()` 每次都从磁盘重读，
#    在内存里改一个 dataclass 实例，下一次读就没了（那正是原来那个字段
#    成了死字段的原因之一）。
_known_models: list[str] = []


def load_settings_for_creds():
    """读设置的快捷方式 —— 凭据验证要用到代理配置。"""
    from ..settings import load_settings

    return load_settings()


# =============================================================================
# 日志
# =============================================================================
#
# ⚠️⚠️ **这个程序原来一行日志都不写。**
#
#     `userdata/logs/` 这个目录建了、`ensure_dirs()` 每次都创建它 ——
#     然后**从来没往里写过任何东西**。
#
#     代价在一次真实排查里付了：用户遇到 `POST /api/turn` 返回
#     **500**，而那个 traceback 只打在**他正看着的那个控制台窗口**里，
#     转瞬即逝。本地复现不出来（同样的代码返回 200），
#     于是**那条错误信息就永远丢了** —— 只能让他重启再赌一次。
#
#     👉 **一个"建了却不用"的目录，和一句"写了却没实现"的注释是同一种错。**
#        它让人以为"日志是有的"。


async def read_json_body(request: Request) -> dict:
    """读请求体里的 JSON。**读不出来就返回 400，不是 500。**

    ⚠️ 实测踩过：请求体不是合法 UTF-8 JSON 时，`await request.json()`
       直接抛 `UnicodeDecodeError` —— 而它会被兜底处理器变成
       **500（服务端错误）**，还会往日志里写一整段 traceback。

       可这是**客户端**发错了东西，不是我们坏了。
       用 500 表达有两个坏处：
         · 排查时被带偏（"服务端崩了"vs"请求发坏了"完全是两个方向）
         · 日志被真正的服务端错误淹没

       （当时的真实场景是我自己用 curl 测，Git Bash 把中文按 GBK 发出去了。）

    ⚠️ 返回空字典而不是抛异常，是为了让调用方继续用
       `body.get("x")` 那套写法 —— **调用方不用为了这个防御多写一层**。
       缺字段由各自的 `if not xxx: raise HTTPException(400, ...)` 兜住。

    ⚠️⚠️ **这里曾经写的是 `body = await read_json_body(request)` —— 自己调自己。**
        （给函数写新 docstring 时，把那行 `await request.json()` 覆写成了自己的名字。
          和更早那次 `_api_guard` 被正则吃掉函数体是**同一个失误**。）

        后果是全局性的、而且**假得很像真的**：
          · 无限递归 → `RecursionError` → 被下面的 `except Exception` 吞掉 → 返回 `{}`
          · 于是**每一个 POST 的请求体都变成空字典**
          · `/api/turn` 收到空 text → 400「说了句空的」

        用户看到的：**发一句话就红字报错，一个字都发不出去。**

        ⚠️ 为什么难发现：`{}` 是一个**合法且常见**的值（调用方本来就允许空体），
           而且没有崩溃、没有 500、没有日志 —— 失败的表现和"客户端确实没发东西"
           一模一样，**调用方无从分辨**。
           静态检查也看不出来：语法对、类型对、名字对。

        👉 `scripts/check_recursion.py` 专门守这一类：**不允许任何函数直接调自己**
          （这个仓库里没有一处递归是有意为之的，它只在两次事故里出现过）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def log_path() -> Path:
    """日志文件在哪。"""
    return paths.logs_dir() / "server.log"


def log_exception(where: str, exc: BaseException) -> None:
    """把一次异常连 traceback 一起写进日志文件。

    ⚠️ **这个函数自己绝不能抛异常。** 它是在处理错误的过程中被调用的 ——
       如果它也炸了，用户看到的是一个"处理错误时又出错了"的二次错误，
       而真正的原始错误仍然无处可寻。
       所以整个函数体都包在 try 里，写不进去就算了。
    """
    import traceback

    try:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\n")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {where}\n")
            f.write(f"{'-' * 70}\n")
            traceback.print_exception(
                type(exc), exc, exc.__traceback__, file=f
            )
    except Exception:  # noqa: BLE001
        pass


def _open_in_file_manager(path: Path) -> tuple[bool, str]:
    """在系统文件管理器里打开一个目录。

    ⚠️ 三个平台三套命令，而且它们的行为差别很大：
        Windows  explorer   —— ⚠️ 它**成功也返回退出码 1**，所以不能看返回值
        macOS    open
        Linux    xdg-open

    Windows 那个坑值得单独说：`explorer <path>` 打开成功时退出码是 1。
    如果按"退出码非零就是失败"来判断，会把成功报成失败 ——
    用户看到的错误信息是"打不开"，而文件夹其实已经弹出来了。
    所以 Windows 上**只捕获启动异常，不看退出码**。
    """
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            # 不等待、不检查退出码 —— 见上面的说明
            subprocess.Popen(
                ["explorer", str(path)],
                close_fds=True,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except FileNotFoundError as exc:
        return False, f"系统里没有可用的文件管理器（{exc}）"
    except OSError as exc:
        return False, str(exc)
    return True, ""


# =============================================================================
# 回合运行器
# =============================================================================


class Hub:
    """一轮对话的执行者，兼事件总线。

    ⚠️ 一次只允许一轮。这不是偷懒，是刻意的：
    两轮同时跑会让工具抢同一份数据目录、抢同一个工作目录，
    产生的现象极难排查（"为什么我的输出覆盖了别人的"）。
    **宁可让用户等，也不要让两轮互相踩。**
    """

    def __init__(self, *, proxy: ProxyConfig | None = None) -> None:
        # ⚠️⚠️ 事件是**广播**的（每个订阅者一条队列），不是"一个共享队列"。
        #
        #    原来是一个 `queue.Queue`，所有 `/api/events` 连接都从它取 ——
        #    而队列的每一项**只会被一个取走者拿到**。
        #
        #    于是：开第二个标签页、或者刷新页面时旧连接还没断掉，
        #    两个连接就会**互相偷事件**。现象是"消息时有时无"，
        #    而病因（多了一个消费者）离现象十万八千里。
        #
        #    广播的代价是内存里多几条队列 —— 但队列无界，
        #    所以**退订必须可靠**（见 api_events 的 finally）。
        self._subs: list[queue.Queue[Any]] = []
        self._subs_lock = threading.Lock()
        # 排查用的计数（见 stats()）
        self._published = 0
        self._delivered = 0

        self.cancel_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # 工具问用户、并阻塞等回答的通道（见 core/ask.py）。
        # ⚠️ 一个 Hub 一个 —— 它承载的是"当前这个问题"，本来就是单数。
        self.ask = QuestionBroker()

        self.proxy = proxy or ProxyConfig()
        self._agent: Agent | None = None
        self._provider: Provider | None = None

        # ⚠️ 工作区**跨轮保持** —— 这是刻意的。
        #
        #    用户是**逐步推进**一件事的："先看看要下什么" → "下吧" →
        #    "跑一下试试" → "结果怎么样"。
        #
        #    每一步都是独立的一轮对话，但它们共享同一个任务状态
        #    （计划、已下载的文件、生成的配置、运行结果）。
        #
        #    如果每轮重建工作区，用户就得在每句话里把站号和日期重说一遍，
        #    而且第二轮会"忘记"第一轮下的数据 —— 完全没法用。
        #
        # ⚠️ `Workspace` 是**模块级导入**的，别在这里再 import 一次 ——
        #    那样它又会变成局部名字，`reset()` 里的另一半就看不到了。
        self.workspace = Workspace()

    # -------------------------------------------------------------------------
    # 状态
    # -------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def agent(self) -> Agent | None:
        return self._agent

    # -------------------------------------------------------------------------
    # 事件广播
    # -------------------------------------------------------------------------

    def subscribe(self) -> queue.Queue[Any]:
        """开一条新的事件队列。**调用方必须负责退订**（见 api_events）。"""
        q: queue.Queue[Any] = queue.Queue()
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Any]) -> None:
        """退订。已经在列表里没有也不报错 —— 退出路径要能重复调用。"""
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: Any) -> None:
        """把一个事件发给**所有**当前订阅者。

        ⚠️ 先复制列表再投递 —— 投递过程中有人订阅/退订不会影响这一轮，
           也不需要一直持锁（持锁时调用 put 可能阻塞）。
        """
        with self._subs_lock:
            targets = list(self._subs)
            self._published += 1
            self._delivered += len(targets)
        for q in targets:
            q.put(event)

    def stats(self) -> dict:
        """给排查用的计数。

        ⚠️ **这不是花架子。** 实测踩过一次："事件没到界面"，
           而症状完全没有指向性 —— 可能是没发出去、可能是没有订阅者、
           也可能是前端没渲染。**三种情况的修法完全不同。**

           用户看到的只有"界面没反应"。而这三个数一摆出来，
           是"发了几条 / 投给了几个人"就一目了然：
               published = 0        → 根本没发出去（循环层的问题）
               delivered = 0        → 发了，但**一个订阅者都没有**（连接的问题）
               delivered > 0 而界面没动 → 前端的问题
        """
        with self._subs_lock:
            return {
                "subscribers": len(self._subs),
                "published": self._published,
                "delivered": self._delivered,
            }

    # -------------------------------------------------------------------------
    # 起一轮
    # -------------------------------------------------------------------------

    def _ensure_agent(self) -> Agent:
        """拿到这个会话的 agent —— **只创建一次**。

        ⚠️⚠️ 这里原来每轮都重建 Agent，是个严重的 bug。

           当时的理由是"取消标志绑在构造时，复用会把上一轮的取消状态
           带进下一轮"。**但重建会连带清空对话历史** ——
           而那时又没接 session，于是每轮都是全新的对话。

           用户的原话："没有上下文吗，我说完就忘了。"

           正确的做法：**复用 agent，只重置该重置的那部分**
           （用 Agent.begin_turn()）。见 core/loop.py 里那段说明。
        """
        if self._agent is not None:
            return self._agent

        session = self._open_session()
        self._agent = Agent(
            provider=self._build_provider(),
            system_prompt=SYSTEM_PROMPT,
            # ⚠️ 工具集绑定到**长期存在的工作区**（self.workspace），
            #    不是每轮新建 —— 见 Hub.__init__ 的说明。
            tools=build_ginan_tools(self.workspace),
            cancel=self.cancel_flag,
            run_dir=self.workspace.work_dir,
            session=session,
            # ⚠️ 有了它，模型才能用 `ask_user` 工具问出**可点的选项**。
            #    不给的话工具会如实回一句"没有可用的界面" ——
            #    那在命令行/单测里是对的，在服务里就是功能缺了。
            ask=self.ask,
        )
        return self._agent

    def _open_session(self):
        """打开（或新建）这个会话的历史文件。

        ⚠️ 接了 session 之后，**重启程序对话也还在**。
           在这之前它是纯内存的 —— 关掉窗口，聊过什么就没了。

           （完整的多会话/任务历史还没做，那是个更大的设计。
             但"基本的不失忆"是**现在就必须有的** ——
             没有它，用户每说一句都要把前面重说一遍。）
        """
        from ..core.session import SessionStore

        try:
            store = SessionStore()
            for m in store.list_all():
                return store.session(m.id)
            return store.session(store.create(title="默认对话").id)
        except OSError:
            # ⚠️ 会话存不了不该让程序起不来 —— 退回纯内存模式。
            #    聊天记录丢了很烦，但"程序打不开"更糟。
            return None

    def start(self, text: str) -> bool:
        """起一轮。已经有轮在跑就返回 False。"""
        with self._lock:
            if self.busy:
                return False

            agent = self._ensure_agent()

            # ⚠️ 只重置**这一轮**的状态，不动对话历史。
            #    见 Agent.begin_turn() 的说明。
            self.cancel_flag = threading.Event()
            agent.begin_turn(self.cancel_flag)

            self._thread = threading.Thread(
                target=self._run, args=(text,), daemon=True, name="ginan-turn"
            )
            self._thread.start()
            return True

    def _build_provider(self) -> Provider:
        """按当前设置造模型客户端。

        ⚠️ 这里**不检查 Key 存不存在** —— 检查放在真正发请求的时候。
        桌面应用要能在没配 Key 时正常启动，
        否则用户第一次打开就是一堆报错，连设置界面都到不了。

        ⚠️ 每轮都重新读设置 —— 这样用户在界面上改完 Key，
        **下一轮就生效**，不需要重启程序。
        """
        from ..settings import load_settings

        s = load_settings()
        return Provider(
            ProviderConfig(
                model=s.model or "deepseek-flash",
                base_url=s.base_url or "https://api.deepseek.com",
                api_key=s.api_key or None,
                proxies=ProxyConfig(
                    mode=s.proxy_mode or "auto",
                    http=s.proxy_http or "",
                    https=s.proxy_https or "",
                ).to_requests(),
            )
        )

    def _run(self, text: str) -> None:
        """工作线程：跑一轮，把事件推进队列。

        ⚠️ 这个函数**无论如何都要把 TURN_DONE 放进队列**。
        否则 SSE 那边会永远等下去，界面永远显示"处理中"。

        所以 finally 是必须的，不是可选的优雅写法。
        """
        assert self._agent is not None
        try:
            for event in self._agent.run(text, emit=self.publish):
                # 取消后不再转发 —— 但**仍然要把生成器跑完**，
                # 因为循环层需要在收尾时补上"被取消的工具"的回复记录，
                # 否则历史里会留下孤儿 tool_call（详见 core/loop.py）。
                self.publish(event)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 这里必须兜住所有异常。
            #    工作线程里抛出的异常不会传到任何地方 ——
            #    没有这个 catch，线程静默死掉，用户看到界面卡住，
            #    而控制台里什么都没有。这是最难查的一类问题。
            #
            # ⚠️ 而且**光推一个事件是不够的**：那个事件只在当时连着的
            #    浏览器里闪一下，用户刷新就没了，我们这边更是看不到。
            #    必须**同时落盘**。
            log_exception("工作线程 agent.run()", exc)
            self.publish(
                {
                    "type": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            self.publish(TURN_DONE)

    def stop(self) -> None:
        """请求取消当前这一轮。"""
        self.cancel_flag.set()
        # ⚠️ **光设标志是不够的。**
        #    如果此刻有工具正在 `ask_user` 上阻塞，它每 0.2 秒只看一次
        #    `cancel` 标志 —— 但真正叫醒它的是 `clear()`。
        #    只设标志的话，工具最多晚 0.2 秒醒，这里其实也行；
        #    但**等待表里那条记录**要是不清，下一次 answer() 会打到
        #    一个已经死掉的问题上。所以两件事一起做。
        self.ask.clear()

    def reset(self) -> dict:
        """开始新任务：清空对话和任务状态。

        ⚠️⚠️ **不动已经下载的数据。**

           下载一次要几分钟、几百 MB。用户说"我想重新算"的时候，
           他要的是"换个站/换一天重新来"，**不是"把数据删了重下"**。

           删数据是个**昂贵且不可逆**的动作，必须是一次独立的、
           用户明确发起的操作，不能混在"新任务"里顺手做掉。

        ============================ 为什么这里可以重建 agent ============================
        之前修过一个 bug：服务端**每轮重建 Agent**，导致用户"说完就忘"。

        这里又重建了 —— 但**不冲突**：

            那次：该保留的（对话历史）被清掉了  ❌
            这次：该清的（对话 + 任务状态）正要清  ✅

        **同一行代码，在不同意图下对错相反。**
        所以判断标准不是"能不能重建"，而是
        **"重建会清掉什么，而那东西该不该清"。**

        ============================ ⚠️⚠️ 但"重建"清不掉历史 ============================
        上面那句注释原来写着"下次用时重建（历史也因此清空）" —— **那是错的。**

        `self._agent = None` 只是丢掉**内存里的那个对象**。
        下次 `_ensure_agent()` 会走 `_open_session()`，而它挑的是
        **最近更新过的那个会话** —— 于是 `Agent._load()` 把
        `sessions/<id>/history.jsonl` **原样读回来**。

        现象：用户点了"新任务"，界面清了，**模型却还记得全部对话**。
        这正是"两边不一致"那类最难查的 bug。

        修法不是再写一遍清理逻辑 —— `Agent.reset()` 早就写好了
        （它同时清内存**和**文件，见 core/loop.py），
        **只是从来没有人调用过它**。

        👉 所以在丢掉 agent **之前**先让它把文件清干净。
        """
        # ⚠️ 顺序不能反：先清历史（那时还拿着 session 的路径），再丢引用。
        if self._agent is not None:
            try:
                self._agent.reset()
            except OSError:
                # 文件删不掉不该让"新任务"整个失败 —— 内存那份已经清了。
                pass

        # ⚠️⚠️ **历史必须清两次，走两条路** —— 只清 agent 那份是不够的。
        #
        #    实测：清完 agent 之后，`history.jsonl` **原封不动**。
        #    因为 `self._agent` 是 None 时上面那段整个跳过 ——
        #    而"用户重启程序、还没说过话、直接点新任务"正是这个情况：
        #    内存里没有 agent，可**磁盘上留着上一次运行的整段对话**。
        #
        #    👉 `Agent.reset()` 认的是"我这个进程里的会话"，
        #       而"磁盘上有没有旧历史"是**另一个问题**，得单独回答。
        try:
            from ..core.session import SessionStore

            store = SessionStore()
            for meta in store.list_all():
                store.session(meta.id).clear(SYSTEM_PROMPT)
        except OSError:
            # 会话存不了/删不掉，不该让"新任务"整个失败
            pass

        self._agent = None
        self.workspace = Workspace()

        # ⚠️ 收件箱要**整个清空** —— 认领过的、没认领过的，都删。
        #
        #    用户 2026-09-21 连着说了三句，一句比一句紧：
        #      「inbox文件夹重置应该是清空，我点新任务里边还有东西」
        #      「没认领也删除」
        #      「点新任务什么都不留」
        #
        #    这就是他的数据定义：**收件箱是投放区，随"新任务"清空；
        #    数据目录是缓存，永远保留。**
        #
        # =====================================================================
        # ⚠️ 原来那版（`inbox.clear_claimed()`）只删"能验证 data/ 里有一份"的
        # =====================================================================
        #    它的判据是"数据目录里已经有一份内容相同的"，而**下面那一步
        #    删观测数据，正好把这份证据销毁掉** —— 两次删除互相拆台：
        #
        #        第一次重置：先清收件箱（那时 data/ 里还有，判得动）
        #                    → 再删 data/ 里的观测数据（证据没了）
        #        第二次重置：再问"这份 data/ 里有吗" → **永远答没有**
        #
        #    实测用户那一箱：193 个文件，**删 0、留 193**，
        #    此后每一次重置都是这个数 —— 收件箱只进不出。
        #    而这正是用户报的那个现象（「里边还有东西」）。
        #
        # 👉 现在**两段之间没有顺序要求了**：清收件箱不看 data/ 里的东西，
        #    删 data/ 也不看收件箱。那个耦合整个消失。
        #    （教训留着：**一个判据依赖的东西，不能在同一段流程里被删掉。**）
        from ..ginan import inbox as ginbox

        try:
            cleared_inbox = ginbox.clear_inbox()
        except OSError:
            cleared_inbox = None

        # ⚠️ 观测数据（`kind == "obs"`）是**当前任务的作业材料**，不是可复用的产品 ——
        #    "新任务"一开就该走。产品（sp3/clk/erp/atx/nav）永远留着：能重下、
        #    体积小、而且经常被多个任务复用（igs20.atx 有 61 MB）。
        #
        #    用户的原话：「缓存不用存以往的观测数据，就存
        #    IGS0OPSRAP_..._CLK.CLK 这种的」「新任务时删除」「不永久留存」。
        try:
            cleared_obs = ginbox.clear_observations()
        except OSError:
            cleared_obs = None

        # ⚠️⚠️ **运行目录（`userdata/runs/`）也要清 —— 这一段是补的。**
        #
        #    在此之前这里**一个字节都没删过**：`runs_dir()` 全项目只有
        #    "建目录"和"报路径"两种用法，于是一次解算的**全部产物**
        #    （配置、日志、`.POS`/`.SP3` 结果，还有几 GB 的
        #      `Filter-*.rts_forward` 和每站一个的 `.TRACE`）**只进不出**。
        #    实测：12 天、41 次运行、14 GB。
        #
        #    而**上面返回给用户的那张清单里，一直写着"会删掉…运行记录"**
        #    （确认框里也是同一句话）—— 也就是说：
        #    **程序说它会删，其实没删。** 用户 2026-10-02 问的就是这个。
        #
        #    👉 现在"会删掉"那张清单是真的了。判据、代价、为什么整个删，
        #       都写在 `ginan/runs.py` 的文件头。
        from ..ginan import runs as gruns

        try:
            cleared_runs = gruns.clear_runs()
        except OSError:
            cleared_runs = None

        # ⚠️ 原来这里写的是「任务状态（计划 / 配置 / **运行记录**）」——
        #    而"运行记录"从没被删过（见上面 `clear_runs()` 那段），
        #    现在它**自己单独一行**（带省了多少 GB），所以从这句里去掉：
        #    留着的话同一件事在报告里出现两次，而且其中一次还是笼统的。
        cleared = ["对话历史", "任务状态（计划 / 配置）"]
        # ⚠️ 用 `ClearResult.summary` 而不是在这儿另写一句 ——
        #    "删了几个"只有一个来源，两边各写各的迟早对不上，
        #    而这句话是告诉用户**他的文件去哪了**的。
        #
        # ⚠️ 条件里带上 `errors` —— **删不掉也要走这条**，
        #    否则"一个都没删掉"会连这一行都不出现，那就又静默了。
        if cleared_inbox and (
            cleared_inbox.deleted or cleared_inbox.deleted_dirs or cleared_inbox.errors
        ):
            cleared.append(f"收件箱：{cleared_inbox.summary}")
        if cleared_obs and cleared_obs.deleted:
            names = "、".join(p.name for p in cleared_obs.deleted[:5])
            more = "…" if len(cleared_obs.deleted) > 5 else ""
            cleared.append(f"观测数据 {names}{more}")
        # ⚠️ 条件里带上 `errors` —— 同收件箱那条：**删不掉也要说出来**，
        #    否则"一个都没删掉"会连这一行都不出现，那就又静默了。
        if cleared_runs and (cleared_runs.deleted or cleared_runs.errors):
            cleared.append(f"运行记录：{cleared_runs.summary}")

        # ⚠️⚠️ **没删掉的单独一份，而且原样带着原因。**
        #
        #    2026-09-21 用户就是这么撞上的：点完新任务，收件箱里还剩一个文件。
        #    那个文件被另一个程序占着（Windows 上 `unlink()` 需要
        #    FILE_SHARE_DELETE，而 Python 的 `open()` 不给），
        #    **几分钟后手删一次就好了** —— 所以它是个会自己好的故障。
        #
        #    但当时用户看不到任何提示：`errors` 不进 `summary`，
        #    而 `summary` 又是前端唯一读的东西。**故障会自己好，
        #    可"用户不知道发生了什么"不会自己好。**
        #
        # ⚠️⚠️ **三处都要收，原来只收了收件箱那一份。**
        #    另两处的 `errors` 谁都不读 —— 也就是说**它们没有出口**：
        #    观测数据删不掉、运行目录删不掉，用户一个字都看不到。
        #    而这两处删的恰恰是**更大的东西**（几百 MB 到几 GB）。
        #
        #    > 同一个形状第三次出现：**一个字段只有一个写入者、零个读取者。**
        #    > 收件箱那次是 `too_big`，这是第三次。
        failed: list[str] = []
        for _r in (cleared_inbox, cleared_obs, cleared_runs):
            if _r:
                failed.extend(_r.errors)

        # ⚠️ `kept` 这个清单**只说真的留着的东西**。
        #
        #    它出过两次错，都是"说得比实际温柔"：
        #      第一版写着"已下载的数据文件（在 data/ 目录）" —— 而观测数据会被删
        #      第二版写着"收件箱里**还没认领过**的文件" —— 而它们现在也会被删
        #
        #    👉 一个描述行为的清单，一旦和实际行为不一致，
        #       就成了"程序说它不会删，结果删了"。**用户会照着它做判断。**
        #
        # ⚠️⚠️ **这些字符串现在会被前端原样显示**（`addNotice` 用的是
        #    `textContent`），所以**一个 `**` 都不能有** —— 有的话用户看到的
        #    就是字面的一排星号。这和 `新任务` 那个确认框踩过的是**同一个坑**，
        #    只是换了个地方发作。`check_ui_text.py` 现在两处都拦。
        return {
            "ok": True,
            "cleared": cleared,
            "failed": failed,
            "kept": [
                "卫星星历/钟差/地球自转/天线表这些产品（在 data/ 目录，可复用）",
                "API 密钥等设置",
            ],
            "note": (
                "收件箱被整个清空了（认领过的、没认领过的都没了）——"
                "那是投放区，下次要用就把文件重新放进去。"
                "观测数据也删掉了（这是你定的规则：缓存不永久留观测数据）。"
                "产品文件没有被删除 —— 下次处理同一天时不用重下。"
            ),
        }


# =============================================================================
# 应用
# =============================================================================


def create_app(hub: Hub | None = None, token: str | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    ⚠️ token 用参数传进来而不是在模块里生成 ——
    这样测试可以指定一个已知的值，不用去猜。
    """
    hub = hub or Hub()
    token = token or secrets.token_urlsafe(24)

    app = FastAPI(title="Ginan Agent", docs_url=None, redoc_url=None)
    app.state.hub = hub
    app.state.token = token

    # -------------------------------------------------------------------------
    # 兜底：任何没被接住的异常，都要**留下痕迹**
    # -------------------------------------------------------------------------
    #
    # ⚠️ 没有这一段的话，处理函数里抛出的异常会被 Starlette 变成
    #    一个光秃秃的 500，而 traceback **只打在控制台**。
    #    用户看到的是一句 "Internal Server Error"，
    #    而我们这边什么都拿不到 —— 排查只能靠猜。
    #
    # ⚠️ 要**同时**写到文件和控制台：
    #    · 文件 —— 用户能把它发给我
    #    · 控制台 —— 用户自己盯着那个窗口时能立刻看见
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        where = f"{request.method} {request.url.path}"
        log_exception(where, exc)
        # ⚠️ 这个 print **自己绝不能炸** —— 它是在处理错误的过程中执行的。
        #    实测踩过：Windows 控制台默认 GBK，那个 ⚠️ 直接让它抛
        #    UnicodeEncodeError，于是用户看到的是"处理错误时又出错"的
        #    二次错误，**真正的原始错误反而被盖住了**。
        #    （`log_exception` 里我写了同样的警告，却没在这里照做。）
        try:
            print(f"\n[错误] {where}\n"
                  f"       详情已写入 {log_path()}\n", flush=True)
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"服务端出错了（{type(exc).__name__}）。"
                          f"详情在 {log_path()}",
            },
        )

    # -------------------------------------------------------------------------
    # 安全：token + Host 校验
    # -------------------------------------------------------------------------

    def _guard(request: Request, x_token: str | None) -> None:
        """token + Host 校验。

        ⚠️ 两道检查缺一不可：
          · token  防"别的网页来戳我"（它拿不到 token）
          · Host   防 DNS rebinding（攻击者把域名解析到 127.0.0.1，
                   此时浏览器认为这是同源请求，token 也可能拿到）

        ⚠️⚠️ **但"每个接口都要过这一关"以前只是句注释，不是事实。**
           实测漏了 5 个：`/api/credentials`(GET+POST)、`/api/settings`(GET+POST)、
           `/api/inbox`、`/api/inbox/claim`、`/api/open-folder` —— 它们都收
           `x_token` 参数、却**从来没调用过 `_guard`**。

           而漏掉的恰好是最不该漏的：**写凭据**和**改设置**。

           所以改成**挂在 router 上**（见下面的 `api`）——
           新加一个接口想漏掉校验，得先**刻意**绕开 router 才行。
           注释说的"每个接口"从此是**结构保证**，不再靠人记得。

        ⚠️ token 从 header **或** query 取：浏览器原生的 EventSource
           不能设自定义请求头，所以 `/api/events` 只能走 query。
           一个函数覆盖两种，省得每个接口各写一遍。
        """
        # ⚠️⚠️ 取主机名不能直接 `split(":")[0]` —— IPv6 的写法是
        #     `[::1]:8000`，那样切出来是 `"["`，**永远匹配不上白名单里的
        #     `[::1]`**。这一项写在那里，会让人以为 IPv6 被考虑过了。
        #
        #     （今天没有实际影响：只绑 127.0.0.1，横幅印的也是 IPv4 地址。
        #      但"判据里有一项恒为假"本身就是个坑 —— 下次谁真去试 IPv6
        #      就会发现它一直是被拒的，而原因在这行。）
        raw_host = request.headers.get("host") or ""
        if raw_host.startswith("["):                 # IPv6：[::1]:8000
            host = raw_host.split("]")[0] + "]"
        else:
            host = raw_host.split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            raise HTTPException(403, "只接受本机访问")
        if (x_token or request.query_params.get("x_token")) != token:
            raise HTTPException(403, "token 不对")

    async def _api_guard(
        request: Request, x_token: str | None = Header(None)
    ) -> None:
        """给 APIRouter 用的依赖版本 —— 每个 /api 路由自动过一遍。"""
        _guard(request, x_token)

    # ⚠️ **所有** /api 接口挂在这个 router 上，它自带校验。
    #    新接口只要写成 `@api.get(...)` 就自动受保护。
    #    `/` 和 `/static` 留在 router 外面 —— 页面本身必须先能加载出来，
    #    否则前端拿不到注入的 token，一切免谈。
    api = APIRouter(dependencies=[Depends(_api_guard)])

    # -------------------------------------------------------------------------
    # 静态页面
    # -------------------------------------------------------------------------

    @app.get("/")
    async def index() -> HTMLResponse:
        """主页。

        ⚠️ 页面里要**注入 token** —— 前端拿不到它就没法调后面的接口。
        注入而不是让前端去别处查，是因为本地应用没必要搞复杂的握手。

        ⚠️ 这里必须用 HTMLResponse 而不是 FileResponse ——
        FileResponse 的构造参数是 `path`（磁盘上的文件），
        传 `content=` 会直接抛 TypeError。
        我们要的是"读出来、改一下、再返回"，所以用 HTMLResponse。
        """
        html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__TOKEN__", token)
        return HTMLResponse(
            content=html, headers={"Cache-Control": "no-store"}
        )

    if FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    # -------------------------------------------------------------------------
    # 直接接口 —— 不经过 LLM
    # -------------------------------------------------------------------------
    #
    # ⚠️ 这几个接口的设计意图：**能让界面秒回的，就不要绕模型**。
    #
    # 环境检查和网络体检的结果是确定性的、结构化的。
    # 让模型去"读一遍再说给用户听"，既慢又可能说错。
    # 模型该用来做的是**理解和解释**，不是**搬运结构化数据**。

    @api.get("/api/environment")
    async def api_environment(request: Request):
        # 这个函数是纯本地的、秒回的，但仍然放到线程里跑 ——
        # 因为它里面有磁盘操作（写探针文件、读磁盘用量），
        # 在事件循环里做同步 IO 会拖慢其他请求。
        report = await asyncio.to_thread(environment.check_environment)
        return JSONResponse(json.loads(report.to_json()))

    @api.get("/api/probe")
    async def api_probe(
        request: Request,
        proxy_mode: str = "auto",
    ):
        mode = proxy_mode if proxy_mode in ("auto", "manual", "none") else "auto"
        # ⚠️ 这个要联网，可能要几秒。一定得放线程里，
        #    否则整个服务在这几秒内不响应任何其他请求
        #    （包括"我还在跑"的心跳）。
        report = await asyncio.to_thread(
            probe.probe_all, ProxyConfig(mode=mode)
        )
        return JSONResponse(json.loads(report.to_json()))

    @api.get("/api/results")
    async def api_results(request: Request):
        """这次任务的结果，**给界面画图用**。

        ⚠️⚠️ 这个接口**不经过模型** —— 和 `/api/workspace`、`/api/environment`
           同一个理由："结果长什么样"是确定性的结构化数据，
           让模型读一遍再用话描述出来，既慢又可能说错。
           ⚠️ 更要紧的是：**图不能靠模型转述** ——
              "它从一米降到两厘米"这句话，和一张能看出这件事的图，
              对不懂 GNSS 的用户是两回事。

        ⚠️ 没跑过、或者跑完没有产物时**不报错**，返回 `has_result: false`
           加一句人话 —— 界面上那块该显示"还没跑"，不是显示一片红。

        ⚠️ 解析 `.POS` 要读文件（最大的那份几 MB），放线程里跑。
        """
        from ..ginan import charts, posfile

        def _collect() -> dict:
            ws = hub.workspace
            files = ws.pos_files() if ws else []
            out: list[dict] = []
            for p in files:
                pf = posfile.parse(p)
                if pf.count == 0:
                    continue
                series = posfile.plot_series(pf)
                series["file"] = p.name
                series["station"] = pf.station or p.name.split("_")[0]
                series["solution"] = pf.solution
                # ⚠️ 只数**有几张图**，不渲染 —— 侧栏要的是一个数，
                #    而渲染一张图要几十 KB 的 JSON 和一次布局计算。
                series["figure_count"] = len(charts.figure_kinds(pf))
                out.append(series)
            return {
                "has_result": bool(out),
                "task": ws.summary() if ws else {},
                "series": out,
            }

        data = await asyncio.to_thread(_collect)
        if not data["has_result"]:
            data["why_not"] = (
                "还没有可以画的结果 —— 先跑一次处理。"
                if not (hub.workspace and hub.workspace.runs)
                else "处理跑过了，但没有可用的定位结果文件。"
            )
        return JSONResponse(data)

    @api.get("/api/figures")
    async def api_figures(request: Request, file: str = ""):
        """把一份 `.POS` 画成图，返回 **plotly 的 figure JSON**（给前端渲染）。

        ⚠️ 为什么单独一个端点、而不是塞进 `/api/results`：
           图很大（几十~几百 KB），而侧栏只需要一句结论。
           让侧栏去拉全部图的 JSON 是白花的时间。

        ⚠️ `file` 是 `.POS` 的**文件名**（不是路径）—— 只允许取本次任务
           已经解析出来的那些文件，**不接受任意路径**：
           否则这个接口就成了"读服务器上任意文件"的入口。
        """
        from ..ginan import charts, posfile

        def _build() -> dict:
            ws = hub.workspace
            files = ws.pos_files() if ws else []
            want = file.strip().lower()
            picked = [p for p in files
                      if not want or p.name.lower() == want]
            out = {}
            for p in picked:
                pf = posfile.parse(p)
                if pf.count == 0:
                    continue
                figs = charts.pos_figures(pf)
                if figs:
                    v = posfile.verdict(pf)
                    out[p.name] = {
                        "figures": figs,
                        # ⚠️ 标签页的文字由**服务端给**（`charts.FIGURE_LABELS`）——
                        #    在 JS 里再写一遍就成了两份，改一处漏一处。
                        "labels": {k: charts.FIGURE_LABELS.get(k, k)
                                   for k in figs},
                        "station": pf.station,
                        "solution": pf.solution,
                        "headline": v.headline,
                        # 图下面那行：说清怎么读它 + 它的局限
                        "foot": charts.reading_note(pf),
                    }
            return {"ok": bool(out), "files": out}

        data = await asyncio.to_thread(_build)
        if not data["ok"]:
            data["why_not"] = (
                "还没有可以画的结果 —— 先跑一次处理。"
                if not (hub.workspace and hub.workspace.runs)
                else "处理跑过了，但没找到可用的定位结果文件。"
            )
        return JSONResponse(data)

    @api.get("/api/residuals")
    async def api_residuals(request: Request):
        """残差诊断**有没有、结论是什么** —— 给结果面板用。

        ⚠️ 和 `/api/results` 同一个理由：**不经过模型**。
           "残差正不正常"是确定性的事实，让模型转述既慢又可能说错。

        ⚠️ 它**只读那个几百 KB 的派生文件**，不去碰几百 MB 的 `.TRACE` ——
           用户把大文件删了之后，这个接口照常工作（图也是）。
        """
        from ..ginan import residuals as gresid

        def _collect() -> dict:
            ws = hub.workspace
            info = gresid.summary_for_ui(ws.work_dir) if ws else None
            if not info:
                return {"has_result": False}
            info["has_result"] = True
            return info

        return JSONResponse(await asyncio.to_thread(_collect))

    @api.get("/api/residual_figures")
    async def api_residual_figures(request: Request):
        """残差那几张图 —— 形状和 `/api/figures` **完全一样**，
        所以前端用的是同一段画图代码（`showFigures`）。"""
        from ..ginan import charts, residuals as gresid

        def _build() -> dict:
            ws = hub.workspace
            loaded = gresid.load_for_figures(ws.work_dir) if ws else None
            if not loaded:
                # ⚠️ 纯文本！这句直接进侧栏/浮层的 textContent，
                #    带 Markdown 记号会**露出字面星号**（这个仓库踩过多次）。
                return {"ok": False, "why_not": (
                    "还没有残差 —— 先跑一次残差诊断"
                    "（它会重跑一遍，但不改变解算结果）。")}
            rf, summary, v = loaded
            figs = charts.resid_figures(rf, summary)
            if not figs:
                return {"ok": False, "why_not": "残差读到了，但画不出图。"}
            return {"ok": True, "files": {rf.path.stem: {
                "figures": figs,
                "labels": {k: charts.RESID_FIGURE_LABELS.get(k, k)
                           for k in figs},
                "station": "残差（观测质量）",
                "solution": "",
                "headline": v.get("headline", ""),
                "foot": charts.resid_reading_note(summary),
            }}}

        return JSONResponse(await asyncio.to_thread(_build))

    @api.get("/api/plan")
    async def api_plan(
        request: Request,
        station: str,
        day: str,
    ):
        from datetime import date as _date

        from ..ginan import catalog

        try:
            d = _date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, f"日期格式不对：{day}，应该写成 2026-09-02")
        plan = catalog.plan_run(station, d)
        return JSONResponse(json.loads(catalog.plan_to_json(plan)))

    # -------------------------------------------------------------------------
    # CDDIS 凭据
    # -------------------------------------------------------------------------
    #
    # ⚠️ 凭据**不进 config.json**，只写 `.netrc`。
    #
    #    理由：`.netrc` 是 CDDIS 的工具链（curl、gnssanalysis、Ginan 自己）
    #    实际会去读的地方。存两份的话，两份迟早会不一致 ——
    #    而"界面上显示已配置，实际下载时却认证失败"是最难查的一类问题。
    #
    #    **单一来源**：.netrc 是唯一的事实，界面只是它的一个视图。

    @api.get("/api/credentials")
    async def api_get_credentials(
        request: Request,
    ):
        """读现有的 CDDIS 凭据状态。

        ⚠️ 只返回**用户名**和"配没配"，**绝不返回密码**。
           界面不需要密码，而少了它，这个接口就不可能泄漏。
        """
        from ..ginan import credentials

        return JSONResponse({
            "configured": credentials.has_credentials(),
            "user": credentials.current_user(),
            "paths": [str(p) for p in credentials.netrc_paths()],
            "signup_url": "https://urs.earthdata.nasa.gov/users/new",
        })

    @api.post("/api/credentials")
    async def api_save_credentials(
        request: Request,
    ):
        """保存并**当场验证** CDDIS 凭据。

        ⚠️⚠️ "当场验证"是整个流程里最关键的一步，不是可选的锦上添花。

           不验证的话：用户填完 → 看到"已保存" → 以为成了 →
           几十分钟后下载失败 → 报错是"下载失败"，完全联想不到是密码打错了。

           有了它，用户**填完立刻知道对不对**，错了当场改。
        """
        from ..ginan import credentials

        body = await read_json_body(request)
        user = str(body.get("user") or "").strip()
        password = str(body.get("password") or "")

        if not user or not password:
            raise HTTPException(400, "用户名和密码都要填")

        # ⚠️ 先**验证**再落盘。
        #
        #    顺序很重要：先写文件再验证的话，用户会看到
        #    "文件已保存，但认证失败" —— 一个半成功的状态，
        #    他还得自己去想"那我到底是有还是没有"。
        #
        #    先验证的话，只有确定能用才写。失败就是失败，干净。
        s = load_settings_for_creds()
        test = await asyncio.to_thread(
            credentials.test_credentials, user, password,
            proxy=ProxyConfig(
                mode=s.proxy_mode or "auto",
                http=s.proxy_http or "",
                https=s.proxy_https or "",
            ),
        )

        if not test.ok:
            return JSONResponse(
                {"ok": False, "detail": test.detail, "suggestion": test.suggestion},
                status_code=200,   # ⚠️ 用 200 而不是 4xx —— 这是业务结果不是传输错误
            )

        written = await asyncio.to_thread(credentials.write_credentials, user, password)
        return JSONResponse({
            "ok": True,
            "detail": test.detail,
            "written": [str(p) for p in written],
        })

    # -------------------------------------------------------------------------
    # 心跳
    # -------------------------------------------------------------------------

    @api.get("/api/ping")
    async def api_ping(request: Request):
        """超轻量的存活检查。给前端的"服务还在吗"轮询用。

        ⚠️ 为什么不复用 /api/environment？
        因为那个会做磁盘操作（写探针文件、读磁盘用量）。
        每 5 秒跑一次磁盘 IO 是没必要的 —— 心跳要尽可能便宜。

        ⚠️ 这个接口存在的理由是**用户体验**：
        服务停掉之后（比如用户关了那个命令行窗口），
        浏览器页面**还会留在屏幕上，而且看起来一切正常**。
        用户再去点任何按钮，得到的是一句 `Failed to fetch` ——
        一句完全不知道该怎么办的技术错误。

        有了心跳，界面能主动发现"后台没了"，并说清楚是怎么回事。
        """
        # ⚠️ 顺手带上 stats —— 排查"事件没到界面"时，这三个数是分水岭。
        #    见 Hub.stats() 的说明。它只是一次加锁读几个整数，够轻。
        return {"ok": True, "busy": hub.busy, "stats": hub.stats()}

    @api.get("/api/workspace")
    async def api_workspace(request: Request):
        """当前任务进行到哪一步了 —— 给界面显示状态用。

        ⚠️ 这个接口**不经过模型**。
           "现在做到哪了"是确定性的结构化数据，
           让模型去读一遍再说给用户听，既慢又可能说错。
        """
        return JSONResponse(hub.workspace.summary())

    # -------------------------------------------------------------------------
    # 收件箱（第四条取数路径）
    # -------------------------------------------------------------------------
    #
    # ⚠️ 这条路是**任何网络环境下都必须走得通**的保险丝 ——
    #    前三条（自动下载 / 代理 / 缓存）都依赖网络。
    #    详见 ginan_agent/ginan/inbox.py 开头的说明。

    @api.get("/api/inbox")
    async def api_inbox(request: Request):
        """看看收件箱里有什么。

        ⚠️ 这是**只读**的 —— 只扫描和识别，不动任何文件。
           扫描可能有点慢（要读每个文件的头部），所以放线程里。
        """
        from ..ginan import inbox, paths

        items = await asyncio.to_thread(inbox.scan)

        # ⚠️ 认领是**复制**，原件留在收件箱 —— 所以"收件箱里有东西"
        #    和"还没认领"是**两件事**。
        #
        #    界面原来只看 count，于是认领完之后那个按钮**还在**，
        #    用户会以为没认领成功，反复点。
        #    （用户原话："程序认领了我的数据后，右下角收件箱就不要提示认领了"。）
        done = await asyncio.to_thread(inbox.already_claimed, items)

        def pack(i):
            return {
                "name": i.path.name,
                "size": i.size_text,
                "kind": i.identity.kind,
                "label": i.identity.label,
                "known": i.known,
                "evidence": i.identity.evidence,
                "marker": i.identity.marker,
                # 数据目录里已经有一份了 —— 界面据此不再催用户认领
                "claimed": i.path in done,
            }

        packed = [pack(i) for i in items]
        return JSONResponse({
            "dir": str(paths.inbox_dir()),
            "count": len(items),
            "known": sum(1 for i in items if i.known),
            # ★ 真正要认领的数目 —— 界面的按钮看的是这个，不是 count
            "unclaimed": sum(1 for p in packed if p["known"] and not p["claimed"]),
            "items": packed,
        })

    @api.post("/api/inbox/claim")
    async def api_inbox_claim(request: Request):
        """把认得出来的文件搬到数据目录。

        ⚠️ 认不出来的**留在原地不动**，并报告出来。
           猜着搬比不搬更糟 —— 搬到错误位置后，Ginan 会给出
           一个和真正原因毫不相干的报错。

        ⚠️ 不覆盖数据目录里已有的同名文件 —— 那是用户的数据。
        """
        from ..ginan import inbox

        body = await read_json_body(request)
        dry = bool(body.get("dry_run"))

        result = await asyncio.to_thread(inbox.claim, dry_run=dry)
        return JSONResponse(json.loads(inbox.claim_to_json(result)))

    # -------------------------------------------------------------------------
    # 打开文件夹
    # -------------------------------------------------------------------------

    @api.post("/api/open-folder")
    async def api_open_folder(request: Request):
        """在系统的文件管理器里打开一个应用目录。

        ⚠️⚠️ 这个接口**会启动一个外部进程**，所以参数绝不能是任意路径。

        只接受一个**白名单里的名字**（inbox / data / runs / settings），
        由服务端自己解析成实际路径。

        为什么不能接受路径？因为那样它就变成了"以用户身份打开任意位置" ——
        而这是个 HTTP 接口，任何能构造请求的东西都能打它。
        token 挡住了一部分，但**凭据可能泄漏，白名单不会**。

        > 原则：凡是"能起进程"的接口，参数都应该是**枚举**而不是**路径**。
        """
        from ..ginan import paths

        body = await read_json_body(request)
        which = str(body.get("which") or "").strip()

        mapping = {
            "inbox": paths.inbox_dir(),
            "data": paths.data_dir(),
            "runs": paths.runs_dir(),
        }
        if which == "settings":
            from ..settings import config_path

            target = config_path().parent
        elif which in mapping:
            target = mapping[which]
        else:
            raise HTTPException(400, f"不认识的位置：{which!r}")

        target.mkdir(parents=True, exist_ok=True)
        ok, detail = await asyncio.to_thread(_open_in_file_manager, target)
        if not ok:
            raise HTTPException(500, f"打不开：{detail}")
        return {"ok": True, "path": str(target)}

    # -------------------------------------------------------------------------
    # 设置
    # -------------------------------------------------------------------------

    @api.get("/api/settings")
    async def api_get_settings(
        request: Request,
    ):
        """读设置。

        ⚠️ **绝不返回完整的 API Key** —— 只返回脱敏后的。
        界面不需要完整值，而少了它，这个接口就不可能泄漏。
        """
        from ..settings import PROVIDER_PRESETS, config_path as settings_config_path
        from ..settings import load_settings

        s = load_settings()
        d = s.to_json()
        d["api_key"] = ""                      # 完整值不出服务端
        d["api_key_masked"] = s.masked_key()
        d["has_key"] = s.has_usable_key()
        # ⚠️⚠️ 这个键**必须在这里给出来** —— 前端一直在读它：
        #         fillModelSelect(s.known_models || [], s.model || '');
        #     而它原来**没有任何生产者**（`settings.py` 里那个同名字段
        #     从来没人赋值、`to_json()` 还把它 pop 掉了）——
        #     于是每次打开设置，模型下拉框都是空的，用户得重新点一次「获取列表」。
        #
        #     ⚠️ 这是「**后端从不返回某个键，调用方一直在读**」那一类，
        #        `.POS` 的 `solution` 键刚出过一次一模一样的。
        d["known_models"] = list(_known_models)
        d["presets"] = [
            {
                "key": p.key,
                "label": p.label,
                "base_url": p.base_url,
                "signup_url": p.signup_url,
                "note": p.note,
            }
            for p in PROVIDER_PRESETS
        ]
        d["config_path"] = str(settings_config_path())
        return JSONResponse(d)

    @api.post("/api/settings")
    async def api_save_settings(
        request: Request,
    ):
        """保存设置。

        ⚠️ api_key 为空字符串时表示"**不要改它**"，而不是"清空它"。
        因为界面回显的是脱敏值，用户不动那个框就把脱敏值传回来了 ——
        如果当成清空，用户会发现"我什么都没改，Key 却没了"。
        """
        from ..settings import load_settings, save_settings

        body = await read_json_body(request)
        s = load_settings()

        if body.get("api_key"):
            s.api_key = str(body["api_key"]).strip()

        for f in ("provider", "base_url", "model", "proxy_mode",
                  "proxy_http", "proxy_https", "inbox_dir", "theme"):
            if f in body and body[f] is not None:
                setattr(s, f, str(body[f]).strip())

        # ⚠️ 改正项是一个**字典**，不能像上面那些字段一样 `str()` 一下。
        #    `clean()` 会把不认识的键和"不是 bool 的值"丢掉 ——
        #    见它自己的说明（字符串 "false" 在 Python 里是真值，那一类坑）。
        if isinstance(body.get("corrections"), dict):
            s.corrections = gcorrections.clean(body["corrections"])

        # ⚠️ inbox_dir 可能指向一个还不存在的目录（用户填了网盘同步路径），
        #    这里不建它 —— 建了反而可能因为路径写错而报一个莫名其妙的错。
        #    真正用它的时候再建。

        save_settings(s)

        # ⚠️ **同步到当前工作区** —— 生成配置读的是 `ws.corrections`
        #    （见 `agent/workspace.py`：让工具去读 settings 会让"这次跑的是
        #     哪一组改正项"变得不确定）。
        #    不同步的症状很具体：用户在设置里勾了一项，跑出来的配置里没有它，
        #    而**没有任何地方会报错**。
        if hub.workspace is not None:
            hub.workspace.corrections = dict(s.corrections)

        return {"ok": True, "api_key_masked": s.masked_key()}

    @api.get("/api/corrections")
    async def api_corrections(request: Request):
        """改正项列表 —— 界面上那 9 个开关**照这个画**。

        ⚠️ 和 `/api/results`、`/api/environment` 同一个理由：**不经过模型**。
           "这一项开着吗、它要哪个文件、文件下了没有"是确定性的事实，
           让模型转述既慢又可能说错，而用户会照着它去做下载的决定。

        ⚠️ 每一项都带上**它要的文件在不在**（含大小）——
           否则用户勾了"海洋极潮"，得到一个"要下 23 MB"的意外，
           而那个数字本来在决定之前就该看得见。

        ⚠️ `on` 是**实际生效值**（用户改过的覆盖档位之后的结果），
           `overridden` 说这一项是不是用户改过的 —— 界面上要能分辨
           "我设的"和"档位默认"，否则改过一项之后他分不清其余 8 项是谁定的。
        """
        from ..settings import load_settings
        from ..ginan import corrections as gcorrections

        def _collect() -> dict:
            s = load_settings()
            ws = hub.workspace
            # ⚠️ 工作区里那份是**这一跑用的**；没有工作区（还没开始任务）时
            #    用设置里的 —— 两处必须给同一个答案，不然界面显示的和
            #    实际写进配置的会对不上。
            overrides = (ws.corrections if ws is not None
                         else s.corrections) or {}
            profile = ((ws.config_inputs.profile if ws and ws.config_inputs
                        else None) or gconfig.DEFAULT_PROFILE)
            on = gcorrections.resolve(profile, overrides)
            data_dir = paths.data_dir()
            items = gcorrections.describe(on)
            for it in items:
                it["overridden"] = it["key"] in overrides
                files = []
                for name in it["files"]:
                    p = data_dir / "tables" / name
                    ok = p.is_file()
                    files.append({
                        "name": name,
                        "have": ok,
                        "mb": round(p.stat().st_size / 1048576, 2) if ok else None,
                    })
                it["file_info"] = files
                # 缺文件还开着 = 这一项**算不出来**（Ginan 不报错，静默少一项）
                it["blocked"] = bool(files) and not all(f["have"] for f in files)
            return {
                "profile": profile,
                "profile_label": gconfig.PROFILES[profile].label,
                "items": items,
            }

        return JSONResponse(await asyncio.to_thread(_collect))

    @api.post("/api/models")
    async def api_models(request: Request):
        """问服务端有哪些模型可用。

        ⚠️ 单独一个接口、用户主动触发，而不是保存时自动查 ——
        因为查模型要联网，可能慢、可能失败。
        让它成为一个"用户点了按钮才发生"的事，失败也不会卡住保存流程。
        """
        from ..settings import fetch_models, load_settings

        body = await read_json_body(request)
        s = load_settings()
        base_url = (body.get("base_url") or s.base_url or "").strip()
        # 用户可以传一个新填的 Key（还没保存），也可以复用已存的
        key = (body.get("api_key") or s.api_key or "").strip()

        if not base_url:
            raise HTTPException(400, "还没填服务地址")
        if not key:
            raise HTTPException(400, "还没填 API Key")

        models = await asyncio.to_thread(fetch_models, base_url, key)
        # ⚠️ 记下来 —— 关掉设置再打开时下拉框还有东西，
        #    用户不用每次重查（查模型要联网，可能慢、可能失败）。
        #
        # ⚠️ **只活在内存里**：进程一退就没了。这是有意的 ——
        #    落到 config.json 的话就成了"上次的列表"，服务端换了模型
        #    它还是旧的，反而误导（`settings.py` 里那条说明仍然成立）。
        global _known_models
        _known_models = list(models)
        return {"models": models, "count": len(models)}

    # -------------------------------------------------------------------------
    # 对话
    # -------------------------------------------------------------------------

    @api.post("/api/turn")
    async def api_turn(request: Request):
        body = await read_json_body(request)
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "说了句空的")
        if not hub.start(text):
            raise HTTPException(409, "上一轮还在跑，等它结束或者先停止")
        return {"ok": True}

    @api.post("/api/stop")
    async def api_stop(request: Request):
        hub.stop()
        return {"ok": True}

    @api.post("/api/answer")
    async def api_answer(request: Request):
        """回答 `ask_user` 提出来的那个问题。

        ⚠️ 409 不奇怪：**用户可能点了两次**，或者问题已经超时被撤了。
           如实返回而不是假装成功 —— 界面据此提示"这个问题过期了"，
           而不是把用户的选择悄悄吞掉。
        """
        body = await read_json_body(request)
        call_id = str(body.get("call_id") or "")
        value = str(body.get("value") or "")
        if not call_id:
            raise HTTPException(400, "没说是回答哪个问题")
        if not hub.ask.answer(call_id, value):
            raise HTTPException(409, "这个问题已经不在了（可能已超时或已回答）")
        return {"ok": True}

    @api.get("/api/history")
    async def api_history():
        """把这次会话的历史还给界面，让它**画回来**。

        ⚠️⚠️ 用户的原话：**"启动软件应该显示上次的对话，除非我点新任务"**

           而在这之前，历史**一直存着**（`sessions/<id>/history.jsonl`），
           但界面**从不渲染它** —— `handleEvent` 只对**实时事件**有反应，
           没有任何代码去"把会话画出来"。

           所以一刷新页面就是一片空白，用户的判断自然是
           **"它忘了"**。而实际上一个字都没丢。

           👉 **存下来了却不显示，和没存是一样的** —— 从用户的角度看没有区别。
              这一条和"日志目录建了却不写"是同一类错。

        ⚠️ 返回的结构要**简单**：界面只需要"谁说的 + 说了什么"。
           工具调用、进度那些过程性的东西不带回来 ——
           历史里的 tool 消息是给模型看的协议，不是给人看的。
        """
        agent = hub.agent
        if agent is None:
            # ⚠️ 还没聊过的时候 agent 是 None，这时**不要去建一个** ——
            #    建了会在磁盘上开一个新会话文件，而"我只是看了看历史"
            #    不该产生副作用。
            try:
                from ..core.session import SessionStore

                store = SessionStore()
                metas = store.list_all()
                if not metas:
                    return {"messages": []}
                msgs = store.session(metas[0].id).read_all()
            except OSError:
                return {"messages": []}
        else:
            msgs = agent.messages

        out = []
        for m in msgs or []:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue          # 系统提示和工具消息不画
            text = m.get("content") or ""
            if not isinstance(text, str) or not text.strip():
                continue          # 只调工具没说话的那几轮，没有可画的正文
            out.append({"role": role, "text": text})

        return {"messages": out}

    @api.get("/api/pending")
    async def api_pending():
        """当前有没有正在等回答的问题。

        ⚠️ 给"刷新页面"用的：工具在服务端**还阻塞着**，
           但浏览器一刷新那个提问块就没了 ——
           用户看到的是一个卡住的界面，而问题其实还在等他。
        """
        return {"question": hub.ask.pending()}

    @api.post("/api/reset")
    async def api_reset(request: Request):
        """开始新任务：清空对话和任务状态，但**保留已下载的数据**。

        ⚠️ 正在跑的时候不许清 —— 那会让工作线程操作一个被换掉的
           工作区，产生很难排查的错乱。
        """
        if hub.busy:
            raise HTTPException(
                409,
                "正在处理中，先点『停止』再开始新任务。",
            )
        return JSONResponse(hub.reset())

    @api.get("/api/events")
    async def api_events(request: Request):
        """SSE 事件流。

        ⚠️ 这个接口**必须**同时接受查询参数里的 token，因为：

            浏览器原生的 EventSource **不能设置自定义请求头**。

        所以前端只能写成 `new EventSource('/api/events?x_token=...')`。
        如果这里只读 header，SSE 会永远返回 403 ——
        而症状是"界面一直显示处理中，什么都收不到"，
        排查起来要绕一大圈（HTTP 层面看 POST 是成功的）。

        ⚠️ 把 token 放进 URL 通常是不好的做法（会进日志、进历史记录）。
        但这里可以接受，理由是：
          · 服务只绑 127.0.0.1，外面根本连不到
          · token 每次启动重新生成，用完即弃
          · 没有第三方能看到这个 URL
        换成 header 的代价是 SSE 根本用不了，不划算。

        （将来如果加了 pywebview 窗口，可以考虑改用一次性 URL 换 cookie 的
          做法来避免 token 出现在 URL 里。目前不值得那个复杂度。）
        """

        # ⚠️ **每个连接一条自己的队列**（广播），不是从共享队列里抢。
        #    抢的话，第二个标签页/没断干净的旧连接会把事件吃掉。
        my_queue = hub.subscribe()

        async def stream():
            try:
                while True:
                    # 客户端断开（用户关了页面）就收工，别泄漏线程
                    if await request.is_disconnected():
                        break

                    # ⚠️ queue.get() 是阻塞的。直接调用会**卡死整个事件循环** ——
                    #    当前协程在等待时，其他协程根本得不到调度，服务就死了。
                    #    asyncio.to_thread 把它挪到线程池：阻塞的是那个线程，
                    #    事件循环照常转。
                    try:
                        item = await asyncio.to_thread(my_queue.get, True, 1.0)
                    except queue.Empty:
                        # 没事件就继续等。**不用手写心跳** ——
                        # sse_starlette 自己有 ping 机制（见下面的参数）。
                        continue

                    if item is TURN_DONE:
                        yield {"event": "done", "data": "{}"}
                        continue

                    # ⚠️⚠️ 这里必须 yield **原始数据**，不能自己拼 "data: xxx"。
                    #
                    #    踩过一次：手写成 `f"data: {json}\n\n"`，
                    #    结果 sse_starlette 又给包了一层，前端收到的是
                    #        data: data: {"type": ...}
                    #    JSON.parse 直接失败 → **整个事件流全废**，
                    #    界面永远收不到任何东西。
                    #
                    #    sse_starlette 接受字符串或 {"event":..., "data":...} 字典，
                    #    由它负责加前缀和换行。
                    #
                    # ⚠️⚠️⚠️ **这一行的缩进是有来历的，别"顺手整一下"。**
                    #
                    #    给整个 while 套 try 的时候，我漏了把这一段也缩进去 ——
                    #    于是它跑到了 `while True:` **外面**，成了永远走不到的
                    #    死代码。现象是：
                    #
                    #        · 事件**发得出去**（publish 计数正常涨到 43）
                    #        · 也确实到了订阅者的队列里（delivered 也是 43）
                    #        · 但**每一条都被循环读走然后丢掉**，一条都没 yield
                    #        · 界面只收得到最后那个 `done`
                    #
                    #    而 `while True` 不会退出，所以外面这行永远不会执行 ——
                    #    **Python 不会为这个报任何错**，静态检查也看不出来。
                    #
                    #    👉 教训：改缩进的时候要**看一眼它属于哪个块**，
                    #       尤其是 `while True` / `try` 这种"包一层"的改动。
                    #       这次是靠排查计数（published/delivered 都正常、
                    #       而客户端收不到）才定位到"发出去了但没转发"。
                    yield json.dumps(item, ensure_ascii=False)
            finally:
                # ⚠️ **必须退订**，而且要在 finally 里。
                #    队列是无界的：不退订的话，每刷新一次页面就永久多一条
                #    没人读的队列，事件一直往里堆 —— 内存只涨不降。
                #    异常、断开、正常结束，三条路都得走到这里。
                hub.unsubscribe(my_queue)

        from sse_starlette.sse import EventSourceResponse

        # ping 是 sse_starlette 内置的心跳：
        # 空闲时定期发注释行，防止中间的代理/浏览器把闲置连接掐掉。
        # 用它比自己手写更可靠 —— 手写的会被包成数据事件（也踩过）。
        return EventSourceResponse(stream(), ping=15)

    # ⚠️ 所有 /api 路由在这里一次性挂上去 —— **校验是 router 带的**，
    #    不在这里、也不在各接口自己身上。见上面 `api` 的说明。
    app.include_router(api)

    return app
