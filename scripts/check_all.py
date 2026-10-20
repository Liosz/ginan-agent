"""跑一遍全部自检。

跑法：
    python scripts/check_all.py

============================ 这个脚本存在的理由 ============================
自检脚本多了之后，人就会开始漏跑 —— 改完代码只跑"我改的那部分"，
结果别处坏掉了没人知道。

一个总入口把这件事变成**一个命令**，漏跑的可能性就小很多。

============================ 全部零成本 ============================
所有自检**不联网、不调用任何 AI 模型、不产生任何费用**。

所以没有理由不跑 —— "跑一次要花钱"才会让人犹豫，
而犹豫的结果就是"这次先不跑了"。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# (脚本, 需要什么, 说明)
# ⚠️ 依赖不是"必须"时会跳过而不是失败 ——
#    比如没装 node 就不该让整个自检红掉，
#    因为前端渲染器不是核心功能的必要部分。
SUITES = [
    ("check_recursion.py", None, "自调用守卫 —— 『函数把自己调用了』那类事故"),
    ("check_registry.py", None, "工具注册表 —— 缺陷 #1 的回归守卫"),
    ("check_core.py", None, "核心层 —— 循环 / 会话 / 取消 / 压缩"),
    ("check_network.py", None, "网络层 —— 判断逻辑 / 代理 / 取数计划"),
    ("check_files.py", None, "文件识别与下载 —— 多源/续传/解压/校验"),
    ("check_paths.py", None, "路径解析 —— 必须按用户走，不能硬编码"),
    ("check_launcher.py", None, "启动器 —— 「双击就能跑」这条路的守卫"),
    ("check_server.py", None, "本地服务 —— 鉴权 / 事件广播 / 新任务"),
    ("check_credentials.py", None, "凭据 —— 重点是『不能弄丢用户其他的凭据』"),
    ("check_ui_text.py", None, "界面文案 —— 防止字面的 ** 露到界面上"),
    ("check_diagnostics.py", None, "诊断 —— 读懂 pea 的输出"),
    ("check_toolchain.py", None, "工具链 —— 从计划到结论的完整流程"),
    ("check_markdown.mjs", "node", "Markdown 渲染器 —— 含 XSS 防护"),
    ("check_clock.mjs", "node", "页头时钟 —— GPS 周必须和 catalog.py 算得一样"),
]


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# =============================================================================
# ★★★ 守卫：自检**不许碰用户真实的 `userdata/`**
# =============================================================================


def _userdata_snapshot() -> dict[str, tuple[str, int]]:
    """真实 `userdata/` 的指纹：文件记大小，目录记 mtime。

    ⚠️ **目录也要记** —— 这是这一条的关键。
       有的污染是"建一个文件再删掉"（比如可写性探针）：
       文件级快照前后一模一样，**只有目录的 mtime 变了**。
       mtime 是唯一看得见的痕迹。

    ⚠️ 只 stat，不读内容 —— `runs/` 里躺着几百 MB 的文件，
       读一遍要好几秒，而大小就够了（改写文件不会改父目录 mtime，
       删了又建会改）。
    """
    root = ROOT / "userdata"
    if not root.is_dir():
        return {}
    out: dict[str, tuple[str, int]] = {}
    for p in root.rglob("*"):
        try:
            rel = str(p.relative_to(root))
            if p.is_dir():
                out[rel] = ("d", p.stat().st_mtime_ns)
            elif p.is_file():
                out[rel] = ("f", p.stat().st_size)
        except OSError:
            continue                      # 正好被删掉了：那本身就是变化，交给下一轮
    return out


def _userdata_diff(
    before: dict[str, tuple[str, int]], after: dict[str, tuple[str, int]]
) -> list[str]:
    """哪些东西变了。返回人话列表（空 = 一个字节都没动）。"""
    out: list[str] = []
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k), after.get(k)
        if b is None:
            out.append(f"多出了 {k}")
        elif a is None:
            out.append(f"少了 {k}")
        elif b != a:
            out.append(f"{k} 变了（{b[0]}{b[1]} → {a[0]}{a[1]}）")
    return out


def check_registry_is_complete() -> list[str]:
    """⚠️⚠️ **登记表本身**要有一条守卫 —— 漏登记的失败方式是「绿色的假象」。

    ── 这个失败方式，项目自己写过 ──
    `check_recursion.py` 的注释里有一句：

        一个守卫最危险的失效方式不是"漏报"，而是**悄悄不再工作了** ——
        那时它输出的是"✓ 没问题"，和"代码真的没问题"长得一模一样。
        （跟 `check_all.py` 里**漏登记一个套件**……是完全同一种失败：
         **绿色的假象**。）

    ⚠️ 而那句话写下了，**守卫本身却没写**：`SUITES` 是一张手写的表，
    没有任何东西拦着它漂移。新加一个 `scripts/check_xxx.py` 忘了登记
    → 这个总入口照常打印「全部通过」—— **而那个套件一次都没跑过**。

    ⚠️ 今天这张表是齐的（实测），但"今天是齐的"不是保证，
    **只有守卫才是**。（这条判据本身就来自这个项目。）

    返回"没登记的文件名"列表。
    """
    here = {s[0] for s in SUITES}
    on_disk = {
        p.name
        for p in (ROOT / "scripts").glob("check_*")
        if p.name != "check_all.py" and p.suffix in (".py", ".mjs")
    }
    return sorted(on_disk - here)


def check_registry_entries_exist() -> list[str]:
    """反过来：登记了但磁盘上没有的，也要报出来。

    ⚠️ 那种情况更隐形 —— 它表现为"缺失"（`? check_xxx.py`），
    而汇总里"缺失"和"失败"是分开数的，容易被当成环境问题放过。
    """
    return [s[0] for s in SUITES if not (ROOT / "scripts" / s[0]).exists()]


def main() -> int:
    print("=" * 66)
    print("Ginan Agent —— 全部自检")
    print("=" * 66)
    print("不联网、不调用模型、不产生任何费用。\n")

    results: list[tuple[str, str, str]] = []

    # ⚠️⚠️ **先查登记表本身** —— 见 check_registry_is_complete 的说明。
    #    这一步必须在跑任何套件**之前**：漏登记的表现是"少跑了一个"，
    #    而那和"跑过了、没事"在输出上长得一模一样。
    unregistered = check_registry_is_complete()
    missing_file = check_registry_entries_exist()
    if unregistered or missing_file:
        print("=" * 66)
        print("⚠️⚠️ 自检登记表**本身**有问题 —— 下面的『全部通过』不算数")
        print("=" * 66)
        for n in unregistered:
            print(f"  ✗ scripts/{n} 存在，但**没登记**在 SUITES 里 —— 它从来没被跑过")
        for n in missing_file:
            print(f"  ✗ SUITES 里登记了 {n}，但磁盘上没有这个文件")
        print("  → 改 scripts/check_all.py 的 SUITES 表")
        print("=" * 66)
        print()
        return 1

    for script, requires, desc in SUITES:
        path = ROOT / "scripts" / script
        if not path.exists():
            results.append((script, "缺失", desc))
            continue

        # 依赖不满足 → 跳过（不是失败）
        if requires and not _has(requires):
            results.append((script, "跳过", f"{desc}（没装 {requires}）"))
            continue

        cmd = ["node", str(path)] if script.endswith(".mjs") else [sys.executable, str(path)]

        print(f"▸ {script}")
        print(f"  {desc}")

        # ★★★ 跑之前拍指纹，跑完核一遍 —— 见 _userdata_snapshot 的说明。
        #     ⚠️ 位置很关键：**必须在 subprocess.run 外面**，
        #        因为它量的是"这个套件把用户的磁盘怎么了"。
        before = _userdata_snapshot()
        try:
            proc = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
        except subprocess.TimeoutExpired:
            results.append((script, "超时", desc))
            print("  ⏱ 超时\n")
            continue

        touched = _userdata_diff(before, _userdata_snapshot())
        if touched:
            # ⚠️⚠️ 这一条**比它自己的红更严重**：套件把用户的磁盘动了。
            #     而且它**不表现为失败** —— 自检照样打「全部通过」，
            #     被改的是用户的东西。（收件箱被清空那次就是这么发生的。）
            results.append((script, "污染", desc))
            print(f"  ☠ 它动了真实的 userdata/：")
            for t in touched[:8]:
                print(f"      {t}")
            if len(touched) > 8:
                print(f"      ……还有 {len(touched) - 8} 处")
            print()
            continue

        output = (proc.stdout or "") + (proc.stderr or "")

        # ⚠️ 提取"全部通过 (40/40)"那一行，而不是**最后一行**。
        #    各个脚本的最后一行是分隔线 `====`，取最后一行只会显示一串等号
        #    —— 看起来像没输出，实际是提取规则写错了。
        summary = ""
        for ln in reversed(output.strip().splitlines()):
            s = ln.strip()
            if "通过" in s and ("(" in s or "失败" in s):
                summary = s
                break
        if not summary:
            # 兜底：拿最后一行非空、非分隔线的
            for ln in reversed(output.strip().splitlines()):
                s = ln.strip()
                if s and not set(s) <= set("=- "):
                    summary = s
                    break

        status = "通过" if proc.returncode == 0 else "失败"
        results.append((script, status, desc))

        mark = "✓" if proc.returncode == 0 else "✗"
        print(f"  {mark} {summary}\n")

        # ⚠️⚠️ **失败的把详情打出来，别吞掉。**
        #
        #    原来这里 `capture_output=True` 之后就只留了 summary 那一行 ——
        #    于是自检失败时用户看到的是「✗ 有失败：1 项未通过（通过 82/83）」，
        #    **看不到是哪一条**。他只能去单独重跑那一个脚本。
        #
        #    ⚠️ 而重跑**常常就绿了**（实测：这一条是**偶发**的，
        #       `check_all` 下约 1/8 复现，单独跑 `check_core` 20 次全绿）——
        #       于是"失败"变成一句无法追查的话，比不报还糟。
        #
        #    👉 失败时把带 ✗ 的行（连同前一行标题）打出来。
        #       不整段倒出来是因为有的自检有几百行，会把界面冲掉。
        if proc.returncode != 0:
            lines = output.splitlines()
            shown = False
            for i, ln in enumerate(lines):
                if "✗" in ln:
                    # 往上找最近的一行"分节标题"（`---` 分隔线之后那行）
                    for j in range(i - 1, max(-1, i - 4), -1):
                        if lines[j].strip() and not set(lines[j].strip()) <= set("=- "):
                            print(f"      {lines[j].strip()}")
                            break
                    print(f"      {ln.strip()}")
                    shown = True
            if not shown and lines:
                print(f"      （没有 ✗ 行；最后 5 行）")
                for ln in lines[-5:]:
                    print(f"      {ln}")

    # ---- 汇总 ----
    print("=" * 66)
    failed = [r for r in results if r[1] == "失败"]
    dirty = [r for r in results if r[1] == "污染"]
    skipped = [r for r in results if r[1] == "跳过"]

    for script, status, desc in results:
        mark = {"通过": "✓", "失败": "✗", "跳过": "○", "缺失": "?",
                "超时": "⏱", "污染": "☠"}[status]
        print(f"  {mark} {script:22} {status}")

    print("=" * 66)
    if dirty:
        print(f"⚠️⚠️ 有 {len(dirty)} 个自检**动了用户真实的 userdata/**（见上面 ☠ 那几行）")
        for script, _, _ in dirty:
            print(f"     {script} —— 它里面那段跑在真实路径上的代码要套 `sandbox()`")
    if failed:
        print(f"有 {len(failed)} 个自检失败。")
        for script, _, _ in failed:
            print(f"  重新单独跑：python scripts/{script}")
    elif not dirty:
        # ⚠️ 只有**又没失败、又没污染**才敢说这句话。
        #    "全部通过"曾经是个只看得见"校验红没红"的结论 ——
        #    而收件箱被清空那次，它照样是绿的。
        print("全部通过。")
    if skipped:
        print(f"（跳过 {len(skipped)} 个 —— 缺少依赖，不是失败）")
    print("=" * 66)

    return 1 if (failed or dirty) else 0


if __name__ == "__main__":
    raise SystemExit(main())
