"""数据源普查 —— 在不同网络环境下，到底哪些源可用。

跑法：
    python scripts/survey_sources.py              # 普查一遍并记录
    python scripts/survey_sources.py --compare    # 和上一次对比
    python scripts/survey_sources.py --list       # 只列出清单

============================ 为什么要做普查 ============================
因为**一台机器的测量不代表所有人**。

开发机上测出"BKG 通、CDDIS 要账号"，那只说明**那台机器、那个网络**。
换个人的网络，结论可能完全反过来。

所以要：
  · 覆盖**尽可能全**的数据源（不只是官方文档提到的那几个）
  · 在不同网络环境下**重复测**
  · 把结果存下来对比 —— 找出"哪种网络下哪些源可用"的规律

这个规律最终会变成产品的默认配置：
对每类文件，按"能用的源"排序，而不是写死一个。

============================ 探测方法 ============================
每个源打一个 `Range: bytes=0-1023` 的请求 —— 只要头部，不要整个文件。

⚠️ 但"通不通"不能只看状态码。见 net.judge_data 的说明：
   CDDIS 认证失败返回的是 **200 + 登录页**，不是 401。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ginan_agent.ginan import net  # noqa: E402

SURVEY_DIR = ROOT / "docs" / "surveys"


# =============================================================================
# 数据源清单
# =============================================================================
#
# ⚠️ 这份清单要**尽量全**，宁可多测不可漏测。
#    因为漏掉的源可能就是某个网络环境下唯一能用的那个。
#
# 每个源包含：
#   key      唯一标识
#   label    给人看的名字
#   category 用途分类（精密产品 / 观测数据 / 模型文件 / 基准）
#   url      探测用的地址（要指向**真实数据**而不是网页）
#   note     补充说明


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    category: str
    url: str
    note: str = ""
    # ⚠️ 这个字段是第一版遗漏的，导致整轮普查的结论大部分是错的。
    #
    #    "能不能连上这台服务器" 和 "这个路径有没有数据文件" 是**两个问题**。
    #    混在一起测的结果是：给一个网站首页配"必须是数据文件"的判断，
    #    于是 GitHub、百度、Apache 全被判为"不通" —— 而它们本来就在正常应答。
    #
    #    expect="data" —— 期望拿到真实数据文件（SP3/ATX 之类）
    #    expect="any"  —— 只要服务器应答即可（首页、目录列表、API）
    expect: str = "data"


# ---- 精密产品（SP3/CLK/BIA/ATX/ERP）的各个 IGS 数据中心 ----
# 这些是**互替**关系 —— 同一个文件它们都有副本，
# 所以"哪个能用"决定了一切。

PRECISE = [
    Source("bkg", "德国 BKG", "精密产品",
           "https://igs.bkg.bund.de/root_ftp/IGS/station/general/igs20.atx",
           "IGS 官方数据中心，免账号"),
    Source("cddis", "NASA CDDIS（美国）", "精密产品",
           "https://cddis.nasa.gov/archive/gnss/products/rcvantex/igs20.atx.gz",
           "IGS 主数据中心，需 Earthdata 账号"),
    Source("ign", "法国 IGN", "精密产品",
           "https://igs.ign.fr/pub/igs/station/general/igs20.atx",
           "欧洲 IGS 数据中心"),
    Source("whu", "武汉大学 IGS", "精密产品",
           "http://igs.gnsswhu.cn/pub/gps/station/general/igs20.atx",
           "国内 IGS 数据中心（若可用，国内用户无需代理）"),
    Source("whu_https", "武汉大学 IGS（https）", "精密产品",
           "https://igs.gnsswhu.cn/pub/gps/station/general/igs20.atx"),
    Source("igs_org", "IGS 官方分发点", "精密产品",
           "https://files.igs.org/pub/station/general/igs20.atx",
           "IGS 官方分发点"),
    Source("nrcan", "加拿大 NRCan", "精密产品",
           "https://webapp.geod.nrcan.gc.ca/geod/data-donnees/",
           "加拿大自然资源部（目录页）", expect="any"),
    Source("pecny", "捷克 Pecný", "精密产品",
           "http://ftp.pecny.cz/LDC/",
           "捷克大地测量观测台（目录页）", expect="any"),
    Source("aiub", "瑞士伯尔尼 AIUB", "精密产品",
           "https://www.aiub.unibe.ch/download/",
           "伯尔尼大学天文研究所（目录页）", expect="any"),
    Source("esa", "欧洲空间局 GSSC", "精密产品",
           "https://gssc.esa.int/",
           "ESA 导航支持中心（首页）", expect="any"),
    Source("gsi_jp", "日本国土地理院", "精密产品",
           "https://terras.gsi.go.jp/",
           "日本 GSI（首页）", expect="any"),
    Source("noaa_ngs", "NOAA/NGS（美国）", "精密产品",
           "https://geodesy.noaa.gov/",
           "美国大地测量局（首页）", expect="any"),
    Source("sopac", "美国 SOPAC", "精密产品",
           "https://sopac-csrc.ucsd.edu/",
           "加州大学圣地亚哥分校（首页）", expect="any"),
    Source("unavco", "UNAVCO/EarthScope", "精密产品",
           "https://www.unavco.org/",
           "美国地球科学观测网（首页）", expect="any"),
    Source("ga_gnss", "澳大利亚 GA（GNSS 门户）", "精密产品",
           "https://gnss.ga.gov.au/",
           "Geoscience Australia（首页）", expect="any"),
]

# ---- 观测数据 ----
OBS = [
    Source("ga_api", "澳大利亚 GA（数据 API）", "观测数据",
           "https://data.gnss.ga.gov.au/api/v1/rinexFiles"
           "?stationId=ALIC&fileType=obs&rinexVersion=3&filePeriod=01D"
           "&decompress=true&tenantId=default"
           "&startDate=2026-01-01T00:00:00Z&endDate=2026-01-02T00:00:00Z",
           "免账号，返回 JSON 含预签名下载地址"),
    Source("cddis_data", "NASA CDDIS（观测数据）", "观测数据",
           "https://cddis.nasa.gov/archive/gnss/data/daily/",
           "全球 IGS 测站"),
    Source("bkg_obs", "德国 BKG（观测数据）", "观测数据",
           "https://igs.bkg.bund.de/root_ftp/IGS/obs/",
           "欧洲测站（目录页）", expect="any"),
]

# ---- 模型文件（tables/）----
MODELS = [
    Source("peanpod", "Ginan 官方 S3（悉尼）", "模型文件",
           "https://peanpod.s3.ap-southeast-2.amazonaws.com/aux/products/tables/gpt_25.grd.gz",
           "⚠️ 全部 34 个 tables/ 都在这里，也是官方脚本指向的地方"),
    Source("iers", "IERS 地球定向参数", "模型文件",
           "https://datacenter.iers.org/data/latestVersion/",
           "ERP 备选来源（目录页）", expect="any"),
    Source("vmf", "TU Wien 对流层格网", "模型文件",
           "https://vmf.geo.tuwien.ac.at/trop_products/GRID/",
           "VMF3 对流层产品（目录页）", expect="any"),
    Source("ncei", "NOAA NCEI（地磁模型）", "模型文件",
           "https://www.ncei.noaa.gov/pub/data/",
           "IGRF 地磁系数（目录页）", expect="any"),
    Source("icgem", "ICGEM 重力场模型", "模型文件",
           "http://icgem.gfz-potsdam.de/",
           "EGM2008 等（首页）", expect="any"),
    Source("jpl", "JPL 行星历表", "模型文件",
           "https://ssd.jpl.nasa.gov/",
           "DE430/DE436（⚠️ GPL-3，不可打包分发）", expect="any"),
    Source("onsala", "瑞典 Onsala（海潮负荷）", "模型文件",
           "http://holt.oso.chalmers.se/loading/",
           "OLOAD/ALOAD BLQ 文件（目录页）", expect="any"),
]

# ---- 基准（用来判断"是不是整个网络都断了"）----
#
# ⚠️ 这些都是网页，所以 expect="any"。
#    第一版忘了这个字段，结果把 GitHub/百度/Apache 全判成"不通" ——
#    而它们本来就在正常应答，只是返回的是 HTML。
BASELINE = [
    Source("github", "GitHub", "基准", "https://github.com/", expect="any"),
    Source("apache", "Apache 官网", "基准", "https://www.apache.org/", expect="any"),
    Source("deepseek", "DeepSeek API", "基准", "https://api.deepseek.com/", expect="any"),
    Source("baidu", "百度（国内基准）", "基准", "https://www.baidu.com/", expect="any"),
]

ALL_SOURCES = PRECISE + OBS + MODELS + BASELINE

CATEGORY_ORDER = ["基准", "精密产品", "观测数据", "模型文件"]


# =============================================================================
# 探测
# =============================================================================


@dataclass
class Result:
    key: str
    label: str
    category: str
    note: str
    reachable: bool
    status: str          # ok / auth / fail
    latency_ms: int
    detail: str

    @property
    def icon(self) -> str:
        return {"ok": "✓", "auth": "🔑", "fail": "✗"}[self.status]


def _judge_any(status: int, head: bytes) -> tuple[bool, str]:
    """宽松判断：只要服务器**应答了**就算可达。

    ⚠️ 和 net.judge_data 的区别就在这里 —— 那个是"这些字节是不是一个可用的数据文件"，
    这个是"这台服务器回我话了吗"。

    混用两者的后果实测过：给 GitHub 首页配"必须是数据文件"的判断，
    于是它被判为不通 —— 而它明明 200 正常应答。

    在这个判断里，**404 也算可达** —— 服务器活着，只是路径不对。
    真正不可达的只有：超时、连不上、DNS 解析失败。
    """
    if 200 <= status < 400:
        return True, f"可达（HTTP {status}）"
    if status in (401, 403):
        return False, f"可达但需认证（HTTP {status}）"
    if 400 <= status < 500:
        # ⚠️ 404 是"路径不对"，不是"服务器不可达" —— 这个区别很重要，
        #    因为前者换个路径就行，后者才需要代理。
        return True, f"可达（HTTP {status}，路径可能不对）"
    if status >= 500:
        return True, f"可达但服务端出错（HTTP {status}）"
    return False, f"异常状态码 {status}"


def probe_one(src: Source, session, timeout: float) -> Result:
    import time as _t

    t0 = _t.monotonic()

    if src.expect == "any":
        # 宽松模式：自己发请求，看有没有应答
        try:
            resp = session.get(src.url, timeout=timeout, stream=True,
                               allow_redirects=True)
            status = resp.status_code
            try:
                head = next(resp.iter_content(1024), b"") or b""
            except Exception:  # noqa: BLE001
                head = b""
            finally:
                resp.close()
            ok, detail = _judge_any(status, head)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, net._explain_exception(exc)
        ms = int((_t.monotonic() - t0) * 1000)
    else:
        # 严格模式：必须是可用的数据文件
        ok, ms, detail = net.head_ok(session, src.url, timeout)

    status = "ok" if ok else ("auth" if ("登录" in detail or "认证" in detail) else "fail")
    return Result(
        key=src.key, label=src.label, category=src.category, note=src.note,
        reachable=ok, status=status, latency_ms=ms,
        detail=detail,
    )


def survey(timeout: float = 12.0, proxy_mode: str = "auto", only=None) -> list[Result]:
    sources = [s for s in ALL_SOURCES if not only or s.key in only]
    session = net.build_session(net.ProxyConfig(mode=proxy_mode))

    # ⚠️ 并发探测。串行的话 26 个源 × 最坏 12 秒 = 5 分钟，没人等得起。
    #    但并发数要克制 —— 对同一批服务器一次打太多请求是不礼貌的，
    #    而且可能被当成攻击而封掉。
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: probe_one(s, session, timeout), sources))

    session.close()
    return results


# =============================================================================
# 展示
# =============================================================================


def print_table(results: list[Result]) -> None:
    by_cat: dict[str, list[Result]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    for cat in CATEGORY_ORDER:
        group = by_cat.get(cat)
        if not group:
            continue
        print(f"\n【{cat}】")
        for r in group:
            name = r.label
            # 中文按两个宽度算，这样列才对得齐
            width = sum(2 if ord(c) > 127 else 1 for c in name)
            pad = " " * max(1, 24 - width)
            print(f"  {r.icon} {name}{pad}{r.latency_ms:>6} ms  {r.detail}")


def summarize(results: list[Result]) -> dict:
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r.status == "ok"),
        "auth": sum(1 for r in results if r.status == "auth"),
        "fail": sum(1 for r in results if r.status == "fail"),
    }


def save_survey(results: list[Result], label: str) -> Path:
    SURVEY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = SURVEY_DIR / f"survey-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "label": label,
                "summary": summarize(results),
                "results": [asdict(r) for r in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_previous() -> dict | None:
    """读上一次的普查结果 —— 用于对比。"""
    if not SURVEY_DIR.exists():
        return None
    files = sorted(SURVEY_DIR.glob("survey-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def compare(results: list[Result], prev: dict) -> None:
    """和上一次对比，突出**变化**。"""
    old = {r["key"]: r for r in prev.get("results", [])}
    print(f"\n【与上次对比】（上次：{prev.get('timestamp','?')}）")

    changed = 0
    for r in results:
        o = old.get(r.key)
        if o is None:
            print(f"  ＋ {r.label}：本次新增")
            changed += 1
        elif o["status"] != r.status:
            arrow = f"{o['status']} → {r.status}"
            mark = "✓" if r.status == "ok" else "✗"
            print(f"  {mark} {r.label}：{arrow}")
            changed += 1

    if not changed:
        print("  （没有变化）")


# =============================================================================
# 主程序
# =============================================================================


def analyze() -> int:
    """把所有历史普查合起来看 —— 找出**跨网络环境稳定**的源。

    ============================ 这才是普查的目的 ============================
    单次普查只能说明"那一刻、那个网络"。
    把多次普查并排看，才能回答真正重要的问题：

        **哪个源在任何网络环境下都能用？**

    那些"全绿"的源，才是产品该写进默认配置的。
    那些"时好时坏"的，只能当备选。
    """
    if not SURVEY_DIR.exists():
        print("还没有任何普查记录。先跑一次 survey_sources.py。")
        return 1

    files = sorted(SURVEY_DIR.glob("survey-*.json"))
    if not files:
        print("还没有任何普查记录。")
        return 1

    surveys = []
    for f in files:
        try:
            surveys.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue

    if not surveys:
        print("普查记录都读不出来。")
        return 1

    # 建立 源 → {普查标签: 状态} 的表
    matrix: dict[str, dict[str, str]] = {}
    labels: list[str] = []
    for s in surveys:
        lbl = s.get("label") or s.get("timestamp", "?")[:16]
        # 同名标签加序号，避免覆盖
        base, n = lbl, 2
        while lbl in labels:
            lbl = f"{base}#{n}"
            n += 1
        labels.append(lbl)
        for r in s.get("results", []):
            matrix.setdefault(r["key"], {})[lbl] = r["status"]

    # 源的中文名（从最后一次普查里取）
    names = {}
    for r in surveys[-1].get("results", []):
        names[r["key"]] = (r["label"], r["category"])

    ICONS = {"ok": "✓", "auth": "🔑", "fail": "✗"}
    width = 14

    print("=" * 78)
    print("跨网络环境的数据源稳定性")
    print("=" * 78)
    print(f"基于 {len(surveys)} 次普查\n")

    # 表头
    head = f"{'源':<26}" + "".join(f"{lbl[:12]:<14}" for lbl in labels)
    print(head)
    print("-" * len(head))

    # ⚠️ 分类必须把"需要账号"和"不通"分开。
    #
    #    踩过一次：原来只有"ok / 不是 ok"两类，
    #    于是 CDDIS 掉进了"从未成功" —— 可它**工作完全正常，只是要账号**。
    #    这个错误结论会直接误导产品设计（"CDDIS 不能用"是错的，
    #    正确说法是"CDDIS 能用，但要先注册"）。
    stable_ok, stable_auth, sometimes, never = [], [], [], []
    for key, row in sorted(matrix.items()):
        label, cat = names.get(key, (key, "?"))
        lw = sum(2 if ord(c) > 127 else 1 for c in label)
        pad = " " * max(1, 26 - lw)
        line = f"{label}{pad}"
        for lbl in labels:
            st = row.get(lbl, "?")
            line += f"{ICONS.get(st, '?'):<14}"
        print(line)

        statuses = [s for s in row.values() if s != "?"]
        if not statuses:
            continue
        if all(s == "ok" for s in statuses):
            stable_ok.append(label)
        elif all(s in ("ok", "auth") for s in statuses) and "auth" in statuses:
            # 一直是"可达但需账号" —— 这是**稳定**状态，不是失败
            stable_auth.append(label)
        elif any(s in ("ok", "auth") for s in statuses):
            sometimes.append(label)
        else:
            never.append(label)

    print()
    print("=" * 78)
    print(f"【稳定可用】{len(stable_ok)} 个 —— 这些才是默认配置该用的")
    for x in stable_ok:
        print(f"    ✓ {x}")

    if stable_auth:
        print(f"\n【稳定可达，但需要账号】{len(stable_auth)} 个")
        for x in stable_auth:
            print(f"    🔑 {x}")

    if sometimes:
        print(f"\n【时好时坏】{len(sometimes)} 个 —— 只能当备选")
        for x in sometimes:
            print(f"    ~ {x}")

    if never:
        print(f"\n【从未成功】{len(never)} 个")
        for x in never:
            print(f"    ✗ {x}")

    print("=" * 78)
    if stable_ok:
        print(
            "结论：默认源应该从「稳定可用」里选，"
            "按文件类型各挑 2-3 个，不依赖任何单一源。"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="数据源普查")
    ap.add_argument("--label", default="", help="给这次普查起个名字（比如 '开启代理'）")
    ap.add_argument("--compare", action="store_true", help="和上一次对比")
    ap.add_argument("--list", action="store_true", help="只列出清单，不探测")
    ap.add_argument("--analyze", action="store_true",
                    help="把所有历史普查合起来分析（跨网络的稳定性）")
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--proxy", default="auto", choices=["auto", "manual", "none"])
    ap.add_argument("--only", default="", help="只测这几个（逗号分隔的 key）")
    args = ap.parse_args()

    if args.analyze:
        return analyze()

    if args.list:
        print(f"共 {len(ALL_SOURCES)} 个源：\n")
        for s in ALL_SOURCES:
            print(f"  {s.category:6} {s.key:12} {s.label}")
        return 0

    print("=" * 74)
    print("Ginan 数据源普查")
    print("=" * 74)
    if args.label:
        print(f"标记：{args.label}")
    print(f"代理：{net.ProxyConfig(mode=args.proxy).describe()}")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    only = [k.strip() for k in args.only.split(",") if k.strip()] or None
    results = survey(timeout=args.timeout, proxy_mode=args.proxy, only=only)

    print_table(results)

    s = summarize(results)
    print("\n" + "=" * 74)
    print(f"共 {s['total']} 个源：✓ 可用 {s['ok']} · 🔑 需账号 {s['auth']} · ✗ 不通 {s['fail']}")

    if args.compare:
        prev = load_previous()
        if prev:
            compare(results, prev)
        else:
            print("\n（没有历史记录可对比）")

    path = save_survey(results, args.label)
    print(f"\n结果已保存：{path.relative_to(ROOT)}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
