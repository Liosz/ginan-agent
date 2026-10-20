"""工作区 —— 一次处理任务的全部状态。

============================ 为什么需要这个 ============================
一个完整的处理流程是**有状态的**：

    plan_run        → 得到取数计划
    fetch_data      → 按计划下载，得到下载结果
    generate_config → 按计划和已下载的文件生成配置
    run_processing  → 按配置跑
    summarize       → 按运行结果出结论

每一步都依赖上一步的产物。

⚠️ 那为什么不把这些产物当成工具参数让模型传？

   因为**它们根本不适合穿过模型**：

     · 取数计划里有几十个文件、每个带 URL 和大小 —— 几 KB 的 JSON
     · 让模型"把上一步的结果原样传回来"，它可能漏字段、改格式、截断
     · 而且每一轮都要重新发一遍，白白占上下文

   **工具之间的中间产物，不该经过模型。**

============================ 这和"全局状态"的区别 ============================
前面修过一个 bug：工具注册表是模块级全局变量，导致"传进来的工具集不生效"。
那次的教训是"能变成对象就别用全局"。这里为什么又用一个共享对象？

区别在于**语义**：

    ❌ 全局工具注册表 → 语义上应该"每个 agent 一份"，用全局就是错的
    ✅ 工作区         → 语义上就是"用户当前在做的这一件事"，**本来就是单数**

用户一次只处理一个任务。这不是"全局状态的坏味道"，
而是"这件事本来就只有一个"。

⚠️ 但它仍然是**显式传进去的**（见 ginan_tools.build_ginan_tools），
   不是模块级变量 —— 这样测试可以造自己的工作区，
   两个 agent 也能各有各的。**显式的单例 ≠ 隐式的全局。**
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
# ⚠️ `time` 这个名字**已经被 `import time` 占了**（下面用它的 `strftime`），
#    所以 `datetime.time` 只能换个名字进来。`class time` 不是 `module time`，
#    混用的话 `time.min` 会在运行到那一行时才炸。
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path

from ..ginan import config as gconfig
from ..ginan import fetch as gfetch
from ..ginan import paths as gpaths
from ..ginan import posfile as gpos
from ..ginan import workflows as gworkflows
from ..ginan.catalog import RunPlan, plan_run


_TABLE_FILE_KEYS: dict[str, str] = {
    "EGM2008.gfc": "egm_files",
}
"""模型文件名 → `ConfigInputs` 里对应的字段名。

⚠️ 只登记**工作流特有**的那些。「完整」档那批通用模型走的是
   `catalog.table_inputs()` —— 那是另一条路（它管"**写进配置**的哪个键"，
   不回答"这个工作流要不要"）。
"""

_TABLE_FILE_ALSO_OK: dict[str, tuple[str, ...]] = {
    "egm_files": ("goco05s.gfc",),
}
"""除了要下的那个，**盘上有这些也算数**（手工准备的替代品）。

⚠️ app **不下载**这些替代品 —— 但用户自己放进来了就不该被拦住。

⚠️ `goco05s.gfc` 只有 5 MB，而 `EGM2008.gfc` 有 252 MB。而模板里
   `egm_degree: 15` —— 只用到 15 阶，两个都远远够。
   **但 app 只下 EGM2008**，因为**只有它被实测跑通过** pod；
   换 goco05s 是改物理，不该顺手做。
"""


def table_file_names(wf: gworkflows.Workflow, key: str) -> tuple[str, ...]:
    """这个工作流为 `key` 这一类去盘上找的**全部**文件名（按优先级）。

    ⚠️⚠️ **必须和 `do_generate_config` 报错时说的是同一份清单。**
       "去找的是这几个"和"告诉用户要放这几个"各写一份的话，
       它们迟早对不上 —— 而那种错的样子是**用户按报错放了文件，
       程序还是找不到**，两边都觉得自己没错。

    ⚠️ 要下哪些文件只有一个出处：`catalog.WORKFLOW_TABLES`。
    """
    from ..ginan import catalog as gcatalog

    todo = [
        name for name, _purpose in gcatalog.workflow_tables(wf.key)
        if _TABLE_FILE_KEYS.get(name) == key
    ]
    return tuple(todo) + tuple(_TABLE_FILE_ALSO_OK.get(key, ()))


def table_files_for(wf: gworkflows.Workflow) -> dict[str, list[str]]:
    """这个工作流要的**模型文件**，以及在盘上真找到的那些。

    ⚠️ 它们不在 `plan.files` 里（那是"按天要下的东西"），
       而是"程序的一次性依赖"，落在 `<data>/tables/`。
       所以不能按计划里的文件名去找 —— 找不到。

    ⚠️⚠️ **两个调用方，一个判据：**

         · `make_config_inputs` 拿它填 `egm_files`（不填 → POD 没有重力场模型）
         · `ginan_tools.do_generate_config` 拿它**拦**（缺了就不让生成配置）

       各自去找一遍的话，"这个工作流要哪些模型文件"就有两个答案，
       而两份迟早会漂 —— 漂的后果是**生成配置时检查的那批**和
       **实际写进配置的那批**不是同一批。那正是本项目反复记着的坑。

    ⚠️ 返回值的键名就是 `ConfigInputs` 里对应的字段名（`egm_files`）。
       **"键在、值为空" ≠ "键不在"**：
           · 键不在    → 这个工作流**不需要**这类文件
           · 键在但空  → 需要，但**盘上没有** ← 调用方要区别对待

    ⚠️ 值是能直接写进配置的相对路径（**带 `tables/` 前缀**）——
       Ginan 拿 `inputs_root` 做字符串拼接，这个前缀不能省。
       （`paths.tables_dir()` 必须在 `data/` 下面，也是同一个原因。）
    """
    out: dict[str, list[str]] = {}

    from ..ginan import catalog as gcatalog

    for name, _purpose in gcatalog.workflow_tables(wf.key):
        key = _TABLE_FILE_KEYS.get(name)
        if key is None:
            continue  # 没登记的文件名 → 跳过（守卫会报出来）
        found: list[str] = []
        for cand in table_file_names(wf, key):
            if (gpaths.tables_dir() / cand).is_file():
                found = [f"tables/{cand}"]
                break
        out[key] = found

    return out


@dataclass
class Workspace:
    """一次处理任务的全部状态。"""

    # ---- 当前任务 ----
    plan: RunPlan | None = None
    fetched: gfetch.FetchReport | None = None
    config_path: Path | None = None
    config_inputs: gconfig.ConfigInputs | None = None

    # ---- 这一次要做哪一种"事" ----
    #
    # ⚠️ 2026-09-24 加的。默认 `ppp` —— 那是**唯一**跑通过的一条路。
    #
    # ⚠️ 它是 `Workspace` 的属性而不是 `plan` 的属性：
    #    换工作流时**计划也得重做**（要下的文件不一样），
    #    但"我要做什么"这件事在拿到计划之前就已经定了。
    workflow: str = gworkflows.DEFAULT

    # ---- 改正项：用户在设置里**逐项**改过的那些 ----
    #
    # ⚠️ 稀疏覆盖（空 = 完全按档位走），形状和 `Settings.corrections` 一致。
    #    放这里而不是让工具去读 `settings`：生成配置的工具**不该**知道
    #    "用户设置存在哪" —— 那是界面层的事，而且那样就没法在一跑里固定住
    #    "这次用的是哪一组改正项"（用户在跑到一半改了设置，配置会变）。
    corrections: dict = field(default_factory=dict)

    # 生成配置时，**下载失败因而没能写进配置**的文件类别（如 ["nav"]）。
    #
    # ⚠️ 必须显式记下来并报出去：只把文件悄悄从配置里去掉，
    #    用户会以为一切正常，直到 Ginan 报一个看不懂的错。
    missing_kinds: list[str] = field(default_factory=list)

    # ---- 运行记录（可以有多次 —— 用户改参数重跑很常见）----
    runs: list = field(default_factory=list)

    # ---- 工作目录 ----
    #
    # ⚠️ 一次任务一个目录，所有产物都在里面。
    #    这样用户要"把这次的结果发给别人"时，直接打包这个目录就行。
    work_dir: Path = field(
        default_factory=lambda: gpaths.runs_dir()
        / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
    )

    # =========================================================================
    # 任务
    # =========================================================================

    def start_plan(
        self, stations: str | list[str], day, workflow: str | None = None
    ) -> RunPlan:
        """开始一个新任务。

        ⚠️ 会**清掉上一次的产物** —— 换站/换天就是新任务，
           把旧计划和旧下载结果留着只会让后面几步用错数据。

        ⚠️ `stations` 可以是一个站名，也可以是一串 —— 后者是**多站一次算完**。
           两条走的是同一条路（见 `catalog.RunPlan`）。

        ⚠️ `workflow` 不给就**沿用当前那个**（默认 ppp）——
           不要写成"不给就重置成 ppp"：那样"换站"会顺带把工作流也换掉，
           而用户只是想换个站。
        """
        if workflow is not None:
            self.workflow = gworkflows.get(workflow).key
        # ⚠️ 把工作流**传下去** —— 它决定这份计划要下哪几类文件
        #    （见 `catalog.plan_run`）。不传的话 `Workflow.needs` 又变回死字段。
        self.plan = plan_run(stations, day, self.workflow)
        self.fetched = None
        self.config_path = None
        self.config_inputs = None
        self.missing_kinds = []
        return self.plan

    @property
    def has_plan(self) -> bool:
        return self.plan is not None

    # =========================================================================
    # 配置
    # =========================================================================

    def _download_attempted(self, kind: str) -> bool:
        """这一类文件有没有**在下载范围里**（不管成没成）。

        ⚠️ 这里原来**绕了一圈**：拿 `filename` 回计划里对，而不是直接看
           `r.kind`。当时给的理由是「`DownloadResult.kind` 是下载后按真实
           内容识别出来的类型，而下载失败时压根没有内容可识别，那个字段
           就是空的」。

        ⚠️⚠️ 那个理由**两条都不成立**：

             ① `kind` 现在填的是**计划里的那一类**（`fetch.py` 里那段说明），
                不只是成功时才有，失败的结果也一样带着
             ② 而且原来那句"下载失败时字段是空的"从来就不是事实 ——
                `ident.kind` 认不出来时返回的是 `"unknown"`，**不是空串**
                （正是这个"永远不空"让另一处的 `or` 兜底成了死代码）

        所以直接比 `r.kind` 就行 —— 少一次文件名比对，也就少一处
        "文件名变了但这里没跟着变"的隐患。
        """
        if self.fetched is None or self.plan is None:
            return False
        return any(r.kind == kind for r in self.fetched.results)

    def _station_positions(
        self, obs_names: list[str], data_dir: Path
    ) -> dict[str, tuple[float, float, float] | None]:
        """从**每一个**观测文件头里取出「站号 → 近似坐标」。

        ⚠️⚠️ 为什么必须做这件事，实测数据见 `config._receiver_options`。
           概括：Ginan 默认**拿不到** RINEX 头里那个坐标，
           于是用单点定位凑合先验坐标 → 对流层模型高度离谱 →
           **大部分卫星回退到单频**。而这一切**不会让解算失败**，
           只是让结果变差。

        ⚠️⚠️ **网络解时这里有好几个文件，每一个都要读。**
           只读第一个的话，其余站在配置里**没有先验坐标** →
           那几站悄悄退回单点定位 → 解算质量下降，
           而现象正是"看起来正常、只是精度差"。
           **一次只算一个站的代码，搬到多站上就会变成这样。**

        ⚠️ 站号取**文件头里那个**（权威），不取用户在对话里说的那个 ——
           用户可能说 `ALIC`，文件头写着 `ALIC00AUS`，
           而 Ginan 内部用的又是 4 位的 `ALIC`。
           取不到才用计划里的站号兜底。

        ⚠️ 读不到坐标时值是 `None`（**不是** (0,0,0)）：
           写进配置会被当成"地心"，比不写更糟。
        """
        from ..ginan import rinex as grinex

        out: dict[str, tuple[float, float, float] | None] = {}

        # ⚠️ 按**计划里那些站**去遍历，而不是按文件列表 ——
        #    这样"配置里站块的顺序"和"用户给的顺序"一致（dict 保序）。
        plan_stations = list(self.plan.stations) if self.plan else []
        for st in plan_stations:
            # 认领时按内容改过名，但**站号前缀是认领时写上去的**
            # （见 `inbox._decide_name`）。
            name = next(
                (n for n in obs_names if n.upper().startswith(st)), None
            )
            if name is None:
                continue
            path = data_dir / name
            if not path.is_file():
                continue
            try:
                info = grinex.inspect(path)
            except Exception:  # noqa: BLE001
                continue

            marker = (getattr(info, "marker", "") or "").strip().upper()
            # `ALIC00AUS` → `ALIC`；头里读不到才用计划里那个
            sid = marker[:4] if len(marker) >= 4 else st[:4]
            if not sid or sid in out:
                continue

            xyz = getattr(info, "approx_xyz", None)
            if not xyz or len(xyz) < 3 or all(v == 0 for v in xyz[:3]):
                # ⚠️ 缺了 / 全零 = 文件头里那个字段是空的 → None
                out[sid] = None
                continue
            out[sid] = (float(xyz[0]), float(xyz[1]), float(xyz[2]))

        return out

    def make_config_inputs(self, profile: str = gconfig.DEFAULT_PROFILE):
        """按当前计划和已下载的文件，凑出生成配置需要的输入。

        ⚠️ 文件名要从**实际下载的结果**里取，不能直接用计划里的名字。
           因为下载器可能改名（实测：观测数据会用文件头里的站名命名）。
           用计划里的名字去写配置，Ginan 会找不到文件。

        ⚠️⚠️ **"没下载过"和"下载过但失败了"必须分开对待。**

           原来的写法是二选一：有下载结果就用、没有就退回计划名。
           但"没有"包含了**两种完全不同的情况**：

              ① 还没开始下    → 退回计划名是对的（用户可能自己去补文件）
              ② 下过但全失败  → 退回计划名是**错的**！

           情况②下写出来的配置里，会有一个**磁盘上根本不存在的文件**，
           于是 Ginan 报"缺文件" —— 而错误信息离根因（下载失败）很远，
           用户/模型会一头雾水地去查配置。

           这正是"**计划里有 ≠ 磁盘上有**"那条原则的一个实例。
           现在宁可**不写**，也要如实告诉调用方缺了什么。
        """
        if self.plan is None:
            raise RuntimeError("还没有取数计划 —— 先调用 plan_run")

        data_dir = gpaths.data_dir()
        missing: list[str] = []

        def names_of(kind: str) -> list[str]:
            out: list[str] = []

            if self.fetched is None:
                # ① 还没开始下载 —— 用计划里的名字，用户可能自己去补文件
                return [f.filename for f in self.plan.files if f.kind == kind]

            for r in self.fetched.ok:
                if r.kind == kind and r.real_name:
                    out.append(r.real_name)
            if out:
                return out

            # ② 下载过、但这一类一个都没成功 —— **绝不退回计划名**
            if self._download_attempted(kind):
                missing.append(kind)
                return []

            # ③ 这一类根本不在下载范围里 —— 才退回计划名
            return [f.filename for f in self.plan.files if f.kind == kind]

        # ⚠️ config_id 里**不放站名** —— 站名已经在输出文件名里了。
        #
        #    Ginan 的输出文件名模板是 `<RECEIVER>_<CONFIG>.POS`，
        #    如果 config_id 是 `ALIC_20260902`，得到的是
        #    `ALIC_ALIC_20260902.POS` —— 站名重复了一遍。
        #
        #    看一眼就知道的事，但用户看到这种文件名会觉得程序很粗糙。
        cid = self.plan.day.strftime("%Y%m%d")

        # ⚠️ 每一类只算一次 —— names_of 有副作用（会往 missing 里记东西）。
        #
        # ⚠️ `ems` 是 2026-09-27 加的（SBAS）。它**不在**任何按天取的源里 ——
        #    只有 `Workflow.dataset` 那条路会产出它（见 `catalog.dataset_files`）。
        #    加进这个元组是**必须**的：不加的话，下载成功了也不会写进配置，
        #    而 SBAS 缺了改正信息**照样出结果**（只是退化成普通 SPP）——
        #    正是这个仓库最怕的那种"看着正常、其实是错的"。
        files = {
            kind: names_of(kind)
            for kind in ("atx", "erp", "sp3", "clk", "nav", "obs", "snx", "bia", "ems")
        }

        # ---- 模型文件：由**工作流**决定要哪些 ----
        #
        # ⚠️ 判据和"要下哪些文件"共用同一个函数 —— 见 `table_files_for`。
        wf = gworkflows.get(self.workflow)
        tables = table_files_for(wf)
        egm_files: list[str] = tables.get("egm_files", [])
        if "egm_files" in tables and not egm_files:
            # ⚠️ 如实记进 missing —— 不说的话，用户会看到
            #    "跑完了但没有轨道"，而根因是缺一个 252 MB 的模型文件。
            missing.append("egm")

        # ---- 测站坐标：从观测文件头里读出来，显式写进配置 ----
        #
        # ⚠️⚠️ 这一项**必须给**，实测数据见 config._receiver_options。
        #     一句话：不给的话，Ginan 会用单点定位凑合先验坐标，
        #     对流层模型拿到离谱的高度，**大部分卫星回退到单频** ——
        #     而结果**看起来正常、只是精度差**。
        stations = self._station_positions(files["obs"], data_dir)

        inputs = gconfig.ConfigInputs(
            config_id=cid,
            inputs_root=data_dir,
            obs_root=data_dir,
            outputs_root=self.work_dir,
            atx_files=files["atx"],
            erp_files=files["erp"],
            sp3_files=files["sp3"],
            clk_files=files["clk"],
            nav_files=files["nav"],
            rnx_inputs=files["obs"],
            # ⚠️ 这两类是**锦上添花**：下不到的话 Ginan 会给一批警告
            #    （`Invalid SINEX file` / `Bias not found …`）但**照样出结果**。
            #
            #    ⚠️ 但它们仍然会被 names_of 记进 missing（那是它第②条的行为）——
            #       **这是对的，不要为它们开特例**：
            #       下不到就该如实说，只是措辞上它们是"少了改正项"，
            #       不是"跑不了"。区别在怎么解释，不在报不报。
            snx_files=files["snx"],
            bias_files=files["bia"],
            # ⚠️ 一张表（站号 → 坐标）—— 单站就是只有一项的表。
            #    网络解时这里有好几项，配置里就会有好几个站块。
            stations=stations,
            profile=profile,
            # ⚠️ 逐项改正项：用户在界面上改过的那些（空 = 按档位默认）。
            #    见 `corrections.py` 和 `config._model_options`。
            corrections=dict(self.corrections),
            # ⚠️ 工作流决定 `process_modes` 和 `outputs` —— 见 `build_config`。
            workflow=wf.key,
            # ⚠️ 空列表时 `build_config` **不会**写这个键（见那段说明）。
            egm_files=egm_files,
            ems_files=files["ems"],
            # ---- 处理时段：**用户要的那一天** ----
            #
            # ⚠️⚠️ 这两行是 2026-09-27 补的 —— 在那之前**从来没有**设过它们，
            #    而那是靠"恰好"在跑：

            #        模板里有 `start_epoch`/`end_epoch` 的（`brdc2sp3.yml`）
            #                        → 用的是**模板硬编码的 2019-07-18**
            #        模板里没有的（`pod_example.yaml`）→ 配置里根本没有这两个键
            #
            #    ⇒ 用户要跑 2026-09-02 的广播星历，配置里会写着 `start_epoch:
            #      2019-07-18` —— 时间和手上的 nav 文件对不上，而 **Ginan 不会
            #      报错**，它只会算出一条空轨道。
            #
            # ⚠️ 同时它还是 `-n`（唯一有效的刹车）的依据：见 `config.epoch_count`。
            #
            # ⚠️ `end_epoch` 给的是**次日 00:00**（闭区间的右端），
            #    这样 `86400 / 间隔` 正好是 POD 的 288、brdc2sp3 的 96 ——
            #    两个数都和实测跑出来的历元数**逐位相同**。
            #
            # ---- ⚠️ 加这两行**动到了 PPP**，所以量过 ----
            #
            #    PPP 是**有实测数字背书**的那条路，动它的前提是**量**，不是推。
            #    对照实验（`ginan-testdata-202506/cheap3/probe_ppp_span.py`，
            #    ALIC 2026-09-02 / 30 个历元 / 同一批文件，只差配置里有没有这两个键）：
            #
            #        有 start/end    POS 63 行，处理 29 个历元
            #        无 start/end    POS 63 行，处理 29 个历元
            #        **两份 POS 逐行相同**
            #
            #    ⇒ 对 PPP 是**逐字节的恒等变换**，可以加。
            #
            #    ⚠️ 第一版的对照实验**什么也没测到**：夹具里那份"观测"文件
            #       名字是 `..._R_..._MN.rnx`，头部却写着 `N: GNSS NAV DATA`
            #       （`_MN` = mixed navigation）—— 两边都跑出 0 个历元、
            #       0 行 POS，而"逐行相同"在**两个空文件**上恒为真。
            #       判据里现在有一条"先证明真的跑出东西了"。
            start_epoch=datetime.combine(self.plan.day, clock_time.min),
            end_epoch=datetime.combine(self.plan.day, clock_time.min)
            + timedelta(days=1),
        )

        # ⚠️ 记下来，让 generate_config 的调用方**能说出来**缺了什么。
        #    只把文件从配置里去掉、却不告诉任何人，用户会以为一切正常，
        #    直到 Ginan 报一个看不懂的错。
        self.missing_kinds = missing
        return inputs

    # =========================================================================
    # 产物
    # =========================================================================

    @property
    def latest_run(self):
        return self.runs[-1] if self.runs else None

    def pos_files(self) -> list[Path]:
        """本次任务产生的所有 .POS 文件。

        ⚠️ 不能写成 `glob("*.POS") + glob("*.pos")` —— **Windows 上会重复**。

           因为 Windows 的文件名匹配**大小写不敏感**，
           两个模式匹配的是同一批文件，加在一起就是每个文件两份。

           踩过：界面上同一份结果显示了两次，看起来像跑了两次。

           用 macOS（默认也不敏感）和 Linux（敏感）也一样有问题 ——
           敏感的系统上不会重复，但不敏感的系统上会。**跨平台不一致**。
        """
        try:
            found = {p.resolve(): p for p in self.work_dir.iterdir()
                     if p.is_file() and p.suffix.lower() == ".pos"}
        except OSError:
            return []
        return sorted(found.values(), key=lambda p: p.name)

    def results(self) -> list[gpos.PosFile]:
        """解析出所有定位结果。"""
        return [gpos.parse(p) for p in self.pos_files()]

    def sp3_files(self) -> list[Path]:
        """本次任务产生的所有 .SP3 轨道文件。

        ⚠️ 和 `pos_files` 一样**按小写后缀判**（`.lower() == ".sp3"`），
           不写成 `glob("*.SP3") + glob("*.sp3")` —— Windows 的文件名匹配
           大小写不敏感，两个模式匹配的是同一批文件，加一起每份就出现两次。

        ⚠️ 这是 **POD 的产物**（PPP 也出 SP3，但那是"把输入抄一遍"，
           数据来自输入的精密轨道而不是估计）。
        """
        try:
            found = {p.resolve(): p for p in self.work_dir.iterdir()
                     if p.is_file() and p.suffix.lower() == ".sp3"}
        except OSError:
            return []
        return sorted(found.values(), key=lambda p: p.name)

    def summary(self) -> dict:
        """给模型和界面看的状态摘要。"""
        return {
            "stations": list(self.plan.stations) if self.plan else [],
            "day": self.plan.day.isoformat() if self.plan else None,
            "planned_files": len(self.plan.files) if self.plan else 0,
            "downloaded": len(self.fetched.ok) if self.fetched else 0,
            "download_failed": len(self.fetched.failed) if self.fetched else 0,
            "has_config": self.config_path is not None,
            "runs": len(self.runs),
            "last_run_ok": bool(self.runs and self.runs[-1].ok),
            "pos_files": [p.name for p in self.pos_files()],
            "work_dir": str(self.work_dir),
        }
