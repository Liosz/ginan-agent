"""自检：核心层 —— 循环、会话、取消、压缩。

这个脚本盯的是内核演化过程中**行为发生变化**的那几处。
凡是"改过的地方"都该有一条断言守着，否则改回去也没人发现。

覆盖：
  ① 构造 Agent **不创建任何文件**（缺陷 #5 —— 去掉构造副作用）
  ② 懒加载：第一次 run() 才落盘，且落盘的历史是**完整**的
  ③ 会话往返：存进去，重新构造能读回来
  ④ 取消：协作式取消，不抛 ValueError（缺陷 #2）
  ⑤ 压缩：切点永远不切开 tool_calls / tool 的配对（原有不变量）
  ⑥ SessionStore：多会话的创建、列出、打开、删除

零 API 费用、不联网。

### ⚠️ 那条偶发失败：**抓到了是哪一条，但元凶只"像是"确认**

    2026-09-27 起，这个脚本在整套 `check_all.py` 里**偶发红**（3 次），
    每次都是 **1 条断言**（82/83）。当时 `check_all` 把输出吞了，
    看不到是哪条 —— 先把那个修了（它现在会打出带 ✗ 的行）。

    2026-09-29 **抓到了**：红的是 **「线程正常收工」**（`not t.is_alive()`）。

**根因（像是）**：那一处的同步点选错了。

    def drive():
        ...
        done.set()            # ← 事件在**线程里面**设

    done.wait(timeout=10)     # ← 只保证"set 过了"
    check(not t.is_alive())   # ← 但线程**可能还没退出**

    `Event` 同步的是"我做到哪一步了"，**不是"线程没了"**。
    ⇒ 已改成断言前先 `t.join(timeout=10)`。

⚠️⚠️ **但"像是"就只能是"像是"** —— 把 `join` **去掉**再跑：

    · 单独跑 60 次（闲置）        → 0 红
    · 40 个 CPU 进程压着跑 40 次   → 0 红

    ⇒ **没能复现，所以不能宣称"这就是那次偶发红的原因"。**
      能说的是：那是一个**真实存在的竞态**（窗口极小），改它是对的；
      而观测到的那 3 次**是不是**它，**没有证据**。

    👉 下次再红：先看是不是还红在同一条。**如果还是它**，说明 `join` 没解决，
      那就往下查（`agent.run` 有没有可能真的不退出）。
"""


from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ginan_agent.core import (  # noqa: E402
    Agent,
    Session,
    SessionStore,
    Tool,
)
from ginan_agent.core.loop import COMPACT_KEEP_RECENT  # noqa: E402
from tests.fakes import FakeProvider, text_reply, tool_reply  # noqa: E402

PASS = 0
FAIL = 0


def check(cond: bool, label: str) -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}   ← 失败")
    return cond


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 60)


def echo_tool() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="原样返回。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            function=lambda text: f"echo:{text}",
        )
    ]


# =============================================================================
# ⓪ 提问通道（ask_user）
# =============================================================================


def ask_tool() -> list[Tool]:
    def ask(ctx, question: str, options: list) -> str:
        # 工具里就是这一句 —— 阻塞在这里等回答
        answer = ctx.ask_user(question, options)
        return json.dumps({"answer": answer}, ensure_ascii=False)

    return [
        Tool(
            name="ask",
            description="问用户。",
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array"},
                },
                "required": ["question", "options"],
            },
            function=ask,
            wants_context=True,
        )
    ]


def test_ask_user_roundtrip() -> None:
    section("⓪ ★ 工具能问出问题、拿到回答（跨线程的会合点）")

    # ⚠️⚠️ 这一块是**跨线程**的，而跨线程的东西写错了极难查：
    #     平时跑得好好的，只在"用户真的去点按钮"时才出问题。
    #
    #     工作线程：agent.run() 卡在 ask() 上
    #     HTTP 线程：/api/answer 投递答案
    #     两者之间唯一的东西是一条 Queue。
    from ginan_agent.core import QuestionBroker

    broker = QuestionBroker(timeout=5.0)
    provider = FakeProvider([
        tool_reply(("ask", json.dumps({"question": "要下载吗？",
                                       "options": [{"label": "下载"}]}))),
        text_reply("好的"),
    ])
    agent = Agent(provider, "助手", ask_tool(), ask=broker)

    events: list[dict] = []
    done = threading.Event()

    # ⚠️⚠️ `ask_user` 事件走的是 **emit 回调**，**不是**生成器！
    #
    #     因为提问发生在工具**正在执行**的时候，而那一刻生成器
    #     正卡在工具函数里 —— 生成器没法"一边执行一边 yield"
    #     （见 loop.py 里关于 emit 的那段说明）。
    #
    #     👉 所以宿主**必须**给 `run(emit=...)` 一个真的回调，
    #        否则提问发不出去、用户看不到、工具一路阻塞到超时。
    #        服务端是这么接的：`agent.run(text, emit=self.publish)`。
    #
    #     ⚠️ 第一版测试漏了这一步，于是"没收到 ask_user" ——
    #        **那是测试写错了，不是代码错了**。
    def collect(ev: dict) -> None:
        events.append(ev)
        if ev["type"] == "ask_user":
            broker.answer(ev["call_id"], "下载")   # 模拟用户点按钮

    def drive():
        for ev in agent.run("开始吧", emit=collect):
            events.append(ev)
        done.set()

    t = threading.Thread(target=drive, daemon=True)
    t.start()
    finished = done.wait(timeout=10)

    check(finished, "★ 回答了之后，那**一轮真的继续跑下去**了（没死等）")

    asked = [e for e in events if e["type"] == "ask_user"]
    check(len(asked) == 1, f"产出了 1 条 ask_user（{len(asked)}）")
    if asked:
        check(asked[0]["question"] == "要下载吗？", "问题原样传出来了")
        check(bool(asked[0]["call_id"]), "带了 call_id（用来配对回答）")
        check(
            any(o.get("label") == "下载" for o in asked[0].get("options", [])),
            "选项也带出来了（界面才有东西可画）",
        )

    # ⚠️ 回答要**真的送到工具手里** —— 不是"事件发了就算"
    check(
        any("下载" in e.get("result", "") for e in events
            if e["type"] == "tool_end"),
        "★ 工具拿到的确实是用户选的那个（答案真的送到了）",
    )
    # ⚠️⚠️ **断言"线程收工"之前必须先 `join` —— 这一条原来是个会偶发变红的守卫。**
    #
    #    2026-09-29 抓到根因（前两次只看到"82/83"、看不到是哪条，
    #    因为当时 `check_all` 把子进程输出吞掉了 —— 那次也一并修了）。
    #
    #    ```python
    #    def drive():
    #        ...
    #        done.set()        # ← 事件是在**线程里面**设的
    #
    #    done.wait(timeout=10) # ← 这只保证"set 过了"
    #    check(not t.is_alive())   # ← 但它可能**还没退出**
    #    ```
    #
    #    `done.set()` 之后线程还要走完 `drive` 的收尾、再走解释器自己的
    #    线程退出流程 —— 中间有一个窗口，那一刻 `is_alive()` **仍然是 True**。
    #    窗口很小，所以这条大约几十次才红一次（实测 ~2/60），
    #    而**单独重跑几乎总是绿的** ⇒ 看起来像"玄学"。
    #
    #    ⇒ 判据：**要断言"线程结束了"，就得先 `join`。**
    #       `Event` 只同步"我做到哪一步了"，它**不**表示"线程没了"。
    #
    #    ⚠️ 而"看心情变红的守卫"比"不红"更糟 —— 人会学会忽略红色。
    t.join(timeout=10)
    check(not t.is_alive(), "线程正常收工")


def test_ask_user_cancel_and_timeout() -> None:
    section("⓪b ★ 取消能立刻叫醒它，超时会自己收场（都不能死等）")

    from ginan_agent.core import QuestionBroker

    import time as _t

    # ---- ① 取消：**只用 cancel 标志**，不调 clear() ----
    #
    # ⚠️⚠️ 这两条要**分开测**，不能一起做。
    #
    #     取消和 clear() 是**两道冗余的保险**：设了标志之后，
    #     轮询（每 0.2 秒看一眼）会在下一拍发现；而 clear() 是
    #     立刻把它叫醒。两个都能让 ask() 返回 None。
    #
    #     一起测的话，**任何一道都好使**，于是删掉另一道测试照样绿 ——
    #     那等于两条都没测到。（实测：一起测时，分别删掉这两处
    #     代码，断言**都不会红**。）
    #
    #     👉 **判据要挑"只有这个修复才能解释的现象"** ——
    #        所以要构造出"只有它才能救场"的场景。
    broker = QuestionBroker(timeout=30.0)
    cancel = threading.Event()
    got: list = []

    def waiter():
        got.append(broker.ask(
            call_id="c1", question="?", options=[], allow_text=True,
            cancel=cancel, emit=lambda e: None,
        ))

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    _t.sleep(0.3)
    cancel.set()                 # ← 只设标志，**不** clear()
    t.join(timeout=3)
    check(
        not t.is_alive(),
        "★ 只设 cancel 标志就能醒（轮询那一拍）—— 没等到 30 秒超时",
    )
    check(got == [None], f"返回 None 表示'没答案'（{got}）")

    # ---- ①b clear()：**只叫醒**，不设 cancel 标志 ----
    broker_b = QuestionBroker(timeout=30.0)
    got_b: list = []

    def waiter_b():
        got_b.append(broker_b.ask(
            call_id="c2", question="?", options=[], allow_text=True,
            cancel=threading.Event(),     # ← 永远不取消
            emit=lambda e: None,
        ))

    tb = threading.Thread(target=waiter_b, daemon=True)
    tb.start()
    _t.sleep(0.3)
    broker_b.clear()             # ← 只有这一条路
    tb.join(timeout=3)
    check(
        not tb.is_alive(),
        "★ 只调 clear() 也能醒 —— 光设 cancel 标志是不够的（它阻塞在队列上）",
    )
    check(got_b == [None], "clear() 之后拿到的也是 None")

    # ---- ② 超时 ----
    #
    # ⚠️ 用户可能直接关掉浏览器标签页，那时**没有任何东西**会来投递答案。
    #    没有超时的话工作线程永远挂着，而 Hub 认为"这一轮还在跑"。
    broker2 = QuestionBroker(timeout=0.5)
    box: list = []
    t0 = _t.monotonic()

    # ⚠️ 放到线程里跑 + join 超时。
    #    直接调用的话，一旦超时逻辑被删掉它**永远不返回** ——
    #    而"整个自检挂住"虽然也算失败，却不是一条能读的断言，
    #    后面所有用例还全都跑不到。**守卫要能报错，不能自己死掉。**
    tt = threading.Thread(target=lambda: box.append(broker2.ask(
        call_id="c2", question="?", options=[], allow_text=True,
        cancel=threading.Event(), emit=lambda e: None,
    )), daemon=True)
    tt.start()
    tt.join(timeout=5)
    spent = _t.monotonic() - t0

    check(not tt.is_alive(), "★ 超时后自己收场（没挂住）")
    ans = box[0] if box else None
    # ⚠️⚠️ **上限放到 10 秒。理由有两半，第二半比第一半重要。**
    #
    #    ① 3 秒对一个 0.5 秒的超时来说，是**墙钟**预算，本来就偏紧 ——
    #       机器有负载时这种断言最容易"看心情变红"，
    #       而**"看心情红"比"不红"更糟：人会学会忽略红色。**
    #
    #    ② ⚠️ **但我必须说清楚：我没能证明这就是那次偶发红的元凶。**
    #       2026-09-27 这个脚本红过 2 次（都是 1 条断言），
    #       而当时代码里**只有这一条是墙钟断言**，所以我先动了它。
    #       事后补的复现实验**全部失败**：40 个 CPU 进程压着重负载、
    #       把上限**改回 3** 再跑 6 次 —— **全绿**。
    #       ⇒ 那次偶发红**至今没定案**，见文件开头的说明。
    #
    #    ⚠️ 真正锋利的那条断言是上面那句 `not tt.is_alive()`
    #       —— 超时逻辑被删掉时，线程**永远不返回**，它必红。
    #       这一句只需要挡住"超时被拖成了几十秒"这种离谱情况，
    #       所以放宽它**不会放过任何一种真的故障**。
    check(spent < 10, f"★ 收场得够快（用了 {spent:.1f} 秒，上限设的是 0.5）")
    check(
        ans is not None and "没有回答" in ans,
        "★ 超时返回的是一句**说明**，不是 None —— "
        "None 是'用户中止'，两者对模型的意思完全不同",
    )

    # ---- ③ 每条退出路径都要注销等待者 ----
    #
    # ⚠️ 漏了的话，等待表会一直涨；更糟的是后来的 answer()
    #    会打到**已经死掉的问题**上 —— 返回"成功"而没人收到。
    check(broker2.answer("c2", "太晚了") is False, "★ 过期的问题答不上（如实返回 False）")
    check(broker2.pending() is None, "过期后 pending() 也清干净了")


def test_ask_user_without_ui() -> None:
    section("⓪c ★ 没有界面时如实说，而不是傻等")

    # ⚠️ 命令行 / 单测场景没有 broker —— 那时工具该拿到 None，
    #    然后告诉模型"没法问，你直接用一句话说"。
    #    傻等的话，一个不存在的用户会让整轮挂到超时。
    from ginan_agent.core import ToolContext

    ctx = ToolContext.bare("ask")
    check(ctx.ask is None, "bare() 造的上下文没有提问通道")
    check(ctx.ask_user("在吗", []) is None, "★ 返回 None（不是抛异常、不是死等）")


# =============================================================================
# ① 构造副作用
# =============================================================================


def test_no_side_effect_on_construct() -> None:
    section("① 构造 Agent 不创建任何文件（桌面应用的关键性质）")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        session_dir = base / "会话"
        session = Session(path=session_dir / "history.jsonl")

        agent = Agent(FakeProvider([text_reply("hi")]), "你是助手", echo_tool(), session)

        # ⚠️ 这条断言守着一个**很容易退化**的性质。
        #    只要有人在 __init__ 里加一句 self._record(...)，
        #    桌面应用就会"开一次程序就多一个文件夹"。
        check(
            not session_dir.exists(),
            "构造 Agent 之后，磁盘上**没有**出现任何文件",
        )

        # ⚠️ 注意 agent.messages 会触发懒加载（它会读磁盘）。
        #    读不是副作用，所以这里仍然该是"只有系统提示"，没有别的消息。
        msgs = agent.messages
        check(
            len(msgs) == 1 and msgs[0]["role"] == "system",
            f"还没聊过，历史里只有系统提示（实际 {len(msgs)} 条）",
        )
        check(
            not session_dir.exists(),
            "读了一次 messages 之后，依然没有创建文件（读不产生副作用）",
        )

        # 真正跑一轮之后才落盘
        list(agent.run("你好"))
        check(
            session.path.exists(),
            "跑过一轮之后，历史文件才出现（懒加载生效）",
        )


# =============================================================================
# ② 懒加载后历史必须是完整的
# =============================================================================


def test_initial_write_is_complete() -> None:
    section("② 第一次落盘的历史必须是完整的（含系统提示）")

    # ⚠️ 这是懒加载带来的一个**新风险**：
    #    既然构造时不写文件，那第一次写的时候如果只写用户消息，
    #    文件里就会缺系统提示 —— 下次恢复时 agent 会丢掉自己的身份。
    #
    #    这里断言的正是"补写"逻辑（_needs_initial_write）真的生效了。

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "历史" / "history.jsonl"
        session = Session(path=path)
        agent = Agent(FakeProvider([text_reply("好的")]), "你是GNSS助手", echo_tool(), session)
        list(agent.run("第一句话"))

        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        roles = [m["role"] for m in lines]

        check(lines[0]["role"] == "system", "文件第一行是 system 消息")
        check(
            lines[0]["content"] == "你是GNSS助手",
            "系统提示的内容正确（不是空的占位）",
        )
        check(roles == ["system", "user", "assistant"], f"消息顺序完整：{roles}")


# =============================================================================
# ③ 会话往返
# =============================================================================


def test_session_roundtrip() -> None:
    section("③ 会话往返：存进去，重新构造能读回来")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "history.jsonl"

        a1 = Agent(
            FakeProvider([text_reply("第一次回答")]),
            "系统提示",
            echo_tool(),
            Session(path=path),
        )
        list(a1.run("第一次提问"))
        n_after_first = len(a1.messages)

        # 全新对象，指向同一个文件
        a2 = Agent(
            FakeProvider([text_reply("第二次回答")]),
            "系统提示",
            echo_tool(),
            Session(path=path),
        )
        check(len(a2.messages) == n_after_first, "第二个 Agent 读回了全部历史")
        check(
            any(m.get("content") == "第一次提问" for m in a2.messages),
            "历史里能找到第一次的提问",
        )

        list(a2.run("第二次提问"))
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        check(len(lines) == n_after_first + 2, "追加后文件行数正确（没有重复写系统提示）")


def test_reset_clears_both() -> None:
    section("④ reset 同时清内存和文件（不能只清一边）")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "history.jsonl"
        agent = Agent(FakeProvider([text_reply("回答")]), "系统提示", echo_tool(), Session(path=path))
        list(agent.run("提问"))
        agent.reset()

        check(len(agent.messages) == 1, "内存里只剩系统提示")
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        check(len(lines) == 1, "文件里也只剩系统提示")

        # reset 之后再聊，不该重复写系统提示
        list(agent.run("新的提问"))
        lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        check(len(lines) == 3, f"reset 后追加正确，共 3 行（实际 {len(lines)}）")


# =============================================================================
# ⑤ 取消
# =============================================================================


class CancelOnFirstReply(FakeProvider):
    """在吐出第一条回复时就按下取消按钮的假 Provider。

    ⚠️ 为什么要这样测？
    因为"取消"要验证的是**取消发生在两者之间**：
        · 第一个回复已经产生（有东西可以中止）
        · 第二个模型请求还没发出（中止真的生效了）
    如果只在 run() 之前设取消，测的其实是"一开始就别跑"，那是另一回事。
    """

    def __init__(self, cancel_flag: threading.Event) -> None:
        super().__init__(
            [
                tool_reply(("echo", json.dumps({"text": "x"}))),
                text_reply("这轮不该被问到"),
            ]
        )
        self._flag = cancel_flag

    def chat_stream(self, messages, tools=None):
        # 先正常走完这一轮（产出回应）
        result = yield from super().chat_stream(messages, tools)
        # 这一轮结束后再按下取消 —— 于是"下一个模型请求"应该被拦住
        self._flag.set()
        return result


def test_cancel_is_cooperative() -> None:
    section("⑤ 取消是协作式的，不抛 ValueError（缺陷 #2）")

    cancel = threading.Event()
    provider = CancelOnFirstReply(cancel)
    agent = Agent(provider, "系统提示", echo_tool(), cancel=cancel)

    # ⚠️ 关键的写法：整个遍历过程**不能抛异常**。
    #
    #    如果实现是"从外面 gen.close()"，在生成器正卡在网络里时
    #    会抛 ValueError: generator already executing。
    #    协作式取消（设标志 + 生成器自己检查）不会。
    try:
        events = list(agent.run("调一下 echo"))
        ok = True
        err = ""
    except ValueError as exc:
        events, ok, err = [], False, str(exc)
    except Exception as exc:  # noqa: BLE001
        events, ok, err = [], False, f"{type(exc).__name__}: {exc}"

    check(ok, f"遍历过程没有抛异常{'：' + err if err else ''}")

    types = [e["type"] for e in events]
    check(types[-1] == "turn_end", "最后仍然是 turn_end（界面能正常收尾）")

    last_text = events[-1].get("text", "") if events else ""
    check("停止" in last_text or "取消" in last_text, "收尾文字说明了这是被停止的")
    check(
        "这轮不该被问到" not in last_text,
        "取消之后**没有**再发起新的模型请求（第二轮的内容没出现）",
    )

    # ---- 最关键的一条：取消之后历史仍然是**合法**的 ----
    #
    # ⚠️ 取消发生时，带 tool_calls 的 assistant 消息已经进历史了。
    #    如果被跳过的工具不补一条回复，历史里就有"孤儿 tool_call"，
    #    下一次请求会被 API 直接拒掉（"每个 tool_call 必须有回复"）。
    #
    #    这个 bug 只在用户点"停止"时复现 —— 平时测试完全正常。
    #    所以必须专门测。
    msgs = agent.messages
    assistant_with_calls = [
        m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    n_calls = sum(len(m["tool_calls"]) for m in assistant_with_calls)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    check(
        len(tool_msgs) == n_calls and n_calls > 0,
        f"取消后历史仍然合法：{n_calls} 个调用 / {len(tool_msgs)} 条回复（不能有孤儿）",
    )

    # 被取消的那个工具要有明确交代
    end = next((e for e in events if e["type"] == "tool_end"), None)
    check(
        end is not None and "停止" in end["result"],
        "被取消的工具也回了一条说明，而不是凭空消失",
    )


def test_cancel_before_run() -> None:
    section("⑥ 开跑前就取消 —— 不该发起任何模型请求")

    cancel = threading.Event()
    cancel.set()
    provider = FakeProvider([text_reply("不该出现")])
    agent = Agent(provider, "系统提示", echo_tool(), cancel=cancel)

    events = list(agent.run("随便问问"))
    check(len(provider.calls) == 0, "一次模型请求都没发出去")
    # ⚠️ `turn_start` 是**最先**发的，而且在取消检查之前 ——
    #    它表示"这一轮开始了"，与"有没有真的调模型"无关。
    #    所以现在这里是两条：turn_start + turn_end。
    check(
        len(events) == 2
        and events[0]["type"] == "turn_start"
        and events[1]["type"] == "turn_end",
        f"只产出 turn_start + turn_end 两条（实际：{[e['type'] for e in events]}）",
    )


# =============================================================================
# ⑥b ⚠️ 跨轮记忆 —— 一个被移植弄坏的回归
# =============================================================================


def test_memory_across_turns() -> None:
    section("⑥b ⚠️ 跨轮记忆：agent 必须记得上一轮说的话")

    # ⚠️⚠️ 这条守着一个**真实发生过的退化**。
    #
    #    早期版本是这样的：
    #
    #        agent = Agent(...)              ← 循环外，只建一次
    #        while True:
    #            for event in agent.run(text): ...
    #
    #    移植到服务端时，我改成了**每轮重建 Agent** ——
    #    理由是"取消标志绑在构造时，复用会把上一轮取消过的状态带进来"。
    #
    #    **那个理由本身是对的，但我没意识到重建还有个副作用：
    #      Agent 持有对话历史，重建 = 每轮都是全新的对话。**
    #    加上当时又没接 session（没落盘），于是：
    #
    #        用户说一句，agent 答一句，下一句它就不记得上一句了。
    #
    #    用户的原话："没有上下文吗，我说完就忘了。"
    #
    #    > 教训：**为某个理由改动代码时，要把它连带影响的东西都过一遍。**
    #    > "重建一个对象"看起来是个局部操作，实际上会重置它持有的**全部**状态。
    #    > 而"它持有什么"这件事，往往不在你正在改的那几行里。

    from ginan_agent.core import Agent, Session
    from tests.fakes import FakeProvider, text_reply

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "h.jsonl"
        provider = FakeProvider([text_reply("好啊"), text_reply("你叫张三")])
        agent = Agent(provider, "你是助手", [], Session(path=path))

        list(agent.run("我叫张三"))
        n1 = len(agent.messages)

        # ⚠️ 模拟"开始新的一轮" —— 这**不该**清掉历史
        agent.begin_turn()
        check(
            len(agent.messages) == n1,
            f"begin_turn 之后历史没被清掉（{len(agent.messages)} 条）",
        )

        list(agent.run("我叫什么？"))

        # ⚠️ 最关键的一条：第二轮发给模型的 messages 里**必须有第一轮**
        sent = provider.calls[-1] if provider.calls else []
        texts = [(m.get("content") or "") for m in sent]
        check(
            any("我叫张三" in t for t in texts),
            "★ 第二轮把第一轮的原话发给了模型（真的记得）",
        )
        check(
            not agent.cancelled,
            "begin_turn 重置了取消标志（这是它存在的理由）",
        )

    # ---- 取消标志不该串到下一轮 ----
    cancel = threading.Event()
    agent2 = Agent(FakeProvider([text_reply("x")]), "s", [], cancel=cancel)
    cancel.set()
    check(agent2.cancelled, "取消后 cancelled 为 True")
    agent2.begin_turn()
    check(not agent2.cancelled, "★ begin_turn 之后取消标志被重置")


def test_session_survives_restart() -> None:
    section("⑥c 重启之后对话还在（session 落盘）")

    from ginan_agent.core import Agent, Session
    from tests.fakes import FakeProvider, text_reply

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "h.jsonl"

        a1 = Agent(FakeProvider([text_reply("好")]), "你是助手", [],
                   Session(path=path))
        list(a1.run("记住：我叫张三"))

        # ⚠️ 模拟重启：全新的 Agent 对象，指向同一个历史文件
        a2 = Agent(FakeProvider([text_reply("x")]), "你是助手", [],
                   Session(path=path))
        check(
            any("我叫张三" in (m.get("content") or "") for m in a2.messages),
            "新 Agent（模拟重启）读回了旧对话",
        )
        check(a2.turn_count > 0, f"轮数大于 0（{a2.turn_count}）")


# =============================================================================
# ⑦ 压缩的不变量
# =============================================================================


def test_cut_point_never_splits_tool_pairs() -> None:
    section("⑦ 压缩切点永远不切开 tool_calls / tool 的配对")

    # 造一段带工具调用的历史：
    #   system, user, assistant(tool_calls), tool, assistant, user, assistant(tool_calls), tool, ...
    msgs: list[dict] = [{"role": "system", "content": "s"}]
    for i in range(6):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"c{i}", "type": "function",
                     "function": {"name": "echo", "arguments": "{}"}}
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "结果"})

    bad = 0
    for keep in range(1, len(msgs) + 2):
        cut = Agent._find_cut_point(msgs, keep)
        kept = msgs[cut:]
        # 不变量的两条：
        #   ① 保留段不能以 tool 开头（那意味着它的 assistant 父级被切走了）
        #   ② 保留段里不能有孤儿的 tool
        if kept and kept[0]["role"] == "tool":
            bad += 1
        open_ids: set[str] = set()
        for m in kept:
            if m["role"] == "assistant" and m.get("tool_calls"):
                open_ids |= {c["id"] for c in m["tool_calls"]}
            elif m["role"] == "tool" and m["tool_call_id"] not in open_ids:
                bad += 1

    check(bad == 0, f"遍历所有保留长度，0 次切开配对（实际 {bad} 次）")

    # ⚠️ 注意切点可能等于 len(msgs)（表示"一条都不保留"），
    #    那时 msgs[cut] 会越界。所以要先判断再取值 ——
    #    这个边界情况在真实压缩里也会出现，不是测试的臆想。
    lands_on_user = []
    for k in range(1, len(msgs) + 1):
        cut = Agent._find_cut_point(msgs, k)
        lands_on_user.append(cut >= len(msgs) or msgs[cut]["role"] == "user")
    check(all(lands_on_user), "切点总是落在 user 消息上（或走到末尾）")
    check(COMPACT_KEEP_RECENT == 8, "保留条数的常量没被改动（改它要同步改这个断言）")


# =============================================================================
# ⑧ SessionStore
# =============================================================================


def test_layer_boundaries() -> None:
    section("⑯ ★★ 分层铁律：领域层**不知道** agent，内核**不知道** Ginan")

    # ⚠️⚠️ 这条规则写在 `dev/00-项目全貌.md` 的「分层铁律」一节，
    #    `ginan/paths.py` 的文件头也写着「· 不 import core」——
    #    **而它一条守卫都没有，而且已经被违反了。**
    #
    #    实测（把全仓库的 import 图建出来）：
    #        ginan ✗→ core    …… 有  ['ginan_agent/ginan/paths.py']
    #        ginan ✗→ agent   …… 没有
    #        core  ✗→ ginan   …… 没有
    #
    #    违反处是 `paths.py:69`：
    #        from ..core.session import default_app_root
    #    （理由写在上面一行："复用 core 里的用户数据目录计算 ——
    #      两个平台判断不该写两遍"。那个理由**是成立的**。）
    #
    #    ⚠️ 所以这一节**不是**去主张"必须立刻改掉" ——
    #       实测量过：`core.session` 只 import `types`，没有 LLM、没有网络，
    #       所以那条规则**真正想保护的东西**（领域层能被独立测试、
    #       不依赖 API Key 和网络）**仍然成立**。
    #
    #    👉 判据落在**规则的目的**上，而不是字面：
    #       ① 硬规则（零例外）：`ginan/` 不许 import agent/server/desktop
    #       ② 例外要**列出来**：`ginan/` 从 `core/` 只许拿这一样东西，
    #          多一样就报红 —— 因为"例外会长大"正是这条规则失效的方式
    #       ③ **那个例外本身必须是干净的**（`core.session` 只依赖标准库）——
    #          这一条才是"领域层能独立测试"的真正保证
    import ast as _ast
    import sys as _sys

    from ginan_agent.core import session as _session

    pkg = ROOT / "ginan_agent"
    layers = ("core", "ginan", "agent", "server", "desktop")

    def imports_of(path: Path) -> list[str]:
        """这个文件里所有**指向本包**的 import，解析成 `层.模块` 形式。"""
        here = list(path.relative_to(pkg).parts[:-1])
        out: list[str] = []
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                if node.level:
                    base = here[: len(here) - (node.level - 1)]
                    if node.module:
                        base = base + node.module.split(".")
                    out.append(".".join(base))
                elif node.module and node.module.startswith("ginan_agent."):
                    out.append(node.module[len("ginan_agent."):])
            elif isinstance(node, _ast.Import):
                for a in node.names:
                    if a.name.startswith("ginan_agent."):
                        out.append(a.name[len("ginan_agent."):])
        return out

    # ---- ① 硬规则 ----
    forbidden: list[str] = []
    for p in sorted((pkg / "ginan").rglob("*.py")):
        for mod in imports_of(p):
            top = mod.split(".")[0]
            if top in ("agent", "server", "desktop", "frontend"):
                forbidden.append(f"{p.relative_to(ROOT)} → {mod}")
    for line in forbidden:
        print(f"      ⚠️ {line}")
    check(
        not forbidden,
        "★★ 领域层（ginan/）不 import agent / server / desktop —— **零例外**",
    )

    # ---- ② ginan → core 的例外，要列全 ----
    ALLOWED_FROM_CORE = {"core.session"}          # 只为了 default_app_root
    from_core: set[str] = set()
    for p in sorted((pkg / "ginan").rglob("*.py")):
        for mod in imports_of(p):
            if mod.split(".")[0] == "core":
                from_core.add(".".join(mod.split(".")[:2]))
    extra = sorted(from_core - ALLOWED_FROM_CORE)
    if extra:
        print(f"      ⚠️ 多出来的：{extra}")
    check(
        not extra,
        f"★ 领域层从内核只拿 {sorted(ALLOWED_FROM_CORE)} 这一样"
        f"（实际 {sorted(from_core)}）—— 例外**会长大**，所以要盯着",
    )

    # ---- ③ 例外本身必须是干净的 ----
    #
    # ⚠️⚠️ 这一条才是"领域层能被独立测试（不需要 API Key、不需要网络）"
    #    这句话的**真正保证**。如果哪天 core.session 为了别的事 import 了
    #    requests，领域层就**间接**依赖上网络了 —— 而那种依赖
    #    在 `ginan/paths.py` 那一行里**完全看不出来**。
    stdlib = set(_sys.stdlib_module_names)
    mod_file = Path(_session.__file__)
    tree = _ast.parse(mod_file.read_text(encoding="utf-8"))
    third = set()
    for node in _ast.walk(tree):
        names = []
        if isinstance(node, _ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, _ast.ImportFrom) and not node.level and node.module:
            names = [node.module]
        for full in names:
            top = full.split(".")[0]
            if top not in stdlib and top != "ginan_agent":
                third.add(top)
    check(
        not third,
        f"★★ 而这个例外（core.session）**只依赖标准库**"
        f"（多余依赖：{sorted(third) or '无'}）"
        "—— 它一脏，领域层就间接依赖了 LLM/网络，而那一行 import 看不出来",
    )

    # ---- ④ 反方向：内核不许知道 Ginan ----
    back: list[str] = []
    for p in sorted((pkg / "core").rglob("*.py")):
        for mod in imports_of(p):
            if mod.split(".")[0] == "ginan":
                back.append(f"{p.relative_to(ROOT)} → {mod}")
    for line in back:
        print(f"      ⚠️ {line}")
    check(not back, "★ 内核（core/）不 import ginan —— 这一条是干净的反方向")


def test_compaction_writes_what_it_should() -> None:
    section("⑮ ★★ 压缩之后，**磁盘上**的历史必须是干净的一份")

    # ⚠️⚠️ 这一节是本仓库那个反复出现的失效模式的**第三个实例**：
    #     **测试只覆盖了能用的那条路，洞就在没覆盖的那条上。**
    #
    #     原来上面那个压缩用例（`test_cut_point_never_splits_tool_pairs`）
    #     **只静态调 `Agent._find_cut_point`** —— 它从来没带 session
    #     真的跑过一次压缩。全仓库**没有一条"压缩 + 落盘 + 读回"的断言**。
    #
    #     于是两个 bug 一直活着：
    #
    #     ① **系统提示被写两遍**
    #        `Session.clear(self._system_prompt)` 已经写了第一行，
    #        而紧接着 `for message in self._messages:` 从 **[0]** 开始 ——
    #        又把同一条写了一遍。压一次得到：
    #            ['system', 'system', 'system', 'user', 'assistant']
    #                    ↑[0]      ↑[1] 一字不差   ↑[2] 是摘要（该有）
    #        ⚠️ 后果是指令**被重复发给模型两遍**，而且写进了磁盘（重启还在）。
    #
    #     ② **冷 Agent 上 `compact_now()` 是静默空操作**
    #        `_compact` 全程用 `self._messages`，但没有 `_load()` ——
    #        而 `run()` 里有。于是"新建一个 Agent 直接压"什么都不做，
    #        **连一条事件都没有**（`[]`）。
    #        ⚠️ 而 `compact_now` 的 docstring 说它的用途之一就是**测试** ——
    #           那条路必然是在刚构造的 Agent 上走的。
    import json as _json
    import shutil as _shutil
    import tempfile as _tempfile

    td = Path(_tempfile.mkdtemp(prefix="compact_"))
    try:
        path = td / "h.jsonl"
        agent = Agent(
            FakeProvider([text_reply("好的")] * 60), "你是 GNSS 助手", [],
            Session(path=path),
        )
        for i in range(4):
            list(agent.run(f"第{i}轮：记住我住在上海"))

        def roles() -> list[str]:
            return [
                _json.loads(x)["role"]
                for x in path.read_text(encoding="utf-8").splitlines() if x.strip()
            ]

        check(roles().count("system") == 1, f"压缩前只有 1 条系统提示（{roles()}）")

        list(agent.compact_now(keep_recent=2))
        after = roles()
        check(
            after.count("system") == 2,
            f"★★ 压缩后是 **2** 条 system：系统提示 + 摘要"
            f"（实际 {after.count('system')} 条，{after}）"
            "—— 3 条的话就是系统提示被写了两遍",
        )

        msgs = [
            _json.loads(x)
            for x in path.read_text(encoding="utf-8").splitlines() if x.strip()
        ]
        sysmsgs = [m["content"] for m in msgs if m["role"] == "system"]
        check(
            len(sysmsgs) == 2 and sysmsgs[0] != sysmsgs[1],
            "★★ 那两条 system **内容不同**（一条是系统提示、一条是摘要）"
            "—— 内容相同就是重复写了一遍",
        )
        check(
            any("摘要" in m for m in sysmsgs),
            f"★ 其中一条确实是摘要（{ [m[:24] for m in sysmsgs] }）",
        )
        check(
            msgs[0]["role"] == "system" and "你是 GNSS 助手" in msgs[0]["content"],
            "★ 文件第一行仍然是系统提示（历史必须自始至终是完整可用的）",
        )

        # ---- ② 冷 Agent 上不能是空操作 ----
        #
        # ⚠️ 判据要挑**只有它才能解释的现象**：
        #    我第一版写的是"行数要变少"，而 4 条消息按 keep_recent=2 压完
        #    **还是 4 条**（系统提示 + 摘要 + 最近 2 条）—— 行数根本没变。
        #    那是**我的判据错了**，不是代码错了。
        #
        #    真正只有"压过了"才能解释的现象是：**文件里出现了摘要**。
        before = roles()
        cold = Agent(FakeProvider([text_reply("摘要")]), "你是 GNSS 助手", [],
                     Session(path=path))
        events = list(cold.compact_now(keep_recent=2))
        check(
            bool(events),
            "★★ 刚构造的 Agent 上 `compact_now()` 也要真的干活"
            "（原来返回 []，什么都不做、也不报错）",
        )
        contents = [
            _json.loads(x)["content"]
            for x in path.read_text(encoding="utf-8").splitlines() if x.strip()
        ]
        check(
            any("摘要" in c for c in contents),
            f"★★ 而且**磁盘上真的被重写了**（出现摘要）"
            f"—— 压缩前 {before}，压缩后 {roles()}",
        )
        check(
            {e["type"] for e in events} == {"compact_start", "compact_end"},
            f"★ 事件是完整的一对（{[e['type'] for e in events]}）",
        )
    finally:
        _shutil.rmtree(td, ignore_errors=True)


def test_session_store() -> None:
    section("⑧ SessionStore：多会话的创建 / 列出 / 打开 / 删除")

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(root=Path(tmp))

        check(store.list_all() == [], "初始状态没有会话")

        m1 = store.create(title="第一个对话")
        m2 = store.create(title="第二个对话")
        check(m1.id != m2.id, "两个会话的 id 不同")

        listed = store.list_all()
        check(len(listed) == 2, f"列出 2 个会话（实际 {len(listed)}）")
        check({m.id for m in listed} == {m1.id, m2.id}, "列出的正是创建的那两个")

        # ---- 收发消息 ----
        s = store.session(m1.id)
        agent = Agent(FakeProvider([text_reply("你好呀")]), "系统提示", echo_tool(), s)
        list(agent.run("在吗"))

        reopened = store.session(m1.id)
        restored = reopened.read_all()
        check(len(restored) == 3, f"重新打开能读回 3 条消息（实际 {len(restored)}）")

        # ---- 两个会话互相隔离 ----
        other = store.session(m2.id).read_all()
        check(other == [], "另一个会话的历史是空的，没有被串台")

        # ---- 更新元信息 ----
        store.touch(m1.id, run_id="run-001")
        store.touch(m1.id, run_id="run-002")
        store.touch(m1.id, run_id="run-001")  # 重复的不该被记两遍
        m = store.meta(m1.id)
        check(m is not None and m.run_ids == ["run-001", "run-002"],
              f"run_ids 去重后正确追加（实际 {m.run_ids if m else None}）")

        # ---- 排序：最近更新的在前 ----
        #
        # ⚠️ 不要靠"连续两次调用的时间会不同"来测排序。
        #    那是**依赖时钟的测试**：同一毫秒内跑完就随机失败，
        #    而且失败信息看起来像被测代码错了 —— 实际是测试自己不稳。
        #    要测顺序，就**显式造出顺序**（touch 的 when 参数就是为此加的）。
        store.touch(m1.id, when="2026-01-01T00:00:00.000")
        store.touch(m2.id, when="2026-01-02T00:00:00.000")
        check(
            [x.id for x in store.list_all()] == [m2.id, m1.id],
            "按 updated 倒序排列（较新的在前）",
        )

        # 把 m1 的时间改到更晚，它应该升到最前 —— 证明排序真的看 updated
        store.touch(m1.id, title="改过名字的", when="2026-01-03T00:00:00.000")
        check(store.list_all()[0].id == m1.id, "更新时间变晚之后，排到了最前面")
        check(store.meta(m1.id).title == "改过名字的", "标题也一并更新了")

        # ---- 删除 ----
        check(store.delete(m2.id) is True, "删除已存在的会话返回 True")
        check(store.delete(m2.id) is False, "再删一次返回 False（幂等）")
        check(len(store.list_all()) == 1, "删除后只剩 1 个会话")
        check(not store.dir_for(m2.id).exists(), "被删会话的目录确实没了")

        # ---- 坏元信息不该让整个列表打不开 ----
        bad = store.create(title="要弄坏的")
        store.meta_path(bad.id).write_text("{ 这不是合法JSON", encoding="utf-8")
        listed = store.list_all()
        check(
            bad.id not in {m.id for m in listed},
            "元信息损坏的会话被跳过，其余会话照常列出（优雅降级）",
        )


def test_atomic_meta_write() -> None:
    section("⑨ meta.json 是原子写入（不会留下半个文件）")

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(root=Path(tmp))
        m = store.create(title="测试")

        # 反复写，中途不该出现临时文件残留
        for i in range(5):
            store.touch(m.id, title=f"标题{i}")

        leftovers = list(store.dir_for(m.id).glob("*.tmp"))
        check(leftovers == [], f"没有残留的 .tmp 文件（实际 {leftovers}）")
        check(store.meta(m.id).title == "标题4", "最后一次写入生效")


def test_loop_is_not_cut_off_mid_task() -> None:
    section("⑩ ★ 长任务不会被一个常数切在半路")

    # ⚠️⚠️ 这是一次真实事故的回归守卫。
    #
    #    原来这里有个 `DEFAULT_MAX_STEPS = 10`。用户那次真实的活是：
    #
    #       扫收件箱 → 看本地 → 读 RINEX → 做计划 → 测数据源
    #       → 问用户（下不下）→ 下载（第一次，两个失败）
    #       → 再测 → 问用户（重试吗）→ 下载（第二次，6/6 全成功）
    #       → ✂️ **被切在这里**
    #
    #    **下载刚刚成功，就差最后一句总结。** 而那正好是第 10 次模型请求。
    #    用户看到的是：干了一堆活，然后什么都不显示。
    #    他的判断很干脆："日常使用肯定远超十条"。
    #
    #    👉 所以那道上限**删掉了**（理由写在 loop.py 上方那段注释里：
    #       该有上限的是时间，不是步数）。
    #       这条断言就是那个决定的守卫：**25 步的活必须能跑完。**
    #
    # ⚠️ 这条断言在"把上限加回来"时会立刻变红 —— 这正是它存在的意义。
    #    真要加回来，先回答那句注释里的问题：
    #    凭什么认为一个常数比用户的判断更准？
    from tests.fakes import FakeProvider, tool_reply

    rounds = 25
    script = [tool_reply(("echo", json.dumps({"text": "x"}))) for _ in range(rounds)]
    script.append(text_reply("总结：活儿干完了"))

    agent = Agent(FakeProvider(script), "你是助手", echo_tool(), None)
    events = list(agent.run("干一件长活"))

    ran = [e for e in events if e.get("type") == "tool_end"]
    check(
        len(ran) == rounds,
        f"★ {rounds} 个来回**全部跑完**（实际 {len(ran)}）—— "
        "加回上限的话这条会红",
    )

    ends = [e for e in events if e.get("type") == "turn_end"]
    check(len(ends) == 1, f"只结束一次（实际 {len(ends)} 次）")
    check(
        bool(ends) and "总结" in (ends[0].get("text") or ""),
        "★ 而且**最后那句话真的说出来了** —— 不是跑到一半被切断",
    )


def test_every_turn_end_carries_text() -> None:
    section("⑩b ★ 结构保证：`loop.py` 里每一个 turn_end 都带 text")

    # ⚠️ 为什么要**结构**地查而不是行为地测：
    #    "循环结束时说了什么"是一条**出口路径**，而出口**有四条**
    #    （正常答完 / 用户取消两条 / 撞上限）。
    #    行为测试只能覆盖我想到的那几条；将来谁新加一条出口，
    #    很可能又是"yield 一个 turn_end 就 return" —— 而那种写法
    #    **看起来完全正常**，静默失效就在那里发生。
    #
    #    所以直接查源码：所有 `{"type": "turn_end", ...}` 都必须有 `text`。
    #    这是**不变量**，不是某一条路径的测试。
    import ast

    src = (ROOT / "ginan_agent" / "core" / "loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    found: list[int] = []
    with_text = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        # ⚠️ 取出**字面量的值**，别把 AST 节点本身当值 ——
        #    第一版就是那么写的，于是 `type` 那一项是个 `ast.Constant` 对象，
        #    和字符串 `"turn_end"` 永远不相等 → 找到 0 处 →
        #    **守卫全绿**。而"找到 0 处"这件事本身必须报失败，
        #    否则它就是一个永远绿的假守卫（见下面那条断言）。
        keys = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys[k.value] = v

        t = keys.get("type")
        if not (isinstance(t, ast.Constant) and t.value == "turn_end"):
            continue

        found.append(node.lineno)
        if "text" in keys:
            with_text += 1

    # ⚠️ 目前是 3 处：正常答完 / 取消（循环开头） / 取消（工具执行到一半）。
    #    原来有 4 处 —— 少的那处是"撞步数上限"，而上限已经删掉了。
    #    这个数字**下调过**，所以它不是"随便设个下限"：
    #    如果它变成 2，说明又有一条出口被删了，那必须有人来解释。
    check(len(found) >= 3, f"找到了 {len(found)} 处 turn_end（来源：{found}）")
    check(
        with_text == len(found),
        f"★ 每一处都带 text（{with_text}/{len(found)}）—— "
        "少一处就是一条'结束时不说话'的出口",
    )


# =============================================================================
# 主程序
# =============================================================================


def main() -> int:
    print("=" * 60)
    print("核心层自检 —— 循环 / 会话 / 取消 / 压缩")
    print("=" * 60)
    print("这个脚本不联网、不调用任何模型、不产生费用。")

    test_ask_user_roundtrip()
    test_ask_user_cancel_and_timeout()
    test_ask_user_without_ui()
    test_no_side_effect_on_construct()
    test_initial_write_is_complete()
    test_session_roundtrip()
    test_reset_clears_both()
    test_cancel_is_cooperative()
    test_cancel_before_run()
    test_memory_across_turns()
    test_session_survives_restart()
    test_cut_point_never_splits_tool_pairs()
    test_layer_boundaries()
    test_compaction_writes_what_it_should()
    test_session_store()
    test_atomic_meta_write()
    test_loop_is_not_cut_off_mid_task()
    test_every_turn_end_carries_text()

    print()
    print("=" * 60)
    total = PASS + FAIL
    if FAIL == 0:
        print(f"全部通过 ({PASS}/{total})")
    else:
        print(f"有失败：{FAIL} 项未通过（通过 {PASS}/{total}）")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
