"""自检用的沙箱：把「程序数据根」整个指到临时目录。

    from tests.sandbox import sandbox

    with sandbox() as (tmp, patched):
        ...任何会读写 userdata/ 的代码...

============================ 为什么必须有这个 ============================
`POST /api/reset` 会走到 `Hub.reset()`，而它里面有这么一行：

    cleared_inbox = ginbox.clear_inbox()        # ← **删用户收件箱里的文件**

参数是默认值，也就是 `paths.inbox_dir()` —— **真实目录**。
而且 2026-09-21 之后它是**整个清空**（认领没认领都删），
所以这一行跑在真实目录上，删的是**用户放在收件箱里的全部东西**。
比原来（只删能配对的副本）**更容易造成损失**。

⚠️ 而这个测试**原来就是这么跑的**：它只给
   `core.session` 和 `settings` 打了补丁，
   **漏了 `ginan.paths`** —— 而 `inbox_dir()`/`data_dir()` 恰好住在那里。
   原注释甚至写着"四个模块都要打补丁"，然后只补了两个。
   **注释描述的是意图，代码才是事实。**

============================ 所以这里是"扫全模块" ============================
不手写模块清单 —— 而是**遍历 `sys.modules`**，
凡是 `ginan_agent.*` 里持有 `default_app_root` 的，一律替换掉。

👉 理由：手写的清单**会漏**（这次就漏了），而漏了不会有任何提示 ——
   测试照跑、照绿，被删的是用户的东西。
   自动扫描的话，将来谁再加一个 import 点，**也自动被盖住**。

============================ ⚠️⚠️ 挪到这里之后补的两处 ============================
2026-10-02 之前，这个沙箱只住在 `check_server.py` 里。而它要挡的事
**不止收件箱** —— 实测（逐套件量了一遍，看哪个会动真实的 `userdata/`）：

  · `check_server.py`  写**真实的 `logs/server.log`**（agent 跑炸那一节
                       会走 `log_exception`，而日志路径是运行时算的）
  · `check_ui_text.py` 在**真实的 `data/`** 里建一个 `.writable-probe`
                       再删掉（`environment._check_dirs` 的可写性探针）

两个都是同一个形状：**套件跑了要碰真实路径的应用代码，却没套沙箱。**
而且两个都不毁数据 —— 所以肉眼看不见。**看得见的才需要挡，看不见的才危险。**

👉 所以沙箱挪到了这里（`tests/` 是个包，两个套件都能 import），
   并且 `check_all.py` 每次跑套件都会核对真实的 `userdata/`
   **一个字节都没动** —— 那条守卫不认清单，认的是结果。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def sandbox() -> Iterator[tuple[Path, list[str]]]:
    """把"程序数据根"指到一个临时目录，yield 出这个临时目录和被打补丁的模块名。

    ⚠️ yield 的是 `(tmp, patched)` —— 第二个是**清单本身**，
       给"沙箱到底盖住了几个模块"这类断言用（见 `check_server` 的 ⓪）。
    """
    # 先把可能持有它的模块**导入进来** —— 没被 import 的模块不在 sys.modules 里，
    # 扫不到。（endpoint 里是函数内 import，模块级测试跑不到。）
    import ginan_agent.core.session  # noqa: F401
    import ginan_agent.ginan.paths  # noqa: F401
    import ginan_agent.settings  # noqa: F401

    tmp = Path(tempfile.mkdtemp(prefix="ginan_selfcheck_"))
    saved: list[tuple[object, object]] = []
    patched: list[str] = []

    for name, mod in list(sys.modules.items()):
        if not name.startswith("ginan_agent"):
            continue
        if getattr(mod, "default_app_root", None) is None:
            continue
        saved.append((mod, mod.default_app_root))
        mod.default_app_root = lambda: tmp       # type: ignore[assignment]
        patched.append(name)

    try:
        yield tmp, patched
    finally:
        for mod, fn in saved:
            mod.default_app_root = fn            # type: ignore[assignment]
        shutil.rmtree(tmp, ignore_errors=True)
