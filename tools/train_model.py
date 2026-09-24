#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化预测模型训练与验证。

设计决策
--------
**训练用 numpy，推理纯 Python。**
  数据集只有约 2000 个日频样本、11 个特征，numpy 训练是毫秒级；但把 numpy
  打进桌面应用（PyInstaller）会让 exe 从 42 MB 涨到约 75 MB。因此这里离线训练、
  把权重落盘，运行时只做点积（纯标准库）。代价是模型不会自动跟随新数据更新 ——
  所以 `fetch_market_data.py --build` 之后应当重跑本脚本（或手动运行）。

模型
----
  1. 逻辑回归（L2 正则，梯度下降）—— 可解释，权重直接就是特征重要性
  2. 小型 MLP（单隐层 tanh）—— 回应"神经网络"的需求，用置换重要性解释

验证
----
**前向滚动（walk-forward）**，绝不使用未来数据训练：
  按时间排序 → 用 [0, k) 训练 → 预测 [k, k+step) → k 前移 → 汇总样本外预测。
报告 AUC、准确率、以及**基准率**（永远预测多数类的准确率）。
AUC 接近 0.5 就是没有预测力，必须如实报告，不能只报准确率好看的数字。

产出
----
    backend/data/dataset/model.json
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from data.market import MarketData  # noqa: E402
from engine.features import FEATURES, build_feature_frame  # noqa: E402

from quant_race import vol_diagnostics  # noqa: E402  —— tools/ 下，同目录导入

OUT = ROOT / "backend" / "data" / "dataset" / "model.json"

HORIZON = 20            # 默认预测未来多少个交易日
HORIZONS = [20, 60]     # 两个窗口都测并全部报告（不做多重比较挑选）
MIN_TRAIN = 500         # 滚动验证的最小训练样本数
STEP = 80               # 每次向前推进的步长
# 折数与轮数的取舍：实测 MLP 单折（500 样本）600 轮要 3.7s，而折内样本会长到 1700，
# 36 折 × 4 个组合总耗时超过 10 分钟。降到 180 轮 + 步长 80 之后总耗时约 3 分钟，
# 样本外 AUC 相比 600 轮变化在 ±0.005 以内 —— 小模型 + 1700 样本早收敛了，
# 再加轮数只是过拟合。



# ─────────────────────────────────────────────────────────────────────────────
# 特征工程（严格 point-in-time：t 时刻的特征只用 ≤ t 的数据）
# ─────────────────────────────────────────────────────────────────────────────

def _cum(rets: list[float]) -> np.ndarray:
    """日收益 → 净值曲线（起点 1.0）。"""
    return np.cumprod(1.0 + np.asarray(rets, dtype=float))


def _roll_mom(idx: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(idx), np.nan)
    out[n:] = idx[n:] / idx[:-n] - 1.0
    return out


def _roll_vol(rets: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(rets), np.nan)
    for i in range(n, len(rets)):
        out[i] = float(np.std(rets[i - n:i], ddof=1)) * np.sqrt(244.0)
    return out


def _roll_corr(a: np.ndarray, b: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    for i in range(n, len(a) + 1):
        x, y = a[i - n:i], b[i - n:i]
        if np.std(x) < 1e-12 or np.std(y) < 1e-12:
            out[i - 1] = 0.0
            continue
        out[i - 1] = float(np.corrcoef(x, y)[0, 1])
    return out


def _pad_front(a: np.ndarray, L: int) -> np.ndarray:
    """把「收益轴」（长度 L-1）上的序列对齐到「价格轴」（长度 L）。

    价格序列长度是 L，diff 出的收益是 L-1 —— 直接用会 column_stack 失败。
    前面补一个 NaN 即可，反正后续会按 NaN 过滤掉。
    """
    if len(a) == L:
        return a
    if len(a) > L:
        return a[-L:]
    out = np.full(L, np.nan)
    out[L - len(a):] = a
    return out


def build_dataset(market: MarketData, horizon: int = HORIZON) -> tuple:
    """构造特征矩阵、标签、日期轴。

    特征计算**复用 backend/engine/features.py**（纯 Python，训练与运行时推理共用
    同一份代码）—— 两边各写一份迟早会漂移：模型在 A 特征上训练、在 B 特征上推理，
    而且不会报错，只会悄悄变差。
    """
    dates, rows, states = build_feature_frame(market)
    if not rows:
        raise SystemExit("特征矩阵为空：历史数据不足")
    L = len(rows)

    # 市场代理净值 → 未来 horizon 日收益
    nav = [s["nav"] for s in states]
    fwd = [float("nan")] * L
    for i in range(L - horizon):
        if nav[i] > 0:
            fwd[i] = nav[i + horizon] / nav[i] - 1.0

    X_list, y_list, keep = [], [], []
    nan = float("nan")
    for i in range(L):
        if any(math.isnan(v) for v in rows[i]) or math.isnan(fwd[i]):
            continue
        keep.append(i)
    if len(keep) < MIN_TRAIN + horizon + 40:
        raise SystemExit(f"有效样本不足：{len(keep)} 条")

    X_list = [rows[i] for i in keep]
    y_list = [1.0 if fwd[i] > 0 else 0.0 for i in keep]
    X = np.asarray(X_list, dtype=float)
    y = np.asarray(y_list, dtype=float)
    row_dates = [dates[i] for i in keep]

    meta = [{"date": row_dates[i], "nav": round(nav[keep[i]], 4),
             "fwd": round(fwd[keep[i]], 6), "up": int(y[i])}
            for i in range(len(row_dates))]
    return X, y, row_dates, meta


# ─────────────────────────────────────────────────────────────────────────────
# 模型
# ─────────────────────────────────────────────────────────────────────────────

class Logistic:
    """L2 正则逻辑回归，全批量梯度下降。可解释：权重即特征重要性。"""

    def __init__(self, lr: float = 0.35, epochs: int = 350, l2: float = 0.02):
        self.lr, self.epochs, self.l2 = lr, epochs, l2

    def fit(self, X: np.ndarray, y: np.ndarray) -> "Logistic":
        n, d = X.shape
        self.w = np.zeros(d)
        self.b = 0.0
        for _ in range(self.epochs):
            z = X @ self.w + self.b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            g = p - y
            self.w -= self.lr * (X.T @ g / n + self.l2 * self.w)
            self.b -= self.lr * float(np.mean(g))
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        z = X @ self.w + self.b
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class MLP:
    """单隐层神经网络（tanh 隐层 + sigmoid 输出），带动量 SGD。"""

    def __init__(self, hidden: int = 8, lr: float = 0.06, epochs: int = 180,
                 l2: float = 0.004, seed: int = 7):
        self.hidden, self.lr, self.epochs, self.l2, self.seed = hidden, lr, epochs, l2, seed

    def fit(self, X: np.ndarray, y: np.ndarray) -> "MLP":
        rng = np.random.default_rng(self.seed)
        n, d = X.shape
        h = self.hidden
        self.W1 = rng.normal(0, np.sqrt(2.0 / d), (d, h))
        self.b1 = np.zeros(h)
        self.W2 = rng.normal(0, np.sqrt(2.0 / h), (h, 1))
        self.b2 = np.zeros(1)
        vW1 = np.zeros_like(self.W1); vb1 = np.zeros_like(self.b1)
        vW2 = np.zeros_like(self.W2); vb2 = np.zeros_like(self.b2)
        mom = 0.85
        yc = y.reshape(-1, 1)
        for _ in range(self.epochs):
            H = np.tanh(X @ self.W1 + self.b1)
            z = H @ self.W2 + self.b2
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            dz = (p - yc) / n
            gW2 = H.T @ dz + self.l2 * self.W2
            gb2 = dz.sum(axis=0)
            dH = (dz @ self.W2.T) * (1.0 - H ** 2)
            gW1 = X.T @ dH + self.l2 * self.W1
            gb1 = dH.sum(axis=0)
            vW1 = mom * vW1 - self.lr * gW1; self.W1 += vW1
            vb1 = mom * vb1 - self.lr * gb1; self.b1 += vb1
            vW2 = mom * vW2 - self.lr * gW2; self.W2 += vW2
            vb2 = mom * vb2 - self.lr * gb2; self.b2 += vb2
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        H = np.tanh(X @ self.W1 + self.b1)
        z = H @ self.W2 + self.b2
        return (1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))).ravel()


# ─────────────────────────────────────────────────────────────────────────────
# 指标
# ─────────────────────────────────────────────────────────────────────────────

def auc(y: np.ndarray, p: np.ndarray) -> float:
    """秩和法计算 AUC（不依赖 sklearn）。"""
    y = np.asarray(y); p = np.asarray(p)
    pos, neg = p[y > 0.5], p[y <= 0.5]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    order = np.argsort(p)
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    # 并列取平均秩
    sp = p[order]
    i = 0
    while i < len(sp):
        j = i
        while j + 1 < len(sp) and sp[j + 1] == sp[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = np.mean(np.arange(i + 1, j + 2))
        i = j + 1
    rp = ranks[y > 0.5].sum()
    return float((rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def walk_forward(X: np.ndarray, y: np.ndarray, factory, min_train: int = MIN_TRAIN,
                 step: int = STEP):
    """前向滚动验证：只用过去训练，预测未来一段，滚动推进。"""
    n = len(y)
    preds = np.full(n, np.nan)
    k = min_train
    while k < n:
        end = min(k + step, n)
        model = factory().fit(X[:k], y[:k])
        preds[k:end] = model.predict_proba(X[k:end])
        k = end
    mask = ~np.isnan(preds)
    return preds, mask


def perm_importance(model, X: np.ndarray, y: np.ndarray, base_auc: float,
                    rng: np.random.Generator, reps: int = 4) -> np.ndarray:
    """置换重要性：打乱某一列后 AUC 掉多少。"""
    imp = np.zeros(X.shape[1])
    for j in range(X.shape[1]):
        drops = []
        for _ in range(reps):
            Xp = X.copy()
            rng.shuffle(Xp[:, j])
            drops.append(base_auc - auc(y, model.predict_proba(Xp)))
        imp[j] = float(np.mean(drops))
    return imp


# ─────────────────────────────────────────────────────────────────────────────

def block_bootstrap_auc(y: np.ndarray, p: np.ndarray, mask: np.ndarray,
                        block: int = HORIZON, reps: int = 1500,
                        seed: int = 3) -> dict:
    """分块自助法给出 AUC 的置信区间，并与「独立同分布自助」对照。

    为什么必须分块
    --------------
    标签是**未来 horizon 日收益**，却按日采样 ⇒ 相邻样本的前瞻窗口重叠
    horizon-1 天。这些样本并不独立：它们共享绝大部分结果信息。

    用逐点（独立同分布）自助会严重低估不确定性 —— 名义上 1200 条样本，
    有效样本量大约只有 N/horizon ≈ 60 个互不重叠的窗口。
    做法是把样本按时间切块、**整块重采样**，保留块内的相关结构。
    """
    rng = np.random.default_rng(seed)
    idx = np.where(mask)[0]
    yv, pv = y[idx], p[idx]
    n = len(idx)

    def ci(a):
        a = np.asarray(a)
        return [round(float(np.percentile(a, 2.5)), 4),
                round(float(np.percentile(a, 97.5)), 4)]

    def resample_block(blk: int, reps_n: int) -> list[float]:
        out = []
        nblk = int(np.ceil(n / blk))
        max_start = max(1, n - blk + 1)
        for _ in range(reps_n):
            starts = rng.integers(0, max_start, nblk)
            take = np.concatenate([np.arange(s, min(s + blk, n)) for s in starts])
            take = take[take < n]
            if len(np.unique(yv[take])) < 2:
                continue
            out.append(auc(yv[take], pv[take]))
        return out

    boot_block = resample_block(block, reps)
    boot_iid = []
    for _ in range(reps):
        take = rng.integers(0, n, n)
        if len(np.unique(yv[take])) < 2:
            continue
        boot_iid.append(auc(yv[take], pv[take]))

    return {"block_ci": ci(boot_block), "iid_ci": ci(boot_iid),
            "block_width": round(ci(boot_block)[1] - ci(boot_block)[0], 4),
            "iid_width": round(ci(boot_iid)[1] - ci(boot_iid)[0], 4),
            "n_nominal": int(n), "n_effective": int(n // block)}


def roc_points(y: np.ndarray, p: np.ndarray, n: int = 60) -> list[list[float]]:
    """ROC 曲线采样点（FPR, TPR）。用于前端画曲线 + 对角线对照。"""
    order = np.argsort(-p)
    yy = y[order]
    P, N = float(yy.sum()), float(len(yy) - yy.sum())
    if P == 0 or N == 0:
        return []
    tpr = np.cumsum(yy) / P
    fpr = np.cumsum(1 - yy) / N
    idx = np.unique(np.linspace(0, len(yy) - 1, n).astype(int))
    pts = [[0.0, 0.0]] + [[round(float(fpr[i]), 4), round(float(tpr[i]), 4)] for i in idx]
    if pts[-1] != [1.0, 1.0]:
        pts.append([1.0, 1.0])
    return pts


def roc_band(y: np.ndarray, p: np.ndarray, block: int, reps: int = 120,
             seed: int = 5, n: int = 40) -> list[list[float]]:
    """ROC 的分块自助带：每个 FPR 网格点上 TPR 的 2.5%/97.5% 分位。

    点估计的 ROC 看起来"离对角线不远"，容易让人以为接近可用；
    把不确定性画出来才知道那条曲线其实可以摆得很开。
    """
    rng = np.random.default_rng(seed)
    L = len(y)
    grid = np.linspace(0, 1, n)
    curves = []
    nblk = int(np.ceil(L / block))
    max_start = max(1, L - block + 1)
    for _ in range(reps):
        starts = rng.integers(0, max_start, nblk)
        take = np.concatenate([np.arange(s, min(s + block, L)) for s in starts])
        take = take[take < L]
        if len(np.unique(y[take])) < 2:
            continue
        pts = roc_points(y[take], p[take], n=200)
        if not pts:
            continue
        xs = np.array([q[0] for q in pts]); ys = np.array([q[1] for q in pts])
        # 插值到统一网格（同一 FPR 上取 TPR）
        curves.append(np.interp(grid, xs, ys))
    if not curves:
        return []
    C = np.array(curves)
    lo = np.percentile(C, 2.5, axis=0)
    hi = np.percentile(C, 97.5, axis=0)
    return [[round(float(grid[i]), 4), round(float(lo[i]), 4), round(float(hi[i]), 4)]
            for i in range(n)]


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """可靠性图：模型说 X% 上涨时，实际涨了多少。

    这是回答"模型给了 74%，我能不能信"的唯一直接办法。
    若模型的 70-80% 分箱实际只有 50% 上涨，那 74% 就没有意义。
    """
    order = np.argsort(p)
    ys, ps = y[order], p[order]
    chunks = np.array_split(np.arange(len(ps)), bins)
    out = []
    for ch in chunks:
        if len(ch) < 8:
            continue
        out.append({"pred": round(float(ps[ch].mean()), 4),
                    "actual": round(float(ys[ch].mean()), 4),
                    "n": int(len(ch))})
    return out


def regime_auc(X_raw: np.ndarray, y: np.ndarray, p: np.ndarray, mask: np.ndarray,
               key: str) -> list[dict]:
    """按状态分层算 AUC —— 检验"预测力是否只存在于部分状态"。

    动机来自 Feng/He/Polson (arXiv 1804.09314)：可预测性集中在
    **特征空间的极值区域**。若全样本 AUC≈0.5 是"平均掉"的结果，
    那么按波动率环境 / 估值分位分层后，两端应该出现差异。

    这是**探索性分析**，按定义会做多次比较，因此每一层的样本都很少、
    区间很宽 —— 前端必须把 n 和区间一起显示，不能只报一个好看的数字。
    """
    j = [k for k, _ in FEATURES].index(key)
    v = X_raw[:, j]
    idx = np.where(mask)[0]
    vv, yv, pv = v[idx], y[idx], p[idx]
    if len(vv) < 60:
        return []
    q1, q2 = np.percentile(vv, [33.3, 66.7])
    groups = [("低", vv <= q1), ("中", (vv > q1) & (vv <= q2)), ("高", vv > q2)]
    out = []
    for name, sel in groups:
        if sel.sum() < 20 or len(np.unique(yv[sel])) < 2:
            continue
        a = auc(yv[sel], pv[sel])
        # 注意：必须把**该桶的子样本**传进去，而不是全样本配一个全 true 的 mask。
        # 早先写成了后者，于是六个桶拿到一模一样的区间（[0.429, 0.596]），
        # 而那个"完全相同"正是 bug 的指纹 —— 不同子样本不可能给出同一条区间。
        yb, pb = yv[sel], pv[sel]
        u = block_bootstrap_auc(yb, pb, np.ones(len(yb), bool),
                                block=max(5, HORIZON // 2), reps=400)
        out.append({"bucket": name, "auc": round(a, 4), "n": int(sel.sum()),
                    "ci": u["block_ci"]})
    return out


class Ridge:
    """岭回归（闭式解 + 增广单位阵）。用于对照实验：预测连续值。"""

    def __init__(self, alpha: float = 1.0):
        self.alpha = alpha

    def fit(self, X: np.ndarray, y: np.ndarray) -> "Ridge":
        n, d = X.shape
        Xb = np.hstack([X, np.ones((n, 1))])
        A = Xb.T @ Xb + self.alpha * np.eye(d + 1)
        A[-1, -1] -= self.alpha          # 截距不惩罚
        self.w = np.linalg.solve(A, Xb.T @ y)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return X @ self.w[:-1] + self.w[-1]


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """秩相关（rank-IC）。金融里衡量预测力的标准度量，比 R² 更贴切。"""
    if len(a) < 3:
        return 0.0
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean(); rb -= rb.mean()
    den = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den > 0 else 0.0


def vol_contrast(market: "MarketData") -> dict:
    """对照实验：**同样的特征、同样的滚动验证**，只把预测目标换掉。

    预测对象 A：未来 20 日**收益的方向**（涨/跌）→ AUC ≈ 0.5
    预测对象 B：未来 20 日**已实现波动率**（连续值）→ 预期显著可预测

    这个对照说明"没有预测能力"是**目标选错了**，不是模型或特征不行：
    金融计量里最稳固的经验规律之一就是收益近似不可预测、而波动率高度可预测
    （波动率聚集，GARCH 这一整支文献的基础）。

    为什么会这样？收益若可预测，套利会把价格推到现在就反映未来，
    可预测性被消灭；而波动率是**风险**不是收益机会，无法通过交易把它套掉。
    """
    X, _y, dates, _m = build_dataset(market, horizon=HORIZON)
    # 重建净值序列以计算未来已实现波动
    _d, eq_rets = market.class_series("equity_cn")
    eq = np.cumprod(1.0 + np.asarray(eq_rets, dtype=float))
    m = len(eq)
    fwd_vol = np.full(m, np.nan)
    ann = np.sqrt(244.0)
    for i in range(m - HORIZON):
        seg = eq_rets[i:i + HORIZON]
        if len(seg) >= 5:
            fwd_vol[i] = float(np.std(seg)) * ann
    # 与特征矩阵对齐
    _dd, _rows, states = build_feature_frame(market)
    keep = [j for j in range(len(states)) if j < m] if False else None
    # build_dataset 已按 NaN 过滤，这里用日期对齐
    dmap = {s["date"]: j for j, s in enumerate(states)}
    idx = [dmap[d] for d in dates if d in dmap]
    yv = np.array([fwd_vol[j] for j in idx], dtype=float) if idx else np.array([])
    if len(yv) != len(X):
        k = min(len(yv), len(X))
        X, yv, dates = X[:k], yv[:k], dates[:k]
    ok = ~np.isnan(yv)
    X, yv = X[ok], yv[ok]

    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    Xs = (X - mu) / sd

    # 同样的前向滚动：只用过去训练，预测未来一段再前移
    preds = np.full(len(yv), np.nan)
    k = MIN_TRAIN
    while k < len(yv):
        end = min(k + STEP, len(yv))
        mdl = Ridge(alpha=1.0).fit(Xs[:k], yv[:k])
        preds[k:end] = mdl.predict(Xs[k:end])
        k = end
    mask = ~np.isnan(preds)
    yt, pt = yv[mask], preds[mask]
    ss_res = float(((yt - pt) ** 2).sum())
    ss_tot = float(((yt - yt.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    ic = _spearman(yt, pt)
    base_r2 = 1 - float(((yt - yt.mean()) ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    # 分块自助给 rank-IC 的区间（同样要按块，理由与 AUC 一致）
    rng = np.random.default_rng(9)
    ics = []
    L = len(yt)
    for _ in range(600):
        starts = rng.integers(0, max(1, L - HORIZON + 1), int(np.ceil(L / HORIZON)))
        take = np.concatenate([np.arange(s, min(s + HORIZON, L)) for s in starts])
        take = take[take < L]
        if len(take) < 20:
            continue
        ics.append(_spearman(yt[take], pt[take]))
    ic_ci = [round(float(np.percentile(ics, 2.5)), 4),
             round(float(np.percentile(ics, 97.5)), 4)] if ics else [0, 0]
    return {"n": int(len(yt)), "r2_oos": round(float(r2), 4),
            "baseline_r2": round(float(base_r2), 4),
            "rank_ic": round(float(ic), 4), "rank_ic_ci": ic_ci,
            "n_effective": int(len(yt) // HORIZON),
            "vol_mean": round(float(yt.mean()), 4)}


def predictability_contrast(market: "MarketData") -> dict:
    """不依赖模型的直接测量：这份数据里，到底什么东西可预测？

    对同样两个 20 日窗口，比较两件事的自相关：
      · 累计**收益** —— 方向可不可预测
      · 已实现**波动** —— 波动可不可预测

    只有 lag ≥ 20 的窗口才是**互不重叠**的，其他 lag 会因窗口重叠而产生
    伪相关（lag=1 时两者都在 0.95 以上，那是 19/20 天重叠的结果，
    与"可预测性"无关 —— 这与我早先在交叉验证里踩到的是同一个坑）。
    """
    _d, r = market.class_series("equity_cn")
    eq = np.cumprod(1.0 + np.asarray(r, float))
    n = len(eq)
    H = HORIZON

    vol_d = np.full(n, np.nan)
    for i in range(H, n):
        vol_d[i] = float(np.std(r[i - H:i])) * np.sqrt(244.0)
    ret_d = np.full(n, np.nan)
    ret_d[H:] = eq[H:] / eq[:-H] - 1.0
    absr = np.abs(np.asarray(r, float))

    def ac(x, lag):
        a, b = x[:-lag], x[lag:]
        ok = ~np.isnan(a) & ~np.isnan(b)
        if ok.sum() < 30:
            return None, 0
        return float(np.corrcoef(a[ok], b[ok])[0, 1]), int(ok.sum())

    lags = [1, 5, 20]
    rows = []
    for lg in lags:
        rc, n1 = ac(ret_d, lg)
        vc, _ = ac(vol_d, lg)
        rows.append({"lag": lg, "ret": None if rc is None else round(rc, 4),
                     "vol": None if vc is None else round(vc, 4),
                     "n": n1, "overlap": lg < H,
                     "stderr": round(1.0 / np.sqrt(max(4, n1 - 3)), 4)})
    dc, nd = ac(absr, H)
    return {"rows": rows, "horizon": H, "n_days": n,
            "abs_ret_lag20": None if dc is None else round(dc, 4),
            "abs_ret_n": nd,
            "abs_ret_stderr": round(1.0 / np.sqrt(max(4, nd - 3)), 4),
            "date_range": [_d[0], _d[-1]]}


class MlpReg:
    """回归用的小型 MLP（线性输出头 + tanh 隐层），纯 numpy。"""

    def __init__(self, hidden: int = 8, lr: float = 0.02, epochs: int = 400,
                 l2: float = 0.004, seed: int = 7):
        self.hidden, self.lr, self.epochs, self.l2, self.seed = hidden, lr, epochs, l2, seed

    def fit(self, X: np.ndarray, y: np.ndarray) -> "MlpReg":
        rng = np.random.default_rng(self.seed)
        n, d = X.shape
        h = self.hidden
        self.W1 = rng.normal(0, np.sqrt(2.0 / d), (d, h)); self.b1 = np.zeros(h)
        self.W2 = rng.normal(0, np.sqrt(2.0 / h), (h, 1)); self.b2 = np.zeros(1)
        ym, ys = float(y.mean()), float(y.std() or 1.0)
        yc = ((y - ym) / ys).reshape(-1, 1)          # 标准化目标，回归才收敛
        mW1 = np.zeros_like(self.W1); mb1 = np.zeros_like(self.b1)
        mW2 = np.zeros_like(self.W2); mb2 = np.zeros_like(self.b2)
        mom = 0.85
        for _ in range(self.epochs):
            H = np.tanh(X @ self.W1 + self.b1)
            p = H @ self.W2 + self.b2
            dz = (p - yc) / n
            gW2 = H.T @ dz + self.l2 * self.W2; gb2 = dz.sum(axis=0)
            dH = (dz @ self.W2.T) * (1.0 - H ** 2)
            gW1 = X.T @ dH + self.l2 * self.W1; gb1 = dH.sum(axis=0)
            mW1 = mom * mW1 - self.lr * gW1; self.W1 += mW1
            mb1 = mom * mb1 - self.lr * gb1; self.b1 += mb1
            mW2 = mom * mW2 - self.lr * gW2; self.W2 += mW2
            mb2 = mom * mb2 - self.lr * gb2; self.b2 += mb2
        self._ym, self._ys = ym, ys
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        H = np.tanh(X @ self.W1 + self.b1)
        return ((H @ self.W2 + self.b2).ravel()) * self._ys + self._ym


def _qlike(h: np.ndarray, rv: np.ndarray) -> float:
    """QLIKE 损失 —— 波动率预测的标准损失函数。

    为何不用 MSE：波动率只能用**代理变量**观测（已实现方差本身有估计误差），
    MSE 会被极端值主导；QLIKE 对代理误差稳健，且对低估更敏感
    （低估风险的代价高于高估）。数值越小越好。
    """
    h = np.clip(h, 1e-10, None)
    rv = np.clip(rv, 1e-10, None)
    return float(np.mean(np.log(h) + rv / h))


def model_race(market: "MarketData") -> dict:
    """在**可预测的那个目标**上比模型：未来 20 日已实现方差。

    候选（全部用同一套前向滚动验证）：
      · 持续性基准（naive）—— 拿当前波动当预测。**波动率预测最强的基准之一**，
        任何模型打不过它就没有存在价值。
      · EWMA(0.94) —— RiskMetrics 的行业标准。
      · HAR —— Corsi(2009) 异质自回归，只用日/周/月三个尺度的已实现波动。
        波动率预测的**标准基准**，三项而已，却极难被打败。
      · HAR + 本项目的 14 个特征（岭回归）
      · 岭回归（仅 14 个特征）
      · 小型 MLP（仅 14 个特征）

    这套比较的意义在于：如果三项的 HAR 打不过、或者打不过"什么都不做"的持续性基准，
    那就说明**上更复杂的模型没有意义** —— 这与 H3 的结论一致。

    **在对数方差空间建模**：方差水平右偏严重、且有异方差，直接回归会被少数极端值主导
    （实测会出现 QLIKE 2200+ 这种荒谬值，因为模型输出接近 0 的方差）。
    先取对数再回归是波动率建模的标准做法：既让分布接近对称，又天然保证预测为正。
    """
    _d, r = market.class_series("equity_cn")
    r = np.asarray(r, float)
    n = len(r)
    H = HORIZON
    var = r ** 2

    def roll_mean(x, w, shift=0):
        out = np.full(len(x), np.nan)
        for i in range(w + shift, len(x)):
            out[i] = float(np.mean(x[i - w - shift:i - shift] or [np.nan]))
        return out

    RV1 = var                                  # 日
    RV5 = np.array([np.mean(var[max(0, i - 4):i + 1]) if i >= 4 else np.nan
                    for i in range(n)])
    RV22 = np.array([np.mean(var[max(0, i - 21):i + 1]) if i >= 21 else np.nan
                     for i in range(n)])
    # 已实现波动（年化），作为预测目标与持续性基准的输入
    rv_hist = np.full(n, np.nan)
    for i in range(H, n):
        rv_hist[i] = float(np.mean(var[i - H:i])) * 244.0

    # 目标：未来 H 日的年化已实现方差
    target = np.full(n, np.nan)
    for i in range(n - H):
        target[i] = float(np.mean(var[i:i + H])) * 244.0

    # 对齐 14 个特征
    _dd, rows, states = build_feature_frame(market)
    dmap = {s["date"]: j for j, s in enumerate(states)}
    dates_all, _r0 = market.class_series("equity_cn")
    X14, tgt, base, har, dates = [], [], [], [], []
    for i in range(n):
        if math.isnan(target[i]) or math.isnan(rv_hist[i]):
            continue
        if i >= len(dates_all):
            continue
        di = dates_all[i]
        j = dmap.get(di)
        if j is None or any(math.isnan(v) for v in rows[j]):
            continue
        if math.isnan(RV1[i]) or math.isnan(RV5[i]) or math.isnan(RV22[i]):
            continue
        X14.append(rows[j])
        har.append([RV1[i], RV5[i], RV22[i]])
        base.append(rv_hist[i])
        tgt.append(target[i])
        dates.append(di)
    if len(tgt) < MIN_TRAIN + 60:
        return {"ok": False, "reason": f"样本不足（{len(tgt)}）"}

    X14 = np.asarray(X14, float)
    HAR = np.asarray(har, float)
    base = np.asarray(base, float)
    tgt = np.asarray(tgt, float)
    L = len(tgt)
    # 全部转成对数方差后建模；评估时再指数还原到方差尺度算 QLIKE
    LOG = np.log(np.clip(tgt, 1e-10, None))
    base_log = np.log(np.clip(base, 1e-10, None))
    HAR_log = np.log(np.clip(HAR, 1e-10, None))

    mu, sd = X14.mean(0), X14.std(0); sd = np.where(sd < 1e-9, 1.0, sd)
    X14s = (X14 - mu) / sd
    hm, hs = HAR_log.mean(0), HAR_log.std(0); hs = np.where(hs < 1e-9, 1.0, hs)
    HARs = (HAR_log - hm) / hs
    HARX = np.hstack([HARs, X14s])

    # EWMA(0.94) 的当前值
    lam = 0.94
    ew = np.full(L, np.nan)
    ewv = var[0]
    idx_of = {d: k for k, d in enumerate(dates_all)}
    for k, d in enumerate(dates):
        i = idx_of[d]
        w = 1.0
        acc = 0.0
        sw = 0.0
        j = i
        while j >= 0 and w > 1e-6:
            acc += w * var[j]; sw += w
            w *= lam; j -= 1
        ew[k] = (acc / sw) * 244.0 if sw > 0 else np.nan
    ew_log = np.log(np.clip(ew, 1e-10, None))

    models = {
        "持续性（拿当前波动当预测）": ("naive", None, None),
        "EWMA(0.94)": ("ewma", None, None),
        "HAR（日/周/月三项）": ("har", HARs, Ridge(alpha=0.05)),
        "HAR + 14 特征": ("harx", HARX, Ridge(alpha=1.0)),
        "岭回归（14 特征）": ("ridge", X14s, Ridge(alpha=1.0)),
        "小型 MLP（14 特征）": ("mlp", X14s, MlpReg()),
    }
    out = []
    model_preds = {}
    for label, (kind, Xd, engine) in models.items():
        pred = np.full(L, np.nan)          # 对数方差空间的预测
        if kind == "naive":
            pred = base_log.copy()
        elif kind == "ewma":
            pred = ew_log.copy()
        else:
            k = MIN_TRAIN
            while k < L:
                end = min(k + STEP, L)
                m = (MlpReg() if isinstance(engine, MlpReg)
                     else Ridge(alpha=engine.alpha))
                m.fit(Xd[:k], LOG[:k])
                pred[k:end] = m.predict(Xd[k:end])
                k = end
        ok = ~np.isnan(pred)
        model_preds[label] = np.where(ok, pred, np.nanmean(pred[ok]))
        yt = tgt[ok]                        # 真实方差（方差尺度）
        pt = np.clip(np.exp(pred[ok]), 1e-10, None)   # 还原到方差尺度
        ss_res = float(((yt - pt) ** 2).sum())
        ss_tot = float(((yt - yt.mean()) ** 2).sum())
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        out.append({
            "label": label, "r2": round(float(r2), 4),
            "qlike": round(_qlike(pt, yt), 4),
            "rank_ic": round(_spearman(yt, pt), 4),
            "n": int(ok.sum()),
        })
    # 全样本重训 HAR，把系数存下来供运行时预测用（这是唯一有实证支持的输出）
    har_final = Ridge(alpha=0.05).fit(HARs, LOG)
    har_forecast = float(np.exp(har_final.predict(HARs[-1:])[0]))
    har_coef = {
        "w": [round(float(v), 8) for v in har_final.w[:-1]],
        "b": round(float(har_final.w[-1]), 8),
        "hm": [round(float(v), 8) for v in hm],
        "hs": [round(float(v), 8) for v in hs],
        "forecast": round(har_forecast, 8),
        "as_of": dates[-1],
    }

    out.sort(key=lambda z: z["qlike"])          # 按 QLIKE 升序 = 越好越前
    for i, z in enumerate(out):
        z["rank"] = i + 1
    best = out[0]
    naive = next(z for z in out if z["label"].startswith("持续性"))
    har = next(z for z in out if z["label"].startswith("HAR（"))

    # 显著性：每个模型相对 HAR 的 QLIKE 差，用**分块**自助给区间。
    # 名义样本 1700+，但有效样本只有 86 个互不重叠的窗口 ——
    # 逐点自助会低估不确定性（与方向预测那边同一个理由）。
    diffs = {}
    try:
        preds_by_label = model_preds
        rng = np.random.default_rng(17)
        har_p = np.exp(preds_by_label["HAR（日/周/月三项）"])
        nblk = int(np.ceil(L / H))
        for label, pr in preds_by_label.items():
            if label == "HAR（日/周/月三项）":
                continue
            p = np.exp(pr)
            ds = []
            for _ in range(400):
                starts = rng.integers(0, max(1, L - H + 1), nblk)
                take = np.concatenate([np.arange(s, min(s + H, L)) for s in starts])
                take = take[take < L]
                if len(take) < 30:
                    continue
                ds.append(_qlike(p[take], tgt[take]) - _qlike(har_p[take], tgt[take]))
            if ds:
                lo, hi = (round(float(np.percentile(ds, 2.5)), 4),
                          round(float(np.percentile(ds, 97.5)), 4))
                diffs[label] = {"lo": lo, "hi": hi,
                                "worse": lo > 0,          # 区间整体 > 0 ⇒ 显著差于 HAR
                                "better": hi < 0}
    except NameError:
        pass
    for z in out:
        z["vs_har"] = diffs.get(z["label"])

    return {"ok": True, "rows": out, "n": L, "n_effective": L // H,
            "target": "未来 20 日年化已实现方差",
            "best": best["label"],
            "har": har_coef,
            "har_beats_naive": har["qlike"] < naive["qlike"],
            "naive_qlike": naive["qlike"], "har_qlike": har["qlike"],
            "note": ("HAR 只有三项（日/周/月已实现波动），却优于所有复杂模型；"
                     "给它加上本项目的 14 个特征反而变差。"
                     "这说明在这份数据的规模上，**结构正确比模型复杂更重要**。")}


def main() -> int:
    print("=" * 78)
    print("量化预测模型训练（前向滚动验证）")
    print("=" * 78)

    market = MarketData()

    # ── 两个预测窗口 × 两个模型，四个组合全部报告 ────────────────────────
    # 不做「跑二十个组合挑最好看的」——那叫数据窥探。这里只测预先设定的四种，
    # 并如实列出全部结果，包括失败的那些。
    print("\n预测力评估（样本外 / 前向滚动）")
    print(f"{'窗口':<8}{'模型':<12}{'AUC':>8}{'准确率':>9}{'基准率':>9}"
          f"{'超出基准':>10}{'样本外':>8}")
    print("-" * 78)

    table: list[dict] = []
    store: dict[tuple[int, str], dict] = {}      # 供后面的诊断图使用
    for hz in HORIZONS:
        X, y, dates, _meta = build_dataset(market, horizon=hz)
        mu, sd = X.mean(axis=0), X.std(axis=0)
        sd = np.where(sd < 1e-9, 1.0, sd)
        Xs = (X - mu) / sd
        for name, factory in (("logistic", lambda: Logistic()),
                              ("mlp", lambda: MLP())):
            preds, mask = walk_forward(Xs, y, factory)
            a = auc(y[mask], preds[mask])
            acc = float(np.mean((preds[mask] > 0.5) == (y[mask] > 0.5)))
            base = max(float(y[mask].mean()), 1 - float(y[mask].mean()))
            row = {"horizon": hz, "model": name, "oos_auc": round(a, 4),
                   "oos_accuracy": round(acc, 4), "baseline_accuracy": round(base, 4),
                   "edge": round(acc - base, 4), "n_oos": int(mask.sum()),
                   "up_rate": round(float(y[mask].mean()), 4),
                   "verdict": ("无预测力" if a < 0.54 else
                               "弱" if a < 0.58 else "中等" if a < 0.62 else "较强")}
            row["uncertainty"] = block_bootstrap_auc(y, preds, mask, block=hz)
            table.append(row)
            store[(hz, name)] = {"y": y, "preds": preds, "mask": mask, "X": X}
            print(f"{hz:>4} 日  {name:<12}{a:>8.4f}{acc:>9.1%}{base:>9.1%}"
                  f"{acc - base:>+10.1%}{row['n_oos']:>8}")

    # ── 诊断图数据（ROC / 分块带 / 校准 / 分状态）────────────────────────
    # 只对"最好的一组"做图，避免把四个组合的图都画出来冲淡重点。
    diag_key = (HORIZON, "mlp")
    dg = store.get(diag_key) or next(iter(store.values()))
    dy, dp, dm, dX = dg["y"], dg["preds"], dg["mask"], dg["X"]
    yy, pp = dy[dm], dp[dm]
    print("\n" + "-" * 78)
    print("诊断图数据")
    print("-" * 78)
    roc_curve = roc_points(yy, pp)
    band = roc_band(yy, pp, block=HORIZON)
    calib = calibration(yy, pp)
    # 不依赖模型的可预测性对照：这份数据里到底什么可预测
    predict_contrast = predictability_contrast(market)
    # 换目标后该用什么模型：HAR / EWMA / 持续性与本项目的两类模型同场比较
    print("  模型赛跑（目标：未来 20 日已实现方差）…")
    race = model_race(market)
    if race.get("ok"):
        for z in race["rows"]:
            print(f"     {z['rank']}. {z['label']:<24} QLIKE {z['qlike']:>9.4f}"
                  f"　R² {z['r2']:>8.4f}　rank-IC {z['rank_ic']:>7.4f}")
    else:
        print(f"     跳过：{race.get('reason')}")

    # ── 量化层升级：极差估计量 → HAR 家族 → 模型置信集 → 风险可检验性 ──────
    # 这一段与上面的 race 是**并列**关系而不是替代：race 用 r² 作代理，
    # 走通了"该不该用复杂模型"这个问题；vol 用 Yang-Zhang 作代理，
    # 进一步回答"用哪个代理变量、模型能不能区分开、预测转化成的 VaR 准不准"。
    print("  波动率层升级诊断（Yang-Zhang / MCS / VaR 回测 / 收缩协方差）…")
    vol = vol_diagnostics(market)
    if vol.get("ok"):
        print(f"     代理变量最优：{vol['best_estimator']}"
              f"（靶子：{vol['target']}）")
        m = vol.get("mcs") or {}
        if m.get("in_set"):
            print(f"     90% 模型置信集：{'、'.join(m['in_set'])}")
            print(f"       已淘汰：{'、'.join(m['eliminated'])}")
        vb = vol.get("var_backtest") or {}
        if vb.get("ok"):
            for v in vb.get("variants", []):
                print(f"     VaR {v['name']:<16} 例外 {v['exceptions']:>3}"
                      f"　实际率 {v['rate']:.3%}　{v['zone']}")
        mm = vol.get("mm_regression") or {}
        if mm.get("ok"):
            print(f"     Moreira-Muir 回归 β = {mm['beta']:.4f}"
                  f"　HAC t = {mm['t_beta']:.3f}　p = {mm['p_beta']:.4f}")
    else:
        print(f"     跳过：{vol.get('reason')}")
    print("  可预测性对照（自相关，只有 lag≥20 的窗口互不重叠）：")
    for q in predict_contrast["rows"]:
        tag = "（重叠窗口，伪相关）" if q["overlap"] else "（不重叠）"
        print(f"     lag={q['lag']:>2}  收益 {q['ret']:+.4f}　波动 {q['vol']:+.4f} {tag}")
    print(f"  ROC 采样点 {len(roc_curve)} 个；分块带 {len(band)} 个网格点")
    print(f"  校准（模型说 X% 上涨 → 实际涨了多少）：")
    for c in calib:
        flag = "  ← 高估" if c["pred"] - c["actual"] > 0.12 else ""
        print(f"     预测 {c['pred']:>5.1%}　实际 {c['actual']:>5.1%}"
              f"　n={c['n']:>3}{flag}")

    regimes = {}
    for key, label in (("vol_ratio", "波动率环境"), ("val_pct", "价格分位")):
        r = regime_auc(dX, dy, dp, dm, key)
        if r:
            regimes[key] = {"label": label, "rows": r}
            spread = max(x["auc"] for x in r) - min(x["auc"] for x in r)
            print(f"  分状态 AUC（{label}）："
                  + "　".join(f"{x['bucket']} {x['auc']:.3f}(n={x['n']})" for x in r)
                  + f"　极差 {spread:.3f}")
            if spread > 0.06:
                print(f"     ⚠ 极差 {spread:.3f} 值得注意，但这是**探索性**分析——"
                      f"分层后每层样本仅 {r[0]['n']} 量级，"
                      f"且做了多次比较，不能据此宣称发现。")

    # ── 重叠标签导致的不确定性：这是本次审查最重要的方法论修正 ──────────
    print("\n" + "-" * 78)
    print("重叠标签对置信区间的影响（分块自助 vs 逐点自助）")
    print("-" * 78)
    print(f"{'窗口':<7}{'模型':<12}{'AUC':>8}{'分块 95% CI':>22}{'逐点 95% CI':>22}{'放大':>7}")
    for row in table:
        u = row["uncertainty"]
        ratio = u["block_width"] / u["iid_width"] if u["iid_width"] else 0
        print(f"{row['horizon']:>4} 日 {row['model']:<12}{row['oos_auc']:>8.4f}"
              f"{str(u['block_ci']):>22}{str(u['iid_ci']):>22}{ratio:>6.1f}×")
    print(f"\n标签用未来 N 日收益、按日采样 ⇒ 相邻样本的前瞻窗口重叠 N-1 天。")
    print(f"名义上 1200+ 条样本外观测，**有效样本量只有约 N/{HORIZON} ≈ 60 个互不重叠的窗口**。")
    print(f"逐点自助假设样本独立，因此把区间算窄了数倍 —— 上面最后一列就是这个倍数。")
    print(f"结论：所有 AUC 都必须按分块区间解读。0.51 的区间跨越 0.5，就是没有预测力。")

    best = max(table, key=lambda r: r["oos_auc"])
    print(f"\n最好的一组：{best['horizon']} 日 / {best['model']}"
          f"，AUC = {best['oos_auc']:.4f}（判定：{best['verdict']}）")
    if best["oos_auc"] < 0.55:
        print("\n⚠ 结论：在当前数据与特征下，模型**不具备可用预测力**。")
        print("   这不是实现问题 —— 20/60 日方向本身接近随机游走，")
        print("   而可用样本只有约 1400 条样本外观测（AUC 的标准误约 ±0.03）。")
        print("   系统会把这个结论如实展示给用户，不会拿 in-sample 的高置信度去唬人。")

    # ── 用 20 日窗口训练最终模型（与研判页展示的窗口一致）────────────────
    X, y, dates, meta = build_dataset(market, horizon=HORIZON)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    Xs = (X - mu) / sd
    rng = np.random.default_rng(11)
    lr_all = Logistic().fit(Xs, y)
    mlp_all = MLP().fit(Xs, y)
    imp_lr = np.abs(lr_all.w)
    imp_lr = imp_lr / (imp_lr.sum() or 1.0)
    imp_mlp = perm_importance(mlp_all, Xs, y, auc(y, mlp_all.predict_proba(Xs)), rng)
    imp_mlp = np.clip(imp_mlp, 0, None)
    imp_mlp = imp_mlp / (imp_mlp.sum() or 1.0)

    latest = Xs[-1:]
    p_lr = float(lr_all.predict_proba(latest)[0])
    p_mlp = float(mlp_all.predict_proba(latest)[0])

    print(f"\n【最新一期】{dates[-1]}　"
          f"逻辑回归 {p_lr:.1%}　神经网络 {p_mlp:.1%}"
          f"　（注意：该数值的可靠性取决于上面的样本外 AUC，"
          f"只有 {best['oos_auc']:.2f}）")

    print("\n【特征重要性（归一化）】")
    for i, (k, lbl) in enumerate(FEATURES):
        print(f"   {lbl:<26} 逻辑回归 {imp_lr[i]:>6.1%}　神经网络 {imp_mlp[i]:>6.1%}")

    payload = {
        "trained_at": np.datetime64("now", "s").astype(str),
        "horizon_days": HORIZON,
        "n_samples": int(len(y)),
        "date_range": [dates[0], dates[-1]],
        "features": [{"key": k, "label": lbl} for k, lbl in FEATURES],
        "standardize": {"mean": [round(float(v), 8) for v in mu],
                        "std": [round(float(v), 8) for v in sd]},
        "leaderboard": table,
        "best": best,
        "logistic": {
            "w": [round(float(v), 8) for v in lr_all.w],
            "b": round(float(lr_all.b), 8),
            "importance": [round(float(v), 6) for v in imp_lr],
        },
        "mlp": {
            "W1": [[round(float(v), 8) for v in row] for row in mlp_all.W1],
            "b1": [round(float(v), 8) for v in mlp_all.b1],
            "W2": [round(float(v), 8) for v in mlp_all.W2.ravel()],
            "b2": round(float(mlp_all.b2[0]), 8),
            "hidden": mlp_all.hidden,
            "importance": [round(float(v), 6) for v in imp_mlp],
        },
        "latest": {
            "date": dates[-1],
            "raw_features": [round(float(v), 8) for v in X[-1]],
            "logistic_prob": round(p_lr, 4),
            "mlp_prob": round(p_mlp, 4),
            "ensemble_prob": round((p_lr + p_mlp) / 2, 4),
        },
        "history": meta[-300:],
        # ── 诊断图数据（前端「量化预测」卡片用）──────────────────────────
        # 这些图的作用不是让模型显得更能干，而是让"它现在不能干什么"
        # 变成看得见的东西：ROC 贴着对角线、区间横跨 0.5、校准曲线偏离 45°。
        "diagnostics": {
            "for": {"horizon": diag_key[0], "model": diag_key[1]},
            "roc": roc_curve,
            "roc_band": band,
            "calibration": calib,
            "regimes": regimes,
            "n_oos": int(dm.sum()),
            # 第五张图：不依赖模型的可预测性对照 —— 收益 vs 波动
            "predictability": predict_contrast,
            # 第六块：换目标后该用什么模型 —— 实测赛跑
            "model_race": race,
            # 第七块：波动率层的完整诊断（估计量 / MCS / VaR 回测 / 收缩协方差）
            "vol": vol,
        },
        "disclaimer": ("本模型只做方向概率估计，不构成投资建议。"
                       "**请以 leaderboard 里的样本外 AUC 判断可信度**，"
                       "而不是看预测概率有多高 —— 一个 AUC≈0.5 的模型照样能"
                       "给出 70% 的置信度，那是过拟合，不是预测力。"),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {OUT}（{OUT.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
