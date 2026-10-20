"""Ginan 领域层 —— 天线型号表（ANTEX 文件）。

============================ 这个模块解决什么问题 ============================
Ginan 要用天线的**相位中心偏差**做改正，而那个数据在 `igs20.atx` 里。

麻烦在于：**型号名必须和表里的一致，一个字符都不能差**。

    ✅ TRM59800.00 NONE
    ❌ TRM59800.00        ← 少了隔离罩代码
    ❌ TRM 59800.00 NONE  ← 多了个空格
    ❌ trimble 59800.00   ← 大小写

用户从设备标签上看到的是 `TRM59800.00`，直接填进去就会失败 ——
而报错是"找不到天线"，看不出是**格式**问题还是"表里没这个型号"。

所以这个模块做两件事：
  ① 告诉他表里到底有哪些（而不是让他猜）
  ② 给个模糊搜索，找相近的

============================ ⚠️ 型号 = 天线名 + 隔离罩 ============================
ANTEX 里每一行的前 20 列是两个字段：

    TRM59800.00     NONE
    0123456789ABCDEFGHIJ
    └─ 0-15: 天线名 ─┘└16-19: 隔离罩┘

**隔离罩（radome）也要写**，而且没装隔离罩时要写 `NONE`。
少了它 Ginan 就找不到 —— 这是最容易漏的一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ATX 每行的标签在第 60 列之后
_LABEL_COL = 60

# 接收机天线和卫星天线的区分依据：
#
# ⚠️ 两者用的是**同一个标签**（`TYPE / SERIAL NO`），只能靠列位置区分。
#
#     接收机：cols 40-49 **空白**
#     卫星：  cols 40-49 有卫星编号（`G032`、`R05` 之类）
#
#     （用"型号名是不是以 BLOCK 开头"来判断是不行的 ——
#       那只是 GPS 卫星的命名习惯，别的系统不这么叫。）
_SAT_ID_COL = slice(40, 50)


@dataclass(frozen=True)
class Antenna:
    """一条天线记录。"""

    name: str        # 型号名，如 `TRM59800.00`
    radome: str      # 隔离罩，如 `NONE`

    @property
    def full(self) -> str:
        """配置文件里要写的完整写法。

        ⚠️ 只要表里有隔离罩字段，就必须带上 ——
           实测 ALIC 的文件头里写的是 `TWIVC6050       NONE`，
           **中间是两个字段，不是名字的一部分**。
        """
        return f"{self.name} {self.radome}".strip()

    def __str__(self) -> str:
        return self.full


# 解析结果缓存。
#
# ⚠️ 这里用模块级缓存是合理的，和"全局工具注册表"那个 bug 不同：
#    · 它只缓存**一个文件的解析结果**，不是可变的业务状态
#    · 键里带了路径和修改时间，文件换了就重新解析
#    · 缓存的内容是只读的（解析完就不变）
#
#    61 MB 的文件每次查询都重新解析要几秒，而用户可能连着问好几个型号。
_CACHE: dict[tuple[str, float], list[Antenna]] = {}


def default_atx() -> Path | None:
    """数据目录里的 ATX 文件。

    ⚠️ 认几个常见名字 —— 用户下载的可能叫 `igs20.atx`、`igs14.atx`，
       也可能被下载器改成了别的名字。
    """
    from . import paths

    data_dir = paths.data_dir()
    if not data_dir.is_dir():
        return None

    candidates = sorted(data_dir.glob("*.atx")) + sorted(data_dir.glob("*.ATX"))
    if not candidates:
        return None

    # 优先 igs20（当前推荐），其次任意一个
    for p in candidates:
        if "igs20" in p.name.lower():
            return p
    return candidates[0]


def load(atx_path: Path | None = None) -> list[Antenna]:
    """读出表里所有接收机天线。读不到就返回空列表。

    ⚠️ **不抛异常** —— 表可能还没下载（用户第一次用）。
       那不是错误，是"还没准备好"，调用方该据此给出不同的提示。
    """
    path = atx_path or default_atx()
    if path is None or not path.is_file():
        return []

    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return []

    if key in _CACHE:
        return _CACHE[key]

    out: list[Antenna] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(line) <= _LABEL_COL:
                    continue
                if line[_LABEL_COL:].strip() != "TYPE / SERIAL NO":
                    continue

                # ⚠️ 卫星天线也用了同一个标签 —— 靠 cols 40-49 区分
                if line[_SAT_ID_COL].strip():
                    continue

                name = line[0:16].strip()
                radome = line[16:20].strip() or "NONE"
                if name:
                    out.append(Antenna(name=name, radome=radome))
    except OSError:
        return []

    # 去重（同一个型号可能有多个序列号的条目）
    seen: set[str] = set()
    uniq: list[Antenna] = []
    for a in out:
        if a.full not in seen:
            seen.add(a.full)
            uniq.append(a)

    _CACHE[key] = uniq
    return uniq


def search(query: str, atx_path: Path | None = None, limit: int = 20) -> list[Antenna]:
    """按关键词找天线。

    ⚠️ 匹配是**大小写不敏感的子串匹配** —— 用户输入的可能不完整
       （只记得 `TRM59800` 或者 `59800`）。

    ⚠️ 结果按"和查询的接近程度"排序：
       完全匹配的前面，前缀匹配其次，其余按名字排。
       用户多半在列表第一眼就能看到自己要的。
    """
    q = (query or "").strip().upper()
    items = load(atx_path)
    if not items:
        return []
    if not q:
        return items[:limit]

    exact: list[Antenna] = []
    prefix: list[Antenna] = []
    contains: list[Antenna] = []

    for a in items:
        full = a.full.upper()
        if full == q or a.name.upper() == q:
            exact.append(a)
        elif a.name.upper().startswith(q) or full.startswith(q):
            prefix.append(a)
        elif q in full:
            contains.append(a)

    return (exact + prefix + contains)[:limit]


def describe(atx_path: Path | None = None) -> dict:
    """给模型/界面看的概况。"""
    path = atx_path or default_atx()
    items = load(path)
    return {
        "atx_file": str(path) if path else None,
        "available": bool(items),
        "count": len(items),
        "note": (
            "配置里的 antenna_type 要用 '型号名 隔离罩' 的形式，"
            "例如 'TRM59800.00 NONE'。没装隔离罩就写 NONE，不能省略。"
        ) if items else (
            "还没下载天线表（igs20.atx）。它是必需文件 —— "
            "没有它 Ginan 无法做天线相位中心改正。"
        ),
    }
