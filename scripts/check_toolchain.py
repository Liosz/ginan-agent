"""自检：工具链 —— 从"要处理某站某天"到"给出结论"的完整流程。

跑法：python scripts/check_toolchain.py

============================ 这个测试覆盖什么 ============================
它测的是**工具之间的协作**，不是单个工具。

单测每个工具都对，连起来可能全错 —— 因为协作的约定（谁先谁后、
中间产物怎么传）不在任何一个工具里，而在它们**之间**。

⚠️ 用假 Ginan —— 秒级返回、场景可控。
   真跑要下 50 MB 数据，还要网络通，根本没法这样测。
"""

from __future__ import annotations

import json
import math
import os
import re as _re
import sys
import tempfile
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ⚠️ 必须在 import 领域模块**之前**设好 —— find_pea_for_run 是调用时读的，
#    但设早一点更稳妥。
os.environ["GINAN_AGENT_FAKE_PEA"] = str(
    (ROOT / "tests" / "fakes" / "pea.py").resolve()
)
os.environ["FAKE_PEA_DELAY"] = "0.001"

from ginan_agent.agent import ginan_tools as GT  # noqa: E402
from ginan_agent.ginan import catalog  # noqa: E402
from ginan_agent.agent.workspace import Workspace  # noqa: E402
from ginan_agent.core.tools import ToolContext, ToolRegistry  # noqa: E402

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


class Runner:
    """包一层，方便连续调用。"""

    def __init__(self, scenario: str = "") -> None:
        os.environ["FAKE_PEA_SCENARIO"] = scenario
        self.ws = Workspace(
            work_dir=Path(tempfile.mkdtemp(prefix="toolchain_"))
        )
        self.reg = ToolRegistry(GT.build_ginan_tools(self.ws))
        self.ctx = ToolContext(
            call_id="t", name="test", cancel=threading.Event(),
            emit=lambda e: None,
        )

    def __call__(self, name: str, **kwargs) -> dict:
        return json.loads(
            self.reg.execute(name, json.dumps(kwargs), ctx=self.ctx)
        )


# =============================================================================
# ① 正常流程
# =============================================================================


def test_happy_path() -> None:
    section("① 完整流程：计划 → 配置 → 检查 → 运行 → 结论")

    r = Runner()

    plan = r("plan_run", stations=["ALIC"], day="2026-09-02")
    check("files" in plan, "plan_run 返回了文件清单")
    check(len(plan["files"]) >= 5, f"计划里有 {len(plan['files'])} 个文件")
    check(r.ws.has_plan, "计划被存进了工作区")

    cfg = r("generate_config")
    check("config_path" in cfg, "生成了配置")
    check(bool(cfg.get("tradeoff")), "**如实说明了档位的代价**")
    check(r.ws.config_path is not None, "配置路径被记进了工作区")

    val = r("validate_config")
    check("ok" in val, "配置检查返回了结果")
    check("config_mutated" in val, "★ 报告了'配置有没有被改写'")

    run = r("run_processing", max_epochs=40)
    check(run["ok"], f"运行成功：{run['headline']}")
    check("next" in run, "告诉模型下一步该干嘛")

    summ = r("summarize_results")
    check("results" in summ, "拿到了结果")
    if "results" in summ:
        d = summ["results"][0]["data"]
        check(d["epochs"] > 0, f"解析出 {d['epochs']} 个历元")
        v = d["verdict"]
        check(bool(v["headline"]), f"有人话结论：{v['headline']}")
        # ⚠️ caveats 是结论的边界，必须存在
        check(bool(v["caveats"]), f"结论带了 {len(v['caveats'])} 条注意事项")


def test_config_has_sinex_and_bias_keys() -> None:
    section("①b ★ 生成的配置里，卫星元数据和码偏差落在**正确的那一层**")

    # ⚠️⚠️ 这一节是 2026-09-20 加的，动机很具体：
    #
    #    为了消掉 Ginan 每次都在打的两族警告（`Invalid SINEX file` /
    #    `Bias not found for G27 on L1W, using undefined_sigma: 1`），
    #    我们开始下两类新文件，并且要写进配置。
    #
    #    ⚠️ 而它们在 YAML 里**不在同一层**（这是 Ginan 的 schema 定的）：
    #
    #        inputs:
    #          snx_files:   [...]          ← 和 atx_files 平级
    #          satellite_data:
    #            bsx_files: [...]          ← 和 sp3_files 平级
    #
    #    **放错层的话，Ginan 不会报错** —— 它只是看不见那个键，
    #    然后继续打它那批警告。也就是说：我们把文件下下来了、
    #    写进配置了、命令也跑了，**而警告一个都没少**。
    #
    #    > 这一类"配了但没生效"的失败，**只能靠检查生成的产物**来防。
    #
    # ⚠️ 这条守卫不看"文件在不在磁盘上"（那是 fetch 的事），
    #    只看**结构对不对** —— 两件事不一样，混在一起测的话，
    #    一个磁盘问题会让结构问题看不出来。
    import yaml

    r = Runner()
    r("plan_run", stations=["ALIC"], day="2026-09-02")
    r("generate_config")

    path = r.ws.config_path
    check(path is not None and Path(path).is_file(), "配置真的落盘了")

    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    inputs = (doc or {}).get("inputs") or {}

    # ---- ① snx_files 在 inputs 这一层 ----
    check(
        "snx_files" in inputs,
        f"★ `inputs.snx_files` 存在（实际 keys：{sorted(inputs)}）",
    )
    check(
        isinstance(inputs.get("snx_files"), list),
        "★ 它是个列表（不是字符串、不是 None）",
    )
    check(
        "snx_files" not in (inputs.get("satellite_data") or {}),
        "★ 它**不在** satellite_data 里面（放错层 Ginan 会看不见，而且不报错）",
    )

    # ---- ② bsx_files 在 satellite_data 里面 ----
    sat = inputs.get("satellite_data") or {}
    check(
        "bsx_files" in sat,
        f"★ `inputs.satellite_data.bsx_files` 存在（实际 keys：{sorted(sat)}）",
    )
    check(
        isinstance(sat.get("bsx_files"), list),
        "★ 它也是个列表",
    )
    check(
        "bsx_files" not in inputs,
        "★ 它**不在** inputs 顶层（和上一条正好相反 —— 两个键的层是反的）",
    )

    # ---- ③ 原来那几个键不能被挤掉 ----
    for key in ("atx_files", "erp_files", "inputs_root"):
        check(key in inputs, f"原有的 `inputs.{key}` 还在")

    # ---- ④ ⚠️⚠️ 上面那条只查了"键在不在"，**这里查"值有没有"** ----
    #
    #     上面那条测试跑的时候**根本没走下载**（`Runner()` 里 `fetched`
    #     是 None），所以它看到的是"还没下载 → 用计划里的名字"那条分支 ——
    #     键当然在，值当然非空。**它证明不了"下载成功了就写得进去"。**
    #
    #     ⚠️⚠️ 而真 bug 恰恰在那条没被走到的路上：
    #
    #         `fetch` 填 `kind` 时用的是 **identify 的词表**（`sinex` /
    #         `unknown`），而 `workspace.names_of()` 按**计划的词表**
    #         （`snx` / `bia`）去查 —— 两套词表对不上，于是
    #         **这两类文件明明下成功了，却被当成没下到**：
    #         配置里那两栏是空的、`missing` 里报"下载失败"（事实相反）。
    #
    #     👉 那两类文件存在的**全部理由**就是消掉 Ginan 的两族警告，
    #        所以"配置里是空的"= 这次改动**等于没做**，而且不报错。
    #
    #     ⚠️ 判据：**"全部下载成功了" ⇒ 每一类的值都非空**。
    #        这条只有把 fetched 填上、真的走一遍才成立 ——
    #        静态看键名是看不出来的。
    r2 = Runner()
    r2("plan_run", stations=["ALIC"], day="2026-09-02")
    _fake_fetch_report(r2.ws, fail_kinds=set())      # 全部成功
    r2("generate_config")

    doc2 = yaml.safe_load(Path(r2.ws.config_path).read_text(encoding="utf-8"))
    inputs2 = (doc2 or {}).get("inputs") or {}
    sat2 = inputs2.get("satellite_data") or {}

    planned = sorted({f.kind for f in r2.ws.plan.files})
    check(
        not r2.ws.missing_kinds,
        f"★★ 全部下载成功时 `missing_kinds` 是空的（实际 {r2.ws.missing_kinds}）"
        "—— 原来 snx/bia 会**明明成功了却被报成失败**",
    )
    for kind, got, where in [
        ("snx", inputs2.get("snx_files"), "inputs.snx_files"),
        ("bia", sat2.get("bsx_files"), "inputs.satellite_data.bsx_files"),
    ]:
        check(
            kind in planned and bool(got),
            f"★★ 计划里有 {kind} ⇒ 配置里 `{where}` **必须有值**（实际 {got}）",
        )


def test_alternatives_need_permission() -> None:
    section("①c ★ 备选产品：**没经用户同意，一个都不许下**")

    # ⚠️⚠️ 这是这次改动里最要紧的一条不变量。
    #
    #     "备选"不是"多一个镜像" —— 它是**另一个分析中心的解算结果**。
    #     换上去，最终坐标会变（毫米到厘米级），而且**是系统性的**：
    #     它和用户之前算过的、别人算过的**不再可比**。
    #
    #     偷偷换掉分析输入，等于在他不知道的情况下改了他的实验条件。
    #
    # ⚠️ 所以这条断言测的是**"拒绝"那一路**：
    #    用户说"先别用"之后，备选**一个文件都不该被下载**。
    #
    #    （"同意"那一路要联网，这里测不了 —— 它靠 `alternative_used`
    #      那段注记来保证"用了就要说清楚"。见 do_fetch_data。）
    #
    # ⚠️ 顺带守一个**修过的 bug**：那句问话原来长这样 ——
    #
    #        "有 3 个文件所有来源都没下到（），要用备选产品吗？"
    #                                   ↑ 空的
    #
    #    因为**失败的结果没带 `kind`**（只有成功的结果才有，那是识别之后填的）。
    #    → 修在 `fetch.download_one`：失败路径也带上 `kind=item.kind`，
    #      **kind 是计划里就有的，和"下载成没成功"无关。**
    ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="perm_")))
    plan = catalog.plan_run("ALIC", date(2026, 9, 2))
    # 只留三类精密产品，而且源指到一个**必然连接被拒**的本地端口 ——
    # 不联网、秒失败。
    for f in plan.files:
        if f.kind in ("sp3", "clk", "erp"):
            f.urls = {"bkg": "http://127.0.0.1:9/nope.gz"}
    plan.files = [f for f in plan.files if f.kind in ("sp3", "clk", "erp")]
    ws.plan = plan

    check(bool(plan.alternatives), "这个计划确实配了备选（否则测的是空气）")

    asked: dict = {}

    class Refuse:
        """假用户：**拒绝**用备选。"""

        def ask(self, *, call_id, question, options, allow_text, cancel, emit, timeout):
            asked["q"] = question
            asked["opts"] = [o["label"] for o in options]
            # 选**推荐项**（第一个）—— 也就是"先别用"
            return options[0]["label"]

    ctx = ToolContext(
        call_id="t", name="fetch_data", cancel=threading.Event(),
        emit=lambda _e: None, ask=Refuse(),
    )
    # ⚠️⚠️ 套沙箱：`do_fetch_data` 会往 `paths.data_dir()` 里建目录。
    #     不套的话，在**刚克隆的**机器上跑全套自检，`check_all` 的总入口
    #     守卫会报「☠ check_toolchain.py 污染」—— 一次自检在别人机器上
    #     建出了数据目录。（实测：2026-10-13。）
    #     这里只是借它跑一遍流程，本来就不该碰真实目录。
    from tests.sandbox import sandbox

    with sandbox():
        out = json.loads(GT.do_fetch_data(ws, ctx))

    # ---- ① 问了，而且问清楚了 ----
    check("question" in asked or "q" in asked, "确实问了用户")
    q = asked.get("q", "")
    check(
        "（" in q and "）" in q and "（）" not in q,
        f"★ 问话里**点名了是哪几类文件**（不是空括号）：{q.splitlines()[0] if q else '—'}",
    )
    check(
        any("备选" in o for o in asked.get("opts", [])),
        f"选项里有'用备选产品'（{asked.get('opts')}）",
    )
    check(
        "推荐" in asked.get("opts", [""])[0],
        "★ **推荐项是'先别用'** —— 推荐'用备选'等于变相自动顶上",
    )

    # ---- ② 拒绝了就**一个备选都不许下** ----
    check(
        "alternative_used" not in out,
        "★ 用户拒绝后，结果里没有备选记录",
    )
    # ⚠️ 报告里的键是 `files`，不是 `results` ——
    #    那是 `fetch_report_json` 的形状，**别照着自己的想象写**。
    #    （第一版这里写成 `results`，于是判断永远为假。）
    files = out.get("files", [])
    used_sources = {f.get("source", "") for f in files if f.get("ok")}
    check(
        not (used_sources & {"igmas", "esa"}),
        f"★ **没有任何备选源的文件被下载**（实际成功的源：{used_sources or '无'}）",
    )
    check(
        bool(files) and all(not f.get("ok") for f in files),
        f"但如实报告了失败（{len(files)} 个，不能因为'没下到'就什么都不说）",
    )


def test_alternatives_success_is_recorded() -> None:
    section("①c-2 ★★ 备选**下到了**之后，工作区必须记上这一笔")

    # ⚠️⚠️ 上面 ①c 测的是「**拒绝**那一路」（备选一个都不许下）。
    #    这一节测的是**同意、而且下到了**那一路 —— 而它原来是被漏掉的：
    #
    #        `if used_files: ... return _json(result_alt)`   ← 提前返回
    #        ...
    #        ws.fetched = report                              ← 永远走不到
    #
    #    ⚠️ 于是"**成功**"这个结局反而**没有记账**（`ws.fetched` 一直是 None）：
    #
    #        · `workspace_status` 报"已下载 0 个"
    #        · `make_config_inputs()` 走"还没开始下载"那条分支 →
    #          拿**计划里的 IGS 文件名**写配置，而磁盘上是 `isc10783.sp3`
    #          → `validate_config` 报"缺文件"，**错得离根因极远**
    #        · `missing_kinds` 是空的 → 连"缺了什么"都不提示
    #
    #    ⚠️ 这正是 `workspace.py` 那条「**计划里有 ≠ 磁盘上有**」的反面。
    #
    #    ⚠️ 为什么原来没被发现：真实走这条路要联网（备选源在国内时好时坏）。
    #       **一个需要联网才能走到的分支，等于平时没人走。**
    from ginan_agent.ginan import fetch

    ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="altok_")))
    plan = catalog.plan_run("ALIC", date(2026, 9, 2))
    for f in plan.files:
        if f.kind in ("sp3", "clk", "erp"):
            f.urls = {"bkg": "http://127.0.0.1:9/nope.gz"}
    plan.files = [f for f in plan.files if f.kind in ("sp3", "clk", "erp")]
    ws.plan = plan

    # ⚠️ 备选产品的名字**故意和计划里不一样** —— 这正是这个 bug 的要害。
    #    （iGMAS 的命名习惯：`isc…` / `isr…` / `isu…`）
    ALT_NAMES = {"sp3": "isc10783.sp3", "clk": "isc10783.clk", "erp": "isc10783.erp"}
    ALT_NAME = ALT_NAMES["sp3"]

    class Agree:
        def ask(self, *, call_id, question, options, allow_text, cancel, emit, timeout):
            return next(o["label"] for o in options if "备选" in o["label"])

    real_alt = catalog.alternative_files
    real_fetch = fetch.fetch_plan

    def fake_alt(_plan, kinds, session=None):
        # ⚠️ 每一类失败的文件都给一个备选 —— 只给 sp3 的话
        #    `missing_kinds` 会（**正确地**）报剩下那几类没下到，
        #    于是"全都下到了"这个前提就不成立了。
        return {"igmas": [
            catalog.FilePlan(
                filename=ALT_NAMES[k], kind=k,
                urls={"igmas": "http://127.0.0.1:9/alt"}, size_hint="测试",
            )
            for k in sorted(kinds)
            if k in ALT_NAMES
        ]}, []

    def fake_fetch(_plan, dest_dir, ctx=None, proxy=None, items=None):
        """⚠️ 两次调用要给出**不同**的结果，否则测的是空气。

        ⚠️ 我第一版写成"无论谁来都给成功结果" —— 于是**第一次下载也成功了**，
           `report.failed` 是空的 → 备选那条路根本没被走到 →
           那条断言（"这一路确实走了备选"）立刻红了。
           **夹具要照着"应该是怎样"造**：第一次全部失败，第二次才是备选成功。
        """
        rep = fetch.FetchReport()
        if items is None:
            # 第一次：计划里那些文件**全部失败**（源是必然连不上的本地端口）
            for f in plan.files:
                rep.results.append(fetch.DownloadResult(
                    filename=f.filename, ok=False, kind=f.kind,
                    detail="测试：连不上",
                ))
            return rep
        # 备选那一次：成功
        for item in items:
            rep.results.append(fetch.DownloadResult(
                filename=item.filename, ok=True, kind=item.kind,
                real_name=item.filename, path=dest_dir / item.filename,
                size=1024, source="igmas",
            ))
        return rep

    catalog.alternative_files = fake_alt
    fetch.fetch_plan = fake_fetch
    try:
        ctx = ToolContext(
            call_id="t", name="fetch_data", cancel=threading.Event(),
            emit=lambda _e: None, ask=Agree(),
        )
        # ⚠️ 同上一节：`do_fetch_data` 会建真实的数据目录 —— 套沙箱。
        from tests.sandbox import sandbox

        with sandbox():
            out = json.loads(GT.do_fetch_data(ws, ctx))
    finally:
        catalog.alternative_files = real_alt
        fetch.fetch_plan = real_fetch

    check(
        out.get("alternative_used") or "备选" in json.dumps(out, ensure_ascii=False),
        "这一路确实走了备选（否则测的是空气）",
    )
    check(
        ws.fetched is not None,
        "★★ 工作区记上了下载报告（原来 `ws.fetched` 一直是 None）",
    )
    inputs = ws.make_config_inputs()
    check(
        not ws.missing_kinds,
        f"★★ 不会误报'有文件没下到'（missing_kinds = {ws.missing_kinds}）",
    )
    check(
        ALT_NAME in inputs.sp3_files,
        f"★★ 配置里用的是**备选产品的真实文件名**（{inputs.sp3_files}）—— "
        f"拿计划名 {ALT_NAME!r} 之外的名字写进去的话，"
        "validate_config 会报'缺文件'，而那个错误离根因极远",
    )


def test_workspace_tracks_state() -> None:
    section("② 工作区正确记录每一步的状态")

    r = Runner()
    s0 = r("workspace_status")
    check(not s0["has_config"] and s0["runs"] == 0, "开始时什么都没有")

    r("plan_run", stations=["DARW"], day="2026-09-01")
    s1 = r("workspace_status")
    check(s1["stations"] == ["DARW"], "记下了测站")
    check(s1["day"] == "2026-09-01", "记下了日期")
    check(s1["planned_files"] > 0, "记下了计划文件数")

    r("generate_config")
    r("run_processing", max_epochs=20)
    s2 = r("workspace_status")
    check(s2["has_config"], "记下了'配置已生成'")
    check(s2["runs"] == 1, "记下了跑过一次")
    check(s2["last_run_ok"], "记下了最后一次成功")

    # ⚠️ 产物文件不能重复 —— 见 workspace.pos_files 的说明
    check(
        len(s2["pos_files"]) == len(set(s2["pos_files"])),
        f"结果文件没有重复（{s2['pos_files']}）",
    )


# =============================================================================
# ③ 顺序约束
# =============================================================================


def test_order_enforced() -> None:
    section("③ 顺序约束：跳过前置步骤要给出可读的错误")

    # ⚠️ 这是**给模型看的**错误。模型看不到我们的代码流程，
    #    只有错误信息能告诉它"你得先做 X"。
    #    说"no plan"没用，要说"必须先调用 plan_run"。

    r = Runner()

    cfg = r("generate_config")
    check("error" in cfg, "没计划就生成配置 → 报错")
    check("plan_run" in cfg["error"], f"错误里指明了该先做什么：{cfg['error'][:50]}")

    run = r("run_processing", max_epochs=10)
    check("error" in run, "没配置就运行 → 报错")
    check("generate_config" in run["error"], "错误里指明了该先做什么")

    summ = r("summarize_results")
    check("error" in summ, "没跑过就总结 → 报错")


def test_bad_input() -> None:
    section("④ 参数错误要给出可读的提示")

    r = Runner()

    bad_day = r("plan_run", stations=["ALIC"], day="2026/9/2")
    check("error" in bad_day, "日期格式错 → 报错")
    check("YYYY-MM-DD" in bad_day["error"], "提示了正确的格式")

    bad_station = r("plan_run", stations=["X"], day="2026-09-02")
    check("error" in bad_station, "站号太短 → 报错")
    check("ALIC" in bad_station["error"], "给了例子")

    bad_profile = r("generate_config", profile="没有这个档位")
    # 没有计划，所以先报"没计划"；先建计划再试
    r("plan_run", stations=["ALIC"], day="2026-09-02")
    bad_profile = r("generate_config", profile="没有这个档位")
    check("error" in bad_profile, "档位不存在 → 报错")
    check("reduced" in bad_profile["error"], "列出了可用的档位")


# =============================================================================
# ⑤ 失败路径
# =============================================================================


def test_missing_file_scenario() -> None:
    section("⑤ 缺文件时：要说清缺哪个、怎么办")

    r = Runner("missing")
    r("plan_run", stations=["ALIC"], day="2026-09-02")
    r("generate_config")
    run = r("run_processing", max_epochs=10)

    check(not run["ok"], "运行失败")
    problems = run["diagnosis"]["problems"]
    check(bool(problems), f"报出了 {len(problems)} 条问题")
    if problems:
        p = problems[0]
        check(bool(p.get("what")), f"解释了是什么：{p['what'][:50]}")
        check(bool(p.get("what_to_do")), f"说了该怎么办：{p.get('what_to_do','')[:50]}")
        check(bool(p.get("raw")), "保留了原文（供排查）")
    check("next" in run, "告诉模型下一步该干嘛")


def test_mutated_config() -> None:
    section("⑥ ⚠️ 配置被静默改写：退出码 0，但必须报出来")

    # ⚠️ 这是**这个产品存在的理由之一**。
    #    Ginan 的 sanity checker 会静默改配置，退出码还是 0。
    #    如果我们的链路不报这个，用户拿到的是"处理成功"，
    #    而实际跑的不是他给的那份配置。

    r = Runner("mutated")
    r("plan_run", stations=["ALIC"], day="2026-09-02")
    r("generate_config")
    val = r("validate_config")

    check(val["ok"], "配置检查通过（退出码是 0）")
    check(val["config_mutated"], "★ 但报告了 config_mutated=true")
    check(bool(val["warnings"]), f"列出了 {len(val['warnings'])} 条改动")
    if val["warnings"]:
        w = val["warnings"][0]
        check(bool(w.get("what_to_do")), "说明了'为什么值得在意、该怎么办'")


def test_cancelled_run() -> None:
    section("⑦ 取消：要如实报告，而不是说'失败'")

    os.environ["FAKE_PEA_SCENARIO"] = ""
    ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="cancel_")))
    reg = ToolRegistry(GT.build_ginan_tools(ws))
    cancel = threading.Event()
    ctx = ToolContext(call_id="t", name="test", cancel=cancel, emit=lambda e: None)
    os.environ["FAKE_PEA_DELAY"] = "0.01"

    reg.execute("plan_run",
                json.dumps({"stations": ["ALIC"], "day": "2026-09-02"}))
    reg.execute("generate_config", "{}")

    threading.Timer(0.4, cancel.set).start()
    r = json.loads(reg.execute("run_processing",
                               json.dumps({"max_epochs": 300}), ctx=ctx))

    check(not r["ok"], "被取消了，所以不算成功")
    check(r.get("cancelled"), "★ 标记为 cancelled —— 而不是笼统的'失败'")
    check("取消" in r["headline"], f"结论说明了是取消：{r['headline']}")
    check("next" in r, "告诉模型下一步该干嘛")


def test_run_pea_never_raises() -> None:
    section("⑦c ★★ `run_pea` 说「永不抛异常」，那就一次都不许抛")

    # ⚠️⚠️ 两个设计各自都说得通，合在一起就是 bug：
    #
    #      · `paths.guard_not_in_ginan` **故意抛 ValueError**
    #        （那里的注释："宁可让程序停下来"）
    #      · `run_pea` 的文档写着「**永不抛异常** —— 所有失败都变成
    #        RunResult 里的字段」
    #
    #    而调用方（`do_run_processing`）**没有 try** ——
    #    于是"工作目录设在 Ginan 安装目录里"这种**它自己主动拒绝**的情况，
    #    表现成一个 traceback。
    #
    #    ⚠️ 顺带一个更隐蔽的：原来两处"根本没跑起来"用的是
    #       `diag.diagnose("无法启动 Ginan：…")` ——
    #       而 `diagnose` 是**从 pea 的输出里认模式**的，喂它一句
    #       我们自己的话，它一条都认不出来 → 结论是那句笼统的
    #       「处理的结束状态不明确」。用户看到的是"不知道怎么了"，
    #       而事实是"程序压根没让它跑" —— 下一步动作完全不同。
    import os as _os
    import tempfile as _tf

    from ginan_agent.ginan import runner as _runner

    base = Path(_tf.mkdtemp(prefix="runpea_"))
    pea = base / "pea.exe"
    pea.write_bytes(b"MZ")
    cfg = Path(_tf.mkdtemp()) / "x.yaml"
    cfg.write_text("inputs: {}\n", encoding="utf-8")

    old = _os.environ.get("GINAN_AGENT_PEA")
    _os.environ["GINAN_AGENT_PEA"] = str(pea)
    try:
        # ⚠️ base 是"Ginan 安装目录"（pea 就住在那儿），
        #    所以往它下面设工作目录**会被拒绝**。
        try:
            r = _runner.run_pea(cfg, base / "workdir")
            crashed = ""
        except Exception as exc:  # noqa: BLE001
            r, crashed = None, f"{type(exc).__name__}: {exc}"

        check(not crashed, f"★★ 不抛异常（原来是 {crashed}）")
        if r is not None:
            check(not r.ok, "而是返回了一个失败的结果")
            check(r.handle is not None, "★ 连 handle 都有（外面要拿它显示'这次运行在哪'）")
            check(
                "拒绝" in r.diagnosis.headline,
                f"★★ 结论里**说清了是拒绝执行**（{r.diagnosis.headline[:40]}…）"
                "—— 原来是笼统的「处理的结束状态不明确」",
            )
            check(
                bool(r.diagnosis.errors and r.diagnosis.errors[0].what_to_do),
                "★ 而且给了「怎么办」",
            )
    finally:
        # ⚠️⚠️ 恢复的必须是**刚设的那个**变量名。
        #    我第一版写成了 `GINAN_AGENT_FAKE_PEA`（名字像，但是另一个）——
        #    于是 `GINAN_AGENT_PEA` **一直留着**，后面每一个用例
        #    都在用我那个临时 `pea.exe`，四个用例直接红了。
        #    **测试之间漏掉全局状态，会让失败看起来出现在毫不相干的地方。**
        if old is None:
            _os.environ.pop("GINAN_AGENT_PEA", None)
        else:
            _os.environ["GINAN_AGENT_PEA"] = old


def test_pea_env_does_not_touch_thread_counts() -> None:
    section("⑦d ★★ 不给 Ginan 限线程 —— 用户机器有几个核不归 app 管")

    from ginan_agent.ginan import environment

    # ⚠️⚠️ 这一条守的是用户提的那句话：**「别出现一个核心干活、多核围观」**。
    #
    #    原来 `pea_env()` 设这两个：
    #        OPENBLAS_NUM_THREADS=1 / GOTO_NUM_THREADS=1
    #    理由是「OpenBLAS 和 OpenMP 嵌套会挂起，按住多余那个池」。
    #    理由不算错，但**没人量过代价**。实测（2026-09-25，同一份 87 站配置、
    #    同一台机器 i5-14600KF / 20 个逻辑核，差异只有那一个变量）：
    #
    #        设了（1 线程）  进程 21 线程  →  平均只占 **1.01** 个核
    #                                        处理 20.6/19.7/18.6/18.4 → 69.6/77.3/79.5 秒
    #        不设            进程 40 线程  →  平均占 **8.32** 个核
    #                                        处理 12/11/12/12 → 43/43/44/45 秒   ← 快 1.6~1.7 倍
    #
    #    ⚠️ 21 线程那一列正是问题所在：Ginan 那 20 个 OMP 线程**建了却没干活**，
    #       所以 20 个核在旁边看着 1 个核跑。
    #
    #    ⚠️⚠️ 但**也不能改成「按核数自动设置」** —— 用户的机器有多少核、
    #       该开多少线程，是库和 Ginan 的事。app 替它决定，用户换一台机器
    #       这个决定就可能又错了。所以这一条守的是「**一个都不许设**」，
    #       不是「设成对的数」。
    saved = {
        k: os.environ.pop(k, None)
        for k in ("OPENBLAS_NUM_THREADS", "GOTO_NUM_THREADS")
    }
    try:
        env = environment.pea_env()
        for name in ("OPENBLAS_NUM_THREADS", "GOTO_NUM_THREADS"):
            check(
                name not in env,
                f"★ {name} **不许设** —— 设了会把并行砍到只剩 1 个核",
            )

        # ---- 对照：用户**自己**设了就要照传 ----
        #
        # ⚠️ 不查这一条的话，一个"什么都不传、把环境清空"的实现也能通过上面。
        #    而那是另一回事：用户的设置是他的选择，不是我们替他做的。
        os.environ["OPENBLAS_NUM_THREADS"] = "3"
        env2 = environment.pea_env()
        check(
            env2.get("OPENBLAS_NUM_THREADS") == "3",
            "★ 用户自己设的值要照传（我们只不**主动**设，不覆盖用户的）",
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_detects_single_core_run() -> None:
    section("⑦f ★★ 只用一个核在跑时，要**说出来**（但**不许打断**）")

    # ⚠️⚠️ 由来（用户点的）：「处理中如果发现只有一个 cpu 工作的情况记得中断检查」。
    #
    #    这个坑本项目真踩过：`pea_env()` 曾设 `OPENBLAS_NUM_THREADS=1`，
    #    把 Ginan 从 20 核压到 1 核，一整轮 POD 慢 1.6~1.7 倍，
    #    而且**跑完才发现** —— 那几个小时已经没了。
    #
    #    ⚠️ 判据是**占用核数**（CPU 秒增量 ÷ 墙钟），不是线程数：
    #       数线程会看到 40 个，而实际只有 1 个核在干活。
    #
    # ⚠️⚠️ **两个方向都要测**，否则守不住：
    #      · 只测"单核要报" ⇒ 一个**永远报警**的实现在这儿是绿的，
    #        而那种实现比不报更糟（狼来了）。
    #      · 只测"多核不报" ⇒ 一个**永远沉默**的实现是绿的，等于没做。
    import os as _os
    import tempfile as _tf

    from ginan_agent.ginan import runner as _runner

    td = Path(_tf.mkdtemp(prefix="cores_"))

    # ⚠️ 假 pea：靠**环境变量**决定烧几个核（不改 `run_pea` 的签名去开后门），
    #    并按真 pea 的样子打历元行（**前向**那种，带 `#`）。
    n_burn = 4 if (_os.cpu_count() or 1) >= 4 else 1
    child = td / "fake_pea.py"
    # ⚠️⚠️ 烧 CPU 必须用**会放 GIL 的东西**（`hashlib`），不能用 `x += 1`：
    #     纯 Python 的紧循环**多线程也不并行** —— 第一版就是这么写的，
    #     于是"4 核"那一半实测也只得 1.0 个核，对照**假通过**。
    #     ⇒ 夹具不贴真实，那条路的 bug 就没有守卫。
    child.write_text(
        "import hashlib, os, sys, threading, time\n"
        "n = 1 if os.environ.get('FAKE_PEA_BURN') == '1' else %d\n"
        "stop = time.monotonic() + 8\n"
        "def burn():\n"
        "    h = hashlib.sha256()\n"
        "    buf = b'x' * 200000\n"
        "    while time.monotonic() < stop:\n"
        "        for _ in range(20): h.update(buf)\n"
        "for _ in range(n): threading.Thread(target=burn, daemon=True).start()\n"
        "for i in range(1, 300):\n"
        "    print(f'Processed epoch #{i} - GPS time: 2022 03 01 0 0 {i %% 60}')\n"
        "    sys.stdout.flush(); time.sleep(0.02)\n" % n_burn,
        encoding="utf-8",
    )
    (td / "x.yaml").write_text("inputs: {}\n", encoding="utf-8")

    seen: list[str] = []

    class Ctx:
        cancelled = False

        def progress(self, m, fraction=None) -> None:  # noqa: ARG002
            seen.append(m)

    # ---- ⓪ **先验出厂默认值本身** ----
    #
    # ⚠️⚠️ 这一段是补上来的，因为**变异测试暴露了一个洞**：
    #     下面为了跑得快，会把四个阈值临时改掉 ——
    #     于是"出厂默认是多少"就**再也没有人看**了。
    #     实测：把 `CORES_SUSPICIOUS` 改成 0.0（等于**永远不报警**）、
    #     把 `CORES_MIN_CPU` 改成 999（等于小机器上永远不报），
    #     **整条守卫照样全绿。**
    #
    #     ⇒ 判据不是"跑起来对不对"，是"**出厂的那个数落在合理区间里**"。
    check(
        1.0 <= _runner.CORES_SUSPICIOUS <= 2.0,
        f"★ 可疑线在 1.0~2.0 之间（出厂 {_runner.CORES_SUSPICIOUS}）"
        "—— 低于 1.0 等于永不报警，高于 2.0 等于多核也报",
    )
    check(
        2 <= _runner.CORES_MIN_CPU <= 8,
        f"★ 机器核数门槛在 2~8 之间（出厂 {_runner.CORES_MIN_CPU}）"
        "—— 太高等于小机器上永不报，太低等于小机器上瞎报",
    )
    # ⚠️ 这条对应的教训是本仓库自己的：「短窗口的读数不可信」
    #    （`ginan-facts` 七点五 ③ —— 26 秒/历元 和 4.7 秒/历元 是同一轮）。
    check(
        10.0 <= _runner.CORES_MIN_SECONDS <= 300.0,
        f"★ 采样窗口在 10~300 秒之间（出厂 {_runner.CORES_MIN_SECONDS}）"
        "—— 太短会被启动开销和 I/O 带偏",
    )
    check(
        2 <= _runner.CORES_MIN_EPOCHS <= 50,
        f"★ 窗口内至少跨 2 个历元（出厂 {_runner.CORES_MIN_EPOCHS}）",
    )

    # ⚠️ 把窗口缩到零点几秒 —— 否则这一条要跑 40 秒 × 2。
    #    这**正是**把阈值做成模块常量的理由（见 runner 里那段说明）。
    keep = (_runner.CORES_MIN_SECONDS, _runner.CORES_MIN_EPOCHS,
            _runner.CORES_SUSPICIOUS, _runner.CORES_MIN_CPU)
    old_fake = _os.environ.get("GINAN_AGENT_FAKE_PEA")
    old_burn = _os.environ.get("FAKE_PEA_BURN")

    def _run(tag: str, burn: str) -> tuple[list[str], object]:
        _os.environ["FAKE_PEA_BURN"] = burn
        seen.clear()
        r = _runner.run_pea(td / "x.yaml", td / tag, ctx=Ctx(), timeout=60)
        return [m for m in seen if "个核" in m], r

    try:
        _runner.CORES_MIN_SECONDS = 0.6
        _runner.CORES_MIN_EPOCHS = 3
        _runner.CORES_MIN_CPU = 4
        _runner.CORES_SUSPICIOUS = 1.5
        _os.environ["GINAN_AGENT_FAKE_PEA"] = str(child)

        if (_os.cpu_count() or 1) < 4:
            # ⚠️ 这台机器本来就 4 核以下 ⇒ 检测**故意**不报（1 个核是正常的）。
            #    那就把判据反过来验：多核不报 + 少核不报，仍然是"说得出话"的。
            print("      ⚠️ 这台机器 <4 核，跑不了「单核要报」那一半，只验反向")
            noisy, _ = _run("run_multi", "0")
            check(not noisy, "★★ 核数够时不乱报警（这台机器就这么点核）")
            return

        # ---- ① 单核：**要报**，而且那一轮**不许被打断** ----
        noisy1, r1 = _run("run_single", "1")
        check(bool(noisy1), f"★★ 单核跑时报了（拿到 {noisy1[:1]}）")
        check(r1.ok, "★★ **而且那一轮没有被打断** —— 只报告，不替用户决定")
        # ⚠️ 光说"只有一个核"没用 —— 要**能照着去查**。
        check(
            any("OPENBLAS" in m for m in noisy1),
            "★ 那句话里指出了**去哪儿查**（OPENBLAS_NUM_THREADS）",
        )

        # ---- ② 对照：多核**不许**报 ----
        #
        # ⚠️ 这一半是必须的：只测 ① 的话，一个**永远报警**的实现在这里是绿的
        #    —— 而那种实现比不报更糟（狼来了，用户学会忽略）。
        noisy4, r4 = _run("run_multi", "0")
        check(not noisy4, f"★★ 对照：4 个核在跑时**不报**（拿到 {noisy4[:1]}）")
        check(r4.ok, "★ 对照那一轮正常结束")
    finally:
        (_runner.CORES_MIN_SECONDS, _runner.CORES_MIN_EPOCHS,
         _runner.CORES_SUSPICIOUS, _runner.CORES_MIN_CPU) = keep
        for _k, _v in (("GINAN_AGENT_FAKE_PEA", old_fake), ("FAKE_PEA_BURN", old_burn)):
            if _v is None:
                _os.environ.pop(_k, None)
            else:
                _os.environ[_k] = _v


def test_pea_always_gets_config_flag() -> None:
    section("⑦e ★★ 跑 pea 必须用 `--config` —— 位置参数会被**静默忽略**")

    # ⚠️⚠️ 这条守卫的由来（`ginan-facts` 七点二十，实测）：
    #
    #         pea --config <路径>     →  1.8 秒出结果
    #         pea <路径>（位置参数）  →  **不报错**，用默认配置进无限历元循环，
    #                                    日志 20 MB/秒往外冒，`-n 3` 也拦不住
    #
    #    产品侧一直是写对的（`runner.py` 那句 `cmd = [exe, "--config", ...]`）——
    #    所以这条守卫守的不是"现在坏着"，而是**"改坏了不会有任何提示"**：
    #    位置参数那条路**退出码是 0**、产物是空的、日志看着还挺像在干活。
    #    ⇒ 一个"把 `--config` 改成位置参数"的重构，靠人眼看不出来。
    #
    #    ⚠️ 判据不看 `cmd` 变量，而看**子进程真的收到了什么** ——
    #       用仓库自己的假 pea 把 argv 打出来（`find_pea_for_run` 认得那个环境变量）。
    import os as _os
    import tempfile as _tf

    from ginan_agent.ginan import runner as _runner

    td = Path(_tf.mkdtemp(prefix="peacfg_"))
    argv_file = td / "argv.json"
    child = td / "fake_pea.py"
    child.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path({str(argv_file)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "print('Processed epoch #1 - GPS time: 2022 03 01 0 0 1')\n",
        encoding="utf-8",
    )
    cfg = td / "x.yaml"
    cfg.write_text("inputs: {}\n", encoding="utf-8")

    old = _os.environ.get("GINAN_AGENT_FAKE_PEA")
    _os.environ["GINAN_AGENT_FAKE_PEA"] = str(child)
    try:
        _runner.run_pea(cfg, td / "run", timeout=60)
    finally:
        if old is None:
            _os.environ.pop("GINAN_AGENT_FAKE_PEA", None)
        else:
            _os.environ["GINAN_AGENT_FAKE_PEA"] = old

    if not argv_file.is_file():
        check(False, "★ 假 pea 没拿到 argv（拿不到就没法判）")
        return
    import json as _json

    argv: list[str] = _json.loads(argv_file.read_text(encoding="utf-8"))

    check("--config" in argv, f"★★ 子进程的 argv 里有 `--config`（拿到 {argv}）")
    if "--config" in argv:
        i = argv.index("--config")
        got = argv[i + 1] if i + 1 < len(argv) else None
        check(
            got is not None and Path(got).name == "x.yaml" and Path(got).is_absolute(),
            f"★★ 而且紧跟的是**绝对路径**的配置（拿到 {got!r}）"
            "—— 相对路径会跟着 CWD 漂",
        )

    # ⚠️ **对照**：配置文件**不许**再作为位置参数出现一次。
    #    光查"有 --config"挡不住"两个都传"——而那种写法下
    #    位置参数那份仍然会被当成别的东西。
    check(
        sum(1 for a in argv if a.endswith(".yaml")) == 1,
        f"★★ 配置路径在 argv 里只出现一次（拿到 {[a for a in argv if a.endswith('.yaml')]}）",
    )


def test_silent_pea_can_still_be_cancelled() -> None:
    section("⑦b ★★ 子进程**一声不吭**时，取消和超时也必须有效")

    # ⚠️⚠️ 上面 ⑦ 那条测不到这一条 —— 它用的假 pea **每个历元都打印**，
    #    所以读管道那个循环永远有东西可读，循环体里的检查一定能跑到。
    #
    #    而真 Ginan 会**一声不吭**：初始化、加载模型、以及那个
    #    **OpenBLAS 挂起**，都是长时间无输出。
    #
    #    ⚠️ 而 `run_pea` 的 timeout 在文档里写的用途**正是**
    #       「防止无限挂起（比如那个 OpenBLAS 的坑）」。
    #       OpenBLAS 挂起恰好就是一声不吭 —— **它挡不住它声称要挡的事**。
    #
    #    实测（修之前：子进程只 sleep 8 秒；timeout=1.0；0.5 秒时取消）：
    #        耗时 8.0 秒  cancelled=False  timed_out=False  ok=True
    #
    #    根因：两个检查都写在 `for line in proc.stdout` 的**循环体里**，
    #    而那是**阻塞读** —— 子进程不说话，循环第一行就永久卡住。
    #
    # ⚠️ 判据必须是**耗时**，不能只看 flag：
    #    "最后标记成取消"和"当时就停下来"是两件事 ——
    #    原来那版最后也会结束（子进程自己跑完了），只是**没听用户的**。
    import os as _os
    import time as _time

    from ginan_agent.ginan import runner as _runner

    def silent_child(td: Path, seconds: float = 8.0) -> Path:
        p = td / "pea_silent.py"
        p.write_text(f"import time\ntime.sleep({seconds})\n", encoding="utf-8")
        (td / "x.yaml").write_text("inputs: {}\n", encoding="utf-8")
        return p

    class Ctx:
        def __init__(self) -> None:
            self.cancelled = False

        def progress(self, m, fraction=None) -> None:  # noqa: ARG002
            pass

    old_pea = _os.environ.get("GINAN_AGENT_FAKE_PEA")
    try:
        # ---- ① 取消 ----
        td = Path(tempfile.mkdtemp(prefix="silent_"))
        _os.environ["GINAN_AGENT_FAKE_PEA"] = str(silent_child(td))
        ctx = Ctx()
        threading.Timer(0.5, lambda: setattr(ctx, "cancelled", True)).start()
        t0 = _time.monotonic()
        r = _runner.run_pea(td / "x.yaml", td / "run", ctx=ctx, timeout=30)
        took = _time.monotonic() - t0
        check(r.cancelled, f"★ 取消被记下来了（cancelled={r.cancelled}）")
        check(
            took < 4,
            f"★★ 而且是**当时就停**，不是等子进程自己跑完"
            f"（实测 {took:.1f} 秒，修之前是 8.0 秒）",
        )

        # ---- ② 超时 ----
        td2 = Path(tempfile.mkdtemp(prefix="silent2_"))
        _os.environ["GINAN_AGENT_FAKE_PEA"] = str(silent_child(td2))
        t0 = _time.monotonic()
        r2 = _runner.run_pea(td2 / "x.yaml", td2 / "run", timeout=1.0)
        took2 = _time.monotonic() - t0
        check(r2.timed_out, f"★ 超时被记下来了（timed_out={r2.timed_out}）")
        check(
            took2 < 4,
            f"★★ 超时也**真的会到点**（实测 {took2:.1f} 秒，修之前是 8.0 秒）",
        )

        # ---- ③ 反过度修正：正常说话的子进程一点没变 ----
        #
        #     ⚠️ 改成"线程读 + 队列取"最容易顺手把逐行处理弄丢：
        #        进度不报了、日志不写了、或者把正常结束也当成超时。
        td3 = Path(tempfile.mkdtemp(prefix="talking_"))
        child = td3 / "pea_talk.py"
        # ⚠️⚠️ 假 pea 必须照着**真 pea** 造：**每个历元打印两遍**。
        #
        #    真输出长这样（实测的真日志里，2880 历元那次）：
        #        前向解： Processed epoch #1234 - GPS time: ...   ← 2880 行
        #        RTS 反向：Processed epoch - 2022-03-01 ...       ← 2879 行
        #
        #    ⚠️ 我第一版只打印了前向那种 —— 于是"两处都数会翻倍"这个
        #       失效方式**测不出来**（变异测试里把子串判断改回去，全绿）。
        #       **夹具不贴真实，那条路的 bug 就没有守卫。**
        child.write_text(
            "import time, sys\n"
            "for i in range(1, 13):\n"
            "    print(f'Processed epoch #{i} - GPS time: 2022 03 01 0 0 {i}')\n"
            "    print(f'Processed epoch - 2022-03-01 00:00:{i:02d}.01 (took 0.001 seconds)')\n"
            "    sys.stdout.flush(); time.sleep(0.01)\n",
            encoding="utf-8",
        )
        (td3 / "x.yaml").write_text("inputs: {}\n", encoding="utf-8")
        _os.environ["GINAN_AGENT_FAKE_PEA"] = str(child)

        seen: list[str] = []

        class Ctx3(Ctx):
            def progress(self, m, fraction=None) -> None:  # noqa: ARG002
                seen.append(m)

        r3 = _runner.run_pea(td3 / "x.yaml", td3 / "run", ctx=Ctx3(), timeout=30)
        check(r3.ok, "★ 正常跑完的子进程仍然算成功")

        counts = [
            int(m.split("已完成 ")[1].split(" 个历元")[0])
            for m in seen if "已完成" in m
        ]
        # ⚠️ 进度只在第 1 个和每 10 个历元报一次，所以 12 个历元的最后一报是 10。
        #    翻倍的话最后一报会是 20 —— 这就是判别点。
        check(
            bool(counts) and max(counts) == 10,
            f"★★ 播报的最大值是 10（拿到 {counts}）"
            "—— 20 就是把前向和 RTS 两遍都数了（**翻倍**）",
        )
        check(
            any("已完成 1 个历元" in m for m in seen),
            f"★ 第一条进度是「1 个历元」（拿到 {seen[:3]}）",
        )

        logs = list((td3 / "run").glob("*.log"))
        lines = logs[0].read_text(encoding="utf-8").splitlines() if logs else []
        check(
            len(lines) == 24,
            f"★★ 日志照常逐行落盘（{len(lines)} 行 / 子进程打印 24 行 = 12 历元 × 2）"
            "—— 现在文件只开一次，不能因此漏写",
        )
    finally:
        if old_pea is None:
            _os.environ.pop("GINAN_AGENT_FAKE_PEA", None)
        else:
            _os.environ["GINAN_AGENT_FAKE_PEA"] = old_pea


# =============================================================================
# ⑨ ★ "计划里有" ≠ "磁盘上有"
# =============================================================================


def _fake_fetch_report(ws, fail_kinds: set) -> None:
    """造一份假的下载报告：指定类别失败，其余成功。

    ⚠️ 为什么不真的去下载？
       自检要**零费用、不联网、不调用模型**。而且真下载也没法稳定复现
       "某个特定类别失败"这种局面 —— 那取决于当天的网络。
       **用假对象把异常路径逼出来**，正是它存在的理由。
    """
    from ginan_agent.ginan.fetch import DownloadResult, FetchReport

    results = []
    for f in ws.plan.files:
        if f.kind in fail_kinds:
            # ⚠️⚠️ **成功的和失败的都要带 kind** —— 这一点是后来改对的。
            #
            #    这里原来写着：「失败的记录里 kind 是空的，因为 kind 是
            #    下载后按真实内容识别出来的类型，失败时压根没有内容可识别。
            #    照实模拟，好让代码必须靠 filename 去对。」
            #
            #    ⚠️ 那条理由**是错的**，而且是**双重**错的：
            #       ① `ident.kind` 认不出来时返回 `"unknown"`，**不是空串**
            #       ② 更根本地：`kind` 该填的是**计划里的那一类**，
            #          那是"下载前就知道"的事，和下载成没成功无关
            #
            #    而生产代码里 9 个失败分支漏填了 `kind`，只有成功那个填了 ——
            #    夹具照着这个不一致去模拟，于是**把一个 bug 固化成了"真实"**。
            #
            #    👉 判据：**夹具要照着"应该是怎样"造，不是照着"现在是怎样"造。**
            #       照现状造的话，现状里那个 bug 就永远测不出来。
            results.append(
                DownloadResult(filename=f.filename, ok=False, kind=f.kind,
                               detail="测试：模拟超时")
            )
        else:
            results.append(
                DownloadResult(
                    filename=f.filename, ok=True, real_name=f.filename,
                    kind=f.kind, size=1024, source="bkg",
                )
            )
    ws.fetched = FetchReport(results=results)


def test_failed_download_not_in_config() -> None:
    section("⑨ ★ 下载失败的文件，绝不能写进配置")

    # ⚠️⚠️ 这是 bug② 的回归守卫。
    #
    #    原来的写法是二选一：有下载结果就用、没有就退回计划名。
    #    但"没有"包含**两种完全不同的情况**：
    #
    #        ① 还没开始下    → 退回计划名是对的
    #        ② 下过但全失败  → 退回计划名**是错的**
    #
    #    情况②下写出来的配置里会有一个**磁盘上根本不存在的文件**，
    #    Ginan 报"缺文件" —— 而那个错误离根因（下载失败）非常远。
    r = Runner()
    r("plan_run", stations=["DCMS"], day="2026-01-01")
    _fake_fetch_report(r.ws, fail_kinds={"nav"})

    cfg = r("generate_config")

    check("error" not in cfg, "一个文件失败不影响整体 —— 配置照样生成")
    check(
        cfg["inputs"]["nav"] == [],
        f"★ 失败的 nav **没有**被写进配置（实际：{cfg['inputs']['nav']}）",
    )
    check(bool(cfg["inputs"]["orbits"]), "成功的文件照常写进去")

    # ⚠️ 只排除、不说明 = 用户一路跑到失败才发现少东西。
    check("missing" in cfg, "★ 明确报告了'缺了什么'")
    if "missing" in cfg:
        check("nav" in cfg["missing"]["kinds"], "缺的是 nav")
        check(
            "广播星历" in cfg["missing"]["what"],
            f"用**人话**说缺什么：{cfg['missing'].get('what')}",
        )
        check(bool(cfg["missing"].get("what_to_do")), "★ 给了'怎么办'")


def test_config_before_download_keeps_plan_names() -> None:
    section("⑨b 还没下载时，仍然用计划里的名字（这个行为**不能**被一起改掉）")

    # ⚠️ 修 bug② 时很容易"顺手改过头"：把退回计划名这条路径整个删掉。
    #    但**那条路是对的** —— 用户可能先看配置，再自己去补文件。
    #
    #    所以这条断言守着"另一侧"：**没下载过 ≠ 下载失败。**
    r = Runner()
    r("plan_run", stations=["ALIC"], day="2026-09-02")

    cfg = r("generate_config")
    check("error" not in cfg, "还没下载也能生成配置")
    check(
        bool(cfg["inputs"]["nav"]),
        "★ 还没下载时，配置里用的是计划里的文件名",
    )
    check(
        "missing" not in cfg,
        "★ '还没下载'**不算**缺失 —— 两种情况的处理必须不同",
    )


# =============================================================================
# ⑩ ★ 不能用的档位
# =============================================================================


def test_standard_profile_requires_real_files() -> None:
    section("⑩ ★ 「完整」档：能选，但**文件不在就不许生成配置**")

    # ⚠️⚠️ 这一节的前身是「不能用的档位：既不能选，硬传也要拦住」——
    #     那时 `standard` 整个没做完（下载功能不存在），所以它**不在可选项里**。
    #
    #     2026-09-20 把下载做完了，于是判断变成两半：
    #
    #       ① 功能做完了没有    → 这份元组（config.AVAILABLE_PROFILES）
    #       ② 这台机器上文件够不够 → do_generate_config 的运行时关卡
    #
    #     ⚠️ **两条都必须过**，少任何一条，"能选"就又会变成一句做不到的承诺。
    #        而这一次的失败模式比上次更阴 —— 见下面 ③。
    from ginan_agent.ginan import fetch as gfetch

    r = Runner()
    r("plan_run", stations=["ALIC"], day="2026-09-02")

    # ---- ① 现在可以选了，而且描述里给的是**真数字** ----
    tool = r.reg.get("generate_config")
    check(tool is not None, "拿到了 generate_config 工具")
    if tool:
        prop = tool.parameters.get("properties", {}).get("profile", {})
        enum = prop.get("enum", [])
        check("standard" in enum, f"★ 「完整」档现在可以选了（{enum}）")
        check("reduced" in enum, "「基础」档仍然可以选")

        desc = json.dumps(prop, ensure_ascii=False)
        # ⚠️ 那个"约 200MB"从来没有任何依据，而且它**误导过模型**去对用户
        #    承诺一次不会发生的下载。现在写的是量出来的数。
        check(
            "200MB" not in desc and "200 MB" not in desc,
            "★ 描述里不再有那个没有依据的'200MB'",
        )
        check("17 MB" in desc, "★ 写的是实测出来的 17 MB")
        check("只下一次" in desc, "★ 而且说清了它是一次性的")

    # ---- ② 文件齐的时候：正常生成 ----
    real_missing = gfetch.missing_tables
    gfetch.missing_tables = lambda *a, **k: []          # 假装都下好了
    try:
        ok = r("generate_config", profile="standard")
        check("error" not in ok, "文件齐了 → 正常生成配置")
        check(
            ok.get("profile_note") is not None,
            "而且带上了档位说明（那些代价要转述给用户）",
        )
    finally:
        gfetch.missing_tables = real_missing

    # ---- ③ ★ 文件缺的时候：必须**拦住** ----
    #
    # ⚠️⚠️ 这是这一节最重要的断言，因为不拦的后果**完全静默**：
    #
    #     配置里会把模型设置写全（troposphere/tides/ionospheric…），
    #     而文件不在磁盘上 → Ginan **不报错**，它只是没去打开那些文件 →
    #     算出来的结果和「基础」档**一模一样**，用户以为用了完整档。
    #
    #     **没有任何地方会告诉他不是。**
    #
    # ⚠️ 这里把 missing_tables 换成假的（不去动用户真的 data/），
    #    测的是**关卡逻辑**；`missing_tables` 本身怎么判断由 ⑩b 测。
    fake = [catalog.plan_tables()[0], catalog.plan_tables()[1]]
    gfetch.missing_tables = lambda *a, **k: fake
    try:
        bad = r("generate_config", profile="standard")
        check("error" in bad, "★ 文件缺了 → 拒绝生成配置")
        msg = bad.get("error", "")
        check("with_tables" in msg, "★ 错误里说了**怎么补**（fetch_data with_tables）")
        check("只下一次" in msg, "★ 说了它是一次性的（不然用户以为每次都要下）")
        check("基础" in msg, "★ 明确不许'悄悄改用基础档糊弄过去'")
    finally:
        gfetch.missing_tables = real_missing


def test_standard_config_really_carries_the_tables() -> None:
    section("⑩a ★★ 完整档生成的配置里，**真的有**那几个键")

    # ⚠️⚠️ 这一条是**这一整轮里最重要的守卫**，因为漏掉它的后果完全静默。
    #
    #     下完文件之后我以为就完事了。实测（拿"有 tables"和"没 tables"
    #     各跑一遍、把 pea 的日志逐行比）才发现：
    #
    #        pea 的 Configuration Summary 里**一个 tables 文件都没有** ——
    #        它压根没去打开过。那 45 MB 静静躺在磁盘上，一点用没有。
    #
    #     ⚠️ 而两次运行的 **exit code 都是 0**、POS 文件都正常生成。
    #        只看"跑通了没有"会得出完全错误的结论。
    #
    #     ⚠️ 更值得记的是：上面 ⑩c 那条守卫（清单和配置键一一对应）
    #        **拦不住这个 bug** —— 它查的是 `catalog.table_inputs()`
    #        这个**函数**，而不是 `build_config()` 的**产物**。
    #        函数是对的，而被调用的一方把结果丢掉了。
    #
    #        👉 **"函数能算对"和"产物里有"是两件事。**
    #           要守的是产物。
    import yaml

    from ginan_agent.ginan import fetch as gfetch

    r = Runner()
    r("plan_run", stations=["ALIC"], day="2026-09-02")

    real_missing = gfetch.missing_tables
    gfetch.missing_tables = lambda *a, **k: []          # 假装文件都齐
    try:
        out = r("generate_config", profile="standard")
    finally:
        gfetch.missing_tables = real_missing

    check("error" not in out, "完整档配置生成成功")
    cfg_path = out.get("config_path")
    check(bool(cfg_path), "拿到了配置路径")
    if not cfg_path:
        return

    cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    inp = cfg.get("inputs", {})

    # ---- ① 八个键，一个都不能少 ----
    check("igrf_files" in inp, "★ inputs.igrf_files 有")
    check("planetary_ephemeris_files" in inp, "★ inputs.planetary_ephemeris_files 有")
    check(
        "gpt_25.grd" in str(inp.get("troposphere", {})),
        "★ inputs.troposphere.gpt2grid_files 指到了 gpt_25.grd",
    )
    tides = inp.get("tides", {})
    for k in ("ocean_tide_loading_blq_files", "atmos_tide_loading_blq_files",
              "ocean_pole_tide_loading_files", "ocean_tide_potential_files"):
        check(k in tides, f"★ inputs.tides.{k} 有")

    # ---- ② 路径必须带 tables/ 前缀 ----
    #     ⚠️ 不带前缀的话 Ginan 会在 inputs_root 那一层找，找不到 ——
    #        而且**不报错**。前缀是契约的一部分。
    blq = tides.get("ocean_tide_loading_blq_files", [])
    check(
        any(x.startswith("tables/") for x in blq),
        f"★ 路径带 tables/ 前缀（实际：{blq}）",
    )

    # ---- ③ snx_files 是**合并**，不是覆盖 ----
    snx = inp.get("snx_files", [])
    check(
        any("igs_satellite_metadata" in str(x) for x in snx),
        f"★ snx_files 里原来的 igs_satellite_metadata 还在（没被覆盖掉）",
    )
    check(
        sum(1 for x in snx if "yaw" in str(x)) == 3,
        f"★ 而且三个偏航姿态文件也并进去了（实际 {snx}）",
    )

    # ---- ④ 对流层模型**只能有 gpt2** ----
    #
    #     ⚠️ vmf3 要的是维也纳那边**每天生成**的格网（`VMF3_YYYYDDD.H00`），
    #        不在 tables 桶里，我们没有任何办法拿到。
    #        加进去的话 pea 会在读 vmf 文件时炸，而那个报错离"档位配错了"很远。
    #        Ginan 官方自己的默认配置写的也是只有 gpt2。
    #     ⚠️ 路径是 `receiver_options.global.models.troposphere.models` ——
    #        第一版我照着 `processing_options` 猜，断言拿到的是空列表，
    #        于是"没有 vmf3"那条**假通过**了（空列表里当然没有 vmf3）。
    #        **一条永远为真的断言比没有断言更坏** —— 它看起来在守着什么。
    rec = cfg.get("receiver_options", {}) or {}
    glob = rec.get("global", {}) or {}
    mods = glob.get("models", {}) or {}
    tropo = mods.get("troposphere", {}) or {}
    models = tropo.get("models", [])

    check(bool(models), f"★ 取到了对流层模型列表（实际 {models}）—— 取不到的话下面那条是假通过")
    check("gpt2" in models, f"★ 对流层用了 gpt2（实际 {models}）")
    check("vmf3" not in models, "★★ **没有** vmf3 —— 它要的格网文件拿不到")


def test_basic_profile_stays_lean() -> None:
    section("⑩a2 ★ 「基础」档不许被顺手污染")

    # ⚠️ 基础档那条路是**验过能跑的**（有实测数字）。加 tables 的时候
    #    很容易图省事把那段无条件执行 —— 那样基础档也会引用模型文件，
    #    而它压根不打算下那些文件。
    import yaml

    r = Runner()
    r("plan_run", stations=["ALIC"], day="2026-09-02")
    out = r("generate_config")            # 默认就是 reduced
    check("error" not in out, "基础档配置生成成功")

    cfg = yaml.safe_load(Path(out["config_path"]).read_text(encoding="utf-8"))
    inp = cfg.get("inputs", {})
    for k in ("igrf_files", "planetary_ephemeris_files", "troposphere", "tides"):
        check(k not in inp, f"★ 基础档的 inputs 里**没有** {k}")

    snx = inp.get("snx_files", [])
    check(
        not any("yaw" in str(x) for x in snx),
        "★ 基础档的 snx_files 里没有偏航姿态文件（它不需要）",
    )


def test_obs_listing_goes_by_content_not_extension() -> None:
    section("⑩d ★★ 列观测数据必须**按内容**，不能按扩展名")

    # ⚠️⚠️ 用户实际撞上的 bug。
    #
    #     他的文件叫 `alic0600.22o` —— 这是 **RINEX 2 的标准短名**，
    #     完全正常。内容是观测数据（marker=ALIC00AUS、2022-03-01、可读）。
    #
    #     而 `_obs_in()` 里写着：
    #
    #         p.suffix.lower() in (".rnx", ".obs")
    #
    #     → 后缀不是 .rnx → **被滤掉** → 工具报"收件箱是空的" →
    #       模型对用户说"你还没放过观测文件"，然后问他要**测站和日期** ——
    #       而那两个答案就在文件的**头和名字里**。
    #
    #     ⚠️ 最值得记的是：它和这个项目的**头号原则正好相反**。
    #        `identify.py` 开篇写着"识别靠内容，不靠文件名"，
    #        还有整整一节测试守着（连"文件名是假的"都专门测）。
    #        然后在**这一个函数**里，又悄悄按扩展名筛了一遍。
    #
    #        👉 **一个原则只要有一处不照做，那一处就是全部。**
    #           别处都按内容判断，不能成为这一处不这么做的理由。
    #
    #     ⚠️ 同一处还藏着第二个 bug：那个过滤器放行了**广播星历** ——
    #        `BRDC00IGS_*.rnx` 也是 .rnx，于是它被当成"可用的观测数据"
    #        列了出来。实测那份 6 个"观测文件"里有 4 个其实是导航文件。
    import tempfile

    from ginan_agent.agent.ginan_tools import _obs_in
    from tests.fakes import heads as _h

    obs_head = _h.REAL_HEADS["obs"]

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        # ① RINEX 2 短名 —— 就是要测的这一个
        (d / "alic0600.22o").write_bytes(
            obs_head + b"ALIC00AUS" + b" " * 51 + b"MARKER NAME\n"
            + b" " * 60 + b"END OF HEADER\n"
        )
        # ② RINEX 3 常规名（原来就能过，防回归）
        (d / "ALIC00AUS_R_20220600000_01D_30S_MO.rnx").write_bytes(obs_head)
        # ③ 广播星历 —— **绝不能被当成观测数据**
        (d / "BRDC00IGS_R_20260010000_01D_MN.rnx").write_bytes(
            _h.REAL_HEADS["nav"]
        )
        # ④ 完全不是数据的
        (d / "读我.txt").write_bytes(b"hello")

        got = {x["file"] for x in _obs_in(d)}

        check(
            "alic0600.22o" in got,
            "★ RINEX 2 短名（.22o）**被列出来了** —— 这就是那个 bug",
        )
        check(
            "ALIC00AUS_R_20220600000_01D_30S_MO.rnx" in got,
            "常规 .rnx 名字仍然列得出来（防回归）",
        )
        check(
            "BRDC00IGS_R_20260010000_01D_MN.rnx" not in got,
            "★★ 广播星历**不**在观测数据里（它是导航文件，原来被误列）",
        )
        check("读我.txt" not in got, "不是数据的文件不在里面")

        # ⚠️ 而且**判据本身**不能是扩展名 —— 直接钉住源码，
        #    防止有人图省事把它改回去。
        #
        # ⚠️⚠️ 这里**必须查 AST，不能查源码文本** —— 踩过三次了：
        #     第一次是 "ginan-theme"、第二次是这里、第三次还是这里。
        #     被查的那段源码里**引用了当初那个 bug**（讲解它为什么是 bug），
        #     于是按关键词查会命中**自己的说明**，守卫被自己的文档绊倒。
        #
        #     反过来更糟：想绕过它，改一下注释就行 ——
        #     **一条能靠改注释绕过的守卫，等于没有守卫。**
        #
        #     👉 判据要落在**代码**上：AST 里有没有 `.suffix` 这个属性访问。
        import ast
        import inspect as _inspect

        from ginan_agent.agent import ginan_tools as _gt
        from ginan_agent.ginan import fetch as _fetch

        # ⚠️⚠️ **清单要一起看** —— 这条 bug 出过**两次**：
        #
        #     ① `ginan_tools._obs_in`     —— 用户直接撞上的那次
        #     ② `fetch.find_local_obs`    —— **一字不差的第二份拷贝**，
        #        而且后果更重：它把「① 文件头里的测站名（权威）」
        #        整个挡在扩展名过滤**后面** → 用户那 `.22o` 永远不被 inspect
        #        → "本地已经有就不用下载"落空 → 转去网上找 → 404
        #        → **看起来像网络问题**。
        #
        # 👉 修①的时候写下的是「**一个原则只要有一处不照做，那一处就是全部**」。
        #    加这个清单，就是为了让"第三处"在**写下的那一刻**就变红。
        #
        # ⚠️ 新加一个"列出/查找观测数据"的函数时，**把它加到这里**。
        targets = [
            (_gt._obs_in, "ginan_tools._obs_in"),
            (_fetch.find_local_obs, "fetch.find_local_obs"),
        ]
        for fn_obj, label in targets:
            src = _inspect.getsource(fn_obj)
            fn = ast.parse(src.lstrip()).body[0]
            uses_suffix = any(
                isinstance(n, ast.Attribute) and n.attr == "suffix"
                for n in ast.walk(fn)
            )
            check(
                not uses_suffix,
                f"★ {label} 的**代码**里没有 .suffix 属性访问 —— 不许按扩展名筛",
            )
            check(
                "identify" in src,
                f"★ {label} 用的是 identify（按内容）",
            )


def test_claimed_data_is_not_called_cache() -> None:
    section("⑩e ★ 认领过的数据，不能被说成「程序下载的缓存」")

    # ⚠️ 认领是**复制**：用户的文件既在收件箱、又在数据目录（改了名）。
    #    不摘出来的话，同一份数据会**同时出现在两组里**，
    #    而"缓存"那组带着说明"不是用户放进来的" ——
    #    模型照着一转述就变成"程序之前在本地留了一份"。
    #
    #    ⚠️ 比重复更糟的是**自相矛盾**：
    #       "你放进来的：[无]" + "缓存里有 6 个"，
    #       而其中一个是用户自己的。他刚认领完，转头被告知"你还没放过数据"。
    # ⚠️⚠️ 这一节**必须是行为测试**，不能只查源码里有没有那个词。
    #
    #     第一版就是查源码的（"源码里有没有 mine_in_data"），
    #     变异测试当场戳穿了它：把
    #         cached = [c for c in cached_all if c["file"] not in mine_in_data]
    #     改成
    #         cached = cached_all
    #     —— **减法没了**，而它仍然是绿的（那个词还在上面几行）。
    #
    #     👉 和「完整档」那次一模一样的教训：
    #        **要守的是产物，不是函数。** 源码里有那个词，
    #        和"结果真的对"是两件事。
    import tempfile as _tf

    from ginan_agent.agent import ginan_tools as _gt
    from ginan_agent.ginan import inbox as ginbox
    from tests.fakes import heads as _h

    obs = _h.REAL_HEADS["obs"]

    def _obs(name: bytes) -> bytes:
        return obs + name + b" " * (60 - len(name)) + b"MARKER NAME\n"

    # 判据是同一个（claimed_pairs 的键就是 already_claimed）—— 顺手钉一下
    src = __import__("inspect").getsource(ginbox.already_claimed)
    check("claimed_pairs" in src, "★ already_claimed 就是 claimed_pairs 的键集合")

    with _tf.TemporaryDirectory() as td:
        inbox_dir = Path(td) / "inbox"
        data_dir = Path(td) / "data"
        inbox_dir.mkdir()
        data_dir.mkdir()

        # 用户放进来的，而且**已经认领过去**（内容一样、名字不同）
        (inbox_dir / "alic0600.22o").write_bytes(_obs(b"ALIC00AUS"))
        (data_dir / "ALIC.rnx").write_bytes(_obs(b"ALIC00AUS"))
        # 一个**真的**缓存（不是用户的，内容是另一份）
        (data_dir / "OTHER00XXX_R_20260010000_01D_30S_MO.rnx").write_bytes(
            _obs(b"OTHER00XXX")
        )

        real_i, real_d = _gt.gpaths.inbox_dir, _gt.gpaths.data_dir
        _gt.gpaths.inbox_dir = lambda: inbox_dir
        _gt.gpaths.data_dir = lambda: data_dir
        try:
            out = json.loads(_gt.do_list_local_obs())
        finally:
            _gt.gpaths.inbox_dir, _gt.gpaths.data_dir = real_i, real_d

        names = {x["file"] for x in out.get("你放进来的", [])}
        check("alic0600.22o" in names, "★ 用户那份**出现在「你放进来的」里**")

        claimed = out.get("已经认领到数据目录的", [])
        check(
            claimed == ["ALIC.rnx"],
            f"★ 说清了它已经认领到数据目录（实际 {claimed}）",
        )

        n_cache = (out.get("程序以前弄来的_缓存") or {}).get("数量", 0)
        check(
            n_cache == 1,
            f"★★ 缓存里**只剩那个真的缓存**（数出来 {n_cache} 个）—— "
            f"用户自己那份被摘掉了。数出 2 就是减法没了：同一份数据会同时"
            f"出现在两组里，而模型会照着「缓存」那组说「程序之前下载的」。",
        )


def test_missing_tables_checks_the_disk() -> None:
    section("⑩b ★ missing_tables 真的去看磁盘，不是永远返回空")

    # ⚠️ 上面 ⑩ 把 missing_tables 换成了假的，所以**它本身必须单独测** ——
    #    否则"文件缺了会拦住"这条守的只是一个替身。
    import tempfile as _tf

    from ginan_agent.ginan import fetch as gfetch

    all_tables = catalog.plan_tables()
    check(len(all_tables) == 10, f"清单是 {len(all_tables)} 个文件")
    check(
        all(t.filename.startswith("tables/") for t in all_tables),
        "★ 它们都带 tables/ 前缀（Ginan 配置里就是这么引用的）",
    )
    check(all(t.one_time for t in all_tables), "★ 全部是一次性文件（不随日期变）")

    # 空的目录 → 十个全缺
    with _tf.TemporaryDirectory() as d:
        empty = Path(d)
        miss = gfetch.missing_tables(empty)
        check(
            len(miss) == len(all_tables),
            f"空目录 → 全都算缺（{len(miss)}/{len(all_tables)}）",
        )

        # 放一个进去（非空文件）→ 少一个
        one = all_tables[0].filename
        p = empty / one
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)
        miss2 = gfetch.missing_tables(empty)
        check(
            len(miss2) == len(all_tables) - 1 and one not in {m.filename for m in miss2},
            "★ 放进去一个之后，它就不再算缺",
        )

        # ⚠️ **空文件不算数** —— 半截的下载比没有更坏：
        #    它会让"下过了"成立，于是永远不再重下。
        p.write_bytes(b"")
        miss3 = gfetch.missing_tables(empty)
        check(
            len(miss3) == len(all_tables),
            "★ 0 字节的文件不算'已下'（半截的下载比没有更坏）",
        )


def test_table_list_and_config_keys_agree() -> None:
    section("⑩c ★ 清单和配置键必须**一一对应**（对不上就是'下了没用'）")

    # ⚠️⚠️ 这条守的是一个**已经发生过**的 bug：
    #
    #     文件下下来了，但配置里没写对应的键 → pea 一个都不去打开 →
    #     **跑出来和基础档一模一样，而且不报错**。
    #
    #     根因就是"两份清单"：一份是要下的文件（TABLES），
    #     一份是配置里的键（TABLE_KEYS）。**任何一边加了、另一边忘了**，
    #     都会静默退化成那个 bug。
    names = {n for n, _ in catalog.TABLES}
    keys = set(catalog.TABLE_KEYS)

    only_files = sorted(names - keys)
    only_keys = sorted(keys - names)
    for x in only_files:
        print(f"      ⚠️ {x} 在下载清单里，但 TABLE_KEYS 里没有 —— 下了不会写进配置")
    for x in only_keys:
        print(f"      ⚠️ {x} 在 TABLE_KEYS 里，但下载清单里没有 —— 配置会引用一个下不到的文件")

    check(not only_files and not only_keys, "★ 两份清单完全对应")

    # 反过来：table_inputs() 里出现的每一个路径，都必须能追到一个下载项
    #
    # ⚠️ 这里是**两层写死的展开**，不是递归函数。
    #    第一版写了个 `walk()` 递归 —— `check_recursion.py` 当场变红：
    #    这个仓库**全局禁止自调用**（那条规矩来自一个真实事故：
    #    一个函数名遮蔽了自己，请求体被静默吞掉）。
    #    而 table_inputs() 的形状本来就只有两层（顶层键 → 子键 → 路径列表），
    #    递归是杀鸡用牛刀 —— 展开写反而更看得清。
    all_paths: list[str] = []
    for _top, _val in catalog.table_inputs().items():
        if isinstance(_val, dict):
            for _sub in _val.values():
                all_paths.extend(x for x in _sub if isinstance(x, str))
        else:
            all_paths.extend(x for x in _val if isinstance(x, str))

    # ⚠️ 分两类看，**不能一刀切**：
    #    · `tables/xxx` 是**具体文件** —— 每一个都必须能在下载清单里追到
    #    · 其它的（`*_ocean.BLQ` 这类）是**通配**，故意不下的：
    #      那是留给"用户自己算了测站 BLQ"的口子，匹配不到东西时静默无副作用
    #      （实测：`*_ocean.BLQ` 一个都没匹配到，pea 照常启动）
    concrete = [p for p in all_paths if p.startswith("tables/")]
    globs = [p for p in all_paths if not p.startswith("tables/")]

    check(len(concrete) == len(names), f"生成的配置里有 {len(concrete)} 个具体文件路径")

    unknown = [g for g in concrete if g.split("/")[-1] not in names]
    for u in unknown:
        print(f"      ⚠️ 配置里引用了 {u}，但它不在下载清单里 —— 那会是一个下不到的文件")
    check(not unknown, "★ 配置里引用的每一个具体文件，下载清单里都有")

    # ⚠️ 通配只允许**已知的那两个**。不这么钉的话，一个写错的具体路径
    #    （比如少打了个 `tables/`）会掉进 globs 里被悄悄放过去。
    check(
        sorted(globs) == ["*_atmos.BLQ", "*_ocean.BLQ"],
        f"★ 通配只有那两条（实际：{globs}）—— 别的都该是具体文件",
    )

    # ⚠️ snx_files 那三个是**并进已有列表**的，不是独立的键 ——
    #    这条守着"别把它们写成同级的新键"（写错的话 Ginan 看不见，不报错）
    ti = catalog.table_inputs()
    check(
        "snx_files" in ti and len(ti["snx_files"]) == 3,
        "★ 三个偏航姿态文件落在 snx_files 里（和 igs_satellite_metadata.snx 并列）",
    )
    check(
        "troposphere" in ti and "gpt2grid_files" in ti["troposphere"],
        "★ gpt2 格网落在 troposphere.gpt2grid_files 里",
    )
    check("tides" in ti and len(ti["tides"]) == 4, "★ 潮汐那四项落在 tides 里")


# =============================================================================
# ⑪ ★ 运行时的警告必须跟着结论走
# =============================================================================


def test_warnings_reach_the_conclusion() -> None:
    section("⑪ ★ 运行时的警告必须跟着结论走")

    # ⚠️⚠️ 原来 `summarize_results` **只看 `.POS` 文件** ——
    #    于是"跑完了但有一堆严重警告"和"一切正常"给出的是
    #    **完全相同的结论**。
    #
    #    实测踩到：一次真实运行里有 227 条警告 ——
    #       · 天线型号没填 → **没做相位中心改正**
    #       · 先验坐标差了 **2067 米**
    #       · 对流层模型拿到 -1000 米的高度
    #       · 有些卫星**回退到单频**
    #    而界面上显示的是"**处理正常完成**"。
    #
    #    **一个说「正常」但实际不可信的结论文，比没有结论更糟** ——
    #    用户会拿着它去做分析、写论文。
    #
    #    ⚠️ 而且这条链路有**两处**会静默断掉，两处都要守：
    #       ① 诊断层看不见警告（ANSI 颜色码）—— 见 check_diagnostics ⑧
    #       ② 看见了也不进结论 —— **就是这一条**
    r = Runner()
    r("plan_run", stations=["ALIC"], day="2026-09-02")
    r("generate_config")
    run = r("run_processing", max_epochs=20)
    check(run["ok"], "（前提）这次运行本身是成功的")

    # ---- 干净的一次运行：不该冒出运行警告 ----
    s0 = r("summarize_results")
    check(
        not s0.get("run_warnings"),
        "没有警告时不硬造警告（否则就成了狼来了）",
    )

    # ---- 给这次运行塞进一条真实形状的警告（带 ANSI 颜色码）----
    from ginan_agent.ginan import diagnostics as D

    r.ws.runs[-1].diagnosis = D.diagnose(
        "\x1b[1;93mWarning: Antenna name not specified for ALIC\x1b[0m\n"
        "PEA finished\n"
    )

    s = r("summarize_results")
    warns = s.get("run_warnings") or []
    check(bool(warns), "★ 警告进了结论（run_warnings 非空）")
    if warns:
        check(
            warns[0]["kind"] == "missing_antenna",
            f"★ 分类正确：{warns[0]['kind']}",
        )
        check(
            bool(warns[0].get("what_to_do")),
            "★ 连带把「该怎么办」也带上了",
        )

    # ⚠️ 光把字段塞进 JSON 不够 —— **必须明确要求模型转述**。
    #    用户看不到这个 JSON，模型不说他就永远不知道。
    note = s.get("note", "")
    check(
        "run_warnings" in note,
        "★ note 里明确要求转述 run_warnings",
    )
    check(
        "必须" in note,
        "用的是「必须」而不是「可以」",
    )


# =============================================================================
# ⑫ ★ 配置里必须带测站的近似坐标
# =============================================================================


# ⚠️ 真实的 ALIC 头部片段（从实测下载的文件里摘的）。
#    那个坐标就是 GA 的文件里写的 APPROX POSITION XYZ。
_ALIC_OBS_HEAD = (
    b"     3.04           OBSERVATION DATA    M                   "
    b"RINEX VERSION / TYPE\n"
    b"ALIC00AUS                                                   "
    b"MARKER NAME\n"
    b" -4052052.7249  4212835.9727 -2545104.5766                  "
    b"APPROX POSITION XYZ\n"
    b"END OF HEADER\n"
)


def test_config_gets_station_position() -> None:
    section("⑫ ★ 配置里必须带测站的近似坐标")

    # ⚠️⚠️ 这一条是**实测发现的**，值得把数据记下来。
    #
    #    不给 `apriori_position` 的后果（ALIC 整天数据，2880 历元）：
    #
    #        Ginan 报 "No receiver apriori position found for ALIC"
    #        → 只能用单点定位的结果凑合先验坐标
    #        → 对流层模型算出的测站高度是 **-1000 米 / 11000 米**（被截断）
    #        → **62 颗卫星回退到单频**
    #
    #    给了之后（同一份数据、同一套配置，**只加这一项**）：
    #        · 上面两条警告**消失**
    #        · 回退单频的卫星 **62 颗 → 17 颗**
    #
    #    ⚠️ 而 RINEX **文件头里明明就有**那个坐标 ——
    #       所以这是一句"**文件里有 ≠ 程序能拿到**"的实例。
    #
    #    ⚠️ 最要命的是失败方式：解算**不会失败**，只是结果变差。
    #       属于本项目最想避免的那一类。
    from datetime import date

    from ginan_agent.ginan import config as gconfig
    from ginan_agent.ginan.catalog import RunPlan

    # ---- ① 从文件头里读得出来吗 ----
    ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="pos_")))
    ws.plan = RunPlan(stations=["ALIC"], day=date(2026, 9, 2))
    tmp = Path(tempfile.mkdtemp(prefix="obs_"))
    (tmp / "ALIC00AUS.rnx").write_bytes(_ALIC_OBS_HEAD)

    got = ws._station_positions(["ALIC00AUS.rnx"], tmp)
    check(list(got) == ["ALIC"], f"★ 站号取文件头那个（9 位 → 4 位）：{list(got)}")
    pos = got.get("ALIC")
    check(pos is not None, "★ 从 APPROX POSITION XYZ 读到了坐标")
    if pos:
        check(
            abs(pos[0] - (-4052052.7249)) < 1e-4
            and abs(pos[2] - (-2545104.5766)) < 1e-4,
            f"坐标值正确：{pos}",
        )

    # ---- ①b ★★★ 网络解：**每一个站的文件都要读** ----
    #
    # ⚠️⚠️ 这一条盯的是"一次只算一个站的代码搬到多站上"的经典退化：
    #
    #        out = 只读 obs_names[0]
    #
    #    单站时完全正常；多站时**只有第一个站拿到先验坐标**，
    #    其余站在配置里没有这个字段 → Ginan 用单点定位凑合 →
    #    那几站的解算质量悄悄下降。
    #
    #    ⚠️ 而现象是"看起来正常、只是精度差" —— 这个仓库最怕的形状。
    #    ⚠️ 而单站夹具**永远暴露不了它**（只有一个文件，读谁都一样）。
    tmp_m = Path(tempfile.mkdtemp(prefix="obs_multi_"))
    for name, x in (("ALIC00AUS.rnx", -4052052.7249),
                    ("DARW00AUS.rnx", -4101084.0),
                    ("CEDU00AUS.rnx", -3753472.0)):
        head = _ALIC_OBS_HEAD.replace(b"ALIC00AUS", name[:9].encode())
        head = head.replace(b"-4052052.7249", f"{x:.4f}".encode())
        (tmp_m / name).write_bytes(head)

    ws.plan = RunPlan(stations=["ALIC", "DARW", "CEDU"], day=date(2026, 9, 2))
    multi = ws._station_positions(
        ["ALIC00AUS.rnx", "DARW00AUS.rnx", "CEDU00AUS.rnx"], tmp_m
    )
    check(
        len(multi) == 3,
        f"★★★ 三个站**都**读到了（实际 {list(multi)}）"
        "—— 只读第一个的话这里只有 1 个",
    )
    check(
        all(v is not None for v in multi.values()),
        f"★★ 而且三个都有坐标（不是只有第一个有）：{multi}",
    )
    check(
        list(multi) == ["ALIC", "DARW", "CEDU"],
        f"★★ 顺序跟着计划走（配置里的站块顺序 = 用户给的顺序）：{list(multi)}",
    )

    # ---- ② 读不到时不硬编 ----
    #
    # ⚠️⚠️ 文件要**按计划里的站名**命名（`ALIC…`）—— `_station_positions`
    #     是靠"文件名以站号开头"找到这个站的文件的（认领时写的前缀）。
    #     第一版这里叫 `X.rnx`，于是它**根本没被读**、返回空表，
    #     而 `all(v is None for v in {}.values())` 是 **True** ——
    #     断言**因为错的原因通过了**。夹具和判据对不上，比没有断言更糟。
    tmp2 = Path(tempfile.mkdtemp(prefix="obs2_"))
    (tmp2 / "ALIC00AUS.rnx").write_bytes(b"     3.04    OBSERVATION DATA\n")
    ws.plan = RunPlan(stations=["ALIC"], day=date(2026, 9, 2))
    got2 = ws._station_positions(["ALIC00AUS.rnx"], tmp2)
    check(
        got2 == {"ALIC": None},
        f"★ 头里没有坐标时不硬编（实际 {got2}）",
    )
    check(not ws._station_positions([], tmp2), "没有观测文件时返回空表")

    # ⚠️ 全零坐标要当成"没读到" —— 写进配置会被当成**地心**，
    #    比不写更糟。
    tmp3 = Path(tempfile.mkdtemp(prefix="obs3_"))
    (tmp3 / "ALIC00AUS.rnx").write_bytes(
        b"     3.04           OBSERVATION DATA\n"
        b"       0.0000        0.0000        0.0000                  "
        b"APPROX POSITION XYZ\n"
    )
    got3 = ws._station_positions(["ALIC00AUS.rnx"], tmp3)
    check(
        got3 == {"ALIC": None},
        f"★ 全零坐标当成'没有'（写进配置会被当成地心）（实际 {got3}）",
    )

    # ---- ③ 配置里真的写出来了吗 ----
    p = Path(".")
    inp = gconfig.ConfigInputs(
        config_id="20260902", inputs_root=p, obs_root=p, outputs_root=p,
        stations={"ALIC": (-4052052.7249, 4212835.9727, -2545104.5766)},
    )
    cfg = gconfig.build_config(inp)
    ro = cfg["receiver_options"]
    check("global" in ro, "全局默认还在")
    check("ALIC" in ro, "★ 配置里出现了按测站分块")
    if "ALIC" in ro:
        ap = ro["ALIC"].get("apriori_position")
        check(
            ap is not None and len(ap) == 3,
            f"★ apriori_position 是三个数：{ap}",
        )
        check(
            ap is not None and abs(ap[0] - (-4052052.7249)) < 1e-3,
            "坐标值写对了",
        )

    # ---- ④ 没有坐标时**不能**伪造一个 ----
    inp2 = gconfig.ConfigInputs(
        config_id="x", inputs_root=p, obs_root=p, outputs_root=p,
        stations={},
    )
    ro2 = gconfig.build_config(inp2)["receiver_options"]
    check(
        list(ro2) == ["global"],
        f"★ 没有坐标时只留 global（不伪造）：{list(ro2)}",
    )


# =============================================================================
# ⑬ ★ "参考位置过期"不能让一份好解算看起来是坏的
# =============================================================================


def _fake_pos(
    offset_n: float, offset_e: float, offset_u: float, n: int = 120
) -> str:
    """造一份 .POS：解算稳定停在「参考位置 + 一个**固定**偏移」处。

    ⚠️ 为什么偏移是**固定**的 —— 那正是"参考位置过期"的特征：
       测站整体移动了一段（板块运动），所以**所有历元都偏同一个量**。

       ⚠️ 区分这一点很关键：
          固定偏移   → 参考位置的问题（不影响"稳不稳"）
          越来越大   → 解算的问题
    """
    lines = [
        "PBO Station Position Time Series. Reference Frame : IGS20",
        "Format Version: 2.0.0",
        "4-character ID: ALIC",
        "First Epoch   : 2026-09-02T00:00:00.01",
        "XYZ Reference position :  -4052052.724900 4212835.972700 "
        "-2545104.576600 (IGS20)",
        "NEU Reference position :  -23.670110 133.885522 603.224004 (IGS20)",
    ]
    for i in range(n):
        hh, mm, ss = i // 120, (i * 30 // 60) % 60, (i * 30) % 60
        # ⚠️ 抖动要**确定**（不用随机数）—— 测试必须可复现。
        j = 0.002 * ((i % 5) - 2) / 2
        lines.append(
            f"2026-09-02T{hh:02d}:{mm:02d}:{ss:02d}.000 2026.67 "
            f"-4052052.724900 4212835.972700 -2545104.576600 "
            f"0.001 0.001 0.001 0 0 0 "
            f"-23.670110 133.885522 603.224004 "
            f"{offset_n + j:.5f} {offset_e + j:.5f} {offset_u + j:.5f} "
            f"0.001 0.001 0.002 0.01 0.02 0.03 ginan"
        )
    return "\n".join(lines) + "\n"


def test_stale_reference_position() -> None:
    section("⑬ ★ 参考位置过期，不能让一份好解算看起来是坏的")

    # ⚠️⚠️ 这是**实测发现的**，而且它把一份**很好的**解算报成了废的。
    #
    #    实测（2026-09-19，ALIC 2026-09-02）：
    #
    #        .POS 表头那个 "Reference position" = 观测文件头里的近似坐标，
    #        而它是 **2020 年前后的旧值**。
    #        ALIC 每年移动 6.7 厘米，到观测日已经过了 6.67 年 ——
    #        参考位置差了 **45 厘米**。
    #
    #        于是（修复前）：
    #            结论："定位偏差约 45.8 厘米（米级水平）"
    #            ✗ 解算还没有稳定下来
    #
    #        而拿 IGS20 的权威坐标一比：**解算实际只差 3.2 厘米**。
    #
    #    ⚠️ 一个错误的假设连锁坏了三件事：
    #        ① `converged_from()` 永远判不收敛（偏差恒大于 10 厘米阈值）
    #        ② `rms()` 被那 45 厘米整体平移主导
    #        ③ 结论把"离参考位置多远"当成了"定位偏差"
    #
    #    👉 关键认识：**整体平移污染"偏差"，但不影响"离散程度"。**
    #       所以判定收敛和质量要用**解算自身的**位置作参考。
    from ginan_agent.ginan import posfile

    # ---- 解算很稳，但整体偏了 45 厘米（= 参考位置过期）----
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ALIC_20260902.POS"
        p.write_text(_fake_pos(0.39, 0.13, 0.09), encoding="utf-8")
        pf = posfile.parse(p)
        v = posfile.verdict(pf)

        check(pf.count == 120, f"读到了 {pf.count} 个历元")

        rep = pf.repeatability()
        check(
            rep["3d"] < 0.03,
            f"★ 重复性是**厘米级**的（{rep['3d']*100:.1f} 厘米）—— 解算其实很稳",
        )

        check(
            v.quality != "poor",
            f"★ 不能因为参考位置过期就判成'米级'（实际 quality={v.quality}）",
        )
        check(
            "重复性" in v.headline,
            f"★ 结论说的是重复性：{v.headline}",
        )
        check(
            "偏差" not in v.headline,
            "★ 结论里**没有**把'离参考位置的偏移'说成偏差",
        )

        # ⚠️ 收敛也要能判出来 —— 修复前它永远说"没稳定下来"。
        check(
            pf.converged_from() >= 0,
            f"★ 能判出收敛了（第 {pf.converged_from()} 个历元起）",
        )
        check(
            not any("没有稳定下来" in c for c in v.caveats),
            "★ 不再误报'解算还没有稳定下来'",
        )

        # ⚠️ 那个偏移量还是要说 —— 但要**说清它不是精度**。
        check(
            any("参考位置" in d for d in v.details),
            "仍然报告了与参考位置的偏移量",
        )
        check(
            any("不是精度" in c or "不是精度" in "".join(v.caveats)
                for c in v.caveats),
            "★ 而且明确说了「那不是精度」",
        )

        # ---- 真正的坏解算还是要判成坏的 ----
        #
        # ⚠️ 这条是防"矫枉过正"：如果为了修上面那个 bug，
        #    把判定改得太宽松，真正的坏解算就会被放过去。
        p2 = Path(td) / "bad.POS"
        lines = _fake_pos(0.39, 0.13, 0.09).splitlines()
        # 把后一半历元的偏移**越拉越大** —— 这是真的没收敛
        out = lines[:6]
        for i, ln in enumerate(lines[6:]):
            parts = ln.split()
            drift = 0.05 * i
            parts[14] = f"{0.39 + drift:.5f}"
            parts[15] = f"{0.13 + drift:.5f}"
            parts[16] = f"{0.09 + drift:.5f}"
            out.append(" ".join(parts))
        p2.write_text("\n".join(out) + "\n", encoding="utf-8")

        pf2 = posfile.parse(p2)
        check(
            pf2.repeatability()["3d"] > 0.5,
            f"★ 真正发散的解算，重复性照样很大"
            f"（{pf2.repeatability()['3d']*100:.0f} 厘米）",
        )


# =============================================================================
# ⑧ 工具集本身
# =============================================================================


def test_tides_in_reduced_profile() -> None:
    section("⑭ ★ 基础档的潮汐改正：该开的开，该关的关")

    # ⚠️⚠️ 这一条对应问题 **T16**（旧编号是 ⑯ —— `dev/00-项目全貌.md` 第十二节
    #     2026-09-20 把待办换成了 T 编号，因为「⑯」在第九节和第十二节**含义不同**。
    #     2026-09-29 才改到这里，之前一直记着"下次动它时记得改"）：
    #     `reduced` 档把**所有**潮汐改正都关了，
    #     而其中固体潮**不需要任何外部文件** —— 关掉它没有任何理由。
    #
    #     实测代价（ALIC 2026-09-02 整天，只改这一项，用 IGc20 权威坐标当尺子）：
    #         关：3D 9.5 / U 7.8 厘米
    #         开：3D 7.7 / U 4.5 厘米
    #     垂直方向几乎减半。
    #
    #     而原来界面上写着"厘米级里差几毫米量级" —— **是错的**。
    #     代价说不准就得说"说不准"，不能编一个小数字。
    import yaml

    r = Runner()
    r("plan_run", stations=["ALIC"], day="2026-09-02")
    res = r("generate_config")
    check("config_path" in res, "生成了配置")

    path = Path(res["config_path"])
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    tides = (cfg["receiver_options"]["global"]["models"]["tides"])

    # ---- ① 固体潮要开着，而且**两个开关都得是 true** ----
    #
    # ⚠️⚠️ 这是最容易踩的一个坑，值得单独立一条：
    #
    #     `enable` 是总开关，`solid` 只管"要不要算固体潮"。
    #     整个 Ginan 源码里 `enable` 只被用在一处：
    #
    #         if (recOpts.tideModels.enable) pppTides(...);
    #
    #     **所以 `solid: true` 配 `enable: false` = 一点改正都没有**，
    #     而且不报错、不警告、日志里也看不出来。
    check(tides.get("enable") is True, "★ 潮汐总开关 enable 是开的")
    check(tides.get("solid") is True, "★ 固体潮 solid 是开的")
    check(
        not (tides.get("solid") and not tides.get("enable")),
        "★ 没有落进「solid 开了但 enable 关着」那个静默失效的组合",
    )

    # ---- ② 需要外部文件的那些**必须还是关的** ----
    #
    # ⚠️ 这一条守的是这个档位的**存在理由**：它不需要任何 tables/ 文件。
    #    哪天有人"顺手"把 otl 也打开，配置里就会引用一个下不到的文件，
    #    而 Ginan 跑到一半才报"缺文件" —— 那正是 standard 档被下架的原因。
    for k, why in (("otl", "OLOAD_GO.BLQ"), ("atl", "ALOAD_GO.BLQ"),
                   ("opole", "opoleloadcoefcmcor.txt")):
        check(
            tides.get(k) is False,
            f"★ {k} 仍然是关的（它要 {why}，而基础档承诺不要任何外部文件）",
        )

    # ---- ③ 代价那句话不能再是错的说法 ----
    tradeoff = res.get("tradeoff", "")
    check(
        "厘米级里差几毫米" not in tradeoff,
        "★ 不再声称「厘米级里差几毫米」（实测是十几厘米，那个说法是错的）",
    )
    check(
        "固体潮" in tradeoff,
        "★ 代价说明里交代了固体潮是开着的（用户要能看懂到底改了什么）",
    )

    # ---- ④ 同一句话只能有一个出处 ----
    #
    # ⚠️ 原来那句错话同时活在**代码两处 + README + 文档**里，
    #    改的时候漏掉了模型读给用户听的那处。
    #    现在 unavailable_reason 从 PROFILES 里取，不会再各说各的。
    from ginan_agent.ginan import config as gconfig

    reason = gconfig.unavailable_reason("standard")
    check(
        gconfig.PROFILES["reduced"].tradeoffs in reason,
        "★ 被拦下时给出的代价说明，和 PROFILES 里那份**是同一句话**",
    )


def _fake_pos_smoothed(n: int = 2880, distinct: int = 7) -> str:
    """造一份"被压成一个点"的平滑解 —— 参数照**实测**的来。

    实测（ALIC 2026-09-02 真跑出来的）：2880 个历元里只有 7 个不同位置。
    """
    lines = [
        "PBO Station Position Time Series. Reference Frame : IGS20",
        "Format Version: 2.0.0",
        "4-character ID: ALIC",
        "First Epoch   : 2026-09-02T00:00:00.01",
        "XYZ Reference position :  -4052052.724900 4212835.972700 "
        "-2545104.576600 (IGS20)",
        "NEU Reference position :  -23.670110 133.885522 603.224004 (IGS20)",
    ]
    for i in range(n):
        hh, mm, ss = i // 120, (i * 30 // 60) % 60, (i * 30) % 60
        # ⚠️ 位置**几乎不变** —— 只在最后几位动，模拟 RTS 收敛后的输出。
        #    取模让不同位置数正好是 `distinct` 个。
        k = i % distinct
        lines.append(
            f"2026-09-02T{hh:02d}:{mm:02d}:{ss:02d}.000 2026.67 "
            f"-4052052.724900 4212835.972700 -2545104.576600 "
            f"0.001 0.001 0.001 0 0 0 "
            f"-23.670110 133.885522 603.224004 "
            f"{0.3809 + k * 1e-6:.6f} {0.2018:.6f} {0.0193:.6f} "
            f"0.001 0.001 0.002 0.01 0.02 0.03 ginan"
        )
    return "\n".join(lines) + "\n"


def _fake_pos_step(n: int = 2880, at: int = 454, step_cm: float = 7.2) -> str:
    """造一份**两段常数**的平滑解 —— 形状照**实测**的来。

    实测（ALIC 2022-03-01 整天，三次运行的真文件）：

        分界在第 454 / 457 / 521 个历元（约 03:47~04:20），
        前一段一个值、后一段另一个值，两段相差 7.2~9.2 厘米（东向为主）。

    ⚠️⚠️ 所以"平滑解 = 一个位置"这个说法**是错的** —— 它是**两段**。
       而 `repeatability()` 算出来是 **2.7 / 2.9 厘米，不是 0**。
       原来那句写死的「重复性恒等于 0」对这两份文件是假话。
    """
    lines = [
        "PBO Station Position Time Series. Reference Frame : IGS20",
        "Format Version: 2.0.0",
        "4-character ID: ALIC",
        "First Epoch   : 2022-03-01T00:00:00.01",
        "XYZ Reference position :  -4052052.730000 4212835.979600 "
        "-2545104.587000 (IGS20)",
        "NEU Reference position :  -23.670110 160903 133.885522 603.235972 "
        "(IGS20/WGS84)",
    ]
    for i in range(n):
        hh, mm, ss = i // 120, (i * 30 // 60) % 60, (i * 30) % 60
        # ⚠️ 东向那一段跳一下（7.2 厘米），和实测那份同一个量级；
        #    前一段是模拟"还没收敛"的那 3.8 小时。
        de = 0.121 if i < at else 0.049
        lines.append(
            f"2022-03-01T{hh:02d}:{mm:02d}:{ss:02d}.000 2022.16 "
            f"-4052052.730000 4212835.979600 -2545104.587000 "
            f"0.001 0.001 0.001 0 0 0 "
            f"-23.670110 133.885522 603.235972 "
            f"0.13700 {de:.5f} 0.13400 "
            f"0.001 0.001 0.002 0.01 0.02 0.03 ginan"
        )
    return "\n".join(lines) + "\n"


def test_smoothed_solution_verdict() -> None:
    section("⑮ ★ 平滑解不能被报成「重复性 0.0 厘米」")

    # ⚠️⚠️ 用户翻结果时打开的是 `_smoothed.POS`，程序对它报的是
    #     「重复性约 0.0 厘米（厘米级）· 质量 good」。
    #
    #     那不是"特别准" —— RTS 平滑对**静态站**会把全天收敛成
    #     **同一个位置**，然后每个历元都输出它。
    #     一堆一模一样的位置，离散程度当然是 0。
    #
    #     ⚠️ 更要命的是：程序会把两份**并排**列出来 ——
    #        前向解 2.8 厘米、平滑解 0.0 厘米，
    #        两个矛盾的结论里，**没意义的那个看起来更好**。
    from ginan_agent.ginan import posfile as gpos

    tmp = Path(tempfile.mkdtemp(prefix="smooth_"))

    sm = tmp / "ALIC_20260902_smoothed.POS"
    sm.write_text(_fake_pos_smoothed(), encoding="utf-8")
    pf = gpos.parse(sm)

    check(pf.solution == "smoothed", f"★ 按文件名认出是平滑解（{pf.solution}）")
    check(
        pf.distinct_positions() < 20,
        f"造出来的就是'被压成一个点'（{pf.distinct_positions()} 个不同位置）",
    )
    check(
        not pf.repeatability_is_meaningful(),
        "★ 判定「重复性对它没意义」",
    )

    v = gpos.verdict(pf)
    # ⚠️ 只看 headline 和 details，**不要整个 text 一起查** ——
    #    details 里有"与参考位置相差 X 厘米"，那个数完全可能正好是 0.0，
    #    于是断言会因为一个无关的数字变红。**断言要盯着它真正要管的东西。**
    check(
        "重复性" not in v.headline and "0.0 厘米" not in v.headline,
        f"★ **headline 里不再出现「重复性 0.0 厘米」**（现在是：{v.headline}）",
    )
    check(
        # ⚠️ 断言盯的是**这句话在不在**，不是它的措辞。
        #    （改过一次措辞："不能当精度看" → "不是精度"，
        #      这条跟着改；它要拦的是"有人把这条 caveat 删了"。）
        any("重复性" in c and "不是精度" in c for c in v.caveats),
        "★ 明确提醒「这份文件里的重复性不是精度」",
    )
    check(v.quality != "good", f"★ 也不给 good（给的是 {v.quality}）")
    check("平滑" in v.headline, "结论里说清了这是平滑解")
    check(bool(v.caveats), "带了注意事项")

    # ---- ⚠️⚠️ 短文件那条路：**这里原来是个洞** ----
    #
    #     原来的判据是 `if count < 100: return True` —— 小样本直接"放过"。
    #     于是**半小时那份平滑解**（实测：60 个历元、1 个不同的位置）
    #     照样被报成「重复性约 0.0 厘米（厘米级）· 质量 good」。
    #
    #     ⚠️⚠️ 而同一节上面那几条断言**测不到它** ——
    #        夹具是 2880 个历元，永远走不到那个分支。
    #        **测试只覆盖了能用的那条路，洞就在没覆盖的那条上。**
    short = tmp / "ALIC_20220301_30min_smoothed.POS"
    short.write_text(_fake_pos_smoothed(n=60, distinct=1), encoding="utf-8")
    pfs = gpos.parse(short)
    check(
        pfs.count == 60 and pfs.distinct_positions() == 1,
        f"★ 短文件夹具照实测造（{pfs.count} 个历元 / "
        f"{pfs.distinct_positions()} 个不同的位置）",
    )
    check(
        not pfs.repeatability_is_meaningful(),
        "★★ 短文件的平滑解**也要**判定「重复性没意义」（原来这里放过）",
    )
    vs = gpos.verdict(pfs)
    check(
        "重复性" not in vs.headline,
        f"★★ 短文件也不再报「重复性 0.0 厘米」（现在是：{vs.headline}）",
    )
    check(vs.quality != "good", f"★ 短文件也不给 good（给的是 {vs.quality}）")

    # ---- ⚠️ 比率检验在**小样本**上没有分辨力 —— 绝对下限就是为这个加的 ----
    #
    #     40 个历元时，2% = 0.8，"只有 1 个位置"是 `1 > 0.8` = **真** ——
    #     **光靠比率会把它放过去**。所以判据里另有一条"至少 3 个不同的位置"。
    #     （60 历元时 2% = 1.2，比率自己也能拦住 —— 所以这条夹具取 40。）
    tiny = tmp / "ALIC_20220301_40ep_smoothed.POS"
    tiny.write_text(_fake_pos_smoothed(n=40, distinct=1), encoding="utf-8")
    pftiny = gpos.parse(tiny)
    check(
        pftiny.count == 40 and pftiny.distinct_positions() == 1,
        f"★ 40 历元夹具（{pftiny.count} 个历元 / {pftiny.distinct_positions()} 个位置）",
    )
    check(
        pftiny.distinct_positions() > pftiny.count * 0.02,
        "★ 说明比率这一条**自己会放过它**（1 > 0.8）—— 光有比率不够",
    )
    check(
        not pftiny.repeatability_is_meaningful(),
        "★★ 靠绝对下限拦住它（比率会漏）",
    )

    # ---- ⚠️⚠️ 两段常数那种：**不能把它说成"恒等于 0"** ----
    #
    #     实测三份整天平滑解，两份是**两段常数**（2423/457、2359/521），
    #     于是 `repeatability()` 算出来是 2.9 / 0.0005 厘米。
    #     而结论里那句「这里的『重复性』恒等于 0」是**写死的**。
    step = tmp / "ALIC_20220301_step_smoothed.POS"
    step.write_text(_fake_pos_step(), encoding="utf-8")
    pft = gpos.parse(step)
    rep = pft.repeatability()["3d"]
    check(
        pft.distinct_positions() == 2 and rep > 0.01,
        f"★ 两段常数夹具（2 个位置，重复性 {rep*100:.1f} 厘米 —— **不是 0**）",
    )
    check(
        not pft.repeatability_is_meaningful(),
        "★ 两段常数也判定「重复性没意义」",
    )
    vt = gpos.verdict(pft)
    check(
        not any("恒等于 0" in c for c in vt.caveats),
        "★★ 不再**写死**「重复性恒等于 0」（实测它有时是 2.9 厘米）",
    )
    check(
        any("不是精度" in c for c in vt.caveats),
        "★ 但仍然说清了「这个数不是精度」",
    )

    # ---- ⚠️⚠️ 那句「两个数字不能直接比」**必须真的发得出去** ----
    #
    #     `do_summarize_results` 靠 `summarize_json` 里的 `solution` 键
    #     判断"这次是不是同时产出了前向解和平滑解"。
    #
    #     ⚠️⚠️ 而那个键**原来根本不存在** —— 于是 `.get("solution")` 永远是
    #        None、`{"forward","smoothed"} ⊆ kinds` 永远不成立，
    #        整块 comparison 说明**一次都没发出去过**。
    #        也就是说：这段专门用来防止"两个矛盾的数并排摆着，用户挑好看的那个信"
    #        的说明，**从来没到过用户眼前**。实测复现（真喂两份结果，comparison 是空的）。
    #
    #     ⚠️ 为什么必须**端到端**测：`summarize_json` 负责写键、
    #        `do_summarize_results` 负责读键 —— **两边分开看都是对的**，
    #        错的是它们之间的那个约定。只有把两份结果真喂进去才看得见。
    import json as _json

    from ginan_agent.agent import ginan_tools as GT
    from ginan_agent.agent.workspace import Workspace

    both = Path(tempfile.mkdtemp(prefix="both_"))
    both.joinpath("ALIC_20220301_smoothed.POS").write_text(
        _fake_pos_step(), encoding="utf-8")
    # ⚠️ 复用同一个构造器造"前向解"：`distinct=n` 就是"每个历元都不同"，
    #    形状和真前向解一致（文件名不带 `_smoothed`，所以判成 forward）。
    both.joinpath("ALIC_20220301.POS").write_text(
        _fake_pos_smoothed(n=200, distinct=200), encoding="utf-8")

    out = _json.loads(GT.do_summarize_results(Workspace(work_dir=both)))
    kinds = {r["data"].get("solution") for r in out["results"]}
    check(
        kinds == {"forward", "smoothed"},
        f"★ summarize_json 把 solution 键带出来了（拿到的是 {kinds}）",
    )
    comp = out.get("comparison")
    check(
        bool(comp),
        "★★ 两份都在时，那句「不能直接比」**真的发出去了**"
        "（原来这个键不存在，整块说明是死的）",
    )
    if comp:
        joined = " ".join(comp.values())
        check(
            "恒等于 0" not in joined,
            "★★ 发出去的说明里也不写死「重复性恒等于 0」",
        )
        check(
            "不是精度" in joined,
            "★ 发出去的说明里说清了「那个数不是精度」",
        )

    # ---- ⚠️ 反过度修正：前向解必须**一点没变** ----
    #
    #     改判定时最容易顺手把正常那份也拦掉 ——
    #     而正常那份才是用户日常看的那份。
    #
    #     ⚠️ 夹具要**贴近真实**：真的前向解几乎每个历元的位置都不同。
    #        第一版复用了 `_fake_pos`，它的抖动量只有 **5 个离散值** ——
    #        于是 800 个历元里只有 5 个不同位置，
    #        被判成"被压成一个点"**其实是判对的**（那份夹具本身不真实）。
    #        **测试失败不一定是代码错，也可能是夹具不像真的。**
    fw = tmp / "ALIC_20260902.POS"
    rows = ["PBO Station Position Time Series. Reference Frame : IGS20",
            "Format Version: 2.0.0", "4-character ID: ALIC",
            "First Epoch   : 2026-09-02T00:00:00.01",
            "XYZ Reference position :  -4052052.724900 4212835.972700 "
            "-2545104.576600 (IGS20)",
            "NEU Reference position :  -23.670110 133.885522 603.224004 (IGS20)"]
    for i in range(800):
        hh, mm, ss = i // 120, (i * 30 // 60) % 60, (i * 30) % 60
        # 确定性的、**每个历元都不同**的抖动
        jn = 0.29 + 0.004 * math.sin(i * 0.7)
        je = 0.13 + 0.004 * math.cos(i * 1.1)
        ju = 0.08 + 0.006 * math.sin(i * 1.9)
        rows.append(
            f"2026-09-02T{hh:02d}:{mm:02d}:{ss:02d}.000 2026.67 "
            f"-4052052.724900 4212835.972700 -2545104.576600 "
            f"0.001 0.001 0.001 0 0 0 "
            f"-23.670110 133.885522 603.224004 "
            f"{jn:.5f} {je:.5f} {ju:.5f} "
            f"0.001 0.001 0.002 0.01 0.02 0.03 ginan"
        )
    fw.write_text("\n".join(rows) + "\n", encoding="utf-8")

    pf2 = gpos.parse(fw)
    check(pf2.solution == "forward", f"前向解被认成 forward（{pf2.solution}）")
    check(
        # ⚠️ 要的是"绝大多数历元位置各不相同"，不是"每一个都不同"。
        #    正弦抖动四舍五入到毫米之后本来就会重合一些 ——
        #    定 90% 是**我当时拍脑袋定的**，跑出来 45% 就不该过。
        #    真实的前向解在 99% 以上，夹具到 45% 已经足够和"被压成
        #    一个点"（0.1%）区分开了。
        pf2.distinct_positions() > pf2.count * 0.3,
        f"夹具贴近真实（{pf2.distinct_positions()}/{pf2.count} 个不同位置，"
        "而平滑解那个是 1/2880）",
    )
    check(
        pf2.repeatability_is_meaningful(),
        "★ 前向解的重复性**仍然有意义**（没被误伤）",
    )
    v2 = gpos.verdict(pf2)
    check(
        "重复性" in v2.headline,
        f"★ 前向解照常给重复性结论（{v2.headline}）",
    )


def test_tool_set() -> None:
    section("⑧ 工具集：数量、标记、描述")

    ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="tset_")))
    tools = GT.build_ginan_tools(ws)

    check(len(tools) >= 8, f"有 {len(tools)} 个工具")
    names = [t.name for t in tools]
    check(len(names) == len(set(names)), "名字没有重复")

    # 每个工具都要有"什么时候用"的描述 —— 那是给模型看的
    no_when = [t.name for t in tools if "什么时候用" not in t.description]
    check(not no_when, f"每个工具都写了'什么时候用'（缺：{no_when or '无'}）")

    # 有副作用的工具要标出来
    dangerous = [t.name for t in tools if t.dangerous]
    check("fetch_data" in dangerous, "下载标了 dangerous")
    check("run_processing" in dangerous, "运行标了 dangerous")

    # 长任务要能报进度
    ctx_tools = [t.name for t in tools if t.wants_context]
    check("fetch_data" in ctx_tools, "下载需要 ctx（要报进度、要能取消）")
    check("run_processing" in ctx_tools, "运行需要 ctx")

    # 每个工具都要能返回点什么（不能返回 None）
    check(all(t.max_result_chars > 0 for t in tools), "都设了结果长度上限")

    # ---- ⚠️⚠️ 每个工具在界面上都要有中文名 ----
    #
    # 这个守卫是**补上一个洞之后加的**（2026-09-20）。
    #
    # 那天新增了 `advise_from_inbox`，`check_all` **全绿** ——
    # 因为**没有任何守卫检查工具名和界面标签是否对齐**。
    #
    # 后果：界面上那张工具卡会显示成 `advise_from_inbox`
    # （`TOOL_LABEL[name] || name` 兜住了崩溃，但露出来的是英文下划线）。
    # 用户看到的是一个**内部标识符**。
    #
    # ⚠️ 这类问题**只在"加了新工具"时发生**，而加工具的人
    #    不会记得去改 `app.js` —— 所以它必须由自检来提。
    import re as _re

    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")
    m = _re.search(r"const TOOL_LABEL = \{(.*?)\n  \};", js, _re.S)
    check(bool(m), "在 app.js 里找到了 TOOL_LABEL")
    labels = set()
    if m:
        labels = set(_re.findall(r"^\s*([a-z_][a-z0-9_]*)\s*:", m.group(1), _re.M))

    missing = [n for n in names if n not in labels]
    check(
        not missing,
        f"★★ 每个工具都有界面标签（缺：{missing or '无'}）"
        "—— 缺的那个会在界面上露出英文标识符",
    )
    extra = sorted(labels - set(names))
    check(
        not extra,
        f"★ 界面标签里也没有多余的（多出来：{extra or '无'}）"
        "—— 那是已经删掉的工具留下的死标签",
    )


# =============================================================================
# 主程序
# =============================================================================


# =============================================================================
# ⑱ ★ POD 的配置来自官方示例，不是手写的
# =============================================================================
#
# ⚠️⚠️ 这一批守卫的由来（2026-09-24 实测）：
#
#     同一份数据、同一个二进制、同一批先验产品，**只有配置不同**：
#
#         手写 POD 配置    → 第 0 个历元 `dpotrf failed`，82 秒/历元，NaN 级联，崩
#         官方 pod_example → 0 次失败，2.7~3.2 秒/历元，SP3/OBX/CLK/SNX 齐全
#
#     手写那份缺的是**整块骨架**：`minimum_constraints`（基准定义 ——
#     没有它法方程秩亏）、`orbit_propagation`（卫星受力模型）、
#     `model_error_handling`、`spp.outlier_screening`…
#
#     👉 所以 POD 改成"载入官方示例当模板，只改文件路径和档位"。
#
#     **下面守的是：改的时候不许偏离示例，除非白名单里写明了理由。**
#
# ⚠️ 这一批能挡住"又一次手写缺了整块"。
#    它**挡不住**"示例本身有问题" —— 那只能靠真跑一次 pea 才会发现。
#    别把守卫通过当成"POD 能跑"。


def _pod_config(profile: str = "standard"):
    """造一份 POD 配置 —— 这一节的守卫共用。

    ⚠️ 用**标记名**当文件名（`ATX_MARKER.atx` 之类），不用真路径：
       这样每个"文件字段有没有被正确替换"都能在 diff 里看见 ——
       用真名字的话，撞上模板里恰好同名的那个就看不出来了。

    ⚠️ `build_config` 是**纯函数**（不读盘、不联网），所以这里给的路径
       在磁盘上不存在也没关系。
    """
    from ginan_agent.ginan import config as gconfig

    # ⚠️ 三个根路径用**三个不同的名字**，不是一个共用的。
    #    共用的话，"`inputs_root` 和 `gnss_observations_root` 写反了"
    #    这种错根本查不出来 —— 两边都是同一个标记。
    return gconfig.build_config(
        gconfig.ConfigInputs(
            config_id="20250615",
            inputs_root=Path("INROOT"),
            obs_root=Path("OBSROOT"),
            outputs_root=Path("OUTROOT"),
            workflow="pod", profile=profile,
            atx_files=["ATX_MARKER.atx"],
            erp_files=["ERP_MARKER.erp"],
            sp3_files=["SP3_MARKER.SP3"],
            clk_files=["CLK_MARKER.CLK"],
            nav_files=["NAV_MARKER.rnx"],
            rnx_inputs=["OBS_MARKER.rnx"],
            snx_files=["SNX_MARKER.snx"],
            egm_files=["tables/EGM_MARKER.gfc"],
            # ⚠️⚠️ **两个站，而且其中一个必须是模板里已经有块的。**
            #
            #      `pod_example.yaml:207` 有 `USN7: {aliases: [PIVOT]}`
            #      （`estimation_parameters.receivers.PIVOT` 依赖它 —— 基准站）。
            #
            #      ⚠️ 只注入一个"模板里没有的站"的话，"覆盖掉模板已有的块"
            #         这个 bug **完全测不出来**：变异测试 M6 就是这么漏过去的
            #         （夹具里只有 TEST，而模板里带块的是 USN7，两者不碰撞）。
            #
            #      👉 这就是"**夹具不贴真实，那条路的 bug 就没有守卫**"。
            #         夹具要覆盖的是**会碰撞的那种输入**，不是好写的那种。
            stations={
                _POD_TEST_STATION: (1.0, 2.0, 3.0),          # 模板里没有 → 新建块
                _POD_COLLIDING_STATION: (4.0, 5.0, 6.0),     # 模板里有 → 只能合并
            },
        )
    )


def _diff_paths(a, b, prefix=()):
    """两个 dict 的差异 —— 返回 `{路径元组: 说明}`。

    ⚠️ **显式队列，不递归** —— `check_recursion.py` 全仓库禁止函数自调用。

    ⚠️ 三种差异**分开记**（只在模板 / 只在我们 / 值不同）：
       混成一种的话，"删掉一个键"和"加一个同名的键"会看起来一样，
       而它们是两件完全不同的事。
    """
    todo: list = [(prefix, a, b)]
    out: dict = {}
    while todo:
        path, x, y = todo.pop()
        if isinstance(x, dict) and isinstance(y, dict):
            for k in x:
                if k not in y:
                    out[path + (str(k),)] = "只在模板里有"
                else:
                    todo.append((path + (str(k),), x[k], y[k]))
            for k in y:
                if k not in x:
                    out[path + (str(k),)] = "只在我们这边有"
            continue
        if x != y:
            out[path] = f"{x!r} -> {y!r}"
    return out


# 允许和官方示例不一样的地方。**每一条都要有理由，理由写在 `config.py` 里。**
#
# ⚠️ 这张表是**字面量**，不是从代码里算出来的 —— 刻意的：
#    它要能独立于实现被读懂。如果哪天 `config.py` 多加了一处偏离
#    而忘了更新这里，守卫就会红，而红的信息里带着那条新路径。
_ALLOWED_POD_DIFF_STANDARD: set[tuple[str, ...]] = {
    # ---- ① 文件路径：由 app 决定（`config._substitutions`）----
    ("inputs", "inputs_root"),
    ("inputs", "gnss_observations", "gnss_observations_root"),
    ("inputs", "gnss_observations", "rnx_inputs"),
    ("inputs", "atx_files"),
    ("inputs", "erp_files"),
    ("inputs", "snx_files"),
    ("inputs", "satellite_data", "nav_files"),
    ("inputs", "satellite_data", "clk_files"),
    ("inputs", "pseudo_observations", "sp3_inputs"),
    ("inputs", "egm_files"),
    ("outputs", "outputs_root"),
    ("outputs", "metadata", "config_description"),
    # ---- ② `include_yamls` 必须删（那个文件在发行版里不存在，
    #         而 Ginan 对 include 失败是**硬失败**）----
    ("inputs", "include_yamls"),
    # ---- ③ 太阳光压用 cannonball（boxwing 要那个不存在的文件）----
    ("satellite_options", "global", "orbit_propagation", "solar_radiation_pressure"),
    # ---- ④ 没有 MongoDB ----
    ("mongo", "enable"),
    # ---- ⑤ trace 降级（示例那组实测一天产出 2.6 GB）----
    ("outputs", "trace", "level"),
    ("outputs", "trace", "output_residuals"),
    # ---- ⑥ `process_modes` 以 `Workflow.modes` 为准 ----
    #
    # ⚠️ 模板里**只有 5 个键**（preprocessor/spp/ppp/ionosphere/slr 里它写了几个），
    #    而 `Workflow.modes` 是**完整值** —— `acsConfig.cpp:6642-6677` 的解析处
    #    接受的是 **6 个**（多一个 `sbas`）。
    #    ⇒ 表补全之后，配置里就比模板多出这两个键。
    #
    # ⚠️ 多出来的都是**默认值**（`slr` 和 `sbas` 的默认都是 false，
    #    见 `acsConfig.hpp:465` / `:585`），所以是恒等变换 —— 但仍然**要说出来**。
    ("processing_options", "process_modes", "slr"),
    ("processing_options", "process_modes", "sbas"),
    # ---- ⑦ 档位差量：`_merge_table_inputs` 给海潮那两项加了通配路径 ----
    ("inputs", "tides", "ocean_tide_loading_blq_files"),
    ("inputs", "tides", "atmos_tide_loading_blq_files"),
    # ---- ⑧ 「完整」档也把 `models.tides` **显式写出来**（2026-10-01）----
    #
    # ⚠️ 以前这里不需要这一条：那时「完整」档**根本不碰 `models`**，
    #    靠的是 Ginan 的默认值（`acsConfig.hpp:1314-1320` 六个键全是 true）
    #    恰好等于我们想要的全开。
    #
    # ⚠️⚠️ 现在必须显式写，两个理由：
    #    · **用户能逐项关掉某几项了** —— 靠默认值关不掉（关了也不生效，
    #      因为默认值就是 true，而"没写"和"写 true"在 Ginan 眼里一样）
    #    · 靠默认值正是 `config._apply_profile_delta` 记的那个坑：
    #      **默认 true + 文件没给 = 算不出来、不报错、结果默默少一项改正**
    #
    # ⚠️ 写出来的值和不写时**完全一样**（六项全开），所以这是
    #    "把隐含的写明确"，不是改行为。这一条要能一直读得懂 ——
    #    不许有人把它当成"白名单放行"随手删掉。
    ("receiver_options", "global", "models", "tides"),
}

# 「基础」档**额外**允许的差异 —— 就是它主动删掉/改掉的那些。
_ALLOWED_POD_DIFF_REDUCED: set[tuple[str, ...]] = (
    _ALLOWED_POD_DIFF_STANDARD
    | {
        # 删掉本档不提供的模型文件键
        ("inputs", "igrf_files"),
        ("inputs", "planetary_ephemeris_files"),
        ("inputs", "troposphere"),
        ("inputs", "tides"),
        # 把 models 写明确 —— 尤其是 tides，**必须显式写 false**。
        # 见 `config._apply_profile_delta`：Ginan 对 tides 的默认值全是 true，
        # 而模板没写那个块 ⇒ 不显式写 false 的话，海潮改正会**开着但算不出来**。
        ("receiver_options", "global", "models", "tides"),
        ("receiver_options", "global", "models", "troposphere", "models"),
        ("receiver_options", "global", "models", "ionospheric_components", "use_2nd_order"),
        ("receiver_options", "global", "models", "ionospheric_components", "use_3rd_order"),
    }
)


_POD_TEST_STATION = "TEST"
"""守卫造 POD 配置时注入的**普通**测站（模板里没有它）。"""

_POD_COLLIDING_STATION = "USN7"
"""守卫注入的**模板里已经有块的**测站。

⚠️ 它的存在是为了让"覆盖掉模板已有的块"这个 bug **可被观察到**。
   见 `_pod_config` 里的说明和变异测试 M6。
"""


def _allowed_station_paths() -> set[tuple[str, ...]]:
    """按测站分块那几条路径 —— 站号是动态的，没法写进字面量白名单。

    ⚠️⚠️ **只放行我们注入的那两个站，不是"所有非 global 的条目"。**

       第一版写的是后者，结果把模板自带的 `USN7` 也放行了 ——
       而 `USN7` 在两边**根本没有差异**。于是"白名单过期"那条守卫
       立刻报了它出来。

       ⚠️ 放行太宽的后果很具体：谁把 `USN7: {aliases: [PIVOT]}` 覆盖掉了
         都不会红，而那个别名是 `estimation_parameters.receivers.PIVOT`
         依赖的（基准站）。**覆盖掉不报错，症状是"基准选错了"。**
    """
    # ⚠️ 两个站的**深度不一样**，因为 `_diff_paths` 只记**最深的那一层**：
    #
    #      TEST  模板里**没有**这个站 ⇒ 整块都是新的 ⇒ diff 停在块一级
    #      USN7  模板里**有**这个站   ⇒ 只有坐标那个键是新的 ⇒ diff 递归进去
    #
    #    写成同一个深度的话，多出来的那条会被"白名单过期"检查报出来
    #    （这正是上一版发生的事）。
    return {
        ("receiver_options", _POD_TEST_STATION),
        ("receiver_options", _POD_COLLIDING_STATION, "apriori_position"),
    }


def test_pod_config_differs_from_the_example_only_where_declared() -> None:
    section("⑱ ★ POD 配置：和官方示例的差异必须都在白名单里")

    from ginan_agent.ginan import examples as gexamples

    tpl = gexamples.load("pod_example.yaml")

    # ---- 「完整」档 ----
    cfg = _pod_config("standard")
    diff = _diff_paths(tpl, cfg)
    allowed = _ALLOWED_POD_DIFF_STANDARD | _allowed_station_paths()
    extra = sorted(set(diff) - allowed)
    check(
        not extra,
        f"★「完整」档没有**没声明**的偏离（多出来的：{extra[:6]}）",
    )
    # ⚠️ 反过来也要报：白名单里写了一条、实际却没出现 —— 说明白名单过期了。
    #    不报的话，白名单会越攒越多，而每一行都看起来"有理由"。
    unused = sorted(allowed - set(diff))
    check(not unused, f"白名单里没有过期的条目（多余的：{unused[:6]}）")

    if extra:
        print("      实际 diff：")
        for k in sorted(diff):
            print(f"        {k}  {diff[k][:70]}")

    # ---- 「基础」档 ----
    cfg2 = _pod_config("reduced")
    diff2 = _diff_paths(tpl, cfg2)
    allowed2 = _ALLOWED_POD_DIFF_REDUCED | _allowed_station_paths()
    extra2 = sorted(set(diff2) - allowed2)
    check(
        not extra2,
        f"★「基础」档没有**没声明**的偏离（多出来的：{extra2[:6]}）",
    )


def test_pod_config_has_the_blocks_that_were_missing() -> None:
    section("⑱b ★ POD 配置：手写那份缺的整块，现在都在")
    cfg = _pod_config("standard")
    po = cfg["processing_options"]

    # ⚠️ 这一组是**逐名断言**，不是 diff ——
    #    diff 失败时给的是"一串路径"，看不出缺的是哪个整块。
    #    这一组把失败定位到具体的块上，读的人一眼知道少了什么。
    check(
        "minimum_constraints" in po,
        "★ processing_options.minimum_constraints（**基准定义**，缺了法方程秩亏）",
    )
    mc = po.get("minimum_constraints", {})
    for k in ("enable", "application_mode", "rotation", "scale", "translation"):
        check(k in mc, f"  · minimum_constraints.{k}")

    check(
        "orbit_propagation" in po,
        "★ processing_options.orbit_propagation（**卫星受力模型**）",
    )
    op = po.get("orbit_propagation", {})
    for k in (
        "integrator_time_step", "central_force", "egm_field", "egm_degree",
        "general_relativity", "indirect_J2", "solid_earth_tide", "ocean_tide",
    ):
        check(k in op, f"  · orbit_propagation.{k}")

    check("model_error_handling" in po, "★ processing_options.model_error_handling")
    check("spp" in po, "★ processing_options.spp（含离群值筛查）")
    check("gnss_general" in po, "★ processing_options.gnss_general")
    gg = po.get("gnss_general", {})
    for k in ("pivot_receiver", "minimise_sat_clock_offsets"):
        check(k in gg, f"  · gnss_general.{k}")

    est = cfg["estimation_parameters"]
    check("global_models" in est, "★ estimation_parameters.global_models（估地球定向）")
    sat = est.get("satellites", {}).get("global", {})
    check(
        sat.get("orbit", {}).get("estimated") == [True],
        f"★ satellites.global.orbit.estimated == [True]（实际 {sat.get('orbit', {}).get('estimated')!r}）",
    )

    # ⚠️⚠️ **模型文件那一块必须 merge 进去，不能整块覆盖。**
    #     模板里有 11 个键，`_model_options()` 只讲 4 个 ——
    #     覆盖会静默丢掉天线相位中心、姿态、地球定向那 7 个，**不报错**。
    #
    #     ⚠️⚠️ **两个档位都要查，不能只查「完整」档。**
    #        档位差量只作用于「基础」档（`_apply_profile_delta` 在
    #        needs_tables 时直接 return）—— 所以**只有「基础」档会走到
    #        那个 merge**。
    #
    #        （第一版只查了「完整」档，于是 M4 变异测试暴露了这个盲区：
    #          "models 被整块覆盖"在「完整」档上根本不会发生，查了等于没查。）
    for prof_key in ("standard", "reduced"):
        models = _pod_config(prof_key)["receiver_options"]["global"]["models"]
        missing = [
            k for k in (
                "eccentricity", "attitude", "clock", "pco", "pcv", "code_bias",
                "phase_bias", "pos", "ionospheric_components", "troposphere", "eop",
            )
            if k not in models
        ]
        check(
            not missing,
            f"★「{prof_key}」档 models 的 11 个键一个不少（缺：{missing}）",
        )

    check(cfg["outputs"].get("orbex", {}).get("output") is True, "★ outputs.orbex（轨道产物）")
    check(cfg["outputs"].get("clocks", {}).get("output") is True, "★ outputs.clocks（钟差产物）")

    # ---- 反向：手写那份**多出来**的键不许回来 ----
    #
    # ⚠️ 手写的 `_satellite_estimation` 多给了 `emp_d_1` / `emp_b_2`
    #    （示例里那几个是**注释掉**的）。谁把它接回模板这条路，
    #    这条就红 —— 因为那意味着"两个真相"又回来了。
    from ginan_agent.ginan import examples as gexamples

    tpl_sat = gexamples.load("pod_example.yaml")["estimation_parameters"]["satellites"]["global"]
    tpl_emp = {k for k in tpl_sat if k.startswith("emp_")}
    our_emp = {k for k in sat if k.startswith("emp_")}
    check(
        our_emp == tpl_emp,
        f"★ 经验加速度键和示例**完全一致**（示例 {sorted(tpl_emp)}，我们 {sorted(our_emp)}）",
    )


def test_pod_file_lists_come_from_config_inputs() -> None:
    section("⑱c ★ POD 配置：文件清单真的来自 ConfigInputs")

    cfg = _pod_config("standard")
    inp = cfg["inputs"]
    sd = inp["satellite_data"]

    # ⚠️ 三条根路径各查各的标记 —— 见 `_pod_config` 里为什么用三个名字。
    pairs = [
        ("inputs_root", str(cfg["inputs"]["inputs_root"]), "INROOT"),
        ("gnss_observations_root",
         str(inp["gnss_observations"]["gnss_observations_root"]), "OBSROOT"),
        ("outputs_root", str(cfg["outputs"]["outputs_root"]), "OUTROOT"),
        ("atx_files", inp["atx_files"], "ATX_MARKER.atx"),
        ("erp_files", inp["erp_files"], "ERP_MARKER.erp"),
        ("nav_files", sd["nav_files"], "NAV_MARKER.rnx"),
        ("clk_files", sd["clk_files"], "CLK_MARKER.CLK"),
        ("sp3_inputs", inp["pseudo_observations"]["sp3_inputs"], "SP3_MARKER.SP3"),
        ("rnx_inputs", inp["gnss_observations"]["rnx_inputs"], "OBS_MARKER.rnx"),
        ("snx_files", inp["snx_files"], "SNX_MARKER.snx"),
        ("egm_files", inp["egm_files"], "EGM_MARKER.gfc"),
    ]
    for name, got, marker in pairs:
        check(
            marker in (got if isinstance(got, str) else " ".join(got)),
            f"  · {name} 用的是我们给的（{marker}）",
        )

    # ⚠️⚠️ **反向：模板里那些文件名必须一个都不剩。**
    #     只查"我们的在不在"是不够的 —— 模板的文件名可能被**追加**在后面，
    #     而那样写出来的配置会引用一批磁盘上没有的文件
    #     （Ginan 的 `checkValidFiles` 对这是**硬失败**）。
    import yaml as _yaml

    text = _yaml.safe_dump(cfg, allow_unicode=True)
    leftovers = [
        n for n in (
            "igs20.atx", "finals.data.iau2000.txt",
            "IGS2R03FIN_20191990000_01D_30S_CLK.CLK",
            "IGS2R03FIN_20191990000_01D_05M_ORB.SP3",
            "products/boxwing.yaml",
        )
        if n in text
    ]
    check(not leftovers, f"★ 模板的文件名一个都没留下（剩下：{leftovers}）")

    # ---- 测站近似坐标 ----
    ro = cfg["receiver_options"]
    check(
        ro.get(_POD_TEST_STATION, {}).get("apriori_position") == [1.0, 2.0, 3.0],
        f"★ 测站坐标写进去了：{ro.get(_POD_TEST_STATION)}",
    )
    # ⚠️⚠️ **模板自带的测站块不能被覆盖掉** —— `pod_example.yaml` 里
    #     `USN7: {aliases: [PIVOT]}`，而 `estimation_parameters.receivers.PIVOT`
    #     依赖那个别名（基准站）。覆盖掉**不会报错**，症状是"基准选错了"。
    #
    #     ⚠️ 所以这里要**同时**查两件事：模板原来的键还在，**而且**
    #        我们的坐标也进去了。只查"别名还在"是不够的 ——
    #        一个只跳过已有块、根本不写坐标的实现也能通过那一条。
    usn7 = ro.get(_POD_COLLIDING_STATION, {})
    check(
        usn7.get("aliases") == ["PIVOT"],
        f"★ 模板自带的 {_POD_COLLIDING_STATION} 别名没被覆盖：{usn7}",
    )
    check(
        usn7.get("apriori_position") == [4.0, 5.0, 6.0],
        f"★ 而且我们的坐标也**合进了**那个已有的块（不是二选一）：{usn7}",
    )


def test_pod_profile_delta_deletes_the_table_keys() -> None:
    section("⑱d ★ POD 档位差量：该删的真删了，该写明确的真写了")

    std = _pod_config("standard")["inputs"]
    for k in ("igrf_files", "planetary_ephemeris_files", "troposphere", "tides"):
        check(k in std, f"  ·「完整」档有 inputs.{k}")

    red = _pod_config("reduced")["inputs"]
    for k in ("igrf_files", "planetary_ephemeris_files", "troposphere", "tides"):
        check(k not in red, f"★「基础」档**删掉了** inputs.{k}")

    # ⚠️⚠️ 这一条是这一节里**最重要**的一条。
    #
    #     Ginan 对 `models.tides` 的默认值**全是 true**，而
    #     `pod_example.yaml` **没写这个块**。
    #     ⇒ 只删 `inputs.tides` 而不写 `models.tides` 的话：
    #        海潮改正**开着**，但模型文件没了 ⇒ **算不出来、不报错**。
    #
    #     所以「基础」档必须把它**显式写成 false**。
    # ⚠️⚠️ **一律用 `.get()`，不许下标取值。**
    #
    #    下标取值在键不存在时会**抛 KeyError** —— 整个测试套件停在那里，
    #    后面的检查一条都跑不到。
    #
    #    （这是变异测试 M5 抓出来的：把 `models.tides` 的写入去掉之后，
    #      守卫不是"报红"而是"崩了"。**一个会崩的守卫比没有守卫更糟** ——
    #      没有守卫至少你还知道没查，崩了会让人以为"测试程序坏了"。）
    models_r = _pod_config("reduced")["receiver_options"]["global"]["models"]
    tides = models_r.get("tides") or {}
    check(
        tides.get("otl") is False and tides.get("atl") is False
        and tides.get("opole") is False,
        f"★「基础」档把海潮/大气潮/极潮**显式写成 False**（不是靠默认值）：{tides}",
    )
    check(
        tides.get("enable") is True and tides.get("solid") is True,
        f"★ 但总开关和固体潮**保持开着**（它们不需要外部文件）：{tides}",
    )

    check(
        (models_r.get("troposphere") or {}).get("models") == ["standard"],
        f"★「基础」档对流层用简化模型：{(models_r.get('troposphere') or {}).get('models')}",
    )
    check(
        (models_r.get("ionospheric_components") or {}).get("enable") is True,
        f"★ 电离层分量的 `enable` 没被整块覆盖弄丢：{models_r.get('ionospheric_components')}",
    )

    # ⚠️⚠️ 「基础」档的 POD **必须被拒绝** —— 见 `Workflow.profiles` 的说明。
    #     不是精度问题：缺 EGM 文件时 Ginan **照常输出一条错的轨道**。
    from ginan_agent.ginan import config as gconfig
    from ginan_agent.ginan import workflows as gworkflows

    pod = gworkflows.get("pod")
    check(
        "standard" in pod.profiles and "reduced" not in pod.profiles,
        f"★ POD 只允许「完整」档：{pod.profiles}",
    )
    reason = gconfig.profile_not_allowed_reason(pod, "reduced")
    check("EGM" in reason or "重力场" in reason, "★ 拒绝理由说清了是模型文件的事")
    check("错" in reason, "★ 拒绝理由说清了**结果会是错的**（不是精度取舍）")


def test_workflow_declares_its_config_source() -> None:
    section("⑱e ★ 每个工作流都写明了：配置是从哪来的")

    from ginan_agent.ginan import examples as gexamples
    from ginan_agent.ginan import workflows as gworkflows

    # ⚠️ 这张表是**字面量** —— 加第 3 个工作流时，必须在这里写明
    #    它属于哪个世界（模板 / 手写）。不写就是红。
    #
    #    这是"两套机制并存"这件事的**守卫**：并存是这一轮**刻意保留**的
    #    状态（PPP 有实测数字背书，不动它），但它必须在代码里**看得见**，
    #    不能是靠"某个字段没填"来暗示。
    #
    # ⚠️ 2026-09-27 加 `brdc2sp3` 时这条**如期变红**了 —— 那是它在干活，
    #    **不是它坏了**。加一行就绿。
    #
    # ⚠️ 2026-09-27 加 `fit_sp3_pseudoobs` 时**又红了一次** —— 同上。
    got = {k: wf.example for k, wf in gworkflows.WORKFLOWS.items()}
    check(
        got == {
            "ppp": None,
            "pod": "pod_example.yaml",
            "brdc2sp3": "brdc2sp3.yml",
            "fit_sp3_pseudoobs": "fit_sp3_pseudoobs.yaml",
            "rtk": "rtk_example.yaml",
            "sbas_l1": "sbas_example_l1.yaml",
            "sbas_dfmc": "sbas_example_dfmc.yaml",
            "sbas_pvs": "sbas_example_pvs.yaml",
        },
        f"★ 工作流 → 配置来源：{got}",
    )

    # ⚠️ **数据集工作流要另外说清"它的数据不是用户给的"。**
    #
    #    这是 ⑤ 引进的唯一新维度：前六条都是「某站某天」，而 SBAS 那三条
    #    跑的是**一份固定的官方演示数据集**（`catalog.DATASETS`）。
    #    加新工作流时如果填了一个不存在的 dataset key，`plan_run` 会
    #    静默走回"按用户给的站和天去下" —— 然后拿到一堆 404。
    from ginan_agent.ginan import catalog as _catalog

    for key, wf in gworkflows.WORKFLOWS.items():
        if wf.dataset is None:
            continue
        ds = _catalog.dataset(wf.dataset)
        check(ds is not None, f"  · {key} 的数据集 {wf.dataset!r} 真的存在")
        if ds is None:
            continue
        # ⚠️ 判据落在**计划**上，不是"数据集里有没有那一类"。
        #
        #    第一版我比的是"needs ⊆ 数据集里的类别"，于是 `atx`/`snx` 报了红 ——
        #    而它们是**一次性文件**（`catalog.atx_plan()` / `snx_plan()`），
        #    本来就不该在数据集里。**红的是我的断言，不是代码。**
        #
        #    真正要保证的是：这个工作流声明的每一类，**计划里都有** ——
        #    缺的那一类会去按天取，而那个日期根本没有数据（一堆 404）。
        planned = {f.kind for f in _catalog.plan_run(["ALIC"], date(2026, 9, 2), key).files}
        missing = sorted(set(wf.needs) - planned)
        check(
            not missing,
            f"★ {key}：声明的 needs 计划里都有（缺 {missing}）—— "
            f"缺的那个会去按天取，而那个日期根本没有数据",
        )
        # 而且计划里的日期和测站**必须是数据集的那份**，不是调用方给的
        p = _catalog.plan_run(["ALIC"], date(2026, 9, 2), key)
        check(
            p.day == ds.day and sorted(p.stations) == sorted(ds.stations),
            f"★ {key}：计划里是数据集自己的日期和测站"
            f"（拿到 {p.day} / {p.stations}）—— 用户给的那两个要被**如实忽略**",
        )

    names = set(gexamples.EXAMPLE_SHA256)
    for key, wf in gworkflows.WORKFLOWS.items():
        if wf.example is not None:
            check(wf.example in names, f"  · {key} 的模板 {wf.example!r} 真的存在")

    # 一个模板不该被两个工作流共用 —— 共用的话"改模板"会同时改两条路。
    used = [wf.example for wf in gworkflows.WORKFLOWS.values() if wf.example]
    check(len(used) == len(set(used)), f"★ 模板没有和工作流多对一：{used}")


def test_example_configs_are_present_and_unmodified() -> None:
    section("⑱f ★ 打包的官方示例：都在，而且没被改过")

    from ginan_agent.ginan import examples as gexamples
    from ginan_agent.ginan import paths as gpaths

    avail = gexamples.available()
    check(
        len(avail) == len(gexamples.EXAMPLE_SHA256),
        f"★ {len(gexamples.EXAMPLE_SHA256)} 个模板都在（找到 {len(avail)} 个）",
    )

    bad = [
        n for n in gexamples.EXAMPLE_SHA256
        if gexamples.sha256_of(n) != gexamples.EXAMPLE_SHA256[n]
    ]
    check(
        not bad,
        f"★ 逐字节没被改过（不符的：{bad}）。"
        f"⚠️ 有意改的话要**同时**更新 `examples.EXAMPLE_SHA256` 并写清理由",
    )

    import yaml as _yaml

    for name in avail:
        try:
            data = _yaml.safe_load((gexamples.example_dir() / name).read_text(encoding="utf-8"))
            ok = isinstance(data, dict) and bool(data)
        except Exception:  # noqa: BLE001
            ok = False
        check(ok, f"  · {name} 能解析成非空 dict")

    # ---- 和上游比（找得到源码树才比）----
    #
    # ⚠️ **比结构，不比字节。** 行尾不归我们管（`.gitattributes` 钉的是 LF，
    #    但别人机器上检出时可能不是）。比字节会产生一堆跟内容无关的红。
    #
    # ⚠️ 找不到源码树就**跳过并说明**，不假装失败 ——
    #    只下二进制的用户本来就没有源码树，那不是错误。
    src = None
    for install in gpaths.ginan_install_dirs():
        cand = install / "source" / "exampleConfigs"
        if cand.is_dir():
            src = cand
            break
    if src is None:
        check(True, "（没找到 Ginan 源码树，跳过和上游的比对）")
        return

    drifted = []
    for name in avail:
        upstream = src / name
        if not upstream.is_file():
            drifted.append(f"{name}(上游没有)")
            continue
        try:
            if _yaml.safe_load(upstream.read_text(encoding="utf-8")) != _yaml.safe_load(
                (gexamples.example_dir() / name).read_text(encoding="utf-8")
            ):
                drifted.append(name)
        except Exception:  # noqa: BLE001
            drifted.append(f"{name}(读不了)")
    check(
        not drifted,
        f"★ 和上游 {src.parent.parent.name} 的示例结构一致（不一致：{drifted[:4]}）",
    )


def test_workflow_tables_are_wired() -> None:
    section("⑱g ★ 工作流特有的模型文件：真的进了下载链")

    from ginan_agent.agent.workspace import _TABLE_FILE_KEYS
    from ginan_agent.ginan import catalog
    from ginan_agent.ginan import workflows as gworkflows

    base = [f.filename for f in catalog.plan_tables(None)]
    ppp = [f.filename for f in catalog.plan_tables("ppp")]
    pod = [f.filename for f in catalog.plan_tables("pod")]

    check(
        ppp == base,
        f"★ 不给工作流 / ppp：只下通用那批（{len(base)} 个）",
    )
    check(
        len(pod) == len(base) + 1,
        f"★ pod 多**一个**文件（{len(base)} → {len(pod)}）",
    )
    extra = [f for f in pod if f not in base]
    check(
        extra == ["tables/EGM2008.gfc"],
        f"★ 多出来的就是重力场模型：{extra}",
    )
    check(
        "tables/EGM2008.gfc" not in ppp,
        "★ ppp **不捎带**下重力场 —— 72 MB，它一个字节都用不到",
    )

    # ⚠️⚠️ **用列表 + `if`，不要 `next(...)`。**
    #
    #     `next()` 在找不到时会抛 `StopIteration`，整个套件停在那里 ——
    #     后面的检查一条都跑不到，而看的人只会以为"测试程序坏了"。
    #
    #     （这一条是变异测试 N1 抓出来的。**同一个形状在 M5 已经出现过一次** ——
    #      那是"下标取值"，这是"next 不设默认值"，错法一模一样。）
    egm_list = [f for f in catalog.plan_tables("pod") if "EGM" in f.filename]
    check(len(egm_list) == 1, f"★ 找得到那一个重力场文件（找到 {len(egm_list)} 个）")
    if egm_list:
        egm = egm_list[0]
        # ⚠️⚠️ URL 必须带 `.gz` —— 桶里存的是**压缩过**的。
        #     实测：少这一个后缀就是 404，而 404 的表现是"这个源不可用"，
        #     会静默掉到下一个源（或者一个源都不剩）。
        check(
            all(u.endswith(".gz") for u in egm.urls.values()),
            f"★ 下载 URL 带 .gz（桶里存的是压缩过的）：{list(egm.urls.values())[0]}",
        )
        check(egm.one_time, "★ 标了 one_time —— 静态模型，只下一次")

    # ⚠️⚠️ **两份清单必须对得上：**
    #
    #      `WORKFLOW_TABLES` 说"要**下**这个文件"
    #      `Workflow.extra_inputs` 说"这个工作流的配置里会有这一类键"
    #
    #    对不上的两种后果都不报错：下了不用（浪费 72 MB），
    #    或者用了没下（配置里引用一个不存在的文件 → Ginan 硬失败）。
    for wf_key, entries in catalog.WORKFLOW_TABLES.items():
        wf = gworkflows.get(wf_key)
        for name, _purpose in entries:
            cfg_key = _TABLE_FILE_KEYS.get(name)
            check(cfg_key is not None, f"  · {name} 登记了它对应的配置键")
            check(
                cfg_key in wf.extra_inputs,
                f"★ {name} 的配置键 {cfg_key!r} 在 {wf_key}.extra_inputs 里"
                f"（否则「要下」和「要写进配置」就对不上）",
            )

    # 反向：`extra_inputs` 里声称要的键，**没有**对应文件的话
    # 说明那份声明是空的 —— 而空声明会让人以为"已经处理了"。
    declared = {
        _TABLE_FILE_KEYS.get(n)
        for entries in catalog.WORKFLOW_TABLES.values()
        for n, _p in entries
    }
    for wf_key in catalog.WORKFLOW_TABLES:
        wf = gworkflows.get(wf_key)
        # ⚠️ 只查"我们声称能提供下载的那几类"。`extra_inputs` 里还有
        #    `planetary_ephemeris_files` 这种走「完整」档通用表的，
        #    它们由另一个机制负责，不在这一条的范围里。
        for k in declared:
            if k in wf.extra_inputs:
                check(
                    k in _TABLE_FILE_KEYS.values(),
                    f"  · {wf_key}.extra_inputs 里的 {k!r} 有文件来源",
                )


def test_egm_gate_blocks_pod() -> None:
    section("⑱h ★ 缺重力场模型时，POD **拒绝生成配置**")

    from ginan_agent.ginan import catalog
    from ginan_agent.ginan import fetch

    # ⚠️⚠️ 为什么要**真的拦住**，而不是只警告一句：
    #
    #     读 Ginan 源码（`orbitProp.cpp:71-77`）：`egm_field: true` 但没有
    #     EGM 文件时，它**只打印一句 `EGM data not initialised`，
    #     跳过最大的那一项受力，轨道照常输出**。
    #
    #     ⇒ 用户拿到一条错的轨道，**没有任何报错**。
    #       这比「跑不起来」糟得多：跑不起来他会来问，跑错了不会。
    #
    # ⚠️⚠️ **夹具要把「通用模型文件都在、只缺 EGM」这个状态造出来。**
    #     不造的话，「完整」档那道门（`missing_tables`）会**先**触发，
    #     报的是"还缺 10 个通用模型"，而这条守卫根本走不到。
    #     （第一版就是这样：以为测的是 EGM 门，实际测的是另一道门。）
    #
    # ⚠️⚠️⚠️ 而"造出来"这件事**不能靠打一个补丁就算数** —— 2026-10-13 实测：
    #
    #     这一段原来只 patch 了 `gpaths.tables_dir`，而**通用模型那道门读的
    #     是 `paths.data_dir()`**（`fetch.missing_tables()` 里写着
    #     `dest = dest_dir or paths.data_dir()`，文件名自带 `tables/` 前缀）。
    #     于是那个补丁对那道门**完全无效**，它看到的是开发者机器上真实的
    #     `userdata/data/tables/` —— 那里正好躺着那 10 个文件 → 门没触发 → 通过。
    #
    #     **也就是说：这条守卫一直是靠"我的机器上恰好有那些文件"绿的。**
    #     在一个干净克隆里（`userdata/` 根本不存在）跑，它红 4 条，
    #     报的是"还要 10 个模型文件没下" —— 正是这段注释警告过的那个形状。
    #
    #     👉 所以夹具改用**沙箱**：`data_dir()` 和 `tables_dir()` 一起落到临时目录，
    #        "只缺 EGM"就是代码真正看到的状态。谁也不用去猜那道门读的是哪个目录。
    from tests.sandbox import sandbox

    with sandbox() as (tmp, _):
        bare = tmp / "data" / "tables"
        bare.mkdir(parents=True, exist_ok=True)
        for name, _purpose in catalog.TABLES:
            (bare / name).write_bytes(b"x")  # 非空即可

        # ⚠️ 先验夹具本身：那 10 个都在的话，通用模型门**不该**报缺。
        #    不验的话，下面测的可能还是另一道门 —— 而且看起来一模一样。
        check(
            not fetch.missing_tables(),
            "★ 夹具造对了：通用模型那道门**没有**触发"
            f"（还缺 {len(fetch.missing_tables())} 个 —— 那就测错门了）",
        )

        ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="egmgate_")))
        ws.start_plan(["ALIC"], date(2026, 9, 2), "pod")

        got = GT.do_generate_config(ws, "standard")
        check(
            '"error"' in got,
            "★ 缺 EGM 时返回的是**错误**，不是一份配置",
        )
        check(
            "EGM2008" in got,
            "★ 错误里**指名道姓**说了缺哪个文件（不是笼统的「缺文件」）",
        )
        check(
            "错" in got or "轨道" in got,
            "★ 而且说清了后果是**结果会错** —— 不是「跑不了」",
        )
        # ⚠️ 比的是**转义之后**的形式。
        #
        #    `got` 是 JSON **文本**，而 Windows 路径里的 `\` 在 JSON 里
        #    会被转义成 `\\`。拿未转义的 `str(path)` 去比是比不到的 ——
        #    而那种失败看起来像"消息里没写路径"，其实写了。
        #    （这条是跑出来的：第一版就是这么比的。）
        import json as _json

        escaped = _json.dumps(str(bare))[1:-1]
        check(
            escaped in got,
            f"★ 还告诉了用户该放到哪个目录（找 {escaped!r}）",
        )

        # ---- 对照：同一份夹具，PPP 必须**照常生成** ----
        #
        # ⚠️ 不查这一条的话，一个"永远拒绝"的实现也能通过上面那几条。
        #    守卫必须能区分"该拦的拦住了"和"什么都拦"。
        ws2 = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="pppok_")))
        ws2.start_plan(["ALIC"], date(2026, 9, 2), "ppp")
        got2 = GT.do_generate_config(ws2, "standard")
        check(
            '"error"' not in got2,
            "★ 同一份夹具下 PPP **照常出配置**（门不是无条件关着的）",
        )


def test_plan_run_carries_the_workflow() -> None:
    section("⑱i ★ 取数计划：带上工作流，而且降级要**报出来**")

    ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="wfplan_")))

    # ---- 不给：沿用默认，回执里也要有 ----
    r = json.loads(GT.do_plan_run(ws, ["ALIC"], "2026-09-02"))
    check(r["workflow"]["key"] == "ppp", f"★ 不给就是默认：{r['workflow']['key']}")
    check("warning" not in r, "★ 没拼错就不该有 warning")

    # ---- 给 pod：回执里是 pod，而且带它自己那句提醒 ----
    r2 = json.loads(GT.do_plan_run(ws, ["ALIC", "DARW"], "2026-09-02", "pod"))
    check(r2["workflow"]["key"] == "pod", f"★ pod 生效了：{r2['workflow']['key']}")
    check(
        bool(r2["workflow"].get("note")),
        "★ POD 自己那句提醒（要**全球分布**的站网）被带进回执了 —— "
        "不带给用户的话，他会拿两个相邻站去定轨",
    )
    check(
        "purpose" in r2["workflow"],
        "★ 回执里有一句话用途 —— 模型才说得清'这次要做什么'",
    )

    # ---- 拼错：**必须如实报出来** ----
    #
    # ⚠️⚠️ `workflows.get` 的约定是"取不到就回 ppp，不抛异常"，
    #     所以这里**特别容易被静默**：模型以为 POD 生效了，
    #     实际跑的是 PPP，而两边对结果的预期完全不同。
    r3 = json.loads(GT.do_plan_run(ws, ["ALIC"], "2026-09-02", "PPPD"))
    check(r3["workflow"]["key"] == "ppp", "★ 拼错的会退回 ppp（这是既有约定）")
    check(
        "warning" in r3 and "PPPD" in r3["warning"],
        f"★ **但是报出来了**，而且指名道姓说清退回哪个：{r3.get('warning','（没有！）')[:60]}",
    )




def _config_with_all_inputs(workflow: str, profile: str | None = None):
    """造一份**每个字段都填满**的配置 —— 用来看"这个工作流到底用哪几类文件"。

    ⚠️ 判据是"**配置里真的出现了**"，不是"我传进去了" ——
       `_build_handwritten` 会把工作流不要的字段丢掉（比如 PPP 没有 `egm_files`），
       那正是我们要看的东西。
    """
    from ginan_agent.ginan import config as gconfig
    from ginan_agent.ginan import workflows as gworkflows

    # ⚠️ 档位**从工作流自己允许的那几个里取**（2026-09-27 改）——
    #    原来写死 `"standard"`。那对 POD 是对的（它只允许 standard），
    #    但对 `brdc2sp3` 是**一个它会拒绝的档位**（它只允许 reduced）：
    #    拿一个真跑起来会被 `do_generate_config` 拦下的档位去做夹具，
    #    看到的就不是"这个工作流真实的样子"。
    if profile is None:
        profile = gworkflows.get(workflow).profiles[0]

    return gconfig.build_config(
        gconfig.ConfigInputs(
            config_id="MARKER",
            inputs_root=Path("INROOT"), obs_root=Path("OBSROOT"),
            outputs_root=Path("OUTROOT"),
            workflow=workflow, profile=profile,
            atx_files=["a.atx"], erp_files=["e.erp"],
            sp3_files=["s.SP3"], clk_files=["c.CLK"], nav_files=["n.rnx"],
            rnx_inputs=["o.rnx"], snx_files=["x.snx"], bias_files=["b.BIA"],
            egm_files=["tables/EGM2008.gfc"],
            # ⚠️ SBAS 那一类也要填 —— 这是"**每个字段都填满**"的夹具，
            #    漏一个字段就等于"这条守卫看不到那个类别"。
            #    实测踩过：漏掉 `ems_files` 时，⑱j 报的是
            #    「计划里多下了 `ems`」——**反了**，真因是夹具没给。
            ems_files=["h00.ems"],
            stations={_POD_TEST_STATION: (1.0, 2.0, 3.0)},
        )
    )


def _config_file_kinds(cfg) -> set[str]:
    """配置里**真正用到**的文件类别。显式栈遍历，不递归。

    ⚠️ 映射用 `catalog.KEY_KINDS`（**不是**在这里再写一份）——
       `config._substitutions` 用同一张表决定"哪些键不写进配置"，
       两份的话"丢掉的"和"检查的"迟早漂开。
    """
    from ginan_agent.ginan import catalog as _catalog

    out: set[str] = set()
    stack: list = [cfg]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _catalog.KEY_KINDS:
                    kind = _catalog.KEY_KINDS[k]
                    # ⚠️ **空列表 = 没用它** —— 那正是"下载失败所以没写进配置"的样子。
                    if kind and v:
                        out.add(kind)
                else:
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return out


def test_workflow_needs_covers_what_the_config_uses() -> None:
    section("⑱j ★ 取数计划要下的类别，必须覆盖配置里真正用到的")

    from ginan_agent.ginan import catalog as _catalog
    from ginan_agent.ginan import workflows as gworkflows

    # ⚠️⚠️ 为什么要有这一条 —— 它是有来历的：
    #
    #     `Workflow.needs` 原来是**死字段**（全项目没人读）。
    #     2026-09-24 让它真正生效之后，**立刻发现 `pod.needs` 里没写 `sp3`** ——
    #     于是 POD 的取数计划里没有先验轨道文件，而配置里那一项会是空的。
    #
    #     ⚠️ 那个洞**在 `needs` 是死字段的时候一直存在**，谁都不知道：
    #        改它没效果，所以没人看它，也就没人发现它写错了。
    #
    #     👉 这条守卫把两份清单**锁在一起**：
    #        "配置里用到的类别" ⊆ "计划里会下的类别"。
    #        任何一边改了而另一边没跟上，这里就红。
    for key, wf in gworkflows.WORKFLOWS.items():
        cfg = _config_with_all_inputs(key)
        used = _config_file_kinds(cfg)
        planned = {
            f.kind
            for f in _catalog.plan_run(["ALIC", "DARW"], date(2026, 9, 2), key).files
        }
        missing = sorted(used - planned)
        check(
            not missing,
            f"★ {key}：配置里用到 {sorted(used)}，计划里都有",
        )
        if missing:
            print(f"        ⚠️ 计划里**没有**：{missing} —— "
                  f"配置要它、但没人去下它，Ginan 会报缺文件")

        # 反向：计划下了、配置一个字都不用的类别 —— 白下。
        unused = sorted(planned - used)
        check(
            not unused,
            f"★ {key}：计划里没有**白下**的类别（多下：{unused}）",
        )


# ⚠️⚠️ **最慢那一档、最坏那一段**的速率（秒/历元）—— 不是"POD 有多快"，
#    是**"超时够不够"的判据**（见 ⑱k）。真要问 POD 快不快，看 `ginan-facts` 那张表。
#
# ⚠️⚠️ **2026-09-27 从 16.7 改成 101 —— 因为这个常数原来取的是"错的档位"。**
#
#    它原来取自 **46 站**（我们自己的数据，16.7 秒/历元）。
#    但 `run_timeout` 要挡的是**最慢的那一档**，而 46 站不是。
#
#    87 站（官方 2019-07-18，`ctor87.yaml`）逐历元实测，**跑到第 247 个历元**
#    （288 个里的 86%，被会话结束杀掉，不是跑崩）：
#
#        第 1~4 历元      平均 27.3 秒
#        第 5~27 历元     平均 84.9 秒（**中位 101.3 秒**）← 持续 23 个历元的平台
#        第 28~120 历元   平均 38.3 秒（中位 30.3）
#        第 121~200 历元  平均 40.3 秒（中位 32.0）
#        第 201~247 历元  平均 44.2 秒（中位 32.4）   ← 在慢慢涨
#
#    ⚠️ **实测的前向总时长：3.03 小时（247 个历元）+ 外推剩下 41 个 ≈ 3.5 小时。**
#       ⇒ **8 小时本来是够的**，这一点必须说清楚，别让这一栏看起来像"救了一轮"。
#
#    ⚠️⚠️ **那为什么还取 101、还是把 `run_timeout` 放到 16 小时？**
#       两个理由，第二个是主要的：
#
#        ① **稳的不一定是稳态。** 我自己就在这里栽过：只看第 5~14 历元，
#           以为 105 秒是稳态，算出"前向 8.6 小时"；再往下看，第 28 历元
#           掉到 39 秒。⇒ **窗口里看着最顺眼的数不能当判据。**
#
#        ② **这一栏是"整个工作流"的常数，它不知道用户选了几个站。**
#           87 站是本项目**实测过的最大站网**，不是用户能选的最大。
#           占用和耗时都随站数涨（磁盘是 1.9 倍站数 → 3.3 倍占用，**超线性**），
#           一个 150 站的站网按同样形状外推就超过 8 小时了。
#           ⇒ 按这一栏自己的原则——**宁可大了白等（用户随时能取消），
#             也不能小了把几小时的活杀掉。**
#
#    👉 判据取 **101**（**全程观测到的最坏速率**）：need = 101 × 288 = **29088 秒**，
#       ⇒ **把 `run_timeout` 改回 28800（8 小时）会红。**
#       ⚠️ 那条红线保护的是"顶穿"这件事，不是"这一轮当时够不够用"。
_POD_SECONDS_PER_EPOCH = 101.0


def test_run_timeout_comes_from_the_workflow() -> None:
    section("⑱k ★ 超时按工作流取 —— 通用那个会把**正常的 POD** 杀掉")

    from ginan_agent.ginan import runner, workflows as gworkflows

    pod = gworkflows.WORKFLOWS["pod"]
    ppp = gworkflows.WORKFLOWS["ppp"]

    # ---- 前提：通用默认确实不够 ----
    #
    # ⚠️⚠️ 这一条是**判据的判据**。
    #
    #     `runner.DEFAULT_TIMEOUT` 的文档写着它的用途是
    #     「防止无限挂起（比如那个 OpenBLAS 的坑），**不是用来限制正常处理的**」。
    #     而实测一整天 POD 要 80 分钟 > 它给的 60 分钟 ——
    #     所以它**正在限制一轮完全正常的处理**：那轮跑到约 210/288 历元
    #     被 app 自己杀掉，用户看到的是"跑一半没了"。
    #
    #     ⚠️ 如果哪天通用默认被调大到够 POD 用了，
    #        这一条会红，而正确的反应是**把 `pod.run_timeout` 删掉**，
    #        不是把这条测试改大 —— 留一个不起作用的字段就是又造一个死字段。
    need = _POD_SECONDS_PER_EPOCH * 288
    check(
        runner.DEFAULT_TIMEOUT < need,
        f"★ 前提成立：通用默认 {runner.DEFAULT_TIMEOUT:.0f} 秒 "
        f"< 一整天 POD 实测所需 {need / 60:.0f} 分钟",
    )
    check(
        pod.run_timeout is not None and pod.run_timeout > need,
        f"★ POD 有自己更大的超时：{pod.run_timeout} 秒（需 > {need:.0f}）",
    )
    check(
        ppp.run_timeout is None,
        "★ PPP **不动** —— 它用通用默认（实测十几分钟一轮，够）",
    )

    # ---- 真正的接缝：`do_run_processing` 有没有把它传下去 ----
    #
    # ⚠️⚠️ 只查表里那两个值是不够的 —— 那两栏完全可能是**死的**。
    #     这个仓库已经有过一个死字段（`Workflow.needs`）：一边声称在用、
    #     一边全项目没有一处读它，改它一点效果都没有。
    #     所以这里**真的调一次** `do_run_processing`，把传给 `run_pea`
    #     的关键字参数抓下来看。
    captured: dict = {}

    class _StubResult:
        """只做到 `do_run_processing` 会碰的那几样。"""

        ok = True
        cancelled = False

        def to_json(self) -> str:
            return '{"ok": true}'

    def _stub(config_path, work_dir, **kw):  # type: ignore[no-untyped-def]
        captured.clear()
        captured.update(kw)
        return _StubResult()

    real_run_pea = runner.run_pea
    try:
        runner.run_pea = _stub  # type: ignore[assignment]
        for key, expect in (("pod", pod.run_timeout), ("ppp", runner.DEFAULT_TIMEOUT)):
            ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix=f"to_{key}_")))
            ws.start_plan(["ALIC"], date(2026, 9, 2), key)
            # ⚠️ 直接给一个路径就够 —— 这道门只判"有没有"，
            #    而我们要测的是它**后面**那一段。
            ws.config_path = ws.work_dir / "config.yaml"

            # ⚠️ 走**试跑**这条分支：`max_epochs` 会跳过磁盘检查。
            #    不跳的话，这一条会在**磁盘紧张的机器上假失败** ——
            #    而且失败的样子是"timeout 没传下去"，和真实原因（磁盘不够）
            #    差了十万八千里。守卫必须只测它声称要测的那件事。
            #    磁盘那道门由 ⑱l 单独守。
            GT.do_run_processing(ws, ToolContext.bare("t"), max_epochs=8)

            check(
                captured.get("timeout") == expect,
                f"★ {key}：传给 run_pea 的 timeout = {captured.get('timeout')}"
                f"（应为 {expect}）",
            )
    finally:
        runner.run_pea = real_run_pea  # type: ignore[assignment]


# ⚠️⚠️ **最慢那一档** POD 实测要多少磁盘（GB）—— 和 `_POD_SECONDS_PER_EPOCH`
#    同一个道理：守的是**最坏情况**，不是常见情况。
#
#    ⚠️ 原来写 25.0，出处是「46 站 51 MB/历元 × 288 = 15 GB；87 站约 18 GB」。
#       **其中「87 站约 18 GB」是错的** —— 它来自一轮**只跑了 7 个历元**的尝试。
#
#    2026-09-27 按历元实测 87 站（`ctor87.yaml`，两个独立窗口）：
#        124 MB/历元（第 4~16 历元） / 132 MB/历元（第 16~22 历元）
#        ⇒ 288 历元光**前向**就约 **37 GB**，再加反向平滑 ≈ **50 GB**
#
#    ⚠️ 后果和超时那条一模一样：按 25 算，`disk_need_gb` 取多少都好过 ——
#       **把 25 改回去也不会红。** 而那会让剩 30 GB 的用户通过检查，
#       然后在跑了几小时之后磁盘满。
#
#    ⚠️ 真正的产物（SP3/OBX/CLK/SNX/POS）只有几 MB ——
#       占地方的**全是 Ginan 的 RTS 状态文件**，不是结果。
_POD_DISK_NEED_GB = 50.0


def test_disk_gate_blocks_pod_before_it_starts() -> None:
    section("⑱l ★ 磁盘不够时，POD **开始前就拒绝**（不是跑到一半才满）")

    import shutil as _shutil

    from ginan_agent.ginan import runner, workflows as gworkflows

    pod = gworkflows.WORKFLOWS["pod"]
    check(
        pod.disk_need_gb >= _POD_DISK_NEED_GB,
        f"★ POD 的磁盘需求 ≥ 实测值：{pod.disk_need_gb} GB",
    )
    # ⚠️ 对照：通用那道检查（`environment._check_disk`）的阈值是 2 GB，
    #    而 POD 要几十 GB —— **它远远不够**，这正是这一栏存在的理由。
    check(
        gworkflows.WORKFLOWS["ppp"].disk_need_gb < pod.disk_need_gb,
        "★ PPP 的需求明显更小（它不写 RTS）—— 门槛不是无差别抬高",
    )

    class _Usage:
        def __init__(self, free_gb: float) -> None:
            self.free = int(free_gb * 1024**3)

    ran: list = []

    class _StubResult:
        ok = True
        cancelled = False

        def to_json(self) -> str:
            return '{"ok": true}'

    def _stub(config_path, work_dir, **kw):  # type: ignore[no-untyped-def]
        ran.append(kw)
        return _StubResult()

    real_usage = _shutil.disk_usage
    real_run_pea = runner.run_pea
    try:
        runner.run_pea = _stub  # type: ignore[assignment]

        def _mk(key: str):  # type: ignore[no-untyped-def]
            ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix=f"disk_{key}_")))
            ws.start_plan(["ALIC"], date(2026, 9, 2), key)
            ws.config_path = ws.work_dir / "config.yaml"
            return ws

        # ---- ① 磁盘不够：拒绝，而且**根本没起 pea** ----
        #
        # ⚠️⚠️ **一律按 `pod.disk_need_gb` 现算，不写死数字。**
        #    这里原来写的是 `_Usage(10.0)` / `"25" in got` ——
        #    而 2026-09-27 把 disk_need_gb 从 25 改成 64 时，
        #    `"25" in got` 立刻红了（**这是对的**，它提醒了改动），
        #    但红的原因是"字面量对不上"，不是"行为变了"。
        #    ⇒ 写死数字的守卫，改一次取值就得改一次测试，
        #      而改测试的人很容易顺手把**断言也改松**。
        need = pod.disk_need_gb
        low = round(need / 6, 1)          # 明显不够
        high = round(need * 1.6, 1)       # 明显够

        _shutil.disk_usage = lambda _p: _Usage(low)  # type: ignore[assignment]
        ran.clear()
        got = GT.do_run_processing(_mk("pod"), ToolContext.bare("t"))
        check('"error"' in got, f"★ 剩 {low} GB < 需要 {need:g} GB → 拒绝")
        check(
            not ran,
            "★ **而且没有起 pea** —— 拒绝必须是「开始前」，不是「跑起来再说」",
        )
        check(
            f"{need:.0f}" in got and f"{low:.1f}" in got,
            f"★ 消息里**两个数都给了**（要多少 / 还剩多少）：{got[:100]}",
        )
        check(
            "RTS" in got,
            "★ 还说了占地方的是什么 —— 不然用户会以为结果本身有几十 GB",
        )

        # ---- ② 磁盘够：放行 ----
        _shutil.disk_usage = lambda _p: _Usage(high)  # type: ignore[assignment]
        ran.clear()
        got2 = GT.do_run_processing(_mk("pod"), ToolContext.bare("t"))
        check('"error"' not in got2, f"★ 剩 {high} GB ≥ {need:g} GB → 放行")
        check(bool(ran), "★ 而且真的起了 pea")

        # ---- ③ 磁盘不够但只是**试跑**：放行 ----
        #
        # ⚠️ 不查这一条的话，一个"只要磁盘小于 need 就拒绝一切"的实现
        #    也能通过上面两条 —— 而那会把**磁盘紧张的用户最该做的事**
        #    （先试跑几个历元看看对不对）给堵死。
        _shutil.disk_usage = lambda _p: _Usage(round(need / 20, 1))  # type: ignore[assignment]
        ran.clear()
        got3 = GT.do_run_processing(_mk("pod"), ToolContext.bare("t"), max_epochs=8)
        check(
            '"error"' not in got3,
            "★ 试跑（max_epochs）不受磁盘门限制 —— 占用按历元分摊，拿整天的需求拦它是拦错了",
        )
        check(bool(ran), "★ 试跑真的起了 pea")

        # ---- ④ **同一个磁盘量下**，PPP 照常放行（门槛不是无差别的）----
        #
        # ⚠️ 用 `low`（把 POD 挡掉的那个值）—— 这才叫"门槛不是无差别的"。
        #    原来这里写死 10.0，而那时 POD 的门槛是 25；**两者是巧合对齐的**，
        #    改过一次取值之后就不一定了。
        _shutil.disk_usage = lambda _p: _Usage(low)  # type: ignore[assignment]
        ran.clear()
        got4 = GT.do_run_processing(_mk("ppp"), ToolContext.bare("t"))
        check('"error"' not in got4, f"★ 同一个 {low} GB 夹具下 PPP 放行")
    finally:
        _shutil.disk_usage = real_usage  # type: ignore[assignment]
        runner.run_pea = real_run_pea  # type: ignore[assignment]


def test_output_filenames_are_safe_on_windows() -> None:
    section("⑱m ★★ 产物文件名不许出现 `<LOGTIME>` —— Windows 上会**静默没有产物**")

    from ginan_agent.ginan import config as gconfig  # noqa: F401
    from ginan_agent.ginan import workflows as gworkflows

    # ⚠️⚠️ 这一条守的是实测撞出来的一个**静默**失败（2026-09-25）：
    #
    #     Ginan 把 `<LOGTIME>` 展开成 `<YYYY>-<MM>-<DD>_<HH>:<mm>` ——
    #     **中间带一个冒号**，而 Windows 文件名不允许 `:` ⇒
    #     文件创建失败，**而且不报任何错**。
    #
    #     实测：官方 LEO 示例跑满 **8640 历元**、`PEA finished`、**退出码 0**，
    #     而 `out/` 里**一个 `.SP3` 都没有**。加一行显式文件名立刻出 22.6 MB。
    #
    #     ⚠️ 而且 Ginan 里**几乎所有产物文件名的默认值都含它**
    #        （pos / clocks / orbex / sinex / gpx / log / ionex / erp / bia …）。
    #        ⇒「启用了但没写 `filename`」的产物在 Windows 上都会静默消失。
    #
    #     ⚠️ app 原本"免疫"只是因为 `pod_example.yaml` **恰好**写了安全的名字。
    #        **碰巧安全不是安全** —— 所以 app 侧强制过一道
    #        （`config._force_safe_output_filenames`），这里守住它。
    #
    #     详见 `docs/ginan-facts.md` 七点十一。
    for key in gworkflows.choices():
        cfg = _config_with_all_inputs(key)
        out = cfg.get("outputs") or {}

        # ---- ① 全配置里不许出现 <LOGTIME>（显式栈遍历，不递归）----
        hits: list[str] = []
        stack: list[tuple[str, object]] = [("outputs", out)]
        while stack:
            path, node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and "<LOGTIME>" in v:
                        hits.append(f"{path}.{k}")
                    elif isinstance(v, (dict, list)):
                        stack.append((f"{path}.{k}", v))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    if isinstance(v, str) and "<LOGTIME>" in v:
                        hits.append(f"{path}[{i}]")
                    elif isinstance(v, (dict, list)):
                        stack.append((f"{path}[{i}]", v))

        check(
            not hits,
            f"★ {key}：产物里没有 `<LOGTIME>`"
            f"（命中：{hits or '无'}）—— 它在 Windows 上展开出冒号，文件写不出来",
        )

        # ---- ② sp3 要输出就必须有**显式**文件名 ----
        #
        # ⚠️ 为什么单独管 sp3：它是**实测撞到过**的那一个
        #    （默认值 `<CONFIG>-<LOGTIME>_<SYS>-Filt.sp3`）。
        #    别的产物只守"值里不含 <LOGTIME>"，不替它们编默认名 —— 那是猜。
        sp3 = out.get("sp3")
        if isinstance(sp3, dict) and sp3.get("output"):
            check(
                bool(sp3.get("filename")),
                f"★ {key}：sp3 要输出就**必须**有显式 `filename`"
                "（不能靠默认值 —— 它含 `<LOGTIME>`）",
            )

    # ---- ③ 直接测那个收尾函数本身 ----
    #
    # ⚠️⚠️ 为什么**必须**直接测：**当前两个工作流的模板恰好都是安全的**
    #     （`pod_example.yaml` 写的就是不含 `<LOGTIME>` 的名字，
    #       PPP 那条是手写的、本来就显式）。
    #     ⇒ 上面那个"跑一遍所有工作流"的循环**永远碰不到这个修复** ——
    #       把 `_force_safe_output_filenames` 整个删掉，它照样全绿。
    #     ⇒ **一个测不到自己声称要测的东西的守卫，等于没有。**
    #       （这不是假想：第一版就是这么写的，删掉函数一跑还是绿的。）
    bad = {
        "outputs": {
            "sp3": {"output": True, "filename": "<CONFIG>-<LOGTIME>_<SYS>-Filt.sp3"}
        }
    }
    gconfig._force_safe_output_filenames(bad)
    got = bad["outputs"]["sp3"]["filename"]
    check("<LOGTIME>" not in got, f"★ 带 `<LOGTIME>` 的名字被换掉了：{got!r}")
    # ⚠️ 断言**精确相等**，不是 `endswith(".SP3")` ——
    #    Ginan 那个默认值的扩展名是**小写** `.sp3`，
    #    我第一版按大写断言，于是守卫红了，而**红的是我的断言不是代码**。
    check(
        got == "<CONFIG>-<YYYY><DDD><HH>_<SYS>-Filt.sp3",
        f"★ 只换掉那段占位符，名字其余部分**逐字保留**：{got!r}",
    )

    filled = {"outputs": {"sp3": {"output": True}}}
    gconfig._force_safe_output_filenames(filled)
    check(
        bool(filled["outputs"]["sp3"].get("filename")),
        "★ sp3 开了却没写名字 → 补一个（不然用上 Ginan 那个带 `<LOGTIME>` 的默认值）",
    )

    # ---- 对照：**没开**的 sp3 不许被动 ----
    #
    # ⚠️ 不查这一条的话，一个"给每个产物都塞个名字"的实现也能通过上面两条 ——
    #    而那是**无根据地改用户的配置**。
    off = {"outputs": {"sp3": {"output": False}}}
    gconfig._force_safe_output_filenames(off)
    check(
        "filename" not in off["outputs"]["sp3"],
        "★ 对照：没开 `output` 的 sp3 **不许**被塞名字",
    )

    # ---- ④ 接缝：`build_config` 真的调了那个函数吗 ----
    #
    # ⚠️⚠️ 上面 ③ 是**直接调**那个函数的 —— 所以**把 `build_config` 里
    #     那一行删掉，③ 照样绿**。而"改了函数但忘了在真路上调用"
    #     正是这个仓库吃过的亏（`Workflow.needs` 当死字段的那一段）。
    #     ⇒ 这里把模板换成一个**带 `<LOGTIME>` 的**，逼接缝自己证明它接上了。
    from ginan_agent.ginan import examples as gexamples

    real_load = gexamples.load
    try:

        def _dirty(name: str):  # type: ignore[no-untyped-def]
            cfg = real_load(name)
            cfg["outputs"]["sp3"]["filename"] = "<CONFIG>-<LOGTIME>_<SYS>-Filt.sp3"
            return cfg

        gexamples.load = _dirty  # type: ignore[assignment]
        polluted = _config_with_all_inputs("pod")
        got2 = polluted["outputs"]["sp3"]["filename"]
        check(
            "<LOGTIME>" not in got2,
            f"★ 接缝：模板带 `<LOGTIME>` 时，`build_config` 出来的必须已经被换掉"
            f"（拿到 {got2!r}）—— 没换说明那个收尾**没接在真路上**",
        )
    finally:
        gexamples.load = real_load  # type: ignore[assignment]


# =============================================================================
# ⑲ ④ 那一轮：`brdc2sp3` + `compare_orbits`
# =============================================================================
#
# 这一节能存在，是因为两条**实测**发现的产品级 bug（2026-09-27）：
#
#     ⑲a  `epoch_control.end_epoch` 在这个版本的 Ginan 里**完全不起作用**，
#          唯一能刹住历元循环的是 `-n`。没有观测的工作流会跑到天荒地老。
#     ⑲d  Ginan 在 Windows 上把 SP3 第 2 个历元起的行首写成 `E*`，
#          而它自己的读取器只认 `*` ⇒ **它读不懂自己的产物**。
#
# 两条的现场记录都在 `docs/ginan-facts.md` 七点十三 / 七点十四。


def test_run_is_bounded_by_epoch_count() -> None:
    section("⑲a ★★ 跑多少个历元要**我们自己算** —— end_epoch 是死的")

    from datetime import datetime as _dt

    from ginan_agent.ginan import config as gconfig
    from ginan_agent.ginan import runner, workflows as gworkflows

    # ---- ① 算得对不对：三个数都要和**实测跑出来的**逐位相同 ----
    #
    # ⚠️ 这三个数不是"86400/间隔"推出来的，是**跑出来过的**：
    #     POD 官方示例日志 `Processed epoch #288 … 23:55`，
    #     brdc2sp3 的产物头 `## …  900.00000000 …… 96 ORBIT`（96 个历元），
    #     PPP 一天 30 秒 = 2880 是这个量的两倍关系，同样是标准值。
    #
    #     用 `floor` 的话是 287/95/2879 —— **少一个历元**，
    #     而少一个历元不会报错，只会让最后一刻钟的结果没算。
    cases = [("pod", 300.0, 288), ("brdc2sp3", 900.0, 96), ("ppp", 30.0, 2880)]
    for label, interval, expect in cases:
        inp = gconfig.ConfigInputs(
            config_id="N", inputs_root=Path("I"), obs_root=Path("O"),
            outputs_root=Path("X"), workflow=label,
            epoch_interval=interval,
            start_epoch=_dt(2026, 9, 2), end_epoch=_dt(2026, 9, 3),
        )
        got = gconfig.epoch_count(inp)
        check(
            got == expect,
            f"★ {label}：一天 / {interval:.0f} 秒 → {got} 个历元（应为 {expect}）",
        )

    # ---- ② 算不出来时**必须返回 None**，不许猜 ----
    #
    # ⚠️ 猜一个默认值的后果是**静默截断**：跑到一半停下，
    #    而截断后的 POS/SP3 和正常跑完的长得一模一样。
    for label, kw in (
        ("没给起止时间", {}),
        ("起止反了", {"start_epoch": _dt(2026, 9, 3), "end_epoch": _dt(2026, 9, 2)}),
    ):
        inp = gconfig.ConfigInputs(
            config_id="N", inputs_root=Path("I"), obs_root=Path("O"),
            outputs_root=Path("X"), epoch_interval=300.0, **kw
        )
        check(
            gconfig.epoch_count(inp) is None,
            f"★ {label} → 返回 None（不传 -n），而不是猜一个数",
        )

    # ---- ③ 接缝：`do_run_processing` 真的把它传下去了吗 ----
    #
    # ⚠️ 只测 `epoch_count` 是不够的 —— 那个函数完全可能是**死的**
    #    （`Workflow.needs` 就当过死字段）。这里抓传给 `run_pea` 的 kwargs。
    captured: dict = {}

    class _StubResult:
        ok = True
        cancelled = False

        def to_json(self) -> str:
            return '{"ok": true}'

    def _stub(config_path, work_dir, **kw):  # type: ignore[no-untyped-def]
        captured.clear()
        captured.update(kw)
        return _StubResult()

    real_run_pea = runner.run_pea
    try:
        runner.run_pea = _stub  # type: ignore[assignment]

        def _mk(workflow: str, interval: float):
            w = Workspace(work_dir=Path(tempfile.mkdtemp(prefix=f"n_{workflow}_")))
            w.start_plan(["ALIC"], date(2026, 9, 2), workflow)
            w.config_path = w.work_dir / "config.yaml"
            w.config_inputs = gconfig.ConfigInputs(
                config_id="N", inputs_root=Path("I"), obs_root=Path("O"),
                outputs_root=Path("X"), workflow=workflow,
                epoch_interval=interval,
                start_epoch=_dt(2026, 9, 2), end_epoch=_dt(2026, 9, 3),
            )
            return w

        GT.do_run_processing(_mk("pod", 300.0), ToolContext.bare("t"))
        check(
            captured.get("max_epochs") == 288,
            f"★ POD：传给 run_pea 的 max_epochs = {captured.get('max_epochs')}（应为 288）",
        )

        GT.do_run_processing(_mk("brdc2sp3", 900.0), ToolContext.bare("t"))
        check(
            captured.get("max_epochs") == 96,
            f"★ brdc2sp3：传给 run_pea 的 max_epochs = {captured.get('max_epochs')}"
            f"（应为 96）—— **没有观测的工作流只有这一个刹车**",
        )

        # ---- ④ 对照：显式给了（试跑）**不许被覆盖** ----
        GT.do_run_processing(_mk("pod", 300.0), ToolContext.bare("t"), max_epochs=7)
        check(
            captured.get("max_epochs") == 7,
            f"★ 对照：显式的 max_epochs=7 没被自动值覆盖"
            f"（实际 {captured.get('max_epochs')}）",
        )

        # ---- ⑤ 对照：算不出来时**不传**，不是传个猜的数 ----
        w = _mk("pod", 300.0)
        w.config_inputs = gconfig.ConfigInputs(
            config_id="N", inputs_root=Path("I"), obs_root=Path("O"),
            outputs_root=Path("X"), workflow="pod", epoch_interval=300.0,
        )
        GT.do_run_processing(w, ToolContext.bare("t"))
        check(
            captured.get("max_epochs") is None,
            f"★ 对照：没给起止时间 ⇒ 不传 -n（实际 {captured.get('max_epochs')}）",
        )
    finally:
        runner.run_pea = real_run_pea  # type: ignore[assignment]

    # ---- ⑥ 接缝：`make_config_inputs` 真的把时段填进去了吗 ----
    #
    # ⚠️⚠️ 这一条守的是另一个洞：`make_config_inputs` **原来从来没设过**
    #     `start_epoch`/`end_epoch`（2026-09-27 才发现）。后果分两种：
    #
    #        模板里有这两个键的（`brdc2sp3.yml`）→ 用**模板硬编码的 2019-07-18**
    #        模板里没有的（`pod_example.yaml`）  → 配置里根本没这两个键
    #
    #     第一种是**静默算错**：用户要跑 2026-09-02，配置写着 2019-07-18，
    #     和手上的 nav 文件对不上，而 Ginan 不报错，只算出一条空轨道。
    ws = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="span_")))
    ws.start_plan(["ALIC"], date(2026, 9, 2), "brdc2sp3")
    inp = ws.make_config_inputs("reduced")
    check(
        inp.start_epoch == _dt(2026, 9, 2) and inp.end_epoch == _dt(2026, 9, 3),
        f"★ 时段来自**用户要的那一天**：{inp.start_epoch} → {inp.end_epoch}"
        f"（应为 2026-09-02 00:00 → 2026-09-03 00:00）",
    )
    check(
        gconfig.epoch_count(inp) == 96,
        f"★ 而且它算得出历元数：{gconfig.epoch_count(inp)}",
    )


def test_brdc2sp3_config_only_uses_files_it_needs() -> None:
    section("⑲b ★ brdc2sp3 的配置里**不许**出现它不用的文件")

    from ginan_agent.ginan import config as gconfig
    from ginan_agent.ginan import workflows as gworkflows

    # ⚠️⚠️ 这一条守的是**静默算错**，不是"配置难看"。
    #
    #     `brdc2sp3` 的**全部意义**是「从**广播星历**算出一条轨道」。
    #     如果配置里同时出现 `clk_files`（精密钟差）和
    #     `pseudo_observations.sp3_inputs`（**当成观测值的**精密轨道），
    #     那它对 `outputs.sp3` 的取数就未必还落在 BROADCAST 上了
    #     （默认源序 `{KALMAN, PRECISE, BROADCAST}`，`acsConfig.hpp:325`）
    #     ⇒ **产出的东西和"广播星历转轨道"不是一回事，而没有一处会报错。**
    #
    #     ⚠️ `_substitutions` 原来是**见非空就写**的 —— 正常流程下看不出问题，
    #        因为 `plan_run` 按 `needs` 筛过、那些字段本来就是空的。
    #        但那是**靠上游恰好没给**，不是**这里保证了不要**。
    wf = gworkflows.WORKFLOWS["brdc2sp3"]
    cfg = _config_with_all_inputs("brdc2sp3")
    inputs = cfg.get("inputs", {})

    for key in ("gnss_observations", "pseudo_observations", "erp_files"):
        check(key not in inputs, f"★ inputs 里没有 `{key}`（它不需要）")
    check(
        "clk_files" not in (inputs.get("satellite_data") or {}),
        "★ inputs.satellite_data 里没有 `clk_files`",
    )
    check(
        "egm_files" not in inputs,
        "★ inputs 里没有 `egm_files`（重力场只有 POD 要，走 extra_inputs）",
    )
    for key in ("atx_files", "snx_files"):
        check(bool(inputs.get(key)), f"★ inputs 里有 `{key}`（它要）")
    check(
        bool((inputs.get("satellite_data") or {}).get("nav_files")),
        "★ inputs.satellite_data 里有 `nav_files`（它就是干这个的）",
    )

    # ---- 对照：POD 那几个键**必须还在** ----
    #
    # ⚠️ 不加这一条的话，一个"把什么都删掉"的实现也能通过上面全部 ——
    #    而那个实现会把 POD 弄坏（它的先验轨道就在 `pseudo_observations`）。
    pod_inputs = _config_with_all_inputs("pod").get("inputs", {})
    for key in ("gnss_observations", "pseudo_observations", "egm_files"):
        check(key in pod_inputs, f"★ 对照：POD 的 `{key}` **还在**")

    # ---- `process_modes` 以表为准 ----
    check(
        cfg["processing_options"]["process_modes"] == dict(wf.modes),
        f"★ process_modes 和表一致：{cfg['processing_options']['process_modes']}",
    )

    # ---- sp3 必须有显式文件名（`<LOGTIME>` 那件事，见 ⑱m）----
    sp3 = (cfg.get("outputs") or {}).get("sp3") or {}
    check(
        bool(sp3.get("output") and sp3.get("filename")),
        f"★ outputs.sp3 开了而且有显式文件名：{sp3.get('filename')!r}",
    )


def test_workflow_delta_does_not_invent_blocks() -> None:
    section("⑲c ★ 对模板的**更正**，不许变成给模板**加东西**")

    from ginan_agent.ginan import config as gconfig

    # ---- ① 直接测那个判据 ----
    present = {"mongo": {"enable": "primary"}}
    got = gconfig._replace_if_present(present, ("mongo", "enable"), "none")
    check(
        got is True and present["mongo"]["enable"] == "none",
        "★ 键在 → 换掉，返回 True",
    )

    absent = {"outputs": {}}
    got2 = gconfig._replace_if_present(absent, ("mongo", "enable"), "none")
    check(
        got2 is False and "mongo" not in absent,
        f"★ 键不在 → **不创建**，返回 False（实际 {absent}）",
    )

    # ⚠️ 中间层缺一层也要 False —— 不能"顺手把中间层补出来"。
    deep = {"satellite_options": {"global": {}}}
    got3 = gconfig._replace_if_present(
        deep,
        ("satellite_options", "global", "orbit_propagation", "solar_radiation_pressure"),
        "cannonball",
    )
    check(
        got3 is False and "orbit_propagation" not in deep["satellite_options"]["global"],
        "★ 中间层不存在 → 也不创建（不是「补出中间层再写」）",
    )

    # ---- ② 两个真模板上各验一遍 ----
    pod = _config_with_all_inputs("pod")
    check(
        pod["satellite_options"]["global"]["orbit_propagation"]
        ["solar_radiation_pressure"] == "cannonball",
        "★ POD：模板写的 `boxwing` 被换成了 `cannonball`",
    )
    check(pod["mongo"]["enable"] == "none", "★ POD：模板写的 `primary` 被换成了 `none`")

    brdc = _config_with_all_inputs("brdc2sp3")
    check(
        "orbit_propagation" not in brdc["satellite_options"]["global"],
        "★ brdc2sp3：模板里没有 `orbit_propagation` ⇒ 配置里也**不许**有"
        "（它不估轨道，这个块对它一个字节都不起作用）",
    )
    check("mongo" not in brdc, "★ brdc2sp3：模板里没有 `mongo` ⇒ 配置里也不许有")


# ⑲e 用的夹具：`compare_orbits` 打在 stdout 上的**真实**片段。
#
# ⚠️ 出处：2026-09-27 实测，`brdc2sp3` 的一天产物（修好 `E*` 之后）
#    对 IGS 精密轨道（`TUG0R03FIN_..._ORB.SP3`）。原文照抄，**没改过数字**。
#
#     ⚠️ 逐星那 124 行只抄了 G01 / G32 两个卫星的 —— 守卫不依赖仓库外
#        的测试数据目录，所以只能抄一小段进来。汇总行是**全的**。
_COMPARE_STDOUT = """Comparing files:
E:/data/TUG0R03FIN_20191990000_01D_05M_ORB.SP3
E:/data/ours_fixed.sp3

+MINCON STATISTICS/2019-07-18 00:00:00
# Position RMS changed in meters:
& Agg-All    - from:   999.000000 to:   999.000000 in 3D over     4 entries
-MINCON STATISTICS/2019-07-18 00:00:00

+MINCON STATISTICS/TOTAL
# Position RMS changed in meters:
& Agg-All    - from:     1.327282 to:     1.319809 in 3D over  2976 entries
& Agg-All    - from:     0.430417 to:     0.438711 in  N over  2976 entries
& Agg-All    - from:     0.662679 to:     0.661025 in  R over  2976 entries
& Agg-All    - from:     1.066431 to:     1.054739 in  T over  2976 entries
& Agg-Used   - from:     1.327282 to:     1.319809 in 3D over  2976 entries
& Agg-Used   - from:     0.430417 to:     0.438711 in  N over  2976 entries
& Agg-Used   - from:     0.662679 to:     0.661025 in  R over  2976 entries
& Agg-Used   - from:     1.066431 to:     1.054739 in  T over  2976 entries
& G01        - from:     0.787782 to:     0.793331 in 3D over    96 entries
& G01        - from:     0.572639 to:     0.594159 in  N over    96 entries
& G01        - from:     0.463060 to:     0.455525 in  R over    96 entries
& G01        - from:     0.279753 to:     0.262387 in  T over    96 entries
& G32        - from:     0.700497 to:     0.695559 in 3D over    96 entries
& G32        - from:     0.521545 to:     0.510737 in  N over    96 entries
& G32        - from:     0.401893 to:     0.411088 in  R over    96 entries
& G32        - from:     0.239099 to:     0.232286 in  T over    96 entries
-MINCON STATISTICS/TOTAL

----- Orbit Comparator -----
"""


def test_comparison_parser_reads_the_real_output() -> None:
    section("⑲e ★ 比对结果解析 —— 拿**真跑出来的** stdout 当夹具")

    from ginan_agent.ginan import sp3file as gsp3

    cmp = gsp3.parse_comparison(_COMPARE_STDOUT)

    check(cmp.satellites == 2, f"★ 逐星行数（夹具里抄了 2 颗）：{cmp.satellites}")
    check(cmp.epochs == 96, f"★ 公共历元数：{cmp.epochs}（应为 96）")
    check(
        cmp.rms_3d == 1.319809,
        f"★ ★ 要报告的那个数：{cmp.rms_3d} 米（应为 1.319809 —— **逐位相同**）",
    )

    # ⚠️⚠️ **只认 `/TOTAL` 那一块。**
    #
    #     夹具里我**故意**在前面放了一个 `/2019-07-18 00:00:00` 的块，
    #     里面的数是 999.0。不截块的话，逐星那些行会被它覆盖/混进来，
    #     而那种错**看不出**（数字看着都合理，只是不是同一个来源）。
    check(
        cmp.rms_3d != 999.0,
        "★ 逐历元那块（999.0）**没有**混进来 —— 只认 `/TOTAL`",
    )

    # ⚠️ `Agg-Used` 排在 `Agg-All` 后面，且两者数字相同 ——
    #    这条是为了确认"先到先得"没被后到的盖掉（数字一样看不出来，
    #    所以直接查名字）。
    check(
        "Agg-All" not in cmp.per_sat and "Agg-Used" not in cmp.per_sat,
        "★ 汇总行没被当成「某颗卫星」塞进逐星表",
    )

    v = gsp3.verdict(cmp)
    check(
        "1.32" in v.headline and v.quality == "fair",
        f"★ 人话结论：{v.headline}（质量档 {v.quality}）",
    )
    check(
        any("两条轨道的差异" in c for c in v.caveats),
        "★ caveats 里说清了「这是差异、不是精度」",
    )

    # ---- 退化情形：只有 1 个公共历元 ----
    #
    # ⚠️ 这一条是**实测撞到过**的：产物没修 `E*` 时 Ginan 只读得到一个历元，
    #    比出来的数是 1,800,000 米 —— 一个**看着像结果**的垃圾数。
    #    结论必须**指名道姓说原因**，不能只说"差异很大"。
    degenerate = gsp3.parse_comparison(
        _COMPARE_STDOUT.replace("to:     1.319809", "to:  2253991.270024")
        .replace("over  2976", "over    31")
        .replace("over    96", "over     1")
    )
    check(degenerate.epochs == 1, f"★ 退化夹具的历元数：{degenerate.epochs}")
    vd = gsp3.verdict(degenerate)
    check(
        vd.quality == "unknown" and "没有意义" in vd.headline,
        f"★ 只有 1 个历元时不下「差异很大」的结论：{vd.headline}",
    )
    check(
        any("历元标签" in c for c in vd.caveats),
        "★ 而且**指出来**最可能的原因是产物标签坏了 —— 这是实测根因，不是猜的",
    )

    # ---- 完全解析不出时的兜底 ----
    check(
        gsp3.parse_comparison("完全不是比对输出").epochs == 0,
        "★ 解析不出任何东西时返回空结果（不抛异常）",
    )


# ⑲d 用的夹具：Ginan 产出的 SP3 的**真实**片段（原文照抄）。
#
# ⚠️ 第 2 个历元的行首是 `E*` 而不是 `*` —— 那是缺陷的现场。
# ⚠️ 数据行里有 `PE01`/`VE01`（Galileo），**它们必须一个字节都不动** ——
#    那就是 `(?m)^E\*` 里那个行首锚存在的全部理由。
_SP3_SNIPPET = """#dV2019  7 18  0  0  0.00000000      96 ORBIT igb14 FIT  GAA
## 2062 345600.00000000   900.00000000 58682 0.0000000000000
/* NOTE: downstream tool flags rows with E* in its report; keep comments verbatim */
+   11   G01G02G03G04G05E01E02E03E04E05R01
*  2019  7 18  0  0  0.00000000
PG01 -12053.996922  21412.164222  -9849.315498    -70.230051               P   P
VE01  -7705.248807   5089.105986  30543.718951     -0.105729               P   P
PE02  12345.678901  -23456.789012  30543.718951     -0.105729               P   P
E*  2019  7 18  0 15  0.00000000
PG01 -12864.494080  21962.714131  -7170.822686    -70.239567               P   P
VG01  -7705.248807   5089.105986  30543.718951     -0.105729               P   P
EOF
"""


def test_sp3_epoch_tag_repair() -> None:
    section("⑲d ★★ 修 SP3 的坏历元标签 —— Ginan 读不懂自己的产物")

    import tempfile as _tf

    from ginan_agent.ginan import runner as _runner
    from ginan_agent.ginan import sp3file as gsp3

    # ---- (a) 行首的 `E*` 要变成 `*` ----
    fixed, n = gsp3.repair_epoch_tags(_SP3_SNIPPET)
    check(n == 1, f"★ 改掉的标签数：{n}（夹具里有 1 个）")
    check(
        "\n*  2019  7 18  0 15  0.00000000" in fixed,
        "★ 第 2 个历元的行首变成了 `*`",
    )

    # ---- (b) ⚠️⚠️ 除了行首那些标签，别的一个字节都不许动 ----
    #
    # ⚠️⚠️ **夹具里的 `/* … E* … */` 那一行是刻意的哨兵。**
    #
    #     实测过（见 `sp3file._BROKEN_EPOCH` 的说明）：在**我们的真产物**上，
    #     `E\*` 和 `(?m)^E\*` 命中数**完全一样**（95 = 95）——
    #     因为数据行是 `PG01`/`VE01` 那种形状，`E` 后面跟的是数字不是 `*`。
    #
    #     ⇒ 也就是说，**只拿真产物当夹具的话，这条守卫永远变不了红**：
    #       把行首锚去掉照样全绿（第一版就是这样，实测确认过）。
    #       **一个变不了红的守卫等于没有。**
    #
    #     而 SP3 的 `/* … */` 是**自由文本**（我们自己的产物里就有 4 行），
    #     所以"注释里出现 `E*`"是个真形状 —— 无锚的写法会把它改成 `*`，
    #     那是**静默改坏一份别人的文件**。夹具里放这一行，锚就是承重的了。
    check(
        "/* NOTE: downstream tool flags rows with E* in its report; "
        "keep comments verbatim */" in fixed,
        "★ ★ 哨兵：**注释行里那个 `E*` 没被动**（行首锚就是为它存在的）",
    )

    before = _SP3_SNIPPET.splitlines()
    after = fixed.splitlines()
    check(len(before) == len(after), "★ 行数没变（没多出/少掉行）")
    for i, (b, a) in enumerate(zip(before, after)):
        if b.startswith("E*"):
            continue
        check(b == a, f"★ 第 {i} 行**逐字未变**：{b[:40]!r}")
    # ⚠️⚠️ **不变量：每修一个标签，正好去掉一个 `E`。**
    #
    #     比逐行比对更强 —— 逐行看的是"内容"，这一条看的是"长度"：
    #     一个"把 `E*` 换成 `*` 但顺手多删了个空格"的实现在逐行上看着也对。
    removed = len(_SP3_SNIPPET) - len(fixed)
    check(
        removed == n,
        f"★ 只短了 {removed} 个字符（每修一个标签正好去掉一个 `E`，应为 {n}）",
    )

    # ⚠️ 用夹具里**真有**的那几个标记。
    #    我第一版写的是 `PE01`，而夹具里是 `VE01`/`PE02` —— **红的是我的断言，
    #    不是代码**（同一轮里第二次犯这个：见 ⑱m 那个 `.SP3` vs `.sp3`）。
    for marker in ("PG01", "VG01", "VE01", "PE02"):
        check(marker in fixed, f"★ 数据行 `{marker}` 还在")

    # ---- (c) 对照：已经干净的文本，改 0 个 ----
    clean, n2 = gsp3.repair_epoch_tags("PG01 1 2 3\n*  2019  7 18  0  0  0\nEOF\n")
    check(n2 == 0 and clean.startswith("PG01"), f"★ 对照：干净文本改 {n2} 个（应为 0）")

    # ---- (d) 接缝：`do_compare_orbits` 真的调了它 ----
    #
    # ⚠️ 上面 (a)(b) 都是**直接调**那个函数的 —— 所以把
    #    `do_compare_orbits` 里那一行删掉，(a)(b) 照样绿。
    #    "改了函数但忘了在真路上调用"正是这个仓库吃过的亏。
    work = Path(_tf.mkdtemp(prefix="cmp_"))
    raw = work / "ours.sp3"
    raw.write_text(_SP3_SNIPPET, encoding="utf-8", newline="")
    truth = work / "truth.sp3"
    truth.write_text(_SP3_SNIPPET.replace("E*", "*"), encoding="utf-8", newline="")

    seen: dict = {}

    # ⚠️⚠️ **stub 用真的 `RunResult`，而且真的把输出写进日志文件。**
    #
    #     第一版的 stub 是个自造的小类，它**自己带了一个 `stdout` 属性** ——
    #     于是 `do_compare_orbits` 里那句 `result.stdout` 在守卫里过得去，
    #     而真的 `RunResult` **根本没有这个字段**（真跑时会 `AttributeError`）。
    #
    #     ⇒ 一个比真实对象"更宽容"的 stub，会把真实的错误挡在守卫之外。
    #       现在这里用真的类，并且走真的 `handle.log_path` ——
    #       代码要是读一个不存在的字段，守卫**立刻**红。
    from ginan_agent.ginan import runner as _runner

    def _stub(config_path, work_dir, **kw):  # type: ignore[no-untyped-def]
        import yaml as _yaml

        seen.update(_yaml.safe_load(Path(config_path).read_text(encoding="utf-8")))
        log = Path(work_dir) / "stub.log"
        log.write_text(_COMPARE_STDOUT, encoding="utf-8")
        return _runner.RunResult(
            ok=True, exit_code=0, duration=0.1,
            handle=_runner.RunHandle(
                run_id="stub", work_dir=Path(work_dir),
                config_path=Path(config_path), log_path=log,
            ),
        )

    ws = Workspace(work_dir=work)
    real = _runner.run_pea
    try:
        _runner.run_pea = _stub  # type: ignore[assignment]
        # ⚠️ 包一层：如果 `do_compare_orbits` 读了 `RunResult` 上**不存在**的字段，
        #    这里会抛 `AttributeError` —— 让它变成**一条红的**，
        #    而不是让整个套件崩掉（崩掉的话后面的检查全看不到）。
        try:
            GT.do_compare_orbits(
                ws, ToolContext.bare("t"), truth=str(truth), test=str(raw)
            )
            check(True, "★ 接缝：do_compare_orbits 真跑通了（没读错字段）")
        except AttributeError as e:
            check(
                False,
                f"★ 接缝：do_compare_orbits 读了 RunResult 上**不存在**的字段：{e}"
                f" —— stub 用的是真 `RunResult`，所以这类错骗不过去",
            )
    finally:
        _runner.run_pea = real  # type: ignore[assignment]

    sent = (seen.get("inputs", {}).get("satellite_data", {}) or {}).get("sp3_files") or []
    check(len(sent) == 2, f"★ 配置里真有两个 sp3：{sent}")
    # ⚠️ 比的是**解析后的路径** —— 工具会把路径规范成「绝对 + 正斜杠」
    #    （和 `config._abs_path` 同一个策略），所以不能拿原字符串比。
    check(
        len(sent) == 2 and Path(sent[0]).resolve() == truth.resolve(),
        "★ 第一个是**参照**（顺序有意义 —— 反了就说不清是谁的）",
    )
    # ⚠️ 判据是"**行首**没有 `E*`"，不是"没有 `E*`"——
    #    注释行里那个 `E*` **应该**还在（那正是行首锚的作用）。
    #    我第一次写的是后者，于是守卫红了，而**红的是我的断言不是代码**。
    sent_test = Path(sent[1]) if len(sent) == 2 else None
    sent_text = sent_test.read_text(encoding="utf-8") if sent_test else ""
    check(
        sent_test is not None
        and sent_test != raw
        and sent_test.is_file()
        and _re.search(r"(?m)^E\*", sent_text) is None,
        "★ 送去比的那份是**修好的副本**（行首已无 `E*`），而且配置指向的是副本",
    )
    check(
        _re.search(r"(?m)^E\*", raw.read_text(encoding="utf-8")) is not None,
        "★ 对照：**原产物没有被改**（那是用户的文件）",
    )


def test_result_reported_even_when_ginan_complains() -> None:
    section("⑲f ★ 拿到结果就报 —— 别因为它抱怨过就说「失败了」")

    import tempfile as _tf

    from ginan_agent.ginan import runner as _runner

    # ⚠️⚠️ 这一条是**真跑那个工具才发现**的（2026-09-27）：
    #
    #     官方 `compare_orbits.yaml` 写着 `chi_square.mode: none`，
    #     而 `E_ChiSqMode`（`enums.h:399-404`）**只有** INNOVATION /
    #     MEASUREMENT / STATE —— **没有 `NONE`**（而示例的注释里把它列为合法值）。
    #
    #     ⇒ 配置加载时必定报错 ⇒ 诊断判成"运行失败" ⇒ `result.ok == False`。
    #       **但那次比对是成功的**（日志里有完整的 `& Agg-All … 1.319809`），
    #       那个枚举在一个 `enable: false` 的块上，不影响任何计算。
    #
    #     ⇒ 拿 `result.ok` 当门，会把**已经拿到的答案**说成"失败了"。
    #       本项目最不喜欢的就是这种错。
    #
    # ⚠️ stub 用**真的** `RunResult` + 真的日志文件（理由见 ⑲d）。
    def _mk(ok: bool, log_text: str):
        work = Path(_tf.mkdtemp(prefix="cmpf_"))
        log = work / "r.log"
        log.write_text(log_text, encoding="utf-8")
        truth = work / "t.sp3"
        truth.write_text(_SP3_SNIPPET.replace("E*", "*"), encoding="utf-8", newline="")
        test = work / "o.sp3"
        test.write_text(_SP3_SNIPPET, encoding="utf-8", newline="")

        real = _runner.run_pea

        def _stub(config_path, work_dir, **kw):  # type: ignore[no-untyped-def]
            # ⚠️ 诊断要**真的跑一遍** `diagnose`（runner 就是这么做的），
            #    不能给一个空的 `Diagnosis()` —— 那样 `problems` 永远是空表，
            #    "抱怨被带出去"这一条就测不到了（第一版就是那样，红的是夹具）。
            from ginan_agent.ginan import diagnostics as _diag

            return _runner.RunResult(
                ok=ok, exit_code=0, duration=0.1,
                diagnosis=_diag.diagnose(log_text),
                handle=_runner.RunHandle(
                    run_id="s", work_dir=work, config_path=Path(config_path),
                    log_path=log,
                ),
            )

        try:
            _runner.run_pea = _stub  # type: ignore[assignment]
            out = GT.do_compare_orbits(
                Workspace(work_dir=work), ToolContext.bare("t"),
                truth=str(truth), test=str(test),
            )
        finally:
            _runner.run_pea = real  # type: ignore[assignment]
        return json.loads(out)

    # ---- ① Ginan 抱怨了，但统计块在 ⇒ **必须报出结果** ----
    complained = "Error: none is not a valid entry for option: @ mode.\n" + _COMPARE_STDOUT
    got = _mk(ok=False, log_text=complained)
    check(
        "error" not in got and got.get("numbers", {}).get("rms_3d_after_helmert_m") == 1.319809,
        f"★ 有抱怨但结果在 ⇒ **照样给数**（拿到 "
        f"{got.get('numbers', {}).get('rms_3d_after_helmert_m')}）",
    )
    check(
        bool(got.get("warnings")),
        "★ 而且把抱怨**如实带出去**（吞掉的话'这次有报错'就消失了）",
    )

    # ---- ② 对照：真的没有结果 ⇒ 必须报失败 ----
    #
    # ⚠️ 不加这一条的话，一个"永远返回成功"的实现也能过 ①——
    #    而那个实现会把"根本没有结果"说成"轨道差 0 米"之类的东西。
    empty = _mk(ok=False, log_text="Error: 什么都没有")
    check(
        "error" in empty,
        f"★ 对照：输出里没有统计块 ⇒ 报失败（拿到 {list(empty)[:3]}）",
    )

    # ---- ③ `repair_file` 的两个边界 ----
    from ginan_agent.ginan import sp3file as gsp3

    work = Path(_tf.mkdtemp(prefix="rep_"))
    src = work / "src.sp3"
    src.write_text(_SP3_SNIPPET, encoding="utf-8", newline="")
    # 目标目录**不存在**也要能写 —— 实测踩过：那时抛的错怪在**源文件**头上
    deep = work / "a" / "b" / "out.sp3"
    try:
        n3 = gsp3.repair_file(src, deep)
        check(deep.is_file() and n3 == 1, "★ 目标目录不存在 ⇒ 自己建，不报错")
    except OSError as e:  # pragma: no cover
        check(False, f"★ 目标目录不存在时崩了：{e}")

    # 源文件不存在 ⇒ 报错要**指对方向**
    try:
        gsp3.repair_file(work / "nope.sp3", work / "x.sp3")
        check(False, "★ 源文件不存在却成功了？")
    except OSError as e:
        check("读不了" in str(e), f"★ 源文件读不到 ⇒ 报错说「读不了」（实际：{e}）")


# =============================================================================
# ⑳ ⑤ 那一轮：SBAS 的**固定演示数据集**
# =============================================================================
#
# 这一节能存在，是因为 SBAS 引进了一个前六条工作流都没有的维度：
# **数据不由用户选**（`Workflow.dataset`）。所以守的东西也不一样 ——
# 不是"配置对不对"，而是"**有没有在用户不知情的时候换掉他的输入**"。


def test_sbas_dataset_matches_the_manifest() -> None:
    section("⑳a ★★ SBAS 数据集：清单是量出来的，不是编的")

    from ginan_agent.ginan import catalog as _catalog

    ds = _catalog.dataset("sbas_2025_315")
    check(ds is not None, "★ 数据集 `sbas_2025_315` 存在")
    if ds is None:
        return

    # ---- ① 离线判据：数量和形状 ----
    #
    # ⚠️ 这几个数是**从官方清单数出来的**（4 站 × 7 小时 × 4 个 15 分钟 = 112…），
    #    所以公式改一位它们就变。这是"没有源码树时"也能守的那一层。
    from collections import Counter

    kinds = Counter(f.kind for f in ds.files)
    check(
        kinds["obs"] == 112 and kinds["ems"] == 48 and kinds["nav"] == 2,
        f"★ 数量：观测 {kinds['obs']}（112）/ ems {kinds['ems']}（48）/ "
        f"广播 {kinds['nav']}（2）",
    )
    check(
        len(ds.stations) == 4 and ds.day.isoformat() == "2025-11-11",
        f"★ 四个站、2025-11-11：{ds.stations} {ds.day}",
    )

    # ⚠️ **`.gz` 那个后缀是踩过两次的坑**。URL 必须由 `ginan_s3_url` 拼，
    #    而它一定带 `.gz`。少了它下到的是 404，而 404 的样子是"源不可用"。
    urls = [f.urls.get("ginan_s3", "") for f in _catalog.dataset_files(ds)]
    check(
        all(u.endswith(".gz") for u in urls) and len(urls) == len(ds.files),
        f"★ 每一条 URL 都以 `.gz` 结尾（{len(urls)} 条，"
        f"例：{urls[0].rsplit('/', 1)[-1]}）",
    )

    # ⚠️ 观测文件必须 `keep_name` —— 否则 112 个文件会被按文件头改成
    #    `HOB2.rnx` / `HOB2_2.rnx`…，**一个时间信息都不剩**。
    obs = [f for f in _catalog.dataset_files(ds) if f.kind == "obs"]
    check(
        all(f.keep_name for f in obs),
        "★ 观测文件的 `keep_name` 都开着（不然名字会被文件头覆盖掉）",
    )
    check(
        all("2025315" in f.filename for f in obs),
        "★ 观测文件名里带着时间（`2025315`）—— 那正是 `keep_name` 要保的东西",
    )

    # ---- ② 有源码树时：和官方清单**逐条比** ----
    #
    # ⚠️ 找不到就**跳过并说明**，不假装失败、也不假装通过 ——
    #    和 ⑱f（打包的示例配置）同一个态度。
    ginan = Path(
        "E:/Code/deepseek-v4.1-flash/test06_ginanagent/ginan-dist/source/inputData"
    )
    if not ginan.is_dir():
        print(f"  · （跳过逐条比对：找不到 Ginan 源码树 {ginan}）")
        return

    def _official(which: str) -> set[str]:
        """官方清单里的 `sbas/` 条目，**补上它自己那一层的根名**。

        ⚠️⚠️ 清单里的路径是**相对各自根目录**的（`./sbas/…`），
            而 `Dataset.files` 的 key 是相对 **`aux/`** 的
            （`data/sbas/…` / `products/sbas/…`）—— 因为拼 URL 时
            `peanpod.s3.amazonaws.com/aux/` 是基址。
            ⇒ 比的时候要把 `data/` / `products/` 补回去。
            （第一版没补，于是两边都 162 条却"逐条不同"。）
        """
        p = ginan / which / f"{which}.list"
        return {
            f"{which}/{ln.strip()[2:]}"
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip().startswith("./sbas/")
        }

    official = _official("data") | _official("products")
    mine = {f.key for f in ds.files}
    # ⚠️ 官方清单里还有一个我们**故意不下**的：160 MB 的 `AUS23907.SNX`。
    #    实测它对结果没有影响（见 `catalog._sbas_files` 的说明）。
    official.discard("products/AUS23907.SNX")
    check(
        mine == official,
        f"★ 和官方清单**逐条相同**（我们有 {len(mine)}、官方有 {len(official)}）"
        f"{'，差的：' + str(sorted(mine ^ official)[:4]) if mine != official else ''}",
    )


def test_dataset_workflow_ignores_the_users_day_and_stations() -> None:
    section("⑳b ★ 数据集工作流：用户给的日期和测站会被**如实忽略**")

    from ginan_agent.ginan import catalog as _catalog
    from ginan_agent.ginan import workflows as gworkflows

    ds = _catalog.dataset("sbas_2025_315")
    for key in ("sbas_l1", "sbas_dfmc", "sbas_pvs"):
        wf = gworkflows.WORKFLOWS.get(key)
        check(wf is not None and wf.dataset == "sbas_2025_315",
              f"★ {key} 声明了数据集")
        if wf is None:
            continue

        # ⚠️ 给的是一份**完全不相干**的输入（别的站、别的日期）——
        #    正确的行为是返回数据集自己的，不是报错、也不是去下那个日期。
        p = _catalog.plan_run(["ALIC"], date(2026, 9, 2), key)
        check(
            p.day == ds.day and sorted(p.stations) == sorted(ds.stations),
            f"★ {key}：忽略用户输入，用数据集的 {p.day} / {p.stations}",
        )
        # ⚠️⚠️ **一次性文件不能漏**（`atx` / `snx`）——
        #    第一版漏了，⑱j 当场就红。
        kinds = {f.kind for f in p.files}
        check(
            {"atx", "snx"} <= kinds,
            f"★ {key}：一次性文件（atx/snx）也在计划里（{sorted(kinds)}）",
        )

    # ---- 对照：非数据集的工作流**不受影响** ----
    #
    # ⚠️ 不加这一条的话，一个"把所有工作流都当成数据集"的实现也能过上面 ——
    #    而那会把 PPP/POD 全弄坏。
    ppp = _catalog.plan_run(["ALIC"], date(2026, 9, 2), "ppp")
    check(
        ppp.day == date(2026, 9, 2) and ppp.stations == ["ALIC"],
        f"★ 对照：PPP 的日期和测站**照用户给的**（{ppp.day} / {ppp.stations}）",
    )


def test_dataset_config_finds_its_files() -> None:
    section("⑳c ★ 数据集配置里，卫星数据文件的路径必须**指得到**")

    from ginan_agent.ginan import config as gconfig

    cfg = _config_with_all_inputs("sbas_l1")
    sd = cfg["inputs"]["satellite_data"]

    # ---- ① `satellite_data_root` 必须是**绝对路径** ----
    #
    # ⚠️ 模板给的是 `sbas/`（它期望文件在 `<输入目录>/sbas/`），
    #    而我们的数据集文件是**平铺**的。靠的是
    #    `acsConfig.cpp:131` 那句"含 `:` 的路径直接返回，不拼前缀"。
    root = sd.get("satellite_data_root", "")
    check(
        root.startswith("INROOT/") or ":" in root or root.startswith("/"),
        f"★ `satellite_data_root` 是绝对路径：{root!r}"
        f"（模板给的 `sbas/` 会让裸名字解析到不存在的子目录）",
    )
    check(
        root != "sbas/",
        "★ 而且**不是**模板原来那个 `sbas/`",
    )

    # ---- ② ems 落在 `satellite_data.sbas_inputs.ems_files` ----
    check(
        sd.get("sbas_inputs", {}).get("ems_files"),
        f"★ `ems_files` 在 `satellite_data.sbas_inputs` 下："
        f"{sd.get('sbas_inputs', {}).get('ems_files')}",
    )

    # ---- ③ 对照：非数据集工作流的 `satellite_data_root` **不许被动** ----
    #
    # ⚠️ 动了它 = 裸文件名的解析方式变了 = 动一条验证过的路。
    for other in ("pod", "brdc2sp3"):
        o = _config_with_all_inputs(other)["inputs"]["satellite_data"]
        check(
            o.get("satellite_data_root") != "INROOT/" and "ems_files" not in o,
            f"★ 对照：{other} 的 `satellite_data_root` 没被改、也没有 `ems_files`"
            f"（拿到 {o.get('satellite_data_root')!r}）",
        )

    # ---- ④ `epoch_interval` 必须是 1 秒 ----
    #
    # ⚠️⚠️ 官方示例**没写**这个键，用的是 Ginan 的默认值 `1`
    #     （`acsConfig.hpp:553`）。而 app 的 `ConfigInputs` 默认是 **30** ——
    #     不写那一栏的话历元数会从 25200 掉到 840，**而且不报错**。
    from ginan_agent.ginan import workflows as _wf

    check(
        _wf.WORKFLOWS["sbas_l1"].epoch_interval == 1.0
        and _wf.WORKFLOWS["sbas_dfmc"].epoch_interval == 1.0
        and _wf.WORKFLOWS["sbas_pvs"].epoch_interval == 1.0,
        "★ 三条 SBAS 的 `epoch_interval` 都是 1 秒（对齐 Ginan 的默认值）",
    )


def test_plan_receipt_says_the_dataset_overrode_your_input() -> None:
    section("⑳d ★ 数据集换掉了用户的输入 —— 回执里必须**说出来**")

    # ⚠️ 不说的后果很具体：用户说"给 ALIC 跑 SBAS"，
    #    计划里却是 HOB2/NEBO/OUS2/TID1 —— 他会在结果里找不到 ALIC，
    #    然后以为程序坏了。
    import tempfile as _tf

    ws = Workspace(work_dir=Path(_tf.mkdtemp(prefix="ds_")))
    got = json.loads(
        GT.do_plan_run(ws, ["ALIC"], "2026-09-02", "sbas_l1")
    )
    d = got.get("dataset")
    check(bool(d), f"★ 回执里有 `dataset` 那一栏（拿到 {list(got)[:6]}）")
    if d:
        check(
            d["day"] == "2025-11-11" and "HOB2" in d["stations"],
            f"★ 它写明用的是 {d['day']} / {d['stations']}",
        )
        check(
            "tell_user" in d and "必须告诉用户" in d.get("tell_user", ""),
            "★ 而且明确要求**转述给用户**",
        )

    # ---- 对照：PPP 的回执里**不该**有这一栏 ----
    ws2 = Workspace(work_dir=Path(_tf.mkdtemp(prefix="ds2_")))
    got2 = json.loads(GT.do_plan_run(ws2, ["ALIC"], "2026-09-02", "ppp"))
    check("dataset" not in got2, "★ 对照：PPP 的回执里没有 `dataset`")


# =============================================================================
# ㉑ ⑥ 那一轮：电离层**做不了** —— 而且原因是"上游"，不是数据
# =============================================================================
#
# ⚠️ 这一节守的是**一个负结果**，所以它和别处的守卫长得不一样：
#    别处的守卫守"这件事做对了"，这里守"**这件事没有被打开**"。
#
# 为什么要守它 —— 见 `docs/decisions.md` ADR-008 和
# `docs/ginan-facts.md` 七点十七。一句话：
#
#     九次受控运行没有一组配置能出产物（崩 / 段错误 / 堆损坏 / 发散 /
#     `IONEX` 恒 0 字节），而且**整个发行版没有一个配置启用过它**。
#
# ⇒ 所以"打开 `ionosphere`"这件事**必须有人拦着** —— 它看起来只是把
#   一个 `false` 改成 `true`，代价却是"用户拿到一个 0 字节的文件"。
#   ⚠️ 而 0 字节的产物**不报错**（`IONEX` 那次退出码就是 0）。
#
# ⚠️ 将来上游修好了，**这条守卫会红** —— 那就是它的用途。
#    红了不是"守卫坏了"，是"该去把 ⑥ 重做一遍了"。


def test_ionosphere_stays_off() -> None:
    section("㉑a ★★ 电离层**不许**被打开 —— 上游那条路是坏的（ADR-008）")

    from ginan_agent.ginan import workflows as GW

    on = [
        (w.key, name)
        for w in GW.WORKFLOWS.values()
        for name, val in w.modes.items()
        if name == "ionosphere" and val
    ]
    check(not on, f"★ 没有任何工作流把 `ionosphere` 打开（拿到 {on}）")

    # 对照：这一栏本身**必须在**每个工作流里 ——
    # 否则上面那条会因为"根本没有这个键"而白过。
    # （`Workflow.modes` 是完整值，见 `workflows.py` 的说明。）
    missing = [w.key for w in GW.WORKFLOWS.values() if "ionosphere" not in w.modes]
    check(not missing, f"★ 对照：`ionosphere` 这一栏每个工作流都写了（缺 {missing}）")

    # ⚠️ 产物那一栏也要守：`ionex` 是那个 0 字节的文件，
    #    `stec` 是那个"三成是负数"的替代品（ADR-008 里写了为什么不接）。
    bad_out = [
        (w.key, o)
        for w in GW.WORKFLOWS.values()
        for o in w.outputs
        if o in ("ionex", "stec")
    ]
    check(not bad_out, f"★ 没有任何工作流声明 `ionex`/`stec` 产物（拿到 {bad_out}）")

    # 对照：`outputs` 里确实有别的值 —— 否则上面那条会因为"谁也声明不出产物"
    # 而变成一条永远绿的空话。
    declared = {o for w in GW.WORKFLOWS.values() for o in w.outputs}
    check(
        "pos" in declared and "sp3" in declared,
        f"★ 对照：别的产物还在（拿到 {sorted(declared)}）",
    )


def test_apriori_and_datasets_do_not_claim_ionosphere() -> None:
    section("㉑b ★ 下载链里**也不许**冒出电离层的文件类别")

    # ⚠️ 和 ⑱j 是同一个循环的两面：那里守"计划要覆盖配置里用到的"，
    #    这里守"两边**都**不许有电离层"。
    #    只守一边的话，另一边的复活不会被发现。
    bad = [k for k in catalog.KINDS if "ionex" in k.lower() or "stec" in k.lower()]
    check(not bad, f"★ `catalog.KINDS` 里没有电离层类别（拿到 {bad}）")

    bad2 = [k for k in catalog.KEY_KINDS if "ionex" in k.lower() or "stec" in k.lower()]
    check(not bad2, f"★ `catalog.KEY_KINDS` 里也没有（拿到 {bad2}）")

    # 对照：别的类别还在。
    check(
        "obs" in catalog.KINDS and "sp3" in catalog.KINDS,
        f"★ 对照：别的类别还在（拿到 {sorted(catalog.KINDS)[:6]}…）",
    )


# =============================================================================
# ㉒ `fit_sp3_pseudoobs`：SP3 既当输入又当伪观测的那一条
# =============================================================================
#
# ⚠️ 这一节守的是**两个实测撞出来的东西**，两个都**不报错**：
#
#   ① `satellite_data.sp3_files` **从来没被替换过**（app 的老 bug）
#      —— 以前的模板都没这个键，所以一直没暴露。不换的后果是配置里
#      留着示例自己的 2019 年文件名，pea 起来第一句话就是
#      `Error: Missing orbit file …` 然后几十毫秒退出。
#
#   ② 官方示例写着 `output_inertial: true` ⇒ 产物是**惯性系**，
#      而 SP3 是通用交换格式，所有人都按地固系读。
#      拿去和 IGS 比会得到 **31,863 公里**的"差异"—— 一个看着像结果的数。


def test_fit_sp3_uses_our_files_in_both_places() -> None:
    section("㉒a ★★ `sp3_files` 和 `sp3_inputs` **两处**都要换成本地文件")

    from ginan_agent.ginan import workflows as gworkflows

    cfg = _config_with_all_inputs("fit_sp3_pseudoobs")
    sat = cfg["inputs"]["satellite_data"]
    pseudo = cfg["inputs"]["pseudo_observations"]

    check(
        sat.get("sp3_files") == ["s.SP3"],
        f"★ `satellite_data.sp3_files` 是**我们给的**文件（拿到 {sat.get('sp3_files')}）",
    )
    check(
        pseudo.get("sp3_inputs") == ["s.SP3"],
        f"★ `pseudo_observations.sp3_inputs` 也是（拿到 {pseudo.get('sp3_inputs')}）",
    )

    # ---- 对照：另外两个工作流的配置里**不该长出来** `sp3_files` ----
    #
    # ⚠️ 这一条守的是 `_replace_if_present` 的那一面：
    #    `pod_example.yaml` / `brdc2sp3.yml` 里**没有**这个键，
    #    用 `_set_path` 就会给它们**凭空加一个**。加进去不报错，
    #    但会让下一个人以为 POD 的 SP3 走这条路（它走的是 `sp3_inputs`）。
    for key in ("pod", "brdc2sp3"):
        c = _config_with_all_inputs(key)
        got = (c.get("inputs", {}).get("satellite_data") or {}).get("sp3_files")
        check(got is None, f"★ 对照：`{key}` 的配置里没有 `sp3_files`（拿到 {got}）")


def test_fit_sp3_output_stays_in_earth_frame() -> None:
    section("㉒b ★★ 产出的 SP3 必须是**地固系** —— 官方示例写的是惯性系")

    from ginan_agent.ginan import workflows as gworkflows

    cfg = _config_with_all_inputs("fit_sp3_pseudoobs")
    sp3 = cfg["outputs"]["sp3"]

    check(
        sp3.get("output_inertial") is False,
        f"★ `outputs.sp3.output_inertial` 是 `False`（拿到 {sp3.get('output_inertial')!r}）",
    )

    # ---- 对照：模板里**确实有这个键**，上一条才有意义 ----
    #
    # ⚠️ 否则"它是 False"可能是因为模板压根没写、我们也没写 ——
    #    那样 Ginan 用它自己的默认值，守卫就成了一句空话。
    from ginan_agent.ginan import examples as gexamples

    raw = gexamples.load("fit_sp3_pseudoobs.yaml")
    check(
        raw["outputs"]["sp3"].get("output_inertial") is True,
        "★ 对照：官方示例里它**是** `True`（我们改的正是这一处）",
    )

    # ---- 对照：`output_velocities` **不许**被动过 ----
    #    实测（2026-09-27）：`#dV` 和 `#dP` 比出来都是 **1.1827 米**，逐位相同。
    #    只改真有问题的那一个 —— 顺手多改会让"我们偏离官方的清单"变长而没理由。
    check(
        sp3.get("output_velocities") is True,
        "★ 对照：`output_velocities` 保持官方示例的值（它无害，量过）",
    )


def test_fit_sp3_differs_from_pod_where_it_should() -> None:
    section("㉒c ★ 它和 POD 的区别在**开关**上，不在产物上")

    from ginan_agent.ginan import workflows as gworkflows

    fit = gworkflows.get("fit_sp3_pseudoobs")
    pod = gworkflows.get("pod")

    check(
        fit.modes["ppp"] is True and pod.modes["ppp"] is True,
        "★ 两个都开 `ppp`（滤波器归它管）",
    )
    check(
        fit.modes["preprocessor"] is False and fit.modes["spp"] is False,
        f"★ 但这条**不开** `preprocessor`/`spp`（它没有观测）",
    )
    check(
        pod.modes["preprocessor"] is True and pod.modes["spp"] is True,
        "★ 对照：POD 那两个**是开的**（它有一堆测站观测）",
    )
    check(
        "obs" not in fit.needs and "obs" in pod.needs,
        f"★ `needs` 也分得开：fit {fit.needs} / pod {pod.needs}",
    )
    # ⚠️ 产物必须不同 —— 它是"出轨道"，不是"出坐标"。
    check(
        "sp3" in fit.outputs and "pos" not in fit.outputs,
        f"★ 产物是轨道（拿到 {fit.outputs}）",
    )


def test_estimates_declaration_matches_the_config() -> None:
    section("㉓ ★★ `Workflow.estimates` —— 它**曾经是个死字段**，现在由这条守卫顶着")

    # ⚠️⚠️ 这一条是 2026-09-27 **量出来**的，不是看出来的：
    #
    #     把 `pod.estimates` 从 `("orbit",)` 改成 `()`，
    #     `config.build_config` 的产物**一个字节都没变**。
    #
    #     根因：读它的三个函数（`_receiver_estimation` / `_satellite_options` /
    #     `_satellite_estimation`）**只被 `_build_handwritten` 调用**。
    #     而 `pod` / `fit_sp3_pseudoobs` 都走模板那条路
    #     （`Workflow.example` 不是 None）⇒ 它们那一栏**从来没被读过**。
    #
    #     ⚠️ 这和 `Workflow.needs` 当初的处境一模一样（见那个字段的 docstring）：
    #        「一边声称在用、一边没人用，比"没有这个字段"更糟」。
    #
    # ⇒ 处置：**不去给它编一个行为**（模板已经写好了那些估计参数，
    #    再"写一遍"就是没用的重复），而是把它变成
    #    **一句能被核对的话** —— 声明了 `orbit`，产出配置里**就得真的在估轨道**。
    #
    #     ⚠️ 将来有人换模板、换开关，把"估轨道"那部分弄丢了，
    #        这条会红。而那种丢失**别的地方看不出来**：
    #        产物照样有（一堆 POS），只是没有了轨道。
    from ginan_agent.ginan import workflows as gworkflows

    declared = {k for k, w in gworkflows.WORKFLOWS.items() if "orbit" in w.estimates}
    not_declared = set(gworkflows.WORKFLOWS) - declared

    check(bool(declared), f"★ 对照：确实有工作流声明了 `orbit`（拿到 {sorted(declared)}）")
    check(
        bool(not_declared),
        f"★ 对照：也确实有**没**声明的（拿到 {sorted(not_declared)}）",
    )

    def says_orbit_estimated(cfg) -> bool:
        node = (
            (cfg.get("estimation_parameters") or {})
            .get("satellites", {})
            .get("global", {})
            .get("orbit")
        )
        if not isinstance(node, dict):
            return False
        est = node.get("estimated")
        return isinstance(est, list) and est and est[0] is True

    for key in sorted(declared):
        cfg = _config_with_all_inputs(key)
        check(
            says_orbit_estimated(cfg),
            f"★ 声明了 `orbit` 的 `{key}`：配置里真的在估卫星轨道",
        )

    for key in sorted(not_declared):
        cfg = _config_with_all_inputs(key)
        check(
            not says_orbit_estimated(cfg),
            f"★ 对照：没声明 `orbit` 的 `{key}`：配置里**没有**在估卫星轨道",
        )


def test_rtk_does_not_grow_blocks_the_template_lacks() -> None:
    section("㉔a ★★ `rtk`：不许长出模板里没有的块（也不许少掉产物）")

    # ⚠️⚠️ 这一条守的是**实测撞出来的 app bug**（2026-09-27）。
    #
    #     `pseudo_observations.sp3_inputs` 的含义是「把这条轨道
    #     **当成观测值，参与估计**」。而 `_substitutions` 原来无条件写它
    #     （用 `_set_path`，会**创建**这个键）——
    #     于是给 `rtk_example.yaml`（模板里没有这个块）**凭空加了一个**。
    #
    #     ⇒ RTK 的轨道本来该是**已知输入**，配置里却写着"当成观测值估"。
    #       **没有任何地方会报错**，跑出来是另一回事。
    cfg = _config_with_all_inputs("rtk")
    inp = cfg["inputs"]

    check(
        "pseudo_observations" not in inp,
        f"★ `rtk` 的配置里**没有** `pseudo_observations`（拿到 {sorted(inp)}）",
    )
    check(
        inp.get("satellite_data", {}).get("sp3_files") == ["s.SP3"],
        "★ 轨道在 `satellite_data.sp3_files`（**已知输入**）里",
    )

    # ---- 对照：估轨道的那两个**必须**有 `pseudo_observations` ----
    for key in ("pod", "fit_sp3_pseudoobs"):
        c = _config_with_all_inputs(key)
        p = c["inputs"].get("pseudo_observations") or {}
        check(
            p.get("sp3_inputs") == ["s.SP3"],
            f"★ 对照：`{key}` **有** `pseudo_observations.sp3_inputs`",
        )

    # ---- `rtk` 一个字节都不要 clk / nav / atx / erp ----
    sat = inp.get("satellite_data") or {}
    for k in ("clk_files", "nav_files"):
        check(k not in sat, f"★ `rtk` 没有 `{k}`（卫星钟差是估的）")
    for k in ("atx_files", "erp_files"):
        check(k not in inp, f"★ `rtk` 没有 `{k}`")

    # ---- 产物：`pos` 必须真的被打开，而且文件名是安全的 ----
    pos = (cfg.get("outputs") or {}).get("pos")
    check(isinstance(pos, dict) and pos.get("output") is True,
          f"★ `rtk` 的 `outputs.pos` 真的开着（拿到 {pos}）")
    if isinstance(pos, dict):
        check("<LOGTIME>" not in str(pos.get("filename", "")),
              f"★ 而且文件名里没有 `<LOGTIME>`（拿到 {pos.get('filename')!r}）")


def test_pos_block_is_filled_not_overwritten() -> None:
    section("㉔b ★ 补 `outputs.pos` 用 `setdefault` —— **不许改模板已有的**")

    # ⚠️ `pod_example.yaml` **本来就有** `outputs.pos`（它自己写的文件名）。
    #    用赋值去"保证它存在"会**顺手把它改名** ——
    #    而改名是一次**没申报的偏离**（POD 那条路的产物名会变，
    #    而按名字找产物的地方全都会静默找不到）。
    from ginan_agent.ginan import examples as gexamples

    raw = gexamples.load("pod_example.yaml")
    want = raw["outputs"]["pos"]["filename"]

    cfg = _config_with_all_inputs("pod")
    got = cfg["outputs"]["pos"]["filename"]
    check(got == want, f"★ POD 的 `pos` 文件名没被动过（{got!r}）")

    # ---- 对照：`rtk` 的模板里**确实没有**这个块，所以是**补出来的** ----
    raw2 = gexamples.load("rtk_example.yaml")
    check("pos" not in (raw2.get("outputs") or {}),
          "★ 对照：`rtk_example.yaml` 里**没有** `outputs.pos`")
    c2 = _config_with_all_inputs("rtk")
    check(
        "pos" in c2["outputs"] and c2["outputs"]["pos"].get("filename"),
        "★ 而生成的配置里**补上了**（且有文件名）",
    )

    # ---- 对照：没声明 `pos` 的工作流**不许**被补 ----
    c3 = _config_with_all_inputs("brdc2sp3")
    check("pos" not in (c3.get("outputs") or {}),
          f"★ 对照：`brdc2sp3` 没有 `pos`（拿到 {sorted(c3.get('outputs', {}))}）")


def test_repro_manifest_is_written() -> None:
    section("㉕ ★★ 复现包：每次运行都留下「这次结果是怎么来的」")

    # ⚠️ 守的是一句**还没兑现的话**：
    #    `fetch.py` 的注释里写着「详细的在复现包（repro/manifest.json）里」，
    #    而原来**没有任何代码生成它**（见项目全貌的 T17）。
    #    一句还没兑现的话比不写更糟 —— 读到的人会以为程序已经替他记着了。
    #
    # 判据分三层，缺一不可：
    #   ① 清单**真的出现在运行目录里**
    #   ② 里面有**能对上的东西**（输入哈希、配置哈希、Ginan 版本）
    #   ③ 跑第二次**不覆盖**第一次
    # 外加一条：**失败的运行也要写** —— "当时跑的是什么、停在哪"正是排查要的。
    import tempfile

    from datetime import date as _date

    from ginan_agent.agent import ginan_tools as GT
    from ginan_agent.core.tools import ToolContext
    from ginan_agent.ginan import config as gconfig
    from ginan_agent.ginan import fetch
    from ginan_agent.ginan import paths as gpaths
    from ginan_agent.ginan import repro, runner

    class _Diag:
        epochs_processed = 2880
        finished = True
        version = "HEAD v4.1.4-5033996"
        headline = "处理正常完成。"
        diagnostics: list = []

    class _Handle:
        def __init__(self, run_id: str, work_dir: Path) -> None:
            self.run_id = run_id
            self.work_dir = work_dir
            self.config_path = work_dir / "config.yaml"
            self.log_path = work_dir / "run.log"

    class _Result:
        def __init__(self, work_dir: Path, run_id: str, ok: bool = True) -> None:
            self.ok = ok
            self.cancelled = False
            self.timed_out = False
            self.exit_code = 0 if ok else 1
            self.duration = 12.5
            self.pos_files: list = []
            self.diagnosis = _Diag()
            self.handle = _Handle(run_id, work_dir)

        def to_json(self) -> str:
            return '{"ok": true}'

    seq = {"n": 0}

    def _stub(config_path, work_dir, **kw):  # type: ignore[no-untyped-def]
        seq["n"] += 1
        return _Result(work_dir, f"run{seq['n']}", ok=seq["n"] != 3)

    real = runner.run_pea
    real_data_dir = gpaths.data_dir
    try:
        runner.run_pea = _stub  # type: ignore[assignment]
        data = Path(tempfile.mkdtemp(prefix="repro_data_"))
        gpaths.data_dir = lambda: data  # type: ignore[assignment]

        w = Workspace(work_dir=Path(tempfile.mkdtemp(prefix="repro_w_")))
        w.start_plan(["ALIC"], _date(2026, 9, 27), "ppp")
        w.config_path = w.work_dir / "config.yaml"
        w.config_path.write_text("x: 1\n", encoding="utf-8")
        w.config_inputs = gconfig.ConfigInputs(
            config_id="N", inputs_root=Path("I"), obs_root=Path("O"),
            outputs_root=Path("X"), workflow="ppp", epoch_interval=30.0,
        )

        # 造两个输入：一个只在盘上（缓存里已有的），一个带"下载报告"
        #
        # ⚠️ 名字必须取自**计划** —— 第一版这里编了个 `SOMETHING.SP3`，
        #    结果那条断言红了：计划里没有的文件当然不会进清单。
        #    **计划才是"这次要用哪些文件"的权威清单**，
        #    下载报告只回答"那些文件是怎么来的"。
        (data / "igs20.atx").write_bytes(b"x" * 100)
        sp3_name = next(f.filename for f in w.plan.files if f.kind == "sp3")
        (data / sp3_name).write_bytes(b"y" * 50)
        w.fetched = fetch.FetchReport(results=[fetch.DownloadResult(
            filename=sp3_name, ok=True, source="whu", size=50,
            sha256="deadbeef", kind="sp3",
        )])
        # 运行目录里放一个"产物"，清单要把它记进去
        (w.work_dir / "ALIC.POS").write_bytes(b"z" * 20)

        GT.do_run_processing(w, ToolContext.bare("t"))

        # ---- ① 清单真的写出来了吗 ----
        mpath = repro.manifest_path(w.work_dir)
        check(mpath.is_file(), f"★ 清单写到了 {mpath.name}")
        if not mpath.is_file():
            return
        man = json.loads(mpath.read_text(encoding="utf-8"))

        # ---- ② 里面有能对上的东西吗 ----
        check(
            man.get("format_version") == repro.FORMAT_VERSION,
            f"★ 带格式版本（{man.get('format_version')}）—— 发出去的清单改不了，必须有",
        )
        task = man.get("task", {})
        check(task.get("stations") == ["ALIC"], f"★ 记了测站：{task.get('stations')}")
        check(task.get("day") == "2026-09-27", f"★ 记了日期：{task.get('day')}")
        check(
            man.get("software", {}).get("ginan", "").startswith("HEAD"),
            f"★ Ginan 版本是从**日志**读的：{man.get('software', {}).get('ginan')}",
        )
        check(
            bool(man.get("config", {}).get("sha256")),
            "★ 配置有哈希（收方可以核对是不是同一份）",
        )

        by_name = {i["name"]: i for i in man.get("inputs", [])}
        check("igs20.atx" in by_name, "★ 输入清单里有 igs20.atx")
        check(
            bool(by_name.get("igs20.atx", {}).get("sha256")),
            "★ 盘上算出来的哈希也记了（缓存里的文件不属于这次下载）",
        )
        check(
            by_name.get(sp3_name, {}).get("source") == "whu",
            "★★ 下载来的文件记着**是从哪个源**来的"
            f"（实际 {by_name.get(sp3_name, {}).get('source')!r}）",
        )
        check(
            any(o.get("path") == "ALIC.POS" for o in man.get("outputs", [])),
            "★ 产物也记了（含路径与哈希）",
        )
        check(
            "pea --config" in man.get("how_to_reproduce", ""),
            "★ 有一段人话写着怎么再跑一遍",
        )
        check(
            "线程" in man.get("how_to_reproduce", ""),
            "★★ 而且**说清了什么情况下结果会不一样**（线程数；实测 1 核 vs 8 核差 3.2 厘米）"
            "—— 这是复现不出来时最容易怪自己的地方",
        )

        # ---- ③ 跑第二次：老的那份要留档，不许被覆盖 ----
        GT.do_run_processing(w, ToolContext.bare("t"))
        check(
            (w.work_dir / "repro" / "manifest-run1.json").is_file(),
            "★★ 第二次运行时，老清单按它自己的 run_id 改名留档了",
        )
        man2 = json.loads(repro.manifest_path(w.work_dir).read_text(encoding="utf-8"))
        check(
            man2.get("run", {}).get("run_id") == "run2",
            f"★ manifest.json 指向最近一次（实际 {man2.get('run', {}).get('run_id')}）",
        )

        # ---- ④ 失败的运行**也要**写 ----
        GT.do_run_processing(w, ToolContext.bare("t"))   # 第三次：ok=False
        man3 = json.loads(repro.manifest_path(w.work_dir).read_text(encoding="utf-8"))
        check(
            man3.get("run", {}).get("ok") is False,
            "★★ 失败的运行也留下了清单（'当时跑的是什么、停在哪'正是排查要的）",
        )
        check(
            (w.work_dir / "repro" / "manifest-run2.json").is_file(),
            "★ 而且失败那次也没覆盖成功的那些",
        )

        # ---- ⑤ 路径要交给模型：用户得知道打包哪个目录 ----
        raw = GT.do_run_processing(w, ToolContext.bare("t"))
        out = json.loads(raw)
        check(
            "work_dir" in out.get("repro", {}),
            "★ 工具输出里带着运行目录，模型才知道让用户打包什么",
        )
    finally:
        runner.run_pea = real  # type: ignore[assignment]
        gpaths.data_dir = real_data_dir  # type: ignore[assignment]



# =============================================================================
# ㉖ 改正项：逐项开关（2026-10-01）
# =============================================================================
#
# 用户的原话：「海潮负荷、大气潮、二阶电离层、日月位置等等参数让用户来选择」。
#
# ⚠️ 这一节守的是**开关真的接到了配置上**，不是"界面上有个勾"。
#    没接上的症状正是本项目最怕的那一类：**看着正常、其实是错的** ——
#    Ginan 那边"开关开着、模型文件却不在"＝ 静默少一项改正，
#    退出码 0、日志里也看不出（见 `config._apply_profile_delta` 的说明）。
#
# ⚠️ 所以 ㉖a 的判据不是"那一项变了"，而是"**只有**那一项变了"：
#    "顺手多改一处"和"少改一处"都会红，而只看单个路径的断言查不出前者。


def _ppp_config(profile: str = "standard", **kw):
    """造一份 PPP 配置 —— 这一节共用。

    ⚠️ 用标记名当文件名（`ATX_MARKER.atx` 那样），和 ⑱ 那一节同一个理由：
       真名字撞上模板里恰好同名的那个，"有没有被正确替换"就看不出来了。
    """
    from ginan_agent.ginan import config as gconfig

    return gconfig.build_config(gconfig.ConfigInputs(
        config_id="20250615",
        inputs_root=Path("INROOT"),
        obs_root=Path("OBSROOT"),
        outputs_root=Path("OUTROOT"),
        workflow="ppp", profile=profile,
        atx_files=["ATX_MARKER.atx"],
        erp_files=["ERP_MARKER.erp"],
        sp3_files=["SP3_MARKER.SP3"],
        clk_files=["CLK_MARKER.CLK"],
        nav_files=["NAV_MARKER.rnx"],
        rnx_inputs=["OBS_MARKER.rnx"],
        snx_files=["SNX_MARKER.snx"],
        stations={"TEST": (1.0, 2.0, 3.0)},
        **kw,
    ))


def _at_path(cfg, *path):
    """按路径取值；中间缺了就返回 None（不抛）。"""
    node = cfg
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def test_every_correction_toggles_exactly_its_own_switch() -> None:
    section("㉖a ★ 每一项改正**只动它自己那一处**（没有连带副作用）")

    from ginan_agent.ginan import catalog, corrections as gcorrections

    base = _ppp_config("standard")
    on = gcorrections.resolve("standard")

    for c in gcorrections.CORRECTIONS:
        cfg = _ppp_config("standard", corrections={c.key: False})
        diff = set(_diff_paths(base, cfg))

        expect: set = set()
        if c.model_path:
            expect.add(("receiver_options", "global", "models") + c.model_path)
        # ⚠️ 只有当那个文件**没有别人还要用**时，它才该从 inputs 里消失。
        #    `igrf14coeffs.txt` 就是共享的（二阶和三阶都要）——
        #    关掉二阶不该把三阶的文件也抽走。
        still_needed = {f for o in gcorrections.CORRECTIONS
                        if o.key != c.key and on[o.key] for f in o.files}
        for name in c.files:
            if name in still_needed:
                continue
            top, sub = catalog.TABLE_KEYS[name]
            expect.add(("inputs", top, sub) if sub else ("inputs", top))

        want = "、".join(".".join(p) for p in sorted(expect)) or "（无）"
        check(
            diff == expect,
            f"★ 关掉「{c.label}」只改 {want}"
            f"（实际改了 {sorted('.'.join(p) for p in diff)}）",
        )


def test_correction_defaults_are_still_the_profiles() -> None:
    section("㉖b ★ 默认值仍然完全等于两个档位（不加参数＝不改行为）")

    from ginan_agent.ginan import corrections as gcorrections

    red = gcorrections.resolve("reduced")
    std = gcorrections.resolve("standard")
    check([k for k, v in red.items() if v] == ["solid"],
          "★「基础」档只有固体潮开着（海潮那几项要模型文件，基础档没有）")
    check(all(std.values()), "★「完整」档 9 项全开")

    # ⚠️ **逐字节相同**，不是"看着差不多"：这一条保证"加了逐项开关"
    #    这件事对没碰过设置的用户是**恒等变换**。
    check(_ppp_config("reduced") == _ppp_config("reduced", corrections=None),
          "★ 不传 corrections 和传 None 生成同一份配置（基础档）")
    check(_ppp_config("standard") == _ppp_config("standard", corrections={}),
          "★ 空覆盖 {} 也是同一份（完整档）")


def test_turning_one_on_in_the_base_profile_actually_wires_it() -> None:
    section("㉖c ★★ 基础档上单独打开一项：**开关和文件都必须进配置**")

    # ⚠️⚠️ 这是整个功能的重点，也是"开了等于没开"那个坑的守卫：
    #     只写开关不写文件 → Ginan 算不出来、不报错；
    #     只写文件不写开关 → 文件下了没用上（也是静默的）。
    #     **两样都要**。
    cfg = _ppp_config("reduced", corrections={"ocean_load": True})

    check(_at_path(cfg, "receiver_options", "global", "models",
                   "tides", "otl") is True,
          "★ 开关写成了 true")
    check(_at_path(cfg, "receiver_options", "global", "models",
                   "tides", "enable") is True,
          "★ 总开关 `enable` 也跟着是 true（它是总闸，见 _model_options）")
    blq = _at_path(cfg, "inputs", "tides", "ocean_tide_loading_blq_files")
    check(bool(blq), f"★ 而且文件写进了配置（拿到 {blq}）")

    # 对照：其余项的文件一个都不该冒出来 —— 否则"只开一项"变成"全开"
    check(_at_path(cfg, "inputs", "planetary_ephemeris_files") is None,
          "★ 对照：日月历表**没有**跟着冒出来")
    check(_at_path(cfg, "inputs", "igrf_files") is None,
          "★ 对照：地磁场系数**没有**跟着冒出来")
    check(_at_path(cfg, "receiver_options", "global", "models",
                   "ionospheric_components", "use_2nd_order") is False,
          "★ 对照：二阶电离层仍然是关的")

    # 对照的另一半：全关时总闸必须是 false，不能是常量 True
    # ⚠️ 建成"常量 true"的话，用户在界面上把 5 项潮汐全关掉，
    #    配置里却写着"潮汐开着"—— 而 Ginan 会去算它，算不出来也不报错。
    from ginan_agent.ginan import corrections as gcorrections
    alloff = {c.key: False for c in gcorrections.CORRECTIONS}
    cfg2 = _ppp_config("standard", corrections=alloff)
    check(_at_path(cfg2, "receiver_options", "global", "models",
                   "tides", "enable") is False,
          "★★ 五项潮汐全关时，总开关 `enable` 必须是 **false**（不能是常量）")


def test_no_correction_claims_an_unmeasured_number() -> None:
    section("㉖d ★ 没量过的项**不许**写成测得的结果")

    # ⚠️ 这是这个项目的底线（见 `corrections.py` 文件头）：
    #    UI 上那行"影响"必须是**我们自己量出来的**，不是文献值。
    #    没量过就写「没量过」—— 一个把文献值冒充实测的界面，
    #    比一个什么都不说的界面更糟：用户会据此做决定。
    from ginan_agent.ginan import corrections as gcorrections

    for c in gcorrections.CORRECTIONS:
        check(bool(c.label and c.what and c.effect),
              f"  「{c.key}」名字/说明/影响三样都写了")
        if c.measured:
            check(c.effect.strip() != "没量过",
                  f"★「{c.label}」标了 measured，就得给出数字")
        else:
            check(c.effect.strip() == "没量过",
                  f"★「{c.label}」没量过 —— 那行字必须就是「没量过」"
                  f"（实际写的是「{c.effect[:24]}…」）")


def test_correction_overrides_are_sparse_and_cleaned() -> None:
    section("㉖e ★ 覆盖值是稀疏的，而且洗得干净")

    from ginan_agent import settings as gsettings
    from ginan_agent.ginan import corrections as gcorrections

    check(gsettings.Settings().corrections == {},
          "★ 默认空（= 完全按档位）—— 不是「九个 true」")

    # ⚠️ `"false"` 这个字符串在 Python 里是**真值** ——
    #    直接 bool() 会变成"开"，而用户明明关掉了。所以 clean 只认真 bool。
    # ⚠️ 键要用**注册表里的**（`ocean_load` 而不是 Ginan 配置里那个 `otl`）——
    #    两套名字是有意的：注册表的键属于我们，`model_path` 才映射到 Ginan。
    cleaned = gcorrections.clean({"ocean_load": True, "nope": 1,
                                  "iono2": "false", "atmos_load": False})
    check(cleaned == {"ocean_load": True, "atmos_load": False},
          f"★ 只留认识的键 + 真 bool（拿到 {cleaned}）")

    # 存盘/读盘一圈不能丢
    s = gsettings.Settings(corrections={"ocean_load": True})
    back = gsettings.Settings.from_dict(s.to_json())
    check(back.corrections == {"ocean_load": True},
          "★ 存进 config.json 再读回来还是同一个（改过哪几项不能丢）")

    # 稀疏覆盖：只改一项时，其余项仍然跟着档位
    over = {"ocean_load": True}
    check(gcorrections.resolve("standard", over)["iono2"] is True,
          "★ 对照：只改一项时，其余项仍然按档位（完整档 → 二阶电离层还是开）")
    check(gcorrections.resolve("reduced", over)["iono2"] is False,
          "★ 对照：同一份覆盖在基础档下，二阶电离层仍然按基础档（关）")



# =============================================================================
# ㉗ 残差诊断：开关真的接到了配置上（2026-10-01）
# =============================================================================
#
# ⚠️ 残差**不是白来的**：Ginan 里要同时满足三条才写得出来
#    （`level: 5` + `output_residuals` + `output_network`），
#    缺一条都是**静默的** —— 文件照常写，只是没有残差行。
#    这一节守的就是"三条都在"，以及"默认那条路一个字节都没变"。


def test_trace_residuals_switch_writes_what_ginan_needs() -> None:
    section("㉗ ★ 残差开关：该开的三条都开，默认那条路一个字节不变")

    from ginan_agent.ginan import config as gconfig

    def trace_of(flag: bool, profile: str = "standard"):
        cfg = _ppp_config(profile, trace_residuals=flag)
        return cfg["outputs"]["trace"]

    off = trace_of(False)
    check(off["level"] == 1 and off["output_residuals"] is False,
          f"★ 默认不写残差（level {off['level']}，residuals "
          f"{off['output_residuals']}）—— 和以前逐字节一样")
    check(not off.get("output_network"),
          "★ 默认也不开网络那份 trace（开了会多一个文件要用户收拾）")

    on = trace_of(True)
    check(on["level"] == 5, f"★ 要残差就得 level 5（实测 1、3 级 0 行）")
    check(on["output_residuals"] is True, "★ `output_residuals: true`")
    check(on["output_network"] is True,
          "★★ 而且必须开 `output_network` —— PPP/RTS 的残差**在网络那份里**。"
          "只开 output_receivers 的话拿到的是 SPP（预处理）的码残差，"
          "而 Ginan 自己的脚本用的是 PPP/RTS")
    check(bool(on.get("network_filename")),
          f"★ 网络那份要有文件名（{on.get('network_filename')}）")
    check(on["output_receivers"] is False,
          "★★ 接收机那份**要关掉**：level 5 下它是 1.5 GB/天，"
          "而我们只要网络那份里的残差")


def test_trace_residuals_reaches_the_template_path_too() -> None:
    section("㉗b ★★ 两条路（手写 / 模板）写出来的 trace 必须一致")

    # ⚠️ 这个仓库有一类反复出现的 bug：同一个东西在两条路上各写一份，
    #    改了一边忘了另一边。判据不是"两边都写了"，而是**两边的值相等**。
    from ginan_agent.ginan import config as gconfig

    hand = _ppp_config("standard", trace_residuals=True)["outputs"]["trace"]
    tmpl = gconfig.build_config(gconfig.ConfigInputs(
        config_id="t", inputs_root=Path("I"), obs_root=Path("O"),
        outputs_root=Path("X"), workflow="pod", profile="standard",
        trace_residuals=True, egm_files=["tables/EGM_MARKER.gfc"],
        stations={"TEST": (1.0, 2.0, 3.0)},
    ))["outputs"]["trace"]

    for key in ("level", "output_residuals", "output_receivers",
                "output_network"):
        check(hand[key] == tmpl[key],
              f"★ 两条路的 `{key}` 一样（手写 {hand[key]!r} / "
              f"模板 {tmpl[key]!r}）")
    check(bool(tmpl.get("network_filename")),
          "★ 模板那边也有网络文件名（模板自带那个就用它的）")


def test_residual_run_gives_the_same_solution() -> None:
    section("㉗c ★★ 开残差**不改变解算结果**（这是整件事的前提）")

    # ⚠️⚠️ 这一条是"残差诊断"敢做的前提：它重跑一遍，但结果必须是**同一份解**。
    #     实测过（ALIC 2026-09-02 整天，level 1 vs level 5，两份 `.POS`
    #     sha256 一致）—— 而那种"实测过一次"的事**必须有守卫**，
    #     否则哪天上游改一行，我们就在给用户看**另一次解算**的残差，
    #     而没有任何地方会报错。
    #
    # ⚠️ 这里守的是**我们能守的那一半**：配置里除了 trace 那三个键，
    #    别的都必须逐字节相同。至于 Ginan 拿到这份配置真的会算出同一个解 ——
    #    那是上面那条实测记录的职责（守卫跑不动 pea）。
    from ginan_agent.ginan import config as gconfig

    a = _ppp_config("standard", trace_residuals=False)
    b = _ppp_config("standard", trace_residuals=True)
    ta, tb = a["outputs"].pop("trace"), b["outputs"].pop("trace")
    diff = _diff_paths(a, b)
    check(not diff,
          f"★★ 除了 `outputs.trace`，两份配置**完全一样**（多出来的：{diff}）")
    check(ta != tb, "（夹具：trace 那两块确实不同）")
    check(gconfig.ConfigInputs(config_id="x", inputs_root=Path("I"),
                               obs_root=Path("O"),
                               outputs_root=Path("X")).trace_residuals
          is False,
          "★ 新字段默认 False —— 老调用方一行都不用改")

def main() -> int:
    print("=" * 60)
    print("工具链自检 —— 从'要处理某站某天'到'给出结论'")
    print("=" * 60)
    print("⚠️ 用假 Ginan，秒级返回、不联网、不下载。")

    test_happy_path()
    test_config_has_sinex_and_bias_keys()
    test_alternatives_need_permission()
    test_alternatives_success_is_recorded()
    test_workspace_tracks_state()
    test_order_enforced()
    test_bad_input()
    test_missing_file_scenario()
    test_mutated_config()
    test_cancelled_run()
    test_run_pea_never_raises()
    test_pea_env_does_not_touch_thread_counts()
    test_silent_pea_can_still_be_cancelled()
    test_pea_always_gets_config_flag()
    test_detects_single_core_run()
    test_failed_download_not_in_config()
    test_config_before_download_keeps_plan_names()
    test_standard_profile_requires_real_files()
    test_standard_config_really_carries_the_tables()
    test_basic_profile_stays_lean()
    test_obs_listing_goes_by_content_not_extension()
    test_claimed_data_is_not_called_cache()
    test_missing_tables_checks_the_disk()
    test_table_list_and_config_keys_agree()
    test_warnings_reach_the_conclusion()
    test_config_gets_station_position()
    test_stale_reference_position()
    test_tides_in_reduced_profile()
    test_smoothed_solution_verdict()
    test_tool_set()
    # ⑱ POD 的配置来自官方示例（不是手写的）—— 见那一节开头的说明
    test_example_configs_are_present_and_unmodified()
    test_workflow_declares_its_config_source()
    test_pod_config_differs_from_the_example_only_where_declared()
    test_pod_config_has_the_blocks_that_were_missing()
    test_pod_file_lists_come_from_config_inputs()
    test_pod_profile_delta_deletes_the_table_keys()
    test_workflow_tables_are_wired()
    test_workflow_needs_covers_what_the_config_uses()
    test_plan_run_carries_the_workflow()
    test_egm_gate_blocks_pod()
    test_run_timeout_comes_from_the_workflow()
    test_disk_gate_blocks_pod_before_it_starts()
    test_output_filenames_are_safe_on_windows()
    # ⑲ ④ 那一轮：brdc2sp3 + compare_orbits —— 见那一节开头的说明
    test_run_is_bounded_by_epoch_count()
    test_brdc2sp3_config_only_uses_files_it_needs()
    test_workflow_delta_does_not_invent_blocks()
    test_comparison_parser_reads_the_real_output()
    test_sp3_epoch_tag_repair()
    test_result_reported_even_when_ginan_complains()
    # ⑳ ⑤ 那一轮：SBAS 的固定演示数据集 —— 见那一节开头的说明
    test_sbas_dataset_matches_the_manifest()
    test_dataset_workflow_ignores_the_users_day_and_stations()
    test_dataset_config_finds_its_files()
    test_plan_receipt_says_the_dataset_overrode_your_input()
    # ㉑ ⑥ 那一轮：**负结果**也要有守卫
    test_ionosphere_stays_off()
    test_apriori_and_datasets_do_not_claim_ionosphere()
    # ㉒ fit_sp3_pseudoobs
    test_fit_sp3_uses_our_files_in_both_places()
    test_fit_sp3_output_stays_in_earth_frame()
    test_fit_sp3_differs_from_pod_where_it_should()
    # ㉓ `estimates` 曾经是死字段 —— 现在它是**一句能被核对的话**
    test_estimates_declaration_matches_the_config()
    # ㉔ rtk
    test_rtk_does_not_grow_blocks_the_template_lacks()
    test_pos_block_is_filled_not_overwritten()
    # ㉕ 复现包
    test_repro_manifest_is_written()
    # ㉖ 改正项：逐项开关
    test_every_correction_toggles_exactly_its_own_switch()
    test_correction_defaults_are_still_the_profiles()
    test_turning_one_on_in_the_base_profile_actually_wires_it()
    test_no_correction_claims_an_unmeasured_number()
    test_correction_overrides_are_sparse_and_cleaned()
    # ㉗ 残差诊断：开关真的接到了配置上
    test_trace_residuals_switch_writes_what_ginan_needs()
    test_trace_residuals_reaches_the_template_path_too()
    test_residual_run_gives_the_same_solution()

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
