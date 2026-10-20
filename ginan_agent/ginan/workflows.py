"""工作流表 —— Ginan 能做的每一种"事"，以及各自要什么。

============================ 为什么要有这个文件 ============================
原来 app 只有一条路：生成**一种形状**的配置（输入精密产品、输出测站位置）。

要接 Ginan 的其它能力时，最直接的做法是在 `build_config` 里加分支：

    if workflow == "pod":    ...
    elif workflow == "slr":  ...
    elif ...

**不要那样做。** 17 个分支意味着"加第 18 个"要改 6 个文件，
而这个仓库已经因为「同一件事有两个真相」踩过好几次坑。

============================ 关键洞察 ============================
逐个读完 Ginan 官方 18 个示例配置之后，它们都是**同一个形状**：

    工作流 = (开哪些 process_modes, 要哪些输入, 出哪些产物, 额外选项)

举例（这一条是这张表存在的全部理由）：

    ppp_example.yaml            slr_pod_with_pseudoobs_lag.yaml
    ────────────────            ───────────────────────────────
    modes: preprocessor/spp/ppp modes: slr
    inputs: obs + sp3/clk/...   inputs: crd_files + sp3 + egm + ...
    outputs: pos                outputs: orbit + ...

两者看着差很远，拆开就是同一个四元组。**所以这是一张表 + 一个分发器。**

============================ ⚠️ 一条设计约束 ============================
**用户不该看到"工作流"这个词。**

用户说的是"帮我定个轨""看看这个站的坐标"，不是"跑 POD 模式"。
工作流是**实现层**的概念 —— 模型负责把意图翻译过来（见 `agent/prompts.py`）。

⚠️ 而且**不要让模型自由填 `process_modes`。**
   那是一组会互相冲突的开关（`slr: true` 时输入完全不同），
   让模型自由组合迟早会出现"看起来配好了、实际跑不了"的组合。
   给模型的是**枚举好的工作流**；每个工作流内部那组开关是**定死的**。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Workflow:
    """Ginan 能做的一种"事"。

    ⚠️ 这是**纯数据**，不含任何逻辑。
       "怎么把这张表变成一份配置"在 `config.py`；
       "要下哪些文件"在 `catalog.py`。
       本文件只回答"有哪些工作流、各自要什么"。
    """

    key: str
    """机器用的标识，会进 `ConfigInputs.workflow`。"""

    label: str
    """给人看的短名字（界面上显示）。"""

    purpose: str
    """一句话用途，写给完全不懂的人看 —— 和 `catalog.KINDS` 同一个原则。"""

    modes: dict[str, bool]
    """`process_modes` 的**完整**值。

    ⚠️ 是完整值，不是"要改的那几个"。
       合并式的写法（只写 `{"slr": True}` 然后去覆盖默认值）会让
       "默认是什么"变成第二处真相 —— 而 `slr: true` 时其它开关该开还是该关，
       是**每个工作流各自的事**，不该有个共享的默认。
    """

    outputs: tuple[str, ...]
    """这个工作流要 Ginan 产出哪些东西（对应 `outputs` 下的键）。

    ⚠️ 报告给用户时要说清"能拿到什么" —— 用户问"能不能定轨"，
       真正想知道的是"我会不会拿到一个轨道文件"。
    """

    needs: tuple[str, ...] = ()
    """要哪些**文件类别**（`catalog.KINDS` 里的键 + 本模块新增的）。

    **用途（现在是真的）**：`catalog.plan_run` 在最后按这个筛掉不要的类别。

    ⚠️⚠️ **这一栏曾经是死字段，值得记下来**（2026-09-24）：

        原文写的是「用途：`plan_run` 按这个决定要下什么、`advise` 按这个说
        还缺什么」—— 而**全项目没有一处读它**。一边声称在用、一边没人用，
        比"没有这个字段"更糟：改它的人会以为改了就生效。

        （实测：把 `"bia"` 从 `pod.needs` 里去掉，**一点效果都没有**。）

        ⚠️ 更糟的是**让它生效的那一刻，立刻发现了两个洞**：
           · `pod.needs` 里**没写 `sp3`** —— POD 的先验轨道文件压根不会去下
           · `ppp.needs` 里写了 `bia` 而 `pod.needs` 也有，但没人知道哪个对

        那个 `sp3` 的洞在它是死字段的时候**一直存在**，谁都不知道：
        改它没效果，所以没人看它，也就没人发现它写错了。

    ⚠️ 现在的判据（守卫 `test_workflow_needs_covers_what_the_config_uses`）：
       **配置里真正用到的每一类文件，都必须在 needs 里** ——
       以及反过来，needs 里不许有配置一个字节都不用的类别（白下）。
    """

    extra_inputs: tuple[str, ...] = ()
    """这个工作流特有的、不上面的类别的输入键名（如 `egm_files`）。

    单列出来是为了**在报告里能说清"缺的是这个工作流特有的东西"** ——
    比如 POD 缺 `EGM2008.gfc`，而 PPP 根本不需要它。
    """

    epoch_interval: float | None = None
    """这个工作流**默认**的解算历元间隔（秒）。`None` = 用 `ConfigInputs` 里的值。

    ⚠️⚠️ 这一条是 POD 跑出发散才发现的。

        app 的默认是 **30 秒**（PPP 用 30 秒采样，天经地义）。
        但官方 `pod_example.yaml` 写的是 **300 秒** —— 而实测用 30 秒会怎样：

            · 2880 个历元 × 68 颗卫星 × (4 个轨道参数 + 经验加速度)
            · 法方程规模爆炸，而且**相邻历元的轨道参数几乎线性相关**
              （卫星 30 秒才走 200 米，不是独立的观测量）
            · 结果：`Failed to solve normal equation` + 满屏 NaN
            · 退出码 `0xC0000005`（段错误）

        ⚠️ 30 秒和 300 秒的差别**不在"精度"上，在"这个方程解不解得出来"上。
           所以它不是个调优参数，是**这个工作流能不能跑的前提**。

    ⚠️ 调用方仍可显式覆盖（`ConfigInputs.epoch_interval`）。
    """

    run_timeout: float | None = None
    """跑这个工作流允许的最长秒数。`None` = 用 `runner.DEFAULT_TIMEOUT`。

    ⚠️⚠️ **这一栏是实测撞出来的，不是想出来的**（2026-09-25）。

        `runner.DEFAULT_TIMEOUT` 是 **3600 秒**，它自己的文档写着用途是
        「**防止无限挂起**（比如那个 OpenBLAS 的坑），
        不是用来限制正常处理的」。

        ⇒ 可实测的一整天 POD：**46 站 16.7 秒/历元 × 288 历元 = 80 分钟**
          （2025-06-15 那批，逐历元采样，稳定无阶跃）。
          **80 分钟 > 60 分钟** —— 于是那轮跑到约 210/288 历元时
          **被 app 自己杀掉**，用户看到的是"跑一半没了"。

        👉 按 `DEFAULT_TIMEOUT` 自己的定义，这就是 bug：
           **它挡住的是一轮完全正常的处理。** 所以 POD 要有自己的值。

        ⚠️ 2026-09-27 重测（修线程之后）：**同一份 46 站配置 = 39.7 分钟**
          （每历元【处理】中位数 3.99 秒）。**那一轮现在能塞进 60 分钟了** ——
          但**这不改变结论**：更慢的站网仍然超过。
          ⇒ 这一栏留着的理由从"46 站会被杀"变成"**更慢的站网会被杀**"。

    ⚠️⚠️ **2026-09-27：从 8 小时改成 16 小时。但先说清楚 —— 8 小时本来是够的。**

        87 站（`official/ctor87.yaml`，官方 2019-07-18）逐历元实测，
        **跑到第 247 个历元**（288 里的 86%，被会话结束杀掉，不是跑崩）：

            第 1~4 历元      平均 27.3 秒
            第 5~27 历元     平均 84.9 秒（**中位 101.3 秒**）← 持续 23 个历元的平台
            第 28~120 历元   平均 38.3 秒
            第 121~200 历元  平均 40.3 秒
            第 201~247 历元  平均 44.2 秒（在慢慢涨）

        ⇒ **实测前向 3.03 小时 + 外推剩下 41 个历元 ≈ 3.5 小时。**
          加反向平滑（46 站那轮约为前向的 35%）≈ **4.5 小时** —— **8 小时够。**

        👉 **那为什么还是放到 16 小时？** 因为**这一栏不知道用户选了几个站**：
           87 站是本项目**实测过的最大站网，不是能选的最大**。
           耗时和占用都随站数涨（磁盘那边是 1.9 倍站数 → 3.3 倍占用，**超线性**），
           一个 150 站的站网按同样形状外推就越过 8 小时了。
           按这一栏自己的原则——**宁可大了白等（用户随时能取消），
           也不能小了把几小时的活杀掉**。

        ⚠️⚠️ **这一栏我改过两版，第一版是错的，记下来免得重犯**：
           · **第一版**只看了第 5~14 历元，把 105 秒当成稳态，
             写下"前向要 8.6 小时" —— **错**。再往下看，第 28 历元起掉到 39 秒。
           · ⇒ **采样窗口里的"稳态"不一定代表一整天。**
             判据要用**全程观测到的最坏速率**，不是窗口里看着最顺眼的那个。

        ⚠️ 完整的表（含修复前那几行）见 `docs/ginan-facts.md` 七点五 ③。

    ⚠️ **它不解决"POD 太慢"这个问题** —— 只解决"慢的活得让它跑完"。
       87 站一轮**几个小时**（头 27 个历元尤其慢，每个 ~85 秒），
       那依然是 POD 能不能给用户用的真障碍。
    """

    disk_need_gb: float = 2.0
    """跑这个工作流**要留多少磁盘**（GB）。默认 2 GB。

    ⚠️⚠️ **这一栏也是实测撞出来的**（2026-09-25）。

        原来的判据是 `environment._check_disk` 里写死的 **2 GB** ——
        那条对 PPP 是够的（几百 MB 是常态），**对 POD 远远不够**：

            2026-06-15 那批 46 站实测：RTS 状态文件 **51 MB/历元**
            ⇒ 288 历元 ≈ **15 GB**（其中 15 GB 全是 RTS，真正的产物只有 4.6 MB）

        ⚠️⚠️ **而"87 站约 18 GB"那一句是错的**（2026-09-27 更正）——
           它来自一轮**只跑了 7 个历元**的尝试，**不是一整天的实测**。
           87 站按历元实测（`ctor87.yaml`，两个独立窗口）：

               124 MB/历元（第 4~16 历元） / 132 MB/历元（第 16~22 历元）
               ⇒ 288 历元光**前向**就 **约 37 GB**，再加反向平滑 ≈ **50 GB**

        ⇒ 所以"取 25 GB"对 87 站**是拦不住的**：剩 30 GB 的用户会通过检查，
          然后在跑了几个小时之后磁盘满。

    ⚠️ 判据在 `do_run_processing`（那里才知道工作流），不在这儿。

    ⚠️ **这一栏是"整个工作流最坏情况"的一个常数，它不知道用户选了几个站。**
       而实测的占用**随站数长**（46 站 → 288 历元约 15 GB；87 站 → 约 50 GB），
       而且不是线性的（站数 1.9 倍，占用约 3.3 倍）。
       ⇒ 取 **64 GB** 是按**已实测的最大站网**留的余量。
         代价要说清：**磁盘不够的用户会被整个 POD 挡在门外**，
         哪怕他只想跑一个小站网 —— 那时候的正确出路是
         `max_epochs` **试跑**（试跑不查磁盘，见 `do_run_processing`）。
    """

    estimates: tuple[str, ...] = ()
    """除了默认要估的那些之外，**还要估什么**。

    ⚠️⚠️ 这一栏是 POD 第一次跑失败才发现的，值得说清。

        `estimation_parameters` 里 Ginan 默认只估**接收机**的那些参数
        （位置/钟差/模糊度/对流层）。**卫星的轨道不是默认要估的** ——
        要让 Ginan 去估它，必须显式写：

            estimation_parameters:
              satellites:
                global:
                  orbit: {estimated: [true], ...}

        不写会怎样：配置**看不出错**，`pea` **exit 0**，
        跑出一堆 POS —— 只是**没有轨道**。而"少了一个产物"
        不会让任何人立刻发现（第一次实测就是这样：headline 说
        "没有找到可用的观测数据"，而真相是轨道压根没进滤波器）。

    ⚠️ 而卫星钟差**不能无脑一起估**：PPP 里它是**输入**（CLK 文件），
       估它就等于把已知的东西再解一遍 —— 那是另一件事了。
       所以这一栏是"要估哪些"，不是"把卫星相关的一起打开"。

    ==================== ⚠️⚠️ 它**曾经是个死字段**（2026-09-27 实测） ====================
    把 `pod.estimates` 从 `("orbit",)` 改成 `()`，**产物一个字节都没变**。

    根因：读它的三个函数（`_receiver_estimation` / `_satellite_options` /
    `_satellite_estimation`）**只被 `_build_handwritten` 调用** ——
    而所有走模板的工作流（`example` 不是 None）都不经过那里。
    ⇒ 这一栏对 `pod` / `fit_sp3_pseudoobs` **从来没被读过**。

    ⚠️ 和 `Workflow.needs` 当初一模一样。那个的处置是"让它真的生效"；
       这一个**没有可生效的东西** —— 模板里那些估计参数**本来就写好了**，
       再"写一遍"只是把同样的值抄第二处（而两处迟早会漂）。

    ⇒ 处置：把它变成**一句能被核对的话** —— 声明了 `orbit` 的工作流，
      产出的配置里**就得真的在估卫星轨道**。
      守卫 `test_estimates_declaration_matches_the_config` 两向都核。
      ⚠️ 这样它的用途不再是"写配置"，而是"**在换模板/改开关时，
         如果有人把估轨道那部分弄丢了，会被抓住**" ——
         而那种丢失别处看不出来：产物照样有（一堆 POS），只是没有了轨道。
    """

    example: str | None = None
    """这份工作流的配置**从哪个官方示例来**。`None` = 手写构造。

    ⚠️⚠️ **这一栏是"配置从哪来"的唯一判据**（见 `config.build_config`）。

        None       → `config._build_handwritten`：逐块手拼
        文件名      → `config._build_from_example`：载入官方示例、只改文件路径

    ⚠️ **为什么会有两种。** 手写那条是 app 最早的做法，照着 PPP 拼的 ——
       对 PPP 能跑，**对其他工作流是残的**。实测（2026-09-24）：

           手写 POD 配置    → 第 0 个历元 `dpotrf failed`，82 秒/历元，NaN 级联
           官方 pod_example → 0 次失败，2.7 秒/历元，产物齐全

       **同一份数据、同一个二进制，只有配置不同。**

       👉 手写 200 个键，错一个的样子是「配置看着正常、跑起来是 NaN」，
          而这种错**读代码发现不了**。

    ⚠️ **新工作流请填这一栏。** 填了就走模板，不填就走手写 ——
       而手写那条对非 PPP 的工作流是没验过的。

    ⚠️ 文件名必须是 `examples.EXAMPLE_SHA256` 里有的
       （守卫 `test_workflow_declares_its_config_source` 会核）。
    """

    profiles: tuple[str, ...] = ("reduced", "standard")
    """这份工作流**允许**用哪些档位。

    ⚠️ 默认两个都允许。**收窄它是有代价的** —— 用户想要的档位会被拒，
       所以只在"那个档位会静默算错"时才收窄。

    ⚠️⚠️ POD 就是那种情况，只留 `standard`。理由不是"精度"，
       是**静默的错误结果**：

           模板的 `orbit_propagation.egm_field: true` 要 `inputs.egm_files`
           和 `planetary_ephemeris_files`，而「基础」档正是把这些删掉的。

           读源码（`orbitProp.cpp:71-77`）：`egm_field` 开着但没有 EGM 文件时，
           Ginan **只打印一句 `EGM data not initialised`，跳过最大的那一项受力**，
           轨道**照常输出**。

       ⇒ 「基础」档的 POD 会**不出声地算出一条错的轨道**。
         没有实测过那条路，所以**拒绝**，不静默降级。
    """

    dataset: str | None = None
    """这份工作流跑的是**一份固定的演示数据集**，不是"用户选的某站某天"。

    ⚠️⚠️ **这是第一个不接用户输入的维度**，所以要说清它为什么存在。

        前面每一条工作流都是「某站某天 → 去 IGS/GA/武大那些源拿」。
        而 **SBAS 的改正信息（`.ems`）没有任何公开的日常源** ——
        它由 SBAS 运营方从地球同步卫星播发，播完就没了，**不归档**。

        ⇒ 能拿到的只有**别人录下来的一段**。Ginan 官方录了
          2025-11-11（DOY 315）00:00–06:45、四个测站，放在自己的桶里。

        ⇒ 所以 SBAS 只能是「**跑一次官方演示**」，不是「处理你自己的数据」。
          这不是偷懒，是数据的物理性质（和 `rt_ppp` 那条"活流不归档"同一类，
          只是那一条连录像都没有）。

    ⚠️ 填了这一栏，`catalog.plan_run` 会**忽略**调用方给的 `stations` 和 `day`
       —— 数据集自己带（见那里的说明）。
       **因此工作流的回执必须把这件事说出来** —— 用户说"给 ALIC 跑 SBAS"
       时，不能默默换成 HOB2 却不告诉他。

    ⚠️ 取值必须是 `catalog.DATASETS` 里有的键。守卫会核。
    """

    note: str = ""
    """有一条要说的话。空着表示没有。"""


# =============================================================================
# 表
# =============================================================================
#
# ⚠️ 每一条都对着官方示例核过，出处写在 `source` 注释里。
#    **不要凭印象加条目** —— 这个仓库里"凭印象"已经错过好几次了。

WORKFLOWS: dict[str, Workflow] = {
    # ---------------------------------------------------------------- PPP
    #
    # 出处：`ppp_example.yaml`
    #   process_modes: preprocessor/spp/ppp = true, ionosphere/slr = false
    #   inputs: atx + erp + snx + satellite_data(nav/clk/sp3/bsx) + gnss_observations
    #   outputs: pos
    "ppp": Workflow(
        key="ppp",
        label="PPp 静态定位",
        purpose=(
            "算**一个测站在哪里**。给它这个站收到的卫星信号，"
            "加上卫星的精确轨道和钟差，它把这个站的三维坐标解出来。"
            "精度能到厘米级甚至毫米级。"
        ),
        modes={
            "preprocessor": True,
            "spp": True,
            "ppp": True,
            "ionosphere": False,
            "slr": False,
            "sbas": False,
        },
        outputs=("pos",),
        needs=("obs", "sp3", "clk", "nav", "atx", "erp", "bia", "snx"),
        # ⚠️ **显式写成 None，不是省略。**
        #    省略和写 None 效果一样，但写出来才看得见 ——
        #    "PPP 走的还是手写那条路"是这一轮刻意保留的状态，
        #    它应该在表里，而不是靠"这个字段没写"来暗示。
        #
        #    `ppp_example.yaml` **也打包了**（`examples.py` 里有），
        #    但这里为 None 表示**没有走它**。将来迁到模板时改这一行即可。
        example=None,
    ),

    # ---------------------------------------------------------------- POD
    #
    # 出处：`pod_example.yaml`
    #   process_modes: preprocessor/spp/ppp = true（**和 PPP 同一组**）
    #
    #   ⚠️⚠️ 它和 PPP 的区别**不在模式开关上**，而在两处：
    #
    #     ① 输入：先验轨道放 `pseudo_observations.sp3_inputs`，
    #        不是 `satellite_data.sp3_files`。
    #        前者是"当成观测值来用"，后者是"当成已知的卫星位置"——
    #        **这正是"被估"和"已知"的分界线**，
    #        而 POD 的全部意义就是**估轨道**。放错地方 = 又退化成 PPP。
    #
    #     ② 产物：`outputs.sp3`（估出来的轨道）+ `outputs.sinex`（测站坐标）。
    #
    #   加 `egm_files`（重力场）是因为**估轨道要算卫星受力** ——
    #   地球重力场是最大的那一项。PPP 不需要它（轨道已知）。
    "pod": Workflow(
        key="pod",
        label="POD 卫星定轨",
        purpose=(
            "算**卫星在哪里**。PPP 是拿现成的卫星轨道去算测站，"
            "POD 反过来 —— 拿一堆测站的观测一起反推卫星的轨道。"
            "这是「精密」两个字真正的来源：IGS 那些 SP3 文件就是这么来的。"
        ),
        modes={
            "preprocessor": True,
            "spp": True,
            "ppp": True,
            "ionosphere": False,
            "slr": False,
            "sbas": False,
        },
        outputs=("pos", "sp3", "sinex"),
        # ⚠️ 配置从官方示例来 —— 见 `Workflow.example` 的说明。
        #    手写那份缺了 `minimum_constraints`、`orbit_propagation` 等整块骨架，
        #    实测第 0 个历元就 `dpotrf failed`。
        example="pod_example.yaml",
        # ⚠️ 只留「完整」档 —— 理由不是精度，是**「基础」档会静默算错**。
        #    见 `Workflow.profiles` 的说明。
        profiles=("standard",),
        # ⚠️ 这一条是"POD 之所以是 POD"的那个开关 —— 见 `estimates` 的说明。
        estimates=("orbit",),
        # ⚠️ 300 秒 —— **不是调优，是前提**。见 `epoch_interval` 的说明。
        #    出处：官方 `pod_example.yaml` 的 `epoch_control.epoch_interval: 300`。
        epoch_interval=300.0,
        # ⚠️ 16 小时 —— 通用默认是 60 分钟，会**在正常处理的中途把它杀掉**
        #    （46 站一整天实测 80 分钟，修线程前）。**取值依据见 `run_timeout`
        #    的说明**：87 站实测前向 3.5 小时（**8 小时其实够**），
        #    但这一栏不知道用户选了几个站 —— 按"往大了取"留余量。
        run_timeout=57600.0,
        # ⚠️ 64 GB —— 46 站一天实测 ≈ 15 GB，**87 站实测 ≈ 50 GB**。
        #    原来写的 25 GB 是按「87 站 ≈ 18 GB」定的，而那个数来自
        #    一轮**只跑了 7 个历元**的尝试，不是一整天的实测。
        #    取值依据见 `disk_need_gb` 的说明。
        disk_need_gb=64.0,
        # ⚠️⚠️ **这里没有 `bia`，PPP 那边有** —— 不是漏了。
        #
        #    官方 `pod_example.yaml` 里**没有 `bsx_files` 这个键**：
        #    它的码偏差是**估**出来的（`models.code_bias.enable: true` +
        #    `estimation_parameters.satellites.global.code_bias.estimated: [true]`），
        #    不是读偏差文件。
        #
        #    ⇒ 下 BIA 文件下来会**一个字节都用不到**，而"下了没用上"
        #      正是这个仓库反复记着的那种静默浪费（下到 45 MB tables
        #      而配置里没写 → 跑出来和没下一样）。
        #
        #    ⚠️ 改这一条要有依据：如果哪天实测发现 POD 确实需要偏差文件
        #      （比如某个改正项的精度明显变差），那是**另一个发现**，
        #      要连 `_substitutions` 里加上 `bsx_files` 一起改。
        # ⚠️⚠️ **`sp3` 在这里，而且很容易漏 —— 实测漏过一次。**
        #
        #     POD 的 SP3 不走 `satellite_data.sp3_files`（那是"已知的卫星位置"），
        #     而是走 `pseudo_observations.sp3_inputs`（"**当成观测值**，参与估计"）。
        #     换个位置而已，**文件是同一类文件**。
        #
        #     漏掉它的后果：取数计划里没有 SP3 → 盘上没有那个文件 →
        #     配置里那一项为空 → Ginan 找不到先验轨道。
        #
        #     ⚠️ 原来自从 `needs` 是死字段起，`sp3` 就**一直没写在这里**，
        #        而没有任何东西发现 —— 因为没人读它。
        #        2026-09-24 让它生效时，这个洞立刻露出来了。
        needs=("obs", "sp3", "clk", "nav", "atx", "erp", "snx"),
        extra_inputs=("egm_files", "planetary_ephemeris_files", "igrf_files",
                      "gpt2grid_files", "ocean_tide_loading_blq_files",
                      "atmos_tide_loading_blq_files",
                      "ocean_pole_tide_loading_files",
                      "ocean_tide_potential_files"),
        note=(
            "⚠️ 它要的是**全球分布**的测站网 —— "
            "轨道是整颗卫星的事，只有一圈站看得见它的一侧时，"
            "另一侧的轨道就没约束。"
        ),
    ),

    # --------------------------------------------------------------- brdc2sp3
    #
    # 出处：`brdc2sp3.yml`
    #   process_modes: **只写了 `preprocessor: false`** —— 别的全是 Ginan 的默认值
    #   inputs: atx + snx(+3 个 yaw 表) + satellite_data.nav_files
    #   outputs: sp3（`<CONFIG>-<YYYY><DDD><HH>-<SYS>.sp3`，`output_interval: 900`）
    #
    # ⚠️⚠️ **它是唯一一条完全不需要观测数据的工作流** —— 输入只有广播星历。
    #
    #     所以它踩到了 ④ 那一轮实测出来的那个**致命**问题：
    #     `epoch_control.end_epoch` 在这个版本里**根本不起作用**
    #     （三条对照实验见 `docs/ginan-facts.md` 七点十三）。
    #
    #     PPP/POD 之所以看着正常，是因为它们的观测文件到当天为止就没了
    #     （官方 POD 的日志写着 `Inputs finished at epoch #289`）。
    #     **`brdc2sp3` 没有观测 ⇒ 那个"恰好"不存在 ⇒ 它会跑到天荒地老。**
    #
    #     实测：`end_epoch` 写着当天 23:45，它跑到了 **2022 年**、
    #     **100,407 个历元**、产出 1036 个 SP3 共 **1.9 GB**，直到被手动杀掉。
    #
    #     ⇒ 它必须靠 `-n`（`max_epochs`）停。见 `ginan_tools.do_run_processing`
    #       里那段自动算 `-n` 的说明。
    "brdc2sp3": Workflow(
        key="brdc2sp3",
        label="广播星历转轨道",
        purpose=(
            "把**卫星自己播发**的轨道参数转成标准的 SP3 轨道文件。"
            "它不需要任何观测数据，几秒钟就出结果。"
            "精度是米级（广播星历本来就这个水平），"
            "用途是快速拿到一份能用的轨道、或者给别的程序当输入。"
        ),
        # ⚠️ `spp: True` **不是我们选的** —— 它是 Ginan 的默认值
        #    （`acsConfig.hpp:578-583`）。
        #
        #    模板只写了 `preprocessor: false`，其余留空。而 `Workflow.modes`
        #    必须是**完整值**（见那个字段的说明），所以这里要把它补全。
        #
        #    补全之后：preprocessor 关（不预处理观测，因为没有观测）、
        #    spp 开（没有任何测站，它什么也不做）、ppp 关（没有滤波器）。
        #    真正干活的**不是**这几个开关，而是"Ginan 读到广播星历之后
        #    会把卫星位置算出来，然后 `outputs.sp3` 把它们写出去"。
        #
        #    ⚠️ 写出去用哪个来源？看 `satellite_options` 的 `sp3` 输出源，
        #       默认是 `{KALMAN, PRECISE, BROADCAST}`（`acsConfig.hpp:325`）——
        #       前两个这里都没有，于是**落到 BROADCAST**，正好是我们要的。
        modes={
            "preprocessor": False,
            "spp": True,
            "ppp": False,
            "ionosphere": False,
            "slr": False,
            "sbas": False,
        },
        outputs=("sp3",),
        # ⚠️ 只要三样，**没有 `obs`**（这是它和 PPP/POD 最大的区别）、
        #    也没有 `sp3`/`clk`（它自己就是来产 SP3 的）、没有 `erp`/`bia`
        #    （模板里没这两个键，实测不写也跑得对）。
        needs=("nav", "atx", "snx"),
        example="brdc2sp3.yml",
        # ⚠️ 900 秒 —— 官方示例的值。广播星历本身就是 15 分钟一个历元的精度，
        #    再密也没有意义。
        epoch_interval=900.0,
        # ⚠️⚠️ **只开「基础」档**，理由和 POD 正好**相反**：
        #
        #     POD 拒 reduced，是因为那个档会让它**静默算出一条错的轨道**；
        #     brdc2sp3 拒 standard，是因为那个档会往配置里塞
        #     **10 个它一个字节都用不到的模型文件**
        #     （`config._merge_table_inputs`：潮汐负荷、行星历表、IGRF、gpt2 格网…）。
        #
        #     而 `do_generate_config` 会因此**拦下用户**：
        #     "「完整」档还要 10 个模型文件没下" —— 为一个根本不需要它们的
        #     工作流，让用户去下 17 MB。
        #
        #     ⇒ 这不是"精度档位"，是**"哪个档位对这件事成立"**。
        profiles=("reduced",),
        # ⚠️ `run_timeout` 留空 = 用通用默认 3600 秒。
        #
        #    ⚠️ 这是**量过之后**才留空的（2026-09-27，端到端跑 app 生成的配置）：
        #
        #        一天 / 96 个历元 / 全球广播星历   **6.6 秒**
        #
        #    通用默认的 3600 秒是它的 **545 倍** —— 够到不需要为它开一栏。
        #    （对照：POD 一整天实测 80 分钟 > 通用默认 60 分钟，所以那个
        #      才需要 `run_timeout`。）
        #
        #    ⚠️ 光有 `run_timeout` 兜底是不够的 —— 它只管"卡住了"，
        #       "跑多少" 归 `-n`（见 `do_run_processing` 里那段）。
        note=(
            "⚠️ 它的产物是**广播星历的精度**（米级），不是精密轨道。"
            "要厘米级的轨道得用 POD，那需要观测数据。"
        ),
    ),

    # ------------------------------------------------------- fit_sp3_pseudoobs
    #
    # 出处：`fit_sp3_pseudoobs.yaml`
    #   process_modes: **ppp = true，preprocessor / spp = false**
    #   inputs: sp3 + `pseudo_observations.sp3_inputs`（同一批文件，两处都要）
    #           + egm + 日月星历 + 海潮系数 + erp + atx + snx(含 3 张姿态表)
    #   outputs: sp3（拟合出来的轨道）+ orbit_ics（初值，YAML）
    #
    # ⚠️⚠️ **它和 POD 的区别在"观测从哪来"，不在"估不估轨道"。**
    #
    #     POD：56 个测站收到卫星信号 → 反推卫星轨道（**独立测定**）
    #     这条：**把 IGS 的 SP3 当成观测值** → 拟合一条动力学轨道上去
    #
    #     ⇒ 所以它的产物**本来就该和输入很接近**。实测（2026-09-27）：
    #
    #           1 小时（4 个公共历元）   **0.10 米**
    #           6 小时（24 个）           0.98 米
    #           一整天（96 个）           **1.18 米**
    #
    #       ⚠️ 这不是"精度"，是**拟合残差** —— 判据是"小"，不是"好"。
    #          它没到厘米级，是因为 app 把光压换成了 `cannonball`
    #          （官方那份用 `boxwing`，而 boxwing.yaml 在发行版里不存在，
    #           见 `config._apply_workflow_delta` ① 那条）。这是**申报过的偏离**。
    #
    # ⚠️ 它**一个观测文件都不要**（`preprocessor`/`spp` 都是 false）——
    #    所以它和 `brdc2sp3` 一样快，也**一样必须靠 `-n` 刹车**
    #    （`end_epoch` 是死的，见 ginan-facts 七点十三）。
    #    实测一天 288 个历元 **19.2 秒**。
    "fit_sp3_pseudoobs": Workflow(
        key="fit_sp3_pseudoobs",
        label="轨道拟合（SP3 → 动力学轨道）",
        purpose=(
            "拿一条现成的卫星轨道（比如 IGS 给的那份），"
            "**拟合**出一条完全符合物理规律的轨道来 —— 相当于把它"
            "「熨平」成一条能自己往前推演的轨道，并给出它的初始状态。"
            "⚠️ 它**不能提高精度** —— 出来的轨道和进去的那条几乎一样。"
        ),
        modes={
            "preprocessor": False,
            "spp": False,
            "ppp": True,
            "ionosphere": False,
            "slr": False,
            "sbas": False,
        },
        # ⚠️ `orbit_ics` 是"拟合出来的初始状态"，一份 YAML。
        #    它**不是**给用户看的产物，是给下一次跑处理当先验用的。
        outputs=("sp3", "orbit_ics"),
        # ⚠️ **没有 `obs`** —— 和 `brdc2sp3` 同一类。
        #    `sp3` 要下是因为它既是输入轨道、又是伪观测（两处引用同一批文件）。
        #    `snx` 那一路实际要到 4 个文件：卫星元数据 + 3 张偏航姿态表。
        needs=("sp3", "nav", "atx", "erp", "snx"),
        extra_inputs=("egm_files",),
        example="fit_sp3_pseudoobs.yaml",
        # ⚠️ **只留「完整」档**，理由和 POD 一样：
        #    它要算卫星受力 ⇒ 必须有重力场（EGM2008）和潮汐模型。
        #    缺重力场**不会报错** —— Ginan 只打一句 `EGM data not initialised`
        #    然后跳过最大的那一项受力，轨道照常输出。
        profiles=("standard",),
        # ⚠️ 和 POD 同一个开关：卫星轨道是**被估的对象**。
        #    见 `estimates` 的说明。
        estimates=("orbit",),
        # ⚠️ 300 秒 —— 官方示例的值，和 POD 同一个理由
        #    （30 秒会让相邻历元的轨道参数线性相关，法方程解不出来）。
        epoch_interval=300.0,
        # ⚠️ `run_timeout` 留空 = 用通用默认 3600 秒。
        #    和 `brdc2sp3` 一样是**量过之后**才留空的（2026-09-27）：
        #    一天 288 个历元 **19.2 秒** —— 通用默认是它的 187 倍。
        note=(
            "⚠️ 它是**拟合**，不是测定 —— 产物和输入那条轨道几乎一样，"
            "**不会更准**。要真正独立的轨道得用 POD（那需要观测数据）。"
        ),
    ),

    # ---------------------------------------------------------------- RTK
    #
    # 出处：`rtk_example.yaml`
    #   process_modes: **只写 `ppp: true`**，其余留默认
    #                  （`acsConfig.hpp:578-585` 的默认是 preprocessor/spp = true）
    #   inputs: **只要 观测 + SP3 + 卫星元数据**
    #           ⚠️ **没有** `clk_files` / `nav_files` / `atx_files` / `erp_files`
    #   outputs: 模板里**连 `pos` 都没有**（由 app 补，见 `config` 那一侧）
    #
    # ⚠️⚠️ **它是"相对定位"，要的是两个靠得近的测站，不是一个站。**
    #     站离得越远，电离层/对流层越不一样，差分就越不成立。
    #
    #     实测（2026-09-27）：手上的 55 个站里，**`YAR2` 和 `YARR` 是共址天线对**
    #     （澳洲 Yarragadee，相距 **3.85 米**）—— 不用额外下任何数据就能跑：
    #
    #         伪距（官方示例的默认）    YAR2 **0.80 米** / YARR 0.84 米
    #         把载波相位也打开          YAR2 **0.43 米** / YARR 0.57 米
    #
    #     ⚠️ 官方示例里写着 `gnss_general.phase_measurements.process: false` ——
    #        **默认只处理伪距**（那是 DGPS 的量级）。要真的用载波相位得显式打开。
    #
    # ⚠️ 它**不需要卫星钟差文件**（`clk_files`）—— 卫星钟差是**估**出来的
    #    （`estimation_parameters.satellites.global.clock.estimated: [true]`），
    #    双差本来就把卫星钟差消掉了。也不需要天线表（`pco`/`pcv` 都关着）。
    "rtk": Workflow(
        key="rtk",
        label="相对定位（两个近距站）",
        purpose=(
            "算**两个靠得很近的测站之间的相对位置**。两个站同时看同一批卫星，"
            "它们共有的误差（卫星钟差、电离层、对流层）一相减就基本没了 —— "
            "所以它比单站定位准得多，也快得多。"
            "⚠️ **站与站之间几公里以内才成立**，越远越不准。"
        ),
        modes={
            "preprocessor": True,
            "spp": True,
            "ppp": True,
            "ionosphere": False,
            "slr": False,
            "sbas": False,
        },
        outputs=("pos",),
        # ⚠️ **只要三样。** 没有 `clk`/`nav`/`atx`/`erp` ——
        #    这不是"我们没下到"，是这条工作流**一个字节都不需要它们**
        #    （下了不用上正是这个仓库反复记着的那种静默浪费）。
        needs=("obs", "sp3", "snx"),
        example="rtk_example.yaml",
        # ⚠️ 「基础」档 —— 模板里那个 `gpt2grid_files` 是**用不到的**
        #    （对流层模型的默认是 `STANDARD`，不是 `gpt2`）。
        profiles=("reduced",),
        # ⚠️ 1 秒 —— 官方示例的值。RTK 的常见形态就是 1 Hz 的实时流。
        #    ⚠️ 但如果你的观测是 30 秒采样的，那就只有 1/30 的历元有解 ——
        #       这是**数据本身的采样率**决定的，不是配置错了。
        epoch_interval=1.0,
        # ⚠️ `run_timeout` 留空 = 用通用默认 3600 秒。
        #    实测（2026-09-27）：300 个历元 / 2 个站 **1.2 秒** ⇒ 一整天
        #    （86400 个历元）约 **350 秒** —— 通用默认够。
        note=(
            "⚠️ **要两个靠得很近的测站**（几公里以内）——站离得远了，"
            "「共有的误差」就不共有，相减消不掉。"
            "⚠️ 解算历元间隔是 **1 秒**；如果你的观测是 30 秒采样的，"
            "那只有 **1/30** 的历元会有解 —— 这是数据本身的采样率决定的，"
            "不是配置错了。"
        ),
    ),

    # --------------------------------------------------------------- SBAS ×3
    #
    # 出处：`sbas_example_l1.yaml` / `sbas_example_dfmc.yaml` / `sbas_example_pvs.yaml`
    #
    # ⚠️⚠️ **三条走的是同一份数据、同一组开关里的一个 `sbas.mode`。**
    #     为什么还是分成三条（而不是一条带参数）：
    #     `Workflow.modes` 那一栏的规矩是「**每个工作流内部那组开关是定死的**，
    #     不给模型自由组合的机会」（见模块 docstring）。
    #     而 `sbas.mode` 恰恰是那种"配错了不报错、只是结果不对"的东西 ——
    #     它决定怎么解读那批 `.ems` 电文。所以三条各写各的。
    #
    # ⚠️ **第四条 `sbas_example_pvs_rt.yaml` 没有对应的条目** ——
    #     它用的是 `sisnet://` 和 NTRIP **活流**（`rtcm_inputs` 里是 URL），
    #     和 `rt_ppp_example` 同一类，做不了。见路线图第四节。
    #
    # ⚠️ 三条的 `modes` 都是"模板只写了 `sbas: true`，别的留默认"补全来的：
    #     `acsConfig.hpp:578-585` 的默认是 preprocessor/spp = true、
    #     ppp = false。而 `pvs` 那个示例**显式**写了 `ppp: true`。
    "sbas_l1": Workflow(
        key="sbas_l1",
        label="SBAS 单频增强（演示）",
        purpose=(
            "用**星基增强系统**（SBAS）的改正信息做定位。"
            "SBAS 是澳大利亚/新西兰那套从同步卫星播发改正的系统（叫 SouthPAN），"
            "它让普通接收机也能做到米级。这一条用的是它的 **L1 单频**服务。"
            "输出是每个历元一个的位置（SPP）。"
        ),
        modes={
            "preprocessor": True,
            "spp": True,
            "ppp": False,
            "ionosphere": False,
            "slr": False,
            "sbas": True,
        },
        outputs=("spp",),
        needs=("obs", "atx", "snx", "nav", "ems"),
        example="sbas_example_l1.yaml",
        profiles=("reduced",),   # 同 brdc2sp3：模型文件对它没用
        dataset="sbas_2025_315",
        # ⚠️ **1 秒** —— 官方示例**没写**这个键，用的是 Ginan 的默认值，
        #    而那个默认值就是 `1`（`acsConfig.hpp:553`）。
        #
        #    不写这一栏的话，app 会填自己那个默认（30 秒）——
        #    实测数据是 1 秒采样的，于是历元数从 **25200 掉到 840**，
        #    精度和覆盖都变了，而**没有任何地方会报错**。
        epoch_interval=1.0,
        note=(
            "⚠️ 它跑的是**官方那份录下来的演示数据**（2025-11-11 的 7 小时、"
            "四个澳大利亚/新西兰的站），**不是你自己选的日期和测站**。"
            "SBAS 的改正信息播完就不归档，没有日常源可用。"
        ),
    ),

    "sbas_dfmc": Workflow(
        key="sbas_dfmc",
        label="SBAS 双频多星座（演示）",
        purpose=(
            "SBAS 的 **DFMC**（双频多星座）服务 —— "
            "比单频那套更准，因为它同时用两个频段，"
            "能消掉电离层带来的大部分误差。用的还是 SouthPAN 的 L5 信息。"
        ),
        modes={
            "preprocessor": True,
            "spp": True,
            "ppp": False,
            "ionosphere": False,
            "slr": False,
            "sbas": True,
        },
        outputs=("spp",),
        needs=("obs", "atx", "snx", "nav", "ems"),
        example="sbas_example_dfmc.yaml",
        profiles=("reduced",),
        dataset="sbas_2025_315",
        epoch_interval=1.0,   # 同上：示例没写，默认就是 1 秒

        note=(
            "⚠️ 同 `sbas_l1`：跑的是官方那份固定的演示数据，"
            "日期和测站都不是你选的。"
        ),
    ),

    "sbas_pvs": Workflow(
        key="sbas_pvs",
        label="SBAS 精密定位（演示）",
        purpose=(
            "用 SouthPAN 的 **PVS**（精密定位服务）做定位。"
            "它比前两条更准 —— 不只是改正信息，还用了载波相位。"
            "输出是每个历元的位置（POS）。"
        ),
        # ⚠️ `ppp: true` 是**示例显式写的**（`sbas_example_pvs.yaml`），
        #    不是默认值 —— 这一条真的走滤波器，所以也慢得多（实测 724 秒 vs 226）。
        modes={
            "preprocessor": True,
            "spp": True,
            "ppp": True,
            "ionosphere": False,
            "slr": False,
            "sbas": True,
        },
        outputs=("pos",),
        needs=("obs", "atx", "snx", "nav", "ems"),
        example="sbas_example_pvs.yaml",
        profiles=("reduced",),
        dataset="sbas_2025_315",
        epoch_interval=1.0,
        # ⚠️ `run_timeout` **不填**（用通用默认 3600 秒）。
        #    实测这一条整天 **724 秒**，是默认值的 1/5 —— 够。
        #    （填一个和默认一样的值是造一个死字段。）
        note=(
            "⚠️ 同前两条：官方演示数据，日期和测站都不是你选的。"
            "⚠️ 它比那两条**慢得多**（实测 12 分钟），因为它真的跑滤波器。"
        ),
    ),
}

DEFAULT = "ppp"
"""默认工作流。

⚠️ 必须是 `ppp` —— app 至今为止**只有**这一条跑通过，
   换默认值等于把没验证过的路变成主路。
"""


def get(key: str | None) -> Workflow:
    """按 key 取，取不到就回默认。

    ⚠️ 取不到时**不抛异常**，回 `ppp`。
       理由：`workflow` 将来会从模型/界面来，拼错一个字母就崩掉整条链路，
       而"退回最熟的那条路"是更安全的降级。
       但**要能看出来发生了降级** —— 见 `is_known`。
    """
    return WORKFLOWS.get((key or "").strip().lower(), WORKFLOWS[DEFAULT])


def is_known(key: str | None) -> bool:
    return (key or "").strip().lower() in WORKFLOWS


def choices() -> list[str]:
    """所有可选的工作流 key —— 给工具描述和界面用。

    ⚠️ 从表里取，不要手写第二个列表。
    """
    return list(WORKFLOWS)
