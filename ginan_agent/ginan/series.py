"""Ginan 领域层 —— 时间序列的**统计**与**平滑**。

============================ 为什么要单独一个模块 ============================
这套东西有两个使用者，而它们必须**用同一份实现**：

    · 结论（`posfile.verdict` 的那些数字）
    · 图（`charts.py` 画出来的曲线、置信带、统计表）

⚠️ 两边各算一遍的话，迟早会出现"图上写着 RMS = 1.2，结论里写着 0.9" ——
   而用户没有任何办法判断哪个是对的。**一个数字只能有一个来源。**

============================ 口径是照 Ginan 抄的，不是自己定的 ============================
Ginan 自己的 `scripts/plot_pos.py:127-145` 是这么算的（逐行对过）：

    w            = 1 / σ²                  ← **加权**
    weighted_mean = Σ(w·x) / Σw
    std_dev      = sqrt( Σ(w·(x-wm)²) / Σw )   ← 也是加权的
    rms          = sqrt( mean(x²) )            ← ⚠️ **不加权**

⚠️ 最后那条是 Ginan 的原样：RMS **不加权**，而 std 加权。
   看起来不一致，但**照抄**（而不是"顺手改成一致"）—— 因为我们的数字
   要和 Ginan 自己的图对得上；改了就成了"看起来一样、其实不一样"。

⚠️ 而且这份口径本身要注明出处：**σ 用的是 .POS 文件里那一列**
   （Ginan 解算输出的形式误差），不是我们估的。
"""

from __future__ import annotations

import math

import numpy as np


def weighted_stats(x, sigma) -> dict:
    """一个分量（dN / dE / dU）的加权统计量 —— **Ginan 的口径**。

    ⚠️ σ 为 0 或负的历元会被**排除**（1/σ² 会炸成 inf）。
       实测 `.POS` 里确实有 sN=0 的行（解算没给出形式误差）。
       排除的数量要报出来 —— 悄悄少算几个历元、和"算对了"长得一样。
    """
    x = np.asarray(x, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    n_all = x.size
    if n_all == 0:
        return {"weighted_mean": float("nan"), "std_dev": float("nan"),
                "rms": float("nan"), "n": 0, "n_dropped": 0}

    ok = np.isfinite(x) & np.isfinite(sigma) & (sigma > 0)
    x, sigma = x[ok], sigma[ok]
    n_dropped = int(n_all - x.size)
    if x.size == 0:
        return {"weighted_mean": float("nan"), "std_dev": float("nan"),
                "rms": float("nan"), "n": 0, "n_dropped": n_dropped}

    w = 1.0 / sigma ** 2
    wm = float(np.sum(w * x) / np.sum(w))
    # ⚠️ 这是**加权**标准差（和 Ginan 一致），不是无偏估计
    std = float(math.sqrt(np.sum(w * (x - wm) ** 2) / np.sum(w)))
    rms = float(math.sqrt(np.mean(x ** 2)))       # ⚠️ 不加权，见文件头
    return {"weighted_mean": wm, "std_dev": std, "rms": rms,
            "n": int(x.size), "n_dropped": n_dropped}


def _bisquare(r: np.ndarray) -> np.ndarray:
    """Cleveland 的 bisquare 权重（稳健迭代用）。"""
    a = np.abs(r)
    out = (1.0 - a ** 2) ** 2
    out[a >= 1.0] = 0.0
    return out


def lowess(y, x, frac: float = 2.0 / 3.0, it: int = 3) -> np.ndarray:
    """LOWESS（Cleveland 1979 局部加权回归）—— 语义对齐 `statsmodels.lowess`。

    ============================ 为什么自己写 ============================
    Ginan 用的是 `statsmodels.nonparametric.smoothers_lowess.lowess`
    （`plot_pos.py:6`）。而 statsmodels 会拖进 scipy + pandas
    （装完一百多 MB），我们只为一条平滑曲线不值得。

    ⇒ 自己实现，但**必须和它算出同一个东西** ——
      判据不是"看着像"，是 `scripts/check_files.py` 里那条
      **对着 statsmodels 逐点比对**的守卫（开发机装了它来量，
      但**它不是本程序的依赖**）。

    ============================ 算法 ============================
    对每个 x₀（每个原始点）：
      ① 取 |x - x₀| 最近的 `k = ceil(frac·n)` 个邻居
      ② 三次核权重 `w = (1 - (d/d_max)³)³`
      ③ 做**局部加权线性回归**，在 x₀ 处取值
    然后重复 `it` 次稳健迭代：用残差算 bisquare 权重乘进 ②，
    把大偏差的点压下去（`it=0` 就是不做稳健化）。

    ⚠️ `frac` 越小越贴数据、越容易跟着噪声跑；Ginan 默认让用户传
       （`--horz-smoothing` / `--vert-smoothing`），我们取 2/3 ——
       那也是 statsmodels 自己的默认。
    """
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    n = x.size
    if n == 0:
        return y.copy()
    if n < 3:
        return y.copy()

    frac = min(max(float(frac), 1e-6), 1.0)
    # ⚠️⚠️ 邻域大小必须是 **floor**，不是 ceil —— 这是实测钉出来的约定。
    #
    #    一开始我写的是 `ceil(frac·n)`，于是和 statsmodels 差 1e-5 米。
    #    在 501 点、三种 frac 上逐个试 k 的取整方式，结论很干净：
    #
    #        frac=0.15  ceil(76) → 差 3.5e-3     floor(75) → 差 7.8e-15
    #        frac=0.5   ceil(251)→ 差 2.6e-3     floor(250)→ 差 6.8e-15
    #        frac=2/3   （两者相同）→ 差 4.4e-15
    #
    #    ⇒ **floor 时差值是浮点噪声量级**，说明算法和它一模一样。
    #      10 微米听起来无所谓，但"差一点"和"完全一样"是两种证据强度：
    #      前者只能写"接近"，后者能写"等价"。
    k = max(3, int(frac * n))
    k = min(k, n)

    yhat = np.empty(n, dtype=float)
    rob = np.ones(n, dtype=float)        # 稳健权重，第一轮全是 1

    for _ in range(max(0, int(it)) + 1):
        for i in range(n):
            d = np.abs(x - x[i])
            # ⚠️ 取第 k 近的距离要**排序后的第 k-1 个**（0 基），
            #    不是 max —— 有并列距离时 max 会把窗口放大一圈。
            idx = np.argpartition(d, k - 1)[:k]
            dd = d[idx]
            dmax = float(np.max(dd))
            if dmax <= 0:
                yhat[i] = float(np.mean(y[idx]))
                continue
            u = dd / dmax
            w = (1.0 - u ** 3) ** 3 * rob[idx]
            sw = np.sum(w)
            if sw <= 0:
                yhat[i] = float(np.mean(y[idx]))
                continue
            xi = x[idx]
            # 局部加权直线（用加权最小二乘的标准公式，避开矩阵求逆）
            xbar = np.sum(w * xi) / sw
            ybar = np.sum(w * y[idx]) / sw
            sxx = np.sum(w * (xi - xbar) ** 2)
            sxy = np.sum(w * (xi - xbar) * (y[idx] - ybar))
            if sxx <= 0:
                yhat[i] = ybar
            else:
                b = sxy / sxx
                yhat[i] = ybar + b * (x[i] - xbar)

        if it <= 0:
            break
        r = y - yhat
        m = float(np.median(np.abs(r)))
        if m <= 0:
            break
        rob = _bisquare(r / (6.0 * m))

    return yhat


def smooth_series(x, sigma=None, *, frac: float = 2.0 / 3.0,
                  it: int = 3) -> dict:
    """一次算齐「画一条曲线」需要的全部东西。

    返回：
        stats   加权统计量（见 `weighted_stats`）
        band    加权均值 ±2σ 的上下界（标量 → 画成两条横线 / 一个带）
        smooth  LOWESS 平滑后的序列（点太少时不平滑，原样返回）

    ⚠️ **历元太少就不平滑**（< 12 个）：LOWESS 在 3~5 个点上会
       画出一条比原数据更"确定"的曲线 —— 那是凭空造出来的形状。
    """
    y = np.asarray(x, dtype=float)
    st = weighted_stats(y, sigma if sigma is not None else np.ones_like(y))
    band = (st["weighted_mean"] - 2 * st["std_dev"],
            st["weighted_mean"] + 2 * st["std_dev"])
    if y.size >= 12 and np.isfinite(y).all():
        xs = np.arange(y.size, dtype=float)
        sm = lowess(y, xs, frac=frac, it=it)
    else:
        sm = y.copy()
    return {"stats": st, "band": band, "smooth": sm}
