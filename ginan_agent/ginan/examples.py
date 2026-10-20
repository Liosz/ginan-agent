"""读 Ginan 官方示例配置 —— 生成配置用的**模板来源**。

============================ 这个模块解决什么 ============================
`config.build_config` 原来是**手写**一份配置（照着 PPP 拼出来的）。
对 PPP 能跑，对其他工作流是残的 —— 手写 200 个键，错一个的样子是
「配置看着正常、跑起来是 NaN」，而这种错**读代码发现不了**。

实测（2026-09-24）：同一份数据、同一个二进制、同一批先验产品，

    手写配置          → 第 0 个历元 `dpotrf failed`，82 秒/历元，NaN 级联，崩
    官方 pod_example  → 0 次 dpotrf 失败，2.7~3.2 秒/历元，四个产物齐全

差别只在配置。

所以：**官方示例载入成 dict，改文件路径，再 dump 回去。**

============================ ⚠️ 这不是 config.py 拒绝的那个方案 ============================
`config.py` 的模块 docstring 写着「别拿 ppp_example.yaml 当模板，用字符串替换填空」，
三条理由是：静默漏替换、缩进改变语义、无法静态检查。

**那三条针对的是字符串替换，对这里全不成立：**

    · 载入是 `yaml.safe_load` → 得到 dict，不是字符串
    · 结构错 Python 立刻抛异常，不是静默
    · 缩进由 `yaml.safe_dump` 负责，人碰不到
    · 测试可以直接断言 dict，不用先写文件再读回来

============================ ⚠️ 为什么随 app 打包，而不是运行时去下 ============================
它们是纯文本配置（Apache-2.0），打包进来零成本（共 106 KB）。

运行时从 GitHub 拉的话会多一个**恰好在离线/无代理时失效**的失败模式 ——
而这个项目专门为无代理用户保留了 `reduced` 档，说明那种用户是被认真对待的。

⚠️ 这条**不违反** THIRD_PARTY.md 里那条约束：那里禁的是 Ginan 的**二进制**
   （它静态链了 GPL-3 的 JPL Ephemeris）。配置文本不含任何 GPL-3 组件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# =============================================================================
# 出处
# =============================================================================

PROVENANCE = "Ginan HEAD v4.1.4-5033996（提交日期 2026-09-16）"
"""这些文件是从哪个版本的 Ginan 抄来的。

⚠️ 为什么要记：Ginan 升级后配置格式可能变。这份记录让"模板比 pea 旧"
   这件事**可见** —— 不可见的话，症状会是"某个键不生效"，而那种症状
   要查很久。
"""

SOURCE_DIR = "source/exampleConfigs"
"""在上游仓库里的相对路径。"""


EXAMPLE_SHA256: dict[str, str] = {
    "brdc2sp3.yml": "c06e7036735eda2c6148186ceb9ea6c96c6aea989ffed92a42776c9c350d4c2e",
    "compare_orbits.yaml": "4069aa7df99f4eb544e6330ff09b702d5f979542d4cd0411eeb86e7eb3226f49",
    "fit_sp3_pseudoobs.yaml": "8a42d85148d33aeff5f28b684c71a004c616856ec13a80dca7b10582e6bc2b6a",
    "LEO_dynPOD.yaml": "dc01c0eb91b5c2b11a74c3c6608343bc36ff62cc6a6220c45b7dfb7fe300ca4e",
    "LEO_kinPOD.yaml": "1ad9c2beb63e65043f3d1e4b5ea543b0ad5f5d41103c18280cf3b99e067b867f",
    "pod_example.yaml": "450dc3adcea9c4f18fa151453d22c70f2657f9b68e9e31a4610cd858814e2ebf",
    "ppp_example.yaml": "a96a923f322f2f59c867899af35b39f3598457c8a985050dab3a15e29e9c8912",
    "record_snr.yaml": "6ce67cd4c4b6bf9b559e6d753d45dddbab092b63fde2e6f97c549c5e6f8be584",
    "record_streams.yaml": "2fdc24d7ce4428999799608136dc696d10f21b7f8fc8ca27ec25d12f2688973c",
    "rt_ppp_example.yaml": "dc1a765e99320d57409743c4f8699c210377e22d118fd2fec7f3a48ad5c08244",
    "rt_rtk_example.yaml": "0331a9ba7eb9eb476c5a9810c4f850647f645d8cb5fcd185e4d56e450b035369",
    "rtk_example.yaml": "751e4f9c04b8127e78af316eeaa79b0d3e82c3817e90fda7add48a620fe9b477",
    "sbas_example_dfmc.yaml": "30e6e11df59f1026fddfa6a6588e8fe4b19a293d3548ee1b56675d1af54ef617",
    "sbas_example_l1.yaml": "789103c3c88d55da6d0f55cb8cc8a354cb685bfd375de0f921531565ece5b50f",
    "sbas_example_pvs.yaml": "cb6a0551e980aabaaf66decdc779f29e6a2c0af77a5f7dc0bbb73a76e6d02ce5",
    "sbas_example_pvs_rt.yaml": "fdf60f2866376ef295b0382c3c152ed7443abe36340804e7a6dbd17d987026df",
    "slr_pod_with_pseudoobs_gal.yaml": "209e604a250a1e0e8ec946392836a33d83c98082669e5804aab4d063278066ac",
    "slr_pod_with_pseudoobs_lag.yaml": "be5f7835d587fcd6cba4c6178e39ecd9dd7e608f611f4919af03c5ad3af6c19d",
}
"""文件名 → 内容 sha256（**逐字节**，含行尾）。

⚠️ 这里面**没有** `README.md` —— 它不是配置，不参与生成。

⚠️ 改一个文件就必须改这里，否则
   `check_toolchain.test_example_configs_are_present_and_unmodified` 会红。
   **那道守卫的目的不是禁止修改，是让修改必须有意识。**
"""

# 这两个后缀都要收 —— `brdc2sp3.yml` 是 `.yml`，只认 `.yaml` 会漏掉它。
_CONFIG_SUFFIXES = (".yaml", ".yml")


# =============================================================================
# 读
# =============================================================================


def example_dir() -> Path:
    """模板目录。

    ⚠️ 从 `__file__` 算，不依赖当前工作目录 —— 和 `locations.py` 同一个原则。
       app 的进程 CWD 会被各种调用方改（尤其跑 pea 时是工作目录），
       用相对路径就会时灵时不灵。
    """
    return Path(__file__).resolve().parent / "example_configs"


def available() -> list[str]:
    """目录里所有示例的名字，**排序**。

    ⚠️ 排序是刻意的：不排序的话顺序取决于文件系统，
       而守卫里会拿它和哈希表比对 —— 那种比对不该受目录遍历顺序影响。

    ⚠️ 只列 `EXAMPLE_SHA256` 里有的（也就是盘上真有、且被记录过的）。
       不列"盘上多出来的文件" —— 多出来的文件要么是有人手滑放进去的，
       要么是上游新增的；两种都该由守卫报出来，而不是悄悄出现在这里。
    """
    d = example_dir()
    if not d.is_dir():
        return []
    found = []
    for name in EXAMPLE_SHA256:
        if name.endswith(_CONFIG_SUFFIXES) and (d / name).is_file():
            found.append(name)
    return sorted(found)


def load(name: str) -> dict[str, Any]:
    """载入一个示例，返回**全新**的 dict。

    ⚠️⚠️ **每次返回新的 dict，不做缓存、不共享。**
       调用方（`config._build_from_example`）会**就地改**这份 dict
       （替换文件路径、应用档位差量、插测站块）。
       如果缓存同一个对象，第二次调用拿到的就是**被上一次改过的**配置 ——
       症状是「同一个工作流第一次对、第二次不对」，
       而且因为是同一进程内，测试里很难复现。

    ⚠️⚠️ **找不到文件就抛异常，绝不静默退回手写路径。**
       静默退回的话，"工作流声明自己有模板、实际配置却是手写的" ——
       那正是这个模块要消灭的那类不一致。

    ⚠️ **不递归。** `check_recursion.py` 全仓库禁止函数自调用。
    """
    if not name:
        raise ValueError("示例名字是空的")

    d = example_dir()
    # ⚠️ 路径必须落在模板目录**里面**。名字来自 `Workflow.example`（我们自己的字面量），
    #    但一道廉价的防线不值得省 —— 这是"从外部字符串变成文件路径"的经典位置。
    path = (d / name).resolve()
    if not path.is_file() or path.parent != d.resolve():
        raise FileNotFoundError(
            f"找不到示例配置 {name!r}。应该在 {d} 里。"
            f"现有：{', '.join(sorted(p.name for p in d.glob('*') )) or '（空）'}"
        )

    import yaml
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"示例配置 {name!r} 读出来不是字典（是 {type(data).__name__}）—— "
            f"文件可能坏了。"
        )
    return data


def provenance() -> str:
    """一句话说出这批模板的出处 —— 给用户看，也给守卫比对用。"""
    return f"{PROVENANCE}，共 {len(EXAMPLE_SHA256)} 个，来自 {SOURCE_DIR}/"


def sha256_of(name: str) -> str:
    """算盘上那个文件的 sha256（守卫用）。

    ⚠️ 单独一个函数而不是在守卫里手写 —— 守卫要核对的正是
       "盘上的字节"和"表里记的"是否一致，两边必须是同一种算法。
    """
    import hashlib

    path = example_dir() / name
    return hashlib.sha256(path.read_bytes()).hexdigest()
