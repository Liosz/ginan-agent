"""② 模型层 —— 把消息发给 LLM，拿回响应。

============================ 边界（坚决不做） ============================
- 不知道"工具"具体是什么、怎么执行 ← 它只负责把说明书递过去
- 不知道"对话进行到第几轮" ← 循环层的事
- 不做任何决策（要不要继续、要不要停）

判断方法：把这个文件单独拿走去做"翻译软件"，它应该依然能工作。
（阶段 2 之后它多了个 tools 参数，但那只是"帮忙递个说明书"，
  它依然不知道工具是干什么的、怎么执行的。）
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from .types import Message, Reply, TextDelta, ToolCall

# =============================================================================
# 重试策略（阶段 3B）
# =============================================================================
#
# ⚠️ 注意：哪些错误**能**重试，哪些**不能**。
#
# 能重试的共同点：**它们可能"自己好"**
#   APIConnectionError  —— 网络抖动、超时（APITimeoutError 是它的子类）
#   RateLimitError      —— 限流，等一会儿再来
#   InternalServerError —— 服务端 5xx，可能换了台机器就好了
#
# 不能重试的（我们**故意不抓**这些）：
#   AuthenticationError —— 401，Key 无效。重试一万次还是 401
#   BadRequestError     —— 400，我们自己的请求格式有问题
#
# 对不可重试的错误重试，只会让你晚 7 秒看到同样的报错，还浪费配额。
#
# 这些异常类都从 openai 包直接导入，不用记路径。
# 验证过 APITimeoutError 是 APIConnectionError 的子类，所以抓父类就够了。
RETRYABLE_ERRORS = (
    APIConnectionError,
    RateLimitError,
    InternalServerError,
)

# 最多尝试几次（含第一次）。
# 3 次意味着"首次 + 2 次重试"，最坏情况等待 1+2 = 3 秒左右。
MAX_ATTEMPTS = 3


def _backoff_delay(attempt: int) -> float:
    """算出第 attempt 次失败后该等几秒。

    两个机制：

    ① **指数退避（exponential backoff）**
       每次等待时间翻倍：1s → 2s → 4s → 8s...
       为什么？因为如果服务端过载，你立刻重试等于火上浇油。
       越失败越要等久一点，给服务端喘息时间。

    ② **抖动（jitter）**
       在结果上加一个 0.5~1.5 倍的随机系数。
       为什么？假设 1000 个客户端同时被限流，如果都等"正好 1 秒"，
       那第 1 秒会有 1000 个请求同时到达 —— 服务器又被冲垮。
       加了随机偏移，请求就被摊平到一段时间里。
       这叫防治「惊群效应（thundering herd）」。

    第 1 次失败等 ~1s，第 2 次等 ~2s，第 3 次等 ~4s（都在 ±50% 内浮动）。
    """
    base = 2 ** (attempt - 1)  # attempt=1 → 1, 2 → 2, 3 → 4
    return base * random.uniform(0.5, 1.5)

# =============================================================================
# 默认配置常量
# =============================================================================
#
# ⚠️ 这些**仍然是默认值，但不再是唯一来源**。
#
# ⚠️ 这些值**曾经是硬编码在源码里的** —— 想改基准地址得改源码，
#    想改窗口大小也得改源码。那对一个要给新手用的桌面应用是致命的：
#
#   · 用户要填自己的 API Key  → 不能从源码里的环境变量名读
#   · 用户在公司网络里要配代理 → 得有地方填
#   · 用户想用别的模型         → 不该改代码
#
# 所以下面这些常量降级为"默认值"，实际使用的是一个 ProviderConfig 对象。
# 这又是同一个手法的第三次出现：**把隐式全局状态变成显式可传对象**
# （tools.py 的工具注册表、session.py 的工作目录、这里的配置）。

# DeepSeek 提供了「OpenAI 兼容接口」—— 意思是它的 API 格式和 OpenAI 一模一样，
# 所以我们能用 OpenAI 官方 SDK，只要把地址改成 DeepSeek 的就行。
BASE_URL = "https://api.deepseek.com"

# 可选模型：
#   deepseek-flash   —— 更快更便宜（当前默认）
#   deepseek-v4-pro  —— 能力更强，工具调用更稳
DEFAULT_MODEL = "deepseek-flash"

# 采样温度：0 最确定，越大越随机。
#
# 为什么 agent 要用 0？
#   我们要的是"可靠"，不是"惊喜"。
#   温度高时，同一个问题可能每次调用不同的工具、参数格式偶尔跑偏。
#   工具调用是结构化输出，容错率低 —— 宁可死板，不要灵活。
TEMPERATURE = 0.0

# 默认 API Key 的环境变量名。
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"

# 模型的上下文窗口大小 —— 一次能"看到"的 token 上限。
#
# ⚠️ 这是**模型属性**，不是我们代码的属性。所以它属于模型层。
#
# 为什么不能把窗口用满？
#   ① 还要给**输出**留空间（max_tokens 也从这个窗口里扣）
#   ② 上下文越长，模型表现往往越差（Lost in the middle）
#   ③ 每轮都要重发全部内容，用满意味着每轮都最贵
CONTEXT_WINDOW = 64_000


@dataclass
class ProviderConfig:
    """与模型通信的全部可调项。

    全部带默认值，所以 `ProviderConfig()` 拿到的就是原来那套行为。
    界面层只要改它关心的那几项即可。

    ⚠️ 关于 proxies —— 这一项是 Ginan 项目**特意加的**，不是通用设计。

    原因很具体：Ginan 的官方数据源（`peanpod.s3.ap-southeast-2.amazonaws.com`、
    `ssd.jpl.nasa.gov` 等）在国内网络下**连不通**，而目标用户正是国内的学生
    和研究者。他们中的很多人靠代理上网。
    如果工具不支持代理，这些人第一步就卡死。

    ⚠️ 注意区分两类请求：
      · **LLM 请求**（这个类管的）—— 很多人已经给 API 配了直连
      · **数据下载请求**（ginan/fetch.py 管的）—— 这个才真正需要代理
    两者可能需要**不同的代理设置**，所以配置是分开的两份，不共用。

    proxies 的格式和 requests 库一致：
        {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    """

    # ---- 去哪儿、用哪个模型 ----
    model: str = DEFAULT_MODEL
    base_url: str = BASE_URL

    # ---- 身份凭证 ----
    # api_key 直接给就用它；为 None 时才去环境变量里找。
    # 这样界面可以把用户填的 Key 直接传进来，不必先写进环境变量。
    api_key: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV

    # ---- 生成参数 ----
    temperature: float = TEMPERATURE

    # ---- 预算与可靠性 ----
    context_window: int = CONTEXT_WINDOW
    max_attempts: int = MAX_ATTEMPTS
    timeout: float = 120.0

    # ---- 网络 ----
    # ⚠️ 显式代理。为 None 时遵守环境变量（HTTP_PROXY / HTTPS_PROXY），
    #    这也是一种合理行为 —— 很多用户就是靠系统代理上网的。
    proxies: dict[str, str] | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def resolved_api_key(self) -> str:
        """拿到实际要用的 Key。找不到就抛出**说人话**的错误。

        ⚠️ 这个方法故意设计成**惰性**的 —— 构造 ProviderConfig 本身不读 Key。
        因为桌面应用要能在**没有 Key 的时候正常启动**，
        否则用户第一次打开就是一堆报错，连填 Key 的界面都看不到。
        所以：启动时不检查，真正要发请求时才检查。
        """
        if self.api_key and self.api_key.strip():
            return self.api_key.strip()
        return load_api_key(self.api_key_env)

    def has_api_key(self) -> bool:
        """不抛异常地检查 Key 在不在。给"环境自检"用。"""
        if self.api_key and self.api_key.strip():
            return True
        # ⚠️ 这里也要读一次 .env —— 否则 `.env` 里的 Key
        #    在用户还没发过请求时就查不到，自检会误报"没有 Key"。
        #
        # ⚠️ 用显式路径，不用裸的 load_dotenv()。
        #    裸的那个从**当前工作目录**往上找 —— 换个目录启动就找不到，
        #    而且失败是静默的（读不到就当没有），极难诊断。
        from ..locations import load_project_dotenv

        load_project_dotenv()
        return bool(os.environ.get(self.api_key_env, "").strip())


def estimate_tokens(text: str) -> int:
    """粗略估算一段文本占多少 token。

    ============================ 为什么只能"估算" ============================
    精确计算需要调用模型的 tokenizer，而：
      · 各家 tokenizer 不同（OpenAI 用 tiktoken，DeepSeek 用自己的）
      · 引入 tokenizer 要多一个依赖
      · 我们的用途只是"判断有没有接近上限"，不需要精确

    ============================ 估算法 ============================
    不同语言的 token 密度差很多（见 docs/05-ai-concepts.md 2.2）：

        中文：   1 个汉字 ≈ 1~1.5 token      （常用字常单独成 token）
        英文：   4 个字符 ≈ 1 token          （一个词往往 1~2 个 token）
        数字：   更贵，3~4 位数字就可能有 1 个 token

    所以这里分开算：数出中文字符（CJK 区间），剩下的按 4 字符 1 token 算。

    ⚠️ 估算天然是"大约"。好在我们的用法是"和阈值比较"，
       而阈值本身留了很大余量（75%）—— **近似值配上宽松的阈值就够了**。
       （这也解释了为什么下面还要用真实数据校准。）
    """
    if not text:
        return 0

    # CJK 统一表意文字区间：U+4E00 ~ U+9FFF
    # 涵盖了绝大多数常用汉字
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk

    return int(cjk * 1.5 + other / 4)


def estimate_messages_tokens(messages: list[Message]) -> int:
    """估算一整串消息占多少 token。

    ⚠️ 除了正文，每条消息还有**固定开销**：
        role、分隔符、格式标记……这些都要算 token。
    粗略估计每条 4 个 token。消息条数多时这部分不可忽略 ——
    100 条消息光是框架开销就是 400 token。

    （这个数字是经验值，不同服务商略有差异。够用。）
    """
    per_message_overhead = 4
    total = len(messages) * per_message_overhead

    for msg in messages:
        content = msg.get("content")
        if content:
            total += estimate_tokens(content)

        # 工具调用也要算 —— 参数是 JSON 字符串，可能很长
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for call in tool_calls:
                fn = call.get("function", {})
                total += estimate_tokens(fn.get("name", ""))
                total += estimate_tokens(fn.get("arguments", ""))

    return total


# =============================================================================
# 读取 API Key
# =============================================================================

def load_api_key(env_name: str = DEFAULT_API_KEY_ENV) -> str:
    """从环境变量读取 API Key，读不到就给出可操作的报错。

    为什么要从环境变量读，而不是写在代码里？三个理由：

      1. **代码会被分享**。写到代码里，Key 就跟着代码进 git、进截图、进聊天记录。
         而环境变量存在 .env 文件里，已被 .gitignore 排除，永远不会被提交。

      2. **不同环境用不同 Key**。你自己的电脑、服务器、同事的电脑，
         各有各的 Key，但代码是同一份。

      3. **泄露后能单独作废**。如果哪天 Key 泄露了，去后台重置一个，
         所有读环境变量的地方自动生效，不用改一行代码。

    注意报错信息的写法：不要只说"Key 没找到"，而要直接告诉人**怎么修**。
    这类"可操作的报错"是新手体验的关键 —— 它把 20 分钟的排查变成 10 秒的动作。
    """
    # ⚠️ 显式指定 .env 的位置 —— 见 locations.dotenv_path 的说明。
    #    裸的 load_dotenv() 从**当前工作目录**往上找，换个目录启动就失效。
    from ..locations import load_project_dotenv

    load_project_dotenv()

    api_key = os.environ.get(env_name, "").strip()

    if not api_key:
        raise RuntimeError(
            f"没有找到 {env_name}。\n"
            "\n"
            "怎么修（任选一种）：\n"
            "  1. 在应用的「设置」界面里直接填入 Key（推荐，会自动保存）\n"
            "  2. 或者在项目根目录建 .env 文件，写：\n"
            f"       {env_name}=sk-你的真实Key\n"
            "     注意等号两边不要有空格，值不要加引号\n"
            "  3. 自检：python scripts/check_setup.py\n"
        )

    return api_key


def _build_http_client(proxies: dict[str, str]) -> Any:
    """按代理设置造一个 httpx 客户端，交给 OpenAI SDK 使用。

    ⚠️ 为什么不直接用 OpenAI(proxies=...)？
    因为那个参数在 openai-python 1.x 后期就被移除了，
    官方建议自己传 http_client。跟着官方走，才不会被版本卡住。

    ⚠️ httpx 的代理参数名也改过：
        ＜0.26   httpx.Client(proxies={"https": "http://..."})
        ≥0.26    httpx.Client(proxy="http://...")     ← 单数，且只收一个 URL

    两种都试，哪种能用用哪种 —— 免得用户装了个略旧的版本就打不开。
    """
    import httpx

    # 优先 https（我们的请求基本都是 https），没有就退回 http
    url = proxies.get("https") or proxies.get("http") or ""
    if not url:
        return httpx.Client()

    try:
        return httpx.Client(proxy=url)        # httpx >= 0.26
    except TypeError:
        return httpx.Client(proxies=proxies)  # httpx < 0.26


# =============================================================================
# Provider —— 这一层的全部对外能力
# =============================================================================

class Provider:
    """与 LLM 通信的全部能力，就这一个类。

    注意它**不知道**的东西：不知道我们在做 agent，
    不知道工具是怎么实现的，不知道对话进行到第几轮。
    这个"无知"是刻意设计的 —— 它让这一层可以被独立替换和测试。
    """

    def __init__(
        self,
        config: "ProviderConfig | None" = None,
        model: str | None = None,
    ) -> None:
        """参数可以是一个 ProviderConfig，也可以只给模型名。

        ⚠️ 为什么**不在构造时**创建客户端、也不读 API Key？

        因为桌面应用有个硬要求：**没有 Key 的时候必须能正常启动**。

        ⚠️ 如果把 `OpenAI(api_key=load_api_key(), ...)` 写在构造函数里，
        就意味着"没配 Key 就构造不出 Provider" → 程序启动即崩 →
        用户看到的是一堆报错，**连填 Key 的界面都到不了**。
        这是典型的"把启动变成了需要先满足条件才能发生的事"。

        改成惰性后：
            构造 Provider        → 永远成功（只存配置）
            真正发第一次请求时   → 才去要 Key，要不到就报错
        于是界面能顺利启动，并把"去填个 Key"当成一个正常步骤引导用户完成。
        """
        self._config = config or ProviderConfig()
        if model:
            # 兼容旧写法：Provider("deepseek-chat")
            self._config.model = model

        self.model = self._config.model

        # 客户端推迟到第一次真正要用时再建（见 _ensure_client）
        self._client: OpenAI | None = None

        # 上一次调用**实际用了多少输入 token**（服务端给的，不是估算）。
        #
        # ⚠️ 为什么把它放在 Provider 上，而不是每次调用返回就算了？
        # 因为循环层需要它来**估算当前历史有多长**。
        # 而"估算"用的是"上次真实值 + 之后新增内容的估算" ——
        # 所以它是跨调用存在的状态，属于 Provider 的一部分。
        #
        # None 表示还没调用过 —— 那时只能纯估算。
        self.last_prompt_tokens: int | None = None

        # 上一次调用的缓存命中情况。
        #
        # 为什么要留在这里（而不只是随 Reply 返回）？
        # 因为它是**跨调用的状态**：循环层需要在压缩前后对比，
        # 才能看出"压缩到底毁了多少缓存"。
        self.last_cache_hit_tokens: int = 0
        self.last_cache_miss_tokens: int = 0

    @property
    def config(self) -> ProviderConfig:
        """当前配置（只读用途）。

        循环层需要它来拿 context_window —— 因为"窗口多大"是**模型属性**，
        而模型是这一层的事。循环层不该自己存一份，否则换模型时两边会不一致。
        """
        return self._config

    # -------------------------------------------------------------------------
    # 惰性创建客户端
    # -------------------------------------------------------------------------

    def _ensure_client(self) -> OpenAI:
        """第一次真正要发请求时才创建 HTTP 客户端。

        这个方法是 __init__ 那段注释的兑现处 —— 把"需要 Key"这件事
        从**构造时**推迟到**使用时**，程序就能在没配 Key 的情况下正常启动。
        """
        if self._client is not None:
            return self._client

        # ⚠️ 这里可能抛异常（没配 Key）。这是**故意的** ——
        # 到这一步说明用户真的要发请求了，此时报错才有意义。
        kwargs: dict[str, Any] = {
            "api_key": self._config.resolved_api_key(),
            "base_url": self._config.base_url,
            "timeout": self._config.timeout,
        }

        if self._config.proxies:
            kwargs["http_client"] = _build_http_client(self._config.proxies)

        if self._config.extra_headers:
            kwargs["default_headers"] = dict(self._config.extra_headers)

        # 客户端自带 max_retries=2，网络抖动会自动重试两次。
        # 这是**第一层**重试；下面 chat_stream 里的重试循环是第二层。
        # 两层叠起来最坏情况要等好几秒 —— 所以界面必须把它跑在工作线程里。
        self._client = OpenAI(**kwargs)
        return self._client

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[TextDelta]:
        """把消息列表发给模型，**流式**产出回复。

        参数：
            messages —— **完整的历史**。模型是无状态的，你不发的东西它一律不知道。
            tools    —— 工具说明书列表（由 tools.tool_schemas() 生成）。
                        传 None 表示这次不带工具，模型只能靠说话回答。

        ============================ 这是个"生成器" ============================
        注意它不是普通函数，是**生成器函数** —— 因为它里面有 `yield`。

        调用它**不会立刻执行**，而是返回一个生成器对象。
        真正执行是在你 `for` 遍历它的时候，**每次遍历到 yield 就暂停**，
        把值交出去，等你要下一个时才继续。

        所以它的产出是"一小片一小片"的：

            for event in provider.chat_stream(...):
                #                  ↑ 每次循环拿到一小段文本
                print(event["text"], end="")

        ============================ 返回值去哪了 ============================
        生成器也能 `return`，但返回值**不在事件流里**。
        它通过 `yield from` 传给调用方：

            reply = yield from provider.chat_stream(...)
            #       ↑ 转发所有事件，并把 return 的东西接住

        为什么要这样设计？因为"流式的事件"和"最终的结果"是两种东西：
          - 事件：给界面实时渲染用（一小段文字、一次工具调用）
          - 结果：给循环层做判断用（完整消息、工具调用列表、finish_reason）
        混在一起反而更乱。
        """
        # =====================================================================
        # 重试循环（阶段 3B）
        # =====================================================================
        #
        # ⚠️ 循环体里的 `yield` 有个微妙之处：
        # 一旦我们往外 yield 过东西，就**不能再重试**了 ——
        # 因为重试意味着重新请求，用户会看到重复的内容。
        #
        # 判断方法：text_parts 或 tool_calls_acc 非空 = 已经收到过东西。
        #
        for attempt in range(1, self._config.max_attempts + 1):
            # 每次尝试都要用**全新的**容器，不能沿用上次的残留
            text_parts: list[str] = []
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason = ""
            prompt_tokens = 0
            completion_tokens = 0
            cache_hit_tokens = 0
            cache_miss_tokens = 0

            try:
                # ---------- 构造请求参数 ----------
                #
                # 用"先装进字典，再用 ** 展开"的写法，而不是直接写在调用里。
                # 原因是 tools 是可选的：
                #   如果传 tools=None 给 SDK，有些服务端会收到 "tools": null 而报错。
                #   所以"没有工具时干脆不带这个字段"更稳。
                #
                # **request 把字典拆成关键字参数：
                #   {"model": "x", "messages": [...]}  →  create(model="x", messages=[...])
                request: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": self._config.temperature,
                    "stream": True,  # ← 让服务端一小片一小片地推
                    # ⚠️ 流式模式下默认**不返回用量统计**（省流量）。
                    # 要拿 token 数就必须显式要求它带上。
                    # 没有这个选项，"这轮用了多少 token" 就无从得知，
                    # 也就没法做上下文预算管理。
                    "stream_options": {"include_usage": True},
                }
                if tools:
                    request["tools"] = tools

                stream = self._ensure_client().chat.completions.create(**request)

                # =============================================================
                # 取消：用 try/finally 保证连接一定被关闭（阶段 3B）
                # =============================================================
                #
                # 为什么必须显式关闭？
                #
                #   假取消：只管不读了，HTTP 连接还开着
                #           → 服务端以为你还在等，继续生成、继续计费
                #
                #   真取消：主动关连接 → 服务端检测到断开 → 停止生成
                #
                # `finally` 块的特性是**无论怎么退出都会执行** ——
                # 正常结束、抛异常、还是用户按了 Ctrl+C（KeyboardInterrupt），
                # 都会走到这里。
                #
                # 这就是"清理"（cleanup）的典型用法：
                # **拿到了资源（连接），就必须保证还回去。**
                try:
                    for chunk in stream:
                        # =====================================================
                        # ⚠️ 用量统计必须在最前面处理！
                        # =====================================================
                        #
                        # 服务端把用量放在**一个 choices 为空的特殊 chunk** 里，
                        # 而这个 chunk 通常在最后。
                        #
                        # 如果把这段代码写在下面的 `if not chunk.choices: continue`
                        # 之后，它就**永远读不到** ——
                        # 因为带用量的那个 chunk 正好会被 continue 跳过。
                        #
                        # 这是个很容易写错的顺序问题：
                        # 两段代码单独看都对，放在一起就错。
                        if getattr(chunk, "usage", None):
                            usage = chunk.usage
                            prompt_tokens = usage.prompt_tokens
                            completion_tokens = usage.completion_tokens

                            # ---------- 缓存命中情况 ----------
                            #
                            # ⚠️ 字段名各家不统一，所以要**两处都试**：
                            #   DeepSeek 用自己的顶层字段 prompt_cache_hit_tokens
                            #   OpenAI 用嵌套的 prompt_tokens_details.cached_tokens
                            #
                            # 用 getattr 逐层取，取不到就用 0 ——
                            # 这样换服务商时不会因为字段缺失而崩。
                            cache_hit_tokens = (
                                getattr(usage, "prompt_cache_hit_tokens", None)
                                or getattr(
                                    getattr(usage, "prompt_tokens_details", None),
                                    "cached_tokens",
                                    None,
                                )
                                or 0
                            )
                            cache_miss_tokens = (
                                getattr(usage, "prompt_cache_miss_tokens", None)
                                or (prompt_tokens - cache_hit_tokens)
                            )

                        # ⚠️ 防御：某些 chunk 的 choices 是空列表
                        # （就是上面那个带用量的 chunk）。
                        # 不检查就会 IndexError。
                        if not chunk.choices:
                            continue

                        choice = chunk.choices[0]
                        delta = choice.delta

                        # ---------- 文本碎片 ----------
                        if delta.content:
                            text_parts.append(delta.content)
                            # 立刻交给外面 —— 这就是"流式"的全部意义：
                            # 不等生成完，收到一点就往外送一点
                            yield {"type": "text_delta", "text": delta.content}

                        # ---------- 工具调用碎片 ----------
                        #
                        # ⚠️ 这是流式里最容易踩的坑。
                        #
                        # 工具调用的参数是**被切碎了送来的**：
                        #   chunk 1: {"index":0,"id":"call_abc","function":{"name":"calculate","arguments":""}}
                        #   chunk 2: {"index":0,"function":{"arguments":"{\"expr"}}
                        #   chunk 3: {"index":0,"function":{"arguments":"ession\": \"1+1\"}"}}
                        #
                        # 注意 `index` —— 模型可能**同时**要求调多个工具，
                        # index 就是"这是第几个工具调用"。
                        # 所以必须**按 index 分组**累积，不能一股脑往一个字符串里拼。
                        if delta.tool_calls:
                            for tc_delta in delta.tool_calls:
                                idx = tc_delta.index

                                # 第一次见到这个 index → 建一条新记录
                                #
                                # 展开成 if 而不是用 setdefault，是为了让新手看清发生了什么
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }

                                entry = tool_calls_acc[idx]

                                # id 和 name 通常只在第一个碎片里出现，所以要判断再赋值
                                if tc_delta.id:
                                    entry["id"] = tc_delta.id
                                if tc_delta.function:
                                    if tc_delta.function.name:
                                        entry["function"]["name"] += tc_delta.function.name
                                    # ⚠️ arguments 用 += **累加**，不能用 = 覆盖
                                    if tc_delta.function.arguments:
                                        entry["function"]["arguments"] += (
                                            tc_delta.function.arguments
                                        )

                        # ---------- 结束原因 ----------
                        # 只有最后一个 chunk 会带 finish_reason，其余都是 None
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason

                finally:
                    # 不管上面发生了什么，一定关连接。
                    # 用 try/except 包一层：关闭本身也可能失败（比如连接已经断了），
                    # 那时不该让"关闭失败"盖住"真正的问题"。
                    try:
                        stream.close()
                    except Exception:
                        pass

                # ---------- 拼装最终结果 ----------
                #
                # 把累积的碎片整理成我们自己干净的数据结构。
                # 按 index 排序，保证工具调用的顺序和模型发出的一致。
                tool_calls: list[ToolCall] = [
                    tool_calls_acc[i] for i in sorted(tool_calls_acc)  # type: ignore[misc]
                ]

                full_text = "".join(text_parts)

                raw: Message = {
                    "role": "assistant",
                    "content": full_text or None,
                }
                # ⚠️ 只有在真的有 tool_calls 时才加这个字段。
                # 因为有些服务端看到空的 tool_calls: [] 会报错。
                if tool_calls:
                    raw["tool_calls"] = tool_calls

                # 成功！直接 return，跳出重试循环
                #
                # 顺便把这次的输入 token 数记在实例上 ——
                # 循环层要靠它判断"历史是不是太长了"。
                # 这就是"用真实数据校准估算"的那个真实数据。
                if prompt_tokens:
                    self.last_prompt_tokens = prompt_tokens
                    self.last_cache_hit_tokens = cache_hit_tokens
                    self.last_cache_miss_tokens = cache_miss_tokens

                return Reply(
                    text=full_text,
                    tool_calls=tool_calls,
                    raw=raw,
                    finish_reason=finish_reason,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cache_hit_tokens=cache_hit_tokens,
                    cache_miss_tokens=cache_miss_tokens,
                )

            except RETRYABLE_ERRORS as exc:
                # ---------- 失败：要不要重试？ ----------
                #
                # 三道闸门，任何一条不满足就不重试，直接把异常抛给上层：
                #
                # ① 已经吐过东西了 —— 重试会导致用户看到重复内容
                # ② 已经是最后一次尝试了 —— 没得重试了
                #
                # （为什么不用管"不可重试的错误"？因为它们根本不在
                #   RETRYABLE_ERRORS 里，压根不会进这个 except。
                #   它们会直接往上冒泡，由 cli 那层的 except Exception 接住。）
                already_emitted = bool(text_parts or tool_calls_acc)
                if already_emitted or attempt == self._config.max_attempts:
                    raise

                delay = _backoff_delay(attempt)

                # 告诉界面"我在重试，不是卡住了"
                yield {
                    "type": "retry",
                    "attempt": attempt,
                    "max_attempts": self._config.max_attempts,
                    "delay": delay,
                    "reason": f"{type(exc).__name__}",
                }

                time.sleep(delay)
                # 循环继续 → 下一次尝试

        # 理论上到不了这里（最后一次尝试失败时上面已经 raise 了）。
        # 但显式写出来，避免如果哪天改了循环逻辑，
        # 函数会"静默返回 None"这种最难查的 bug。
        raise RuntimeError("重试循环异常结束，这是不应该发生的")
