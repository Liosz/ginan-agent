"""Ginan 领域层 —— 读懂 pea 的输出。

============================ 这个模块解决什么问题 ============================
`pea` 失败时会打印一堆东西，然后退出。**对懂 GNSS 的人是信息，对不懂的人是噪音。**

实测的一段真实输出：

    [2026-09-18 04:19:44] [info]  Missing antenna information (ANTEX file) file /path/igs20.atx
    Warning: Invalid SINEX file
    Dry-run failed: configuration is invalid
    PEA finished

一个新手看到这个只会问"那我该干嘛"。而这个模块的职责就是回答那个问题。

============================ ⚠️ 为什么必须自己解析，不能只看退出码 ============================
两个原因，都是实测出来的：

  ① **Ginan 会"成功"但配置被改过**
     6 个 sanity checker 里 4 个会**静默改写配置**，只发 warning 级日志。
     退出码 0，但跑的已经不是用户要给的那份配置了。

  ② **`--dry-run` 的成功语是误导的**
     `sanityChecks()` 返回 void，`runAllChecks()` 的结果被丢弃。
     退出码只覆盖"YAML 能解析 + 声明文件都存在"，**与 checker 无关**。

所以：**退出码 + 输出解析，两个都要看。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["error", "warning", "info"]


@dataclass
class Diagnostic:
    """从输出里识别出的一条问题。

    ⚠️ `explain` 和 `fix` 是这个类的重点，不是 `raw`。

       用户不需要看到 pea 的原话，他需要知道"发生了什么"和"我该干嘛"。
       raw 保留下来是给**排查问题**用的（用户截图给开发者时要有原文）。
    """

    severity: Severity
    kind: str            # 机器可读的分类
    raw: str             # pea 的原话（保留，用于排查）
    what: str            # 人话解释：发生了什么
    what_to_do: str = ""  # 该怎么办

    @property
    def is_blocking(self) -> bool:
        return self.severity == "error"


# =============================================================================
# 识别规则
# =============================================================================
#
# ⚠️ 这些模式全部来自**实测的 pea 输出**，不是照文档猜的。
#    加新规则时请先跑一次真的 pea，把原文抄进来。


@dataclass(frozen=True)
class Rule:
    kind: str
    severity: Severity
    pattern: re.Pattern
    explain: str
    fix: str = ""


def _p(s: str) -> re.Pattern:
    return re.compile(s, re.IGNORECASE)


# =============================================================================
# ⚠️⚠️ 先把终端颜色码去掉 —— 否则**一条警告都看不到**
# =============================================================================
#
# 实测：pea 的输出**带 ANSI 颜色码**。
#
#     >>> repr(日志里的一行)
#     '\x1b[1;93mWarning: Antenna name not specified for DCMS\x1b[0m'
#       ↑ 行首是转义符，不是 'W'
#
# ⚠️ 当时**唯一**的那条警告规则写的是 `^Warning:` —— **锚定在行首**。
#    所以它一条都匹配不上：实测那次运行里 **227 条警告**全被漏掉，
#    结论报的是"**处理正常完成**"。
#
# ⚠️ 这个 bug 特别难发现，因为**解析失败的默认表现是"什么都没找到"** ——
#    和"这次运行真的没问题"**长得一模一样**。
#    没有报错、没有异常，只是安静地漏掉了一切。
#
# ⚠️⚠️ 还有一层：**这里绝对不能靠"我加的规则能匹配上"来自我验证。**
#
#    后来加的具体规则都是 `search` + **不带锚点**的，所以带不带颜色码都能匹配 ——
#     用它们当"去色有没有生效"的判据**测不出来**
#     （测试第一版就是这么写的，把 strip_ansi 删掉照样全绿）。
#
#     所以测试里专门放了一条**只能被锚定规则接住**的警告当探针。
#     **验证一个修复有没有生效，要用只有它才能解释的现象。**
#
# > 教训：**解析外部程序的输出时，先假设它会带颜色码。**
# > 尤其是那种"没有匹配上就静默跳过"的解析器 ——
# > 它会把"解析器坏了"伪装成"一切正常"。
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """去掉终端颜色码。

    ⚠️ 单独导出成函数，因为：
       ① 测试要用它（断言"先去色再匹配"这件事本身）
       ② 用户复制日志给别人看时也用得上

    ⚠️ 不要去改 pea 的调用参数去关颜色（比如加 `--no-color`）——
       那样子类进程的输出格式就和我们解析的不一样了，
       而且**手动运行 pea 时看到的又会是另一种**。
       **在解析这一侧处理**，比要求对方配合更可靠。
    """
    return ANSI_ESCAPE.sub("", text)


RULES: list[Rule] = [
    # ---- 缺输入文件 ----
    #
    # 实测格式：`Missing <描述> file <路径>`
    # （来源：inputs.cpp 的 checkValidFile）
    Rule(
        "missing_file", "error",
        _p(r"Missing\s+(?P<what>.+?)\s+file\s+(?P<path>\S+)"),
        "缺少一个必需的文件，所以处理没有开始。",
        "这通常意味着下载没下全。可以重新获取数据，"
        "或者把文件手动放进收件箱。",
    ),

    # ---- 配置无效 ----
    Rule(
        "invalid_config", "error",
        _p(r"(Dry-run failed: configuration is invalid|Incorrect configuration)"),
        "配置有问题，Ginan 拒绝开始处理。",
        "上面通常有一条具体的原因（比如缺文件、路径写错）。",
    ),

    # =========================================================================
    # ⚠️ 具体的警告规则 —— **必须排在最后那条兜底规则之前**
    # =========================================================================
    #
    # ⚠️⚠️ 规则是**按顺序匹配、一行只取第一条**的。
    #     所以**越具体的越要排在前面**。
    #
    #     这些全部来自实测输出（2026-09-18，用一份"头部是空的" RINEX 跑的），
    #     原文抄在每条规则的注释里。
    #
    # ---- 天线信息缺失：**没有做相位中心改正** ----
    #
    # 实测原文：
    #   Warning: Antenna name not specified for DCMS
    #   Warning: Antenna details not found for DCMS
    #
    # ⚠️ 这是**最危险的一类**：它不报错、不影响完成，只是让结果
    #    系统性地偏几厘米 —— 而"看起来完全正常"。
    Rule(
        "missing_antenna", "warning",
        _p(r"Antenna (?:name not specified|details not found)"),
        "测站的天线型号没有填，**天线相位中心改正没有做**。",
        "这会引入**几厘米的系统偏差**，而且结果看起来完全正常、不会报错。"
        "把天线的真实型号补进 RINEX 头部再重跑。"
        "（`inspect_rinex` 会告诉你缺什么，`repair_rinex` 能补。）",
    ),

    # ---- 先验坐标缺失或离谱 ----
    #
    # 实测原文：
    #   Warning: No receiver apriori position found for DCMS
    #   Warning: Apriori for DCMS is 2067.22m from SPP estimate
    #
    # ⚠️ 后面那个数字是"先验坐标与单点定位估计值的差"。
    #    正常情况下应该是**几十米以内** —— 实测 **2067 米**，
    #    说明观测数据的头里根本没有近似坐标。
    Rule(
        "bad_apriori_position", "warning",
        _p(r"(?:No receiver apriori position found"
           r"|Apriori for \S+ is (?P<dist>[0-9.]+)\s*m from SPP)"),
        "测站的**先验坐标**缺失或明显不对，Ginan 只能用单点定位的结果凑合。",
        "先验坐标差太多时，对流层模型和定位结果都会明显失真。"
        "正常差值在几十米以内；**上千米**说明数据头里没有近似坐标。",
    ),

    # ---- 对流层模型拿到离谱的高度 ----
    #
    # 实测原文：
    #   Warning: tropSAAS: hgt < -1000m, setting it to -1000m
    #   Warning: tropSAAS: hgt > 11000m, setting it to 11000m
    #
    # ⚠️ 这是**先验坐标不对的下游症状**，但现象非常直白 ——
    #    测站的海拔算出来是 -1000 米或者 11000 米。
    Rule(
        "bad_tropo_height", "warning",
        _p(r"tropSAAS:\s*hgt\s*[<>]\s*(?P<hgt>-?[0-9.]+)\s*m"),
        "对流层模型算出的测站高度**明显不合理**，已被强行截断。",
        "这几乎总是「先验坐标不对」的连带症状 —— **定位结果不可信**。",
    ),

    # ---- 回退到单频 ----
    #
    # 实测原文：
    #   Warning: Code measurement not available on secondary frequency
    #            for G01 at DCMS, falling back to single-frequency
    #
    # ⚠️ PPP 需要双频。回退到单频之后精度大幅下降 —— 而它不报错。
    Rule(
        "single_frequency_fallback", "warning",
        _p(r"falling back to single-?frequency"),
        "有些卫星**回退到单频**了 —— 而精密单点定位需要双频。",
        "精度会明显下降。用 `inspect_rinex` 看看这份数据到底是不是双频的。",
    ),

    # ---- SINEX 无效 ----
    #
    # ⚠️ 这条的文案**改过**（2026-09-20）—— 因为原来是假的。
    #
    #    原文写的是"SINEX 是本程序目前不下载的文件 —— 影响通常有限，但要知道。"
    #    两句都站不住：
    #      · 前半句**已经不是事实**：现在会下 `igs_satellite_metadata.snx` 了
    #      · 后半句"影响通常有限"是**我们猜的**，没有任何实测依据
    #
    #    👉 一句话写得像结论、其实是猜测，比不写更糟 ——
    #       它会让读的人**停止追查**。
    Rule(
        "invalid_sinex", "warning",
        _p(r"Invalid SINEX file"),
        "卫星元数据文件（SINEX）没读到，卫星的姿态只能用通用模型凑合。",
        "这个文件本来会随计划一起下载（`igs_satellite_metadata.snx`）。"
        "看到这条说明**它没下到**（或用户手动删了）—— "
        "可以重新跑一次取数，或单独把它下回来。"
        "影响主要在地影期那几段，但它确实是一处该改而没改的改正。",
    ),

    # ---- 码偏差缺失 ----
    #
    # ⚠️ 这一族实测**一次 24 小时的解算里有 100 多条**：
    #      Bias not found for G27 on L1W, using undefined_sigma: 1
    #      Receiver code bias not found for ALIC : L1W. Using undefined …
    #
    # ⚠️ 两种成因要分开说，**不能混成一句**：
    #      ① 用户处理的是 **2024 年以前**的数据 —— 那时这个产品还没发布
    #         （实测边界：GPS 周 2238 = 2022-11-27）
    #      ② 产品有，但这次没下到
    #    混起来说的话，第①种情况的用户会去反复重试一件**不可能成功**的事。
    Rule(
        "bias_missing", "warning",
        _p(r"Bias not found for .* using undefined_sigma|"
           r"(Receiver|Satellite) code bias not found"),
        "有些卫星/接收机的**信号延迟偏差**没查到，那些观测量用了默认值兜底。",
        "码偏差文件（`*.BIA`）本来会随计划下载。没查到的常见原因有两个："
        "① 数据日期在 2022-11 以前 —— 那时 IGS 还没发布这个产品，"
        "**重试也没用**；② 这次没下到 —— 重跑一次取数即可。"
        "它对最终坐标的影响是毫米级的（改正的是伪距，而解算由相位主导），"
        "但不该当成'没有影响'。",
    ),

    # ---- ⚠️ 配置被静默改写 ----
    #
    # 这一条最容易被忽略：**退出码还是 0，但配置已经不是你给的那份了**。
    #
    # 实测原文（来自 sanity checker）：
    #   Warning: Epoch tolerance is greater than half the epoch interval.
    #            Adjusting to half the epoch interval.
    #
    # ⚠️⚠️ 它的模式**收得很紧**，这是刻意的：
    #
    #     原来写的是 `^Warning:` —— 匹配**任何**警告行。于是
    #     天线没填、先验坐标错 2 公里、回退到单频……全被报成"配置被改了"。
    #
    #     现在只匹配**"改一个值"这个动作**的措辞
    #     （adjust / override / replace / reset / change … to …）。
    #     认不出来的那些会掉到最后的兜底规则，**如实说"不认识"** ——
    #     那比给一个自信但错误的解释强。
    Rule(
        "config_mutated", "warning",
        _p(r"Warning:.*\b(?:adjust|overrid|replac|reset|chang)\w*\b.*\bto\b"),
        "Ginan 自动调整了配置里的某些值（它认为原值不合理）。",
        "这不会让处理失败，但结果和你写的不完全一致。"
        "如果对精度有要求，值得看一眼被改了什么。",
    ),

    # ---- 线程设置 ----
    #
    # 实测原文（来自 Ginan README）：
    #   OpenBLAS Warning : Detect OpenMP Loop and this application may hang
    Rule(
        "openblas_threads", "warning",
        _p(r"OpenBLAS Warning\s*:\s*Detect OpenMP Loop"),
        "BLAS 库和 Ginan 自己的多线程在抢 CPU，可能让程序卡住。",
        # ⚠️ 建议改过（2026-09-25）：原来照抄 OpenBLAS 自己那句话，
        #    让用户设 OPENBLAS_NUM_THREADS=1。**那个建议是有代价的** ——
        #    实测设了之后进程平均只占 1 个核，不设能占 8 个（快 1.6 倍以上），
        #    因为 Ginan 那 20 个 OMP 线程建了却没干活。
        #    本程序**不再**替用户设它（见 `environment.pea_env()`）。
        "⚠️ 但**不要**拿 OPENBLAS_NUM_THREADS=1 去压它："
        "实测那会把并行砍到只剩 1 个核 —— 20 个核在旁边看着 1 个跑"
        "（不设能占 8 个核，快 1.6 倍以上）。"
        "本程序不会替你设。真卡住了再回来看这一条。",
    ),

    # ---- 数据相关 ----
    #
    # ⚠️⚠️ 这一条**被收窄过一次**，收窄的理由值得看完（2026-09-29 实测）：
    #
    #    它原来的 pattern 是 `(No observations|zero epochs|no data)`，
    #    于是它接住了**两种完全正常的输出**：
    #
    #        `Warning: Epoch 1 has no observations`          实测 1948 次
    #        `Receiver ZIML has no data for this epoch ...`  实测约 10000 次
    #
    #    后者在多站网解里是**每个站的每个缺数据历元**都打一句 ——
    #    完全正常。而这条规则是 **error** 级，于是 `headline` 变成
    #    「处理没有完成 —— 在这段时间里没有找到可用的观测数据。」
    #
    #    实测代价：**25 个触发它的文件，25 个都真的处理了历元**
    #    （最多的 25,200 个），而结论全都说"没有找到观测"。
    #    这是个"跑完了却说没跑完"的假话，用户会去查一个不存在的数据问题。
    #
    # ⚠️ 而**真正的致命写法在这些日志里一次都没出现过** ——
    #    所以现在只接明确带 `Error:` 的那种，其余交给下面两条 info 规则。
    Rule(
        "no_observations", "error",
        _p(r"error\s*:\s*.*(no observations|zero epochs|no data)"),
        "在这段时间里没有找到可用的观测数据。",
        "确认观测文件覆盖了你指定的时间段。",
    ),
    # ⚠️ 这两条是**信息**，不是警告 —— 它们描述的是正常现象，
    #    没有任何需要用户做的事：
    #      · 开头的历元常常还没有观测（数据还没开始）
    #      · **不需要观测的工作流**（广播星历转轨道）每个历元都会打这句
    #      · 多站网解里某个站在某个历元缺数据是常态
    #    ⚠️ 用 `info` 而不是 `warning` 是有实际后果的：
    #       `ginan_tools` 只把 warning/error 交给模型（见那里对 severity 的过滤），
    #       所以 info 不会糊到用户脸上 —— 但它仍然留在 JSON 记录里可查。
    Rule(
        "epoch_without_observations", "info",
        _p(r"Warning:\s*Epoch \d+ has no observations"),
        "有些历元没有观测数据。",
        "如果这次处理本来就不需要观测（比如广播星历转轨道），这是正常的。",
    ),
    Rule(
        "receiver_epoch_gap", "info",
        _p(r"has no data for this epoch"),
        "某个测站在某个历元没有数据。",
        "多站一起解算时很常见（比如那个站的数据还没开始）。开头几个历元尤其正常。",
    ),
    Rule(
        "no_satellites", "error",
        _p(r"(No satellites|insufficient satellites)"),
        "能用的卫星太少，无法定位（至少要 4 颗）。",
        "可能是仰角设置太高，或者观测时段太短。",
    ),

    # ---- 精度相关的警告（不致命，但要知道）----
    #
    # ⚠️⚠️ 这条规则原来**从来没生效过**（2026-09-29 实测发现）。
    #    pattern 写的是 `(large residual|residual.*exceed)` —— 那是**推测**的措辞，
    #    而 Ginan 真正打印的是（实测原文，从 TRACE 里抄的）：
    #
    #        LARGE MEAS    ERROR OF :     1234.5678	AT   42 :	 PHAS_MEAS	 E14	ALIC	L1C
    #        LARGE STATE   ERROR OF :        0.0123	AT   17 :	     ORBIT	 L64	     Y
    #
    #    **一个"residual"字样都没有。** 于是这些行全都落进了兜底的
    #    `unrecognized_error`（因为它含 "ERROR"），被判成 **error** ——
    #    而它其实只是"这个观测值/状态量偏大"的常规提示。
    #
    #    👉 规则文档里那句"这些模式全部来自实测"必须真的做到。
    #       凭印象写一条 pattern，比没有这条规则更糟：它看起来是被覆盖的。
    Rule(
        "large_residuals", "warning",
        # ⚠️ **三种措辞**都要（都是实测抄来的，一条别少）：
        #    LARGE MEAS    ERROR OF :  1234.5678  AT 42 :  PHAS_MEAS  E14 ...
        #    Largest state error is :     0.0123  AT 17 :       ORBIT  L64 ...
        #    Largest  ref meas error:     <数>     AT  1 :  CODE_MEAS  G12 ...
        _p(r"(LARGE\s+(MEAS|STATE)\s+ERROR OF"
           r"|Largest\s+\w*\s*(meas|state)\s+error)"),
        "有些观测值或状态量的偏差比较大，已被当作异常值剔除。",
        "少量是正常的。如果很多，说明数据质量可能有问题。",
    ),
    Rule(
        "yaw_bias_missing", "warning",
        # 实测 6 次：
        #   Warning: Max yaw rate not found for G061 in satYawGpsIIR,
        #            check sinex files for '+SATELLITE/YAW_BIAS_RATE' block
        _p(r"Max yaw rate not found for"),
        "缺卫星偏航率（yaw bias rate）—— Ginan 只能自己估卫星的姿态。",
        "这个文件本程序**能下**：数据目录里应该有一份 "
        "`sat_yaw_bias_rate.snx`（缺失时重新跑一次「准备数据」会补上）。",
    ),
    Rule(
        "innovation_error", "warning",
        # 实测 10 次。⚠️ 这一行的原文**就这三个词**：`Error: INNOVATION`
        #    —— Ginan 自己没打印任何细节。所以这里要如实说"上游没给细节"，
        #      而不是假装我们能解释它。
        _p(r"^\s*Error:\s*INNOVATION\s*$"),
        "Ginan 的观测残差（innovation）检查没过 —— "
        "但它**只打了 INNOVATION 这个词，没有给任何细节**。",
        "先用试跑（少跑几个历元）看能不能复现，再看同一时段的观测和先验值。"
        "这条在排查时值得截图给开发者 —— 原文本身没有任何线索。",
    ),
    Rule(
        "config_sanity_hint", "info",
        # 实测 3 次：Warning: ionospheric_components:outage_reset_limit
        #            < epoch_interval, but it probably shouldnt be
        _p(r"but it probably shouldnt be"),
        "Ginan 觉得配置里某项的取值和别的项对不上（它自己也用 "
        "“probably” 留了余地）。",
        "先不动。如果结果看起来不对，再回来看这一条。",
    ),
    Rule(
        "invalid_option_value", "info",
        # 实测 10 次：Error: none is not a valid entry for option: @ mode.
        #   （出现在实验用的"负对照"配置里 —— 那些配置故意写了个不合法的值）
        _p(r"is not a valid entry for option"),
        "配置里某个选项的值不被接受（Ginan 只认它列出的那几个取值）。",
        "如果这份配置不是你自己改的，报给开发者；自己改过的就对照官方示例改回来。",
    ),
    Rule(
        "no_jpl_ephemeris", "info",
        # 实测 6 次：Warning: No JPL ephemeris file found.
        _p(r"No JPL ephemeris file found"),
        "没有 JPL 的行星历表（DE436）—— Ginan 退回了它自带的解析星历。",
        "**这是设计好的回退**（见 config.py 里那段）。本程序不随包提供那份历表"
        "（许可证原因）。想和官方示例逐字一致，就自己把 "
        "`tables/DE436.1950.2050` 放进数据目录。",
    ),

    # ---- 解算过程出的问题 ----
    Rule(
        "filter_diverged", "error",
        _p(r"(filter diverged|divergence detected|numerical instability)"),
        "解算过程发散（数值不稳定），无法继续。",
        "常见原因：观测数据里有大的错误、或者某个参数设得不合理。"
        "可以先用短一点的时间段试试。",
    ),
    Rule(
        "out_of_memory", "error",
        _p(r"(bad_alloc|out of memory|std::bad_alloc)"),
        "内存不够，处理中断了。",
        "数据量太大或者系统内存紧张。可以只处理一部分时段。",
    ),

    # ---- 2026-09-29 补：从真实日志里捞出来、之前全落进兜底的几种 ----
    #
    # ⚠️ 这几条的措辞**是从 TRACE 里逐字抄的**，每条后面写着实测出现次数。
    #    加规则请照这个做法 —— 上面 `large_residuals` 那条就是凭印象写、
    #    结果从来没匹配上过的教训。
    Rule(
        "bad_rnax_header", "warning",
        # 实测 11 次：*** ERROR radome2none(): string length is less then 15 characters received 0
        _p(r"radome\s*2?none\(\).*string length"),
        "观测文件的头部里，天线罩（radome）那一栏长度不对 —— "
        "Ginan 把它当成空值处理了（函数名就叫 radome2none）。",
        "天线罩是天线型号的一部分，对不上就可能按错的天线算改正。"
        "用 repair_rinex 把这一栏按实际型号补上。",
    ),
    Rule(
        "invalid_yaml_option", "warning",
        # 实测 7 次（两种路径）：
        #   ... is not a valid yaml option
        _p(r"is not a valid yaml option"),
        "配置里有一个 Ginan 不认识的设置项 —— **那一项不会生效**。",
        "多半是配置模板里带的历史遗留键（官方示例里就有）。"
        "不影响跑通，但如果你本来指望那一项起作用，得看别的地方。",
    ),
    Rule(
        "filter_iterations_limit", "warning",
        # 实测 11 次：Warning: Max pre-fit filter iterations limit reached ... in /MINCON_TRANSFORM
        _p(r"Max (pre|post)-fit filter iterations limit reached"),
        "滤波器迭代到了上限还没收敛（`/PPP`、`/IONO`、`/MINCON_TRANSFORM` 等环节都可能）。",
        "偶尔出现是正常的。如果很多，多半是这段数据的几何条件或数据质量不好。",
    ),
    Rule(
        "no_apriori_clock", "warning",
        # 实测 4 次：Warning: No receiver apriori clock found for YARL
        _p(r"No receiver apriori clock found"),
        "某个测站没有先验钟差信息（这个站的数据里没带，或者钟差文件里没有它）。",
        "大多数情况下不影响结果。如果那个站的结果明显不对，从这里查起。",
    ),
    Rule(
        "no_sat_clock", "warning",
        # 实测 1 次：Warning: No sat clk found for G18.
        _p(r"No sat clk found for"),
        "某颗卫星的钟差没找到 —— 那一颗会被跳过。",
        "偶尔一两颗正常。如果整批卫星都缺，检查钟差文件是不是对应那一天。",
    ),
    Rule(
        "erp_missing", "warning",
        # 实测 3 次：Warning: Failed to get ERP at 2025-06-15 00:00:00.01.
        _p(r"Failed to get ERP at"),
        "拿不到地球自转参数（ERP）。",
        "个别历元缺是正常的。全都缺说明 ERP 文件没下到、或者日期对不上。",
    ),
    Rule(
        "no_precise_ephemeris", "warning",
        # 实测 1 次：Warning: Looking for precise position, but no precise ephemerides found for E03
        _p(r"no precise ephemerides found for"),
        "精密星历里没有这颗卫星 —— 它的观测会被忽略。",
        "检查星历文件是不是覆盖了这段时间、以及这颗卫星当天的实际状态。",
    ),
    Rule(
        "start_epoch_rounded", "info",
        # 实测 1 次：Warning: Start epoch not configured, rounding down first obs time ...
        _p(r"Start epoch not configured, rounding down"),
        "没指定起算时刻，Ginan 按第一个观测历元自动对齐了。",
        "",
    ),

    # ---- 兜底：没被上面任何规则接住的 error ----
    #
    # ⚠️ 这条**必须在最后**。
    #
    #    规则是按顺序匹配、匹配到一条就 break 的。
    #    兜底规则放前面的话，所有具体规则都不会被触发 ——
    #    于是所有错误都显示成笼统的"出错了"。
    #
    #    这比"没有兜底"更糟：用户以为系统认识这个错误（因为它给了说明），
    #    实际上那说明什么都没说。
    Rule(
        "unrecognized_error", "error",
        _p(r"\berror\b\s*[:\-]?\s*(?P<what>.{0,120})"),
        "Ginan 报了一个错误。",
        "原文保留在下面 —— 可以把它发给开发者，或者在 Ginan 的 "
        "GitHub Issues 里搜索相同的文字。",
    ),

    # ---- 兜底：没被上面任何规则接住的 warning ----
    #
    # ⚠️ 同样**必须在最后**（理由见上一条）。
    #
    # ⚠️⚠️ 这条规则原来在**列表最前面**，而且写的是 `^Warning:` ——
    #     匹配**任何**警告，解释还写死成"Ginan 自动调整了配置里的某些值"。
    #
    #     后果（实测那次 227 条警告全是这么被吃掉的）：
    #
    #         天线型号没填        → "配置被改了"
    #         先验坐标错 2 公里   → "配置被改了"
    #         回退到单频          → "配置被改了"
    #
    #     **一个笼统的规则会把所有具体问题吃掉** —— 而且它给的是
    #     一个**自信的、错误的**解释，比"我不知道这是什么"更糟：
    #     用户会照着那个错误的方向去查，而真正的问题永远浮不出来。
    #
    #     现在它退到最后，而且**如实说自己不认识**。
    Rule(
        "other_warning", "warning",
        _p(r"^Warning:\s*(?P<what>.{0,120})"),
        "Ginan 报了一条警告（原文见下）。",
        "多数警告不影响结果。但如果结论看起来不对，"
        "这条原文是排查的起点。",
    ),
]


# =============================================================================
# 解析
# =============================================================================


@dataclass
class Diagnosis:
    """一次运行的诊断结果。"""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    version: str = ""
    epochs_processed: int = 0
    finished: bool = False       # pea 有没有走到 "PEA finished"

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    @property
    def blocking(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_blocking]

    @property
    def headline(self) -> str:
        """一句话总结。

        ⚠️⚠️ **判据的顺序就是结论的正确性** —— 这里原来把 `blocking` 放在最前面，
        于是**只要日志里出现一条 error 级诊断**，结论就是"处理没有完成"。

        实测代价（2026-09-29，扫 ginan-testdata 里的真实日志）：
        触发 `no_observations` 的 **25 个文件全部**都真的处理了历元
        （最多的 25,200 个 / 20,057 个），而结论是
        「处理没有完成 —— 在这段时间里没有找到可用的观测数据。」

        👉 **跑完了却说没跑完**，用户会去查一个根本不存在的数据问题。
        而真正"没跑完"的证据就在同一个对象里：`finished`。

        ⇒ 判据改成：**先看它有没有跑完**，再看有什么要说的。
           `finished` 为真 ⇒ 结论必须是"完成"（问题照说，但不改口）。
        """
        noted = self.errors + self.warnings   # info 不算"需要注意"

        if self.finished:
            if noted:
                return (f"处理完成了（{self.epochs_processed} 个历元），"
                        f"但有 {len(noted)} 处需要注意。")
            if self.epochs_processed == 0:
                # ⚠️ 说"正常完成"而不提"一个历元都没算"，是一句听着像成功、
                #    实际什么都没产出的话。试运行（--dry-run）也走这条路。
                return "处理结束了，但没有解算任何历元（可能是试运行，或者时段内没有数据）。"
            return "处理正常完成。"

        # 没跑到结束 —— 这时候 blocking 才是对"为什么"的解释
        if self.blocking:
            return "处理没有完成 —— " + self.blocking[0].what
        if self.epochs_processed:
            return (f"处理中途停了（已经算了 {self.epochs_processed} 个历元），"
                    "没有看到正常的结束标志。")
        return "处理的结束状态不明确（没有看到正常的结束标志）。"

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "headline": self.headline,
                "version": self.version,
                "epochs": self.epochs_processed,
                "finished": self.finished,
                "problems": [
                    {
                        "severity": d.severity,
                        "kind": d.kind,
                        "what": d.what,
                        "what_to_do": d.what_to_do,
                        "raw": d.raw[:200],
                    }
                    for d in self.diagnostics
                ],
            },
            ensure_ascii=False,
            indent=2,
        )


# 版本横幅：`PEA starting... (HEAD v4.1.4-5033996 from ...)`
_VERSION = _p(r"PEA starting\.\.\.\s*\((?P<ver>[^)]+)\)")

# 进度。⚠️⚠️ 真 pea 一个历元会打印**两遍**，两种写法：
#
#     前向解：`Processed epoch #1234 - GPS time: 2022 03 01 0 0 30 (...)`
#     RTS 反向：`Processed epoch - 2022-03-01 00:01:00.01 (took ...)`
#
# ⚠️ 实测（真日志 2880 历元那次）：
#      `grep -c 'Processed epoch #'`   → 2880   ← 真实的历元数
#      `grep -cE 'Processed epoch - '` → 2879   ← 反向那一遍，少 1
#
# 所以**只认前向那种**（带 `#`）：
#   · 认成两种都数 → **正好翻倍**（下面是另一处犯的错）
#   · 认成只有 `-` 那种 → 比真实少 1
#
# ⚠️⚠️ **2026-09-29 补：用 `-n` 截断的运行，这个数会比产物少 1。**
#
#    实测（`brdc2sp3`，ALIC 2026-09-02，app 自动传 `-n 96`）：
#
#        日志里 `Processed epoch #` 的行数         95
#        最后一条日志的历元                        23:30:00
#        产物 SP3 头部声称的历元数                 96
#        产物最后一个历元                          23:45:00   ← 那一历元**真的算出来了**
#
#    ⇒ Ginan 在 `-n` 到点时会先写产物、再跳出循环，**最后那一历元不打日志行**。
#
#    ⚠️ 上面那句"2880 是真实的历元数"是拿**跑到自然结束**那次量的
#       （观测读完了就停，走的是另一条出口）—— 两种情况下这个计数**不一样**。
#
#    ⚠️ 而本程序**现在每次都传 `-n`**（那是我加的刹车，见 `do_run_processing`），
#       所以结论里那个"（N 个历元）"**系统性偏小 1**。
#       **知道就行，不要去给这个数 +1** —— 自然结束的那条路上它是准的，
#       加一反而会把它弄错；而且产物自己写着真实历元数，需要精确时该去读产物。
_EPOCH = _p(r"Processed epoch\s*#")

# 结束标志
_FINISHED = _p(r"PEA finished")


def diagnose(output: str) -> Diagnosis:
    """从 pea 的完整输出里读出发生了什么。

    ⚠️ 纯函数 —— 不跑进程、不读文件。
       这样能用真实的输出片段做测试（见 scripts/check_diagnostics.py）。

    ⚠️ 去重：同一个问题可能被打印很多次（每个历元一次），
       但用户只需要知道"有这个问题"，不需要看 2880 遍。
       按 (kind, explain) 去重。
    """
    d = Diagnosis()
    seen: set[tuple[str, str]] = set()

    # ⚠️ **先去色再逐行匹配。** 见上面 `strip_ansi` 的说明 ——
    #    漏掉这一步的后果是"227 条警告一条都看不到"。
    for line in strip_ansi(output).splitlines():
        line = line.strip()
        if not line:
            continue

        # ---- 版本 ----
        m = _VERSION.search(line)
        if m and not d.version:
            d.version = m.group("ver").strip()

        # ---- 进度计数 ----
        if _EPOCH.search(line):
            d.epochs_processed += 1

        # ---- 结束标志 ----
        if _FINISHED.search(line):
            d.finished = True

        # ---- 问题 ----
        for rule in RULES:
            m = rule.pattern.search(line)
            if not m:
                continue

            key = (rule.kind, rule.explain)
            if key in seen:
                break          # 已经报过了，不重复
            seen.add(key)

            # ⚠️ 有些规则带命名组，把具体信息填进解释里 ——
            #    "缺少必需的文件" 远不如 "缺少天线文件 igs20.atx" 有用
            explain = rule.explain
            fix = rule.fix
            groups = m.groupdict()
            if groups.get("what") and groups.get("path"):
                explain = f"缺少文件：{groups['what'].strip()}（{groups['path']}）"
            elif groups.get("what"):
                explain = f"Ginan 提示：{groups['what'].strip()}"

            d.diagnostics.append(
                Diagnostic(
                    severity=rule.severity,
                    kind=rule.kind,
                    raw=line,
                    what=explain,
                    what_to_do=fix,
                )
            )
            break              # 一行只匹配第一条规则

    return d


# =============================================================================
# 单调有用的助手
# =============================================================================


def short_version(full: str) -> str:
    """把 `HEAD v4.1.4-5033996 from 2026-09-16 ...` 压成 `v4.1.4`。

    ⚠️ 版本号要显示给用户看，但那一长串里只有 `v4.1.4` 对他有意义。
       完整的字符串在排查问题时才有用 —— 放在工具提示或者详情里。
    """
    m = re.search(r"(v\d+\.\d+\.\d+)", full or "")
    return m.group(1) if m else (full or "")
