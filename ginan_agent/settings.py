"""应用设置 —— API Key、模型、代理。

============================ 存在哪 ============================
    <软件目录>/userdata/config.json             （见 core.session.default_app_root）

⚠️⚠️ 这里原来写的是 `%LOCALAPPDATA%/GinanAgent/config.json` ——
    **那是第一版的设计，早就被推翻了**（用户原话："我的东西散在系统各处，找不到"）。

    数据目录那次改动把 `core/session.py` 的注释改了，**漏了这个文件** ——
    同一件事两处记，只改了一处。

    ⚠️ 而这段说明就在下面那句承诺的头顶上：
       「必须做到两条：① **界面上如实告诉用户**它存在哪」
       —— **那段说明本身就是错的。**

⚠️ 和 `.env` 的区别：`.env` 是**开发时**用的（放在项目目录、进 .gitignore），
   config.json 是**用户运行时**用的（放在用户数据目录、由界面写入）。
   两者不冲突：config.json 优先，读不到才退回环境变量。

============================ 关于 API Key 的存放 ============================
明文存在本地 JSON 里。这是个**有意的取舍**：

  更好的做法：Windows Credential Manager / macOS Keychain / Secret Service
  代价：每个平台一套 API，或者引入 keyring 依赖（它自己又要处理一堆后端）

对于一个本地 GNSS 工具，明文 JSON 是可以接受的 —— 桌面软件普遍这么做
（VS Code、Postman、各种 CLI 都是）。但必须做到两条：
  ① **界面上如实告诉用户**它存在哪
  ② **绝不上传**，也绝不写进日志

将来真要提升安全性，可以加"可选的系统钥匙串存储"，但不必现在做。

============================ 关于模型名 ============================
⚠️ 这里**故意不写死模型列表**。

模型名几个月就变一次（改名、上下线、出新版本），写死的列表一定会过期，
而过期的表现是"用户照着选了却报错说模型不存在" —— 很难解释。

所以：预设里只写**服务地址**（这个稳定），模型从 `/v1/models` **现查**。
查不到时允许用户手动填。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core.session import default_app_root

CONFIG_FILE = "config.json"


# =============================================================================
# 服务商预设
# =============================================================================
#
# ⚠️ 只写 base_url，不写模型名（理由见模块开头）。
#
# ⚠️ 顺序是刻意的：**国内的放前面**。
#    目标用户是国内学生，他们的网络访问国外 API 常常需要代理，
#    而国内服务直连即可。把能直连的放前面，第一步的成功率就高很多。


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    signup_url: str = ""
    note: str = ""


PROVIDER_PRESETS: list[ProviderPreset] = [
    ProviderPreset(
        "deepseek", "DeepSeek（深度求索）",
        "https://api.deepseek.com",
        "https://platform.deepseek.com/api_keys",
        "国内直连，有免费额度，工具调用稳定（默认推荐）",
    ),
    ProviderPreset(
        "dashscope", "阿里云通义千问",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://bailian.console.aliyun.com/",
        "国内直连",
    ),
    ProviderPreset(
        "zhipu", "智谱 GLM",
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/",
        "国内直连",
    ),
    ProviderPreset(
        "moonshot", "月之暗面 Kimi",
        "https://api.moonshot.cn/v1",
        "https://platform.moonshot.cn/",
        "国内直连",
    ),
    ProviderPreset(
        "siliconflow", "硅基流动",
        "https://api.siliconflow.cn/v1",
        "https://cloud.siliconflow.cn/",
        "国内聚合平台，一个 Key 用多家模型",
    ),
    ProviderPreset(
        "openai", "OpenAI",
        "https://api.openai.com/v1",
        "https://platform.openai.com/api-keys",
        "⚠️ 国内通常需要代理",
    ),
    ProviderPreset(
        "openrouter", "OpenRouter",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/keys",
        "⚠️ 国外聚合平台，通常需要代理",
    ),
    ProviderPreset(
        "ollama", "本地 Ollama",
        "http://127.0.0.1:11434/v1",
        "https://ollama.com/download",
        "完全本地运行，不联网、不花钱；需要模型支持工具调用",
    ),
    ProviderPreset(
        "custom", "自定义（任何 OpenAI 兼容服务）",
        "",
        "",
        "填服务地址即可，vLLM / LM Studio / 自建服务都行",
    ),
]


def find_preset(key_or_url: str) -> ProviderPreset | None:
    """按 key 或 base_url 找预设。"""
    for p in PROVIDER_PRESETS:
        if p.key == key_or_url:
            return p
    for p in PROVIDER_PRESETS:
        if p.base_url and p.base_url.rstrip("/") == key_or_url.rstrip("/"):
            return p
    return None


# =============================================================================
# 设置
# =============================================================================


@dataclass
class Settings:
    """全部用户可配项。

    全部带默认值 —— 这样第一次运行时不用做任何配置就能构造出一个对象，
    界面才能正常启动并引导用户去填。
    """

    # ---- 模型 ----
    provider: str = "deepseek"          # 预设的 key，仅用于界面回显
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-flash"

    # ---- 网络 ----
    proxy_mode: str = "auto"            # auto / manual / none
    proxy_http: str = ""
    proxy_https: str = ""

    # ---- 目录 ----
    inbox_dir: str = ""                 # 空 = 用默认位置

    # ---- 界面 ----
    theme: str = "dark"

    # ---- 解算改正项（逐项）----
    #
    # ⚠️ **稀疏**：只存用户明确改过的那几项（键见 `ginan/corrections.py`）。
    #    空字典 = 完全按档位走 —— 也就是"用户没动过"。
    #
    # ⚠️⚠️ 为什么不是"存全部 9 项的值"：档位要保住"一键预设"的身份。
    #    存全值的话，用户在界面上改一项，其余 8 项就被冻在那一刻，
    #    之后换档位（基础 ↔ 完整）**一项都不会跟着变**，而且看不出来。
    #
    # ⚠️ 默认值必须是空字典，不能是"完整档那 9 项" ——
    #    那会让第一次启动就把用户的默认档位（基础）覆盖成完整，
    #    顺带在第一次运行时逼他下 45 MB 模型文件。
    corrections: dict = field(default_factory=dict)

    # ⚠️⚠️ 这里原来有一个 `known_models` 字段，**删掉了**。
    #
    #    它的注释写着「非持久化字段：上次从接口查到的模型列表（缓存，给界面用）」
    #    —— 但**从来没有任何地方给它赋过值**，而 `to_json()` 又把它 pop 掉了。
    #
    #    而前端 `app.js` 一直在读它：
    #         fillModelSelect(s.known_models || [], s.model || '');
    #
    #    → 永远是 `undefined` → 每次打开设置，模型下拉框**都是空的**。
    #
    #    ⚠️ 这又是「**后端从不返回某个键，调用方一直在读**」那一类
    #       （`.POS` 的 `solution` 键刚出过一次一模一样的）。
    #       成因也一样：**字段被声明了，但没有任何一方负责写它。**
    #
    #    👉 那个"缓存"现在放在它该在的地方 —— `server/app.py` 里一个
    #       模块级变量（只活在这次运行的内存里，仍然不落盘）。
    #       放在这儿做不到：`load_settings()` 每次都从磁盘重读，
    #       在内存里改一个 dataclass 实例，下一次读就没了。

    # -------------------------------------------------------------------------

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        """从字典还原。

        ⚠️ 只认识的字段才用 —— 这样旧版本的 config.json 在升级后
        仍然能读，不会因为多了/少了一个键就整个失败。
        （这个模式和 SessionMeta.from_dict 是同一个思路。）

        ⚠️ 旧版本的 config.json 里可能有 `known_models`（那个字段删掉了），
           上面的过滤正好把它挡在外面 —— **老配置照样读得进来**。
        """
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def masked_key(self) -> str:
        """给界面回显用的脱敏 Key。

        ⚠️ 回显时**绝不返回完整 Key**。
        界面只需要显示"配没配、配的是哪个"，完整值没有任何理由离开服务端。
        """
        k = self.api_key.strip()
        if not k:
            return ""
        # ⚠️⚠️ 短 Key 也要**至少遮住一半** —— 上面的承诺是"绝不返回完整值"，
        #    而 `len(k) <= 2` 时 `k[:2] + "*" * 0` **就是完整值本身**。
        #    实测：`'A'` → `'A'`，`'AB'` → `'AB'`。
        #
        #    ⚠️ 它是脱敏函数，"短输入"恰恰是它最该保守的地方。
        if len(k) <= 10:
            keep = min(2, max(0, len(k) - 2))
            return k[:keep] + "*" * (len(k) - keep)
        return f"{k[:6]}…{k[-4:]}"

    def has_usable_key(self) -> bool:
        return bool(self.api_key.strip())


# =============================================================================
# 读写
# =============================================================================


def config_path() -> Path:
    return default_app_root() / CONFIG_FILE


def load_settings() -> Settings:
    """读设置。文件不存在或损坏都返回默认值 —— **绝不抛异常**。

    ⚠️ 为什么绝不抛异常？
    因为设置读取发生在**程序启动时**。如果配置损坏就崩溃，
    用户会陷入一个死循环：想改配置 → 但程序起不来 → 只能手动找文件删掉。

    正确做法：读不出来就用默认值启动，然后在界面上提示"配置有问题"，
    让用户**在能用的界面里**修。这叫"始终能启动"原则。
    """
    p = config_path()
    if not p.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError):
        return Settings()


def save_settings(s: Settings) -> None:
    """写设置。

    ⚠️ 先写临时文件再 rename —— 原子写入。
    直接覆盖的话，写到一半断电就会留下半个 JSON，
    下次启动配置读不出来（虽然会降级到默认值，但用户填的东西没了）。

    ⚠️ Windows 上 rename 到已存在的文件会失败（不像 POSIX 那样原子替换）。
    Path.replace() 在 Windows 上用的是 MoveFileEx，是允许覆盖的，
    所以这里用 replace 而不是 rename。
    """
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(s.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


# =============================================================================
# 从接口查模型列表
# =============================================================================


def fetch_models(base_url: str, api_key: str, timeout: float = 20.0) -> list[str]:
    """问服务端有哪些模型可用。

    ⚠️ 这是**故意**不写死模型列表的兑现处。

    大多数 OpenAI 兼容服务都提供 `GET {base_url}/models`，
    返回 `{"data": [{"id": "..."}, ...]}`。

    查不到时返回空列表 —— 调用方应该退回"让用户手动填"，
    而不是显示一个错误把用户拦住。**能手动填永远是一条退路。**
    """
    url = base_url.rstrip("/") + "/models"
    try:
        import requests

        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:  # noqa: BLE001
        # ⚠️ 这里故意吞掉所有异常。
        #    模型的"列表"只是便利功能，不该因为它拿不到就中断设置流程。
        return []

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    ids: list[str] = []
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            ids.append(it["id"])
        elif isinstance(it, str):
            ids.append(it)
    return sorted(ids)
