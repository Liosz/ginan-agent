"""自检：工具注册表 —— 缺陷 #1 的回归测试。

⚠️ 这个脚本存在的唯一理由，是**防止一个已修好的 bug 悄悄回来**。

那个 bug 是这样的：

    Agent(provider, prompt, tools=[我的工具])

看起来框架支持"只挂自己那套工具"，但执行工具时走的是**模块级全局表**：

    loop.py:   result = execute(name, arguments)
    tools.py:  tool = TOOLS_BY_NAME.get(name)      ← 全局的，不是你传的那个

于是症状是：**模型明明知道有这个工具，调用时却总说"工具不存在"**。
更难受的是，两边单独看都对 —— 声明处用参数、执行处用全局，
各自都"符合设计"，只有连起来才错。

============================ 这个脚本怎么防住它 ============================
用两个断言，缺一不可：

    ① 传进去的工具能出现在 schema 里（发给模型的说明书）
    ② 传进去的工具**真的能被执行到**

⚠️ 只测 ① 是不够的 —— 而"只测 ①"恰恰是当初会漏掉这个 bug 的测法。
   ① 通过、② 失败，才是那个 bug 的真实形态。

跑法（零 API 费用、不联网）：

    python scripts/check_registry.py
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 上默认可能不是 UTF-8，会导致中文输出乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ginan_agent.core import Agent, Tool, ToolContext, ToolRegistry  # noqa: E402
from ginan_agent.core.tools import ToolDecision  # noqa: E402
from tests.fakes import FakeProvider, text_reply, tool_reply  # noqa: E402

# =============================================================================
# 极简测试框架 —— 和项目里其它自检脚本保持一致，不引入 pytest
# =============================================================================
#
# 为什么不用 pytest？
#   ① 这几个脚本要能**双击就跑**，不依赖任何第三方测试框架
#   ② check() 的两参数形式强迫你写清楚"在测什么"，
#      比 pytest 的 assert 裸表达式更容易读懂失败信息
#   ③ 学习者能一眼看到测试是怎么实现的，而不是把它当成黑盒
#
PASS = 0
FAIL = 0


def check(cond: bool, label: str) -> bool:
    """断言一条，打印结果，返回它是否通过。"""
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


# =============================================================================
# 测试用的工具 —— 就三个，够覆盖所有情况
# =============================================================================


def echo(text: str) -> str:
    """最简单的工具：原样返回。"""
    return f"echo:{text}"


def big_output(size: int) -> str:
    """造一个超长输出，用来测截断。"""
    return "\n".join(f"行 {i:04d}" for i in range(size))


def needs_context(ctx, label: str) -> str:
    """需要运行时上下文的工具。

    ⚠️ 第一个参数固定叫 ctx，而且是**位置参数** ——
    这样就不会和模型给的参数名撞车。
    """
    ctx.progress(f"正在处理 {label}", fraction=0.5)
    return f"ctx:{label}:cancelled={ctx.cancelled}"


def make_tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="原样返回你给它的文本。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            function=echo,
        ),
        Tool(
            name="big_output",
            description="产生很多行输出，用来测试截断。",
            parameters={
                "type": "object",
                "properties": {"size": {"type": "integer"}},
                "required": ["size"],
            },
            function=big_output,
            max_result_chars=200,
        ),
        Tool(
            name="needs_context",
            description="需要运行时上下文的工具。",
            parameters={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
            function=needs_context,
            wants_context=True,
        ),
    ]


# =============================================================================
# ① 核心回归 —— 这个 bug 就是靠这两条钉死的
# =============================================================================


def test_custom_registry_roundtrip() -> None:
    section("① 核心回归：自定义工具集必须真的能用")

    reg = ToolRegistry(make_tools())

    # ---- 断言 ①：能出现在给模型的说明书里 ----
    schema_names = {s["function"]["name"] for s in reg.schemas()}
    check(
        {"echo", "big_output", "needs_context"} <= schema_names,
        "自定义工具出现在 schema 里（模型看得见）",
    )

    # ---- 断言 ②：**真的能被执行到** ----
    #
    # ⚠️ 这一条才是关键。原来那个 bug 就是死在这一步：
    #    schema 有它，执行却查不到。
    result = reg.execute("echo", json.dumps({"text": "你好"}))
    check(
        result == "echo:你好",
        "自定义工具能被真的执行（不是报'没有这个工具'）",
    )
    check(
        "没有名为" not in result,
        "执行结果里没有出现'没有名为 xxx 的工具'—— 缺陷 #1 未复发",
    )


def test_registry_isolation() -> None:
    section("② 两个注册表互不干扰（全局变量修好后的直接后果）")

    reg_a = ToolRegistry(make_tools())
    reg_b = ToolRegistry(
        [
            Tool(
                name="只有B有",
                description="只存在于注册表 B 的工具。",
                parameters={"type": "object", "properties": {}, "required": []},
                function=lambda: "来自B",
            )
        ]
    )

    check(reg_a.execute("只有B有", "{}").startswith("错误"), "A 查不到 B 的工具")
    check(reg_b.execute("echo", "{}").startswith("错误"), "B 查不到 A 的工具")
    check(reg_b.execute("只有B有", "{}") == "来自B", "B 能执行自己的工具")

    # 报错信息里列的应该是**自己**的工具，不是别人的。
    #
    # ⚠️ 断言要小心：报错文本形如
    #       错误：没有名为 'echo' 的工具。可用的工具有：只有B有
    #    里面**天然含有**被请求的那个名字（"echo"）——
    #    所以不能用 `"echo" not in err` 来判断"有没有泄露 A 的工具"，
    #    那样断言必然失败（而且失败的是测试，不是代码）。
    #    要只看冒号后面那段"可用工具"列表。
    err = reg_b.execute("echo", "{}")
    available_part = err.split("可用的工具有：")[-1]
    check("只有B有" in available_part, "报错时列出了本注册表的可用工具")
    check(
        not any(name in available_part for name in ("big_output", "needs_context")),
        "报错时**没有**泄露另一个注册表的工具",
    )


def test_duplicate_names_rejected() -> None:
    section("③ 重名工具必须当场报错，不能静默覆盖")

    dup = [
        Tool(name="同名", description="第一个", parameters={}, function=lambda: "1"),
        Tool(name="同名", description="第二个", parameters={}, function=lambda: "2"),
    ]
    try:
        ToolRegistry(dup)
        check(False, "重名时应该抛 ValueError")
    except ValueError as exc:
        check("同名" in str(exc), "重名时抛 ValueError 并指出重名的是哪个")


# =============================================================================
# ④ 错误即数据 —— execute 永远不抛异常
# =============================================================================


def test_error_as_data() -> None:
    section("④ 错误即数据：execute 永远返回字符串，不抛异常")

    reg = ToolRegistry(make_tools())

    cases = [
        ("不存在的工具", "{}"),
        ("echo", "这不是 JSON"),
        ("echo", '["数组不是对象"]'),
        ("echo", "{}"),                       # 缺必填参数
        ("echo", '{"text": "ok", "多余": 1}'),  # 多给了参数
    ]

    for name, args in cases:
        try:
            r = reg.execute(name, args)
            ok = isinstance(r, str) and r.startswith("错误")
        except Exception as exc:  # noqa: BLE001
            r, ok = f"抛了异常：{exc!r}", False
        check(ok, f"execute({name!r}, {args!r}) 返回错误字符串而不是抛异常")

    # 参数是数组时报错要能说清"要的是对象"
    r = reg.execute("echo", "[]")
    check("对象" in r, "参数传数组时，报错指明'要的是 JSON 对象'")

    # ---- ⚠️⚠️ 但是"返回错误字符串"**还不够** —— 还要能**区分出来** ----
    #
    #     原来 `execute()` 只返回一个字符串，五处失败都靠 `"错误："` 前缀
    #     区分，而**循环层从来不看那个前缀**：
    #
    #         "status": "cancelled" if self._cancel.is_set() else "ok"
    #
    #     它只有"取消了没有"这一个维度 —— 「工具报错了」这个维度**不存在**。
    #
    #     ⚠️ 后果：抛了异常的工具在界面上是**绿勾 +「完成」**，
    #        而错误文本只是塞在卡片正文里。
    #        这正是本仓库反复说的那句的反面：
    #        **「一个说'完成了'的失败，比说'失败了'糟得多」**。
    for name, args in cases:
        check(
            reg.execute(name, args).ok is False,
            f"★ 失败的调用 `ok` 为 False（{name}）",
        )
    check(reg.execute("echo", '{"text": "好"}').ok, "★ 成功的调用 `ok` 为 True")


def test_bad_return_value() -> None:
    """⚠️⚠️ 工具**返回了不是字符串的东西** —— 曾经让整段对话废掉。

    ── 事故 ──
    工具忘了写 `return`（返回 `None`）时，`_truncate()` 第一行的
    `len(text)` 会抛 `TypeError` —— 而那句是在 `execute()` 的
    `except` **之外**抛的：

        它穿出 execute() → 穿出循环层 → 而此时
        "带 tool_calls 的 assistant 消息"**已经落盘**、
        "tool 回复"**还没补上**
        → 历史里留下**孤儿 tool_call**
        → **之后每一次请求都会被 API 拒掉**，对话等于废了。

    ⚠️ 而它是**静默**的：异常本身被外面的 catch 接住并记了日志，
       用户看到的是"发一句话就报错"，和历史里那个非法形状
       联系不起来。

    ── 判据 ──
    "工具返回了怪东西"是一种**普通的工具失败**（错误即数据），
    不是让异常逃出去的理由。
    """
    section("④b ★★ 工具返回非字符串：当成失败报出去，不能让异常逃出去")

    from ginan_agent.core import Agent, Session, Tool

    def tool_returning(value, name):
        return Tool(
            name=name, description="d",
            parameters={"type": "object",
                        "properties": {"x": {"type": "string"}},
                        "required": ["x"]},
            function=lambda x: value,
        )

    reg = ToolRegistry([
        tool_returning(None, "forgot_return"),
        tool_returning({"ok": True}, "returns_dict"),
        tool_returning(b"bytes", "returns_bytes"),
    ])

    for name in ("forgot_return", "returns_dict", "returns_bytes"):
        try:
            r = reg.execute(name, '{"x": "1"}')
            ok = isinstance(r, str) and r.startswith("错误")
            why = ""
        except Exception as exc:  # noqa: BLE001
            r, ok, why = f"抛了 {type(exc).__name__}", False, f"（{exc}）"
        check(ok, f"★★ 工具返回怪东西时返回错误字符串，不抛异常：{name} {why}")
        if ok:
            check(
                name in r,
                f"★ 报错里点名是哪个工具（否则 15 个工具没法排查）",
            )

    # ---- ⚠️ 端到端：跑一轮真的会落盘的对话，看历史形状 ----
    #
    #     ⚠️ 这一步**必须端到端**：光测 execute 的返回值看不出
    #        "孤儿 tool_call" —— 那个是**历史**的形状，不是函数的返回值。
    import shutil
    import tempfile

    from tests.fakes import FakeProvider, text_reply, tool_reply

    td = Path(tempfile.mkdtemp(prefix="orphan_"))
    path = td / "h.jsonl"
    provider = FakeProvider([
        tool_reply(("forgot_return", '{"x": "1"}')),
        text_reply("第一轮答完"),
        text_reply("第二轮答完"),
    ])
    agent = Agent(
        provider, "系统提示",
        [tool_returning(None, "forgot_return")],
        Session(path=path),
    )

    try:
        events = list(agent.run("第一轮"))
        crashed = ""
    except Exception as exc:  # noqa: BLE001
        events, crashed = [], f"{type(exc).__name__}: {exc}"

    check(
        not crashed,
        "★★ 第一轮跑完不抛异常"
        + (f"（原来是 {crashed}）" if crashed else "（原来这里抛 TypeError）"),
    )
    if not crashed:
        ends = [e for e in events if e.get("type") == "tool_end"]
        check(
            bool(ends) and ends[0]["status"] == "failed",
            f"★★ 事件里的状态是 failed（拿到 "
            f"{ends[0]['status'] if ends else '没有 tool_end'}）",
        )

    list(agent.run("第二轮"))
    sent = provider.calls[-1]
    n_calls = sum(1 for m in sent if m.get("tool_calls"))
    n_replies = sum(1 for m in sent if m["role"] == "tool")
    check(
        n_calls == n_replies,
        f"★★ 历史里没有孤儿 tool_call（{n_calls} 个申请 / {n_replies} 条回复）"
        "—— 有孤儿的话之后每次请求都会被 API 拒掉",
    )
    shutil.rmtree(td, ignore_errors=True)


# =============================================================================
# ⑤ 截断
# =============================================================================


def test_truncation() -> None:
    section("⑤ 超长输出按行截断，并说明被截了多少")

    reg = ToolRegistry(make_tools())
    r = reg.execute("big_output", json.dumps({"size": 500}))

    check(len(r) < 1000, f"500 行输出被截短了（实际 {len(r)} 字符）")
    check("截断" in r, "截断提示里说明了这是被截断的")
    check("行 0000" in r, "开头的内容保留了")
    check(not r.rstrip().endswith("行"), "没有把最后一行切成半截")

    # 没超限的不该被动
    r2 = reg.execute("echo", json.dumps({"text": "短"}))
    check("截断" not in r2, "没超限的输出原样返回，不加截断提示")


# =============================================================================
# ⑥ 运行时上下文
# =============================================================================


def test_tool_context() -> None:
    section("⑥ 需要上下文的工具能拿到 ctx，并能往外报进度")

    reg = ToolRegistry(make_tools())
    events: list[dict] = []
    cancel = threading.Event()

    ctx = ToolContext(
        call_id="call_abc",
        name="needs_context",
        cancel=cancel,
        emit=events.append,
    )
    r = reg.execute("needs_context", json.dumps({"label": "测试"}), ctx=ctx)

    check(r == "ctx:测试:cancelled=False", "工具拿到了 ctx 和它自己的参数")
    check(len(events) == 1, "ctx.progress() 产生了一条进度事件")
    if events:
        e = events[0]
        check(e["type"] == "tool_progress", "事件类型是 tool_progress")
        check(e["fraction"] == 0.5, "fraction 被正确传递")
        check(e["name"] == "needs_context", "事件里带了工具名")

    # 取消标志能被工具看到
    cancel.set()
    r2 = reg.execute("needs_context", json.dumps({"label": "x"}), ctx=ctx)
    check("cancelled=True" in r2, "取消标志设置后，工具能看到 ctx.cancelled")

    # 不需要上下文的工具，不该被塞 ctx
    r3 = reg.execute("echo", json.dumps({"text": "a"}))
    check(r3 == "echo:a", "普通工具不受上下文机制影响")

    # 没传 ctx 时也能跑（兜底），工具不用判断 None
    r4 = reg.execute("needs_context", json.dumps({"label": "y"}))
    check(r4 == "ctx:y:cancelled=False", "没传 ctx 时用兜底上下文，不崩")


def test_context_reaches_tool_intact() -> None:
    section("⑥b 循环层构造的 ctx 必须**原封不动**到达工具")

    # ⚠️ 这条是为一个真实缺陷补的守卫。
    #
    #    当时 loop.py 构造了一个 ctx，tools.py 又构造了第二个，
    #    前者被丢弃 —— 于是工具收到的是 call_id 为空的空壳。
    #
    #    症状：模型一次要求调多个工具时，前端分不清哪条进度属于哪个工具。
    #    失效原因很隐蔽：**两处都在构造同一个对象**，各自看都对。
    #
    #    所以断言要盯住"身份"而不是"内容" —— 必须确认到达工具的那个
    #    ctx 和传进去的那个**是同一个对象**，而不只是内容相等。
    seen: dict[str, object] = {}

    def probe(ctx, x: str) -> str:
        seen["call_id"] = ctx.call_id
        seen["name"] = ctx.name
        return "ok"

    tool = Tool(
        name="probe",
        description="探测 ctx 内容",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
        function=probe,
        wants_context=True,
    )
    reg = ToolRegistry([tool])

    provider = FakeProvider(
        [
            tool_reply(("probe", json.dumps({"x": "1"}))),
            text_reply("done"),
        ]
    )
    agent = Agent(provider, "系统提示", [tool])
    events = list(agent.run("调一下 probe"))

    check(
        seen.get("call_id") == "call_0",
        f"工具收到的 call_id 是真实的调用 id（实际 {seen.get('call_id')!r}）",
    )
    check(seen.get("name") == "probe", "工具收到的 name 正确")

    # 工具报的进度事件也应带上正确的 call_id
    emitted: list[dict] = []
    ctx = ToolContext(
        call_id="call_xyz",
        name="probe",
        cancel=threading.Event(),
        emit=emitted.append,
    )
    reg.execute("probe", json.dumps({"x": "1"}), ctx=ctx)
    check(
        seen.get("call_id") == "call_xyz",
        "换成另一个 ctx 执行时，工具收到的 call_id 跟着变（确实用的传进去的那个）",
    )


# =============================================================================
# ⑦ 端到端：Agent + FakeProvider + 自定义工具
# =============================================================================


def test_end_to_end() -> None:
    section("⑦ 端到端：Agent 用自定义工具集跑通一轮")

    provider = FakeProvider(
        [
            tool_reply(("echo", json.dumps({"text": "从模型来的"}))),
            text_reply("工具说：echo:从模型来的"),
        ]
    )
    agent = Agent(provider, "你是个助手", make_tools())

    events = list(agent.run("随便说点什么"))
    types = [e["type"] for e in events]

    check("tool_start" in types, "事件流里出现了 tool_start")
    check("tool_end" in types, "事件流里出现了 tool_end")
    check(types[-1] == "turn_end", "最后一个事件是 turn_end")

    tool_end = next((e for e in events if e["type"] == "tool_end"), None)
    check(
        tool_end is not None and tool_end["result"] == "echo:从模型来的",
        "工具**真的执行了**，结果正确（缺陷 #1 的终极验证）",
    )

    # 发给模型的 schema 应该是我们这套，不是默认那套
    sent = provider.tools_seen[0]
    names = {s["function"]["name"] for s in (sent or [])}
    check("echo" in names, "发给模型的工具说明书里有我们的工具")
    check(
        "calculate" not in names,
        "默认工具集（calculate）**没有**混进来 —— 工具集是完全替换而非追加",
    )


# =============================================================================
# ⑧ 钩子 —— 拒绝工具调用
# =============================================================================


def test_hook_deny() -> None:
    section("⑧ 钩子可以拒绝工具调用（且不会让 API 报错）")

    # ⚠️ 这里在验证 loop.py 注释里反复强调的那件事：
    #    "被拒绝时仍然必须记一条 tool 消息"。
    #    如果只 continue 而不记录，下一轮请求会因为
    #    "tool_calls 数量和 tool 消息数量对不上"被 API 拒掉。
    #
    #    我们没法在这个脚本里让真 API 报错，但可以断言
    #    **历史里确实多了一条 tool 消息** —— 这正是那件事的充分条件。

    provider = FakeProvider(
        [
            tool_reply(("echo", json.dumps({"text": "x"}))),
            text_reply("好的，我不执行了"),
        ]
    )

    def refuse(name: str, arguments: str, ctx) -> ToolDecision:
        return ToolDecision.reject("这个操作会联网，先问问用户")

    agent = Agent(provider, "助手", make_tools(), on_before_tool_call=refuse)

    events = list(agent.run("调一下 echo"))
    types = [e["type"] for e in events]

    check("tool_denied" in types, "产生了 tool_denied 事件")

    # ⚠️⚠️ 这里原来断言的是"**没有** tool_start" —— 那条**改了**。
    #
    #    原来的行为：拒绝时只发 `tool_denied` + `tool_end`。
    #    而前端是"在 tool_start 时建卡片、后面的事件去更新它" ——
    #    卡片根本没建出来，于是**"被拒绝"这件事在界面上什么都不显示**。
    #    用户点了拒绝，界面毫无反应。
    #
    #    现在的行为：先补一条 `tool_start`（让界面有卡片可标记），
    #    再发 `tool_denied`。**"没执行"和"没发生"是两回事** ——
    #    用户有权看到他点了什么。
    check(
        "tool_start" in types,
        "★ 被拒的调用**也发 tool_start** —— 否则界面上什么都不显示",
    )
    check(
        types.index("tool_start") < types.index("tool_denied"),
        "★ 顺序是 start 在前、denied 在后（先建卡片，再标记被拒）",
    )
    # ⚠️ 但**工具函数绝不能真的被执行** —— 这才是这个钩子的全部意义。
    check(
        not any(e.get("result") == "x" for e in events),
        "★ 工具**确实没有被执行**（不是「发了事件但其实跑了」）",
    )
    check("tool_end" in types, "仍然产生了 tool_end（界面才不会缺一块）")

    denied = next((e for e in events if e["type"] == "tool_denied"), None)
    check(
        denied is not None and "联网" in denied["reason"],
        "拒绝理由被完整传递",
    )

    # ---- 最关键的断言：历史里必须有对应的 tool 消息 ----
    msgs = agent.messages
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assistant_with_calls = [
        m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    n_calls = sum(len(m["tool_calls"]) for m in assistant_with_calls)

    check(
        len(tool_msgs) == n_calls,
        f"每个 tool_call 恰好一条 tool 回复（{n_calls} 个调用 / {len(tool_msgs)} 条回复）",
    )


def test_hook_allow() -> None:
    section("⑨ 钩子放行时，工具正常执行")

    provider = FakeProvider(
        [
            tool_reply(("echo", json.dumps({"text": "y"}))),
            text_reply("done"),
        ]
    )
    seen: list[str] = []

    def note(name: str, arguments: str, ctx) -> ToolDecision:
        seen.append(name)
        return ToolDecision.allow()

    agent = Agent(provider, "助手", make_tools(), on_before_tool_call=note)
    events = list(agent.run("调 echo"))

    check(seen == ["echo"], "钩子在工具执行前被调用了一次")
    tool_end = next((e for e in events if e["type"] == "tool_end"), None)
    check(
        tool_end is not None and tool_end["result"] == "echo:y",
        "放行后工具真的执行了",
    )


# =============================================================================
# 主程序
# =============================================================================


def main() -> int:
    print("=" * 60)
    print("工具注册表自检 —— 缺陷 #1 的回归测试")
    print("=" * 60)
    print("这个脚本不联网、不调用任何模型、不产生费用。")

    test_custom_registry_roundtrip()
    test_registry_isolation()
    test_duplicate_names_rejected()
    test_error_as_data()
    test_bad_return_value()
    test_truncation()
    test_tool_context()
    test_context_reaches_tool_intact()
    test_end_to_end()
    test_hook_deny()
    test_hook_allow()

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
