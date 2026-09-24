#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合构建与横截面统计：收缩协方差、层次风险平价、溢出一致性。

这三个方法各自解决本项目一个**已经写在「已知限制」里**的问题：

| 已知限制 | 对应的解决方案 |
|---|---|
| 「类内配置是等权，不是最优化组合」 | 层次风险平价 HRP（López de Prado 2016） |
| 22 个标的、样本期短 → 样本协方差矩阵病态 | Ledoit-Wolf 收缩（2004） |
| 相关性热力图只说明「同涨同跌」，不含方向 | Diebold-Yilmaz 溢出一致性（2012/2014） |

参考
----
· Ledoit, O. & Wolf, M. (2004). Honey, I Shrunk the Sample Covariance Matrix.
  Journal of Portfolio Management 30(4), 110-119.
· Ledoit, O. & Wolf, M. (2020). Analytical Nonlinear Shrinkage of Large
  Covariance Matrices. Annals of Statistics 48(5), 3043-3065.
· López de Prado, M. (2016). Building Diversified Portfolios that Outperform
  Out-of-Sample. Journal of Portfolio Management 42(4), 59-69.
· Diebold, F.X. & Yilmaz, K. (2012). Better to Give than to Receive:
  Predictive Directional Measurement of Volatility Spillovers.
  International Journal of Forecasting 28(1), 57-66.
· Diebold, F.X. & Yilmaz, K. (2014). On the Network Topology of Variance
  Decompositions. Journal of Econometrics 182(1), 119-134.
"""
from __future__ import annotations

import math

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# 收缩协方差
# ─────────────────────────────────────────────────────────────────────────────

def ledoit_wolf(X: np.ndarray, target: str = "constant_correlation") -> dict:
    """Ledoit-Wolf (2004) 收缩协方差矩阵。

    问题：22 个标的、有效样本有限时，样本协方差矩阵 S 的特征值谱被严重拉伸
    —— 最小的特征值趋近 0（求逆后爆炸），最大的被高估。
    等权组合因此常常在样本外打败"最优"组合（DeMiguel et al. 2009 的著名结论）。

    收缩的做法是把 S 朝一个**结构简单、条件数好**的目标矩阵 F 拉：
        Σ̂ = δ·F + (1 − δ)·S
    收缩强度 δ 由数据决定（使期望平方误差最小），不是拍出来的超参数。

    target="constant_correlation"（默认）：
        F_ii = s_ii
        F_ij = r̄·sqrt(s_ii·s_jj)，其中 r̄ 为所有两两相关系数的均值
    即"保留各自的方差，但把相关性一律拉平到平均相关"。

    返回含收缩强度 delta 与目标矩阵名，便于在界面上解释"为什么权重变了"。
    """
    X = np.asarray(X, float)
    T, N = X.shape
    if T < 2 or N < 2:
        return {"ok": False, "reason": "样本不足"}
    Xc = X - X.mean(axis=0)
    S = (Xc.T @ Xc) / T                       # 用 MLE 版本，与 Ledoit-Wolf 原文一致
    var = np.diag(S).copy()
    sd = np.sqrt(np.clip(var, 1e-18, None))
    corr = S / np.outer(sd, sd)
    np.fill_diagonal(corr, 1.0)

    if target == "identity":
        F = np.diag(var)
    else:
        off = corr[~np.eye(N, dtype=bool)]
        rbar = float(off.mean()) if off.size else 0.0
        F = rbar * np.outer(sd, sd)
        np.fill_diagonal(F, var)

    # π̂：样本协方差元素的渐近方差之和
    X2 = Xc ** 2
    pi_mat = (X2.T @ X2) / T - S ** 2
    pi_hat = float(pi_mat.sum())

    # ρ̂：目标矩阵与样本协方差之间的协方差（constant-correlation 目标的完整式）
    if target == "identity":
        rho_hat = float(np.diag(pi_mat).sum())
    else:
        rho_hat = float(np.diag(pi_mat).sum())
        for i in range(N):
            for j in range(N):
                if i == j:
                    continue
                term = ((sd[j] / sd[i]) * ((X2[:, i] * (Xc[:, i] * Xc[:, j])).mean()
                                           - var[i] * S[i, j])
                        + (sd[i] / sd[j]) * ((X2[:, j] * (Xc[:, i] * Xc[:, j])).mean()
                                             - var[j] * S[i, j]))
                rho_hat += (rbar / 2.0) * term

    gamma_hat = float(((F - S) ** 2).sum())
    if gamma_hat <= 1e-18:
        delta = 0.0
    else:
        kappa = (pi_hat - rho_hat) / gamma_hat
        delta = max(0.0, min(1.0, kappa / T))
    Sigma = delta * F + (1.0 - delta) * S

    # 条件数：收缩是否真的改善了矩阵求逆的稳定性
    def _cond(M):
        w = np.linalg.eigvalsh((M + M.T) / 2)
        w = w[w > 1e-16]
        return float(w.max() / w.min()) if w.size else float("inf")

    return {"ok": True, "sigma": Sigma, "delta": round(delta, 4),
            "target": target, "n": int(N), "T": int(T),
            "cond_sample": round(_cond(S), 2), "cond_shrunk": round(_cond(Sigma), 2),
            "rbar": round(float(corr[~np.eye(N, dtype=bool)].mean()), 4)
            if target != "identity" else None}


# ─────────────────────────────────────────────────────────────────────────────
# 层次风险平价
# ─────────────────────────────────────────────────────────────────────────────

def _corr_distance(corr: np.ndarray) -> np.ndarray:
    """相关系数 → 距离：d_ij = sqrt(0.5·(1 − ρ_ij))。

    这是把相关系数转成**度量空间**的标准做法（López de Prado 2016 §2.1）：
    ρ=1 → d=0，ρ=0 → d=0.707，ρ=−1 → d=1。满足三角不等式。
    """
    c = np.clip(np.asarray(corr, float), -1.0, 1.0)
    return np.sqrt(np.maximum(0.5 * (1.0 - c), 0.0))


def _single_linkage_order(D: np.ndarray) -> list:
    """单链接层次聚类的**准对角化顺序**。

    不建树对象，直接用最近邻链：每轮合并距离最小的一对簇，
    用「两簇间最小距离」作为簇间距离（single linkage），
    最后按合并顺序展开叶节点。返回的排列让高相关的标的彼此相邻。
    """
    n = D.shape[0]
    clusters = [[i] for i in range(n)]
    dist = D.copy().astype(float)
    np.fill_diagonal(dist, np.inf)
    while len(clusters) > 1:
        k = len(clusters)
        best, bi, bj = np.inf, 0, 1
        for i in range(k):
            for j in range(i + 1, k):
                if dist[i, j] < best:
                    best, bi, bj = dist[i, j], i, j
        merged = clusters[bi] + clusters[bj]
        # 新簇与其余簇的距离 = 组内最小距离
        newrow = np.minimum(dist[bi], dist[bj])
        keep = [x for x in range(k) if x not in (bi, bj)]
        clusters = [clusters[x] for x in keep] + [merged]
        sub = dist[np.ix_(keep, keep)]
        dist = np.full((len(clusters), len(clusters)), np.inf)
        dist[:len(keep), :len(keep)] = sub
        dist[len(keep), :len(keep)] = newrow[keep]
        dist[:len(keep), len(keep)] = newrow[keep]
    return list(clusters[0])


def _cluster_var(cov: np.ndarray, idx: list) -> float:
    """簇方差：用**逆方差权重**加权后的组合方差。

    w_i ∝ 1/σ²_ii，再归一化 —— 这与 HRP 递归中使用的权重一致，
    保证"子簇方差"与最终分配口径统一。
    """
    sub = cov[np.ix_(idx, idx)]
    ivp = 1.0 / np.clip(np.diag(sub), 1e-18, None)
    w = ivp / ivp.sum()
    return float(w @ sub @ w)


def hrp_weights(cov: np.ndarray) -> dict:
    """层次风险平价（López de Prado 2016）的完整三步。

    1. 由协方差导出相关矩阵，转成距离矩阵
    2. 单链接层次聚类 → 准对角化顺序（把相似的标的排到一起）
    3. 沿该顺序递归二分：每一步按两个子簇的方差反比分配权重

    相对均值-方差最优化的优势在于**不需要求协方差的逆** ——
    逆矩阵正是样本外表现崩溃的根源。
    """
    cov = np.asarray(cov, float)
    n = cov.shape[0]
    if n < 2:
        return {"ok": False, "reason": "标的不满 2 个"}
    sd = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    corr = cov / np.outer(sd, sd)
    np.fill_diagonal(corr, 1.0)
    D = _corr_distance(corr)
    order = _single_linkage_order(D)

    w = np.ones(n)
    stack = [order]
    while stack:
        idx = stack.pop()
        if len(idx) < 2:
            continue
        half = len(idx) // 2
        left, right = idx[:half], idx[half:]
        v1 = _cluster_var(cov, left)
        v2 = _cluster_var(cov, right)
        alpha = 1.0 - v1 / (v1 + v2) if (v1 + v2) > 0 else 0.5
        for i in left:
            w[i] *= alpha
        for i in right:
            w[i] *= (1.0 - alpha)
        stack.append(left)
        stack.append(right)

    w = w / w.sum()
    # 有效分散度：1/Σw² （等权时为 n，完全集中时为 1）
    enb = 1.0 / float((w ** 2).sum())
    return {"ok": True, "weights": w.tolist(), "order": order,
            "quasi_diag": [int(i) for i in order],
            "effective_n": round(enb, 3), "n": int(n)}


def compare_allocations(cov: np.ndarray, mu: np.ndarray | None = None,
                        target_vol: float = 0.12) -> dict:
    """把等权、最小方差、风险平价、HRP 放在一起比。

    比较口径是**风险贡献分散度**与**样本外不易崩溃的构造方式**，
    而不是"哪个预期收益高" —— 后者需要收益预测，而本项目不提供。
    """
    cov = np.asarray(cov, float)
    n = cov.shape[0]
    out = {}
    ones = np.ones(n)

    out["等权"] = (ones / n).tolist()

    try:
        inv = np.linalg.pinv(cov)
        w = inv @ ones
        w = w / w.sum()
        out["最小方差"] = np.clip(w, 0, None).tolist()
    except np.linalg.LinAlgError:
        pass

    try:
        ivp = 1.0 / np.clip(np.diag(cov), 1e-18, None)
        out["逆方差（风险平价近似）"] = (ivp / ivp.sum()).tolist()
    except Exception:  # noqa: BLE001
        pass

    h = hrp_weights(cov)
    if h.get("ok"):
        out["层次风险平价 HRP"] = h["weights"]

    rows = []
    for name, w in out.items():
        w = np.asarray(w, float)
        port_var = float(w @ cov @ w)
        mrc = cov @ w
        rc = w * mrc / port_var if port_var > 0 else np.zeros(n)
        rows.append({
            "label": name,
            "ann_vol": round(math.sqrt(max(port_var, 0.0) * 244.0), 4),
            "effective_n": round(1.0 / float((w ** 2).sum()), 3),
            # 风险贡献的集中度：越接近 1/n 越分散
            "risk_concentration": round(float((rc ** 2).sum()), 4),
            "max_weight": round(float(w.max()), 4),
        })
    rows.sort(key=lambda r: r["risk_concentration"])
    return {"ok": True, "rows": rows,
            "hrp_order": h.get("order"),
            "note": ("目标波动 %.0f%%；比较的是风险分散度而非预期收益 —— "
                     "本项目不提供收益预测。" % (target_vol * 100))}


# ─────────────────────────────────────────────────────────────────────────────
# Diebold-Yilmaz 溢出一致性
# ─────────────────────────────────────────────────────────────────────────────

def _var_fit(R: np.ndarray, p: int) -> tuple:
    """OLS 估计 VAR(p)，返回系数矩阵列表 [Φ_1..Φ_p] 与残差协方差。"""
    T, N = R.shape
    Y = R[p:]
    X = np.hstack([R[p - k - 1:T - k - 1] for k in range(p)])
    Yc, Xc = Y - Y.mean(0), X - X.mean(0)
    B, *_ = np.linalg.lstsq(Xc, Yc, rcond=None)
    resid = Yc - Xc @ B
    Sigma = (resid.T @ resid) / max(1, len(resid) - Xc.shape[1])
    Phi = [B[k * N:(k + 1) * N].T for k in range(p)]
    return Phi, Sigma


def _ma_coeffs(Phi: list, H: int, N: int) -> list:
    """把 VAR 的系数递推成 MA(∞) 的前 H 项：A_h = Σ_k Φ_k A_{h−k}。"""
    A = [np.eye(N)]
    for h in range(1, H + 1):
        S = np.zeros((N, N))
        for k in range(1, min(len(Phi), h) + 1):
            S += Phi[k - 1] @ A[h - k]
        A.append(S)
    return A


def connectedness(returns: np.ndarray, horizon: int = 10, lags: int = 2,
                  names: list | None = None) -> dict:
    """Diebold-Yilmaz 广义预测误差方差分解与总溢出指数。

    与相关系数矩阵的区别：相关系数是**同期、对称**的；
    溢出指数基于 VAR 的预测误差方差分解，是**有方向、可加总**的 ——
    能回答"谁在向谁传导风险"，而不只是"谁和谁一起动"。

        θ_ij(H) = σ_jj⁻¹ · Σ_{h=0}^{H−1} (e_i' A_h Σ e_j)²
                  / Σ_{h=0}^{H−1} (e_i' A_h Σ A_h' e_i)

    行归一化后，总溢出指数 S(H) = 100 · Σ_{i≠j} θ̃_ij / Σ θ̃_ij。
    S 越高，说明风险越是"系统性的"—— 这时分散化的边际效果越小。

    注意：广义分解的行和不为 1（这是它的特性，不是 bug），
    必须显式归一化，否则指数会超出 [0, 100]。
    """
    R = np.asarray(returns, float)
    R = R[~np.isnan(R).any(axis=1)]
    T, N = R.shape
    if T < lags + horizon + 20:
        return {"ok": False, "reason": f"样本不足（{T}）"}
    try:
        Phi, Sigma = _var_fit(R, lags)
    except np.linalg.LinAlgError:
        return {"ok": False, "reason": "VAR 估计失败"}
    A = _ma_coeffs(Phi, horizon - 1, N)
    sd = np.sqrt(np.clip(np.diag(Sigma), 1e-18, None))

    num = np.zeros((N, N))
    den = np.zeros(N)
    for i in range(N):
        ei = np.zeros(N); ei[i] = 1.0
        for h in range(horizon):
            for j in range(N):
                num[i, j] += (ei @ A[h] @ Sigma[:, j]) ** 2
            den[i] += ei @ A[h] @ Sigma @ A[h].T @ ei
    theta = np.zeros((N, N))
    for i in range(N):
        for j in range(N):
            theta[i, j] = num[i, j] / (sd[j] * den[i]) if den[i] > 0 else 0.0
    row = theta.sum(axis=1, keepdims=True)
    theta_n = np.divide(theta, row, out=np.zeros_like(theta), where=row > 1e-18)
    total = float(100.0 * (theta_n.sum() - np.trace(theta_n)) / N)

    to_others = (theta_n.sum(axis=1) - np.diag(theta_n)) / N * 100.0
    from_others = (theta_n.sum(axis=0) - np.diag(theta_n)) / N * 100.0
    net = to_others - from_others

    pair = []
    for i in range(N):
        for j in range(N):
            if i != j and theta_n[i, j] >= 0.10:
                pair.append({"from": (names or list(range(N)))[i],
                             "to": (names or list(range(N)))[j],
                             "value": round(float(theta_n[i, j] * 100), 2)})
    pair.sort(key=lambda r: -r["value"])

    idx = np.argsort(-net)
    return {
        "ok": True, "total_spillover": round(total, 2),
        "horizon": horizon, "lags": lags, "n": int(N), "T": int(T),
        "table": [[round(float(x * 100), 2) for x in r] for r in theta_n],
        "names": names or [str(i) for i in range(N)],
        "net": [{"name": (names or [str(i) for i in range(N)])[int(i)],
                 "net": round(float(net[i]), 2)} for i in idx],
        "top_pairs": pair[:12],
        "note": ("总溢出指数越高，风险越偏系统性，分散化的边际效果越小。"
                 "该指标基于 VAR 的预测误差方差分解，与相关系数矩阵互补。"),
    }
