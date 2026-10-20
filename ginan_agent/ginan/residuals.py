"""Ginan 领域层 —— 残差诊断的**落盘约定**（目录名 / 文件名 / 读回）。

============================ 为什么单独一个模块 ============================
"残差诊断放在哪、叫什么"这件事有**三个**使用者，而它们必须给同一个答案：

    · 工具（`do_run_residual_diagnostics`）—— 写
    · 服务端（`/api/residuals` / `/api/residual_figures`）—— 读
    · 界面（结果面板里那个按钮）—— 判断"有没有"

⚠️ 各写一份的后果很具体：工具写到 `residuals/`、服务端去 `traces/` 找 ——
   界面上那个按钮**永远不出现**，而且没有任何地方会报错。

============================ 产物长什么样 ============================
    <work_dir>/
        residuals/
            <CONFIG>.yaml              这次重跑用的配置（trace 打开的）
            NET_<CONFIG>.TRACE         大文件（一天约 300 MB）—— **不自动删**
            NET_<CONFIG>_smoothed.TRACE
            residual_summary.json      派生的小文件（几百 KB）：统计 + 抽过样的点

⚠️ 大文件**不删**：它是"这次残差是怎么来的"的唯一证据，删不删由用户定
   （同 `repro` 的规矩）。派生文件小到可以随手发出去，所以图不依赖它。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import tracefile as gtracefile

DIR_NAME = "residuals"
BUNDLE_NAME = gtracefile.BUNDLE_NAME


def dir_of(work_dir: Path) -> Path:
    return Path(work_dir) / DIR_NAME


def bundle_path(work_dir: Path) -> Path:
    return dir_of(work_dir) / BUNDLE_NAME


def load(work_dir: Path) -> dict | None:
    """读派生文件。没有就返回 None（**不抛** —— 没有残差不是错误）。"""
    p = bundle_path(work_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def summary_for_ui(work_dir: Path) -> dict | None:
    """给结果面板用的那一小块（**不带抽样的点** —— 那有几百 KB）。"""
    data = load(work_dir)
    if not data:
        return None
    return {
        "file": data.get("file"),
        "count": data.get("count"),
        "verdict": data.get("verdict") or {},
        "residuals": {
            k: {
                "label": (v or {}).get("label", k),
                "n": ((v or {}).get("postfit") or {}).get("n", 0),
                "normalized_std": (((v or {}).get("normalized") or {})
                                   .get("std")),
                "normalized_available": (v or {}).get(
                    "normalized_available", False),
                "over_3sigma_pct": (v or {}).get("frac_over_3sigma"),
            }
            for k, v in ((data.get("summary") or {}).get("kinds") or {}).items()
        },
    }


def load_for_figures(work_dir: Path):
    """画图用 —— 返回 `(ResidFile, summary, verdict)`。没有就返回 None。"""
    data = load(work_dir)
    if not data or not data.get("points"):
        return None
    return gtracefile.unbundle(data)
