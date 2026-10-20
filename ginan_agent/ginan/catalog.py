"""Ginan 领域层 —— 数据源目录与文件清单。

============================ 这个模块解决的核心问题 ============================
**同一份数据，不同的用户能从不同的地方拿到。**

我们在开发机上测出"BKG 能连、悉尼 S3 连不上"，但那只说明**那台机器**。
换一个人的网络，结论可能完全反过来。

所以这里的结构是：

    一个文件  →  一组候选源  →  运行时探测哪个能用

**绝不写死"就用哪个源"。**

============================ 四条取数路径 ============================
这个模块负责前两条，路径定义见 docs/ginan-facts.md：

    ① 自动下载        多源尝试（本模块提供 URL）
    ② 代理后下载      同一套逻辑，只是走了代理
    ③ 缓存复用        一次性文件下过就不再下（one_time 标记）
    ④ 手动提供        ginan/inbox.py 负责（用户在浏览器里下好丢进目录）

============================ 一条重要的诚实原则 ============================
每个文件都带一句 `purpose` —— **给完全不懂 GNSS 的人看的人话用途**。

不是"SP3 精密星历文件"，而是"卫星在每一时刻的精确位置，
没有它就只能用广播星历，精度会差到米级"。

用户看不懂在下载什么，就不会信任这个工具。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from . import sources

# =============================================================================
# 数据源
# =============================================================================


@dataclass(frozen=True)
class Source:
    """一个数据来源。

    ⚠️ requires_auth 是个**重要的产品信息**，不只是技术标记 ——
    它决定了界面上要不要弹"请先注册账号"的引导。
    **能免账号就免账号**：新手卡在注册流程上的概率极高。
    """

    key: str
    label: str
    requires_auth: bool = False
    note: str = ""


# =============================================================================
# 源的排序 = 实测出来的，不是猜的
# =============================================================================
#
# ⚠️ 下面的顺序来自 **4 轮真实普查**（scripts/survey_sources.py），
#    分别在 4 种不同的代理环境下测的。
#    原始数据在 docs/surveys/ 里，可以用 `--analyze` 重新看。
#
# 为什么要强调这一点？
# 因为**猜的源排序会害死人**：
#   · 猜错了 → 用户第一步就卡住，而报错信息看不出是"这个源不行"
#   · 而且开发机上往往测不出来（我们的网络恰好能通）
#
# 实测结论（4/4 全绿 = 稳定）：
#   · 德国 BKG        ✓✓✓✓   精密产品首选
#   · IGS 官方分发点   ✓✓✓✓   精密产品备选
#   · 捷克 Pecný      ✓✓✓✓   精密产品备选
#   · 日本 GSI        ✓✓✓✓   精密产品备选
#   · 欧洲空间局 GSSC  ✓✓✓✓   精密产品备选
#   · GA 数据 API     ✓✓✓✓   观测数据首选
#   · Ginan 官方 S3   ✓✓✓✓   全部 tables/ 模型文件
#
# 实测淘汰（4 轮全挂，绝不写进默认）：
#   · 法国 IGN        ✗✗✗✗   持续超时
#
# ⚠️ 保留"淘汰者"的记录很必要 —— 否则将来有人会重新踩一遍。
#    **记录失败和记录成功一样重要。**
#
# ⚠️⚠️ 但反过来也成立：**过期的淘汰记录会挡住真正能用的源。**
#
#     上面原来还有一条「武汉大学 IGS ✗✗✗✗ 502 / SSL（不可用）」——
#     2026-09-20 复查发现它**早就能用了**，而且是中国大陆最快的源
#     （实测比 BKG 快 20~30 倍）。当年测挂的两个原因都很具体：
#       ① 试的是**不带 www** 的域名（那个确实死了）
#       ② https 的证书**真的过期了**（不是判成的"中间人拦截"），
#          而它 **http 完全正常** —— 用户早就知道"http 才好使"
#
#     👉 一份写死"这个源不行"的名单，**要么定期复查，要么就别写死**。
#        它省下的那点时间，远抵不上下一个人因此跳过它的代价。

SOURCES: dict[str, Source] = {
    "whu": Source(
        key="whu",
        label="武汉大学 IGS 数据中心",
        requires_auth=False,
        note=(
            "⚠️ 中国大陆访问最快的源，实测比 BKG 快 20~30 倍"
            "（同一个文件：武大 1792~3165 KB/s，BKG 47~60 KB/s）。"
            "精密产品 + 广播星历 + **观测数据**都有，还带 2022 年的老式短名。"
            "⚠️ 下载要走两步（先查清单拿 id 再下载），见 ginan/sources.py；"
            "响应尾部有调试垃圾，要截断，见 sources.strip_trailer。"
            "⚠️ **观测那条是第三种清单接口**（`obsList`，按站号查，见 "
            "`sources._whu_obs_find`）—— 和产品用的 `jmxlList`/`gbxlList` 不是一回事。"
            "实测 26 个站的样本里 21 个有，其中 9 个是 GA 没有的站"
        ),
    ),
    "igmas": Source(
        key="igmas",
        label="iGMAS 国际GNSS监测评估系统",
        requires_auth=False,
        note=(
            "⚠️ **备选产品**（不是 IGS 产品的镜像）—— 它给的是 iGMAS 自己"
            "解算的 `isc/isr/isu` 系列。只能用 http、不带 www 的域名解析不了。"
            "⚠️ 四个坑：响应开头有 BOM、gzip 流没有结束标记、连续下载会被限流、"
            "下载路径要 POST 检索 HTML 表格才能拿到。**是全部源里最脆的一个。**"
        ),
    ),
    "nrcan": Source(
        key="nrcan",
        label="加拿大 NRCan（CACS）",
        requires_auth=False,
        note=(
            "⚠️ **只用来补老日期**：老日期的 IGS 快速产品（`igr*`）原来只有 CDDIS 一个源，"
            "而 CDDIS 要账号 —— 这条路不要账号。"
            "✅ 2026-09-29 实测：多个周的 HEAD 200；真下载过一个 66,145 字节的 `.Z`，"
            "解开 156,019 字节、96 个历元、头部 `FIT EMR`。"
            "⚠️ 覆盖**不匀**（1100~1300 周 404、2180~2200 周 403），"
            "而且**慢**（10~13 KB/s）。⇒ 只是多一个选项，不是主力。"
            "⚠️ 新长名（2023 之后）它有没有，**没验过**，所以那种日期不给它。"
        ),
    ),
    "gssc": Source(
        key="gssc",
        label="欧洲空间局 GSSC（多分析中心镜像）",
        requires_auth=False,
        note=(
            "⚠️ **只有 FTP**（HTTPS 那条路 404）。它和现有的 `esa` 源**不是一回事**："
            "`esa` 只有 ESA 自己那一支，这里是**多 AC 的镜像**（同一个周目录里"
            "WUM/IGS/GRG/COD/JGX/GFZ/EMR/ESA/SIO/USN 都有），还带 `MD5SUMS`。"
            "✅ 2026-09-29 实测（经本程序自己的 ftp.py）：SP3 909,378 字节 → "
            "解压 2,056,044 字节、289 历元；CLK 5,033,751 字节；ERP 534 字节。"
            "⚠️ 这一版**只用它做 COD 那一支的镜像**（和 AIUB 同一份文件，"
            "头部都是 `FIT AIUB`）—— 多 AC 那一大片留给以后。"
            "⚠️ 实测**慢**（18 KB/s），而且**慢链路会截断**（下 CLK 时曾在 38% 处断掉）。"
        ),
    ),
    "noaa": Source(

        key="noaa",
        label="美国 NOAA/NGS（CORS 网）",
        requires_auth=False,
        note=(
            "⚠️ **补的是覆盖，不是速度**：实测 15~35 KB/s（2 MB 要两分钟），比谁都慢。"
            "武大有 IGS 站（FAIR/JPLM/PIE1/ALGO/NIST），但**没有** CORS 专有的那些"
            "（AB02/1LSU/AB06/AB04/P123）—— 美国本地站就靠它。"
            "✅ 2026-09-29 实测：`ab02` 2026 年第 270 天，1,969,666 字节 → 解压"
            "6,250,648 字节，是 **RINEX 2.11** 明文（本程序支持 RINEX 2）。"
            "⚠️ **2025 前的日期不给这个源** —— 那时只有 Hatanaka 压缩的 `.d.gz`，"
            "而本程序的 CrinEX 只支持 3.0。见 `NOAA_PLAIN_FROM`。"
            "⚠️ 实测**慢链路下会截断**（抓到过一个比 Content-Length 少的残缺文件），"
            "所以完整性校验必须真的生效。"
        ),
    ),
    "aiub": Source(

        key="aiub",
        label="瑞士伯尔尼大学 AIUB（CODE）",
        requires_auth=False,
        note=(
            "⚠️ **这不是 IGS 产品的镜像，是 CODE 分析中心自己的解算**"
            "（`COD0MGXFIN_…`）—— 所以它只出现在**备选**里，不在镜像里。"
            "换上它会改变结果（毫米到厘米级），要用户点头。"
            "✅ 2026-09-29 实测：SP3/CLK/ERP/BIA 四个文件都真实下载成功过"
            "（SP3 那 289 历元、116 颗卫星）。"
            "⚠️ 路径能**直接拼**（少见），但**慢**（22 KB/s），"
            "而且是**最终产品**、出得晚 —— 最近几天拿不到是正常的。"
        ),
    ),
    "whu_ftp": Source(
        key="whu_ftp",
        label="武汉大学 IGS 数据中心（FTP）",
        requires_auth=False,
        note=(
            "同一个数据中心的**另一条通道**。路径能直接拼、**没有尾部调试垃圾**，"
            "实测 255 KB/s（比 BKG 快 4~5 倍，但比武大的 HTTP 慢 5~10 倍）。"
            "⚠️ 留着它的理由不是快 —— 是 2024 那次改版只关掉了 HTTP 的目录树、"
            "FTP 没受影响，**两条路是会被分别改坏的**"
        ),
    ),
    "sio": Source(
        key="sio",
        label="美国 SIO / Scripps",
        requires_auth=False,
        note=(
            "匿名直连，按文件名即可下载（**不用两步查询**）。"
            "档案很全：新式长名 + 老式短名（一直到 1991 年），而且有**码偏差**"
            "（DCB/OSB 的 .BIA）—— 那是 Ginan 一直警告缺的东西。"
            "⚠️ BKG 对老日期没有档案，这个源补上了那个缺口"
        ),
    ),
    "bkg": Source(
        key="bkg",
        label="德国 BKG 数据中心",
        requires_auth=False,
        note="IGS 官方数据中心，免账号。4 轮普查全部可用，作为精密产品的首选源",
    ),
    "ga": Source(
        key="ga",
        label="澳大利亚 GA",
        requires_auth=False,
        note="澳测站观测数据，免账号，返回的已是解压后的明文。4 轮普查全部可用",
    ),
    "igs": Source(
        key="igs",
        label="IGS 官方分发点",
        requires_auth=False,
        note="files.igs.org，4 轮普查全部可用。精密产品的第一备选",
    ),
    "pecny": Source(
        key="pecny",
        label="捷克 Pecný 天文台",
        requires_auth=False,
        note=(
            "⚠️⚠️ **别接**（2026-09-29 实测）：FTP 有货，但**归档断在 GPS 周 2430"
            "（2026-08-02）**，之后全是 **0 字节空壳**（服务器照样回 226）；"
            "而且**没有 HTTP 通路**。起作用的只有那句「4 轮普查全部可用」——"
            "⚠️ **那是「服务器通」，不是「数据拿得到」**，两者差得很远。"
            "详见 SOURCES_REACHABLE 里的同一条。"
        ),
    ),
    "gsi": Source(
        key="gsi",
        label="日本国土地理院",
        requires_auth=False,
        note=(
            "⚠️⚠️ **对自动化不可行**（2026-09-29 实测）：路径规律摸到了，但**是三步"
            "POST 表单**，要带浏览器 User-Agent，而且**约二三十个请求后整台机器就被"
            "按 IP 掐掉**；官方还公告 FTP 站 2026-10-30 关停、之后只剩要账号的 SFTP。"
            "⚠️ **没有真下载成功过。** 起作用的只有那句「4 轮普查全部可用」——"
            "那是「服务器通」，不是「数据拿得到」。"
        ),
    ),
    "esa": Source(
        key="esa",
        label="欧洲空间局 GSSC",
        requires_auth=False,
        note="4 轮普查全部可用",
    ),
    "ginan_s3": Source(
        key="ginan_s3",
        label="Ginan 官方数据仓（悉尼）",
        requires_auth=False,
        note=(
            "「完整」档要的模型文件只有这里有。"
            "⚠️ 原来这里写着'无代理时超时，有代理才能用' —— **那条记录是错的**。"
            "2026-09-20 实测：区域端点确实不通，但**全球端点 peanpod.s3.amazonaws.com "
            "直连 200、1.9 秒**。同一个桶。换域名就够了，不用挂代理。"
        ),
    ),
    "cddis": Source(
        key="cddis",
        label="NASA CDDIS",
        requires_auth=True,
        note=(
            "IGS 主数据中心，需要注册 Earthdata 账号。"
            "4 轮普查中 3 轮可达、1 轮被代理拦截 —— "
            "**能在有账号时用，但不能作为唯一依赖**"
        ),
    ),
    # ---- 实测不稳定，只作最后的备选 ----
    "aiub": Source(
        key="aiub",
        label="瑞士伯尔尼 AIUB",
        requires_auth=False,
        note="4 轮中 1 轮被代理的 SSL 中间人拦掉，只能当备选",
    ),
}

# 已知不可用 —— **留在这里是为了防止将来有人重新踩**
#
# ⚠️ 不要因为"看起来是个好源"就把它加回 SOURCES。
#    加之前先跑一遍 survey_sources.py。
SOURCES_KNOWN_BAD: dict[str, str] = {
    "法国 IGN": "4 轮普查全部超时（含开代理）",
    # ⚠️⚠️ 「武汉大学 IGS」2026-09-20 **从这张表里移走了** —— 它现在能用，
    #     而且是中国大陆最快的源（见 SOURCES["whu"]）。
    #
    #     原来记的是"4 轮普查全部失败：http 502、https 被 SSL 中间人拦截"。
    #     那**当时是真的**，但结论已经过期了，过期原因是两层：
    #
    #       ① 域名：`igs.gnsswhu.cn`（不带 www）确实死了，
    #          但 **`www.igs.gnsswhu.cn` 是好的** —— 当时没试带 www 的
    #       ② https 的证书**真的过期了**（不是中间人），
    #          而它 **http 完全正常** —— 用户早就知道这一点
    #
    #     👉 留在这里**不是记历史，是留了个坑**：
    #        下一个人看到"不要再尝试这条路"，就会跳过唯一一个
    #        大陆直连能跑到 3 MB/s 的源。
    #        **一条过期的"不可用"记录，比没有记录更糟。**
    "澳大利亚 GA GNSS 门户": "4 轮中 1 轮被代理拦截，不满足'稳定'标准",
}


# =============================================================================
# 文件种类
# =============================================================================


@dataclass(frozen=True)
class FileKind:
    """一类文件的说明 —— 用给人话写给新手看。"""

    key: str
    label: str          # 短名字，界面上显示
    purpose: str        # 一句话用途，给完全不懂的人看


KINDS: dict[str, FileKind] = {
    "atx": FileKind(
        "atx", "天线改正表",
        "记录每种天线的相位中心偏差。缺了它，测出来的位置会系统性偏几厘米。",
    ),
    "sp3": FileKind(
        "sp3", "卫星轨道",
        "每颗卫星在每一时刻的精确位置。这是'精密'定位里'精密'的来源 —— "
        "没有它就只能用广播星历，精度会掉到米级。",
    ),
    "clk": FileKind(
        "clk", "卫星钟差",
        "卫星上的钟不完美，这份文件记录它每一时刻偏了多少。"
        "1 微秒的钟差对应 300 米的位置误差，所以它和轨道同等重要。",
    ),
    "erp": FileKind(
        "erp", "地球自转参数",
        "地球自转不是匀速的，这份文件记录它的微小摆动。用来把卫星坐标换算到地面坐标。",
    ),
    "nav": FileKind(
        "nav", "广播星历",
        "卫星自己播发的轨道参数。精度不如精密星历，但用于预处理和数据质量检查。",
    ),
    "obs": FileKind(
        "obs", "观测数据",
        "你的测站接收到的卫星信号。这是**你要处理的对象**，其他都是辅助。",
    ),
    # ---- 以下两类是 2026-09-20 加的，为的是消掉 Ginan 每次都在打的两族警告 ----
    "snx": FileKind(
        "snx", "卫星元数据",
        "记录每颗卫星**属于哪个型号**（Block IIR / IIF / Galileo…）和它的姿态参数。"
        "缺了它，卫星的姿态只能用通用模型凑合 —— 地影期那几段会算得糙一点。",
    ),
    "bia": FileKind(
        "bia", "码偏差",
        "卫星和接收机的**信号延迟偏差**（同一个信号，不同卫星/不同接收机之间"
        "会有几纳秒的差异）。不改正的话，伪距观测量带着一个说不清的偏移。",
    ),
    # ⚠️ 2026-09-21 加的。**这一类下不到** —— 没有哪个源提供气象文件
    #    （那是各测站自己观测出来的，不是 IGS 产品）。
    #    加它是因为**用户手上**会有这种文件（他那一箱里有 9 个），
    #    而 `identify` 原来认不出来，把它们报成"认不出来"。
    #
    #    ⚠️ 而且**现在还没接进 Ginan 配置**（`inputs` 里没有 met，
    #       对流层走的是简化模型 / 外部格网）。所以它只是"认得出、
    #       留得住"，**不代表会参与解算**。这一点在给用户的说法里要带上，
    #       否则就成了"程序收下了我的气象文件，应该用上了吧"。
    "met": FileKind(
        "met", "气象数据",
        "测站当地的**气温/气压/湿度**观测（RINEX MET）。"
        "本程序目前**还没有把它接进解算**（对流层用的是模型，不是实测气象），"
        "所以它是认得出来、能存下来，但暂时不参与计算。",
    ),
    # ---- 2026-09-20 加的：为「完整」档解锁 ----
    "table": FileKind(
        "table", "模型文件",
        "地球重力场、日月位置、海潮负荷、大气格网这些**静态模型**。"
        "它们描述的是物理世界本身，不随日期变 —— 所以**只下一次，以后一直用**。"
        "「基础」档不需要它们，靠 Ginan 内置的简化模型就够。",
    ),
    # ---- 2026-09-27 加的：为 SBAS 解锁 ----
    "ems": FileKind(
        "ems", "SBAS 改正信息",
        "**星基增强系统**（SBAS）从地球同步卫星播发的改正信息 —— "
        "它告诉接收机「刚才那个卫星的轨道和钟差偏了多少」。"
        "有了它，普通单点定位能从几米提升到米级甚至更好。"
        "\n"
        "⚠️ 这类文件**没有公开的日常源** —— 只有 SBAS 运营方播发，"
        "而播发出去就没了。本项目用的是 Ginan 官方那份**录下来的演示数据**"
        "（2025-11-11 的 7 小时），所以 SBAS 只能用那一份数据跑。",
    ),
}


# 配置里的**键名** → 它属于哪一类文件（`KINDS` 的键）。
#
# ⚠️⚠️ 存在的理由：**同一类文件在不同工作流里落在不同的键上。**
#
#     SP3 在 PPP 里是"已知的卫星位置"      → `inputs.satellite_data.sp3_files`
#     SP3 在 POD 里是"当成观测值，参与估计" → `inputs.pseudo_observations.sp3_inputs`
#
#     同一类文件、换个位置 —— 而"估"和"已知"的分界线正在这里。
#     所以**两个键名都要在表里**：漏掉 `sp3_inputs` 的话，
#     "POD 的取数计划里没有 SP3"这个 bug 就测不出来（而它真的发生过）。
#
# ⚠️ 值的 `None` 是**故意的**，它表示"这个键不归这份表管"：
#     `egm_files` 走的是另一条链（`WORKFLOW_TABLES`），由别处守。
#
# ⚠️ 两个调用方，**一张表**：
#     · `config._substitutions` —— 工作流不要的类别就别写进配置
#     · `check_toolchain` 的 ⑱j  —— "配置里用到的类别" ⊆ "计划里会下的类别"
#   各写一份的话两边迟早漂开，而那正是这个仓库反复记着的坑。
KEY_KINDS: dict[str, str | None] = {
    "atx_files": "atx",
    "erp_files": "erp",
    "sp3_files": "sp3",
    "sp3_inputs": "sp3",
    "clk_files": "clk",
    "nav_files": "nav",
    "rnx_inputs": "obs",
    "snx_files": "snx",
    "bsx_files": "bia",
    # ⚠️ SBAS 的改正信息。**它没有日常源**（见 `KINDS["ems"]`），
    #    只有官方那份演示数据集里有 —— 所以这条只在 SBAS 那三个工作流上出现。
    "ems_files": "ems",
    "egm_files": None,
}


# =============================================================================
# 固定演示数据集
# =============================================================================
#
# ⚠️⚠️ 这是**第一个"数据不由用户选"的东西**，所以值得说清它和上面那些的区别。
#
#     前面每一条工作流都是「**某站某天**，去 IGS/GA/武大那些源拿」——
#     `plan_run(stations, day, workflow)` 的三个参数都由用户给。
#
#     而 SBAS 的改正信息（`.ems`）**没有任何公开的日常源**：它由 SBAS 运营方
#     从地球同步卫星播发，播完就没了，不归档。市面上没有"SBAS 的 IGS"这种东西。
#
#     ⇒ 能拿到的只有**别人录下来的一段**。Ginan 官方那份是
#       **2025-11-11（DOY 315）00:00–06:45、四个测站**，放在自己的 S3 桶里。
#
#     ⇒ 所以 SBAS 在 app 里的形状只能是「**跑一次官方演示**」，
#       不是「处理你自己的数据」。这不是偷懒，是数据的物理性质 ——
#       和 `rt_ppp`（活流不归档）是同一类，只是那一条连录像都没有。
#
# ⚠️ 因此 `plan_run` 见到带 `dataset` 的工作流时，会**忽略**调用方给的
#    `stations` 和 `day`（数据集自己带）。用户说"给 ALIC 跑 SBAS"时，
#    如实告诉他"SBAS 只有官方那 4 个站那 7 小时"，而不是硬跑一个空结果。


@dataclass(frozen=True)
class DatasetFile:
    """数据集里的一份文件。`key` 是**桶里的相对路径**（不带 `.gz`）。"""

    key: str
    kind: str
    size_hint: str
    note: str = ""


@dataclass(frozen=True)
class Dataset:
    """一份**固定的**演示数据集 —— 日期和测站都是它自己带的。"""

    key: str
    label: str
    purpose: str        # 一句话用途，写给完全不懂的人看
    day: date
    stations: tuple[str, ...]
    window: str         # 覆盖的时段，给人看的
    files: tuple[DatasetFile, ...]
    """每份文件的 `key` 是 **`aux/` 下的相对路径**（不带 `.gz`）。
    URL 由 `dataset_files()` 拼 —— 它在本模块后面（`GINAN_S3_BASE` 之后）。"""
    note: str = ""

    @property
    def total_mb(self) -> float:
        """大概要下多少 MB —— **给用户看的那种大概**，不是精确值。

        ⚠️ 取的是每个文件的 `size_hint` 里的数字，而那些是**实测**的
           （见 `sbas_demo()` 的说明）。给的是一个量级判断，
           不是"还差多少字节"——后者只有下完才知道。
        """
        total = 0.0
        for f in self.files:
            for tok in f.size_hint.replace(",", "").split():
                try:
                    total += float(tok)
                    break
                except ValueError:
                    continue
        return total


# SBAS 演示数据集覆盖的那一天。**DOY 315 = 2025-11-11**。
_SBAS_DOY = 315

# 四个测站，以及它们**文件名里的类型字符**。
#
# ⚠️⚠️ `OUS2` 是 `R`，另外三个是 `S` —— **这是实测的，不是规律**。
#     一开始我按"SBAS 数据所以是 `S`"去拼，于是 OUS2 那份 404；
#     逐个对官方 `data.list` 才发现它是 `R`。
#     所以这一栏是**照抄清单**，不是从"SBAS"这个词推出来的。
_SBAS_STATIONS = (
    ("HOB2", "AUS", "S"),
    ("NEBO", "AUS", "S"),
    ("OUS2", "NZL", "R"),
    ("TID1", "AUS", "S"),
)


def _sbas_files() -> tuple[DatasetFile, ...]:
    """SBAS 数据集要下的全部文件。

    ⚠️⚠️ **这里是用公式生成的 —— 但公式是量出来的，不是猜的。**

       实测（对官方 `data.list` / `products.list` 逐个匹配）：

            观测  112 个 = 4 站 × 7 小时 × 4 个 15 分钟
                  名字形状 `{站}00{国}_{S|R}_2025{DOY}{HH}{MM}_15M_01S_MO.rnx`
                  **112/112 全对得上**，小时恰好 00–06、分钟恰好 00/15/30/45
            ems    48 个 = 2 个频段 × 24 小时，`{band}/h{HH}.ems`
            广播    2 个 = DOY 314 和 315 各一份

       ⇒ 正因为量过，才敢生成。守卫 `test_sbas_dataset_matches_the_manifest`
         会在**有 Ginan 源码树时**拿这份清单和官方清单逐条比 ——
         公式错一位它立刻红。

    ⚠️ **不含 `AUS23907.SNX`（160 MB）。** 实测：带上它和不带它，
       L1 的 SPP 产物 **3385 行逐行相同**。它里面确实有那 4 个站的元数据，
       但四份示例又在 `receiver_options` 里显式写了 `antenna_type`，
       所以它在这儿没有用武之地。**不下它省 160 MB。**
    """
    out: list[DatasetFile] = []

    # ---- 观测：4 站 × 7 小时 × 4 个 15 分钟 ----
    for st, ctry, typ in _SBAS_STATIONS:
        for hh in range(7):
            for mm in (0, 15, 30, 45):
                out.append(
                    DatasetFile(
                        f"data/sbas/{st}00{ctry}_{typ}_2025{_SBAS_DOY}"
                        f"{hh:02d}{mm:02d}_15M_01S_MO.rnx",
                        kind="obs",
                        size_hint="约 16.7 MB",
                        note="15 分钟的 1 秒采样观测",
                    )
                )

    # ---- SBAS 改正信息：两个频段各 24 小时 ----
    for band, what in (
        ("uraL1", "L1 频段的 SBAS 改正（单频服务）"),
        ("uraL5", "L5 频段的 SBAS 改正（双频多星座 / 精密定位用）"),
    ):
        for h in range(24):
            out.append(
                DatasetFile(
                    f"products/sbas/{band}/h{h:02d}.ems",
                    kind="ems",
                    size_hint="约 0.33 MB",
                    note=what,
                )
            )

    # ---- 广播星历：DOY 314 和 315 ----
    #
    # ⚠️ 为什么 DOY 315 的数据还要 **314** 那份：跨天的星历要前一天的
    #    才能算（每条广播星历的有效期是几小时，00:00 那一批是前一天播的）。
    for doy in (_SBAS_DOY - 1, _SBAS_DOY):
        out.append(
            DatasetFile(
                f"products/sbas/BRDC00IGS_R_2025{doy:03d}0000_01D_MN.rnx",
                kind="nav",
                size_hint="约 12.5 MB",
                note="多系统广播星历（24 小时一份）",
            )
        )

    return tuple(out)


def sbas_demo() -> Dataset:
    """Ginan 官方那份 SBAS 演示数据。"""
    return Dataset(
        key="sbas_2025_315",
        label="SBAS 官方演示数据（2025-11-11）",
        purpose=(
            "**星基增强系统**（SBAS）的演示数据。SBAS 是从地球同步卫星"
            "播发定位改正信息的系统 —— "
            "澳大利亚和新西兰那套叫 **SouthPAN**。"
            "Ginan 官方录下了它 2025-11-11 早上 7 小时的信息和四个测站的观测。"
        ),
        day=date(2025, 11, 11),
        stations=("HOB2", "NEBO", "OUS2", "TID1"),
        window="2025-11-11 00:00 – 06:45 UTC",
        files=_sbas_files(),
        note=(
            "⚠️ 这是**固定的一份**，不是「选个日期就能跑」—— "
            "SBAS 的改正信息播完就不归档，市面上没有日常源。"
        ),
    )


DATASETS: dict[str, Dataset] = {
    "sbas_2025_315": sbas_demo(),
}


def dataset(key: str | None) -> Dataset | None:
    """按 key 取数据集。取不到返回 None（**不猜、不降级**）。"""
    return DATASETS.get((key or "").strip().lower())


# =============================================================================
# 计划里的一项
# =============================================================================


@dataclass
class FilePlan:
    """"要拿到一个文件"这件事的完整描述。

    一个 FilePlan 自带：
      · 最终落到磁盘上的文件名
      · 从哪些源能拿到（源 key → URL）
      · 人话用途
      · 是不是一次性文件
    """

    filename: str
    kind: str
    urls: dict[str, str] = field(default_factory=dict)
    size_hint: str = "未知"
    # ⚠️ 这个文件属于**哪一天**。
    #
    #    为什么计划里要带日期？因为 `urls` 里的值**不一定是一个能直接 GET 的
    #    URL** —— 有的源（武汉大学）要先查一次文件清单才知道地址，
    #    而查清单需要日期。见 ginan/sources.py。
    #
    #    ⚠️ 不从这个字段推别的东西（文件名里也含着日期，但**解析文件名
    #       来还原日期**是在制造一个"两处真相"，迟早会对不上）。
    day: date | None = None
    # ⚠️ 一次性 = 下载后长期有效，缓存住就不必再下。
    #    这类文件**不该**让用户等第二遍 —— 61 MB 的天线文件下载两次很烦。
    one_time: bool = False

    # 这一个文件的用途。**留空就用 kind 那层的通用说明。**
    #
    # ⚠️ 为什么需要它：像 tables/ 那样一类里有十个各不相同的文件时，
    #    "模型文件：描述物理世界本身..."这种话对用户没有信息量 ——
    #    他真正想知道的是"**为什么要下这 8.7 MB 的 DE436**"。
    #    而那个答案每个文件都不一样，只能一个一个写。
    note: str = ""

    # 落盘名**照计划里的来**，不要从文件内容推。
    #
    # ⚠️⚠️ 默认行为是：观测数据的落盘名取自**文件头里的 MARKER NAME**
    #     （`fetch._decide_name`）—— 那是为了治"用户从浏览器下的文件叫
    #     `igs20(1).atx`"这类脏名字，对散装文件是对的。
    #
    #     但**数据集来的文件名字是我们自己定的**（照抄官方清单），
    #     而且名字本身带着信息。SBAS 那 112 个观测只来自 4 个站，
    #     按默认规则会变成 `HOB2.rnx`、`HOB2_2.rnx`……`HOB2_28.rnx` ——
    #     **116 个文件里一个时间信息都不剩**。
    #
    #     ⇒ 标了 `keep_name` 的，名字由计划负责，`_decide_name` 不插手。
    keep_name: bool = False

    @property
    def purpose(self) -> str:
        if self.note:
            return self.note
        return KINDS[self.kind].purpose if self.kind in KINDS else ""

    @property
    def label(self) -> str:
        return KINDS[self.kind].label if self.kind in KINDS else self.kind

    @property
    def sources(self) -> list[str]:
        return list(self.urls)


@dataclass
class RunPlan:
    """一次运行的完整取数计划。

    ============================ ⚠️ 为什么是 `stations` 而不是 `station` ============================
    Ginan 的**网络解**就是"把多个测站放进同一个滤波器" ——
    在配置里表现为 `gnss_observations.rnx_inputs` 里列多个文件、
    `receiver_options` 里按站名分多个块。

    而**单站解就是这个东西的 N=1 特例**，不是另一个东西：

        · 同一个 `plan_run`
        · 同一份配置结构
        · 同一条跑法

    ⚠️ 所以这里**不保留** `station` 那个单数字段。
       留一个"单站时读这个、多站时读那个"的字段对，等于留两条路 ——
       而两条路里**总有一条长期没人走**，然后悄悄坏掉
       （这个仓库已经为这个形状付过几次学费了）。
    """

    stations: list[str]          # 4 位站号，**顺序 = 配置里的顺序**
    day: date
    files: list[FilePlan] = field(default_factory=list)

    # ---- 备选产品（**默认不下载**）----
    #
    # ⚠️⚠️ 这是**另一个东西**，不是"多一个镜像"。区别是本次改动里最重要的一条：
    #
    #     镜像：  `IGS0OPSRAP_…_ORB.SP3` 在武大有、在 SIO 有、在 BKG 有
    #              → **同一个文件**，谁快用谁，结果一模一样
    #     备选：  `ESA0OPSFIN_…_ORB.SP3`
    #              → **另一个分析中心的解算结果**，文件名都不一样
    #
    #     把它塞进 `urls` 里当普通源，会出两件坏事：
    #       ① 落盘名和内容对不上（下载器按请求名命名，于是 ESA 的数据
    #          存成了 IGS 的名字）—— 配置里写着 IGS，跑的却是 ESA
    #       ② **用户永远不会知道**这次的解是用谁的产品算的
    #
    #     👉 所以它单独一栏，而且**默认不下**（`fetch_plan` 不碰它）。
    #        只有镜像全挂了、**并且用户点头**之后，才会走这条路。
    #        见 `ginan_tools.do_fetch_data`。
    #
    # ⚠️ 存的是 **kind → {源: 待解析标记}**，**不是** FilePlan ——
    #    因为备选产品的**真名要查了才知道**（ESA 的前缀随年代变：
    #    2026 是 `ESA0OPSFIN_`，2022 是 `ESA0MGNFIN_`）。
    #    在计划阶段就得写死一个名字的话，只能猜 —— 而这个项目的规矩是
    #    **不猜**。所以这里只声明"这个类型有备选、去哪查"，
    #    真名在 `alternative_files()` 里解析出来。
    alternatives: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def missing_from_all_sources(self) -> list[FilePlan]:
        """一个源都没有的文件 —— 说明我们压根不知道去哪拿。"""
        return [f for f in self.files if not f.urls]

    def describe(self) -> str:
        """给模型看的一行摘要。"""
        n = len(self.files)
        one = sum(1 for f in self.files if f.one_time)
        n_st = len(self.stations)
        who = "、".join(self.stations) if n_st <= 3 else (
            "、".join(self.stations[:3]) + f" 等 {n_st} 个站"
        )
        # ⚠️⚠️ 措辞不能叫"网络解" —— **实测：多站的结果和单站逐位相同。**
        #
        #     原因（不是 bug）：这份配置里**轨道和钟差是固定输入**
        #     （SP3/CLK），每个接收机的待估参数（位置、钟差、对流层、
        #     模糊度）都不依赖别的站 —— 数学上本来就该一样。
        #
        #     ⚠️ 要测站之间**真的互相约束**，得让它估计**共同参数** ——
        #        那正是 POD（估卫星轨道/钟差）和电离层建模（估公共电离层）
        #        在做的事，不是"在 ppp 模式里多放几个站"。
        #
        #     > 叫它"网络解"就是**说了一件没做到的事**。
        #     > 而"程序说它做到了"比"它没做到"更难发现 ——
        #     > 用户会以为自己拿到了联合平差的解。
        what = "多站（一次算完）" if n_st > 1 else "站"
        return (
            f"{who} {self.day.isoformat()}：{what}，"
            f"共 {n} 个文件（其中 {one} 个是一次性的天线文件，缓存后可复用）"
        )


# =============================================================================
# 日期计算
# =============================================================================

_GPS_EPOCH = date(1980, 1, 6)


def gps_week(d: date) -> int:
    """算出某天属于第几个 GPS 周。

    GPS 纪元是 1980-01-06。IGS 的产品目录就是按 GPS 周分文件夹的。
    """
    return (d - _GPS_EPOCH).days // 7


def year_doy(d: date) -> str:
    """YYYYDDD 格式 —— IGS 文件名里用的日期格式。

    例：2026-09-02 → "2026245"
    """
    return f"{d.year}{d.timetuple().tm_yday:03d}"


def gps_dow(d: date) -> int:
    """GPS 星期几（0 = 周日）。

    ⚠️ 旧式短文件名里要它：`igr21992.sp3.Z` 的最后一个数字
       就是星期几。GPS 的"周"从**周日**算起，和 ISO 的周一大不相同 ——
       用 `d.weekday()` 会**整体差一天**，而差一天的后果是下错文件：
       Ginan 拿到隔壁那天的轨道，算出来的位置偏几米，
       **而且不报任何错**。

    例：2022-03-01（周二）→ 2
    """
    return (d - _GPS_EPOCH).days % 7


# =============================================================================
# 旧式文件名（短名）
# =============================================================================
#
# ⚠️⚠️ IGS 在 **GPS 周 2237**（约 2022-11-27）换了命名规则。
#     在那之前是短名 `AAAWWWWD.TYP.Z`：
#
#         igr  2199  2  .sp3  .Z
#         │    │     │  │    └── Unix compress 压缩
#         │    │     │  └─────── 类型
#         │    │     └────────── GPS 星期几（0=周日）
#         │    └──────────────── GPS 周
#         └───────────────────── 分析中心：igs 最终 / igr 快速 / igu 超快
#
#     之后的**新式长名**是 `IGS0OPSRAP_20220600000_01D_15M_ORB.SP3.gz`。
#     两套名字**互不通用** —— 拿错一套就是 404。
#
# ⚠️ 阈值 2237 出自**上游 GinanUI**
#     （ginan-dist/source/scripts/GinanUI/app/models/dl_products.py）：
#         elif gps_week < 2237:
#             # Old naming convention for non-repro3 products before week 2237
#
#     ⚠️ 注意 CDDIS 对**存在和不存在的路径都返回 302**（跳到登录页），
#        所以"试一下 URL 通不通"**不能**用来验证文件名。
#        上面这个阈值是读上游源码得来的，不是我试出来的。
OLD_NAMING_BEFORE_WEEK = 2237

# ⚠️ BKG 的存档是**有洞的**，不是连续的。
#    实测（2026-09-19 直接列它的目录）：
#      · 它一共有 205 个周目录
#      · 2236 → 2436 是连续的，往前只有 860/999/1048/1054/1530 几个孤点
#      · 我们要的 2199 **不在里面**
#
#    所以给老日期配 BKG 这个源**只会白等超时** —— 而且更糟：
#    它的 404 会被读成"产品还没发布"，把人引向完全错误的方向。
BKG_PRODUCTS_FROM_WEEK = 2238

# 码偏差（OSB.BIA）从这一周开始才有。
#
# ⚠️ 实测（在武大逐周查 `COD0OPSRAP_`，一天一天试）：
#
#       周 2231（2022-10-15）  0 条
#       周 2236（2022-11-15）  0 条
#       周 2237（2022-11-22）  0 条
#       周 2238（2022-11-27）  28 条   ← 从这儿开始
#
# ⚠️⚠️ 它和 `BKG_PRODUCTS_FROM_WEEK`（2238）、
#     `OLD_NAMING_BEFORE_WEEK`（2237，即 2238 起换长命名）**是同一个点**。
#     **不是巧合** —— IGS 在那一周同时改了命名、BKG 开始镜像产品、
#     偏差文件开始发布。那是 IGS 的一次整体换代。
#
# 👉 所以给老日期配偏差源 = 白等一次超时 + 一条"下载失败"，
#    而那条失败会让人去查一个根本不存在的东西。
BIAS_FROM_WEEK = 2238


def _old_product_name(d: date, typ: str) -> str:
    """旧式短名。**不含 `.Z` 后缀** —— 那是 URL 的事，不是文件的事。

    ⚠️ 这里返回的是**解压之后**该叫什么。
       `igr21992.sp3.Z` 解开之后是 `igr21992.sp3`。
       如果连 `.Z` 一起当文件名，Ginan 的配置里就会引用一个
       带着压缩后缀、而磁盘上并不存在的名字。
    """
    return f"igr{gps_week(d)}{gps_dow(d)}.{typ}"


def _product_names(d: date) -> dict[str, str]:
    """这一天的 sp3/clk/erp 该叫什么名字（不含压缩后缀）。

    ⚠️ 名字和 URL 必须**从同一个地方**取。
       原来 `plan_run` 里的 filename 和 `_cddis_urls` 里的 URL 基名
       是**两个独立拼出来的字面量** —— 只改一边，缓存 glob、
       配置引用、下载落盘的名字就全对不上了。
    """
    if gps_week(d) < OLD_NAMING_BEFORE_WEEK:
        return {t: _old_product_name(d, t) for t in ("sp3", "clk", "erp")}
    yd = year_doy(d)
    return {
        "sp3": f"IGS0OPSRAP_{yd}0000_01D_15M_ORB.SP3",
        "clk": f"IGS0OPSRAP_{yd}0000_01D_05M_CLK.CLK",
        "erp": f"IGS0OPSRAP_{yd}0000_01D_01D_ERP.ERP",
    }


# =============================================================================
# 各源的 URL 构造
# =============================================================================


# =============================================================================
# ⚠️ 一个重要的区分：源"可达" ≠ 知道"数据在哪"
# =============================================================================
#
# 普查测的是**第一层**：这台服务器通不通。
# 但产品需要的是**第二层**：这个具体文件的 URL 是什么。
#
# 实测发现这两层差别很大：
#
#   捷克 Pecný   服务器 200 正常    但 /LDC/、/igs/ 全 404，猜不出结构
#   日本 GSI     服务器 200 正常    但 /data/ 404
#   澳大利亚 GA 门户  200 正常      但那是给浏览器看的页面，不是数据接口
#
# 而它们的真实目录结构**不是标准化的** —— 每个数据中心自己定。
# 猜路径的后果是把一个 404 当成"文件还没发布"，浪费用户时间。
#
# 所以下面的 URL 只写**实测下载成功过**的。
# 其余的记在 SOURCES_REACHABLE 里，等将来有时间逐个摸清结构再启用。
#
# > 原则：**宁可少一个源，不要多一个错的源。**
#
#   Pečlivě 确认过的 3 个源，比 10 个猜的源有用得多。


def _bkg_urls(d: date) -> dict[str, str]:
    """德国 BKG 的 URL。

    ✅ 实测验证：SP3 / CLK / ERP 三个文件都真实下载成功过（M0.5）。

    ⚠️ 用的是 RAP（快速产品）而不是 FIN（最终产品）。
    对 PPP 完全够用，而且**出得早** —— 昨天的数据今天就能算。

    ⚠️⚠️ **老日期返回空字典，不配这个源。**

        BKG 的存档有洞（见 `BKG_PRODUCTS_FROM_WEEK` 上面的实测记录）。
        给它配一个 404 的源不只是"浪费时间"：还会**误导**——
        下载结果里会出现"德国 BKG 数据中心：HTTP 404"，而
        探测层把 404 判成"源是可达的，只是这个文件没有（可能日期太新）"。
        于是一个"这个源根本没有那天数据"的事实，
        被读成了"产品还没发布，再等等"。
    """
    if gps_week(d) < BKG_PRODUCTS_FROM_WEEK:
        return {}

    w = gps_week(d)
    names = _product_names(d)
    base = f"https://igs.bkg.bund.de/root_ftp/IGS/products/{w}"
    return {
        "sp3": f"{base}/{names['sp3']}.gz",
        "clk": f"{base}/{names['clk']}.gz",
        "erp": f"{base}/{names['erp']}.gz",
    }


# 瑞士伯尔尼 AIUB（CODE 分析中心）的地址前缀。
#
# ⚠️ 入口是 `https://www.aiub.unibe.ch/download/`，但那个页面**直接跳**到
#    一个 S3 桶列表 `https://code.aiub.unibe.ch/s3_script/aiub_s3_bucket_listing.php`
#    —— 文件真正的地址在 `www.aiub.unibe.ch/download/<桶内路径>`，
#    并且会再 301 到 `zhw-b.s3.cloud.switch.ch`（瑞士的 S3）。
#    ⚠️ **所以下载必须允许跟跳转**（`fetch` 那条路本来就跟）。
AIUB_BASE = "https://www.aiub.unibe.ch/download/CODE_MGEX/CODE"


# 美国 NOAA/NGS 的 CORS 观测：**明文 RINEX 从这一天起才有**。

# ⚠️ 实测（2026-09-29，站点 ab02）：它同一天挂两种文件 ——
#
#        2024/300   类型位 o -> **404**   类型位 d -> 200
#        2024/365   类型位 o -> 200       类型位 d -> 200
#        2025/001   类型位 o -> 200       类型位 d -> 200
#
# ⇒ 分界在 2024 年第 300~365 天之间。**这里取 2025-01-01，偏保守**。
#
# ⚠️⚠️ **为什么不能用类型位 `d` 那份**：那是 Hatanaka 压缩的 **RINEX 2**，
#    而本程序的 `crinex.py` **只支持 3.0**（它自己的说明里写着）。
#    ⇒ 老日期**不给这个源**，而不是给一个下游解不开的文件。
#
# ⚠️ RINEX 2 短名是 `站号+年积日+序号.年+类型`（`ab022700.26o.gz`）——
#    所以判据是**末尾的 `o.gz`**，不是 `.o.gz`（`.` 属于年字段）。
NOAA_PLAIN_FROM = date(2025, 1, 1)

# NOAA CORS 的 RINEX 根
NOAA_CORS_BASE = "https://geodesy.noaa.gov/corsdata/rinex"


def _noaa_obs_url(station: str, day: date) -> str | None:
    """美国 NOAA/NGS CORS 的观测文件地址。**能直接拼**，不用查询。

    ✅ 2026-09-29 实测下载成功（`ab02`，2026 年第 270 天）：
       1,969,666 字节 -> 解压 6,250,648 字节，
       头是 `2.11  OBSERVATION DATA  G (GPS)  RINEX VERSION / TYPE`。

    ⚠️ 站号**必须小写**，而且要**原样**（CORS 的站号有 4 位的也有 5 位的：
       `ab02` / `1lsu`）—— 不要补齐也不要截断。

    ⚠️ 实测**慢**：15~35 KB/s（2 MB 的文件要两分钟）。
       交给测速排序去处理（`fetch._rank_by_speed`）就行。

    ⚠️ 实测**错误路径给干净的 404**（不存在的站/日期/文件都是 404），
       不会用 200 返回首页 —— 所以"200 就是真的"。
    """
    if day < NOAA_PLAIN_FROM:
        return None
    st = station.strip().lower()
    if not st:
        return None
    ddd = f"{day.timetuple().tm_yday:03d}"
    yy = f"{day.year % 100:02d}"
    return f"{NOAA_CORS_BASE}/{day.year}/{ddd}/{st}/{st}{ddd}0.{yy}o.gz"


# ---- 美国 NOAA/NGS（CORS 观测）----

# ⚠️ 加它的理由是**覆盖**，不是速度（实测 15~35 KB/s，比谁都慢）：
#    实测武大有 IGS 站（FAIR/JPLM/PIE1/ALGO/NIST），
#    但**没有** CORS 专有的那些（AB02/1LSU/AB06/AB04/P123）。
#    所以它补的是「美国本地 CORS 站」这一块。

# ⚠️⚠️ **这里的顺序只是「测速失败时的兜底」** —— 真正决定先下哪个的是
#    `fetch` 里的**实测测速**（`_rank_by_speed`）。
#    为什么会需要测速：同一份观测实测差 **44 倍**
#    （2026-09-25 WUH2：武大 1305 KB/s vs GA 22 KB/s），
#    根因是 GA 的观测下载走 AWS S3 `ap-southeast-2`。

#    ⚠️⚠️ **「谁排第一」写死在代码里必然是错的** —— 换个地区就可能反过来。
#       第一版我写的就是「武大排第一」，还配了条守卫守着那个顺序；
#       用户点破了：「不是谁在前，是测一下速，用最快的」。
#       ⇒ 顺序降级成兜底，守卫改成守**行为**（见 `check_network` ⑯）。

# ⚠️ 武大那条**按站号查**（文件名事先不知道），见 `sources._whu_obs_find`：
#    2026-09-29 实测 26 个站的样本里 **21 个有**，其中 9 个是 GA 没有的。
#    只有老式短名（RINEX 2）的站会被判成「没有」—— 那是**对的**
#    （本程序的 CrinEX 只支持 3.0）。

# ⚠️ GA 那份**不用解码**（它 API 的 `decompress=true` 给的是明文），
#    比武大少 4.9 秒 CPU —— 所以测速打平时它仍然是个好选择。


# ---- 欧洲空间局 GSSC（多分析中心镜像，FTP）----

# ⚠️ 它和上面现有的 `esa` 源**不是一回事**：
#    `esa` 只有 ESA 自己那一支（HTTPS）；这里是一个**多 AC 的镜像**
#    （同一个周目录里 WUM/IGS/GRG/COD/JGX/GFZ/EMR/ESA/SIO/USN 都有），
#    还带 `MD5SUMS`。⚠️ **只有 FTP** —— HTTPS 那条路是 404。

# ⚠️ 这一版**只用它做 COD 那一支的镜像**（和 AIUB 同一份文件），
#    因为那是本项目**实测过的**；多 AC 那一大片留给以后。

# ✅ 2026-09-29 实测（经本程序自己的 `ginan/ftp.py` 下载 + gunzip + 查内容）：
#      SP3  COD0MGXFIN_20262140000_01D_05M_ORB.SP3.gz
#           909,378 字节 / 48.3 秒 = **18 KB/s** → 解压 2,056,044 字节、289 历元
#           头部 `#dP2026  8  2 ... FIT AIUB` —— **和 AIUB 那份是同一个文件**
#      CLK  5,033,751 字节 → 解压 35,660,520 字节
#      ERP        534 字节 → 解压 1,631 字节

# ⚠️ 实测**慢链路会截断**（下 CLK 时进程曾在 38% 处断掉，gzip 直接报
#    `EOFError`）⇒ 完整性校验必须真的生效，不能只看"下完了"。
GSSC_BASE = "ftp://gssc.esa.int/gnss/products"


def aiub_urls(d: date) -> dict[str, str]:
    """AIUB（CODE）那一份的三个产品 —— **含 GSSC 的镜像**。

    ⚠️ 两个源给的是**同一个文件**（实测头部都是 `FIT AIUB`），
       所以它们**属于同一个 FilePlan**（同一组），而不是两组。
       这也正是本项目那条规矩：**名字和内容必须配套** ——
       同一组里的 URL 换着下，落盘名是一样的。
    """
    return _aiub_urls(d)


def _gssc_urls(d: date) -> dict[str, str]:
    """GSSC 那条 FTP 的 COD 产品地址（**和 AIUB 是同一个文件**）。"""
    w = gps_week(d)
    yd = year_doy(d)
    return {
        "sp3": f"{GSSC_BASE}/{w}/COD0MGXFIN_{yd}0000_01D_05M_ORB.SP3.gz",
        "clk": f"{GSSC_BASE}/{w}/COD0MGXFIN_{yd}0000_01D_30S_CLK.CLK.gz",
        "erp": f"{GSSC_BASE}/{w}/COD0MGXFIN_{yd}0000_01D_12H_ERP.ERP.gz",
    }


# ---- 加拿大 NRCan（CACS，**老日期**的 IGS 快速镜像）----

# ⚠️ 加它的理由很具体：**老日期的 `igr*` 原来只有 CDDIS 一个源，而 CDDIS 要账号**
#    （见 `_cddis_urls` 里那段）。NRCan 这条路**不要账号**。

# ✅ 2026-09-29 实测：多个周的 HEAD 是 200（1400~2100、2210~2430 抽查通过），
#    并真下载过一个：`final/0789/em107890.sp3.Z` 66,145 字节 → 解开 156,019 字节、
#    **96 个历元**、头部 `FIT EMR`。

# ⚠️ 覆盖**不匀**：周 1100~1300 是 404，**2180~2200 回 403**（不是 404）。
#    ⇒ 它**只是多一个选项**，不是主力。404/403 会被当成
#      「这个源这次不行」自动跳到下一个源，所以不匀**不致命**。

# ⚠️ 慢：实测 10~13 KB/s。交给测速排序去处理（`fetch._rank_by_speed`）。

# ⚠️⚠️ **只给老日期**（< `OLD_NAMING_BEFORE_WEEK`）—— 新长名那种它有没有
#    **没验过**，而「没验过的东西不写进来」是这个文件的规矩。
NRCAN_BASE = "https://cacs.nrcan.gc.ca/gps/products/rapid"


def _nrcan_urls(d: date) -> dict[str, str]:
    """NRCan 的 IGS 快速产品（**只有老日期**）。"""
    w = gps_week(d)
    if w >= OLD_NAMING_BEFORE_WEEK:
        return {}
    names = _product_names(d)
    return {t: f"{NRCAN_BASE}/{w}/{names[t]}.Z" for t in ("sp3", "clk", "erp")}


def obs_urls_for(station: str, day: date) -> dict[str, str]:

    """一个测站的观测数据，**所有能拿到它的源**（顺序 = 兜底顺序）。

    ⚠️ 抽成函数的理由：这段原来是内联在 `plan_run` 里的，
       逻辑一多就没法单独测（`check_network` 要断言「有没有 noaa」
       「2025 前不给 noaa」这些，内联的话只能靠跑整个 plan_run）。
    """
    urls = {
        "whu": sources.lookup_token("whu", "obs", station),
        "ga": ga_rinex_url(station, day, "obs"),
    }
    noaa = _noaa_obs_url(station, day)
    if noaa:
        urls["noaa"] = noaa
    return urls


def _aiub_urls(d: date) -> dict[str, str]:

    """瑞士伯尔尼大学 AIUB（CODE 分析中心）的 URL。

    ⚠️⚠️ **它不是镜像，是另一个分析中心的结果** —— 所以进的是
       `plan.alternatives`（备选），**不是** `urls`（镜像）。

       镜像 = 同一份文件（`IGS0OPSRAP_…`）的多个副本，下哪个都一样。
       这条路给的是 **`COD0MGXFIN_…`** —— CODE 自己的 MGEX 最终解。
       换上它会**改变结果**（毫米到厘米级），所以必须用户点头。

    ✅ 2026-09-29 实测（**真下载过**，这是从 `SOURCES_REACHABLE`
       升级到"可用"的唯一条件）：

           SP3  COD0MGXFIN_20262620000_01D_05M_ORB.SP3.gz
                909,612 字节 → 解压 2,056,044 字节、**289 历元、116 颗卫星**
           CLK  …_01D_30S_CLK.CLK.gz    2,555,904 字节
           ERP  …_01D_12H_ERP.ERP.gz          537 字节
           BIA  …_01D_01D_OSB.BIA.gz       65,749 字节

    ⚠️ **路径能直接拼**（不用两步查询）—— 这很少见，值得留着：
           https://www.aiub.unibe.ch/download/CODE_MGEX/CODE/{年}/{名}.gz

    ⚠️ 但**慢**：实测 SP3 **22 KB/s**、CLK 21 KB/s、BIA 13 KB/s。
       所以它**不该**被当成主力 —— 交给测速排序去处理就好
       （见 `fetch._rank_by_speed`）。

    ⚠️ 它是**最终产品（FIN）**，出得晚。实测 2026 年只到第 262 天
       （查询时是第 268 天）—— 所以**最近几天要它，是拿不到的**，
       那属于"还没发布"，不是"源坏了"。
    """
    yd = year_doy(d)
    y = f"{d.year:04d}"
    base = f"{AIUB_BASE}/{y}"
    # ⚠️ 采样率那段**每种产品都不一样**（05M / 30S / 12H / 01D / 30S）——
    #    这是从它自己的目录列表里读出来的，**不是按规律推的**。
    return {
        "sp3": f"{base}/COD0MGXFIN_{yd}0000_01D_05M_ORB.SP3.gz",
        "clk": f"{base}/COD0MGXFIN_{yd}0000_01D_30S_CLK.CLK.gz",
        "erp": f"{base}/COD0MGXFIN_{yd}0000_01D_12H_ERP.ERP.gz",
    }


def _cddis_urls(d: date) -> dict[str, str]:
    """NASA CDDIS 的 URL（需要 Earthdata 账号）。

    ⚠️⚠️ 用 **RAP（快速产品）**，不是 FIN（最终产品）——
       这一条是**实测踩出来的**，不是照抄文档来的。

       我原来按"CDDIS 是主数据中心、应该有最终产品"拼了 `IGS0OPSFIN_`，
       结果实测 **404**：

           IGS0OPSFIN_20262450000_01D_15M_ORB.SP3.gz  → 404
           IGS0OPSRAP_20262450000_01D_15M_ORB.SP3.gz  → 200（97 KB，真的是 gzip）

       原因是**最终产品要等约两周**才发布。对于"近期数据"这个主要场景，
       CDDIS 上只有 RAP。

       好在 RAP 和 BKG 用的是**同一套文件名**，所以两边可以共用一套逻辑。

    ⚠️ 这也解释了为什么"猜路径"很危险：
       404 页面也是 HTML，和登录页**长得几乎一样**，
       于是"文件不存在"会被误报成"认证没过" —— 一个完全错误的方向。

    ==================== 2026-09-29：三个名字全部实测过 ====================
    这一天终于拿到了账号，把 `_product_names()` 拼出来的三个名字**逐个下过**。
    基准日用 **2026-09-27**（为什么不能随便挑日期，见下面那条）：

        sp3  IGS0OPSRAP_20262700000_01D_15M_ORB.SP3.gz   96,771 B   ✅
        clk  IGS0OPSRAP_20262700000_01D_05M_CLK.CLK.gz  1,062,350 B ✅
        erp  IGS0OPSRAP_20262700000_01D_01D_ERP.ERP.gz      368 B   ✅

    而且做了**对照**：把 clk 改成 15M / 30S、erp 改成 15M，**三个全 404**。
    ⇒ 说明 `05M_CLK` / `01D_ERP` 是它对的那一套，**不是"碰巧也通"**。

    ⚠️⚠️ **RAP 有约 2 天的发布延迟**（连扫 15 天量出来的）：

        2026-09-29（DOY 272）  404      ← 当天
        2026-09-28（DOY 271）  404      ← 昨天
        2026-09-27（DOY 270）  ✅       ← 从这儿往前，15 天全在

    这条**没有加日期守卫**，是有意的：`urls` 里 cddis 排第 5
    （`MAX_SOURCES_PER_FILE` 正好是 5），只有在前四个源全挂之后才会轮到它，
    而那时候"多试一次"正是你想要的。加一个跟"今天"有关的分支会让
    `plan_run` 不再可复现，代价大于收益。
    ⇒ 记在这里，是为了下一个人看到"近两天 CDDIS 会 404"时**不要以为是坏了**。

    ⚠️ 但也别把这条读成"CDDIS 不能用"：**它只影响最近两天**，
       而老日期（短名 `.Z` 那条分支）完全不受影响。
    """
    w = gps_week(d)
    names = _product_names(d)
    base = f"https://cddis.nasa.gov/archive/gnss/products/{w}"

    # ⚠️ 老日期：短名 + **`.Z`**（Unix compress）。
    #
    #    ✅ 实测下载成功过：`igr21992.sp3.Z`（2022-03-01），
    #       48598 字节，解开后头部是
    #       `#cP2022  3  1  0  0  0.00000000      96 ORBIT IGb14 HLM  IGS` ——
    #       日期和类型都对得上。
    #
    #    ⚠️ 这里**不能**靠"试一下 URL 通不通"来验证：
    #       CDDIS 对**存在和不存在的路径都返回 302**（跳登录页）。
    #       上面那句"实测下载成功"指的是**真的把文件下下来了**，
    #       不是"HEAD 返回了 200" —— 这两件事在这里差别很大。
    if w < OLD_NAMING_BEFORE_WEEK:
        return {
            t: f"{base}/{names[t]}.Z"
            for t in ("sp3", "clk", "erp")
        }

    return {
        "sp3": f"{base}/{names['sp3']}.gz",
        "clk": f"{base}/{names['clk']}.gz",
        "erp": f"{base}/{names['erp']}.gz",
    }


def _cod_bias_urls(d: date) -> dict[str, str]:
    """码偏差文件 —— CODE 的**快速** OSB 产品。

    ✅ 实测下载成功：`COD0OPSRAP_20262450000_01D_01D_OSB.BIA.gz`
       61,776 字节 → 解压 747,388 字节，头两行：
           `%=BIA 1.00 COD 2026:246:36196 IGS 2026:245:00000 2026:246:00000 …`
           `OUTPUT  CODE IGS 1-day rapid bias solution for G/R/E`

    ============================ 为什么是它 ============================
    ⚠️ 这是为了消掉 Ginan 每次都在打的 `Bias not found for G27 on L1W,
       using undefined_sigma: 1` —— 它缺的就是这类文件。

    候选有几个，实测比较过（都在武大逐字节验过内容）：

        IGS2OPSRTS_*_OSB.BIA.gz   2.2 MB → 解压 **44.7 MB**（1 分钟分辨率的实时流）
        IGS3OPSRTS_*_OSB.BIA.gz   4.6 MB → 解压 **56 MB**
        GFZ0MGXRAP_*_DCB.BIA.gz    61 KB —— 但 DCB 和 OSB 是**两种**偏差
        JAX0MGXFIN_*_OSB.BIA.gz     6 KB —— 但覆盖面窄
        COD0OPSRAP_*_OSB.BIA.gz    61 KB ← **选的这个**

    选它的三条理由：
      ① **体积**：61 KB/天。IGS 那两个实时流是 2~5 MB，对每天都要下的东西太重
      ② **口径一致**：它是 **RAP（快速）**，和我们用的轨道/钟差同一档
         （IGS 那两个是 **RTS 实时**，精度档位都不一样）
      ③ **覆盖面**：文件头自己写着 `for G/R/E` —— 正好是我们处理的三套系统
         （默认只开 GPS + Galileo）

    ⚠️ 它**不是 IGS 综合产品**，是 CODE 一家的 —— 这一点必须说清楚。
       镜像上**没有** IGS 组合的日常 OSB。影响是毫米级（码偏差改正的是伪距，
       而最终解算由相位主导），但**不该假装它是 IGS 的**。

    ============================ ⚠️ 老日期根本没有这个文件 ============================
    实测（在武大逐周查 `COD0OPSRAP_`）：

        周 2231（2022-10-15）  0 条
        周 2236（2022-11-15）  0 条
        周 2237（2022-11-22）  0 条
        周 2238（2022-11-27）  **28 条**   ← 从这一周开始有

    👉 所以**老日期返回空字典**（下面那行 early return），配一个 404 的源
       不只是浪费时间 —— 它会让下载结果里出现一条"失败"，
       而用户/模型会去查一个**根本不存在的东西**。

    ⚠️ 那个周数（2238）和 `BKG_PRODUCTS_FROM_WEEK`、
       `OLD_NAMING_BEFORE_WEEK`（2237，即 2238 起换新名）**是同一个时间点** ——
       IGS 在那一周同时改了命名、BKG 开始有产品、偏差文件开始发布。
       **不是巧合：那是 IGS 的一次整体换代。**
    """
    if gps_week(d) < BIAS_FROM_WEEK:
        return {}

    yd = year_doy(d)
    name = f"COD0OPSRAP_{yd}0000_01D_01D_OSB.BIA.gz"
    w = gps_week(d)
    return {
        "whu": sources.lookup_token("whu", "jmxl", name),
        "whu_ftp": f"ftp://igs.gnsswhu.cn/pub/gps/products/{w}/{name}",
        "sio": f"https://garner.ucsd.edu/pub/products/{w}/{name}",
    }


def _whu_ftp_urls(d: date) -> dict[str, str]:
    """武汉大学的**匿名 FTP** —— 同一个数据中心的另一条通道。

    ✅ 实测下载成功（`ftp://igs.gnsswhu.cn/pub/gps/products/2434/…SP3.gz`，
       97,173 字节，0.4 秒，255 KB/s，gzip 头正确）。

    ⚠️ **它和 HTTP 那条不是重复的**，各自会坏：
        · FTP 路径**可以直接拼**（`/pub/gps/products/{周}/{文件名}`），
          不用两步查询、**也没有尾部那 19 KB 调试垃圾**
        · 但 HTTP 那条快 5~10 倍（实测 1.3~3 MB/s vs 255 KB/s）

        2024 年那次改版就是把 HTTP 的目录树关掉了（老 URL 全 404）——
        **说明这两条路是会被分别改坏的**，所以都留着有意义。

    ⚠️ 档案到 GPS 周 0576（1991 年），实测服务器上共 1865 个周目录。

    ⚠️ FTP 的**根路径多一层 `/pub`**（HTTP 是 `/index.php/igs/...`），
       而且它列的是**服务器上的真实路径** —— 接口返回的 `path` 字段
       （`pub/gps/products/2434/…`）就是它，两处能对上。
    """
    w = gps_week(d)
    names = _product_names(d)
    suffix = ".Z" if w < OLD_NAMING_BEFORE_WEEK else ".gz"
    base = f"ftp://igs.gnsswhu.cn/pub/gps/products/{w}"
    return {
        "sp3": f"{base}/{names['sp3']}{suffix}",
        "clk": f"{base}/{names['clk']}{suffix}",
        "erp": f"{base}/{names['erp']}{suffix}",
    }


def _sio_urls(d: date) -> dict[str, str]:
    """美国 SIO / Scripps（`garner.ucsd.edu`）的 URL。

    ✅ 实测下载成功（三个文件都逐字节验过内容）：
         IGS0OPSRAP_20262450000_01D_15M_ORB.SP3.gz  97,173 B → 解压 253,249 B，`#cP2026  9  2 …`
         igr21992.sp3.Z（老短名）                    98,155 B → LZW 解开 253,250 B，`#cP2022  3  1 …`
         GFZ0MGXRAP_20262450000_01D_01D_DCB.BIA.gz  61,472 B → `%=BIA 1.00 GFZ …`

    ⚠️⚠️ **它是"镜像"，不是"另一个分析中心"** —— 这一点决定它能不能放进 urls。

        镜像：**同一个文件名**，在另一个地方也有一份   → 可以互换 ✓
        另一个 AC：`ESA0OPSFIN_..._ORB.SP3` 是**另一份解算结果**

        后者**不能**放进同一个 urls 槽位 —— 换掉它等于**悄悄换掉了分析输入**，
        结果会变而没人知道。ESA 那个源就是这种情况，所以没有加进来。

        👉 **多源互替的前提是"同一个文件有好几个镜像"，不是"有类似的文件"。**

    ⚠️ 档案很老（实测到 GPS 周 0576 = 1991 年），而且**老式短名也有**
       （`igr/igs/igu/cod/esa/sio/sir…` 一堆前缀）—— 所以它对 2022 那类
       老日期是个有效的备源，而 BKG 对老日期是**没有档案的**（见 BKG_PRODUCTS_FROM_WEEK）。

    ⚠️ 目录列表页很大（单周 792 KB），**不要去拉列表页**，直接按文件名取。
    """
    w = gps_week(d)
    names = _product_names(d)
    suffix = ".Z" if w < OLD_NAMING_BEFORE_WEEK else ".gz"
    base = f"https://garner.ucsd.edu/pub/products/{w}"
    return {
        "sp3": f"{base}/{names['sp3']}{suffix}",
        "clk": f"{base}/{names['clk']}{suffix}",
        "erp": f"{base}/{names['erp']}{suffix}",
    }


def _bkg_brdc_url(d: date) -> str:
    """德国 BKG 的广播星历（**全球统一，与测站无关**）。

    ✅ 实测下载成功：2026-001（753 KB）和 2026-245（588 KB）两天都试过，
       解压出来是 RINEX 3.04 `GNSS NAV DATA  M: MIXED`。

    ⚠️⚠️ **它的目录结构和精密产品完全不同** —— 这是实测摸出来的：

           精密产品：  products/{GPS周}   /IGS0OPSRAP_{年积日}0000_...
           广播星历：  BRDC/{公历年}/{三位年积日}/BRDC00IGS_R_{年积日}0000_...

       一个是 **GPS 周**，一个是 **公历年 + 年积日**。
       照着"IGS 的命名规则"去猜是猜不出来的 ——
       我第一次就猜成了 `products/{周}/BRDC...`，实测 **404**。

    ⚠️ `00IGS` 是**合并产品**的机构代码（IGS 把各分析中心的广播星历合成一份）。
       同一个目录下还有 `00WRD`（更全，1.6 MB）、`00DLR` 等，
       我们只要合并的那份 —— 它已经包含全部星座。
    """
    yd = year_doy(d)          # "2026001"
    doy = yd[4:]              # "001" —— 目录名用三位年积日
    return (
        f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{d.year}/{doy}"
        f"/BRDC00IGS_R_{yd}0000_01D_MN.rnx.gz"
    )


def _cddis_brdc_url(d: date) -> str:
    """NASA CDDIS 的广播星历（需要 Earthdata 账号）。

    ✅ **2026-09-29 实测：路径正确**（终于有账号了）。三条独立证据：

       ① 列目录：`gnss/data/daily/2026/brdc/` 里 1362 项，
          其中就有 `BRDC00IGS_R_20262710000_01D_MN.rnx.gz`
       ② 真下载：HTTP 200、`application/x-gzip`、1,216,262 字节
       ③ **内容自证**（这一条最重要）：解开之后的头两行是
              `     3.04           N: GNSS NAV DATA    M: MIXED            RINEX VERSION / TY`
              `MergeMNfile.tcl     IGS                 20260929 093506 GMT PGM / RUN BY / DAT`
          ——格式、内容、生成时间全都对得上。

    ⚠️ 这里以前写着"没有实测下载过（无账号）"，还说"404 也是 HTML，
       和登录页长得很像，所以只能说看起来对"。**那个谨慎是对的，
       而这次实测也说明了它为什么必要**：同一个数据集里另一条路径
       （`CDDIS_ATX_URL`）就是这么挂着"没验证过"活到 2026-09-29 的，
       一量发现**根本不存在**。

    ⚠️ 注意 CDDIS 把它放在 `gnss/data/daily/`（观测数据那棵目录树），
       **不是** `gnss/products/` —— 广播星历在 CDDIS 归类为"数据"而非"产品"。
    """
    yd = year_doy(d)
    return (
        f"https://cddis.nasa.gov/archive/gnss/data/daily/{d.year}/brdc"
        f"/BRDC00IGS_R_{yd}0000_01D_MN.rnx.gz"
    )


# ---- 静态文件（不随日期变）----

# ✅ 实测：M0.5 下载成功（61 MB，真实 ANTEX 格式）
BKG_ATX_URL = "https://igs.bkg.bund.de/root_ftp/IGS/station/general/igs20.atx"

# ✅ 实测：HTTP 206 可达，目录列表确认存在 igs20.atx
# ⚠️ 但 IGS 官方站**只放天线文件和测站元数据，不放轨道/钟差产品** ——
#    它的 /pub/product/ 目录里只有 readme.txt。所以它只能覆盖 ATX 这一类。
IGS_ATX_URL = "https://files.igs.org/pub/station/general/igs20.atx"

# ✅ 实测下载成功（273,931 字节，首行 `%=SNX 2.02 DLR 26:245:31821 …`）
#
# ⚠️ 只有这一个源 —— 实测 BKG 和 SIO 都是 404，武大的文件库里也没有。
#    但它是**一次性**文件，下过一次就缓存，所以单源可以接受。
IGS_SAT_METADATA_URL = (
    "https://files.igs.org/pub/station/general/igs_satellite_metadata.snx"
)

# ⚠️⚠️ 这里曾经有一条 `CDDIS_ATX_URL`（`gnss/products/rcvantex/igs20.atx.gz`），
#    **2026-09-29 删掉了 —— 那个地址不存在。**
#
#    它挂着"没验证过"的注释活了很久，而"没验证过"的路径实测**就是错的**：
#
#        · 那个文件             → 404
#        · 它的目录 `rcvantex/`  → 404（**整个目录不存在**）
#        · 父目录 `gnss/products/` 清单 1828 项里，没有任何含 atx/antex/rcv 的项
#        · `gnss/data/antex/`、`gnss/igsstation/` 也都是 404
#
#    👉 **一个错的源比没有源更糟**（见 plan_run 里那段）：
#       它会让下一个人以为"CDDIS 上有天线文件"，从而去排查"为什么下不动"。
#       而天线文件这边我们已经有两个**实测能下**的源（BKG 61 MB 真实下载过、
#       IGS 官网 206 可达），CDDIS 这一栏是纯粹的负债。

# ---- Ginan 官方数据仓（模型文件）----
#
# ⚠️⚠️ 这里踩过一个坑，值得留着：**同一个桶有两个主机名，只有一个通。**
#
#     2026-09-20 实测（国内网络、**不走代理**）：
#
#         peanpod.s3.ap-southeast-2.amazonaws.com   ✗ 超时 12 秒（走代理也不通）
#         peanpod.s3.amazonaws.com                  ✓ 200，1.9 秒
#
#     区域端点解析到的 IP 不通，S3 的**全球端点**通。桶是同一个，内容完全一样。
#
#     ⚠️ 而本文件里原来写着"无代理时超时 → 它是'有代理才能用'的源" ——
#        那条记录是**错的**，而且错得有害：它让每个用户以为必须挂代理，
#        于是根本不会去试。**过期的记录比没有记录更糟。**
#
#     官方脚本 getProducts.sh 用的是区域端点。我们**故意不用它**。
GINAN_S3_BASE = "https://peanpod.s3.amazonaws.com/aux"

# 区域端点留着 —— 哪天全球端点不通了还有一条路（反之亦然）。
# ⚠️ 但**默认必须是全球端点**：上面那两个数字就是理由。
GINAN_S3_BASE_REGION = "https://peanpod.s3.ap-southeast-2.amazonaws.com/aux"


def ginan_s3_url(key: str, base: str = GINAN_S3_BASE) -> str:
    """Ginan 官方数据仓里的**任意一份文件**。

    ⚠️ `key` 是 **`aux/` 下的相对路径**（`data/sbas/…`、`products/sbas/…`），
       **不带 `.gz`** —— 后缀由本函数拼。

    ⚠️⚠️ **`.gz` 这个后缀是踩过两次的坑**（① 的 `EGM2008.gfc`、
       ⑤ 的整批 SBAS 数据）：少写它就是 404，而 404 的样子是
       "**这个源不可用**" —— 会静默掉到下一个源，或者报"拿不到"。
       所以拼 URL 这件事**只有一个地方做**，调用方不许自己拼。
    """
    return f"{base}/{key}.gz"


def ginan_s3_table_url(name: str, base: str = GINAN_S3_BASE) -> str:
    """Ginan 官方数据仓里的一个 tables/ 模型文件。

    ⚠️ 参数是**不带 `tables/` 前缀**的文件名，例如 `gpt_25.grd`。
       路径前缀由本函数拼 —— 调用方不要自己拼，否则两处真相。
    """
    return ginan_s3_url(f"products/tables/{name}", base)


def dataset_files(ds: Dataset) -> list[FilePlan]:
    """把一份数据集摊成取数计划里的文件。

    ⚠️⚠️ **`keep_name=True`** —— 见 `FilePlan.keep_name` 的说明。
       这条对 SBAS 是**必需**的：112 个观测文件只来自 4 个站，
       而默认规则会按文件头里的测站名把它们改成 `HOB2.rnx`、`HOB2_2.rnx`……
       **116 个名字里一个时间信息都不剩**。
    """
    return [
        FilePlan(
            filename=f.key.split("/")[-1],
            kind=f.kind,
            urls={"ginan_s3": ginan_s3_url(f.key)},
            size_hint=f.size_hint,
            day=ds.day,
            note=f.note or f"来自 {ds.label}",
            keep_name=True,
        )
        for f in ds.files
    ]


# =============================================================================
# 「完整」档要的模型文件
# =============================================================================
#
# ============================ 这份清单是哪来的 ============================
# ⚠️ **不是"桶里有什么就下什么"，也不是猜的。**
#
#    出处：Ginan 官方 UI 自己的下载清单 ——
#
#        <ginan 源码>/scripts/GinanUI/app/models/dl_products.py
#        里面的 `METADATA`（注释写着 "Static metadata product files that are
#        always required"）
#
#    那份清单一共 13 条，我们已经有 3 条（igs_satellite_metadata.snx、
#    igs20.atx，以及我们自己的 ERP 产品顶掉了 finals.data.iau2000.txt），
#    所以真正缺的是下面这 10 个。
#
# ⚠️ **不要**改成"把桶里 37 个文件全下"：
#      桶里 37 个 / 114 MB，而**要用的**是这 10 个 / 17 MB。
#      多下的那些是 Ginan 别的模块（POD、LEO、SLR）用的，与 PPP 无关 ——
#      白等 97 MB，而且以后桶里加文件时会跟着无限膨胀。
#
# ⚠️ 也别信工具描述里原来写的"约 200MB" —— 那个数字从来没有依据，
#    而且它已经**误导过模型**去对用户承诺一次根本不会发生的下载。
#
# ============================ 为什么要哪些 ============================
# 每一项的"谁要它"都对着 default_config.yaml 里的一个配置键，
# 那个文件里带 `#AUTO` 标记的正是"必须自动准备好"的。
#
# 复现这份清单的办法（桶的列表是公开的，不需要账号）：
#   curl -s "https://peanpod.s3.amazonaws.com/?list-type=2&prefix=aux/products/tables/"
TABLES: tuple[tuple[str, str], ...] = (
    # (文件名，给人看的一行用途)
    ("DE436.1950.2050",
     "日月和其它行星的位置（1950–2050）—— 固体潮要用"),
    ("opoleloadcoefcmcor.txt",
     "海洋极潮系数 —— 地极移动引起的海水质量重分布"),
    ("fes2014b_Cnm-Snm.dat",
     "海潮负荷的球谐系数"),
    ("OLOAD_GO.BLQ",
     "各测站的海潮负荷位移（GOT 模型）"),
    ("gpt_25.grd",
     "全球气压温度格网 —— 对流层延迟的 gpt2 模型用"),
    ("ALOAD_GO.BLQ",
     "各测站的大气潮负荷位移"),
    ("qzss_yaw_modes.snx",
     "QZSS 卫星的偏航姿态模式"),
    ("sat_yaw_bias_rate.snx",
     "卫星偏航姿态的偏差速率"),
    ("igrf14coeffs.txt",
     "地球磁场模型系数 —— 电离层二阶/三阶项要用"),
    ("bds_yaw_modes.snx",
     "北斗卫星的偏航姿态模式"),
)


WORKFLOW_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    # (文件名，给人看的一行用途)
    "pod": (
        ("EGM2008.gfc",
         "地球重力场的球谐系数 —— 估卫星轨道要算地球引力，这是最大的那一项"),
    ),
    "fit_sp3_pseudoobs": (
        ("EGM2008.gfc",
         "地球重力场的球谐系数 —— 它和 POD 一样要算卫星受力，"
         "只是把观测换成了别人给的轨道"),
    ),
}
"""**只有某个工作流要**的模型文件。

============================ 为什么和 `TABLES` 分开 ============================
`TABLES` 那批是「完整」档的通用模型（海潮、极潮、行星历表…），**任何工作流都
可能用**。而这里的是**某个工作流的专属依赖**：

    POD 要估卫星轨道 → 必须算卫星受力 → 必须要有重力场模型
    PPP 不需要       → 它的卫星轨道是**输入**（SP3 文件），根本不用算受力

⚠️⚠️ **不能把它们混进 `TABLES`。** 混进去的话「完整」档会无条件多下 72 MB
   的重力场 —— 而 PPP 一个字节都用不到。"下了没用上"正是这个仓库反复
   记着的那种静默浪费。

⚠️⚠️ **也不能只靠"磁盘上没有就当成不需要"。** 缺重力场的 POD 不会报错 ——
   Ginan 只打印一句 `EGM data not initialised`，**跳过最大的那一项受力，
   轨道照常输出**（`orbitProp.cpp:71-77`）。所以缺了必须**拦住**，
   见 `ginan_tools.do_generate_config` 里那道门。

⚠️ 和 `TABLE_KEYS` 的分工是刻意的：
     · `TABLE_KEYS` 管"这个文件写进配置的**哪个键**"（给 `table_inputs()` 用）
     · 这张表只管"**要下**哪些文件"
   重力场模型**不进** `TABLE_KEYS` —— 配置里那个 `egm_files` 键由
   `config._substitutions` 从 `ConfigInputs` 填，不从这里推。两边合起来会打架。
"""


def workflow_tables(workflow: str | None) -> tuple[tuple[str, str], ...]:
    """这个工作流**特有**的模型文件。没有就返回空。

    ⚠️ `workflow=None` / 拼错时返回**空**，不报错 —— 和 `workflows.get` 的
       降级策略一致：拿不到就回最保守的那个（不下多余的）。
    """
    return WORKFLOW_TABLES.get((workflow or "").strip().lower(), ())


def table_plan(name: str, purpose: str) -> FilePlan:
    """打包成一个 FilePlan —— 供 `plan_tables()` 用。

    ⚠️ `filename` 带 `tables/` 前缀，这是**故意的**：
       Ginan 的配置里就是这么引用它们的（`gpt2grid_files: [tables/gpt_25.grd]`），
       而解析方式是 `inputs_root + "tables/..."`（见 debug.cpp:1873）。
       所以文件必须真的落在 `<app>/data/tables/` 下。

    ⚠️ `one_time=True` —— 它们不随日期变，下过一次就一直用。
       这个字段本来就是为这类文件准备的（见 FilePlan 的说明）。
    """
    return FilePlan(
        filename=f"tables/{name}",
        kind="table",
        urls={
            "ginan_s3": ginan_s3_table_url(name),
            "ginan_s3_region": ginan_s3_table_url(name, GINAN_S3_BASE_REGION),
        },
        one_time=True,
        note=purpose,
    )


def plan_tables(workflow: str | None = None) -> list[FilePlan]:
    """要下的模型文件 = 「完整」档那批 **+ 这个工作流特有的那批**。

    ⚠️ 这是**一次性**的：调用方应该先看本地有没有，只下缺的那些。
       全下是 17 MB（PPP）；POD 多一个 ~72 MB 的重力场模型。
       全都有时是 0 字节。

    ⚠️ `workflow=None` 时**只给通用那批** —— 不猜。
       猜的话，"没给工作流"会静默变成"按 POD 下 72 MB"，
       而用户压根没说要跑 POD。
    """
    out = [table_plan(name, purpose) for name, purpose in TABLES]
    for name, purpose in workflow_tables(workflow):
        out.append(table_plan(name, purpose))
    return out


# =============================================================================
# 这些文件在 Ginan 配置里的位置
# =============================================================================
#
# ⚠️⚠️ **这份表是必须的，不是补充说明。** 踩过一次，教训很贵：
#
#     一开始以为"把文件下到 data/tables/ 就够了，Ginan 自己会去找"。
#     实测**不是**：pea 启动时的 Configuration Summary 里
#     **一个 tables 文件都没有**，一次都没去打开过 ——
#     而我们下的那 45 MB 就静静躺在磁盘上，一点用都没有。
#
#     ⚠️ 最坏的是它**不报错**：跑出来的结果和「基础」档**一模一样**，
#        用户以为用了完整档，其实一个模型都没用上。
#        （这个结论是拿"有 tables"和"没 tables"各跑一遍、日志逐行比出来的，
#          不是看代码看出来的。）
#
#     👉 文件要下，**而且**配置里要写。少一半都不行。
#
# 值的形状：(顶层键, 子键)。
#   子键为空 → 是 inputs 下的一级键，值是**列表**（`igrf_files: [...]`）
#   子键非空 → `inputs.<顶层键>.<子键>`，值也是列表
TABLE_KEYS: dict[str, tuple[str, str]] = {
    "igrf14coeffs.txt":       ("igrf_files", ""),
    "DE436.1950.2050":        ("planetary_ephemeris_files", ""),
    "gpt_25.grd":             ("troposphere", "gpt2grid_files"),
    "OLOAD_GO.BLQ":           ("tides", "ocean_tide_loading_blq_files"),
    "ALOAD_GO.BLQ":           ("tides", "atmos_tide_loading_blq_files"),
    "opoleloadcoefcmcor.txt": ("tides", "ocean_pole_tide_loading_files"),
    "fes2014b_Cnm-Snm.dat":   ("tides", "ocean_tide_potential_files"),
    # 这三个**不是独立的键** —— 它们是加进 `snx_files` 那个列表里的，
    # 和 `igs_satellite_metadata.snx` 并列。
    "qzss_yaw_modes.snx":     ("snx_files", ""),
    "sat_yaw_bias_rate.snx":  ("snx_files", ""),
    "bds_yaw_modes.snx":      ("snx_files", ""),
}

# GinanUI 的官方默认配置里，两个潮汐负荷项除了查表文件还各带一条通配 ——
# 那是给"用户自己算了测站 BLQ"留的口子。它匹配不到东西时是**静默无副作用**的
# （实测：`*_ocean.BLQ` 一个都没匹配到，pea 照常启动，summary 里只列出真文件）。
#
# ⚠️ 但 `gpt2grid_files` 那一栏**没有**通配 —— 别顺手加一个。
TABLE_EXTRA: dict[str, list[str]] = {
    "ocean_tide_loading_blq_files": ["*_ocean.BLQ"],
    "atmos_tide_loading_blq_files": ["*_atmos.BLQ"],
}


def table_inputs() -> dict[str, Any]:
    """「完整」档要写进配置 `inputs:` 里的那些键。

    ⚠️ 这份内容是**从 TABLES 推出来的**，不是另抄一遍文件名 ——
       两份清单一定会漂，而漂的后果就是这次的 bug：
       **文件下了，配置里没写，于是下了等于没下，而且不报错。**
    """
    out: dict[str, Any] = {}

    for name, _purpose in TABLES:
        full = f"tables/{name}"
        top, sub = TABLE_KEYS[name]

        if sub:
            out.setdefault(top, {}).setdefault(sub, []).append(full)
            out[top][sub].extend(TABLE_EXTRA.get(sub, []))
        else:
            out.setdefault(top, []).append(full)

    return out


# ---- GA 的 API ----
GA_API = "https://data.gnss.ga.gov.au/api/v1/rinexFiles"


# ---- 可达但路径未验证的源 ----
#
# ⚠️ 这张表要**留着**，不要删。
#    它记录的是"我们试过、服务器通、但找不到数据路径"的中间状态。
#    删掉的话，将来有人会重新试一遍，再浪费一次时间。
#
#    从"可达"升级到"可用"的条件只有一个：**真实下载成功一次**。
SOURCES_REACHABLE: dict[str, str] = {
    "捷克 Pecný": "⚠️ **2026-09-29 摸清了，结论是「别接」**（留在这里免得下次重摸）：FTP `ftp://ftp.pecny.cz/LDC/orbits/{周}/igs{周}{星期}.sp3.Z` 能下（实测 96 历元 SP3 + 9218 行 CLK），但**归档断在 GPS 周 2430（2026-08-02）**，之后全是 **0 字节空壳**（服务器照样回 226）；**没有 HTTP 通路**；只有老式短名。",
    "日本国土地理院": "⚠️ **2026-09-29 探过，判定：对自动化不可行**（留在这里免得下次重探）：路径规律摸到了（`/{年}/{年积日}/{站4}{年积日}{序号}.{yy}d.gz`），但**是三步 POST 表单**，而且要带浏览器 User-Agent（不带直接挂到超时），**约二三十个请求后整台机器就被按 IP 掐掉**。⚠️ **没有真下载成功过，所以不给绿灯**（官方公告：FTP 站 2026-10-30 关停，之后只剩要账号的 SFTP）。",
    # ⚠️ 欧洲空间局 GSSC **已经不在这张表里了**（2026-09-29）——路径摸清了（FTP `gnss/products/{周}/`，能直接拼），SP3/CLK/ERP 三个都真下载成功过，于是升级进了 SOURCES（作为 COD 那一支的镜像，见 `_gssc_urls`）。
    "NOAA/NGS": "服务器可达（200），产品路径待摸清",
    # ⚠️ 加拿大 NRCan **已经不在这张表里了**（2026-09-29）——路径摸清了（`https://cacs.nrcan.gc.ca/gps/products/rapid/{周}/igr{周}{星期}.sp3.Z`，能直接拼），而且真下载成功过（`igr20624.sp3` 96 历元、头部 IGS），于是升级进了 SOURCES（**只给老日期**，见 `_nrcan_urls`）。
    # ⚠️ 美国 SOPAC（= garner.ucsd.edu = 上面那个 sio）**已经不在这张表里了** ——
    #    2026-09-20 摸清了它的产品路径（`/pub/products/{GPS周}/`）并真实下载成功，
    #    于是它从"可达"升级成了"可用"，进了 SOURCES。
    #
    # ⚠️ 瑞士伯尔尼 AIUB **也不在这张表里了**（2026-09-29）——
    #    路径摸清了（`CODE_MGEX/CODE/{年}/`，能直接拼），
    #    而且 SP3/CLK/ERP/BIA **四个文件都真实下载成功过**。
    #    但它给的是 **CODE 自己的解算**（`COD0MGXFIN_…`），不是 IGS 的产品，
    #    所以**进的是 `plan.alternatives`**（备选），不是 `urls`（镜像）。
    #    见 `_aiub_urls`。
    #
    #    👉 这正是这张表存在的意义：**中间状态要留着，升级的路径要写得出来。**
    #       从"可达"升级到"可用"的条件只有一条 —— **真实下载成功一次**。
    "UNAVCO/EarthScope": "服务器可达（200），产品路径待摸清",
}


def ga_rinex_url(
    station: str,
    day: date,
    file_type: str = "obs",
    decompress: bool = True,
) -> str:
    """构造 GA 的**查询** URL（不是文件地址）。

    ⚠️ 它返回的是一段 JSON，里面每个元素有一个 `fileLocation`，
    那才是指向真正文件的预签名 URL。所以 GA 这条路要**两步**：
        ① 查 API 拿到 fileLocation
        ② 下载 fileLocation

    ⚠️ `decompress=true` 是个很实用的参数，但它有个反直觉的后果：
    返回的 URL 文件名写着 `.crx.gz`，而**内容是未压缩的明文**。
    所以**永远不要靠文件名判断内容**（见 ginan-facts.md）。
    """
    start = datetime.combine(day, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.combine(day + timedelta(days=1), datetime.min.time()).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return (
        f"{GA_API}?stationId={station.upper()}"
        f"&fileType={file_type}"
        f"&rinexVersion=3"
        f"&filePeriod=01D"
        f"&decompress={'true' if decompress else 'false'}"
        f"&tenantId=default"
        f"&startDate={start}&endDate={end}"
    )


# =============================================================================
# 造计划
# =============================================================================


def atx_plan() -> FilePlan:
    """天线改正表 —— **与日期、与测站都无关**的一次性文件。

    ⚠️ 源按"实测可靠性"排序，**不是按我想到的顺序**：
       bkg   —— M0.5 真实下载成功（61 MB 文件）
       igs   —— 目录列表确认存在，HTTP 206 可达

    ⚠️ 原来还有第三栏 `cddis`，**2026-09-29 删了** ——
       那个地址实测不存在（来龙去脉写在 `CDDIS_ATX_URL` 删掉的地方）。

    ⚠️ 抽成函数是因为**数据集那条路也要它**。2026-09-27 之前它写在
       `plan_run` 里，而数据集分支早早就 `return` 了 ——
       于是 SBAS 的计划里既没有 `atx` 也没有 `snx`，
       而配置里需要它们（⑱j 当场就红了）。
    """
    return FilePlan(
        filename="igs20.atx",
        kind="atx",
        urls={"bkg": BKG_ATX_URL, "igs": IGS_ATX_URL},
        size_hint="约 61 MB",
        one_time=True,
    )


def snx_plan() -> FilePlan:
    """卫星元数据（SINEX）—— 同样是**与日期、测站无关**的一次性文件。

    出处和理由见原来那段（`plan_run` 里的注释），一句话：没有它 Ginan 会给
    每颗卫星打两条警告，一次 24 小时解算里实测 50 多条。
    """
    return FilePlan(
        filename="igs_satellite_metadata.snx",
        kind="snx",
        urls={"igs": IGS_SAT_METADATA_URL},
        size_hint="约 270 KB",
        one_time=True,
    )


def plan_run(
    stations: str | Sequence[str], day: date, workflow: str | None = None
) -> RunPlan:
    """给"某站某天"（或**一批站**某天）造一份取数计划。

    ⚠️ `workflow` 决定**要下哪几类文件**（按 `Workflow.needs` 筛）。
       不给就**不筛** —— 全给。猜的话，"没给工作流"会静默变成"按某个
       工作流下"，而用户压根没说要跑那个。

    ⚠️ 这里是**纯函数** —— 不联网、不写盘、不 import 任何 LLM 相关的东西。
    它只是把"需要什么、从哪拿"算出来。
    真正去下载是 fetch.py 的事，探测哪个源通用 probe.py 的事。

    这样分工的好处：**可以完全离线地测"计划造得对不对"**。

    ============================ 多站 = 网络解 ============================
    `stations` 给一个名字就是单站解，给一串就是**网络解** ——
    两者走的是**同一条路**（见 `RunPlan` 的说明）。

    ⚠️ 产品文件（SP3/CLK/ERP/ATX/导航…）是**全球统一、与测站无关**的，
       所以**只加一份**。多站只在"观测数据"那一项上分叉。
       （踩过一次：广播星历被当成"每站各一份"去下，见下面 nav 那一段。）
    """
    # ============================ ⚠️⚠️ 数据集：站和日期**不是用户给的** ============================
    #
    # 带 `dataset` 的工作流（目前只有 SBAS）跑的是**一份固定的东西**：
    # 它自己带日期、自己带测站。所以这条路**在用户给的参数之前**就分叉出去 ——
    #
    #     `plan_run(["ALIC"], 2026-09-02, "sbas_l1")`
    #         ⇒ 返回的 `RunPlan` 里是 **HOB2/NEBO/OUS2/TID1 和 2025-11-11**
    #
    # ⚠️ **为什么不报错、也不"就用用户给的站"**：
    #    用户说"给 ALIC 跑 SBAS"时，正确的是**如实告诉他**"SBAS 只有官方那 4 个站
    #    那 7 小时"（那个说法在 `workflows.Workflow.dataset` 那一栏，
    #    由工具回执转达）。而按用户给的站去下一个不存在的日期，
    #    会得到一堆 404 和一句"源不可用"——那是在甩锅给数据源。
    from . import workflows as _wf

    ds = dataset(_wf.get(workflow).dataset)
    if ds is not None:
        # ⚠️⚠️ **一次性文件不能漏**（`atx` / `snx`）。
        #
        #    它们是"与日期、与测站都无关"的，所以不走数据集，但也**不是
        #    数据集的对立面** —— 一个都不能少。第一版只返回了数据集里的
        #    文件，于是配置里要用 `atx`/`snx` 而计划里没有，
        #    ⑱j（"配置里用到的类别，计划里都要有"）当场就红了。
        return RunPlan(
            stations=list(ds.stations),
            day=ds.day,
            files=[atx_plan(), snx_plan(), *dataset_files(ds)],
        )

    # ⚠️⚠️ **先判 `str`** —— `str` 也是 `Sequence[str]`，
    #     先走通用分支的话 `"ALIC"` 会被拆成 `['A','L','I','C']`，
    #     然后一路顺利地去下载四个字母的"测站"。**不报错**。
    if isinstance(stations, str):
        names = [stations]
    else:
        names = list(stations)

    # ⚠️ 去重但**保持顺序**：配置文件里站的顺序会体现在输出里，
    #    用户给了什么顺序就按什么顺序，别自作主张排序。
    #    重复的站要去掉 —— 同一个站进两次滤波器就是把它算两遍，
    #    而 Ginan 不会因此报错（它多半会用后一份覆盖前一份）。
    seen: set[str] = set()
    clean: list[str] = []
    for s in names:
        n = (s or "").strip().upper()
        if n and n not in seen:
            seen.add(n)
            clean.append(n)

    if not clean:
        raise ValueError("至少要给一个测站名")

    plan = RunPlan(stations=clean, day=day)

    bkg = _bkg_urls(day)
    sio = _sio_urls(day)
    whu_ftp = _whu_ftp_urls(day)
    cddis = _cddis_urls(day)
    nrcan = _nrcan_urls(day)
    yd = year_doy(day)
    names = _product_names(day)

    # ---- 一次性文件 ----
    plan.files.append(atx_plan())

    # ---- 精密产品（按天）----
    #
    # ⚠️⚠️ **源的顺序 = 尝试的顺序**，这是一条有依据的排序，不是随手排的：
    #
    #     whu  武汉大学   —— 中国大陆实测 1792~3165 KB/s，且免账号。
    #                        对这台机器的用户来说这是**唯一的首选**：
    #                        同一天同一个文件，比 BKG 快 20~30 倍。
    #                        （要两步查询，见 ginan/sources.py）
    #     sio  美国 Scripps —— 匿名直连、**不用两步**、档案最全
    #                        （1991 年起，且老式短名也有 —— BKG 对老日期没档案）。
    #                        实测约 65 KB/s，和 BKG 同量级，所以排在 BKG 前面
    #                        是因为**覆盖面**而不是速度。
    #     bkg  德国 BKG    —— 长期主力，稳定，但只有新式长名（周 2236 起）
    #     cddis NASA      —— ⚠️ **要账号**。排序上放最后是**故意的**：
    #                        能免账号就免账号，新手卡在注册流程上的概率极高。
    #                        （2026-09-29 补：账号那条路**实测能走通**了，
    #                          见 `credentials.py` 末尾那段与 `_cddis_urls`；
    #                          但"要账号"这件事没变，排序也就不变。）
    #
    # ⚠️ 这里原来只有 BKG 一个源，注释里写着"TODO：逐个摸清结构后补上"。
    #    现在补上了三个，而**每一个都是真实下载成功过的**（不是"看起来应该能用"）。
    #
    # ⚠️ 普查里还有几个服务器可达但**不能放进这里**的（Pecný / GSI / ESA…）：
    #    它们的目录结构不标准（猜不出来）—— 而且 ESA 那种更要小心，
    #    它只有 `ESA0OPSFIN_*`，那是**另一个分析中心的解算结果**，
    #    换上去等于**悄悄换掉了分析输入**。**一个错的源比没有源更糟。**
    # ⚠️ 文件名和 URL 都从 `_product_names` / 两个 URL 构造函数取，
    #    **不要在这里再拼一遍字面量**。老日期要换成短名，
    #    而 `filename` 和 URL 基名对不上的话，缓存 glob、配置引用、
    #    下载落盘的名字会三处各不相同。
    for kind, hint in (("sp3", "约 100 KB"), ("clk", "约 1.1 MB"),
                       ("erp", "约 400 B")):
        # ⚠️⚠️ **字典的顺序 = 尝试的顺序**（fetch 只试前 `MAX_SOURCES_PER_FILE` 个）。
        #
        #    所以"武大放第一个"是一个**有意的决定**，不是随手加的：
        #    实测同一天同一个文件，武大 1792~3165 KB/s，BKG 47~60 KB/s ——
        #    差 20~30 倍。**用户抱怨的"慢"就是这个。**
        #
        #    ⚠️ 它的值是 `lookup://…` 而不是 http URL —— 因为武大要两步
        #       （先查清单拿 id，再下载）。详见 ginan/sources.py。
        #       解析不出来时 fetch 会自动落到下一个源，不会卡住。
        #
        #    ⚠️⚠️ 这里要的是**它库里的文件名**（带压缩后缀），不是落盘后的名字。
        #        `names[kind]` 是落盘名（`…_ORB.SP3`），而武大库里存的是
        #        `…_ORB.SP3.gz` —— **少一个后缀就永远查不到**。
        #
        #        实测踩过：解析无声地返回 None，于是**每次都掉到 BKG**，
        #        而表面上一切正常（文件下下来了、内容也对），
        #        只是**一直是慢的那个源**。这类"降级是静默的"最难发现。
        #
        #        后缀随命名年代变（和 `_cddis_urls` 同一个判断）：
        #        新式 `.gz`，老式短名是 `.Z`（`igr21992.sp3.Z`）。
        remote = names[kind] + (
            ".Z" if gps_week(day) < OLD_NAMING_BEFORE_WEEK else ".gz"
        )
        urls = {"whu": sources.lookup_token("whu", "jmxl", remote)}
        # ⚠️ 紧跟着武大的 HTTP —— 它们是**同一个数据中心的两种协议**，
        #    实测速度 whu(HTTP) 1.3~3 MB/s > whu(FTP) 255 KB/s > 其余。
        #    2024 那次改版只关掉了 HTTP 的目录树，FTP 没受影响 ——
        #    两条路是会被**分别**改坏的，所以都留着。
        #
        #    ⚠️ 只给 sp3/clk/erp 加。广播星历在 FTP 上是不是同一个路径
        #       **没有验证过** —— 没验过的东西不写进来。
        if kind in whu_ftp:
            urls["whu_ftp"] = whu_ftp[kind]
        if kind in sio:
            urls["sio"] = sio[kind]
        if kind in bkg:
            urls["bkg"] = bkg[kind]
        if kind in cddis:
            urls["cddis"] = cddis[kind]
        if kind in nrcan:
            urls["nrcan"] = nrcan[kind]
        plan.files.append(
            FilePlan(
                filename=names[kind],
                kind=kind,
                urls=urls,
                size_hint=hint,
                day=day,
            )
        )

    # ---- 备选产品（**默认不下载**）----
    #
    # ⚠️ 这些**不是镜像**，是**另一个分析中心的解算结果**。
    #    换上去会**改变结果**（毫米到厘米级），所以：
    #      · 默认不下（`fetch_plan` 根本不看这一栏）
    #      · 只在镜像全挂了、**并且用户点头**之后才下
    #    详见 RunPlan.alternatives 和 ginan_tools.do_fetch_data。
    #
    # ⚠️ 只给 sp3/clk/erp 配：
    #    · ESA 没有 IGS 那样的广播星历产品
    #    · 天线表我们已经有两个稳的源（BKG + IGS 官网），不需要备选
    #
    # ⚠️ 这里的**顺序无关紧要** —— 备选那条路同样走 `fetch` 的实测测速
    #    （`_rank_by_speed`）。列出来的顺序只是"测速失败时的兜底"。
    #
    # ⚠️ AIUB 那条**路径能直接拼**（不用两步查询），而 igmas/esa 要查表 ——
    #    所以它到得比别人早，但**慢**（实测 22 KB/s）。这正好是测速该干的事。
    aiub = _aiub_urls(day)
    gssc = _gssc_urls(day)
    for kind in ("sp3", "clk", "erp"):
        alts = {
            "igmas": sources.lookup_token("igmas", kind, ""),
            "esa": sources.lookup_token("esa", kind, ""),
        }
        if kind in aiub:
            alts["aiub"] = aiub[kind]
        if kind in gssc:
            alts["gssc"] = gssc[kind]
        plan.alternatives[kind] = alts

    # ---- 观测数据 ----
    #
    # ⚠️ 注意这一类和上面的产品文件有一个**本质区别**：
    #
    #     产品文件（SP3/CLK/ERP/ATX）是**全球统一**的 ——
    #       同一份轨道文件，BKG、CDDIS、其他 IGS 数据中心都有副本，
    #       所以"多源互替"是天然的。
    #
    #     观测数据是**测站专属**的 ——
    #       ALIC 站的观测数据只存在于**运营它的机构**那里。
    #       澳测站的运营方是 GA，所以只有 GA 有（或它的镜像）。
    #       这不是设计缺陷，是数据的物理归属。
    #
    # 所以这里的"多源"不是"同一个文件 有好几个镜像"，
    # 而是**"不同的站要问不同的机构要"**。
    #
    # ⚠️ TODO（M4）：目前只支持 GA 的站。
    #    要支持 IGS 全球测站，需要按站号查它的运营机构，
    #    再去对应的数据中心取（CDDIS 需要拼出完整文件名，
    #    其中含国家代码，得先有测站元数据才能拼对）。
    #    在做到之前，非 GA 的站会明确提示"不支持"，
    #    而不是给一个 404 让人困惑。
    #
    # ⚠️ 观测数据的**真实文件名我们事先也不知道** ——
    #    GA 的 API 会返回一个预签名 URL，文件名在那里面。
    #    所以这里先填一个"占位名"，下载时再根据实际情况确定。
    #    这也是为什么"识别文件"必须靠内容嗅探（见 inbox.py）：
    #    名字是不可靠的。
    # ⚠️ **只有观测数据这一项是按站分叉的** —— 上面那些产品全部只加一份。
    for st in clean:
        obs_urls = obs_urls_for(st, day)
        plan.files.append(
            FilePlan(
                filename=f"{st}_UNKNOWN.rnx",   # 占位，下载后改名
                kind="obs",
                urls=obs_urls,
                # ⚠️⚠️ **这个 `day` 是必须的** —— 2026-09-29 加武大观测时
                #    漏了它，`check_network` 当场拦下。
                #
                #    `fetch` 调 `sources.resolve(..., day=item.day)`，
                #    而 `resolve` 第一句就是 `if day is None: return None`。
                #    ⇒ 漏了它的话武大那条**永远是"没查到"**，然后
                #      **静默降级到 GA** —— 对 GA 有的站毫无影响（所以看起来全绿），
                #      而 GA **没有**的那些站（WUH2/URUM/GMSD…）就什么都拿不到。
                #
                #    之前在 GA 那条路上不需要它（GA 把日期写在 URL 里了），
                #    所以这个字段对这种文件从来没被用过。
                day=day,
                size_hint="约 50 MB",
            )
        )

    # ---- 卫星元数据（一次性）----
    #
    # ⚠️ 这一份是为了消掉 Ginan 的 `Warning: Invalid SINEX file`。
    #
    #    它记录每颗卫星的**型号**（Block IIR / IIF / Galileo-1/2…）和姿态参数。
    #    没有它，Ginan 会给每颗卫星打两条警告：
    #        Max yaw rate not found for G063 in satYawGpsIIR…
    #        Attitude model not implemented for BLOCK IIF…
    #    —— 一次 24 小时的解算里实测有 50 多条。
    #
    # ⚠️ **只有 IGS 官网有这一个源**（实测：BKG 和 SIO 都是 404，
    #    武大整个文件库里也没有 —— 枚举过一周的全部 3200 个文件）。
    #    好在它是**一次性**文件（下过一次就缓存），单源的风险可以接受；
    #    真下不到的话，行为退回到"以前那样"（多一些警告），不会跑不了。
    #
    # ⚠️ 用**不带日期**的那一份。带日期的（`…_2434.snx`）是每周快照，
    #    对我们是多余的：Ginan 按 PRN 匹配，多余的新卫星不会被用上。
    plan.files.append(snx_plan())

    # ---- 码偏差（按天）----
    #
    # ⚠️ 这一份是为了消掉 `Bias not found for … using undefined_sigma: 1`。
    #    选型和理由写在 `_cod_bias_urls` 里（含实测过的四个候选对比）。
    # ⚠️ 老日期（周 2238 以前）**根本没有这个产品** —— `_cod_bias_urls`
    #    会返回空字典，那就**不要把它放进计划**。
    #    放进去了会怎样：下载时每个源都 404，最后报"失败 1 个"，
    #    而用户/模型会去查一个不存在的东西。见 BIAS_FROM_WEEK。
    bias = _cod_bias_urls(day)
    if bias:
        plan.files.append(
            FilePlan(
                filename=f"COD0OPSRAP_{yd}0000_01D_01D_OSB.BIA",
                kind="bia",
                urls=bias,
                size_hint="约 60 KB",
                day=day,
            )
        )

    # ---- 广播星历 ----
    #
    # ⚠️⚠️ **它是全球统一的，和测站无关** —— 所有测站看到的是同一份卫星
    #     广播星历。（这一点和上面的观测数据**正好相反**：观测数据是测站专属的。）
    #
    # 所以这里**只能**用全球源，绝不能把测站传进去。
    #
    #     ⚠️ 原来就是错的：代码写的是 `ga_rinex_url(station, day, "nav")`，
    #        而**上面几行的注释里自己还写着"是全球统一的（不分测站）"** ——
    #        然后下一行照样把 station 传了进去。
    #
    #        后果：GA 的 API 对**非 GA 的测站**返回**空数组**（实测：非 GA 站 → `[]`），
    #        于是这一项**必定下载失败**。而失败信息看起来像"GA 超时"，
    #        指向的是网络问题 —— **离真正的根因（传错了参数）非常远**。
    #
    #        **错的不是源，是把测站传了进去。**
    #
    # ✅ 现在用武大为主源、BKG 和 CDDIS 为备源，三者都是全球源。
    #
    # ⚠️ 武大的广播星历走 `gbxlList`，**和精密产品不是同一个接口**
    #    （用 jmxlList 查广播星历会得到 0 条 —— 实测踩过）。
    #    所以 token 里那个 `gbxl` 不是分类标签，是**接口名**。
    nav_name = f"BRDC00IGS_R_{yd}0000_01D_MN.rnx"
    plan.files.append(
        FilePlan(
            # ⚠️ 用**真实文件名**，不再是占位符 —— 因为全球源的文件名我们是
            #    事先就知道的（它不随测站变）。这样配置里写的就是最终名字。
            filename=nav_name,
            kind="nav",
            urls={
                "whu": sources.lookup_token("whu", "gbxl", nav_name + ".gz"),
                "bkg": _bkg_brdc_url(day),
                "cddis": _cddis_brdc_url(day),
            },
            size_hint="约 13 MB",
            day=day,
        )
    )

    # ---- 按工作流筛一遍 ----
    #
    # ⚠️⚠️ **`Workflow.needs` 原来是个死字段** —— 全项目没有一处读它，
    #     而 `workflows.py` 里的文档写着「用途：`plan_run` 按这个决定要下什么」。
    #     一边声称在用、一边没人用，这种不一致比"没有这个字段"更糟：
    #     改 `needs` 的人会以为改了就生效。
    #
    #     （实测踩过：把 `"bia"` 从 `pod.needs` 里去掉，**一点效果都没有**。）
    #
    # ⚠️ 位置在**最后**、只筛一次 —— 上面那些块是手写的一条一条，
    #    在每一块里单独判断"这个工作流要不要"会让 `needs` 又变成装饰品。
    #
    # ⚠️ 对 PPP 是**恒等变换**（它的 needs 正好是全部 8 类）——
    #    所以这条改动不动 PPP 的取数计划，那条路有实测数字背书。
    if workflow:
        from . import workflows as _wf

        wanted = set(_wf.get(workflow).needs)
        plan.files = [f for f in plan.files if f.kind in wanted]

    return plan


def _name_from_url(url: str) -> str:
    """从一个下载地址里取出**文件名**（可能带压缩后缀，调用方负责去掉）。

    ⚠️ 两种形状，实测过（**不能只取路径最后一段**）：

        ESA：  文件名在路径里
               …/gnss-products/2434/ESA0OPSFIN_20262450000_01D_05M_ORB.SP3.gz

        iGMAS：文件名藏在**查询参数**里，路径最后一段是**空的**
               …/downItem/?fid=/products/products/1078/b1cb2a/isc10783.sp3.Z
               ↑ urlparse 之后 path 是 "/Product/TreePage/downItem/"

    ⚠️ 只取路径的话，iGMAS 会得到空字符串 —— 而空名字在路径拼接里
       **一点提示都没有**，直到下载器去操作那个目录才炸。

    ⚠️⚠️ **取不到"像个文件名"的东西就返回空**，绝不返回路径里随便一段：

        武大的下载地址是 `…/index.php/igs/download?year=2026&id=310281`，
        路径最后一段是 **`download`** —— 那是个**动词**，不是文件名。
        拿它当文件名会老老实实造出一个叫 `download` 的文件，
        **而且一路都不会报错**。

        👉 判据用"有没有扩展名"：我们的产品文件一律带扩展名
           （`.sp3` / `.clk` / `.Z` / `.gz`），没扩展名的一定不是文件名。
    """
    import re
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    name = parsed.path.rsplit("/", 1)[-1]

    if not name and parsed.query:
        m = re.search(r"fid=([^&]+)", parsed.query)
        if m:
            name = unquote(m.group(1)).rsplit("/", 1)[-1]

    # 必须像个文件名：路径里那一小段带扩展名才算
    if "." not in name:
        return ""

    return name


def alternative_files(
    plan: RunPlan,
    kinds: list[str],
    session=None,
) -> tuple[dict[str, list[FilePlan]], list[str]]:
    """把"备选产品"解析成**真能下载的 FilePlan**。

    ⚠️⚠️ **这是一个联网函数**，而且**只能在用户点头之后调用**。
       它问服务器要 ESA（将来还有 iGMAS）那一周的真文件名 ——
       这正是"为什么计划阶段不能建 FilePlan"的原因（见 RunPlan.alternatives）。

    返回 `({源: [FilePlan, …]}, 说明)` —— **按源分组，组的顺序就是优先级**。
    ⚠️ 为什么分组而不是一个平铺的列表：见下面那段（名字必须和内容配套，
       而每个源给的文件名不一样）。

    说明里写的是"哪些类型没有解析出来"，供调用方如实转述给用户。

    ⚠️ 文件名取自**解析出来的 URL**，不是拼出来的：
       URL 是服务器给的，而拼接是猜。拼接猜错的后果是
       **文件存成了另一个名字**，而配置里引用的是那个错名字 ——
       跑起来找不到文件，错误却指向"文件缺失"而不是"名字错了"。
    """
    groups: dict[str, list[FilePlan]] = {}
    notes: list[str] = []

    for kind in kinds:
        urls = plan.alternatives.get(kind)
        if not urls:
            notes.append(f"{kind}：没有备选来源")
            continue

        # ⚠️⚠️ **每个源各建一个 FilePlan，不能合并成一个 `urls` 字典。**
        #
        #     实测踩过，而且后果正是这个模块最怕的那种：
        #
        #         sp3 → 从 iGMAS 下来，存成 `isc10783.sp3`   ✓
        #         clk → iGMAS 那边没有，**落到 ESA**，
        #               却仍然存成 `isc10783.clk`           ✗✗
        #
        #     **名字说是 iGMAS 的，内容其实是 ESA 的。**
        #     而配置是按文件名引用文件的 —— 于是"跑的是谁的产品"
        #     在磁盘上根本看不出来。
        #
        #     根因：`item.filename` 只能有一个，而**每个源给的
        #     文件名是不一样的**（`isc10783.clk` vs `ESA0OPSFIN_….CLK`）。
        #     把它们塞进同一个 `urls`，就是逼着一个名字去覆盖两种内容。
        #
        #     👉 所以"源的优先级"从**一个 FilePlan 内的 urls 顺序**
        #        提升成了**组与组之间的顺序**。调用方一组一组地试，
        #        哪一组成了就用哪一组 —— 名字永远和内容配套。
        for source, token in urls.items():
            real = sources.resolve(token, plan.day, session=session)
            if not real:
                continue

            name = _name_from_url(real)
            if not name:
                # ⚠️⚠️ **空名字必须当场拒绝**，不能放它进 FilePlan。
                #
                #     实测踩过：空名字会让 `dest_dir / ""` 等于**目录本身**，
                #     于是下载器去删那个目录，报
                #         `PermissionError: [WinError 5] … Temp\igmas_xxxx`
                #     —— 报错里只有一个临时目录路径，**看不出和文件名有关**。
                #
                #     > 一个空字符串在路径拼接里没有任何提示音。
                notes.append(f"{kind}：{source} 的地址里取不到文件名")
                continue

            # 去掉压缩后缀 —— 磁盘上落的是**解压后**的名字，
            # 和 IGS 那边的规矩一致（`igr21992.sp3.Z` → `igr21992.sp3`）。
            for suffix in (".gz", ".Z"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]

            groups.setdefault(source, []).append(
                FilePlan(
                    filename=name,
                    kind=kind,
                    urls={source: real},
                    size_hint="（备选产品）",
                    day=plan.day,
                )
            )

    return groups, notes


def plan_to_json(plan: RunPlan, workflow: str | None = None) -> str:
    """把计划转成给模型看的 JSON。

    这是领域层和 agent 层的接口 —— 领域层负责算，agent 层负责转格式。
    这里提供一个现成的转换，省得每处都手写一遍 asdict。

    ⚠️ `workflow`（要做什么事）**不属于 `RunPlan`** —— `RunPlan` 回答的是
       "要哪些文件"。所以它是参数，不是字段。

    ⚠️⚠️ 但它**必须出现在回执里**：同一批文件，不同工作流要做的事完全不同。

           ppp：轨道和钟差是**已知输入**
           pod：轨道是**被估的对象**（SP3 进 `pseudo_observations`）

       不说的话，模型拿到一份看起来一样的文件清单，
       会按 PPP 去理解 POD 的计划。
    """
    from . import workflows as _wf

    w = _wf.get(workflow)
    return json.dumps(
        {
            "workflow": {
                "key": w.key,
                "label": w.label,
                "purpose": w.purpose,
                # ⚠️ 工作流自己那句提醒（比如 POD 要全球分布的站网）——
                #    这是**必须转达给用户**的东西，不能只活在代码里。
                **({"note": w.note} if w.note else {}),
            },
            # ⚠️ 键名用复数 —— 单站时是一个只含一项的列表。
            #    模型读到 `stations: ["ALIC"]` 就知道"可以不止一个"，
            #    而读到 `station: "ALIC"` 会以为这里永远只能有一个。
            "stations": plan.stations,
            "date": plan.day.isoformat(),
            "gps_week": gps_week(plan.day),
            "summary": plan.describe(),
            "files": [
                {
                    "filename": f.filename,
                    "kind": f.kind,
                    "label": f.label,
                    "purpose": f.purpose,
                    "size_hint": f.size_hint,
                    "one_time": f.one_time,
                    "available_from": [
                        {"source": k, "label": SOURCES[k].label, "needs_account": SOURCES[k].requires_auth}
                        for k in f.sources
                        if k in SOURCES
                    ],
                }
                for f in plan.files
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
