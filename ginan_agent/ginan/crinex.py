"""Ginan 领域层 —— CrinEX（Hatanaka 压缩 RINEX）解码。**纯 Python，零依赖。**

============================ 为什么需要它 ============================
IGS 分发 RINEX 3 观测数据的**标准形式就是 `.crx.gz`**（CDDIS 等主数据中心
只给这个）。而 **Ginan 读不了 CrinEX** —— 它自己的文档写着：

    ginan-dist/source/src/cpp/common/acsConfigDocs.cpp:20
    > The data should be uncompressed rinex (gunzipped,
    > and not in hatanaka format), or RTCM3 formatted binary data.

所以在有这个东西之前，从 CDDIS 下的观测数据**一份都用不了**。

============================ ⚠️ 为什么是"手写"而不是"加依赖" ============================
PyPI 上有个 `hatanaka` 包（MIT），但它**不是纯 Python** ——
wheel 里直接打包了 RNXCMP 的 C 二进制（`crx2rnx.exe`），
还多带 `ncompress`、`importlib-resources` 两个依赖。

这个项目刚因为 GPL-3 把 Ginan 的二进制挡在门外，**不引入捆绑二进制**。

而且**有先例**：`ginan/lzw.py` 就是这么手写 LZW 来解 `.Z` 的 ——
零依赖、可单测、行为完全可控。

============================ ⚠️⚠️ 怎么保证它是对的 ============================
这是**数值敏感的代码**，写错的样子恰好是这个项目最怕的那种 ——
「看起来正常、只是精度差」。所以它不能靠"读起来对"来验收。

**验收基准**：用户收件箱里那 48 份 `.crx.gz`，**每一份都有同站同天的 `.rnx`**。
用参考实现（RNXCMP 的 `crx2rnx`）验过：**48/48 逐字节完全一致**。

于是这个解码器的验收是**逐字节比对**，不是"看着差不多"。
（`check_files.py` 里的夹具就是从那 48 对里取的。）

============================ 算法 ============================
来自 RNXCMP 的格式说明与参考实现：

  **头部**：第 1 行是 `CRINEX VERS / TYPE`（选语法），第 2 行 `CRINEX PROG / DATE`
  **丢弃**，其余原样复制到输出。

  **数据段**两个差分引擎：

    · `TextDiff` —— **逐字符**。空格保留、`&` 清成空格、其他字符覆写。
      管**历元行**和每颗卫星的 **LLI/SSI 标志串**。

    · `NumDiff` —— **整数高阶差分**。观测值乘 1000 变成整数（钟差乘 1e12），
      用**带符号的二项式系数**（帕斯卡三角第 m 行交替正负）从增量重建：

          new = delta + Σ(k=0..m-1) (-1)^k · C(m, k+1) · buf[k]

      阶数 m 每来一个样本涨 1，直到 `level`（rnx2crx 用 3）。
      `order&value` 这个记号是一次**弧段重置**（换星、丢锁之后重来）。

============================ ⚠️ 输出为什么是 LF ============================
实测三种文件的行尾，**同一个"家族"里两种都有**：

    `.crx.gz`（输入，CDDIS 上游）                  LF
    `.rnx`  （用户手上那份期望输出）               CRLF
    `BRDC…_MN.rnx.gz`（我们下的广播星历）          LF
    `TWTF…_MO.rnx.gz`（明文 RINEX 3，压过的）      CRLF

👉 **行尾是"谁生成的"的属性，不是格式的属性。** 规范明说
   crx2rnx 在 Unix 上出 LF、在 DOS 上出 CR+LF —— 那份 `.rnx` 的 CRLF
   是**编译产物的副作用**。

所以这里**固定输出 LF**：
  · 输入本来就是 LF（CDDIS 的 `.crx.gz` 实测就是）
  · 跨平台一致，也是本仓库 `.gitattributes` 对文本的约定
  · Ginan 两种都读（我们自己 `rinex.py` 里就是 `splitlines()`）

⚠️ 而且**这个程序本来就在给 Ginan 喂两种行尾** ——
   `_decompress_into` 是字节保真的，`.rnx.gz` 里的 CRLF 原样落盘。
   所以"多一种行尾"不引入新风险。

⚠️ **验收时要归一化行尾再比** —— 逐字节比的是**内容**，
   不是那个平台副产物。`check_files.py` 里就是这么比的。

============================ ⚠️ 只做 CRINEX 3.0 ============================
有两代格式：
    CRINEX 3.0  —— 压 RINEX 3，文件名 `.crx` / `.crx.gz`   ← **本模块做这个**
    CRINEX 1.0  —— 压 RINEX 2，文件名 `.d` / `.d.Z`        ← 遇到会**明确拒绝**

不是做不了，是**不一次做两代**：3.0 是现在 IGS 在发的，
1.0 是老数据（IGS 周 2237 之前）。混着做的风险是"两边都半吊子"，
而这里的错误代价是**静默的数值错误**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ⚠️ 差分阶数的**安全上限**。rnx2crx v4.0.3 固定用 3，
#    而 crx2rnx 允许到 5。这里留到 6 —— 参考实现也是 6
#    （它的注释写着"classic M = 5 history"）。
#
#    超过这个值说明**记号是坏的**，不要照着算（算出来是个看着像数的东西）。
MAX_ORDER = 6

# RINEX 3 一个观测字段的宽度：`F14.3` 的值 + LLI + SSI
OBS_FIELD_WIDTH = 16
OBS_VALUE_WIDTH = 14


class CrinexError(ValueError):
    """CrinEX 数据坏了 / 不认识。

    ⚠️ 继承 `ValueError` —— 调用方（`inbox` / `fetch`）已经习惯把
       "这个文件不能用"表达成 ValueError。见 `fetch` 里对 `_decide_name`
       那个 try/except 的说明。
    """


# =============================================================================
# 两个差分引擎
# =============================================================================


@dataclass
class NumDiff:
    """整数高阶差分引擎（Hatanaka 的 `NumDiff`）。

    ⚠️ **这是整个解码器的核心，也是最容易写错的地方。**
       重建用**带符号的二项式系数**：

           new = delta + Σ(k=0..m-1) (-1)^k · C(m, k+1) · buf[k]

       `buf` 是历史，**最近的在前**。

       写错一个系数，结果**仍然是一个像模像样的数** ——
       这正是这里最危险的地方。所以它逐字节对着真文件验。

    ⚠️ 这里是**热路径**：一份日观测有 270 万个观测，每一个都要过这儿。
       所以下面写得比"顺眼"更靠近"快" ——
       系数硬编码、`buf` 用定长列表按下标搬。

       👉 但是**先对着 48 份真文件逐字节验过，再改的写法** ——
          改了之后又验了一遍。**热路径的优化不该拿正确性赌**：
          那 48 份就是这次的验收基准，`git` 里能复现。
    """

    level: int
    m: int = 0
    buf: list[int] = field(default_factory=lambda: [0] * MAX_ORDER)

    def force_init(self, data: int, level: int) -> int:
        """弧段重置：清阶数、换 level、把历史种成这个值。"""
        self.m = 0
        self.level = level
        b = self.buf
        b[5] = b[4]; b[4] = b[3]; b[3] = b[2]
        b[2] = b[1]; b[1] = b[0]; b[0] = data
        return data

    def decompress(self, delta: int) -> int:
        """从增量重建下一个值，阶数朝 `level` 涨。

        ⚠️ 下面那张"硬编码的系数表"**不是抄来的**，是**算出来的**：
              m=1: +1·b0
              m=2: +2·b0 −1·b1
              m=3: +3·b0 −3·b1 +1·b2
              m=4: +4·b0 −6·b1 +4·b2 −1·b3
              m=5: +5·b0 −10·b1 +10·b2 −5·b3 +1·b4
              m=6: +6·b0 −15·b1 +20·b2 −15·b3 +6·b4 −1·b5
           就是帕斯卡三角第 m 行、隔一个取一个、符号交替。

           ⚠️ **表外（m > MAX_ORDER）不该发生** —— `_parse_reset` 已经卡住了。
              真到了那里说明记号坏了，**报错，别硬算**：
              硬算会拿一个不存在的阶数重建，得到一个**看着像数的错值**。
        """
        if self.m < self.level:
            self.m += 1
        m = self.m
        b = self.buf
        if m == 1:
            new = delta + b[0]
        elif m == 2:
            new = delta + 2 * b[0] - b[1]
        elif m == 3:
            new = delta + 3 * b[0] - 3 * b[1] + b[2]
        elif m == 4:
            new = delta + 4 * b[0] - 6 * b[1] + 4 * b[2] - b[3]
        elif m == 5:
            new = delta + 5 * b[0] - 10 * b[1] + 10 * b[2] - 5 * b[3] + b[4]
        elif m == 6:
            new = (delta + 6 * b[0] - 15 * b[1] + 20 * b[2]
                   - 15 * b[3] + 6 * b[4] - b[5])
        else:
            raise CrinexError(f"差分阶数 {m} 超出 1..{MAX_ORDER} —— 记号是坏的")
        b[5] = b[4]; b[4] = b[3]; b[3] = b[2]
        b[2] = b[1]; b[1] = b[0]; b[0] = new
        return new


@dataclass
class TextDiff:
    """逐字符差分引擎（Hatanaka 的 `TextDiff`）。

    规则（就三条）：
        空格  → 保留缓冲区那一位
        `&`   → 把这一位**清成空格**
        其他  → 覆写

    ⚠️ 比缓冲区长的输入，**多出来的部分直接续上去** —— 历元行的
       卫星列表会随历元变长，那一位没有"上一个值"可保留。
    """

    buffer: str = ""

    def force_init(self, data: str) -> str:
        self.buffer = data
        return data

    def decompress(self, data: str) -> str:
        if len(data) > len(self.buffer):
            self.buffer += data[len(self.buffer):]
        out = list(self.buffer)
        for i, ch in enumerate(data):
            if ch == " ":
                continue
            if i < len(out):
                out[i] = " " if ch == "&" else ch
        self.buffer = "".join(out)
        return self.buffer


# =============================================================================
# 记号
# =============================================================================


def _parse_reset(token: str) -> tuple[int, int] | None:
    """解析 `order&value` 这个重置记号。不是重置就返回 None。

    ⚠️ `order` 必须在 1..MAX_ORDER 之间。**越界要报错，不能照算** ——
       照算的话会拿一个不存在的阶数去重建，得到一个看着像数的错值。
    """
    amp = token.find("&")
    if amp < 0:
        return None
    try:
        order = int(token[:amp])
    except ValueError as exc:
        raise CrinexError(f"重置记号的阶数不是整数：{token!r}") from exc
    if not (1 <= order <= MAX_ORDER):
        raise CrinexError(f"重置记号的阶数 {order} 超出 1..{MAX_ORDER}：{token!r}")
    try:
        value = int(token[amp + 1:])
    except ValueError as exc:
        raise CrinexError(f"重置记号的值不是整数：{token!r}") from exc
    return order, value


def _apply_token(engine: NumDiff | None, token: str) -> tuple[NumDiff, int]:
    """一个观测/钟差记号 → (引擎, 恢复出来的值)。"""
    reset = _parse_reset(token)
    if reset is not None:
        order, value = reset
        if engine is None:
            engine = NumDiff(level=order)
        return engine, engine.force_init(value, order)
    try:
        delta = int(token)
    except ValueError as exc:
        raise CrinexError(f"增量不是整数：{token!r}") from exc
    if engine is None:
        # ⚠️ 没有弧段就开始给增量 —— 说明数据坏了。
        #    硬猜一个基准的话，整条弧段的**每一个值都会偏同一个常数**，
        #    而结果看起来完全正常。**必须报错。**
        raise CrinexError(f"这个值之前没有弧段初始化记号：{token!r}")
    return engine, engine.decompress(delta)


# =============================================================================
# 输出格式化
# =============================================================================


def _format_scaled(scaled: int, decimals: int, width: int) -> str:
    """缩放整数 → 定宽小数字段。

    ⚠️⚠️ **绝对值小于 1 的值，前导零要换成空格**：

           0.197  →  `          .197`
          -0.170  →  `         -.170`

       这是 RNXCMP 的**格式怪癖**（不是我们发明的），
       而它**必须照做** —— 逐字节比对时差这一个字符就是"不一样"。

    ⚠️⚠️ **这条规则是实测钉死的，而且被参考实现坑过一次。**

       参考实现的注释写的是「负的去掉、非负的保留（`0.216`）」。
       于是第一版照着写了。结果是**1232 行不一致**。

       对着真数据数了一遍（真文件的所有观测字段）：
           去零 3323 次 / 留零 **0** 次
           负去零 3129 次 / 负留零 **0** 次
       👉 **一律去零**，不分正负。

       然后**钟差那个字段又踩了同一脚**：修完观测值之后跑全量 48 对，
       还有 6 个站日 × 3 天 = 2880 行不一致 ——
           `0.000278099000`  vs  `.000278099000`
       原来**同一条规则也管钟差**（只是它 12 位小数、字段宽 15）。

    > 「读参考实现的注释」和「对着真数据数一遍」是两件事。
    > 前者的代价是 1232 行；**后者的教训是不该只在一个字段上数。**

    ⚠️ 从**缩放整数**直接格式化，不走浮点 ——
       走浮点的话 `* 1e-3` 会引入二进制舍入，第三位小数偶尔差 1。
       （这种错**不会让程序崩**，只会让结果差几毫米。）

       👉 所以钟差也**不**写作 `value / 1e12` —— 见 `_format_clock`。
    """
    negative = scaled < 0
    mag = abs(scaled)
    unit = 10 ** decimals
    whole, frac = divmod(mag, unit)
    sign = "-" if negative else ""
    # 整数部分是 0 → 那个零不写（负号照写）—— 见上面那段实测
    if whole == 0:
        body = f"{sign}.{frac:0{decimals}d}"
    else:
        body = f"{sign}{whole}.{frac:0{decimals}d}"
    return f"{body:>{width}}"


def _format_value(scaled: int) -> str:
    """一个观测值 → RINEX 3 的 `F14.3` 文本（值 14 列，LLI/SSI 另外两列）。"""
    return _format_scaled(scaled, 3, OBS_VALUE_WIDTH)


def _format_clock(scaled: int) -> str:
    """接收机钟差 → RINEX 3 的 `F15.12` 文本。

    ⚠️ 钟差按 **1e12** 缩放（对应 12 位小数），字段宽 **15**。
       和观测值**共用同一条去零规则** —— 这一条是跑全量 48 对才发现的
       （2880 行），见 `_format_scaled` 里那段。
    """
    return _format_scaled(scaled, 12, 15)


def _trim_end(s: str) -> str:
    return s.rstrip()


# =============================================================================
# 头部
# =============================================================================


# 观测类型行：`G   22 C1C L1C ...`（RINEX 3）
_OBS_TYPES = re.compile(r"^([A-Za-z0-9])\s*(\d+)\s+(.*)$")


def _parse_obs_counts(header_lines: list[str]) -> dict[str, int]:
    """从 RINEX 头部读出**每个系统有几种观测**。

    ⚠️ 这个数决定了每颗卫星那一行有几个观测值 —— **读错就全错**，
       而且是那种"每行都少/多一个字段"的错，一路看起来只是格式难看。

    ⚠️ `SYS / # / OBS TYPES` **一行放不下会换行**，续行的系统码位置是空的。
       踩过一次：只处理有系统码的行，**续行的观测码全丢了** ——
       于是计数偏小，卫星行被截断。
    """
    counts: dict[str, int] = {}
    for line in header_lines:
        if len(line) < 60 or line[60:].strip() != "SYS / # / OBS TYPES":
            continue
        sysid = line[0:1].strip()
        if not sysid:
            continue                      # 续行 —— 计数已经记过了
        m = _OBS_TYPES.match(line[:60].strip())
        if m:
            try:
                counts[m.group(1).upper()] = int(m.group(2))
            except ValueError:
                continue
    return counts


# =============================================================================
# 主入口
# =============================================================================


def decode(text: str) -> str:
    """CrinEX 文本 → 明文 RINEX 文本。

    ⚠️ **抛 `CrinexError` 而不是返回半截结果。**
       一个"解到一半"的文件比"解不开"危险得多 ——
       它会以正常的名字落盘，然后 Ginan 拿到一份截断的观测数据，
       **报的错和真正的原因离得很远**。
    """
    lines = text.splitlines()
    if not lines:
        raise CrinexError("文件是空的")

    first = lines[0]
    if "CRINEX VERS" not in first:
        raise CrinexError("第 1 行没有 CRINEX VERS / TYPE —— 这不是 CrinEX 文件")
    version = first[:20].strip()
    if not version.startswith("3."):
        raise CrinexError(
            f"这是 CRINEX {version}（压的是 RINEX 2），本程序只支持 CRINEX 3.0"
            "（压 RINEX 3 的那种）。老数据的 `.d` 文件需要另找工具转换。"
        )

    return _Decoder().run(lines)


def decode_header(text: str) -> str:
    """**只解头部** —— 明文 RINEX 的前几行。

    ⚠️ 什么时候需要它：`advise`（「这一堆能算什么」）要在一堆文件上
       读站名、起始时间、观测类型。而**完整解码一份 50 MB 的日观测要 7 秒**，
       48 份就是五分钟 —— 一个交互式工具不能这样。

       头部在 CrinEX 里本来就是**明文复制**的（只多两行自述行），
       所以这一步**不用碰数据段**，是常数时间。

    ⚠️ 但要说清楚它**给不出什么**：观测时长要靠数据段的历元算，
       这里拿不到。调用方别拿它当"完整信息"用。
    """
    lines = text.splitlines()
    if not lines:
        raise CrinexError("文件是空的")
    if "CRINEX VERS" not in lines[0]:
        raise CrinexError("第 1 行没有 CRINEX VERS / TYPE —— 这不是 CrinEX 文件")
    if not lines[0][:20].strip().startswith("3."):
        raise CrinexError(
            f"这是 CRINEX {lines[0][:20].strip()}（压的是 RINEX 2），"
            "本程序只支持 CRINEX 3.0"
        )
    out: list[str] = []
    for line in lines[2:]:                  # 前两行是自述行，不进输出
        out.append(line)
        if "END OF HEADER" in line:
            return "\n".join(out) + "\n"
    raise CrinexError("头部里没有 END OF HEADER —— 文件被截断了")


def convert_to_rnx(src, dst, compression: str = "") -> None:
    """**一步到位**：把一个（可能还套着 gzip / `.Z` 的）CrinEX 转成明文 RINEX。

    这是给 `inbox` 和 `fetch` 用的入口 —— 那两处都要做同一件事：
    剥外层压缩 → 解 CrinEX → 原子落盘。

    ⚠️ 参数 `compression` 是**外层容器**（`identify.FileIdentity.compression`），
       不是"内容是不是 CrinEX"（那个由调用方判断，见了才调这里）。

    ⚠️ 全程 **latin-1**：那是字节的双射，非 ASCII 字节原样往返。
       见 `decode_file` 里那段。
    """
    import gzip
    from pathlib import Path

    src, dst = Path(src), Path(dst)

    if compression == "gzip":
        with gzip.open(src, "rt", encoding="latin-1", newline="") as f:
            text = f.read()
    elif compression == "z":
        from . import lzw

        # ⚠️ `.Z` 没有流式文本接口 —— 先解到内存。老的 CrinEX 文件本来就小
        #    （`lzw.MAX_OUTPUT` 是 256 MB，够用）。
        text = lzw.unlzw(src.read_bytes()).decode("latin-1")
    elif compression:
        # `gzip?` / `z?`:压缩包本身是坏的。**不要往下走** ——
        # 硬解会得到一句"这不是 CrinEX"，把用户指向错误的方向。
        raise CrinexError(
            f"外层压缩包解不开（{compression}）—— 文件可能坏了或没传完"
        )
    else:
        text = src.read_text(encoding="latin-1")

    plain = decode(text)

    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="latin-1", newline="\n") as f:
            f.write(plain)
        tmp.replace(dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        raise


def decode_file(src, dst) -> None:
    """把一份 CrinEX 文件解成明文 RINEX 写到 `dst`。

    ⚠️ **先写 `.tmp` 再改名** —— 解到一半失败时**不会留下半个文件**
       冒充成完整的。那种文件 Ginan 会读到一半才报错，
       而报错和真正的原因离得很远。

       （这个仓库在 `_decompress_into` / `_gunzip` 两处都写着同一条 ——
        这里是第三处，理由一样。）

    ⚠️⚠️ 读写都走 **latin-1**，不走 utf-8：

       latin-1 是**字节的双射**（每个 0~255 的字节都有对应字符），
       所以任何非 ASCII 字节**原样往返** —— 不会被替换成 U+FFFD 丢掉。

       RINEX 头部的 COMMENT 行里**真的可能有非 ASCII**（观测员姓名、
       机构名），而那是**别人的数据**。用 utf-8 + errors 兜底的话，
       一个不合规的字节就会被**静默改掉**，然后落盘的文件和数据源不一样。

    ⚠️ 输出行尾是 **LF**，不跟随输入 —— 见本模块末尾那段说明。
    """
    import os
    from pathlib import Path

    src, dst = Path(src), Path(dst)
    with src.open("r", encoding="latin-1", newline="") as f:
        plain = decode(f.read())
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="latin-1", newline="\n") as f:
            f.write(plain)
        os.replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        raise


@dataclass
class _Decoder:
    """一次解码的全部状态。

    ⚠️ 引擎按**卫星**存，不按全局存 —— 每颗星有自己的弧段。
       存成全局的话，两颗星交替出现就会互相污染历史，
       而结果**仍然是一堆像模像样的坐标**。
    """

    epoch_diff: TextDiff = field(default_factory=TextDiff)
    clock_diff: NumDiff | None = None
    obs_diff: dict[str, list[NumDiff | None]] = field(default_factory=dict)
    flag_diff: dict[str, TextDiff] = field(default_factory=dict)
    obs_count: dict[str, int] = field(default_factory=dict)

    def run(self, lines: list[str]) -> str:
        out: list[str] = []

        # ---- 头部 ----
        # 第 1 行（CRINEX VERS）和第 2 行（CRINEX PROG / DATE）**都不进输出**，
        # 其余原样搬过去。
        i = 2
        header: list[str] = []
        saw_end = False
        while i < len(lines):
            line = lines[i]
            i += 1
            header.append(line)
            if "END OF HEADER" in line:
                saw_end = True
                break
        if not saw_end:
            raise CrinexError("头部里没有 END OF HEADER —— 文件被截断了")
        self.obs_count = _parse_obs_counts(header)
        out.extend(header)

        self._body(lines[i:], out)
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------ 数据段

    def _body(self, lines: list[str], out: list[str]) -> None:
        i = 0
        n = len(lines)
        while i < n:
            raw = lines[i]
            i += 1
            if not raw.strip():
                # ⚠️ 历元之间有一个空行（CrinEX 的写法），**不进输出**。
                #    它和"某个观测是空的"不是一回事。
                continue

            descriptor = (
                self.epoch_diff.force_init(raw) if raw.startswith(">")
                else self.epoch_diff.decompress(raw)
            )

            # RINEX 3 历元行：`> YYYY MM DD HH MM SS.sssssss  F NN<svlist>`
            #   第 31 列是历元标志，32..35 是卫星数，41 起是 3 字符的卫星号
            try:
                numsat = int(descriptor[32:35].strip())
            except ValueError as exc:
                raise CrinexError(
                    f"历元行里读不出卫星数：{descriptor[:60]!r}"
                ) from exc
            try:
                flag = int(descriptor[31:32].strip())
            except ValueError:
                flag = 0

            # 事件记录（flag > 1）：后面跟的是注释/头部行，**没有钟差**，
            # 而且**整段不做差分**。
            if flag > 1:
                out.append(_trim_end(descriptor[:35]))
                for _ in range(numsat):
                    if i < n:
                        out.append(_trim_end(lines[i]))
                        i += 1
                continue

            # ---- 钟差：单独一行 ----
            if i >= n:
                raise CrinexError("历元后面缺少钟差行 —— 文件被截断了")
            clock_text = self._decode_clock(lines[i])
            i += 1

            head = descriptor[:35]
            epoch_out = head
            if clock_text:
                # ⚠️ 钟差字段在**第 41 列**（35..41 是留给它的空位）。
                #    不补这几个空格的话，Ginan 解析出来的历元行是错位的。
                epoch_out = head + " " * max(0, 41 - len(head)) + clock_text
            out.append(_trim_end(epoch_out))

            svs = self._sv_tokens(descriptor, numsat)
            for sv in svs:
                if i >= n:
                    raise CrinexError(f"历元里说还有卫星 {sv}，但文件到这儿就没了")
                out.append(
                    _trim_end(self._decode_sat_line(sv, lines[i], self._count_for(sv)))
                )
                i += 1

    def _sv_tokens(self, descriptor: str, numsat: int) -> list[str]:
        lst = descriptor[41:]
        if len(lst) < numsat * 3:
            raise CrinexError(
                f"历元行里的卫星列表比说的 {numsat} 颗短：{descriptor[:80]!r}"
            )
        return [lst[k * 3: k * 3 + 3] for k in range(numsat)]

    def _count_for(self, sv: str) -> int:
        return self.obs_count.get(sv[:1].upper(), 0)

    def _decode_clock(self, line: str) -> str:
        token = line.strip()
        if not token:
            return ""
        self.clock_diff, value = _apply_token(self.clock_diff, token)
        # ⚠️ 走整数格式化，**不写 `value / 1e12`** —— 见 `_format_clock`。
        return _format_clock(value)

    # ---- 一颗卫星的一行 ----

    def _decode_sat_line(self, sv: str, line: str, n_obs: int) -> str:
        engines = self.obs_diff.setdefault(sv, [])
        if len(engines) < n_obs:
            engines.extend([None] * (n_obs - len(engines)))

        # ⚠️ `split(" ", n_obs)` —— **不是** `split()`。
        #
        #    这里是**热路径**（270 万个观测），所以不能按字符走。
        #    而 `split(" ")` 和默认的 `split()` 有个关键区别：
        #    **前者保留空串、后者把它们吃掉**。而 CrinEX 里
        #    "两个空格"正好表示"这一位没有观测" —— 吃掉就全错位了。
        #
        #    `maxsplit=n_obs` 让最后一个元素**自带**标志串，
        #    正好对应参考实现里"读完 n_obs 个记号，剩下的就是标志"。
        parts = line.split(" ", n_obs)

        values: list[int | None] = []
        append = values.append
        for k in range(n_obs):
            tok = parts[k] if k < len(parts) else ""
            if not tok:
                append(None)          # 这一位没有观测，**引擎不动**
                continue
            engines[k], v = _apply_token(engines[k], tok)
            append(v)

        # 剩下的就是 LLI/SSI 标志串
        flag_raw = parts[n_obs] if len(parts) > n_obs else ""
        flags = self.flag_diff.setdefault(sv, TextDiff()).decompress(flag_raw)

        return self._format_sat_line(sv, values, flags)

    @staticmethod
    def _format_sat_line(sv: str, values: list[int | None], flags: str) -> str:
        """一颗卫星的一行：`sv` + 每观测 16 列（值 14 + LLI + SSI）。

        ⚠️⚠️ **"值空着"和"标志空着"是两件独立的事**，不能一起填掉。

           实测（GMSD 的 R15/R11，全量 48 对里只有这两行）：
               值 = 空（这一位没有观测）
               但 LLI = `1`（**丢了锁**）

           真实场景就是"信号丢了锁、那个历元也拿不到观测" ——
           值列空着，LLI 列照写 `1`。

           ⚠️ 这个坑**参考实现也踩了**（它把 `value.is_none()` 时
              LLI/SSI 一起推空格）。所以它帮不了我们 ——
              **只有逐字节对着真文件比才看得出来**。

           > 又一次：「参考实现」不等于「真数据的标准」。
           > 前面钟差去零那一条也是这么发现的。

           ⚠️ 反过来说，无条件写标志是**安全**的：
              真的什么都没发生时，标志串那两位就是空格，写出来还是空格。
              所以这个修改**只可能在"有标志"时改变输出**，而那时它是对的。

        ⚠️ 热路径：`_format_scaled` 的调用**内联**在这里了
           （270 万次函数调用省下来是实打实的）。逻辑一模一样，
           见 `_format_scaled` 的说明 —— 两条规则：去前导零、整数格式化。
        """
        parts: list[str] = [sv]
        append = parts.append
        nflags = len(flags)
        for k, v in enumerate(values):
            j = k * 2
            lli = flags[j] if j < nflags else " "
            ssi = flags[j + 1] if j + 1 < nflags else " "
            if v is None:
                append(" " * OBS_VALUE_WIDTH + lli + ssi)
                continue
            if v < 0:
                whole, frac = divmod(-v, 1000)
                body = f"-.{frac:03d}" if whole == 0 else f"-{whole}.{frac:03d}"
            else:
                whole, frac = divmod(v, 1000)
                body = f".{frac:03d}" if whole == 0 else f"{whole}.{frac:03d}"
            append(f"{body:>14}" + lli + ssi)
        return "".join(parts)
