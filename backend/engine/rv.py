#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已实现波动率的**日内极差估计量**与 HAR 家族的回归设计矩阵。

为什么需要这个模块
------------------
原实现把「日收益率的平方 r²」当作当日已实现方差。这在数据上是一种浪费：
数据集里有完整的开高低收（OHLC），而 r² 只用到了收盘价一个数，
是对波动率**噪声最大**的估计量之一。

极差类估计量（range-based estimators）利用当日最高价与最低价所携带的
日内波动信息，在同样的数据量下把估计效率提高数倍。文献里的效率比
（相对 r²，同方差情形）：

    Parkinson (1980)         ≈ 4.9
    Garman-Klass (1980)      ≈ 7.4
    Rogers-Satchell (1991)   ≈ 6.0（且对漂移免疫）
    Yang-Zhang (2000)        ≈ 14（同时处理隔夜跳空与漂移）

参考
----
· Parkinson, M. (1980). The Extreme Value Method for Estimating the Variance
  of the Rate of Return. Journal of Business 53(1), 61-65.
· Garman, M. & Klass, M. (1980). On the Estimation of Security Price
  Volatilities from Historical Data. Journal of Business 53(1), 67-78.
· Rogers, L.C.G. & Satchell, S.E. (1991). Estimating Variance from High, Low
  and Closing Prices. Annals of Applied Probability 1(4), 504-512.
· Yang, D. & Zhang, Q. (2000). Drift-Independent Volatility Estimation Based on
  High, Low, Open, and Close Prices. Journal of Business 73(3), 477-491.
· Bollerslev, T., Patton, A. & Quaedvlieg, R. (2016). Exploiting the Errors:
  A Simple Approach for Improved Volatility Forecasting.
  Journal of Econometrics 192(1), 1-18.  ← HARQ

约定
----
· 所有函数接收等长的 numpy 数组，返回**日频方差**（非年化）。
· 允许出现 NaN 前导（开盘价用于第一根 K 线时无前收）。
· 不抛异常、不做 I/O，便于训练与运行时共用同一份实现。
"""
from __future__ import annotations

import math

import numpy as np

# 4·ln2，Parkinson 估计量的分母
_FOUR_LN2 = 4.0 * math.log(2.0)
# 2·ln2 − 1，Garman-Klass 的第二项系数
_TWO_LN2_M1 = 2.0 * math.log(2.0) - 1.0

WEEK = 5
MONTH = 22


# ─────────────────────────────────────────────────────────────────────────────
# 单根 K 线的估计量
# ─────────────────────────────────────────────────────────────────────────────

def parkinson(h: np.ndarray, l: np.ndarray) -> np.ndarray:
    """σ²_P = (1 / 4ln2) · (ln(H/L))²

    只用到最高价与最低价。假设价格服从无漂移的几何布朗运动、且区间内无跳空。
    优点是最稳健、对坏数据不敏感；缺点是忽略了隔夜跳空与开盘跳空。
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        rng = np.log(np.asarray(h, float) / np.asarray(l, float))
    return rng ** 2 / _FOUR_LN2


def garman_klass(o: np.ndarray, h: np.ndarray, l: np.ndarray,
                 c: np.ndarray) -> np.ndarray:
    """σ²_GK = 0.5·(ln(H/L))² − (2ln2 − 1)·(ln(C/O))²

    在 Parkinson 的基础上加入开盘价与收盘价，效率更高；
    代价是需要开盘价可靠（指数与 ETF 的开盘价在部分行情源里质量一般）。
    注意：单根 K 线的取值**可能为负**，需在窗口上平均后再使用。
    """
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.log(h / l)
        co = np.log(c / o)
    return 0.5 * hl ** 2 - _TWO_LN2_M1 * co ** 2


def rogers_satchell(o: np.ndarray, h: np.ndarray, l: np.ndarray,
                    c: np.ndarray) -> np.ndarray:
    """σ²_RS = ln(H/C)·ln(H/O) + ln(L/C)·ln(L/O)

    对**任意漂移**都无偏 —— 这是它相对前两者的主要优势。
    单根取值同样可能为负，须在窗口上平均。
    """
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)


def yang_zhang(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
               window: int = MONTH) -> np.ndarray:
    """σ²_YZ —— 同时处理隔夜跳空、开盘跳空与漂移的估计量。

        σ²_YZ = σ²_overnight + k·σ²_open_to_close + (1 − k)·σ²_RS
        k     = 0.34 / (1.34 + (n + 1) / (n − 1))

    其中：
        σ²_overnight    = 隔夜对数收益 ln(O_t / C_{t−1}) 的样本方差
        σ²_open_to_close= 日内对数收益 ln(C_t / O_t) 的样本方差
        σ²_RS           = Rogers-Satchell 在窗口内的均值

    这是四个估计量里最接近「真实日内方差」的一个，本项目以它作为主代理变量。
    窗口长度 n 默认 22（一个月），与 HAR 的月度尺度一致。
    """
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    n_obs = len(c)
    out = np.full(n_obs, np.nan)
    if n_obs < window + 1:
        return out

    with np.errstate(divide="ignore", invalid="ignore"):
        prev_c = np.concatenate([[np.nan], c[:-1]])
        log_oc = np.log(c / o)                     # 日内
        log_co = np.log(o / prev_c)                # 隔夜
        rs = rogers_satchell(o, h, l, c)

    k = 0.34 / (1.34 + (window + 1) / (window - 1))

    for i in range(window, n_obs):
        oc = log_oc[i - window + 1:i + 1]
        co = log_co[i - window + 1:i + 1]
        rs_w = rs[i - window + 1:i + 1]
        if np.isnan(oc).any() or np.isnan(co).any() or np.isnan(rs_w).any():
            continue
        # ddof=1 的样本方差；隔夜与日内各自去均值
        var_oc = float(np.var(oc, ddof=1)) if window > 1 else 0.0
        var_co = float(np.var(co, ddof=1)) if window > 1 else 0.0
        var_rs = float(np.mean(rs_w))
        yz = var_co + k * var_oc + (1.0 - k) * var_rs
        # RS 项在样本内可能给出很小的负贡献，兜到 0 上避免开方出 NaN
        out[i] = max(yz, 0.0)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 连续成分 / 跳跃成分的分离
# ─────────────────────────────────────────────────────────────────────────────

def decompose_jump(close_ret: np.ndarray, cont: np.ndarray) -> tuple:
    """把「收益平方」拆成连续成分与跳跃成分。

        J_t = max(r_t² − C_t, 0)
        C_t = min(r_t², C_t)

    为什么可以这样拆：极差类估计量在**没有跳空**时给出的是扩散方差的无偏估计，
    而当日内出现跳跃时，r_t² 会被跳跃放大、极差估计量则相对保守。
    两者之差因而可以当作跳跃的保守代理。

    这是对 Barndorff-Nielsen & Shephard (2004) 双幂变差分解的**日频近似** ——
    真正的双幂变差需要日内高频数据，本数据集只有日频 OHLC，
    因此这里明确标注为近似，不宣称等价。

    参考：Barndorff-Nielsen, O. & Shephard, N. (2004). Power and Bipower
    Variation with Stochastic Volatility and Jumps. Journal of Financial
    Econometrics 2(1), 1-37.
    """
    r2 = np.asarray(close_ret, float) ** 2
    c = np.asarray(cont, float)
    jump = np.maximum(r2 - c, 0.0)
    conti = np.minimum(r2, c)
    return conti, jump


def quarticity_proxy(rv: np.ndarray) -> np.ndarray:
    """已实现四次变差的日频代理 RQ_t ≈ RV_t²。

    推导：若日内收益独立同分布 r_i ~ N(0, σ²/n)，则
        RQ = (n/3)·Σ r_i⁴ ，而 E[r_i⁴] = 3σ⁴/n²
        ⇒ E[RQ] = (n/3)·n·3σ⁴/n² = σ⁴
    而 RV = Σ r_i² 的期望正是 σ²，故 RQ ≈ RV²（高斯情形）。

    这是 Bollerslev, Patton & Quaedvlieg (2016) 的 HARQ 项所依赖的量。
    本数据集没有日内数据，无法直接算四次变差，因此采用上述高斯近似，
    并在文档中如实说明它是近似而非估计。
    """
    rv = np.asarray(rv, float)
    return np.maximum(rv, 0.0) ** 2


def measurement_error_proxy(parkinson_rv: np.ndarray,
                            gk_rv: np.ndarray) -> np.ndarray:
    """HARQ 交互项中「测量误差」的日频代理：**跨估计量离散度**。

    为什么不直接用 RQ。Bollerslev-Patton-Quaedvlieg (2016) 的 HARQ 依赖
    `RQ_t^{1/2}` 作为"当日波动测量误差"的代理。但在**日频 OHLC** 数据上
    （每天只有一个收益观测，M = 1）：

        RQ = (M/3)·Σ r_i⁴  →  r⁴/3

    即退化成 RV 的确定性函数。此时交互项 RQ^{1/2}·logRV 就变成了
    logRV 的二次项，模型不再是"按测量误差修正日系数"，而只是一个
    二次型增项 —— 这与原文的机制不是一回事。**上面的 quarticity_proxy()
    正是这个退化版本，保留它仅用于对照。**

    本函数改用另一个可得的信息来源：两个**假设不同、效率不同**的极差估计量
    之间的分歧。Parkinson 只用高低价、Garman-Klass 额外用开收盘价；
    两者在测量误差小时应当接近，误差大时分歧上升。取相对离散度：

        ME_t = |P_t − GK_t| / ((P_t + GK_t)/2)

    这是一个**无量纲**的量，与 RV 的水平解耦，因此交互项不会退化成
    自变量的确定性函数。

    诚实标注：这是本项目在日频数据约束下对 HARQ 的**适配**，
    不是原文的 RQ^{1/2}。文档与界面均按"适配"表述，不宣称等价。
    """
    p = np.asarray(parkinson_rv, float)
    g = np.asarray(gk_rv, float)
    denom = np.where((p + g) > 0, (p + g) / 2.0, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.abs(p - g) / denom
    return np.where(np.isfinite(out), out, 0.0)


def back_transform(log_var: np.ndarray, resid_var: float = 0.0) -> np.ndarray:
    """对数方差预测 → 方差水平，并带对数正态的偏差修正。

    若 log RV = Xβ + u，u ~ N(0, σ²_u)，则

        E[RV | X] = exp(Xβ + σ²_u/2)

    漏掉 `σ²_u/2` 会**系统性低估**预测的方差水平，从而让所有 QLIKE
    数值整体偏移。它不改变模型之间的排序（所有候选受同样的偏移），
    但会让报告的绝对水平失真 —— 因此每次从对数空间还原都应带上这一项。

    resid_var 传训练残差的方差的**无偏估计**；缺省 0 表示不做修正
    （仅在对照实验中按需使用）。
    """
    lv = np.asarray(log_var, float)
    return np.exp(np.clip(lv + resid_var / 2.0, -60.0, 60.0))


# ─────────────────────────────────────────────────────────────────────────────
# HAR 家族的设计矩阵
# ─────────────────────────────────────────────────────────────────────────────

def har_components(rv: np.ndarray, week: int = WEEK,
                   month: int = MONTH) -> tuple:
    """由日频方差序列构造 HAR 的三个尺度：日 / 周 / 月。

    Corsi (2009) 的核心洞察是：不同市场参与者（日内、周度、月度）
    对波动的反应速度不同，因此把三个尺度的平均放在一起就能逼近长记忆。
        RV_d = RV_t
        RV_w = mean(RV_{t−4..t})
        RV_m = mean(RV_{t−21..t})

    参考：Corsi, F. (2009). A Simple Approximate Long-Memory Model of
    Realized Volatility. Journal of Financial Econometrics 7(2), 174-196.
    """
    rv = np.asarray(rv, float)
    n = len(rv)
    d = rv.copy()
    w = np.full(n, np.nan)
    m = np.full(n, np.nan)
    for i in range(n):
        if i + 1 >= week:
            seg = rv[i - week + 1:i + 1]
            if not np.isnan(seg).any():
                w[i] = float(np.mean(seg))
        if i + 1 >= month:
            seg = rv[i - month + 1:i + 1]
            if not np.isnan(seg).any():
                m[i] = float(np.mean(seg))
    return d, w, m


def harq_interaction(rv_d: np.ndarray, rq: np.ndarray) -> np.ndarray:
    """HARQ 的交互项：RQ_t^{1/2} · log(RV_d,t)。

    Bollerslev, Patton & Quaedvlieg (2016) 的出发点是：HAR 的日系数 β_d
    其实不是常数 —— 当日的 RQ 越大（测量误差越大），β_d 越向 0 收缩。
    把这个随时间的收缩写进模型，即得 HARQ：

        log RV_{t+1} = β0 + (β_d + β_dQ · RQ_t^{1/2}) · log RV_d,t
                            + β_w · log RV_w,t + β_m · log RV_m,t + ε

    RQ^{1/2} 的量纲与 RV 相同但分布右偏严重，这里**除以其样本均值**做标准化，
    使交互项的系数可解释为「相对于平均测量误差水平的额外斜率」。
    """
    rv_d = np.asarray(rv_d, float)
    rq = np.asarray(rq, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.nanmean(np.sqrt(rq))
        rq_half = np.sqrt(rq) / (scale if scale and scale > 0 else 1.0)
        return rq_half * np.log(np.clip(rv_d, 1e-12, None))


def har_design(rv: np.ndarray, week: int = WEEK, month: int = MONTH,
               rq: np.ndarray | None = None,
               cont: np.ndarray | None = None,
               jump: np.ndarray | None = None) -> dict:
    """构造 HAR 家族的设计矩阵（在对数方差空间）。

    返回 dict，含若干命名的自变量矩阵，便于在同一个前向滚动框架里
    并列比较，而不是各写一套验证代码：

        har    : [log RV_d, log RV_w, log RV_m]                 —— 基准
        harq   : har + [RQ^{1/2}·log RV_d]                      —— 测量误差修正
        har_cj : [log C_d, log J_d, log RV_w, log RV_m]         —— 连续/跳跃分离
        har_x  : har + 外部特征（由调用方拼接）

    自变量一律取对数：方差水平右偏严重且异方差，直接回归会被少数极端值主导
    （本项目早期实测会出现 QLIKE 2000+ 的荒谬值，因为模型吐出接近 0 的方差）。
    取对数既让分布接近对称，又天然保证预测为正。
    """
    rv = np.asarray(rv, float)
    d, w, m = har_components(rv, week, month)
    with np.errstate(divide="ignore", invalid="ignore"):
        ld = np.log(np.clip(d, 1e-12, None))
        lw = np.log(np.clip(w, 1e-12, None))
        lm = np.log(np.clip(m, 1e-12, None))
    base = np.column_stack([ld, lw, lm])

    out = {"har": base, "names": {"har": ["logRV_d", "logRV_w", "logRV_m"]}}

    if rq is not None:
        inter = harq_interaction(d, rq)
        out["harq"] = np.column_stack([base, inter])
        out["names"]["harq"] = ["logRV_d", "logRV_w", "logRV_m", "RQ^½·logRV_d"]

    if cont is not None and jump is not None:
        cont = np.asarray(cont, float)
        jump = np.asarray(jump, float)
        with np.errstate(divide="ignore", invalid="ignore"):
            lc = np.log(np.clip(cont, 1e-12, None))
            lj = np.log(np.clip(jump, 1e-12, None))
        # 跳跃项可能整日为 0（NRV 认为无跳），log 后为极小值，信息仍在
        out["har_cj"] = np.column_stack([lc, lj, lw, lm])
        out["names"]["har_cj"] = ["logC_d", "logJ_d", "logRV_w", "logRV_m"]

    return out


# ─────────────────────────────────────────────────────────────────────────────
# 从 OHLC 直接得到主代理变量
# ─────────────────────────────────────────────────────────────────────────────

def realized_variance(o, h, l, c, window: int = MONTH,
                      estimator: str = "yang_zhang") -> np.ndarray:
    """按指定估计量给出日频已实现方差序列（年化前的原始值）。

    estimator 取 "yang_zhang" / "garman_klass" / "rogers_satchell" /
    "parkinson" / "close_squared"（后者即原实现，保留用于对照）。
    """
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    if estimator == "yang_zhang":
        return yang_zhang(o, h, l, c, window)
    if estimator == "garman_klass":
        gk = garman_klass(o, h, l, c)
        return _rolling_mean(gk, window)
    if estimator == "rogers_satchell":
        rs = rogers_satchell(o, h, l, c)
        return _rolling_mean(rs, window)
    if estimator == "parkinson":
        return _rolling_mean(parkinson(h, l), window)
    if estimator == "close_squared":
        with np.errstate(divide="ignore", invalid="ignore"):
            prev = np.concatenate([[np.nan], c[:-1]])
            r = np.log(c / prev)
        return r ** 2
    raise ValueError(f"未知估计量：{estimator}")


def _rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    """单根 K 线取值可能为负的估计量（GK / RS）需要先在窗口上平均。"""
    x = np.asarray(x, float)
    n = len(x)
    out = np.full(n, np.nan)
    for i in range(window - 1, n):
        seg = x[i - window + 1:i + 1]
        if np.isnan(seg).any():
            continue
        out[i] = max(float(np.mean(seg)), 0.0)
    return out


ESTIMATORS = ("yang_zhang", "garman_klass", "rogers_satchell",
              "parkinson", "close_squared")

ESTIMATOR_LABELS = {
    "yang_zhang": "Yang-Zhang（隔夜+日内+RS）",
    "garman_klass": "Garman-Klass",
    "rogers_satchell": "Rogers-Satchell（抗漂移）",
    "parkinson": "Parkinson（高低）",
    "close_squared": "收盘价平方 r²（原实现）",
}

# 文献中的相对效率（同方差、无跳空情形下，方差估计量相对 r² 的效率倍数）
EFFICIENCY = {
    "close_squared": 1.0,
    "parkinson": 4.9,
    "garman_klass": 7.4,
    "rogers_satchell": 6.0,
    "yang_zhang": 14.0,
}
