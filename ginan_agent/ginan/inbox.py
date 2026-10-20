"""Ginan 领域层 —— 收件箱：用户手动提供文件的第四条路。

============================ 这条路为什么必须有 ============================
前三取数路径都依赖网络（自动下载 / 代理 / 缓存）。
**但有些网络环境下，再怎么努力也下不到。**

比如：
  · 用户的网络到哪个 IGS 数据中心都不通，又不能用代理
  · 数据在另一台机器上，只能用 U 盘拷过来
  · 单位有内网镜像，但地址不公开
  · CDDIS 要账号，用户不想注册

这时候唯一的出路是：**用户自己想办法把文件弄到手**，
然后告诉程序"文件在这儿"。

所以这是**最后一道保险丝** —— 任何网络环境下都必须走得通。

============================ 它做五件事 ============================
    ① 扫描 —— 递归找出目录里所有文件（不管名字；**深度有上限，见 MAX_DEPTH**）
    ② 识别 —— 靠**内容**判断是什么（见 identify.py）
    ③ 命名 —— 按内容给它一个**对的名字**
    ④ 复制 —— 到数据目录放一份能用的（**原件留在收件箱**）
    ⑤ 清空 —— "新任务"时把收件箱**整个清空**（见 `clear_inbox`）

⚠️ ① 原来写的是"**不管嵌套几层**"。**那句话是假的**，而且它一直假着 ——
   实测：往下 4 层内的文件看得见，**第 5 层起完全看不见**，
   而用户**一个字都不会被告知**。

   实测代价（用户 2026-09-20 丢进来的一个文件夹，194 个文件）：
       `scan()` 只看见 21 个，**173 个（89%）静默消失**。
   边界还是"多套一层文件夹"这种毫无道理的东西：
       `met/2026/244/`（4 层）活着，`obs/rinex2/2026/244/`（5 层）全没。

   > 一句写在函数正上方的假注释比没有注释更糟 —— 读代码的人会信它。
   > 这和 `MAX_DEPTH` 那个数字现在一起改掉了，而且**加了守卫**。

⚠️ ④ 原来是"搬运"（`shutil.move`，原件就没了），现在改成"复制"。
   理由见 `claim()` 的说明 —— 一句话：**原始数据往往不可再得**。
   配合 ⑤ 才有意义：原件一直留着，直到用户主动"新任务"。

============================ ⚠️ 为什么名字要重算 ============================
用户从浏览器下载会得到 `igs20(1).atx`、
从网盘同步可能是 `新建文件夹/abc123`。

而 Ginan 的配置里**写的是精确文件名**（`atx_files: [igs20.atx]`）——
名字不对，配置就找不到文件。

所以搬运时不能原样保留名字，要**按内容重新命名**。
这是这条路能不能真正走通的最后一环。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import identify, paths
from .identify import FileIdentity

# 扫描时最多往下走几层。
#
# ⚠️ 设上限是因为用户可能把整个下载文件夹丢进来，
#    里面有几十层嵌套和几万个文件。
#    走太深会卡住，而且真正有用的文件通常就在浅层。
#
# ============================ ⚠️ 为什么从 4 改成 8 ============================
# 原来写 4。**4 太浅了** —— 浅到连用户自己整理过的文件夹都走不进去。
#
# 实测（用户 2026-09-20 丢进来的那个文件夹）：
#     收件箱/data/met/2026/244/…          → 4 层，看得见
#     收件箱/data/obs/rinex2/2026/244/…   → 5 层，**全丢**
#
# 边界是"多套了一层分类文件夹"这种毫无道理的东西 —— 而这一箱里
# **173/194（89%）** 的文件都在第 5 层，也就是**一个都没被看见**。
#
# 真实的镜像布局（CDDIS / IGS 那种 `年/年积日` 两层）本来就在 4-6 层，
# 8 层能覆盖住，而且照样截得住下面那个环路问题。
#
# ⚠️ 但**不能直接取消上限** —— `_walk` 没有环路检测。
#    实测拿一个 junction 环路压过：把上限拉到 1000 时，
#    同一个 `leaf.txt` 被扫出 **64 次**（路径长到 588 字符才被 OSError 截住）。
#    上限是顺带在挡这个的，它是**有工作在做**的，只是 4 太小。
MAX_DEPTH = 8

# 单个文件最大 500 MB —— 超过的多半不是我们要的东西
MAX_SIZE = 500 * 1024 * 1024

# ⚠️⚠️ 指纹要**行尾归一化**。见 `_fingerprint` 里那段 ——
#    一句话：`\r\n` 和 `\n` 是**产物**的差别（Unix 工具出 LF、
#    Windows 工具出 CRLF），不是**数据**的差别。
#    不归一化的话，同一份观测数据的两种行尾会被判成两份，
#    于是"认领过了"又说没认领 —— 就是 `_digest` 要修的那个 bug 的翻版。

# 内容指纹取"前多少字节"。见 `_digest()`。
#
# ⚠️ 1 MB 是个折中：够把"两个不同文件"区分开（RINEX 头部 + 头几个历元
#    就足以区分不同站、不同天），又不至于为了判个重复去读 GB 级文件。
HASH_BYTES = 1024 * 1024

# Unix compress 的魔数，和 `lzw.MAGIC` 是同一个东西。
# ⚠️ 这里刻意**再写一遍字面量**而不是 `from . import lzw` 取 ——
#    `_digest` 是每次扫描要跑几百次的路径，而 `lzw` 只在真的遇到 `.Z`
#    时才需要（那时才 import）。见 `_digest_lzw()`。
_LZW_MAGIC = b"\x1f\x9d"


@dataclass
class InboxItem:
    """收件箱里的一个文件。"""

    path: Path
    identity: FileIdentity
    size: int

    @property
    def known(self) -> bool:
        """这个文件能不能被认领。

        ⚠️ 用 `is_data` 而不是 `known` —— 区别见 identify.FileIdentity。

           "认出来是网页"也是认出来了，但网页不是数据文件，
           不该被搬进数据目录。**搬进去之后 Ginan 会报一个无关的错。**
        """
        return self.identity.is_data

    @property
    def size_text(self) -> str:
        mb = self.size / (1024 * 1024)
        if mb >= 1:
            return f"{mb:.1f} MB"
        # ⚠️ 小店里的文件不能说成 "0 KB" —— 实测一份 212 字节的 ERP
        #    显示的是 "0 KB"，用户会以为文件是空的。
        if self.size >= 1024:
            return f"{self.size / 1024:.0f} KB"
        return f"{self.size} 字节"


@dataclass
class Move:
    """一次搬运的记录。

    ⚠️ 每一条都要能被用户看到 —— 我们动了人家磁盘上的文件。
    """

    src: Path
    dst: Path
    kind: str
    renamed: bool = False
    skipped: bool = False
    reason: str = ""

    # ⚠️ 原件是否还留在收件箱里。
    #
    #    这是**给用户看的**一句话 —— 我们动了他磁盘上的东西，
    #    他有权知道原件还在不在。以前的语义是"搬走"（原件没了），
    #    现在是"复制"（原件还在），不说清楚就等于偷偷改了行为。
    kept_in_inbox: bool = True

    @property
    def summary(self) -> str:
        if self.skipped:
            return f"{self.src.name} —— 跳过：{self.reason}"
        kept = "（原件仍留在收件箱）"
        if self.renamed:
            return f"{self.src.name} → {self.dst.name}（按内容改名）{kept}"
        return f"{self.src.name} → 数据目录{kept}"


@dataclass
class ClaimResult:
    """一次认领的结果。"""

    scanned: int = 0
    moves: list[Move] = field(default_factory=list)
    unknown: list[InboxItem] = field(default_factory=list)
    too_big: list[InboxItem] = field(default_factory=list)

    # ⚠️ 这里**原来有一个 `unusable` 字段**（"认得出、是数据、但用不了"，
    #    当时唯一的成员是 CrinEX）。**现在删了** ——
    #    `ginan/crinex.py` 能解 CrinEX 之后，这一类**一个成员都没有了**。
    #
    #    转换失败的文件走 `skipped`（那条记录带 reason，比一个光秃秃的
    #    分类更有用）。
    #
    #    ⚠️ 这个仓库对死字段有明确的立场：**「后端从不返回某个键，
    #       调用方一直在读」**吃过不止一次亏（`too_big` 当初就是）。
    #       所以字段没了成员就该删掉，而不是"留着将来可能用得上"。
    #       将来真有需要，从这里重新长出来就行 —— 上面这段话就是留给它的位置。

    # ⚠️ 深度截断的说明（来自 `ScanResult.depth_note`），没有截断时是空串。
    #
    #    放在这里而不是"再传一个 ScanResult 进来"，是因为调用方
    #    （工具、界面）只需要**一句能直接给用户看的话**。
    depth_note: str = ""

    @property
    def moved(self) -> list[Move]:
        return [m for m in self.moves if not m.skipped]

    @property
    def skipped(self) -> list[Move]:
        return [m for m in self.moves if m.skipped]

    @property
    def summary(self) -> str:
        if not self.scanned:
            return "收件箱是空的"
        parts = [f"看了 {self.scanned} 个文件"]
        if self.moved:
            parts.append(f"认领了 {len(self.moved)} 个")
        if self.unknown:
            parts.append(f"认不出 {len(self.unknown)} 个")
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 个")
        # ⚠️⚠️ 太大的文件原来**完全不说** —— `too_big` 那个列表
        #    「只有定义和一处 append，零个读取者」。
        #    于是用户看到"看了 3 个文件、认领了 2 个"，第三个不翼而飞。
        #    **静默漏掉一个文件，比报错糟得多** —— 他不知道自己少了东西。
        if self.too_big:
            parts.append(f"{len(self.too_big)} 个太大没搬")
        return "，".join(parts)


# =============================================================================
# ① 扫描
# =============================================================================


@dataclass
class ScanResult:
    """一次扫描的**全部**结果 —— 包括"没看到的东西"。

    ⚠️⚠️ `too_deep_files` 这个字段的存在理由，就是这次修复的核心：

       一次扫描有两种"看不到"：
         ① 我们看了、判断它不该算（空文件、隐藏文件）—— **有意的**
         ② 我们**压根没走到那儿**（太深）—— **代价**

       ① 不需要说，② **必须说**。原来的接口只有"文件列表"，
       表达不了②，于是它就变成了静默丢失。

       > 一个返回值只能表达"我找到了什么"的时候，
       > 它就永远说不出"我还漏了什么"。
    """

    items: list[InboxItem] = field(default_factory=list)

    # 因为太深而没进去的目录，和里面**一共多少个文件**
    too_deep_dirs: list[Path] = field(default_factory=list)
    too_deep_files: int = 0

    @property
    def truncated(self) -> bool:
        return bool(self.too_deep_dirs)

    @property
    def depth_note(self) -> str:
        """给用户看的一句话。没有截断时返回空串。

        ⚠️ 要说清**三件事**，少一件用户就不知道该干什么：
           有多少个文件没看、为什么、怎么办。
           「有东西没看到」而不说怎么办，等于把问题丢给他。
        """
        if not self.too_deep_dirs:
            return ""
        return (
            f"⚠️ 有 {self.too_deep_files} 个文件在超过 {MAX_DEPTH} 层的子目录里，"
            f"这次**没有看**（{len(self.too_deep_dirs)} 个目录）。"
            f"把它们挪到浅一点的地方，或者直接放到收件箱根目录下，再扫一次。"
        )


def scan_detailed(root: Path | None = None) -> ScanResult:
    """扫描收件箱 —— 和 `scan()` 一样，但**把没走到的地方也报出来**。

    ⚠️ 需要知道"有没有漏掉东西"的调用方（`claim()`、界面）用这个。
       只想要文件列表的（列本地数据、`already_claimed()`）继续用 `scan()`。

    ⚠️ 不抛异常。收件箱是用户随手丢东西的地方，
       里面有什么都不奇怪 —— 一个读不了的文件不该让整次扫描失败。
    """
    root = root or paths.inbox_dir()
    result = ScanResult()
    if not root.is_dir():
        return result

    found, too_deep = _walk(root)
    result.too_deep_dirs = too_deep
    result.too_deep_files = _count_files_under(too_deep)

    for p in found:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        if size > MAX_SIZE:
            result.items.append(
                InboxItem(p, FileIdentity(evidence="文件太大"), size)
            )
            continue
        result.items.append(InboxItem(p, identify.identify(p), size))

    return result


def scan(root: Path | None = None) -> list[InboxItem]:
    """扫描收件箱里的所有文件。

    ⚠️ 递归找，但**跳过隐藏文件和临时文件**：
       `.DS_Store`、`Thumbs.db`、`~$xxx.docx`、
       以及我们自己的 `.part`（下载未完成的东西）。

    ⚠️ 这是 `scan_detailed()` 的薄包装，**保留是因为它有 5 个调用方**
       （`ginan_tools.py` 两处、`server/app.py` 一处、`claim()`、
       `already_claimed()`）。只是这些调用方**拿不到"漏了什么"** ——
       需要那个信息的（`claim()`）改用 `scan_detailed()`。

    ⚠️ 不抛异常。收件箱是用户随手丢东西的地方，
       里面有什么都不奇怪 —— 一个读不了的文件不该让整次扫描失败。
    """
    return scan_detailed(root).items


def _walk(root: Path) -> tuple[list[Path], list[Path]]:
    """递归遍历，控制深度并跳过无意义的文件。

    返回 `(找到的文件, 因为太深而**没进去**的目录)`。

    ⚠️⚠️ 第二个返回值是**新加的，而且是这一次修复的重点**。

       原来的 `_walk` 是个生成器，遇到超过 `MAX_DEPTH` 的目录就
       `continue` —— **一声不响地走开**。调用方拿到的只是一个
       "看起来完整"的列表，而少掉的那部分**没有任何地方能知道**。

       👉 于是用户看到的是「看了 21 个文件，认领了 12 个，认不出 9 个」，
          而那 21 个后面的 **173 个文件连一个字的交代都没有**。

       ⚠️ 这和 `too_big` 是**同一个形状**（见 `ClaimResult.summary` 里那段）。
          「**静默漏掉一个文件，比报错糟得多**」——
          那句话当时只照着做在 `too_big` 上，深度这条路一个读取者都没有。

       > 一个"跳过某处"的分支，要么把跳过这件事**报出去**，
       > 要么就别跳过。**没有第三种正确的写法。**
    """
    files: list[Path] = []
    too_deep: list[Path] = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            name = p.name
            # 跳过隐藏文件 / 系统文件 / 未下完的
            if name.startswith(".") or name.startswith("~$"):
                continue
            if name in ("Thumbs.db", "desktop.ini"):
                continue
            if name.endswith((".part", ".tmp", ".crdownload")):
                continue

            try:
                if p.is_dir():
                    if depth < MAX_DEPTH:
                        stack.append((p, depth + 1))
                    else:
                        # 记下来 —— 这里面的东西我们**没看过**
                        too_deep.append(p)
                    continue
                if p.is_file():
                    files.append(p)
            except OSError:
                continue
    return files, too_deep


def _count_files_under(dirs: list[Path]) -> int:
    """数出这些目录里一共有多少个文件（**不算**识别，只数）。

    ⚠️ 为什么要专门数一遍：报"有 3 个目录太深"对用户没用 ——
       他不知道那 3 个目录里是 3 个文件还是 30000 个。
       要报就报**他会丢掉多少个文件**。

    ⚠️ 这里**不再有深度上限** —— 我们要的就是"到底有多少"。
       代价可控：只是 `iterdir`，没有 `stat`、没有 `identify`
       （识别才是贵的那个，它要读文件头甚至解压）。
       而 `too_deep` 本身是空的（正常情况），所以这个函数通常一行都不跑。

    ⚠️ 同样跳过隐藏/临时文件 —— 不然报出来的数字会虚高，
       用户按这个数字去核对会**对不上**，那比不报还糟。
    """
    total = 0
    stack = list(dirs)
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            name = p.name
            if name.startswith(".") or name.startswith("~$"):
                continue
            if name in ("Thumbs.db", "desktop.ini"):
                continue
            if name.endswith((".part", ".tmp", ".crdownload")):
                continue
            try:
                if p.is_dir():
                    stack.append(p)
                elif p.is_file():
                    total += 1
            except OSError:
                continue
    return total


# =============================================================================
# ③ 命名
# =============================================================================


# 浏览器下载会加的东西：`文件(1).atx`、`文件 - 副本.atx`
_BROWSER_JUNK = re.compile(r"\s*[\(\（]\d+[\)\）]|\s*-\s*副本$")


# 压缩后缀。⚠️ 只认**我们能解开**的两种：
#
#     .gz   gzip      —— IGS 分发 RINEX 3 的标准形式
#     .Z    compress  —— 老数据（IGS 周 2237 之前）
#
# ⚠️ **`.crx`（Hatanaka）不在里面**，虽然它也常被当成"压缩"。
#    它不是压缩，是**另一种编码**，解开要 RNXCMP 那套。
#    把它混进来会让"认领后能直接用"变成假话 —— 我们解不开它。
_COMPRESSION_SUFFIXES = (".gz", ".z")


def _decompress_into(src: Path, dst: Path, compression: str) -> None:
    """把压缩文件解到 `dst`。

    ⚠️ 先写 `.tmp` 再改名：解压中途失败时**不会留下半个文件**
       冒充成完整的（那种文件 Ginan 会读到一半才报错）。
       和 `fetch._gunzip` 同一个理由。

    ⚠️ 失败的异常**不在这里吞** —— 由调用方翻译成给用户看的话。
       这里只管解压这件事本身。
    """
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        if compression == "gzip":
            import gzip

            with gzip.open(src, "rb") as fin, tmp.open("wb") as fout:
                shutil.copyfileobj(fin, fout, 1024 * 1024)
        elif compression == "z":
            from . import lzw

            lzw.unlzw_stream(src, tmp)
        else:
            raise ValueError(f"不认识的压缩格式：{compression!r}")
        tmp.replace(dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        raise


def _strip_compression_suffix(name: str) -> str:
    """去掉末尾的压缩后缀。`igr21992.erp.Z` → `igr21992.erp`。

    ⚠️ 大小写不敏感 —— `.Z` 是大写（Unix compress 的传统写法），
       而 `.gz` 是小写，两种都真实存在。
    """
    low = name.lower()
    for suf in _COMPRESSION_SUFFIXES:
        if low.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


def decide_name(item: InboxItem, existing: set[str]) -> tuple[str, bool]:
    """给一个文件定最终名字。返回 (名字, 是否改了名)。

    ⚠️ 优先用**内容里的权威信息**，其次才是文件名：

      ① RINEX 观测数据 → 用文件头里的 MARKER NAME
         （这是唯一可靠的来源。GinanUI 从文件名截前 4 个字符，
           遇到非标准命名会静默得到错误结果。**那是 bug。**）

      ② 其他类型 → 用原文件名，但**去掉浏览器加的垃圾**
         （`igs20(1).atx` → `igs20.atx`）。
         因为 Ginan 的配置里写的是精确文件名，多一对括号就找不到。

      ③ 名字还是可疑（没有扩展名 / 全乱码） → 按类型给个兜底名

    ⚠️⚠️ **第二个返回值只有一个判据：落盘的名字 ≠ 收件箱里那个名字。**
        每个分支**只决定名字**，改没改由函数末尾那一句统一判。
        见下面那段长注释 —— 这里原来每个分支各判一次，其中两支判错了。
    """
    orig = item.path.name
    kind = item.identity.kind

    # ⚠️⚠️ 算名字之前先把**压缩后缀**去掉。
    #
    #    因为落到数据目录里的是**解压之后**的内容（见 `_claim_one`）——
    #    名字必须描述那一份，不能描述收件箱里那一份。
    #
    #    实测（没去后缀时）：
    #        igr21992.erp.Z   →  igr21992.erp.erp     ✗ 多了一截
    #        xxx.rnx.gz       →  靠分支①的站名救回来了（那个分支不看原名）
    #
    #    ⚠️ 而且它比"名字难看"严重：`decide_name` 的后半段在判
    #       "扩展名和内容对不对得上"，带着 `.Z` 去判会**永远判成对不上**，
    #       于是每次都走"补扩展名"那条路 —— 补出来的就是那个双后缀。
    base_name = _strip_compression_suffix(orig)
    compressed = base_name != orig

    # ---- 分支只决定 `base`（想要的名字），一律**不返回** ----
    base: str | None = None

    # ---- ① RINEX：用文件头里的测站名 ----
    if kind == "obs" and item.identity.marker:
        # marker 形如 `ALIC00AUS`，取前 4 位是站号
        base = f"{item.identity.marker[:4].upper()}.rnx"

    elif kind == "nav":
        nav = _BROWSER_JUNK.sub("", base_name)
        if not nav.lower().endswith((".rnx", ".nav")):
            nav = Path(nav).stem + ".rnx"
        base = nav

    else:
        # ---- ② 其他：清掉浏览器垃圾 ----
        cleaned = _BROWSER_JUNK.sub("", base_name).strip()
        if cleaned and cleaned != base_name:
            base = cleaned               # 去掉了浏览器后缀
        # ⚠️ 只是去掉了压缩后缀的话，名字**也变了** —— 要说出来。
        #    不然调用方会以为"名字没动过"，而落盘的名字和收件箱里那个
        #    对不上时，用户会找不到自己的文件去哪了。
        elif compressed:
            base = base_name

        # ---- ③ 名字不可信时的兜底 ----
        #
        # ⚠️ 什么算"不可信"？
        #    没有扩展名（`abc123`）、或者扩展名和内容对不上
        #    （内容是 SP3，名字却是 `.txt`）。
        else:
            stem = Path(orig).stem
            ext = Path(orig).suffix.lower()

            expected_ext = {
                "sp3": ".sp3", "clk": ".clk", "erp": ".erp",
                "atx": ".atx", "obs": ".rnx", "nav": ".rnx", "sinex": ".snx",
            }.get(kind, "")

            if not ext or (expected_ext and ext != expected_ext
                           and not orig.lower().endswith(expected_ext)):
                base = f"{stem}{expected_ext}"     # 补上扩展名

    if base is None:
        return orig, False

    name = _dedupe(base, existing)
    # ⚠️ 「改过名」的判据**只有这一条**：落盘的名字和收件箱里那个不一样。
    return name, name != orig


# ⚠️⚠️ 这个函数**原来有四个 return，每个各判一次"改没改名"** —— 而其中两支判错了。
#
#    错的那两支（obs / nav）直接 `return _dedupe(base, existing)`：
#    `_dedupe` 的第二个元素是"**重名了、加了序号**"，
#    和"改过名了"**是两件事**。名字换了但没重名时它返回 False。
#
#    实测（真实的一天 55 个观测文件，IGS 标准长名）：
#        55/55 全部被改名（`ALBH00XXX_R_20251660000_01D_30S_MO.rnx` → `ALBH.rnx`）
#        **而 report 里 renamed 全是 False** —— 报出来的是
#          「ALBH00XXX_R_…_MO.rnx → 数据目录」，名字一个字都没提。
#
#    ⚠️ 后果不是"少了一行提示"，是**用户找不到自己的文件**：
#       他照报告去数据目录里找 `ALBH00XXX_…_MO.rnx`，那个名字根本不存在。
#       而 `Move` 那段注释写着"我们动了人家磁盘上的文件，他有权知道"。
#
#    > 两个布尔值长得一样、含义不一样，**隔着一次 return 就看不出来了**。
#    > 所以现在：`_dedupe` 只返回名字，`decide_name` 只在一处判 `name != orig`。
#    > 想让第二个返回值出错，得先把名字改错 —— 那已经被别的守卫盯着了。
#
#    ---- 更早的那次（形状错，不是含义错）----
#    写的时候在**两处**犯了同一个错：`return _dedupe(...), True` ——
#    _dedupe 本身已经返回二元组了，再套一层得到 ((名字, 布尔), True)，
#    调用方 unpack 出来的是个元组。
#
#    症状：TypeError: unsupported operand type(s) for /: 'WindowsPath' and 'tuple'
#    报错位置在**调用方**（inbox.claim 的 dest_dir / name），
#    而真正的错在**这里** —— 隔着几百行。
#
#    > 一个函数有多个 return 时，它们返回的形状**必须完全一致**。
#    > 抽个小的构造助手（比如 make_result(name, renamed)）能避免这类问题，
#    > 但更重要的是：**每加一个 return，就回头看看其他的长什么样。**


def _dedupe(name: str, existing: set[str]) -> str:
    """重名时加序号。**只返回名字。**

    ⚠️ 但**不覆盖**已有文件 —— 那是用户的数据。
       加个 `_2` 后缀，并在报告里说清楚。

    ⚠️⚠️ 它**原来还返回一个布尔**，含义是"加了序号"。而 `decide_name`
       要的布尔是"改过名" —— 两个含义被同一个名字 `renamed` 串了起来，
       于是 obs / nav 那两支把 False 当成了"没改名"上报给用户。
       见 `decide_name` 下面那段。**一个函数产出两个同名的布尔，
       迟早有人接错那根线** —— 所以这里只返回名字。
    """
    if name not in existing:
        return name
    p = Path(name)
    for i in range(2, 100):
        cand = f"{p.stem}_{i}{p.suffix}"
        if cand not in existing:
            return cand
    return name


# =============================================================================
# ④ 搬运
# =============================================================================


def claim(
    dest_dir: Path | None = None,
    root: Path | None = None,
    dry_run: bool = False,
) -> ClaimResult:
    """把收件箱里认得出来的文件**复制**一份到数据目录。

    参数 dry_run=True 时只报告会做什么，**不动任何文件** ——
    界面可以先给用户看一眼再确认。

    ⚠️ 停在收件箱里不搬的，有**三类**，每一类都要能说出来
       （`ClaimResult` 的 `too_big` / `unknown` / `skipped`，
        外加 `depth_note` 那句"还有几个文件在太深的目录里没看"）：

         · 太大      —— `too_big`
         · 认不出    —— `unknown`（可能根本不是数据）
         · 已有/出错 —— `skipped`
                       （⚠️ CrinEX 解码失败也走这里——那条记录带 reason，
                        比一个光秃秃的分类更有用）

       ⚠️ **每一类都不能静默**。这个仓库为"漏报一类"吃过两次亏：
          `too_big` 当初是"算出来了但没人读"，深度截断当初是"根本没记"。
          **用户不知道就等于丢东西。**

    ⚠️ **压缩的、CrinEX 的，落盘的都是解开的明文** ——
       `.gz` 解压、`.crx` **解码**（`ginan/crinex.py`）。落到数据目录里的
       名字是 `.rnx`，而内容**真的**是 RINEX。

    ============================ ⚠️ 为什么是复制不是移动 ============================
    原来这里是 `shutil.move`，文档字符串还特意写着"**移动**而不是复制
    （用户明确要的是"搬过去"）"。**现在改掉了**，理由是用户的语义澄清：

        收件箱 = 用户的投放区，**原件留在这儿**
        数据目录 = 程序的缓存，认领后在那里放一份能用的

    这个区分解决一个真实的痛点：认领之后，用户**再也找不到自己那份原始数据了**。
    而原始数据往往是整个流程里**唯一不可再得**的东西 ——
    别人的产品文件都能重下，他自己测的东西不能。

    ⚠️ 代价是多占一份磁盘空间，以及**两份可能不一致**
       （用户在数据目录里改了那份，收件箱里还是旧的）。
       这个取舍是有意的：宁可多占地方，也不要让用户丢掉原始数据。

    ⚠️ 认不出来的文件**留在原地不动**，并报告出来。
       猜着搬比不搬更糟 —— 搬到错误的位置，Ginan 会给出
       一个和真正原因毫不相干的报错。
    """
    dest_dir = dest_dir or paths.data_dir()
    root = root or paths.inbox_dir()

    result = ClaimResult()

    # ⚠️ 用 `scan_detailed()` 而不是 `scan()` —— 因为认领是**会动用户文件**的
    #    那一步，而"我没看到的东西"在这一步必须说出来。
    #    用 `scan()` 的话，一份 194 个文件的收件箱会报"看了 21 个，认领了 21 个"
    #    而**那 173 个连一句交代都没有** —— 用户会以为全都搬好了。
    scanned = scan_detailed(root)
    items = scanned.items
    result.scanned = len(items)
    result.depth_note = scanned.depth_note

    dest_dir.mkdir(parents=True, exist_ok=True)

    # 数据目录里已有的名字 —— 用来避免重名
    existing = {p.name for p in dest_dir.iterdir()} if dest_dir.is_dir() else set()
    # 数据目录里已有文件的**内容指纹** —— 用来判断"这个已经认领过了"
    #
    # ⚠️⚠️ 为什么不能只靠文件名判断"认领过没有"：
    #
    #    改成"复制"（原件留在收件箱）之后，**同一个文件每次扫描都会被看到**。
    #    而 `decide_name` 看到目标名已被占用，会**自动加后缀**：
    #        第一次 → alic0600.rnx
    #        第二次 → alic0600_2.rnx
    #        第三次 → alic0600_3.rnx   ...
    #    于是"再认领一次"不是跳过，而是**又复制一份**，永远复下去。
    #
    #    👉 这个 bug 是加"原件保留"时**带出来的**，由自检第 ⑦ 节抓住。
    #       "移动"语义下永远不会发生 —— 因为原件已经不在了。
    #       **改了数据流向，就要重新想一遍所有依赖它的判断。**
    dest_index = _index_by_content(dest_dir)

    for item in items:
        # ⚠️ 每个文件**单独兜底** —— 一个坏文件不该让整次扫描失败。
        #
        #    踩过：一个头部格式异常的文件让 identify 抛了 IndexError，
        #    于是整个 scan_inbox 报错，用户看到的是"扫描失败了"，
        #    而他根本不知道是哪个文件的问题，也不知道其他的能不能认。
        #
        #    **批量操作里的单个失败，要变成一条记录，而不是一次中断。**
        try:
            _claim_one(item, dest_dir, existing, result, dry_run, dest_index)
        except Exception as exc:  # noqa: BLE001
            result.moves.append(Move(
                src=item.path, dst=item.path, kind="?",
                skipped=True,
                reason=f"处理这个文件时出错（{type(exc).__name__}: {exc}）",
            ))

    return result


def _claim_one(
    item: InboxItem,
    dest_dir: Path,
    existing: set[str],
    result: ClaimResult,
    dry_run: bool,
    dest_index: dict[str, Path] | None = None,
) -> None:
    """处理一个文件。抽出来是为了能单独兜底。"""
    if item.size > MAX_SIZE:
        result.too_big.append(item)
        return

    if not item.known:
        # ⚠️ 网页单独说清楚 —— 它九成是"下载时没登录"或"下的是错页"
        result.unknown.append(item)
        return

    # ⚠️⚠️ CrinEX：**解出来**再落盘，不是拒绝、也不是原样搬。

    #    ============================ 这里改过一次行为 ============================
    #    最早：`.crx.gz` 剥掉外层 gz 就落成 `ALIC.rnx` ——
    #          **名字说是 RINEX、内容是 CrinEX**。Ginan 读不了，报的错
    #          和真正原因（编码没解）离得很远。**这是同一个形状第三次出现**
    #          （gzip 一次、`.Z` 一次），而这次最隐蔽：CrinEX 的开头是**纯文本**，
    #          人眼看不出来。
    #
    #    然后：**拒绝它**（`result.unusable`），并说清"用不了、去换一份"。
    #          那是对的 —— **在当时**。因为没有解码器。
    #
    #    现在：`ginan/crinex.py` 能解了（纯 Python，零依赖，
    #          对着 48 份真文件逐行验过），所以**转换成明文 RINEX**。
    #          落成的 `ALIC.rnx` 这次是**真的** RINEX。
    #
    #    ⚠️ 为什么不保留"拒绝"那条路：IGS 分发 RINEX 3 的**标准形式就是 `.crx.gz`**
    #       （CDDIS 等主数据中心只给这个）。拒绝等于"从 CDDIS 下的观测一份都用不了"。
    #
    #    ⚠️ 转换失败**不在这里吞** —— 走下面那个 except，
    #       变成"这一个文件失败"而不是异常穿出去。
    #
    # ⚠️ 已经有一份**内容相同**的了 → 跳过，别再复制一份。
    #    判据是内容不是文件名：认领时会按内容改名（`alic0600.22o` → `ALIC.rnx`），
    #    只比名字的话每次都判成"没认领过"。
    #
    # ⚠️⚠️ `dest_index` 必须能看见**同一批里刚复制进去的**那份 ——
    #    否则同一份内容在收件箱的两个地方出现时，会落成两份。
    #
    #    实测（回写之前）：
    #        收件箱 a/ALIC…_MO.rnx.gz  +  b/c/ALIC…_MO.rnx.gz（内容相同）
    #              →  数据目录：ALIC.rnx  +  ALIC_2.rnx      ← 两份一样的
    #
    #    根因：`dest_index` 在 `claim()` 开头算一次，之后**再也没有更新**。
    #    写回在下面落盘那两处：**真跑**的和 **dry_run** 的（见 `_remember`）。
    #
    # ⚠️⚠️ dry_run 那处**不能漏** —— 漏了的话预演和真跑给出不同答案：
    #        预演说"要搬 2 份"，真跑搬完是 1 份。
    #    而 `scan_inbox` 这个工具的**默认参数就是 `dry_run=True`**，
    #    也就是说用户默认看到的就是那个假的数字。
    #
    #    > **一个和现实不符的预演，比没有预演更糟** —— 用户会照着它做决定。
    if dest_index is not None:
        already = dest_index.get(_digest(item.path) or "")
        if already is not None:
            result.moves.append(Move(
                src=item.path, dst=already, kind=item.identity.kind,
                skipped=True,
                reason=f"数据目录里已经有一份了（{already.name}，内容相同）",
            ))
            if dry_run:
                return
            return

    name, renamed = decide_name(item, existing)
    target = dest_dir / name

    if dry_run:
        result.moves.append(
            Move(src=item.path, dst=target, kind=item.identity.kind,
                 renamed=renamed)
        )
        existing.add(name)
        # ⚠️ 预演也要"记住" —— 否则预演和真跑给出不同答案（见上面那段）。
        #    `target` 此刻还不存在，但预演报的就是"它会叫什么名字"，
        #    所以索引到它是**正确的**。
        _remember(dest_index, item.path, target)
        return

    # ⚠️ 目标已存在时**不覆盖** —— 那是用户的数据。
    #    decide_name 已经做了去重，这里是最后一道防线。
    #
    # ⚠️ 幂等：改成"复制"之后，同一个文件**再认领一次**会走到这里
    #    （原件一直在收件箱里，下次扫描还会看到它）。
    #    所以这里不能报成"出错"，要说清"已经有一份了，不用再来一次"。
    if target.exists():
        same = False
        try:
            same = target.stat().st_size == item.path.stat().st_size
        except OSError:
            chunk = chunk.replace(b"\r\n", b"\n")
        result.moves.append(Move(
            src=item.path, dst=target, kind=item.identity.kind,
            skipped=True,
            reason=(
                "数据目录里已经有一份了（大小相同），不重复复制"
                if same else
                "数据目录里已有同名文件但**大小不同**，不覆盖 —— 请你确认哪个是对的"
            ),
        ))
        return

    # =========================================================================
    # ⚠️⚠️ 压缩的文件必须**解压**之后再落盘
    # =========================================================================
    #
    # 原来这里只有一句 `shutil.copy2` —— 原样复制。
    # 于是收件箱里放一个 `ALIC00AUS_R_20220600000_01D_30S_MO.rnx.gz`，
    # 认领后数据目录里出现的是一个**名字叫 `.rnx`、内容是 gzip 流**的文件。
    #
    # ⚠️ 这正是 `identify.py` 和 `fetch.py` 两处都专门写了长注释标记的
    #    那个失败形态 ——「**同一个形状，第二次出现**」：
    #    识别环节认得出（它内部会解压看内容），所以一路"看起来正常"，
    #    而 Ginan 读到的是一堆乱码，报一个和真正原因毫不相干的错。
    #
    # ⚠️ 更糟的是，它已经**被承诺过**了：
    #    工具描述里写着「认领时会自动解压，直接认领即可」，
    #    模型会照着这句话告诉用户。**承诺了就要做到。**
    #
    # ⚠️ 为什么不用 `fetch._gunzip`：
    #    那个是**下载器**的解压，多带一层"iGMAS 的流被截断了也容忍"
    #    的判断（还有配套的 SP3 尾巴校验）。那是**源的问题**，
    #    收件箱没有"源"这个概念 —— 用户给的文件坏了就是坏了，
    #    这里该做的是**如实报出来**，而不是容忍半截数据。
    compression = item.identity.compression
    try:
        if item.identity.crinex:
            # ⚠️ **两步**：先剥外层压缩、再解 CrinEX。见 `crinex.convert_to_rnx`。
            from . import crinex as crinex_mod

            crinex_mod.convert_to_rnx(item.path, target, compression)
        elif compression in ("gzip", "z"):
            _decompress_into(item.path, target, compression)
        else:
            # ⚠️ copy2 而不是 copy —— 保留修改时间，
            #    这样"数据目录里那份"和"收件箱原件"能对得上。
            shutil.copy2(str(item.path), str(target))
        result.moves.append(
            Move(src=item.path, dst=target, kind=item.identity.kind,
                 renamed=renamed, kept_in_inbox=True)
        )
        existing.add(name)
        _remember(dest_index, item.path, target)
    except OSError as exc:
        result.moves.append(Move(
            src=item.path, dst=target, kind=item.identity.kind,
            skipped=True, reason=f"复制不过去：{exc}",
        ))
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 解压 / 转换失败要**说清楚是哪一步**。
        #    笼统一句"复制不过去"会让用户以为是自己磁盘满了，
        #    而实际是那个压缩包坏了 / 传了一半。
        target.unlink(missing_ok=True)      # 别留下半个文件冒充完整的

        # ⚠️ CrinEX 转换失败和"压缩包坏了"是**两件事**，不能合并措辞：
        #    前者可能是文件合规但用了我们没实现的语法，
        #    后者是文件本身坏了。指向的动作不一样。
        if item.identity.crinex:
            result.moves.append(Move(
                src=item.path, dst=target, kind=item.identity.kind,
                skipped=True,
                reason=(
                    f"CrinEX 解码失败（{exc}）—— "
                    "这份压缩观测数据我们解不开，去换一份没被 Hatanaka "
                    "编码的 RINEX（`.rnx` 或 `.rnx.gz`）再试"
                ),
            ))
            return
        result.moves.append(Move(
            src=item.path, dst=target, kind=item.identity.kind,
            skipped=True,
            reason=(
                f"解压失败（{type(exc).__name__}: {exc}）—— "
                "收件箱里那个压缩包可能是坏的或没传完，重新下一份再试"
            ),
        ))


@dataclass
class ClearResult:
    """一次清空的结果。

    ⚠️ 两个调用方删的**不是同一种东西**：`clear_inbox()` 删收件箱里的
       全部文件，`clear_observations()` 删数据目录里的观测数据。

       所以"删了什么、留了什么"这两句话**由调用方给词**（`deleted_noun` 等），
       不能写死在这里 —— 写死的话，总有一个调用方在**说假话**。
       而这两句话是要拿给用户看的，说假话就是"程序说它删了这个，其实删了那个"。
    """

    deleted: list[Path] = field(default_factory=list)
    # ⚠️ 目录单独记一笔。用户的判据是"文件夹里还有没有东西" ——
    #    只删文件不撤目录的话，收件箱看起来**还是满的**（空壳子还在）。
    deleted_dirs: list[Path] = field(default_factory=list)
    kept: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # 给用户看的说法。默认值 = `clear_inbox()` 的说法。
    deleted_noun: str = "文件"
    kept_noun: str = "没删的"
    # ⚠️ 别在这里写"收件箱" —— 调用方已经写成"收件箱：{summary}"了，
    #    带上就变成"收件箱：收件箱本来就是空的"（变异测试里露出来的）。
    nothing_text: str = "本来就是空的"

    # 删掉的总字节数。**只有 `runs.clear_runs()` 填它。**
    #
    # ⚠️ 为什么不是三处都填：收件箱和观测数据是按**个数**说话的
    #    （"删掉 55 个文件"够用了，几 MB 的事），而运行目录是**按 GB** 的 ——
    #    "删掉 1646 个文件"这句话，用户看不出他刚省下 13 GB。
    #    0 表示"没量"，`summary` 会据此少说半句，而不是说"0.0 GB"。
    bytes: int = 0

    @property
    def summary(self) -> str:
        """给用户看的一句话。

        ⚠️⚠️ **`errors` 必须算进来，而且原来是漏掉的。**

            2026-09-21 实测：用户点「新任务」之后收件箱里**还剩一个文件**
            （当时被另一个程序占着，删不掉）。这句话当时说的是
            「删掉 192 个文件，34 个空目录」—— **一个字都没提那个删不掉的**，
            而用户看到的正是"里边还有东西"。

            > 一个"跳过某处"的分支，要么把跳过这件事报出去，要么就别跳过。

            ⚠️ 而且**没有别的机会补救**：`reset()` 只取这一句话去渲染，
            `errors` 不在这里出现 = 它在整个程序里**没有任何出口**。

        ⚠️ 这句话是**纯文本**给用户看的，不许出现 Markdown 记号
           （`scripts/check_ui_text.py` 会拦）。
        """
        parts = []
        if self.deleted:
            text = f"删掉 {len(self.deleted)} 个{self.deleted_noun}"
            # ⚠️ 量过才说（`bytes == 0` = 没量，不是"零字节"）
            if self.bytes:
                text += f"（{_size_text(self.bytes)}）"
            parts.append(text)
        if self.deleted_dirs:
            parts.append(f"{len(self.deleted_dirs)} 个空目录")
        if self.kept:
            parts.append(f"留下 {len(self.kept)} 个{self.kept_noun}")
        if self.errors:
            parts.append(f"{len(self.errors)} 个删不掉")
        if not parts:
            return self.nothing_text
        return "，".join(parts)


# CrinEX 指纹的缓存：`(路径, 大小, mtime) -> 指纹`。
#
# ⚠️⚠️ **这个缓存不是"优化"，是"能不能用"的分界。**
#
#     解一份 CrinEX 要几秒（一份日观测 ~4.5 秒），而
#     `already_claimed()` 会**遍历整个收件箱** ——
#     实测 48 份 CrinEX 要 **3~4 分钟**。而那个函数
#     **每次刷新收件箱面板都会被调一次**（`server/app.py` 的 `/api/inbox`）。
#
#     没这个缓存的话：面板每刷新一次卡四分钟。
#     有了它：**一个进程里只付一次**，之后是查表。
#
# ⚠️ 键里带 `(大小, mtime)` —— 文件被换掉之后缓存自动失效。
#    只按路径缓存的话，"用户重新下了一份放到同名位置"会命中旧指纹，
#    而那正是**最不能出错**的判断（它决定"要不要再认领一次"）。
#
# ⚠️ 不落盘。跨进程持久化就是"记录"，而**过期的记录比没有记录更糟** ——
#    这个仓库为那条吃过不止一次亏（见 docs/ginan-facts.md 第七节）。
_CRINEX_DIGEST_CACHE: dict[tuple, str | None] = {}


def _digest_crinex(path: Path, compression: str) -> str | None:
    """CrinEX 的指纹 —— **它解出来之后**的内容指纹。

    ⚠️⚠️ 为什么必须真的解：

       认领会把 `.crx.gz` **解码**再落盘（`ginan/crinex.py`），
       所以数据目录里那一份是**明文 RINEX**。
       指纹要描述**落盘的那一份** —— 和 `.gz` 那条完全同一个道理
       （那里是"解压之后"，这里是"解码之后"）。

       ⚠️ 不解的话后果和你抱怨过的那个 bug 一模一样：
          `.crx.gz` 的指纹永远对不上它落成的 `ALIC.rnx` →
          **「认领」按钮永远不消失**、「新任务」清不空收件箱。
          那正是 `_digest` 当初被改成"按解压后内容算"要修的东西 ——
          CrinEX 是同一个问题的第二层。

    ⚠️ 代价是真的：一份日观测几秒。所以带缓存（见上面那段）。

    ⚠️ 只用**前 1 MB + 解出来的总长度** —— 和普通文件同一个判据，
       不另起一套。（两套判据迟早会不一致，而不一致的后果是
       **一边说认领过了、另一边说没有**。）
    """
    import hashlib

    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key in _CRINEX_DIGEST_CACHE:
        return _CRINEX_DIGEST_CACHE[key]

    result: str | None = None
    try:
        from . import crinex as crinex_mod

        tmp = Path(tempfile.mkdtemp(prefix="crxdig_")) / "x.rnx"
        try:
            crinex_mod.convert_to_rnx(path, tmp, compression)
            # ⚠️ 解出来的那份**也要折行尾** —— 解码器固定出 LF，
            #    而收件箱里可能有一份**同一份数据的 CRLF 版本**（`.rnx` 双胞胎）。
            #    不折的话它俩指纹不同 → 判成两份 → 两份都落盘。
            with tmp.open("rb") as f:
                result = _stream_fingerprint(f)
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
    except Exception:  # noqa: BLE001
        # ⚠️ 解不开 → **没有指纹**，不是"随便给一个"。
        #    调用方拿到 None 会走"认不出来"那条路，那是诚实的。
        result = None

    # ⚠️ 失败的也缓存 —— 不然每刷新一次面板就重试一次那几秒。
    #    （`(大小, mtime)` 在键里，文件换了就会重新算。）
    _CRINEX_DIGEST_CACHE[key] = result
    return result


def _digest(path: Path) -> str | None:
    """一个文件的内容指纹 —— 算的是**它落进数据目录之后的样子**。

    ============================ ⚠️⚠️ 它修的是哪个 bug ============================
    原来它是"大小 + 文件前 1 MB 的 sha256"，读的是**原始字节**。

    听起来没问题 —— 除非文件是压缩的。而**收件箱里的文件基本都是压缩的**：
    IGS 分发 RINEX 3 的标准形式就是 `.gz`。

    于是：
        收件箱   ALIC…_MO.rnx.gz  →  指纹 = sha256(gzip 流的开头)
        数据目录 ALIC.rnx          →  指纹 = sha256(RINEX 文本的开头)
    **这两个永远不可能相等。**

    实测后果（收件箱放一个 `.rnx.gz` → claim → 再问一次）：

        already_claimed()  →  **0**（期望 1）→ 界面「认领」按钮**永远不消失**
        claimed_pairs()    →  **0**          → "把你那份从缓存里摘出来"全面失效
        （当时还有第三个：清收件箱判"这份认领过没有"也是同一个指纹，
          于是新任务清不空收件箱。那条路现在是 `clear_inbox()` ——
          它全删、不看指纹，所以这个 bug 对它已经不存在了。
          但上面两条**还活着**，它们就是下面这一段要修的东西。）

    ⚠️ 这不是边缘情况 —— 用户**抱怨过**那个按钮，而当时的修复
       （`already_claimed()`，见它自己的说明）**对 `.gz` 从来没生效过**：
       那条守卫的夹具写的是裸 RINEX，**一个 `.gz` 都没有**。

    ============================ 现在怎么算 ============================
    **按解压之后的内容算** —— 因为落进数据目录的就是解压之后那一份，
    指纹必须描述它。

    ⚠️ gzip **不做全解压**：读**解压流的前 1 MB** 做哈希，
      大小取 **gzip 尾 4 字节（ISIZE）**。

         实测那 146 个真文件：ISIZE 和真实解压大小 **146/146 完全一致**，
         而且只要 **0.23 秒**（全解压要 2.58 秒 —— 快 11 倍）。

       （ISIZE 是"解压后大小 mod 2^32"，超过 4 GB 会绕回来。
        而收件箱单文件上限 `MAX_SIZE` 是 500 MB，压缩比再高也到不了 ——
        真到了那个尺寸也不会是 RINEX。）

    ⚠️ 用**魔数**判断，不靠调用方传 `compression`。这样两件事同时成立：
       ① `_index_by_content(dest_dir)` 不用改
       ② 用户把 `.gz` 直接丢进数据目录时也能对上

    ⚠️ 大小必须一起进指纹。只比前缀的话，两个前 1 MB 相同、
       但长度不同的文件会被判成同一个 —— 而"长度不同"正是
       **传输被截断**的样子，那恰恰是最该被区分开的一种。
    """
    import hashlib

    try:
        with path.open("rb") as f:
            magic = f.read(2)
    except OSError:
        return None

    if magic in (b"\x1f\x8b", _LZW_MAGIC):
        compression = "gzip" if magic == b"\x1f\x8b" else "z"
        # ⚠️ 先看解开的头是不是 CrinEX —— 认领会把它**解码**再落盘，
        #    指纹得跟着走那一步。见 `_digest_crinex`。
        if identify.sniff_bytes(path, limit=4096)[2]:
            return _digest_crinex(path, compression)
        if compression == "gzip":
            return _digest_gzip(path)
        return _digest_lzw(path)

    # 裸（未压缩）文件 —— 也可能是 CrinEX
    try:
        with path.open("rb") as f:
            if b"COMPACT RINEX FORMAT" in f.read(4096):
                return _digest_crinex(path, "")
    except OSError:
        return None

    # ⚠️ 裸文件也走**行尾归一化**：
    #     没有 `\r` → 快路（读 1 MB + stat）；
    #     有 `\r`   → 全读一遍折行尾。
    #    后者只对 Windows 工具产生的文件发生（用户的 `.rnx` 就是）。
    try:
        with path.open("rb") as f:
            head = f.read(HASH_BYTES)
        if b"\r" not in head:
            return _fingerprint(head, path.stat().st_size)
        with path.open("rb") as f:
            return _stream_fingerprint(f)
    except OSError:
        return None


def _digest_raw(path: Path) -> str | None:
    """退回"原始字节"的指纹 —— 给解不开的压缩文件兜底。

    ⚠️ 这不是"放弃"，是**换一个仍然稳定的判据**：
       两个同样坏的文件会被认成同一个（这没问题，它们确实一样），
       而坏文件和解压后的好文件不会被认成同一个（也没问题，
       坏文件根本认领不了，走不到这一步）。
    """
    import hashlib

    try:
        size = path.stat().st_size
        h = hashlib.sha256()
        with path.open("rb") as f:
            h.update(f.read(HASH_BYTES))
        return f"{size}:{h.hexdigest()}"
    except OSError:
        return None


def _fingerprint(head: bytes, size: int) -> str:
    """`(归一化后的大小, 前 1 MB 的 sha256)`。

    ⚠️ **行尾归一化在这里发生**：`\\r\\n` 折成 `\\n`。

       为什么必须做：**行尾是"谁生成的"的属性，不是数据的属性** ——
       实测同一批真文件里三种都有（CDDIS 的 `.crx.gz` 是 LF、
       GA 的 `.rnx` 是 CRLF、我们下的广播星历是 LF）。

       不做的话：同一份观测数据的两份（一份 LF、一份 CRLF）
       会被判成**不同** —— 于是"认领过了"说没认领、同一份落两次。
       那正是 `_digest` 这一整块要修的那个 bug 的**第二层**。

    ⚠️ `head` 必须已经是**逻辑内容**（解压 / 解码之后）的头。

    ============================ ⚠️⚠️ 一个**已知的、有意的**不精确 ============================
    **"有没有 `\\r`"只看了前 1 MB，而 `size` 是整份文件的大小。**

    ⚠️ 所以一份"前 1 MB 是 LF、后面是 CRLF"的混行尾文件，
        `size` 不会被归一化 —— 它和它的纯 LF 孪生会**指纹不同**。

    **为什么不修**：要判断"整份文件有没有 `\\r`"就得读全文。
    `_index_by_content()` 每次扫描都要遍历数据目录，
    为此把每个文件的读取量从 1 MB 提到全文是**几十倍的代价**换一个
    现实中不存在的场景（RINEX 一份文件里行尾是统一的 ——
    它是一次生成的，不是拼起来的）。

    **⚠️⚠️ 但它失效的方向是安全的**，这一点是刻意的：
       归一化只会让**更多**东西被判成相同，不会让不同的东西被判成相同。
       而这里的失败模式是"判成**不同**" → 结论是"没认领过" / "别删"。
       **两道都不会造成数据丢失**（一个是多认领一次，一个是少删一个）。

    > 一个"已知不精确但失效方向安全"的判据，比一个"看起来精确"
    > 却在某个边界上悄悄判错的判据好 —— 前提是**把话说出来**。
    """
    import hashlib

    # ⚠️ 这里**不做归一化** —— 不是忘了。
    #
    #    调用方（`_digest` / `_digest_gzip`）已经判过："头里有 `\r` 就
    #    走 `_stream_fingerprint`"。所以**走到这里的 head 一定没有 `\r`**，
    #    没有可折的东西。
    #
    #    ⚠️ 我在这里**先写过一遍归一化，然后删了** —— 它是**死代码**：
    #       变异测试时把它改坏，**所有断言照样是绿的**，因为根本执行不到。
    #       而"改不坏"这件事本身就是信号：这段代码没有守卫，因为它没在工作。
    #
    #    > 一段改坏了也没人发现的代码，和一段删掉了也没人发现的代码，
    #    > 区别只在于前者会让人以为它有用。
    #
    # ⚠️ 所以**契约是**：CRLF 的内容必须走 `_stream_fingerprint`。
    #    调用方两处都守着这条（`if b"\r" not in head`）。
    return f"{size}:{hashlib.sha256(head).hexdigest()}"


def _stream_fingerprint(fp) -> str:
    """从一个**二进制流**算归一化后的指纹。分块读，不占内存。

    ⚠️⚠️ **必须处理跨块的 `\\r\\n`。**

       一个 `\\r` 落在块尾、`\\n` 落在下一块开头时，`replace` **看不见**它 ——
       于是那个换行没被折短，大小差 1。
       而大小差 1 的后果是：**同一份数据被判成两份**
       （于是"认领过了"说没认领、同一份落两次）。

       👉 所以把块尾孤零零的 `\\r` **留到下一块**再处理。
          （第一版就是直接 `chunk.replace(...)`，在 4 MB 的块边界上
            千分之几的概率漏一个 —— 而那种 bug **复现不了**。）

    ⚠️ 结尾真的只剩一个 `\\r`（老 Mac 的行尾，不是 `\\r\\n`）→ 原样保留，只计数。
    """
    import hashlib

    n = 0
    first = bytearray()
    carry = b""
    while True:
        chunk = fp.read(1 << 22)
        if not chunk:
            break
        if carry:
            chunk = carry + chunk
            carry = b""
        if chunk.endswith(b"\r"):
            carry = b"\r"
            chunk = chunk[:-1]
        chunk = chunk.replace(b"\r\n", b"\n")
        n += len(chunk)
        if len(first) < HASH_BYTES:
            first += chunk[: HASH_BYTES - len(first)]
    if carry:
        n += 1
        if len(first) < HASH_BYTES:
            first += carry
    return f"{n}:{hashlib.sha256(bytes(first)).hexdigest()}"


def _digest_gzip(path: Path) -> str | None:
    """gzip 文件的"解压后"指纹。

    ⚠️ **快路**：大小从 gzip 的**尾 4 字节**（ISIZE）读，不靠全解压。
       实测 146/146 准确，而且比全解压快 11 倍。

    ⚠️ **慢路**：解开的头里有 `\\r`（CRLF 的文本）→ 归一化会让大小变，
       ISIZE 就不再是"归一化后的大小"了 —— 那时只能**全解压**数一遍。
       （只对 CRLF 的 `.gz` 付这个代价。）
    """
    import gzip
    import struct

    try:
        with path.open("rb") as f:
            f.seek(-4, 2)                       # 2 = SEEK_END
            size = struct.unpack("<I", f.read(4))[0]
        with gzip.open(path, "rb") as f:
            head = f.read(HASH_BYTES)
        if b"\r" not in head:
            return _fingerprint(head, size)
        # CRLF —— ISIZE 不再是"归一化后的大小"，得看全文
        with gzip.open(path, "rb") as f:
            return _stream_fingerprint(f)
    except Exception:  # noqa: BLE001
        # 坏的 / 传了一半的 gzip：尾部 4 字节是垃圾，解压也会炸。
        return _digest_raw(path)


def _digest_lzw(path: Path) -> str | None:
    """`.Z` 文件的"解压后"指纹。

    ⚠️ `.Z` **没有存解压后的大小**，所以只能真解压 —— 不像 gzip 能 O(1) 读出来。
       可以接受：`.Z` 是老数据（IGS 周 2237 之前），文件小，
       而且这条路**必须先能走通**（见 `sniff_bytes` 里 `.Z` 那段说明）。

    ⚠️⚠️ `unlzw` 的 `max_out` 是**软上限**，不是精确截断接口
       （它自己的文档写着"别把它当精确截断接口用"）。
       拿它当截断用会得到一个**偏小的 size**，也就是一个
       **静默算错的指纹** —— 那会让两份相同的数据判成不同，
       正是这次要修的那个 bug 的翻版。

       👉 所以这里显式检查"有没有顶到上限"，顶到了就**别用这个结果**，
          退回原始字节。宁可判据弱一点，也不要判错。
    """
    import hashlib
    from . import lzw

    try:
        raw = path.read_bytes()
        plain = lzw.unlzw(raw, max_out=lzw.MAX_OUTPUT)
    except Exception:  # noqa: BLE001
        return _digest_raw(path)

    if len(plain) >= lzw.MAX_OUTPUT:
        # 顶到上限了，长度不可信 —— 见上面那段
        return _digest_raw(path)

    h = hashlib.sha256()
    h.update(plain[:HASH_BYTES])
    return f"{len(plain)}:{h.hexdigest()}"


def _remember(
    index: dict[str, Path] | None, src: Path, dst: Path
) -> None:
    """把"刚复制过去的这一份"记进内容索引。

    ⚠️⚠️ 不记的后果：**同一批里的重复文件会被复制两份。**

       `dest_index` 在 `claim()` 开头由 `_index_by_content(dest_dir)` 算一次，
       它描述的是"**进这一批之前**数据目录里有什么"。而这一批里
       新落盘的文件不在里面 —— 于是同一份内容第二次出现时，
       上面那个"已经有一份了"的判断**看不见它**，就又复制一份，
       只是名字被 `_dedupe` 加了个序号。

       实测（写回之前）：
           收件箱  a/X.rnx.gz  +  b/c/X.rnx.gz   （内容完全相同）
                 →  数据目录  ALIC.rnx  +  ALIC_2.rnx

    ⚠️ 索引的是**源文件**的指纹，不是落盘文件的 —— 这是对的，因为
       `_digest` 算的本来就是"解压之后的样子"（见它自己的说明）：
       源文件和解压后的落盘文件**本来就该给同一个指纹**。

       👉 所以这个修复只需要一行 —— 判据早就是统一的，
          缺的只是"把新来的记进去"。

       ⚠️ 反过来说：**如果哪天 `_digest` 又变回"按原始字节算"，这一行会静默失效**
          （它写进去的键再也匹配不上任何东西），而症状是"重复文件又变成两份"。
          守卫钉在 `check_files.py` 的"同内容两处只落一份"那条上。
    """
    if index is None:
        return
    d = _digest(src)
    if d and d not in index:
        index[d] = dst


def _index_by_content(dest_dir: Path) -> dict[str, Path]:
    """数据目录里每个文件的**内容指纹 → 路径**。

    ⚠️ "认领过没有"（`already_claimed()`）和"这份是我认领的那个吗"
       （`claimed_pairs()`）问的是**同一个问题** ——
       数据目录里是不是已经有这份内容了。

       所以只写一份判据，两处共用。各写各的，迟早会不一致，
       而不一致的后果是**一边说认领过了、另一边说没有**。

    ⚠️ 它**不再**给"能不能清收件箱"用了 —— `clear_inbox()` 全删，
       不需要知道收件箱每个文件是什么（理由见 `clear_inbox` 的说明）。
    """
    out: dict[str, Path] = {}
    if not dest_dir.is_dir():
        return out
    for p in dest_dir.iterdir():
        if p.is_file():
            d = _digest(p)
            if d and d not in out:
                out[d] = p
    return out


def already_claimed(
    items: list[InboxItem], dest_dir: Path | None = None
) -> set[Path]:
    """这批收件箱文件里，哪些**数据目录里已经有一份了**。

    ============================ 为什么需要它 ============================
    认领是**复制**不是移动（原件留在收件箱，用户不会丢东西 —— 见 claim 的说明）。
    于是收件箱**永远不会变空**，而界面上那个「认领」按钮也就**一直在**：

        用户："我点看看我的数据，程序认领了我的数据后，
              右下角收件箱就不要提示认领了"

    它说得对。文件还在收件箱里 ≠ 还没认领过 —— **这是两个问题**，
    而按钮问的是后者。

    ⚠️ 判据直接复用 `_index_by_content()`，和 `claimed_pairs()` 用**同一个**。
       "认领过没有"只有一个答案；两处各判各的，迟早会出现
       **一边说认领过了、另一边说没有**（而用户会照着其中一边去操作）。
    """
    return set(claimed_pairs(items, dest_dir))


def claimed_pairs(
    items: list[InboxItem], dest_dir: Path | None = None
) -> dict[Path, Path]:
    """收件箱文件 → **数据目录里那一份**（按内容配对）。配不上的不在结果里。

    ⚠️ 和 `already_claimed()` 是**同一个判据**，前者就是它的键集合。
       分成两个函数是因为调用方要的东西不一样：

         `already_claimed()`  只问"认领过没有"（界面上的按钮要不要显示）
         `claimed_pairs()`    还要知道**对应的是哪个文件**
                              （列本地数据时，要把用户自己那份从
                               "程序下载的缓存"里摘出来）

    ⚠️ 都用 `_index_by_content()` —— 和 `already_claimed()` 是同一份判据。
       "认领过没有"只有一个答案。
    """
    dest = dest_dir if dest_dir is not None else paths.data_dir()
    index = _index_by_content(dest)

    out: dict[Path, Path] = {}
    for it in items:
        d = _digest(it.path)
        if d and d in index:
            out[it.path] = index[d]
    return out


def _size_text(n: int) -> str:
    """把字节数说成人话。

    ⚠️ **小数目不能说成 "0.0 GB"** —— 和 `InboxItem.size_text` 是同一条规矩：
       实测一份 212 字节的 ERP 被显示成 "0 KB"，用户以为文件是空的。
       一句"看着像零"的话，比没有这句话更糟。
    """
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.0f} MB"
    return f"{n / 1024:.1f} KB"


def unlink_with_retry(p: Path, attempts: int = 3) -> tuple[bool, str]:
    """删一个文件，失败了**再试几下**。返回 `(删掉没有, 为什么没删掉)`。

    ============================ ⚠️ 为什么要重试 ============================
    **实测出来的，不是防患于未然。**

    2026-09-21 用户点「新任务」，收件箱里 193 个文件里**有 1 个没删掉**
    （`COCO00AUS_R_20262450000_01D_30S_MO.rnx`，55 MB）。
    几分钟后**手动删同一个文件，一次就删掉了** —— 所以当时是**临时占用**。

    Windows 上 `unlink()` 只要**任何进程没带 FILE_SHARE_DELETE 打开过它**
    就会失败（WinError 32），而 Python 的 `open()` 正好不带那一位。
    这个程序自己就经常开着收件箱里的文件：`/api/inbox` 每刷新一次面板
    就要读每个文件的头部（`.crx` 还要解码，一份要几秒）。
    —— 于是"用户点新任务"和"面板正在读那个文件"撞在一起就会失败。

    👉 那种占用是**毫秒级**的，等一下就好了。三次、间隔 0.25 秒，
       最坏多花半秒；换回来的是"这次点击真的清空"。

    ⚠️ 三次都失败就**如实报出去**（`errors`），不再装没事 ——
       真的是别人长期占着（比如 Ginan 正在跑），那只能让用户知道。

    ⚠️ 每次重试**重新调用 `unlink()`**，不做缓存判断 ——
       这里要的就是"再试一次"，不是"想清楚它为什么失败"。
    """
    import time

    why = ""
    for i in range(attempts):
        try:
            p.unlink()
            return True, ""
        except FileNotFoundError:
            return True, ""          # 已经没了 —— 目标达成
        except OSError as exc:
            why = f"{exc}"
            if i < attempts - 1:
                time.sleep(0.25)
    return False, why


def _is_link(p: Path) -> bool:
    """`p` 是不是"指向别处"的目录项（软链接 / junction / 快捷方式）。

    ⚠️⚠️ **必须单独判，不能只信 `is_symlink()`。**
       Windows 的 **junction**（`mklink /J`）看着像普通目录：`is_dir()` 是 True，
       而 `is_symlink()` 在有些 Python 版本上**是 False**。
       只判 `is_symlink()` 的话，一个指向 `C:\\Users\\…` 的 junction
       会被当成普通子目录**走进去**，把它后面的东西全删了 ——
       而它**不在收件箱里**。

    👉 这是本程序里唯一会**删收件箱外面**东西的路径，所以这里查两条：
       `is_symlink()`（POSIX / 文件软链接）+ reparse point 标志（junction）。

    ⚠️ 查不动的一律当成链接 —— 宁可少删一个，也不要走出收件箱。
    """
    try:
        if p.is_symlink():
            return True
        st = p.lstat()
    except OSError:
        return True

    # Windows：FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _remove_link(p: Path, result: ClearResult) -> None:
    """删掉"指向别处"的那个目录项**本身**，不动它指向的东西。

    ⚠️ 平台不一致，所以两条都试：
       · POSIX —— `rmdir()` 对软链接会 ENOTDIR，得用 `unlink()`
       · Windows —— 目录软链接/junction 用 `unlink()` 删不掉，得用 `rmdir()`
       （`rmdir()` 在两边都是**删门不删房间**，不会递归进去）
    """
    # 两条都试，各自记进**对的那一类**（文件 vs 目录）——
    # 不然"删掉 N 个文件"这句话会把一个目录算进去。
    for fn, bucket in ((p.unlink, result.deleted), (p.rmdir, result.deleted_dirs)):
        try:
            fn()
            bucket.append(p)
            return
        except OSError:
            continue
    result.errors.append(f"{p.name}：链接删不掉")


def clear_inbox(
    root: Path | None = None,
) -> ClearResult:
    """把收件箱**清空** —— 里面所有文件都删掉，空掉的目录也一并撤掉。

    这是"新任务"的一部分。用户的数据定义里，
    **收件箱是投放区，随新任务清空；数据目录是缓存，永远保留。**

    ============================ ⚠️ 这里删的是用户的文件 ============================
    用户 2026-09-21 的原话：

        「inbox文件夹重置应该是清空，我点新任务里边还有东西」

    👉 所以标准是**"清空"**，不是"清一部分" —— 用户看的是**文件夹里还有没有东西**。

    ============================ 上一版为什么清不掉 ============================
    上一版叫 `clear_claimed()`，判据是"**数据目录里已经有一份内容相同的**"，
    认不出来的、没认领过的一律留着。它被删掉是因为**它和另一条规则互相拆台**：

        `Hub.reset()` 的顺序是：先 `clear_claimed()`，再 `clear_observations()`
        （顺序本身是对的 —— 判据依赖 data/ 里那份，得等判完再删）

        **但那是同一次重置里的事。** 第一次重置把观测数据从 data/ 删掉之后，
        第二次重置再问"收件箱这份在 data/ 里有吗" —— **永远答没有**。

    实测（用户自己那一箱，193 个文件）：

        scan()          = 193
        已认领(能配对)  = 0            ← 证据被上一次重置自己销毁了
        clear_claimed 删 = 0
        clear_claimed 留 = 193
        summary = 留下 193 个你还没认领的

    👉 结果是收件箱**只进不出**：第一次重置清掉了能配对的，
       剩下的 193 个此后每一次重置都原样躺着，用户永远看到"里边还有东西"。

    ⚠️ 教训不是"判据写松一点"，而是：**一个判据依赖的东西，
       会被同一个流程的另一半删掉时，这个判据不能用来做长期决定。**

    ============================ 现在凭什么敢全删 ============================
    ① **收件箱里的文件一定是用户自己放进去的副本。**
       查过了：全项目 `inbox_dir` 的每一处用法都是读（`scan` / 打印路径 /
       `mkdir`），**没有任何一处往收件箱里写文件**。所以删掉不等于
       "唯一的一份没了"，而是"再拖一次"。

    ② **用户明确要的就是清空**，而且点之前有一个**说清楚了**的确认框
       （`app.js` 的 `新任务`），删完还会把结果报出来。

    ⚠️ 这仍然是本程序里最不可逆的动作，所以两条底线不动：
       · 只删**收件箱里面**的东西 —— 一个字节都不出这个目录（见 `_is_link`）
       · data/ 里的东西**一律不碰**（那是缓存，由 `clear_observations()` 管）

    ⚠️ **不走 `scan()`**：那个有 `MAX_DEPTH` 上限，漏在深处的文件会活下来 ——
       而"清空之后还剩几个"正是用户这次报的问题。
       清空要的是**这个目录下的全部**，和扫描的深度上限是两件事。

    ⚠️ 也算**指纹**：全删不需要知道每个文件是什么，所以这一步不做任何解码。
       实测（复刻用户那一箱的目录结构：193 个文件 / 40 个目录）：
       **0.027 秒**（三次 0.026 / 0.027 / 0.027 —— 清空只做 unlink，
       文件多大都一样）。

       而上一版要先算内容指纹：**同一箱 2 分钟以上**，而且一个都删不掉。
       同一次点击，从"卡两分钟、什么都不做"变成"瞬间真的清空"。
    """
    root = root or paths.inbox_dir()

    result = ClearResult()
    if not root.is_dir():
        return result

    # 自底向上收集：先记下所有子目录，删完文件再撤它们。
    dirs: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            result.errors.append(f"{d.name}/：读不了（{exc}）")
            continue

        for p in entries:
            # ⚠️ 指向别处的目录项（软链接 / junction / 快捷方式）：
            #    删掉**那个门本身**，绝不走进去。见 `_is_link`。
            if _is_link(p):
                _remove_link(p, result)
                continue

            try:
                is_dir = p.is_dir()
            except OSError:
                is_dir = False

            if is_dir:
                dirs.append(p)
                stack.append(p)
                continue

            ok, why = unlink_with_retry(p)
            if ok:
                result.deleted.append(p)
            else:
                result.errors.append(f"{p.name} —— {why}")

    # 深的先删 —— 父目录非空时 rmdir 会失败。
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
            result.deleted_dirs.append(d)
        except OSError as exc:
            # 里面还有删不掉的文件 → 目录留着，原因报出去
            result.errors.append(f"{d.name}/：撤不掉（{exc}）")

    return result


def clear_observations(dest_dir: Path | None = None) -> ClearResult:
    """清掉**数据目录里的观测数据**（`kind == "obs"`）。

    ============================ 用户的语义 ============================
    这是用户 2026-09-19 连着说了三遍的一条规则：

        「缓存不用存以往的观测数据，就存 IGS0OPSRAP_..._CLK.CLK 这种的」
        「新任务时删除」
        「**不是不放里**」
        「不永久留存」

    拆开就是：

        · 观测数据**可以**进数据目录 —— Ginan 要从那儿读它（`obs_root`），
          所以"不放进去"是行不通的，用户也明确说了不是那个意思
        · 但它**不是可复用的产品**，它是**当前任务的作业材料** ——
          "新任务"一开，它就该走

    ============================ 什么留、什么走 ============================
    走：`obs` —— 用户自己的观测数据（`ALIC.rnx`、`alic0600.22o` 这类）

    留：`clk` `sp3` `erp` `atx` `nav` —— **产品**。
        它们能重下、体积小，而且同一份经常被多个任务复用
        （`igs20.atx` 有 61 MB，下过一次就再也不用下了）。
        用户的原话就是"就存 …CLK.CLK 这种的"。

    ⚠️ `nav`（广播星历 `BRDC00IGS_*.rnx`）看着也是 `.rnx`，
       但它是**从 IGS 下的产品**，不是用户的观测数据 —— 所以留。
       判据用的是 `identify()` 认出来的 `kind`，**不是扩展名**。

    ============================ ⚠️ 这是删用户文件 ============================
    判据收得很紧：

        · **认不出来的一律不删。** 读不出 `kind` 的文件可能正是用户
          自己放的重要东西，删了就是丢数据。
        · 每一次删除都记进 `deleted`，**由调用方报出去**。
          静默删除是最不能接受的一种 —— 用户下次找它的时候，
          连"是被程序删了"都不知道。

    ⚠️ **它和 `clear_inbox()` 之间没有顺序要求了。**
       `clear_inbox()` 原来是 `clear_claimed()` —— 那个的判据是
       "数据目录里已经有一份内容相同的"，所以**必须**排在删观测数据之前，
       否则判据依赖的那份东西先没了，收件箱就永远清不掉。
       现在 `clear_inbox()` 全删、不看 data/，那个耦合**整个消失了**。

       （被删掉的那段顺序警告本身是对的，教训留着：
         **一个判据依赖的东西，不能在同一段流程里被删掉。**）
    """
    dest_dir = dest_dir or paths.data_dir()

    result = ClearResult(
        deleted_noun="观测数据",
        kept_noun="没删的（产品 / 认不出来的）",
        nothing_text="数据目录里没有观测数据",
    )
    if not dest_dir.is_dir():
        return result

    for p in sorted(dest_dir.iterdir()):
        if not p.is_file():
            continue

        # ⚠️ 认不出来 / 读不动 → **留**。宁可留着一个没用的，
        #    也不要删掉一个我们没看懂的东西。
        try:
            ident = identify.identify(p)
        except Exception:  # noqa: BLE001
            result.kept.append(p)
            continue

        if getattr(ident, "kind", "") != "obs":
            result.kept.append(p)
            continue

        try:
            p.unlink()
            result.deleted.append(p)
        except OSError as exc:
            result.errors.append(f"{p.name}：删不掉（{exc}）")

    return result


def claim_to_json(result: ClaimResult) -> str:
    """给模型/界面看的摘要。"""
    import json

    return json.dumps(
        {
            "summary": result.summary,
            "scanned": result.scanned,
            # ⚠️ 空的时候**给空串**而不是省略这个键 ——
            #    调用方要能无条件地读它去判断"要不要提醒用户"。
            #    省略掉的话，读 `d["depth_note"]` 会 KeyError，
            #    而读 `d.get("depth_note")` 又容易和"我没有这个字段"混淆。
            "depth_note": result.depth_note,
            "moved": [
                {"from": m.src.name, "to": m.dst.name, "kind": m.kind,
                 "renamed": m.renamed}
                for m in result.moved
            ],
            "skipped": [{"file": m.src.name, "reason": m.reason}
                        for m in result.skipped],
            "too_big": [
                {"file": it.path.name, "size": it.size_text}
                for it in result.too_big
            ],
            "unknown": [
                {"file": it.path.name, "size": it.size_text,
                 "why": it.identity.evidence}
                for it in result.unknown
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
