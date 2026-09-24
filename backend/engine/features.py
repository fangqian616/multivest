#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化模型的特征工程 —— **纯标准库**实现，训练与推理共用同一份代码。

为什么要单独抽出来
------------------
训练脚本用 numpy（快），但桌面应用不能把 numpy 打进包（exe 会从 42 MB 涨到 ~75 MB），
所以运行时推理走纯 Python。如果两边各写一份特征逻辑，迟早会漂移 ——
模型在 A 特征上训练、在 B 特征上推理，而且不会报错，只会悄悄变差。

因此这里只写一次：纯 Python 标准库实现，训练脚本 import 它再转成 numpy 数组。

严格 point-in-time
------------------
第 t 行的特征只使用 ≤ t 的数据；标签用 t+horizon 之后的数据，
由调用方负责（本模块不产出标签）。
"""
from __future__ import annotations

import math
from typing import Optional

# （键, 中文名）—— 顺序即特征向量顺序，训练与推理必须一致
FEATURES: list[tuple[str, str]] = [
    ("mom5", "5 日动量"),
    ("mom20", "20 日动量"),
    ("mom60", "60 日动量"),
    ("mom120", "120 日动量"),
    ("val_pct", "价格分位（一年高低区间）"),
    ("vol20", "20 日已实现波动"),
    ("vol_ratio", "波动率环境（20/60 日）"),
    ("dist_ma20", "相对 20 日均线偏离"),
    ("breadth", "市场宽度（20 日动量为正的占比）"),
    ("dispersion", "截面离散度"),
    ("bond_eq_rs", "债券-权益相对强弱"),
    ("gold_mom20", "黄金 20 日动量"),
    ("corr_level", "股债 20 日滚动相关"),
    ("volume_trend", "成交活跃度（20/60 日均量）"),
]
FEATURE_KEYS = [k for k, _ in FEATURES]
FEATURE_LABELS = {k: lbl for k, lbl in FEATURES}

TRADING_DAYS = 244.0
VAL_WINDOW = 244          # 价格分位的回看窗口（一年）
MIN_HISTORY = 130         # 至少要有这么多天才能算出全部特征


# ─────────────────────────────────────────────────────────────────────────────
# 基础统计
# ─────────────────────────────────────────────────────────────────────────────

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pstdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def _cum(rets: list[float]) -> list[float]:
    out, v = [], 1.0
    for r in rets:
        v *= (1.0 + r)
        out.append(v)
    return out


def _ret(series: list[float], i: int, n: int) -> Optional[float]:
    if i - n < 0:
        return None
    prev = series[i - n]
    return (series[i] / prev - 1.0) if prev > 0 else None


def _roll_vol(rets: list[float], i: int, n: int) -> Optional[float]:
    if i - n < 0:
        return None
    seg = rets[i - n:i]
    return _pstdev(seg) * math.sqrt(TRADING_DAYS) if len(seg) >= 2 else None


def _roll_corr(a: list[float], b: list[float], i: int, n: int) -> Optional[float]:
    if i - n < 0:
        return None
    x, y = a[i - n:i], b[i - n:i]
    if len(x) < 3:
        return None
    mx, my = _mean(x), _mean(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx < 1e-12 or sy < 1e-12:
        return 0.0
    cov = sum((x[k] - mx) * (y[k] - my) for k in range(len(x)))
    return max(-1.0, min(1.0, cov / (sx * sy)))


# ─────────────────────────────────────────────────────────────────────────────
# 特征矩阵
# ─────────────────────────────────────────────────────────────────────────────

def build_feature_frame(market) -> tuple[list[str], list[list[float]], list[dict]]:
    """返回 (日期轴, 特征矩阵, 每行的原始市场状态)。

    矩阵的每一行长度等于 len(FEATURES)，顺序一致。
    含 NaN 的行（历史长度不够）由调用方过滤。
    """
    dates, eq_rets = market.class_series("equity_cn")
    if len(eq_rets) < MIN_HISTORY + 5:
        return [], [], []
    _, bond_rets = market.class_series("bond")
    _, gold_rets = market.class_series("gold")

    n = len(eq_rets)
    eq = _cum(eq_rets)
    bond = _cum(bond_rets[-n:]) if len(bond_rets) >= n else _cum(bond_rets)
    gold = _cum(gold_rets[-n:]) if len(gold_rets) >= n else _cum(gold_rets)
    m = min(len(eq), len(bond), len(gold))
    eq, bond, gold = eq[-m:], bond[-m:], gold[-m:]
    dates = dates[-m:]
    eq_r = [eq[i] / eq[i - 1] - 1.0 for i in range(1, m) if eq[i - 1] > 0]
    bond_r = [bond[i] / bond[i - 1] - 1.0 for i in range(1, m) if bond[i - 1] > 0]

    # 沪深300 成交量（成交活跃度）
    vol_axis: list[float] = []
    bars = (market._history.get("sh000300") or {}).get("bars") or []
    if bars and bars[0].get("v"):
        vol_axis = [float(b.get("v") or 0) for b in bars[-m:]]

    # 各标的的 20 日动量（宽度与离散度）
    sym_moms: list[list[Optional[float]]] = []
    for sym in market.symbols.values():
        if sym.n_obs < 120:
            continue
        b = (market._history.get(sym.code) or {}).get("bars") or []
        cl = [float(x["c"]) for x in b[-(m + 25):]]
        if len(cl) < 25:
            continue
        if len(cl) < m:
            cl = [None] * (m - len(cl)) + cl  # type: ignore[list-item]
        else:
            cl = cl[-m:]
        row: list[Optional[float]] = []
        for i in range(m):
            if cl[i] is None or i - 20 < 0 or cl[i - 20] is None or cl[i - 20] <= 0:
                row.append(None)
            else:
                row.append(cl[i] / cl[i - 20] - 1.0)  # type: ignore[operator]
        sym_moms.append(row)

    rows: list[list[float]] = []
    states: list[dict] = []
    nan = float("nan")
    for i in range(m):
        f: dict[str, float] = {}
        f["mom5"] = _ret(eq, i, 5) or nan
        f["mom20"] = _ret(eq, i, 20) or nan
        f["mom60"] = _ret(eq, i, 60) or nan
        f["mom120"] = _ret(eq, i, 120) or nan

        if i >= VAL_WINDOW:
            win = eq[i - VAL_WINDOW:i]
            lo, hi = min(win), max(win)
            f["val_pct"] = (eq[i] - lo) / (hi - lo) if hi > lo else 0.5
        else:
            f["val_pct"] = nan

        # eq_r[i-1] 对应第 i 日的收益（eq_r 从第 1 日起）
        j = i - 1
        v20 = _roll_vol(eq_r, j, 20) if j >= 0 else None
        v60 = _roll_vol(eq_r, j, 60) if j >= 0 else None
        f["vol20"] = v20 if v20 is not None else nan
        f["vol_ratio"] = (v20 / v60) if (v20 and v60 and v60 > 1e-9) else nan

        if i >= 20:
            ma = _mean(eq[i - 20:i])
            f["dist_ma20"] = (eq[i] / ma - 1.0) if ma > 0 else 0.0
        else:
            f["dist_ma20"] = nan

        m20b = _ret(bond, i, 20)
        m20e = _ret(eq, i, 20)
        f["bond_eq_rs"] = (m20b - m20e) if (m20b is not None and m20e is not None) else nan
        g20 = _ret(gold, i, 20)
        f["gold_mom20"] = g20 if g20 is not None else nan

        c = _roll_corr(eq_r, bond_r[-len(eq_r):] if len(bond_r) >= len(eq_r) else bond_r,
                       j, 20) if j >= 0 else None
        f["corr_level"] = c if c is not None else nan

        if vol_axis and i >= 60:
            a20 = _mean(vol_axis[i - 20:i])
            a60 = _mean(vol_axis[i - 60:i])
            f["volume_trend"] = (a20 / a60) if a60 > 0 else 1.0
        else:
            f["volume_trend"] = nan

        vals = [r[i] for r in sym_moms if r[i] is not None]
        if len(vals) >= 5:
            f["breadth"] = sum(1 for x in vals if x > 0) / len(vals)
            f["dispersion"] = _pstdev(vals)
        else:
            f["breadth"] = nan
            f["dispersion"] = nan

        rows.append([f[k] for k in FEATURE_KEYS])
        states.append({"date": dates[i], "nav": round(eq[i], 4)})

    return dates, rows, states


def latest_feature_row(market) -> Optional[dict]:
    """取最新一行的特征（供推理用）。返回 None 表示历史不足。"""
    dates, rows, states = build_feature_frame(market)
    for i in range(len(rows) - 1, -1, -1):
        if all(not math.isnan(v) for v in rows[i]):
            return {"date": states[i]["date"], "features": rows[i],
                    "nav": states[i]["nav"]}
    return None
