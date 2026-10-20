"""Ginan 领域层 —— 把结果画成**正规的图**（plotly）。

============================ 这些图是照 Ginan 抄的 ============================
不是"自己想的图"，是 `ginan-dist/.../scripts/plot_pos.py` 的四张图，
逐条对着实现的（行号指的是那份源码）：

    Fig1  时序      `plot_pos.py:165-298`
          每个历元的**误差棒** ±σ、**LOWESS 平滑**线、**加权均值**线、
          **±2σ 置信带**（`compute_statistics` 的口径见 `series.py`）、
          标题里带每分量的 加权均值 / 标准差 / RMS、x 轴带 rangeslider
    Fig2  平面      `plot_pos.py:300-389`
          dE–dN 散点，**1:1 等比例**（`scaleanchor`，1 厘米东 = 1 厘米北）
          可选按合成不确定度 `sqrt(sN²+sE²)` 上色
    Fig4  热力图    `plot_pos.py:437-493`
          dE–dN 的 2D 等高线 + 散点 + **边缘直方图**（x2/y2 域）+ 加权均值标记

⚠️ **没做的：Fig3 地图**（`plot_pos.py:391-435`）——
   它要 `open-street-map` 在线底图（我们用离线），而且**静态测站的经纬度
   几乎不动**（那是给动态平台看的图）。这是"做不了/没必要"，不是"还没做"。

============================ 和 Ginan 的两处**有意不同** ============================
① **单位用厘米**（Ginan 用米）。cm 级的解用米写出来是 `0.012` ——
   给不懂 GNSS 的人看，`1.2 厘米` 比 `0.012 米` 直接得多。
   ⚠️ 单位**写在轴标题上**，不会歧义，也不会有人把数字读错。
② **标签是中文**（Ginan 是英文）。理由同上：
   我们的用户不是 GNSS 工程师，"Weighted Mean" 对他不是信息。
③ **位置按「解算自己的参考位置」减去一个常数**（`posfile.self_reference()`）——
   `.POS` 里那三列是相对**文件头的参考坐标**的偏差，而那个坐标可能很旧。
   实测（ALIC 2026-09-02 整天，2880 个历元）：

       文件头参考      加权均值 **+38.36 厘米**、RMS 38.70 厘米
       解算自身参考    加权均值 **+0.01 厘米**、RMS  1.82 厘米

   ⚠️⚠️ 不减的话图是**自相矛盾**的：曲线飘在 +38 厘米上，
      而图下面那行说明写着"贴住 0 表示稳定下来了" ——
      用户看到的是"这条线根本没贴住 0"。侧栏的结论（重复性 2.9 厘米）
      也是同源的，图必须和它说同一件事。
   ⚠️ **标准差不受影响**（平移不改变离散程度），所以只有均值和 RMS 会变 ——
      也正因为如此，只改一半（曲线减了、标题没减）看起来才"像那么回事"，
      这一条是实测出来的，不是推的。

============================ 怎么送到界面 ============================
服务端用 plotly 拼图 → `fig.to_json()` → 前端用随包的 `plotly.min.js`
`Plotly.newPlot(el, fig.data, fig.layout)`。

⚠️ **不是** `fig.to_html()` 那条路：那个产出的是 `<div>` + `<script>`，
而 `<script>` 插进 `innerHTML` **不会执行**（浏览器规定）。走 JSON 就没这问题。
"""

from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import plotly.graph_objects as go

from . import series as gseries
from . import tracefile as gtracefile

# 三个分量在界面上的名字和颜色。
#
# ⚠️ 颜色照 Ginan（`plot_pos.py:178`：dN 红 / dE 绿 / dU 蓝）——
#    **不改**，因为"北红东绿高蓝"是这个领域的惯例，
#    用户看别人论文/软件时颜色是对得上的。
COMPONENTS = (
    ("dN", "dn", "sn", "北向", "#d63b3b"),
    ("dE", "de", "se", "东向", "#2fa84f"),
    ("dU", "du", "su", "高程", "#3b6fd6"),
)

# LOWESS 的跨度。默认 2/3 是 statsmodels 的默认（Ginan 让用户传，见 `plot_pos.py:117`）。
LOWESS_FRAC = 2.0 / 3.0

# ⚠️ 图上最多画多少个点。**这一条是拿"图还能不能读"定的，不是拿性能定的**：
#    一天 30 秒采样 = 2880 点，在一条 900 px 宽的线上是每像素 3 个点，
#    误差棒会糊成一片。抽到 1500 既是"看不出差别"的上限，
#    也让 plotly 的 JSON 保持在几百 KB 量级（省的是**传输和渲染**）。
MAX_POINTS = 1500


def _downsample(n: int) -> np.ndarray:
    """等间隔取索引，**并保住最后一个点**（理由同 `posfile.plot_series`）。"""
    if n <= MAX_POINTS:
        return np.arange(n)
    stride = max(1, math.ceil(n / MAX_POINTS))
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return np.asarray(idx, dtype=int)


def _extract(pf):
    """从一份 .POS 里取出画图要的全部列（一次取齐，免得各处再索引）。

    ⚠️⚠️ 位置**先减去 `self_reference()`** —— 见文件头「第三处有意不同」。
       σ 那一列**不减**：它是形式误差，和平移无关。
    """
    t = np.array([e.time.timestamp() for e in pf.epochs], dtype=float)
    # 顺序和 COMPONENTS 一致：dN/dE/dU ↔ ref 的第 0/1/2 项
    ref = pf.self_reference()
    out = {"t": t}
    for i, (key, attr, sig, _label, _color) in enumerate(COMPONENTS):
        out[key] = (np.array([getattr(e, attr) for e in pf.epochs],
                             dtype=float) - ref[i])
        out[sig] = np.array([getattr(e, sig) for e in pf.epochs], dtype=float)
    return out


def _iso(times: np.ndarray) -> list[str]:
    """时间轴用 ISO 字符串 —— plotly 认这种格式，而且**带时区信息**。"""
    import datetime as dt

    return [dt.datetime.fromtimestamp(float(t)).isoformat(timespec="seconds")
            for t in times]


def _stats_title(pf, data, idx) -> str:
    """标题里那行统计量 —— 照 Ginan（`plot_pos.py:261`）。

    ⚠️ Ginan 的写法是 `Weighted Mean = … Std Dev = … RMS = …`，
       我们用中文，但**保留三样**：加权均值 / 加权标准差 / RMS。
    """
    parts = []
    for key, _attr, sig, label, _c in COMPONENTS:
        st = gseries.weighted_stats(data[key][idx], data[sig][idx])
        parts.append(
            f"{label}：加权均值 {st['weighted_mean'] * 100:+.2f}、"
            f"加权标准差 {st['std_dev'] * 100:.2f}、"
            f"RMS {st['rms'] * 100:.2f} 厘米"
        )
    head = f"{pf.station or ''} {pf.solution or ''}".strip() or "定位结果"
    return head + "<br><sub>" + "　｜　".join(parts) + "</sub>"


def _default_y_range(pf, data) -> list[float] | None:
    """主图的**默认纵轴范围** —— 让「收敛之后」那一段能被看清。

    ⚠️⚠️ 为什么必须有这一条（实测，不是审美）：

       ALIC 2026-09-02 那一整天，第一个历元的高程**误差棒是 ±199 厘米**
       （滤波器刚起步，形式误差就是这么大）。plotly 自动缩放会照着它把
       纵轴拉到 ±400 —— 于是**后面 2870 个历元全挤在一条水平线上**，
       ±2σ 带、趋势线、误差棒一个都看不出来。

       👉 一张"一条直线"的图，和"这次解算稳不稳"这个问题**没有关系** ——
          它会让人以为程序没在干活。

    ⚠️ 判据是**收敛之后**那一段的 99 百分位 ×3，而不是最大值：
       最大值就是那个起步的误差棒，用它定范围等于没限。
    ⚠️ **收敛判不出来（`converged_from() < 0`）就不限** ——
       那时候"后面挤成一条线"恰恰是要看的东西，限了才是真的藏问题。
    ⚠️ 限了范围，超出去的点就画不到 —— 所以图下面那行说明里要写清
       "开头那几分钟在范围外，拖滑块往回看"（`reading_note` 里那句）。
    """
    conv = pf.converged_from()
    if conv <= 0:
        return None
    # ⚠️ 用**全部**收敛之后的历元（不是抽过样的 `idx`）——
    #    抽样会漏掉个别跳点，而这里要的正是"这段到底有多散"。
    tail = np.concatenate([data[k][conv:] * 100.0 for k in ("dN", "dE", "dU")])
    tail = tail[np.isfinite(tail)]
    if tail.size == 0:
        return None
    lim = max(3.0 * float(np.percentile(np.abs(tail), 99.0)), 1.0)
    # ⚠️ 只有"限完确实好读"才限：拿**自动缩放会取到的范围**（含误差棒，
    #    也就是第一版那张被压平的图）比一比。差不多就别动它 ——
    #    白限一次等于给图加了个没必要的裁切。
    full = max(float(np.max(np.abs(data[k] * 100.0)))
               + float(np.max(np.abs(data[s] * 100.0)))
               for k, s in (("dN", "sn"), ("dE", "se"), ("dU", "su")))
    if lim * 1.5 >= full:
        return None
    return [-lim, lim]


def time_series_figure(pf, *, smooth: bool = True) -> dict:
    """Fig1 —— 三维位置随时间的变化。**这是主图。**"""
    data = _extract(pf)
    idx = _downsample(pf.count)
    if idx.size == 0:
        return {}

    t = _iso(data["t"][idx])
    fig = go.Figure()

    for key, _attr, sig, label, color in COMPONENTS:
        y = data[key][idx] * 100.0
        s = data[sig][idx] * 100.0
        smooth_y = None
        if smooth and idx.size >= 12:
            # ⚠️ 平滑用**抽过样之后**的点：Ginan 是在全量上平滑的，
            #    这里抽到 1500 点之后平滑 —— 差的量级远小于线宽，
            #    而省下的是每次出图的 O(n²)（2880² = 830 万次）。
            sm_all = gseries.smooth_series(data[key][idx], data[sig][idx],
                                           frac=LOWESS_FRAC)
            smooth_y = sm_all["smooth"] * 100.0
            st = sm_all["stats"]
        else:
            st = gseries.weighted_stats(data[key][idx], data[sig][idx])

        # ① 原始观测 + 误差棒（±σ）。Ginan 用 markers + error_y（`plot_pos.py:206`）
        #
        # ⚠️⚠️ 图例只留**每个分量一条**（`legendgroup` + 其余 showlegend=False）：
        #     9 条踪迹全列出来的话，图例比图还高，而且会和 rangeslider 叠在一起
        #     （第一版就是这样，截图里糊成一团）。
        #     用 legendgroup 之后，**点一下图例就开关那一个分量的全部踪迹** ——
        #     功能没少，图例干净了。
        fig.add_trace(go.Scatter(
            x=t, y=y, mode="markers", name=label,
            legendgroup=label,
            marker=dict(size=4, color=color),
            error_y=dict(type="data", array=s, arrayminus=s, visible=True,
                         color="rgba(150,150,150,.45)", thickness=1, width=0),
            hovertemplate=(f"{label} %{{y:.2f}} 厘米<br>"
                           "±%{error_y.array:.2f} 厘米<br>%{x}<extra></extra>"),
        ))

        # ② LOWESS 平滑（Ginan 只在用户给了 --*-smoothing 时才画，我们默认画）
        if smooth_y is not None:
            fig.add_trace(go.Scatter(
                x=t, y=smooth_y, mode="lines", name=f"{label}（平滑后的趋势）",
                legendgroup=label, showlegend=False,
                line=dict(color=color, width=2.5),
                hovertemplate=f"{label} 趋势 %{{y:.2f}} 厘米<extra></extra>",
            ))

        # ③ 加权均值 ④ ±2σ 带 —— 顺序要紧：带要画在均值线**下面**
        wm = st["weighted_mean"] * 100.0
        band = 2.0 * st["std_dev"] * 100.0
        fig.add_trace(go.Scatter(
            x=t, y=[wm + band] * len(t), mode="lines",
            line=dict(width=0), showlegend=False, hoverinfo="skip",
            legendgroup=label, name=f"{label} 上界",
        ))
        fig.add_trace(go.Scatter(
            x=t, y=[wm - band] * len(t), mode="lines",
            line=dict(width=0), fill="tonexty",
            fillcolor="rgba(120,120,120,.13)",
            legendgroup=label, showlegend=False,
            name=f"{label} ±2σ（95%）",
            hovertemplate=f"{label} ±2σ 带<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=t, y=[wm] * len(t), mode="lines",
            line=dict(color=color, width=1, dash="dash"),
            legendgroup=label, showlegend=False,
            name=f"{label} 加权均值",
            hovertemplate=f"{label} 加权均值 %{{y:.2f}} 厘米<extra></extra>",
        ))

    fig.update_layout(
        title=_stats_title(pf, data, idx),
        xaxis=dict(title="时间", type="date", rangeslider=dict(visible=True),
                   rangeslider_thickness=0.05),
        yaxis=dict(title="相对最终位置的偏差（厘米）",
                   range=_default_y_range(pf, data)),
        # ⚠️ **不写 height** —— 让图填满容器（前端那个浮层给多高就多高）。
        #    写死的话，在大屏上底下留一大片空白，在小屏上又要滚 ——
        #    同一个数字不可能两种屏幕都对。
        autosize=True,
        margin=dict(l=70, r=20, t=100, b=40),
        hovermode="closest",
        # 图例放进图内左上角（半透明底），不去和 rangeslider 抢底部那条
        legend=dict(x=0.01, y=0.99, xanchor="left", yanchor="top",
                    bgcolor="rgba(255,255,255,.72)",
                    bordercolor="rgba(0,0,0,.12)", borderwidth=1,
                    font=dict(size=11)),
    )
    return json.loads(fig.to_json())


def plane_figure(pf, *, colour_sigma: bool = False) -> dict:
    """Fig2 —— 平面上的散布（东 vs 北），**1:1 等比例**。

    ⚠️ 等比例不是装饰：`scaleanchor` 保证 1 厘米东和 1 厘米北在图上是同一长度。
       画成不等比例的话，一个南北向的"长条"会看起来像圆的 ——
       而这正是判断"哪个方向差"的地方（`plot_pos.py:359-360`）。
    """
    data = _extract(pf)
    idx = _downsample(pf.count)
    if idx.size == 0:
        return {}

    de = data["dE"][idx] * 100.0
    dn = data["dN"][idx] * 100.0
    comp = np.sqrt(data["sn"][idx] ** 2 + data["se"][idx] ** 2) * 100.0

    st_n = gseries.weighted_stats(data["dN"][idx], data["sn"][idx])
    st_e = gseries.weighted_stats(data["dE"][idx], data["se"][idx])

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=de, y=dn, mode="markers", name="每个历元",
        marker=dict(
            size=6,
            color=comp if colour_sigma else "#3b6fd6",
            colorscale="Jet" if colour_sigma else None,
            showscale=colour_sigma,
            colorbar=dict(title="合成 σ（厘米）") if colour_sigma else None,
        ),
        text=[f"合成 σ {c:.2f} 厘米" for c in comp],
        hovertemplate="东 %{x:.2f} 厘米<br>北 %{y:.2f} 厘米<br>%{text}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[st_e["weighted_mean"] * 100.0], y=[st_n["weighted_mean"] * 100.0],
        mode="markers", name="加权均值",
        marker=dict(size=15, color="white", symbol="x",
                    line=dict(color="black", width=2)),
        hovertemplate="加权均值<extra></extra>",
    ))
    fig.update_layout(
        title=f"{pf.station or ''} 平面散布（1:1）",
        xaxis=dict(title="东向偏差（厘米）"),
        yaxis=dict(title="北向偏差（厘米）", scaleanchor="x", scaleratio=1),
        autosize=True,
        margin=dict(l=70, r=20, t=70, b=50),
        legend=dict(x=0.01, y=0.99, xanchor="left", yanchor="top",
                    bgcolor="rgba(255,255,255,.72)",
                    bordercolor="rgba(0,0,0,.12)", borderwidth=1,
                    font=dict(size=11)),
    )
    return json.loads(fig.to_json())


def heatmap_figure(pf) -> dict:
    """Fig4 —— 平面密度（等高线）+ 边缘直方图（`plot_pos.py:437-493`）。"""
    data = _extract(pf)
    idx = _downsample(pf.count)
    if idx.size == 0:
        return {}

    de = data["dE"][idx] * 100.0
    dn = data["dN"][idx] * 100.0
    st_n = gseries.weighted_stats(data["dN"][idx], data["sn"][idx])
    st_e = gseries.weighted_stats(data["dE"][idx], data["se"][idx])

    fig = go.Figure()
    fig.add_trace(go.Histogram2dContour(
        x=de, y=dn, colorscale="Jet", reversescale=True,
        xaxis="x", yaxis="y", showscale=False, ncontours=20,
        hovertemplate="东 %{x:.2f}<br>北 %{y:.2f}<extra>密度</extra>",
    ))
    fig.add_trace(go.Scatter(
        x=de, y=dn, mode="markers", xaxis="x", yaxis="y",
        marker=dict(color="rgba(0,0,0,.35)", size=3), showlegend=False,
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=[st_e["weighted_mean"] * 100.0], y=[st_n["weighted_mean"] * 100.0],
        mode="markers", xaxis="x", yaxis="y", name="加权均值",
        marker=dict(size=14, color="white", symbol="x",
                    line=dict(color="black", width=2)),
        hovertemplate="加权均值<extra></extra>",
    ))
    # 边缘直方图：x 方向贴在上面（xaxis2），y 方向贴在右边（yaxis2）
    fig.add_trace(go.Histogram(y=dn, xaxis="x2", marker=dict(color="#8ba4a8"),
                               showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Histogram(x=de, yaxis="y2", marker=dict(color="#8ba4a8"),
                               showlegend=False, hoverinfo="skip"))
    fig.update_layout(
        title=f"{pf.station or ''} 平面密度（东–北）",
        xaxis=dict(title="东向偏差（厘米）", domain=[0, 0.84], zeroline=False),
        yaxis=dict(title="北向偏差（厘米）", domain=[0, 0.84], zeroline=False,
                   scaleanchor="x", scaleratio=1),
        xaxis2=dict(domain=[0.85, 1], showticklabels=False, showgrid=False),
        yaxis2=dict(domain=[0.85, 1], showticklabels=False, showgrid=False),
        bargap=0, autosize=True, showlegend=False,
        margin=dict(l=70, r=20, t=70, b=50),
    )
    return json.loads(fig.to_json())


def reading_note(pf) -> str:
    """图下面那行「怎么读这张图」。

    ⚠️ 这条不是客套话，是**这张图最容易读错的地方**：
       纵轴是**相对解算自身最终位置**的偏差，不是"离真实位置的误差" ——
       图上看着贴住 0，不等于绝对精度好（那要用权威坐标去比，
       而程序不知道权威坐标）。`posfile` 的结论里已经反复强调这一点，
       图这边也必须说，否则用户会从图上得出比结论更强的判断。
    """
    note = (
        "纵轴是「相对本次解算最终位置」的偏差 —— 曲线贴住 0 表示"
        "「稳定下来了」，不等于「离真实位置准」（那要用 IGS 权威坐标去比）。"
        "红线=北向，绿线=东向，蓝线=高程；浅色带是 ±2σ（95%）；"
        "虚线是加权均值。"
    )
    # ⚠️ 限过纵轴范围就必须说出来：不说的症状是**用户以为开头那一段不存在**，
    #    或者更糟 —— 以为那根冲上去的线就是全部。
    # ⚠️ 这句要**点名是"时序图"** —— 它是三张图共用的一行说明，
    #    而"限了纵轴"只发生在时序图上（平面图/密度图没有这个动作）。
    #    不点名的话，用户在平面图上会去找一条并不存在的"范围外的开头"。
    if _default_y_range(pf, _extract(pf)) is not None:
        note += ("（时序图的纵轴默认只画到收敛之后的量级 —— 开头几分钟"
                 "（是从几米降下来的）在范围外，把下面的滑块往左拖就能看见。）")
    return note


def figure_kinds(pf) -> list[str]:
    """这份 `.POS` 该出哪几张图 —— **不渲染**，只回答"有哪些"。

    ⚠️ 单独抽出来是因为有两个调用者，而它们**必须给同一个答案**：
       · 侧栏要知道"查看图（N 张）"里的 N（`/api/results`）—— 它**不能**
         为此把图渲染一遍（几十 KB × 每张，而侧栏只需要一个数）
       · 真正的渲染（`/api/figures`）

    ⚠️ 图不是"能画就画"：
       · 历元太少（< 3）→ 一张都不出（画不出东西）
       · 位置被压成常数的解（平滑解）→ **只出时序图**：
         平面图和热力图都要求"点在一个面上散开"，而它的所有点重合在
         一个位置上 —— 热力图会是**一个像素的亮斑**，平面图是一个点。
         那两张"图"没有任何信息，却会占满屏幕。
    """
    if pf.count < 3:
        return []
    kinds = ["time_series"]
    if pf.repeatability_is_meaningful():
        kinds += ["plane", "heatmap"]
    return kinds


# 图的 key → 界面上的标签。⚠️ 标签只在这里写一次（前端拿它当标签页文字）。
FIGURE_LABELS = {
    "time_series": "位置随时间",
    "plane": "平面散布",
    "heatmap": "平面密度",
}


def pos_figures(pf) -> dict[str, dict]:
    """一份 `.POS` 的全部图。**这是给界面用的入口。**"""
    builders = {
        "time_series": time_series_figure,
        "plane": plane_figure,
        "heatmap": heatmap_figure,
    }
    # ⚠️ 顺序和取舍都来自 `figure_kinds` —— 不在这里再判断一遍
    out = {}
    for kind in figure_kinds(pf):
        fig = builders[kind](pf)
        if fig:
            out[kind] = fig
    return out


# =============================================================================
# 残差图（`.TRACE` → 观测质量）
# =============================================================================
#
# 照 Ginan 自己的 `scripts/plot_trace_res.py` 的图族来做（那份 3357 行，
# 画的是同一批数据）。挑出来的是**对"这数据好不好"最有用的四张**：
#
#     ① 残差随时间    —— 有没有整段变差、有没有系统性漂移
#     ② 归一化残差分布 —— 是不是近似正态、尾巴有多长（**最能量化的一张**）
#     ③ 每颗卫星的 RMS —— 是哪颗星在拉后腿
#     ④ 每个频点的 RMS —— 是哪个频点在拉后腿
#
# ⚠️ 没做的：Ginan 还有"卫星 × 测站热力图"（单站时只有一列，没有意义）、
#    模糊度重置事件（要另一族 trace 行，而且我们的配置里没开）。
#    这是"没必要 / 没数据"，不是"还没做"。

RESID_COLORS = {"CODE_MEAS": "#d98b2b", "PHAS_MEAS": "#7b5cd6"}

# 残差图的抽稀上限。⚠️ 比位置图那张（1500）宽，因为散点的信息密度低。
RESID_MAX_POINTS = 2000


def _resid_sample(rf, meas: str) -> list:
    """抽稀，但**永远保住超限的点**。

    ⚠️⚠️ 这一条是残差图和位置图最大的不同：位置图的离群点是噪声，
       残差图的离群点**正是要看的东西**（跳点、粗差、哪颗星有问题）。
       均匀抽稀会把它们抹掉，画出来一张"很干净"的图 —— 而那是假的。

       实测（ALIC 2026-09-02 一天）：PPP 残差 4900 条里归一化超 3 的
       只有几十条 —— 均匀抽到 2000 点几乎必然把它们全丢掉。
    """
    pts = rf.by_kind(meas)
    if not pts:
        return []
    bad, rest = [], []
    for p in pts:
        nz = p.normalized
        if (math.isfinite(nz) and abs(nz) > 3) or (
                not math.isfinite(nz) and abs(p.postfit) > 0.5):
            bad.append(p)
        else:
            rest.append(p)
    room = max(0, RESID_MAX_POINTS - len(bad))
    if room and rest:
        stride = max(1, math.ceil(len(rest) / room))
        rest = rest[::stride]
    out = bad + rest
    out.sort(key=lambda p: p.t)
    return out


def _meas_label(meas: str) -> str:
    return gtracefile.MEAS_LABEL.get(meas, meas)


def resid_time_figure(rf, summary: dict) -> dict:
    """① 后验残差随时间 —— 码和相位**分开画**（量级差两个数量级）。"""
    from plotly.subplots import make_subplots

    kinds = [k for k in rf.kinds if rf.by_kind(k)]
    if not kinds:
        return {}
    fig = make_subplots(rows=len(kinds), cols=1, shared_xaxes=True,
                        vertical_spacing=0.08,
                        subplot_titles=[_meas_label(k) for k in kinds])
    t0 = rf.points[0].t
    bin_min = summary.get("bin_minutes", 30)
    for row, meas in enumerate(kinds, start=1):
        pts = _resid_sample(rf, meas)
        color = RESID_COLORS.get(meas, "#666")
        fig.add_trace(
            go.Scatter(
                x=[p.t for p in pts], y=[p.postfit for p in pts],
                mode="markers", name=_meas_label(meas),
                marker=dict(size=4, color=color, opacity=0.55),
                text=[f"{p.sat} {p.sig}" for p in pts],
                hovertemplate="%{text}<br>%{x}<br>后验残差 %{y:.4f}"
                              "<extra></extra>",
            ),
            row=row, col=1,
        )
        # 分窗均值：散点看不出"有没有系统性漂移"，这条线能
        s = (summary.get("series") or {}).get(meas) or {}
        if s.get("t_minutes"):
            fig.add_trace(
                go.Scatter(
                    x=[t0 + dt.timedelta(minutes=m) for m in s["t_minutes"]],
                    y=s["mean"], mode="lines",
                    name=f"{_meas_label(meas)}（{bin_min} 分钟均值）",
                    line=dict(color=color, width=2.5),
                    hovertemplate="%{x}<br>均值 %{y:.4f}<extra></extra>",
                ),
                row=row, col=1,
            )
    fig.update_layout(
        title="观测残差随时间",
        autosize=True,
        margin=dict(l=70, r=20, t=70, b=40),
        legend=dict(x=0.01, y=0.99, xanchor="left", yanchor="top",
                    bgcolor="rgba(255,255,255,.72)",
                    bordercolor="rgba(0,0,0,.12)", borderwidth=1,
                    font=dict(size=11)),
        hovermode="closest",
    )
    fig.update_xaxes(rangeslider=dict(visible=True),
                     rangeslider_thickness=0.04, row=len(kinds), col=1)
    fig.update_yaxes(title_text="后验残差（米）", row=len(kinds), col=1)
    return json.loads(fig.to_json())


def resid_hist_figure(rf, summary: dict) -> dict:
    """② 归一化残差分布 —— **最能量化"残差正不正常"的一张**。

    ⚠️ 判据不是"看着像不像钟形"，是**尾巴**：正态的话 99.7% 落在 ±3σ 里。
       图上把 ±1、±3 画成竖线，超出去的部分一眼可见。
    """
    from plotly.subplots import make_subplots

    kinds = [k for k in rf.kinds if rf.by_kind(k)]
    if not kinds:
        return {}
    fig = make_subplots(rows=1, cols=len(kinds),
                        subplot_titles=[_meas_label(k) for k in kinds])
    for col, meas in enumerate(kinds, start=1):
        vals = [p.normalized for p in rf.by_kind(meas)
                if math.isfinite(p.normalized)]
        if not vals:
            continue
        color = RESID_COLORS.get(meas, "#666")
        fig.add_trace(
            go.Histogram(
                x=vals, nbinsx=80, marker=dict(color=color), name=meas,
                hovertemplate="归一化残差 %{x:.2f}<br>%{y} 条<extra></extra>",
            ),
            row=1, col=col,
        )
        for k in (-3, -1, 1, 3):
            fig.add_vline(x=k, line=dict(color="rgba(0,0,0,.35)", width=1,
                                         dash="dot"),
                          row=1, col=col)
    fig.update_layout(
        title="归一化残差分布（后验残差 ÷ 形式误差）",
        autosize=True, showlegend=False, bargap=0.02,
        margin=dict(l=70, r=20, t=70, b=50),
    )
    fig.update_xaxes(title_text="归一化残差（虚线：±1σ / ±3σ）")
    fig.update_yaxes(title_text="条数", row=1, col=1)
    return json.loads(fig.to_json())


def _resid_bars(summary: dict, key: str) -> dict:
    """按卫星 / 按频点的 RMS 柱状图（码和相位各一组）。"""
    by = summary.get(key) or {}
    kinds = [k for k in by if by[k]]
    if not kinds:
        return {}
    fig = go.Figure()
    for meas in kinds:
        table = by[meas]
        names = list(table)
        fig.add_trace(go.Bar(
            x=names, y=[table[n]["rms"] for n in names],
            name=_meas_label(meas),
            marker=dict(color=RESID_COLORS.get(meas, "#666")),
            text=[f"{table[n]['n']} 条" for n in names],
            hovertemplate="%{x}<br>RMS %{y:.4f}<br>%{text}<extra></extra>",
        ))
    label = "卫星" if key == "by_satellite" else "频点"
    fig.update_layout(
        title=f"每个{label}的残差 RMS",
        barmode="group", autosize=True,
        margin=dict(l=70, r=20, t=70, b=50),
        legend=dict(x=0.01, y=0.99, xanchor="left", yanchor="top",
                    bgcolor="rgba(255,255,255,.72)",
                    bordercolor="rgba(0,0,0,.12)", borderwidth=1,
                    font=dict(size=11)),
    )
    fig.update_xaxes(title_text=label)
    fig.update_yaxes(title_text="RMS（米）")
    return json.loads(fig.to_json())


def resid_figure_kinds(rf) -> list[str]:
    """这份残差该出哪几张 —— **不渲染**（同 `figure_kinds` 的道理）。"""
    if rf.count == 0:
        return []
    kinds = ["time"]
    if any(math.isfinite(p.normalized) for p in rf.points):
        kinds.append("hist")
    if rf.count >= 30:
        kinds += ["sat", "sig"]
    return kinds


RESID_FIGURE_LABELS = {
    "time": "残差随时间",
    "hist": "归一化残差分布",
    "sat": "每颗卫星",
    "sig": "每个频点",
}


def resid_figures(rf, summary: dict) -> dict[str, dict]:
    """一份 `.TRACE` 的全部图。**给界面用的入口。**"""
    builders = {
        "time": lambda: resid_time_figure(rf, summary),
        "hist": lambda: resid_hist_figure(rf, summary),
        "sat": lambda: _resid_bars(summary, "by_satellite"),
        "sig": lambda: _resid_bars(summary, "by_signal"),
    }
    out = {}
    for kind in resid_figure_kinds(rf):
        fig = builders[kind]()
        if fig:
            out[kind] = fig
    return out


def resid_reading_note(summary: dict) -> str:
    """残差图下面那行「怎么读」。"""
    kinds = summary.get("kinds") or {}
    if kinds and any(not d.get("normalized_available")
                     for d in kinds.values()):
        return (
            "⚠️ 这份 .TRACE 的形式误差（σ）整列是 0，所以只有原始残差、"
            "没有归一化残差 —— 平滑解（RTS）那份就是这样。"
            "要看归一化残差得读 PPP 那一份（`NET_*.TRACE`）。"
        )
    note = (
        "纵轴是「观测值减掉模型算出来的值」。归一化残差 = 残差 ÷ 形式误差 —— "
        "它把「低仰角本来就大」这件事除掉，是唯一能跨卫星、跨频点比的量："
        "正常应该大致落在 ±1 里、近似正态。虚线是 ±1σ 和 ±3σ："
        "正态的话 99.7% 应该落在 ±3σ 以内，尾巴越长说明那一类观测越不干净。"
        "⚠️ 残差小只说明「观测和模型自洽」，不等于位置一定准。"
    )
    # ⚠️ 归一化残差直方图中间那根**尖峰**要说一句：实测相位那组里约 2%
    #    的后验残差**恰好是 0**（不是四舍五入）。成因没查（推测是"定义该卫星
    #    模糊度的那一条观测"，**这只是推测**）—— 但不说的话，那根尖峰会
    #    被读成"残差特别好"。
    zeros = [(d.get("label", k), d.get("frac_near_zero", 0.0))
             for k, d in kinds.items() if d.get("frac_near_zero", 0) > 0.005]
    if zeros:
        who = "、".join(f"{lab} {frac * 100:.1f}%" for lab, frac in zeros)
        note += (f"（{who} 的残差**恰好是 0** —— 直方图中间那根尖峰就是它。"
                 "成因我们没查；别把那根尖峰读成「残差特别好」。）")
        note = note.replace("**", "")
    return note
