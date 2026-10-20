"""Ginan 领域层 —— 复现包：把「这次结果是怎么来的」写成一份文件。

============================ 为什么需要它 ============================
`fetch.py` 里早就写着这么一句：

    「详细的在复现包（repro/manifest.json）里。」

⚠️ **而这句话一直没有兑现**（见 `dev/00-项目全貌.md` 的 T17）——
   没有任何代码生成那个文件。**一句还没兑现的话，比不写更糟**：
   读到它的人会以为"程序已经替我记着了"，于是不去自己记。

============================ 它要回答的问题 ============================
一个做研究的用户，半年后（或者审稿人）会问：

    这个结果是怎么来的？换台机器还能跑出同样的东西吗？

所以清单里必须有**三样能对上的东西**：

    ① 用了什么配置（文件 + 哈希）
    ② 喂进去什么数据（每个文件的哈希 —— 下载的还记着**从哪个源**来的）
    ③ 什么软件跑的（Ginan 版本 —— 从日志里读，不是我猜的）

⚠️ 哈希是这里唯一"能证明"的东西。文件名一样不代表内容一样
   （同一个 `igs20.atx` 有 61 MB 的官方版，也有别人裁过的版本）。

============================ 分层 ============================
纯函数 + 显式参数，**不 import agent / core**（同 `posfile.py` 的做法）。
调用方（`ginan_tools.do_run_processing`）把已经拿在手里的东西传进来。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

# 清单格式的版本号。
#
# ⚠️ 有它才能"以后改字段时，老清单仍然读得懂"。
#    写到盘上的东西**一旦发出去就改不了了**（用户已经把它发给合作者了），
#    所以格式必须有版本，而不是"改了就改了"。
FORMAT_VERSION = 1

REPRO_DIR_NAME = "repro"
MANIFEST_NAME = "manifest.json"

# 超过这个大小的文件**不算哈希**，只记大小。
#
# ⚠️ 为什么要设上限：Ginan 会往运行目录里写 `.TRACE`，
#    实测见过 140 MB 一份的，文档里还记着 Windows 上能到 2.1 GB。
#    为了一份清单去读几百 MB 的日志，是拿"每次运行都多等几秒"换一个
#    几乎没人会核对的数字。
#
# ⚠️ 但**不能悄悄跳过** —— 跳过的要在清单里写明"没算"，
#    否则读的人会以为哈希是空的 = 文件不存在。
MAX_HASH_BYTES = 64 * 1024 * 1024


def sha256_of(path: Path, *, limit: int = MAX_HASH_BYTES) -> str | None:
    """算一个文件的 sha256。太大或读不到时返回 None（**不抛异常**）。

    ⚠️ 返回 None 而不是抛异常，和 `posfile.parse` / `RunResult.output`
       同一个原则：算不出哈希不该让整个流程崩掉。
    """
    try:
        if path.stat().st_size > limit:
            return None
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _file_record(path: Path, *, note: str = "") -> dict:
    """一个文件在清单里的样子：名字 + 大小 + 哈希。"""
    try:
        size = path.stat().st_size
    except OSError:
        return {"name": path.name, "missing": True, "note": note or "文件不在了"}
    digest = sha256_of(path)
    rec: dict = {"name": path.name, "bytes": size}
    if digest is None:
        rec["sha256"] = None
        rec["note"] = f"超过 {MAX_HASH_BYTES // (1024 * 1024)} MB，没算哈希"
    else:
        rec["sha256"] = digest
    if note:
        rec["note"] = note
    return rec


# =============================================================================
# 输入文件 —— 把「计划里的」和「下载报告里的」合起来
# =============================================================================


def collect_inputs(plan, fetched, data_dir: Path) -> list[dict]:
    """这次运行**用到**的输入文件清单。

    两个来源合起来，缺一不可：

        `plan`     —— 权威的**清单**（该有哪些文件、各是什么类别）
        `fetched`  —— 那些文件**实际是怎么来的**（哪个源、多大、什么哈希）

    ⚠️ 为什么不能只用 `fetched`：缓存命中的文件**不在**下载报告里
       （`skipped=True` 的也在，但用户手动放进数据目录的、上一轮就下好的，
       都可能压根没经过这次下载）。只用报告会漏掉它们 ——
       而漏掉的恰好是"最说不清来源"的那一批。

    ⚠️ 也不能只用 `plan`：它只有文件名，没有来源和哈希。

    `fetched` 可以是 None（用户手工准备的数据，从没下载过）。
    """
    if plan is None:
        return []

    # 报告按**落盘之后的真实文件名**建索引 —— 下载会把文件改名
    # （`real_name`，见 `fetch._decide_name`），所以两个名字都要能查到。
    by_name: dict[str, object] = {}
    for r in getattr(fetched, "results", []) or []:
        for key in (getattr(r, "filename", ""), getattr(r, "real_name", "")):
            if key:
                by_name[key] = r

    out: list[dict] = []
    for f in plan.files:
        name = f.filename
        rec = by_name.get(name)
        item: dict = {"name": name, "kind": f.kind}

        path = data_dir / name
        # ⚠️ `on_disk` 里也有 `name`（和上面同一个值），合并即可 ——
        #    第一版这里写了个 `pop("name")` 想去重，结果把**需要保留的**
        #    那一个也删了。清单里就没有文件名了。
        on_disk = _file_record(path)
        on_disk.pop("name", None)
        item.update(on_disk)

        if rec is not None:
            item["source"] = getattr(rec, "source", "") or "（缓存/未知）"
            item["from_cache"] = bool(getattr(rec, "skipped", False))
            # ⚠️ 报告里有哈希就**用报告的** —— 那是下载当场算的，
            #    比现在重算更接近"当时拿到的是什么"。
            if getattr(rec, "sha256", ""):
                item["sha256"] = rec.sha256
            if getattr(rec, "size", 0):
                item["bytes"] = rec.size
        else:
            # 没经过这次下载 —— 说清楚，别假装知道它从哪来
            item["source"] = "（不是这次下载的：缓存里已有，或用户自己放的）"
            item["from_cache"] = True

        out.append(item)

    return out


def collect_outputs(work_dir: Path, *, skip: Path | None = None) -> list[dict]:
    """运行目录里产生的**全部文件**（除了清单自己）。

    ⚠️ 口径是"这个目录里有什么就记什么"，不去区分"产物/日志/配置" ——
       因为要**把这次运行整个复现出来**，配置和日志和产物一样是必需品
       （配置是配方，日志是过程）。
       而且这样清单还能自校验：接收方可以拿它逐条核对。
    """
    out: list[dict] = []
    for p in sorted(work_dir.rglob("*")):
        if not p.is_file():
            continue
        if skip is not None and p == skip:
            continue
        if REPRO_DIR_NAME in p.relative_to(work_dir).parts:
            continue                    # 不把清单自己算进去（它会变）
        rec = _file_record(p)
        rec["path"] = str(p.relative_to(work_dir)).replace("\\", "/")
        out.append(rec)
    return out


# =============================================================================
# 拼清单
# =============================================================================


def build_manifest(
    *,
    workflow: str,
    workflow_label: str,
    stations: list[str],
    day: str,
    config_name: str,
    config_sha256: str | None,
    inputs: list[dict],
    run: dict,
    outputs: list[dict],
    app_version: str = "",
    corrections: dict | None = None,
) -> dict:
    """拼出清单的内容（**纯函数** —— 不碰磁盘，所以能直接测）。"""
    return {
        "format_version": FORMAT_VERSION,
        "created": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "task": {
            "workflow": workflow,
            "what_it_does": workflow_label,
            "stations": list(stations),
            "day": day,
        },
        "software": {
            "ginan": run.get("ginan_version") or "（日志里没读到）",
            "ginan_agent": app_version or "（未知）",
        },
        "config": {
            "name": config_name,
            "sha256": config_sha256,
            # ⚠️ 改正项（潮汐/电离层那些开关）属于"这次结果是怎么来的" ——
            #    同一个输入文件、同一份配置模板，**只差一个改正项开关**，
            #    解就能差几厘米（实测见 `corrections.py` 每一项的 effect）。
            #    不记下来的话，别人拿着这份清单也复现不出同一个结果，
            #    而且他会以为自己哪里做错了。
            "corrections": dict(corrections or {}),
        },
        "inputs": inputs,
        "run": run,
        "outputs": outputs,
        "how_to_reproduce": _how_to(config_name, run),
    }


def _how_to(config_name: str, run: dict) -> str:
    """一段人话：怎么用这些文件再跑一遍，以及**什么情况下结果会不一样**。

    ⚠️ 后半句（差异来源）是这份清单里最容易漏、而研究者最需要的东西。
       "同样的输入应该得到同样的结果"是个**近似**成立的假设，
       不写清楚边界，复现不出来的人只会怀疑自己哪里做错了。
    """
    return (
        f"① 用同一份配置（`{config_name}`，哈希见上）；\n"
        "② 确认 `inputs` 里每个文件的哈希与上面一致"
        "（它们应当在数据目录里，平铺放着）；\n"
        f"③ 用 Ginan {run.get('ginan_version') or '（版本见上）'} 跑："
        f"`pea --config {config_name}`。\n"
        "\n"
        "⚠️ 想**逐字节**重现，还要注意线程数：实测同一份配置和输入，"
        "用同样的线程设置跑两次，产物除时间戳外逐字节相同；"
        "但线程数不同会有差异（实测 1 核 vs 8 核：中位数差 3.2 厘米）。"
        "本程序**不替 Ginan 设线程数**，所以换了机器可能不一样。"
    )


# =============================================================================
# 写盘
# =============================================================================


def manifest_path(work_dir: Path) -> Path:
    return work_dir / REPRO_DIR_NAME / MANIFEST_NAME


def write_manifest(work_dir: Path, manifest: dict) -> Path:
    """写进 `<运行目录>/repro/manifest.json`，返回写好的路径。

    ⚠️ 一个任务可以跑多次（改参数重跑很常见）。这时**不能直接覆盖** ——
       上一次的结果和它的清单是一套，覆盖掉就再也说不清"当时跑的是什么"了。

    ⚠️ 也不能按序号命名（`manifest-2.json`）—— 用户要发给别人，
       而"最新那份"必须有个一眼能认出来的名字。

    ⇒ 所以：写之前把**老的那份按它自己的 run_id 改名**留档
      （`manifest-<run_id>.json`），`manifest.json` 永远指最近一次。
      run_id 从老清单里读 —— 不靠文件名猜。
    """
    target = manifest_path(work_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_file():
        old_id = _read_run_id(target)
        if old_id:
            backup = target.with_name(f"manifest-{old_id}.json")
            try:
                target.replace(backup)
            except OSError:
                pass

    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _read_run_id(path: Path) -> str:
    """从老清单里读 run_id。读不出来就返回空串（**不抛异常**）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    run = data.get("run")
    if isinstance(run, dict):
        return str(run.get("run_id") or "")
    return ""


def run_record(result, diagnosis) -> dict:
    """把一次运行记成清单里的样子。

    ⚠️ 这里**照抄 `RunResult` 和 `Diagnosis` 的字段**，不再自作主张地推导 ——
       两处各推一遍迟早会对不上（这是本仓库反复踩过的形状）。
    """
    return {
        "run_id": result.handle.run_id,
        "ok": bool(result.ok),
        "cancelled": bool(result.cancelled),
        "timed_out": bool(result.timed_out),
        "exit_code": int(result.exit_code),
        "seconds": round(float(result.duration), 1),
        "epochs": int(getattr(diagnosis, "epochs_processed", 0) or 0),
        "finished": bool(getattr(diagnosis, "finished", False)),
        "ginan_version": getattr(diagnosis, "version", "") or "",
        "headline": getattr(diagnosis, "headline", "") or "",
        "problems": [
            {"kind": d.kind, "severity": d.severity, "what": d.what}
            for d in getattr(diagnosis, "diagnostics", []) or []
        ],
    }
