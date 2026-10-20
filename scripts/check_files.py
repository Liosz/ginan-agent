"""自检：文件识别与下载。

跑法：python scripts/check_files.py

============================ 不联网 ============================
下载器最难测的部分是"网络出错时怎么办"，而**制造真的网络错误是不可能的**
（你没法让 BKG 恰好在传到第 3 个 chunk 时断开）。

所以用假 session —— 按剧本表演各种故障。这是唯一能把异常路径逼出来的办法。

============================ 一条重要的回归 ============================
这里有一个针对**真实踩过的坑**的测试：

    BKG 上是 .gz 压缩文件，下载器原样存成了没有 .gz 后缀的名字。
    然后识别、校验和、落盘**全部成功** —— 因为识别内部会解压看内容。

    "看起来成功"和"真的能用"是两回事。

所以下面专门断言：**下载后的文件必须是解压过的明文**。
"""

from __future__ import annotations

import gzip
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ginan_agent.ginan import fetch, identify  # noqa: E402
from ginan_agent.ginan.catalog import FilePlan  # noqa: E402
from tests.fakes import heads as _heads  # noqa: E402

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
# 真实的文件开头（从实测下载的文件里摘的）
# =============================================================================

# ⚠️ 真实文件的头部字节搬到了 `tests/fakes/heads.py` —— 因为
#    **另一个自检脚本（check_server）现在也要用**。
#    各存一份的话，改了一处忘了另一处，而"两边不一致"的后果是
#    **一边认得出来、另一边认不出来**。
#    每个头部**来自哪个文件**写在那个文件里。
REAL_HEADS = _heads.REAL_HEADS


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


def _obs_head(
    station: str, year: int, month: int, day: int, *, with_time: bool = True
) -> bytes:
    """一个**贴真实**的 RINEX 3 观测头部（定宽，标签在第 60 列）。

    ⚠️⚠️ `with_time` 这个参数是用来钉住一条**取舍**的，见下面 ④d。

    为什么非要有 `TIME OF FIRST / LAST OBS`：
        `find_local_obs()` 判"这份文件是哪一天的"，靠的就是这两行。
        而 2026-09-21 之前，这一节**所有夹具都没有它们** ——
        于是"按天找"这条路一次都没被走到，而它坏掉的样子是
        **拿别的日子的数据去算，而且不报错**。
    """
    pad = (station + " " * 60)[:60].encode("ascii")

    def lab(data: str, label: str) -> bytes:
        return f"{data:<60}{label:<20}\n".encode("ascii")

    stamp = f"{year} {month:5d} {day:5d}"
    out = (
        lab("     3.04           O: OBSERVATION DATA    M: MIXED",
            "RINEX VERSION / TYPE")
        + pad + lab("", "MARKER NAME")
    )
    if with_time:
        out += lab(f"{stamp}     0     0    0.0000000     GPS", "TIME OF FIRST OBS")
        out += lab(f"{stamp}    23    59   30.0000000     GPS", "TIME OF LAST OBS")
    return out + lab("", "END OF HEADER")


# =============================================================================
# ① 识别：靠内容
# =============================================================================


def test_identify_by_content() -> None:
    section("① 识别靠内容，不靠文件名")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        for kind, head in REAL_HEADS.items():
            # ⚠️ 故意用**完全错误**的文件名。
            #    如果识别依赖文件名，这些测试全都会失败。
            p = write(tmp, "随便起的名字.bin", head)
            got = identify.identify(p)
            check(
                got.kind == kind,
                f"{kind:6} 被认出来了（文件名是假的）→ {got.label}",
            )

        # 认不出来时要如实说，不能猜
        #
        # ⚠️ bytes 字面量只能放 ASCII —— 中文字符要写成转义。
        #    （踩过一次：直接写中文进了 bytes，SyntaxError。）
        p = write(tmp, "mystery.bin", b"\x00\x01\x02\x03 unknown content here")
        got = identify.identify(p)
        check(not got.known, "认不出来时返回 unknown（不瞎猜）")
        check(bool(got.evidence), "认不出来时给出了线索（开头是什么）")


def test_rinex_version_variants() -> None:
    section("①b ★ RINEX 各种版本的写法都要认（一个编造的夹具曾掩盖了 bug）")

    # ⚠️⚠️ 这一节记录一个**很值得记的教训**。
    #
    #    原来的 REAL_HEADS["nav"] 长这样：
    #
    #        b"     3.04           NAVIGATION DATA     M ...RINEX VERSION / TYPE"
    #                            ^^^^^^^^ 3.x 的版本号 + 2.x 的类型串
    #
    #    而注释上写着"**真实的**文件开头（从实测下载的文件里摘的）"。
    #
    #    **但那是个不存在的组合。** 真实的 RINEX 3.x 导航文件写的是
    #    `N: GNSS NAV DATA`（3.x 把类型挪到了第 20 列，正文改成 "NAV DATA"）；
    #    `NAVIGATION DATA` 是 2.x 的写法。
    #
    #    于是：代码只匹配 "NAVIGATION DATA"、测试夹具也写成 "NAVIGATION DATA",
    #    **两边共享同一个误解，测试就给这个 bug 盖了章。**
    #
    #    后果不只是"认不出来"——文件其实**下载成功了**，
    #    但识别说"内容对不上"，下载器据此**把它丢弃**。
    #    现象是"广播星历下载失败"，根因却在读文件的那几行代码里。
    #
    #    👉 教训两条：
    #       ① **夹具要么是真的，要么标清楚是构造的** ——
    #          写着"实测"却是编的，比没有夹具更糟。
    #       ② **测试不能只测"我们想到的写法"**，要覆盖真实世界的各种变体 ——
    #          否则它守住的只是我们自己的想象。
    cases = [
        # (说明, 头部字节, 期望认成什么, 这字节的来源)
        (
            "RINEX 3.x 导航（IGS 标准写法）",
            b"     3.04           N: GNSS NAV DATA    M: MIXED            "
            b"RINEX VERSION / TYPE\n",
            "nav",
            "实测下载（BKG 的 BRDC00IGS_R_20260010000_01D_MN.rnx）",
        ),
        (
            "RINEX 2.x 导航",
            b"     2.11           NAVIGATION DATA     G (GPS)             "
            b"RINEX VERSION / TYPE\n",
            "nav",
            "RINEX 2 规范里的写法",
        ),
        (
            "RINEX 3.x 观测（IGS 标准写法）",
            b"     3.04           O: OBSERVATION DATA    M: MIXED         "
            b"RINEX VERSION / TYPE\n",
            "obs",
            "RINEX 3 规范里的写法",
        ),
        (
            "RINEX 3.x 观测（转换器写法，没有 O: 前缀）",
            b"     3.04           OBSERVATION DATA    M: Mixed            "
            b"RINEX VERSION / TYPE\r\n",
            "obs",
            "用户自己的 DCMS 文件（RinexConvert 1.5.2 生成）",
        ),
    ]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for desc, head, want, origin in cases:
            got = identify.identify(write(tmp, "x.bin", head))
            check(
                got.kind == want,
                f"{desc} → {got.kind}   〔字节来源：{origin}〕",
            )


def test_identify_compressed() -> None:
    section("② 压缩态也能识别")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        raw = REAL_HEADS["sp3"]
        gz = gzip.compress(raw)
        p = write(tmp, "s.gz", gz)

        got = identify.identify(p)
        check(got.kind == "sp3", "gzip 里的 SP3 被认出来")
        check(got.compression == "gzip", "压缩类型被标出来")


def test_identify_edge_cases() -> None:
    section("③ 边界：空文件、不存在、二进制垃圾")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        check(not identify.identify(tmp / "不存在").known, "不存在的文件不崩")
        check(not identify.identify(write(tmp, "空", b"")).known, "空文件不崩")
        check(
            not identify.identify(write(tmp, "垃圾", bytes(range(256)) * 4)).known,
            "二进制垃圾不崩",
        )


def test_marker_from_header() -> None:
    section("④ 测站名从文件头读（不猜文件名）")

    # ⚠️ 这个样本是**构造的**，不是实测下载的 —— 标出来，别冒充。
    #
    #    为什么不用 REAL_HEADS["obs"]？因为**手头没有一个"头部有站名的真实文件"**：
    #      · 用户那份 DCMS 文件的 MARKER NAME 是**空的**（60 个空格）
    #      · 实测下载的示范文件是 nav，它本来就没有测站名
    #
    #    所以这里用一个**符合 RINEX 3 规范**的最小样本 —— 它测的是
    #    "解析器会不会去读头部"，不是"能不能认出真实文件的每一个字节"。
    #    两件事分开，样本的来源才说得清楚。
    #
    #    ⚠️ 顺带一提：RINEX 里站名的宽度是 60 字符左对齐，
    #       4 位站号 + 2 位标志 + 3 位国家码（如 `ALIC00AUS`）。
    obs_with_marker = (
        b"     3.04           O: OBSERVATION DATA    M: MIXED            "
        b"RINEX VERSION / TYPE\r\n"
        b"ALIC00AUS                                                   "
        b"MARKER NAME\r\n"
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # ⚠️ 文件名是 abc123，但头里写的是 ALIC00AUS
        p = write(tmp, "abc123_乱七八糟.rnx", obs_with_marker)
        got = identify.identify(p)
        check(got.kind == "obs", "认出来了是观测数据")
        check(got.marker == "ALIC00AUS", f"测站名取自文件头：{got.marker}")
        check("abc123" not in got.marker, "没有从文件名里猜")

    # ⚠️ 反过来也要测：**头里没有站名时，不能瞎编一个出来**。
    #    这正是用户那份文件的真实情况 —— 而"编一个站名"会让程序
    #    理直气壮地去用错的数据。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        empty = (
            b"     3.04           OBSERVATION DATA    M: Mixed            "
            b"RINEX VERSION / TYPE\r\n"
            b"                                                            "
            b"MARKER NAME\r\n"
        )
        p = write(tmp, "DCMS_r_2026001.rnx", empty)
        got = identify.identify(p)
        check(got.kind == "obs", "空站名的文件照样认得出是观测数据")
        check(
            not got.marker,
            f"★ 头里没站名时 marker 是空的（不瞎猜，实际={got.marker!r}）",
        )


def test_known_vs_is_data() -> None:
    section("④b ⚠️「认出来了」不等于「是可用的数据」")

    # ⚠️ 这两个属性能混用，代价很大。
    #
    #    踩过一次：收件箱搬运用的是 `known` ——
    #    于是一个网页文件（kind == "html"，认出来了）被**当数据认领搬走了**。
    #    搬进数据目录之后，Ginan 读到它会报一个完全无关的错。

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        html = write(tmp, "x", b"<!DOCTYPE html><html><body>404 Not Found")
        got = identify.identify(html)
        check(got.kind == "html", "网页被识别为 html（不是一个笼统的 unknown）")
        check(got.known, "它算「认出来了」")
        check(not got.is_data, "★ 但它不是数据 —— is_data 必须为 False")

        login = write(tmp, "y", b'<!DOCTYPE html><title>Earthdata Login</title>')
        got2 = identify.identify(login)
        check(not got2.is_data, "登录页也不是数据")
        check("登录" in got2.label, f"标签点明了是登录页（{got2.label}）")

        notfound = write(tmp, "z", b'<!DOCTYPE html><h1>404 Not Found</h1>')
        got3 = identify.identify(notfound)
        check("404" in got3.label, f"404 页和登录页**区分开**（{got3.label}）")

        sp3 = write(tmp, "w", REAL_HEADS["sp3"])
        got4 = identify.identify(sp3)
        check(got4.known and got4.is_data, "真正的数据文件 is_data 为 True")


def test_rinex_header() -> None:
    section("④c RINEX 头部解析 —— 判断用户的数据能不能用")

    from ginan_agent.ginan import rinex

    # ⚠️ 这个模块的职责是**在跑之前**发现"这份数据做不了 PPP"。
    #    最要紧的是**单频** —— 它是**静默失败**：
    #    程序能跑完、不报错、结果是错的。

    DUAL = (
        b"     3.04           OBSERVATION DATA    M                   "
        b"RINEX VERSION / TYPE\n"
        b"MYST00CHN                                                   MARKER NAME\n"
        b"                         TWIVC6050       NONE                ANT # / TYPE\n"
        b"  2026     9     2     0     0    0.0000000     GPS         TIME OF FIRST OBS\n"
        b"G   12 C1C L1C C2W L2W S1C S2C                              "
        b"SYS / # / OBS TYPES\n"
        b"END OF HEADER\n"
        b"> 2026 09 02 00 00  0.0000000  0 12\n"
        b"> 2026 09 02 00 00 30.0000000  0 12\n"
    )

    SINGLE = (
        b"     3.04           OBSERVATION DATA    M                   "
        b"RINEX VERSION / TYPE\n"
        b"MYST00CHN                                                   MARKER NAME\n"
        b"  2026     9     2     0     0    0.0000000     GPS         TIME OF FIRST OBS\n"
        b"G    4 C1C L1C S1C D1C                                      "
        b"SYS / # / OBS TYPES\n"
        b"END OF HEADER\n"
    )

    # ⚠️⚠️ **RINEX 2**（`# / TYPES OF OBSERV`）—— 观测码是**两位**的。
    #
    #    上面那两份夹具都是 RINEX 3.04（`C1C L1C …`），所以这条一直没被守过。
    #    而 RINEX 2 是**用户最可能拿到的第一种文件** ——
    #    测站短名 `alic0600.22o` 就是它。
    #
    #    ⚠️ 当时的 bug：判频段的正则要求**三位**（`^[CL](\d)(\w)$`），
    #       `L1` 匹配不上 → 频段集合永远是空 → 判成"单频"→
    #       用户拿一份完全正常的双频文件，被告知「做不了 PPP」。
    DUAL2 = (
        b"     2.11           OBSERVATION DATA    M (MIXED)           "
        b"RINEX VERSION / TYPE\n"
        b"MYST00CHN                                                   MARKER NAME\n"
        b"     9    C1    L1    D1    P2    L2    D2    C2    S1    S2"
        b"# / TYPES OF OBSERV \n"
        b"  2022     3     1     0     0    0.0000000     GPS         "
        b"TIME OF FIRST OBS\n"
        b"END OF HEADER\n"
        b" 22  3  1  0  0  0.0000000  0  1G01\n"
        b" 22  3  1  0  0 30.0000000  0  1G01\n"
    )
    # 单频的 RINEX 2 —— 加了它才说明"认识两位码"没有**矫枉过正**
    # （一律判成双频是另一个错，而且是更危险的那个）
    SINGLE2 = (
        b"     2.11           OBSERVATION DATA    M (MIXED)           "
        b"RINEX VERSION / TYPE\n"
        b"MYST00CHN                                                   MARKER NAME\n"
        b"     4    C1    L1    D1    S1                              "
        b"# / TYPES OF OBSERV \n"
        b"  2022     3     1     0     0    0.0000000     GPS         "
        b"TIME OF FIRST OBS\n"
        b"END OF HEADER\n"
        b" 22  3  1  0  0  0.0000000  0  1G01\n"
    )

    COMPRESSED = b"\x1f\x8b\x08\x00\x00\x00\x00\x00"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        p = write(tmp, "a.rnx", DUAL)
        info = rinex.inspect(p)
        check(info.marker == "MYST00CHN", f"读到测站名：{info.marker}")
        check(info.antenna.startswith("TWIVC6050"), f"读到天线：{info.antenna}")
        check(info.dual_frequency, "双频数据判定为可用")
        check(info.ok, f"没有报问题（{info.problems}）")

        # ⚠️ 单频必须被拦住
        p2 = write(tmp, "b.rnx", SINGLE)
        info2 = rinex.inspect(p2)
        check(not info2.dual_frequency, "单频数据被识别出来")
        check(not info2.ok, "单频被判为不可用")
        check(
            any("单频" in x for x in info2.problems),
            f"说明里点明了是单频：{info2.problems[0][:40] if info2.problems else ''}",
        )

        # ---- ⚠️⚠️ RINEX 2：观测码是两位，必须也认得出来 ----
        p3 = write(tmp, "alic0600.22o", DUAL2)
        info3 = rinex.inspect(p3)
        check(
            info3.dual_frequency,
            f"★★ **RINEX 2 的双频**也要判成双频"
            f"（频段：{ {c.system: sorted(c.bands) for c in info3.constellations.values()} }）",
        )
        check(
            info3.ok,
            f"★★ 而且不报问题（{info3.problems[:1]}）—— 原来这里报的是"
            "「这份数据是单频的，做不了 PPP」",
        )

        # ⚠️ 反过度修正：把正则放宽成"两位也认"之后，**单频的 RINEX 2
        #    必须仍然被判成单频**。不然就是从"误杀双频"变成"放过单频"，
        #    而后者更危险 —— 单频数据能跑完、不报错、结果是错的。
        p4 = write(tmp, "single.22o", SINGLE2)
        info4 = rinex.inspect(p4)
        check(
            not info4.dual_frequency,
            f"★ RINEX 2 的**单频**仍然判单频（频段："
            f"{ {c.system: sorted(c.bands) for c in info4.constellations.values()} }）",
        )
        check(not info4.ok, "★ 单频的 RINEX 2 也仍然不可用")

        # ⚠️ 压缩的文件要给出"先解压"的提示，而不是"格式不对"
        p3 = write(tmp, "c.rnx", COMPRESSED)
        info3 = rinex.inspect(p3)
        check(not info3.ok, "压缩文件被判为不可用")
        check(
            any("解压" in x for x in info3.problems),
            "说明了需要先解压（而不是说'格式不对'）",
        )

        # 不是 RINEX
        p4 = write(tmp, "d.bin", b"\x00\x01\x02 not a rinex file")
        info4 = rinex.inspect(p4)
        check(not info4.ok, "非 RINEX 文件被判为不可用")

    # ⚠️ 采样间隔和时长要能从**数据**里推 —— 很多文件头里没有这两个字段
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = write(tmp, "e.rnx", DUAL)
        info = rinex.inspect(p)
        check(info.interval == 30.0, f"从历元推算出采样间隔 30 秒（实际 {info.interval}）")


def test_malformed_header() -> None:
    section("④e 畸形头部不能让整次扫描失败")

    # ⚠️⚠️ 这条是**用户实际碰到的**：
    #     他的一个文件让 scan_inbox 抛了 IndexError，
    #     界面显示"扫描失败了" —— 而他不知道是哪个文件的问题，
    #     也不知道其他的能不能认。

    from ginan_agent.ginan import inbox, identify

    # ---- 精确复现那个 IndexError ----
    #
    # RINEX 的头部是定宽的，标签在第 60 列之后。
    # 所以这种"左边空、右边有标签"的行是**合法**的：
    #
    #     '                    MARKER NAME'
    #
    # 原来的代码守卫检查的是整行（非空），
    # 但取值用的是左边 60 列（空）—— 守卫没防住。
    #
    # > 守卫必须检查你实际要用的那个东西。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = write(tmp, "空标记名.rnx",
                  b"     3.04           OBSERVATION DATA    M                   "
                  b"RINEX VERSION / TYPE\n"
                  b"                                                            "
                  b"MARKER NAME\n")
        try:
            got = identify.identify(p)
            check(True, "空标记名不再抛异常")
            check(got.marker == "", f"返回空字符串（{got.marker!r}）")
        except Exception as exc:  # noqa: BLE001
            check(False, f"仍然抛异常：{type(exc).__name__}: {exc}")

    # ---- 一个坏文件不该让整批失败 ----
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "inbox"
        dest = Path(td) / "data"
        root.mkdir()
        dest.mkdir()
        (root / "坏的.rnx").write_bytes(
            b"     3.04           OBSERVATION DATA    M                   "
            b"RINEX VERSION / TYPE\n"
            b"                                                            "
            b"MARKER NAME\n"
        )
        (root / "好的.rnx").write_bytes(
            b"     3.04           OBSERVATION DATA    M                   "
            b"RINEX VERSION / TYPE\n"
            b"HOB200AUS                                                   "
            b"MARKER NAME\n"
        )

        try:
            r = inbox.claim(dest_dir=dest, root=root)
            check(True, f"整次扫描没失败：{r.summary}")
            check(r.scanned == 2, f"两个文件都被扫到了（{r.scanned}）")
            check(
                any(not m.skipped for m in r.moves),
                "好的那个文件仍然被正常处理",
            )
        except Exception as exc:  # noqa: BLE001
            check(False, f"整批失败：{type(exc).__name__}: {exc}")

    # ---- 空目录 / 不存在的目录 ----
    #
    # ⚠️⚠️ **`dest_dir` 必须给。** 不给的话 `claim()` 用它自己的默认值 ——
    #     `paths.data_dir()`，也就是**用户真实的数据目录**，并且会 `mkdir` 出来。
    #
    #     在开发机上这件事看不出来（那些目录本来就在，建了等于没建）；
    #     而在**干净克隆里跑**会当场现形：`check_all` 的总入口守卫
    #     报「☠ check_files.py 污染」—— 一次自检在别人机器上建了两个目录。
    #     （实测：2026-10-13，在一个刚克隆出来的副本里跑 check_all。）
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "dest"
        dest.mkdir()
        try:
            r = inbox.claim(dest_dir=dest, root=Path(td), dry_run=True)
            check(r.scanned == 0, f"空目录不崩：{r.summary}")
        except Exception as exc:  # noqa: BLE001
            check(False, f"空目录崩了：{exc}")

        try:
            r = inbox.claim(dest_dir=dest, root=Path(td) / "根本不存在", dry_run=True)
            check(r.scanned == 0, f"不存在的目录不崩：{r.summary}")
        except Exception as exc:  # noqa: BLE001
            check(False, f"不存在的目录崩了：{exc}")


def test_repair_and_antennas() -> None:
    section("④f 补头部 + 天线查询（让 agent 能自己处理'元数据全空'的文件）")

    # ⚠️ 现实问题：很多设备厂商的转换工具只转**观测数据**，元数据全空。
    #    用户"重新导出一遍"也没用 —— 工具就是不给。
    #
    #    而 RINEX 头部是纯文本定宽，补几行就行。

    from ginan_agent.ginan import atx, rinex

    HEADERLESS = (
        b"     3.04           OBSERVATION DATA    M                   "
        b"RINEX VERSION / TYPE\n"
        b"                                                            MARKER NAME\n"
        b"                                                            "
        b"REC # / TYPE / VERS\n"
        b"                                                            ANT # / TYPE\n"
        b" -2175411.7684  4401703.2661  4055053.0634                  "
        b"APPROX POSITION XYZ\n"
        b"  2026     1     1     0     0    0.0000000     GPS         "
        b"TIME OF FIRST OBS\n"
        b"G   16 C1C L1C C2W L2W S1C S2C                              "
        b"SYS / # / OBS TYPES\n"
        b"END OF HEADER\n"
        b"> 2026 01 01 00 00  0.0000000  0 12\n"
        b"G01  12345678.000\n"
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = write(tmp, "dcms26001000000.26O", HEADERLESS)

        # ---- 从文件名推测站号 ----
        check(
            rinex.infer_marker_from_filename(src) == "DCMS",
            "从 RINEX 2 风格的文件名推出站号 DCMS",
        )
        check(
            rinex.infer_marker_from_filename(Path("abc.rnx")) == "",
            "文件名里看不出站号时不瞎猜",
        )

        # ---- 修补前：能用的、能补的、要问的 ----
        before = rinex.inspect(src)
        check(before.ok, "数据本身没问题（不是致命的）")
        check(not before.ready, "但还不能直接跑")
        check(bool(before.fixable), f"列出了能自动补的（{len(before.fixable)} 项）")
        check(
            any("DCMS" in x for x in before.fixable),
            "指出站号能从文件名推",
        )
        check(
            any("天线" in x for x in before.needs_input),
            "指出天线型号需要用户提供",
        )

        # ---- 修补 ----
        dst = tmp / "DCMS_R_202601010000_01D_30S_MO.rnx"
        ok, msg = rinex.repair(
            src, dst, marker="DCMS00CHN",
            receiver="SINOGNSS", antenna="TRM59800.00 NONE",
        )
        check(ok, f"修补成功：{msg}")

        after = rinex.inspect(dst)
        check(after.marker == "DCMS00CHN", f"站号补上了：{after.marker}")
        check(
            after.antenna == "TRM59800.00 NONE",
            f"天线补上了：{after.antenna}",
        )
        check(after.ready, "补完之后 ready_to_run = True")

        # ---- ⚠️ 原文件必须原封不动 ----
        check(
            src.read_bytes() == HEADERLESS,
            "★ 原文件**一个字节都没变**（用户的野外数据不可再生）",
        )

        # ---- 观测数据部分逐字节一致 ----
        def body(p: Path) -> bytes:
            d = p.read_bytes()
            i = d.find(b"END OF HEADER")
            return d[d.find(b"\n", i) + 1:]

        check(
            body(src) == body(dst),
            "★ 观测数据部分逐字节一致（只改了头部）",
        )

    # ---- 天线查询 ----
    #
    # ⚠️ 型号必须和表里逐字一致，而用户手里的标签通常不带隔离罩代码。
    #    所以要有模糊搜索，把候选列出来让他挑。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fake_atx = write(tmp, "igs20.atx",
                         b"     1.4            M                                       "
                         b"ANTEX VERSION / SYST\n"
                         b"TRM59800.00     NONE                                        "
                         b"TYPE / SERIAL NO\n"
                         b"TRM59800.00     SCIS                                        "
                         b"TYPE / SERIAL NO\n"
                         b"LEIAR25.R3      NONE                                        "
                         b"TYPE / SERIAL NO\n"
                         b"BLOCK IIA           G01                 G032      1992-079A "
                         b"TYPE / SERIAL NO\n")

        items = atx.load(fake_atx)
        check(len(items) == 3, f"解析出 3 种接收机天线（实际 {len(items)}）")
        # ⚠️ 卫星天线用的是**同一个标签**，必须靠列位置排除
        check(
            all("BLOCK" not in a.name for a in items),
            "★ 卫星天线被正确排除（它们用的是同一个标签）",
        )
        check(
            any(str(a) == "TRM59800.00 NONE" for a in items),
            "接收机天线带上了隔离罩代码",
        )

        r = atx.search("TRM59800", fake_atx)
        check(len(r) == 2, f"模糊搜索找到 2 个（实际 {len(r)}）")
        check(r[0].name.startswith("TRM59800"), "最接近的排在前面")

        check(atx.search("不存在的东西", fake_atx) == [], "找不到时返回空列表")

    # ---- 天线表还没下载时不能崩 ----
    check(atx.load(Path("根本不存在的.atx")) == [], "表文件不存在时返回空列表")


def test_local_obs_detection() -> None:
    section("④d ★★ 本地已有的观测数据不该再下载 —— 而且要找**对那一天**")

    # ⚠️ 这一条是为了"用户自己的测站"：
    #    自建站的数据只在用户手里，去数据源下载必然 404，
    #    而那个报错看起来像网络问题。
    #
    # ⚠️⚠️ 2026-09-21 加的一半：**一个站有多天的时候，必须挑对那一天。**
    #
    #     原来的 `find_local_obs(station)` **不收日期** —— 于是它返回
    #     "随便哪一个"。实测（用户那份三天的数据，认领之后）：
    #
    #         data/ 里：ALIC.rnx(09-03) ALIC_2.rnx(09-02) ALIC_3.rnx(09-01)
    #         find_local_obs("ALIC")  → ALIC.rnx   ← 09-03
    #
    #     给"ALIC 09-02"做计划时，配置里被写成 `ALIC.rnx`（09-03 的观测）
    #     配上 09-02 的轨道/钟差 —— **配置看着完全正常**，
    #     跑出来的是拿错日子算的解，**没有任何地方会告诉用户**。
    #
    #     > 这是本项目最不能接受的那一类：**静默给错东西**。
    #
    #     ⚠️ 它藏了这么久，是因为这一节原来的夹具
    #        **一个站只有一个文件、而且没有观测时段** ——
    #        那正好是唯一不会暴露它的两种情形。
    from datetime import date as _date

    from ginan_agent.ginan import fetch

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # ⚠️ 文件名完全不标准 —— 必须靠**内容**里的测站名匹配
        write(tmp, "我的数据2026.rnx", _obs_head("MYST00CHN", 2026, 9, 2))

        found = fetch.find_local_obs("MYST", _date(2026, 9, 2), tmp)
        check(found is not None, "按文件头里的测站名找到了（文件名完全不标准）")
        check(found is not None and "我的数据" in found.name, f"找到的是 {found.name if found else ''}")

        check(
            fetch.find_local_obs("ALIC", _date(2026, 9, 2), tmp) is None,
            "别的站不会被误匹配",
        )

        # 占位文件名的反推
        check(fetch._station_from_filename("MYST_UNKNOWN.rnx") == "MYST", "从占位名反推测站")
        check(fetch._station_from_filename("X_UNKNOWN.rnx") == "", "太短的返回空")

    # =====================================================================
    # ★★★ 一个站三天 —— **这条才是抓 bug 的那条**
    # =====================================================================
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 文件名故意**看不出日期** —— 和认领之后的 `ALIC.rnx/ALIC_2/ALIC_3`
        # 一样，只有头里才知道是哪一天。
        for name, day in (("站数据.rnx", 1), ("站数据_2.rnx", 2), ("站数据_3.rnx", 3)):
            write(tmp, name, _obs_head("MYST00CHN", 2026, 9, day))

        got = {}
        for day in (1, 2, 3):
            f = fetch.find_local_obs("MYST", _date(2026, 9, day), tmp)
            got[day] = f.name if f else None
        check(
            got == {1: "站数据.rnx", 2: "站数据_2.rnx", 3: "站数据_3.rnx"},
            f"★★★ 三天各找各的，**不串**（实际 {got}）"
            "—— 串了就是拿错日子算，而且不报错",
        )
        check(
            len(set(got.values())) == 3,
            "★★★ 三天给出的是**三个不同的文件**"
            "（只收站名的那版会对三天都返回同一个）",
        )
        check(
            fetch.find_local_obs("MYST", _date(2026, 9, 9), tmp) is None,
            "★ 没有的那一天返回 None（让它去下载，而不是拿别的日子顶）",
        )

    # ---- 读不出观测时段的文件**不算命中** ----
    #
    # ⚠️ 这是一条**取舍**，写下来免得下一个人以为是漏了：
    #
    #     漏掉（判成没有）→ 程序去下载 → 找不到就如实说"缺这一天的观测"（**响的**）
    #     误收（判成有）  → 拿错日子的数据去算（**静默的**）
    #
    #   两者都错，但只有前者会有人来问。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        write(tmp, "没时间的.rnx",
              _obs_head("MYST00CHN", 2026, 9, 2, with_time=False))
        check(
            fetch.find_local_obs("MYST", _date(2026, 9, 2), tmp) is None,
            "★ 头里读不出观测时段的，**不敢当成那一天**（宁可让它去下载）",
        )


def test_local_obs_filename_fallback() -> None:
    section("④d-2 ★ 文件名兜底：头部没站名时按名字认（规则必须收紧）")

    # ⚠️ 为什么要这层兜底：用户自己那份 `dcms260101000000.rnx` 的头部
    #    **三个字段全空**（站名、天线、接收机）。只按头部匹配的话，
    #    这类文件永远找不到 —— 现象是"程序说没看到你的数据"，
    #    然后跑去网上找一个不存在的站、失败，排查方向被带偏。
    #
    # ⚠️⚠️ 但兜底是**猜**。猜错的代价不是报错，而是
    #     **用了别的站的数据，算出一个看起来完全正常的结果**。
    #     所以规则收得很紧，下面把边界全钉住。

    from datetime import date

    from ginan_agent.ginan import fetch

    # ---- 边界：什么名字算、什么不算 ----
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for name, expect, why in [
            ("dcms260101000000.rnx", True, "站号 + 数字日期（用户那种）"),
            ("DCMS_R_202512310000_01D_30S_MO.rnx", True, "站号 + 下划线"),
            ("DCMS.rnx", True, "正好四个字符"),
            ("DCMSX.rnx", False, "第 5 位是字母 → 不能当成 DCMS"),
            ("2026_DCMS_backup.rnx", False, "站号不在前四位 → 拒绝"),
            ("abc123_乱七八糟.rnx", False, "完全无关的名字"),
        ]:
            got = fetch._station_prefix_matches(tmp / name, "DCMS")
            check(
                got == expect,
                f"{'接受' if expect else '拒绝'} {name}  〔{why}〕",
            )

    # ---- 两级匹配：头部优先，名字兜底 ----
    #
    # ⚠️ 夹具换成 `_obs_head()` —— 它们原来**没有观测时段**，
    #    于是"按天找"那条路在这一节里一次都走不到（见 `_obs_head` 的说明）。
    EMPTY_HEADER = _obs_head("", 2026, 1, 1)

    def with_marker(station: str, day: int = 1) -> bytes:
        return _obs_head(station, 2026, 1, day)

    # ⚠️ 这一节测的是**站名匹配**，日期只要对得上就行，别让它变成干扰项。
    D = date(2026, 1, 1)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ① 头部空 → 名字兜底接住
        write(tmp, "dcms260101000000.rnx", EMPTY_HEADER)
        found = fetch.find_local_obs("DCMS", D, tmp)
        check(
            found is not None and found.name == "dcms260101000000.rnx",
            f"★ 头部没站名时，按**文件名**找到了（{found.name if found else '没找到'}）",
        )
        check(fetch.find_local_obs("ALIC", D, tmp) is None,
              "别的站不会因为兜底被误匹配")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ② ⚠️ 头里明确写了别的站 → 名字**不能**推翻它
        #
        #    名字说 DCMS、头里说 ALIC —— **头是权威的**。
        #    拿名字去推翻事实，就是用错数据的开始。
        write(tmp, "dcms_but_actually_alic.rnx", with_marker("ALIC00AUS"))
        check(
            fetch.find_local_obs("DCMS", D, tmp) is None,
            "★ 头里写了别的站时，名字兜底**不生效**",
        )
        check(
            fetch.find_local_obs("ALIC", D, tmp) is not None,
            "但按头里的站名照样能找到",
        )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ③ 头部有站名时**优先**用头部（即使文件名也对得上）
        write(tmp, "DCMS_a.rnx", with_marker("ALIC00AUS"))   # 头: ALIC
        write(tmp, "DCMS_b.rnx", with_marker("DCMS00CHN"))   # 头: DCMS
        found = fetch.find_local_obs("DCMS", D, tmp)
        check(
            found is not None and found.name == "DCMS_b.rnx",
            f"★ 头部匹配优先（找到 {found.name if found else '没找到'}）",
        )

    # ---- ⚠️⚠️ RINEX 2 短名：**这一条原来没有，所以那个 bug 一直没被发现** ----
    #
    #    上面三条用的输入**全是 `.rnx`** —— 于是 `find_local_obs` 里
    #    `p.suffix.lower() not in (".rnx", ".obs")` 那一行从来没被走过。
    #
    #    ⚠️ 而那一行和 `ginan_tools._obs_in` 里被修掉的那行**一字不差** ——
    #       同一个 bug 的第二份拷贝，且后果更重：
    #       它把「① 文件头里的测站名（权威）」整个挡在过滤**后面** →
    #       用户那份 `.22o` 永远不被 inspect → "本地有就不用下载"落空 →
    #       程序转去网上找 → 404 → **看起来像网络问题**。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # RINEX 2 的标准短名（用户那份就是它）
        write(tmp, "alic0600.22o", with_marker("ALIC00AUS"))
        found = fetch.find_local_obs("ALIC", D, tmp)
        check(
            found is not None and found.name == "alic0600.22o",
            f"★★ 按**内容**认出 RINEX 2 短名 `.22o`"
            f"（{found.name if found else '★ 没找到 —— 就是那个 bug'}）",
        )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # ⚠️ 反过度修正：放宽成"按内容认"之后，
        #    **广播星历不能被当成观测数据**（它是导航文件）。
        write(tmp, "BRDC00IGS_R_20260010000_01D_MN.rnx", _heads.REAL_HEADS["nav"])
        check(
            fetch.find_local_obs("BRDC", D, tmp) is None,
            "★ 广播星历不被当成观测数据（按内容判，不是看 .rnx 后缀）",
        )

    # ---- 站号太短不猜 ----
    with tempfile.TemporaryDirectory() as td:
        check(
            fetch.find_local_obs("A", D, Path(td)) is None,
            "站号只有一个字母时不猜（必然误匹配）",
        )

    # ---- ★ 找到之后要**顺手说清它能不能用** ----
    #
    # ⚠️⚠️ "找到了"和"能用"是**两个**问题。
    #    只说前一个的话，用户会一路跑到结果出来才发现不对 ——
    #    那时候错误已经变成一个**看起来正常的数字**了。
    #
    #    所以认领本地数据时就把话说了 —— 早于配置、早于运行。
    from ginan_agent.ginan.catalog import FilePlan

    def header_line(content: str, label: str) -> bytes:
        """一格 RINEX 头行：内容占前 60 列，标签从第 60 列起。"""
        return (content.ljust(60) + label).encode("ascii") + b"\n"

    def obs_with(marker: str, antenna: str) -> bytes:
        out = header_line(
            "     3.04           O: OBSERVATION DATA    M: MIXED",
            "RINEX VERSION / TYPE",
        )
        out += header_line(marker, "MARKER NAME")
        out += header_line(" " * 20 + antenna, "ANT # / TYPE")
        # ⚠️ **观测时段不能省** —— 少了它，`find_local_obs()` 判不出
        #    这份是哪一天的，于是这份"本地有数据"根本不会被认出来
        #    （判据见 `find_local_obs`：宁可漏，不可拿错日子）。
        stamp = "2026     1     1"
        out += header_line(f"{stamp}     0     0    0.0000000     GPS",
                           "TIME OF FIRST OBS")
        out += header_line(f"{stamp}    23    59   30.0000000     GPS",
                           "TIME OF LAST OBS")
        return out + header_line("", "END OF HEADER")

    # ⚠️ `download_one` 现在**必须**知道是哪一天 —— 见 `find_local_obs`。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        write(tmp, "dcms260101000000.rnx", obs_with("", ""))   # 全空
        r = fetch.download_one(
            FilePlan(filename="DCMS_UNKNOWN.rnx", kind="obs", urls={}), tmp, D
        )
        check(r.ok, "本地有数据 → 判定成功（不会再去网上找）")
        check(
            "站名" in r.detail and "天线" in r.detail,
            f"★ detail 里说清了头部缺什么",
        )
        check(
            "inspect_rinex" in r.detail and "repair_rinex" in r.detail,
            "★ 而且给了下一步怎么做",
        )
        check(
            "问用户" in r.detail,
            "★ 点明了天线型号只能问用户（推不出来）",
        )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        write(tmp, "dcms260101000000.rnx", obs_with("DCMS00CHN", "TRM59800.00"))
        r = fetch.download_one(
            FilePlan(filename="DCMS_UNKNOWN.rnx", kind="obs", urls={}), tmp, D
        )
        check(
            "⚠️" not in r.detail,
            f"★ 元数据齐全时**不瞎报警**（{r.detail}）",
        )

    # ---- ★★ 有本地数据，但**不是这一天** → 不许拿它顶 ----
    #
    # ⚠️ 这条是"按天找"在**下载这条路**上的守卫：
    #    只给站名的那版会拿 01-02 的文件去顶 01-01 的活，
    #    而且报的是"已存在（…），跳过下载" —— 一路看着正常。
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        write(tmp, "dcms260102000000.rnx", _obs_head("DCMS00CHN", 2026, 1, 2))
        r = fetch.download_one(
            FilePlan(filename="DCMS_UNKNOWN.rnx", kind="obs", urls={}), tmp, D
        )
        check(
            not r.ok,
            "★★ 本地只有**别的日子**的数据时，不许当成找到了"
            f"（实际 {r.detail[:60]}）",
        )
        check(
            "01-02" not in r.detail and "dcms260102000000.rnx" not in r.detail,
            "★★ 而且不许把那个文件报成「用上了」",
        )


# =============================================================================
# ⑤ 下载：假 session 造各种故障
# =============================================================================


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, chunk_size=64, fail_at=None):
        self.status_code = status
        self._body = body
        self._chunk = chunk_size
        self._fail_at = fail_at
        self.headers = headers or {}
        self._sent = 0
        # ⚠️ 假对象要跟得上真实接口。
        #
        #    cddis_get 里会检查 `resp.is_redirect` —— 假响应缺这个属性时，
        #    测试会抛 AttributeError。而这看起来像"代码坏了"，
        #    实际是**假对象过时了**。
        #
        #    > 假对象是照着接口**重写**的，不是继承的 ——
        #    > 所以接口变了它不会自动跟上，得手动补。
        self.url = ""
        self.is_redirect = False

    def iter_content(self, size):
        for i in range(0, len(self._body), self._chunk):
            if self._fail_at is not None and self._sent >= self._fail_at:
                raise ConnectionError("模拟传输中断")
            chunk = self._body[i:i + self._chunk]
            self._sent += len(chunk)
            yield chunk

    def close(self):
        pass

    def json(self):
        """⚠️ 补这一条是因为 `_resolve_query_url` 要解析 GA 的查询接口。

        ⚠️ 假对象是照着接口**重写**的，不是继承的 —— 所以真实代码
           用到一个新方法时，假对象**不会自动跟上**，得手动补。
           缺了它的表现是 AttributeError，看起来像"代码坏了"，
           实际是**假对象过时了**。
        """
        import json as _json

        return _json.loads(self._body.decode("utf-8"))


class FakeSession:
    """按剧本返回响应的假会话。"""

    def __init__(self, script: dict):
        self.script = script

    def get(self, url, **kwargs):
        r = self.script.get(url)
        if r is None:
            raise ConnectionError(f"没有为 {url} 准备剧本")
        if isinstance(r, Exception):
            raise r
        return r


def test_download_result_kind_is_plan_kind() -> None:
    section("⑤c-2 ★★ 下载结果里的 kind 必须是**计划里的那一类**")

    # ⚠️⚠️ 这是一条真 bug 的守卫，而且**必须走真的下载路径**。
    #
    #    ── bug 本身 ──
    #    `download_one` 原来写的是 `kind=ident.kind or item.kind`：
    #
    #        计划 kind     identify 认成     结果里存的
    #        snx           sinex            sinex      ✗
    #        bia           unknown          unknown    ✗
    #        sp3/clk/…     同名             同名        ✓
    #
    #    `ident.kind` **永远不是空串**（认不出来是 `"unknown"`），
    #    所以 `or item.kind` 那个兜底是**死代码**。
    #
    #    而下游（`workspace.names_of()` → 配置）按**计划的词表**查 →
    #    `snx`/`bia` 两类**明明下成功了却被当成没下到** ——
    #    配置里那两栏空着、`missing` 里报"下载失败"（事实相反）。
    #
    #    ⚠️ 那两类文件存在的全部理由就是消掉 Ginan 的两族警告，
    #       所以"配置里是空的"= 这次改动等于没做，而且不报错。
    #
    #    ── 为什么不能用"造一个 DownloadResult"来测 ──
    #    ⚠️⚠️ 我第一版就是这么测的（在 check_toolchain 里），结果
    #        **变异测试没红** —— 因为夹具直接 `DownloadResult(kind=…)`，
    #        **绕开了 fetch.py 里那段赋值代码**，测的是消费者不是生产者。
    #
    #        而那正是这个仓库反复踩的那个坑：
    #        **测试只覆盖了能用的那条路，洞就在没覆盖的那条上。**
    #
    #    👉 所以这条用 `FakeSession` 驱动**真的** `download_one`。
    from ginan_agent.ginan.catalog import FilePlan

    SNX = (
        b"%=SNX 2.0 ALIC 2026:001:00000 IGS 2026:002:00000 2026:002:00000 "
        b"00000001 IGS\n+FILE/COMMENT\n test\n-FILE/COMMENT\n"
    )
    BIA = (
        b"%=BIA 1.00 COD 2026:001:00000 COD 2026:001:86370\n"
        b"BIAS/SOLUTION\n*BIAS SVN PRN STATION OBS1 OBS2\n"
    )

    # (计划 kind, 内容, identify 会认成什么)
    #
    # ⚠️ 第三列是**前提**，不是期望值 —— 它记录"两套词表对不对得上"：
    #      `snx`  → 对不上（identify 说 `sinex`）← **当年出错的就是它**
    #      `bia`  → 对得上
    #      `sp3`  → 对得上
    #    对得上的那两条**测不出这个 bug**，留着是为了说明"不是所有类型都错"。
    #
    # ⚠️⚠️ `bia` 那一格原来是 `"unknown"` —— 2026-09-21 给 identify 补上
    #      `%=BIA` 规则之后，它变成 `"bia"`，这条前提断言就红了。
    #      **那是它在正常工作**：一条"前提仍然成立吗"的断言，
    #      本来就应该在前提变了的时候喊出来，而不是静默地变得没有意义。
    for kind, body, identified_as in [
        ("snx", SNX, "sinex"),
        ("bia", BIA, "bia"),
        ("sp3", b"#a\x20\x20\x20  SP3\n" + b"x" * 500, "sp3"),
    ]:
        from ginan_agent.ginan import identify as _ident

        url = f"https://example.invalid/{kind}"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            item = FilePlan(filename=f"probe.{kind}", kind=kind, urls={"bkg": url},
                            size_hint="测试")
            sess = FakeSession({
                url: FakeResp(200, body, {"Content-Length": str(len(body))})
            })
            r = fetch.download_one(item, tmp, session=sess)

            check(r.ok, f"下载成功（{kind}）：{r.detail[:60]}")
            check(
                r.kind == kind,
                f"★★ `kind` 是计划里的 {kind!r}，不是 identify 认出来的"
                f"（实际 {r.kind!r}）",
            )

        # ⚠️ 把"identify 会认成别的"这件事本身也钉住 ——
        #    否则哪天 identify 改了词表，这条测试会**静默地变得没有意义**
        #    （两边刚好撞上，就测不出原来的 bug 了）。
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe.bin"
            probe.write_bytes(body)
            got = _ident.identify(probe).kind
            check(
                got == identified_as,
                f"★ 前提仍然成立：identify 认 {kind} 是 {identified_as!r}"
                f"（实际 {got!r}）"
                + (
                    "—— 两边不同名，所以当初才会对不上"
                    if got != kind else
                    "—— 这个两边同名，它不是当年出错的那一类"
                ),
            )


def test_content_derived_name_is_safe() -> None:
    section("⑤c-3 ★★ 按**文件内容**定出来的名字，必须先清洗再用")

    # ⚠️⚠️ 观测数据的落盘名取自**文件头里的 MARKER NAME** ——
    #    那是**别人给的数据**，不是我们能控制的。而它被直接拿去拼磁盘路径。
    #
    #     实测（清洗之前）三种坏结果：
    #       · 标签落在第 1 列的不合规文件 → "测站名"成了 `MARKER` →
    #         文件叫 `MARKER.rnx`，`ok=True`、**没有任何警告**，
    #         配置随后引用这个错名字
    #       · 标签里带 `:`（Windows 非法字符）→ `_local` 正确拒绝，
    #         但拒绝的表现是**异常穿出 download_one、穿出 fetch_plan**
    #         → 整批下载中止，用户拿到 traceback
    #       · 带 `/` 或 `..` → 同上，而且那是**往目录外写**
    #
    #     ⚠️ 判据：**清洗过再拼路径**，而且"名字不可用"是
    #       **这一个文件失败**，不是整个任务崩掉。
    from ginan_agent.ginan.catalog import FilePlan

    L = lambda c, lab: c[0:60].ljust(60) + lab.ljust(20) + "\n"  # noqa: E731

    def obs(marker_line: str) -> bytes:
        return (
            L("     2.11           OBSERVATION DATA    M (MIXED)",
              "RINEX VERSION / TYPE")
            + marker_line
            + L("  2022     3     1     0     0    0.0000000     GPS",
                "TIME OF FIRST OBS")
            + L("", "END OF HEADER")
            + " 22  3  1  0  0  0.0000000  0  1G01\n"
        ).encode()

    url = "https://example.invalid/x"
    cases = [
        ("正常标签", "ALIC00AUS" + " " * 51 + "MARKER NAME\n", "ALIC00AUS.rnx"),
        ("标签顶到第 1 列", "MARKER" + " " * 54 + "MARKER NAME\n", "fallback.rnx"),
        ("标签带 `:`", "C:ALIC00AUS" + " " * 49 + "MARKER NAME\n", "C_ALIC00AUS.rnx"),
        ("标签带路径", "../../etc/passwd" + " " * 44 + "MARKER NAME\n", "etc_passwd.rnx"),
        ("标记那行是空的", " " * 60 + "MARKER NAME\n", "fallback.rnx"),
    ]

    for label, marker_line, expect in cases:
        with tempfile.TemporaryDirectory() as td:
            item = FilePlan(filename="fallback.rnx", kind="obs",
                            urls={"bkg": url}, size_hint="测试")
            # ⚠️ Content-Length 必须**照实写**。我第一版随手写了 9999，
            #    于是触发了下载器的完整性检查（"得到 N 字节，应有 9999"）——
            #    那个检查本身是对的，错的是夹具。
            body = obs(marker_line)
            sess = FakeSession({url: FakeResp(200, body,
                                              {"Content-Length": str(len(body))})})
            try:
                r = fetch.download_one(item, Path(td), session=sess)
                crashed = ""
            except Exception as exc:  # noqa: BLE001
                r, crashed = None, f"{type(exc).__name__}: {exc}"

            check(
                not crashed,
                f"★★ 不抛异常（{label}）{('：' + crashed) if crashed else ''}",
            )
            if r is not None:
                check(
                    r.real_name == expect,
                    f"★★ 落盘名 = {expect!r}（{label}，实际 {r.real_name!r}）",
                )
                # ⚠️ 落盘的文件必须真的在 dest_dir **里面**
                check(
                    r.path is not None and Path(td) in r.path.parents,
                    f"★ 文件落在目标目录**里面**（{label}）—— 不许被 `..` 带出去",
                )

    # ---- 清洗函数本身：它得真的会拒绝 ----
    check(fetch._safe_filename("") == "", "★ 空串进去还是空串（调用方要判它）")
    check(fetch._safe_filename("...") == "", "★ 只有点和横线的名字判为空")
    check("/" not in fetch._safe_filename("a/b"), "★ 路径分隔符被换掉")
    check(":" not in fetch._safe_filename("C:x"), "★ 冒号被换掉（Windows 非法）")

    # ---- ⚠️⚠️ 最后一道防线：`_local` 拒绝的名字，只能让**这一个文件**失败 ----
    #
    #    上面那些用例**测不到**这条 —— 清洗之后 `_local` 已经收不到非法名了
    #    （我做了变异测试：把这段兜底拿掉，上面全绿）。
    #
    #    ⚠️ 但这不等于兜底可以删：
    #       `_local` 是**唯一**拼下载路径的地方，它**按设计会抛 ValueError**，
    #       而"名字从哪来"将来还会变（收件箱、用户手输、新的源）。
    #       真正的不变量是**契约**，不是"现在这个调用方"：
    #
    #          **名字不可用 ⇒ 这一个文件失败，不是整批崩。**
    #
    #       所以这里**直接测那个契约** —— 把 `_decide_name` 换成一个
    #       故意返回非法名的桩，看 `download_one` 是返回失败还是抛异常。
    #
    #    ⚠️ 判据：**测契约，不要测"当前有没有人能触发它"。**
    #       后者变了，这条就静默失效；前者才是真正要守的东西。
    real_decide = fetch._decide_name
    fetch._decide_name = lambda *_a, **_k: "C:evil.rnx"
    try:
        with tempfile.TemporaryDirectory() as td:
            item = FilePlan(filename="ok.rnx", kind="obs",
                            urls={"bkg": url}, size_hint="测试")
            body = obs("ALIC00AUS" + " " * 51 + "MARKER NAME\n")
            sess = FakeSession({url: FakeResp(200, body,
                                              {"Content-Length": str(len(body))})})
            try:
                r = fetch.download_one(item, Path(td), session=sess)
                crashed = ""
            except Exception as exc:  # noqa: BLE001
                r, crashed = None, f"{type(exc).__name__}: {exc}"

            check(
                not crashed,
                f"★★ 非法名字**不让异常穿出去**（原来是 {crashed}）—— "
                "穿出去的话整批下载中止，用户拿到 traceback 而不是"
                "『这一个没成、别的继续』",
            )
            if r is not None:
                check(not r.ok, "★★ 而是把**这一个文件**标成失败")
                check("文件名" in r.detail, f"★ 说清了是名字的问题：{r.detail[:50]}")
    finally:
        fetch._decide_name = real_decide


def test_decompress_failure_is_per_file() -> None:
    section("⑤c-4 ★★ 解压失败只能是**这一个文件**的事，不能让整批下载中止")

    # ⚠️⚠️ 事故的形状：`download_one` 里那句
    #
    #         try:    _gunzip(...)
    #         except OSError as exc:  ...
    #
    #    而 `_gunzip` 对**非 iGMAS 的源**会把 gzip 库的异常原样抛出 ——
    #    最常见的是 **`EOFError`**，而 `EOFError` **不是 `OSError` 的子类**
    #    （实测：`isinstance(EOFError(), OSError)` 是 False）。
    #
    #    而 `download_one` 函数体顶层**没有兜底 except**，
    #    `fetch_plan` **一个 try 都没有** —— 于是它一路穿到工具层：
    #
    #        **整批下载中止**，用户拿到一个 traceback，
    #        而不是"哪些成了、哪些没成"。
    #
    #    ⚠️ 触发条件不苛刻：只要某个源的响应没有 `Content-Length`
    #       （分块传输）又被中途切断，就是这条。
    #
    # ⚠️ 这里用**桩**把异常直接打进去，而不是去造一个刚好能被嗅探认出、
    #    又刚好解压失败的压缩包 —— 后者依赖"前 4KB 膨胀出 8KB"这种
    #    内部阈值，**阈值一变这条测试就静默失效**。
    #    判据要盯**契约**：`_gunzip` 抛任何异常 ⇒ 这一个文件失败。
    from ginan_agent.ginan.catalog import FilePlan

    head = b"#aP2026 9  2  0  0  0.00000000      96 ORBIT IGS20 HLM  IGS\n"
    body = head + (b"PG01  12345.678901 -12345.678901  12345.678901    123.456789\n" * 500)
    full = gzip.compress(body)
    # 只留前 60% —— 头完好（嗅探认得出来），中间断掉
    cut = full[: int(len(full) * 0.6)]
    url = "https://example.invalid/x.sp3.gz"
    item = FilePlan(filename="x.sp3", kind="sp3", urls={"bkg": url}, size_hint="测试")

    # ---- ① 真·截断的 gzip 走完整下载路径 ----
    with tempfile.TemporaryDirectory() as td:
        sess = FakeSession({url: FakeResp(200, cut,
                                          {"Content-Length": str(len(cut))})})
        try:
            r = fetch.download_one(item, Path(td), session=sess)
            crashed = ""
        except Exception as exc:  # noqa: BLE001
            r, crashed = None, f"{type(exc).__name__}: {exc}"

        check(not crashed, f"★★ 不抛异常（原来是 {crashed}）")
        if r is not None:
            check(not r.ok, f"★★ 把它标成失败（detail：{r.detail[:60]}）")
            check("解压" in r.detail, "★ 说清是**解压**失败，不是笼统的'下载失败'")
        check(
            not list(Path(td).iterdir()),
            f"★★ 不留半个文件（实际 {[p.name for p in Path(td).iterdir()]}）"
            "—— 半个文件会冒充成完整的，Ginan 读到一半才报错",
        )

    # ---- ② 契约：`_gunzip` 抛**任何**异常都只能让这一个文件失败 ----
    #
    #    ⚠️ 上面那条依赖"这个压缩包刚好能被嗅探认出"。
    #       这一条不依赖任何阈值 —— 它把异常直接打进去。
    real_gunzip = fetch._gunzip
    try:
        for exc in (EOFError("流没有正常结束"), ValueError("畸形头"), MemoryError()):
            fetch._gunzip = lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e)
            with tempfile.TemporaryDirectory() as td:
                sess = FakeSession({url: FakeResp(200, full,
                                                  {"Content-Length": str(len(full))})})
                try:
                    r2 = fetch.download_one(item, Path(td), session=sess)
                    ok = (not r2.ok) and "解压" in r2.detail
                    why = ""
                except Exception as e:  # noqa: BLE001
                    ok, why = False, f"（穿了 {type(e).__name__}）"
                check(
                    ok,
                    f"★★ `_gunzip` 抛 {type(exc).__name__} ⇒ 这一个文件失败"
                    f"而不是异常穿出去 {why}",
                )
    finally:
        fetch._gunzip = real_gunzip


def test_ga_two_step_query() -> None:
    section("⑤c ★ GA 的观测数据是**两步**的：先查接口，再下文件")

    # ⚠️⚠️ 这个分支原来**根本没写**。
    #
    #    计划里给 GA 的 obs 配的是**查询接口**的 URL
    #    （`data.gnss.ga.gov.au/api/v1/rinexFiles?…`），
    #    而下载器把它当普通文件下 —— 于是拿到一段 JSON，
    #    再被识别环节判为"内容对不上"。
    #
    #    实测现象（2026-09-18）：
    #        要的是 obs，拿到的是 认不出来
    #        （开头是 '[{"siteId":"alic","fileType":"obs",…'）
    #
    #    ⚠️ 它能藏这么久，是因为这条路被两件事**先后**挡着：
    #        ① 先是网络超时（没开代理时 GA 连不上）
    #        ② 后来用户自己的本地文件直接命中了 —— **压根没走到下载**
    #
    #       > **一个从没被走到的分支，等于没写过。**
    #
    #    ⚠️ 而且这个误判**看起来像"源有问题"**：报错说的是
    #       "内容对不上"，而不是"你把查询接口当文件下了"。
    #       第一反应会是"GA 的数据不对"，方向完全错了。

    import json

    api = "https://data.gnss.ga.gov.au/api/v1/rinexFiles?stationId=ALIC&fileType=obs"
    real = "https://example.invalid/presigned/ALIC00AUS_R_20262450000.rnx"

    obs_body = REAL_HEADS["obs"] + b"x" * 2000
    sess = FakeSession({
        api: FakeResp(200, json.dumps([
            {"siteId": "alic", "fileType": "obs", "fileLocation": real,
             "fileSize": len(obs_body), "metadataStatus": "PUBLIC"},
        ]).encode()),
        real: FakeResp(200, obs_body, {"Content-Length": str(len(obs_body))}),
    })

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        item = FilePlan(filename="ALIC_UNKNOWN.rnx", kind="obs", urls={"ga": api})
        r = fetch.download_one(item, dest, session=sess)

        check(r.ok, f"★ 两步下载成功（{r.detail[:60]}）")
        check(
            (dest / (r.real_name or "x")).is_file() if r.ok else False,
            "落盘的是**真文件**，不是那段 JSON",
        )

    # ---- 各种"接口不给力"的情况都要说清楚 ----
    cases = [
        ("接口返回空数组", 200, b"[]", "没有该站该天的数据"),
        # ⚠️ bytes 字面量只能放 ASCII —— 中文要写成转义。
        #    （这条注释上面就写着，我还是踩了一次。）
        ("接口返回非 JSON", 200, b"<html>login page</html>", "不是 JSON"),
        ("接口返回 500", 500, b"{}", "500"),
        ("接口没给 fileLocation", 200, b'[{"siteId":"alic"}]', "fileLocation"),
    ]
    for desc, status, body, expect in cases:
        with tempfile.TemporaryDirectory() as td:
            s = FakeSession({api: FakeResp(status, body)})
            item = FilePlan(filename="ALIC_UNKNOWN.rnx", kind="obs",
                            urls={"ga": api})
            r = fetch.download_one(item, Path(td), session=s)
            check(not r.ok, f"{desc} → 判定失败")
            check(
                expect in r.detail,
                f"  说明里点到了「{expect}」：{r.detail[:70]}",
            )

    # ---- ⚠️ 别把普通 URL 也当查询接口 ----
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        body = REAL_HEADS["sp3"] + b"x" * 500
        u = "https://igs.bkg.bund.de/root_ftp/IGS/products/2434/X.SP3"
        s = FakeSession({u: FakeResp(200, body, {"Content-Length": str(len(body))})})
        item = FilePlan(filename="X.SP3", kind="sp3", urls={"bkg": u})
        r = fetch.download_one(item, dest, session=s)
        check(r.ok, "★ 普通 URL 不会被误当成查询接口（原样下）")


def test_download_decompresses() -> None:
    section("⑤ ⚠️ 关键回归：下载后必须是**解压过的明文**")

    # 这个测试盯的是一个真实踩过的坑：
    #   BKG 上是 .gz，下载器原样存成没有 .gz 后缀的名字，
    #   然后识别/校验和/落盘全部"成功" —— 因为识别内部会解压看内容。
    #   但磁盘上那个文件仍然是压缩的，Ginan 读它只会得到乱码。

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        raw = REAL_HEADS["sp3"] + b"x" * 2000
        gz = gzip.compress(raw)

        url = "https://example.invalid/sp3.gz"
        sess = FakeSession({url: FakeResp(200, gz, {"Content-Length": str(len(gz))})})

        item = FilePlan(filename="TEST.SP3", kind="sp3", urls={"bkg": url})
        r = fetch.download_one(item, dest, session=sess)

        check(r.ok, f"下载返回成功（{r.detail}）")
        if r.ok:
            data = r.path.read_bytes()
            check(
                data[:2] != b"\x1f\x8b",
                "落盘的是**明文**，不是 gzip",
            )
            check(data == raw, "解压后的内容与原始数据完全一致")
            check(r.size == len(raw), f"大小是解压后的大小（{r.size}）")
            check(not list(dest.glob("*.gz")), "没有残留的压缩文件")


def test_download_keeps_nav_filename() -> None:
    section("⑤b ★ 广播星历的文件名必须原样保留（IGS 标准命名）")

    # ⚠️⚠️ 这个 bug **一直被 bug① 挡着没暴露**，很值得记下来。
    #
    #    `_decide_name` 里原来有这么一段：
    #
    #        if item.kind == "nav":
    #            return f"BRDC_{item.filename.split('_')[-1] or 'nav'}.rnx"
    #
    #    那是"计划里还用占位名（`BRDC_2026001.rnx`）"的年代留下的。
    #    而计划后来给的是**真实的 IGS 文件名**：
    #
    #        BRDC00IGS_R_20260010000_01D_MN.rnx
    #          → split('_')[-1]  =  "MN.rnx"
    #          → "BRDC_MN.rnx.rnx"            ← 名字被毁掉了
    #
    #    ⚠️ 为什么一直没被发现：**广播星历从来就没下载成功过**
    #       （源配错了 —— 就是 bug①），所以这一行**根本没机会跑**。
    #       等 ① 修好、真的下下来了，它才现形。
    #
    #       **一个 bug 会掩盖另一个 bug。** 修完一个，要回头看它下游
    #       有没有从来没被走到过的代码 —— 那些地方没被验证过。
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        raw = REAL_HEADS["nav"] + b"x" * 2000
        gz = gzip.compress(raw)
        NAME = "BRDC00IGS_R_20260010000_01D_MN.rnx"
        url = f"https://example.invalid/{NAME}.gz"
        sess = FakeSession({url: FakeResp(200, gz, {"Content-Length": str(len(gz))})})

        item = FilePlan(filename=NAME, kind="nav", urls={"bkg": url})
        r = fetch.download_one(item, dest, session=sess)

        check(r.ok, f"广播星历下载成功（{r.detail}）")
        if r.ok:
            check(
                r.real_name == NAME,
                f"★ 落地名**原样保留**（实际 {r.real_name!r}）",
            )
            check(
                ".rnx.rnx" not in r.real_name,
                "★ 没有出现 `.rnx.rnx` 这种双扩展名",
            )
            check((dest / NAME).is_file(), "文件就在预期的名字下")


def test_download_multi_source_fallback() -> None:
    section("⑥ 多源回退：第一个源挂了会自动试下一个")

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        raw = REAL_HEADS["erp"]
        url_a = "https://a.invalid/erp"
        url_b = "https://b.invalid/erp"
        sess = FakeSession({
            url_a: ConnectionError("A 挂了"),
            url_b: FakeResp(200, raw, {"Content-Length": str(len(raw))}),
        })

        item = FilePlan(filename="TEST.ERP", kind="erp",
                        urls={"bkg": url_a, "cddis": url_b})
        r = fetch.download_one(item, dest, session=sess)

        check(r.ok, "第一个源失败后，第二个源成功了")
        check(r.source == "cddis", f"记录了实际用的源：{r.source}")
        check(r.path.read_bytes() == raw, "内容正确")


def test_download_detects_wrong_content() -> None:
    section("⑦ 内容对不上要报错（不能默默存下来）")

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        # 要的是 SP3，服务器返回的却是 ERP
        wrong = REAL_HEADS["erp"]
        url = "https://x.invalid/f"
        sess = FakeSession({url: FakeResp(200, wrong, {"Content-Length": str(len(wrong))})})

        item = FilePlan(filename="WANT.SP3", kind="sp3", urls={"bkg": url})
        r = fetch.download_one(item, dest, session=sess)

        check(not r.ok, "内容不符时判定为失败")
        check("对不上" in r.detail, f"错误说明指出内容不符：{r.detail[:60]}")
        check(not list(dest.iterdir()), "失败时不留下垃圾文件")


def test_download_resumes() -> None:
    section("⑧ 断点续传：中断后保留 .part，重试能接着下")

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        raw = REAL_HEADS["sp3"] + b"y" * 500
        url = "https://r.invalid/f"

        # 第一次：传到一半断了
        sess1 = FakeSession({url: FakeResp(200, raw, fail_at=200)})
        item = FilePlan(filename="TEST.SP3", kind="sp3", urls={"bkg": url})
        r1 = fetch.download_one(item, dest, session=sess1)
        check(not r1.ok, "第一次中断，判定为失败")
        part = dest / "TEST.SP3.part"
        check(part.exists(), "⚠️ 保留了 .part 文件（没白下）")

        # 第二次：服务器支持续传（206），只返回剩下的部分
        #
        # ⚠️⚠️ **必须带 `Content-Range`** —— 按 RFC，206 **必须**有这个头，
        #     真实的服务器（实测过 Ginan 的 S3）也确实带：
        #
        #         Content-Range: bytes 54165504-72363936/72363937
        #
        #     ⚠️ 这个夹具原来**只有 `Content-Length`** —— 也就是造了一个
        #        HTTP 上不存在的响应。于是两条路都测不到：
        #          · "服务端从**别的**位置发的"（那会把 .part 拼坏）
        #          · "总大小该以 Content-Range 为准"（不是自己猜的起点 + 响应长度）
        #
        #     👉 **夹具不贴真实，那条路的 bug 就没有守卫。**
        #        这次是加上"核对续传起点"那个修复才把它暴露出来的。
        already = part.stat().st_size
        rest = raw[already:]
        sess2 = FakeSession({
            url: FakeResp(206, rest, {
                "Content-Length": str(len(rest)),
                "Content-Range": f"bytes {already}-{len(raw)-1}/{len(raw)}",
            })
        })
        r2 = fetch.download_one(item, dest, session=sess2)

        check(r2.ok, f"重试成功（{r2.detail}）")
        if r2.ok:
            check(
                r2.path.read_bytes() == raw,
                f"续传后内容完整（{r2.size} 字节，期望 {len(raw)}）",
            )


def test_resume_position_is_verified() -> None:
    section("⑧b ★ 续传：服务端从哪发的，必须核对（不能闷头往 .part 上拼）")

    # ⚠️⚠️ 这一节的由来（2026-09-24，实测 `EGM2008.gfc`）：
    #
    #     `.part` 最后是 **78,655,393** 字节，而服务端说那个文件只有
    #     **72,363,937** —— **正好多了 6 MiB**（= 它那次续传的起点）。
    #
    #     算术上只有一个机制能同时解释"正好多 6 MiB"和"完整性检查放它过去了"：
    #     **服务端收了 Range、回了 206，但发的是整份文件。** 而旧代码算总大小的
    #     写法是 `Content-Length + already` —— 服务端发的既是整份，
    #     这个加法就把 total 一起放大了，于是 written 和 total 同时多 6 MiB，
    #     两边仍然相等，检查**看不出任何异常**。
    #
    #     ⚠️ 那个机制**没有复现过**（直连这台机器时服务端的 206 完全正常）。
    #        但下面两件事**本身就该做**，与那个猜测成不成立无关：
    #          ① 206 必须带 `Content-Range`（RFC 要求），总大小以它为准
    #          ② 服务端从**别的位置**发的数据必须当场拒绝 ——
    #             拼到 `.part` 后面只会得到一个坏文件
    import tempfile as _tf

    raw = REAL_HEADS["sp3"] + b"y" * 500
    url = "https://r.invalid/f"
    item = FilePlan(filename="TEST.SP3", kind="sp3", urls={"bkg": url})

    def _seed_part(dest: Path, n: int) -> Path:
        """造一个"下了一半"的 .part。"""
        (dest / "TEST.SP3.part").write_bytes(raw[:n])
        return dest / "TEST.SP3.part"

    # ---- ① 206 但**没有** Content-Range：不可信，必须拒 ----
    #
    # ⚠️ 夹具补上这个头**之前**，这条根本测不出来 ——
    #    而"夹具不贴真实，那条路的 bug 就没有守卫"。
    dest = Path(_tf.mkdtemp(prefix="resume_a_"))
    part = _seed_part(dest, 200)
    sess = FakeSession({url: FakeResp(206, raw[200:], {"Content-Length": str(len(raw) - 200)})})
    r = fetch.download_one(item, dest, session=sess)
    check(not r.ok, f"★ 206 却没有 Content-Range → 拒绝（{r.detail[:44]}）")
    check(
        part.stat().st_size == 200,
        f"★ 而且 .part **原样留着**（{part.stat().st_size} 字节，期望 200）"
        f" —— 它是好的，坏的是那份响应",
    )

    # ---- ② 206 但**从错误的位置**发：拼上去就是坏文件 ----
    dest = Path(_tf.mkdtemp(prefix="resume_b_"))
    part = _seed_part(dest, 200)
    # 服务端从 0 开始发（等于把整份又发了一遍）—— 正是怀疑的那个机制
    sess = FakeSession({
        url: FakeResp(206, raw, {
            "Content-Length": str(len(raw)),
            "Content-Range": f"bytes 0-{len(raw)-1}/{len(raw)}",
        })
    })
    r = fetch.download_one(item, dest, session=sess)
    check(
        not r.ok,
        f"★ 从错误位置发（请求 200，服务端从 0 发）→ 拒绝（{r.detail[:44]}）",
    )
    check(
        part.stat().st_size == 200,
        f"★ 而且没有把整份拼上去（{part.stat().st_size} 字节，"
        f"**如果拼了就正好是这个 bug 本身**）",
    )

    # ---- ③ 对照：Content-Range 正确时必须**照常成功** ----
    #
    # ⚠️ 不查这一条的话，一个"永远拒绝 206"的实现也能通过上面两条。
    dest = Path(_tf.mkdtemp(prefix="resume_c_"))
    _seed_part(dest, 200)
    sess = FakeSession({
        url: FakeResp(206, raw[200:], {
            "Content-Length": str(len(raw) - 200),
            "Content-Range": f"bytes 200-{len(raw)-1}/{len(raw)}",
        })
    })
    r = fetch.download_one(item, dest, session=sess)
    check(r.ok, f"★ 位置正确时照常成功（{r.detail}）")
    if r.ok:
        check(r.path.read_bytes() == raw, "★ 续传后内容完整")


def test_download_too_long_is_rejected() -> None:
    section("⑧d ★ 下载**多了**也要算失败（不能只查'少了'）")

    # ⚠️⚠️ 旧的完整性检查写的是 `written < total` —— **只查一个方向**。
    #
    #     实测那个多出 6 MiB 的 `.part` **通过了**这道检查，
    #     然后被交给 `_gunzip`，炸成 `zlib.error: invalid code lengths set`。
    #
    #     👉 那条错误**完全指不到真因**：读到它的人会去查 gzip、查源站、
    #        查文件格式，而真相是"这个文件被写多了"。
    #
    #     **一道"完整性检查"漏掉一个方向，它就挡不住它存在的理由。**
    import tempfile as _tf

    raw = REAL_HEADS["sp3"] + b"y" * 400
    url = "https://r.invalid/f"
    item = FilePlan(filename="TEST.SP3", kind="sp3", urls={"bkg": url})
    dest = Path(_tf.mkdtemp(prefix="toolong_"))

    # 服务端声称 300 字节，实际发了 500（FakeResp 会照发不误）
    sess = FakeSession({url: FakeResp(200, raw, {"Content-Length": "300"})})
    r = fetch.download_one(item, dest, session=sess)

    check(not r.ok, f"★ 实际收到 500 > 声称的 300 → 判定失败（{r.detail[:60]}）")
    check(
        "多" in r.detail,
        f"★ 而且说明是**多了**（不是笼统的'不完整'）：{r.detail[:60]}",
    )


def test_unlink_quietly_never_raises() -> None:
    section("⑧e ★ 清理临时文件失败时，不许把真正的错误盖掉")

    # ⚠️⚠️ 实测的原始现场：
    #
    #     一个坏掉的 `.part` 让 `_gunzip` 抛 `zlib.error`，走到错误处理里 ——
    #     而 `part.unlink()` **自己又抛了**
    #     `PermissionError: [WinError 32] 另一个程序正在使用此文件`。
    #
    #     ⇒ 用户和日志看到的是一条**文件被占用**的 traceback，
    #       真因（下下来的文件是坏的）**一个字都没露出来**。
    #
    #     ⚠️ Windows 上"删不掉"是**常态而不是意外**（杀毒软件、索引服务、
    #        别的进程刚好开着它）。清理失败不该改变这条记录说什么。
    import tempfile as _tf

    d = Path(_tf.mkdtemp(prefix="unlink_"))
    f = d / "x.part"
    f.write_bytes(b"x")

    handle = open(f, "rb")          # 持有句柄 → Windows 上 unlink 必然失败
    try:
        raised = ""
        try:
            fetch._unlink_quietly(f)
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        check(not raised, f"★ 文件被占用时不抛异常（{raised or '没抛'}）")
        check(f.exists(), "★ 文件确实还在 —— 这正是要容忍的那种情况（占点磁盘而已）")
    finally:
        handle.close()

    # 句柄放开之后应该能删掉 —— 证明它不是"永远不删"
    fetch._unlink_quietly(f)
    check(not f.exists(), "★ 句柄放开后照常删掉（不是永不删除）")
    fetch._unlink_quietly(f)        # 再来一次：文件已经不在了
    check(True, "★ 文件本来就不在时也不抛（missing_ok 那一半）")


def test_download_incomplete_detected() -> None:
    section("⑨ 不完整的下载要被发现（不能当成成功）")

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        raw = REAL_HEADS["sp3"]
        url = "https://i.invalid/f"
        # 声明 1000 字节但只给 20
        sess = FakeSession({
            url: FakeResp(200, raw, {"Content-Length": "1000"})
        })
        item = FilePlan(filename="T.SP3", kind="sp3", urls={"bkg": url})
        r = fetch.download_one(item, dest, session=sess)
        check(not r.ok, "长度不符判定为失败")
        check("完整" in r.detail or "字节" in r.detail, f"说明指出了问题：{r.detail[:50]}")


# =============================================================================
# ⑩ 缓存
# =============================================================================


def test_one_time_cache() -> None:
    section("⑩ 一次性文件命中缓存时不再下载")

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        raw = REAL_HEADS["atx"] * 10
        (dest / "igs20.atx").write_bytes(raw)

        url = "https://should-not-be-called.invalid/f"
        sess = FakeSession({url: AssertionError("不该被调用！")})

        item = FilePlan(filename="igs20.atx", kind="atx",
                        urls={"bkg": url}, one_time=True)
        r = fetch.download_one(item, dest, session=sess)

        check(r.ok, "命中缓存，返回成功")
        check(r.skipped, "标记为'已跳过下载'")
        check(r.source == "缓存", "来源标为缓存")


# =============================================================================
# ⑥ .Z（Unix compress）—— 老日期的 IGS 产品就是这个格式
# =============================================================================


# ⚠️ 夹具是**真实文件**，不是构造的：
#    · bkg09997.sum.Z      —— BKG 公开 FTP，5 KB，完整走一遍解码（含表格填满）
#    · igr21992.erp.Z      —— CDDIS 上 2022-03-01 的真实 ERP，385 字节
#
# ⚠️ 这里原来写着一句**自相矛盾**的话：「两个都不用账号就能下
#    （ERP 那个要 CDDIS 账号，但只有 385 字节）」——
#    前半句和后半句直接打架，而下一个人只会读到前半句。
#
#    2026-09-29 实测（认证会话 + 真匿名对照）：**CDDIS 那个是要账号的**，
#    匿名请求拿到的是 302（跳 urs.earthdata.nasa.gov）。
#    （顺带一提：那次"匿名"探针一开始被骗了 —— 见 ginan-facts 里
#      「requests 会自己去读 ~/_netrc」那条。）
#
#    教训⑯：**夹具要么是真的，要么标清楚是构造的。**
#    写着"实测"却是编的，比没有夹具更糟 —— 它让下一个人相信它。
FIXTURES = ROOT / "tests" / "fixtures"


def test_lzw_decodes_real_file() -> None:
    section("⑥ ★ 自己能解开 .Z —— 不只是「认得出来」")

    from ginan_agent.ginan import lzw

    p = FIXTURES / "bkg09997.sum.Z"
    if not p.is_file():
        check(False, f"夹具不见了：{p}")
        return

    raw = p.read_bytes()
    out = lzw.unlzw(raw)

    # ⚠️ 这两个数是**实测**的：5132 字节压缩 → 18026 字节明文。
    #    写死是有意的 —— 解码器写错一点点，长度就会不对。
    check(len(raw) == 5132, f"夹具是 5132 字节（{len(raw)}）")
    check(
        len(out) == 18026,
        f"★ 解出 18026 字节（实际 {len(out)}）—— 解码器逐字节正确",
    )
    check(
        out.startswith(b"****" * 5),
        "开头是我们预期的文本（不是乱码）",
    )
    check(
        b"GPS WEEK 0999" in out,
        "★ 内容里能找到 'GPS WEEK 0999' —— 解出来的是真东西",
    )


def test_lzw_truncated_is_normal() -> None:
    section("⑥b ★ 截断的 .Z 返回前缀，不抛异常（嗅探天天走到这条路）")

    # ⚠️ 识别类型时只喂**开头几 KB**，所以"数据不够"是**常态**。
    #    如果截断就抛异常，嗅探那条路就得层层兜底 ——
    #    而嗅探本身是最该"尽量别抛异常"的地方。
    from ginan_agent.ginan import lzw

    p = FIXTURES / "bkg09997.sum.Z"
    if not p.is_file():
        check(False, f"夹具不见了：{p}")
        return

    raw = p.read_bytes()
    full = lzw.unlzw(raw)

    ok = True
    for n in (3, 10, 50, 500, 2000, len(raw) - 1):
        try:
            part = lzw.unlzw(raw[:n])
        except Exception as exc:  # noqa: BLE001
            check(False, f"前 {n} 字节抛了 {type(exc).__name__}")
            ok = False
            break
        if not full.startswith(part):
            check(False, f"前 {n} 字节解出来的不是完整结果的前缀")
            ok = False
            break
    if ok:
        check(True, "★ 各种截断长度都返回前缀、都不抛异常")

    # 坏数据要**明确报错**，不能悄悄返回空
    for bad, desc in ((b"\x1f\x9d", "只有魔数"), (b"not a Z file", "根本不是 .Z")):
        try:
            lzw.unlzw(bad)
            check(False, f"{desc} 应该报错却没报")
        except lzw.LzwError:
            check(True, f"{desc} → 明确报 LzwError")


def test_identify_z_format() -> None:
    section("⑥c ★ .Z 要能认出「是什么」，而不只是「是压缩的」")

    # ⚠️⚠️ 这一条是整个 #9 的**解药**。
    #
    #    不加它的话：文件名对了、HTTP 200 了、字节数也对，
    #    但识别说"内容对不上" → 下载器据此**把文件删掉** →
    #    用户看到的是"下载失败"，根因在读文件的这几行。
    p = FIXTURES / "igr21992.erp.Z"
    if not p.is_file():
        check(False, f"夹具不见了：{p}")
        return

    got = identify.identify(p)
    check(got.kind == "erp", f"★ .Z 里的 ERP 被认出来了（{got.kind}）")
    check(got.compression == "z", f"标了压缩类型（{got.compression!r}）")
    check(got.confidence == "high", "置信度是 high")


def test_download_decompresses_z() -> None:
    section("⑥d ★ 下载 .Z 落盘必须是**明文**（gzip 那个坑的第二次）")

    # ⚠️ 和 ⑤（gzip）同一个断言、同一个理由：
    #    "识别通过"只证明"这是一个 ERP 的压缩包"，
    #    不证明"这里有一个可用的 ERP"。
    p = FIXTURES / "igr21992.erp.Z"
    if not p.is_file():
        check(False, f"夹具不见了：{p}")
        return

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        url = "https://example.invalid/igr21992.erp.Z"
        blob = p.read_bytes()
        sess = FakeSession({
            url: FakeResp(200, blob, {"Content-Length": str(len(blob))}),
        })

        item = FilePlan(filename="igr21992.erp", kind="erp", urls={"cddis": url})
        r = fetch.download_one(item, dest, session=sess)

        check(r.ok, f"下载成功（{r.detail}）")
        if r.ok:
            data = r.path.read_bytes()
            check(
                data[:2] != b"\x1f\x9d",
                "★ 落盘的是**明文**，不是 .Z",
            )
            check(
                data.startswith(b"version 2"),
                "★ 内容是正确的 ERP 文本",
            )


# =============================================================================
# ⑦ 收件箱：认领是**复制**，清空只清"证明有备份的"
# =============================================================================


def test_inbox_claim_copies() -> None:
    section("⑦ ★ 认领之后，原件必须还留在收件箱")

    # ⚠️⚠️ 这一条守的是用户的**原始数据**。
    #
    #    原来是 `shutil.move` —— 认领完，用户那份就**没了**。
    #    而别人的产品文件都能重下，**他自己测的东西不能**。
    #
    #    改成复制之后，"新任务"清空收件箱才有意义（见下一条）。
    import tempfile

    from ginan_agent.ginan import inbox

    box = Path(tempfile.mkdtemp(prefix="inbox_"))
    dest = Path(tempfile.mkdtemp(prefix="dest_"))
    src = box / "alic0600.22o"
    src.write_bytes(REAL_HEADS["obs"])

    before = src.read_bytes()
    r = inbox.claim(dest_dir=dest, root=box)

    check(len(r.moved) == 1, f"认领了 1 个（{r.summary}）")
    check(src.is_file(), "★ 收件箱里**原件还在**")
    check(src.read_bytes() == before, "★ 原件内容没被动过")
    check(
        any(m.kept_in_inbox for m in r.moved),
        "★ 记录里写明了'原件仍留在收件箱'（用户有权知道）",
    )

    landed = [p for p in dest.iterdir() if p.is_file()]
    check(len(landed) == 1, f"数据目录里有一份（{[p.name for p in landed]}）")
    if landed:
        check(landed[0].read_bytes() == before, "数据目录那份内容一致")

    # ---- 幂等：再认领一次不该出错，也不该重复复制 ----
    r2 = inbox.claim(dest_dir=dest, root=box)
    check(
        len(r2.moved) == 0 and len(r2.skipped) == 1,
        f"★ 再认领一次是'已有，跳过'而不是报错（{r2.summary}）",
    )
    check(
        len([p for p in dest.iterdir() if p.is_file()]) == 1,
        "★ 没有变成两份",
    )


def test_inbox_reports_the_name_it_actually_used() -> None:
    section("⑦ ★★ 认领报出来的名字，必须就是真正落盘的那个名字")

    # ⚠️⚠️ 这一条是拿**真数据**抓出来的（2025-166 那天 55 个观测文件）。
    #
    #    实测：55/55 全部被改名
    #          （`ALBH00XXX_R_20251660000_01D_30S_MO.rnx` → `ALBH.rnx`）
    #          而报告里 `renamed` 全是 False —— 报出来的是
    #          「ALBH00XXX_…_MO.rnx → 数据目录」，**一个字都没提改名**。
    #
    #    ⚠️ 后果不是"少一行提示"，是**用户找不到自己的文件**：
    #       他照报告去数据目录里找那个长名，而那个名字根本不存在。
    #       而 `Move` 那段注释写着"我们动了人家磁盘上的文件，他有权知道"。
    #
    #    根因：`decide_name` 原来四个 return 各判一次"改没改名"，
    #    而 obs / nav 那两支直接返回 `_dedupe(...)` 的布尔 ——
    #    那个布尔的含义是"**重名了、加了序号**"，不是"改过名"。
    #
    #    > 所以这里**不能只断名字** —— 名字一直是对的（`dst` 一直是真名），
    #    > 错的正是"改过名吗"那一句。三样都要断：
    #    > **落盘名 / renamed / 给用户看的那句话**。
    import json
    import tempfile

    from ginan_agent.ginan import inbox

    box = Path(tempfile.mkdtemp(prefix="inbox_"))
    dest = Path(tempfile.mkdtemp(prefix="dest_"))
    # ⚠️ 文件名和内容里的站号**故意不一样** ——
    #    谁想靠文件名、或者靠内容单独猜，都猜不到正确答案。
    src = box / "ALBH00XXX_R_20251660000_01D_30S_MO.rnx"
    src.write_bytes(_obs_head("ALIC00AUS", 2025, 6, 15))

    dry = inbox.claim(dest_dir=dest, root=box, dry_run=True)
    check(len(dry.moved) == 1, f"预演认领了 1 个（{dry.summary}）")

    r = inbox.claim(dest_dir=dest, root=box)
    landed = sorted(p.name for p in dest.iterdir() if p.is_file())
    check(landed == ["ALIC.rnx"],
          f"落盘的名字是按**内容**算的：{landed}")

    if r.moved and landed:
        m = r.moved[0]
        check(m.dst.name == landed[0],
              f"报告里的名字 == 磁盘上的名字（{m.dst.name}）")
        check(dry.moved[0].dst.name == m.dst.name,
              "★ 预演和真跑给的是**同一个**名字")
        check(m.renamed,
              "★ 报告必须说'改过名'（长名 → ALIC.rnx）")
        check("ALIC.rnx" in m.summary and "改名" in m.summary,
              f"★ 给用户看的那句话里要有真正的落盘名：{m.summary}")

        d = json.loads(inbox.claim_to_json(r))["moved"][0]
        check(d["renamed"] is True, "★ 界面读的是 JSON 那一份，它也必须是 True")
        check(d["to"] == "ALIC.rnx", f"JSON 里的 to 是真名（{d['to']}）")

    # ---- 对照①：名字本来就对的，**不许**说改过名 ----
    box2 = Path(tempfile.mkdtemp(prefix="inbox_"))
    dest2 = Path(tempfile.mkdtemp(prefix="dest_"))
    (box2 / "ALIC.rnx").write_bytes(_obs_head("ALIC00AUS", 2025, 6, 15))
    r2 = inbox.claim(dest_dir=dest2, root=box2)
    check(
        len(r2.moved) == 1 and not r2.moved[0].renamed,
        "对照：名字本来就对 → 不许报成改过名（不然这条守卫只是恒真）",
    )

    # ---- 对照②：**重名加序号**那一路也要报改名 ----
    #
    # ⚠️ 这一条守的是"改对的时候别改过头"：
    #    旧的布尔量的是"加了序号"，新的是"名字变了"。
    #    这里两个文件内容不同（隔了一天）、站号相同 ——
    #    第二个必然拿到 `ALIC_2.rnx`，和原名 `b.rnx` 也不一样。
    box3 = Path(tempfile.mkdtemp(prefix="inbox_"))
    dest3 = Path(tempfile.mkdtemp(prefix="dest_"))
    (box3 / "a.rnx").write_bytes(_obs_head("ALIC00AUS", 2025, 6, 15))
    (box3 / "b.rnx").write_bytes(_obs_head("ALIC00AUS", 2025, 6, 16))
    r3 = inbox.claim(dest_dir=dest3, root=box3)
    names = sorted(p.name for p in dest3.iterdir() if p.is_file())
    check(names == ["ALIC.rnx", "ALIC_2.rnx"],
          f"同站两天 → 第二个加序号（{names}）")
    check(all(m.renamed for m in r3.moved),
          "★ 加序号那一路也要报'改过名'"
          f"（{[m.renamed for m in r3.moved]}）")


def test_inbox_claim_decompresses() -> None:
    section("⑦d ★★ 认领要**解压**：数据目录里不能出现「名叫 .rnx、内容是 gzip」")

    # ⚠️⚠️ 这一节守的是「**同一个形状，第三次出现**」。
    #
    #    `identify.py` 和 `fetch.py` 两处都写着长注释标记过它：
    #        **识别环节认得出（它内部会解压看内容）→ 一路"看起来正常"
    #          → 而磁盘上那个文件仍然是压缩的 → Ginan 读到乱码，
    #            报一个和真正原因毫不相干的错。**
    #
    #    前两次是下载路径（gzip 一次、`.Z` 一次），都修了。
    #    而**收件箱这条最后保险丝**一直是 `shutil.copy2` —— 原样复制。
    #
    #    ⚠️ 而且它已经被**承诺**过了：工具描述里写着
    #       「认领时会自动解压，直接认领即可」，模型照着这句话告诉用户。
    #       **承诺了就要做到** —— 否则比一开始不说更糟。
    import gzip
    import tempfile

    from ginan_agent.ginan import inbox

    # 一份最小的合法 RINEX 2 观测文件（站号 ALIC）
    rows = ["C1", "L1", "D1", "P2", "L2", "D2", "C2", "S1", "S2"]
    head = (
        f"{'     2.11           OBSERVATION DATA    M (MIXED)':<60}"
        f"{'RINEX VERSION / TYPE':<20}\n"
        f"{'ALIC':<60}{'MARKER NAME':<20}\n"
        f"{('%6d' % len(rows)) + ''.join('%6s' % r for r in rows):<60}"
        f"{'# / TYPES OF OBSERV':<20}\n"
        f"{'  2022     3     1     0     0    0.0000000     GPS':<60}"
        f"{'TIME OF FIRST OBS':<20}\n"
        f"{'':<60}{'END OF HEADER':<20}\n"
    )
    OBS = (head + "".join(
        " 22  3  1  0 %2d %10.7f  0  1G01\n" % (i // 2, (i % 2) * 30)
        for i in range(400)
    )).encode()

    box = Path(tempfile.mkdtemp(prefix="inbox_"))
    dest = Path(tempfile.mkdtemp(prefix="dest_"))

    # ⚠️⚠️ 明文那份**必须和 gzip 那份内容不同** —— 原来它俩的字节**一模一样**。
    #
    #     修 ③（同一批里同内容只落一份）之前，"两份都落盘"是对的，
    #     所以下面那条断言一直是绿的。修完之后它**正确地**变成一份，断言就红了。
    #
    #     👉 这不是"守卫被改坏了" —— 是**夹具本身不贴真实**：
    #        现实中没人会把同一份观测数据用两个名字放进同一个收件箱。
    #        而"明文原样复制"这件事**根本不需要靠重复内容来测**。
    #        换个站号（ALIC → DARW），两个目的就都还测得到。
    OBSC = OBS.replace(b"ALIC" + b" " * 56, b"DARW" + b" " * 56)
    check(OBSC != OBS, "★ 明文那份确实**内容不同**（否则测的就是另一件事）")

    (box / "ALIC00AUS_R_20220600000_01D_30S_MO.rnx.gz").write_bytes(gzip.compress(OBS))
    shutil.copy(str(ROOT / "tests/fixtures/igr21992.erp.Z"), box / "igr21992.erp.Z")
    plain = box / "plain.rnx"
    plain.write_bytes(OBSC)

    r = inbox.claim(dest_dir=dest, root=box)

    names = sorted(p.name for p in dest.iterdir())
    check(len(names) == 3, f"三份都认领了（{names}）")

    # ---- ① gzip：内容必须是明文，名字不能带 .gz ----
    got = dest / "ALIC.rnx"
    if check(got.is_file(), f"gzip 那份落在 `ALIC.rnx`（实际 {names}）"):
        blob = got.read_bytes()
        check(
            blob[:2] != b"\x1f\x8b",
            f"★★ 内容**是明文**，不是 gzip 流（头两字节 {blob[:2]!r}）"
            "—— 压缩着落盘的话 Ginan 会读到乱码，"
            "报一个和真正原因毫不相干的错",
        )
        check(blob == OBS, "★★ 而且解出来的内容和原件**一模一样**")

    # ---- ② .Z：同上，而且名字不能变成 `.erp.erp` ----
    z = dest / "igr21992.erp"
    check(z.is_file(), f"`.Z` 那份落在 `igr21992.erp`（实际 {names}）")
    check(
        not (dest / "igr21992.erp.erp").exists(),
        "★★ 没有出现 `.erp.erp` 这种双扩展名"
        "（压缩后缀不该参与命名 —— 落盘的是解压后的内容）",
    )
    if z.is_file():
        check(z.read_bytes()[:2] != b"\x1f\x9d", "★★ `.Z` 那份内容也是明文")

    # ---- ③ 没压缩的：**一点行为都不能变** ----
    #
    #     ⚠️ 反过度修正。加解压的时候最容易顺手改成"所有文件都过一遍
    #        解压器"，而明文文件过解压器就是错的。
    darw = dest / "DARW.rnx"
    check(darw.is_file(), f"明文那份落在 `DARW.rnx`（实际 {names}）")
    if darw.is_file():
        check(
            darw.read_bytes() == OBSC,
            "★★ 明文那份**一个字节都没动**（过了解压器就会被改坏）",
        )

    # ---- ④ 原件必须都还在 ----
    check(
        sorted(p.name for p in box.iterdir())
        == ["ALIC00AUS_R_20220600000_01D_30S_MO.rnx.gz", "igr21992.erp.Z", "plain.rnx"],
        "★ 认领是**复制**：三份原件都还留在收件箱里",
    )

    # ---- ⑤ 坏掉的压缩包：说清楚 + **不留半个文件** ----
    bad_box = Path(tempfile.mkdtemp(prefix="badbox_"))
    bad_dest = Path(tempfile.mkdtemp(prefix="baddest_"))
    full = gzip.compress(OBS + b"x" * 200000)
    (bad_box / "half.rnx.gz").write_bytes(full[: int(len(full) * 0.6)])

    rb = inbox.claim(dest_dir=bad_dest, root=bad_box)
    check(
        len(rb.skipped) == 1,
        f"★★ 解压失败的那份被跳过，没有当成认领成功（{rb.summary}）",
    )
    if rb.skipped:
        reason = rb.skipped[0].reason
        check(
            "解压" in reason,
            f"★★ 而且说清楚是**解压**失败，不是笼统的'复制不过去'"
            f"（{reason[:50]}）",
        )
    check(
        not list(bad_dest.iterdir()),
        f"★★ 失败后**不留半个文件**（实际 {[p.name for p in bad_dest.iterdir()]}）"
        "—— 半个文件会冒充成完整的，Ginan 读到一半才报错",
    )


def test_inbox_clear_reports_failures() -> None:
    section("⑦b2 ★★★ 清空收件箱：删不掉的**必须说出来**，临时占用的要重试")

    # ⚠️⚠️ 2026-09-21 用户报的第二个现象（第一个是"整个清不掉"）：
    #
    #      点了新任务，收件箱里**还剩一个文件**
    #      （`COCO00AUS_R_20262450000_01D_30S_MO.rnx`，55 MB）。
    #      几分钟后手动删同一个文件，**一次就删掉了** —— 所以是**临时占用**。
    #
    #    而当时用户看不到任何提示。两件事一起坏了：
    #      ① `ClearResult.summary` 只数 deleted / kept / deleted_dirs，
    #         **`errors` 一个字都不提** → 报的是"删掉 192 个文件，34 个空目录"
    #      ② `newTask()` 拿到 reset 的返回**直接丢掉**，然后 `location.reload()`
    #         → 那份报告从来没到过用户眼前（`grep 'r.cleared' app.js` 是空的）
    #
    #    ⚠️ 为什么这不是罕见情况：Windows 上 `unlink()` 只要**任何进程**
    #       没带 FILE_SHARE_DELETE 打开过它就会失败（WinError 32），
    #       而 Python 的 `open()` 正好不给那一位。
    #       而这个程序自己就常开着收件箱里的文件 —— `/api/inbox`
    #       每刷新一次面板就要读每个文件的头部（`.crx` 还要解码，一份几秒）。
    #
    #    👉 两条路都要有：**临时的重试救回来**，**真删不掉的报出来**。
    import tempfile
    import threading

    from ginan_agent.ginan import inbox

    with tempfile.TemporaryDirectory() as td:
        box = Path(td) / "inbox"
        box.mkdir()

        # ---- ① 一直被占着 → 删不掉，而且**必须报出来** ----
        stuck = box / "占着的.rnx"
        stuck.write_bytes(b"x" * 1000)
        (box / "好删的.rnx").write_bytes(b"y" * 1000)

        held = stuck.open("rb")          # 抓着不放（Python 的 open 就够）
        try:
            r = inbox.clear_inbox(box)
        finally:
            held.close()

        check(len(r.deleted) == 1, f"★ 好删的那个删掉了（{r.summary}）")
        check(
            bool(r.errors) and "占着的.rnx" in r.errors[0],
            f"★★★ 删不掉的那个**进了 errors、而且带着原因**：{r.errors}",
        )
        check(
            "删不掉" in r.summary,
            "★★★ **summary 里也说了** —— 用户看得到的只有这一句"
            f"（{r.summary}）",
        )
        check(stuck.is_file(), "★ 没删掉就是没删掉，不假装删了")
        # ⚠️ 这条盯的是"一个都没删掉"那种情况：那时 deleted 是空的，
        #    而 summary 原来会走"收件箱本来就是空的"那个分支 —— **说反话**。
        check(
            "本来就是空的" not in r.summary or r.deleted,
            f"★ 没说『本来就是空的』（有东西没删掉的时候那句话是假的）",
        )

        # ---- ② 只是**临时**占着 → 重试要救回来 ----
        #
        # ⚠️ 这一条对着的就是用户那一次：占用只持续了一瞬间
        #    （面板正在读那个文件），等一下就好了。
        brief = box / "临时占着的.rnx"
        brief.write_bytes(b"z" * 1000)
        h = brief.open("rb")
        threading.Timer(0.1, h.close).start()
        try:
            r2 = inbox.clear_inbox(box)
        finally:
            # ⚠️ 一定要在离开 `with` **之前**松手。
            #    只靠那个 Timer 的话，没有重试时整个流程快过 0.1 秒，
            #    临时目录的清理会撞上一个还开着的文件 —— 于是测试
            #    **抛异常而不是报红**（变异测试里踩到过）。
            #    `close()` 是幂等的，Timer 已经关过也没关系。
            h.close()

        check(
            not brief.exists(),
            f"★★ 临时占用**重试之后删掉了**（不重试的话它会留下 —— "
            f"用户看到的就是『还在』）（{r2.summary}）",
        )
        check(not r2.errors, f"★ 而且没报错（{r2.errors}）")


def test_clear_runs_removes_the_whole_run_dir() -> None:
    section("⑦c ★★★ 「新任务」要清的第三处：运行目录（`userdata/runs/`）")

    # ⚠️⚠️ 守的是一句**说了很久没兑现的话**。
    #
    #    「新任务」那个确认框里一直写着
    #        「⚠️ 会删掉：… · 任务状态（计划 / 配置 / 运行记录）」
    #    而实际上**运行目录一个字节都没删过**：`runs_dir()` 全项目只有
    #    "建目录"（`workspace.work_dir`）和"报路径"两种用法。
    #
    #    实测（2026-10-02）：12 天、41 次运行、**14 GB**。其中 99% 是
    #    求解器自己写的中间文件（`Filter-*.rts_forward` 6.86 GB +
    #    每站一个的 `.TRACE` 6.15 GB），真正的记录只有 0.12 GB。
    #    用户就是这么问出来的：
    #        "每次点新任务，之前解算的产生的中间产物，没用的东西都删除了吗"
    #
    #    👉 这一节盯三件事：**整个删掉**、**邻居不许被连带**、
    #       **删不掉要说出来**（Windows 上占用是真的会发生，见 ⑦b2）。
    import tempfile

    from ginan_agent.ginan import runs as gruns

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "runs"
        rd = root / "20260101-000000-abcd"
        (rd / "sub").mkdir(parents=True)
        (rd / "ALBH_20250615.POS").write_bytes(b"pos" * 100)
        (rd / "sub" / "Filter-x.rts_forward").write_bytes(b"r" * 4096)
        other = root / "20260101-010000-ef01"
        other.mkdir()
        (other / "a.TRACE").write_bytes(b"t" * 2048)

        # 邻居：**不在** runs/ 里，绝不能被连带
        (Path(td) / "logs").mkdir()
        (Path(td) / "logs" / "server.log").write_text("别删我", encoding="utf-8")

        r = gruns.clear_runs(root)

        check(not rd.exists(), "★★ 运行目录**整个**删了（不是只删中间文件）")
        left = [str(p.relative_to(root)) for p in root.rglob("*")]
        check(left == [], f"★★ runs/ 里一个文件都不留（还剩 {left}）")
        check(root.is_dir(), "★ runs 目录本身留着（下次还要往里写）")
        check(
            (Path(td) / "logs" / "server.log").is_file(),
            "★★ 对照：它的邻居（logs/）没被连带删 —— 这是删用户文件，"
            "范围必须收在 runs/ 里面",
        )
        check(len(r.deleted_dirs) == 2, f"★ 两个运行目录都撤了（{len(r.deleted_dirs)} 个）")
        check(
            bool(r.bytes) and "KB" in r.summary,
            f"★★ 报出**省了多少**（{r.summary}）—— 用户看不到字节数就不知道"
            "他刚清掉的是几百 MB 还是几 GB",
        )

        # ---- 删不掉必须报出来（Windows 上占用真会发生）----
        stuck_dir = root / "20260102-000000-ffff"
        stuck_dir.mkdir()
        stuck = stuck_dir / "占着的.POS"
        stuck.write_bytes(b"x" * 100)
        held = stuck.open("rb")          # 抓着不放（Python 的 open 就够）
        try:
            r2 = gruns.clear_runs(root)
        finally:
            # ⚠️ 一定要在离开 `with` **之前**松手 —— 见 ⑦b2 里同一条说明。
            held.close()
        check(
            bool(r2.errors) and "占着的.POS" in r2.errors[0],
            f"★★★ 占着不放的那个**进了 errors、带着原因**：{r2.errors}",
        )
        check(stuck.is_file(), "★ 没删掉就是没删掉，不假装删了")


def test_inbox_clear_all() -> None:
    section("⑦b ★★ 清空收件箱：**全都删**，一个文件都不留")

    # ⚠️⚠️ 用户 2026-09-21 的原话，连着三句：
    #      「inbox文件夹重置应该是清空，我点新任务里边还有东西」
    #      「没认领也删除」
    #      「点新任务什么都不留」
    #
    #    上一版（`clear_claimed()`）只删"能证明数据目录里有一份"的，
    #    而那条判据**把自己废掉了**：它依赖 data/ 里那份，
    #    而同一个"新任务"流程紧接着就把 data/ 里的观测数据删了 ——
    #    第二次重置再问就永远问不出答案。实测用户那一箱（193 个文件）：
    #    **删 0、留 193**，而且此后每一次重置都是这个数。
    #
    #    👉 现在的判据是"**在不在收件箱里**"，不依赖任何别的东西。
    #
    #    这一节盯五件事，缺一件这个修复就是假的：
    #      ① **深处的文件也要删** —— 走 `scan()` 的话它有 MAX_DEPTH 上限，
    #         深处那几个会活下来，而"清空之后还剩几个"正是用户报的现象
    #      ② 没认领的、认不出来的、隐藏的、Thumbs.db —— 一样删
    #      ③ **目录也要撤** —— 只删文件的话收件箱看起来还是满的
    #      ④ 数据目录**一个字节都不动**
    #      ⑤ 一个字节都不许出收件箱（junction 那一节）
    import tempfile

    from ginan_agent.ginan import inbox

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        box = root / "inbox"
        dest = root / "data"
        dest.mkdir()
        (dest / "igs20.atx").write_bytes(b"ATX-product")

        # ① 嵌到第 7 层 —— `scan()` 那套 MAX_DEPTH 在深处会漏
        deep = box / "data" / "obs" / "rinex3" / "2026" / "244" / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "深处的.rnx").write_bytes(b"D" * 50)

        # ② 没认领过的 / 认不出来的 / 隐藏的 / 系统的
        (box / "随手放的.bin").write_bytes(b"x" * 10)
        (box / "data" / ".hidden").write_bytes(b"h")
        (box / "Thumbs.db").write_bytes(b"t")

        before = len([p for p in box.rglob("*") if p.is_file()])
        r = inbox.clear_inbox(box)

        check(r.errors == [], f"清空没报错（{r.errors}）")
        check(
            len(r.deleted) == before,
            f"★★ 一个不剩：清掉 {len(r.deleted)}/{before} 个文件（{r.summary}）",
        )
        check(
            not [p for p in box.rglob("*") if p.is_file()],
            "★★ 收件箱里没有任何文件了 —— 认领过的、没认领的、最深处那些都在内",
        )
        check(
            list(box.iterdir()) == [],
            "★★ 目录也撤干净了（只删文件的话，那个文件夹看着还是满的）",
        )
        check(
            len(r.deleted_dirs) >= 7,
            f"★ 撤掉的空目录数对得上（{len(r.deleted_dirs)} 个）",
        )
        check(
            (dest / "igs20.atx").is_file(),
            "★ 数据目录**一个字节都没动**（那是缓存，归 clear_observations 管）",
        )

        # --- 收件箱不存在（用户还没放过东西）→ 不该炸 ---
        check(
            not inbox.clear_inbox(root / "根本没有").deleted,
            "★ 收件箱不存在时不炸，也没说删了东西",
        )

        # =====================================================================
        # ⑤ 一个字节都不许出收件箱
        # =====================================================================
        #
        # ⚠️⚠️ 这一条是**实测出来的**，不是防患于未然：
        #
        #     Windows 的 junction（`mklink /J`）指向收件箱**外面**时：
        #
        #        is_symlink()   = **False**   ← 认不出来是链接
        #        is_dir()       = True        ← 看着就是个普通子目录
        #        reparse 标志   = True        ← 只有这一条认得出
        #        os.walk(followlinks=False)   →  **走进去了**，
        #                                        报出了外面的文件
        #
        #     👉 如果用 `os.walk`（哪怕是默认的 `followlinks=False`），
        #        它会把收件箱外面的文件当自己的删掉。
        #        所以 `clear_inbox` 是手写的遍历 + `_is_link()` 显式判两道。
        outside = root / "外面"
        outside.mkdir()
        (outside / "别删我.txt").write_text("keep")

        link = box / "门"
        made = _make_dir_link(link, outside)
        # ⚠️ 建不出来就**报红**，不做"静默跳过" ——
        #    跳过的话这条路上没有守卫，而它一旦坏了是**删到收件箱外面**。
        check(made, "★ 建得出目录链接（建不出的话这一节等于没测）")
        if made:
            r2 = inbox.clear_inbox(box)
            check(
                (outside / "别删我.txt").is_file(),
                "★★★ 链接指向的东西**纹丝不动** —— 那是收件箱外面的文件",
            )
            check(
                not list(box.iterdir()),
                f"★ 但那个链接本身被撤掉了（{r2.summary}）",
            )


def _make_dir_link(link: Path, target: Path) -> bool:
    """建一个指向目录的链接（Windows 用 junction —— 不需要管理员）。"""
    import os
    import subprocess

    if os.name == "nt":
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return r.returncode == 0 and link.exists()
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def test_identify_bia_and_met() -> None:
    section("⑦n ★★ 码偏差（BIA）和气象文件（MET）要认得出来")

    # ⚠️⚠️ 这两类是 2026-09-21 补的，补之前**都认不出来**。
    #
    #   · `bia` —— 这个更值得记一笔：**词表里有它，辨认层却产不出它**。
    #
    #       `catalog.KINDS` 有 `bia`（下载那一路会产出它）、
    #       `advise.OPTIONAL_KINDS` 也读它 —— 而 `identify` 一条规则都没有。
    #       于是 `kind == "bia"` **永远不可能为真**：
    #         - 「还缺 bia」这句话**永远成立**，哪怕文件就在手边
    #         - 用户丢一个 `.BIA` 进收件箱 → 判成"认不出来" → 认领不了
    #
    #       > 这是「**后端从不返回某个键，调用方一直在读**」第二次出现
    #       > （第一次是 `compression == "hatanaka"`）。
    #
    #   · `met` —— 用户那一箱里有 9 个气象文件，**全被报成"认不出来"**。
    #       它们是完全标准的 RINEX MET，而他自己的 README 里写着
    #       "用来算对流层 ZTD"。他没法理解程序为什么认不出。
    #
    # ⚠️ 夹具用的是**真文件的字节**（出处写在 `tests/fakes/heads.py` 里），
    #    不是编的 —— 编的夹具恰好测不出"真实文件认不出来"这一类问题。
    import gzip as _gz
    import tempfile

    from ginan_agent.ginan import identify, inbox

    tmp = Path(tempfile.mkdtemp(prefix="ident_"))
    for name, kind in (("bia", "bia"), ("met", "met")):
        p = tmp / f"{name}.bin"
        p.write_bytes(REAL_HEADS[name])
        ident = identify.identify(p)
        check(
            ident.kind == kind,
            f"★★ {name} 认得出是 {kind!r}（实际 {ident.kind!r}，"
            f"依据 {ident.evidence!r}）",
        )
        check(
            ident.is_data and ident.usable,
            f"★ {name} 是**可用数据**（不是 html 那种「认出来了但不能用」）",
        )

    # ---- 认出来了就得能认领 ----
    #
    # ⚠️ 补规则**会打开一条新的代码路径**（这两类文件从"认不出、
    #    留在原地"变成"能认领、会进数据目录"）。新路径必须有守卫 ——
    #    否则它会以别的方式坏掉（改错名、解压失败、落成怪名字）。
    with tempfile.TemporaryDirectory() as td:
        box = Path(td) / "inbox"
        dest = Path(td) / "data"
        box.mkdir()

        (box / "HKWS00HKG_R_20262440000_01D_MM.rnx.gz").write_bytes(
            _gz.compress(REAL_HEADS["met"])
        )
        r = inbox.claim(dest_dir=dest, root=box)
        check(len(r.moved) == 1, f"★ 气象文件认得领（{r.summary}）")
        check(
            (dest / "HKWS00HKG_R_20262440000_01D_MM.rnx").is_file(),
            f"★★ 而且落成了解压后的正名（实际 {[p.name for p in dest.iterdir()]}）",
        )


def test_erp_two_spellings() -> None:
    section("⑦f ★ ERP 的两种写法都要认得（ESA 用大写）")

    # ⚠️⚠️ 这和上面 nav 那次是**同一个教训**，只是换了个文件类型：
    #
    #     IGS/BKG 的 ERP 第一行：  `version 2`   （小写）
    #     ESA 的 ERP 第一行：      `VERSION 2`   （大写）
    #
    #     而识别是 `first.startswith(marker)`，**大小写敏感**。
    #
    #     ⚠️ 实测是怎么发现的：接 ESA 当备选产品时，
    #        `ESA0OPSFIN_…_ERP.ERP.gz` 下载成功、却报"内容对不上"，
    #        于是被丢弃 —— 现象是"ERP 下载失败"，
    #        而原因在识别规则表里少了大写那一行。
    #
    #     👉 代价不对称：**多一行规则**几乎不要钱，
    #        少一行则会让一整类文件永远认不出来，而且报错指向别处。
    import tempfile

    from ginan_agent.ginan import identify

    tmp = Path(tempfile.mkdtemp(prefix="erp_"))
    for label, head in (("小写 version 2", REAL_HEADS["erp"]),
                        ("大写 VERSION 2", _heads.ERP_ESA)):
        p = tmp / "x.gz"
        p.write_bytes(head)
        ident = identify.identify(p)
        check(
            ident.kind == "erp" and ident.known,
            f"★ {label} 认得出是 ERP（实际 {ident.kind!r}）",
        )


def test_whu_trailer_and_resolver() -> None:
    section("⑦e ★ 武大：尾部垃圾要切掉；查不到时**不能装作查到了**")

    # ⚠️⚠️ 这一节守的是 2026-09-20 新接的源（武汉大学 IGS 数据中心）。
    #     它是中国大陆最快的源（实测比 BKG 快 20~30 倍），
    #     但它有两个**只属于它**的毛病，不守就会静默出错。
    #
    # ⚠️ 为什么这两个必须**行为**测、不能结构测：
    #    它们都是"数据变形"类的 bug —— 语法全对、名字全对，
    #    错的是**字节**。读源码看不出来，只有真跑一遍才知道。
    import gzip
    import tempfile

    from ginan_agent.ginan import sources

    tmp = Path(tempfile.mkdtemp(prefix="whu_"))

    # ---- ① 尾部追加的调试 HTML，必须切掉 ----
    payload = b"hello whu\n" * 500
    stream = gzip.compress(payload)
    junk = b'<div id="think_page_trace">' + b"x" * 19489

    f = tmp / "good.gz"
    f.write_bytes(stream + junk)

    got = sources.strip_trailer("whu", f)
    check(got, "★ 认出了尾部垃圾并切掉")
    check(
        f.stat().st_size == len(stream),
        f"★ 切到 gzip 流的末尾，不多不少（{f.stat().st_size} == {len(stream)}）",
    )
    try:
        check(
            gzip.decompress(f.read_bytes()) == payload,
            "★ 切完之后能正常解压，内容一字不差",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"★ 切完能解压 —— 实际抛了 {type(exc).__name__}")

    # ---- ② 干净的 gzip 不能被动 ----
    g = tmp / "clean.gz"
    g.write_bytes(stream)
    check(
        not sources.strip_trailer("whu", g) and g.read_bytes() == stream,
        "★ 没有垃圾的文件**一个字节都不动**",
    )

    # ---- ③ 别的源不许被这个逻辑碰到 ----
    #
    # ⚠️ 这条挡的是"把武大的补丁撒到所有源上"——
    #    那会让别的源的文件在**恰好含有同样字节串**时被莫名截断。
    h = tmp / "other.gz"
    h.write_bytes(stream + junk)
    check(
        not sources.strip_trailer("bkg", h) and h.read_bytes() == stream + junk,
        "★ 非武大的源**不碰**（补丁只给需要它的那个源）",
    )

    # ---- ④ 查不到时必须返回 None，不能瞎编一个 URL ----
    #
    # ⚠️ 这个场景**真实发生过**：`catalog` 拼 token 时用的是**落盘名**
    #    （`…_ORB.SP3`），而武大库里存的是**远端名**（`…_ORB.SP3.gz`）——
    #    少一个后缀就查不到。而它失败的方式是**静默降级到下一个源**，
    #    表面上一切正常（文件下下来了、内容也对），只是**一直是慢的那个**。
    #
    #    > **降级如果是静默的，就没人会发现它在降级。**
    from datetime import date

    class FakeResp:
        def __init__(self, rows):
            self.status_code = 200
            self._rows = rows

        def json(self):
            return {"rows": self._rows}

    class FakeSession:
        def __init__(self, rows):
            self._rows = rows
            self.posted = []

        def post(self, url, **kw):
            self.posted.append(url)
            return FakeResp(self._rows)

    sources.reset_cache()
    rows = [{"filename": "IGS0OPSRAP_20262450000_01D_15M_ORB.SP3.gz",
             "id": 310281, "year": 2026}]
    sess = FakeSession(rows)

    tok = sources.lookup_token("whu", "jmxl", "IGS0OPSRAP_20262450000_01D_15M_ORB.SP3.gz")
    url = sources.resolve(tok, date(2026, 9, 2), session=sess)
    check(
        url is not None and "id=310281" in url and "year=2026" in url,
        f"★ 查得到时拼出正确的下载地址（{url}）",
    )
    check(
        len(sess.posted) == 1,
        "★ 一次查询就够（同一天的文件清单是一次性拉的，不是每个文件查一次）",
    )

    # 落盘名（不带 .gz）查不到 —— **这就是那个真实 bug 的形状**
    sources.reset_cache()
    wrong = sources.lookup_token("whu", "jmxl", "IGS0OPSRAP_20262450000_01D_15M_ORB.SP3")
    check(
        sources.resolve(wrong, date(2026, 9, 2), session=FakeSession(rows)) is None,
        "★ 名字对不上时返回 None（**不能瞎猜一个地址**）",
    )

    # ---- ⑤ 网络异常也不能炸出去 ----
    class BoomSession:
        def post(self, url, **kw):
            raise ConnectionError("模拟网络不通")

    sources.reset_cache()
    check(
        sources.resolve(tok, date(2026, 9, 2), session=BoomSession()) is None,
        "★ 网络故障返回 None（让它安静地降级到下一个源，而不是崩掉整次下载）",
    )

    # ---- ⑥ 没有日期就查不了（要如实返回 None）----
    sources.reset_cache()
    check(
        sources.resolve(tok, None, session=FakeSession(rows)) is None,
        "★ 没有日期 → None（它按天索引，猜日期等于查错文件）",
    )

    sources.reset_cache()


def test_cache_drops_observations_keeps_products() -> None:
    section("⑦d ★ 缓存：观测数据不永久留存，产品永远留")

    # ⚠️ 用户连着说了三遍才把它说清，值得原样记下来：
    #
    #     「缓存不用存以往的观测数据，就存 IGS0OPSRAP_..._CLK.CLK 这种的」
    #     「新任务时删除」
    #     「**不是不放里**」
    #     「不永久留存」
    #
    #   拆开：观测数据**可以**进数据目录（Ginan 要从那儿读，`obs_root`），
    #   但它是**当前任务的作业材料**，不是可复用的产品 —— 新任务一开就该走。
    #
    # ⚠️ 判据必须是**识别出来的 kind**，不能按扩展名 ——
    #    广播星历 `BRDC00IGS_*.rnx` 也是 `.rnx`，但它是**产品**，得留。
    #    这条断言里专门放了它，就是为了钉死这一点。
    import tempfile

    from ginan_agent.ginan import inbox

    dest = Path(tempfile.mkdtemp(prefix="cache_"))

    # 用户自己的观测数据 → 该走
    (dest / "ALIC.rnx").write_bytes(REAL_HEADS["obs"])
    # 产品 → 全留（.rnx 的 nav 混在里面，专门用来试"按扩展名判断"这个坑）
    for name, kind in (
        ("IGS0OPSRAP_20260010000_01D_05M_CLK.CLK", "clk"),
        ("IGS0OPSRAP_20260010000_01D_15M_ORB.SP3", "sp3"),
        ("BRDC00IGS_R_20260010000_01D_MN.rnx", "nav"),
        ("igs20.atx", "atx"),
        ("igr21992.erp", "erp"),
    ):
        (dest / name).write_bytes(REAL_HEADS[kind])

    # 认不出来的 → 绝不删
    (dest / "随手放的东西.bin").write_bytes(b"\x00\x01\x02" * 100)

    r = inbox.clear_observations(dest_dir=dest)

    check(
        [p.name for p in r.deleted] == ["ALIC.rnx"],
        f"★ 只删了观测数据（实际删了 {[p.name for p in r.deleted]}）",
    )
    check(not (dest / "ALIC.rnx").exists(), "观测文件确实没了")

    for name in (
        "IGS0OPSRAP_20260010000_01D_05M_CLK.CLK",
        "IGS0OPSRAP_20260010000_01D_15M_ORB.SP3",
        "igs20.atx",
        "igr21992.erp",
    ):
        check((dest / name).is_file(), f"产品留着：{name}")

    check(
        (dest / "BRDC00IGS_R_20260010000_01D_MN.rnx").is_file(),
        "★ 广播星历（`.rnx`）也留着 —— 按扩展名判断的话这个会被误删",
    )
    check(
        (dest / "随手放的东西.bin").is_file(),
        "★ 认不出来的**一根汗毛都不动**",
    )


def test_inbox_same_size_different_content() -> None:
    section("⑦c ★ 大小相同但内容不同，绝不能当成'已认领'")

    # ⚠️ 只比大小是不够的：两个不同的文件完全可能一样长。
    #    判错了就是**删掉用户唯一的一份**。
    import tempfile

    from ginan_agent.ginan import inbox

    box = Path(tempfile.mkdtemp(prefix="inbox_"))
    dest = Path(tempfile.mkdtemp(prefix="dest_"))
    a = box / "a.bin"
    a.write_bytes(b"AAAA" * 100)

    # 数据目录里放一个**同样大小、不同内容**的文件
    (dest / "b.bin").write_bytes(b"BBBB" * 100)

    pairs = inbox.claimed_pairs(inbox.scan(box), dest_dir=dest)
    check(
        not pairs,
        "★ 大小相同内容不同 → **配不上对**（判成配对的话，界面会说好了）",
    )

    # ⚠️⚠️ **长度不同、但前缀完全相同** —— 这一条是专门盯着
    #     "指纹里有没有算上大小"的。
    #
    #     指纹是"大小 + 前 1 MB 的哈希"。如果**只留哈希**，
    #     下面这两个文件会被判成同一个 —— 因为指纹只看前 1 MB，
    #     而这两个文件的**前 1 MB 逐字节相同**。
    #     于是收件箱那份被删掉，而它比数据目录那份**长一倍**，
    #     多出来的部分是数据目录里根本没有的。
    #
    #     ⚠️⚠️ 两个文件都必须**大于 1 MB**，否则哈希自己就能区分开，
    #        这条用例就白写了 —— 第一版就是这么写的（200 KB vs 100 KB），
    #        把 size 从指纹里去掉**照样全绿**。
    #
    #     **判据要挑"只有这个修复才能解释的现象"。**
    box2 = Path(tempfile.mkdtemp(prefix="inbox_"))
    dest2 = Path(tempfile.mkdtemp(prefix="dest_"))
    long_one = box2 / "long.bin"
    long_one.write_bytes(b"X" * 2_500_000)          # 前 1 MB 是 X
    (dest2 / "short.bin").write_bytes(b"X" * 1_500_000)   # 前 1 MB 也是 X

    pairs2 = inbox.claimed_pairs(inbox.scan(box2), dest_dir=dest2)
    check(
        not pairs2,
        "★ 前缀相同但长度不同 → 配不上对（指纹必须把大小算进去）",
    )


# =============================================================================
# 主程序
# =============================================================================


def test_already_claimed_survives_copy_semantics() -> None:
    section("㉑ ★ 「文件还在收件箱里」≠「还没认领」")

    # ⚠️⚠️ 用户的原话：
    #      "我点看看我的数据，程序认领了我的数据后，
    #       右下角收件箱就不要提示认领了"
    #
    #     根因：认领是**复制**（原件留在收件箱，用户不会丢东西），
    #     所以认领完之后**收件箱看起来还是满的** →
    #     界面只看 count 的话，那个按钮**永远在** → 用户以为没成功，反复点。
    #
    #     👉 "文件在收件箱里" 和 "还没认领过" 是**两个问题**。
    #
    # ⚠️ 两个问法（`already_claimed` / `claimed_pairs`）的判据必须**完全一样**
    #    （内容指纹）—— 否则会出现"一边说认领过了、另一边说没有"。
    import tempfile

    from ginan_agent.ginan import inbox

    with tempfile.TemporaryDirectory() as td:
        inbox_dir = Path(td) / "inbox"
        data_dir = Path(td) / "data"
        inbox_dir.mkdir()
        data_dir.mkdir()

        obs = REAL_HEADS["obs"]

        # 收件箱里一份，数据目录里**同样内容但换了个名字**
        # （认领会按内容改名，所以名字一定不一样 —— 只能比内容）
        (inbox_dir / "我下的.alic.rnx").write_bytes(obs)
        (data_dir / "ALIC.rnx").write_bytes(obs)

        items = inbox.scan(inbox_dir)
        check(len(items) == 1, "扫到 1 个文件")

        done = inbox.already_claimed(items, dest_dir=data_dir)
        check(
            len(done) == 1,
            "★ 名字完全不同，但内容一样 → 判为**已认领**（按内容比，不按名字）",
        )

        # 反向：内容不同（大小也不同）→ 不算已认领
        (inbox_dir / "别的.rnx").write_bytes(obs + b"\n" + b"Z" * 500)
        items2 = inbox.scan(inbox_dir)
        done2 = inbox.already_claimed(items2, dest_dir=data_dir)
        check(
            len(done2) == 1,
            "★ 内容不同的那份**不算**已认领（否则界面会骗用户说已经好了）",
        )

        # 而且 `claimed_pairs` 用的是同一个判据 —— 这两个答案**必须一致**
        # （一个说"认领过了"，另一个配不上对的话，用户会照着一个去操作）
        #
        # ⚠️ 取键用 `pairs.keys()`（`claimed_pairs` 返回的是 `{收件箱: 数据目录}`）——
        #    别照着自己的想象写。**断言写了个不存在的键 = 永远为假**，
        #    而测试看起来"在测东西"。（这个坑今天在别处踩过一次。）
        pairs = inbox.claimed_pairs(items2, dest_dir=data_dir)
        paired = {p.name for p in pairs}
        expected = {p.name for p in done2}
        check(
            paired == expected and bool(expected),
            f"★ 两个判据给同一个答案（配对上 = 判为已认领的）："
            f"{paired} vs {expected}",
        )

        # ---- 数据目录里那份被删了 → 判据必须改口 ----
        #
        # ⚠️ 这一条盯的是"按钮会不会回来"：用户把缓存删了之后，
        #    界面**必须重新显示「认领」**。
        #    判据说"还认领过"的话，那个按钮再也不出现，用户就没法把数据拿回来了。
        #
        # （上一版这里测的是"收件箱那份**不删**" —— 那是 `clear_claimed` 时代的
        #   行为：判据依赖 data/ 里那份，所以删了就"证不动"。现在清空收件箱
        #   不看 data/，那条断言没有对象了；**但判据本身还是要对**。）
        for p in data_dir.iterdir():
            p.unlink()                      # 模拟"用户把缓存删了"

        check(
            not inbox.already_claimed(items2, dest_dir=data_dir),
            "★★ 数据目录那份没了 → 判据改口说『没认领』（按钮才会回来）",
        )


def test_tables_endpoint_and_paths() -> None:
    section("⑳ ★ 模型文件：换对了端点，而且路径挡得住越界")

    # ⚠️⚠️ 这一节守两件**互相独立**的事，都踩过：
    #
    #     ① **端点**：S3 的**区域端点**（`peanpod.s3.ap-southeast-2.…`）
    #        在国内直连超时（12 秒），而**全球端点**（`peanpod.s3.…`）
    #        200、1.9 秒。**同一个桶**。
    #
    #        而代码里原来写着"无代理时超时 → 它是'有代理才能用'的源" ——
    #        那条记录让每个用户以为必须挂代理，于是根本不会去试。
    #
    #     ② **路径**：模型文件名自带 `tables/` 前缀，而下载器里
    #        `dest_dir / "tables/x"` 的父目录不存在 —— 失败会发生在
    #        **下载完成之后**（白下 17 MB）。而放宽路径拼接又踩过另一个坑：
    #        空文件名让 `dest_dir / ""` 等于目录本身，于是"删那个文件"
    #        变成了"删那个目录"。
    from ginan_agent.ginan import catalog
    from ginan_agent.ginan import fetch as gfetch

    # ---- ① 端点 ----
    url = catalog.ginan_s3_table_url("gpt_25.grd")
    check(
        "peanpod.s3.amazonaws.com" in url,
        f"★ 默认端点是**全球**端点（实际：{url}）",
    )
    check(
        "ap-southeast-2" not in url,
        "★ 默认**不是**那个在国内连不上的区域端点",
    )
    # 区域端点仍然作为备选留着 —— 哪天全球的也不通了还有一条路
    tables = catalog.plan_tables()
    check(
        any("ap-southeast-2" in u for u in tables[0].urls.values()),
        "区域端点仍然作为备选留着",
    )
    check(
        list(tables[0].urls)[0] == "ginan_s3",
        "★ 而且**排在最前面**（dict 顺序 = 优先级）",
    )

    # ---- ② 清单形状 ----
    check(len(tables) == 10, f"清单是 10 个（实际 {len(tables)}）")
    check(
        all(t.filename.startswith("tables/") for t in tables),
        "每个都带 tables/ 前缀（Ginan 配置里就是这么引用的）",
    )
    check(
        all(t.urls and all(u.endswith(".gz") for u in t.urls.values()) for t in tables),
        "每个都有源，且都是 .gz（桶里存的就是压缩的）",
    )

    # ---- ③ 路径：挡得住越界 ----
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        dest = Path(td)

        # 合法的：正常文件名、带子目录的、再深一层的
        for good in ("a.grd", "tables/a.grd", "tables/qzss_ohi/ohi-qzs1.txt"):
            p = gfetch._local(dest, good)
            check(Path(p).is_relative_to(Path(dest).resolve()),
                  f"接受 {good} 且落在 dest 里")

        # ⚠️ 父目录要**自动建出来** —— 不建的话失败发生在下载完之后
        check((dest / "tables").is_dir(), "★ 父目录被自动建出来了")

        # 危险的：一律拒绝
        for bad in ("", "a/", "../x.grd", "/abs/x.grd", "C:/x.grd",
                    "tables/../../x", "./"):
            try:
                gfetch._local(dest, bad)
                check(False, f"★ 拒绝 {bad!r}")
            except ValueError:
                check(True, f"★ 拒绝 {bad!r}")

        # ⚠️⚠️ Windows 上 `/abs/x.grd` **不是** `is_absolute()`（它有根没有盘符），
        #     但它拼接时照样会甩掉左边 —— 这条是写测试时才发现的。
        import inspect

        check(
            "p.root" in inspect.getsource(gfetch._local),
            "★ 判据里看了 `p.root`（光看 is_absolute() 会漏掉 Windows 的 /abs/x）",
        )

    # ---- ④ 下载入口也会拦（不是只有 _local 拦） ----
    #
    # ⚠️ 这一条要的是"**一个字节都不下**"：等下载完才发现写不下去，
    #    用户已经白等了几分钟。
    with _tf.TemporaryDirectory() as td:
        rep = fetch.download_one(
            FilePlan(filename="", kind="table",
                     urls={"bkg": "http://127.0.0.1:9/a.gz"}),
            Path(td),
        )
        check(
            not rep.ok and "不合法" in rep.detail,
            f"★ 空文件名在下载入口就被拒（{rep.detail[:40]}）",
        )


def test_inbox_deep_dirs() -> None:
    section("⑦g ★★ 收件箱：**嵌套目录**里的文件不能被静默丢掉")

    # ⚠️⚠️ 这一节守的是一个**真实发生过、而且用户看得见**的 bug。
    #
    #    用户 2026-09-20 往收件箱丢了一个文件夹（`data/{met,nav,obs}/…`），
    #    问"收件箱支持多文件多目录分析吗"。实测：
    #
    #        收件箱里一共 194 个文件，`scan()` 只看见 21 个，
    #        **173 个（89%）静默消失** —— 而报告里一个字都没提。
    #
    #    边界还是完全没道理的那种：
    #        `met/2026/244/`（深 4 层）活着
    #        `obs/rinex2/2026/244/`（深 5 层）**全没**
    #
    #    ⚠️ 而且这是**已经修过一次的形态**：`too_big` 当初也是"算出来了但
    #       没人读"，注释里写着「**静默漏掉一个文件，比报错糟得多**」。
    #       那次只照着做在 `too_big` 上 —— 深度这条路**一个读取者都没有**，
    #       而 `scripts/` 里 grep `MAX_DEPTH` 是 **0 命中**。
    #
    #    ⚠️ 夹具原来是**平的**（`box / "x.rnx"`），所以这条路从来没被测过。
    #       这一节从第 1 层一直造到第 12 层。
    import tempfile

    from ginan_agent.ginan import inbox

    with tempfile.TemporaryDirectory() as td:
        box = Path(td) / "inbox"
        # 每层各放一个文件：第 i 层放在 d1/d2/.../di/ 下面
        for i in range(1, 13):
            q = box
            for j in range(1, i + 1):
                q = q / f"d{j}"
            q.mkdir(parents=True, exist_ok=True)
            (q / f"f{i}.rnx").write_bytes(b"x")

        s = inbox.scan_detailed(box)

        check(
            len(s.items) == inbox.MAX_DEPTH,
            f"★★ 看到 {len(s.items)} 个（期望 {inbox.MAX_DEPTH} —— "
            f"MAX_DEPTH 以内的一层不落）",
        )
        check(
            len(s.items) >= 8,
            f"★★ 深度上限至少 8 —— 真实的镜像布局（年/年积日）在 4~6 层，"
            f"4 层会漏掉绝大多数（实际 {inbox.MAX_DEPTH}）",
        )

        # ⚠️ 关键：漏掉的部分**必须自己说出来**
        check(s.truncated, "★★ 截断了 → `truncated` 必须为真")
        check(
            s.too_deep_files == 12 - inbox.MAX_DEPTH,
            f"★★ 而且报出**具体多少个文件**没看（{s.too_deep_files}，"
            f"期望 {12 - inbox.MAX_DEPTH}）—— 只说「有目录太深」对用户没用",
        )
        check(
            str(inbox.MAX_DEPTH) in s.depth_note and "没" in s.depth_note,
            f"★ 那句话里带上了层数和「没看」（{s.depth_note[:40]}…）",
        )

        # ⚠️ 反过来：没截断时**不许**说截断了（否则提示会变成噪音，
        #    用户学会忽略它之后，真截断那一次也看不见了）
        flat = Path(td) / "flat"
        flat.mkdir()
        (flat / "a.rnx").write_bytes(b"x")
        check(
            inbox.scan_detailed(flat).depth_note == "",
            "★ 没截断时 `depth_note` 是空串（不能变成天天喊的狼来了）",
        )

        # ⚠️⚠️ `claim()` 也必须把它带出去 —— 认领**会动用户文件**，
        #    而那一步如果不说"我有东西没看到"，用户会以为全搬好了。
        r = inbox.claim(dest_dir=Path(td) / "dest", root=box, dry_run=True)
        check(
            r.depth_note != "" and "depth_note" in inbox.claim_to_json(r),
            "★★ 认领的报告里也带着这句话（工具/界面才拿得到）",
        )


def test_inbox_gz_roundtrip() -> None:
    section("⑦h ★★ 收件箱：`.gz` 认领之后，问『认领过没有』的两处**必须同一个答案**")

    # ⚠️⚠️ 用户抱怨过的那个 bug，对 `.gz` **从来没修好过**。
    #
    #    他的原话（写在 `already_claimed` 的说明里）：
    #        "我点看看我的数据，程序认领了我的数据后，
    #         右下角收件箱就不要提示认领了"
    #
    #    根因：指纹算的是**原始字节**，而
    #        收件箱  ALIC…_MO.rnx.gz  →  sha256(gzip 流的开头)
    #        数据目录 ALIC.rnx         →  sha256(RINEX 文本的开头)
    #    **永远不可能相等。**
    #
    #    实测（修之前）：already_claimed → 0、claimed_pairs → 0。
    #    后果：界面「认领」按钮**永远不消失**。
    #
    #    ⚠️ 当时还有第三个症状："新任务清不空收件箱"。那条路现在叫
    #       `clear_inbox()`，它**全删、不看指纹** —— 所以对这个 bug 已经
    #       免疫了（那个症状连同判据一起消失了）。上面两条**还活着**，
    #       它们才是这一节要守的东西。
    #
    #    ⚠️ 而守卫 `test_already_claimed_survives_copy_semantics` 一直是**绿的** ——
    #       因为它的夹具写的是**裸 RINEX，一个 `.gz` 都没有**。
    #       而 IGS 分发 RINEX 3 的标准形式就是 `.gz`：**真实的那条路没有守卫**。
    #
    #    > 夹具不贴真实，那条路的 bug 就没有守卫。
    import gzip
    import tempfile

    from ginan_agent.ginan import inbox

    OBS = (
        b"     3.04           OBSERVATION DATA    G (GPS)"
        + b" " * 20 + b"RINEX VERSION / TYPE\n"
        + b"ALIC" + b" " * 56 + b"MARKER NAME" + b" " * 9 + b"\n"
        + b"x" * 4000
    )

    with tempfile.TemporaryDirectory() as td:
        box = Path(td) / "inbox"
        dest = Path(td) / "data"
        box.mkdir()

        (box / "ALIC00AUS_R_20262440000_01D_30S_MO.rnx.gz").write_bytes(
            gzip.compress(OBS)
        )

        r = inbox.claim(dest_dir=dest, root=box)
        check(r.moved and r.moved[0].dst.name == "ALIC.rnx",
              f"认领了，落在 ALIC.rnx（{r.summary}）")

        # ⚠️⚠️ 这两个答案必须**完全一致**。任何一个不一致，用户就会
        #    照着其中一个去操作，而另一个在骗他。
        items = inbox.scan(box)
        check(
            len(inbox.already_claimed(items, dest)) == 1,
            "★★ already_claimed 说「认领过了」（修之前这里是 0）"
            "—— 它直接决定界面上那个按钮显不显示",
        )
        check(
            len(inbox.claimed_pairs(items, dest)) == 1,
            "★★ claimed_pairs 也配得上（它决定「哪份是你的、哪份是缓存的」）",
        )

        # ⚠️ 认领之后收件箱里那份**还在**（认领是复制，用户不会丢东西）——
        #    所以"点新任务之后收件箱是不是空的"要单独验一次：
        #    用户报的就是这件事（「我点新任务里边还有东西」）。
        check(
            list(box.rglob("*.rnx.gz")),
            "★ 认领**不会**动收件箱里那份（复制，不是搬运）",
        )
        cleared = inbox.clear_inbox(box)
        check(
            len(cleared.deleted) == 1 and cleared.errors == [],
            f"★★ clear_inbox 把它删了 —— 新任务之后收件箱是空的"
            f"（{cleared.summary}）",
        )
        check(
            not list(box.rglob("*.rnx.gz")),
            "★ 收件箱真的清空了",
        )
        check(
            (dest / "ALIC.rnx").is_file(),
            "★★ 数据目录里那份**永远不删**（删的是收件箱里的副本）",
        )


def test_inbox_same_content_two_places() -> None:
    section("⑦i ★★ 收件箱：同一份内容放在两个地方，只能落**一份**")

    # ⚠️ 实测（修之前）：收件箱 `a/X.gz` + `b/c/X.gz`（内容完全相同）
    #        →  数据目录 `ALIC.rnx` + `ALIC_2.rnx`   ← 两份一模一样的
    #
    #    根因：`dest_index` 在 `claim()` 开头算一次，**之后再没更新过** ——
    #          于是"已经有一份了"那个判断**看不见同一批里刚复制进去的**。
    #
    #    ⚠️⚠️ 而且 dry_run 和真跑**必须给出同一个答案**：
    #        `scan_inbox` 这个工具的默认参数就是 `dry_run=True`，
    #        所以用户默认看到的就是预演。
    #        **一个和现实不符的预演，比没有预演更糟。**
    import gzip
    import tempfile

    from ginan_agent.ginan import inbox

    OBS = (
        b"     3.04           OBSERVATION DATA    G (GPS)"
        + b" " * 20 + b"RINEX VERSION / TYPE\n"
        + b"ALIC" + b" " * 56 + b"MARKER NAME" + b" " * 9 + b"\n"
        + b"y" * 4000
    )

    def build(td):
        box = Path(td) / "inbox"
        for sub in ("a", "b/c"):
            d = box / sub
            d.mkdir(parents=True)
            (d / "ALIC00AUS_R_20262440000_01D_30S_MO.rnx.gz").write_bytes(
                gzip.compress(OBS)
            )
        return box, Path(td) / "data"

    with tempfile.TemporaryDirectory() as td1:
        box, dest = build(td1)
        r = inbox.claim(dest_dir=dest, root=box, dry_run=True)
        preview = sorted(m.dst.name for m in r.moved)
        preview_skipped = len(r.skipped)

    with tempfile.TemporaryDirectory() as td2:
        box, dest = build(td2)
        r2 = inbox.claim(dest_dir=dest, root=box)
        real = sorted(p.name for p in dest.iterdir())

        check(
            real == ["ALIC.rnx"],
            f"★★ 数据目录里只落**一份**（实际 {real}）"
            "—— 两份同内容的观测文件会让 Ginan 重复处理同一个站",
        )
        check(len(r2.moved) == 1, f"★ 报告里也只说搬了 1 份（{r2.summary}）")

        check(
            preview == real,
            f"★★ **预演和真跑必须一模一样**（预演说 {preview}，真跑是 {real}）"
            "—— 工具的默认参数就是 dry_run，用户默认看到的是预演",
        )
        check(
            preview_skipped == len(r2.skipped),
            f"★ 跳过的条数也要一致（预演 {preview_skipped}，真跑 {len(r2.skipped)}）",
        )


def test_crinex_decoder() -> None:
    section("⑦j ★★ CrinEX 解码：逐字节对着**参考实现**生成的夹具验")

    # ⚠️⚠️ 这个解码器是**数值敏感**的代码，而它写错的样子恰好是这个项目
    #     最怕的那种 —— **看起来正常、只是精度差**。
    #     所以它不能靠"读起来对"来验收。

    #     **验收基准**：`tests/fixtures/sample.crx` / `sample.rnx` 这一对。
    #     取材是真文件（用户收件箱里那 48 份之一），截前 4 个历元；
    #     ⚠️ 期望输出是用**参考实现**（RNXCMP 的 `crx2rnx`）生成的 ——
    #        不是我们自己解出来的，否则就是自证。
    #
    #     （大头还在外面：48 份完整真文件、每份几万到十几万行，
    #       逐字节比过 **48/48**。那些文件太大不能进仓库，
    #       所以仓库里留这一对小的当**回归**。）
    from ginan_agent.ginan import crinex

    src = (ROOT / "tests/fixtures/sample.crx").read_text(encoding="latin-1")
    want = (
        (ROOT / "tests/fixtures/sample.rnx")
        .read_bytes().decode("latin-1").replace("\r\n", "\n")
    )

    got = crinex.decode(src)
    check(got == want, f"★★★ 逐字节一致（解出 {len(got)} 字符 / 期望 {len(want)}）")
    if got != want:
        gl, wl = got.splitlines(), want.splitlines()
        for i, (a, b) in enumerate(zip(gl, wl)):
            if a != b:
                check(False, f"  第 {i+1} 行不同：\n   解出 {a[:80]!r}\n   期望 {b[:80]!r}")
                break

    # ---- 夹具本身要贴真实 ----
    check(
        "CRINEX VERS" in src.splitlines()[0],
        "★ 夹具第 1 行是 CRINEX VERS / TYPE",
    )
    check(
        b"\r" not in (ROOT / "tests/fixtures/sample.crx").read_bytes(),
        "★★ 夹具没被换行转换污染（见 .gitattributes 里 "
        "`tests/fixtures/** -text`）",
    )

    # ---- ⚠️ 三个**踩过的坑**，每个都单独钉住 ----
    #
    # 它们的共同点：**参考实现的注释/代码是错的或不全的**，
    # 只有逐字节对着真数据比才看得出来。
    out = got

    # ① 前导零：绝对值 < 1 的值要把 `0` 换成空格（` .197` 而不是 `0.197`）
    #    参考实现的注释说"非负的保留零" —— **实测 0 次保留、3323 次去掉**。
    import re as _re

    fields = []
    for ln in out.splitlines():
        if ln[:1] in "GRECJIS" and len(ln) > 3:
            for k in range(len(ln) // 16):
                fields.append(ln[3 + k * 16: 3 + k * 16 + 14])
    n_keep = sum(1 for f in fields if _re.match(r"^ *-?0\.\d{3}$", f))
    n_drop = sum(1 for f in fields if _re.match(r"^ *-?\.\d{3}$", f))
    check(
        n_drop > 0,
        f"★ 夹具里出现过「去前导零」的写法（{n_drop} 个）—— 否则这条测不到",
    )
    check(
        n_keep == 0,
        f"★★ 而且**一个「保留零」的都没有**（实测：去掉 {n_drop} / 保留 {n_keep}）"
        "—— 参考实现说非负的保留，**真数据说一律去掉**",
    )

    # ② 钟差字段走**同一条**去零规则（12 位小数、宽 15）
    #
    #    ⚠️⚠️ 这一条**没法在 `sample` 那个夹具里测**：
    #       ALIC 这份的钟差**从来没有小于 1 微秒**过（扫了全部 16 个站，
    #       只有 CEDU / CUSV / ULAB 有）。
    #       而"按 ALIC 造一个夹具"就测不到它 ——
    #       这正是这个仓库反复吃的那条亏：**夹具不贴真实，那条路就没有守卫**。
    #
    #    👉 所以**单独一个小夹具**（`clk.crx`，从 CEDU 截前 2 个历元）：
    #       它的钟差**第 34 行**就是小于 1 的。
    clk_src = (ROOT / "tests/fixtures/clk.crx").read_text(encoding="latin-1")
    clk_want = (
        (ROOT / "tests/fixtures/clk.rnx")
        .read_bytes().decode("latin-1").replace("\r\n", "\n")
    )
    clk_got = crinex.decode(clk_src)
    check(
        clk_got == clk_want,
        f"★★★ 钟差夹具也逐字节一致（{len(clk_got)} vs {len(clk_want)}）",
    )
    clk = [
        ln for ln in clk_got.splitlines()
        if ln.startswith(">") and _re.search(r"  -?\.\d{12}$", ln)
    ]
    check(
        len(clk) > 0,
        f"★ 钟差夹具里有「去零」的行（{len(clk)} 行）—— 全量 48 对里这一条"
        "整整 2880 行，是跑全量才发现的",
    )
    check(
        not [
            ln for ln in clk_got.splitlines()
            if ln.startswith(">") and _re.search(r"  -?0\.\d{12}$", ln)
        ],
        "★★ 而且**一个「保留零」的钟差都没有** —— 和观测值同一条规则",
    )

    # ③ 值空着 ≠ 标志空着（丢锁但没观测到）
    blank_with_flag = [
        ln for ln in out.splitlines()
        if ln[:1] in "GRECJIS" and _re.search(r" {14}\d", ln)
    ]
    check(
        len(blank_with_flag) > 0,
        f"★ 夹具里有「值空着但 LLI 有数」的字段（{len(blank_with_flag)} 行）"
        "—— 这条**参考实现自己也写错了**（它把标志一起填成空格）",
    )


def test_inbox_decodes_crinex() -> None:
    section("⑦j2 ★★ 收件箱：`.crx.gz` 必须**解开**再落盘，不是拒绝、不是原样搬")

    # ⚠️⚠️ **同一个形状的第三次和第四次**。这块的行为改过两回：
    #
    #     最早：`.crx.gz` 剥掉外层 gz 就落成 `ALIC.rnx` ——
    #           **名字说是 RINEX、内容是 CrinEX**。Ginan 读不了，报的错
    #           和真正原因（编码没解）离得很远。
    #
    #     然后：**拒绝**（说"用不了，去换一份"）。在当时是对的 —— 没有解码器。
    #
    #     现在：`ginan/crinex.py` 能解了，所以**解成明文再落盘**。
    #           ⚠️ IGS 分发 RINEX 3 的**标准形式就是 `.crx.gz`**
    #              （CDDIS 等主数据中心只给这个），拒绝等于
    #              "从 CDDIS 下的观测一份都用不了"。
    import gzip
    import tempfile

    from ginan_agent.ginan import identify, inbox

    crx_text = (ROOT / "tests/fixtures/sample.crx").read_text(encoding="latin-1")
    want = (
        (ROOT / "tests/fixtures/sample.rnx")
        .read_bytes().decode("latin-1").replace("\r\n", "\n")
    )

    with tempfile.TemporaryDirectory() as td:
        box = Path(td) / "inbox"
        dest = Path(td) / "data"
        box.mkdir()

        name = "ALIC00AUS_R_20262440000_01D_30S_MO.crx.gz"
        (box / name).write_bytes(gzip.compress(crx_text.encode("latin-1")))

        # ---- ① 识别层：认得出，而且是 obs ----
        ident = identify.identify(box / name)
        check(ident.crinex, "★★ `.crx.gz` 认出来了")
        check(
            ident.kind == "obs",
            "★ 它**仍然是 obs**（所以光判 kind 拦不住它）",
        )
        check(
            not ident.usable,
            "★★ `usable` 为假 —— 因为它的定义是「能不能**直接**喂给 Ginan」",
        )

        # ---- ② 认领层：解出来 ----
        r = inbox.claim(dest_dir=dest, root=box)
        landed = sorted(p.name for p in dest.iterdir())
        check(r.moved and landed == ["ALIC.rnx"],
              f"★★ 落在 `ALIC.rnx`（实际 {landed}）")

        got = (dest / landed[0]).read_bytes().decode("latin-1").replace("\r\n", "\n")
        check(
            got == want,
            f"★★★ **解出来的内容逐字节等于参考实现**（{len(got)} vs {len(want)}）"
            "—— 这条是这个守卫的全部意义",
        )
        check(
            not (dest / "ALIC.rnx").read_bytes().startswith(b"\x1f\x8b"),
            "★★ 内容是明文，不是 gzip 流",
        )
        check(
            b"COMPACT RINEX FORMAT" not in (dest / landed[0]).read_bytes(),
            "★★★ 数据目录里**不能有**内容是 CrinEX 的 `.rnx`"
            "（那是最坏的一种：名字说能用、内容用不了）",
        )
        check(
            list(box.glob("*.crx.gz")),
            "★ 原件留在收件箱 —— 那是用户的东西，我们只复制",
        )

        # ---- ③ 认领之后：**按钮必须消失** ----
        #
        # ⚠️⚠️ 这一条是**用户抱怨过的那个 bug 的第二层**。
        #     原来 `_digest` 比的是原始字节 —— 对 `.gz` 修过一回，
        #     而 **CrinEX 是同一个问题的第二层**：
        #         `.crx.gz` 的指纹（对照它解出来的 CrinEX 文本）
        #         永远对不上它落成的 `ALIC.rnx`。
        #     后果一模一样：那个按钮永远在。
        items = inbox.scan(box)
        check(
            len(inbox.already_claimed(items, dest)) == 1,
            "★★ already_claimed 说「认领过了」（不解码的话这里永远是 0）",
        )
        # ⚠️ 顺手把用户报的那件事也钉住：**点新任务之后收件箱要是空的**。
        #    （清空这条路不看指纹，所以它对这个 bug 免疫 ——
        #      正是因为免疫，它才更要单独验一次，别哪天又被挂回指纹上。）
        cleared = inbox.clear_inbox(box)
        check(
            len(cleared.deleted) == 1 and cleared.errors == [],
            f"★★ clear_inbox 把 CrinEX 那几份也清掉了（{cleared.summary}）",
        )


def test_fingerprint_ignores_line_endings() -> None:
    section("⑦j3 ★★ 指纹要忽略行尾 —— 行尾是**产物**的属性，不是数据的属性")

    # ⚠️⚠️ 这条是修 CrinEX 时**带出来的**：解码器固定输出 LF，
    #     而收件箱里可能有一份**同一份数据的 CRLF 版本**（`.rnx` 双胞胎，
    #     GA 的工具在 Windows 上产生的）。
    #
    #     不归一化行尾的话：同一份观测数据的两种行尾被判成**两份** ——
    #     于是"认领过了"说没认领、同一份落两次。
    #     和 `_digest` 要修的那个 bug **完全同一个形状**。
    #
    #     实测三种行尾同时存在：CDDIS 的 `.crx.gz` 是 LF、
    #     GA 的 `.rnx` 是 CRLF、我们下的广播星历是 LF。
    import tempfile

    from ginan_agent.ginan import inbox

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        lf, crlf = d / "lf.txt", d / "crlf.txt"
        lf.write_bytes(b"hello\nworld\n")
        crlf.write_bytes(b"hello\r\nworld\r\n")
        check(
            inbox._digest(lf) == inbox._digest(crlf),
            f"★★ 同一份文本的 LF / CRLF 两种写法**同一个指纹**"
            f"（{inbox._digest(lf)} vs {inbox._digest(crlf)}）",
        )

        # ⚠️ 反过来也要钉：**不同的**内容绝不能因为归一化被判成同一个
        other = d / "other.txt"
        other.write_bytes(b"hello\nWORLD\n")
        check(
            inbox._digest(other) != inbox._digest(lf),
            "★★ 内容不同的**必须**给出不同指纹（归一化不是「什么都当一样」）",
        )

        # ⚠️ `\r\n` 跨在分块边界上（4 MB）—— 直接 `chunk.replace()` 会漏掉它，
        #    于是大小差 1，也就是同一份数据被判成两份。
        body = b"x" * 2000 + b"\r\n" + b"y" * ((1 << 22) - 3) + b"\r\n" + b"z" * 500
        a, b = d / "a.bin", d / "b.bin"
        a.write_bytes(body)
        b.write_bytes(body.replace(b"\r\n", b"\n"))
        check(
            inbox._digest(a) == inbox._digest(b),
            f"★★ 跨 4 MB 块边界的 `\\r\\n` 也处理对了"
            f"（{inbox._digest(a)} vs {inbox._digest(b)}）"
            "—— 漏一个就是大小差 1，而那种 bug 复现不了",
        )
    section("⑦l ★★ RINEX 头部**长**的时候，末尾的信息不能被切掉")

    # ⚠️⚠️ 这条守的是一个**会把好数据判成坏数据**的 bug。
    #
    #    `rinex.py` 里原来写的是 `lines = text.splitlines()[:MAX_HEADER_LINES]`，
    #    而 `MAX_HEADER_LINES = 200`。
    #
    #    实测（用户那一箱 IGS 数据）：
    #        `AUCK00NZL_R_20262450000_01D_30S_MO.rnx` 的头部有 **290 行**，
    #        而 `TIME OF FIRST OBS` 在**第 288 行** —— 被那行切片砍掉了。
    #
    #    于是：`first_obs = None` → `inspect()` 报「文件头里没有起始时间」
    #          → **一份完好的观测数据被判成用不了**。
    #          （那一箱 5 个站中招。）
    #
    #    ⚠️ 为什么 200 会不够：**RINEX 3 的头部长度不是固定的**。
    #       多系统站每个系统一段 `SYS / # / OBS TYPES`，
    #       再加几十行 `SYS / PHASE SHIFT`。7 个系统轻松两三百行。
    #
    #    ⚠️ 而解析循环里**本来就有** `if "END OF HEADER" in line: break` ——
    #       头部的结束该由**终止符**说了算，是那行切片让循环永远到不了。
    import tempfile

    from ginan_agent.ginan import rinex

    # 造一个**真实形状**的长头部：多系统 + 大量 PHASE SHIFT 行，
    # 把 TIME OF FIRST OBS 挤到远远超过 200 行的位置
    def pad(text: str) -> str:
        return f"{text:<60}{'':<20}\n"

    rows = ["C1C", "L1C", "D1C", "S1C"]
    head_lines = [
        pad(f"{'     3.04           OBSERVATION DATA    M':<60}").replace(
            " " * 20 + "\n", "RINEX VERSION / TYPE\n"
        ),
        pad("ALIC"),
        pad(f"{'%6d' % len(rows) + ''.join('%6s' % r for r in rows)}"),
    ]
    # ⚠️ 直接按 60 列拼，别用 pad 的 replace 花活
    head_lines = [
        f"{'     3.04           OBSERVATION DATA    M':<60}{'RINEX VERSION / TYPE':<20}\n",
        f"{'ALIC':<60}{'MARKER NAME':<20}\n",
        f"{('%6d' % len(rows)) + ''.join('%6s' % r for r in rows):<60}"
        f"{'# / TYPES OF OBSERV':<20}\n",
    ]
    # 堆 300 行 PHASE SHIFT —— 真实的多系统 RINEX 3 头部就是这个样子
    for i in range(300):
        head_lines.append(
            f"{'G L1C':<60}{'SYS / PHASE SHIFT':<20}\n"
            if i % 60 == 0 else
            f"{'G L%02d  0.00000' % (i % 60):<60}{'SYS / PHASE SHIFT':<20}\n"
        )
    head_lines.append(
        f"{'  2026     9     2     0     0    0.0000000     GPS':<60}"
        f"{'TIME OF FIRST OBS':<20}\n"
    )
    head_lines.append(f"{'':<60}{'END OF HEADER':<20}\n")
    head = "".join(head_lines).encode()

    fobs_line = next(
        i for i, l in enumerate(head.decode().splitlines())
        if l[60:].strip() == "TIME OF FIRST OBS"
    )
    check(
        fobs_line + 1 > 200,
        f"★ 夹具贴真实：TIME OF FIRST OBS 在第 {fobs_line + 1} 行"
        f"（**超过**了原来那个 200 的上限，否则这一节测不出东西）",
    )

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "long.rnx"
        p.write_bytes(head)
        info = rinex.inspect(p)
        check(
            info.first_obs is not None,
            f"★★ 读出了起始时间（{info.first_obs}）"
            "—— 读不出的话一份好数据会被判成'用不了'",
        )
        check(
            not any("没有起始时间" in x for x in info.problems),
            f"★★ 也没有报「文件头里没有起始时间」（problems={info.problems[:1]}）",
        )
        check(
            rinex.MAX_HEADER_LINES > fobs_line,
            f"★ 上限（{rinex.MAX_HEADER_LINES}）**大于**真实头部长度"
            f"（{fobs_line + 1}）—— 头部的结束该由 END OF HEADER 说了算",
        )


def test_rinex_long_header() -> None:
    section("⑦l ★★ RINEX 头部**长**的时候，末尾的信息不能被切掉")

    # ⚠️⚠️ 这条守的是一个**会把好数据判成坏数据**的 bug。
    #
    #    `rinex.py` 里原来写的是 `lines = text.splitlines()[:MAX_HEADER_LINES]`，
    #    而 `MAX_HEADER_LINES = 200`。
    #
    #    实测（用户那一箱 IGS 数据）：
    #        `AUCK00NZL_R_20262450000_01D_30S_MO.rnx` 的头部有 **290 行**，
    #        而 `TIME OF FIRST OBS` 在**第 288 行** —— 被那行切片砍掉了。
    #
    #    于是：`first_obs = None` → `inspect()` 报「文件头里没有起始时间」
    #          → **一份完好的观测数据被判成用不了**。
    #          （那一箱 5 个站中招。）
    #
    #    ⚠️ 为什么 200 会不够：**RINEX 3 的头部长度不是固定的**。
    #       多系统站每个系统一段 `SYS / # / OBS TYPES`，
    #       再加几十行 `SYS / PHASE SHIFT`。7 个系统轻松两三百行。
    #
    #    ⚠️ 而解析循环里**本来就有** `if "END OF HEADER" in line: break` ——
    #       头部的结束该由**终止符**说了算，是那行切片让循环永远到不了。
    import tempfile

    from ginan_agent.ginan import rinex

    rows = ["C1C", "L1C", "D1C", "S1C"]

    def lab(data: str, label: str) -> str:
        # ⚠️ RINEX 是**定宽**格式：标签**必须**从第 60 列开始。
        return f"{data:<60}{label:<20}\n"

    head_lines = [
        lab("     3.04           OBSERVATION DATA    M", "RINEX VERSION / TYPE"),
        lab("ALIC", "MARKER NAME"),
        lab(f"G{len(rows):5d} " + " ".join(rows), "SYS / # / OBS TYPES"),
    ]
    # 堆 300 行 PHASE SHIFT —— 真实的多系统 RINEX 3 头部就是这个样子
    for i in range(300):
        head_lines.append(
            lab("G L1C" if i % 60 == 0 else f"G L{i % 60:02d}  0.00000", "SYS / PHASE SHIFT")
        )
    head_lines.append(
        lab("  2026     9     2     0     0    0.0000000     GPS", "TIME OF FIRST OBS")
    )
    head_lines.append(lab("", "END OF HEADER"))
    head = "".join(head_lines).encode()

    fobs_line = next(
        i for i, l in enumerate(head.decode().splitlines())
        if l[60:].strip() == "TIME OF FIRST OBS"
    )
    check(
        fobs_line + 1 > 200,
        f"★ 夹具贴真实：TIME OF FIRST OBS 在第 {fobs_line + 1} 行"
        f"（**超过**了原来那个 200 的上限，否则这一节测不出东西）",
    )

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "long.rnx"
        p.write_bytes(head)
        info = rinex.inspect(p)
        check(
            info.first_obs is not None,
            f"★★ 读出了起始时间（{info.first_obs}）"
            "—— 读不出的话一份好数据会被判成「用不了」",
        )
        check(
            not any("没有起始时间" in x for x in info.problems),
            f"★★ 也没有报「文件头里没有起始时间」（problems={info.problems[:1]}）",
        )
        check(
            rinex.MAX_HEADER_LINES > fobs_line,
            f"★ 上限（{rinex.MAX_HEADER_LINES}）**大于**真实头部长度"
            f"（{fobs_line + 1}）—— 头部的结束该由 END OF HEADER 说了算",
        )


def test_advise_aggregates() -> None:
    section("⑦m ★★ 「这一堆能算什么」：聚合 + 透过压缩看")

    # ⚠️⚠️ 这个功能存在的理由（`prompts.py` 早就要求模型回答它，
    #     但当时**没有任何工具能回答**）：
    #
    #         → 说清楚：这份数据**能算什么**、**还缺什么**、
    #           缺的**能不能自动下**。这才叫"给建议"。
    #
    #    而当时模型手上那两块信息**无处可 join**：
    #        `scan_inbox`      有 kind，没有站名和时间
    #        `list_local_obs`  有站名和时间，**把非 obs 全丢了**
    #
    #    这一节守两件最容易错的事：
    #      ① **收件箱里的 `.gz` 是正常的**，不能判成"用不了"
    #      ② 输出必须**聚合**（几十个文件 → 一小段），否则会撞 8000 字符上限
    import gzip
    import tempfile

    from ginan_agent.ginan import advise, inbox

    # ⚠️ 必须是**真的 RINEX 3 头部**（`SYS / # / OBS TYPES` + 系统字母）。
    #
    #    第一版我写的是 RINEX 2 的标签 `# / TYPES OF OBSERV`，
    #    却把版本号写成 3.04 —— 于是解析器按 2 字符的观测码去读
    #    3 字符的 `C1C`，判出"单频"，`ready=False`，
    #    整节断言红了。
    #
    #    👉 不是代码错，是**夹具不贴真实** —— 又是这条。
    def obs(marker: str, doy: int = 244) -> bytes:
        from datetime import date as _date
        from datetime import timedelta

        rows = ["C1C", "L1C", "D1C", "S1C", "C2W", "L2W", "D2W", "S2W"]
        d = _date(2026, 1, 1) + timedelta(days=doy - 1)
        stamp = f"{d.year} {d.month:5d} {d.day:5d}"

        def lab(data: str, label: str) -> str:
            # ⚠️ RINEX 是**定宽**格式：标签**必须**从第 60 列开始。
            #    第一版我用 `{data:<52}` 拼，标签落在了第 58 列 ——
            #    于是 `SYS / # / OBS TYPES` 根本没被认出来，
            #    报"文件头里没有列出观测类型"。
            return f"{data:<60}{label:<20}\n"

        return (
            lab("     3.04           OBSERVATION DATA    M", "RINEX VERSION / TYPE")
            + lab(marker, "MARKER NAME")
            # ⚠️ 计数字段是 %5d（第 2~6 列），观测码从第 7 列起
            + lab(f"G{len(rows):5d} " + " ".join(rows), "SYS / # / OBS TYPES")
            + lab("TWIVC6050           NONE", "ANT # / TYPE")
            + lab(f"{stamp}     0     0    0.0000000     GPS", "TIME OF FIRST OBS")
            + lab(f"{stamp}     1     0    0.0000000     GPS", "TIME OF LAST OBS")
            + lab("", "END OF HEADER")
            + "".join(
                f"> {d.year} {d.month:2d} {d.day:2d} {i:2d}  0  0.0000000  0  8\n"
                + "".join(f"G{1+i%9:02d}{' '*14}\n" for _ in range(8))
                for i in range(2)
            )
        ).encode()

    with tempfile.TemporaryDirectory() as td:
        box = Path(td) / "inbox"
        box.mkdir()

        # ① 同一个站：一份 .gz、一份裸文件
        (box / "ALIC00AUS_R_20262440000_01D_30S_MO.rnx.gz").write_bytes(
            gzip.compress(obs("ALIC00AUS"))
        )
        # ② 另一个站
        (box / "DARW00AUS_R_20262440000_01D_30S_MO.rnx").write_bytes(
            obs("DARW00AUS")
        )

        # ---- ① 压缩**不是**问题 ----
        #
        # ⚠️⚠️ `rinex.inspect()` 遇到 gzip 会说"需要先解压" ——
        #    那句话是给**数据目录**写的（那里的文件本来就该是解开过的）。
        #    拿它判收件箱，会把一份好数据报成"用不了"，
        #    **而且报得很像那么回事**。
        s = inbox.scan_detailed(box)
        pile = advise.analyze(s.items)
        gz_files = [f for f in pile.obs if f.name.endswith(".gz")]
        check(
            gz_files and gz_files[0].usable,
            f"★★ 收件箱里的 `.gz` 判成**能用**（usable={gz_files[0].usable if gz_files else None}）"
            "—— 收件箱的文件天生带压缩，claim 会在路上解压它",
        )
        check(
            gz_files and gz_files[0].duration_minutes > 0,
            f"★★ 而且读出了时长（{gz_files[0].duration_minutes if gz_files else 0:.0f} 分钟）"
            "—— 读不出来的话「偏短」那类提示就永远不会出现",
        )

        check(
            len(pile.candidates) == 2,
            f"★★ 两个 (站, 天) 候选（实际 {pile.candidates}）",
        )

        # ---- ② 输出必须聚合 ----
        out = advise.to_json(pile)
        check(
            len(out) < 8000,
            f"★★ 输出 {len(out)} 字符，在 8000 上限内"
            "—— 超了会被**截断**，而截断的后果是模型以为就这么多",
        )
        check(
            len(out) < 1500,
            f"★ 2 个文件的输出只有 {len(out)} 字符（聚合成按站，不是逐个文件列）",
        )
        check(
            "ALIC" in out and "DARW" in out,
            "★ 两个站都在里面",
        )

        # ---- ③ 缺什么要说出来 ----
        import json

        d = json.loads(out)
        check(
            "sp3" in json.dumps(d.get("缺什么", {}), ensure_ascii=False),
            "★★ 说得出缺什么（这一箱里没有 SP3/CLK/ERP/ATX）"
            "—— 「还缺什么」正是 prompts 要求模型回答的三件事之一",
        )
        check(
            "能自动下吗" in json.dumps(d.get("缺什么", {}), ensure_ascii=False),
            "★★ 也说得出「缺的能不能自动下」",
        )

        # ---- ⑤ ★★ 措辞：说的是"**收件箱里没有**"，不是"你没有" ----
        #
        # ⚠️⚠️ `missing_kinds` 自己那段说明里就写着这条规矩：
        #
        #     「判据是"收件箱里有没有这一类文件"，**不是**"数据目录里有没有"
        #       ——数据目录里可能已经有程序早先下好的。
        #       所以这句话的措辞必须是"**收件箱里没有**"，不能是"你没有"。」
        #
        #    而 `to_json` 输出的原来是 `"还缺（必需）"` /
        #    `"必需（缺了做不了 PPP）"` —— **两句都是"你没有"**。
        #    规则写下了，输出没照做。
        #
        #    实测代价（2026-09-21，用户那一箱）：他的 `data/` 里
        #    `igs20.atx` 天天都在、245 天的 SP3/CLK/ERP/OSB 也都在，
        #    而那句话说的是"缺 sp3、clk、erp、atx"。他问的恰好是
        #    "这些能算什么" —— **他会拿到一个错答案**。
        # ⚠️⚠️ **断言必须落在"装清单的那个键"上，不能落在整块 JSON 上。**
        #
        #     第一版我写的是 `"收件箱" in json.dumps(缺什么块)` —— 变异测试
        #     把键名改回 `必需（缺了做不了 PPP）` 之后，它**居然还是绿的**：
        #     因为块里**别的说明文字**（"数据目录里可能早就有了…"）
        #     也含"收件箱"这两个字。**断言被别的修复满足了。**
        #
        #     > 判据要挑"只有这个修复才能解释的现象"。
        #     （这和夹具那次是同一类错，只是换成了断言本身不够准。）
        #
        #     所以先**找出那个键**（值是 `missing_kinds` 的那个），再只看它。
        missing_block = d.get("缺什么", {})
        holder = [
            k for k, v in missing_block.items()
            if isinstance(v, list) and v == pile.missing_kinds
        ]
        check(
            len(holder) == 1,
            f"★ 找得到装缺失清单的那个键（{holder}）",
        )
        check(
            bool(holder) and "收件箱" in holder[0],
            f"★★★ 那个键名说的是「**收件箱**里没有」，不是「你没有」"
            f"（实际 {holder}）",
        )
        check(
            bool(holder) and "还缺" not in holder[0],
            f"★★★ 而且不用「还缺」这种听起来像「你没有」的说法"
            f"（实际 {holder}）",
        )
        # ⚠️ 而且必须给出**核对的办法** —— 只说"数据目录里可能已有"
        #    而不说怎么查，等于把问题丢回给用户。
        check(
            "plan_run" in json.dumps(missing_block, ensure_ascii=False),
            "★★ 并说清怎么核对（plan_run 给出那一天究竟要什么）",
        )

        # ---- ⑥ ★★ 头一句话不许**过头** ----
        #
        # ⚠️ 原来写的是"**可以做 N 个日解**"。那是个过头话：候选只说
        #    "这一天有这个站的观测"，能不能真跑起来还要看产品在不在 ——
        #    模块开头「它不做什么」自己就写着这一条。
        #    实测里它和下一节"缺 sp3/clk/erp/atx"**自相矛盾**。
        head = d.get("一句话", "")
        check(
            "可以做" not in head,
            f"★★★ 头一句不说「可以做 N 个日解」（那是个承诺）：{head}",
        )
        check(
            "有观测" in head and "缺什么" in head,
            f"★★ 而是陈述「有几个 (站, 天) 有观测」，"
            f"并把「能不能跑」指给下一节：{head}",
        )

    # ---- ④ 几十个文件时输出也不能爆 ----
    #
    # ⚠️⚠️ 这条才是**真正的约束**：工具输出上限 8000 字符，
    #    而收件箱里有上百个文件是常态（用户那一箱 194 个）。
    #    **按文件逐个列的话，光文件名就能占满** ——
    #    然后被截断，而截断的后果是模型以为就这么多。
    with tempfile.TemporaryDirectory() as td:
        box = Path(td) / "inbox"
        box.mkdir()
        stations = [f"S{i:03d}" for i in range(40)]
        for st in stations:
            for doy in (244, 245, 246):
                (box / f"{st}00AUS_R_2026{doy}0000_01D_30S_MO.rnx.gz").write_bytes(
                    gzip.compress(obs(f"{st}00AUS", doy))
                )
        s = inbox.scan_detailed(box)
        pile = advise.analyze(s.items)
        out = advise.to_json(pile)
        check(len(s.items) == 120, f"扫到 120 个文件（实际 {len(s.items)}）")
        check(
            len(pile.candidates) == 120,
            f"120 个 (站, 天) 候选（实际 {len(pile.candidates)}）",
        )
        check(
            len(out) < 8000,
            f"★★ 120 个文件仍然只输出 {len(out)} 字符"
            "—— 不聚合的话光文件名就爆了",
        )
        # ⚠️ 站太多时要**明说还有多少没列**，而不是让下游把尾巴砍掉
        d = json.loads(out)
        check(
            "未列出" in json.dumps(d["观测"], ensure_ascii=False),
            f"★★ 超过 {advise.MAX_STATIONS} 个站时明说「还有 N 个没列」"
            "—— 让下游截断的话，模型会以为只有列出来那些",
        )

    # ---- ⑤ 解不开的压缩包：要说"坏了"，不能说"认不出来" ----
    #
    # ⚠️ 这是一条**防御分支**（正常流程走不到 —— 解不开的包在 identify
    #    那一步就被归成 unknown 了）。但分支没人走过的后果，
    #    就是这个仓库反复吃的那个亏：**代码在，但从没执行过**。
    #    所以直接调 `_obs_file` 喂它一个坏包。
    from ginan_agent.ginan import identify as _ident
    from ginan_agent.ginan.inbox import InboxItem as _Item

    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "broken.rnx.gz"
        # gzip 魔数对，但内容是垃圾 —— 解不开
        bad.write_bytes(b"\x1f\x8b" + b"\x00" * 500)
        item = _Item(bad, _ident.identify(bad), bad.stat().st_size)
        f = advise._obs_file(item)
        check(
            not f.usable,
            "★ 解不开的压缩包判成**不可用**",
        )
        # ⚠️⚠️ 断言必须**卡在"解不开"这三个字上**，不能放宽到"压缩"。
        #
        #     第一版我写的是 `"解不开" in p or "压缩" in p` —— 而**错误的**
        #     那条信息（`inspect` 的「这是 gzip 压缩的文件，需要先解压」）
        #     里**也有"压缩"两个字**。于是变异测试时：
        #     把防御分支关掉，这条断言**照样是绿的**。
        #
        #     > 一个能被两种相反结果同时满足的断言，等于没有断言。
        joined = " ".join(f.problems)
        check(
            "解不开" in joined,
            f"★★ 说的是「压缩包解不开」而不是「需要先解压」（problems={f.problems}）"
            "—— 「需要先解压」是给**数据目录**的话，在收件箱这里是错的："
            "用户照着它去解压也不会变好，包是真的坏了",
        )
        check(
            "需要先解压" not in joined,
            "★★ 也没混进那句「需要先解压」—— 那会把'文件坏了'说成'程序还没处理'",
        )


def test_download_decodes_crinex() -> None:
    section("⑦k ★★ 下载：拿到 CrinEX 要**解开**再落盘（不能存成 `.rnx`）")

    # ⚠️⚠️ 和收件箱**同一个洞的下载侧**。
    #
    #    `_decide_name` 对任何 `kind="obs"` 且有 marker 的都返回 `{marker}.rnx`
    #    —— 而 **CrinEX 的头部就是明文 RINEX，marker 读得到**。
    #    所以它会落成 `ALIC.rnx`，内容是 Hatanaka 编码，
    #    **而且过得了"内容对不上"那道校验**（`ident.kind` 确实是 `"obs"`）。
    #
    #    ⚠️ 曾经的行为是**拒绝**（`ok=False`）。现在能解了，所以改成解开。
    #       为什么不能"照原样存成 `.crx`"：那样它不匹配配置里的 `ALIC*.rnx`，
    #       于是**静默不参与解算** —— 用户看到"下载成功"，
    #       然后拿到一个很差的解，而原因离现象非常远。
    #       **下载失败会立刻被看到，静默不参与不会。**
    import gzip
    import tempfile

    from ginan_agent.ginan import fetch
    from ginan_agent.ginan.catalog import FilePlan

    crx_text = (ROOT / "tests/fixtures/sample.crx").read_text(encoding="latin-1")
    want = (
        (ROOT / "tests/fixtures/sample.rnx")
        .read_bytes().decode("latin-1").replace("\r\n", "\n")
    )

    for label, payload in (
        ("裸 CrinEX", crx_text.encode("latin-1")),
        ("`.crx.gz`（外层 gzip + 里层 CrinEX）",
         gzip.compress(crx_text.encode("latin-1"))),
    ):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            url = "https://x.invalid/f"
            sess = FakeSession({
                url: FakeResp(200, payload, {"Content-Length": str(len(payload))})
            })
            item = FilePlan(
                filename="ALIC00AUS_R_20262440000_01D_30S_MO.crx.gz",
                kind="obs", urls={"ga": url},
            )
            r = fetch.download_one(item, dest, session=sess)

            check(r.ok, f"★★ {label}：下载成功（{r.detail[:50]}）")
            landed = sorted(p.name for p in dest.iterdir())
            check(
                landed == ["ALIC00AUS.rnx"],
                f"★★ {label}：落成 `ALIC00AUS.rnx`（实际 {landed}）",
            )
            if landed:
                got = (dest / landed[0]).read_bytes().decode("latin-1")
                got = got.replace("\r\n", "\n")
                check(
                    got == want,
                    f"★★★ {label}：**解出来的内容逐字节等于参考实现**"
                    f"（{len(got)} vs {len(want)}）",
                )
                check(
                    b"COMPACT RINEX FORMAT" not in (dest / landed[0]).read_bytes(),
                    f"★★★ {label}：磁盘上那份**不是** CrinEX"
                    "（名字说 RINEX、内容也是 RINEX）",
                )


def test_line_endings_are_not_mixed() -> None:
    section("㉕ ★★ 行尾**不许混** —— 混了会让按字符串改文件的工具静默失效")

    # ⚠️⚠️ 这条是我自己踩出来的（2026-09-29）：
    #
    #    为了做变异测试，我用脚本按字符串替换改 `catalog.py` ——
    #    结果**替换没生效**，靠 `assert t != orig` 才发现。
    #    查下去：那个文件**行尾混了** —— 2136 行 CRLF、**14 行 LF**，
    #    而那 14 行正是我前几轮编辑时加进去的。
    #
    #    ⇒ 后果不是"格式不好看"，是**按字符串改文件的工具会静默失效**：
    #      你搜一个两行的片段（用 LF 连），而文件里那一处是 CRLF 连的 ——
    #      **匹配不上**，改动没发生，而你以为改了。
    #      这就是本仓库记在案的那个 CRLF 陷阱的**同一个形状**。
    #
    # ⚠️ 这个仓库**不是统一 LF**：`ginan/workflows.py` 和
    #    `dev/00-项目全貌.md` 本来就是 CRLF。
    #    所以判据只能是"**每个文件自己一致**"，不能是"全都得是 LF"。
    bad: list[str] = []
    for path in sorted(ROOT.rglob("*.py")) + sorted(ROOT.rglob("*.md")):
        if any(part in {".venv", "__pycache__", "node_modules", ".git"} for part in path.parts):
            continue
        raw = path.read_bytes()
        LF = bytes([10])
        CRLF = bytes([13, 10])
        lf, crlf = raw.count(LF), raw.count(CRLF)
        if crlf and lf - crlf:
            bad.append(f"{path.relative_to(ROOT)}（CRLF {crlf} / LF {lf - crlf}）")
    for b in bad[:10]:
        print(f"      ⚠️ 行尾混了：{b}")
    check(not bad, f"★★ 没有行尾混掉的文件（找到 {len(bad)} 个）")


def test_plot_series_is_plotable() -> None:
    section("★ 画图用的序列：参考系 / 降采样 / 什么时候**不画**")

    # ⚠️ `posfile.plot_series` 是结果面板那张折线图的**唯一**数据来源。
    #    它的三条判据都不是格式问题，每一条不对都会画出一张**骗人的图**。
    import datetime as dt

    from ginan_agent.ginan import posfile as P

    def mk(n, *, dup_from=None, step_s=30, ref=None):
        t0 = dt.datetime(2026, 9, 2)
        eps = []
        for i in range(n):
            k = i if dup_from is None else min(i, dup_from)
            eps.append(P.PosEpoch(
                time=t0 + dt.timedelta(seconds=step_s * i),
                x=0.0, y=0.0, z=0.0, lat=0.0, lon=0.0, height=0.0,
                dn=0.01 * k, de=0.0, du=0.0,
                sn=0.0, se=0.0, su=0.0))
        return P.PosFile(station="T", reference_xyz=ref, epochs=eps)

    # ---- ① 参考系必须是**解算自身**，不是文件头那个 ----
    #
    # ⚠️ 用文件头那个的话，整条曲线会被抬高几十厘米（实测 ALIC 那份差 45 厘米，
    #    因为文件头里的坐标是 6 年前的）—— 图上就**看不出收敛**，
    #    只看到一条挂在半空的线。而"稳住了没有"正是这张图要回答的。
    pf = mk(100, dup_from=9, ref=(6_000_000.0, 2_000_000.0, -1_000_000.0))
    s = P.plot_series(pf)
    tail = s["n"][-4:]
    check(
        all(abs(v) < 0.001 for v in tail),
        f"★ 末尾（= 最终位置）落在 0 附近：{tail} —— 参考系用的是解算自身",
    )
    check(
        max(abs(v) for v in s["n"]) > 5,
        f"★ 而开头是抬起来的（最大 {max(abs(v) for v in s['n']):.1f} 厘米）"
        " —— 开头那一段正是「看得出它在收敛」的地方，不许裁掉",
    )

    # ---- ② 降采样：等间隔抽 + **保最后一个点** ----
    big = mk(2880, dup_from=100)
    s2 = P.plot_series(big, max_points=600)
    check(s2["count"] == 2880, f"原始历元数照实报（{s2['count']}）")
    check(
        s2["plotted"] <= 640,
        f"★ 抽到 {s2['plotted']} 个点（上限 600 + 末尾保点）",
    )
    # ⚠️⚠️ 判据是**最后一个点的时间**，不是它的值。
    #
    #    第一版查的是"末尾的值 ≈ 0" —— 而我的夹具末尾是常数，
    #    于是"最后一个点"和"倒数第五个点"的值一模一样：
    #    **把保末尾那段删掉，守卫照样绿**（变异测试逼出来的假绿）。
    #
    #    ⇒ 改成问"画到的最后一个时刻，是不是最后那个历元的时刻" ——
    #      这才是"保住了末尾点"这件事本身，和值是否巧合无关。
    total_min = s2["t_minutes"][-1] if s2["t_minutes"] else -1
    expect_min = (big.count - 1) * 30 / 60
    check(
        abs(total_min - expect_min) < 0.01,
        f"★★ 最后一个点就是最后一个历元（画到 {total_min:.1f} 分钟，"
        f"全程 {expect_min:.1f}）—— 抽掉它，图会看起来像「没收敛到最终位置」",
    )

    # ---- ③ 被压成常数的解：**不画**，并说清为什么 ----
    #
    # ⚠️ 实测：半小时那份平滑解 30 个历元的高程全是 0.00，
    #    画出来是一根压在 0 上的直线 —— 看着"完美稳定"，实际什么都没说，
    #    而它还会和"从 1 米降下来"的前向解并排显示，**没意义的那个看着更好**。
    flat = mk(60, dup_from=0)
    s3 = P.plot_series(flat)
    check(s3["plotted"] == 0, f"★ 不画（plotted={s3['plotted']}）")
    why = s3.get("why_not") or ""
    check(bool(why), f"★ 但要说明为什么：{why[:38]}")
    check("**" not in why and "`" not in why,
          "★★ 那句话直接进侧栏的 textContent ⇒ 必须是纯文本")
    check(
        bool(s3.get("headline")),
        "★ 图没有，结论还是要给（用户至少知道发生了什么）",
    )


def test_lowess_matches_statsmodels() -> None:
    section("★★ LOWESS 必须和 statsmodels（=Ginan 用的那个）算出同一个东西")

    # ⚠️⚠️ 这是"我们没在重写一个**长得像**的东西"的唯一证据。
    #
    #    Ginan 用 `statsmodels.nonparametric.smoothers_lowess.lowess`
    #    （`scripts/plot_pos.py:6`）。我们为了不把 scipy+pandas 那一百多 MB
    #    拖进来，自己实现了同一个算法（`ginan/series.py`）——
    #    而"自己实现"这三个字必须靠**数值等价**来兑现，不能靠"看着像"。
    #
    # ⚠️ 判据用的是**黄金值**（下面那四个数），不是"跑 statsmodels 对一遍"：
    #    statsmodels **不是本程序的依赖**，守卫里 import 它会直接报红。
    #    黄金值是在开发机上用 statsmodels 0.15.0 算出来后**抄进来的**。
    #
    #    输入是**确定性**的（固定种子 + 解析式），所以这四个数
    #    在任何机器上都能复现；它们变了 = 算法被改动了。
    import numpy as np

    from ginan_agent.ginan import series

    n = 201
    x = np.arange(n, dtype=float)
    y = (np.sin(x / 20.0) + 0.3 * np.cos(x / 7.0)
         + 0.05 * np.random.default_rng(20260929).standard_normal(n))

    got = series.lowess(y, x, frac=2.0 / 3.0, it=3)

    # statsmodels 0.15.0 / frac=2/3 / it=3 的输出（逐位抄的）
    golden = {
        0: 1.000532186093883,
        50: 0.192831429530058,
        100: -0.374551057112216,
        200: 0.606859934622921,
    }
    for i, want in golden.items():
        check(
            abs(float(got[i]) - want) < 1e-12,
            f"★ 第 {i:3d} 点 = {float(got[i]):.15f}（statsmodels 是 {want:.15f}）",
        )

    # ---- 邻域约定：必须是 floor，不是 ceil ----
    #
    # ⚠️ 这条守的是**一个取整约定**，而它真的会差出可见的量：
    #    实测 501 点时 ceil 版与 statsmodels 差 3.5e-3，floor 版差 7.8e-15。
    #    换句话说，改成 ceil 就**不再等价**了 —— 只是"接近"。
    #    证据：n=501、frac=0.15 时 k 必须是 75 而不是 76。
    n2 = 501
    x2 = np.arange(n2, dtype=float)
    y2 = np.sin(x2 / 40.0) + 0.05 * np.random.default_rng(7).standard_normal(n2)
    got2 = series.lowess(y2, x2, frac=0.15, it=3)
    # statsmodels 0.15.0 在同一输入上的三个点（逐位抄的）
    for i, want in ((0, 0.0513007989937511),
                    (100, 0.554087284386963),
                    (500, -0.17350158164367704)):
        check(
            abs(float(got2[i]) - want) < 1e-12,
            f"★ n=501/frac=0.15 第 {i:3d} 点 = {float(got2[i]):.15f}"
            f"（statsmodels 是 {want:.15f}）",
        )

    # ---- 对照：自己造的退化输入不许崩 ----
    for label, yy, xx in (
        ("空序列", np.array([]), np.array([])),
        ("两个点", np.array([1.0, 2.0]), np.array([0.0, 1.0])),
        ("全常数", np.full(50, 3.0), np.arange(50, dtype=float)),
    ):
        try:
            out = series.lowess(yy, xx, frac=2 / 3, it=3)
            check(out.shape == yy.shape, f"对照：{label} 原样返回、不崩")
        except Exception as exc:  # noqa: BLE001
            check(False, f"对照：{label} 抛了 {type(exc).__name__}")


def test_weighted_stats_match_ginan() -> None:
    section("★ 加权统计量的口径 = Ginan 的 `compute_statistics`")

    # ⚠️ 口径是**逐行照抄** `scripts/plot_pos.py:127-145` 的，包括那条
    #    **看着不一致但必须照抄**的地方：std 加权、而 RMS 不加权。
    #    （改了就成了"看起来一样、其实不一样"。）
    import numpy as np

    from ginan_agent.ginan import series

    x = np.array([1.0, 2.0, 3.0, 4.0])
    s = np.array([1.0, 1.0, 1.0, 1.0])
    st = series.weighted_stats(x, s)
    # σ 全相等 → 加权统计量退化成普通统计量
    check(abs(st["weighted_mean"] - 2.5) < 1e-12, "σ 相等时 = 算术平均")
    check(abs(st["std_dev"] - np.std(x)) < 1e-12, "σ 相等时 = 总体标准差（不是样本的）")
    check(abs(st["rms"] - np.sqrt(np.mean(x ** 2))) < 1e-12, "RMS = sqrt(mean(x²))")

    # ⚠️ 关键的一条：**σ 不等时，RMS 不受权重影响**
    s2 = np.array([0.1, 0.1, 10.0, 10.0])
    st2 = series.weighted_stats(x, s2)
    check(
        abs(st2["rms"] - st["rms"]) < 1e-12,
        "★★ RMS 与 σ 无关（照抄 Ginan —— 它不加权）",
    )
    check(
        st2["weighted_mean"] < st["weighted_mean"],
        f"★ 而加权均值会被拉向 σ 小的那端（{st2['weighted_mean']:.4f} < "
        f"{st['weighted_mean']:.4f}）",
    )

    # ---- σ 为 0 的历元必须**排除并报数**，不能炸成 inf ----
    st3 = series.weighted_stats(np.array([1.0, 2.0, 3.0]),
                                np.array([1.0, 0.0, 1.0]))
    check(st3["n"] == 2, f"σ=0 的历元被排除（剩 {st3['n']} 个）")
    check(st3["n_dropped"] == 1, "★ 而且**报出丢了一个**（悄悄少算 = 看着像算对了）")
    check(st3["weighted_mean"] == 2.0, "剩下两个点的均值")


def test_figures_have_the_shape_the_ui_needs() -> None:
    section("★ 图的形状：等比例 / 自适应高度 / 图例不糊 / 纯文本")

    # ⚠️ 这些不是"格式偏好"，每一条都对应一个**看得见的问题**：
    #    · 平面图不等比例 → 一个南北向的长条会看起来像圆的
    #    · 写死 height    → 大屏上底下留一大片空白（第一版就是这样，截图里看到的）
    #    · 9 条踪迹全进图例 → 图例比图还高、和 rangeslider 叠在一起糊成一团
    #    · 文案带 Markdown → 侧栏/脚注走 textContent，会露出字面星号
    import datetime as dt

    from ginan_agent.ginan import charts
    from ginan_agent.ginan import posfile as P

    t0 = dt.datetime(2026, 9, 2)
    eps = [P.PosEpoch(time=t0 + dt.timedelta(seconds=30 * i), x=0.0, y=0.0,
                      z=0.0, lat=0.0, lon=0.0, height=0.0,
                      dn=0.01 * (i % 20), de=0.01 * (i % 7), du=0.02 * (i % 11),
                      sn=0.005, se=0.005, su=0.01)
           for i in range(600)]
    pf = P.PosFile(station="TEST", epochs=eps, solution="forward")

    kinds = charts.figure_kinds(pf)
    figs = charts.pos_figures(pf)
    check(
        list(figs) == kinds,
        f"★ `figure_kinds` 和 `pos_figures` 给的是同一批（{kinds}）"
        " —— 两处各判一次就迟早对不上",
    )
    check(
        set(figs) == {"time_series", "plane", "heatmap"},
        f"三张图都在（实际 {sorted(figs)}）",
    )

    for name, fig in figs.items():
        lay = fig.get("layout", {})
        check("height" not in lay,
              f"★ {name}：**不写死高度**（让前端那个浮层决定多高）")
        check(lay.get("autosize") is True, f"  {name}：autosize=True")

    ts = figs["time_series"]["layout"]
    check(
        ts["xaxis"].get("rangeslider", {}).get("visible") is True,
        "★ 时序图带 rangeslider（能拖着看某一小段 —— 这是交互图的意义）",
    )
    named = [tr for tr in figs["time_series"]["data"]
             if tr.get("showlegend") is not False]
    check(
        len(named) == 3,
        f"★ 图例只有 3 条（每个分量一条，实际 {len(named)}）"
        " —— 9 条全列出来会糊成一片、还和 rangeslider 叠在一起",
    )
    groups = {tr.get("legendgroup") for tr in named}
    check(
        groups == {"北向", "东向", "高程"},
        f"★ 而且按分量分组（{sorted(groups)}）—— 点一下开关整个分量",
    )

    check(
        figs["plane"]["layout"]["yaxis"].get("scaleanchor") == "x",
        "★★ 平面图 1:1 等比例（1 厘米东 = 1 厘米北）—— "
        "不等比例的话「哪个方向差」就读不出来了",
    )

    # ---- 文案：必须是纯文本（侧栏/脚注走 textContent）----
    for label, text in (("读图说明", charts.reading_note(pf)),):
        check("**" not in text and "`" not in text,
              f"★ {label}是纯文本（不能被自己的 Markdown 露馅）")

    # ---- 退化输入：太少历元不出图 ----
    short = P.PosFile(station="T", epochs=eps[:2], solution="forward")
    check(charts.figure_kinds(short) == [], "历元 < 3 → 一张都不出")
    check(charts.pos_figures(short) == {}, "  （`pos_figures` 同样）")


def _plotly_array(v):
    """把 figure JSON 里的一个数组取成 Python 的 list。

    ⚠️ plotly 序列化 numpy 数组时**不是**写成 `[1.0, 2.0, …]`，而是
       `{"dtype": "f8", "bdata": "<base64 的小端浮点>"}` —— 省一大截体积。
       直接 `list(v)` 拿到的是字典的**键**（`['dtype','bdata']`），
       于是断言会以一个看不懂的方式失败（我第一次就这么踩的）。

    ⚠️ 这条路径要确认过前端吃得下：随包的 `plotly.min.js` 是 **plotly.js v4.1.1**，
       `bdata` 是它认的格式（见过它渲染出曲线，见 `probe/result_panel.png`）。
    """
    if isinstance(v, dict) and "bdata" in v:
        import array
        import base64
        # 类型码是单字符：'d' = 8 字节浮点（f8），'f' = 4 字节（f4）
        a = array.array("d" if v.get("dtype", "").endswith("f8") else "f")
        a.frombytes(base64.b64decode(v["bdata"]))
        return list(a)
    return list(v)


def test_axis_zoom_never_hides_a_bad_solution() -> None:
    section("★ 纵轴默认缩放：能看清收敛之后的细节，但**绝不掩盖没收敛**")

    # ⚠️ 这条是拿真数据撞出来的：ALIC 2026-09-02 整天那份，第一个历元的
    #    高程误差棒是 **±199 厘米**，plotly 自动缩放照它把纵轴拉到 ±400 ——
    #    后面 2870 个历元全挤成一条直线，±2σ 带和趋势线一个都看不见。
    #
    #    ⚠️ 但"限纵轴"是个**有风险**的动作：限错了就是把问题藏起来。
    #       所以这里两条都要钉住：该限的限、**不该限的一个都不许限**。
    import datetime as dt

    from ginan_agent.ginan import charts
    from ginan_agent.ginan import posfile as P

    def mk(vals, sigma=0.01):
        t0 = dt.datetime(2026, 9, 2)
        return P.PosFile(
            station="T", solution="forward",
            epochs=[P.PosEpoch(time=t0 + dt.timedelta(seconds=30 * i),
                               x=0.0, y=0.0, z=0.0, lat=0.0, lon=0.0,
                               height=0.0, dn=v, de=v, du=v,
                               sn=sigma, se=sigma, su=sigma)
                    for i, v in enumerate(vals)])

    # ---- ① 开头抖 5 米、之后稳在厘米级：应该限 ----
    conv = [5.0] * 20 + [0.005 * ((i % 5) - 2) for i in range(580)]
    pf = mk(conv)
    rng = charts.time_series_figure(pf)["layout"]["yaxis"].get("range")
    check(rng is not None, "★ 开头抖几米的那份：纵轴**限了**（否则后面全挤成直线）")
    # ⚠️ 先把数字算出来再拼消息：f-string 是**无论如何都会求值**的，
    #    写成 `f"…{abs(rng[1]):.1f}…"` 时 rng 是 None 就抛 TypeError，
    #    于是这条守卫在变异下**变成崩溃**而不是"一条红的断言"（踩过）。
    lim = abs(rng[1]) if rng else float("inf")
    check(lim < 50, f"★ 而且限在厘米量级（±{lim:.1f} 厘米），不是 ±500")
    check("在范围外" in charts.reading_note(pf),
          "★ 限了就在说明里讲清楚（不然用户以为开头那段不存在）")

    # ---- ② 一直没收敛：**不许限** ----
    #    ⚠️ 这条是整条守卫的重点：一个"总是限到厘米级"的实现
    #       会把一份几米的解画成一张漂亮的图 —— 而那是**假**的。
    #    ⚠️ 夹具是**噪声**（±0.5 米上下乱跳），不是"一路漂"——
    #       漂的那种会在末尾穿过参考值，于是被判成"收敛了"（实测过：
    #       600 个历元里只有第 510~538 个落进阈值，照样返回索引）。
    #       要的是**一段连续的窗口都进不了阈值**，所以只能拿噪声。
    noisy = [((i * 7919) % 1000) / 1000.0 - 0.5 for i in range(600)]
    pf2 = mk(noisy)
    check(pf2.converged_from() < 0, "（夹具：这份确实判为没收敛）")
    check(charts.time_series_figure(pf2)["layout"]["yaxis"].get("range") is None,
          "★★ 没收敛的那份**不限纵轴** —— 限了就是把问题藏起来")
    check("在范围外" not in charts.reading_note(pf2),
          "★ 没限就别在说明里提「范围外」（说了用户会去找不存在的东西）")

    # ---- ③ 历元太少 / 平滑解那样的常数解：不炸 ----
    check(charts.time_series_figure(mk([0.01] * 4)) is not None,
          "只有 4 个历元也不炸")


def test_figure_and_title_use_one_reference() -> None:
    section("★ 曲线和标题里的数字**同源**（减的是同一个参考位置）")

    # ⚠️⚠️ 这条守卫是拿**真数据**量出来的（ALIC 2026-09-02 整天，2880 历元）：
    #     `.POS` 文件头那个参考坐标比解算自己的位置**北偏 38.36 厘米**。
    #
    #     当时的实现：曲线减过参考（贴住 0），标题里的加权均值和 RMS
    #     没减（+38.36 / 38.70）—— 同一张图上，线在 0 附近、
    #     标题写着 38 厘米，而图下面那行说明还写着"贴住 0 表示稳定下来了"。
    #
    #     ⚠️ 为什么必须断言标题：**标准差是平移不变的**，
    #        所以"曲线减了、标题没减"在肉眼上"看着挺像那么回事" ——
    #        只有均值/RMS 会露馅，而它得逐字比。
    import datetime as dt

    from ginan_agent.ginan import charts
    from ginan_agent.ginan import posfile as P
    from ginan_agent.ginan import series as gs

    OFFSET = (0.50, -0.30, 0.20)      # 文件头参考离解算位置多远（米）
    t0 = dt.datetime(2026, 9, 2)
    eps = [P.PosEpoch(time=t0 + dt.timedelta(seconds=30 * i), x=0.0, y=0.0,
                      z=0.0, lat=0.0, lon=0.0, height=0.0,
                      dn=OFFSET[0] + 0.01 * (i % 20),
                      de=OFFSET[1] + 0.01 * (i % 7),
                      du=OFFSET[2] + 0.02 * (i % 11),
                      sn=0.005, se=0.005, su=0.01)
           for i in range(600)]
    pf = P.PosFile(station="TEST", epochs=eps, solution="forward")

    fig = charts.time_series_figure(pf)
    ref = pf.self_reference()
    want = [(OFFSET[0] + 0.01 * (i % 20) - ref[0]) * 100 for i in range(600)]
    got = _plotly_array(fig["data"][0]["y"])
    check(
        len(got) == len(want)
        and max(abs(a - b) for a, b in zip(got, want)) < 1e-6,
        "★ 曲线画的是「相对解算自己位置」的偏差（减过参考）",
    )

    title = fig["layout"]["title"]
    title = title.get("text", "") if isinstance(title, dict) else str(title)
    sig = [e.sn * 100 for e in eps]
    good = gs.weighted_stats(want, sig)["weighted_mean"]
    bad = gs.weighted_stats([e.dn * 100 for e in eps], sig)["weighted_mean"]
    check(
        abs(good - bad) > 10,
        f"（夹具：两个口径的均值差 {bad - good:.1f} 厘米 —— 差得够远才验得出来）",
    )
    check(f"{good:+.2f}" in title,
          f"★ 标题里的加权均值就是曲线上那个（{good:+.2f}）")
    check(f"{bad:+.2f}" not in title,
          f"★★ 标题里**不许**出现没减过参考的那个数（{bad:+.2f}）——"
          "「曲线减了、标题没减」看着像对的，其实是两个不同的位置")




def test_trace_parser_reads_the_real_lines() -> None:
    section("★ 残差解析：拿**真文件里的原样行**当夹具")

    # ⚠️ 夹具是**逐字抄下来的真实行**（ALIC 2026-09-02 那次跑的
    #    `NET_*.TRACE`）—— 包括那些**字段内的空格对齐**（`   CODE_MEAS`、
    #    ` G15`）。按空白切分而不是按制表符切的话，卫星号会和后面的
    #    字段粘在一起，而症状是"数字看着像对的"。
    import math
    import tempfile
    from pathlib import Path as _P

    from ginan_agent.ginan import tracefile as T

    lines = [
        "+RESIDUALS/PPP",
        "#\tIt\t                  Time\t        Type\t Sat\t Str\t   Code"
        "\t       Prefit Res\t      Postfit Res\t      Meas Sigma\t"
        "    Prefit Ratio\t   Postfit Ratio\tComm",
        "%\t 0\t2026-09-02 00:00:00.01\t   CODE_MEAS\t G15\tALIC\tL1W-L2W"
        "\t      -2.08213896\t      -0.89409875\t      1.65298000"
        "\t       0.0000000\t       0.0000000\t",
        "%\t 1\t2026-09-02 00:00:00.01\t   CODE_MEAS\t G15\tALIC\tL1W-L2W"
        "\t      -1.10000000\t      -0.10000000\t      1.00000000"
        "\t       0.0000000\t       0.0000000\t",
        "%\t-1\t2026-09-02 00:00:00.01\t   PHAS_MEAS\t E36\tALIC\t    L1C"
        "\t       0.00000000\t       0.00732336\t      0.00000000"
        "\t       0.0000000\t       0.0000000\tP-L1C",
        "这行不是残差，得跳过",
    ]
    with tempfile.TemporaryDirectory() as td:
        p = _P(td) / "x.TRACE"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rf = T.parse(p)

    check(rf.n_lines == 3, f"★ 认出了 3 行残差（实际 {rf.n_lines}）")
    check(rf.n_bad == 0, f"★ 没有认错的（实际 {rf.n_bad}）")
    check(rf.blocks == ("/PPP",), f"★ 块名去重后是 ('/PPP',)（实际 {rf.blocks}）")

    # ⚠️ CODE 那两条是同一测量的两次迭代 —— 默认**只留最后一次**
    code = rf.by_kind("CODE_MEAS")
    check(len(code) == 1, f"★ 同一测量只留最后一次迭代（实际 {len(code)} 条）")
    check(abs(code[0].postfit - (-0.1)) < 1e-9,
          f"★ 留下的是 iter=1 那条（后验残差 {code[0].postfit}）")

    ph = rf.by_kind("PHAS_MEAS")
    # ⚠️ 先把值取出来再拼消息：f-string 是**无论如何都会求值**的，
    #    写成 `f"…{ph[0].sat}…"` 时 ph 是空的就抛 IndexError ——
    #    于是这条守卫在变异下**变成崩溃**而不是"一条红的断言"（踩过）。
    who = f"{ph[0].sat} {ph[0].sig}" if ph else "（一条都没有）"
    check(len(ph) == 1 and ph[0].sat == "E36" and ph[0].sig == "L1C",
          f"★ 卫星/频点没和别的字段粘在一起（{who}）")
    check(bool(ph) and abs(ph[0].postfit - 0.00732336) < 1e-9,
          "★ 后验残差读对了（0.00732336）")

    # ⚠️ 这一条是"没数到 σ"的守卫：平滑那份 σ 整列是 0，
    #    归一化残差**必须**变成 NaN，而不是悄悄算成 0。
    # ⚠️ 短路写法（`ph and …`）：ph 是空的时候**不许**去取 ph[0] ——
    #    否则这条守卫在变异下会**崩溃**而不是"变红"（同一节里踩过一次）。
    check(bool(ph) and not math.isfinite(ph[0].normalized),
          "★ σ=0 时归一化残差是 NaN（不是 0 —— 0 会看起来像「完美」）")


def test_trace_summary_reports_what_it_cannot_compute() -> None:
    section("★★ 残差统计：算不出来的东西**必须说出来**")

    import datetime as dt
    import tempfile
    from pathlib import Path as _P

    from ginan_agent.ginan import tracefile as T

    def line(i, meas, sat, sig, post, sigma, *, it=0):
        # ⚠️ 时间要**每条都不同** —— 一样的 (时间, 卫星, 频点) 会被
        #    "只留最后一次迭代"那条规则合并成一条，夹具就测不到占比了
        #    （第一版就是 ：99 条同时间戳，合并成 1 条）。
        hh, mm = divmod(i, 60)
        return (f"%\t{it}\t2026-09-02 {hh:02d}:{mm:02d}:00.01\t   {meas}\t {sat}"
                f"\tALIC\t{sig}\t       0.00000000\t{post:16.8f}"
                f"\t{sigma:14.8f}\t       0.0\t       0.0\t")

    # ① σ=0（平滑那份的样子）→ 归一化**算不出来**，而且要说出来
    with tempfile.TemporaryDirectory() as td:
        p = _P(td) / "rts.TRACE"
        p.write_text("\n".join(
            line(0, "CODE_MEAS", "G15", "L1W", 0.3, 0.0) for _ in range(5)
        ) + "\n", encoding="utf-8")
        rf = T.parse(p)
    s = T.summarize(rf)
    v = T.verdict(s)
    check(s["kinds"]["CODE_MEAS"]["normalized_available"] is False,
          "★ σ 全是 0 时，标成「归一化残差算不出来」")
    check("算不出" in v["headline"],
          f"★★ 结论里也说出来了（实际：{v['headline'][:24]}）")
    check("3σ" not in v["headline"] or "0.00%" not in v["detail"],
          "★★ 而且**不许**报成「超过 3σ 的占 0.00%」—— 那看着像完美")

    # ② σ 正常 → 归一化可用，超 3σ 的占比按真数算
    with tempfile.TemporaryDirectory() as td:
        p = _P(td) / "ppp.TRACE"
        rows = [line(i, "PHAS_MEAS", "G01", "L1W", 0.01, 0.01)
                for i in range(99)]
        # ⚠️ 那条粗差要给**更高的迭代号**（真实的粗差也是在后验迭代里才暴露的）——
        #    同一个迭代号的话，"只留最后一次迭代"规则会留下**先出现**的那条，
        #    夹具就测不到"超 3σ 的占比"了（第一次就写错在这儿）。
        rows.append(line(99, "PHAS_MEAS", "G01", "L1W", 0.5, 0.01, it=1))  # 50σ
        p.write_text("\n".join(rows) + "\n", encoding="utf-8")
        rf2 = T.parse(p)
    s2 = T.summarize(rf2)
    d2 = s2["kinds"]["PHAS_MEAS"]
    check(d2["normalized_available"] is True, "★ σ 正常时归一化可用")
    check(abs(d2["frac_over_3sigma"] - 0.01) < 1e-9,
          f"★ 超 3σ 的占比 = 1/100（实际 {d2['frac_over_3sigma']}）")

    # ③ 空文件 / 没有残差 → 结论不许说"正常"
    with tempfile.TemporaryDirectory() as td:
        p = _P(td) / "empty.TRACE"
        p.write_text("PEA starting...\n没有残差\n", encoding="utf-8")
        rf3 = T.parse(p)
    v3 = T.verdict(T.summarize(rf3))
    check(v3["quality"] == "unknown" and "没有残差" in v3["headline"],
          f"★ 没有残差时说「没有残差行」，不说「正常」（{v3['headline'][:20]}）")
    check("level" in v3["detail"] and "output_residuals" in v3["detail"],
          "★ 而且告诉用户要去开哪两个开关")


def test_resid_figures_keep_the_outliers() -> None:
    section("★★ 残差图：抽稀时**永远保住超限的点**")

    # ⚠️⚠️ 这是残差图和位置图最大的不同：位置图的离群点是噪声，
    #     残差图的离群点**正是要看的东西**。均匀抽稀会把它们抹掉，
    #     画出来一张"很干净"的图 —— 而那是假的。
    import datetime as dt
    import tempfile
    from pathlib import Path as _P

    from ginan_agent.ginan import charts
    from ginan_agent.ginan import tracefile as T

    t0 = dt.datetime(2026, 9, 2)
    rows = []
    for i in range(5000):
        rows.append(f"%\t-1\t{(t0 + dt.timedelta(seconds=30 * i)).isoformat(sep=' ')}.01"
                    f"\t   PHAS_MEAS\t G01\tALIC\tL1W\t 0.0\t 0.01\t 0.01\t 0.0\t 0.0\t")
    # 一条 40σ 的跳点 —— 它必须在图上
    rows.append(f"%\t-1\t{t0.isoformat(sep=' ')}.01\t   PHAS_MEAS\t G09\tALIC\tL1W"
                "\t 0.0\t 0.40\t 0.01\t 0.0\t 0.0\t")
    with tempfile.TemporaryDirectory() as td:
        p = _P(td) / "big.TRACE"
        p.write_text("\n".join(rows) + "\n", encoding="utf-8")
        rf = T.parse(p)

    check(rf.count == 5001, f"（夹具：5001 条，实际 {rf.count}）")
    check(rf.count > charts.RESID_MAX_POINTS,
          f"（夹具：条数要超过抽稀上限 {charts.RESID_MAX_POINTS}）")
    kept = charts._resid_sample(rf, "PHAS_MEAS")
    check(len(kept) <= charts.RESID_MAX_POINTS + 1,
          f"抽稀到 {len(kept)} 个点（上限 {charts.RESID_MAX_POINTS}）")
    check(any(abs(p.normalized) > 3 for p in kept),
          "★★ 那条 40σ 的跳点**没被抽掉**（否则图会「看起来很干净」）")
    check(any(p.sat == "G09" for p in kept), "★ 而且就是它（G09）")

    # ---- 图的形状（和 POS 那几张同一个判据）----
    summary = T.summarize(rf)
    figs = charts.resid_figures(rf, summary)
    check(list(figs) == charts.resid_figure_kinds(rf),
          f"★ `resid_figure_kinds` 和 `resid_figures` 给的是同一批（{list(figs)}）")
    check({"time", "hist"} <= set(figs), f"★ 时序和分布两张都在（{sorted(figs)}）")
    for name, fig in figs.items():
        lay = fig.get("layout", {})
        check("height" not in lay, f"★ {name}：不写死高度")
        check(lay.get("autosize") is True, f"  {name}：autosize=True")
    check(figs["time"]["layout"]["xaxis"].get("rangeslider", {}).get("visible")
          is True, "★ 时序图带 rangeslider")

    # ---- 文案（走 textContent，不能带 Markdown）----
    for label, text in (("读图说明", charts.resid_reading_note(summary)),):
        check("**" not in text and "`" not in text,
              f"★ {label}是纯文本")

    # ---- 退化输入：没有残差 → 一张图都不出 ----
    empty = T.ResidFile(path=_P("x"), points=[])
    check(charts.resid_figure_kinds(empty) == [], "没有残差 → 一张都不出")
    check(charts.resid_figures(empty, {}) == {}, "  （`resid_figures` 同样）")

def main() -> int:
    print("=" * 60)
    print("文件识别与下载自检")
    print("=" * 60)
    print("⚠️ 不联网 —— 用假 session 表演各种网络故障。")

    test_identify_by_content()
    test_line_endings_are_not_mixed()
    test_rinex_version_variants()
    test_identify_compressed()
    test_identify_edge_cases()
    test_marker_from_header()
    test_rinex_header()
    test_malformed_header()
    test_repair_and_antennas()
    test_local_obs_detection()
    test_local_obs_filename_fallback()
    test_known_vs_is_data()
    test_download_result_kind_is_plan_kind()
    test_content_derived_name_is_safe()
    test_decompress_failure_is_per_file()
    test_ga_two_step_query()
    test_download_decompresses()
    test_plot_series_is_plotable()
    test_lowess_matches_statsmodels()
    test_weighted_stats_match_ginan()
    test_figures_have_the_shape_the_ui_needs()
    test_figure_and_title_use_one_reference()
    test_axis_zoom_never_hides_a_bad_solution()
    # 残差（.TRACE）—— 解析 / 统计 / 图
    test_trace_parser_reads_the_real_lines()
    test_trace_summary_reports_what_it_cannot_compute()
    test_resid_figures_keep_the_outliers()
    test_download_keeps_nav_filename()
    test_download_multi_source_fallback()
    test_download_detects_wrong_content()
    test_download_resumes()
    test_resume_position_is_verified()
    test_download_too_long_is_rejected()
    test_unlink_quietly_never_raises()
    test_download_incomplete_detected()
    test_one_time_cache()
    test_lzw_decodes_real_file()
    test_lzw_truncated_is_normal()
    test_identify_z_format()
    test_download_decompresses_z()
    test_inbox_claim_copies()
    test_inbox_reports_the_name_it_actually_used()
    test_inbox_clear_all()
    test_inbox_clear_reports_failures()
    test_clear_runs_removes_the_whole_run_dir()
    test_inbox_claim_decompresses()
    test_inbox_same_size_different_content()
    test_already_claimed_survives_copy_semantics()
    test_inbox_deep_dirs()
    test_inbox_gz_roundtrip()
    test_inbox_same_content_two_places()
    test_crinex_decoder()
    test_inbox_decodes_crinex()
    test_fingerprint_ignores_line_endings()
    test_download_decodes_crinex()
    test_rinex_long_header()
    test_advise_aggregates()
    test_tables_endpoint_and_paths()
    test_cache_drops_observations_keeps_products()
    test_whu_trailer_and_resolver()
    test_erp_two_spellings()
    test_identify_bia_and_met()

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
