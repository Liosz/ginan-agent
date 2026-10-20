"""把 Ginan 领域层包装成 agent 能用的工具。

============================ 这一层是唯一的跨界点 ============================
    ginan/   —— 纯函数，不知道 agent、不知道 LLM
    core/    —— agent 内核，不知道 Ginan
    agent/   —— **只有这一层同时知道两边** ← 本文件

所以这个文件应该**很薄**：它只做"翻译"，业务逻辑在 ginan/ 里。

============================ ⚠️ 中间产物不走模型 ============================
完整的处理链路有 5 步，每步依赖上一步的产物（计划、下载结果、配置、运行结果）。

**这些产物不经过模型** —— 它们存在工作区里（workspace.py），
工具自己去取。让模型传的话，它可能漏字段、改格式、截断，
而且每一轮都要重发一遍，白白占上下文。

工具之间靠**工作区**协作，靠**参数**传递的只有用户真正要做的选择。

============================ description 是写给模型看的 ============================
不是写给人看的文档。重点不是"这个工具是什么"，
而是"**什么时候该用它**"。

而且**顺序约束要写进描述里** —— 比如"下载之前必须先 plan_run"。
模型看不到代码里的依赖关系，你不说它就不知道。
"""

from __future__ import annotations

import dataclasses

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..core.tools import Tool, ToolContext
from ..ginan import advise as ginadvise
from ..ginan import (catalog, charts as gcharts, config as gconfig,
                    corrections as gcorrections, environment,
                    examples as gexamples, residuals as gresid,
                    tracefile as gtracefile)
from ..ginan import fetch, inbox
from ..ginan import paths as gpaths
from ..ginan import net, posfile, probe, repro, runner, sp3file as gsp3
from ..ginan import workflows as gworkflows
from ..ginan.net import ProxyConfig
from .workspace import Workspace, table_file_names, table_files_for


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _err(msg: str) -> str:
    """统一的错误返回。

    ⚠️ 错误要能被模型**读懂并据此改正**，所以要包含：
       ① 什么不对  ② 正确的做法是什么
       只说"失败了"等于没说。
    """
    return _json({"error": msg})


def _app_version() -> str:
    """本程序的版本号（写进复现包用）。

    ⚠️ 读不到就返回空串 —— 版本号是**附注**，不该让它有机会把主流程弄崩。
    """
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


# 文件类别 → 人话。给模型和用户看的，不要让它看到 `sp3`、`clk` 这种内部代号。
# ⚠️⚠️ 这里原来有一张 `_KIND_LABELS` 中文名映射表 —— **删掉了**。
#
#    它是 `catalog.KINDS` 的**第二份拷贝**，而且比正本**少两类**：
#    没有 `snx`（卫星元数据）和 `bia`（码偏差）。
#
#    ⚠️ 于是缺这两类文件时，模型读到的是内部代号：
#          "缺了什么" → "snx、bia"     ← 对用户毫无意义
#       而 `snx`/`bia` 恰好是"下不到也照样出结果"的两类，
#       **最可能真的缺** —— 也就是最可能走到这条路径。
#
#    ⚠️ 同一份文件往上 200 行还专门写着：
#       「用 `catalog.KINDS[...].label`（那份中文名是**早就有的**）——
#         不在这里另写一张映射表。同一件事有两个名字表，迟早只改一边。」
#       **写下了这条，然后又在下面抄了一张。**


# =============================================================================
# 环境与网络
# =============================================================================


def do_check_environment() -> str:
    report = environment.check_environment()
    return report.to_json()


def do_probe_sources(ctx: ToolContext, proxy_mode: str = "auto") -> str:
    """探测数据源。

    ⚠️ 这个工具**必须响应取消** —— 11 个源、每个超时 20 秒，
       最坏要等 40 秒。而用户点了"停止"之后如果几十秒没反应，
       他会认为程序坏了。

       **取消的响应速度是可靠性的一部分。**
       （见 probe.probe_all 的 cancelled 参数）
    """
    ctx.progress("正在测试数据源连通性…", fraction=0.0)
    mode = proxy_mode if proxy_mode in ("auto", "manual", "none") else "auto"

    def report_progress(done: int, total: int) -> None:
        ctx.progress(f"已测 {done}/{total} 个数据源", fraction=done / total)

    report = probe.probe_all(
        proxy=ProxyConfig(mode=mode),
        cancelled=lambda: ctx.cancelled,
        on_progress=report_progress,
    )

    if report.cancelled:
        ctx.progress("已中止", fraction=None)
        return _json({
            "cancelled": True,
            "tested": len(report.sources),
            "note": "用户中止了检查，结果是**不完整**的。",
        })

    ctx.progress(f"完成：{report.verdict}", fraction=1.0)
    return report.to_json()


# =============================================================================
# 计划
# =============================================================================


def do_plan_run(
    ws: Workspace,
    stations: str | list[str],
    day: str,
    workflow: str | None = None,
) -> str:
    """造取数计划并存进工作区。

    ⚠️ `stations` 收**一个站名或一串站名** —— 后者就是**网络解**。
       一个词用一个参数，不做"单站参数 / 多站参数"两个入口：
       两个入口里总有一个长期没人走（见 `catalog.RunPlan` 的说明）。

    ⚠️ `workflow` 决定**要做什么事**（见 `workflows.WORKFLOWS`）。
       不给就**沿用当前那个**（默认 ppp）—— 见 `Workspace.start_plan`。
       给了但拼错会**静默降级到 ppp**（`workflows.get` 的约定），
       所以要如实报出实际用了哪个 —— 见下面的回执。

    ⚠️⚠️ **工作流会改变"多个测站"的含义**，这一点必须在回执里说清：

        ppp 各站**互不相干**（轨道钟差是固定输入，没有共享待估参数）
        pod 各站**共享卫星轨道参数** —— 那才是真正的网络解

       （工具描述里原来只写了前一句，那是 PPP 的事实。
         照它去选 POD 会得到错误的预期。）
    """
    # ⚠️ 拼错时 `workflows.get` 会**静默回 ppp**。这里先记下来，
    #    回执里要如实说"实际用的是哪个"—— 不说的话，模型以为 POD 生效了。
    asked = gworkflows.is_known(workflow) if workflow else True

    try:
        d = date.fromisoformat(day.strip())
    except ValueError:
        return _err(
            f"日期格式不对：{day!r}。"
            "请写成 YYYY-MM-DD 的形式，例如 2026-09-02。"
        )

    names = [stations] if isinstance(stations, str) else list(stations)
    names = [n.strip().upper() for n in names if n and n.strip()]
    bad = [n for n in names if not (2 <= len(n) <= 4)]
    if bad:
        return _err(
            f"测站代码看起来不对：{'、'.join(bad)}。"
            "通常是 4 个字母，例如 ALIC、DARW、HOB2。"
        )
    if not names:
        return _err("没给测站。至少要一个，例如 ALIC。")

    plan = ws.start_plan(names, d, workflow)
    out = json.loads(catalog.plan_to_json(plan, ws.workflow))

    # ⚠️⚠️ **拼错的工作流名会被静默降级到 ppp**（`workflows.get` 的约定），
    #     所以必须**如实报出来** —— 不报的话模型以为 POD 生效了，
    #     而实际跑的是 PPP，两边对结果的预期完全不同。
    if not asked:
        wf_now = gworkflows.get(ws.workflow)
        out["warning"] = (
            f"没有叫 {workflow!r} 的工作流，**已经退回「{wf_now.label}」**。"
            f"可选的是：{'、'.join(gworkflows.choices())}。"
            f"要换的话重新调一次这个工具。"
        )

    # ⚠️⚠️ **数据集工作流：用户给的日期和测站被换掉了,必须说出来。**
    #
    #     SBAS 那几条跑的是**一份固定的官方演示数据集**（见
    #     `catalog.Dataset`），`plan_run` 会**忽略**调用方给的那两个参数。
    #
    #     ⚠️ 不说的后果很具体：用户说"给 ALIC 跑 SBAS"，
    #        计划里却是 HOB2/NEBO/OUS2/TID1 和 2025-11-11 ——
    #        他会在结果里找 ALIC，找不到，然后以为程序坏了。
    wf_now = gworkflows.get(ws.workflow)
    ds = catalog.dataset(wf_now.dataset) if wf_now.dataset else None
    if ds is not None:
        out["dataset"] = {
            "key": ds.key,
            "label": ds.label,
            "day": ds.day.isoformat(),
            "stations": list(ds.stations),
            "window": ds.window,
            "size": f"约 {ds.total_mb:.0f} MB",
            "why": ds.note,
            "what": (
                f"「{wf_now.label}」跑的是**固定的一份官方演示数据** —— "
                f"日期和测站都不是你给的，"
                f"而是 **{ds.day.isoformat()}**、"
                f"**{'、'.join(ds.stations)}**。"
            ),
            "tell_user": (
                "这一点**必须告诉用户** —— 他说的是别的站/别的日期的话，"
                "要直接讲清楚「这条只能用官方那份数据，"
                "因为 SBAS 的改正信息播完就不归档」，"
                "而不是让他自己去结果里找不到站。"
            ),
        }

    return json.dumps(out, ensure_ascii=False, indent=2)


def do_workspace_status(ws: Workspace) -> str:
    return _json(ws.summary())


# =============================================================================
# 下载
# =============================================================================


def do_fetch_data(
    ws: Workspace,
    ctx: ToolContext,
    proxy_mode: str = "auto",
    with_tables: bool = False,
) -> str:
    """按计划下载。

    ⚠️ 这是**长任务** —— 几百 MB 可能要十几分钟。
       所以全程通过 ctx.progress 报进度，并且响应取消。

    `with_tables=True` 时**顺带把模型文件补齐**（一次性，之后一直用）：

        · 「完整」档的通用模型：10 个
        · **当前工作流特有的**：POD 还要一个重力场模型（`EGM2008.gfc`，
          约 72 MB 压缩后 / 252 MB 解压后）

    ⚠️ 两批都算进来才是对的 —— 只下通用那批的话，POD 会在**生成配置**那一步
       被拦住（"缺重力场模型"），而用户刚刚才"补下过模型"，会以为程序坏了。
    """
    if ws.plan is None:
        return _err(
            "还没有取数计划。**必须先调用 plan_run** 确定要下载哪些文件 —— "
            "不要凭空猜文件名，Ginan 的文件名必须逐字精确。"
        )

    mode = proxy_mode if proxy_mode in ("auto", "manual", "none") else "auto"
    ctx.progress(f"开始下载 {len(ws.plan.files)} 个文件…", fraction=0.0)

    report = fetch.fetch_plan(
        ws.plan,
        gpaths.data_dir(),
        ctx=ctx,
        proxy=ProxyConfig(mode=mode),
    )

    # ---- 顺带补「完整」档的模型文件 ----
    #
    # ⚠️ 为什么放在这儿而不是单独开一个工具：
    #    模型文件的**存在意义**就是给配置用，而配置在下载之后才生成。
    #    分成两个工具的话，模型可能忘了调第二个 —— 而**忘了调的后果是静默的**
    #    （配置里不写那些键 → 完整档和基础档算出来一模一样，谁也不报错）。
    tables_report = None
    if with_tables:
        # ⚠️ **带上当前工作流** —— 它决定要不要连"工作流特有"的那批一起下
        #    （POD 的重力场模型，约 72 MB）。
        #
        #    ⚠️ 不带的话症状很隐蔽：文件下下来了、配置也生成了，但
        #       生成配置时那道"缺重力场"的门会拦住 ——
        #       而用户明明刚刚"补下了模型"，会以为程序坏了。
        todo = fetch.missing_tables(workflow=ws.workflow)
        if todo:
            ctx.progress(f"补下 {len(todo)} 个模型文件（一次性）…")
            tables_report = fetch.fetch_tables(
                ctx=ctx, workflow=ws.workflow, proxy=ProxyConfig(mode=mode)
            )

    # ---- 镜像全挂了？问用户要不要用**备选产品** ----
    #
    # ⚠️⚠️ 为什么要问，而不是自动顶上 —— 这是本次改动里最要紧的一条：
    #
    #     "备选"不是"多一个镜像"。镜像之间是**同一个文件**（IGS 的轨道在
    #     武大有、在 SIO 有、在 BKG 有），换哪个结果都一样。
    #
    #     而备选是 **ESA 自己解算的一套轨道/钟差** —— 是**另一份产品**。
    #     换上去，最终坐标会变（毫米到厘米级），而且是**系统性的**：
    #     它和用户之前算过的结果、和别人算的结果**不再可比**。
    #
    #     👉 所以这件事**必须用户拍板**。偷偷换掉分析输入，
    #        等于在他不知道的情况下改了他的实验条件。
    #
    # ⚠️ 而且**只问一次**，且只在真的还有失败文件时问。
    #    每次都问会变成噪音，用户就会开始无脑点"是" —— 那比不问更糟。
    # ⚠️ 备选那条路的返回值先放这儿，**函数末尾统一返回** ——
    #    因为"记账"必须发生在那之前（见下面唯一的记账点）。
    alt_payload: dict | None = None

    if report.failed and ws.plan.alternatives and ctx.ask is not None:
        failed_kinds = sorted({r.kind for r in report.failed})
        # ⚠️ 用 `catalog.KINDS[...].label`（那份中文名是**早就有的**）——
        #    不在这里另写一张映射表。同一件事有两个名字表，
        #    迟早只改一边。
        names = "、".join(
            catalog.KINDS[k].label if k in catalog.KINDS else k
            for k in failed_kinds
        )
        answer = ctx.ask_user(
            f"有 {len(report.failed)} 个文件所有来源都没下到（{names}）。\n\n"
            f"还有一个办法：改用**别的分析中心自己解算的**同类产品"
            f"（iGMAS / 欧洲空间局）。\n"
            f"⚠️ 但那是**另一份产品**，不是同一份文件的镜像 —— "
            f"换上之后算出来的坐标会略有不同（毫米到厘米级），"
            f"和你之前算的、别人算的**不再严格可比**。\n\n"
            f"要用吗？",
            options=[
                {"label": "先别用，我自己想办法（推荐）",
                 "detail": "可以把文件手动放进收件箱，或者换个网络重试"},
                {"label": f"用备选产品顶上（{len(failed_kinds)} 类文件）",
                 "detail": "会标明用的是谁的产品；结果与 IGS 产品有细微差别"},
                {"label": "再试一次镜像", "detail": "网络可能是临时抖动"},
            ],
            timeout=300,
        )

        if answer and "备选" in answer:
            # ⚠️ 自己建会话（要带上用户的代理设置）——
            #    不传的话 sources 会用默认代理，而用户可能选了 manual/none。
            groups, notes = catalog.alternative_files(
                ws.plan, failed_kinds,
                session=net.build_session(ProxyConfig(mode=mode)),
            )

            # ⚠️⚠️ **一组一组地试，不是"一个文件试多个源"。**
            #
            #     每个备选源给的**文件名不一样**（iGMAS 是 `isc10783.sp3`，
            #     ESA 是 `ESA0OPSFIN_….SP3`）。要是把它们塞进同一个
            #     FilePlan 的 `urls` 里，落盘名只能取第一个源的那个 ——
            #     于是一旦落到第二个源，**名字和内容就对不上了**
            #     （实测踩过：下的是 ESA 的钟差，存成了 iGMAS 的名字）。
            #
            #     👉 所以"源的优先级"体现在**组的顺序**上，
            #        哪一组成了就用哪一组，名字永远和内容配套。
            still = set(failed_kinds)
            used_sources: list[str] = []
            used_files: list[str] = []

            for source, files in groups.items():
                todo = [f for f in files if f.kind in still]
                if not todo:
                    continue
                ctx.progress(
                    f"改用备选产品（{catalog.SOURCES[source].label if source in catalog.SOURCES else source}）"
                    f"下载 {len(todo)} 个文件…",
                    fraction=None,
                )
                alt = fetch.fetch_plan(
                    ws.plan, gpaths.data_dir(), ctx=ctx,
                    proxy=ProxyConfig(mode=mode), items=todo,
                )
                # ⚠️ 把这一批**换掉**对应那一批失败的，不是叠加。
                #    叠加的话 `failed` 里会同时留着旧的那条失败记录，
                #    用户会以为还是没下全。
                done_kinds = {f.kind for f in todo}
                report.results = [
                    r for r in report.results if r.ok or r.kind not in done_kinds
                ]
                report.results.extend(alt.results)

                for r in alt.ok:
                    still.discard(r.kind)
                    used_files.append(r.real_name or r.filename)
                if alt.ok:
                    used_sources.append(source)
                if not still:
                    break

            # ⚠️⚠️ 这里**只准备内容，不提前返回** —— 返回挪到函数末尾那个
            #    唯一的记账点**之后**。
            #
            #    原来是 `if used_files: ... return _json(result_alt)`，
            #    而 `ws.fetched = report` 在函数末尾 ——
            #    于是"用户同意了备选、备选也下到了"这个**成功的**结局
            #    反而**没有把报告记进工作区**（`ws.fetched` 一直是 None）。
            #
            #    连带后果（全都在那行之后，所以全被跳过）：
            #      · `workspace_status` 报"已下载 0 个"
            #      · `make_config_inputs()` 走"还没开始下载"那条分支，
            #        拿**计划里的 IGS 文件名**去写配置 ——
            #        而备选产品按**自己的名字**落盘（`isc…` / `ESA0OPS…`）
            #        → `validate_config` 报"缺文件"，**错得离根因极远**
            #      · `missing_kinds` 是空的，"缺了什么"也不会提示
            #
            #    ⚠️ 这正是 `workspace.py` 那条「**计划里有 ≠ 磁盘上有**」
            #       的反面案例 —— 只不过这次被绕过去的是工作区自己。
            #
            #    ⚠️ 我第一版修法是"在这里也写一句 ws.fetched = report"，
            #       那等于同一个赋值出现在**两处** —— 正是这个仓库反复说的
            #       「同一个东西在两处构造，就一定有一处是错的」。
            #       所以改成：**记账只在一处**，返回挪到它后面。
            if used_files:
                alt_payload = json.loads(fetch.fetch_report_json(report))
                alt_payload["alternative_used"] = {
                    "note": (
                        "**这次用的不是 IGS 的综合产品，是备选分析中心自己解算的**"
                        "（文件名可以看出来：iGMAS 是 `isc…` / `isr…` / `isu…`，"
                        "ESA 是 `ESA0OPS…`）。结果与用 IGS 产品时"
                        "可能有毫米到厘米级的差别。"
                        "**转述结论时必须说明这一点。**"
                    ),
                    "sources": used_sources,
                    "files": used_files,
                }
                alt_payload["hint"] = (
                    "⚠️ 用的是备选产品。如果之后拿到了 IGS 的产品，"
                    "重新下载并重跑一遍能得到和'标准流程'一致的结果。"
                )
            else:
                result_note = "；".join(notes) or "备选也没查到"
                report.results.append(
                    fetch.DownloadResult(
                        filename="(备选产品)", ok=False, detail=result_note
                    )
                )

    # =========================================================================
    # ⚠️ 唯一的记账点 —— 所有路径（含上面的备选那条）都要经过这里。
    # =========================================================================
    ws.fetched = report

    if alt_payload is not None:
        return _json(alt_payload)

    result = json.loads(fetch.fetch_report_json(report))

    # 模型文件如实报 —— 成没成都说。
    # ⚠️ 不说的话，用户以为"完整"档能用了，而配置里会缺那几个键，
    #    于是**算出来的和基础档一模一样**，谁也不报错。
    if with_tables:
        if tables_report is None:
            result["tables"] = {"note": "模型文件之前已经下过，本次没重复下载。"}
        else:
            result["tables"] = json.loads(fetch.fetch_report_json(tables_report))
            if not tables_report.all_ok:
                result["tables"]["note"] = (
                    "⚠️ 模型文件没下全。**先别用「完整」档** —— "
                    "缺的模型会让它算得和「基础」档一样，而且不会报错。"
                )

    result["hint"] = (
        "有文件没下来。可以：① 换代理重试；"
        "② 用 probe_data_sources 看看哪些源不通；"
        "③ 让用户自己下载后放进收件箱。"
    )
    if report.failed:
        return _json(result)

    # 全成功时也要**如实说**用的是不是备选 —— 上面那条路已经返回了，
    # 走到这里说明全是镜像。
    return _json(result)


# =============================================================================
# 配置
# =============================================================================


def do_generate_config(ws: Workspace, profile: str = gconfig.DEFAULT_PROFILE) -> str:
    """生成 Ginan 的 YAML 配置。"""
    if ws.plan is None:
        return _err("还没有取数计划。先调用 plan_run。")

    if profile not in gconfig.PROFILES:
        return _err(
            f"没有叫 {profile!r} 的处理档位。"
            f"可用的是：{', '.join(gconfig.PROFILES)}。"
        )

    # ⚠️⚠️ 档位**存在**，但**现在还不能用**（比如 standard 需要那 34 个 tables/）。
    #
    #    必须在这里拦住 —— 放它过去的话，生成的配置里会引用一批
    #    根本不存在的文件，Ginan 跑到一半才报"缺文件"。
    #
    #    ⚠️ 而且拦截**要给出替代方案**，不能只说"不行"：
    #       用户/模型得知道"那我该用什么"。
    if not gconfig.is_available(profile):
        return _err(gconfig.unavailable_reason(profile))

    # ⚠️⚠️ 档位**本身是好的**，但**这个工作流不接受它** —— 和上一条是两件事。
    #
    #    上一条：档位**整体**还没做完
    #    这一条：档位没问题，但拿它做**这件事**会静默算错
    #
    #    ⚠️ 必须在这儿拦。放过去的话，用户拿到的是一条
    #       **从输出上完全看不出错的**轨道（见 `profile_not_allowed_reason`）。
    #
    #    ⚠️⚠️ **绝不能自动换成允许的档位再跑。** 自动降级会让用户以为
    #        他选的档位跑了、实际跑的是另一个 —— 那比拒绝更糟：
    #        拒绝是看得见的，静默替换不是。
    wf = gworkflows.get(ws.workflow)
    if profile not in wf.profiles:
        return _err(gconfig.profile_not_allowed_reason(wf, profile))

    # ⚠️⚠️ 档位能用，但**它要的文件不一定在磁盘上**。
    #
    #    这是原来那条"能选就是一种承诺"的**加强版**：
    #    以前是"这个档位整个没做完"，现在是"这个档位做完了，
    #    但你得先有那几个模型文件"。
    #
    #    ⚠️ 不在这里拦的话会出**最坏的那种错**：
    #       配置里写着完整的模型设置，而文件不在 → Ginan **不报错**，
    #       它只是没去打开那些文件 → 算出来的和「基础」档**一模一样**，
    #       用户以为用了完整档。**没有任何地方会告诉他不是。**
    if gconfig.PROFILES.get(profile, gconfig.PROFILES[gconfig.DEFAULT_PROFILE]).needs_tables:
        missing = fetch.missing_tables()
        if missing:
            names = "、".join(m.filename.split("/")[-1] for m in missing[:4])
            more = f" 等 {len(missing)} 个" if len(missing) > 4 else ""
            return _err(
                f"「完整」档还要 {len(missing)} 个模型文件没下（{names}{more}）。\n"
                f"它们是静态模型，**只下一次，以后一直用**。\n"
                f"👉 先用 `fetch_data` 并带上 `with_tables=true` 把它们补齐，"
                f"再回来生成配置。\n"
                f"⚠️ 现在**不要**改用「基础」档糊弄过去 —— 先问用户愿不愿意下。"
            )

    # ⚠️⚠️ 这个工作流**特有的**模型文件（POD 的重力场）—— 和上一条不是一回事：
    #
    #      `missing_tables`：管「完整」档那 10 个**通用**模型文件
    #      这一条：管工作流自己额外要的（`Workflow.extra_inputs`）
    #
    #    ⚠️⚠️ **必须拦，而且这条比上一条更隐蔽。**
    #
    #       POD 缺 `EGM2008.gfc` 时会发生什么：
    #         ① `config._substitutions` 把 `egm_files` 当空列表 → **删掉那个键**
    #         ② 而模板里 `orbit_propagation.egm_field` 还是 `true`
    #         ③ Ginan 于是**跳过最大的那一项受力，轨道照常输出**
    #
    #       ⇒ 用户拿到一条错的轨道，**没有任何报错**。
    #         这比"跑不起来"糟得多：跑不起来他会来问，跑错了不会。
    #
    #    ⚠️ 判据和 `make_config_inputs` 填 `egm_files` 用的是**同一个函数**
    #       （`table_files_for`），所以"这里检查的那批"和"写进配置的那批"
    #       不可能不是同一批。文件名清单也一样，出自 `TABLE_FILE_CANDIDATES`。
    tables = table_files_for(wf)
    absent = [k for k, v in tables.items() if not v]
    if absent:
        lines = [
            f"   · {k}：找过 {'、'.join(table_file_names(wf, k))}"
            for k in absent
        ]
        return _err(
            f"{wf.label}还缺它**必须要的**模型文件。\n"
            f"\n"
            + "\n".join(lines)
            + f"\n"
            f"\n"
            f"⚠️ 这一条**不能跳过**：缺了它，Ginan 会**跳过最大的那一项受力、"
            f"照常输出轨道** —— 结果是错的，但从输出上看不出来。\n"
            f"\n"
            f"👉 用 `fetch_data` 并带上 `with_tables=true` 把它下下来"
            f"（一次性，之后一直用）。\n"
            f"   它比「完整」档那批通用模型大得多，所以**默认不下** —— "
            f"只有真要跑 {wf.label} 时才需要。\n"
            f"\n"
            f"也可以自己准备一份放进这个目录：\n"
            f"    {gpaths.tables_dir()}\n"
            f"⚠️ 文件名要和上面写的**完全一致**（Ginan 的配置里就按这个名字找）。"
        )

    inp = ws.make_config_inputs(profile)
    cfg = gconfig.build_config(inp)
    path = gconfig.write_config(
        cfg, ws.work_dir / f"{inp.config_id}.yaml", gconfig.config_header(inp)
    )
    ws.config_path = path
    ws.config_inputs = inp

    prof = gconfig.PROFILES[profile]
    payload = {
        "config_path": str(path),
        "profile": prof.label,
        "profile_note": prof.description,
        "tradeoff": prof.tradeoffs,
        # ⚠️ **配置是从哪来的** —— 手写，还是官方示例改的。
        #
        #    为什么要报出来：这两种配置的**可信度不一样**。
        #    模板那条路的骨架来自 Ginan 官方示例（被实测跑通过），
        #    手写那条是 app 自己拼的（对 POD 是残的，实测第 0 个历元就崩）。
        #
        #    ⚠️ 模型要能据此对用户说话 —— 用户问"这份配置可靠吗"时，
        #       "来自官方示例，只改了文件路径"和"程序拼的"是**两个答案**。
        "config_source": gconfig.config_source(wf),
        # ⚠️ 改正项：**实际生效的那一组**（用户改过的覆盖档位之后的结果）。
        #    报出来有两个用处：
        #      · 模型能回答"这次算了哪些改正"（用户问得到，而它看不见配置）
        #      · 用户改过设置之后，能确认改动**真的进了配置**
        #    ⚠️ 措辞里带 `measured` —— 没量过的那几项，谁都不许说它"更好"。
        "corrections": gcorrections.describe(
            gcorrections.resolve(prof.key, inp.corrections)),
        "inputs": {
            "observations": inp.rnx_inputs,
            "orbits": inp.sp3_files,
            "clocks": inp.clk_files,
            "atx": inp.atx_files,
            "nav": inp.nav_files,
        },
        "next": "下一步可以调用 validate_config 检查，或者直接 run_processing。",
    }

    # ⚠️⚠️ 下载失败因而**没能写进配置**的文件，必须主动说出来。
    #
    #    这些文件是被刻意排除的（写进去只会让 Ginan 报"缺文件"，
    #    而那个错误离根因很远）。但**只排除、不说明**更糟 ——
    #    用户会以为配置是完整可跑的，一路跑到失败才发现少东西。
    if ws.missing_kinds:
        payload["missing"] = {
            "kinds": ws.missing_kinds,
            "what": "、".join(
                catalog.KINDS[k].label if k in catalog.KINDS else k
                for k in ws.missing_kinds
            ),
            "why": "这些文件之前**下载失败了**，所以没有写进配置。"
                   "写进去的话 Ginan 会报'缺文件'，那个错误看不出是下载的问题。",
            "what_to_do": "把它们补下来再重新生成配置："
                          "先 fetch_data 重试，或者让用户手动放进收件箱（scan_inbox）。",
        }

    return _json(payload)


def do_validate_config(ws: Workspace, ctx: ToolContext) -> str:
    """跑一次 --dry-run 检查配置。

    ⚠️⚠️ 这一步的价值不在于"会不会失败"，而在于**它抓不到什么**。

       实测确认：`--dry-run` 的退出码只覆盖"YAML 能解析 + 声明的文件都存在"，
       **完全不覆盖 sanity checker** —— 因为 sanityChecks() 返回 void，
       runAllChecks() 的结果被丢弃了。

       而 6 个 checker 里 4 个会**静默改写配置**。

       所以这个工具**必须自己解析 warning 行** ——
       否则用户永远不知道自己给的那份配置被改过。
       这是我们要补的第一层价值。
    """
    if ws.config_path is None:
        return _err("还没有生成配置。先调用 generate_config。")

    result = runner.run_pea(
        ws.config_path, ws.work_dir, ctx=ctx, dry_run=True, timeout=300
    )

    warnings = [d for d in result.diagnosis.diagnostics if d.severity == "warning"]

    return _json({
        "ok": result.ok,
        "headline": result.headline,
        "exit_code": result.exit_code,
        # ⚠️ 这一项是重点 —— 退出码 0 不代表配置没被改
        "config_mutated": bool(
            any(d.kind == "config_mutated" for d in warnings)
        ),
        "warnings": [
            {"what": d.what, "what_to_do": d.what_to_do, "raw": d.raw[:160]}
            for d in warnings
        ],
        "errors": [
            {"what": d.what, "what_to_do": d.what_to_do, "raw": d.raw[:160]}
            for d in result.diagnosis.errors
        ],
    })


# =============================================================================
# 运行
# =============================================================================


def do_run_residual_diagnostics(
    ws: Workspace,
    ctx: ToolContext,
    max_epochs: int | None = None,
) -> str:
    """为了看**观测质量**而重跑一次：同一份配置，只把 trace 的残差打开。

    ============================ 为什么是"重跑" ============================
    Ginan 的残差**不是白来的**（实测，见 `tracefile.py` 文件头）：
    要 `trace.level: 5` + `output_residuals: true` + `output_network: true`，
    而 `level: 5` 会让**接收机**那份 trace 涨到 GB 级。所以默认那条路
    （level 1）里根本没有残差 —— 想看就得重跑一次。

    ⚠️⚠️ 而**重跑不改变结论**：同一份配置 level 1 和 level 5 各跑一遍，
        `.POS` **逐字节相同**（实测 sha256 一致）。所以这一趟拿到的残差
        **就是用户正在看的那份解的残差**，不是另一次解算的。

    ⚠️ 开销（实测，ALIC 2026-09-02 一整天）：**148 秒**、`.TRACE` 约 200 MB
       （网络 144 MB + 平滑 52 MB；接收机那份在这条路上是关掉的，
       否则它自己就要 1.5 GB）。
       跑完压成 `residuals/residual_summary.json`（几百 KB）——
       那个大文件**不删**，删不删由用户决定（同 repro 的规矩）。
    """
    if ws.config_path is None or ws.config_inputs is None:
        return _err("还没有配置 —— 先调用 generate_config。")

    d = ws.work_dir / gresid.DIR_NAME
    d.mkdir(parents=True, exist_ok=True)

    # ⚠️ 同一份 `ConfigInputs`，只动两处：输出目录 + 打开残差。
    #    重算一遍输入是错的 —— 那会让这次跑的输入和主跑**可能不一样**
    #    （文件换了、少了），而"残差对不上那份解"这件事没人看得出来。
    inp = dataclasses.replace(
        ws.config_inputs,
        outputs_root=d,
        trace_residuals=True,
        config_id=f"{ws.config_inputs.config_id}_resid",
    )
    cfg = gconfig.build_config(inp)
    path = gconfig.write_config(cfg, d / f"{inp.config_id}.yaml",
                                gconfig.config_header(inp))

    epochs = max_epochs or gconfig.epoch_count(inp)
    ctx.progress(
        "正在为「残差诊断」重跑一次（同一份配置，只是把 trace 打开）"
        "—— 解算结果**不会变**，只是多留一份残差…", fraction=None)
    result = runner.run_pea(path, d, ctx=ctx, timeout=runner.DEFAULT_TIMEOUT,
                            max_epochs=epochs)
    if not result.ok:
        out = json.loads(result.to_json())
        return json.dumps({
            "ok": False,
            "why": "残差诊断那一趟没跑完。",
            "diagnosis": [d2.what for d2 in result.diagnosis.errors][:3],
            "work_dir": str(d),
        }, ensure_ascii=False)

    # ---- 找残差那份 trace ----
    #
    # ⚠️ 优先 **PPP** 那份（`NET_*.TRACE`）而不是 `_smoothed.TRACE`：
    #    实测平滑那份的 `sigma` 整列是 0，**归一化残差算不出来**
    #    （见 tracefile 的 verdict）。Ginan 自己的脚本默认用平滑那份，
    #    但它的归一化残差图在这里会全是 NaN —— 我们不用它当主数据。
    cands = sorted(p for p in d.glob("NET_*.TRACE")
                   if "smoothed" not in p.stem.lower())
    if not cands:
        cands = sorted(d.glob("NET_*.TRACE"))
    if not cands:
        found = [p.name for p in d.iterdir() if p.suffix.lower() == ".trace"]
        return json.dumps({
            "ok": False,
            "why": "跑完了，但没有网络 trace（`NET_*.TRACE`）—— 残差没写出来。",
            "trace_files": found[:6],
            "work_dir": str(d),
        }, ensure_ascii=False)

    ctx.progress(f"正在读残差（{cands[0].name}）…", fraction=None)
    rf = gtracefile.parse(cands[0])
    summary = gtracefile.summarize(rf)
    v = gtracefile.verdict(summary)

    # 抽稀后的点也存下来 —— 那样画图不必再读那 300 MB
    sampled = {"summary": summary, "points": {}, "verdict": v}
    for meas in rf.kinds:
        rows = gcharts._resid_sample(rf, meas)
        sampled["points"][meas] = [
            {"t": p.t.isoformat(sep=" ", timespec="seconds"),
             "sat": p.sat, "recv": p.recv, "sig": p.sig,
             "prefit": round(p.prefit, 6),
             "postfit": round(p.postfit, 6),
             "sigma": round(p.sigma, 6)}
            for p in rows
        ]
    (d / gtracefile.BUNDLE_NAME).write_text(
        json.dumps(gtracefile.bundle(rf, summary, v,
                                     sampled=sampled["points"]),
                   ensure_ascii=False),
        encoding="utf-8")

    biggest = max(d.glob("*.TRACE"), key=lambda p: p.stat().st_size)
    return json.dumps({
        "ok": True,
        "headline": v["headline"],
        "detail": v["detail"],
        "note": v.get("note", ""),
        "trace_file": cands[0].name,
        "trace_mb": round(cands[0].stat().st_size / 1048576, 1),
        "biggest_trace": biggest.name,
        "biggest_mb": round(biggest.stat().st_size / 1048576, 1),
        "residuals": {
            k: {"label": d2["label"], "n": d2["postfit"]["n"],
                "normalized_std": (round(d2["normalized"]["std"], 3)
                                   if d2["normalized_available"] else None),
                "over_3sigma_pct": (round(d2["frac_over_3sigma"] * 100, 3)
                                    if d2["normalized_available"] else None)}
            for k, d2 in summary["kinds"].items()
        },
        "count": rf.count,
        # ⚠️ `RunResult` 里**没有** `epochs` —— 历元数是从日志里读出来的
        #    （和复现包同一个出处，见 repro.run_record）。
        "epochs_run": repro.run_record(result, result.diagnosis).get("epochs"),
        "seconds": round(result.duration, 1),
        "work_dir": str(d),
        "next": ("下一步：结果面板里点「看残差」，或者直接说'看看残差'。"
                 "⚠️ 那个 .TRACE 有几 MB 到几百 MB，不需要了可以删 —— "
                 "图和统计已经压进 residual_summary.json。"),
    }, ensure_ascii=False)


def do_run_processing(
    ws: Workspace,
    ctx: ToolContext,
    max_epochs: int | None = None,
) -> str:
    """跑 Ginan 处理。

    ⚠️ 参数 max_epochs 是给**试跑**用的 —— 先跑几十个历元确认没问题，
       再跑完整时段。这对新手尤其重要：
       一次完整的处理可能要十几分钟，失败了要重来。

    ⚠️ 长时间运行必须能被取消 —— 用户随时可能想停。
    """
    if ws.config_path is None:
        return _err("还没有生成配置。先调用 generate_config。")

    # ⚠️ 两个"按工作流取"的东西都在这里定 —— 因为**只有这一步知道工作流**。
    #
    #    `environment._check_disk` 那道是通用的（阈值 2 GB），
    #    而 `runner.DEFAULT_TIMEOUT` 的文档写着它的用途是
    #    「**防止无限挂起**（比如那个 OpenBLAS 的坑），
    #      **不是用来限制正常处理的**」——
    #    对 POD 这两样**都不够**，而按它们自己的定义，那都是 bug 不是调优。
    #    取值依据见 `workflows.Workflow` 的 `run_timeout` / `disk_need_gb`。
    wf = gworkflows.get(ws.workflow)
    timeout = wf.run_timeout or runner.DEFAULT_TIMEOUT

    # ---- 磁盘：**开始前**就查，不是跑到一半才发现 ----
    #
    # ⚠️ 为什么在这里**拒绝**而不是只警告一句：
    #    一轮 POD 要 80 分钟。跑到第 60 分钟因为磁盘满而失败 ——
    #    那正是我们刚花力气修掉的那种失败：「**跑一半没了**」，
    #    而且丢掉的还有那 60 分钟的算力。
    #    与其跑一半丢，不如开始前就说清**要多少、还剩多少**。
    #
    # ⚠️ 但**试跑不算**（`max_epochs`）：试跑只跑几十个历元，
    #    磁盘占用按历元数线性分摊，用整天的需求去拦试跑是拦错了 ——
    #    而试跑恰恰是磁盘紧张的用户最该被允许做的事。
    try:
        free_gb = shutil.disk_usage(ws.work_dir).free / (1024**3)
    except OSError:
        free_gb = None

    if free_gb is not None and not max_epochs and free_gb < wf.disk_need_gb:
        return _err(
            f"磁盘空间不够，**先别开始**。\n"
            f"\n"
            f"{wf.label}要写约 {wf.disk_need_gb:.0f} GB"
            f"（其中绝大部分是 Ginan 的 RTS 状态文件，不是结果本身），"
            f"而这个位置只剩 **{free_gb:.1f} GB**：\n"
            f"    {ws.work_dir}\n"
            f"\n"
            f"⚠️ 这不是「可能不够」，是**会跑到一半停下** —— 而那一轮要跑很久。\n"
            f"\n"
            f"怎么办：\n"
            f"  · 清理磁盘后重试。`userdata/runs/` 下旧的那些运行目录是大头，"
            f"但**别整个删** —— 中间文件（`.rts_forward`、`.TRACE`）占了绝大部分，"
            f"而结果（`.POS`/`.SP3`/`.SNX`）只有几 MB；\n"
            f"  · 或者先用 `max_epochs` **试跑**几个历元看看结果对不对"
            f"（试跑不查磁盘，占用也小得多）。"
        )

    # ---- 历元数：**唯一真能刹住 Ginan 的东西** ----
    #
    # ⚠️⚠️ 这一条是 ④ 逼出来的（2026-09-27）：`epoch_control.end_epoch`
    #     在这个版本的 Ginan 里**完全不起作用**。三条对照实验：
    #
    #         end_epoch 设成 01:00，不给 -n  → 跑到 **17,794 个历元**
    #         同样配置加 `-n 5`              → 退出码 0，`Exiting at epoch 5`
    #         两个都不给                      → 跑到 **24,455 个历元**
    #
    #     PPP/POD 之所以一直没暴露，是因为它们的**观测文件到当天为止就没了**
    #     （官方 POD 的日志写着 `Inputs finished at epoch #289`）——
    #     **那是"恰好"，不是"配置管住了"**。
    #
    #     `brdc2sp3` 没有观测 ⇒ 那个"恰好"不存在。实测它跑到 **2022 年**、
    #     100,407 个历元、**1036 个 SP3 共 1.9 GB**，直到被手动杀掉。
    #
    #     ⚠️ 而在那之前，`run_timeout` 会先把算力烧光（POD 那一档是 16 小时）、
    #        并把磁盘写满 —— 用户看到的是"卡住了"。
    #
    # ⚠️ 显式给了 `max_epochs`（试跑）就**用它** —— 绝不覆盖调用方的决定。
    # ⚠️ 算不出来（没设起止时间）就**不传** —— 行为和以前完全一样，
    #    绝不猜一个默认值（猜小了会静默截断结果）。
    epochs = max_epochs
    if not epochs and ws.config_inputs is not None:
        epochs = gconfig.epoch_count(ws.config_inputs)

    ctx.progress("正在启动 Ginan…", fraction=None)
    if max_epochs:
        ctx.progress(f"（试跑模式：只处理前 {max_epochs} 个历元）", fraction=None)

    result = runner.run_pea(
        ws.config_path, ws.work_dir, ctx=ctx, timeout=timeout, max_epochs=epochs
    )
    ws.runs.append(result)

    # ---- 复现包：把"这次结果是怎么来的"写到运行目录里 ----
    #
    # ⚠️ `fetch.py` 的注释里早就写着"详细的在复现包（repro/manifest.json）里"，
    #    而**没有任何代码生成它**（T17）。一句还没兑现的话比不写更糟：
    #    读到的人会以为程序已经替他记着了。
    #
    # ⚠️ **失败的运行也要写** —— "当时跑的是什么、停在哪"正是排查要的东西。
    #    所以这里不做 `if result.ok` 判断。
    #
    # ⚠️ 写失败**不能影响运行本身**（一次成功的处理不该因为清单写不出来就报错），
    #    所以整段包在 try 里，失败只记一条进度提示。
    repro_path: Path | None = None
    try:
        wf = gworkflows.get(ws.workflow)
        cfg = ws.config_path
        repro_path = repro.write_manifest(ws.work_dir, repro.build_manifest(
            workflow=wf.key,
            workflow_label=wf.purpose,
            stations=list(ws.plan.stations) if ws.plan else [],
            day=ws.plan.day.isoformat() if ws.plan else "",
            config_name=cfg.name if cfg else "",
            config_sha256=repro.sha256_of(cfg) if cfg else None,
            inputs=repro.collect_inputs(ws.plan, ws.fetched, gpaths.data_dir()),
            run=repro.run_record(result, result.diagnosis),
            outputs=repro.collect_outputs(ws.work_dir),
            app_version=_app_version(),
            # ⚠️ 记**实际生效的整组**，不是用户改过的那几项 ——
            #    看清单的人要能一眼说出"这次算了哪些改正"，
            #    而不是先知道默认档位再自己推。
            corrections=gcorrections.resolve(
                ws.config_inputs.profile if ws.config_inputs
                else gconfig.DEFAULT_PROFILE,
                ws.corrections),
        ))
    except Exception as exc:  # noqa: BLE001
        ctx.progress(f"（复现包没写出来：{type(exc).__name__}）", fraction=None)

    out = json.loads(result.to_json())
    if repro_path is not None:
        # ⚠️ 路径要**交给模型**：用户要"把结果发给别人"时得知道该打包哪个目录，
        #    而模型看不到我们的文件系统。
        out["repro"] = {
            "manifest": str(repro_path),
            "work_dir": str(ws.work_dir),
            "note": "把 work_dir 整个打包发给别人 —— 里面有配置、产物、日志和这份清单。",
        }
    # ⚠️ 明确告诉模型"接下来该干嘛" —— 它看不到我们的代码流程
    if result.ok:
        out["next"] = "处理成功。下一步调用 summarize_results 解读结果。"
    elif result.cancelled:
        out["next"] = "用户取消了。已完成的进度保留在运行目录里。"
    else:
        out["next"] = (
            "处理失败了。看看上面 problems 里每条的 what_to_do —— "
            "那里面写了该怎么办。"
        )
    return _json(out)


# =============================================================================
# 结果
# =============================================================================


def do_compare_orbits(
    ws: Workspace,
    ctx: ToolContext,
    truth: str,
    test: str = "",
) -> str:
    """把两条轨道比一比 —— 回答"我这条轨道到底准不准"。

    ============================ ⚠️ 为什么它不是"工作流" ============================
    Ginan 的 `compare_orbits` **不在处理流程里**：

        `acsConfig.cpp:4427-4436` —— 它在 `main` 顶部、**在 `checkValidFiles`
        之前**就调 `compareOrbits(sp3_files)` 然后 **`exit(EXIT_SUCCESS)`**。

    ⇒ 它不需要 `process_modes`、不需要观测、不需要 `epoch_control`、
      不需要任何别的输入文件。**只要两个 SP3 路径**，结果打在 stdout。

    ⇒ 硬塞进 `Workflow` 那张表的话，得给它编一组 `modes` 和 `outputs`，
      而那两栏对它一个字节都不成立。（对应 ADR：为什么它是工具不是工作流。）

    ============================ ⚠️ 我们的产物必须先修 ============================
    Ginan 在 Windows 上写 SP3 时，**第 2 个历元起的行首是 `E*` 不是 `*`** ——
    而它的读取器只认 `*`。所以拿一份**没修过**的自己的产物去比，得到的是：

        1 个公共历元、残差 1,800,000 米 —— 一个**看着像结果**的垃圾数

    实测修完之后是 **96 个历元、1.32 米**（广播星历该有的量级）。
    ⇒ 所以 `test` 那一份会**先复制再修**（副本，不动原产物），见 `sp3file`。

    ⚠️ `truth` 是"参照"，`test` 是"被检验的"。**顺序有意义** ——
       反过来的话报出来的数一样，但"谁的"就说不清了。
    """
    # ⚠️ 路径统一成「绝对 + 正斜杠」——和 `config._abs_path` 同一个策略
    #    （见那里的三条理由）。Ginan 两种都吃得下，但一致本身就是价值。
    files: dict[str, str] = {}
    for label, raw in (("truth", truth), ("test", test)):
        if not raw:
            continue
        p = Path(raw).expanduser()
        if not p.is_file():
            return _err(f"找不到要比较的轨道文件（{label}）：{raw}")
        files[label] = p.resolve().as_posix()

    # ⚠️ `test` 省略时用**本次跑出来的** SP3 —— 那是"我的轨道准不准"这个
    #    问题里最常被问的那一份。省略 `truth` 没有合理默认（参照必须由用户给）。
    if "test" not in files:
        produced = ws.sp3_files()
        if not produced:
            return _err(
                f"没给要比较的文件，而且本次任务也没有产出 SP3 轨道文件。\n"
                f"（本次工作目录：{ws.work_dir}）\n"
                f"要么显式指定两个文件，要么先跑一次能产出轨道的处理（POD / brdc2sp3）。"
            )
        files["test"] = str(produced[0])

    if "truth" not in files:
        return _err(
            "要比对轨道就得有一个**参照**。请指定参照文件 —— "
            "通常是同一时段的 IGS 精密轨道（SP3）。"
        )

    # ---- 我们自己的产物先修 `E*` ----
    #
    # ⚠️ **修的是副本。** 原产物是用户的，我们没有理由改它 ——
    #    而且修过一次之后文件就和"Ginan 产生的东西"不一样了，
    #    下一个人不知道的话会以为 Ginan 写的就是对的。
    src = Path(files["test"])
    repaired = ws.work_dir / f"{src.stem}_forcmp{src.suffix}"
    try:
        n_fixed = gsp3.repair_file(src, repaired)
    except OSError as e:
        return _err(f"读不了 {src.name}：{e}")
    files["test"] = repaired.resolve().as_posix()

    # ---- 配置：直接载入打包好的官方示例 ----
    #
    # ⚠️ 不走 `config.build_config` —— 那个函数按 `Workflow` 分支，
    #    而这条根本不是工作流（见上）。直接用模板本身。
    cfg = gexamples.load("compare_orbits.yaml")
    cfg["inputs"]["inputs_root"] = str(ws.work_dir.resolve()).replace("\\", "/") + "/"
    cfg["inputs"]["satellite_data"]["sp3_files"] = [files["truth"], files["test"]]
    cfg["outputs"]["outputs_root"] = str(ws.work_dir.resolve()).replace("\\", "/") + "/"

    cfg_path = ws.work_dir / "compare_orbits.yaml"
    with cfg_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    ctx.progress("正在比对两条轨道…", fraction=None)
    result = runner.run_pea(cfg_path, ws.work_dir, ctx=ctx, timeout=600.0)

    # ⚠️⚠️ **结果只能从 `result.output()` 拿，它读的是日志文件。**
    #
    #    `RunResult` **没有** `stdout` 字段（字段是
    #    `ok/exit_code/duration/handle/diagnosis/...`），pea 的输出
    #    被 `run_pea` 逐行写进 `handle.log_path`。
    #    第一版我写的是 `result.stdout` —— 那会在真跑时 `AttributeError`，
    #    而守卫**测不出来**，因为守卫的 stub 自己造了一个 `stdout`。
    #    （stub 现在改用**真的** `RunResult` + 真的日志文件，见 ⑲d。）
    cmp = gsp3.parse_comparison(result.output())
    v = gsp3.verdict(cmp)

    # ⚠️ `problems` 嵌在 **`diagnosis`** 下面，不在顶层 ——
    #    第一版写的是 `.get("problems", [])`，那**永远**返回空表
    #    （顶层没有这个键），于是"为什么失败"一个字都传不出去。
    diag = json.loads(result.to_json()).get("diagnosis") or {}
    problems = diag.get("problems", [])

    # ---- ⚠️⚠️ 判据是「**有没有拿到结果**」，不是 `result.ok` ----
    #
    #     实测撞到过（2026-09-27，真跑这个工具才发现的）：
    #
    #         官方 `compare_orbits.yaml` 里写了 `chi_square.mode: none`，
    #         而 `E_ChiSqMode`（`enums.h:399-404`）**只有三个值**：
    #         `INNOVATION` / `MEASUREMENT` / `STATE` —— **没有 `NONE`**。
    #         （而示例自己的注释里把 `NONE` 列为合法值。）
    #
    #         ⇒ 配置加载时必定打一条 `Error: none is not a valid entry`，
    #           诊断把它判成"运行失败" ⇒ `result.ok == False`。
    #
    #     **但那次比对是成功的** —— 日志里有完整的
    #     `& Agg-All … to: 1.319809 in 3D over 2976 entries`。
    #     那个枚举在一个 `enable: false` 的块上，**不影响任何计算**。
    #
    #     ⇒ 拿 `result.ok` 当门，会把一个**已经拿到的答案**说成"失败了"。
    #       这是本项目最不喜欢的那种错（有数据却说没有）。
    #       判据改成"解出 TOTAL 块了吗"，而把配置的抱怨挂成 caveat 说出去。
    if cmp.rms_3d is None:
        return _json({
            "ok": False,
            "error": "比对没能正常完成 —— 从它的输出里读不到统计结果。",
            "exit_code": result.exit_code,
            "headline": result.headline,
            "problems": problems,
        })

    payload: dict[str, Any] = {
        "truth": Path(files["truth"]).name,
        "test": Path(files["test"]).name,
        "verdict": {
            "headline": v.headline,
            "quality": v.quality,
            "details": v.details,
            "caveats": v.caveats,
        },
        "numbers": {
            "satellites": cmp.satellites,
            "epochs": cmp.epochs,
            "rms_3d_after_helmert_m": cmp.rms_3d,
            "rms_before_helmert_m": (cmp.agg.get("3D").before if cmp.agg.get("3D") else None),
        },
        "note": (
            "headline 是给人看的一句话结论，直接转述即可；"
            "caveats 必须一并说明 —— 那是这个数的适用边界。"
        ),
    }

    # ⚠️ 结果拿到了，但 Ginan 途中报过错 —— **如实带出去**，不吞。
    #
    #    吞掉的话，"这次跑有报错"这件事就消失了；而不说清的话，
    #    模型可能把一条**无影响的**配置抱怨当成"结果不可信"。
    #    所以两个都写：有报错、以及它是不是影响了结果。
    if problems:
        payload["warnings"] = {
            "problems": problems,
            "what": "Ginan 在这次运行里报过上面这些。**结果仍然有效** —— "
                    "判据是「统计块出来了没有」，不是「它有没有抱怨」。",
            "note": "转述时可以说「这次比对有报错，但不影响结果」，"
                    "不要因此说结果是错的。",
        }

    # ⚠️ 修过就**说出来**。不说的话，用户永远不知道那个缺陷影响过他的产物。
    if n_fixed:
        payload["repaired_epoch_tags"] = n_fixed
        payload["repair_note"] = (
            f"⚠️ 被检验的那份产物有 {n_fixed} 个历元标签是坏的（Ginan 在 Windows 上"
            f"把它们写成了 `E*`）—— 比对前先修了**副本**，原文件没动。"
            f"不修的话 Ginan 只读得到一个历元，比出来的数看着像结果其实是垃圾。"
        )

    return _json(payload)


def do_summarize_results(ws: Workspace) -> str:
    """解读定位结果，给出人话结论。

    ⚠️ 这是**产品差异化的落点**。不要只返回数字 ——
       我们的用户完全不懂 GNSS，给他 RMS 表格等于没给。
    """
    pos_files = ws.pos_files()
    if not pos_files:
        if not ws.runs:
            return _err("还没有跑过处理。先调用 run_processing。")
        return _err(
            "处理跑过了，但没有产生定位结果文件。"
            "这通常意味着数据不足以解算 —— 看看 run_processing 返回的诊断。"
        )

    summaries = []
    for p in pos_files:
        pf = posfile.parse(p)
        if pf.count == 0:
            continue
        summaries.append({
            "file": p.name,
            "data": json.loads(posfile.summarize_json(pf)),
        })

    if not summaries:
        return _err("定位结果文件是空的 —— 可能处理中途失败了。")

    payload = {
        "results": summaries,
        "note": (
            "上面的 verdict 里有一句人话结论，直接转述给用户即可。"
            "caveats 里的内容**必须一并说明** —— "
            "那是这个结论的适用边界，藏起来会误导人。"
        ),
    }

    # ⚠️⚠️ **前向解和平滑解的数字不能并排摆着让用户自己比。**
    #
    #    实测：一次运行产生两个文件 ——
    #        前向解   重复性 6.0 厘米（实测 ALIC 2022-03-01 整天）
    #        平滑解   重复性 0.0～2.9 厘米   ← 看着更好，其实**这个指标对它没定义**
    #
    #    ⚠️ 这里原来写的是「前向解 2.8 厘米 / 平滑解 0.0 厘米」——
    #       前向那个 2.8 出自另一次运行（口径不同），而平滑那个 0.0
    #       **根本不稳定**：同一个站同一天三次运行是 0.0 / 2.7 / 2.9。
    #       用一组只在某一次成立的数字当例子，下一个读的人会以为它是通则。
    #
    #    两个矛盾的结论并排出现时，用户会挑那个"更好看"的信 ——
    #    而这次更好看的那个恰好是**什么都没说**的那个。
    #
    #    所以两份都在时，主动把关系说清楚，并且明确要求模型**别引用
    #    平滑解那个重复性**。
    kinds = {s["data"].get("solution") for s in summaries}
    if "smoothed" in kinds and "forward" in kinds:
        payload["comparison"] = {
            "两份是什么": (
                "同一次运行产出了两份结果：`.POS` 是**前向解**（每个历元一个位置），"
                "`_smoothed.POS` 是**平滑解**（RTS 反向平滑，用全天数据回头修）。"
            ),
            "数字不能直接比": (
                "前向解那个「重复性」是真实的历元间离散程度；"
                "平滑解会把位置压成常数（实测整天那份 2880 个历元里"
                "只有 2 个不同的位置），"
                "它的「重复性」反映的是「被切成了几段常数」，**不是精度** —— "
                "实测同一个站同一天三次运行是 0.0 / 2.7 / 2.9 厘米。"
            ),
            "哪份更准": (
                "平滑解通常**更接近真实位置**（实测 ALIC 那天：平滑解离权威坐标 "
                "1.6 厘米，前向解 7.7 厘米）。但「到底多准」要靠权威坐标去比，"
                "程序自己算不出来。"
            ),
            "怎么跟用户说": (
                "给结论时**用前向解说明稳定性，用平滑解说明最优位置**，"
                "并且说清这是两份不同的产物。"
                "⚠️ 不要把平滑解的「重复性」当成精度报出去 —— "
                "它可能是 0.0，也可能是 2.9，**两个都不是精度**。"
            ),
        }

    # ⚠️⚠️ **运行时的警告必须跟着结论一起走。**
    #
    #    这里原来**只看 `.POS` 文件** —— 于是"跑完了但有一堆严重警告"
    #    和"一切正常"给出的是**完全相同的结论**。
    #
    #    实测踩到：一次运行里有 227 条警告（天线没填 → 没做相位中心改正；
    #    先验坐标差 2 公里；对流层模型拿到 -1000 米的高度；回退到单频），
    #    而界面上显示的是"**处理正常完成**"。
    #
    #    **一个说"正常"但实际不可信的结论文，比没有结论更糟** ——
    #    用户会拿着它去做分析、写论文。
    run_warnings: list[dict] = []
    if ws.latest_run is not None:
        for x in ws.latest_run.diagnosis.warnings:
            run_warnings.append({
                "kind": x.kind,
                "what": x.what,
                "what_to_do": x.what_to_do,
                "raw": x.raw[:160],
            })

    if run_warnings:
        payload["run_warnings"] = run_warnings
        payload["note"] = (
            "上面的 verdict 里有一句人话结论，直接转述给用户即可。"
            "caveats 里的内容**必须一并说明** —— "
            "那是这个结论的适用边界，藏起来会误导人。\n"
            "\n"
            "⚠️⚠️ **`run_warnings` 也必须一并说出来**，不能只报好消息：\n"
            "    那是 Ginan 在这次运行里报的警告。它们**不影响「跑完」**，\n"
            "    但其中一些会**明显影响结果的可信度**（比如没做天线改正、\n"
            "    先验坐标差上千米、回退到单频）。\n"
            "    **用户看不到这个文件，你不说他就永远不知道。**"
        )

    return _json(payload)


# =============================================================================
# 用户自己的数据
# =============================================================================


def do_inspect_rinex(path: str) -> str:
    """检查一份 RINEX 观测文件能不能用。

    ⚠️ 这是给"用户自己的测站"这个场景的入口。

       自建站的数据只在用户手里 —— 去数据源下载必然 404，
       而那个报错看起来像网络问题，把排查带偏。

       这个工具回答的问题是：**"我这份数据能做 PPP 吗？"**
       而不是等跑完了才发现不行。
    """
    from ..ginan import rinex

    p = Path(path).expanduser()
    if not p.is_file():
        return _err(
            f"找不到这个文件：{p}\n"
            "请确认路径写对了。也可以把它放进收件箱，程序会自动认领。"
        )

    info = rinex.inspect(p)
    return rinex.to_json(info)


def do_list_antennas(query: str = "") -> str:
    """从天线表里找型号。

    ⚠️ 这个工具的存在理由：**型号名必须和表里逐字一致。**

       用户从设备标签上看到 `TRM59800.00`，直接填进配置会失败 ——
       因为表里写的是 `TRM59800.00 NONE`（还带隔离罩代码）。
       而报错是"找不到天线"，看不出是**格式**问题还是"表里真没有"。

       与其让用户猜，不如把表里的候选列出来给他挑。
    """
    from ..ginan import atx

    info = atx.describe()
    if not info["available"]:
        return _json({
            **info,
            "hint": (
                "天线表还没下载。它是必需文件，"
                "在下载精密产品的同一个流程里会一起拿到。"
            ),
        })

    results = atx.search(query, limit=25)
    return _json({
        **info,
        "query": query,
        "results": [str(a) for a in results],
        "hint": (
            "把用户的天线型号（可以只记得一部分）作为 query 传进来，"
            "从结果里挑。**必须原样使用结果里的字符串** —— "
            "一个字符都不能改（大小写、空格、隔离罩都要对）。"
        ),
    })


def do_repair_rinex(
    path: str,
    marker: str = "",
    antenna: str = "",
    receiver: str = "",
) -> str:
    """补全 RINEX 文件头里缺失的字段，写出一份新的。

    ⚠️ **绝不覆盖原文件。**

       用户的原始数据是**不可再生的** —— 那是他扛着接收机在野外测出来的。
       而我们只是补几个头部字段，改坏了得不偿失。
    """
    from ..ginan import rinex

    src = Path(path).expanduser()
    if not src.is_file():
        return _err(f"找不到文件：{src}")

    before = rinex.inspect(src)

    # ⚠️ 没给的值**尝试自己推** —— 能推的别麻烦用户
    if not marker:
        marker = rinex.infer_marker_from_filename(src)

    if not antenna and before.antenna:
        antenna = before.antenna      # 原本就有，不用改

    # 输出名用 RINEX 3 的规范格式
    #
    # ⚠️ 名字要**有意义** —— 用户后面要靠它认出这是哪个站哪天。
    #    原来的 `dcms260101000000.26O` 是 RINEX 2 的老命名，
    #    而且和内容（RINEX 3.04）对不上。
    #
    # ⚠️ 站号**原样使用，不要补位**。
    #
    #    踩过一次：`(marker).ljust(9, "0")` 把 `DCMS` 变成了 `DCMS00000`。
    #    看着像 RINEX 的 9 位站号，其实**不是** ——
    #    那个格式是「4位站号 + 2位标志 + 3位国家码」，比如 `ALIC00AUS`。
    #    补零补出来的是个四不像，懂 GNSS 的人一看就知道是程序瞎编的。
    #
    #    用户给什么就用什么。Ginan 读的是**文件头里的 MARKER NAME**，
    #    文件名只是个标识。
    day = ""
    if before.first_obs:
        day = before.first_obs.strftime("%Y%m%d")
    interval = int(before.interval) if before.interval else 30
    station = (marker or "UNKNOWN").upper()[:9]
    out_name = f"{station}_R_{day}0000_01D_{interval:02d}S_MO.rnx"

    dst = gpaths.data_dir() / out_name
    if dst.exists():
        return _err(
            f"数据目录里已经有同名文件了：{out_name}\n"
            "如果你要重新生成，先把旧的删掉或者改名。"
        )

    ok, msg = rinex.repair(
        src, dst,
        marker=marker, receiver=receiver, antenna=antenna,
    )
    if not ok:
        return _err(f"修补失败：{msg}")

    after = rinex.inspect(dst)
    v = rinex.verdict(after)

    return _json({
        "ok": True,
        "output": str(dst),
        "output_name": out_name,
        "original_untouched": str(src),
        "note": "原文件没有被改动 —— 这是新写出来的一份。",
        "still_missing": after.needs_input,
        "ready_to_run": after.ready,
        "verdict": v["headline"],
        "next": (
            "可以接着制定取数计划了。"
            if after.ready
            else "还缺信息，见 still_missing。"
        ),
    })


def _obs_in(directory) -> list[dict]:
    """列出某个目录里能当观测数据用的文件。

    ⚠️⚠️ **按内容判断，不按扩展名。** 这里原来写的是

            p.suffix.lower() in (".rnx", ".obs")

       —— 而这是个真 bug，用户撞上了：

           他的文件叫 `alic0600.22o`（RINEX 2 的**标准短名**，完全正常），
           内容明明白白是观测数据（marker=ALIC00AUS、2022-03-01、可读），
           但后缀不是 .rnx → **被这一行滤掉了** → 工具报"收件箱是空的"。

           于是他看到的是：明明刚认领了自己的数据，
           程序却说"你还没放过观测文件"，然后问他要测站和日期 ——
           而那两个答案**就在文件的头和名字里**。

       ⚠️ 更值得记的是：**它和这个项目最重要的那条原则正好相反。**
          `identify.py` 开篇就写着"识别靠内容，不靠文件名"，
          还有整整一节测试守着它（连"文件名是假的"都专门测了）。
          然后在**这一个函数**里，又悄悄按扩展名筛了一遍。

          👉 一个原则只要有一处不照做，那一处就是全部。
             **别处都按内容判断，不能成为这一处不这么做的理由。**

    ⚠️ `rinex.inspect()` 读的是文件头（不是整个文件），
       所以 50 MB 的观测文件也只读开头几 KB，不会卡。
    """
    from ..ginan import identify, rinex

    out = []
    try:
        for p in sorted(directory.iterdir()):
            if not p.is_file():
                continue

            # ⚠️ 先按**内容**认：能当观测数据的才看。
            #    压缩态（.crx.gz / .Z）identify 也认得出来，但那种文件
            #    rinex.inspect 读不了 —— 所以下面单独兜住，如实报出来，
            #    而不是假装它不存在。
            ident = identify.identify(p)
            if ident.kind != "obs":
                continue

            try:
                info = rinex.inspect(p)
            except Exception:  # noqa: BLE001
                out.append({
                    "file": p.name,
                    "marker": ident.marker,
                    "start": None,
                    "duration_minutes": None,
                    "usable": False,
                    "problems": [
                        f"内容是观测数据，但读不出文件头"
                        f"（压缩态是 {ident.compression or '未知'}？）"
                        "—— 认领时会自动解压，直接认领即可"
                    ],
                })
                continue

            out.append({
                "file": p.name,
                "marker": info.marker,
                "start": info.first_obs.isoformat() if info.first_obs else None,
                "duration_minutes": round(info.duration_minutes, 1),
                "usable": info.ok,
                "problems": info.problems,
            })
    except OSError:
        pass
    return out


def do_list_local_obs() -> str:
    """看看有哪些观测数据 —— **收件箱和你放进去的，分开说**。

    ⚠️⚠️ 用户的原话：
        "点准备数据应该是检查我**收件箱**里的数据"
        "2026-09-02 这个数据我没放到收件箱，他在说什么"

       当时的行为：去 `data/` 里翻了一遍，把**两个都报出来** ——
       其中 `2026-09-02` 是**程序早先自己下载的缓存**，
       而用户从来没放过它。于是他的判断是"它记着以前的东西没忘"。

       ⚠️ 他说得没错 —— **从用户的角度看，把缓存里的东西说成"你的数据"
          和"它记得以前的事"是一回事**，因为两者他都没主动做过。

    ⚠️ 所以这里**必须分开报**，而且标题要说清楚是谁放的：

        你放进来的（收件箱）    ← 用户认得的东西
        程序之前弄来的（缓存）  ← 用户没参与，只是程序下过

    ⚠️ 只在收件箱空的时候**提一句**缓存里有东西，不要主动展开 ——
       用户问"我的数据"时，他要的是**他自己的**。
    """
    from ..ginan import inbox as ginbox

    inbox_dir = gpaths.inbox_dir()
    yours = _obs_in(inbox_dir)

    data_dir = gpaths.data_dir()
    cached_all = _obs_in(data_dir)

    # ⚠️⚠️ **认领过的那一份要摘出来。**
    #
    #     认领是**复制**：用户的文件既在收件箱里，又在数据目录里（改了名）。
    #     不摘的话，**同一份数据会同时出现在两组里** ——
    #     而"缓存"那一组带着说明"不是用户放进来的"，
    #     模型照着一转述就变成"程序之前在本地留了一份"，
    #     用户会以为程序在说别人的东西。
    #
    #     ⚠️ 比"重复"更糟的是它**自相矛盾**：
    #        "你放进来的：[无]" + "缓存里有 6 个" ——
    #        而其中一个是用户自己的。用户刚认领完数据，
    #        转头被告知"你还没放过数据"，他会以为认领没成功。
    items = ginbox.scan(inbox_dir)
    pairs = ginbox.claimed_pairs(items)
    mine_in_data = {p.name for p in pairs.values()}

    cached = [c for c in cached_all if c["file"] not in mine_in_data]

    payload: dict = {
        "你放进来的": yours,
        "收件箱路径": str(inbox_dir),
        "note": (
            "「你放进来的」是收件箱里的文件 —— 那才是**用户自己提供的**数据。"
            "只报这一组就能回答'我的数据能算什么'。"
        ),
    }

    # ⚠️ 认领过的那些要**说清楚它们在哪** ——
    #    用户可能已经认领了（数据目录里有一份），而那才是真正会被处理的那份。
    #    不说的话，模型会以为"文件还在收件箱 = 还没收拾好"，再去催一次。
    if mine_in_data:
        payload["已经认领到数据目录的"] = sorted(mine_in_data)
        payload["认领说明"] = (
            "这些**是用户自己的数据**（他放进收件箱、程序认领过去的），"
            "数据目录里那份才是要处理的那一份。\n"
            "⚠️ 它们**不是**缓存 —— 别把它们说成'程序之前下载的'。"
            "也不要再问用户'要不要认领'，他早就确认过了。"
        )

    # ⚠️ 缓存那份**默认不展开**，给个数量就够 —— 见上面的说明。
    #    用户真要问"本地还有没有别的"，再单独看。
    if cached:
        payload["程序以前弄来的_缓存"] = {
            "数量": len(cached),
            "说明": (
                "这些是**程序之前自己下载或认领的**，不是用户放进来的。"
                "⚠️ 别把它们说成'你的数据' —— 用户会以为程序记得以前的事。"
                "要提的话，说清楚'程序之前在本地留了一份'。"
            ),
        }

    if not yours:
        payload["note"] = (
            "⚠️ 收件箱是空的 —— 用户还没有自己放过数据。"
            "如果缓存里有东西，**说清那是程序之前下载的**，别让用户以为是他放的。"
        )

    return _json(payload)


# =============================================================================
# 问用户
# =============================================================================


def do_ask_user(
    ctx: ToolContext,
    question: str,
    options: list[dict],
    allow_text: bool = True,
) -> str:
    """问用户一个问题，**阻塞**等他回答，把答案交给模型。

    ⚠️⚠️ 这个工具存在的理由，是用户的一句原话：

        "他说确认的话我就开始，**没有选项**"

        原来是模型在正文里写一句"你确认的话我就开始"，然后停下。
        界面上只有**一行字** —— 没有按钮、没有可点的东西。
        用户不知道该回什么，也不知道程序是在等他还是在卡着。

    ⚠️ 为什么不做成"界面从模型的话里猜"（看到问号就弹按钮）：
       模型的反问和"要用户拍板"长得一模一样。猜错的时候，
       用户会看到两个跟他的话毫无关系的按钮 —— 比没有按钮更糟。
       **能结构化的东西不要靠解析。**
    """
    # ⚠️ 选项要**规范一遍**再发出去 —— 模型给的东西不能直接信。
    cleaned: list[dict] = []
    for i, opt in enumerate(options or [], start=1):
        if not isinstance(opt, dict):
            continue
        label = str(opt.get("label") or "").strip()
        if not label:
            continue
        item: dict = {"id": str(i), "label": label[:60]}
        detail = str(opt.get("detail") or "").strip()
        if detail:
            item["detail"] = detail[:200]
        cleaned.append(item)
        if len(cleaned) >= 6:       # 再多就不像"选项"、像一份表格了
            break

    if not cleaned:
        return _err(
            "问用户至少要给一个选项。"
            "如果你只是想让他随便说点什么，就别用这个工具 —— 直接说就行。"
        )

    answer = ctx.ask_user(question, cleaned, allow_text=allow_text)
    if answer is None:
        # ⚠️ None 有两种可能，**都要如实说**，别猜是哪一种。
        return _json({
            "answered": False,
            "note": (
                "用户没有回答 —— 可能是他中止了这一轮，"
                "也可能当前环境没有可以提问的界面。\n"
                "**不要重复调用这个工具**，那只会再卡一次。"
                "请改用别的方式：要么按最合理的默认值继续并把你的假设说清楚，"
                "要么在正文里用一句话问他、然后结束这一轮。"
            ),
        })

    return _json({"answered": True, "answer": answer})


# =============================================================================
# 目录
# =============================================================================


def do_get_data_dir() -> str:
    return _json({
        "data_dir": str(gpaths.data_dir()),
        "inbox_dir": str(gpaths.inbox_dir()),
        "runs_dir": str(gpaths.runs_dir()),
        "explain": (
            "data_dir 是 Ginan 读取输入文件的地方；"
            "inbox_dir 是用户可以手动放文件的地方，程序会自动认领。"
        ),
    })


def do_advise_from_inbox() -> str:
    """看一眼收件箱里**这一大堆文件能算什么**。

    ⚠️⚠️ 这个工具补的是一个**早就被承诺、但一直没有工具支撑**的能力。

       `prompts.py` 里写着：

           → 然后说清楚：这份数据**能算什么**、**还缺什么**、
             缺的**能不能自动下**。这才叫"给建议"。

       —— 而当时**没有任何工具能回答这三句话**：

         · `scan_inbox`      有 kind，**没有站名、没有时间**
         · `list_local_obs`  有站名、有时间，但**把非 obs 全丢了**
                             —— 而 SP3/CLK 恰恰是"能不能算"的关键

       两边**无处可 join**，模型只能把上百行摆成一张表在脑子里心算 ——
       而工具输出有 8000 字符上限，那么多行**会被截断**。

    👉 所以这个工具**聚合**：按站、按天，把"观测 × 时间 × 辅助文件"
       三样 join 到一起，直接给出 `(站, 天)` 候选清单 ——
       那正是 `catalog.plan_run(station, day)` 要的两个参数。

    ⚠️ 它是**只读**的：不认领、不搬、不改任何东西。
       要看"准备搬什么"用 `scan_inbox`。
    """
    scanned = inbox.scan_detailed()
    pile = ginadvise.analyze(
        scanned.items, too_deep_files=scanned.too_deep_files
    )
    return ginadvise.to_json(pile)


def do_scan_inbox(dry_run: bool = True) -> str:
    """扫描收件箱并（可选地）认领。

    ⚠️ 默认 dry_run=True —— **只报告，不动文件**。
       用户应该先看到"程序认出了什么、准备搬到哪"，再决定要不要搬。
       直接动用户的文件是不礼貌的，而且搬错了错很难解释。
    """
    result = inbox.claim(dry_run=dry_run)
    out = json.loads(inbox.claim_to_json(result))
    out["dry_run"] = dry_run

    # ⚠️⚠️ 认领是**复制**，原件留在收件箱 —— 所以**认领完再扫一遍，
    #     收件箱里那些文件还在**。不区分的话，模型会一遍遍地说
    #     "收件箱里有 N 个文件，要不要认领？"，而它们**早就认领过了**。
    #
    #     （用户原话："程序认领了我的数据后，右下角收件箱就不要提示认领了"。）
    #
    # 👉 所以这里多报两件事：
    #      · 哪些**已经在数据目录里**了
    #      · 还有几个**真的**没认领
    #    模型据此就能跳过"要不要认领"这一步，直接进入分析。
    items = inbox.scan()
    done = inbox.already_claimed(items)
    pending = [i for i in items if i.known and i.path not in done]

    out["already_in_data"] = sorted(
        i.path.name for i in items if i.path in done
    )
    out["still_unclaimed"] = sorted(i.path.name for i in pending)

    if not pending and out["already_in_data"]:
        out["note"] = (
            "收件箱里那些文件**都已经认领过了**（数据目录里有同样的内容），"
            "不需要再问用户要不要认领。\n"
            "👉 直接往下走：调 advise_from_inbox 看这份数据能算什么、还缺什么，"
            "然后照着它给用户建议。"
        )
    elif dry_run and result.moves:
        out["note"] = (
            "这是**预览**，文件还没有被移动。"
            "用户确认后再用 dry_run=false 真正搬。"
        )
    return _json(out)


# =============================================================================
# 包装成 Tool
# =============================================================================


def build_ginan_tools(ws: Workspace) -> list[Tool]:
    """构造 Ginan 工具集。

    ⚠️ 工作区是通过**闭包**捕获的，不是模块级全局。
       这样测试能造自己的工作区，两个 agent 也各有各的。
       （对比 core/tools.py 里那个踩过的坑：工具注册表用了模块级变量，
         导致"传进来的工具集不生效"。）

    ⚠️ 需要 ctx 的工具，函数签名的第一个参数必须是 ctx
       （core/tools.py 会按名字注入）。
       需要工作区的则是第一个参数叫 ws。
    """

    def tool_plan(stations: list[str], day: str,
                  workflow: str | None = None) -> str:
        return do_plan_run(ws, stations, day, workflow)

    def tool_fetch(ctx: ToolContext, proxy_mode: str = "auto",
                   with_tables: bool = False) -> str:
        return do_fetch_data(ws, ctx, proxy_mode, with_tables)

    def tool_config(profile: str = gconfig.DEFAULT_PROFILE) -> str:
        return do_generate_config(ws, profile)

    def tool_validate(ctx: ToolContext) -> str:
        return do_validate_config(ws, ctx)

    def tool_run(ctx: ToolContext, max_epochs: int | None = None) -> str:
        return do_run_processing(ws, ctx, max_epochs)

    def tool_summarize() -> str:
        return do_summarize_results(ws)

    def tool_residuals(ctx: ToolContext, max_epochs: int | None = None) -> str:
        return do_run_residual_diagnostics(ws, ctx, max_epochs)

    def tool_compare(ctx: ToolContext, truth: str, test: str = "") -> str:
        return do_compare_orbits(ws, ctx, truth, test)

    def tool_status() -> str:
        return do_workspace_status(ws)

    return [
        # ---------------------------------------------------------------
        # 环境
        # ---------------------------------------------------------------
        Tool(
            name="check_environment",
            description=(
                "检查这台机器能不能跑 Ginan：主程序在哪、依赖齐不齐、"
                "磁盘够不够、AI 密钥配了没。**不联网，秒回。**\n"
                "\n"
                "什么时候用：\n"
                "  · 用户第一次使用时，先跑这个建立基线\n"
                "  · 用户说'跑不起来''报错了''没反应'时，先跑这个\n"
                "  · 任何要动 Ginan 的操作之前"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            function=do_check_environment,
            max_result_chars=12_000,
        ),
        Tool(
            name="probe_data_sources",
            description=(
                "测试各个数据源现在能不能连上。**需要联网，约 3-5 秒。**\n"
                "\n"
                "返回按用途分组（精密产品 / 观测数据 / 模型文件），"
                "每组会说明'够不够用'。连不通的会附上'该怎么办'，"
                "直接转述给用户即可。\n"
                "\n"
                "什么时候用：用户问'为什么下载不了'、准备下载之前、"
                "用户说开了/关了代理之后。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "proxy_mode": {
                        "type": "string",
                        "enum": ["auto", "manual", "none"],
                        "description": "auto=跟随系统（默认），none=直连，manual=用设置里的代理",
                    }
                },
                "required": [],
            },
            function=do_probe_sources,
            wants_context=True,
        ),

        # ---------------------------------------------------------------
        # 计划
        # ---------------------------------------------------------------
        Tool(
            name="plan_run",
            description=(
                "为某个测站某一天制定处理计划：列出需要哪些文件、"
                "每个从哪几个源能拿到、多大、干什么用的。\n"
                "\n"
                "⚠️ **这是整个流程的第一步，也是下载之前的必经步骤。**\n"
                "不要凭空猜文件名 —— Ginan 的文件名必须逐字精确，"
                "猜错一个字就跑不起来。\n"
                "\n"
                "调用后会**保存**这个计划，后面的 fetch_data / "
                "generate_config 都基于它。\n"
                "\n"
                "什么时候用：\n"
                "  · 用户说'处理某个站某天的数据'时 —— 这是第一步\n"
                "  · 用户换了站号或日期时，重新调用一次\n"
                "  · 需要告诉用户'要下载什么、多大、从哪下'时"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow": {
                        "type": "string",
                        "enum": list(gworkflows.choices()),
                        # ⚠️⚠️ 这张清单**从 `workflows.WORKFLOWS` 现推**，
                        #     不在这里另抄一遍 —— 抄的那份一定会漂，
                        #     而漂的后果是"描述里有这个选项、实际跑不了"。
                        "description": (
                            "**要做什么事**。不给就沿用当前那个（默认 ppp）。\n"
                            "\n"
                            + "\n".join(
                                f"  · {k} —— {w.label}：{w.purpose}"
                                for k, w in gworkflows.WORKFLOWS.items()
                            )
                            + "\n\n"
                            "⚠️ 判据是**用户想要什么产物**，不是你手上有什么数据：\n"
                            "   '看看这个站在哪' → ppp；'帮我定个轨道' → pod。\n"
                            "⚠️ pod 会多下一批模型文件（重力场，约 72 MB，一次性），"
                            "而且**只支持「完整」档**。"
                        ),
                    },
                    "stations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "要处理的测站代码，每个 4 个字母，例如 [\"ALIC\"]。"
                            "给**多个**站会一次配置、一次运行把它们都算完，"
                            "用户想算一片区域时用这个。\n"
                            "\n"
                            "⚠️⚠️ **各站之间是什么关系，取决于 workflow**：\n"
                            "  · ppp（默认）：各站**互不相干** —— 轨道和钟差是"
                            "固定输入，测站之间没有共享的待估参数。"
                            "实测各站结果与单独算**逐位相同**。"
                            "**不要说成「联合平差 / 网络解」，那没做到。**\n"
                            "  · pod：各站**共享卫星轨道参数** —— 那才是真正的"
                            "网络解，站越多轨道约束越强。而且它要的是**全球分布**"
                            "的站网，不是一个区域里的几个站。"
                        ),
                    },
                    "day": {
                        "type": "string",
                        "description": "日期，格式 YYYY-MM-DD，例如 2026-09-02",
                    },
                },
                "required": ["stations", "day"],
            },
            function=tool_plan,
        ),
        Tool(
            name="workspace_status",
            description=(
                "看看当前任务进行到哪一步了：计划做了没、下载了几个文件、"
                "配置生成了没、跑过几次、结果文件在哪。\n"
                "\n"
                "什么时候用：用户问'到哪了''还要多久'，"
                "或者你自己不确定上一步做完了没。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            function=tool_status,
        ),

        # ---------------------------------------------------------------
        # 下载
        # ---------------------------------------------------------------
        Tool(
            name="fetch_data",
            description=(
                "按已制定的计划下载数据。**需要联网，可能很久（几分钟到几十分钟）。**\n"
                "\n"
                "自动多源回退、断点续传、校验完整性、自动解压。\n"
                "\n"
                "⚠️ **必须先调用 plan_run。** 没有计划就直接下载会报错。\n"
                "\n"
                "什么时候用：用户确认了取数计划之后。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "proxy_mode": {
                        "type": "string",
                        "enum": ["auto", "manual", "none"],
                        "description": "同 probe_data_sources",
                    },
                    "with_tables": {
                        "type": "boolean",
                        "description": (
                            "只在用户要用「完整」档时传 true。"
                            "它会顺带把 10 个模型文件补齐（第一次约 17 MB，"
                            "**只下一次，以后一直用**，不随日期变）。"
                            "⚠️ 用户还没决定要用哪个档位时**不要**传 —— "
                            "先问清楚，别替他下 17 MB。"
                        ),
                    },
                },
                "required": [],
            },
            function=tool_fetch,
            wants_context=True,
            dangerous=True,     # 会下载几百 MB，值得让用户确认
        ),

        # ---------------------------------------------------------------
        # 配置
        # ---------------------------------------------------------------
        Tool(
            name="generate_config",
            description=(
                "生成 Ginan 的 YAML 配置。\n"
                "\n"
                "⚠️ **必须先调用 plan_run。**\n"
                "\n"
                "会告诉用户用了哪个档位、有什么代价 —— "
                "**那些代价必须一并转述**，不能只报好消息。\n"
                "\n"
                "什么时候用：数据下载完之后。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        # ⚠️ 只列**现在真的能用**的档位 —— 不是 list(PROFILES)。
                        #
                        #    **能选 = 一种承诺。** 所以不能选的不该列出来。
                        #    （这条规矩是从一个真实的坑留下的：这里原来列着
                        #      standard，描述里写着"需要额外下载约 200MB"，
                        #      而那个下载动作当时**根本不存在** ——
                        #      于是模型照着这句话对用户承诺了一次不会发生的下载。
                        #      2026-09-20 把下载做完了，它才重新出现在这个列表里。）
                        "enum": list(gconfig.AVAILABLE_PROFILES),
                        "description": (
                            "处理档位。\n"
                            "reduced=基础：不需要任何额外文件，跑得快。适合先试跑。\n"
                            "standard=完整：模型更精细（海潮负荷、大气潮、"
                            "二阶电离层、日月位置都算上）。"
                            "需要先下 **10 个模型文件，约 17 MB** —— "
                            "用 `fetch_data(with_tables=true)` 下，"
                            "**只下一次，以后一直用**。\n"
                            "⚠️ 模型文件没下全时 generate_config 会拒绝 —— "
                            "别改用基础档糊弄，先问用户愿不愿意下那 17 MB。"
                        ),
                    }
                },
                "required": [],
            },
            function=tool_config,
        ),
        Tool(
            name="validate_config",
            description=(
                "在真正跑之前检查配置，**约几秒**。\n"
                "\n"
                "⚠️ 重点：Ginan 的检查有个**已知的盲区** —— "
                "它会报告'配置没问题'，但实际可能有几项被它**自动改掉了**"
                "（Ginan 认为某些值不合理时会静默调整，只发 warning）。\n"
                "\n"
                "这个工具会把那些改动**也报出来**。"
                "如果结果里有 config_mutated=true，**必须告诉用户** —— "
                "他给的和实际跑的不是同一份配置。\n"
                "\n"
                "什么时候用：generate_config 之后、run_processing 之前。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            function=tool_validate,
            wants_context=True,
        ),

        # ---------------------------------------------------------------
        # 运行
        # ---------------------------------------------------------------
        Tool(
            name="run_processing",
            description=(
                "运行 Ginan 处理。**可能很久（几分钟到几十分钟）。**\n"
                "\n"
                "参数 max_epochs 用于**试跑**：只处理前 N 个历元。\n"
                "⚠️ 建议**第一次跑时先用这个试跑**（比如 60），"
                "确认没问题再跑完整时段 —— 完整跑要十几分钟，失败了要重来。\n"
                "\n"
                "失败时会返回每条问题的解释和'该怎么办'，直接转述给用户。\n"
                "\n"
                "什么时候用：配置检查通过之后。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "max_epochs": {
                        "type": "integer",
                        "description": "只处理前这么多个历元（试跑用）。不传就是全部。",
                    }
                },
                "required": [],
            },
            function=tool_run,
            wants_context=True,
            dangerous=True,
        ),

        # ---------------------------------------------------------------
        # 结果
        # ---------------------------------------------------------------
        Tool(
            name="summarize_results",
            description=(
                "解读处理结果，给出**人话结论**。\n"
                "\n"
                "返回里包含：定位偏差、收敛情况、有效历元数，"
                "以及一段结论和它需要注意的事项。\n"
                "\n"
                "⚠️ 结论后面的 caveats **必须一并告诉用户** —— "
                "那是结论的适用边界（比如'数据太短，还没收敛'），"
                "只说'精度 1.2 厘米'而不说边界会误导人。\n"
                "\n"
                "什么时候用：run_processing 成功之后。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            function=tool_summarize,
            max_result_chars=14_000,
        ),
        Tool(
            name="run_residual_diagnostics",
            description=(
                "看**观测质量**：为残差重跑一次（同一份配置，只把 trace 打开）。\n"
                "\n"
                "⚠️ 它会**再跑一遍**（一天约 2.5 分钟、留在盘上约 200 MB）——"
                "所以**先问用户**要不要跑。\n"
                "\n"
                "⚠️ 但它**不改变解算结果**：同一份配置，trace 级别高一点，"
                "`.POS` 逐字节相同（实测）。所以这一趟拿到的残差，"
                "就是用户正在看的那份解的残差。\n"
                "\n"
                "它回答的是位置的图回答不了的问题：**数据本身好不好** ——"
                "哪颗卫星、哪个频点在拉后腿，有没有整段变差。\n"
                "\n"
                "什么时候用：用户问「数据质量怎么样」「残差大不大」"
                "「哪颗星有问题」的时候。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "max_epochs": {
                        "type": "integer",
                        "description": "只处理前这么多个历元（想快点看的话）。",
                    }
                },
                "required": [],
            },
            function=tool_residuals,
            wants_context=True,
            dangerous=True,
        ),
        Tool(
            name="compare_orbits",
            description=(
                "把**两条轨道**比一比，回答「这条轨道准不准」。\n"
                "\n"
                "⚠️ 它**不是**一个处理工作流 —— 不下载、不处理观测、几秒钟出结果。"
                "Ginan 收到这两个文件就直接比、然后退出。\n"
                "\n"
                "参数：\n"
                "  · truth —— **参照**轨道（通常是同一时段的 IGS 精密 SP3）\n"
                "  · test  —— 被检验的那条。**省略就用本次跑出来的**轨道\n"
                "\n"
                "返回一句人话结论 + 几个数（卫星数、公共历元数、扣掉整体差异后的"
                "三维 RMS）。\n"
                "\n"
                "⚠️ 结论后面的 caveats **必须一并告诉用户** —— "
                "尤其是「这只是两条轨道的差异，不是精度」那一条。\n"
                "\n"
                "什么时候用：\n"
                "  · POD 跑完之后，想知道「我这条轨道准不准」\n"
                "  · 用户拿了一份 SP3，想和已知的比一比"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "truth": {
                        "type": "string",
                        "description": "参照轨道文件的路径（SP3）",
                    },
                    "test": {
                        "type": "string",
                        "description": (
                            "被检验的轨道文件路径。省略则用本次任务产出的轨道。"
                        ),
                    },
                },
                "required": ["truth"],
            },
            function=tool_compare,
            wants_context=True,
            max_result_chars=6_000,
        ),

        # ---------------------------------------------------------------
        # 收件箱
        # ---------------------------------------------------------------
        Tool(
            name="advise_from_inbox",
            description=(
                "看一眼收件箱里这一堆文件**能算什么**。只读，不动任何文件。\n"
                "\n"
                "它回答的就是那三句话：\n"
                "  ① 能算什么 —— 按测站和日期给出 (站, 天) 候选清单，"
                "直接可以喂给 plan_run\n"
                "  ② 还缺什么 —— 缺哪几类辅助文件（SP3/CLK/ERP/ATX 之类）\n"
                "  ③ 哪些用不了 —— 比如 CrinEX 编码的观测（Ginan 读不了），"
                "并指出同站同天有没有能用的替代品\n"
                "\n"
                "什么时候用：用户说'看看我的数据'、'准备数据'、"
                "'这些能算什么'，**或者刚扫完收件箱之后**。\n"
                "\n"
                "⚠️ 输出是**聚合过的**（按站，不是一个个文件）—— "
                "收件箱里有几百个文件时也能一次看完。"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            function=do_advise_from_inbox,
        ),
        Tool(
            name="scan_inbox",
            description=(
                "扫描收件箱：看看用户手动放进来的文件里，程序认得哪些。\n"
                "\n"
                "⚠️ 默认**只报告、不动文件**（dry_run=true）。\n"
                "用户应该先看到'认出了什么、准备搬到哪'，再决定要不要搬。\n"
                "确认无误后再用 dry_run=false 真正搬运。\n"
                "\n"
                "什么时候用：下载失败之后（用户可以自己下），"
                "或者用户说'我把文件放好了'。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "true=只看看（默认）；false=真的搬",
                    }
                },
                "required": [],
            },
            function=do_scan_inbox,
        ),
        Tool(
            name="inspect_rinex",
            description=(
                "检查一份 RINEX 观测文件**能不能用于精密定位**。秒回，不联网。\n"
                "\n"
                "会告诉用户：测站名、时间范围、时长、采样间隔、天线型号，"
                "以及各个卫星系统是否双频。\n"
                "\n"
                "⚠️ **用户想用自己的测站数据时，先用这个检查** —— "
                "单频数据做不了 PPP，而这是个静默的失败：\n"
                "程序能跑完、不报错、但结果是错的。\n"
                "\n"
                "什么时候用：\n"
                "  · 用户说'我有自己的数据''能测我这个站吗'\n"
                "  · 用户把文件放进收件箱之后\n"
                "  · 下载/处理失败，怀疑是数据本身的问题时"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "RINEX 文件的完整路径",
                    }
                },
                "required": ["path"],
            },
            function=do_inspect_rinex,
        ),
        Tool(
            name="list_antennas",
            description=(
                "从天线表（igs20.atx）里找天线型号。秒回，不联网。\n"
                "\n"
                "⚠️ 用户的天线型号**必须和表里逐字一致** —— "
                "大小写、空格、隔离罩代码都要对。"
                "而他手里的标签通常写着 `TRM59800.00`，"
                "表里却是 `TRM59800.00 NONE`。直接填会失败，"
                "报错还看不出是格式问题还是表里没有。\n"
                "\n"
                "所以**别让用户猜**：问他记得的型号（哪怕只有一部分，"
                "比如 `TRM59800` 或 `59800`），把这个工具的结果给他挑。\n"
                "\n"
                "什么时候用：\n"
                "  · inspect_rinex 说缺天线型号时\n"
                "  · 用户问'我的天线该填什么'\n"
                "  · 用户报了一个型号，要确认表里有没有"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "天线型号的关键词，可以只写一部分。留空则列出全部。",
                    }
                },
                "required": [],
            },
            function=do_list_antennas,
            max_result_chars=6_000,
        ),
        Tool(
            name="repair_rinex",
            description=(
                "补全 RINEX 文件头里缺失的字段（站点名、天线型号等），"
                "**写出一份新的**文件。\n"
                "\n"
                "⚠️ **绝不覆盖原文件** —— 用户的原始数据不可再生，"
                "我们只是补几个头部字段，改坏了得不偿失。\n"
                "\n"
                "站点名如果没传，会**自动从文件名推**。\n"
                "\n"
                "什么时候用：\n"
                "  · inspect_rinex 的 can_auto_fix 非空时\n"
                "  · 用户的数据是转换工具导出的（元数据常常是空的）\n"
                "\n"
                "⚠️ 天线型号推不出来，**必须先问用户**。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要修补的 RINEX 文件"},
                    "marker": {"type": "string", "description": "站点名（不传则从文件名推）"},
                    "antenna": {"type": "string", "description": "天线型号，格式 '型号名 隔离罩'，如 'TRM59800.00 NONE'"},
                    "receiver": {"type": "string", "description": "接收机型号（可选）"},
                },
                "required": ["path"],
            },
            function=do_repair_rinex,
            dangerous=True,     # 会写文件
        ),
        Tool(
            name="list_local_obs",
            description=(
                "列出**用户放进收件箱的**观测数据（顺便提一句程序缓存里还有多少）。\n"
                "\n"
                "什么时候用：\n"
                "  · 用户说'我已经把数据放进去了'但不确定程序认没认出来\n"
                "  · 想确认某个站的数据是否已经在本地\n"
                "  · 排查'为什么还要重新下载'"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            function=do_list_local_obs,
        ),
        Tool(
            name="get_data_dir",
            description=(
                "查看各个目录在哪。\n"
                "\n"
                "什么时候用：\n"
                "  · 用户问'文件存哪了''我想自己放文件'\n"
                "  · 需要让用户手动提供文件时\n"
                "  · 用户想清理磁盘、看看占了多少空间"
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            function=do_get_data_dir,
        ),

        Tool(
            name="ask_user",
            description=(
                "问用户一个问题，界面上会显示成**可点的按钮**，然后等他的回答。\n"
                "\n"
                "什么时候用：\n"
                "  · **要用户拍板**：下载几百 MB、跑十几分钟的完整解算、改他的文件\n"
                "  · **缺了只有他知道的信息**：天线型号、测站名、文件在哪\n"
                "  · **两条路都行、但他有偏好**：先试跑还是直接完整跑\n"
                "\n"
                "⚠️ 不要在正文里问一句就停下来等 —— 用户没法回答，\n"
                "   界面也不会给出可点的东西。**要问就用这个工具。**\n"
                "\n"
                "⚠️ 也别什么都问。默认值、下一步该干嘛、你自己能判断的，\n"
                "   直接做就行。**一次问清**，别拆成好几轮。\n"
                "\n"
                "⚠️ 推荐的那个选项**放第一个**，并在 label 里说清代价"
                "（比如'下载 75 MB，约 2 分钟'）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要问的话。说人话，别用术语。",
                    },
                    "options": {
                        "type": "array",
                        "description": "2~6 个选项，推荐项放第一个。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "按钮上的字，短一点（10 字以内最好）",
                                },
                                "detail": {
                                    "type": "string",
                                    "description": "可选。补一句代价或后果",
                                },
                            },
                            "required": ["label"],
                        },
                    },
                    "allow_text": {
                        "type": "boolean",
                        "description": (
                            "允不允许用户不打选项、自己写一段话。"
                            "问天线型号这类'选项列不全'的问题时设 true（默认就是 true）。"
                        ),
                    },
                },
                "required": ["question", "options"],
            },
            function=do_ask_user,
            # ⚠️ 需要 ctx —— 提问要经过那条跨线程的通道（见 core/ask.py）
            wants_context=True,
            # ⚠️ 不标 dangerous：它本身**不做事**，只是问一句。
            #    真正危险的是它后面的那个动作，而那个动作自己会再问一次。
            max_result_chars=2000,
        ),
    ]
