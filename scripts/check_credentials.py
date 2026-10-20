"""自检：凭据读写。

跑法：python scripts/check_credentials.py

============================ 最重要的一条 ============================
这个脚本守的核心是：**改 .netrc 时不能把用户别的东西弄丢。**

`.netrc` 是**共享文件** —— 用户可能在里面配了公司内网、
私有 Git 服务器、别的数据中心的凭据。

GinanUI 的实现是**直接构造一份新内容覆盖写**，
于是用户的其他凭据会被无声地抹掉。
这是典型的"为了完成一个任务，破坏了用户别的东西"。

所以这里反复测"读-改-写"：写入前有什么、之后还剩什么。

============================ 不联网 ============================
验证凭据那步要联网，所以那个函数单独测（用假响应），
而不是真的去打 NASA 的服务器。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ginan_agent.ginan import credentials  # noqa: E402

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
# ① 解析
# =============================================================================


def test_parse() -> None:
    section("① 解析 .netrc")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".netrc"
        p.write_text(
            "machine example.com\n"
            "    login alice\n"
            "    password secret1\n"
            "\n"
            "machine urs.earthdata.nasa.gov\n"
            "    login bob\n"
            "    password secret2\n"
            "\n"
            "# 一行注释\n"
            "default\n"
            "    login guest\n"
            "    password guest\n",
            encoding="utf-8",
        )

        entries = credentials.read_netrc(p)
        machines = [m for m, _ in entries]

        check(len(entries) == 3, f"解析出 3 个条目（实际 {len(entries)}）")
        check("example.com" in machines, "普通域名解析正确")
        check("urs.earthdata.nasa.gov" in machines, "Earthdata 域名解析正确")
        check("__default__" in machines, "default 兜底条目被保留（不是丢掉）")

        d = dict(entries)
        check(d["example.com"]["login"] == "alice", "用户名解析正确")
        check(d["example.com"]["password"] == "secret1", "密码解析正确")


def test_parse_tolerance() -> None:
    section("② 解析要能容忍用户手改坏的文件")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".netrc"

        # 用户手动编辑可能留下各种毛病
        p.write_text(
            "machine a.com\n"
            "login\n"                       # 缺值
            "machine b.com\n"
            "    login bob\n"
            "    password xxx\n"
            "只有一行没有值\n"
            "    password 没有对应的machine\n",
            encoding="utf-8",
        )

        try:
            entries = credentials.read_netrc(p)
            ok = True
        except Exception as exc:  # noqa: BLE001
            entries, ok = [], False
            print(f"      抛了 {type(exc).__name__}: {exc}")

        check(ok, "损坏的文件不抛异常")
        check(
            any(m == "b.com" for m, _ in entries),
            "能从损坏的文件里捞出正常的那条",
        )

        # 不存在的文件
        check(credentials.read_netrc(Path(td) / "没有这个") == [],
              "文件不存在时返回空列表")


# =============================================================================
# ③ ⚠️ 核心：读-改-写，不覆盖
# =============================================================================


def test_merge_does_not_clobber() -> None:
    section("③ ⚠️ 核心：改 CDDIS 凭据时不能抹掉用户的**其他**凭据")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".netrc"

        # 用户已经在里面配了别的东西
        p.write_text(
            "machine git.company.internal\n"
            "    login zhangsan\n"
            "    password 公司内网的密码\n"
            "\n"
            "machine cddis.nasa.gov\n"
            "    login 旧的用户名\n"
            "    password 旧的密码\n",
            encoding="utf-8",
        )

        # 我们写入新的 CDDIS 凭据
        credentials.write_credentials("新用户", "新密码", paths=[p])

        entries = dict(credentials.read_netrc(p))

        check(
            "git.company.internal" in entries,
            "★ 公司内网的凭据**还在**（没被覆盖）",
        )
        check(
            entries.get("git.company.internal", {}).get("login") == "zhangsan",
            "★ 公司内网的用户名也原样保留",
        )
        check(
            entries.get("cddis.nasa.gov", {}).get("login") == "新用户",
            "CDDIS 的凭据被更新了",
        )
        check(
            entries.get("urs.earthdata.nasa.gov", {}).get("login") == "新用户",
            "两个域名都写了（ursearthdata + cddis）",
        )


def test_default_stays_last() -> None:
    section("④ default 兜底条目必须排在最后")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / ".netrc"
        # default 在最前面 —— 这是坏的顺序，我们要把它挪到最后
        p.write_text(
            "default\n"
            "    login guest\n"
            "machine other.com\n"
            "    login x\n",
            encoding="utf-8",
        )

        credentials.write_credentials("u", "p", paths=[p])
        text = p.read_text(encoding="utf-8")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        # ⚠️ netrc 的语义是：default 之后的条目**全都被它兜底**，
        #    所以 default 必须在最后，否则它会把后面的 machine 吞掉。
        check(
            lines.index("default") > lines.index("machine other.com"),
            "default 被移到了最后（否则它会吞掉后面的条目）",
        )
        check("machine other.com" in text, "原有的条目没丢")


def test_multiple_paths() -> None:
    section("⑤ 该写的位置都写了")

    paths = credentials.netrc_paths()
    check(len(paths) >= 1, f"至少有一个路径（{len(paths)} 个）")

    if sys.platform == "win32":
        names = [p.name for p in paths]
        check("_netrc" in names, "Windows 上包含 _netrc（带下划线）")
        check(".netrc" in names, "Windows 上也包含 .netrc（两个都写最保险）")
    else:
        check(paths[0].name == ".netrc", "Unix 上用 .netrc")


# =============================================================================
# ⑥ 状态查询
# =============================================================================


def test_state_queries() -> None:
    section("⑥ 状态查询：配没配、用户名是什么")

    # ⚠️ 这两个函数读的是**真实的家目录**。
    #    为了不污染用户的 .netrc，这里只测"不崩溃"和"返回值类型对"，
    #    不去写真实文件。
    try:
        got = credentials.has_credentials()
        check(isinstance(got, bool), f"has_credentials 返回布尔值（{got}）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"has_credentials 不该抛异常：{exc}")

    try:
        user = credentials.current_user()
        check(isinstance(user, str), f"current_user 返回字符串（{user!r}）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"current_user 不该抛异常：{exc}")


# =============================================================================
# ⑦ 验证逻辑（用假响应，不联网）
# =============================================================================


class FakeResp:
    def __init__(self, status=200, body=b""):
        self.status_code = status
        self._body = body

    def iter_content(self, size):
        yield self._body[:size]

    def close(self):
        pass


def test_judge_credentials() -> None:
    section("⑦ 判断逻辑：靠响应**内容**，不靠状态码")

    # ⚠️ 这里直接测 judge_credentials —— 纯函数，不联网、不发请求。
    #
    #    原来这段逻辑内联在 test_credentials 里，测试只能猴补丁
    #    requests.get。而实现改成走 session 之后补丁就失效了，
    #    测试**静默变成永远通过** —— 一个不再测任何东西的测试，
    #    比没有测试更危险（它给人虚假的安全感）。
    #
    #    抽成纯函数之后，这类耦合就不存在了。

    LOGIN = b'<!DOCTYPE html><html><head><title>Earthdata Login</title>'
    NOT_FOUND = b'<!DOCTYPE html><html><body><h1>404 Not Found</h1>'
    DATA = b'real readme content here'

    cases = [
        (200, DATA, True, "拿到真实内容 -> 通过"),
        (200, LOGIN, False, "拿到登录页 -> **不通过**（即使状态码是 200）"),
        (200, NOT_FOUND, False, "拿到 404 页 -> 不通过，但说明要指向路径问题"),
        (500, b"", False, "服务器 500 -> 不通过"),
        (403, b"", False, "403 -> 不通过"),
    ]

    for status, body, expect_ok, label in cases:
        r = credentials.judge_credentials(status, body)
        check(r.ok is expect_ok, f"{label}（实际 ok={r.ok}）")
        if not expect_ok:
            check(bool(r.suggestion), "   失败时给出了'怎么办'")

    # ⚠️ 404 和登录页要能区分开 —— 两者的排查方向完全不同。
    #    踩过：把 CDDIS 的 404 当成"认证没过"，排查方向完全跑偏。
    r404 = credentials.judge_credentials(200, NOT_FOUND)
    rlogin = credentials.judge_credentials(200, LOGIN)
    check("404" in r404.detail, "404 的说明里点明了是 404")
    check("账号" in r404.suggestion, "404 的说明明确排除'账号问题'")
    check("登录" in rlogin.detail, "登录页的说明指向凭据问题")


def test_empty_credentials() -> None:
    section("⑧ 空用户名/密码直接拒绝，不发请求")

    check(not credentials.test_credentials("", "p").ok, "用户名为空时直接拒绝")
    check(not credentials.test_credentials("u", "").ok, "密码为空时直接拒绝")



# =============================================================================
# 主程序
# =============================================================================


def test_storage_location() -> None:
    section("⓪ ★ 凭据必须存在**软件目录**里（用户明确要求）")

    # ⚠️ 用户的原话："凭据怎么在 c 盘，要在软件目录内"
    #
    #    而启动横幅上写着"这个程序会把**所有**数据都放在自己的文件夹里 ——
    #    包括你填的 AI 密钥"。`config.json` 一直在 userdata 里，
    #    **只有凭据跑到了家目录** —— 承诺和事实对不上。
    from ginan_agent.ginan import credentials, paths

    root = paths.app_root()
    for p in credentials.netrc_paths():
        check(
            root in p.parents or p.parent == root,
            f"写的位置在软件目录内：{p}",
        )

    # ⚠️ 但**读**要包含家目录 —— 老用户升级后凭据还在那儿，
    #    只看新位置的话会判定"没配过"，让他重填一遍。
    reads = credentials.read_paths()
    check(
        any("Users" in str(p) or p == Path.home() / ".netrc" for p in reads),
        "★ 读取顺序里**包含家目录**（老用户的凭据还读得到）",
    )
    check(
        reads.index(credentials.netrc_paths()[0])
        < max(i for i, p in enumerate(reads) if p not in credentials.netrc_paths()),
        "★ 软件目录**排在前面** —— 否则用户改了密码会被旧位置盖掉",
    )


def test_migrate_legacy() -> None:
    section("⓪b ★ 搬家：老凭据搬一份过来，**旧的绝不删**")

    import tempfile

    from ginan_agent.ginan import credentials

    tmp = Path(tempfile.mkdtemp(prefix="creds_"))
    new_a = tmp / "new_netrc"
    new_b = tmp / "new.netrc"
    legacy = tmp / "legacy_netrc"
    legacy.write_text(
        "machine cddis.nasa.gov\n\tlogin olduser\n\tpassword oldpass\n",
        encoding="utf-8",
    )

    written = credentials.migrate_legacy_into([new_a, new_b], [legacy])
    check(bool(written), "搬过来了")
    check(
        legacy.is_file() and "olduser" in legacy.read_text(encoding="utf-8"),
        "★ 家目录那份**原封不动**（那是用户的文件，我们只是复制）",
    )
    check(
        "olduser" in new_a.read_text(encoding="utf-8"),
        "★ 新位置拿到了凭据",
    )

    # ---- 已经有新的了 → 什么都不做（别拿旧的盖掉新的）----
    new_a.write_text(
        "machine cddis.nasa.gov\n\tlogin NEWuser\n\tpassword NEWpass\n",
        encoding="utf-8",
    )
    written2 = credentials.migrate_legacy_into([new_a, new_b], [legacy])
    check(not written2, "★ 新位置已有凭据时不搬（否则会把新密码盖回旧的）")
    check(
        "NEWuser" in new_a.read_text(encoding="utf-8"),
        "★ 新位置那份没被动过",
    )


def test_cddis_download_actually_uses_auth_session() -> None:
    section("⑧ ★★ 下载 CDDIS 时，那条认证会话必须**真的被用上**")

    # ⚠️⚠️ 这条守的是一段**从来没有执行过**的代码。
    #
    #    `credentials.ScopedBasicAuth` / `cddis_session` 里那套
    #    "跨主机跳转要重新带凭据" 的设计写得挺完整 ——
    #    可它在**下载路径上一次都没跑过**，因为：
    #
    #        fetch_plan:  sess = session or net.build_session(proxy)
    #                     download_one(..., session=sess)      ← 永远非 None
    #
    #        download_one: sess, ours = _session_for(src, session, proxy)
    #
    #        _session_for: if session is not None:
    #                          return session, False           ← 早返回
    #                      if source_key == "cddis": ...       ← 永远到不了
    #
    #    后果：下载 CDDIS **只会拿到一个登录页**，
    #    而那看起来像"账号不对"，实际是"代码从来没把账号带上"。
    #    **一个指向错误方向的报错，比没有报错更浪费时间。**
    #
    # ⚠️ 为什么必须**端到端**测 `fetch_plan`：
    #    单独测 `_session_for("cddis", None, None)` 是绿的（它本来就对），
    #    bug 在**调用方**那一句 —— 只有真跑一遍 `fetch_plan` 才看得见。
    #    （这正是本仓库反复踩的「两边分开看都对，错的是它们之间的约定」。）
    import tempfile

    from ginan_agent.ginan import credentials, fetch
    from ginan_agent.ginan.catalog import FilePlan, RunPlan

    SENTINEL = object()
    seen: dict = {}

    real_cddis = credentials.cddis_session
    real_try = fetch._try_download

    def fake_cddis(proxy=None):
        return SENTINEL

    def fake_try(item, url, dest_dir, *, source, session, **kw):
        seen["session"] = session
        seen["source"] = source
        # ⚠️ 必须返回**真的 DownloadResult**，不能返回 None ——
        #    调用方紧接着就 `if result.ok`（我第一版就栽在这儿）。
        return fetch.DownloadResult(
            filename=item.filename, ok=False, kind=item.kind,
            source=source, detail="测试：只关心会话",
        )

    credentials.cddis_session = fake_cddis
    fetch._try_download = fake_try
    try:
        with tempfile.TemporaryDirectory() as td:
            plan = RunPlan(
                stations=["ALIC"], day=__import__("datetime").date(2026, 9, 2),
                files=[FilePlan(filename="x.sp3", kind="sp3",
                                urls={"cddis": "https://cddis.nasa.gov/x"},
                                size_hint="测试")],
            )
            fetch.fetch_plan(plan, Path(td))      # ⚠️ 不传 session
    finally:
        credentials.cddis_session = real_cddis
        fetch._try_download = real_try

    check(
        seen.get("source") == "cddis",
        f"确实走到了 cddis 那个源（{seen.get('source')}）",
    )
    check(
        seen.get("session") is SENTINEL,
        "★★ 下载 CDDIS 用的是**带认证的那个会话**"
        "（不是调用方统一建的那个普通会话）"
        "—— 用错的话只会拿到登录页，而那是死代码复活",
    )

    # ⚠️ 反过度修正：**调用方显式传进来的会话要照用**（测试靠它）。
    check(
        fetch._session_for("cddis", SENTINEL, None)[0] is SENTINEL,
        "★ 调用方显式传了会话时仍然尊重它（否则所有假会话的测试全废）",
    )


def main() -> int:
    print("=" * 60)
    print("凭据自检 —— 重点是『不能弄丢用户其他的凭据』")
    print("=" * 60)
    print("⚠️ 不联网（验证逻辑用假响应测）。")

    test_cddis_download_actually_uses_auth_session()
    test_storage_location()
    test_migrate_legacy()
    test_parse()
    test_parse_tolerance()
    test_merge_does_not_clobber()
    test_default_stays_last()
    test_multiple_paths()
    test_state_queries()
    test_judge_credentials()
    test_empty_credentials()

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
