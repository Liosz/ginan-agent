"""Ginan 领域层 —— 文件识别：这是什么文件？

============================ 为什么必须靠内容 ============================
因为**文件名不可信**。三个真实场景：

  ① 用户从浏览器下载，得到 `igs20(1).atx`、`未命名(3).gz`
  ② 网盘同步过来，可能是 `新建文件夹/abc123`
  ③ **GA 的 API 返回的 URL 写着 `.crx.gz`，内容却是未压缩的明文**

第三条最阴险 —— 文件名和内容**由两个不同的系统决定**，
理论上它们应该一致，实际上不会。

实测确认（见 docs/ginan-facts.md）：
    GET ...&decompress=true  →  URL 里的文件名是 *.crx.gz
                              →  实际内容是 ASCII 明文 RINEX

所以：**读头几百个字节，按内容判断**。

============================ 为什么这里不做完整解压 ============================
识别只需要文件开头的几十个字节。

⚠️ 一个 61 MB 的 ATX 文件，如果为了"看看是什么"就整个解压到内存，
   那是纯粹的浪费 —— 而且用户的磁盘可能正紧张。

所以：
  · gzip  → 流式解压**前 4 KB** 就够了
  · 不解压整个文件
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass
from pathlib import Path

from . import lzw

# 读多少字节用来判断。
#
# ⚠️ 4 KB 是权衡：
#   太小 → RINEX 头部的 "OBSERVATION DATA" 可能在更后面（前面有一堆 COMMENT）
#   太大 → 对每个文件都多读一点，批量识别时累积起来不划算
#
# 实测 RINEX 3.x 的关键标识都在前 100 字节内，4 KB 有充足余量。
SNIFF_BYTES = 4096


# =============================================================================
# 结果
# =============================================================================


@dataclass
class FileIdentity:
    """一个文件"是什么"。"""

    kind: str = "unknown"       # sp3 / clk / erp / atx / nav / obs / sinex / unknown
    label: str = "认不出来"
    confidence: str = "none"    # high / medium / low / none
    evidence: str = ""          # 依据是什么（给用户看，也是排错线索）
    compression: str = ""       # 外层容器："" / "gzip" / "z" / "gzip?"
    crinex: bool = False        # 内容是 CrinEX（Hatanaka）—— **剥不掉，用不了**
    marker: str = ""            # RINEX 里的测站名（如果读得到）
    version: str = ""           # 文件版本

    @property
    def known(self) -> bool:
        """认出来了是什么。

        ⚠️ 注意"认出来了"**不等于"是可用的数据"**。
           `kind == "html"` 也是"认出来了" —— 我们明确知道那是个网页。
           但它显然不是数据文件。

           需要判断"这东西能不能用"的地方，用下面的 `is_data`。
        """
        return self.kind != "unknown"

    @property
    def is_data(self) -> bool:
        """这是一个**可用的数据文件**吗。

        ⚠️ 和 `known` 的区别很重要，踩过一次：

           收件箱搬运时用的是 `known` —— 于是一个网页文件
           （`kind == "html"`，认出来了）被**当数据认领搬走了**。

           搬进数据目录之后，Ginan 读到它会报一个完全无关的错。

           所以"认出来是什么"和"这东西能不能用"是**两个问题**，
           不能用一个属性应付。

           HTML 是**认得出但没用**的典型 —— 它九成是登录页、
           错误页或拦截页，也就是"这次下载其实失败了"。
        """
        return self.kind not in ("unknown", "html")

    @property
    def usable(self) -> bool:
        """这东西能**直接喂给 Ginan** 吗。

        ⚠️⚠️ 又一个"认出来是什么"和"能不能用"分家的例子 ——
           **同一个形状第二次出现**（第一次是 HTML，见上面 `is_data`）。

           CrinEX 是**认得出、也是数据、但 Ginan 不能直接读**：
           RINEX 的头部（含 MARKER NAME）是明文，所以识别得很顺、
           `kind="obs"`、`marker` 也读得到 —— 一路看起来正常。
           但观测段是 Hatanaka 编码，而 Ginan 自己文档写着
           （`ginan-dist/source/src/cpp/common/acsConfigDocs.cpp:20`）：

               The data should be uncompressed rinex (gunzipped,
               and not in hatanaka format)

           👉 判据刻意**不用** `compression` ——
              `.crx` 和 `.crx.gz` 是同一个问题的两种包装，
              任何调用方只要问"能不能**直接**用"，就不该被迫先剥一层壳再问。

        ⚠️⚠️ **注意"直接"这两个字。** 这个属性**不**表示"这份数据没用" ——
           `ginan/crinex.py` 能把它解成明文 RINEX（纯 Python，零依赖，
           对着 48 份真文件逐行验过），**收件箱和下载路径都会自动解**。

           所以它的正确用法是：
             · `find_local_obs()` —— 数据目录里躺着 CrinEX，说明有人绕过流程
               放进去的，**不该当"本地已经有"直接交出去**
             · `advise.py` —— 判"这一份能不能参与解算"（那里会**先解头部**
               再判，所以 CrinEX 一样能算出候选）

           **不是**"看到 False 就告诉用户用不了"。
        """
        return self.is_data and not self.crinex


# =============================================================================
# 识别规则
# =============================================================================
#
# ⚠️ 每条规则都记录它匹配到的**原文**（evidence）。
#
#    因为识别错误是最难查的一类问题 —— 用户会说"它把这个文件认成别的了"，
#    而我们没有任何线索。带上依据，至少能看出是哪条规则误判。

# (kind, 人话名字, 特征标记, 出现位置)
#
# 出现位置：
#   0 = 必须在第一行（格式的强标志，可信度最高）
#   1 = 在前若干行里出现即可
RULES: list[tuple[str, str, str, int]] = [
    # ---- 强标志：第一行就能确定 ----
    ("sp3",   "卫星轨道（SP3）",     "#a",              0),
    ("sp3",   "卫星轨道（SP3）",     "#c",              0),
    ("sp3",   "卫星轨道（SP3）",     "#d",              0),
    ("sinex", "SINEX 解算文件",      "%=SNX",           0),
    # ⚠️⚠️ ERP 的第一行也有**两种写法** —— 和下面导航文件是同一个教训：
    #
    #        IGS/BKG 的：  `version 2`   （小写）
    #        ESA 的：      `VERSION 2`   （大写）
    #
    #    实测踩到：ESA 的 `ESA0OPSFIN_…_ERP.ERP.gz` 被识别成"认不出来"，
    #    于是**下载器把它丢弃**，现象是"ERP 下载失败"——
    #    而真正的原因在这一行规则表里。
    #
    #    ⚠️ 匹配是 `first.startswith(marker)`，**大小写敏感**。
    #       只写一个小写的话，大写那份永远认不出来。
    #
    #    ⚠️ 后来补到**第三种**（iGMAS 的 `Version 2`）才看明白：
    #       这条路走不通 —— 下一个源还有第四种写法。
    #       → 所以比较改成了**大小写不敏感**（见下面那行 `first_low`），
    #         规则表里只需要留这**一条**。
    ("erp",   "地球自转参数（ERP）", "version 2",       0),

    # ---- 需要看头部内容 ----
    ("atx",   "天线改正表（ANTEX）", "ANTEX VERSION",   1),
    ("obs",   "观测数据（RINEX）",   "OBSERVATION DATA", 1),

    # ⚠️⚠️ 导航文件有**两种写法**，必须都列上 —— 只写一个会漏掉另一个。
    #
    #     RINEX 2.x：  `     2.11           NAVIGATION DATA     G (GPS)   RINEX VERSION / TYPE`
    #     RINEX 3.x：  `     3.04           N: GNSS NAV DATA    M: MIXED  RINEX VERSION / TYPE`
    #                                        ↑ 3.x 把类型挪到了这一列，正文改叫 "NAV DATA"
    #
    #     ⚠️ 原来只有 `"NAVIGATION DATA"` 那一条 —— 于是**所有 RINEX 3 的
    #        导航文件都认不出来**（报"认不出来"）。
    #
    #     而这个错误的**后果特别隐蔽**：文件其实**下载成功了**，
    #     但识别环节说"内容对不上"，下载器据此**把它丢弃** ——
    #     于是现象是"广播星历下载失败"，而真正的原因在读文件的那几行代码里。
    #
    #     实测踩到：BRDC00IGS_R_20260010000_01D_MN.rnx（BKG，753 KB，
    #     HTTP 200）被识别成"认不出来"，理由是开头是
    #     `3.04  N: GNSS NAV DATA  M: MIXED  RINEX`。
    #     ——**报错信息里就写着正确答案**，但规则表里没有它。
    ("nav",   "广播星历（RINEX）",   "NAVIGATION DATA",  1),   # RINEX 2.x
    ("nav",   "广播星历（RINEX）",   "NAV DATA",         1),   # RINEX 3.x

    # ⚠️⚠️ 下面两类是 2026-09-21 补的。补之前它们**认不出来** ——
    #     而 `bia` 特别值得记一笔：**词表里有它，辨认层却产不出它**。
    #
    #     `catalog.KINDS` 有 `bia`（下载那一路会产出它，见 `_cod_bias_urls`），
    #     `advise.OPTIONAL_KINDS` 也读它 —— 但 `identify` 一条规则都没有。
    #     于是 `kind == "bia"` **永远不可能为真**：
    #
    #       · "还缺 bia" 这句话**永远成立**（哪怕文件就在手边）
    #       · 用户往收件箱丢一个 `.BIA`，会被判成"认不出来"、认领不了
    #
    #     > 这就是「**后端从不返回某个键，调用方一直在读**」那张老面孔 ——
    #     > 第一次是 `compression == "hatanaka"`，这是第二次。
    #
    #     实测原文（用户那份 `COD0OPSRAP_…_OSB.BIA`，第一行）：
    #         %=BIA 1.00 COD 2026:246:36196 IGS 2026:245:00000 …
    #     ⚠️ GFZ 的 **DCB** 文件第一行也长这样 —— DCB 和 OSB 是两种偏差，
    #        但都是 `%=BIA` 容器，所以这里合成一类（Ginan 自己会分辨）。
    ("bia",   "码偏差（OSB/DCB，BIA）", "%=BIA",          0),

    # ⚠️ 气象文件（`_MM.rnx`）—— 用户那份数据里有 9 个，全被报成"认不出来"。
    #    而它们是完全标准的 RINEX MET。实测第一行：
    #         3.02           METEOROLOGICAL DATA      RINEX VERSION / TYPE
    #    和 `OBSERVATION DATA` 同一个位置，所以用同一种弱标志匹配。
    #
    #    ⚠️ **本程序还没有把 met 接进 Ginan 配置**（`inputs` 里没有它，
    #       对流层用的是简化模型 / 外部格网）。所以认出它只是**不再谎报
    #       "认不出"**，不代表这些文件会参与解算。见 `catalog.KINDS["met"]`。
    ("met",   "气象数据（RINEX MET）", "METEOROLOGICAL DATA", 1),
]

# RINEX 时钟文件要特殊处理：
# 它的头部也是 "RINEX VERSION / TYPE"，但类型列是 'C' 而不是 'O'/'N'。
# 所以不能只靠关键词，要看那一行的具体内容。


# =============================================================================
# 读取与解压
# =============================================================================


def sniff_bytes(path: Path, limit: int = SNIFF_BYTES) -> tuple[bytes, str, bool]:
    """读出文件开头的若干字节，自动处理压缩。

    返回 (字节, 外层压缩类型, 内容是不是 CrinEX)。

    ⚠️ gzip 用流式解压，只取前 limit 字节 —— 不解压整个文件。
       一个 61 MB 的 ATX 没必要为了识别全解开。

    ⚠️⚠️ 为什么 CrinEX 要**单独**一个返回值，而不是塞进"压缩类型"：

       `.crx.gz` 是**两层** —— 外面 gzip，里面 CrinEX。
       一个字符串表达不了两件事，任何"挑一个说"的写法都会丢掉一层。

       而且这两层的**处理方式完全不同**：
         · gzip 剥掉就完了（`_decompress_into` / `_gunzip`）
         · CrinEX **剥不掉** —— 我们没有解码器，
           而 Ginan 自己文档写着要"not in hatanaka format"

       所以：`compression` 只说"外层容器能不能剥"，
             `crinex` 单独说"剥完之后还能不能用"。
    """
    try:
        raw = path.open("rb").read(limit)
    except OSError:
        return b"", "", False

    # gzip 魔数：0x1f 0x8b
    if raw[:2] == b"\x1f\x8b":
        try:
            with gzip.open(io.BytesIO(raw)) as gz:
                # ⚠️ 这里可能抛 EOFError —— 因为我们只喂了前 4 KB，
                #    而 gzip 流显然没结束。**这是正常的**，
                #    我们要的就是"有多少读多少"。
                head = gz.read(limit)
            return head, "gzip", _looks_like_hatanaka(head)
        except (EOFError, OSError, gzip.BadGzipFile):
            # 退而求其次：直接解压原始字节（可能本来就是残缺的）
            try:
                head = gzip.decompress(raw)[:limit]
                return head, "gzip", _looks_like_hatanaka(head)
            except Exception:  # noqa: BLE001
                return raw, "gzip?", False

    # Unix compress（.Z）：魔数 0x1f 0x9d
    #
    # ⚠️⚠️ **这一条不加，老数据会被下载器当成"认不出来"直接删掉。**
    #
    #    症状链条很长：文件名对了、下载 200 了、字节数也对，
    #    但识别说"内容对不上" → 白名单把文件删了 → 用户看到的是
    #    "下载失败"，而根因在读文件的这几行。
    #
    #    （这条坑的形状和当年的 gzip 一模一样：
    #      下载器不解压 → 识别通过（它内部会解压看内容）→
    #      磁盘上那个文件仍然是压缩的。**同一个形状，第二次出现。**）
    #
    # ⚠️ 和 gzip 分支同一个道理：只喂前面一点字节，
    #    解到够识别就停。`unlzw` 允许截断，返回已解出的部分。
    if raw[:2] == lzw.MAGIC:
        try:
            head = lzw.unlzw(raw, max_out=limit)
            return head, "z", _looks_like_hatanaka(head)
        except lzw.LzwError:
            # 坏数据。**标成 "z?" 而不是"认不出来"** ——
            # 用户/模型看到 "z?" 就知道"这是个 .Z，但解不开"，
            # 比一句笼统的"内容对不上"可查得多。
            return raw, "z?", False

    # Hatanaka（CRX）是文本格式，但需要专用解码器才能读。
    # ⚠️ 这里**不尝试解码** —— 因为：
    #    ① 识别只需要看头部，而 CRX 的头部通常是明文的
    #    ② 完整解码要引 hatanaka 依赖，而它在识别阶段不是必需的
    #
    # ⚠️⚠️ 但"不解码"≠"不识别"。见 `_looks_like_hatanaka` 里那段说明：
    #    这里**必须**认出来，否则调用方会把 CRX 当成普通 RINEX，
    #    给它起一个 `.rnx` 的名字落盘 —— 而 Ginan 读不了它。
    return raw, "", _looks_like_hatanaka(raw)


# CrinEX 的自述标记。**第一行就有**，标签列是 `CRINEX VERS   / TYPE`。
#
#     3.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE
_CRINEX_MARKER = "COMPACT RINEX FORMAT"


def _looks_like_hatanaka(head: bytes) -> bool:
    """判断这是不是一个 CrinEX（Hatanaka 编码的 RINEX）。

    ============================ ⚠️⚠️ 判据换过一次 ============================
    旧写法数"头部**非打印字符**的比例 > 2%"，注释写着
    「CRX 的观测段里有大量非打印字符，正常 RINEX 的观测段是纯 ASCII」。

    **这句话是错的**，而且错得很安静 —— 它导致这个函数**永远返回 False**：

        实测那 48 个真 CrinEX 文件（`userdata/inbox/…/rinex3_crx/`）：
            前 4096 字节非打印   0.00%
            整个文件非打印       0.00%

    CrinEX 用**可打印的变长编码**，不是二进制乱码。而且它的头部
    本来就是明文 RINEX —— 而 `sniff_bytes` 只喂头部 4096 字节。
    两个原因叠起来，那个 2% 阈值一次都够不到。

    👉 后果：`compression="hatanaka"` 成了一个**从来没返回过的值**，
       而 `identify()` 里读它的那个分支（"观测数据（Hatanaka 压缩）"）
       **一次都没执行过**。
       又一次「**后端从不返回某个键，调用方一直在读**」。

    ============================ 现在的判据 ============================
    **让格式自己说** —— CrinEX 第一行就写着 `COMPACT RINEX FORMAT`。

        实测：48 个真 CrinEX 文件，**48/48 命中**
             146 个非 CrinEX 文件，**0 个误判**

    > 能用"格式声明的"就别用"看起来像的"。
    > 统计式启发法在阈值附近总要犯错，而显式标记不会。
    > （这条教训这个仓库已经付过一次学费：ERP 的 `version 2` / `VERSION 2` /
    >   `Version 2` —— 见 RULES 表上面那段。）
    """
    try:
        text = head.decode("ascii", errors="replace")
    except Exception:  # noqa: BLE001
        return False
    return _CRINEX_MARKER in text


# =============================================================================
# 主函数
# =============================================================================


def identify(path: Path) -> FileIdentity:
    """判断一个文件是什么。

    ⚠️ **这个函数不抛异常。**
       它会在用户"往收件箱里扔了一堆不知道什么文件"时被批量调用，
       其中一个不可读不该让整批失败。
    """
    path = Path(path)
    if not path.is_file():
        return FileIdentity(evidence="文件不存在")

    head, compression, crinex = sniff_bytes(path)
    if not head:
        return FileIdentity(
            compression=compression, crinex=crinex, evidence="文件是空的或读不了"
        )

    # 用 latin-1 解码 —— 它**永不失败**（每个字节都有对应字符）。
    # ⚠️ 这里刻意不用 utf-8 + errors='replace'：
    #    utf-8 解码失败会产生 U+FFFD，那会让关键词匹配变得不可靠。
    #    而我们只做 ASCII 关键词匹配，latin-1 足够了。
    text = head.decode("latin-1", errors="replace")
    lines = text.splitlines()

    # ---- 先试强标志（第一行） ----
    first = (lines[0] if lines else "").strip()

    # ⚠️⚠️ **大小写不敏感地比。**
    #
    #     原来是 `first.startswith(marker)`（敏感），于是要"把每种写法都列一遍"。
    #     而 ERP 的第一行实测有**三种**写法：
    #
    #         IGS / BKG：  `version 2`     （全小写）
    #         ESA：        `VERSION 2`     （全大写）
    #         iGMAS：      `Version 2`     （首字母大写）
    #
    #     ⚠️ 前两种是**逐个补进规则表**的 —— 补到第三种时才看明白：
    #        **"列举写法"这条路走不通**，因为下一个源还有第四种。
    #
    #     👉 改成不敏感比较之后，规则表里只需要留一条 `version 2`。
    #        检查过其余标记（`#cP`、`%=SNX`、`ANTEX VERSION`、`OBSERVATION DATA`）
    #        —— 它们本来就是大写或小写的固定写法，放宽大小写不会误伤。
    first_low = first.lower()

    for kind, label, marker, where in RULES:
        if where == 0:
            if first_low.startswith(marker.lower()):
                return FileIdentity(
                    kind=kind, label=label, confidence="high",
                    evidence=f"首行以 {marker!r} 开头",
                    compression=compression, crinex=crinex,
                    version=_guess_version(kind, lines),
                )

    # ---- RINEX 时钟：要看类型列 ----
    #
    # RINEX CLK 的头部第 1 行形如：
    #         3.00           C                                       RINEX VERSION / TYPE
    #                                 ↑ 这一列是 C（Clock）
    # 而观测是 O、导航是 N。所以**必须看那一列**，不能只看关键词。
    for ln in lines[:5]:
        if "RINEX VERSION / TYPE" in ln:
            typ = ln[20:21].strip() if len(ln) > 20 else ""
            if typ == "C":
                return FileIdentity(
                    kind="clk", label="卫星钟差（RINEX CLK）",
                    confidence="high",
                    evidence="RINEX 头部类型列为 C",
                    compression=compression, crinex=crinex,
                    version=ln[:9].strip(),
                )
            break

    # ---- 再试弱标志（头部若干行） ----
    for kind, label, marker, where in RULES:
        if where == 1 and marker in text:
            # ⚠️ 观测数据要额外说清是不是 CrinEX。
            #
            #    这一句以前写的是 `compression == "hatanaka"` —— 而那个值
            #    **从来没被返回过**（判据坏了，见 `_looks_like_hatanaka`），
            #    所以这个分支一次都没执行过。现在判 `crinex`，它真的会为真。
            #
            #    ⚠️ 而且这里**必须**把话说全：光说"观测数据"会让用户以为
            #       它能用。它是**认得出、用不了**的那种。
            if kind == "obs" and crinex:
                # ⚠️ 这句原来是「需先转成 RINEX」—— 现在**我们转得了**
                #    （`ginan/crinex.py`），所以说法要跟着变：
                #    对用户来说它不再是"你要去想办法"，而是"程序会处理"。
                label = "观测数据（CrinEX 编码，认领时自动解开）"
            return FileIdentity(
                kind=kind, label=label, confidence="high",
                evidence=f"头部含 {marker!r}",
                compression=compression, crinex=crinex,
                marker=_extract_marker(text) if kind in ("obs", "nav") else "",
                version=_guess_version(kind, lines),
            )

    # ---- 网页：单独识别，不归到"认不出来" ----
    #
    # ⚠️ 这一条是**必须单独列的**，不能混进 unknown。
    #
    #    因为"下载成功但内容是 HTML"是一个**特定且常见**的故障：
    #    认证没过（CDDIS 返回登录页）、路径不对（返回错误页）、
    #    被中间设备拦截（返回警告页）……
    #
    #    如果它只被归成"认不出来"，调用方可能觉得"大概是某种没用过的
    #    格式吧"就放过去了 —— 于是**把一个登录页当成数据文件存了下来**。
    #
    #    实测踩过：CDDIS 的登录页被存成了 .SP3 文件，
    #    校验和、大小、落盘全部"成功"。
    #    直到跑 Ginan 才报一个和真正原因毫不相干的错。
    #
    #    所以给它一个**明确的名字**，让调用方能专门拒绝它。
    stripped = text.lstrip()[:200].lower()
    if stripped.startswith("<!doctype html") or stripped.startswith("<html"):
        # ⚠️ 尽量说清是**哪种**网页 —— 因为用户该做的事完全不同。
        #
        #    "登录页" → 去配账号
        #    "404"    → 换个源或换日期（**和账号无关**）
        #
        #    实测踩过：把 CDDIS 的 404 页面当成"认证没过"报了，
        #    于是往"检查用户名密码"的方向排查 —— 而真正的问题是
        #    **文件名拼错了**（用了还没发布的 FIN 产品名）。
        #
        #    一个指向错误方向的报错，比没有报错更浪费时间。
        kind_hint = "网页（不是数据文件）"
        if "404" in text[:2000] or "not found" in text[:2000].lower():
            kind_hint = "网页（404 错误页）"
        elif "earthdata login" in text[:2000].lower():
            kind_hint = "网页（登录页）"

        return FileIdentity(
            kind="html",
            label=kind_hint,
            confidence="high",
            evidence=f"内容是 HTML —— {kind_hint}",
            compression=compression, crinex=crinex,
        )

    # ---- 认不出来 ----
    #
    # ⚠️ 认不出来时**不要猜**。
    #    返回 unknown 让调用方去问用户，比猜一个错的强 ——
    #    猜错的后果是把文件搬到错误的位置，而 Ginan 会给出
    #    一个和真正原因毫不相干的报错。
    preview = first[:60] if first else "(空)"
    return FileIdentity(
        confidence="none",
        compression=compression, crinex=crinex,
        evidence=f"认不出来，开头是：{preview!r}",
    )


def _extract_marker(text: str) -> str:
    """从 RINEX 头部抓测站名。

    ⚠️ 这是**权威来源** —— 比文件名可靠得多。
       GinanUI 踩过的坑就是从文件名截前 4 个字符当测站名，
       遇到非标准命名会静默得到错误结果。

    ⚠️⚠️ 这个函数踩过一个 IndexError，原因值得记下来：

       原来的写法是

           return ln[:60].strip().split()[0] if ln.strip() else ""

       守卫检查的是 `ln.strip()`（**整行**有没有内容），
       而实际取值用的是 `ln[:60].strip()`（**左边 60 列**有没有内容）。

       两者不是一回事：RINEX 的头部是定宽的，
       标签固定在第 60 列之后。所以下面这种行是合法的：

           '                                                            MARKER NAME'

       整行有内容（守卫通过），但左边 60 列是空的
       （`.split()` 返回空列表，`[0]` 就越界）。

       用户碰到的就是这个 —— 他的一份数据里就有这么一行空标记名。

       > **守卫必须检查你实际要用的那个东西。**
       > 检查相邻的东西看起来也能防住常见情况，
       > 但恰恰在最不常见的地方漏掉。
    """
    for ln in text.splitlines()[:80]:
        if "MARKER NAME" not in ln:
            continue
        field = ln[:60].strip()      # ← 和下面用的**同一个表达式**
        parts = field.split()
        if not parts:
            return ""
        got = parts[0]
        # ⚠️⚠️ 标签**落在第 1 列**的不合规文件，取到的"测站名"就是标签本身。
        #
        #     RINEX 是定宽格式，标签应该在第 60 列之后；但实测确实存在
        #     把标签顶到最前面的文件（否则上面那段 IndexError 的注释
        #     也不会存在）。
        #
        #     那时 `ln[:60]` 是 `'MARKER'`，于是"测站名"成了 `MARKER` ——
        #     而下游（`fetch._decide_name`）会拿它去拼文件名，
        #     得到一个 `MARKER.rnx`。**它看起来像个正常名字，
        #     所以不报错、不警告，一路写进配置。**
        #
        #     ⚠️ 判据用**整词相等**而不是"包含"：真实站号是 4 个字母，
        #        不会是 `MARKER` 这 6 个字母。宁可这里返回空
        #        （调用方会退回用文件名/请求名），也不要造一个错名字。
        if got.upper() == "MARKER":
            return ""
        return got
    return ""


def _guess_version(kind: str, lines: list[str]) -> str:
    """抓版本号 —— 只用于显示，抓不到也没关系。"""
    if kind == "sp3":
        # SP3 的版本在首行：`#cP2026 9 2 ...`
        return lines[0][1:2] if lines and len(lines[0]) > 1 else ""
    for ln in lines[:3]:
        if "VERSION" in ln.upper():
            return ln[:9].strip()
    return ""


# =============================================================================
# 批量识别
# =============================================================================


def identify_many(paths: list[Path]) -> dict[Path, FileIdentity]:
    """批量识别。给收件箱用。"""
    return {Path(p): identify(Path(p)) for p in paths}


def describe_kinds() -> list[tuple[str, str]]:
    """列出所有能识别的类型 —— 给界面显示"我能认哪些"。"""
    seen: dict[str, str] = {}
    for kind, label, _, _ in RULES:
        seen.setdefault(kind, label)
    seen.setdefault("clk", "卫星钟差（RINEX CLK）")
    return sorted(seen.items())
