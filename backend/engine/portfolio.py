#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」组合构建层。

职责：把配置委员会裁决出的**大类权重**，展开成一份可执行、可校验、可复现的方案：
  · 大类 → 具体工具的子分配（确定性，不依赖 LLM）
  · 金额化（按家庭可投资资产规模）
  · 前瞻与历史两套风险收益测算
  · 真实历史情景回放的压力测试
  · 风险预算（识别"权重小但风险大"的隐藏敞口）
  · 期限分层与流动性分层
  · 再平衡规则

这一层是对 LLM 输出的**接地与约束**：LLM 给权重，系统给账。
"""
from __future__ import annotations

import math
from typing import Optional

from data.market import MarketData, TRADING_DAYS
from engine.agents import ALLOC_CLASSES, ALLOC_KEYS, CLASS_BY_KEY


# ─────────────────────────────────────────────────────────────────────────────
# 前瞻假设（明确标注为假设，供测算用；不是预测）
# ─────────────────────────────────────────────────────────────────────────────

FORWARD_ASSUMPTIONS: dict[str, dict] = {
    "equity_cn": {
        "return": 0.065, "vol": 0.19,
        "note": "以沪深300长期年化（约 3.5%–7%）为锚，考虑当前估值偏低给予小幅上修；"
                "历史上 A 股长期收益高度依赖入场估值，区间很宽。",
    },
    "equity_global": {
        "return": 0.070, "vol": 0.18,
        "note": "以美股长期年化（约 7%–10%）下调，扣除 QDII 费率与实际溢价损耗。",
    },
    "bond": {
        "return": 0.030, "vol": 0.025,
        "note": "以 10 年期国债收益率与历史持有收益为锚，利率下行空间已收窄。",
    },
    "gold": {
        "return": 0.035, "vol": 0.16,
        "note": "黄金长期实际回报接近 0–2%，近年涨幅显著高于长期中枢，故向前瞻回归。",
    },
    "commodity": {
        "return": 0.030, "vol": 0.26,
        "note": "商品无内生收益，仅反映供需与通胀，波动极高。",
    },
    "cash": {
        "return": 0.014, "vol": 0.004,
        "note": "按当前货币市场利率中枢。",
    },
    "deposit": {
        "return": 0.018, "vol": 0.006,
        "note": "1-3 年期存款与固收类理财的加权中枢。",
    },
    "insurance": {
        "return": 0.025, "vol": 0.010,
        "note": "养老年金 IRR 中枢；流动性极差，提前退保会损失本金。",
    },
}


def _instrument_map(market: MarketData) -> dict[str, list[dict]]:
    """大类 → 该类下**可投**的具体工具（来自内置数据集）。"""
    out: dict[str, list[dict]] = {}
    for cls in ALLOC_CLASSES:
        dc = cls["dataset_class"]
        if not dc:
            out[cls["key"]] = []
            continue
        syms = [s for s in market.symbols.values()
                if s.asset_class == dc and s.tradable]
        syms.sort(key=lambda s: -s.n_obs)
        out[cls["key"]] = [s.as_dict() for s in syms]
    return out


NON_MARKET_INSTRUMENTS = {
    "deposit": [
        {"name": "3 年期定期存款 / 大额存单", "note": "受存款保险保护，流动性受期限约束"},
        {"name": "固收类银行理财（R2 及以下）", "note": "净值型，注意业绩基准不等于到手收益"},
    ],
    "insurance": [
        {"name": "个人养老金账户（税优）", "note": "每年额度内可税前扣除，退休前锁定"},
        {"name": "商业养老年金 / 增额终身寿", "note": "IRR 约 2.5%，流动性极差，退保有损失"},
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# 权重处理
# ─────────────────────────────────────────────────────────────────────────────

def normalize_weights(weights: dict) -> tuple[dict, dict]:
    """把权重规整到恰好 100%，返回 (规整后权重, 规整记录)。"""
    clean: dict[str, float] = {}
    for k in ALLOC_KEYS:
        v = weights.get(k, 0)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        if v < 0:
            v = 0.0
        clean[k] = v

    total = sum(clean.values())
    audit = {"raw_total": round(total, 6), "adjusted": False, "dropped_keys": []}
    extra = set(weights) - set(ALLOC_KEYS)
    if extra:
        audit["dropped_keys"] = sorted(extra)

    if total <= 1e-9:
        # 全零 → 退化为保守默认配置，并记录
        n = len(ALLOC_KEYS)
        clean = {k: 1.0 / n for k in ALLOC_KEYS}
        audit.update({"adjusted": True, "reason": "全零输入，退化为等权"})
        return clean, audit

    if abs(total - 1.0) > 1e-6:
        clean = {k: v / total for k, v in clean.items()}
        audit.update({"adjusted": True, "reason": f"权重合计 {total:.4f}，已按比例归一"})
    return clean, audit


def portfolio_forward_stats(weights: dict, market: MarketData) -> dict:
    """用前瞻假设 + 真实相关矩阵计算组合的前瞻收益/波动。"""
    codes_map = {c["key"]: c["dataset_class"] for c in ALLOC_CLASSES}
    ret = sum(w * FORWARD_ASSUMPTIONS[k]["return"] for k, w in weights.items())
    # 方差 = ΣΣ w_i w_j σ_i σ_j ρ_ij
    var = 0.0
    keys = [k for k, w in weights.items() if w > 1e-9]
    for a in keys:
        for b in keys:
            rho = _class_correlation(a, b, codes_map, market)
            var += (weights[a] * weights[b]
                    * FORWARD_ASSUMPTIONS[a]["vol"] * FORWARD_ASSUMPTIONS[b]["vol"] * rho)
    vol = math.sqrt(max(0.0, var))
    return {
        "expected_return": round(ret, 6),
        "expected_vol": round(vol, 6),
        "sharpe_rf0": round(ret / vol, 4) if vol > 1e-9 else None,
        "assumptions": {k: FORWARD_ASSUMPTIONS[k] for k in keys},
        "diversification_ratio": (round(
            sum(weights[k] * FORWARD_ASSUMPTIONS[k]["vol"] for k in keys) / vol, 4)
            if vol > 1e-9 else None),
    }


def _class_correlation(a: str, b: str, codes_map: dict, market: MarketData) -> float:
    """大类之间的相关系数：取各类代表工具之间的相关。"""
    if a == b:
        return 1.0
    ca, cb = codes_map.get(a), codes_map.get(b)
    if not ca or not cb:
        return 0.3   # 无行情的类别（存款/保险）：给一个保守的低相关默认值
    sa = max((s for s in market.symbols.values() if s.asset_class == ca),
             key=lambda s: s.n_obs, default=None)
    sb = max((s for s in market.symbols.values() if s.asset_class == cb),
             key=lambda s: s.n_obs, default=None)
    if not sa or not sb:
        return 0.3
    return market.correlation(sa.code, sb.code)


# ─────────────────────────────────────────────────────────────────────────────
# 方案组装
# ─────────────────────────────────────────────────────────────────────────────

def build_proposal(
    weights: dict,
    market: MarketData,
    household: dict,
    ranges: Optional[dict] = None,
    class_rationale: Optional[dict] = None,
) -> dict:
    """把大类权重组装成完整方案。"""
    w, audit = normalize_weights(weights or {})
    investable = float(household.get("investable_assets") or 0)

    inst = _instrument_map(market)

    classes_out = []
    for cls in ALLOC_CLASSES:
        k = cls["key"]
        weight = w[k]
        syms = inst.get(k, [])
        sub = []
        if syms:
            # 类内等权（确定性），并给出每只的金额
            per = weight / len(syms) if syms else 0
            for s in syms:
                sub.append({"code": s["code"], "name": s["name"],
                            "sub_weight": round(per, 6),
                            "amount": round(investable * per, 2),
                            "annual_return_hist": s["annual_return"],
                            "annual_vol_hist": s["annual_vol"],
                            "max_drawdown_hist": s["max_drawdown"]})
        else:
            for item in NON_MARKET_INSTRUMENTS.get(k, []):
                sub.append({"name": item["name"], "note": item["note"],
                            "sub_weight": None, "amount": None})

        rng = (ranges or {}).get(k)
        if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
            # 未给出区间时按 ±25% 相对带宽给出保守区间
            rng = [max(0.0, weight * 0.75), min(1.0, weight * 1.25)]

        classes_out.append({
            "key": k, "label": cls["label"], "desc": cls["desc"],
            "risk": cls["risk"], "liquidity": cls["liquidity"],
            "weight": round(weight, 6),
            "amount": round(investable * weight, 2),
            "range": [round(float(rng[0]), 6), round(float(rng[1]), 6)],
            "in_range": bool(float(rng[0]) - 1e-6 <= weight <= float(rng[1]) + 1e-6),
            "rationale": (class_rationale or {}).get(k, ""),
            "instruments": sub,
        })

    # 汇总层
    risky = sum(w[k] for k in ("equity_cn", "equity_global", "gold", "commodity"))
    defensive = sum(w[k] for k in ("bond", "cash", "deposit", "insurance"))

    # 期限分层
    horizon = {
        "short_term": {  # 0-3 年：本金安全优先
            "label": "短期（0-3 年）",
            "target": "本金安全与随时可用，覆盖应急金与近期确定支出",
            "weight": round(sum(w[k] for k in ("cash", "deposit")), 6),
            "classes": ["cash", "deposit"],
        },
        "mid_term": {    # 3-10 年
            "label": "中期（3-10 年）",
            "target": "以票息与稳健增值为主，承受有限波动",
            "weight": round(sum(w[k] for k in ("bond", "insurance")), 6),
            "classes": ["bond", "insurance"],
        },
        "long_term": {   # 10 年以上
            "label": "长期（10 年以上）",
            "target": "承担波动以换取长期复利，用于养老金等超长期目标",
            "weight": round(sum(w[k] for k in ("equity_cn", "equity_global", "gold", "commodity")), 6),
            "classes": ["equity_cn", "equity_global", "gold", "commodity"],
        },
    }
    for v in horizon.values():
        v["amount"] = round(investable * v["weight"], 2)

    hist = market.portfolio_stats(w)
    fwd = portfolio_forward_stats(w, market)
    stress = market.stress_test(w)
    risk_budget = market.marginal_risk(_instruments_weights(w, market))

    return {
        "weights": {k: round(v, 6) for k, v in w.items()},
        "normalize_audit": audit,
        "investable_assets": investable,
        "classes": classes_out,
        "risk_assets_weight": round(risky, 6),
        "defensive_assets_weight": round(defensive, 6),
        "horizon_layers": horizon,
        "historical": {k: v for k, v in hist.items()
                       if k not in ("curve", "curve_dates")},
        "historical_curve": hist.get("curve"),
        "historical_curve_dates": hist.get("curve_dates"),
        "forward": fwd,
        "stress": stress,
        "risk_budget": risk_budget,
        "backtest_basis": (
            "历史回放与压力测试按**大类**构建日收益序列：每个大类由其全部可投工具"
            "等权组成，某工具尚未上市时该日仅对其余成员等权。这样做是为了让"
            "2018 去杠杆熊市、2020 疫情崩盘、2022 全球加息等情景不被短历史标的"
            "（如 2021 年才上市的恒生科技ETF）挤出窗口。存款/理财与保险/养老金"
            "无连续行情，按利率表折算为恒定日收益参与。"
        ),
    }


def _instruments_weights(weights: dict, market: MarketData) -> dict[str, float]:
    """大类权重 → 工具级权重（类内等权），供风险预算计算。"""
    out: dict[str, float] = {}
    for cls in ALLOC_CLASSES:
        dc = cls["dataset_class"]
        if not dc:
            continue
        syms = [s for s in market.symbols.values()
                if s.asset_class == dc and s.tradable]
        if not syms:
            continue
        per = weights.get(cls["key"], 0.0) / len(syms)
        for s in syms:
            out[s.code] = per
    return out


def baseline_weights() -> dict:
    """标准配置（中性基准），供分析师相对偏离的参照。"""
    return {"equity_cn": 0.22, "equity_global": 0.08, "bond": 0.35, "gold": 0.08,
            "commodity": 0.00, "cash": 0.12, "deposit": 0.10, "insurance": 0.05}


def conservative_weights(market: MarketData, household: dict) -> dict:
    """达标兜底方案：当 LLM 方案被 QC 否决且无法修正时使用。

    规则：
      · 现金+存款至少覆盖应急金目标
      · 权益上限取家庭 equity_ceiling 的一半（保守）
      · 其余按债券/黄金分配
    """
    ceiling = float(household.get("equity_ceiling") or 0.35)
    eq = max(0.05, ceiling * 0.5)
    # 应急金比例
    investable = max(1.0, float(household.get("investable_assets") or 1.0))
    em_target = float(household.get("emergency_target_months") or 6)
    outflow = float(household.get("monthly_outflow") or 0)
    em_need = em_target * outflow
    cash_like = max(0.10, min(0.45, em_need / investable))
    rest = max(0.0, 1.0 - eq - cash_like)
    bond = rest * 0.62
    gold = rest * 0.14
    insurance = rest * 0.09
    deposit = rest - bond - gold - insurance
    w = {"equity_cn": eq * 0.72, "equity_global": eq * 0.28,
         "bond": bond, "gold": gold, "commodity": 0.0,
         "cash": cash_like * 0.5, "deposit": cash_like * 0.5 + deposit,
         "insurance": insurance}
    return normalize_weights(w)[0]
