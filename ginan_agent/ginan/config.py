"""Ginan 领域层 —— 生成 YAML 配置。

============================ 这个模块的定位 ============================
把"要处理哪个站、哪一天、有哪些文件"翻译成一份 Ginan 能跑的 YAML。

上游是 catalog（取数计划），下游是 runner（跑 pea）。

============================ 为什么用 dict 而不是字符串模板 ============================
最直觉的做法是拿 `ppp_example.yaml` 当模板，用字符串替换填空。**别这么做：**

  · 替换失败是**静默**的（占位符没匹配上，配置里就留着一个 `<XXX>` 没换）
  · 缩进错了会让 YAML 变成另一个意思（或者直接解析失败）
  · 没人能静态检查一个字符串

用 dict 构造再 dump 成 YAML：
  · 结构错了 Python 立刻报错
  · 能被测试直接断言（不用先写文件再读回来）
  · 类型是明确的

============================ ⚠️ 绝对路径是刻意的 ============================
见 docs/ginan-facts.md：`conditionalPrefix()` 对含 `:` 或绝对路径**原样返回**。

这一条同时消掉三个坑：
  · `exampleConfigs/{data,products,pea}` 是软链接（Windows 解压后变成 0 字节）
  · "必须从 exampleConfigs/ 目录运行"
  · 不想往 Ginan 源码目录里写东西

**所以这里生成的路径全是绝对的。** 别"优化"成相对路径。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import catalog
from . import corrections as gcorrections
from . import examples
from . import workflows as gworkflows


# =============================================================================
# 配置档位
# =============================================================================


@dataclass(frozen=True)
class Profile:
    """一种预设的处理方案。

    ⚠️ 为什么要有"档位"这个概念，而不是把所有参数都暴露出来？
       因为 Ginan 有 3000+ 个配置项。**全暴露等于没暴露** ——
       新手面对 3000 个选项只会关掉窗口。

       档位是把"常见场景 + 已知取舍"打包成一个选择。
       用户只需要选"我要哪种"，不需要知道风速模型用的是 GPT2 还是 VMF3。
    """

    key: str
    label: str
    description: str      # 一句话说明这是什么
    tradeoffs: str        # 用了它的代价（**必须如实说**）
    needs_tables: bool    # 需不需要那 34 个 tables/ 模型文件


# 目前只有两个档位。
#
# ⚠️ `reduced` 是**无代理用户的唯一选择** —— 它不需要任何 tables/ 文件。
#    实测跑通过（见 docs/ginan-facts.md 第三节）。
#
# ⚠️ `standard` **2026-09-20 实测跑通了**（在那之前它整个没做完）。
#    跑通的意思具体是：模型文件真的被 pea 读进去了（Configuration Summary
#    里那 8 个键都在），且结果**确实和 reduced 不一样**。
#
#    ⚠️⚠️ 但**"更完整"不等于"更准"** —— 实测数字见下面 standard 的 tradeoffs。
#        别因为"看起来更全"就把它设成默认。
PROFILES: dict[str, Profile] = {
    "reduced": Profile(
        key="reduced",
        label="基础（推荐）",
        description="不需要任何额外的模型文件，开箱即用。",
        # ⚠️⚠️ 这段话是**给用户看的**，所以它必须说准。
        #
        #    最早写的是"厘米级里差几毫米量级" —— **实测下来是错的**
        #    （垂直方向差十几厘米）。而当时那个说法还漏了一件事：
        #    固体潮本来就不需要额外文件，压根不该关。
        #
        #    ⚠️ 后来写的是"具体数值没有实测过，说不准"。到 2026-09-20
        #       这句话也过期了 —— 现在**测过了**（模型文件终于下得到）：
        #
        #       某测站同一天（2026-01-01，2882 个历元）：
        #           reduced  重复性 3.8 厘米（水平 1.8 / 高程 3.3）
        #           standard 重复性 4.5 厘米（水平 2.9 / 高程 3.5）
        #       两者的解**相差 3.0 米**（三维 RMS）。
        #
        #       👉 所以这几项改正**不是"可忽略的小量"**，是米级的东西。
        #          写"说不准"是诚实，但现在已经知道了，就该写上。
        tradeoffs=(
            "固体潮改正开着 —— 它不需要任何额外文件，没有理由关。"
            "关掉的是海潮、大气潮、海洋极潮三项负荷改正"
            "（每一项各要一个模型文件），"
            "另外对流层用的是简化模型，不是外部格网。"
            "这几项的影响**不是小量**：同一份数据实测下来，"
            "和「完整」档的解相差 3 米左右。沿海测站受影响更明显。"
        ),
        needs_tables=False,
    ),
    "standard": Profile(
        key="standard",
        label="完整（需要额外的模型文件）",
        description="把所有改正模型都算上（海潮负荷、大气潮、海洋极潮、"
                    "二阶电离层、日月位置）。",
        # ⚠️⚠️ 这一段必须**如实**，尤其是"它不保证更准"这句。
        #
        #    原来的写法是"启用全部改正模型，精度最高"
        #    —— **"精度最高"没有依据**，属于承诺了一件没测过的事。
        #
        #    2026-09-20 实测（某测站 2026-01-01，2882 历元，同一份观测）：
        #
        #        reduced   重复性 3.8 厘米
        #        standard  重复性 4.5 厘米   ← 略**差**
        #
        #    ⚠️ 这**不是**说 standard 做错了。几点必须一起说：
        #      · 这套数据**没有真实天线型号**（文件头是空的），
        #        而改正越细，天线模型不准带来的偏差越藏不住
        #      · 该测站不在 OLOAD_GO.BLQ 的测站表里，海潮负荷只能取到公共部分
        #      · "重复性"衡量的是**自己跟自己的一致性**，不是**准不准**；
        #        要判准不准得拿权威坐标（比如 IGS 的 SINEX 解）来比，
        #        而这个测站没有
        #
        #    👉 所以结论是：**它算的东西确实更多，但不保证结果更好。**
        #       对已有权威坐标、天线信息齐全的测站才谈得上"更准"。
        tradeoffs=(
            "需要先下 10 个模型文件（约 17 MB，**只下一次，以后一直用**）。"
            "⚠️ 但要清楚：**算的模型更多，不等于结果更准。**"
            "同一份观测实测下来，「完整」档的重复性反而略差一点"
            "（4.5 厘米 vs 3.8 厘米）；两者的解相差约 3 米。"
            "在测站天线信息不全、或测站不在海潮负荷表里的情况下，"
            "多出来的改正不一定帮上忙。"
            "想要真正判准不准，得拿权威坐标去比 —— 程序自己不知道哪个更准。"
        ),
        needs_tables=True,
    ),
}

DEFAULT_PROFILE = "reduced"


# =============================================================================
# 哪些档位**现在真的能用**
# =============================================================================
#
# ============================ 这份名单的历史 ============================
#
# 这里原来是 `("reduced",)` —— `standard` 被**刻意**排除在外，理由写得很清楚：
#
#     「它需要那批 `tables/` 模型文件，但**下载那批文件的功能还没做**：
#       `catalog.ginan_s3_table_url()` 写好了却零个调用者，`plan_run()`
#       也不产出 tables。所以现在选它 = 配置里引用一批根本不存在的文件。
#       **能选本身就是一种承诺**，宁可少提供一个选项，也不要提供一个
#       选了会坏的选项。」
#
# ⚠️ 那个判断是对的 —— 而且 2026-09-20 真去做的时候，它**又对了一次**，
#    只是错在哪一层和当初想的不一样：
#
#     · 文件**下得到了**（换个 S3 端点就行，见 catalog.GINAN_S3_BASE）
#     · 但**下了不等于有用** —— 光把文件放进 `data/tables/` 是不够的，
#       配置里还必须把那些键写出来（`gpt2grid_files` / `ocean_tide_...` …）。
#       不写的话 pea **一个都不会去打开**，而它**不会报任何错**：
#       跑出来和「基础」档一模一样，用户以为用了完整档。
#
#     👉 所以现在多了一道**运行时**的关卡，见 ginan_tools.do_generate_config：
#        档位要的模型文件不在磁盘上 → 拒绝生成配置，并且告诉调用方
#        怎么把文件补上。
#
# ============================ 现在的规矩 ============================
#
#   · 这份元组管的是"**功能做完了没有**"
#   · `do_generate_config` 管的是"**这台机器上的文件够不够**"
#
# 两者都要过。少任何一个，"能选"就又会变成一句做不到的承诺。
AVAILABLE_PROFILES: tuple[str, ...] = ("reduced", "standard")


def is_available(key: str) -> bool:
    """这个档位现在能不能用。"""
    return key in AVAILABLE_PROFILES


def unavailable_reason(key: str) -> str:
    """档位用不了的原因 + 该怎么办。给模型和用户看。"""
    if key == "standard":
        # ⚠️ 代价那句话**从 PROFILES 里取**，不要在这里再抄一遍。
        #
        #    原来这里抄了一份，于是"厘米级里差几毫米"这个错说法
        #    同时活在**代码两处 + README + 文档**里（实测是十几厘米）。
        #    改的时候只改一处就会漏掉别的 —— 而漏掉的那处
        #    恰恰是模型读给用户听的那处。
        #
        #    **同一句话只该有一个出处。**
        # ⚠️⚠️ **那句「34 个 / 约 200 MB」全是过期的，而且两个数都不对。**
        #
        #    实测：要下的是 `catalog.TABLES` 里那 **10 个**；
        #    而 `catalog.py` 专门写过一段 ——「别信工具描述里原来写的
        #    『约 200MB』，**那个数字从来没有依据**，而且它已经
        #    **误导过模型**去对用户承诺一次根本不会发生的下载」。
        #
        #    「下载那批文件的功能还没做」也是假的：2026-09-20 就做完了
        #    （`fetch_tables` / `missing_tables` / `table_plan`）。
        #
        #    ⚠️ 写死的数字**一定会过期**，而过期的表现是用户照着它做决定。
        #
        #    👉 判据：**这段话里的数字要有出处**。个数从 `TABLES` 现算；
        #       **体积干脆不提** —— 它没有任何数据出处（只在注释里），
        #       与其抄一个可能过期的，不如让用户去 `fetch_data` 看真实清单。
        from . import catalog as _catalog

        n_tables = len(_catalog.TABLES)
        return (
            f"「完整」档需要额外下 {n_tables} 个模型文件（只下一次，之后长期有效）。\n"
            "用之前要先让 `fetch_data` 带上 `with_tables=true` 把它们下下来，"
            "否则配置里会引用一批不存在的文件，Ginan 跑到一半才报「缺文件」。\n"
            "\n"
            "现在请用「基础」档（reduced）—— 它不需要任何额外文件，开箱即用。\n"
            f"代价：{PROFILES['reduced'].tradeoffs}"
        )
    return f"档位 {key!r} 现在不可用。"


def _profile_label(key: str) -> str:
    """档位的显示名 —— 取不到就回退成 key 本身。

    ⚠️ 只用于**报错信息**，所以取不到不抛异常：用户拼错档位名时，
       看到的是"没有叫 'xxx' 的档位"，而不是一个 KeyError 的 traceback。
    """
    prof = PROFILES.get(key)
    return prof.label if prof else key


def profile_not_allowed_reason(wf: gworkflows.Workflow, profile: str) -> str:
    """这个工作流**不接受**这个档位的原因 + 该怎么办。给模型和用户看。

    ⚠️ 和 `unavailable_reason` 是**两件事**，不要混为一谈：

        `unavailable_reason(key)`            档位**本身**还没做完 / 全局不可用
        `profile_not_allowed_reason(wf, key)` 档位是好的，**但这个工作流不能用它**

    ⚠️⚠️ 目前只有 POD 收窄了档位，理由**不是精度，是「基础」档会静默算错**
       （见 `workflows.Workflow.profiles`）。

       所以这里的话必须把"为什么会算错"讲清楚 —— 只说"不支持"
       会让用户以为是**可以商量**的，然后自己改配置绕过去。
       而绕过去的后果是一条**看不出错的轨道**。
    """
    allowed = "、".join(_profile_label(k) for k in wf.profiles)

    if profile == "reduced":
        return (
            f"{wf.label}现在**不能用「基础」档** —— 这不是精度取舍，"
            f"是它会**算出一条错的轨道，而且从输出上看不出来**。\n"
            f"\n"
            f"原因：{wf.label}要按官方示例的受力模型算（地球重力场、行星历表、海潮），"
            f"而「基础」档正是把这几个模型文件关掉的。\n"
            f"⚠️ 读 Ginan 源码（`orbitProp.cpp:71-77`）：重力场开关开着但文件不在时，"
            f"它**只打印一句 `EGM data not initialised`，跳过最大的那一项受力**，"
            f"轨道**照常输出**。\n"
            f"\n"
            f"👉 两条路：\n"
            f"   ① 改用「完整」档（standard）—— 生成配置时会检查那些模型文件在不在，"
            f"缺了会明说是哪几个；\n"
            f"   ② 或者改用 PPP —— 它不估轨道，也就不需要那些文件。"
        )

    return (
        f"{wf.label}不能用「{_profile_label(profile)}」档。"
        f"它能用的档位是：{allowed}。"
    )


def config_source(wf: gworkflows.Workflow) -> str:
    """这份工作流的配置**从哪来** —— 一句话，给用户和模型看。

    ⚠️ 为什么要能说出这件事：两种来源的**可信度不一样**。

        官方示例（模板）  骨架来自 Ginan 官方示例，**实测跑通过**；
                          我们只改了文件路径、档位、trace 级别
        手写              app 自己拼的。对 PPP 有实测数字背书，
                          对其他工作流**没验过** —— 实测 POD 第 0 个历元就崩

    ⚠️ 用户问"这份配置可靠吗"时，这两个是**不同的答案**。
       所以它必须能被说出来，不能只活在代码里 ——
       写进配置文件的表头、也写进 `generate_config` 的返回值。
    """
    if wf.example:
        return f"Ginan 官方示例 {wf.example}（结构逐块保留，只改了文件路径和档位）"
    return "由本程序拼出（不是官方示例）"


# =============================================================================
# 生成
# =============================================================================


@dataclass
class ConfigInputs:
    """生成一份配置需要的东西。

    全部是**已经确定的信息** —— 这个模块不做任何"猜"或"找"。
    该找什么是 catalog 的事，该下什么是 fetch 的事。
    """

    config_id: str                     # 配置标识，会出现在输出文件名里
    inputs_root: Path                  # 精密产品所在目录
    obs_root: Path                     # 观测数据所在目录
    outputs_root: Path                 # 输出目录

    # ---- 要生成哪一种配置 ----
    #
    # ⚠️⚠️ 2026-09-24 加的。**加它之前，app 只能生成一种形状的配置** ——
    #     `process_modes` 和 `outputs` 都是写死在 `build_config` 里的，
    #     于是 Ginan 的 18 种工作流里只有 PPP 走得通（实测 1/18）。
    #
    #     现在这两处由 `workflows.WORKFLOWS` 那张表决定，
    #     这个字段就是"要表里的哪一行"。
    #
    # ⚠️ 默认必须是 `ppp` —— 那是**唯一**跑通过的一条路。
    #    换默认值等于把没验证过的路变成主路。
    workflow: str = gworkflows.DEFAULT

    # ---- 各工作流特有的输入 ----
    #
    # ⚠️ 这一栏**故意保持很小**：每加一个字段都意味着
    #    "配置里有更多地方能出错"。只加**真的被某个工作流用到**的。
    #
    # egm_files —— 地球重力场模型（`EGM2008.gfc` 等）。
    #   **只有 POD 要它**（估轨道要算卫星受力，重力场是最大的一项）。
    #   PPP 不需要 —— 轨道是已知输入。所以它是 `extra_inputs` 里的一员。
    egm_files: list[str] = field(default_factory=list)

    # ems_files —— SBAS 的改正信息（`KINDS["ems"]`）。
    #   **只有 SBAS 那三个工作流要它**，而且它没有日常源 ——
    #   只来自官方那份固定的演示数据集（见 `catalog.Dataset`）。
    ems_files: list[str] = field(default_factory=list)

    atx_files: list[str] = field(default_factory=list)
    erp_files: list[str] = field(default_factory=list)
    sp3_files: list[str] = field(default_factory=list)
    clk_files: list[str] = field(default_factory=list)
    nav_files: list[str] = field(default_factory=list)
    rnx_inputs: list[str] = field(default_factory=list)

    # ---- 卫星元数据 / 码偏差 ----
    #
    # ⚠️ 这两个是 2026-09-20 加的，为的是消掉 Ginan 每次都在打的两族警告：
    #
    #     snx_files  →  `Warning: Invalid SINEX file`
    #                   `Max yaw rate not found for G063 …`
    #                   `Attitude model not implemented for BLOCK IIF …`
    #     bias_files →  `Bias not found for G27 on L1W, using undefined_sigma: 1`
    #                   `Receiver code bias not found for ALIC`
    #
    #    一次 24 小时的解算里实测有 **100 多条**。缺了它们不等于跑不了 ——
    #    Ginan 会用默认值兜底，但那个默认值是"我们不知道"的意思，
    #    而它**不会**改变退出码。所以用户看到的是"跑成功了"。
    #
    # ⚠️ 在 YAML 里它们落在**两个不同的地方**（见 generate_config）：
    #       snx_files  →  inputs.snx_files                   （和 atx_files 平级）
    #       bias_files →  inputs.satellite_data.bsx_files    （和 sp3_files 平级）
    #    这是 Ginan 的 schema 定的，不是我们随便放的。
    snx_files: list[str] = field(default_factory=list)
    bias_files: list[str] = field(default_factory=list)

    # ---- 测站信息 ----
    #
    # ⚠️⚠️ 这个**不是可有可无的**，是实测发现必须给的。
    #     不给的代价见 `_receiver_options()` 里的说明 ——
    #     概括：整天的解算质量会明显下降，**而失败方式恰好是
    #     "看起来正常、只是精度差"**。
    #
    # ============================ ⚠️ 为什么是"一张表"而不是"一个站" ============================
    #     Ginan 的 `receiver_options` 是**按站名分块**的：
    #
    #         receiver_options:
    #           global: { … }
    #           ALIC:   { apriori_position: [...] }
    #           DARW:   { apriori_position: [...] }
    #
    #     所以这里的形状跟它一致：**站号 → ECEF 坐标（或 None）**。
    #     单站就是只有一个键的表 —— 和 `RunPlan.stations` 同一个道理，
    #     不为"单站"单开一条路。
    #
    #     ⚠️ 键是 4 位站号（Ginan 内部那个 ID，如 `ALIC`），
    #        不是 RINEX 里的 9 位 `ALIC00AUS`。
    #     ⚠️ 值是 `None` 表示"**读不到这个站的坐标**"——那时**整个块都不写**，
    #        而不是写 (0,0,0)（那是地心，Ginan 会当真）。
    #     ⚠️ 顺序 = 配置里的顺序（dict 保序）。
    stations: dict[str, tuple[float, float, float] | None] = field(
        default_factory=dict
    )

    # ---- 处理参数 ----
    epoch_interval: float = 30.0
    start_epoch: datetime | None = None
    end_epoch: datetime | None = None

    # ---- 星座 ----
    #
    # ⚠️ 默认只开 GPS + Galileo。为什么？
    #    · GLONASS 用频分多址，模糊度处理方式和别的系统不同，
    #      在只有一个频点数据时容易出问题
    #    · BeiDou 的 Ginan 支持还在开发中（官方 README 自己标了 *）
    #    先用两个最稳的系统把流程跑通，比一上来全开然后莫名其妙失败强。
    systems: tuple[str, ...] = ("gps", "gal")

    profile: str = DEFAULT_PROFILE

    # ---- 改正项：**逐项**覆盖档位的默认 ----
    #
    # ⚠️ 稀疏：只有用户明确改过的那几项在这里（键是 `corrections.CORRECTIONS`
    #    的 key）。空 = 完全按档位走 —— 也就是**行为和不加这个字段时一模一样**。
    #
    # ⚠️⚠️ 为什么是"覆盖"而不是"整份定值"：档位要保住"一键预设"的身份。
    #    整份定值的话，用户在界面上改了一项，其余 8 项就被冻在那一刻的值上，
    #    之后再换档位（比如从基础换到完整）**一项都不会变** —— 而且没人看得出来。
    corrections: dict[str, bool] = field(default_factory=dict)

    # ---- 留下残差（给"残差诊断"用）----
    #
    # ⚠️ 残差**不是白来的**（实测，见 `tracefile.py` 文件头）：
    #    Ginan 里要同时满足三条才写得出来 —— `trace.level: 5`、
    #    `output_residuals: true`、`output_network: true`（PPP/RTS 的残差
    #    在网络那份里）。缺一条都是**静默的**：文件照常写，只是没有残差行。
    #
    # ⚠️ 而 `level: 5` 会把**接收机**那份也撑到 GB 级（实测 32 MB/60 历元
    #    ≈ 1.5 GB/天），所以这条路上把它关掉，只要网络那份
    #    （实测整天 144 MB + 平滑 52 MB ≈ 200 MB/天、148 秒）。
    #
    # ⚠️⚠️ **它不改结果**：同一份配置 level 1 和 level 5 各跑一遍，
    #    `.POS` **逐字节相同**（实测 sha256 一致）。所以"开残差"是
    #    纯观测动作 —— 诊断看到的就是那份解的残差。
    trace_residuals: bool = False


def _trace_block(inp: ConfigInputs) -> dict[str, Any]:
    """`outputs.trace` —— **要不要留残差**。

    ⚠️ 两条路的写法必须**一模一样**（手写那份直接用它，模板那份照它改值），
       所以它只能有一个出处。两个地方各写一份的话，迟早有一边忘了改。

    实测（ALIC 2026-09-02，60 个历元，同一份配置只改 trace）：

        level 1 + output_residuals     0 行残差     7.0 MB
        level 3 + output_residuals     0 行残差    22.1 MB
        level 5 + output_residuals     4236 行     32.5 MB  ← 只有 SPP（预处理）
        level 5 + 网络 trace           18460 行     6.4 MB  ← PPP + RTS 都在，而且更小
        整天（网络那份，2880 历元）              144 MB + 52 MB，148 秒

    ⇒ 要残差就得 `level: 5`；而残差在**网络**那份里，接收机那份只会白白撑大。
    """
    if inp.trace_residuals:
        return {
            "level": 5,
            # ⚠️ 实测：level 5 下接收机那份会到 1.5 GB/天（32 MB/60 历元），
            #    而我们只要网络那份里的 PPP/RTS 残差。
            "output_receivers": False,
            "output_network": True,
            "network_filename": "NET_<CONFIG>.TRACE",
            "output_residuals": True,
        }
    return {
        "level": 1,
        "output_receivers": True,
        "receiver_filename": "<RECEIVER>_<CONFIG>.TRACE",
        "output_residuals": False,
    }


def _workflow_outputs(wf: gworkflows.Workflow, inp: ConfigInputs) -> dict[str, Any]:
    """这个工作流要 Ginan **额外**产出哪些东西。

    ⚠️⚠️ 这一段是"只改 `process_modes` 不够"的证据。

        第一版我差点以为 POD 只要把 `ppp: true` 打开就行 —— 它的
        `process_modes` 和 PPP **一模一样**。但 Ginan **不会输出一个
        没被要求的产物**：`outputs` 里没有 `sp3`，跑完就只有一堆 POS，
        而"定轨"的那颗卫星轨道根本不会出现。

        👉 所以 POD 的开关和 PPP 相同、**产物和输入不同**。
           这正是"工作流"必须包含 outputs 的理由。

    ⚠️ 文件名不带站点位 —— 轨道是**整网**的产物，不是某个站的。
       （PPP 的 `<RECEIVER>_<CONFIG>.POS` 就带，因为那是每站一份。）
    """
    out: dict[str, Any] = {}
    for key in wf.outputs:
        if key == "pos":
            continue          # pos 是**每个工作流都有**的，在上面写死了
        if key == "sp3":
            out["sp3"] = {
                "output": True,
                "filename": "<CONFIG>.SP3",
                # ⚠️ 输出间隔 300 秒和官方 `pod_example.yaml` 一致。
                #    定轨解算的历元间隔本来就是 300 秒（见 `_epoch_control`），
                #    按 30 秒输出只是把同样的值重复 10 遍。
                "output_interval": 300,
            }
        elif key == "sinex":
            out["sinex"] = {
                "output": True,
                "directory": "./",
                "filename": "<CONFIG>.SNX",
            }
        elif key == "orbit_ics":
            # ⚠️ 文件名**必须显式写** —— 默认值是
            #    `<ORBIT_ICS_DIRECTORY>/<CONFIG>-<LOGTIME>-orbits.yaml`，
            #    而 `<LOGTIME>` 展开后带冒号，Windows 上是非法文件名
            #    ⇒ `std::ofstream` 打不开 ⇒ 默默什么都不写。
            #
            #    实测（2026-09-27）：不写 filename 时，`ics/` 底下会留下
            #    **每个整点一个 0 字节的空文件**（24 个），而一个字节的内容都没有。
            #    写了之后同一份配置出 **40 KB** 的真实内容。
            #    这是 `ginan-facts` 七点十一 那一条的**第四个实例**。
            out["orbit_ics"] = {
                "output": True,
                "filename": "<CONFIG>-orbits.yaml",
            }
        else:
            raise ValueError(
                f"工作流 {wf.key!r} 要产物 {key!r}，但这里不知道怎么生成它。"
                f"**加工作流时要同时在这里加一条** —— "
                f"静默忽略的话，跑完会少一个产物而没人发现。"
            )
    return out


def _receiver_estimation(wf: gworkflows.Workflow) -> dict[str, Any]:
    """接收机那部分的估计参数 —— **PPP 和 POD 要的值不一样**。

    ⚠️⚠️ 这一段的差别是"法方程解不出来"的直接嫌疑犯，值得说清。

        app 原来只有一组值（为 PPP 调的）。把它们和官方
        `pod_example.yaml` 的 `estimation_parameters.receivers.global` 逐值比：

            参数                      app     官方 POD    差别
            ────────────────────────  ──────  ─────────  ────────────────
            pos.sigma                 **100**     **1**   差 100 倍
            pos.process_noise          无          0.0    缺一项
            clock.process_noise       100         10     差 10 倍
            ambiguities.outage_limit  120        900     差 7.5 倍
            ion_stec.sigma            200        500     差 2.5 倍
            ion_stec.outage_limit     120        900
            code_bias                 无         有      少一整项

        ⚠️ **`pos.sigma` 那 100 倍是重点。** 它是测站先验坐标的约束强度：

            · PPP：只解一个站，坐标放多松都无所谓
            · POD：**同时估测站坐标和卫星轨道** —— 这两组参数会**互相吸收**。
              把测站位置放成 100 米、又完全不给过程噪声约束，
              就等于让 46 个站的坐标自由漂去迎合轨道 →
              **设计矩阵秩亏** → `Failed to solve normal equation`

        ⚠️ 注意 `outage_limit` 也差很多（120 → 900 秒）：
           它管"断数据多久算模糊度失锁"。POD 的历元间隔是 **300 秒**，
           而 120 < 300 —— 也就是说**每个历元都会被判成失锁**，
           模糊度永远攒不起来。这一个数字单独就足以让解算崩掉。

        👉 数值全部抄自 `pod_example.yaml`（2026-09-24）。
           **不要凭理解改** —— 它们是上游调过的。
    """
    if "orbit" not in wf.estimates:
        return {}
    return {
        "pos": {"estimated": [True], "sigma": [1], "process_noise": [0.0]},
        "clock": {"estimated": [True], "sigma": [1000], "process_noise": [10]},
        "ambiguities": {"estimated": [True], "sigma": [1000], "process_noise": [0],
                        "outage_limit": [900]},
        "trop": {"estimated": [True], "sigma": [0.3], "process_noise": [0.0001]},
        "trop_grads": {"estimated": [True], "sigma": [0.03],
                       "process_noise": [1.0e-6]},
        "ion_stec": {"estimated": [True], "sigma": [500], "process_noise": [10],
                     "outage_limit": [900]},
        "code_bias": {"estimated": [True], "sigma": [20], "process_noise": [0]},
    }


def _satellite_options(wf: gworkflows.Workflow) -> dict[str, Any]:
    """卫星那一侧的模型设置 —— **POD 必须改，PPP 必须不动**。

    ⚠️⚠️ 这一段是 POD 第一次跑出 NaN 之后才找到的真正根因，
        比 `satellites.orbit.estimated` 更靠底层。

        官方 `pod_example.yaml` 的 `satellite_options.global` 里有一个
        `models` 块，它写着**卫星的位置/钟差从哪里取，按什么优先级**：

            pos:   {enable: true, sources: [KALMAN, PRECISE, BROADCAST]}
            clock: {enable: true, sources: [KALMAN, PRECISE, BROADCAST]}

        **`KALMAN` 排在 `PRECISE` 前面。**

        而 app 原来只发：

            satellite_options: {global: {error_model: ELEVATION_DEPENDENT}}

        —— 没有 `models`，于是 Ginan 用它自己的**默认优先级**
        （精密产品优先）。对 PPP 那是对的（轨道是输入）；
        对 POD **是致命的**：

            滤波器算了卫星轨道 → 但求卫星位置时**不用估计值，去读先验 SP3**
            → 估计和观测模型**断开**
            → 法方程奇异 → **NaN 级联**

        实测现象（2026-09-24，46 站整天）：

            · SP3 产物只有 8175 字节（基本是先验轨道的转储）
            · `_smoothed.SP3` **0 字节**
            · 日志里几千行 `n0 nan nan nan  e nan  E nan  M nan`
            · 退出码 `3221225477`（0xC0000005，段错误）

        ⚠️ 而它**不报"你少了 models 块"** —— 报的是
           `Failed to solve normal equation`，离根因很远。

    ⚠️ `orbit_propagation` 里的 `solar_radiation_pressure` 官方用的是
       `boxwing`（要一个 boxwing.yaml，走 `include_yamls`）。
       这里用 **`cannonball`** —— 它只用 `srp_cr/mass/area` 三个数，
       **不需要额外文件**，少一条路径解析的坑。
       代价是光压模型糙一点，对"能不能跑通"这个目标够用。
    """
    if "orbit" not in wf.estimates:
        # ⚠️ PPP 这一侧**一个字都不加** —— 它的卫星轨道/钟差是**输入**，
        #    改成 KALMAN 优先会把已知的东西拿去估，那是另一回事。
        return {}

    return {
        "models": {
            # ⚠️ 顺序就是优先级。**KALMAN 必须在前** —— 见上面的说明。
            "clock": {"enable": True,
                      "sources": ["KALMAN", "PRECISE", "BROADCAST"]},
            "pos": {"enable": True,
                    "sources": ["KALMAN", "PRECISE", "BROADCAST"]},
            "attitude": {"enable": True,
                         "sources": ["MODEL", "PRECISE", "NOMINAL"]},
            "pco": {"enable": True},
            "pcv": {"enable": True},
            "code_bias": {"enable": True},
            "phase_bias": {"enable": False},
        },
        "orbit_propagation": {
            "albedo": "cannonball",
            "antenna_thrust": True,
            "empirical": True,
            # 三个方向（D=沿迹, Y=法向, B=径向）在地影期要不要估经验加速度
            "empirical_dyb_eclipse": [True, False, False],
            "planetary_perturbations": [
                "moon", "sun", "mercury", "venus", "mars",
                "jupiter", "saturn", "uranus", "neptune", "pluto",
            ],
            "pseudo_pulses": {"enable": False},
            # ⚠️ cannonball 而不是 boxwing —— 见 docstring
            "solar_radiation_pressure": "cannonball",
            "mass": 1000,
            "area": 15,
            "srp_cr": 1.75,
            "power": 20,
        },
    }


def _satellite_estimation(wf: gworkflows.Workflow) -> dict[str, Any]:
    """卫星那部分要估什么 —— **只有 POD 类工作流才有内容**。

    ⚠️ 数值全部抄自 `pod_example.yaml` 的
       `estimation_parameters.satellites.global`（2026-09-24 抄）。
       **不要凭理解改** —— 那些 sigma / process_noise 是上游调过的经验值。

    `emp_*` 是**经验加速度**（empirical acceleration）：轨道受的那些
    没建模干净的力（太阳光压、地球反照…）用一个分段常数去吸收它。
    没有这几项，轨道的"沿迹方向"会漂 —— 而那正是最需要约束的方向。
    """
    if "orbit" not in wf.estimates:
        return {}

    return {
        "satellites": {
            "global": {
                "clock": {"estimated": [True], "sigma": [1000],
                          "process_noise": [1], "tau": [100]},
                "code_bias": {"estimated": [True], "sigma": [10],
                              "process_noise": [0]},
                # ⚠️ sigma 是**四维** `[10,10,10,0.01]` —— 前三项是位置(m)，
                #    第四项是速度(m/s)。**量纲不同**，所以不能只写一个数。
                "orbit": {"estimated": [True], "sigma": [10, 10, 10, 0.01],
                          "process_noise": [0]},
                "emp_d_0": {"estimated": [True], "sigma": [10]},
                "emp_y_0": {"estimated": [True], "sigma": [1]},
                "emp_b_0": {"estimated": [True], "sigma": [1]},
                "emp_d_1": {"estimated": [True], "sigma": [1]},
                "emp_b_1": {"estimated": [True], "sigma": [1]},
                "emp_d_2": {"estimated": [True], "sigma": [1]},
                "emp_b_2": {"estimated": [True], "sigma": [1]},
            },
        },
    }


# ⚠️⚠️ **产物文件名里绝不能出现 `<LOGTIME>`**（2026-09-25 实测）。
#
#    Ginan 把 `<LOGTIME>` 展开成 `<YYYY>-<MM>-<DD>_<HH>:<mm>` ——
#    **中间带一个冒号**。而 **Windows 文件名不允许 `:`** ⇒
#    文件创建失败，**而且不报任何错**。
#
#    实测：官方 LEO 示例跑满 8640 历元、`PEA finished`、退出码 0，
#    而 `out/` 里**一个 `.SP3` 都没有**。加一行显式文件名立刻出 22.6 MB。
#
#    ⚠️ 官方三个示例里，**POD 那份恰好写了安全的名字，LEO 和 SLR 都没写**
#       （于是用上默认值 `<CONFIG>-<LOGTIME>_<SYS>-Filt.sp3`）。
#       ⇒ **"碰巧安全"不是安全** —— 所以这里强制过一道。
#
#    详见 `docs/ginan-facts.md` 七点十一。
_UNSAFE_FILENAME_TOKEN = "<LOGTIME>"
_SAFE_FILENAME_REPLACEMENT = "<YYYY><DDD><HH>"
_SAFE_SP3_FILENAME = "<CONFIG>_<YYYY><DDD><HH>.SP3"


def _force_safe_output_filenames(cfg: dict[str, Any]) -> None:
    """把产物文件名修成 Windows 上能落盘的形状。**就地改，不返回。**

    ⚠️ 为什么放在 `build_config` 末尾、**两条路共用**：
       手写那条本来就写了显式文件名（`<CONFIG>.SP3`），是安全的；
       模板那条是**从示例继承**的 —— `pod_example.yaml` 恰好安全，
       但换一个示例就可能不安全。修在唯一的接缝上，两条路都盖到。

    ⚠️ **这不是"第二个分支"** —— "哪个工作流走哪条路"那个决定仍然只在
       `workflows.Workflow.example` 里（见 `build_config` 的说明）。
       这里只是对所有配置做同一件事。
    """
    out = cfg.get("outputs")
    if not isinstance(out, dict):
        return

    # ① 任何产物的文件名里出现 `<LOGTIME>` 就换掉
    for block in out.values():
        if not isinstance(block, dict):
            continue
        name = block.get("filename")
        if isinstance(name, str) and _UNSAFE_FILENAME_TOKEN in name:
            block["filename"] = name.replace(
                _UNSAFE_FILENAME_TOKEN, _SAFE_FILENAME_REPLACEMENT
            )

    # ② sp3 要输出、却没有显式文件名 ⇒ 补一个
    #
    #    ⚠️ 为什么单独管 sp3：**它的默认值就是那个带 `<LOGTIME>` 的**，
    #       而别的产物的默认值没见过这个问题（LEO 那轮 clocks/pos 都在）。
    #       只补实测过的那个，不无根据地推广。
    sp3 = out.get("sp3")
    if isinstance(sp3, dict) and sp3.get("output") and not sp3.get("filename"):
        sp3["filename"] = _SAFE_SP3_FILENAME


def build_config(inp: ConfigInputs) -> dict[str, Any]:
    """构造一份 Ginan 配置（Python dict，还没序列化）。

    ============================ 这个函数有两条路 ============================
    判据**只有一个**：`Workflow.example` 是不是 None。

        example is None    → `_build_handwritten`：**手写**拼出整个 dict
        example 是文件名    → `_build_from_example`：载入官方示例、改文件路径

    ⚠️ **为什么会有两条：** 手写那条是 app 最早的（也是唯一）做法，照着 PPP
       拼出来的 —— 对 PPP 能跑，对**其他工作流是残的**。实测（2026-09-24）：

           手写 POD 配置    → 第 0 个历元 `dpotrf failed`，82 秒/历元，NaN 级联
           官方 pod_example → 0 次失败，2.7 秒/历元，产物齐全
           **同一份数据、同一个二进制，只有配置不同**

       👉 手写 200 个键，错一个的样子是「配置看着正常、跑起来是 NaN」。
          所以新工作流走模板（见 `examples.py`）。

    ⚠️⚠️ **这里不许有第二个分支。**
       "哪个工作流走哪条路"是**数据**，记在 `workflows.WORKFLOWS` 的
       `example` 字段里。在这儿写 `if wf.key == "pod"` 等于把那个决定
       复制一份 —— 而两份迟早对不上（这个仓库反复吃过的亏）。

    ⚠️ 这个函数是**纯的**：不写文件、不联网。它只**读**模板文件
       （`examples.load`，那也是纯读）。给定同样的输入必然得到同样的输出。

       这样测试可以直接断言结构，不用先落盘再读回来。
    """
    profile = PROFILES.get(inp.profile, PROFILES[DEFAULT_PROFILE])

    # ⚠️ 工作流从表里取 —— `process_modes` 和 `outputs` 都由它决定。
    #    取不到会**降级到 ppp**（见 `workflows.get`），不抛异常。
    wf = gworkflows.get(inp.workflow)

    if wf.example is None:
        cfg = _build_handwritten(inp, wf, profile)
    else:
        cfg = _build_from_example(inp, wf, profile)

    # ⚠️ 两条路**共用**的收尾 —— 见 `_force_safe_output_filenames` 的说明。
    _force_safe_output_filenames(cfg)
    return cfg


def _abs_path(path: Path) -> str:
    """把路径处理成 Ginan 要的样子：**绝对路径 + 正斜杠 + 结尾斜杠**。

    ⚠️ 三个要点，每一个都踩过：

       ① **必须绝对。** 相对路径相对的是**进程当前工作目录** ——
          而跑 pea 时 CWD 被设成工作目录（`runner.run_pea`），
          不是配置所在目录。`outputs_root` 因此会写到意料之外的地方。
          （`paths.py` 和 `runner.py` 都记着这件事。）

       ② **统一用正斜杠。**

          ⚠️ **理由不是"反斜杠会被 YAML 吃掉"** —— 那条说法**不准确**，
             这里 2026-09-24 纠正过：

                YAML 里 `\\` 只在**双引号**标量中是转义字符。
                PyYAML 把这种路径写成**普通标量**（不加引号），
                而普通标量里 `\\` 就是字面的反斜杠。

             实测：一份用裸反斜杠写 `inputs_root` 的配置（
             `inputs_root: E:\\Code\\...\\products/`）被 pea **正常读取**，
             文件全都找到了。

          👉 那为什么还用正斜杠？两个站得住的理由：
             · Ginan 自己的示例配置全用正斜杠，跟着走少一处差异
             · 跨平台一致（这套代码在 Linux 上跑时反斜杠是**真的分隔符问题**，
               不是转义问题）

          ⚠️ 别因为"反正没关系"就改回反斜杠 —— 一致本身就是价值，
             而且这条路径将来可能被拼进别的上下文（比如双引号里）。

       ③ **必须带尾斜杠。** Ginan 的 `conditionalPrefix()` 是**字符串拼接**，
          不是路径 join：`E:/data` + `tables/x.gfc` = `E:/datatables/x.gfc`。

    ⚠️⚠️ **两条路（手写 / 模板）共用这一个函数。**
       各写一份的话，这条策略就有两个答案，而它们迟早会漂
       （比如一处加了 `resolve()`、另一处没加）。
    """
    s = str(Path(path).resolve()).replace("\\", "/")
    if not s.endswith("/"):
        s += "/"
    return s


# =============================================================================
# 模板那条路 —— 载入官方示例，只改"文件在哪"和档位
# =============================================================================


def _build_from_example(
    inp: ConfigInputs, wf: gworkflows.Workflow, profile: Profile
) -> dict[str, Any]:
    """把官方示例当模板：载入成 dict → 改文件路径 → 应用档位 → 返回。

    ⚠️ **这不是模块 docstring 拒绝的那个方案。**
       那里拒绝的是**字符串替换**，理由是静默漏替换、缩进改变语义、
       没法静态检查。这里是 `yaml.safe_load` 成 dict 再改 —— 三条都不成立
       （结构错 Python 立刻抛、缩进由 dumper 负责、测试直接断言 dict）。

    ⚠️ 顺序**不能换**，每一步都依赖前一步：

       ① `_substitute_files`     换文件路径。**必须最先** —— 模板里的文件名
                                 我们多半没有，留着会被 `checkValidFiles` 判硬失败
       ② `_apply_profile_delta`  档位差量（删掉本档不用的模型文件键、
                                 把 `models` 写明确）
       ③ `_apply_workflow_delta` 工作流决定的几项 + **声明的偏离**
       ④ `_add_station_positions` 测站近似坐标（`setdefault`，不覆盖模板已有的块）
       ⑤ `_merge_table_inputs`   「完整」档的模型文件。**必须最后** ——
                                 它对 `snx_files` 是 merge，先做会被 ① 整份替换掉

    ⚠️ 纯函数：只**读**模板文件，不写盘。改的是 `examples.load()` 返回的副本。
    """
    cfg = examples.load(wf.example)
    _substitute_files(cfg, inp, wf)
    _apply_profile_delta(cfg, profile, inp.corrections)
    _apply_workflow_delta(cfg, inp, wf)
    _add_station_positions(cfg, inp)
    _merge_table_inputs(cfg, profile, inp.corrections)
    return cfg


def _set_path(cfg: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """按路径设一个值。中间层不存在就创建。

    ⚠️ 只用**显式循环**，不递归 —— `check_recursion.py` 全仓库禁止函数自调用。

    ⚠️ 模板里缺的键**也会被创建**（比如某些工作流要 `bsx_files` 而模板没有）。
       那种"凭空多出来的键"会被
       `test_pod_config_differs_from_the_example_only_where_declared` 逮住，
       逼着改的人**在守卫的白名单里写明理由** —— 这是刻意的：
       往模板里加东西应该是件需要解释的事。
    """
    node = cfg
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _replace_if_present(cfg: dict[str, Any], path: tuple[str, ...], value: Any) -> bool:
    """**只在模板里已经有这个键时**才改它的值。返回有没有改。

    ⚠️⚠️ 为什么不直接用 `_set_path`（它会沿途创建缺的层）——
       因为这两种改动**不是一回事**：

           对模板的**更正**      模板写了 `boxwing` / `primary`，
                                 我们换成 `cannonball` / `none`
                                 ⇒ **有东西可换才换**
           工作流**自己的决定**    `process_modes`、`epoch_control`、trace 级别
                                 ⇒ 无条件设 —— 那本来就是我们的权威，不是"更正"

       `_set_path` 用在第一种上会**凭空给模板加一个它没有的块**。

       ⚠️ 实测（2026-09-27）：`brdc2sp3.yml` 里既没有 `orbit_propagation`
          也没有 `mongo`，于是生成的配置里冒出了这两块 ——
          而它们对"把广播星历转成 SP3"这件事**一个字节都不起作用**。
          加进去不会报错，只会让配置变长；但"配置里有它"会让下一个人
          以为它有意义。

    ⚠️ 和 `_add_station_positions` 的 `setdefault` 是**同一个原则的两面**：
       那边是「别把模板已有的覆盖掉」，这边是「别给模板加它没有的」。
       合起来就一句话：**只改该改的**。
    """
    node: Any = cfg
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return False

    if not isinstance(node, dict) or path[-1] not in node:
        return False

    node[path[-1]] = value
    return True


def _drop_path(cfg: dict[str, Any], path: tuple[str, ...]) -> None:
    """按路径删一个键。**不存在就什么都不做**（不抛异常）。

    ⚠️ 不抛异常是刻意的：删一个本来就没有的键，语义上就是"它不该在"，
       而结果确实是不在。为此抛异常会让守卫里"两种档位都跑一遍"变得难写。
    """
    node = cfg
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            return
        node = nxt
    node.pop(path[-1], None)


def _substitutions(
    inp: ConfigInputs, wf: gworkflows.Workflow
) -> dict[tuple[str, ...], Any]:
    """模板里哪些键由 `ConfigInputs` 决定。**值为 None = 删掉这个键。**

    ⚠️ 键是**元组**（`("inputs", "satellite_data", "clk_files")`），
       不是点号字符串。点号写法要 `split(".")`，而 split 出来的东西
       没法静态检查 —— 少一个点就悄悄指到别的键上。元组写错是 Python 立刻报错。

    ⚠️⚠️ **这张表是"两个世界"的分界线。**
       这里列的键，值由 **app** 决定；没列的键，值由**官方示例**决定 ——
       而后者正是 POD 缺的那一大片（`minimum_constraints`、
       `orbit_propagation`、`model_error_handling`…）。

       **往这张表里加键要掂量：加一个，就少一个由示例保证的东西。**

    ⚠️ 空列表 → `None` → **删掉那个键**，不写一个空列表。
       Ginan 的 `checkValidFiles` 对"写了但盘上没有"的路径是**硬失败**；
       而空的 `[ ]` 虽然不失败，却让"键在不在"变成两件事（"不在"和"空"），
       下游每处都得多写一层判断。

    ============================ ⚠️ 为什么要判工作流 ============================
    这个函数原来是**见非空就写**的 —— 传进来什么就写什么，不看工作流用不用。
    在正常流程下看不出问题（`plan_run` 按 `needs` 筛过，那些字段本来就是空的），
    但那是**靠上游恰好没给**，不是**这里保证了不要**。

    ⚠️⚠️ 而"恰好"是会破的，破的方式还是**静默**的：

        `brdc2sp3` 的全部意义是"**从广播星历**算出一条轨道"。
        如果配置里同时出现 `clk_files`（精密钟差）和
        `pseudo_observations.sp3_inputs`（**当成观测值的**精密轨道），
        那它对 `sp3` 输出的取数就未必还落在 BROADCAST 上了
        （默认源序是 `{KALMAN, PRECISE, BROADCAST}`，见 `acsConfig.hpp:325`）
        ⇒ **产出的东西和"广播星历转轨道"不是一回事，而没有任何地方会报错。**

    ⇒ 判据用 `Workflow.needs`（"这个工作流要哪几类文件"），
      配置键 → 类别的映射用 `catalog.KEY_KINDS` ——
      **和守卫 ⑱j 同一张表**，所以"这里丢掉的"和"计划里下的"不可能漂开。
    """
    wanted = set(wf.needs)

    def take(key: str, value: Any) -> Any:
        """这一类工作流不要 → `None`（删掉那个键），要 → 有内容就给。

        ⚠️ 两种键走**两条不同的判据**，因为它们是两种不同的东西：

            `KEY_KINDS[key]` 是类别名   → 按 `Workflow.needs` 判
                                          （那是"要下哪几类文件"）
            `KEY_KINDS[key] is None`    → 按 `Workflow.extra_inputs` 判
                                          （那是"工作流特有的、不上类别的输入"）

        ⚠️ 第二类目前只有 `egm_files`（重力场模型）。**不能让它漏过去** ——
           实测漏过一次：给 `brdc2sp3` 传 `egm_files`，配置里就长出了
           `inputs.egm_files`，而它的模板里根本没有这个键。
           那会让守卫 ⑱j 报"配置要它、计划里没人下它" —— 报得对。
        """
        kind = catalog.KEY_KINDS.get(key)
        if kind is None:
            return value if key in wf.extra_inputs else None
        return value if kind in wanted else None

    out: dict[tuple[str, ...], Any] = {
        # ---- 根路径（绝对路径，见 `_abs_path`）----
        ("inputs", "inputs_root"): _abs_path(inp.inputs_root),
        ("outputs", "outputs_root"): _abs_path(inp.outputs_root),

        # ---- 卫星数据 ----
        # ⚠️ POD 的先验轨道在 `pseudo_observations.sp3_inputs`，
        #    不是 `satellite_data.sp3_files` —— 见 `_build_handwritten` 里那段的说明。
        ("inputs", "satellite_data", "nav_files"): take(
            "nav_files", list(inp.nav_files) or None
        ),
        ("inputs", "satellite_data", "clk_files"): take(
            "clk_files", list(inp.clk_files) or None
        ),
        # ⚠️ `pseudo_observations.sp3_inputs` **不在这里** ——
        #    它的写入是**有条件的**（模板里有那个键才写），
        #    而这个函数表达不了"有才写"。挪到了 `_apply_workflow_delta` ⑩。
        #
        #    实测（2026-09-27，`rtk`）：这里原来用 `_set_path` 写它，
        #    于是给 `rtk_example.yaml`（模板里**没有** `pseudo_observations`）
        #    **凭空长出了** `sp3_inputs` —— 而它的含义是"把这条轨道
        #    **当成观测值**去估"。RTK 里轨道是**已知输入**，不是被估的对象。
        #    ⇒ 配置看着正常，跑出来是另一回事。
        # ⚠️ SBAS 的改正信息挂在 `satellite_data.sbas_inputs` 下
        #    （不是和 nav_files 平级）—— 形状由 Ginan 定的。
        #    它的路径同样走 `satellite_data_root` 那个前缀，见下面。
        ("inputs", "satellite_data", "sbas_inputs", "ems_files"): take(
            "ems_files", list(inp.ems_files) or None
        ),

        # ---- 静态 / 改正文件 ----
        ("inputs", "atx_files"): take("atx_files", list(inp.atx_files) or None),
        ("inputs", "erp_files"): take("erp_files", list(inp.erp_files) or None),
        ("inputs", "snx_files"): take("snx_files", list(inp.snx_files) or None),
        ("inputs", "egm_files"): take("egm_files", list(inp.egm_files) or None),

        # ---- 元数据 ----
        ("outputs", "metadata", "config_description"): inp.config_id,
    }

    # ---- 观测 ----
    #
    # ⚠️ 不要观测时**整块都不写** —— 那个删除由 `_substitute_files` 显式做，
    #    不在这里塞一个 `None`。
    #
    #    （第一版就是塞 `None` 的，而它**能工作纯粹是因为字典顺序**：
    #      `_drop_path` 排在后面执行，所以把前面刚建出来的块又删掉了。
    #      谁把 `out` 的顺序调一下，行为就会静默改变 —— 一条写法层面的
    #      顺序依赖，正是这个仓库反复记着的那种坑。）
    if "obs" in wanted:
        out[("inputs", "gnss_observations", "gnss_observations_root")] = _abs_path(
            inp.obs_root
        )
        out[("inputs", "gnss_observations", "rnx_inputs")] = (
            list(inp.rnx_inputs) or None
        )

    # ---- 数据集工作流：卫星数据文件是**平铺**的 ----
    #
    # ⚠️⚠️ `satellite_data_root` 是 `nav_files` / `ems_files` / `sp3_files` …
    #     **共用**的一层前缀（`acsConfig.cpp:6328-6338` 先把它拼到
    #     `<INPUTS_ROOT>` 上，再拼到每个文件名上）。
    #
    #     SBAS 模板给的是 `sbas/`，也就是它期望文件在 `<输入目录>/sbas/` 下。
    #     而**我们的数据集文件是平铺在数据目录里的**（`fetch` 只有一层，
    #     见 `catalog.dataset_files` 的 `keep_name`）。
    #
    #     ⇒ 把这个键设成**绝对路径**。`conditionalPrefix`（`acsConfig.cpp:131`）
    #       对**含 `:` 的路径直接返回**，所以前缀不再拼接，裸名字就落在
    #       数据目录根下 —— 正是文件实际所在的地方。
    #
    #     ⚠️ **只在数据集工作流上设**。别的模板（POD / brdc2sp3）没有这个键，
    #       而"给它加一个"会改变裸文件名的解析方式 —— 那是动一条验证过的路。
    if wf.dataset:
        out[("inputs", "satellite_data", "satellite_data_root")] = _abs_path(
            inp.inputs_root
        )

    return out


def _substitute_files(
    cfg: dict[str, Any], inp: ConfigInputs, wf: gworkflows.Workflow
) -> None:
    """把模板里的文件清单换成**我们手上真有的文件**。

    ⚠️⚠️ **绝不退回模板里原来的文件名。**
       退回去 = 写出一个磁盘上不存在的路径 = Ginan 报"缺文件"，
       而那个错误离根因（下载失败）很远 —— 用户会一头雾水地去查配置。

       这正是 `workspace.make_config_inputs` 那段"计划里有 ≠ 磁盘上有"的
       同一个原则。（那一段已经在**上游**把"没下过"和"下过但失败"分开了，
         所以这里拿到的是已经判断过的结果。）
    """
    # ⚠️⚠️ **观测要整块一起处理，不能只删里面的文件。**
    #
    #    留下一个只有 `gnss_observations_root` 的块，等于告诉 Ginan
    #    「这里有个观测源，只是里面没文件」。而"**没有观测**"和
    #    "**有个空的观测源**"在 Ginan 里不是一回事。
    #
    #    ⚠️ 这一句放在循环**外面**是刻意的：放进 `_substitutions` 的返回值里
    #       （当成一个值为 None 的条目）也能工作，但那时对不对取决于
    #       字典的迭代顺序 —— 一条写法层面的顺序依赖，谁调一下顺序就静默变了。
    if "obs" not in wf.needs:
        _drop_path(cfg, ("inputs", "gnss_observations"))

    for path, value in _substitutions(inp, wf).items():
        if value is None:
            _drop_path(cfg, path)
        else:
            _set_path(cfg, path, value)


def _apply_profile_delta(cfg: dict[str, Any], profile: Profile,
                         corrections: dict[str, bool] | None = None) -> None:
    """档位在模板上的差量：**删掉本档不用的模型文件 + 把 `models` 写明确**。

    ============================ ⚠️ 为什么必须有这一步 ============================
    官方 `pod_example.yaml` 里 `inputs.tides` 那几个键是**写死在模板里的**，
    而「基础」档不需要那些模型文件。

    如果只把 `inputs.tides` 删掉就完事，会出这件事：

        Ginan 对 `receiver_options.global.models.tides` 的**默认值全是 true**
        （`acsConfig.hpp:1314-1320`），而 `pod_example.yaml` **没写这个块**。

        ⇒ 配置里海潮改正**开着**，但它的模型文件刚被删掉了
        ⇒ **算不出来、不报错、结果默默少了这一项改正**

    这正是本项目最怕的那类失败：看着正常、其实是错的。
    所以档位差量**必须把 `models` 显式写出来**，不能靠默认值。

    ============================ ⚠️ 合并，不是覆盖 ============================
    `_model_options()` 只讲 4 个键（troposphere / tides /
    ionospheric_components / phase_bias），而模板的 `models` 下有 **11 个**
    （eccentricity / attitude / clock / pco / pcv / code_bias / phase_bias /
      pos / ionospheric_components / troposphere / eop）。

    **整块覆盖会静默丢掉那 7 个** —— 天线相位中心、姿态、地球定向就都不算了，
    同样不报错。所以是 merge 进已有的 dict，不是替换。

    ⚠️ 删除清单**不在这里另写一份**：它就是把 `catalog.table_inputs()`
       里出现过的路径拿掉。"哪个档位要哪些模型文件"只有一个出处 ——
       也就是决定"下不下"的那张表。两个方向共用一张表，就不可能漂开。
    """
    # ---- ① 删掉这一跑不要的模型文件键 ----
    #
    # ⚠️ 2026-10-01：原来是"整档删"（`if not needs_tables: 全删`）。
    #    现在按 `_wanted_table_files` 算出来的清单逐键判 ——
    #    「基础」档一项都不想要，结果和以前**逐字节相同**；
    #    而"完整档但用户关掉了海洋极潮"现在也能正确删掉那一栏。
    #
    # ⚠️ `snx_files` 不在此列 —— 它在 `_substitute_files` 里已经被**整份替换**
    #    掉了（换成 app 自己下到的那一批），模板里那 3 个 yaw 文件自然就没了。
    keep = _filtered_table_inputs(profile, corrections)
    for key in catalog.table_inputs():
        if key == "snx_files":
            continue
        if key not in keep:
            _drop_path(cfg, ("inputs", key))

    # ---- ② 把 models 写明确 ----
    # 见上面那个"海潮改正开着但算不出来"的说明。
    node: Any = cfg
    for key in ("receiver_options", "global", "models"):
        nxt = node.get(key) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    # ⚠️ **逐子键 merge，不是整块赋值。**
    #
    #    `_model_options` 对每一项给的是**它关心的那几个键**，
    #    不是那一项的完整定义。整块赋值会丢掉模板里多出来的键 ——
    #    实测踩过：`models.ionospheric_components` 被整块替换，
    #    模板里的 `enable: true` 就没了。
    #
    #    （这一轮同一个形状出现了**三次**：卫星 models 11 个键、
    #      epoch_control 的 max_rec_latency、这里 —— 每次都是"整块覆盖
    #      一个模板已有的 dict"。写 merge 的时候要一路 merge 到底。）
    for key, value in _model_options(profile, corrections).items():
        existing = node.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            existing.update(value)
        else:
            node[key] = value


def _apply_workflow_delta(
    cfg: dict[str, Any], inp: ConfigInputs, wf: gworkflows.Workflow
) -> None:
    """工作流自己决定的那几项 —— 以及**声明的偏离**。

    ⚠️⚠️ **这里每一项都是"我们和官方示例不一样"的地方。**
       全部要在 `check_toolchain.
       test_pod_config_differs_from_the_example_only_where_declared`
       的白名单里**写明白**。往这里加东西 = 加一条偏离，要配理由。
    """
    # ---- ① `include_yamls` 必须删 ----
    #
    # ⚠️⚠️ 官方 `pod_example.yaml` 引用 `products/boxwing.yaml`，
    #    而 Ginan 发行版里那个目录是没有的（上游是**符号链接**，
    #    Windows 解压后变成 0 字节的残留）。
    #
    #    读源码（`acsConfig.cpp:4659`）：include 的文件读不到 ⇒
    #    `Failed to parse configuration file` ⇒ **pea 直接拒绝启动**。
    #    不是警告，是硬失败。
    #
    #    而 boxwing 只是 `solar_radiation_pressure` 的一种选型 ——
    #    我们本来就用 cannonball（见下），所以这个 include 不需要。
    _drop_path(cfg, ("inputs", "include_yamls"))

    # ---- ② 太阳光压模型 ----
    # ⚠️ 模板用 `boxwing`（要上面那个 boxwing.yaml），我们换成 `cannonball` ——
    #    后者不需要任何额外文件。这是 app 原来的选择，保留。
    #
    # ⚠️ 用 `_replace_if_present` 而不是 `_set_path`：这是**对模板的更正**，
    #    没写这个键的工作流（`brdc2sp3`）不该被加上一个空的受力模型块。
    _replace_if_present(
        cfg,
        ("satellite_options", "global", "orbit_propagation", "solar_radiation_pressure"),
        "cannonball",
    )

    # ---- ③ process_modes ----
    # ⚠️⚠️ **以 `wf.modes` 为准，不是以模板为准。**
    #    "哪个工作流开哪几个开关"只有一个真相：`workflows.WORKFLOWS`。
    #    模板里那组值是**被校验**的对象，不是权威 ——
    #    否则同一件事就有了两个答案。
    #    （守卫里会断言模板的值和我们的一致，`slr` 这种模板没写的键算 `False`。）
    cfg.setdefault("processing_options", {})["process_modes"] = dict(wf.modes)

    # ---- ④ 解算历元间隔 ----
    # ⚠️ POD 的 300 秒**不是调优，是前提** —— 30 秒那组会让法方程解不出来
    #    （见 `Workflow.epoch_interval` 的说明）。沿用现有的优先级。
    #
    # ⚠️⚠️ **merge 进模板已有的块，不整块替换。**
    #    模板的 `epoch_control` 里还有 `max_rec_latency` 这类键，
    #    整块替换会把它**静默弄没**。
    #
    #    （实测：第一版就是整块替换，diff 出来 `max_rec_latency` 不见了。
    #      同一个形状这一轮已经出现过两次 —— 见 `_apply_profile_delta`
    #      里 "合并，不是覆盖" 那段。**模型文件那个丢的是 7 个键，
    #      这个丢的是 1 个，但错法一模一样。**）
    ec = cfg["processing_options"].get("epoch_control")
    if not isinstance(ec, dict):
        ec = {}
    ec.update(_epoch_control(inp, wf))
    cfg["processing_options"]["epoch_control"] = ec

    # ---- ⑤ trace 级别 ----
    #
    # ⚠️⚠️ **只改这两个值，不整块替换。**
    #    模板里还有 `output_network` / `output_config` / `network_filename`
    #    等键，整块换掉会把它们一起弄没 —— 那又是一次静默丢东西。
    #
    # ⚠️ 数值是有实测依据的（2026-09-24，一天 POD，87 站全球网）：
    #
    #        trace.level: 3 + output_residuals: true（示例的默认）
    #            → TRACE 共 2.6 GB，其中 Network 单个 138 MB
    #        真正的产物（SP3/OBX/CLK/SNX/POS）加起来才 4.6 MB
    #
    #    级别每高一档，输出按观测数乘着涨。新手第一次跑，
    #    看到的是"算完了但磁盘满了"，而联想不到是输出级别。
    trace = cfg.setdefault("outputs", {}).setdefault("trace", {})
    if isinstance(trace, dict):
        # ⚠️ **只改这几个值，不整块替换**（见上面那段说明）——
        #    模板里还有 `output_config` / `output_json` 等键。
        want = _trace_block(inp)
        trace["level"] = want["level"]
        trace["output_residuals"] = want["output_residuals"]
        trace["output_receivers"] = want["output_receivers"]
        if want.get("output_network"):
            # ⚠️ 网络那份**只有要残差时才开**：开了会多一个文件，
            #    而"多出来的文件"也是用户要收拾的东西。
            trace["output_network"] = True
            trace.setdefault("network_filename", want["network_filename"])

    # ---- ⑥ MongoDB ----
    # ⚠️ 模板默认 `primary`（写到 MongoDB）。那是可选的数据库存储，
    #    我们的用途不需要 —— 开着的话没装 MongoDB 的用户会看到一堆连接错误。
    #
    # ⚠️ 同上：模板没写这个块的（`brdc2sp3`）不用加 —— 不加就是 Ginan 的
    #    默认行为，而那个默认本来就是"不写 mongo"。
    _replace_if_present(cfg, ("mongo", "enable"), "none")

    # ---- ⑦ `satellite_data.sp3_files` —— **模板里有才换** ----
    #
    # ⚠️⚠️ 这一条是 2026-09-27 加 `fit_sp3_pseudoobs` 时**实测撞出来的 app bug**。
    #
    #    `_substitutions` 只管 `pseudo_observations.sp3_inputs`（"当成观测值的"），
    #    **从来没管过** `satellite_data.sp3_files`（"当成已知位置的"）。
    #    以前一直没暴露，是因为**别的模板都没有这个键**：
    #        `pod_example.yaml` / `brdc2sp3.yml` 里搜 `sp3_files` —— 0 命中。
    #
    #    而 `fit_sp3_pseudoobs.yaml` 两处都有（同一个 SP3 既当已知轨道、
    #    又当伪观测）。不换的后果是：**配置里留着示例自己的 2019 年文件名**，
    #    pea 起来第一件事就是
    #        `Error: Missing orbit file …IGS2R03FIN_20191980000…`
    #        `Error: Incorrect configuration`
    #    然后退出码 1、几十毫秒结束 —— 一个**离根因很远**的报错。
    #
    #    ⚠️ 为什么不是 `_set_path`：同样是"对模板的更正"（见 `_replace_if_present`）。
    #       无条件设会让 `pod` / `brdc2sp3` **凭空长出**一个 `sp3_files`
    #       —— 而那两个工作流的 SP3 根本不在这条路上（POD 是被估的对象，
    #       brdc2sp3 自己产）。多出来的键不会报错，但会让下一个人以为它有意义。
    #
    #    ⚠️ 也不放进 `_substitutions`：那里表达不了"模板有才写"，
    #       硬塞就得给它加一层新的标记机制 —— 而那是一处机制，
    #       不是一处数据。
    if "sp3" in wf.needs:
        _replace_if_present(
            cfg,
            ("inputs", "satellite_data", "sp3_files"),
            list(inp.sp3_files),
        )

    # ---- ⑧ 产出的 SP3 要**地固系**，不要惯性系 ----
    #
    # ⚠️⚠️ 这是 2026-09-27 加 `fit_sp3_pseudoobs` 时**量出来的**，
    #    而且**单独关一个开关就复现/消失**（两次对照，其余全同）：
    #
    #        `output_inertial: true`（= 官方示例的值）   **惯性系**
    #        `output_inertial: false`                   **地固系** ✅
    #
    #    实测数（同一天、同一个 `-n 288`）：
    #
    #        惯性系那份   PG01   8501.71  13507.40  21222.43
    #        地固系那份   PG01 -14420.35   6772.84  21243.88   ← 和 IGS 对得上
    #
    #    ⚠️ 两份的**模长一样**（都 ≈26,560 km）、钟差一模一样
    #       —— 是**转了个坐标系**，不是算错了。所以拿去和 IGS 的 SP3 比，
    #       得到的是 **31,863 公里**的"差异"，而它看着像结果。
    #
    #    ⚠️ 为什么必须改：SP3 是**通用交换格式**，所有人都按地固系读它
    #       （IGS 自己的 SP3、app 的 `compare_orbits`、任何第三方工具）。
    #       惯性系那份只有 Ginan 自己认。
    #
    #    ⚠️ `output_velocities` **不用动** —— 那份也量了：
    #       开着（`#dV`）和关着（`#dP`）比出来**都是 1.1827 米**，逐位相同。
    #       所以只改正真有问题的那一个，不顺手多改。
    _replace_if_present(
        cfg,
        ("outputs", "sp3", "output_inertial"),
        False,
    )

    # ---- ⑨ 声明了 `pos` 产物的工作流：**保证那个块存在** ----
    #
    # ⚠️ 官方模板里**不一定有 `outputs.pos`** ——
    #    `pod_example.yaml` 有，而 `rtk_example.yaml` **一个字节都没有**。
    #    不补的话，跑完一个 POS 都不出（退出码还是 0）——
    #    正是"少了一个产物而没人立刻发现"那种。
    #
    # ⚠️ **`setdefault`，不是赋值。** 模板里写了 `pos`（比如 POD）的
    #    **不许动它的文件名** —— 那是模板已有的决定，改名是一次没申报的偏离。
    #    和 `_add_station_positions` 是同一个原则：只补缺的，不覆盖有的。
    #
    # ⚠️ 文件名**显式写**（而不是靠 Ginan 的默认）——
    #    默认值含 `<LOGTIME>`，在 Windows 上非法 ⇒ 静默没有产物
    #    （`ginan-facts` 七点十一，这条已经是第四次遇到）。
    if "pos" in wf.outputs:
        pos = cfg.setdefault("outputs", {}).setdefault("pos", {})
        if isinstance(pos, dict):
            pos.setdefault("output", True)
            if not pos.get("filename"):
                pos["filename"] = "<RECEIVER>_<CONFIG>.POS"

    # ---- ⑩ `pseudo_observations.sp3_inputs` —— **模板里有才写** ----
    #
    # ⚠️ 和 ⑦ 是**同一个 bug 的两面**（都是"没给它加东西"）：
    #
    #     `pseudo_observations.sp3_inputs` 的含义是「把这条轨道
    #     **当成观测值，参与估计**」（见 `_build_handwritten` 里那段）。
    #     ⇒ 它只该出现在**正在估轨道**的工作流里。
    #
    #     而 `_substitutions` 原来用 `_set_path` 写它 —— 那会**创建**这个键。
    #     实测（2026-09-27，`rtk`）：`rtk_example.yaml` 里**没有**这个块，
    #     于是生成的配置里长出了一个 `sp3_inputs` —— 而 RTK 的轨道是
    #     **已知输入**（`satellite_data.sp3_files`），不是被估的对象。
    #     配置看着正常，跑出来是另一回事，**没有任何地方会报错**。
    #
    # ⚠️ 对照：`pod` / `fit_sp3_pseudoobs` 的模板里**本来就有**这个块
    #    ⇒ 这里对它们是**同值替换**，一个字节都不变（守卫会核）。
    if "sp3" in wf.needs:
        _replace_if_present(
            cfg,
            ("inputs", "pseudo_observations", "sp3_inputs"),
            list(inp.sp3_files),
        )
    else:
        _drop_path(cfg, ("inputs", "pseudo_observations"))


def _add_station_positions(cfg: dict[str, Any], inp: ConfigInputs) -> None:
    """把每个测站的**近似坐标**写进 `receiver_options`。

    ⚠️ 为什么必须给：见 `_receiver_options` 那段说明。一句话 ——
       不给的话 Ginan 用单点定位凑合先验坐标，对流层模型拿到离谱的高度，
       **大部分卫星回退到单频**。而这一切不会让解算失败，只是让结果变差。

    ⚠️⚠️ **`setdefault`，不是赋值。**
       模板里已经有测站块了 —— `pod_example.yaml:207` 写着

           USN7:
             aliases: [PIVOT]

       而 `estimation_parameters.receivers.PIVOT` 正依赖这个别名（基准站）。
       直接赋值会把它连带弄没，**而且不会报错** ——
       症状是"解算能跑但基准选错了"，要查很久。

    ⚠️ 坐标是 `None` 时**整块不写**（不是写 (0,0,0)）——
       全零会被当成"地心"，比不写更糟。这条和 `_receiver_options` 一致。

    ⚠️ `examples.load()` 每次返回新 dict，所以这里是就地改那份副本，
       不会污染模板。
    """
    if not inp.stations:
        return

    ro = cfg.get("receiver_options")
    if not isinstance(ro, dict):
        ro = cfg["receiver_options"] = {}

    for sid, pos in inp.stations.items():
        if not sid or not pos:
            continue
        block = ro.get(sid)
        if not isinstance(block, dict):
            block = {}
        block["apriori_position"] = [round(float(v), 4) for v in pos[:3]]
        ro[sid] = block


def _build_handwritten(
    inp: ConfigInputs, wf: gworkflows.Workflow, profile: Profile
) -> dict[str, Any]:
    """**手写**一份配置 —— 逐块拼出来。

    ⚠️ 这是 app 最早的做法，照着 PPP 拼的。它对 PPP 能跑（有实测数字背书），
       但**对其他工作流是残的** —— 理由和实测见 `build_config` 的说明。

    ⚠️ **新工作流不要走这条路。** 在 `workflows.WORKFLOWS` 里给
       `Workflow.example` 填上官方示例的文件名。

    ⚠️ `wf` 和 `profile` 是**调用方算好传进来的** —— 不要在这里重新
       `get()` 一次。两处都算的话，"用哪个档位""是哪个工作流"就有了两个答案。

    ⚠️⚠️ **这个函数目前一个字都不能改。** PPP 的配置是逐键被实测过的
       （`ginan-facts.md` 里 3.8/4.5 厘米那组），动它就要重测。
       POD 的改造走 `_build_from_example`，和这里无关。
    """
    # ⚠️ 路径统一走 `_abs_path()` —— **两条路（手写 / 模板）共用同一个实现**。
    #    各写一份的话，"绝对路径 + 正斜杠"这条策略就有两个答案，
    #    而它们迟早会漂（比如一处加了 `resolve()`、另一处没加）。
    cfg: dict[str, Any] = {
        "inputs": {
            # ⚠️ 绝对路径 —— 见模块开头的说明
            "inputs_root": _abs_path(inp.inputs_root),

            "atx_files": list(inp.atx_files),
            "erp_files": list(inp.erp_files),

            # ⚠️ 和 `atx_files` 平级 —— 这是 Ginan schema 的位置，
            #    不是"顺手放这儿"。见 ConfigInputs 里那段的说明。
            "snx_files": list(inp.snx_files),

            # 重力场模型 —— **只有 POD 要**（估轨道要算卫星受力）。
            # ⚠️ 放在顶层，和 atx/erp 平级（出处：`pod_example.yaml:7`）。
            #
            # ⚠️ 判据是**工作流要不要它**（`extra_inputs`），不是"有没有值"。
            #    按"有没有值"写的话，调用方哪天多填了一个字段，
            #    PPP 的配置里就会**多出一个它不需要的键** —— 而那种偏差
            #    不会报错，只会让两种配置的差别变得说不清。
            **({"egm_files": list(inp.egm_files)}
               if "egm_files" in wf.extra_inputs and inp.egm_files else {}),

            "satellite_data": {
                "nav_files": list(inp.nav_files),
                "clk_files": list(inp.clk_files),
                # ⚠️⚠️ POD **不在这里**放 sp3 —— 见下面 `pseudo_observations`。
                **({} if "pod" == wf.key else {"sp3_files": list(inp.sp3_files)}),
                # ⚠️ 偏差文件在 `satellite_data` **里面**，
                #    和上面三个平级 —— 和 snx_files 不在同一层。
                "bsx_files": list(inp.bias_files),
            },

            # ============ 先验轨道：POD 和 PPP 的**分界线** ============
            #
            # ⚠️⚠️ 同一个 SP3 文件，放在**哪里**决定了它是"已知"还是"被估"：
            #
            #     satellite_data.sp3_files        → **已知**的卫星位置（PPP 走这条）
            #     pseudo_observations.sp3_inputs   → **当成观测值**，参与估计
            #                                        （POD 走这条）
            #
            #     POD 的全部意义就是**估轨道**。放错地方 = 又退化成 PPP，
            #     而那样跑出来的结果**看起来完全正常**（一堆 POS），
            #     只是没有轨道文件 —— 而"少了产物"没人会立刻发现。
            #     出处：`pod_example.yaml:33-35`。
            **({"pseudo_observations": {
                "eci_pseudoobs": False,
                "sp3_inputs": list(inp.sp3_files),
            }} if "pod" == wf.key else {}),

            "gnss_observations": {
                "gnss_observations_root": _abs_path(inp.obs_root),
                "rnx_inputs": list(inp.rnx_inputs),
            },
        },

        "outputs": {
            "metadata": {
                "config_description": inp.config_id,
            },
            "outputs_root": _abs_path(inp.outputs_root),
            "pos": {
                "output": True,
                "filename": "<RECEIVER>_<CONFIG>.POS",
            },

            # ⚠️ 下面这几块由**工作流**决定开不开（见 `workflows.WORKFLOWS`）。
            #
            #    加它们之前，app 的 `outputs` 里只有 `pos` —— 所以哪怕把
            #    `process_modes` 打开了，POD 也算不出轨道：**Ginan 不会
            #    输出一个没被要求的产物。** 这是"只改开关不够"的例子。
            **_workflow_outputs(wf, inp),
            # ⚠️ trace level 默认给 **1**，不给示例里的 4。
            #
            #    级别越高输出越大：level 4 + output_residuals 会产生
            #    每历元每观测的残差块，一天下来能到 GB 级 ——
            #    而 Windows 上有 **2.1 GB 的单文件上限**（Ginan README 明确写了）。
            #
            #    新手跑第一次时，看到的是"处理到一半文件写坏了"，
            #    完全联想不到是输出级别的问题。
            #
            #    想看细节时再调高 —— 那是个明确的、用户主动做的选择。
            "trace": _trace_block(inp),
        },

        # 卫星和接收机的误差模型。
        #
        # 这些值和 Ginan 的示例一致 —— 它们是**默认推荐值**，
        # 不是我们随便定的。改动要在有实测依据的前提下做。
        "satellite_options": {
            # ⚠️ `error_model` 是 PPP 用的；POD 还要 `models` + `orbit_propagation`
            #    （见 `_satellite_options`）。**PPP 不会被改到** ——
            #    那个函数在非 POD 工作流上返回空字典。
            "global": {
                "error_model": "ELEVATION_DEPENDENT",
                **_satellite_options(wf),
            },
        },

        "receiver_options": _receiver_options(inp, profile),

        "processing_options": {
            # ⚠️⚠️ **这一块 2026-09-24 之前是写死的** —— 那是"app 只能做 PPP"
            #     的根因。现在它来自 `workflows.WORKFLOWS` 那张表。
            #
            #     ⚠️ 值是**整份**，不是"要改的几个"：每个工作流自己说清
            #        五个开关各是什么。合并式的写法会让"默认是什么"
            #        变成第二处真相。
            "process_modes": dict(wf.modes),
            "epoch_control": _epoch_control(inp, wf),
            "gnss_general": {
                "add_eop_component": True,
                "use_primary_signals": True,
                "sys_options": _sys_options(inp.systems),
            },
            "ppp_filter": {
                "outlier_screening": {
                    "chi_square": {"enable": False},
                    "postfit": {"max_iterations": 10, "omega_test": True},
                },
                "ionospheric_components": {
                    "common_ionosphere": True,
                    "use_gf_combo": False,
                    "use_if_combo": False,
                },
                "chunking": {"by_receiver": True, "size": 0},
                # RTS 是反向平滑 —— 用整个时段的数据回头修正每个历元。
                # 对静态站很有用：平滑后的位置比前向解稳定得多。
                "rts": {
                    "enable": True,
                    "lag": -1,
                    "filename": "<CONFIG>_<RECEIVER>.rts",
                },
            },
        },

        # 状态估计参数 —— 卡尔曼滤波里每个未知量的初值和噪声模型。
        #
        # ⚠️ 这些数字**不要凭直觉改**。
        #    它们决定了滤波器多快收敛、多信任测量。
        #    调错了不会报错，只会得到"看起来正常但精度不对"的结果。
        "estimation_parameters": {
            "receivers": {
                "global": {
                    "pos": {"estimated": [True], "sigma": [100]},
                    "clock": {"estimated": [True], "sigma": [1000],
                              "process_noise": [100]},
                    "ambiguities": {"estimated": [True], "sigma": [1000],
                                    "process_noise": [0], "outage_limit": [120]},
                    "ion_stec": {"estimated": [True], "sigma": [200],
                                 "process_noise": [10], "outage_limit": [120],
                                 "sigma_limit": [1000]},
                    "trop": {"estimated": [True], "sigma": [0.3],
                             "process_noise": [0.0001]},
                    "trop_grads": {"estimated": [True], "sigma": [0.03],
                                   "process_noise": [1.0e-6]},
                    # ⚠️ 需要估轨道的工作流（POD）在这里**整组换掉**上面的值。
                    #    上面那组是给 PPP 调的 —— 对 POD 会
                    #    `Failed to solve normal equation`（见函数说明）。
                    #    PPP 时这里返回空字典，**一个字都不改**。
                    **_receiver_estimation(wf),
                },
            },
            # ⚠️⚠️ **`satellites` 这一块只有需要估轨道的工作流才有。**
            #
            #    默认（PPP）里它**整个不存在** —— 因为 PPP 的卫星轨道和钟差
            #    是**输入**（SP3/CLK），估它们等于把已知的东西再解一遍。
            #
            #    POD 才要它。而**没有它的后果非常隐蔽**：
            #
            #        配置看着没错 → `pea` **exit 0** → 跑出一堆 POS
            #        → **只是没有轨道**。
            #
            #    第一次实测就是这样：headline 写着"没有找到可用的观测数据"，
            #    而真相是**轨道压根没进滤波器**。产物少了一个，
            #    而"少一个产物"不会让任何人立刻发现。
            #    （出处：`pod_example.yaml` 的 `estimation_parameters.satellites`）
            **_satellite_estimation(wf),
        },

        # ⚠️ MongoDB 关掉。Ginan 的 POD 示例默认是开的，
        #    但那是可选的数据库存储 —— 我们的用途不需要。
        #    开着的话，没装 MongoDB 的用户会看到一堆连接错误。
        "mongo": {"enable": "none"},
    }

    _merge_table_inputs(cfg, profile, inp.corrections)
    return cfg


def _wanted_table_files(profile: Profile,
                        corrections: dict[str, bool] | None = None) -> set[str]:
    """这一跑到底要哪几个 `tables/` 文件。

    = **档位要的**（`needs_tables` → `catalog.TABLES` 全套）
      ∪ **开着的改正项各自要的**（`corrections.files_for`）

    ⚠️ 两边的来源不同，不能只留一边：
       · `TABLES` 里还有 `gpt_25.grd` 和 3 个 yaw 姿态表 —— 它们**不属于
         任何一个改正项**（不是"改正"，是"要不要这个模型文件"），
         谁都不该把它们关掉，所以整档照给。
       · 改正项那一边要**独立于档位**：用户在「基础」档只打开海潮负荷时，
         配置里必须真的出现 `OLOAD_GO.BLQ`，而不是因为"这不是完整档"
         就悄悄不给 —— 那样用户开了等于没开，**而且不报错**。
    """
    on = gcorrections.resolve(profile.key, corrections)
    want = gcorrections.files_for(on)
    if profile.needs_tables:
        # ⚠️ 只补那些**不属于任何改正项**的文件（`gpt_25.grd`、3 个 yaw 姿态表）——
        #    它们是"档位要不要这个模型"，没有开关可以关。
        #    ⚠️⚠️ 不能写成"整档全给"：那样用户在「完整」档把海洋极潮关掉时，
        #        那 23 MB 的系数文件**照样会写进配置**，开关等于没作用。
        owned = {f for c in gcorrections.CORRECTIONS for f in c.files}
        want |= {name for name, _purpose in catalog.TABLES
                 if name not in owned}
    return want


def _filtered_table_inputs(profile: Profile,
                           corrections: dict[str, bool] | None = None
                           ) -> dict[str, Any]:
    """`catalog.table_inputs()` 里只留下「这一跑真要的」文件。

    ⚠️ 通配符（`*_ocean.BLQ` / `*_atmos.BLQ`）**跟着它那一栏走**：
       那一栏一个真文件都不剩时，通配符也不留 —— 留着虽然无害
       （匹配不到就静默略过），但会让"这一栏到底生没生效"读不出来。
    """
    want = _wanted_table_files(profile, corrections)
    out: dict[str, Any] = {}
    for top, val in catalog.table_inputs().items():
        if isinstance(val, dict):
            kept: dict[str, list[str]] = {}
            for sub, files in val.items():
                real = [f for f in files
                        if f.startswith("tables/") and f.split("/")[-1] in want]
                if real:
                    wild = [f for f in files if not f.startswith("tables/")]
                    kept[sub] = real + wild
            if kept:
                out[top] = kept
        else:
            real = [f for f in val if f.split("/")[-1] in want]
            if real:
                out[top] = real
    return out


def _merge_table_inputs(cfg: dict[str, Any], profile: Profile,
                        corrections: dict[str, bool] | None = None) -> None:
    """「完整」档的模型文件 —— **必须写进配置，光下到磁盘上没用**。

    ⚠️⚠️ 实测踩过：以为下到 `data/tables/` 就够了（`inputs_root` 是前缀，
        拼出来正好是 `<app>/data/tables/xxx`），所以**没写这段**。

        结果 pea 启动时那份 Configuration Summary 里
        **一个 tables 文件都没有** —— 它压根没去打开过。
        那 45 MB 静静躺在磁盘上，一点用都没有。

        ⚠️⚠️ 最坏的是它**一句话都不说**：跑出来的结果和「基础」档
             **一模一样**，用户以为用了完整档，其实一个模型都没用上。

        （这个结论是拿"有 tables"和"没 tables"各跑一遍、把两次的日志
          逐行比出来的 —— **不是读代码读出来的**。
          如果只看"跑通了没有"，两次都是 exit 0，会得出完全错误的结论。）

    ⚠️ 内容从 `catalog.table_inputs()` 来 —— **不在这里另抄一份文件名**。
       两份清单会漂，而漂的后果正是上面那个"下了等于没下"。

    ⚠️ 「基础」档**不加**这些键：它本来就不需要模型文件，
       而且那条路是**验过能跑的**（有实测数字），不要顺手动它。

    ⚠️ **两条路（手写 / 模板）都调这个函数** —— 见 `build_config`。
       模板那条路尤其需要：官方 `pod_example.yaml` 的 `inputs.tides` 等
       四个键是**写死在模板里的**，「基础」档必须把它们去掉，
       而"去掉"和"加上"用的是**同一张表**（`catalog.table_inputs()`），
       这样两个方向不可能漂开。

    ⚠️ **必须在替换完 `snx_files` 之后再调**（模板那条路的顺序要求）：
       这个函数对 `snx_files` 是 `extend`（合并），
       先 merge 再替换的话，并进来的那 3 个 yaw 文件会被覆盖掉。
    """
    # ⚠️ 2026-10-01：这里原来是一句 `if not profile.needs_tables: return`。
    #    现在**逐项**说了算（用户可能只打开其中一两项），所以改成
    #    "按算出来的清单写" —— 清单空就是一项都不写，和以前的行为一致。
    for key, value in _filtered_table_inputs(profile, corrections).items():
        if key == "snx_files":
            # ⚠️ 合并，不是覆盖 —— 这个键本来就有 igs_satellite_metadata.snx，
            #    直接赋值会把它连带弄没，**而且不会报错**。
            cfg["inputs"].setdefault("snx_files", []).extend(value)
        else:
            cfg["inputs"][key] = value


def _receiver_options(inp: ConfigInputs, profile: Profile) -> dict[str, Any]:
    """接收机选项 —— 全局默认 + 本测站的近似坐标。

    ============================ ⚠️ 为什么要显式给测站坐标 ============================
    实测（2026-09-18，ALIC 整天数据，2880 历元）：

        **不给** `apriori_position`：
            Ginan 报 `No receiver apriori position found for ALIC`
            → 只能用单点定位的结果凑合
            → 对流层模型算出的测站高度是 **-1000 米 / 11000 米**（被强行截断）
            → **62 颗卫星回退到单频**
            → 定位偏差 45.8 厘米

        **给了**之后（同一份数据、同一套配置，只加这一项）：
            · "先验坐标缺失"和"对流层高度离谱" 两条警告**消失**
            · 回退单频的卫星 **62 颗 → 17 颗**

    ⚠️ 而 RINEX **文件头里明明有** `APPROX POSITION XYZ` ——
       我们实测拿到的那份 ALIC 文件第 10 行就写着
       `-4052052.7249  4212835.9727 -2545104.5766`。

       **"文件里有"不等于"程序能拿到"。**

       所以这里把它读出来、**显式写进配置** ——
       Ginan 官方的示例配置（`exampleConfigs/ppp_example.yaml`）
       也是这么做的：

           # ALIC:
           #   apriori_position: [-4052052.7254, 4212835.9872, -2545104.6139]

    ⚠️ 代价是三个数字，收益是整天的解算质量 ——
       而**不给的失败方式恰好是"看起来正常、只是精度差"**，
       属于本项目最想避免的那一类。

    ⚠️ 站号用的是 Ginan 内部那个 4 位 ID（`rec.id`）——
       它出现在警告里是 `for ALIC`，而不是 RINEX 的 9 位 `ALIC00AUS`。
    """
    options: dict[str, Any] = {
        "global": {
            "elevation_mask": 15,
            "error_model": "ELEVATION_DEPENDENT",
            "code_sigma": 0.3,
            "phase_sigma": 0.003,
            "clock_codes": ["AUTO", "AUTO"],
            "zero_dcb_codes": ["AUTO", "AUTO"],
            "rec_reference_system": "GPS",
            "models": _model_options(profile, inp.corrections),
        },
    }

    # ⚠️ **一个站一个块** —— 网络解就是"这里有好几个块"。
    #    产品（SP3/CLK/ERP/ATX…）是全球统一的，所以整份配置里
    #    **只有这一处**按站分叉。
    #
    # ⚠️ 读不到坐标的站**整个块都不写**：写 (0,0,0) 会被当成地心。
    for sid, pos in inp.stations.items():
        if not sid or not pos:
            continue
        x, y, z = pos
        options[sid] = {
            "apriori_position": [round(float(x), 4),
                                 round(float(y), 4),
                                 round(float(z), 4)],
        }

    return options


def _model_options(profile: Profile,
                   corrections: dict[str, bool] | None = None) -> dict[str, Any]:
    """误差改正模型 —— **档位定对流层，改正项定潮汐和电离层**。

    ⚠️ 2026-10-01 起，潮汐和电离层这几项**不再写死在档位里** ——
       它们来自 `corrections.CORRECTIONS` 那张表，用户可以逐项改。
       档位仍然是**默认值**的来源：`corrections` 为空时，这里写出来的
       和不加这个参数时**逐字节相同**（有守卫钉着）。

    ⚠️⚠️ `enable` 是**总开关**，子项只管"要不要算那一项"。
       整个源码里 `enable` 只被用在一处（`ppp_obs.cpp`）：

           if (recOpts.tideModels.enable) pppTides(...);

       **所以 `solid: true` 配 `enable: false` = 一点改正都没有**，
       而且不报错、不警告、日志里也看不出来。
       （这个组合实测过：结果和全关**一模一样**。）
       ⇒ `enable` 必须是「**有没有任何一项开着**」，不能写成常量 True ——
         用户在界面上把 5 项潮汐全关掉时，常量 True 会让配置里写着
         "潮汐开着"，而实际一项都不算。

    ⚠️ **固体潮在「基础」档也是开的** —— 它不要任何外部文件，
       关掉它没有理由。它需要的两样东西我们都有：
         日、月位置   → 有 DE436 就用，没有就退到 SOFA 的解析星历
                        （`planets.cpp`：`planetPosEcef` → `jplEphPos` 失败
                          则走 `iauEpv` / `iauMoon`）
         地球自转参数 → **本来就要下的** ERP 文件，不是为它多下的

       实测（ALIC 2026-09-02 整天，2880 历元，同一批跑两遍、
       两份配置逐字节相同、只差 tides 块，尺子是 IGc20 的权威坐标）：

           关：3D 9.5 / N -0.1 / E -5.5 / U 7.8 厘米
           开：3D 7.7 / N -0.5 / E -6.2 / U **4.5** 厘米

       **垂直方向几乎减半，水平方向基本不动** —— 这正是固体潮该有的样子
       （它就是垂直方向的位移为主）。改善落在"物理上该改善的那个方向"，
       才说明这个数是它带来的，而不是两次跑之间的随机差异。

       ⚠️ 口径：上面是**全部 2880 个历元**的平均位置，不涉及收敛判定
          （那个两次跑判出来的起点不一样，162 vs 491，拿它当口径等于
          引入了一个非受控变量）。只看收敛后的历元则是 8.3 → 4.0 厘米。
    """
    on = gcorrections.resolve(profile.key, corrections)

    # ---- 潮汐：一个总开关 + 五个子项（子项清单来自注册表，不在这里抄）----
    # ⚠️ 顺序按 Ginan 的结构体（`acsConfig.hpp:1314-1320`：solid / otl /
    #    atl / spole / opole），不是按注册表的顺序 —— 注册表那个顺序是
    #    **给界面看的**（不要文件的排前面）。生成的配置给人读，
    #    和 Ginan 自己的顺序一致少一层认知负担。
    tide_items = {c.model_path[1]: c for c in gcorrections.CORRECTIONS
                  if c.model_path and c.model_path[0] == "tides"}
    tides: dict[str, Any] = {
        "enable": any(on[c.key] for c in tide_items.values())}
    for sub in ("solid", "otl", "atl", "spole", "opole"):
        if sub in tide_items:
            tides[sub] = on[tide_items[sub].key]

    iono = {c.model_path[1]: on[c.key]
            for c in gcorrections.CORRECTIONS
            if c.model_path and c.model_path[0] == "ionospheric_components"}

    # ---- 对流层仍然由档位定 ----
    #
    # ⚠️ 它不是"改正项"（不存在"关掉对流层"这回事），而是"用哪个模型"，
    #    所以留在档位里。
    #
    # ⚠️⚠️ `models` 里**只能有 gpt2，不能加 vmf3** —— 这里原来写的是
    #     `["gpt2", "vmf3"]`，那是一个**跑不起来的组合**：
    #
    #         VMF3 要的是维也纳那边**每天生成**的格网文件
    #         （`src/cpp/trop/tropVMF3.cpp:3565` 拼的是
    #          `VMF3_YYYYDDD.H00`），走配置里的 `vmf_files` 那一栏 ——
    #         它**不在** tables 那个桶里，我们没有任何办法拿到。
    #
    #     而 Ginan 官方自己的默认配置写的也正是 `models: [gpt2]`。
    #
    #     ⚠️ 这类错误很贵：它在配置生成时**不报错**，要到 pea 真跑起来
    #        去读 vmf 文件时才炸，而那个报错离"我们没配错档位"很远。
    #
    # ⚠️ standard 不是"低配" —— 它是 Ginan 内置的简化对流层模型，
    #    不需要 gpt_25.grd 那样的外部格网文件。
    return {
        "phase_bias": {"enable": False},
        "troposphere": {
            "enable": True,
            "models": ["gpt2"] if profile.needs_tables else ["standard"],
        },
        "tides": tides,
        "ionospheric_components": iono,
    }


def _epoch_control(inp: ConfigInputs, wf: gworkflows.Workflow) -> dict[str, Any]:
    """时间控制。

    ⚠️ `wait_next_epoch: 3600` 是**后处理模式**该用的值。

       它的意思是"如果数据还没到，最多等 3600 秒"。
       实时处理时才需要调小 —— 后处理时数据全在磁盘上，
       设大一点没关系，设小了反而可能因为读盘慢而误判"这一历元没数据"。

    ⚠️ `epoch_interval` 的**优先级**：调用方显式给了就用它，
       否则用工作流的默认（见 `Workflow.epoch_interval`）。
       POD 必须是 300 秒 —— 用 30 秒法方程解不出来，见那里的说明。
    """
    ec: dict[str, Any] = {
        # ⚠️ 走 `effective_epoch_interval` —— 和 `epoch_count` **同一个函数**。
        #    各写一遍的话，配置里写 900 秒而 `-n` 按 30 秒算，
        #    两个数会在同一份配置上打架（实测差点踩上，见 `epoch_count`）。
        "epoch_interval": effective_epoch_interval(inp, wf),
        "wait_next_epoch": 3600,
    }
    # ⚠️ 只在明确指定时才写这两个键 ——
    #    写成 None 会让 Ginan 收到一个字符串 "None" 然后解析失败。
    if inp.start_epoch is not None:
        ec["start_epoch"] = inp.start_epoch.strftime("%Y-%m-%d %H:%M:%S")
    if inp.end_epoch is not None:
        ec["end_epoch"] = inp.end_epoch.strftime("%Y-%m-%d %H:%M:%S")
    return ec


def effective_epoch_interval(inp: ConfigInputs, wf: gworkflows.Workflow) -> float:
    """这份配置**实际会用**的解算历元间隔（秒）。

    ⚠️⚠️ 优先级是「**工作流的**盖过调用方给的」—— 和 `_epoch_control` 一致。

       理由见 `Workflow.epoch_interval`：对 POD 来说 300 秒**不是调优，是前提**
       （30 秒那组会让法方程解不出来）。所以工作流说了算。

    ⚠️ 这个函数存在的理由是**防止两处各算一遍**：
       配置里写一个值，而"一共多少个历元"按另一个值算 —— 两个数会在
       同一份配置上打架，而且**都不报错**。实测差点踩上（见 `epoch_count`）。
    """
    if wf.epoch_interval is not None:
        return wf.epoch_interval
    return inp.epoch_interval


def epoch_count(inp: ConfigInputs) -> int | None:
    """这份输入描述的处理**一共多少个历元**。算不出来返回 `None`。

    ⚠️⚠️ 这个数存在的唯一理由：**`epoch_control.end_epoch` 在这个版本的 Ginan 里
    完全不起作用**（2026-09-27 实测，见 `docs/ginan-facts.md` 七点十三）。

    三条对照实验（同一个配置，只改这两个地方）：

        `end_epoch: 2019-07-18 01:00:00`，不给 `-n`   → 跑到 **17,794 个历元**
        同上，加 `-n 5`                              → **退出码 0**，
                                        `Exiting at epoch 5 as epoch count 5 has been reached`
        两个都不给                                    → 跑到 **24,455 个历元**

    ⚠️ 而 PPP/POD 之所以看着正常，是因为它们的**观测文件到当天为止就没了**
       （官方 POD 的日志写着 `Inputs finished at epoch #289`）——
       那是"恰好"，不是"配置管住了"。**没有观测的工作流（`brdc2sp3`）
       就没有这个恰好**：实测它跑到 2022 年、100,407 个历元、1.9 GB 产物。

    ⇒ "要跑多少"必须由**我们**算出来，不能指望配置里那个键。

    ⚠️ 返回 `None` 表示**算不出来**（没给起止时间）——那时调用方
       **不传 `-n`**，行为和以前完全一样（跑到观测文件读完为止）。
       **绝不猜一个默认值**：猜小了会静默截断结果，而截断后的 POS/SP3
       看起来和正常跑完的一模一样。
    """
    if inp.start_epoch is None or inp.end_epoch is None:
        return None

    wf = gworkflows.get(inp.workflow)

    span = (inp.end_epoch - inp.start_epoch).total_seconds()
    if span <= 0:
        return None

    # ⚠️⚠️ **必须用配置里那个间隔，不是 `inp.epoch_interval`。**
    #
    #     两者**可以不一样**：`Workflow.epoch_interval` 会盖掉它
    #     （POD 300 秒、brdc2sp3 900 秒，而 `inp.epoch_interval` 默认 30）。
    #
    #     实测差点撞上：`epoch_count` 原来读的是 `inp.epoch_interval`，
    #     于是 `brdc2sp3` 算出来是 **2880**（按 30 秒），而配置里写的是 900 秒
    #     ⇒ `-n 2880` 配 900 秒的配置 = **允许跑 30 天**，
    #       而它没有观测、没有任何东西会拦住它。
    #
    #     ⇒ 用 `effective_epoch_interval` —— **和 `_epoch_control` 同一个函数**，
    #       两边不可能给出不同的间隔。
    interval = effective_epoch_interval(inp, wf)
    if not interval or interval <= 0:
        return None

    # ⚠️ `ceil` 不是 `floor`：`end_epoch` 是**闭区间的右端点**
    #    （一天的 00:00 → 次日 00:00），所以要覆盖到最后一刻。
    #
    #    实测对得上：86400 / 300 = **288**（POD 实测就是 288）、
    #                86400 / 900 = **96**（brdc2sp3 实测就是 96）、
    #                86400 / 30  = 2880。
    #    用 `floor` 的话是 287/95/2879 —— **少一个历元**，而少一个历元
    #    不会报错，只会让最后 5 分钟/15 分钟的结果没算。
    return int(math.ceil(span / interval))


def _sys_options(systems: tuple[str, ...]) -> dict[str, Any]:
    """每个星座的信号优先级。

    ⚠️ `code_priorities` 里的码是**按优先级排的**，Ginan 从头找第一个可用的。
       排在前面的应该是：信号质量好、噪声低、且接收机普遍支持的。

       这几组值来自 Ginan 示例配置 —— 是经过验证的默认，
       不是我们随便挑的。
    """
    table = {
        "gps": {
            "process": True,
            "reject_eclipse": False,
            "code_priorities": ["L1W", "L1C", "L2W", "L2S"],
        },
        "gal": {
            "process": True,
            "reject_eclipse": False,
            "code_priorities": ["L1C", "L1X", "L5Q", "L5X"],
        },
        "glo": {
            "process": True,
            "reject_eclipse": False,
            "code_priorities": ["L1C", "L1P", "L2C", "L2P"],
        },
        "bds": {
            "process": True,
            "reject_eclipse": False,
            "code_priorities": ["L2I", "L6I", "L7I"],
        },
    }
    out: dict[str, Any] = {}
    for name in table:
        if name in systems:
            out[name] = dict(table[name])
        else:
            # ⚠️ 不处理的星座要**显式写 False**，不能省略。
            #    省略是"用默认值"，而默认值可能是 True ——
            #    于是用户以为关掉了，实际还在处理，结果莫名其妙。
            out[name] = {"process": False}
    return out


# =============================================================================
# 落盘
# =============================================================================


def write_config(cfg: dict[str, Any], path: Path, header: str = "") -> Path:
    """把配置写成 YAML 文件。

    ⚠️ `sort_keys=False` —— **必须**。
       PyYAML 默认会把键按字母排序，而 Ginan 的配置是有**逻辑顺序**的
       （inputs 在前、outputs 在后）。

       排序之后文件虽然还能跑，但人看起来完全乱了，
       而且和示例配置对不上，排查问题时很难对照。

    ⚠️ `default_flow_style=False` —— 让列表竖着展开。
       示例配置里 `code_priorities: [L1W, L1C, ...]` 是横的，
       但绝大多数列表是竖的。竖着更好读，也更好 diff。
    """
    import yaml

    path = Path(path)

    # ⚠️ 绝不给 Ginan 的安装目录写文件 —— 见 paths.guard_not_in_ginan
    from . import paths as _paths

    _paths.guard_not_in_ginan(path, "生成的配置文件")

    path.parent.mkdir(parents=True, exist_ok=True)

    text = yaml.safe_dump(
        cfg,
        allow_unicode=True,          # 中文注释/描述别转成 \uXXXX
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        width=1000,
    )

    body = (header + "\n" if header else "") + text
    path.write_text(body, encoding="utf-8")
    return path


def config_header(inp: ConfigInputs) -> str:
    """给生成的配置文件写一段人话表头。

    ⚠️ 这段表头是**给用户看的**，不是给 Ginan 的（YAML 注释它不读）。

       为什么值得写：用户打开这份配置时，最需要的不是"有哪些参数"
       （那有 3000 个），而是"这份配置是干什么的、用了什么档位、
       有什么代价"。**直接写在文件顶上，不用去别处查。**
    """
    profile = PROFILES.get(inp.profile, PROFILES[DEFAULT_PROFILE])
    lines = [
        "# " + "=" * 70,
        "# Ginan 配置 —— 由 Ginan Agent 自动生成",
        "# " + "=" * 70,
        "#",
        f"# 配置标识：{inp.config_id}",
        f"# 配置来源：{config_source(gworkflows.get(inp.workflow))}",
        f"# 处理档位：{profile.label}",
        f"#   {profile.description}",
        "#",
        "# ⚠️ 代价（如实说明）：",
    ]
    for line in profile.tradeoffs.split("。"):
        if line.strip():
            lines.append(f"#   {line.strip()}。")
    lines += [
        "#",
        "# 输入文件：",
        f"#   观测数据  {len(inp.rnx_inputs)} 个",
        f"#   卫星轨道  {len(inp.sp3_files)} 个",
        f"#   卫星钟差  {len(inp.clk_files)} 个",
        f"#   天线文件  {len(inp.atx_files)} 个",
        "#",
        "# 需要人工修改吗？**不需要** —— 文件全在，直接跑就行。",
        "# " + "=" * 70,
        "",
    ]
    return "\n".join(lines)
