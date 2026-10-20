"""自检：读懂 pea 的输出。

跑法：python scripts/check_diagnostics.py

============================ 为什么要单独测这个 ============================
因为 `diagnostics.py` 的输入是**一大段文字**，而它的规则全是模式匹配。

模式匹配最容易出的错是**静默的**：
  · 规则太宽 → 把正常输出也报成错误（用户被假警告淹没）
  · 规则太窄 → 真问题没被识别（用户拿到"处理正常完成"但结果是坏的）
  · 顺序错了 → 兜底规则抢在具体规则前面，所有错误都变成笼统的"出错了"

三种都不会让程序崩，只会让**结论不对**。所以必须用真实的输出片段测。

⚠️ 下面那些输出片段是**从真实 pea 的输出里抄的**，
   不是编的 —— 编的片段测不出"规则和真实格式不匹配"这种错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ginan_agent.ginan import diagnostics as D  # noqa: E402

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
# 真实的输出片段
# =============================================================================

BANNER = "PEA starting... (HEAD v4.1.4-5033996 from 2026-09-16 17:56:53 +1000)"

NORMAL = f"""{BANNER}

Outputs:
	trace level:                   1

===============================
...End Configuration Summary
===============================

Processed epoch #1 - GPS time: 2026 09 02 0 0 0.01 (data handling took 0.001 seconds)
Processed epoch #2 - GPS time: 2026 09 02 0 0 30.01 (data handling took 0.001 seconds)
Processed epoch #3 - GPS time: 2026 09 02 0 1 0.01 (data handling took 0.001 seconds)

⚠️ ⚠️ 下面这三行是 **RTS 反向**那一遍 —— 真 pea **每个历元打印两遍**。
   实测（真日志 2880 历元那次）：前向 2880 行、RTS 2879 行。
   ⚠️ 而历元数只认**前向**那种（带 `#`）：两种都数会**翻倍**，
   只数 RTS 会**少 1** —— 两处计数原来各犯了一个。
   （见 diagnostics._EPOCH 和 runner.py 里进度计数那段说明。）
Processed epoch - 2026-09-02 00:00:00.01 (took 00:00:00.00)
Processed epoch - 2026-09-02 00:00:30.01 (took 00:00:00.00)
Processed epoch - 2026-09-02 00:01:00.01 (took 00:00:00.00)

PEA started  processing at : 2026-09-18 04:19:44.06
and finished processing at : 2026-09-18 04:19:47.61
Total processing duration  : 00:00:03.54

PEA finished
"""

MISSING = (
    "Error: Missing antenna information (ANTEX file) file /data/products/igs20.atx\n"
    "Warning: Invalid SINEX file\n"
    "Dry-run failed: configuration is invalid\n"
    "PEA finished\n"
)

MUTATED = (
    "Warning: Epoch tolerance is greater than half the epoch interval. "
    "Adjusting to half the epoch interval.\n"
    "Dry-run successful: configuration and sanity checks passed\n"
    "PEA finished\n"
)

OPENBLAS = (
    "OpenBLAS Warning : Detect OpenMP Loop and this application may hang.\n"
    "Please use OPENBLAS_NUM_THREADS=1\n"
    "PEA finished\n"
)


# =============================================================================
# ① 正常输出不能被误报
# =============================================================================


def test_epoch_counting() -> None:
    section("④b ★★ 「处理了多少个历元」：两处计数必须一致，而且不能翻倍")

    # ⚠️⚠️ 真 pea **一个历元打印两遍**，两种写法：
    #
    #        前向解： Processed epoch #1234 - GPS time: 2022 03 01 0 0 30 (...)
    #        RTS 反向：Processed epoch - 2022-03-01 00:01:00.01 (took ...)
    #
    #    实测（真日志 2880 历元那次）：
    #        前向 = 2880   ← 真实历元数（和 .POS 里的行数一致）
    #        RTS  = 2879   ← 反向那一遍，少 1
    #
    #    ⚠️ 而两处计数各错各的：
    #       · `runner.py` 判的是子串 `"Processed epoch" in line` → **两种都数**
    #         → 计数**正好翻倍** → 进度条 `width:197%`、字面写"5760 个历元"
    #       · `diagnostics._EPOCH` 的正则要求紧跟短横 → 只认 RTS 那种 → **少 1**
    #
    #    👉 判据：**只认前向那种**（带 `#`），两处都这样。
    out = (
        f"{BANNER}\n"
        + "".join(
            f"Processed epoch #{i} - GPS time: 2022 03 01 0 0 {i} "
            f"(data handling took 0.001 seconds)\n"
            for i in range(50)
        )
        + "".join(
            f"Processed epoch - 2022-03-01 00:{i:02d}:00.01 (took 0.001 seconds)\n"
            for i in range(49)
        )
        + "PEA finished\n"
    )
    d = D.diagnose(out)
    check(
        d.epochs_processed == 50,
        f"★★ 数出来的是**前向**那 50 个（实际 {d.epochs_processed}）"
        "—— 99 是两种都算了（翻倍方向），49 是只算了 RTS（少 1）",
    )

    # ⚠️ 只有 RTS 那种写法时（前向被日志级别滤掉的极端情况），
    #    不该数成 0 —— 但也不该凭空多数。这里明确记录**当前的选择**：
    #    认不出来就是 0，而不是猜。
    only_rts = BANNER + "".join(
        f"Processed epoch - 2022-03-01 00:{i:02d}:00.01 (took 0.001 seconds)\n"
        for i in range(10)
    )
    check(
        D.diagnose(only_rts).epochs_processed == 0,
        "★ 只有 RTS 那种写法时数 0（**不猜**）—— 前向那种才是权威",
    )


def test_clean_run() -> None:
    section("① 正常输出：不能报出任何问题")

    # ⚠️ 这条是防"规则太宽"。
    #    假警告会让用户去查根本不存在的问题 —— 比漏报还浪费时间。
    d = D.diagnose(NORMAL)

    check(d.version.startswith("HEAD v4.1.4"), f"读出版本：{d.version[:24]}")
    check(d.epochs_processed == 3, f"数出 3 个历元（实际 {d.epochs_processed}）")
    check(d.finished, "识别出正常结束")
    check(not d.errors, f"没有误报错误（实际 {[x.kind for x in d.errors]}）")
    check(not d.warnings, f"没有误报警告（实际 {[x.kind for x in d.warnings]}）")
    check("正常完成" in d.headline, f"结论正确：{d.headline}")


# =============================================================================
# ② 缺文件
# =============================================================================


def test_missing_file() -> None:
    section("② 缺文件：要说清**缺的是哪个**")

    d = D.diagnose(MISSING)

    check(d.errors, "识别为错误")
    kinds = [x.kind for x in d.diagnostics]
    check("missing_file" in kinds, f"分类正确（{kinds}）")

    # ⚠️ 关键：解释里要带上**具体是哪个文件**。
    #    "缺少必需的文件" 对用户没用 —— 他要知道去弄哪一个。
    mf = next((x for x in d.diagnostics if x.kind == "missing_file"), None)
    check(mf is not None and "igs20.atx" in mf.what,
          f"指出了具体的文件：{mf.what[:60] if mf else '(没找到)'}")
    check(mf is not None and bool(mf.what_to_do), "给出了'怎么办'")

    check(d.finished, "注意：输出里有 'PEA finished'，但退出码是 1")


# =============================================================================
# ③ ⚠️ 配置被静默改写 —— 退出码还是 0
# =============================================================================


def test_config_mutated() -> None:
    section("③ 配置被改写：退出码 0，但必须报出来")

    # ⚠️ 这条是**这个模块存在的核心理由**。
    #
    #    Ginan 的 6 个 sanity checker 里 4 个会**静默改写配置**，
    #    只发 warning 级日志，退出码仍然是 0。
    #
    #    如果我们的诊断不报这个，用户拿到的是"处理成功"，
    #    而实际跑的已经不是他给的那份配置了。
    d = D.diagnose(MUTATED)

    check(d.finished, "识别出正常结束")
    check(not d.errors, "不是错误（退出码是 0）")
    check(len(d.warnings) == 1, f"报出 1 条警告（实际 {len(d.warnings)}）")

    w = d.warnings[0] if d.warnings else None
    check(w is not None and w.kind == "config_mutated", "分类正确")
    check("正常完成" not in d.headline, "结论里**不能**只说'正常完成'")
    check("注意" in d.headline, f"结论里提到了需要注意：{d.headline}")


# =============================================================================
# ④ OpenBLAS
# =============================================================================


def test_openblas() -> None:
    section("④ OpenBLAS 警告能被识别")

    d = D.diagnose(OPENBLAS)
    kinds = [x.kind for x in d.diagnostics]
    check("openblas_threads" in kinds, f"分类正确（{kinds}）")
    w = next((x for x in d.diagnostics if x.kind == "openblas_threads"), None)
    check(w is not None and "OPENBLAS_NUM_THREADS" in w.what_to_do,
          "建议里点了具体的环境变量名")
    # ⚠️ 但方向必须是「**别**设」——
    #
    #    这条守卫原来只查"建议里有没有那个变量名"，而当时的建议是
    #    照抄 OpenBLAS 自己那句话（`Please use OPENBLAS_NUM_THREADS=1`）。
    #    实测（2026-09-25）：按那句话设了之后进程**平均只占 1 个核**，
    #    不设能占 8 个、每历元快 1.6 倍以上 —— 因为 Ginan 那 20 个
    #    OMP 线程建了却在旁边看着。
    #
    #    ⇒ 一个只查"有没有提到那个变量"的守卫，**对建议的方向是瞎的**：
    #      把它从「设成 1」改成「设成 64」它也照样绿。所以补这一条。
    check(
        w is not None and ("不要" in w.what_to_do or "别" in w.what_to_do),
        "★ 而且明确说了**不要**去设那个变量 —— 照 OpenBLAS 自己的话做会只剩 1 个核",
    )


# =============================================================================
# ⑤ 兜底规则
# =============================================================================


def test_catchall() -> None:
    section("⑤ 兜底规则：不认识的错误也要报出来")

    # ⚠️ 兜底规则**必须在最后**。
    #    放前面的话所有具体规则都不会被触发 ——
    #    于是所有错误都显示成笼统的"出错了"，比没有兜底更糟：
    #    用户以为系统认识这个错误，实际那说明什么都没说。

    d = D.diagnose("Error: something completely unexpected happened\nPEA finished\n")
    check(d.errors, "不认识的错误也被报出来了")
    check(
        d.errors[0].kind == "unrecognized_error",
        f"用的是兜底分类（{d.errors[0].kind}）",
    )
    check("原文" in d.errors[0].what_to_do or "发" in d.errors[0].what_to_do,
          "告诉用户可以把原文发给开发者")

    # 具体规则优先于兜底
    d2 = D.diagnose(MISSING)
    check(
        any(x.kind == "missing_file" for x in d2.diagnostics),
        "有具体规则时不走兜底（说明兜底排在了最后）",
    )


# =============================================================================
# ⑥ 去重
# =============================================================================


def test_dedup() -> None:
    section("⑥ 同一个问题报很多次时只显示一条")

    # ⚠️ pea 可能每个历元都打印一次同样的警告。
    #    2880 个历元就是 2880 条 —— 用户只需要知道"有这个问题"。
    many = "\n".join(
        "Warning: Epoch tolerance is greater than half the epoch interval."
        for _ in range(50)
    ) + "\nPEA finished\n"

    d = D.diagnose(many)
    check(len(d.warnings) == 1, f"50 条相同警告去重成 1 条（实际 {len(d.warnings)}）")


def test_version_short() -> None:
    section("⑦ 版本号要压短")

    full = "HEAD v4.1.4-5033996 from 2026-09-16 17:56:53 +1000"
    check(D.short_version(full) == "v4.1.4", f"压成 {D.short_version(full)}")
    check(D.short_version("") == "", "空字符串不崩")
    check(D.short_version("没有版本号") == "没有版本号", "没有版本号时原样返回")


# =============================================================================
# 主程序
# =============================================================================


# ⚠️ 真实的、**带 ANSI 颜色码**的输出片段（从实测日志里抄的）。
#
#    ⚠️ 行首那个 `\x1b[1;93m` 是**探针**：
#       如果哪天有人把 strip_ansi 去掉，下面的断言立刻变红。
REAL_ANSI_WARNINGS = (
    "\x1b[1;93mWarning: Antenna name not specified for DCMS\x1b[0m\n"
    "\x1b[1;93mWarning: Antenna details not found for DCMS\x1b[0m\n"
    "\x1b[1;93mWarning: No receiver apriori position found for DCMS\x1b[0m\n"
    "\x1b[1;93mWarning: Apriori for DCMS is 2067.22m from SPP estimate\x1b[0m\n"
    "\x1b[1;93mWarning: tropSAAS: hgt > 11000m, setting it to 11000m\x1b[0m\n"
    "\x1b[1;93mWarning: Code measurement not available on secondary frequency "
    "for G01 at DCMS, falling back to single-frequency\x1b[0m\n"
    "\x1b[1;93mWarning: Invalid SINEX file\x1b[0m\n"
    # ⚠️ 下面这条是**探针** —— 见 test_ansi_stripped 里的说明。
    #    它只能被**锚定的兜底规则**接住，所以它才是"ANSI 去没去掉"的试金石。
    "\x1b[1;93mWarning: 一条我们从没见过的警告\x1b[0m\n"
    "PEA finished\n"
)


def test_ansi_stripped() -> None:
    section("⑧ ★ 带颜色码的输出必须照样能解析")

    # ⚠️⚠️ 这是**最隐蔽的一个 bug**，值得把经过说清楚。
    #
    #    pea 的输出带 ANSI 颜色码：
    #
    #        '\x1b[1;93mWarning: Antenna name not specified for DCMS\x1b[0m'
    #          ↑ 行首是转义符，不是 'W'
    #
    #    而**唯一的那条警告规则**当时写的是 `^Warning:` —— 锚定在行首。
    #    于是它**一条都匹配不上**：实测那次运行里 227 条警告全被漏掉，
    #    而结论报的是"**处理正常完成**"。
    #
    #    ⚠️ 这个 bug 特别难发现，因为
    #       **解析失败的默认表现是"什么都没找到"** ——
    #       和"这次运行真的没问题"**长得一模一样**。
    #       没有报错、没有异常，只是安静地漏掉了一切。
    #
    #    ⚠️⚠️ **一个容易写错的地方（我自己第一版就写错了）**：
    #
    #       后来加的那些具体规则用的是 `search` 且**不带锚点**
    #       （`Antenna ...`、`tropSAAS:...`），所以**带不带颜色码都能匹配**。
    #       拿它们当"去色有没有生效"的判据是**测不出来**的 ——
    #       我第一版就是这么写的，把 strip_ansi 删掉测试照样全绿。
    #
    #       👉 所以这里专门放了一条**只能被锚定规则接住**的警告
    #          （fixture 里那条"从没见过的警告"）。
    #          它才是真正的试金石。
    check(
        D.strip_ansi("\x1b[1;93mWarning: x\x1b[0m") == "Warning: x",
        "strip_ansi 去掉了颜色码",
    )

    d = D.diagnose(REAL_ANSI_WARNINGS)
    check(
        len(d.diagnostics) > 0,
        f"★ 带颜色码的警告也能被看到（抓到 {len(d.diagnostics)} 条）",
    )
    check(
        "正常完成" not in d.headline,
        f"★ 结论不再说'正常完成'：{d.headline}",
    )
    check(
        bool(d.warnings),
        f"★ 被归类成警告而不是错误（{len(d.warnings)} 条）",
    )

    # ⚠️ 这条才是真正的探针：**锚定的兜底规则**必须也能匹配带色行。
    unknown = [x for x in d.diagnostics if x.kind == "other_warning"]
    check(
        any("从没见过" in x.raw for x in unknown),
        "★ 锚定的兜底规则也能接住带颜色码的行（去色真的生效了）",
    )


def test_specific_warning_rules() -> None:
    section("⑨ ★ 具体警告要**各归各类**，不能被笼统规则吃掉")

    # ⚠️ 原来那条笼统规则排在最前面、模式是 `^Warning:`，
    #    解释还写死成"Ginan 自动调整了配置里的某些值"。
    #
    #    于是：天线没填 → "配置被改了"；先验坐标错 2 公里 → "配置被改了"；
    #    回退到单频 → "配置被改了"。
    #
    #    ⚠️ **一个笼统的规则会把所有具体问题吃掉** ——
    #       而且它给的是一个**自信的、错误的**解释，
    #       比"我不知道这是什么"更糟：用户会照着错误的方向去查。
    d = D.diagnose(REAL_ANSI_WARNINGS)
    kinds = {x.kind for x in d.diagnostics}

    for want, desc in [
        ("missing_antenna", "天线没填（→ 没做相位中心改正）"),
        ("bad_apriori_position", "先验坐标不对"),
        ("bad_tropo_height", "对流层高度离谱"),
        ("single_frequency_fallback", "回退到单频"),
        ("invalid_sinex", "SINEX 无效"),
    ]:
        check(want in kinds, f"★ 认出了「{desc}」（{want}）")

    check(
        "config_mutated" not in kinds,
        "★ 这些**没有**被误报成'配置被改写'",
    )

    # ⚠️ 每条都要说清"该怎么办" —— 只说"有问题"等于没说。
    for x in d.diagnostics:
        if x.kind in ("missing_antenna", "bad_apriori_position"):
            check(bool(x.what_to_do), f"「{x.kind}」给了下一步怎么办")

    # ⚠️ 认不出来的那些要**如实说不认识**，而不是硬套一个解释。
    #
    #    ⚠️ 注意通用格式化会把带 `what` 命名组的规则改写成
    #       "Ginan 提示：<原文>" —— 所以兜底给的就是原文本身，
    #       这比硬套一个"配置被改了"诚实得多。
    others = [x for x in d.diagnostics if x.kind == "other_warning"]
    check(len(others) == 1, f"认不出的落到兜底（{len(others)} 条）")
    check(
        all(x.raw for x in others),
        "★ 兜底保留了原文（不认识也要有据可查）",
    )
    check(
        all("Ginan 提示" in x.what or "警告" in x.what for x in others),
        "兜底只说「这里有一条我没见过的输出」，不硬套解释",
    )


def test_warning_catchall_is_last() -> None:
    section("⑩ ★ 兜底规则必须排在最后（顺序就是优先级）")

    # ⚠️ 规则是**按顺序匹配、一行只取第一条**的。
    #    兜底排前面 → 所有具体规则都不会被触发。
    #
    #    实测：`^Warning:` 那条兜底原来在**第一位**，
    #    于是 227 条警告全被它吃掉、全报成"配置被改了"。
    kinds = [r.kind for r in D.RULES]
    check(
        kinds[-1] == "other_warning",
        f"警告兜底在最后一位（实际倒数第一是 {kinds[-1]}）",
    )
    check(
        kinds[-2] == "unrecognized_error",
        f"错误兜底在倒数第二（实际 {kinds[-2]}）",
    )
    check(
        "config_mutated" not in kinds[:5],
        "★ '配置被改写'不再排在前面抢匹配",
    )


# =============================================================================
# ★ 真实输出行 —— 每条规则都必须接得住**它真正要接的那一行**
# =============================================================================

# ⚠️⚠️ 这张表是这次补规则时**逐字从 TRACE 里抄出来的**（括号里是实测出现次数，
#     数据来源：ginan-testdata-*/ 里 141 个真实日志的抽样审计）。
#
# 它守的是一类**特别隐蔽**的失效：规则写下了、看起来覆盖了、其实**从来没匹配上**。
#
#     实测教训：`large_residuals` 原来的 pattern 是
#         `(large residual|residual.*exceed)`
#     —— 那是**凭印象**写的措辞。而 Ginan 真正打印的是
#         `LARGE MEAS    ERROR OF : ...`
#     **一个 "residual" 字样都没有。** 于是这条规则从写下那天起就没生效过，
#     而那些行落进了兜底的 `unrecognized_error`，被判成 **error**。
#
#     👉 一条不匹配的规则比没有规则更糟：它让人以为这里已经有人看过了。
#
# ⚠️ 表里的行**带 ANSI 颜色码**，和真实日志一样 —— 因为 `diagnose` 会先
#    `strip_ansi`，而"先剥再匹配"这个顺序本身就是被测的一部分。
_REAL_LINES: list[tuple[str, str, str]] = [
    # (实测行原文, 期望的 kind, 出处/次数)
    ("\x1b[1;93m2026-09-25 12:34:56.789\tLARGE MEAS    ERROR OF :"
     "     1234.5678\tAT   42 :\t PHAS_MEAS\t E14\tALIC\t    L1C\x1b[0m",
     "large_residuals", "第三种：LARGE MEAS ERROR OF"),
    ("2026-09-25 12:00:00.00\tLargest state error is :     0.0123\t"
     "AT 17 :\t     ORBIT\t L64",
     "large_residuals", "Largest state error is"),
    ("2026-09-25 12:00:00.00\tLargest  ref meas error:     12.3\t"
     "AT   1 :\tCODE_MEAS\t G12",
     "large_residuals", "Largest ref meas error"),
    ("\x1b[1;93mWarning: Epoch 1 has no observations\x1b[0m",
     "epoch_without_observations", "1948 次"),
    ("Warning: Receiver ZIML has no data for this epoch (reason=future_obs,"
     " source=E:/Code/x)",
     "receiver_epoch_gap", "约 10000 次"),
    ("*** ERROR radome2none(): string length is less then 15 characters"
     " received 0",
     "bad_rnax_header", "11 次"),
    ("Warning: receiver_options:global:models:ionospheric_components:"
     "iono_sigma_limit: is not a valid yaml option",
     "invalid_yaml_option", "7 次"),
    ("Warning: Max pre-fit filter iterations limit reached at"
     " 2026-09-25 12:00:00.00 in /MINCON_TRANSFORM, limit is 10",
     "filter_iterations_limit", "11 次"),
    ("Warning: No receiver apriori clock found for YARL",
     "no_apriori_clock", "4 次"),
    ("Warning: No sat clk found for G18.",
     "no_sat_clock", "1 次"),
    ("Warning: Failed to get ERP at 2025-06-15 00:00:00.01.",
     "erp_missing", "3 次"),
    ("Warning: Looking for precise position, but no precise ephemerides"
     " found for E03",
     "no_precise_ephemeris", "1 次"),
    ("Warning: Start epoch not configured, rounding down first obs time"
     " 2026-09-25 00:00:00.00, epoch_interval=30",
     "start_epoch_rounded", "1 次"),
    ("Error: none is not a valid entry for option: @ mode.",
     "invalid_option_value", "10 次"),
    ("Warning: No JPL ephemeris file found.",
     "no_jpl_ephemeris", "6 次"),
    ("\x1b[1;93mWarning: Max yaw rate not found for G061 in satYawGpsIIR,"
     " check sinex files for '+SATELLITE/YAW_BIAS_RATE' block\x1b[0m",
     "yaw_bias_missing", "6 次"),
    ("\x1b[101mError: INNOVATION\x1b[0m",
     "innovation_error", "10 次（原文就这三个词）"),
    ("Warning: ionospheric_components:outage_reset_limit < epoch_interval,"
     " but it probably shouldnt be",
     "config_sanity_hint", "3 次"),
]


def test_real_lines_are_recognized() -> None:
    section("★ 真实输出行：每条规则都要接得住它真正要接的那一行")

    import ginan_agent.ginan.diagnostics as D

    for line, want, note in _REAL_LINES:
        d = D.diagnose(line)
        kinds = [x.kind for x in d.diagnostics]
        check(
            kinds[:1] == [want],
            f"★ {want:26s} ← {note}（实际 {kinds}）",
        )

    # ⚠️ 反面：这几行**不许**落进兜底。
    #    兜底那一类的 `what` 只是套话，而 error 级的兜底还会改写 headline。
    fallbacks = {"unrecognized_error", "other_warning"}
    for line, want, _ in _REAL_LINES:
        kinds = {x.kind for x in D.diagnose(line).diagnostics}
        check(
            not (kinds & fallbacks),
            f"  {want} 没有被兜底吃掉",
        )


def test_routine_messages_are_not_errors() -> None:
    section("★★ 常规消息不许判成 error —— 它会把结论改成「处理没有完成」")

    # ⚠️⚠️ 这是这次最要紧的一条。实测（141 个真实日志的抽样）：
    #
    #     `Warning: Epoch 1 has no observations`        1948 次
    #     `Receiver XXX has no data for this epoch`     约 10000 次
    #
    #    两者都是**正常输出**：前者对"不需要观测的工作流"（广播星历转轨道）
    #    每个历元都会打；后者在多站网解里是每个站的每个缺数据历元都打。
    #
    #    而 `no_observations` 规则原来是 error 级、pattern 又宽到能接住它们，
    #    于是结论变成「处理没有完成 —— 在这段时间里没有找到可用的观测数据。」
    #    **实测 25 个触发它的文件，25 个都真的处理了历元**（最多 25,200 个）。
    import ginan_agent.ginan.diagnostics as D

    for line, kind, note in [
        ("Warning: Epoch 1 has no observations", "epoch_without_observations",
         "1948 次"),
        ("Warning: Receiver ZIML has no data for this epoch (reason=future_obs)",
         "receiver_epoch_gap", "约 10000 次"),
    ]:
        d = D.diagnose(line)
        got = [(x.kind, x.severity) for x in d.diagnostics]
        check(
            got[:1] == [(kind, "info")],
            f"★ {note} 的那一行是 info（实际 {got}）",
        )
        check(not d.blocking, f"  而且不参与 blocking：{d.headline}")
        check(
            "没有完成" not in d.headline,
            f"★★ 结论里不许出现「没有完成」：{d.headline}",
        )

    # 对照：真正致命的写法（带 `Error:` 前缀）**必须**还是 error
    fatal = D.diagnose("Error: no observations for this run")
    check(
        bool(fatal.blocking) and fatal.blocking[0].kind == "no_observations",
        f"对照：`Error: no observations...` 仍然是 error（{fatal.headline}）",
    )


def test_finished_run_is_not_reported_as_failed() -> None:
    section("★★ 跑完了的运行，结论不许说「没有完成」")

    # ⚠️ 判据的顺序就是结论的正确性。
    #    `headline` 原来**先看 blocking**，于是日志里只要有一条 error 级诊断，
    #    结论就是"处理没有完成" —— 而 `finished` 就躺在同一个对象里。
    #
    #    实测：`fit_exp/fit_6h/stdout.txt` 处理了 **71 个历元**、
    #    正常打到结束标志，结论却是「处理没有完成 ——
    #    在这段时间里没有找到可用的观测数据。」
    import ginan_agent.ginan.diagnostics as D

    log = "\n".join([
        "PEA starting... (HEAD v4.1.4-5033996 from ...)",
        "Warning: Epoch 1 has no observations",
        *[f"Processed epoch #{i} - GPS time: 2025 06 15 0 0 30 (took 0.1s)"
          for i in range(1, 72)],
        "Error: something unrecognized happened",
        "PEA finished",
    ])
    d = D.diagnose(log)
    check(d.finished, "识别到结束标志")
    check(d.epochs_processed == 71, f"数出 71 个历元（实际 {d.epochs_processed}）")
    check(bool(d.errors), "那条 error 仍然被报出来（不是把它藏了）")
    check(
        "没有完成" not in d.headline,
        f"★★ 但结论不说「没有完成」：{d.headline}",
    )
    check("71" in d.headline, f"而且把历元数说出来：{d.headline}")

    # 对照：**真的没跑完**时必须照实说
    crashed = D.diagnose("\n".join([
        *[f"Processed epoch #{i} - GPS time: 2025 06 15 0 0 30" for i in range(1, 10)],
        "Error: bad_alloc",
    ]))
    check(
        "没有完成" in crashed.headline or "中途停了" in crashed.headline,
        f"对照：没跑到结束就说没跑完（{crashed.headline}）",
    )


def main() -> int:
    print("=" * 60)
    print("诊断自检 —— 读懂 pea 的输出")
    print("=" * 60)
    print("⚠️ 不联网、不跑进程。用的是从真实输出里抄来的片段。")

    test_epoch_counting()
    test_clean_run()
    test_missing_file()
    test_config_mutated()
    test_openblas()
    test_catchall()
    test_dedup()
    test_version_short()
    test_ansi_stripped()
    test_specific_warning_rules()
    test_warning_catchall_is_last()
    test_real_lines_are_recognized()
    test_routine_messages_are_not_errors()
    test_finished_run_is_not_reported_as_failed()

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
