"""自检：界面文案。

跑法：python scripts/check_ui_text.py

============================ 为什么需要这个 ============================
有一种 bug 会**反复出现**，因为它的成因很容易被忽略：

    界面上出现了字面的 `**` 或 `|` 或 `` ` ``。

成因是**两种文本混用了**：

    模型输出的正文  → 渲染成 HTML（Markdown）
    我们写给用户的话 → 直接当纯文本塞进 textContent

而人在写"给用户的话"时，会**不自觉地用 Markdown 语法** ——
因为旁边的代码注释里全是 `**加粗**`，写着写着就带上了。

⚠️ 这个坑踩过两次：
    ① 模型输出的 Markdown 没渲染（用户看到满屏 `**` 和 `|`）
    ② 环境检查的「建议」里写了 `**自动帮您设置**`（用户看到字面星号）

第一次修完之后，第二次照样发生 —— 说明**光改代码不够，得有东西拦着**。

============================ 规则 ============================
    UI 文案（label / detail / advice）  → **纯文本**
    模型输出                            → Markdown

两者是不同性质的东西，不该用同一套格式。
UI 文案是结构化的固定文案，要换行就 `\n`，要强调就换个说法 ——
不需要 Markdown。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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


# =============================================================================
# ① 环境检查的文案
# =============================================================================


def test_frontend_js_parses() -> None:
    section("⓪ ★★ 前端 JS 必须能**解析** —— 一个语法错会让整页脚本死掉")

    # ⚠️⚠️ 2026-10-20 的事故：`app.js` 里有一处字符串被写成了**真换行**
    #     （本该是 `\n` 转义）→ 整个脚本 SyntaxError → 页面上
    #     **点设置、点新任务都没反应**，网络面板停在 HTML 里的静态文字
    #     「探测中…」，看起来像"卡在数据源检测"。
    #
    #     而当时全套自检**全绿**：这个套件把 app.js 当**文本**读、
    #     用正则找文案，**从不解析它** —— 语法错它一个字都看不出来。
    #
    #     > 一个从来没被解析过的脚本，等于没有守卫。
    #
    #     👉 `node --check` 只解析、不执行：不联网、不花钱、秒级。
    #        没装 node 时跳过并**说明**（和 check_all 对 .mjs 的处理一致）。
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        check(True, "（跳过：这台机器没有 node，无法解析 JS）")
        return

    js = sorted((ROOT / "ginan_agent" / "frontend").glob("*.js"))
    check(bool(js), f"找到了前端脚本：{[p.name for p in js]}")
    bad: list[str] = []
    for p in js:
        r = subprocess.run(
            [node, "--check", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            bad.append(f"{p.name}: {err[0] if err else '解析失败'}")
    check(not bad, "★★ 前端 JS 全部能解析" + ("" if not bad else f"：{bad}"))


def test_environment_copy() -> None:
    section("① 环境检查的 label / detail / advice 是纯文本")

    from ginan_agent.ginan import environment
    from tests.sandbox import sandbox

    # ⚠️⚠️ **套沙箱** —— `check_environment()` 里有一项是"数据目录能不能写"：
    #
    #        probe = paths.data_dir() / ".writable-probe"
    #        probe.write_text(""); probe.unlink()
    #
    #    不套的话，它会在**用户真实的 `data/`** 里建一个文件再删掉。
    #    ⚠️ 而它删干净了 —— 所以**肉眼看不见**：目录里一个多余的文件都没有，
    #       只有目录的 mtime 变了。（实测：14 个套件里只有这一个会碰 data/。）
    #
    #    > 这一节本来只关心"文案是不是纯文本"，却顺手动了用户的磁盘。
    #    > **一个测试跑到哪，就该在哪止步。**
    with sandbox():
        report = environment.check_environment()
    bad: list[str] = []

    for item in report.items:
        for field in ("label", "detail", "advice"):
            text = getattr(item, field, "") or ""
            for marker in ("**", "__", "`"):
                if marker in text:
                    bad.append(f"{item.key}.{field} 里有 {marker!r}：{text[:50]}")

    if bad:
        for b in bad:
            print(f"      {b}")
    check(not bad, f"检查了 {len(report.items)} 个条目，没有 Markdown 记号")


# =============================================================================
# ② 数据源探测的文案
# =============================================================================


def test_probe_copy() -> None:
    section("② 数据源探测的文案是纯文本")

    from ginan_agent.ginan import probe

    bad: list[str] = []
    # 不联网 —— 直接构造几个典型对象看 advice
    samples = [
        probe.SourceProbe("x", "测试", "精密产品", False, 100, "超时", False),
        probe.SourceProbe("y", "测试", "精密产品", False, 100, "需要登录", True),
        probe.SourceProbe("z", "测试", "模型文件", False, 100, "超时", False),
    ]
    for s in samples:
        text = s.advice or ""
        for marker in ("**", "`"):
            if marker in text:
                bad.append(f"{s.purpose}/{s.requires_auth} 的 advice 里有 {marker!r}")

    if bad:
        for b in bad:
            print(f"      {b}")
    check(not bad, "三种状态的 advice 都是纯文本")


# =============================================================================
# ③ 收件箱的文案
# =============================================================================


def test_inbox_copy() -> None:
    section("③ 收件箱的文案是纯文本")

    from ginan_agent.ginan import inbox

    # 直接看 Move.summary 的输出
    m = inbox.Move(src=Path("/tmp/a.rnx"), dst=Path("/tmp/b.rnx"),
                   kind="obs", renamed=True)
    check("**" not in m.summary and "`" not in m.summary,
          f"搬运记录是纯文本：{m.summary}")

    r = inbox.ClaimResult(scanned=0)
    check("**" not in r.summary, f"认领摘要是纯文本：{r.summary}")


# =============================================================================
# ④ 结果结论的文案
# =============================================================================


def _pos_case(n: int, distinct: int, solution: str, *, gappy: bool,
              step: float = 30.0):
    """造一份合成的 .POS 数据 —— 用来把 `verdict()` 的每条分支都打到。

    ⚠️ 为什么是**造**而不是拿真文件：真文件在 `ginan-testdata-*/` 里，
       那**不进软件包**，而这个自检是软件包的一部分。
       造出来的数据只要**能打到分支**就够了。

    `step` 是采样间隔（秒）—— 本程序里有 1 Hz 的工作流（`rtk_example`），
    而结论里要把"第几个历元"换算成时间，那一步**必须用真实间隔**。
    """
    import datetime as dt

    from ginan_agent.ginan import posfile

    t0 = dt.datetime(2026, 9, 2, 0, 0, 0)
    epochs = []
    for i in range(n):
        # 只有前 `distinct` 个位置是变的，后面全部重复 →
        # 用来打到"被压成常数"那条（`repeatability_is_meaningful()`）。
        k = min(i, max(0, distinct - 1))
        t = t0 + dt.timedelta(seconds=step * i)
        if gappy and i >= n // 2:
            t += dt.timedelta(minutes=45)   # 中间挖一个大洞 → 解算成功率掉下来
        epochs.append(posfile.PosEpoch(
            time=t, x=0.0, y=0.0, z=0.0, lat=0.0, lon=0.0, height=0.0,
            dn=0.01 * k, de=0.01 * k, du=0.02 * k,
            sn=0.005, se=0.005, su=0.01,
        ))
    return posfile.PosFile(
        station="TEST",
        reference_xyz=(6_000_000.0, 2_000_000.0, -1_000_000.0),
        epochs=epochs,
        solution=solution,
    )


def test_posfile_copy() -> None:
    section("④ 定位结论的文案是纯文本 —— 扫**输入空间**，不是挑一个样例")

    # ⚠️⚠️ 这条守卫原来**只造了一个 `PosFile()`（空文件）**。
    #
    #    而 `verdict()` 有好几条互斥的分支，空文件走的是**唯一干净的那条**
    #    （"没有解算结果"）。于是另外两条分支里的 Markdown 记号
    #    一路露到了用户眼前 —— 实测用户读到的是：
    #
    #        这份是**平滑解**（文件名带 `_smoothed`）。
    #        上面那个「与文件里的参考位置相差」，**不是精度** ——
    #
    #    👉 **守一条分支的守卫，绿起来和守全部分支的一模一样**，
    #       而且它比没有守卫更坏一点：它会让人以为这里已经有人看过了。
    #
    # ⇒ 所以不再"挑样例"，改成把 `verdict()` 分岔所依赖的维度**叉乘一遍**。
    #    （叉乘**不能保证**打到全部分支 —— 但比一个点好得多，
    #      下面还会断言"每个已知的分支条件都被打到了"。）
    from ginan_agent.ginan import posfile

    cases = []
    for n in (0, 1, 3, 4, 5, 30, 60, 300):
        for distinct in (1, 2, 3, 10):
            for solution in ("forward", "smoothed", "unknown"):
                for gappy in (False, True):
                    cases.append(_pos_case(n, distinct, solution, gappy=gappy))

    bad: list[str] = []
    for pf in cases:
        v = posfile.verdict(pf)
        for t in [v.headline] + v.details + v.caveats:
            if "**" in t or "`" in t:
                bad.append(t)

    for b in bad[:6]:
        print(f"      {b[:70]}")
    if len(bad) > 6:
        print(f"      … 还有 {len(bad) - 6} 条")
    check(not bad, f"★ {len(cases)} 种输入下，结论文案里都没有 Markdown 记号")

    # ⚠️ 上面那句"没有 Markdown"只有在**矩阵真的打到了各条分支**时才有意义。
    #    所以这里再把每个**已知的**分支条件显式断言一次 ——
    #    哪天有人改了某个分岔条件、导致样例不再落进那条分支，这里会红，
    #    而不是"静悄悄地少守了一条"。
    empty = [pf for pf in cases if pf.count == 0]
    constant = [pf for pf in cases
                if pf.count >= 4 and not pf.repeatability_is_meaningful()]
    gradable = [pf for pf in cases
                if pf.count >= 4 and pf.repeatability_is_meaningful()]
    short = [pf for pf in cases if 0 < pf.count and pf.duration_minutes() < 30]
    long_ = [pf for pf in cases if pf.duration_minutes() >= 30]
    check(bool(empty), "矩阵里有『一个历元都没有』的样例")
    check(bool(constant), "★ 矩阵里有『位置被压成常数』的样例（平滑解那条分支）")
    check(bool(gradable), "★ 矩阵里有能正常分级的样例")
    check(bool(short), "矩阵里有『时长不足 30 分钟』的样例")
    check(bool(long_), "矩阵里有『时长足够』的样例")

    # ⚠️ 还有一条只在这里守得住：**太短的解算不该被判好坏**。
    #    14 分钟的解做到米级是**必然**的，把它报成「质量 poor」
    #    是在怪解算 —— 而事实是"这些数据根本不够下判断"，
    #    和"平滑解那个指标没定义"是同一类问题（所以待遇也该一样）。
    graded_too_early = [
        (pf.count, round(pf.duration_minutes(), 1), posfile.verdict(pf).quality)
        for pf in cases
        if 0 < pf.duration_minutes() < 30
        and posfile.verdict(pf).quality in ("good", "fair", "poor")
    ]
    check(
        not graded_too_early,
        f"★ 时长不足 30 分钟的解**不判好坏**（被判了的有 "
        f"{len(graded_too_early)} 个，例如 {graded_too_early[:3]}）",
    )

    # ⚠️⚠️ 下面两条守的是"**说的必须是量到的事**"。
    #
    #    `converged_from()` 返回 -1 有**两种完全不同的原因**，
    #    而结论原来只描述了一种 —— 于是另一种成了假话：
    #
    #        历元数 < CONVERGE_EPOCHS  →  **根本没去判断**
    #        实测：`ALIC_g1_o4.POS`（8 个历元）被告知
    #              "误差一直在 10 厘米以上波动" —— 我们压根没看过。
    few = _pos_case(8, 8, "forward", gappy=False)
    # ⚠️ 位置一直在变 → 找不到"连续 20 个历元都稳定"的窗口。
    #    （第一版这里写的是 distinct=2，那份**收敛了** —— 于是"对照"那条
    #     测的是空气。造样例时要想清楚它到底会不会落进目标分支。）
    many = _pos_case(60, 60, "forward", gappy=False)
    t_few = " ".join(posfile.verdict(few).details + posfile.verdict(few).caveats)
    t_many = " ".join(posfile.verdict(many).details + posfile.verdict(many).caveats)
    check(
        "还不够判断" in t_few,
        "★ 历元不够时说的是「还不够判断有没有收敛」（不是「没找到」）",
    )
    check(
        "没找到连续" not in t_few,
        "★★ 而且**不许**说「没找到连续 N 个历元稳定」—— 那是没量到的事",
    )
    check("没找到连续" in t_many, "对照：真的找过了才说「没找到连续」")

    # ⚠️ 第二条：**收敛时间必须按真实采样间隔换算**。
    #    写死 30 秒的话，1 Hz 的数据会被报成 30 倍的时间。
    def conv_text(pf):
        return " ".join(
            d for d in posfile.verdict(pf).details if "开始稳定" in d
        )

    slow = _pos_case(300, 10, "forward", gappy=False, step=30.0)   # 30 秒采样
    fast = _pos_case(300, 10, "forward", gappy=False, step=1.0)    # 1 Hz
    cs, cf = conv_text(slow), conv_text(fast)
    check(bool(cs) and bool(cf), "两个样例都判出了收敛（否则这条测的是空气）")
    check("分钟" in cs, f"30 秒采样时报的是分钟（{cs[:34]}）")
    check("秒" in cf and "分钟" not in cf,
          f"★ 1 Hz 采样时报的是秒 —— 写死 30 秒的话这里会报成 30 倍（{cf[:34]}）")


# =============================================================================
# ⑤ 布局：滚动的容器必须能缩
# =============================================================================


def test_scroll_containers() -> None:
    section("⑤ 布局：写了 overflow 滚动的格子，必须有 min-height: 0")

    # ⚠️ 这条守着一个 CSS 里**最经典、最反直觉**的坑。
    #
    #    现象：聊天内容一长，右边栏跟着一起上下滚。
    #    而它们的 CSS 看起来完全独立 —— 各自都写了 overflow-y: auto。
    #
    #    原因：网格/弹性子项的 `min-height` 默认是 **auto 而不是 0**。
    #    `min-height: auto` 的意思是"我不肯缩到比内容还小"。
    #    所以内容一长，那一格就被撑破 → 整个页面开始滚 → 别的栏跟着走。
    #
    #    **它自己的 overflow-y: auto 根本没机会生效** ——
    #    因为它压根没被限制在"格子那么高"里。
    #
    #    解法：显式写 min-height: 0。
    #
    # ⚠️ 为什么值得自动化检查？
    #    因为它**不会报错**，只是长得不对。而且改别的地方时很容易
    #    顺手把 min-height: 0 删掉（我自己就差点这么干）。
    import re

    css_path = ROOT / "ginan_agent" / "frontend" / "styles.css"
    css = re.sub(r"/\*.*?\*/", "", css_path.read_text(encoding="utf-8"), flags=re.S)

    # 收集每个选择器的全部声明（同名规则要合并 —— 我们确实分了两处写）
    rules: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"^([.#][\w:()\- ]+)\s*\{([^}]*)\}", css, re.M):
        sel = m.group(1).strip()
        for decl in m.group(2).split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                rules.setdefault(sel, {})[k.strip()] = v.strip()

    # 网格/弹性的直接子项才需要 min-height: 0
    GRID_CHILDREN = [".sidebar", ".main"]

    for sel in GRID_CHILDREN:
        d = rules.get(sel, {})
        check(bool(d), f"{sel} 有样式定义")
        check(
            d.get("min-height") == "0",
            f"★ {sel} 有 min-height: 0（否则内容一长就会撑破布局）",
        )
        check(
            d.get("overflow") in ("hidden", "auto", "scroll")
            or d.get("overflow-y") in ("auto", "scroll", "hidden"),
            f"{sel} 限制了自己的溢出（overflow 或 overflow-y）",
        )

    # 真正在滚的那个容器
    msgs = rules.get(".messages", {})
    check(msgs.get("overflow-y") == "auto", "聊天区自己滚（overflow-y: auto）")
    check(
        msgs.get("overscroll-behavior") == "contain",
        "滚到头不把滚动传给外面",
    )

    # 页面本身不该滚 —— 页面一滚，所有栏都会跟着走
    app = rules.get(".app", {})
    check(app.get("overflow") == "hidden", "★ 整页不滚（overflow: hidden）")
    check(app.get("height") == "100vh", "整页高度锁定在视口")

    # 侧栏也要能独立滚
    side = rules.get(".sidebar", {})
    check(side.get("overflow-y") == "auto", "侧栏自己能滚")
    check(
        side.get("overscroll-behavior") == "contain",
        "侧栏滚到头不传出去",
    )


# =============================================================================
# ⑥ 前端确实**只**把 Markdown 用在模型输出上
# =============================================================================


def test_frontend_boundary() -> None:
    section("⑥ 前端：Markdown 渲染只用于模型输出")

    import re

    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    # ⚠️ 要检查的是"渲染出来的内容**进了哪里**"，不是"调用了几次"。
    #
    #    一开始写的是 `n == 1`（只能调一次）——
    #    太死了：同一个气泡有节流渲染和收尾渲染两处调用，
    #    两处都是对的，但测试会红。
    #
    #    **检查意图，不要检查形式。** 意图是"Markdown 只用在模型输出上"。
    targets = re.findall(r"(\S+)\.innerHTML\s*=\s*window\.renderMarkdown\(", js)

    check(bool(targets), f"确实用了渲染器（{len(targets)} 处）")

    # ⚠️ 名字**变过**，但**意图没变**：Markdown 只能进"模型输出气泡"。
    #
    #    原来是 `turn.bubble`（一轮一个气泡）。
    #    现在改成"一段一个气泡"（见 app.js 里 sealBubble 的说明），
    #    于是目标变成了 `turn.seg.el` 或 `seg.el`。
    #
    #    ⚠️ 所以这里**是改判据，不是删判据** ——
    #       这个检查挡的是"把 Markdown 渲染结果塞进工具卡片/按钮/提示里"
    #       （那样用户会看到字面的 `**` 和竖线）。
    #       把判据删掉换成 `check(True, ...)` 就等于把守卫拆了。
    #
    #    判据用**白名单**而不是"找规律"：气泡就这么几个名字，
    #    而且它们变了要有人知道 —— 加名字这件事本身就该被看见。
    BUBBLE_TARGETS = {
        "turn.bubble", "turn.seg.el", "seg.el",
        # 画历史时的那个气泡（见 renderHistory）。名字要**说得出它是什么** ——
        # 所以它叫 histBubble 而不是 bubble：后者太泛，将来某个工具卡片
        # 也叫 bubble 的话，这条守卫就分不出来了。
        "histBubble",
    }

    bad = [t for t in targets if t not in BUBBLE_TARGETS]
    if bad:
        for b in bad:
            print(f"      ⚠️ 渲染结果被塞进了 {b} —— 那不该是模型输出")
    check(
        not bad,
        f"渲染结果只进模型输出气泡（{len(targets)} 处调用，目标是 {set(targets)}）",
    )

    # ⚠️ 界面文案必须用 textContent（安全赋值），不能走 innerHTML ——
    #    那不仅会渲染 Markdown，还会**执行 HTML**（XSS）。
    for name in ("adv.textContent", "detail.textContent", "name.textContent"):
        check(name in js, f"界面文案用安全赋值（{name}）")

    # 界面文案应该用安全赋值（textContent），不是 innerHTML +
    # 不过 check-dot 那些是固定字符串拼的，允许
    check(
        "adv.textContent" in js or "adv.innerHTML" not in js,
        "环境检查的建议用的是安全赋值",
    )


def test_message_ordering() -> None:
    section("⑦ ★ 消息顺序：工具卡片必须夹在两段话中间（不是全堆在下面）")

    # ⚠️⚠️ 用户的原话：
    #     "回我话总在上边的框里接，下边出来一个查看数据目录的框…
    #      要按顺序，后说的放后边，不光是文字对话，查看数据目录的框这种也是"
    #
    #     根因：一轮只建**一个**气泡并一直复用它，而工具卡片是
    #     追加到末尾的 → 所有话挤在第一个气泡里，卡片全堆在下面。
    #
    # ⚠️ 这个检查是**结构性的，不是行为性的** —— 它看源码里有没有那几条
    #    关键写法，而不是真的跑一遍 DOM。
    #
    #    为什么不做行为测试：`app.js` 是个 IIFE，一加载就连 SSE、
    #    拉环境、拉网络，要跑它得先搭一整套 DOM + fetch + EventSource 的桩。
    #    那个成本现在还付不起。
    #
    #    ⚠️ 所以它的**强度有限**：有人把逻辑改错但保留这些写法时，
    #       它不会红。**真正的行为测试是欠着的**，别把它当成已经测过了。
    #       它挡的是"有人把修复整个回退掉"这一种情况 —— 而那是概率最大的那种。
    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    check(
        "function sealBubble()" in js,
        "★ 有 sealBubble() —— 把当前这段文字封口",
    )
    check(
        "sealBubble();" in js.split("case 'tool_start'")[1].split("break;")[0],
        "★ tool_start 时**封口**再建卡片（卡片才会落在文字下面）",
    )
    check(
        "if (turn && turn.seg) return turn.seg.el;" in js,
        "★ 有'当前段'的概念（不是一轮一个气泡）",
    )
    # ⚠️ 节流状态必须挂在**段**上。挂在 turn 上的话，第二段会把
    #    第一段的文字重渲染一遍（因为 raw 是共享的）。
    check(
        "turn.seg = { el: bubble, wrap, raw: '', timer: null }" in js,
        "★ 节流状态（raw / timer）挂在**段**上，不是挂在 turn 上",
    )
    check(
        "turn.raw" not in js,
        "★ 没有残留的 turn.raw（那个共享状态正是乱序的成因）",
    )


def test_every_panel_loader_is_wired_to_startup() -> None:
    section("⑱ ★★ 每个面板的加载函数都必须在**启动序列**里被调用")

    # ⚠️⚠️ 守的是一个**自检和接口测试都抓不到**的 bug（2026-09-29 踩的）：
    #
    #    `loadResults()` 一开始只加进了「新任务」的重置路径
    #    （那里有 4 空格缩进的三行 `loadEnvironment/loadNetwork/loadInbox`），
    #    而**启动序列那份**（2 空格缩进）没加。
    #
    #    后果：刷新页面之后，结果面板**一直是空的**，
    #    直到用户点一次「新任务」才会出现 —— 而"点新任务会清空一切"
    #    正是用户最不想要的副作用。
    #
    #    ⚠️ 为什么别的检查都看不见它：
    #       · 接口测试：`/api/results` 返回的数据**完全正确**
    #       · CSS 守卫：类名都有样式
    #       · 函数存在、语法正确、没有孤儿类名
    #       全都绿。**唯一看见它的是截图** —— 面板停在静态占位上。
    #
    #    ⇒ 所以判据是：**定义了 `async function loadXxx()`，就得在
    #      `connect();` 之后的那段里被调用一次。**
    #      将来加第五个面板时，这条会拦住"忘了接线"。
    import re as _re

    # ⚠️ **侧栏面板**的加载函数 —— 它们必须在启动序列里跑。
    #    ⚠️ 不在这里的那些是**对话框**的加载函数（打开时才调）：
    #       `loadCredentials`（设置对话框里的 CDDIS 账号那一栏）。
    #       把它们一起要求"启动时调用"是错的 —— 那会白跑一次网络请求。
    PANEL_LOADERS = ("loadEnvironment", "loadNetwork", "loadResults", "loadInbox")
    # ⚠️ 对话框的 loader **不进启动序列** —— 它们是"打开那个框时才拉"。
    #    `loadCorrections` 属于这一类：改正项列表在设置面板里，
    #    启动时不需要它（而且它要读磁盘上那些模型文件在不在）。
    DIALOG_LOADERS = ("loadCredentials", "loadCorrections")

    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")
    defined = set(_re.findall(r"async function (load[A-Za-z]+)\(", js))
    check(bool(defined), f"找得到加载函数（{sorted(defined)}）")

    # ⚠️ 新加一个 `loadXxx` 时**必须显式归类**：
    #    要么进 PANEL_LOADERS（= 启动时就要跑），要么进 DIALOG_LOADERS。
    #    不归类的话这里会红 —— 逼着做决定，而不是"默认它不用接"。
    unclassified = sorted(defined - set(PANEL_LOADERS) - set(DIALOG_LOADERS))
    check(
        not unclassified,
        f"★ 每个加载函数都被显式归过类（没归的：{unclassified}）"
        " —— 面板的进 PANEL_LOADERS，对话框的进 DIALOG_LOADERS",
    )

    tail = js.split("connect();", 1)[-1] if "connect();" in js else ""
    check(bool(tail), "找得到启动序列（`connect();` 之后那段）")

    # ⚠️⚠️ 判据必须是**一条调用语句**，不能是"字符串出现过"。
    #
    #    第一版查的是 `f"{n}()" in tail` —— 而 tail 里有**我自己写的注释**
    #    （"踩过：`loadResults()` 一开始只加进了…"），于是：
    #
    #        把 `loadResults();` 从启动序列删掉 → 守卫**照样全绿**
    #
    #    一条"因为注释里提过它所以通过"的守卫，正是本仓库警告过三次的
    #    「守卫被自己的文档绊倒」。是**变异测试**把它逼出来的：
    #    不变红的守卫等于没有。
    #
    #    ⇒ 改成行锚定：整行（允许缩进）就是一次调用。
    called = set(_re.findall(r"^[ \t]*(load[A-Za-z]+)\(\);", tail, _re.M))
    missing = sorted(n for n in PANEL_LOADERS if n in defined and n not in called)
    check(
        not missing,
        f"★ 每个**面板**的 loader 都在启动序列里被调用（漏掉的：{missing}）"
        " —— 漏一个 = 那个面板刷新页面后永远是空的",
    )



def test_corrections_panel_is_wired() -> None:
    section("⑲ ★ 解算改正：设置里那 9 个开关**从界面接到底**")

    # ⚠️ 这一节守的是"界面 → 配置"那条链上的**每一个断点**。
    #    每一段都能单独断，而断了的症状都是**静默**的：
    #
    #      ① 面板里没那块      → 用户根本看不见（功能等于不存在）
    #      ② 不拉 /api/corrections → 面板永远停在骨架屏
    #      ③ 勾了不提交        → 用户以为改了，配置里没变（**最坏的一种**：
    #                             他会拿一个"设了但没生效"的结果去做判断）
    #      ④ 提交的是整份而不是覆盖值 → 档位从此换不动（见 settings.corrections）
    #      ⑤ 没有「恢复默认」  → 改乱了回不去，只能去删 config.json
    html = (ROOT / "ginan_agent" / "frontend" / "index.html").read_text(
        encoding="utf-8")
    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(
        encoding="utf-8")
    import re as _re

    check('id="corr-list"' in html, "① 设置面板里有改正项列表（corr-list）")
    check('id="corr-reset"' in html, "⑤ 有「全部恢复默认」按钮")
    check("/api/corrections" in js, "② app.js 会去拉 /api/corrections")

    # ⚠️ 判据用**行锚定**的正则，不是"字符串出现过" ——
    #    这一节自己上面就有过教训：宽判据会被注释里的同名文字喂饱。
    check(bool(_re.search(r"^\s*corrections:\s*corrOverrides", js, _re.M)),
          "★★ ③ 保存时把覆盖值提交上去（不提交 = 改了等于没改）")
    check(bool(_re.search(r"^\s*corrOverrides\s*=\s*Object\.assign", js,
                          _re.M)),
          "★ 打开设置时从设置里把覆盖值读回来（否则每次打开都是默认样）")
    check(bool(_re.search(r"^\s*modal\.corrReset\.addEventListener", js,
                          _re.M)),
          "★ ⑤ 那个按钮真的接上了（不是个摆设）")

    # ④ 提交的必须是 corrOverrides（稀疏覆盖），不是整份 9 项的值
    body = js.split("async function saveSettings()", 1)[-1].split(chr(10) + "  }", 1)[0]
    check("corrections: corrOverrides" in body,
          "★★ ④ 提交的是**用户改过的那几项**，不是整份 —— "
          "整份会让档位从此换不动，而没人看得出来")

    # ② 打开设置时要调用它（对话框的 loader 不进启动序列，见 ⑱）
    opens = js.split("async function openSettings()", 1)[-1]
    opens = opens.split("function closeSettings", 1)[0]
    check("loadCorrections();" in opens,
          "② 打开设置时真的调了 loadCorrections()（不是定义了没人用）")

def test_native_dialogs_are_plain_text() -> None:
    section("⑧ ★ `confirm()` / `alert()` 里不能有 Markdown 记号")

    # ⚠️⚠️ 这是这个文件里所有规则的**同一个成因**，只是换了个地方发作：
    #
    #        "我们写给用户看的话"被当成了 Markdown。
    #
    #    前两次是 Python 那边的 label / advice。这一次在 app.js 里 ——
    #    而它比前两次更隐蔽，因为**浏览器原生对话框不渲染 Markdown**：
    #
    #        confirm('会删掉：\n  · **观测数据**（你自己测的那份）')
    #
    #    用户看到的是**字面的一排星号**。
    #
    #    ⚠️ 而且它一直露着：`新任务` 那个确认框里原来就写着
    #       `**已经认领过**`、`**还没认领过**` —— 没人报，是因为
    #       它只在用户点"新任务"时弹一下，谁也不会为此专门提一嘴。
    #
    #    👉 判据：`confirm(` / `alert(` 的**括号里那段**不许出现
    #       `**` / `__` / 反引号。只看括号里，不看整个文件 ——
    #       app.js 里别的地方（模板、注释）用 Markdown 是正常的。
    import re

    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    def call_blocks(name: str) -> list[str]:
        """取出 `name(...)` 括号里的原文（含括号）。"""
        out: list[str] = []
        for m in re.finditer(rf"\b{name}\s*\(", js):
            start = m.end() - 1                  # 指向 '('
            depth = 0
            for j in range(start, len(js)):
                if js[j] == "(":
                    depth += 1
                elif js[j] == ")":
                    depth -= 1
                    if depth == 0:
                        out.append(js[start : j + 1])
                        break
        return out

    blocks = call_blocks("confirm") + call_blocks("alert")
    check(len(blocks) >= 3, f"找到了 {len(blocks)} 个原生对话框")

    bad: list[str] = []
    for b in blocks:
        for marker in ("**", "__", "`"):
            if marker not in b:
                continue
            # ⚠️ 要打印**含那个记号的那一行**，不是第一行 ——
            #    第一版打的是 `b.splitlines()[1]`，而那是 `'开始新任务？\n\n'`，
            #    真正的毛病在下面好几行。**报错指错地方**比不报还费时间。
            for ln in b.splitlines():
                if marker in ln:
                    bad.append(f"{marker!r} 出现在：{ln.strip()[:70]}")
                    break

    for x in bad:
        print(f"      ⚠️ {x}")
    check(
        not bad,
        "★ 原生对话框里是纯文本 —— 有记号的话用户看到的是字面星号",
    )

    # =====================================================================
    # ⚠️⚠️ 「新任务」那个框**必须点名它要删的东西** —— 一句一句地检查
    # =====================================================================
    #
    #    这是整个程序里唯一删用户文件的地方，所以这段话的准确度
    #    不是"体验好不好"，是"用户会不会丢东西"。
    #
    #    它**错过两次**，两次都是同一个方向（说得比实际温柔）：
    #
    #      ① 原来挂着 `**已经认领过**`，用户看到的是一排星号
    #      ② 后来「会保留」那张清单里挂着"收件箱里还没认领过的文件"，
    #         而 2026-09-21 之后**没认领的也会被删**（用户明确要的）
    #
    #    👉 所以这里不检查"文案长什么样"，检查**它有没有说那句话**。
    #       ⚠️ 而且**只能看 confirm 那段括号里** ——
    #          app.js 的注释里也写着"收件箱"这些字，
    #          整文件搜的话，文案删光了这条还是绿的。
    # ⚠️ 挑"会删掉"而不是"新任务"：`alert('开始新任务失败：…')` 里也有那三个字，
    #    按它挑会挑到两个，这条守卫就废了。
    newtask = [b for b in blocks if "会删掉" in b]
    check(len(newtask) == 1, f"找到了「新任务」那个确认框（{len(newtask)} 个）")
    if newtask:
        text = newtask[0]
        check(
            "收件箱" in text and "全部文件" in text,
            "★★ 「会删掉」里点名了收件箱里的**全部文件**"
            "（没认领的也删 —— 不写清楚用户不知道）",
        )
        check(
            "会保留" in text,
            "★ 仍然分开写着『会保留』（用户要知道什么还在）",
        )
        # ⚠️⚠️ 运行记录（`userdata/runs/`）**也要点名** —— 2026-10-02 补的。
        #
        #    在那之前这里挂的是「任务状态（计划 / 配置 / 运行记录）」：
        #    **说了要删，而 `Hub.reset()` 一个字节都没删过它**
        #    （实测 12 天 41 次运行 14 GB）。现在真删了，
        #    所以文案必须跟着说清"删的是**整个运行目录**、结果也一起走" ——
        #    不然用户会以为只清中间文件、结果还在。
        erased_part = text.split("会保留")[0]
        check(
            "运行记录" in erased_part,
            "★★ 「会删掉」里点名了运行记录",
        )
        check(
            "结果" in erased_part,
            "★★ 而且说清楚**结果也一起走**（.POS/.SP3 都在那个目录里）"
            "—— 不写的话用户会以为只清中间文件",
        )
        # 「会保留」那一段里不许出现收件箱
        kept_part = text.split("会保留")[-1]
        check(
            "收件箱" not in kept_part,
            "★★ 「会保留」里不许提收件箱 —— 它现在一个文件都不留",
        )
        check(
            "运行记录" not in kept_part,
            "★★ 「会保留」里也不许提运行记录 —— 它现在也一个都不留",
        )


def test_autoscroll_follows_the_tail() -> None:
    section("⑨ ★ 自动滚动：跟的是**最新的内容**，不是那条回复的标题")

    # ⚠️⚠️ 用户的原话："对话框回复我的时候，我的界面现在是滚动到它回话的
    #     标题，不是最新的内容"。
    #
    #     成因是**在渲染时现算几何距离**：
    #
    #         nearBottom = scrollHeight - scrollTop - clientHeight < 120
    #         if (nearBottom) scrollTop = scrollHeight
    #
    #     渲染是节流的（120ms 一次），两次之间模型可能吐出几百像素 ——
    #     于是这一次检查看到的距离已经超过阈值 → 判成"用户翻上去了"
    #     → **不滚了**，而且之后只会离得更远，**再也回不来**。
    #     停下来的位置正好是那条回复的开头 —— 也就是"标题"。
    #
    #     👉 改成由**真实的滚动事件**驱动：
    #        只有用户真的动手滚了，标志才变；
    #        内容自己变长**不触发 scroll 事件**，所以标志保持"跟底"。
    #
    #     **几何量会骗人，事件不会。**
    #
    # ⚠️ 强度说明：和 ⑦ 一样是**结构性**检查，不是行为测试 ——
    #    真跑一遍要搭 DOM + EventSource 的桩。它挡的是"有人把
    #    这套改回现算"这一种，而那是概率最大的那种。
    import re

    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    check(
        "let stickToBottom = true;" in js,
        "★ 有 stickToBottom 这个状态（跟不跟底）",
    )
    check(
        re.search(r"el\.messages\.addEventListener\(\s*'scroll'", js) is not None,
        "★ 它由**真实的滚动事件**改写（这是整条修复的要点）",
    )

    # `scrollDown()` 自己**不许**再算几何距离 —— 现算就是那个 bug。
    start = js.find("function scrollDown()")
    check(start != -1, "找到了 scrollDown()")
    if start != -1:
        depth = 0
        end = len(js)
        for j in range(js.find("{", start), len(js)):
            if js[j] == "{":
                depth += 1
            elif js[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        body = js[start:end]
        check(
            "scrollHeight - m.scrollTop" not in body
            and "nearBottom" not in body,
            "★ scrollDown() 里**没有**现算距离（现算 = 滚动会中途卡死）",
        )
        check("stickToBottom" in body, "★ 它读的是那个由事件维护的标志")

    # 封口那次渲染也要滚 —— 它是高度变化最大的一次（Markdown 一渲染，
    # 表格/列表会撑高一大截），不补这一下，一轮的**结尾**留在屏幕外面。
    seal = js.find("function sealBubble()")
    if seal != -1:
        seg = js[seal : js.find("function scheduleRender", seal)]
        check(
            "scrollDown();" in seg,
            "★ 封口后也滚一下（否则最后一段的内容停在屏幕外）",
        )

    check(
        "stickToBottom = true;" in js.split("function newTurn()")[-1][:900],
        "★ 新一轮开始时重新跟底（用户刚发消息，他要看的就是回答）",
    )


def test_starter_cards_speak_for_the_user() -> None:
    section("⑩ ★ 起手卡片说的是**用户想说的话**，不是写死的测站和日期")

    # ⚠️⚠️ 用户的原话："我点准备数据怎么回复这个，
    #     点准备数据应该是检查我收件箱有什么数据并且给我建议"
    #
    #     那张卡片的 `data-say` 原来写着：
    #
    #         "帮我看看 ALIC 站 2026 年 9 月 2 日需要下载哪些数据"
    #
    #     两个问题：
    #
    #       ① **它替用户说了他没说过的话。** 测站和日期是**写死在界面里
    #          的常量** —— 点一下就等于声称"我要 ALIC 的 9 月 2 日"。
    #          而用户只是想"准备一下我的数据"。
    #          （更巧的是，2026-09-02 正好是他刚让程序删掉的那份数据。）
    #
    #       ② **它和卡片的名字不是一回事。** 卡片叫「准备数据」，
    #          用户的理解是"看看我有什么、还缺什么"，而那句话让 agent
    #          直接去查环境 + 做**取数计划** —— 屏幕上就是两张
    #          "检查运行环境""制定取数计划"的卡片。
    #
    #     👉 起手卡片是**代用户发言**，所以它只能说他真想说的那种话：
    #        不能替他选定一个测站、一天数据。
    import re

    html = (ROOT / "ginan_agent" / "frontend" / "index.html").read_text(encoding="utf-8")
    cards = re.findall(
        r'<button class="card"\s+data-say="([^"]+)"(.*?)</button>', html, re.S
    )
    check(len(cards) >= 3, f"找到了 {len(cards)} 张起手卡片")

    # 测站代码是 4 个大写字母（ALIC / DARW / …）；日期是「2026 年 9 月 2 日」
    # 或 `2026-09-02` 这类写法。
    STATION = re.compile(r"\b[A-Z]{4}\b")
    DATE = re.compile(r"\d{4}\s*年|\d{4}-\d{2}-\d{2}")

    bad: list[str] = []
    for say, block in cards:
        m = STATION.search(say)
        if m:
            bad.append(f"写死了测站 {m.group(0)!r}：{say[:40]}")
        if DATE.search(say):
            bad.append(f"写死了日期：{say[:40]}")

    for b in bad:
        print(f"      ⚠️ {b}")
    check(
        not bad,
        "★ 没有卡片替用户选定测站/日期（起手卡片只能说他真想说的那种话）",
    )

    # 「准备数据」这张：用户理解它 = 看我的数据。它就得真的说那件事。
    prep = [b for say, b in cards if "准备数据" in b]
    check(len(prep) == 1, "找到了「准备数据」那张卡片")
    if prep:
        say = [s for s, b in cards if "准备数据" in b][0]
        check(
            "收件箱" in say,
            f"★ 它说的是**看收件箱**（实际：{say[:44]}）—— "
            "不说的话 agent 会跑去查环境 + 做取数计划",
        )


def test_prompt_knows_about_the_inbox() -> None:
    section("⑩b ★ 系统提示词里必须有「先看收件箱」这条")

    # ⚠️ 这条和上面那条是**同一个约定的两半**，所以放在一起：
    #
    #        卡片负责**说**（界面）
    #        提示词负责**做**（agent）
    #
    #     只改一半就是白改：卡片说"看看我的数据"，而提示词里
    #     **一个字都没提收件箱** —— 于是 agent 按它唯一知道的流程走
    #     （① 检查环境 ② 测数据源 ③ 制定计划…），用户看到的就是
    #     一堆和"我的数据"无关的卡片。
    #
    #     ⚠️ 实测就是这么发生的：三张卡的「标准流程」写在提示词里，
    #        收件箱**从来没出现在那份流程里**。
    #
    # ⚠️ 强度说明：这是**关键词检查**，很粗。它挡的是"这条规则整个
    #    被删掉"这一种。规则有没有被模型执行，得真跑一次才知道。
    src = (ROOT / "ginan_agent" / "agent" / "prompts.py").read_text(encoding="utf-8")

    check("收件箱" in src, "★ 提示词里提到了收件箱")
    check(
        "scan_inbox" in src or "list_local_obs" in src,
        "★ 而且指明了先调哪个工具（光说'注意收件箱'不是可执行的）",
    )


# =============================================================================
# ⑩c 「做不了」要**分种类**说（ADR-008）
# =============================================================================


def _prompt_block(src: str, start: str, end: str) -> str | None:
    """把提示词里从 `start` 到 `end` 的那一段切出来。

    ⚠️ 两个锚点都要**找得到**，找不到返回 `None` —— 调用方必须把它当失败。
       不然"锚点被删了"会退化成一个空串，而**空串能通过所有 `not in` 断言**
       （那种守卫只会越删越绿，见 `check_toolchain` 开头那段）。
    """
    i = src.find(start)
    if i < 0:
        return None
    j = src.find(end, i + len(start))
    if j < 0:
        return None
    return src[i:j]


# 「只许以否定形式出现」用的否定词。
_NEGATIONS = ("不要", "不许", "别", "不能", "不是")


def _only_negated(block: str, phrase: str, window: int = 24) -> bool:
    """`phrase` 在这个块里**只以否定形式**出现（比如"不要说成…"）。

    ⚠️ 为什么不能直接 `phrase not in block`：**"不要说成 X" 比 "X" 好** ——
       用户最容易踩的正是那一句，所以要明确写出来。直接禁字符串会把
       写法更好的一版判成错的。

    ⚠️ 但"明确写出来"和"真的这么建议"必须分得开 —— 判据是**往前看**：
       紧挨着它前面有没有否定词。这一段就是这条守卫的全部牙齿：
       把电离层那段的建议句整句抄过来（没有否定词）⇒ 红。
    """
    at = -1
    while True:
        at = block.find(phrase, at + 1)
        if at < 0:
            return True
        if not any(n in block[max(0, at - window):at] for n in _NEGATIONS):
            return False


def test_prompt_says_which_kind_of_impossible() -> None:
    section("⑩c ★★ 提示词要把「做不了」的**种类**说对（ADR-008）")

    # ⚠️ 为什么需要这条守卫：**「做不了」有三种，说错了用户会做错事** ——
    #
    #        没做          → 等我们
    #        做不了·数据    → 永远不会有（实时流那五条）  ← 别等，绕道走
    #        做不了·上游    → 等官方修（电离层）          ← 只能等
    #
    #   把"上游"说成"数据"，用户会以为换个数据源就行；
    #   把"数据"说成"上游"，用户会一直等一个永远不会来的修复。
    #
    # ⚠️⚠️ 所以这条守卫的**判据是"两块的说法互不相同"**，不是"有'做不了'三个字"：
    #    一个把两块写成同一段话的实现在这里必须红 —— 而那正是最容易犯的懒。
    src = (ROOT / "ginan_agent" / "agent" / "prompts.py").read_text(encoding="utf-8")

    ion = _prompt_block(src, "用户问\"电离层\"", "用户问\"实时")
    live = _prompt_block(src, "用户问\"实时", "6. **⚠️ 要用户拍板时")

    check(ion is not None, "★ 找得到电离层那一段（锚点没被删）")
    check(live is not None, "★ 找得到实时流那一段（锚点没被删）")
    if ion is None or live is None:
        return

    # ---- 电离层：上游坏了 ⇒ 只能等官方 ----
    check("等官方修" in ion, "★ 电离层那段说的是「等官方修」（做不了·上游）")
    check("不是你的数据" in ion, "★ 而且点明不是用户数据的问题")

    # ---- 实时流：数据不归档 ⇒ 永远不会有，别等 ----
    check("将来也不会有" in live or "永远也不会有" in live,
          "★★ 实时流那段说的是「将来也不会有」（做不了·数据）")

    # ⚠️⚠️ 这一段是整条守卫的牙齿：**"等官方修"只许出现在否定句里**。
    #     它在这两块里的**角色完全相反** ——
    #         电离层那段：它就是给用户的结论（肯定句）
    #         实时流那段：它是**要避免的说法**（否定句）
    #     把上一段的建议抄到这一段 ⇒ 没有否定词 ⇒ 红。
    check('说成"等官方修"' in live, "★★ 实时流那段要**明说**不要用「等官方修」这个说法")
    check(_only_negated(live, "等官方修"),
          "★★ 而且「等官方修」在那段里**只许出现在否定句里**"
          " —— 流就是不归档，官方修不了")

    # ⚠️ 这一条是防"顺手把三种都说成没做"：那段必须**主动否掉**"等我们"。
    check("不是我们还没开发" in live or "不是我们没做" in live,
          "★★ 实时流那段要**主动否掉**「还没开发」—— 不说的话用户会一直等我们")

    # ---- 五个名字要点到，否则模型对不上用户的话 ----
    named = [k for k in ("rt_ppp", "rt_rtk", "record_streams",
                         "record_snr", "sbas_pvs_rt") if k in live]
    check(len(named) == 5, f"★ 五条都点了名（缺 {sorted(set(['rt_ppp','rt_rtk','record_streams','record_snr','sbas_pvs_rt']) - set(named))}）")

    # ---- ⚠️ 对照：SBAS 那三条**不能被一起说成做不了** ----
    #   它们和 `sbas_pvs_rt` 名字像，但跑的是归档好的演示数据，**能做**。
    check("sbas_l1" in live and "能做" in live,
          "★ 对照：`sbas_l1`/`sbas_dfmc`/`sbas_pvs` 那三条要**明确保住**"
          "（名字像不等于同一条路，它们是能跑的）")


# =============================================================================
# ⑪ 类名对账：app.js 会写出来的每一个 class，样式表里必须有规则
# =============================================================================


def _css_without_comments() -> str:
    """去掉注释的 CSS 原文。断言一律用这个 —— 注释里也会出现 `.xxx`。"""
    import re

    path = ROOT / "ginan_agent" / "frontend" / "styles.css"
    return re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)


def _has_class_rule(css: str, token: str) -> bool:
    """样式表里有没有 `.token` 这条规则。

    ⚠️ 判据是 `.token` 后面**不能再跟字符**（`(?![\\w-])`）——
       否则 `.tool` 会被 `.tool-head` 假阳性命中，而它们是两个不同的类。
       `.token` 后面跟空格、`{`、`:`（伪类）、`.`（另一个类）都算命中。
    """
    import re

    return re.search(r"\." + re.escape(token) + r"(?![\w-])", css) is not None


def _has_compound_rule(css: str, classes: list[str]) -> bool:
    """⚠️⚠️ **承载这些 class 的那个元素，真的拿得到规则吗。**

    和上面那个的判断**不一样**，而差别是要紧的：

        `_has_class_rule(css, "auth")` 问的是"样式表里出现过 `.auth` 吗"。
        而元素是 `<span class="check-dot auth">` —— 它需要一条
        **同时**选中这两个类的规则（`.check-dot.auth`）。

    ⚠️ 实测那条守卫是可以被静默绕过的：
       把 `.check-dot.auth { … }` 改名成 `.dot.auth { … }`（换个复合选择器），
       `_has_class_rule(css, "auth")` **照样返回 True**，
       而那个 span **一点样式都没有了** —— 显示的还是一个中性灰点，
       和"还没测"长得一样。**那正是这条守卫存在的唯一理由。**

    ⚠️ 判据：把样式表切成选择器，逐个**复合段**比对 ——
       某个复合段上的类**包含**这些类，才算命中。
    """
    import re

    want = set(classes)
    # ⚠️ 按 `{}` 切开之后，偶数段是选择器、奇数段是声明体。
    #    不切的话，声明里的 `.5em` 之类会被当成类名。
    chunks = re.split(r"[{}]", css)
    for sel in chunks[0::2]:
        for part in re.split(r"[\s>+~]+", sel):
            # 只看这一段里的类名（去掉 `:hover` 之类的伪类不影响）
            got = set(re.findall(r"\.([A-Za-z_][\w-]*)", part))
            if want and want <= got:
                return True
    return False


# 状态词：不在自动捞取的四个模式里，但 app.js / 后端确实会写出来。
# 每一项都注明**谁**会写它 —— 没有出处的一项就不该在这一行活着。
_STATE_WORDS = {
    # 工具卡片的终态，来自 app.js 的 STATUS_STYLE（经 classList.add(style.cls)）
    "done", "denied", "cancelled", "not-run", "failed",
    # check-dot 的状态。⚠️ "auth" 这一项曾经**根本没有 CSS 规则** ——
    # 于是"需要登录"的数据源显示成一个中性灰点，和"还没测"长得一样。
    # 这就是这一节存在的理由。
    "ok", "warn", "fail", "info", "auth",
}


def _classes_written_by_js(js: str) -> set[str]:
    """把 app.js 会写出来的 class 名捞出来。

    五个来源，前五个都是自动捞的：

      ① `className = '...'` —— 含 `+` 拼接，也含反引号模板
      ② `classList.add/remove/toggle/contains('...')`
      ③ `innerHTML = '... class="xxx yyy" ...'` 这类模板里的 class 属性
      ④ STATUS_STYLE 里的 `cls: '...'`
      ⑤ `setAttribute('class', '...')` —— SVG 元素只能这么设

    外加手工维护的 _STATE_WORDS（见上面那个常量）。
    """
    import re

    out: set[str] = set()

    def eat(text: str) -> None:
        """把一段字符串字面量里的类名收进来。"""
        # 模板里的 `${...}` 是变量，不是类名 —— 连同里面的字面量一起丢掉
        text = re.sub(r"\$\{[^}]*\}", " ", text)
        out.update(text.split())

    # ① className =（单引号串 或 反引号模板）
    for m in re.finditer(r"\.className\s*=\s*([^;]+);", js):
        for a, b in re.findall(r"'([^']*)'|`([^`]*)`", m.group(1)):
            eat(a or b)

    # ② classList.xxx('...')
    for m in re.finditer(
        r"classList\.(?:add|remove|toggle|contains)\(\s*'([^']+)'", js
    ):
        eat(m.group(1))

    # ③ 模板字符串里写出来的 class="..."
    #    ⚠️ `[^"$]*` 在 `$` 处停下 —— `class="check-dot ${g.icon}"`
    #       只该收进 `check-dot`，`${g.icon}` 的值是别的来源管的。
    for m in re.finditer(r'class="([^"$]*)', js):
        eat(m.group(1))

    # ④ cls: '...'
    for m in re.finditer(r"\bcls\s*:\s*'([^']+)'", js):
        eat(m.group(1))

    # ⑤ setAttribute('class', '...')
    #    ⚠️ 漏了这个的话 `.tool-icon` 就查不到 —— 它是**建 SVG 元素**时
    #       用 setAttribute 写上去的（SVG 上不能直接给 className 赋值）。
    #       也就是说"用 className 才能设类名"这个假设，在 SVG 上不成立。
    for m in re.finditer(r"setAttribute\(\s*'class'\s*,\s*'([^']+)'", js):
        eat(m.group(1))

    out.update(_STATE_WORDS)
    return {c for c in out if c}


def test_css_covers_every_class_js_writes() -> None:
    section("⑪ ★ 类名对账：app.js 写出来的每个 class，样式表里都有规则")

    # ⚠️⚠️ 为什么需要这一节 —— 这是重写样式表时**最容易失守、而且完全静默**
    #     的一类 bug：
    #
    #         CSS 文件重写一遍，某个类名忘了写进去 →
    #         元素还在、文字还在、什么都不报错 →
    #         只是那一块**没有任何样式**，长得和旁边不一样。
    #
    #     清点现有代码时就发现了两处这种"沉默的空洞"：
    #
    #         .check-dot.auth     probe.py 会说"需要登录"，但没人给它来过样式
    #         .progress-text      进度文字根本没规则，继承到什么算什么
    #
    #     这两处都**存活了很久**，因为没有任何东西会因为它们而失败。
    #
    #     👉 判据：把 app.js 里会写出来的 class 全捞出来，逐个回样式表里找。
    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")
    css = _css_without_comments()

    written = _classes_written_by_js(js)

    # 先确认"捞"这个动作本身是好的 —— 捞空了的话下面全是空转
    check(len(written) >= 25, f"从 app.js 捞到 {len(written)} 个类名")
    # 抽两个确认它们在集合里：捞取逻辑一旦坏掉，这两条会立刻变红
    for probe_token in ("tool-head", "check-dot"):
        check(probe_token in written, f"捞取结果里有 {probe_token}")

    missing = sorted(c for c in written if not _has_class_rule(css, c))
    for m in missing:
        print(f"      ⚠️ .{m} —— app.js 会写它，但样式表里查无此类")
    check(
        not missing,
        "★ 没有孤儿类名（写着它、却没有一条规则的地方）",
    )


# =============================================================================
# ⑫ 结构依赖 + 残留
# =============================================================================


def test_restyle_did_not_break_dependencies() -> None:
    section("⑫ ★ 重做样式时不能碰坏的那几样东西")

    import re

    css = _css_without_comments()
    html = (ROOT / "ginan_agent" / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    rules: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"^([.#][\w:()\- ]+)\s*\{([^}]*)\}", css, re.M):
        for decl in m.group(2).split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                rules.setdefault(m.group(1).strip(), {})[k.strip()] = v.strip()

    # --- ① index.html 里 9 个 <svg> 一个 fill/stroke 属性都没有 -------------
    # 它们能显示出来，全靠这两条规则。删掉 → 整屏图标变成黑色实心块。
    check(
        rules.get(".logo", {}).get("fill") == "none"
        and rules.get(".logo", {}).get("stroke") == "currentColor",
        "★ .logo 还是 fill:none + stroke:currentColor（9 个 SVG 的着色靠它）",
    )
    check(
        rules.get(".icon", {}).get("fill") == "none"
        and rules.get(".icon", {}).get("stroke") == "currentColor",
        "★ .icon 还是 fill:none + stroke:currentColor",
    )
    # 而且 HTML 里的 SVG 确实没有自带 fill/stroke（否则上面两条就失去意义了）
    svg_no_fill = len(re.findall(r"<svg(?![^>]*\bfill=)", html))
    check(svg_no_fill >= 8, f"{svg_no_fill} 个 <svg> 仍然不带 fill 属性")

    # --- ② 那个自由输入框只有 .ask-free input 一条样式来源 ----------------
    #     app.js 用 createElement('input') 建的，**没有 class、没有 id**。
    check("ask-free" in css and ".ask-free input" in css,
          "★ .ask-free input 还在（那个框唯一的样式来源）")

    # --- ③ 另外两处曾经的空洞 ---------------------------------------------
    #
    # ⚠️⚠️ 判据是 `_has_compound_rule`（**元素真的拿得到规则**），
    #    不是 `_has_class_rule`（"样式表里出现过这个词"）。
    #
    #    实测过差别：把 `.check-dot.auth` 改名成 `.dot.auth` 之后，
    #    旧的判据**照样绿**，而 `<span class="check-dot auth">` 没样式了。
    #    **一条能靠改个选择器绕过的守卫，等于没有守卫。**
    check(
        _has_compound_rule(css, ["check-dot", "auth"]),
        "★ `<span class=\"check-dot auth\">` 拿得到规则（`.check-dot.auth`，原来没有）"
        "—— 判据是**元素拿得到**，不是「样式表里出现过 .auth」",
    )
    check(
        _has_compound_rule(css, ["progress-text"]),
        "★ `.progress-text` 那个 div 拿得到规则（原来没有）",
    )

    # --- ④ #empty 删不得 ---------------------------------------------------
    #     app.js 只 .remove() 它，从不重建 —— 回到起手卡片全靠 location.reload()
    check('id="empty"' in html, "★ index.html 里还有 #empty（点「新任务」要靠它回来）")
    check("el.empty.remove()" in js, "（app.js 那边确实在删它 —— 所以它必须留在 HTML 里）")

    # --- ⑤ 时钟脚本要先于 app.js 加载 -------------------------------------
    i_clock = html.find("/static/clock.js")
    i_app = html.find("/static/app.js")
    check(i_clock != -1, "index.html 引了 clock.js")
    check(i_clock != -1 and i_clock < i_app, "★ 它排在 app.js **前面**（app.js 启动时要用它）")

    # --- ⑤b ★ 离线横幅必须在**最上面** -------------------------------------
    #
    #     ⚠️ 这里抓到过一个真 bug（重做界面时用无头浏览器截图看出来的）：
    #
    #        横幅是 app.js 运行时 prepend 进 .app 的，原来靠
    #        `grid-column: 1 / -1` **自动放置**。而 .app 的网格用
    #        grid-template-areas 把两行都占满了 → 自动放置只好把它塞进
    #        一行**隐式**网格，那行排在 1fr 的主区**后面** →
    #        横幅出现在窗口最底下，夹在输入框下面。
    #
    #        而它自己写着"程序可能已经关闭 —— 那个黑色的命令行窗口就是它"。
    #        **最不该被错过的一条提示，被摆到了最不起眼的角落。**
    #
    #     👉 判据：它有具名格子 `bar`，而且 .app 里真的定义了这个名字。
    #        少了任何一半，横幅都会掉回那个隐式行里。
    bar_rule = rules.get(".offline-bar", {})
    check(
        bar_rule.get("grid-area") == "bar" or bar_rule.get("grid-row") == "1",
        "★ 离线横幅有**具名**格子（不是靠自动放置）",
    )
    app_areas = rules.get(".app", {}).get("grid-template-areas", "")
    check(
        re.search(r'"bar\s+bar"', app_areas) is not None,
        f"★ .app 的 grid-template-areas 第一行是 bar bar（实际：{app_areas}）",
    )
    # 横幅断线时把输入区关掉。⚠️ 这条用的是 :has()，**它不生效是不会报错的** ——
    # 所以在这里记一笔：实测 Edge 支持，而且实测这条规则确实算出了 opacity 0.45。
    #
    # ⚠️ 这里**不能**用上面那个 rules 字典查：它靠正则
    #    `^([.#][\w:()\- ]+)\s*\{` 解析，而 `.app:has(.offline-bar) .composer`
    #    里那个 `.`（在括号内）不在字符类里 → 整条规则**根本匹配不上**。
    #    直接在原文里找。
    check(
        "has(.offline-bar)" in css,
        "★ 离线时输入区变灰的规则还在（:has() 静默失效过一次）",
    )

    # --- ⑥ 浅色主题的残留 ---------------------------------------------------
    #     用户拍板只留深色。留一半的话，那个按钮点下去毫无反应 ——
    #     比一开始就没有更糟。
    #
    #     ⚠️ 判据是**代码写法**，不是关键词。上面那段说明里就写着
    #        'ginan-theme' 这个词（那是注释，不是代码）——
    #        按关键词查的话，守卫会被自己文档里的说明绊倒。
    #        而且按关键词查还有反过来的风险：为了绕过它，改注释就行。
    check("[data-theme" not in css, "★ 样式表里没有 [data-theme] 了")
    check("btn-theme" not in html, "★ HTML 里没有主题切换按钮了")
    check("dataset.theme" not in js, "★ app.js 里没有写 data-theme 的代码了")
    check(
        "setItem('ginan-theme'" not in js and "getItem('ginan-theme'" not in js,
        "★ app.js 里没有读写 ginan-theme 的代码了",
    )
    check("function applyTheme" not in js, "★ applyTheme() 整个删掉了，不是留着不用")


# =============================================================================
# ⑭b 收件箱：按钮问的是"还有几个没认领"，不是"收件箱里有几个"
# =============================================================================


def test_inbox_button_asks_the_right_question() -> None:
    section("⑭b ★ 收件箱的「认领」按钮：问的是**没认领的**，不是**总数**")

    # ⚠️⚠️ 用户的原话：
    #      "我点看看我的数据，程序认领了我的数据后，
    #       右下角收件箱就不要提示认领了"
    #
    #     根因：认领是**复制**（原件留在收件箱，用户不会丢东西），
    #     所以认领完之后收件箱**看起来还是满的** ——
    #     按钮只看 `count` 的话就**永远在**，用户以为没成功，反复点。
    #
    #     👉 "文件在收件箱里" 和 "还没认领过" 是**两个问题**。
    #
    # ⚠️ 这是**前后端之间的一个契约**，所以要同时钉住两半：
    #     后端必须**给出** unclaimed，前端必须**用它**。少一半都不起作用，
    #     而且少哪一半都不会报错 —— 只会退回原来那个行为。
    import re

    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")
    py = (ROOT / "ginan_agent" / "server" / "app.py").read_text(encoding="utf-8")

    # ---- 后端：给出 unclaimed 和每个文件的 claimed ----
    check(
        '"unclaimed"' in py,
        "★ 后端把 unclaimed（真正还要认领几个）算出来了",
    )
    check(
        '"claimed"' in py,
        "★ 后端逐个文件给出 claimed（数据目录里有没有同样内容）",
    )
    check(
        "already_claimed" in py,
        "★ 它用的是 inbox.already_claimed —— 和 claimed_pairs 同一个判据",
    )

    # ---- 前端：按钮看的是 unclaimed ----
    m = re.search(r"btn-claim-inbox'\);(.{0,900}?)setPill", js, re.S)
    check(m is not None, "找到了按钮那段逻辑")
    if m:
        seg = m.group(1)
        check(
            "unclaimed" in seg,
            "★ 按钮的分支看的是 unclaimed",
        )
        check(
            "if (d.count > 0 && d.known > 0)" not in seg,
            "★ 而且**不再是**原来那个只看总数的判断",
        )

    # ---- 前端：读不到 unclaimed 时要**保守**（宁可多问一次） ----
    #
    #     ⚠️ 这里最容易写反：写成"读不到就当成 0"的话，
    #        旧后端 + 新页面 = 按钮永远不出现 = **用户没法认领**。
    #        那比多显示一个按钮糟得多。
    check(
        "d.unclaimed === undefined" in js,
        "★ 读不到 unclaimed 时退回 known（宁可多问一次，也不要挡住认领）",
    )


# =============================================================================
# ⑭ 「正在发生」的提示，必须在事情结束时撤掉
# =============================================================================


def test_transient_notices_are_cleared() -> None:
    section("⑭ ★ 描述「此刻」的提示，收工时要撤掉")

    # ⚠️⚠️ 用户的原话：
    #      "点停止之后，程序停了，'正在停止……会等当前这一步跑完（最长几十秒）'
    #       这个提示就该删除或隐藏"
    #
    #     成因：那条提示是 addNotice() 写进对话里的，**然后就一直在那儿**。
    #     停止早就完成了，屏幕上还挂着一句"正在停止" —— 它描述的是
    #     **此刻正在发生的事**，而此刻已经过去了。用户看到只会以为"还没停"。
    #
    #     👉 它是**状态**，不是一句留在对话记录里的话。
    #        状态在事情结束时就该消失。
    #
    #     ⚠️ 但也**不能干脆不显示**：取消是协作式的，最长要等几十秒，
    #        那几十秒里没有反馈的话，"点了没反应"和"卡死了"在用户眼里
    #        是一回事（这条也踩过，注释里留着）。
    #        所以判据是"**留到停完为止**"，不是"不留"。
    #
    # ⚠️ 强度说明：和 ⑦ ⑨ 一样是**结构性**检查，不是行为测试 ——
    #    真跑一遍要搭 DOM + EventSource + 一个能取消的真实回合。
    #    它挡的是"有人把这段清理删掉/搬走"这一种，而那是概率最大的那种。
    #    真正的行为验证是**手工点一次停止**（见 PLAN 的验证一节）。
    import re

    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    # ① addNotice 得把元素交出来，否则调用方没法撤它
    m = re.search(r"function addNotice\([^)]*\)\s*\{(.*?)\n  \}", js, re.S)
    check(m is not None, "找到了 addNotice()")
    if m:
        check("return div;" in m.group(1), "★ addNotice() 返回那个元素（不然撤不掉）")

    # ② 有那个引用，而且是从 addNotice 接过来的
    check("let stopNotice = null;" in js, "★ 有 stopNotice 这个引用")
    check("stopNotice = addNotice(" in js, "★ 它接的是 addNotice 的返回值")

    # ③ 有撤销函数，而且真的把元素从 DOM 里拿掉
    m = re.search(r"function clearStopNotice\(\)\s*\{(.*?)\n  \}", js, re.S)
    check(m is not None, "找到了 clearStopNotice()")
    if m:
        check("stopNotice.remove()" in m.group(1), "★ 它真的把元素移除了")
        check("stopNotice = null;" in m.group(1), "★ 而且把引用清了（不然会重复撤）")

    # ④ ★ 关键：收工那一处必须调到它
    #
    #    setBusy(false) 是**唯一**的收工点（endTurn 走它，停止失败也走它），
    #    所以断言就落在那里 —— 换个地方加清逻辑，这条会红。
    m = re.search(r"function setBusy\(busy\)\s*\{(.*?)\n  \}", js, re.S)
    check(m is not None, "找到了 setBusy()")
    if m:
        body = m.group(1)
        off = body.find("if (!busy)")
        check(off != -1, "setBusy() 里有 if (!busy) 这一支")
        if off != -1:
            check(
                "clearStopNotice();" in body[off:],
                "★ 收工时撤掉那条提示（这是它一直挂着的成因）",
            )


# =============================================================================
# ⑬ GPS 周：页头和程序内部必须用同一套周号
# =============================================================================


def test_frontend_does_not_drop_information() -> None:
    section("⑰ ★ 前端不许把后端给的信息**悄悄丢掉**")

    # ⚠️⚠️ 两条同源的毛病：**后端给了，前端不画**。
    #
    #    它们的共同点是**用户永远不知道自己少了什么** ——
    #    没有报错、没有提示，就是"信息不在这里"。
    #    而这正是本项目反复记的那条：静默漏掉比报错糟得多。
    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    # ---- ① 中文输入法：按 Enter 是在**选词**，不是发送 ----
    #
    #    ⚠️ 主输入框有 `!e.isComposing`，而 `ask_user` 那个自由输入框
    #       （用户在这里填天线型号、测站名 —— 正是会用输入法的地方）
    #       **没有**。中文用户选词的那一下就把半截话发出去了。
    #
    #    ⚠️ 同一份文件里，一处有、一处没有 ——
    #       "一个原则只要有一处不照做，那一处就是全部"。
    # ⚠️ 这里数的是**出现次数**（含注释里的提及），所以判据是"至少两处"——
    #    正好两处的话说明**有一处只活在注释里**，那正是这个 bug 的形状。
    n_if = len(__import__("re").findall(r"!e\.isComposing", js))
    check(
        n_if >= 2,
        f"★ 两个输入框都排除了输入法组合状态（`!e.isComposing` 出现 {n_if} 处）",
    )

    # ---- ② 认领结果里的 `skipped` 必须渲染 ----
    #
    #    ⚠️ 后端一直在返回 `skipped`（带原因），而前端从来没画过 ——
    #       用户看到"看了 3 个、认领了 2 个"，第三个不翼而飞。
    claim_fn = js.split("async function claimInbox()", 1)
    body = claim_fn[1].split("\n  document.getElementById('btn-claim-inbox')", 1)[0] \
        if len(claim_fn) > 1 else ""
    check(bool(body), "找到了 claimInbox 那段（否则下面两条测的是空气）")
    check(
        "r.skipped" in body,
        "★★ 认领结果里「**没搬的**」也画出来了（带原因）"
        "—— 原来只画了 moved 和 unknown，跳过的那个不翼而飞",
    )
    check(
        "r.too_big" in body,
        "★ 太大而没处理的那一类也说了（后端单独一类）",
    )

    # ---- ③ 「新任务」删了什么 —— 这一条是**第三条**，同一个毛病 ----
    #
    #    ⚠️⚠️ 2026-09-21 用户报的：
    #          「点了新任务，…\245还在」
    #
    #       后端算好了要说的话（`cleared` / `kept` / `note`），
    #       而前端是这么写的：
    #
    #           const r = await api('/api/reset', { method: 'POST' });
    #           el.messages.innerHTML = '';
    #           location.reload();      // ← r 连同那份报告一起没了
    #
    #       `grep 'r.cleared' app.js` 当时是**空的**。
    #       所以那个没能删掉的文件（被别的程序占着），
    #       用户**没有任何途径**知道它为什么还在。
    #
    #    ⚠️ 它比 ①② 更难发现，因为**故障会自己好**（占用是一瞬间的事），
    #       用户重试一次就成功了 —— 于是"从来没报过"这件事
    #       永远不会有人来报。**能自愈的故障，最容易掩盖没报的错。**
    nt = js.split("async function newTask()", 1)
    nt_body = nt[1].split("alert('开始新任务失败", 1)[0] if len(nt) > 1 else ""
    check(bool(nt_body), "找到了 newTask 那段（否则下面几条测的是空气）")
    check(
        "r.failed" in nt_body and "r.cleared" in nt_body,
        "★★★ newTask 真的读了返回的 failed / cleared"
        "（原来拿到 r 就丢，一个字都没读）",
    )
    # ⚠️ 顺序：**先存后 reload**。反了的话报告会被 reload 一起带走。
    i_set = nt_body.find("sessionStorage.setItem")
    i_reload = nt_body.find("location.reload()")
    check(
        i_set != -1 and i_reload != -1 and i_set < i_reload,
        f"★★ 先存进 sessionStorage 再 reload（setItem@{i_set} < reload@{i_reload}）",
    )
    check(
        "showNewTaskReport()" in js,
        "★★ 页面上有个地方把它显示出来，而且**真的被调用了**"
        "（只定义不调用 = 还是看不见）",
    )
    check(
        "sessionStorage.removeItem" in js,
        "★ 读完就删 —— 只报一次，不能每次刷新都冒出来",
    )
    # ⚠️⚠️ **存进去用的键和读出来用的键必须一模一样。**
    #     两边各写一个字面量，打错一个字母 → 存了读不到 →
    #     **和没存完全一样**，而且不报任何错。
    #     这正是这个 bug 的形状（信息在最后一米丢掉），所以要专门拦。
    import re as _re

    keys = set(_re.findall(r"sessionStorage\.\w+\('([^']+)'", js))
    check(
        keys == {"ginan-newtask"},
        f"★★ 存和读用的是同一个键（实际找到 {sorted(keys)}）",
    )


def test_error_turn_also_ends() -> None:
    section("⑯ ★★ 「出错那一轮」也必须收工（否则界面就此锁死）")

    # ⚠️⚠️ 这是一条真 bug 的守卫，症状是**界面锁死**。
    #
    #    服务端 `Hub._run` 的形状：
    #
    #         try:     for event in agent.run(...): publish(event)
    #         except:  publish({"type": "error", ...})     ← 只推 error
    #         finally: publish(TURN_DONE)                  ← 前端收到的是 `done`
    #
    #    异常一抛，生成器就断了 —— **`turn_end` 永远不来**。
    #    而 `turn_end` 是**唯一**调 `endTurn()` 的地方。
    #
    #    后果（首次使用最常见的路径：没配 API Key 就发一句话）：
    #      · `turn` 一直非空、`setBusy(false)` 从来没跑
    #      · 发送按钮 hidden、输入框 disabled —— **发不出第二句话**
    #      · 「停止」点一下会把自己 disabled 掉（只有 setBusy(false) 能恢复）
    #        → **连停止也点不动了**
    #      · 「正在停止……会等当前这一步跑完」**永久留在屏幕上**
    #
    #    ⚠️ 这一节和 ⑭（那条提示要撤掉）是一对：
    #       ⑭ 测的是**正常收工**时会撤，它是绿的；而这条路上永远收不了工。
    #       **绿的是它覆盖的那条路，洞在没覆盖的那条上。**
    #
    # ⚠️ 强度说明：和 ⑦ 一样，这是**结构性**的检查，不跑 DOM
    #    （app.js 是 IIFE，要跑它得搭一整套 DOM + fetch + EventSource 的桩）。
    #    它挡的是"有人把兜底整个回退掉"，挡不住"逻辑改错但写法还在"。
    js = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    marker = "es.addEventListener('done',"
    check(marker in js, "★ 前端注册了 `done` 监听器（服务端 TURN_DONE 的落点）")

    body = js.split(marker, 1)[1].split("});", 1)[0] if marker in js else ""
    check(
        "endTurn()" in body,
        "★★ `done` 里调了 `endTurn()` —— 出错那一轮的**唯一**收工点"
        "（原来那里是一句空注释「turn_end 已经处理过了」，"
        "而那在异常路径上不成立）",
    )
    check(
        "if (turn)" in body,
        "★ 而且判了 `turn` 在不在 —— 正常路径上 turn_end 已经收过工，"
        "这时是空操作（endTurn 本身也是幂等的）",
    )


def test_clock_agrees_with_catalog() -> None:
    section("⑬ ★ 页头的 GPS 周 = 程序内部用的 GPS 周")

    # ⚠️ 页头上那个 `GPS 2437.0` 不是装饰：整个程序内部一直用 GPS 周说话
    #    （catalog.BKG_PRODUCTS_FROM_WEEK = 2238 之类），用户会在取数计划、
    #    缓存目录名和报错里反复看到那个数字。
    #
    #    而它是**另一套实现**：Python 用 date 相减，JS 用毫秒相减。
    #    两套各写各的，就可能各错各的 —— 页头显示 2437，而计划里写 2238。
    #
    #    👉 守卫的办法是把两边钉在**同一组契约值**上：
    #       这一半测 Python（下面），
    #       scripts/check_clock.mjs 测 JS（同样的三个日期）。
    #       任何一边漂了，那一边就会红。
    from datetime import date

    from ginan_agent.ginan import catalog

    # ⚠️ 和 check_clock.mjs 的 CONTRACT 是同一组，改一处必须改两处。
    CONTRACT = [
        (date(1980, 1, 6), 0, 0, "纪元本身"),
        (date(2022, 11, 27), 2238, 0,
         "IGS 改长命名那一周 —— catalog.BKG_PRODUCTS_FROM_WEEK 就是 2238"),
        (date(2026, 9, 20), 2437, 0, "写测试那天，恰好是周界"),
    ]

    for d, w, dow, why in CONTRACT:
        got = (catalog.gps_week(d), catalog.gps_dow(d))
        check(got == (w, dow), f"{d} → {w}.{dow}   （{why}）")

    # ⚠️ 日期算错是**最贵**的一类错误：日期差一天，下到的是隔壁那天的轨道，
    #    Ginan 照算不误、不报任何错，结果偏几米。
    #    所以顺手再验一次"GPS 周从周日算起"这条约定。
    #    2022-03-01 是周二 → 2（catalog.gps_dow 的文档字符串里写的例子）。
    check(
        catalog.gps_dow(date(2022, 3, 1)) == 2,
        "★ 2022-03-01（周二）→ 2 —— GPS 的周从**周日**算起，不是周一",
    )


# =============================================================================
# 主程序
# =============================================================================


def main() -> int:
    print("=" * 60)
    print("界面文案自检 —— 防止字面的 ** 露到界面上")
    print("=" * 60)
    print("⚠️ 不联网。这个 bug 踩过两次，所以需要测试拦着。")

    test_frontend_js_parses()
    test_environment_copy()
    test_probe_copy()
    test_inbox_copy()
    test_posfile_copy()
    test_scroll_containers()
    test_frontend_boundary()
    test_message_ordering()
    test_native_dialogs_are_plain_text()
    test_autoscroll_follows_the_tail()
    test_starter_cards_speak_for_the_user()
    test_prompt_knows_about_the_inbox()
    test_prompt_says_which_kind_of_impossible()
    test_css_covers_every_class_js_writes()
    test_restyle_did_not_break_dependencies()
    test_inbox_button_asks_the_right_question()
    test_transient_notices_are_cleared()
    test_error_turn_also_ends()
    test_frontend_does_not_drop_information()
    test_clock_agrees_with_catalog()
    test_every_panel_loader_is_wired_to_startup()
    test_corrections_panel_is_wired()

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
