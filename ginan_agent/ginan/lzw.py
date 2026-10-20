"""Ginan 领域层 —— 解压 Unix `compress` 格式（`.Z` 结尾的文件）。

============================ 为什么需要自己写一个 ============================
IGS 的老产品（约 GPS 周 2237 之前）在 CDDIS 上是 **`.Z` 格式** ——
Unix `compress` 的 LZW 压缩。

而 **Python 标准库没有 LZW**：`gzip`/`zlib`/`bz2`/`lzma` 都不认 `0x1f 0x9d`。

上游 Ginan 界面（GinanUI）用的是第三方包 `unlzw3`。我们**不引这个依赖**，
理由和"下载器不依赖 wget"是同一条：

    用户双击 `run.bat` 时，每多一个要 pip 装的包，
    就多一次"装不上 → 整个程序起不来"的机会。

而 `compress` 的 LZW 是个**封闭、有定论**的算法（1984 年定死，之后再没变过），
实现它大约一百行，而且**可以用真实文件验证** —— 不是"看起来对"。

============================ 这个算法的几个关键点 ============================
    · 头部 3 字节：`1f 9d` + flags
      flags 的 0x80 位 = 块模式；低 5 位 = maxbits（通常是 16）
    · 码宽从 **9 位**起，表满就 +1，最大到 maxbits
    · **码是 LSB 先行的连续比特流**，码宽变化时**不重新对齐**
      （这一点和 GIF 的 LZW 不同 —— 那边是 MSB 先行，还有 early change）
    · 块模式下 **256 是"清表"码**，第一个可用码是 257
    · ⚠️ **没有 early change**：码宽在"下一个空闲码 >= (1 << 码宽)"时才增长
    · 表填到 `1 << maxbits` 就**冻结** —— 不再加新词条，但照常继续解码

============================ ⚠️ 截断的输入是**正常**的 ============================
识别文件类型时只需要看**开头一点内容**（见 identify.py 的嗅探）。
所以 `unlzw()` 允许"数据没读完就没了"，这时**返回已经解出来的部分**，
不抛异常。

    这个设计不是偷懒：如果截断就报错，嗅探那条路就得先判断长度、
    再决定要不要 try —— 而嗅探本身是个"尽量别抛异常"的地方。
    **把"数据不够"变成正常返回值，比让每个调用方都兜一遍干净得多。**
"""

from __future__ import annotations

from pathlib import Path

# `compress` 的魔数。⚠️ 和 gzip 的 `1f 8b` 只差一个字节 ——
# 所以嗅探里两个都要判，而且顺序无所谓（第二字节不同）。
MAGIC = b"\x1f\x9d"

# 起始码宽。`compress` 固定从 9 位开始，这不是可配置项。
INIT_BITS = 9

# 块模式下的保留码
CLEAR = 256

# 解整个文件时的上限。
#
# ⚠️ 为什么要设：`unlzw()` 是**一把梭**解到内存里的（不做流式）。
#    产品文件通常几十 KB 到几 MB，解完没问题；
#    但如果哪天喂进来一个几百 MB 的 `.Z`，内存会直接吃掉。
#
#    设了上限之后，失败方式是**一句明确的报错**，而不是 OOM ——
#    而 OOM 在用户看来是"程序莫名其妙没了"。
MAX_OUTPUT = 256 * 1024 * 1024


class LzwError(ValueError):
    """`.Z` 数据坏了（不是截断）。"""


def _read_codes(data: bytes, start: int, maxbits: int, block_mode: bool,
                max_out: int | None):
    """把比特流解成码序列，边解边吐出字节。

    做成生成器是为了让"截断"能优雅收场：数据没了就 return，
    已经 yield 出去的字节仍然有效。
    """
    pos = start
    acc = 0          # 比特缓冲
    nbits = 0        # 缓冲里还有多少位

    def read(width: int):
        """取 width 位。数据不够返回 None。"""
        nonlocal pos, acc, nbits
        while nbits < width:
            if pos >= len(data):
                return None
            acc |= data[pos] << nbits
            pos += 1
            nbits += 8
        value = acc & ((1 << width) - 1)
        acc >>= width
        nbits -= width
        return value

    max_code = (1 << INIT_BITS) - 1
    max_max_code = 1 << maxbits
    width = INIT_BITS
    free_ent = (CLEAR + 1) if block_mode else CLEAR

    # 词条表。用 list 按下标存，比 dict 快也简单。
    table: list[bytes] = [bytes([i]) for i in range(256)]
    table += [b""] * (max_max_code - 256)

    prev: int | None = None
    produced = 0

    while True:
        # ⚠️ 增长判据：**下一个空闲码 >= (1 << 码宽)**。
        #    这就是"没有 early change"的含义 ——
        #    写成 `free_ent > max_code` 也对（等价），但写法要说清意图。
        if free_ent >= (1 << width) and width < maxbits:
            width += 1

        code = read(width)
        if code is None:
            return                      # 数据用完了（截断）—— 正常收场

        if block_mode and code == CLEAR:
            width = INIT_BITS
            free_ent = CLEAR + 1
            prev = None
            continue

        if prev is None:
            # 清表之后（或文件开头）的第一个码必须是字面量。
            if code >= 256:
                raise LzwError(f"清表之后的第一个码是 {code}，不是字面量")
            emit = table[code]
        elif code < free_ent:
            emit = table[code]
        elif code == free_ent:
            # KwKwK 情形：码正好指向"马上要加的那条"。
            emit = table[prev] + table[prev][:1]
        else:
            # 码指向还没定义的词条 —— 数据坏了。
            # ⚠️ 但**截断**也可能走到这里（最后几个码被切掉）。
            #    区分不了，就当截断处理 —— 宁可少报错，也不要因为
            #    最后的半个码把整个文件判成坏的。
            return

        yield emit
        produced += len(emit)
        if max_out is not None and produced >= max_out:
            return

        if prev is not None and free_ent < max_max_code:
            table[free_ent] = table[prev] + emit[:1]
            free_ent += 1

        prev = code


def unlzw(data: bytes, max_out: int | None = None) -> bytes:
    """解开 `.Z` 数据。**截断的输入返回已解出的部分，不抛异常。**

    max_out 是**软上限** —— 每解出一个词条检查一次，所以可能超一点点
    （一个词条最长可以是几百字节）。嗅探时给几 KB 够用，
    它要的是"开头那点内容"，不是精确的字节数。

    ⚠️ 别把它当精确截断接口用。
    """
    if len(data) < 3 or not data.startswith(MAGIC):
        raise LzwError("不是 .Z 数据（开头不是 1f 9d）")

    flags = data[2]
    maxbits = flags & 0x1F
    block_mode = bool(flags & 0x80)

    if not (INIT_BITS <= maxbits <= 16):
        raise LzwError(f"maxbits={maxbits} 不合理（应该在 9~16 之间）")

    out = bytearray()
    for chunk in _read_codes(data, 3, maxbits, block_mode, max_out):
        out += chunk
        if max_out is not None and len(out) >= max_out:
            break
    return bytes(out)


def unlzw_stream(src: Path, dst: Path, max_output: int = MAX_OUTPUT) -> int:
    """把 `.Z` 文件解压到 dst，返回解出多少字节。

    ⚠️ 先写 `.tmp` 再改名 —— 解压中途失败时不会留下半个文件
       冒充成完整的（那种文件 Ginan 会读到一半才报错）。
       这和 `fetch._gunzip` 是同一套做法，理由也一样。

    ⚠️ 上限是**先检查再解**：超过 max_output 直接报错，
       而不是解完了再发现太大。
    """
    data = src.read_bytes()

    # 压缩比通常在 2~5 倍，用 5 倍粗估一下，超了就早退
    if len(data) * 5 > max_output * 2:
        raise LzwError(
            f"{src.name} 解压后可能超过 {max_output // (1024 * 1024)} MB —— "
            "这个格式的整文件解码是一把梭进内存的，先不做了"
        )

    plain = unlzw(data, max_out=max_output)
    if len(plain) >= max_output:
        raise LzwError(f"{src.name} 解压后超过 {max_output // (1024 * 1024)} MB")

    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(plain)
    tmp.replace(dst)
    return len(plain)
