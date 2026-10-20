"""自检：**不允许任何函数直接调用自己**。

跑法：python scripts/check_recursion.py

============================ 为什么需要这个 ============================
这不是一条"代码风格"规定 —— 它是**同一个事故的第三次表现**，前两次分别长这样：

  ① `server/app.py` 的 `_api_guard`
     我用正则批量改调用点时，把**函数自己的定义体**吃掉了。
     结果：五个接口（写凭据、改设置…）**一个都没校验**，全部裸奔。

  ② `server/app.py` 的 `read_json_body`
     写新 docstring 时，把那行 `body = await request.json()`
     覆写成了 `body = await read_json_body(request)` —— **自己调自己**。
     结果见下。

============================ ② 到底造成了什么 ============================
```
async def read_json_body(request: Request) -> dict:
    try:
        body = await read_json_body(request)   # ← 无限递归
    except Exception:                          # RecursionError 被这行吞掉
        return {}
```

于是**每一个 POST 的请求体都变成 `{}`**：

  · `/api/turn` 拿到空 text → 400「说了句空的」
  · 用户看到的是：**发一句话就红字报错，一个字都发不出去**

⚠️ **为什么它比崩溃难查得多：**

  1. **没有崩溃、没有 500、没有日志** —— `except Exception` 把
     `RecursionError` 一起吞了，而"吞掉"是这里**有意为之**的设计
     （非法 JSON 要返回 400 而不是 500）。
  2. **那个错误值是假的像真的** —— `{}` 是**合法且常见**的输入
     （调用方本来就允许空请求体）。失败的表现和"客户端确实没发东西"
     **一模一样，调用方无从分辨**。
  3. **静态检查看不出来** —— 语法对、类型对、名字对、缩进对。
     `mypy` 不会响，`flake8` 不会响。

  👉 这正是本仓库反复出现的那一类：
     **"看起来对、其实错"，而且它伪装成的是一个平凡情况。**

============================ 判据：只认"裸名字" ============================
`foo(...)` —— 会报。这是自调用唯一可能的样子。

`self.x.foo(...)` / `super().foo(...)` / `obj.foo(...)` —— **不报**。
名字碰巧相同，但它们指向的是**别的对象的方法**。

⚠️ 这条区分是必须的，不是图省事：按"名字相同"去匹配，
在本仓库会得到 **9 个命中，9 个全是误报**
（`self._thread.start()`、`self._agent.reset()`、`self._waiters.clear()`…）。
一个天天喊狼来了的守卫，人会学会直接无视它 —— 那它就等于不存在。

============================ 覆盖范围与边界 ============================
覆盖：仓库里所有 `.py`（包括 `scripts/` 自己）。

⚠️ **不覆盖互相递归**（A 调 B、B 调 A）—— 那不是这次的成因，
   而且真要写出来也是明显的死循环。这里只管"自己调自己"这一种。

⚠️ **如果真的需要递归怎么办**：本仓库目前一处都没有，也不需要有。
   真出现了，正确做法是**回来改这个守卫并在改动里说明理由** ——
   那个"改守卫"的动作本身就是那次评审。
   **故意不提供 `# noqa` 之类的豁免标记**：豁免标记会被随手加，
   而这件事需要的恰恰是"停下来想一想"。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 这些目录里的东西不是我们的代码（虚拟环境、缓存、依赖）。
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}

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
# 检测器 —— 单独抽成函数，好在下面的自检里喂它人造代码
# =============================================================================


def find_self_calls(source: str) -> list[tuple[int, int, str]]:
    """找出 `source` 里所有"函数直接调自己"的地方。

    返回 [(函数定义行, 调用行, 函数名)]，按定义行排序。行号从 1 开始。

    ⚠️ 只认裸名字调用（见文件头"判据"一节）。
    """
    tree = ast.parse(source)
    hits: list[tuple[int, int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            # 关键判断：必须是**裸名字**。属性调用（`x.foo()`）一律不算。
            if isinstance(func, ast.Name) and func.id == node.name:
                hits.append((node.lineno, sub.lineno, node.name))
                break

    return sorted(hits)


# =============================================================================
# ① 守卫先自检 —— 拿一段**确定有 bug** 的代码喂它，必须报红
# =============================================================================


def test_detector_itself() -> None:
    section("① ★ 守卫先自检：喂它一段真的有 bug 的代码，必须报出来")

    # ⚠️⚠️ 这一节是**这个脚本存在的前提**。
    #
    #    一个守卫最危险的失效方式不是"漏报"，而是**悄悄不再工作了** ——
    #    那时它输出的是"✓ 没问题"，和"代码真的没问题"**长得一模一样**。
    #    （跟 `check_all.py` 里漏登记一个套件、`app.routes` 里遍历到 0 条路由，
    #      是完全同一种失败：**绿色的假象**。）
    #
    #    所以在扫真代码之前，先拿两段**人造**的代码验证检测器本身：
    #    一段必须报，一段必须不报。
    buggy = '''
async def read_json_body(request):
    try:
        body = await read_json_body(request)
    except Exception:
        return {}
'''
    hits = find_self_calls(buggy)
    check(len(hits) == 1, f"★ 检测器认得出来 `read_json_body` 那个事故（命中 {len(hits)} 处）")
    if hits:
        check(hits[0][2] == "read_json_body", f"  报出来的名字对：{hits[0][2]}()")

    # 反例：这几种"名字相同但**不是**自己"的写法，一个都不许报 ——
    #       它们正是那 9 个误报的形状。
    innocent = '''
def start(self):
    self._thread.start()

def reset(self):
    self._agent.reset()

def clear(self):
    self._waiters.clear()

class Sub(Base):
    def __init__(self, x):
        super().__init__(x)

def inner_shadow():
    def inner_shadow():
        return 1
'''
    false_hits = find_self_calls(innocent)
    for line, _, name in false_hits:
        print(f"      ⚠️ 误报：{name}() 第 {line} 行")
    check(
        not false_hits,
        "★ `self.x.x()` / `super().x()` / 同名嵌套函数**都不算**（这是那 9 个误报的形状）",
    )


# =============================================================================
# ② 扫全仓库
# =============================================================================


def test_repo_is_clean() -> None:
    section("② ★ 全仓库没有一个函数调用自己")

    files: list[Path] = []
    for p in sorted(ROOT.rglob("*.py")):
        if SKIP_DIRS & set(p.parts):
            continue
        files.append(p)

    check(len(files) >= 20, f"扫到了 {len(files)} 个 .py 文件")

    bad: list[str] = []
    unparsed: list[str] = []

    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        try:
            src = p.read_text(encoding="utf-8")
        except OSError as exc:
            unparsed.append(f"{rel}（读不了：{exc}）")
            continue
        try:
            hits = find_self_calls(src)
        except SyntaxError as exc:
            # ⚠️ 解析不了要**报失败**，不能跳过 ——
            #    跳过的意思是"这个文件没被检查过"，
            #    而它可能正是有问题的那个。
            unparsed.append(f"{rel}（语法错：{exc}）")
            continue
        for def_line, call_line, name in hits:
            bad.append(f"{rel}:{def_line}  {name}() 在第 {call_line} 行调用了自己")

    for line in unparsed:
        print(f"      ⚠️ 没检查成：{line}")
    check(not unparsed, "每个文件都真的被检查过了（解析失败不算通过）")

    for line in bad:
        print(f"      ⚠️ {line}")
    check(
        not bad,
        f"★ 没有自调用（{len(files)} 个文件全过）—— "
        "有的话就是请求体被吞、或者守卫被吃掉的那种事故",
    )


# =============================================================================


def main() -> int:
    print("=" * 60)
    print("自调用守卫")
    print("=" * 60)
    print("⚠️ 只做静态分析：不导入、不执行、不联网。")

    test_detector_itself()
    test_repo_is_clean()

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
