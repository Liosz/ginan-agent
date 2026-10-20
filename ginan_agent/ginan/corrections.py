"""Ginan 领域层 —— 「改正项」：算哪些物理改正，**逐项**由用户定。

============================ 这一层解决什么 ============================
在这之前，改正项是**打包在两个档位里**的（`config.PROFILES`）：

    基础（默认）= 只有固体潮
    完整        = 9 项全开

档位本身没错 —— 错的是它**只给两个极端**：

  · 想要海潮负荷、但不想要那 23 MB 的海洋极潮系数？**做不到。**
  · 想知道"我这一项到底影响多少"？**界面上看不见这一项存在。**

而每一项都不是"可忽略的小量"：实测同一份数据，光是把潮汐改正全关掉，
解就差了**米级**（见 `PROFILES` 里那段）。**用户有权知道、也有权选。**

⚠️ 但"全部暴露"同样不行：Ginan 有 3000+ 个配置项，
全暴露 = 没暴露（见 `config.Profile` 的说明）。
所以这里**只收 9 项** —— 收的标准是三条，缺一不可：

    ① 它是**物理改正**（不是求解策略、不是输出格式）
    ② 它对解的影响**在厘米级以上**（或者我们量过它有多大）
    ③ 它**要么不要文件、要么那个文件我们能拿到**

============================ ⚠️⚠️ 这一份必须有实测数字 ============================
本项目的规矩：**没量过就写"没量过"**，不许拿文献值冒充实测值。

每一项的 `effect` 字段就是那行"开/关它到底差多少"。
数字来自 `ginan-testdata-202506/corr_exp/run_corr.py`：
同一份观测（ALIC 2026-09-02，2880 历元）、同一套配置（「完整」档、9 项全开）、
**只有这一个开关不同**的两次解相比。量出来的三维位移：

    固体潮        8.33 厘米   ← 唯一的大项，几乎全在高程（+8.25）
    固体极潮      0.92
    二阶电离层    0.26
    海洋极潮      0.08
    海潮负荷      0.07        ← 这个站的海潮负荷本来就小（径向 M2 只有 2.5 毫米）
    大气潮负荷    0.03
    三阶电离层    0.0006
    日月/行星历表 0.0001
    海潮位势      0.0000      ← 去掉它，解逐位相同

⚠️⚠️ **这张表是"一个站、一天"的**，不要当普适结论用：
   · 海潮负荷在开阔海域的沿海站可以到几厘米（本站恰好在海湾里）
   · 电离层那两项和太阳活动、纬度有关
   · "解移动多少"衡量的是**这一项算不算得出来**，不是"准了多少"——

   判准不准要权威坐标，而程序自己不知道哪个更准（见 `posfile.verdict`）。
   所以文案一律写"解移动了 X 厘米"，**绝不能**写成"精度提高了 X 厘米"。

⚠️ 一个副产品：两个"配置完全相同"的变体（去掉文件那两次其实没删掉）
   跑出来的位移是 **0.0000 厘米** —— 说明这条流水线本身是确定性的，
   几毫米量级的差不是噪声，是真的效应。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Correction:
    """一个改正项。"""

    key: str
    label: str                      # 界面上那一行（人话，不带术语）
    what: str                       # 一句话说清它是什么
    # 写进 `receiver_options.global.models` 的哪个位置。None = 没有开关，
    # 只靠"文件在不在"来开关（Ginan 就是这么设计的，不是我们偷懒）。
    model_path: tuple[str, ...] | None
    # 需要哪些模型文件（`catalog.TABLES` 里的文件名）。空 = 不要文件。
    files: tuple[str, ...]
    # 各档位下的默认值。键是 `config.PROFILES` 的档位名。
    on_by_profile: Mapping[str, bool]
    # 实测：开和关差多少。**必须是量出来的**，不是文献值。
    effect: str
    measured: bool = False          # 量过没有（界面据此措辞）


# =============================================================================
# 9 项
# =============================================================================
#
# ⚠️ 顺序 = 界面上的顺序，按"影响从大到小 + 要不要文件"排：
#    不要文件的排前面（用户不需要为一个下载做决定）。
CORRECTIONS: tuple[Correction, ...] = (
    Correction(
        key="solid",
        label="固体潮",
        what="地球的固体部分被日月拉住、每天涨落两次（幅度几十厘米）。"
             "这是最大的一项，关掉它是错的。",
        model_path=("tides", "solid"),
        files=(),
        on_by_profile={"reduced": True, "standard": True},
        measured=True,
        effect="实测（ALIC 2026-09-02 整天，关掉它再跑一遍）："
               "解移动 8.33 厘米，几乎全在高程（+8.25）；"
               "重复性 2.94 → 3.53 厘米（关掉更差）",
    ),
    Correction(
        key="solid_pole",
        label="固体极潮",
        what="地球自转轴在动，把固体地球也带着形变一点。",
        model_path=("tides", "spole"),
        files=(),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解移动 0.92 厘米（主要在高程 +0.89）；重复性没变",
    ),
    Correction(
        key="ocean_load",
        label="海潮负荷",
        what="涨潮时海水压在地壳上，把测站压下去一点 —— 沿海测站最明显。",
        model_path=("tides", "otl"),
        files=("OLOAD_GO.BLQ",),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解只移动 0.07 厘米 —— 这个站的海潮负荷本来就小"
               "（表里径向 M2 振幅 2.5 毫米）。开阔海域的沿海站会大得多",
    ),
    Correction(
        key="atmos_load",
        label="大气潮负荷",
        what="大气压的周期变化（半日、全日）压在地壳上引起的形变。",
        model_path=("tides", "atl"),
        files=("ALOAD_GO.BLQ",),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解移动 0.03 厘米（这个站、这天，看不出来）",
    ),
    Correction(
        key="ocean_pole",
        label="海洋极潮",
        what="自转轴摆动时海水的质量重新分布，再压回地壳。",
        model_path=("tides", "opole"),
        files=("opoleloadcoefcmcor.txt",),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解移动 0.08 厘米",
    ),
    Correction(
        key="ocean_potential",
        label="海潮位势",
        what="海潮的球谐系数 —— 算固体潮时，海水自己那部分引力也要算进去。",
        model_path=None,                      # 只有文件，没有开关
        files=("fes2014b_Cnm-Snm.dat",),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解一点没动（四位小数上完全相同）—— 去掉这个 11 MB "
               "的文件，结果不变",
    ),
    Correction(
        key="planetary_ephemeris",
        label="日月/行星位置（JPL 历表）",
        what="用 JPL 的历表算日月位置。不给的话 Ginan 退回它自带的解析公式 —— "
             "固体潮、相对论都要用日月位置。",
        model_path=None,                      # 只有文件，没有开关
        files=("DE436.1950.2050",),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解移动 0.0001 厘米（看不出来）—— Ginan 退回它自带的"
               "解析星历，这份数据上够用",
    ),
    Correction(
        key="iono2",
        label="二阶电离层",
        what="信号穿过电离层的二阶项（和一阶比小得多，但太阳活动高年能到厘米级）。",
        model_path=("ionospheric_components", "use_2nd_order"),
        files=("igrf14coeffs.txt",),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解移动 0.26 厘米（主要在高程 −0.26）；重复性 2.94 → 2.95 厘米",
    ),
    Correction(
        key="iono3",
        label="三阶电离层",
        what="同上，第三阶。量级更小。",
        model_path=("ionospheric_components", "use_3rd_order"),
        files=("igrf14coeffs.txt",),
        on_by_profile={"reduced": False, "standard": True},
        measured=True,
        effect="实测：解移动 0.0006 厘米（看不出来）",
    ),
)

BY_KEY: dict[str, Correction] = {c.key: c for c in CORRECTIONS}


def resolve(profile_key: str, overrides: Mapping[str, bool] | None = None
            ) -> dict[str, bool]:
    """算出**这一跑**每一项到底开不开。

    ⚠️ `overrides` 是**稀疏**的：只有用户明确改过的那几项在里面。
       这样"档位"仍然是有效的一键预设 —— 用户在界面上改一项，
       不会把其他 8 项一起冻住（那会让下次换档位失效，而且没人看得出来）。

    ⚠️ 不认识的 key **忽略**，不报错：用户保存的设置可能来自旧版本，
       而"多了一个键"不该让配置生成失败。
    """
    out = {c.key: bool(c.on_by_profile.get(profile_key, False))
           for c in CORRECTIONS}
    for key, value in (overrides or {}).items():
        if key in out:
            out[key] = bool(value)
    return out


def clean(overrides: Mapping | None) -> dict[str, bool]:
    """洗一遍用户存下来的覆盖值。

    ⚠️ 只留认识的键、值必须是 bool —— **不认识就丢，不报错**。
       这份数据来自磁盘上的 `config.json`（可能是旧版本写的），
       也可能来自浏览器。为它让配置生成失败是不划算的：
       丢一个不认识的键，等于那一项按档位走，无害。

    ⚠️ 但**值必须是真 bool**：`"false"` 这种字符串在 Python 里是真值，
       直接 `bool()` 会变成"开" —— 而用户明明关掉了。
    """
    out: dict[str, bool] = {}
    for key, value in (overrides or {}).items():
        if key in BY_KEY and isinstance(value, bool):
            out[str(key)] = value
    return out


def files_for(enabled: Mapping[str, bool]) -> set[str]:
    """这些开关要用到哪些模型文件。"""
    out: set[str] = set()
    for c in CORRECTIONS:
        if enabled.get(c.key):
            out.update(c.files)
    return out


def describe(enabled: Mapping[str, bool]) -> list[dict]:
    """给界面/模型看的一份清单（**已经算好每项开不开**）。"""
    return [{
        "key": c.key,
        "label": c.label,
        "what": c.what,
        "effect": c.effect,
        "measured": c.measured,
        "files": list(c.files),
        "on": bool(enabled.get(c.key)),
    } for c in CORRECTIONS]


def summary_line(enabled: Mapping[str, bool]) -> str:
    """一行话说清这一跑开了哪些 —— 给日志/复现包用。"""
    on = [c.label for c in CORRECTIONS if enabled.get(c.key)]
    off = [c.label for c in CORRECTIONS if not enabled.get(c.key)]
    return f"开：{'、'.join(on) or '（无）'}；关：{'、'.join(off) or '（无）'}"
