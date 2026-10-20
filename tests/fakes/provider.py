"""假 Provider —— 按剧本表演的"模型"。

============================ 它和真 Provider 的关系 ============================
它只实现真 Provider 的**对外形状**：

    .config                     —— 配置对象（循环层要读 context_window）
    .chat_stream(messages, tools) —— 生成器，产出事件、return 一个 Reply
    .last_prompt_tokens         —— 上次用的 token 数
    .last_cache_hit_tokens / .last_cache_miss_tokens

⚠️ 注意它**没有继承** Provider，也没有实现 __init__ 里那些东西。
这是刻意的：如果 FakeProvider 继承真 Provider，那么真 Provider 改一处，
假对象就跟着变 —— 测试就不再是"独立验证"，而变成了"和被测代码一起改"。
**假对象应该是照着接口重写一遍，而不是继承。**
（这种做法在测试领域有个名字叫"鸭子类型的假对象"。）

============================ 剧本怎么演 ============================
按顺序给几轮回复，每轮要么是纯文字，要么是要求调工具：

    fake = FakeProvider([
        tool_reply("get_current_time", "{}"),   # 第 1 轮：模型要调工具
        text_reply("现在是下午三点"),             # 第 2 轮：模型给答案
    ])

剧本用完了还继续问，就返回一句兜底文字 —— 免得测试因为"剧本不够长"
而卡在循环里。

⚠️ 循环层**没有步数上限**了（原来有，见 `core/loop.py` 上方那段说明），
所以这个兜底**是必须的**：没有它，剧本用完就真的停不下来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ginan_agent.core.provider import ProviderConfig
from ginan_agent.core.types import Message, Reply


@dataclass
class ScriptedReply:
    """剧本里的一轮回复。

    用 dataclass 而不是裸元组，是为了让剧本书写起来能自解释 ——
    看到 `tool_reply("run_pea", '{"config": "a.yaml"}')` 比
    看到 `("run_pea", '{"config": "a.yaml"}')` 清楚得多。
    """

    text: str = ""
    # 每个元素是 (工具名, 参数JSON字符串)
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    finish_reason: str = "stop"


def text_reply(text: str, finish_reason: str = "stop") -> ScriptedReply:
    """造一轮"纯说话、不调工具"的回复。"""
    return ScriptedReply(text=text, finish_reason=finish_reason)


def tool_reply(*calls: tuple[str, str], text: str = "") -> ScriptedReply:
    """造一轮"要求调用工具"的回复。

    可以一次要调多个工具：

        tool_reply(("tool_a", "{}"), ("tool_b", '{"x":1}'))
    """
    return ScriptedReply(
        text=text,
        tool_calls=list(calls),
        finish_reason="tool_calls",
    )


class FakeProvider:
    """按剧本表演的假 Provider。"""

    def __init__(
        self,
        script: list[ScriptedReply] | None = None,
        *,
        context_window: int = 64_000,
        # 每段文本切片多长 —— 用来模拟"流式是一小片一小片来的"
        chunk_size: int = 4,
    ) -> None:
        self.script = list(script or [])
        self.chunk_size = chunk_size
        self.calls: list[list[Message]] = []   # 记录每次收到的消息，供断言
        self.tools_seen: list[list[dict] | None] = []  # 记录每次收到的工具说明

        self._config = ProviderConfig(context_window=context_window)
        self.last_prompt_tokens: int | None = None
        self.last_cache_hit_tokens = 0
        self.last_cache_miss_tokens = 0

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def chat_stream(self, messages, tools=None):
        """和真 Provider 一样：产出事件，return 一个 Reply。"""
        self.calls.append(list(messages))
        self.tools_seen.append(tools)

        # 剧本用完了 → 兜底给个结束语，免得测试卡在循环里
        if self.script:
            step = self.script.pop(0)
        else:
            step = text_reply("（剧本已用完）")

        # ---------- 模拟流式：一小片一小片地吐 ----------
        #
        # ⚠️ 为什么要切碎片？因为真实场景就是碎片的，
        # 而"碎片拼接"恰恰是最容易写错的地方（尤其是工具调用的参数）。
        # 不切的话，测试就测不到那条路径。
        for i in range(0, len(step.text), self.chunk_size):
            yield {"type": "text_delta", "text": step.text[i : i + self.chunk_size]}

        # ---------- 拼装最终消息 ----------
        raw: Message = {"role": "assistant", "content": step.text or None}
        tool_calls = []
        if step.tool_calls:
            for idx, (name, args) in enumerate(step.tool_calls):
                tool_calls.append(
                    {
                        "id": f"call_{idx}",
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    }
                )
            raw["tool_calls"] = tool_calls  # type: ignore[typeddict-unknown-key]

        # 模拟 token 统计 —— 粗糙但够用
        approx = sum(len(m.get("content") or "") for m in messages) // 4
        self.last_prompt_tokens = approx
        self.last_cache_hit_tokens = 0
        self.last_cache_miss_tokens = approx

        return Reply(
            text=step.text,
            tool_calls=tool_calls,  # type: ignore[arg-type]
            raw=raw,
            finish_reason=step.finish_reason,
            prompt_tokens=approx,
            completion_tokens=len(step.text) // 4,
        )


class ExplodingProvider:
    """一调用就抛异常的 Provider —— 用来测"循环层遇到异常怎么办"。

    ⚠️ 单独一个类而不是给 FakeProvider 加个开关，
    是因为这个假对象的**用途完全不同**：它不表演对话，它表演故障。
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("模拟的模型调用失败")
        self._config = ProviderConfig()
        self.last_prompt_tokens: int | None = None
        self.last_cache_hit_tokens = 0
        self.last_cache_miss_tokens = 0

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def chat_stream(self, messages, tools=None):
        raise self._exc
        yield  # pragma: no cover  —— 这一行让函数变成生成器，写法上是必须的
