#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风险模型的**可检验**部分：VaR 回测与夏普比率的过拟合校正。

设计出发点
----------
一个波动率预测说「未来 20 日波动 16.8%」，这句话对不对？
波动率本身无法直接观测，所以「预测准不准」只能靠代理变量间接判断 ——
这是本项目之前唯一的评估方式。

VaR 回测把它变成一个**可证伪的命题**：如果模型说「1% 概率的单日损失不超过
2.3%」，那么在一段样本里超过 2.3% 的天数就应该接近 1%。多了就是低估风险，
少了就是高估。这个检验不依赖任何代理变量，用的是真实发生的损益。

参考
----
· Kupiec, P. (1995). Techniques for Verifying the Accuracy of Risk Measurement
  Models. Journal of Derivatives 3(2), 73-84.                ← 无条件覆盖率
· Christoffersen, P. (1998). Evaluating Interval Forecasts.
  International Economic Review 39(4), 841-862.              ← 独立性与条件覆盖
· Basel Committee on Banking Supervision (1996). Supervisory Framework for the
  Use of "Backtesting" in Conjunction with the Internal Models Approach.
                                                             ← 三色区
· Bailey, D. & López de Prado, M. (2014). The Deflated Sharpe Ratio: Correcting
  for Selection Bias, Backtest Overfitting and Non-Normality.
  Journal of Portfolio Management 40(5), 94-107.             ← DSR
· Moreira, A. & Muir, T. (2017). Volatility-Managed Portfolios.
  Journal of Finance 72(4), 1611-1644.                       ← 波动率管理回归
"""
from __future__ import annotations

import math

import numpy as np

from engine.statlib import block_indices, chi2_sf, norm_cdf, norm_ppf, ols_hac  # type: ignore

EULER_GAMMA = 0.5772156649015329

# Basel 三色区：在 250 个交易日的回测窗口里，1% VaR 的例外次数
BASEL_GREEN_MAX = 4
BASEL_YELLOW_MAX = 9


# ─────────────────────────────────────────────────────────────────────────────
# VaR 回测
# ─────────────────────────────────────────────────────────────────────────────

def kupiec_pof(hits: np.ndarray, p: float) -> dict:
    """Kupiec (1995) 无条件覆盖率检验（POF）。

        LR_uc = −2·ln[ (1−p)^{T−x}·p^x / (1−π̂)^{T−x}·π̂^x ]  ~  χ²(1)

    原假设：实际例外率 = 名义水平 p。
    拒绝意味着模型系统性高估或低估风险 —— 但不区分是哪一种，
    也不关心例外是否**聚集**（那是下面的独立性检验）。
    """
    hits = np.asarray(hits, float)
    T = len(hits)
    x = int(hits.sum())
    if T == 0:
        return {"ok": False, "reason": "无样本"}
    pi = x / T
    if x == 0:
        lr = -2.0 * (T * math.log(1 - p) - T * math.log(1 - pi)) if pi < 1 else 0.0
    elif x == T:
        lr = -2.0 * (T * math.log(p) - T * math.log(pi))
    else:
        ll_null = (T - x) * math.log(1 - p) + x * math.log(p)
        ll_alt = (T - x) * math.log(1 - pi) + x * math.log(pi)
        lr = -2.0 * (ll_null - ll_alt)
    lr = max(lr, 0.0)
    return {"ok": True, "n": T, "exceptions": x, "expected": round(p * T, 2),
            "rate": round(pi, 5), "nominal": p,
            "lr": round(lr, 4), "df": 1, "p_value": round(chi2_sf(lr, 1), 4),
            "verdict": ("无法拒绝（覆盖率与名义水平一致）" if chi2_sf(lr, 1) >= 0.05
                        else ("低估风险（例外过多）" if pi > p else "高估风险（例外过少）"))}


def christoffersen_independence(hits: np.ndarray) -> dict:
    """Christoffersen (1998) 独立性检验。

    例外若**成群出现**（大波动来的时候连续几天击穿），
    覆盖率可能仍然正确，但风险管理已经失效 —— 因为损失是集中发生的。
    本检验对一阶马尔可夫转移概率做似然比检验，统计量 ~ χ²(1)。
    """
    hits = np.asarray(hits, int)
    if len(hits) < 3:
        return {"ok": False, "reason": "样本过短"}
    a, b = hits[:-1], hits[1:]
    n00 = int(((a == 0) & (b == 0)).sum())
    n01 = int(((a == 0) & (b == 1)).sum())
    n10 = int(((a == 1) & (b == 0)).sum())
    n11 = int(((a == 1) & (b == 1)).sum())
    if n01 + n11 == 0 or n00 + n10 == 0:
        # 没有例外，或例外全部连续 —— 退化情形
        return {"ok": True, "n00": n00, "n01": n01, "n10": n10, "n11": n11,
                "lr": 0.0, "df": 1, "p_value": 1.0,
                "verdict": "样本内无例外，独立性不可判定"}
    pi01 = n01 / (n00 + n01)
    pi11 = n11 / (n10 + n11)
    pi = (n01 + n11) / (n00 + n01 + n10 + n11)

    def _ll(p1, p2):
        out = 0.0
        if n00:
            out += n00 * math.log(1 - p1)
        if n01:
            out += n01 * math.log(p1)
        if n10:
            out += n10 * math.log(1 - p2)
        if n11:
            out += n11 * math.log(p2)
        return out

    lr = -2.0 * (_ll(pi, pi) - _ll(pi01, pi11))
    lr = max(lr, 0.0)
    pv = chi2_sf(lr, 1)
    return {"ok": True, "n00": n00, "n01": n01, "n10": n10, "n11": n11,
            "pi01": round(pi01, 4), "pi11": round(pi11, 4),
            "lr": round(lr, 4), "df": 1, "p_value": round(pv, 4),
            "verdict": ("无法拒绝（例外不聚集）" if pv >= 0.05
                        else "例外显著聚集：波动具有聚集性，风险模型未捕捉")}


def conditional_coverage(hits: np.ndarray, p: float) -> dict:
    """条件覆盖检验：LR_cc = LR_uc + LR_ind ~ χ²(2)。"""
    uc = kupiec_pof(hits, p)
    ind = christoffersen_independence(hits)
    if not uc.get("ok") or not ind.get("ok"):
        return {"ok": False, "reason": "子检验不可用"}
    lr = uc["lr"] + ind["lr"]
    pv = chi2_sf(lr, 2)
    return {"ok": True, "lr": round(lr, 4), "df": 2, "p_value": round(pv, 4),
            "verdict": ("无法拒绝：覆盖率与独立性同时成立" if pv >= 0.05
                        else "拒绝：风险模型在覆盖率或独立性上至少有一项不成立"),
            "unconditional": uc, "independence": ind}


def basel_zone(exceptions: int, n: int) -> str:
    """Basel 三色区（按 250 个交易日、1% VaR 标定，其余样本按比例折算）。"""
    scaled = exceptions * (250.0 / n) if n else 0.0
    if scaled <= BASEL_GREEN_MAX:
        return "绿区"
    if scaled <= BASEL_YELLOW_MAX:
        return "黄区"
    return "红区"


def _student_t_ppf(p: float, df: float) -> float:
    """Student-t 分位数（df 较大时退化为正态）。

    没有 scipy，所以用**Cornish-Fisher 展开**得到分位数的初值再二分修正：
        z_t ≈ z + (z³+z)/(4·df) + (5z⁵+16z³+3z)/(96·df²) + ...
    对 df ≥ 4（本项目实际取值远大于此）精度足够。

    为什么需要它：日收益的峰度显著高于正态，用正态分位数做 1% VaR
    会**系统性低估**尾部损失。这是过滤历史模拟法与 t-GARCH 类模型
    被 Basel 体系接受的直接原因。
    """
    z = norm_ppf(p)
    if df <= 2:
        return z * 3.0
    return (z + (z ** 3 + z) / (4.0 * df)
            + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96.0 * df ** 2)
            + (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / (384.0 * df ** 3))


def estimate_t_df(returns: np.ndarray, lo: float = 3.0, hi: float = 60.0) -> float:
    """由超额峰度反解 Student-t 的自由度。

        df = 6/κ + 4  （κ 为超额峰度；正态时 κ=0，df→∞）
    再夹到 [lo, hi] 避免退化。这是一个矩估计，不是 MLE ——
    对"该不该用肥尾分位数"这个判断已经足够。
    """
    r = np.asarray(returns, float)
    r = r[np.isfinite(r)]
    if len(r) < 30:
        return hi
    m, s = r.mean(), r.std(ddof=1)
    if s <= 0:
        return hi
    kurt = float((((r - m) / s) ** 4).mean())      # 非超额峰度
    excess = max(kurt - 3.0, 1e-6)
    return float(min(max(6.0 / excess + 4.0, lo), hi))


def var_backtest(returns: np.ndarray, sigma: np.ndarray, level: float = 0.99,
                 window: int = 20, seed: int = 17,
                 dist: str = "both") -> dict:
    """对一串**预测波动**做 VaR 回测。

    参数
    ----
    returns : 实际日收益（用于判定击穿）
    sigma   : 对应的**日标准差**预测（与 returns 等长，NaN 表示无预测）
    level   : VaR 置信水平，0.99 表示 1% 分位
    dist    : "normal" / "student_t" / "both"

    做法：VaR_t = −q_{1−level} · σ_t（正数表示损失阈值），
    当 −r_t > VaR_t 记为一次击穿。分别做覆盖率、独立性与条件覆盖检验。

    **同时报告正态与肥尾两种分位数**是有意为之：若正态 VaR 显著低估风险
    而 t 分位数纠正了它，那么结论就是"波动预测没错、错在分布假设"；
    若两者都低估，则问题出在波动预测本身。这决定了下一步该改哪里。
    """
    r = np.asarray(returns, float)
    s = np.asarray(sigma, float)
    ok = ~np.isnan(r) & ~np.isnan(s) & (s > 0)
    r, s = r[ok], s[ok]
    if len(r) < 60:
        return {"ok": False, "reason": f"可用样本过短（{len(r)}）"}
    p = 1.0 - level
    df = estimate_t_df(r)
    out = {"ok": True, "n": int(len(r)), "level": level, "nominal": p,
           "t_df": round(df, 2)}

    variants = []
    if dist in ("normal", "both"):
        variants.append(("正态分位数", -norm_ppf(p)))
    if dist in ("student_t", "both"):
        variants.append(("t 分位数（肥尾）", -_student_t_ppf(p, df)))

    for name, z in variants:
        var = z * s
        hits = ((-r) > var).astype(int)
        variants_out = {
            "name": name, "z": round(z, 4),
            "exceptions": int(hits.sum()), "rate": round(float(hits.mean()), 5),
            "zone": basel_zone(int(hits.sum()), len(r)),
            "unconditional": kupiec_pof(hits, p),
            "independence": christoffersen_independence(hits),
            "conditional": conditional_coverage(hits, p),
        }
        out["variants" if dist == "both" else "result"] = (
            out.get("variants", []) + [variants_out] if dist == "both" else variants_out)
    if dist == "both":
        out["variants"] = out.pop("variants")
    else:
        out.update(out.pop("result"))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 夏普比率的过拟合校正
# ─────────────────────────────────────────────────────────────────────────────

def sharpe(returns: np.ndarray, periods: int = 244) -> float:
    r = np.asarray(returns, float)
    if len(r) < 2:
        return 0.0
    sd = float(r.std(ddof=1))
    return float(r.mean() / sd * math.sqrt(periods)) if sd > 0 else 0.0


def max_drawdown(returns: np.ndarray) -> float:
    r = np.asarray(returns, float)
    eq = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1.0).min())


def deflated_sharpe(observed_sr: float, trials_sr: list, n_obs: int,
                    skew: float, kurt: float) -> dict:
    """Bailey & López de Prado (2014) 的紧缩夏普比率（DSR）。

    问题：如果试了 N 个模型，挑出夏普最高的那个，它的夏普必然被**选择偏差**
    抬高。DSR 把这件事变成一次假设检验 ——
    "观测到的夏普是否显著大于 N 次试验下纯运气能达到的最大值？"

        SR0 = sqrt(V[SR_n])·[ (1−γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ]
        DSR = Z[ (SR − SR0)·sqrt(T − 1) /
                 sqrt(1 − γ3·SR + (γ4 − 1)/4·SR²) ]

    其中 γ ≈ 0.5772 为欧拉常数，γ3/γ4 为收益的偏度与峰度（峰度为**非超额**）。
    分母是 Sharpe 估计量在非正态下的方差修正。

    返回 DSR（0~1），可读作"该夏普在多重试验下仍显著的概率"。
    """
    trials = np.asarray([t for t in trials_sr if np.isfinite(t)], float)
    N = max(2, len(trials))
    var_sr = float(trials.var(ddof=1)) if N > 1 else 0.0
    sr0 = math.sqrt(max(var_sr, 0.0)) * (
        (1 - EULER_GAMMA) * norm_ppf(1 - 1.0 / N)
        + EULER_GAMMA * norm_ppf(1 - 1.0 / (N * math.e)))
    denom = math.sqrt(max(1e-12, 1 - skew * observed_sr
                          + (kurt - 1) / 4.0 * observed_sr ** 2))
    z = (observed_sr - sr0) * math.sqrt(max(1, n_obs - 1)) / denom
    return {"ok": True, "sr": round(observed_sr, 4), "sr0": round(sr0, 5),
            "n_trials": int(N), "n_obs": int(n_obs),
            "skew": round(skew, 4), "kurt": round(kurt, 4),
            "z": round(z, 4), "dsr": round(norm_cdf(z), 4),
            "verdict": ("通过：在多重试验校正后仍然显著" if norm_cdf(z) >= 0.95
                        else "未通过：校正选择偏差后不再显著")}


def moments(r: np.ndarray) -> tuple:
    """返回 (偏度, 峰度-非超额)。"""
    r = np.asarray(r, float)
    n = len(r)
    if n < 4:
        return 0.0, 3.0
    m = r.mean()
    s = r.std(ddof=1)
    if s <= 0:
        return 0.0, 3.0
    sk = float((((r - m) / s) ** 3).sum() * n / ((n - 1) * (n - 2)))
    ku = float((((r - m) / s) ** 4).mean())
    return sk, ku


# ─────────────────────────────────────────────────────────────────────────────
# 波动率管理组合（Moreira & Muir 2017）
# ─────────────────────────────────────────────────────────────────────────────

def volatility_managed_regression(r_next: np.ndarray, r_now: np.ndarray,
                                   sigma: np.ndarray,
                                   c: float | None = None) -> dict:
    """Moreira & Muir (2017) 的波动率管理预测回归。

        r_{t+1} = α + β·(c / σ̂²_t)·r_t + ε

    若 β 显著为正，说明"在波动低的时候加仓、高的时候减仓"这个做法
    有预测回归意义上的支持；若 β 不显著，则该做法的收益来自风险调整
    而非择时能力 —— 这两种结论对应完全不同的产品叙事，
    所以必须实测而不是引用文献。

    **反向证据须知**：Cederburg, O'Doherty, Wang & Yan (2020),
    "On the Performance of Volatility-Managed Portfolios",
    *Journal of Financial Economics* 138(1), 95-117 —— 在扩展样本上未能复现
    Moreira-Muir 的结果，认为其显著性依赖特定样本期。
    另有 Barroso & Detzel (2021, JFE 140(3)) 指出交易成本会吞掉大部分收益；
    DeMiguel, Martín-Utrera & Uppal (2024, JF 79(6)) 给出调和结论：
    **条件多因子**组合的样本外、扣费后夏普提升约 +13%，
    即应当管理组合整体，而非逐资产边际管理。

    标准误用 Newey-West HAC —— 重叠窗口下普通标准误会系统性低估。
    """
    r_next = np.asarray(r_next, float)
    r_now = np.asarray(r_now, float)
    sigma = np.asarray(sigma, float)
    ok = ~np.isnan(r_next) & ~np.isnan(r_now) & ~np.isnan(sigma) & (sigma > 0)
    r_next, r_now, sigma = r_next[ok], r_now[ok], sigma[ok]
    if len(r_next) < 60:
        return {"ok": False, "reason": f"样本过短（{len(r_next)}）"}
    scale = c if c is not None else float(np.var(r_now, ddof=1))
    X = (scale / sigma ** 2 * r_now)[:, None]
    fit = ols_hac(r_next, X)
    beta, alpha = fit["beta"][0], fit["beta"][1]
    t_beta, p_beta = fit["t"][0], fit["p"][0]
    return {"ok": True, "alpha": round(alpha, 6), "beta": round(beta, 6),
            "t_beta": round(float(t_beta), 4), "p_beta": round(float(p_beta), 4),
            "r2": round(fit["r2"], 5), "n": fit["n"], "hac_lags": fit["lags"],
            "scale_c": round(scale, 8),
            "verdict": ("β 显著为正：波动率管理有预测回归支持"
                        if p_beta < 0.05 and beta > 0 else
                        ("β 显著为负：波动率管理方向相反" if p_beta < 0.05
                         else "β 不显著：波动率管理的收益无法归因于择时能力")),
            "caveat": ("反之证据：Cederburg, O'Doherty, Wang & Yan (2020, "
                       "Journal of Financial Economics 138(1)) 在扩展样本上未能复现 "
                       "Moreira-Muir 的结果；DeMiguel, Martín-Utrera & Uppal "
                       "(2024, JF 79(6)) 发现收益主要来自条件多因子组合的构造，"
                       "而非逐资产缩放。")}


def vol_target_backtest(returns: np.ndarray, sigma: np.ndarray,
                        target_vol: float, ann: float = 244.0,
                        mult_bounds: tuple = (0.5, 1.5),
                        n_trials: int = 1, trial_srs: list | None = None) -> dict:
    """波动目标化策略的回测与**诚实的**绩效统计。

    仓位乘数 m_t = clip(target_vol / σ̂_t, 下界, 上界)，策略收益 = m_{t−1}·r_t。
    报告的不只是夏普与回撤，还有：
      · 换手率（真实成本的主要来源，回测里若忽略会高估收益）
      · Deflated Sharpe（校正模型选择带来的过拟合）
      · 与买入持有基准的对比
    """
    r = np.asarray(returns, float)
    s = np.asarray(sigma, float)
    n = len(r)
    m = np.ones(n)
    for i in range(n):
        if not math.isnan(s[i]) and s[i] > 0:
            m[i] = min(max(target_vol / (s[i] * math.sqrt(ann)), mult_bounds[0]),
                       mult_bounds[1])
    ok = np.arange(1, n)
    ok = ok[~np.isnan(s[ok - 1]) & ~np.isnan(r[ok])]
    if len(ok) < 60:
        return {"ok": False, "reason": f"可用样本过短（{len(ok)}）"}
    strat = m[ok - 1] * r[ok]
    hold = r[ok]
    sk, ku = moments(strat)
    sr_s = sharpe(strat, int(ann))
    sr_h = sharpe(hold, int(ann))
    trials = trial_srs if trial_srs is not None else [sr_s] * max(1, n_trials)
    dsr = deflated_sharpe(sr_s, trials, len(strat), sk, ku)
    turn = float(np.abs(np.diff(m[ok])).mean()) if len(ok) > 1 else 0.0
    return {
        "ok": True, "n": int(len(strat)),
        "strategy": {"sharpe": round(sr_s, 4),
                     "ann_return": round(float(strat.mean()) * ann, 4),
                     "ann_vol": round(float(strat.std(ddof=1)) * math.sqrt(ann), 4),
                     "max_drawdown": round(max_drawdown(strat), 4)},
        "buy_hold": {"sharpe": round(sr_h, 4),
                     "ann_return": round(float(hold.mean()) * ann, 4),
                     "ann_vol": round(float(hold.std(ddof=1)) * math.sqrt(ann), 4),
                     "max_drawdown": round(max_drawdown(hold), 4)},
        "avg_multiplier": round(float(m[ok - 1].mean()), 4),
        "turnover": round(turn, 4),
        "deflated_sharpe": dsr,
        "note": ("回测未计入交易成本、冲击成本与融资成本；换手率作为成本代理一并给出。"
                 "夏普已做多重试验校正（DSR）。"),
    }
