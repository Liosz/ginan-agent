"""自检：本地服务 —— 鉴权、事件广播、新任务。

跑法：python scripts/check_server.py

============================ 为什么需要这个 ============================
这个服务能**起子进程、下文件、读写用户的磁盘**。
任何用户访问的网页都可能去戳它（DNS rebinding / CSRF）。

而它的防线只有两道：token 和 Host 校验。

⚠️ 实测漏过：`_guard` 的注释写着"每个接口都要过这一关"，
   但 `/api/credentials`(GET+POST)、`/api/settings`(GET+POST)、
   `/api/inbox`、`/api/inbox/claim`、`/api/open-folder`
   **八次调用、五个接口，一次都没校验过** ——
   而漏掉的恰好是**写凭据**和**改设置**这两件最不该漏的事。

   👉 所以这里的第一条断言是**结构性的**：
      "每个 /api 路由都必须挂着校验依赖"。
      它不测行为，测的是"有没有可能被遗忘"。

============================ 不联网 ============================
用 FastAPI 的 TestClient，全在进程内。不启端口、不联网、不花钱。

⚠️ TestClient 默认发 `Host: testserver`，而我们的 Host 校验会把它挡掉 ——
   所以必须传 `base_url="http://127.0.0.1"`。
   不传的话**每一个请求都是 403**，测试看起来像"应用坏了"。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.sandbox import sandbox  # noqa: E402  （sys.path 上面刚铺好）

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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
# ⓪ 沙箱 —— 自检**绝不能碰用户真正的数据**
# =============================================================================
#
# ⚠️ `sandbox()` 原来就定义在**这里**。2026-10-02 挪去了 `tests/sandbox.py`：
#    要用它的不止这一个套件（`check_ui_text` 也要 —— 它会跑真环境检查，
#    在真实的 `data/` 里建可写性探针）。**说明也跟着搬过去了，这里是引用不是副本。**




def test_sandbox_covers_everything() -> None:
    section("⓪ ★ 自检跑在沙箱里 —— 真实收件箱**一个字节都不许动**")

    # ⚠️⚠️ 这一节守的是**自检自己**，而且它守的是一件比"代码有 bug"严重得多的事：
    #     **测试删用户的数据。**
    #
    #     一个会误删用户文件的测试，比没有测试更糟 ——
    #     因为它不但没防住问题，还亲手制造了一个，而且**没人会怀疑测试**。

    from ginan_agent.ginan import paths as gpaths

    # 真实路径先记下来（**在**打补丁之前读）
    real_inbox = gpaths.inbox_dir().resolve()
    real_data = gpaths.data_dir().resolve()

    with sandbox() as (tmp, _):
        inside_inbox = gpaths.inbox_dir().resolve()
        inside_data = gpaths.data_dir().resolve()

        check(
            inside_inbox != real_inbox,
            f"★ 收件箱被改到了沙箱（真实的是 {real_inbox.name}）",
        )
        check(
            tmp in inside_inbox.parents or inside_inbox == tmp,
            f"★ 收件箱确实在临时目录里：{inside_inbox}",
        )
        check(
            inside_data != real_data and (tmp in inside_data.parents),
            "★ 数据目录也在临时目录里",
        )

    # 出了沙箱要还原 —— 否则后面的用例会跑到临时目录里去，
    # 而临时目录已经被删了，失败的样子会离病因很远。
    check(
        gpaths.inbox_dir().resolve() == real_inbox,
        "★ 退出沙箱后路径还原（否则后面的用例会在已删除的目录上跑）",
    )


# =============================================================================
# ① 鉴权：结构 + 行为
# =============================================================================


def test_results_endpoint_shape() -> None:
    section("★ /api/results —— 结果面板的数据源")

    # ⚠️ 这个接口**不经过模型**（同 /api/workspace、/api/environment）：
    #    "结果长什么样"是确定性数据，让模型转述既慢又可能说错 ——
    #    而**图尤其不能靠模型转述**。
    #
    # ⚠️ 这里只测**形状与边界**（有没有产物两条路都要能走通）。
    #    "曲线画出来对不对"在 `check_files` 里测 `posfile.plot_series`；
    #    "画出来长什么样"只能靠截图（自检测不出线的位置）。
    from fastapi.testclient import TestClient

    from ginan_agent.server.app import create_app

    app = create_app(token="T")
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/api/results", headers={"x-token": "T"})
    check(r.status_code == 200, f"有 token → 200（实际 {r.status_code}）")

    d = r.json()

    # ---- 还没跑：不报错，给一句人话 ----
    check(d.get("has_result") is False, "没跑过时 has_result = False")
    why = d.get("why_not") or ""
    check(bool(why), f"★ 而且给了一句人话：{why[:40]}")
    check("**" not in why and "`" not in why,
          "★★ 那句话是**纯文本** —— 它直接进侧栏的 textContent，"
          "带 Markdown 会露出字面星号")

    # ---- 对照：必须**没有** series / 也必须没有半截的图 ----
    check(d.get("series") == [], f"没结果时 series 是空表（实际 {d.get('series')!r}）")

    # ---- 无 token：仍然要挡住（别因为它是"读接口"就松掉） ----
    check(
        c.get("/api/results").status_code == 403,
        "无 token → 403（新接口不能漏挂鉴权）",
    )



def test_corrections_endpoint_shape() -> None:
    section("★ /api/corrections —— 设置里那 9 个开关的数据源")

    # ⚠️ 这个接口也**不经过模型**：用户要照着它决定"要不要下那 23 MB"，
    #    让模型转述既慢又可能说错一个文件大小。
    #
    # ⚠️ 这里测的是**形状和边界**；"开关真的进了配置"在
    #    `check_toolchain` 的 ㉖ 那一节测（那才是要紧的地方）。
    from fastapi.testclient import TestClient

    from ginan_agent.server.app import create_app

    app = create_app(token="T")
    c = TestClient(app, base_url="http://127.0.0.1")

    check(c.get("/api/corrections").status_code == 403,
          "无 token → 403（新接口不能漏挂鉴权）")

    r = c.get("/api/corrections", headers={"x-token": "T"})
    check(r.status_code == 200, f"有 token → 200（实际 {r.status_code}）")
    d = r.json()

    from ginan_agent.ginan import corrections as gcorrections

    items = d.get("items") or []
    check(len(items) == len(gcorrections.CORRECTIONS),
          f"★ 9 项都在（实际 {len(items)}）")
    check(bool(d.get("profile_label")),
          f"★ 报出默认来自哪个档位（{d.get('profile_label')!r}）")

    for it in items:
        for key in ("key", "label", "what", "effect", "on",
                    "overridden", "file_info", "blocked", "measured"):
            check(key in it, f"  「{it.get('key')}」有 {key}")
        # ⚠️ 文案直接进 textContent / innerHTML —— 带 Markdown 会露馅
        for field in ("label", "what", "effect"):
            v = it.get(field) or ""
            check("**" not in v and "`" not in v,
                  f"★ 「{it.get('key')}」的 {field} 是纯文本")
        check(isinstance(it.get("file_info"), list),
              f"  「{it.get('key')}」的 file_info 是列表")

    # ---- 对照：要文件的那几项，必须**说得出文件在不在** ----
    with_files = [i for i in items if i["file_info"]]
    check(len(with_files) >= 5,
          f"★ 至少 5 项要说清文件在不在（实际 {len(with_files)}）")
    for it in with_files:
        for f in it["file_info"]:
            check("have" in f and "name" in f,
                  f"  「{it['key']}」的文件项有 name/have（{f.get('name')}）")
            if f.get("have"):
                check(isinstance(f.get("mb"), (int, float)),
                      f"  ★ 已经有的文件要给出大小（{f.get('name')}）")

    # ---- 对照：不认识的键不许让接口炸 ----
    from ginan_agent.ginan import corrections as gc
    check(gc.clean({"不存在的项": True}) == {},
          "★ 对照：不认识的键被丢掉，不报错（旧 config.json 也能读）")

    # ---- ★★ 存设置时要**同步到当前工作区** ----
    #
    # ⚠️⚠️ 生成配置读的是 `ws.corrections`（见 agent/workspace.py），
    #    不是设置文件。两者之间那一步（POST /api/settings → ws）**断了
    #    不会报任何错** —— 用户在设置里勾了一项，跑出来的配置里没有它，
    #    而他会以为"这一项没用"。这个仓库已经踩过好几次同类（声明了字段、
    #    没有任何一方负责写它）。
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from ginan_agent.server.app import Hub

    hub2 = Hub()
    app2 = create_app(hub2, token="T")
    c2 = TestClient(app2, base_url="http://127.0.0.1")
    check(hub2.workspace is not None, "（夹具：新 Hub 自带一个工作区）")

    # ⚠️ 真的写盘 —— `save_settings` 会写 config.json，所以把配置目录
    #    指到临时目录，别动用户真实的设置。
    real = _Path(tempfile.mkdtemp(prefix="ga-corr-")) / "config.json"
    import ginan_agent.settings as gsettings
    orig_path = gsettings.config_path

    # ⚠️⚠️ 给**用户真实那份**拍个指纹，这一节跑完再核一遍。
    #    见下面「这一段必须在 finally 之前」那段 —— 这个指纹就是它的守卫。
    user_cfg = orig_path()
    user_bytes = user_cfg.read_bytes() if user_cfg.is_file() else None

    try:
        gsettings.config_path = lambda: real
        r2 = c2.post("/api/settings", headers={"x-token": "T"},
                     json={"corrections": {"ocean_load": True,
                                           "不认识的项": True}})
        check(r2.status_code == 200, f"保存设置 → 200（实际 {r2.status_code}）")
        check(hub2.workspace.corrections == {"ocean_load": True},
              f"★★ 保存后工作区里**真的**拿到了那一项"
              f"（实际 {hub2.workspace.corrections}）")
        # ⚠️ 反面对照：不认识的键不许混进工作区（它会让配置生成时的
        #    `resolve` 忽略它，但会让"用户改了什么"这句话说不准）
        check("不认识的项" not in hub2.workspace.corrections,
              "★ 不认识的键没跟着同步过去（先在 clean() 里就洗掉了）")
        check(_json.loads(real.read_text(encoding="utf-8"))
              .get("corrections") == {"ocean_load": True},
              "★ 也真的落盘了（重开程序还在）")

        # ---- 对照：界面上"归位"的那条路 ----
        #    恢复默认 = 提交空覆盖，不是"把 9 项都设成 false"
        #
        # ⚠️⚠️ 这一段**必须在 `finally` 之前** —— 它也是一次
        #    `POST /api/settings`，**会写盘**。写在 `finally` 后面的话，
        #    它写的就是**用户真实的 config.json**。
        #
        #    实测（2026-10-02）：先把真实那份的 `corrections` 填成
        #    `{"ocean_load": true, "solid": true}`，跑一遍 check_server ——
        #    出来是 `{}`。**用户的设置被静默清空。**
        #
        #    ⚠️ 它一直没露出来的原因很典型：那台机器上 `corrections`
        #       本来就是空的，所以写进去的内容和原来**一模一样**，
        #       只有 mtime 变了。（我是先看见 mtime 变了才去查的。）
        #       而用户只要在设置里勾过任何一项，下一次自检就会把它抹掉。
        r3 = c2.post("/api/settings", headers={"x-token": "T"},
                     json={"corrections": {}})
        check(r3.status_code == 200 and hub2.workspace.corrections == {},
              "★ 提交空覆盖 = 恢复默认（工作区里也清空了）")
    finally:
        gsettings.config_path = orig_path

    # ★★ 这一节的**底线**：用户真实的设置文件，一个字节都不许动。
    #    它是上面那个"必须在 finally 之前"的守卫 —— 谁把那一段挪出去，
    #    这里就红。
    after = user_cfg.read_bytes() if user_cfg.is_file() else None
    check(
        after == user_bytes,
        "★★ 跑完这一节，用户真实的 config.json **一个字节都没动**"
        + ("" if after == user_bytes else "（它被写过！）"),
    )


def test_residual_endpoints_shape() -> None:
    section("★ /api/residuals + /api/residual_figures —— 残差那一屏的数据源")

    # ⚠️ 同 `/api/results`：**不经过模型**。而且这两个接口只读那个几百 KB 的
    #    派生文件 —— 用户把几百 MB 的 `.TRACE` 删掉之后，它们照常工作。
    from fastapi.testclient import TestClient

    from ginan_agent.server.app import create_app

    app = create_app(token="T")
    c = TestClient(app, base_url="http://127.0.0.1")

    check(c.get("/api/residuals").status_code == 403,
          "无 token → 403（新接口不能漏挂鉴权）")
    check(c.get("/api/residual_figures").status_code == 403,
          "  （图那个也一样）")

    d = c.get("/api/residuals", headers={"x-token": "T"}).json()
    check(d.get("has_result") is False,
          "还没跑过残差时 has_result = False（**不报错**）")

    f = c.get("/api/residual_figures", headers={"x-token": "T"}).json()
    check(f.get("ok") is False, "还没有残差时图的接口 ok = False")
    why = f.get("why_not") or ""
    check(bool(why), f"★ 而且给了一句人话：{why[:40]}")
    check("**" not in why and "`" not in why,
          "★★ 那句话是**纯文本** —— 它直接进浮层的 textContent，"
          "带 Markdown 会露出字面星号")


def test_residual_bundle_round_trip() -> None:
    section("★★ 残差派生文件：落盘 → 读回 → 出图（不碰那份几百 MB 的）")

    # ⚠️ 落盘/读回这一对是**跨进程**的（工具写、服务端读），所以不能只测"函数能跑"。
    #    这里真的写一个临时目录，再走 `residuals.load_for_figures` 读回来。
    import datetime as dt
    import tempfile
    from pathlib import Path as _P

    from ginan_agent.ginan import charts, residuals as gresid
    from ginan_agent.ginan import tracefile as T

    t0 = dt.datetime(2026, 9, 2)
    pts = [T.Resid(t=t0 + dt.timedelta(seconds=30 * i), iteration=-1,
                   meas="PHAS_MEAS", sat=f"G{i % 9 + 1:02d}", recv="ALIC",
                   sig="L1W", prefit=0.0, postfit=0.01 * ((i % 7) - 3),
                   sigma=0.01)
           for i in range(400)]
    rf = T.ResidFile(path=_P("NET_TEST.TRACE"), points=pts, n_lines=400,
                     blocks=("/PPP",))
    summary = T.summarize(rf)
    v = T.verdict(summary)
    sampled = {m: [{"t": p.t.isoformat(sep=" ", timespec="seconds"),
                    "sat": p.sat, "recv": p.recv, "sig": p.sig,
                    "prefit": 0.0, "postfit": p.postfit, "sigma": p.sigma}
                   for p in charts._resid_sample(rf, m)] for m in rf.kinds}

    with tempfile.TemporaryDirectory() as td:
        wd = _P(td)
        d = gresid.dir_of(wd)
        d.mkdir(parents=True)
        (d / gresid.BUNDLE_NAME).write_text(
            __import__("json").dumps(
                T.bundle(rf, summary, v, sampled=sampled), ensure_ascii=False),
            encoding="utf-8")

        ui = gresid.summary_for_ui(wd)
        check(bool(ui), "★ 面板摘要读得回来")
        check(ui["count"] == 400,
              f"★★ 条数是**原始**的 400，不是抽稀后的 "
              f"{len(sampled.get('PHAS_MEAS', []))}")

        loaded = gresid.load_for_figures(wd)
        check(loaded is not None, "★ 画图那一份也读得回来")
        rf2, s2, v2 = loaded
        check(rf2.count == 400, "★ 读回来的总条数还是原始的（用 bundle 里那个数）")
        figs = charts.resid_figures(rf2, s2)
        check(bool(figs), f"★ 读回来后画得出图（{sorted(figs)}）")

    # ---- 对照：没有派生文件时**不抛异常**（还没跑过是正常状态）----
    with tempfile.TemporaryDirectory() as td:
        check(gresid.summary_for_ui(_P(td)) is None,
              "★ 没有残差时返回 None，不抛")
        check(gresid.load_for_figures(_P(td)) is None, "  （图那份也一样）")

def test_guard_is_structural() -> None:
    section("① ★ 每个 /api 接口都挂着校验（结构保证，不靠人记得）")

    from ginan_agent.server.app import create_app

    app = create_app(token="T")

    # ⚠️ FastAPI 0.141 起，`include_router` 进来的路由**不会摊平**到
    #    `app.routes` 里，而是包在一个 `_IncludedRouter` 节点里。
    #    所以要顺着它取真正的 APIRouter —— 直接遍历 app.routes 会一个都找不到，
    #    而那种"检查了 0 个路由"的测试**永远是绿的**。
    routers = []
    for r in app.routes:
        orig = getattr(r, "original_router", None)
        if orig is not None:
            routers.append(orig)

    api_routes = []
    for rt in routers:
        api_routes += [x for x in rt.routes if getattr(x, "path", "").startswith("/api/")]

    check(len(api_routes) >= 15, f"找到了 {len(api_routes)} 个 /api 路由")

    naked = [r.path for r in api_routes if not getattr(r, "dependencies", None)]
    if naked:
        for p in naked:
            print(f"      ⚠️ 没挂校验：{p}")
    check(
        not naked,
        f"★ 没有裸奔的 /api 接口（{len(api_routes)} 个全挂上了）",
    )


def test_guard_enforces() -> None:
    section("①b ★ 无 token / 错 token / 恶意 Host 一律 403")

    from fastapi.testclient import TestClient

    from ginan_agent.server.app import create_app

    app = create_app(token="SECRET")
    # ⚠️ base_url 必须是 127.0.0.1，否则 Host 校验把每个请求都挡掉 ——
    #    那样"该过的没过"会让整个测试失去意义。见文件头的说明。
    c = TestClient(app, base_url="http://127.0.0.1")

    check(c.get("/api/ping").status_code == 403, "无 token → 403")
    check(
        # ⚠️ 值必须是 ASCII —— HTTP 头按 latin-1 编码，
        #    塞中文进去 httpx 直接抛 UnicodeEncodeError，
        #    而那个报错离"我在测鉴权"这件事很远。
        c.get("/api/ping", headers={"x-token": "wrong-token"}).status_code == 403,
        "错 token → 403",
    )
    check(
        c.get("/api/ping", headers={"x-token": "SECRET"}).status_code == 200,
        "对 token（header）→ 200",
    )
    check(
        c.get("/api/ping?x_token=SECRET").status_code == 200,
        "对 token（query，SSE 只能走这条）→ 200",
    )
    check(
        c.get(
            "/api/ping?x_token=SECRET", headers={"host": "evil.example"}
        ).status_code
        == 403,
        "★ Host 不是本机 → 403（防 DNS rebinding）",
    )

    # ⚠️ 这五个是**实测漏掉过**的。它们比别的更需要保护：
    #    能写凭据、能改设置、能打开文件夹。
    for ep in (
        "/api/credentials",
        "/api/settings",
        "/api/inbox",
        "/api/workspace",
        "/api/environment",
    ):
        check(c.get(ep).status_code == 403, f"★ {ep} 无 token → 403（以前完全没拦）")

    check(
        c.post("/api/inbox/claim", json={}).status_code == 403,
        "★ POST /api/inbox/claim 无 token → 403",
    )
    check(
        c.post("/api/open-folder", json={"which": "data"}).status_code == 403,
        "★ POST /api/open-folder 无 token → 403",
    )

    # 首页必须**不需要** token —— 前端要从它里面拿 token。
    check(c.get("/").status_code == 200, "首页不需要 token（否则前端拿不到它）")


def test_sse_yield_is_inside_the_loop() -> None:
    section("①c ★ SSE 的 yield 必须在循环**里面**（缩进事故，踩过一次）")

    # ⚠️⚠️ 这是一次**真实事故**的回归守卫，事故很值得记。
    #
    #    给 `while True:` 外面套一层 `try:` 的时候，我漏了把循环体
    #    后面的那段也一起缩进去 —— 于是
    #
    #        yield json.dumps(item, ensure_ascii=False)
    #
    #    跑到了 **`while True:` 外面**，成了永远走不到的死代码。
    #
    #    现象（在用户那边）：
    #        · 发消息后永远显示"正在等模型回应"，再也没有下文
    #        · 模型问了问题，但**界面上没有按钮**（ask_user 事件被吞了）
    #        · 点了停止，按钮永远停在"正在停止"（turn_end 事件被吞了）
    #
    #    而服务端这边**一切正常**：事件发得出去、也确实进了订阅者的队列
    #    （排查计数 published / delivered 都是 43），就是一条都没转发。
    #
    #    ⚠️ **Python 不会为这个报任何错** —— `while True` 不会退出，
    #       外面那行只是永远不执行。静态检查也看不出来。
    #
    #    ⚠️ 为什么是"结构检查"而不是行为检查：
    #       `TestClient` 的流式响应是**攒完再给**的，驱动不了 SSE ——
    #       实测它收到的永远是 0 条。要行为测试就得真起一个服务进程，
    #       而自检约定是"不联网、不起服务"。
    #
    #       所以这里退而求其次：**直接断言缩进关系**。
    #       它挡的正是"缩进又没对齐"这一种 —— 而那就是这次的全部成因。
    import re

    src = (ROOT / "ginan_agent" / "server" / "app.py").read_text(encoding="utf-8")
    lines = src.splitlines()

    def indent_of(pattern: str) -> int | None:
        for ln in lines:
            if pattern in ln:
                return len(ln) - len(ln.lstrip())
        return None

    i_while = indent_of("while True:")
    i_yield = indent_of("yield json.dumps(item")
    i_done = indent_of('yield {"event": "done"')

    check(i_while is not None, "找到了 SSE 的 while True")
    check(i_yield is not None, "找到了转发事件的 yield")
    if i_while is not None and i_yield is not None:
        check(
            i_yield > i_while,
            f"★ 转发事件的 yield **在循环里面**"
            f"（while 缩进 {i_while}，yield 缩进 {i_yield}）—— "
            "跑到外面的话每一条事件都会被读走然后丢掉",
        )
    if i_while is not None and i_done is not None:
        check(
            i_done > i_while,
            f"★ done 那条 yield 也在循环里面（缩进 {i_done}）",
        )


def test_frontend_renders_turn_end_text() -> None:
    section("①d ★ `turn_end` 带的话，前端必须真的画到屏幕上")

    # ⚠️⚠️ 这是一次真实事故的守卫，而且它和 ①c 那次是**同一类**：
    #    服务端老老实实发了，**前端没接**。
    #
    #    事故现场（app.js）：
    #
    #        case 'turn_end':
    #          endTurn();        ← 就这一行，`ev.text` 从来没被读过
    #          break;
    #
    #    模型的字走 `text_delta` 一条条画上去，这条路没问题。
    #    但有些话**根本不是模型说的**，是服务端加的：
    #
    #        · 撞到单轮步数上限（"已达到最大步数…"）
    #        · 用户点了停止（"（已停止。…）"）
    #
    #    这两种情况**一条 text_delta 都没有** —— 那句话只存在于
    #    `turn_end.text` 里。前端不读它，它就等于没说。
    #
    #    用户的实际遭遇：跑完一轮真实的活（下载了 6 个文件、全成功），
    #    然后**屏幕上一个字都没有**。他的原话是"下载成功后什么都不显示"。
    #
    #    ⚠️ 为什么是结构检查：这东西要真的驱动一遍前端才知道有没有画出来，
    #       而那需要浏览器（自检约定是"不联网、不起服务"）。
    #       退而求其次 —— 直接断言那段代码里**用到了 `ev.text`**。
    #       它挡的正是"这个字段没人用"这一种，而那就是这次的全部成因。
    src = (ROOT / "ginan_agent" / "frontend" / "app.js").read_text(encoding="utf-8")

    start = src.find("case 'turn_end':")
    check(start != -1, "找到了前端的 turn_end 分支")

    if start != -1:
        # 截到下一个 case 为止 —— 只看这个分支自己的代码
        rest = src[start:]

        def next_case(offset: int) -> int:
            """下一个 case 的位置（排除开头自己那个）。"""
            return rest.find("case '", offset)

        end = next_case(len("case 'turn_end':"))
        block = rest if end == -1 else rest[:end]

        check("ev.text" in block, "★ 这个分支里用到了 ev.text（不然那句话没人画）")
        check(
            "sawText" in block,
            "★ 而且判断了『这一轮有没有吐过字』—— 不判断的话，"
            "正常回答会被画两遍（turn_end.text 是同一段话的完整版）",
        )
        if "ev.text" not in block:
            print("      ⚠️ 当前那个分支长这样：")
            for ln in block.splitlines()[:12]:
                print(f"         {ln}")


# =============================================================================
# ② 事件广播
# =============================================================================


def test_events_are_broadcast() -> None:
    section("② ★ 事件是广播的，不是'谁抢到算谁的'")

    # ⚠️ 原来 Hub 只有一个 `queue.Queue`，所有 SSE 连接都从它取 ——
    #    而队列的每一项**只会被一个取走者拿到**。
    #    开第二个标签页、或者刷新时旧连接没断干净，两个连接就互相偷事件。
    #    现象是"消息时有时无"，离病因十万八千里。
    from ginan_agent.server.app import Hub

    def take(q):
        """取一条，取不到返回 None。

        ⚠️ 不能用 `get_nowait()` —— 队列空时它**抛 Empty**，
           于是"没收到"表现为**整个自检崩掉**，后面的用例一条都不跑。
           而真正需要的是：这一条失败、其余照跑。
           **守卫要能报错，不能自己死掉。**
        """
        import queue as _q

        try:
            return q.get_nowait()
        except _q.Empty:
            return None

    hub = Hub()
    a = hub.subscribe()
    b = hub.subscribe()

    hub.publish({"type": "hello"})
    check(take(a) == {"type": "hello"}, "订阅者 A 收到了")
    check(take(b) == {"type": "hello"}, "★ 订阅者 B 也收到了（广播）")

    # 后加入的不该收到"历史"
    hub.unsubscribe(a)
    hub.unsubscribe(b)
    c_ = hub.subscribe()
    check(take(c_) is None, "★ 退订后新订阅的收不到旧事件（没有回放）")

    hub.unsubscribe(c_)
    hub.unsubscribe(c_)          # 重复退订不该报错
    check(True, "重复退订不报错（退出路径要能多次调用）")


def test_settings_returns_known_models() -> None:
    section("⑱ ★★ 前后端字段约定：`known_models` 必须真的有人给")

    # ⚠️⚠️ 又是「**后端从不返回某个键，调用方一直在读**」那一类 ——
    #    `.POS` 的 `solution` 键刚出过一次一模一样的。
    #
    #    前端 `app.js` 一直在读：
    #         fillModelSelect(s.known_models || [], s.model || '');
    #
    #    而它的提供方**一个都没有**：
    #      · `settings.Settings` 里有个同名字段，**从来没人赋值**
    #      · 而且 `to_json()` 还专门把它 pop 掉
    #      · `/api/models` 查到了模型，返回给调用方之后**就扔了**
    #
    #    → 永远 `undefined` → **每次打开设置，模型下拉框都是空的**，
    #      用户得重新点一次「获取列表」（而查模型要联网，可能慢、可能失败）。
    #
    # ⚠️ 判据要**端到端**：单独看 `/api/models`（返回 models）和
    #    `/api/settings`（返回 config）两边都是对的 ——
    #    错的是**它们之间的约定**，只有把两次调用连起来才看得见。
    from fastapi.testclient import TestClient

    from ginan_agent import settings as _settings
    from ginan_agent.server.app import Hub, create_app

    real_fetch = _settings.fetch_models
    _settings.fetch_models = lambda base_url, key: ["model-a", "model-b"]
    try:
        with sandbox():
            app = create_app(Hub(), token="S")
            c = TestClient(app, base_url="http://127.0.0.1")
            H = {"x-token": "S"}

            # 先看一眼"还没查过"时的样子
            r0 = c.get("/api/settings", headers=H)
            check(r0.status_code == 200, f"GET /api/settings 通（{r0.status_code}）")
            check(
                "known_models" in r0.json(),
                f"★★ 这个键**存在**（哪怕是空的）——"
                f"实际键：{sorted(r0.json())[:8]}…",
            )

            # 用户点了「获取列表」
            r1 = c.post("/api/models", headers=H,
                        json={"base_url": "https://example.invalid", "api_key": "k"})
            check(r1.status_code == 200, f"POST /api/models 通（{r1.status_code}）")
            check(r1.json().get("models") == ["model-a", "model-b"], "查到了两个模型")

            # 再打开设置 —— 下拉框该有东西了
            r2 = c.get("/api/settings", headers=H)
            got = r2.json().get("known_models")
            check(
                got == ["model-a", "model-b"],
                f"★★ 关掉再打开设置，模型列表**还在**（实际 {got}）"
                "—— 原来这里永远是 undefined",
            )
    finally:
        _settings.fetch_models = real_fetch


def test_error_turn_still_ends() -> None:
    section("⑰ ★★ 一轮**出错**之后也必须发 TURN_DONE（没有它前端收不了工）")

    # ⚠️⚠️ 这条守的是**界面上最难看的一种死法**。
    #
    #    服务端的形状是：
    #
    #         try:     for event in agent.run(...): publish(event)
    #         except:  publish({"type": "error", ...})   ← 只推 error
    #         finally: publish(TURN_DONE)                ← 前端收到 `done`
    #
    #    异常一抛，生成器就断了 —— **`turn_end` 永远不来**。
    #    而前端 `turn_end` 是**唯一**调 `endTurn()` 的地方：
    #
    #        发送按钮消失、输入框锁死（发不出第二句）
    #        「停止」点一下会把自己 disabled 掉（连停都停不了）
    #        「正在停止……」永久留在屏幕上
    #
    #    ⚠️ 触发它的正是**首次使用最常见的路径**：没配 API Key 就发了一句话。
    #
    #    ⚠️ 为什么这条必须**行为测**：
    #       前端那边已经有一条结构性断言（`check_ui_text` ⑯，看 `done`
    #       里有没有 `endTurn()`）。但那只是"前端以为服务端会发 done"。
    #       **服务端到底发不发**，只有真跑一轮才知道 ——
    #       两边各测各的，中间那个约定没人看着。
    from ginan_agent.core import Agent
    from ginan_agent.server.app import TURN_DONE, Hub
    from tests.fakes.provider import ExplodingProvider

    # ⚠️⚠️ **套沙箱** —— 这一轮**一定会炸**，而 `Hub._run` 出错时会走
    #    `app.log_exception()`，把 traceback 写进 `logs/server.log`。
    #    那个路径是**运行时**算出来的（`log_path()` → `paths.logs_dir()`），
    #    所以不套沙箱，写的就是**用户真实的日志文件**。
    #
    #    实测：2026-10-02 之前，每跑一次 check_server，真实日志就长 886 字节。
    #    （逐套件量过：14 个套件里只有这一个会写。其余都干净。）
    with sandbox():
        hub = Hub()
        q = hub.subscribe()

        # ⚠️ 直接塞一个"一调用就炸"的 provider 进 agent ——
        #    绕过真的 `_build_provider()`（那要读设置、要联网）。
        hub._agent = Agent(
            ExplodingProvider(RuntimeError("没有找到 API KEY")), "系统提示", []
        )

        check(hub.start("你好"), "这一轮起来了")
        hub._thread.join(timeout=15)

        seen: list = []
        while True:
            try:
                seen.append(q.get_nowait())
            except Exception:  # noqa: BLE001  —— 队列空了
                break

        kinds = [
            "TURN_DONE" if item is TURN_DONE else (item or {}).get("type")
            for item in seen
        ]
        check("error" in kinds, f"★ 出错时推了 error 事件（拿到 {kinds}）")
        check(
            bool(kinds) and kinds[-1] == "TURN_DONE",
            f"★★ **最后一条是 TURN_DONE**（拿到 {kinds}）—— "
            "没有它前端会永久停在'处理中'",
        )
        check(
            "turn_end" not in kinds,
            "★★ 而出错那条路上确实**没有** turn_end —— "
            "所以说前端的 `done` 兜底是必需的不是多余的",
        )
        hub.unsubscribe(q)


# =============================================================================
# ③ 新任务：真的清历史
# =============================================================================


def test_post_body_actually_arrives() -> None:
    section("③ ★ POST 的请求体必须真的送到服务端（不是空字典）")

    # ⚠️⚠️ 这是一次真实事故的回归守卫，事故现场见 `app.py` 的 `read_json_body`：
    #
    #        async def read_json_body(request):
    #            try:
    #                body = await read_json_body(request)   # ← 自己调自己
    #            except Exception:
    #                return {}
    #
    #    无限递归 → `RecursionError` → 被那个 `except Exception` 吞掉 → 返回 `{}`。
    #    **每一个 POST 的请求体都变成空字典**，于是 `/api/turn` 收到空 text，
    #    用户发任何一句话都是红字「说了句空的」。
    #
    #    👉 而它之所以能溜过**整套自检**，是因为原来那些 POST 断言
    #       **一个都没检查过请求体**：
    #         · `POST /api/inbox/claim` 发的是 `json={}`（空的，丢了也看不出来）
    #         · `POST /api/open-folder` 只断言 403 —— 而 403 发生在
    #           **读请求体之前**，那条路径根本走不到这里
    #
    #    所以这条断言必须做到两件事：
    #      ① 真的发一个**非空**的请求体
    #      ② 断言**发出去的那个值本身**被服务端收到了
    from fastapi.testclient import TestClient

    from ginan_agent.server.app import Hub, create_app

    with sandbox():
        app = create_app(Hub(), token="S")
        c = TestClient(app, base_url="http://127.0.0.1")
        H = {"x-token": "S"}

        # 探针选 `/api/open-folder`：它把收到的 `which` **原样回显在错误信息里**
        # （`不认识的位置：'...'`），所以能直接读出"服务端到底收到了什么"。
        # 而且它在 `mkdir` **之前**就抛了 —— 零副作用、不真去打开文件夹。
        marker = "位置测试-中文也要能过"
        r = c.post("/api/open-folder", json={"which": marker}, headers=H)

        check(r.status_code == 400, f"未知位置 → 400（拿到 {r.status_code}）")

        detail = ""
        try:
            detail = str(r.json().get("detail", ""))
        except Exception:  # noqa: BLE001
            detail = r.text

        check(
            marker in detail,
            f"★ 服务端**收到了请求体里的原值**（中文也没被弄坏）",
        )
        if marker not in detail:
            print(f"      ⚠️ 服务端实际说的是：{detail!r}")
            print("         —— 引号里是空的话，就是 `read_json_body` 又在吞请求体了")

        # 第二条：空对象也要能正常走完（别为了上面那条把合法的空体弄坏）
        r2 = c.post("/api/open-folder", json={}, headers=H)
        check(r2.status_code == 400, "空请求体 → 仍然是 400（不是崩溃）")


def test_reset_clears_history() -> None:
    section("④ ★ 「新任务」必须真的清掉对话历史")

    # ⚠️⚠️ 这条修的是一个**双重**的 bug：
    #
    #    ① 提交版里 `/api/reset` 直接 **500** ——
    #       `Hub.reset()` 里写 `Workspace()`，而 `Workspace` 只在
    #       `__init__` 里局部 import 过，在那个作用域里根本不存在。
    #       → 界面上的「新任务」**一直是坏的**。
    #
    #    ② 修好之后历史**仍然没清** —— 因为 `self._agent` 是 None 时
    #       整个清理被跳过。而"重启程序、还没说话、直接点新任务"
    #       正是这个情况：内存里没 agent，磁盘上留着上次的整段对话。
    #
    #    用户看到的是**干净的界面**，模型却记得全部对话 ——
    #    这正是"看起来对、其实错"那一类。
    #
    # ⚠️⚠️ 而且 `Hub.reset()` 里还有一行**删用户文件**的
    #    （`clear_inbox()` → 清空收件箱），所以这一节**必须**跑在沙箱里。
    #    见 `test_sandbox_covers_everything` 那节的说明。
    from fastapi.testclient import TestClient

    from ginan_agent.ginan import paths as gpaths
    from ginan_agent.server.app import Hub, create_app
    from tests.fakes import REAL_HEADS

    real_inbox = gpaths.inbox_dir().resolve()          # 打补丁**之前**读

    with sandbox() as (tmp, patched):
        check(
            len(patched) >= 3,
            f"沙箱盖住了 {len(patched)} 个模块：" + "、".join(m.split(".")[-1] for m in patched),
        )

        hub = Hub()
        app = create_app(hub, token="S")
        c = TestClient(app, base_url="http://127.0.0.1")
        H = {"x-token": "S"}

        from ginan_agent.core.session import SessionStore

        store = SessionStore(root=tmp)
        sess = store.session(store.create(title="默认对话").id)
        sess.append({"role": "user", "content": "我之前说过的话"})
        sess.append({"role": "assistant", "content": "我之前回过的话"})

        files = list((tmp / "sessions").glob("*/history.jsonl"))
        check(len(files) == 1, "造好了一份会话历史")
        if files:
            before = files[0].read_text(encoding="utf-8")
            check("我之前说过的话" in before, "历史里确实有那句对话")

            # ⚠️ 顺手把「新任务会清空收件箱」这条也**真的验一遍** ——
            #    它是整个程序里唯一删用户文件的地方，而在沙箱里验证它，
            #    既能测到行为，又不会碰到用户真正的东西。
            #
            #    造五个文件，覆盖四种命运：
            #      · 收件箱里的观测原件（数据目录里有一份同内容的）→ 删
            #      · 收件箱里**没认领过的**                        → 删
            #        （2026-09-21 用户明确要的：「没认领也删除」）
            #      · 收件箱**文件夹深处**的那个                    → 删
            #      · 数据目录里的观测数据                          → 删
            #      · 数据目录里的**产品**                          → 留
            inbox = gpaths.inbox_dir()
            data = gpaths.data_dir()
            inbox.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            (data / "ALIC.rnx").write_bytes(REAL_HEADS["obs"])
            (inbox / "alic0600.22o").write_bytes(REAL_HEADS["obs"])
            (data / "igs20.atx").write_bytes(REAL_HEADS["atx"])
            (inbox / "还没认领的.rnx").write_bytes(b"different" * 512)
            deep = inbox / "obs" / "rinex3" / "2026" / "244"
            deep.mkdir(parents=True, exist_ok=True)
            (deep / "深处的.rnx").write_bytes(b"deep" * 512)

            #      · **运行目录**里的东西（上一次解算的产物）            → 删
            #      · 它的邻居（logs/）                                  → 留
            #
            # ⚠️ 运行目录这一条是 2026-10-02 补的：在那之前 `Hub.reset()`
            #    **一个字节都没删过它**，而界面上一直写着"会删掉…运行记录"。
            #    实测：12 天、41 次运行、14 GB。**程序说它会删，其实没删。**
            runs_dir = gpaths.runs_dir()
            run1 = runs_dir / "20260101-000000-abcd"
            (run1 / "sub").mkdir(parents=True, exist_ok=True)
            (run1 / "ALBH_20250615.POS").write_bytes(b"pos" * 100)
            (run1 / "sub" / "Filter-x.rts_forward").write_bytes(b"r" * 4096)
            logs_dir = tmp / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "server.log").write_text("别删我", encoding="utf-8")

            r = c.post("/api/reset", headers=H)
            check(r.status_code == 200, f"POST /api/reset → {r.status_code}（以前是 500）")

            after = files[0].read_text(encoding="utf-8")
            check(
                "我之前说过的话" not in after,
                "★ 重置后**磁盘上**的旧对话没了",
            )
            check(
                len([ln for ln in after.splitlines() if ln.strip()]) == 1,
                "★ 只剩系统提示那一行",
            )

            # ---- 收件箱：**一个文件都不留** ----
            #
            # ⚠️⚠️ 用户 2026-09-21 报的就是这件事：
            #      「inbox文件夹重置应该是清空，我点新任务里边还有东西」
            #      「没认领也删除」「点新任务什么都不留」
            #
            #    这一节原来盯的是**两段删除的顺序**（`clear_claimed` 的判据
            #    依赖 data/ 里那份，所以它必须排在删观测数据之前）。
            #    现在清空**不看 data/**，那个耦合不存在了，顺序守卫也就没有对象了。
            #
            #    👉 但"用户点完新任务，收件箱是不是真的空了"**必须有人盯着** ——
            #       这正是他报的那个现象，而且它可以是**任何**原因造成的
            #       （判据挂错、深度上限、某个后缀被跳过……）。
            #       所以这里改盯**结果**，并且覆盖三种不同来路的文件。
            check(
                not (inbox / "alic0600.22o").exists(),
                "★ 收件箱里认领过的那份没了",
            )
            check(
                not (inbox / "还没认领的.rnx").exists(),
                "★★ 没认领过的**也删**（用户明确要的：没认领也删除）",
            )
            check(
                not (deep / "深处的.rnx").exists(),
                "★★ 文件夹**深处**的那个也删了（深度上限不该管清空）",
            )
            left = [str(p.relative_to(inbox)) for p in inbox.rglob("*")]
            check(
                left == [],
                f"★★ 收件箱**一个文件都不留**（还剩 {left}）",
            )

            # ---- 数据目录 ----
            check(
                not (data / "ALIC.rnx").exists(),
                "★ 观测数据**没有永久留存**（用户定的规则：新任务时删）",
            )
            check(
                (data / "igs20.atx").exists(),
                "★ 产品（天线表）留着 —— 61 MB，下次不用重下",
            )

            # ---- 运行目录（`userdata/runs/`）----
            #
            # ⚠️⚠️ 这一组是**"程序说的话终于成真了"**的守卫：
            #     确认框和返回的 `cleared` 里一直写着"会删掉…运行记录"，
            #     而 2026-10-02 之前 `Hub.reset()` 从没碰过它。
            check(not run1.exists(), "★★ 上一次解算的产物**整个删了**（运行目录）")
            left_runs = [str(p.relative_to(runs_dir)) for p in runs_dir.rglob("*")]
            check(left_runs == [], f"★★ 运行目录里一个文件都不留（还剩 {left_runs}）")
            check(runs_dir.is_dir(), "★ 而 runs 目录本身留着（下次还要往里写）")
            check(
                (logs_dir / "server.log").is_file(),
                "★★ 对照：它的邻居（logs/）没被连带删 —— 这是删用户文件，"
                "范围必须收在 runs/ 里面",
            )
            check(
                gpaths.inbox_dir().resolve() != real_inbox,
                "★ 以上操作全在沙箱里 —— 用户的真实收件箱不是这个路径",
            )

            back = r.json()
            check(
                any("观测数据" in s for s in back.get("cleared", [])),
                f"★ 结果里**点名说了**删掉观测数据（{back.get('cleared')}）",
            )
            # ⚠️ 报告里也必须点名收件箱 —— 用户看到"清空了 3 个文件"
            #    才知道东西去哪了。**删了不说，比删了更糟。**
            check(
                any("收件箱" in s for s in back.get("cleared", [])),
                f"★ 结果里**点名说了**清了收件箱（{back.get('cleared')}）",
            )
            # ⚠️ 运行目录那一笔也要点名，而且**带上省了多少** ——
            #    "清掉了"和"清掉了 13 GB"对用户是两条不同的信息
            #    （后者才是他点这个按钮想解决的问题）。
            check(
                any("运行记录" in s for s in back.get("cleared", [])),
                f"★★ 结果里**点名说了**清了运行目录（{back.get('cleared')}）",
            )
            check(
                not any("运行记录" in s for s in back.get("kept", [])),
                f"★ 而『会保留』里不许提运行记录 —— 它现在一个都不留"
                f"（{back.get('kept')}）",
            )
            # ⚠️ 而 `kept` 那张清单**不许再提收件箱**：它现在只会说假话。
            #    （它出过两次错，两次都是"说得比实际温柔"。）
            check(
                not any("收件箱" in s for s in back.get("kept", [])),
                f"★ 而『会保留』那张清单里不许再提收件箱（{back.get('kept')}）",
            )

            # ⚠️⚠️ 这份报告是**原样显示**的（前端 `addNotice` 用的是
            #     `textContent`）—— 里面**一个 Markdown 记号都不能有**，
            #     否则用户看到的是字面的一排星号。
            #     这和「新任务」那个确认框踩过的是同一个坑，只是换了个地方。
            flat = (
                list(back.get("cleared", []))
                + list(back.get("kept", []))
                + list(back.get("failed", []))
                + [back.get("note", "")]
            )
            bad_md = [x for x in flat if "**" in x or "`" in x or "__" in x]
            check(
                not bad_md,
                f"★★ 报告里没有 Markdown 记号（否则用户看到字面星号）：{bad_md}",
            )
            check(
                "failed" in back and isinstance(back["failed"], list),
                "★★ 而且有一份**单独列出来的『没删掉的』**（failed）"
                "—— 没有它的话，『一个都没删掉』会连一行提示都不出现",
            )

            # =============================================================
            # ★★★ 用户那次到底发生了什么：一个文件被占着，删不掉
            # =============================================================
            #
            # ⚠️ 2026-09-21 实测复现：Windows 上 `unlink()` 只要有任何进程
            #    没带 FILE_SHARE_DELETE 打开过它就会失败（WinError 32），
            #    而 Python 的 `open()` 正好不给那一位。
            #    用户当时就是这么撞上的（面板正在读那个文件），
            #    **几分钟后手删一次就好了** —— 故障自己会好。
            #
            # 👉 但"用户不知道发生了什么"不会自己好。所以这一节要证明：
            #    ① 删不掉的**真的进了报告**（不是只记在代码里）
            #    ② 报告是**用户看得到的那一份**（`cleared`，前端渲染它）
            lock = inbox / "被占着的.rnx"
            lock.write_bytes(b"L" * 2048)
            held = lock.open("rb")
            try:
                r3 = c.post("/api/reset", headers=H)
            finally:
                held.close()

            check(r3.status_code == 200, f"占着文件时 reset 也不该炸（{r3.status_code}）")
            b3 = r3.json()
            check(
                any("被占着的.rnx" in s for s in b3.get("failed", [])),
                f"★★★ 删不掉的那个**出现在 failed 里、带着原因**"
                f"（{b3.get('failed')}）",
            )
            check(
                any("删不掉" in s for s in b3.get("cleared", [])),
                f"★★★ 而且用户能看到的那份报告（cleared）里**也说了**"
                f"（{b3.get('cleared')}）",
            )
            check(
                lock.is_file(),
                "★ 没删掉就是没删掉 —— 不假装删了",
            )
            # ⚠️ 顺手把重试也验了：占用只持续一瞬间的话，**不该**留下文件。
            #    这条对着的就是用户那一次（面板读完那个文件就松手了）。
            import threading

            brief = inbox / "临时占着的.rnx"
            brief.write_bytes(b"B" * 2048)
            h_brief = brief.open("rb")
            threading.Timer(0.1, h_brief.close).start()
            try:
                r4 = c.post("/api/reset", headers=H)
            finally:
                # ⚠️ 见 check_files 里同一处的说明：一定要在**离开这一节之前**
                #    松手，否则还没有重试时整个流程快过 0.1 秒，
                #    后面的沙箱清理会撞上一个还开着的文件。
                h_brief.close()

            check(
                not brief.exists(),
                f"★★ 临时占用**重试之后删掉了**（不重试的话它会留下 ——"
                f" 用户看到的就是『还在』）（{r4.json().get('cleared')}）",
            )


# =============================================================================
# 主程序
# =============================================================================


def _real_inbox_listing() -> list[str]:
    """真实收件箱里的文件清单（名字 + 大小）。绊线用。

    ⚠️ 读不到就返回空表 —— 这个函数**自己不能抛异常**，
       它是安全网，不是被测对象。
    """
    try:
        from ginan_agent.ginan import paths as gpaths

        d = gpaths.inbox_dir()
        if not d.is_dir():
            return []
        return sorted(f"{p.name}:{p.stat().st_size}" for p in d.iterdir() if p.is_file())
    except OSError:
        return []


def main() -> int:
    print("=" * 60)
    print("本地服务自检")
    print("=" * 60)
    print("⚠️ 用 TestClient，在进程内跑 —— 不启端口、不联网。")

    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
    except ImportError as exc:
        print(f"\n⚠️ 没装 fastapi/httpx（{exc}）—— 跳过。")
        print("   这不是失败：服务端自检需要额外依赖，而核心功能不需要它们。")
        return 0

    # ⚠️⚠️ **绊线**：跑之前先记下用户真实收件箱的样子，跑完再对一遍。
    #
    #     这是一个**兜底**，不是替代品 —— 上面每一节该用沙箱还是要用沙箱。
    #     它的价值在于：**将来新写的用例**如果忘了沙箱、或者哪个 endpoint
    #     将来新增了删文件的行为，这一条会当场抓住，而不是等到用户发现
    #     "我放进收件箱的东西没了"。
    #
    #     自检本身去删用户的数据，是比"代码有 bug"严重得多的错：
    #     它**没人会怀疑** —— 而用户的东西已经没了。
    inbox_before = _real_inbox_listing()

    test_sandbox_covers_everything()
    test_results_endpoint_shape()
    test_corrections_endpoint_shape()
    test_residual_endpoints_shape()
    test_residual_bundle_round_trip()
    test_guard_is_structural()
    test_guard_enforces()
    test_sse_yield_is_inside_the_loop()
    test_frontend_renders_turn_end_text()
    test_events_are_broadcast()
    test_error_turn_still_ends()
    test_settings_returns_known_models()
    test_post_body_actually_arrives()
    test_reset_clears_history()

    section("⑤ ★ 绊线：用户真实的收件箱，自检前后必须一模一样")
    inbox_after = _real_inbox_listing()
    check(
        inbox_before == inbox_after,
        f"★ 真实收件箱没被动过（{len(inbox_before)} 个文件）",
    )
    if inbox_before != inbox_after:
        print(f"      跑之前：{inbox_before}")
        print(f"      跑之后：{inbox_after}")

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
