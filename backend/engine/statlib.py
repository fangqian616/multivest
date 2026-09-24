#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检验统计所需的基础分布与协方差工具（不依赖 scipy）。

为什么自己写
------------
本项目的运行时要打进单文件 exe，多一个 scipy 会显著增大体积；
而这里需要的只是**四个函数**：χ² 上尾概率、标准正态 CDF 与其反函数、
以及 Newey-West 异方差自相关稳健标准误。它们都有稳定且短的数值实现，
自己写比引入依赖划算。

参考实现
--------
· 正则化不完全 Gamma 函数：Numerical Recipes, 3rd ed., §6.2（级数 + 连分式）
· 正态分位数：Acklam, P.J. (2003). An Algorithm for Computing the Inverse
  Normal Cumulative Distribution Function. 相对误差 < 1.15e-9
"""
from __future__ import annotations

import math

import numpy as np

_EPS = 1e-300
_FPMIN = 1e-300
_ITMAX = 300


# ─────────────────────────────────────────────────────────────────────────────
# 正则化不完全 Gamma 函数 —— χ² 检验的 p 值来源
# ─────────────────────────────────────────────────────────────────────────────

def _gser(a: float, x: float) -> float:
    """P(a, x) 的级数展开（x < a+1 时收敛快）。"""
    if x <= 0.0:
        return 0.0
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(_ITMAX):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-15:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    """Q(a, x) 的连分式展开（x >= a+1 时收敛快）。"""
    b = x + 1.0 - a
    c = 1.0 / _FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, _ITMAX + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = b + an / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(x: float, df: int) -> float:
    """χ² 分布的上尾概率 P(X > x)。"""
    if x <= 0:
        return 1.0
    a, xx = df / 2.0, x / 2.0
    if xx < a + 1.0:
        return 1.0 - _gser(a, xx)
    return _gcf(a, xx)


# ─────────────────────────────────────────────────────────────────────────────
# 标准正态
# ─────────────────────────────────────────────────────────────────────────────

def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 2003 的有理逼近）。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p 必须落在 (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ─────────────────────────────────────────────────────────────────────────────
# 稳健标准误
# ─────────────────────────────────────────────────────────────────────────────

def newey_west_se(x: np.ndarray, resid: np.ndarray, lags: int | None = None) -> np.ndarray:
    """OLS 系数的 Newey-West (1987) HAC 标准误。

    为什么必须用 HAC：波动率与收益的回归残差同时存在异方差与自相关，
    普通的 OLS 标准误会**系统性低估**，从而把噪声当成显著。
    存在重叠窗口（本项目 H=20）时这个问题尤其严重。

    滞后阶数默认按 Newey-West 经验法则取 floor(4·(T/100)^(2/9))。

    参考：Newey, W. & West, K. (1987). A Simple, Positive Semi-Definite,
    Heteroskedasticity and Autocorrelation Consistent Covariance Matrix.
    Econometrica 55(3), 703-708.
    """
    x = np.asarray(x, float)
    resid = np.asarray(resid, float)
    T, k = x.shape
    if lags is None:
        lags = int(math.floor(4 * (T / 100.0) ** (2.0 / 9.0)))
    xtx_inv = np.linalg.pinv(x.T @ x)
    S = (x * resid[:, None]).T @ (x * resid[:, None])       # lag 0
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)                        # Bartlett 核
        g = (x[lag:] * resid[lag:, None]).T @ (x[:-lag] * resid[:-lag, None])
        S += w * (g + g.T)
    V = xtx_inv @ S @ xtx_inv
    return np.sqrt(np.clip(np.diag(V), 0.0, None))


def ols_hac(y: np.ndarray, X: np.ndarray, lags: int | None = None) -> dict:
    """带 HAC 标准误的 OLS，返回系数、t 值、R² 与 p 值。"""
    y = np.asarray(y, float)
    X = np.asarray(X, float)
    X1 = np.hstack([X, np.ones((len(X), 1))])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    resid = y - X1 @ beta
    se = newey_west_se(X1, resid, lags)
    dof = max(1, len(y) - X1.shape[1])
    sigma2 = float(resid @ resid) / dof
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else 0.0
    tvals = beta / np.where(se > 0, se, np.inf)
    pvals = [2.0 * (1.0 - norm_cdf(abs(float(t)))) for t in tvals]
    return {"beta": beta.tolist(), "se": se.tolist(), "t": tvals.tolist(),
            "p": pvals, "r2": r2, "sigma2": sigma2, "n": int(len(y)),
            "lags": int(lags if lags is not None
                         else math.floor(4 * (len(y) / 100.0) ** (2.0 / 9.0)))}


# ─────────────────────────────────────────────────────────────────────────────
# 分块自助
# ─────────────────────────────────────────────────────────────────────────────

def block_indices(n: int, block: int, reps: int, rng: np.random.Generator) -> list:
    """稳态/循环分块自助的索引集合。

    重叠的 H 日窗口使相邻观测强相关，逐点自助会严重低估不确定性；
    按不小于 H 的块重采样可以保留这种相关结构。

    **循环**（circular）而非简单截断：块越过末尾时回绕到开头，
    避免末段观测被系统性地少抽到。
    """
    nblk = int(math.ceil(n / block))
    out = []
    for _ in range(reps):
        starts = rng.integers(0, n, nblk)
        idx = np.concatenate([(np.arange(s, s + block) % n) for s in starts])
        out.append(idx[:n])
    return out
