#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波动率层的完整诊断：估计量选择 → HAR 家族赛跑 → 模型置信集 → 风险可检验性。

为什么单独成模块
----------------
原来的 `model_race()` 把注意力放在"哪个模型 QLIKE 更低"，
用 r² 作为已实现方差的代理，并以「与 HAR 的分块自助区间」做显著性判断。
升级后要回答的问题变多了，且每一个都对应一篇方法论文献：

    1. 用哪个波动率代理变量？        → Parkinson / Garman-Klass / RS / Yang-Zhang
    2. 模型能不能区分开？            → 模型置信集 MCS（Hansen-Lunde-Nason 2011）
    3. 波动预测转化成的 VaR 靠谱吗？ → Kupiec 1995 + Christoffersen 1998
    4. 波动目标化到底赚的是什么？    → Moreira-Muir 2017 预测回归（含 HAC）
    5. 回测的夏普是不是挑出来的？    → Deflated Sharpe（Bailey-López de Prado 2014）
    6. 组合权重能不能不靠求逆？      → Ledoit-Wolf 收缩 + HRP（López de Prado 2016）

把这六件事放在一个模块里，是为了让它们共享**同一套滚动验证与同一份数据口径** ——
否则各写一套，结论之间互相不可比，读者也无法复核。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "backend", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from engine.alloc import (compare_allocations, connectedness, hrp_weights,  # noqa: E402
                          ledoit_wolf)
from engine.mcs import model_confidence_set  # noqa: E402
from engine.risk import (deflated_sharpe, moments, var_backtest,  # noqa: E402
                         volatility_managed_regression, vol_target_backtest)
from engine.rv import (EFFICIENCY, ESTIMATOR_LABELS, decompose_jump,  # noqa: E402
                       har_design, quarticity_proxy, realized_variance)

HORIZON = 20
ANN = 244.0
MIN_TRAIN = 500
STEP = 80
MCS_REPS = 500
BLOCK_REPS = 400


# ─────────────────────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────────────────────

def _ridge_fit(X, y, alpha=0.05):
    X1 = np.hstack([X, np.ones((len(X), 1))])
    return np.linalg.solve(X1.T @ X1 + alpha * np.eye(X1.shape[1]), X1.T @ y)


def _ridge_pred(w, X):
    return np.hstack([X, np.ones((len(X), 1))]) @ w


def _qlike(hhat, rv):
    h = np.clip(hhat, 1e-10, None)
    r = np.clip(rv, 1e-10, None)
    return float(np.mean(np.log(h) + r / h))


def _spearman(a, b):
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean(); rb -= rb.mean()
    den = math.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den else 0.0


def _walk_forward(X, target, alpha=0.05):
    """统一的向前滚动回归，返回 (yt, pt, idx) —— 方差尺度。

    **必须把时间下标一并返回**：不同模型的可用样本区间并不相同
    （持续性基准损失了前 H 个观测），而模型置信集要求各模型的损失
    逐时点对齐。若只按长度截断，等于把不同日期拼在一起比，
    MCS 的结论会失真。
    """
    mask = ~np.isnan(X).any(axis=1) & ~np.isnan(target)
    Xv, yv = X[mask], target[mask]
    if len(yv) < MIN_TRAIN + 60:
        return None, None, None
    mu, sd = Xv.mean(0), Xv.std(0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    Xs = (Xv - mu) / sd
    ylog = np.log(np.clip(yv, 1e-12, None))
    pred = np.full(len(yv), np.nan)
    k = MIN_TRAIN
    while k < len(yv):
        e = min(k + STEP, len(yv))
        w = _ridge_fit(Xs[:k], ylog[:k], alpha)
        # 训练残差的方差：用于对数正态的偏差修正。
        # log RV = Xβ + u，若漏掉 exp(σ²_u/2)，还原到方差尺度时会系统性低估水平，
        # 所有 QLIKE 数值整体偏移。排序不受影响，但报告出来的绝对水平会失真。
        resid = ylog[:k] - _ridge_pred(w, Xs[:k])
        s2 = float(resid @ resid) / max(1, k - Xs.shape[1])
        pred[k:e] = _ridge_pred(w, Xs[k:e]) + s2 / 2.0
        k = e
    ok = ~np.isnan(pred)
    return (yv[ok], np.clip(np.exp(pred[ok]), 1e-10, None),
            np.nonzero(mask)[0][ok])


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def vol_diagnostics(market, alloc_key: str = "equity_cn") -> dict:
    """跑完整套波动率诊断。返回可直接写进 model.json 的 dict。"""
    rep = market.representative(alloc_key) or "sh000300"
    dates, o, h, l, c = market.ohlc(rep)
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    n = len(c)
    if n < MIN_TRAIN + HORIZON + 60:
        return {"ok": False, "reason": f"K 线不足（{n}）"}

    # 靶子固定为 Yang-Zhang：用**最好的代理变量**当靶子，比较才公平 ——
    # 否则换靶子等于换赛道，模型之间的 QLIKE 不可比。
    yz = realized_variance(o, h, l, c, 22, "yang_zhang")
    target = np.full(n, np.nan)
    for i in range(n - HORIZON):
        seg = yz[i:i + HORIZON]
        if not np.isnan(seg).any():
            target[i] = float(np.mean(seg)) * ANN

    # ── 1. 估计量选择 ─────────────────────────────────────────────────────
    est_rows = []
    for est in ("close_squared", "parkinson", "garman_klass",
                "rogers_satchell", "yang_zhang"):
        rv = realized_variance(o, h, l, c, 22, est)
        yt, pt, _idx = _walk_forward(har_design(rv)["har"], target)
        if yt is None:
            continue
        ss_res = float(((yt - pt) ** 2).sum())
        ss_tot = float(((yt - yt.mean()) ** 2).sum())
        est_rows.append({
            "key": est, "label": ESTIMATOR_LABELS[est],
            "efficiency": EFFICIENCY[est],
            "qlike": round(_qlike(pt, yt), 4),
            "r2": round(1 - ss_res / ss_tot, 4) if ss_tot else 0.0,
            "rank_ic": round(_spearman(yt, pt), 4),
            "n_effective": len(yt) // HORIZON,
        })
    est_rows.sort(key=lambda r: r["qlike"])
    for i, r in enumerate(est_rows):
        r["rank"] = i + 1
    best_est = est_rows[0]["key"] if est_rows else "yang_zhang"

    # ── 2. HAR 家族赛跑（全部用选定的估计量）──────────────────────────────
    with np.errstate(divide="ignore", invalid="ignore"):
        cret = np.log(c / np.concatenate([[np.nan], c[:-1]]))
    cont, jump = decompose_jump(cret, yz)
    # HARQ 的交互项用**跨估计量离散度**而非 RQ：日频 OHLC 只有 M=1 个观测，
    # RQ = (M/3)Σr⁴ 会退化成 r⁴/3，交互项因此变成 logRV 的二次项，
    # 失去"按测量误差修正日系数"的原意（详见 engine/rv.py 的说明）。
    from engine.rv import measurement_error_proxy  # noqa: PLC0415
    me = measurement_error_proxy(
        realized_variance(o, h, l, c, 22, "parkinson"),
        realized_variance(o, h, l, c, 22, "garman_klass"))
    design = har_design(yz, rq=me, cont=cont, jump=jump)

    preds, race_rows = {}, []

    def _record(label, yt, pt, idx, note=""):
        ss_res = float(((yt - pt) ** 2).sum())
        ss_tot = float(((yt - yt.mean()) ** 2).sum())
        preds[label] = (yt, pt, np.asarray(idx))
        race_rows.append({"label": label,
                          "qlike": round(_qlike(pt, yt), 4),
                          "r2": round(1 - ss_res / ss_tot, 4) if ss_tot else 0.0,
                          "rank_ic": round(_spearman(yt, pt), 4),
                          "n": int(len(yt)), "note": note})

    # 持续性基准
    pers = np.log(np.clip(np.concatenate([[np.nan] * HORIZON, yz[:-HORIZON]]) * ANN,
                          1e-12, None))
    m = ~np.isnan(pers) & ~np.isnan(target)
    if m.sum() > MIN_TRAIN:
        _record("持续性（拿当前波动当预测）", target[m],
                np.clip(np.exp(pers[m]), 1e-10, None), np.nonzero(m)[0],
                "波动率预测的强基准")

    # EWMA(0.94)
    lam, ew = 0.94, np.full(n, np.nan)
    for i in range(n):
        acc = sw = 0.0; wgt = 1.0; j = i
        while j >= 0 and wgt > 1e-6:
            v = yz[j]
            if not np.isnan(v):
                acc += wgt * v; sw += wgt
            wgt *= lam; j -= 1
        ew[i] = (acc / sw) * ANN if sw > 0 else np.nan
    m = ~np.isnan(ew) & ~np.isnan(target)
    if m.sum() > MIN_TRAIN:
        _record("EWMA(0.94)", target[m], np.clip(ew[m], 1e-10, None),
                np.nonzero(m)[0], "RiskMetrics 行业标准")

    har_notes = {
        "har": "Corsi(2009)，三个尺度、三项系数",
        "harq": "Bollerslev-Patton-Quaedvlieg(2016) 的日频适配：交互项用跨估计量离散度代替 RQ^½",
        "har_cj": "Barndorff-Nielsen-Shephard(2004) 的日频近似",
    }
    label_of = {"har": "HAR（日/周/月三项）", "harq": "HARQ（+测量误差交互）",
                "har_cj": "HAR-CJ（连续/跳跃分离）"}
    for key in ("har", "harq", "har_cj"):
        yt, pt, idx = _walk_forward(design[key], target)
        if yt is not None:
            _record(label_of[key], yt, pt, idx, har_notes[key])

    # 不取对数的对照 —— 保留它是为了说明"取对数"不是随手的选择
    mask = ~np.isnan(design["har"]).any(axis=1) & ~np.isnan(target)
    Xv, yv = design["har"][mask], target[mask]
    if len(yv) > MIN_TRAIN + 60:
        mu, sd = Xv.mean(0), Xv.std(0); sd = np.where(sd < 1e-9, 1.0, sd)
        Xs = (Xv - mu) / sd
        raw = np.full(len(yv), np.nan)
        k = MIN_TRAIN
        while k < len(yv):
            e = min(k + STEP, len(yv))
            raw[k:e] = _ridge_pred(_ridge_fit(Xs[:k], yv[:k], 0.05), Xs[k:e])
            k = e
        ok = ~np.isnan(raw)
        _record("HAR（在方差尺度回归，不取对数）", yv[ok],
                np.clip(raw[ok], 1e-10, None), np.nonzero(mask)[0][ok],
                "对照组：说明对数变换的必要性")

    race_rows.sort(key=lambda r: r["qlike"])
    for i, r in enumerate(race_rows):
        r["rank"] = i + 1

    # ── 3. 模型置信集 ─────────────────────────────────────────────────────
    mcs = {"ok": False, "reason": "不可用"}
    labels = [r["label"] for r in race_rows if r["label"] in preds]
    if len(labels) >= 3:
        # 只保留**所有模型都有预测**的时点，保证损失矩阵逐时点可比
        common = set(preds[labels[0]][2].tolist())
        for lb in labels[1:]:
            common &= set(preds[lb][2].tolist())
        common = np.array(sorted(common))
        mcs = {"ok": False, "reason": f"共同时点不足（{len(common)}）"}
        if len(common) > MIN_TRAIN:
            cols, keep = [], []
            for lb in labels:
                pos = {int(v): i for i, v in enumerate(preds[lb][2])}
                if not all(int(t) in pos for t in common):
                    continue
                take = np.array([pos[int(t)] for t in common])
                yt, pt = preds[lb][0][take], preds[lb][1][take]
                cols.append(np.log(np.clip(pt, 1e-10, None))
                            + yt / np.clip(pt, 1e-10, None))
                keep.append(lb)
            if len(keep) >= 3:
                mcs = model_confidence_set(np.column_stack(cols), keep,
                                           block=HORIZON, alpha=0.10,
                                           reps=MCS_REPS, stat="T_SQ")
                mcs["common_obs"] = int(len(common))

    # ── 4. HAR 最终系数（供运行时推理）────────────────────────────────────
    HAR = design["har"]
    mask = ~np.isnan(HAR).any(axis=1) & ~np.isnan(target)
    HARv, TV = HAR[mask], target[mask]
    hm, hs = HARv.mean(0), HARv.std(0); hs = np.where(hs < 1e-9, 1.0, hs)
    HARs = (HARv - hm) / hs
    w = _ridge_fit(HARs, np.log(np.clip(TV, 1e-12, None)), 0.05)
    har_coef = {
        "w": [round(float(v), 8) for v in w[:-1]],
        "b": round(float(w[-1]), 8),
        "hm": [round(float(v), 8) for v in hm],
        "hs": [round(float(v), 8) for v in hs],
        "as_of": dates[int(np.nonzero(mask)[0][-1])],
        "estimator": best_est,
        "representative": rep,
        "representative_name": (market.get(rep).name if market.get(rep) else rep),
    }

    # ── 5. VaR 回测：正态 vs 肥尾 ─────────────────────────────────────────
    # 口径：σ_t 由第 t 日收盘后可得的信息算出，用来判定第 t+1 日是否击穿。
    # 因此 sig 要向前错开一位，否则会用到未来的信息（前视偏差）。
    r = np.diff(np.log(c))
    sig_all = np.sqrt(np.clip(yz[:-1], 1e-18, None))
    r_next, r_now, sig = r[1:], r[:-1], sig_all[:-1]
    var_bt = var_backtest(r_next, sig, level=0.99, dist="both")

    # ── 6. 波动目标化回测 + Deflated Sharpe ───────────────────────────────
    n_trials = max(1, len(race_rows))
    trial_srs = []
    for lb in labels[:6]:
        yt, pt, _ix = preds[lb]
        sig_l = np.sqrt(np.clip(pt, 1e-18, None))[:len(r_next)]
        if len(sig_l) == len(r_next):
            bt = vol_target_backtest(r_next, sig_l, 0.12, trial_srs=None, n_trials=1)
            if bt.get("ok"):
                trial_srs.append(bt["strategy"]["sharpe"])
    vol_bt = vol_target_backtest(r_next, sig, 0.12,
                                 trial_srs=trial_srs or None, n_trials=n_trials)

    # ── 7. 波动率管理预测回归 ─────────────────────────────────────────────
    mm = volatility_managed_regression(r_next, r_now, sig)

    # ── 8. 组合构建：收缩协方差 / HRP / 溢出一致性 ────────────────────────
    alloc_block = {"ok": False}
    conn_block = {"ok": False}
    try:
        codes = [s.code for s in market.symbols.values()
                 if s.tradable and s.asset_class == "equity_cn"]
        if len(codes) >= 4:
            common_dates = None
            series = {}
            for cd in codes:
                dd = market.dates(cd)
                rr = market.returns(cd)
                series[cd] = dict(zip(dd, rr))
                common_dates = set(dd) if common_dates is None else common_dates & set(dd)
            common_dates = sorted(common_dates or [])
            if len(common_dates) > 250:
                R = np.column_stack([[series[cd][d] for d in common_dates] for cd in codes])
                lw = ledoit_wolf(R)
                if lw.get("ok"):
                    cmp_ = compare_allocations(lw["sigma"])
                    names = [market.get(cd).name if market.get(cd) else cd for cd in codes]
                    conn_block = connectedness(R, horizon=10, lags=2, names=names)
                    alloc_block = {
                        "ok": True, "n_assets": lw["n"], "T": lw["T"],
                        "delta": lw["delta"], "cond_sample": lw["cond_sample"],
                        "cond_shrunk": lw["cond_shrunk"], "rbar": lw["rbar"],
                        "rows": cmp_["rows"],
                    }
    except Exception as exc:  # noqa: BLE001
        alloc_block = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    best = race_rows[0]["label"] if race_rows else None
    return {
        "ok": True,
        "horizon_days": HORIZON,
        "representative": rep,
        "target": f"未来 {HORIZON} 日年化 Yang-Zhang 已实现方差",
        "n": int(n),
        "n_effective": int(n // HORIZON),
        "estimators": est_rows,
        "best_estimator": best_est,
        "rows": race_rows,
        "best": best,
        "mcs": mcs,
        "har": har_coef,
        "var_backtest": var_bt,
        "vol_target": vol_bt,
        "mm_regression": mm,
        "allocation": alloc_block,
        "connectedness": conn_block,
        "note": ("靶子固定为 Yang-Zhang 已实现方差；模型比较用 Hansen-Lunde-Nason "
                 "模型置信集而非逐对检验，避免多重比较。VaR 回测同时给出正态与肥尾"
                 "两种分位数，用于区分「波动预测不准」与「分布假设不对」。"),
    }


def main() -> int:
    from data.market import MarketData  # noqa: E402

    m = MarketData()
    res = vol_diagnostics(m)
    if not res.get("ok"):
        print("失败：", res.get("reason"))
        return 1
    print(json.dumps({k: v for k, v in res.items()
                      if k not in ("rows", "estimators")},
                     ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
