#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」质量门禁（QC）。

对应 consensus-pipeline 的 `quality_controller.py`：LLM 产出的方案必须通过
一组**确定性硬门禁**才能交付给家庭。门禁不通过时，系统带着失败清单让配置委员会
修订；连续修订失败则退化为达标兜底方案，绝不把一个违反家庭约束的方案交出去。

分级：
    hard —— 不通过则方案不得交付（触发修订或兜底）
    soft —— 不通过只记录警告，方案可交付但必须向家庭明示
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.agents import ALLOC_KEYS, CLASS_BY_KEY

WEIGHT_TOLERANCE = 0.005        # 权重合计容差
EQUITY_TOLERANCE_PP = 0.03      # 权益超上限的容忍带宽（3 个百分点）
MAX_SINGLE_CLASS = 0.60         # 单一非债券大类上限
MAX_BOND_CLASS = 0.75           # 债券类上限（防御资产允许更集中）
MIN_LIQUID = 0.10               # 现金+存款最低占比


@dataclass
class Check:
    id: str
    name: str
    severity: str            # hard / soft
    passed: bool
    detail: str
    actual: Optional[object] = None
    limit: Optional[object] = None

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "severity": self.severity,
                "passed": self.passed, "detail": self.detail,
                "actual": self.actual, "limit": self.limit}


@dataclass
class QCReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "hard" and not c.passed]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "soft" and not c.passed]

    @property
    def passed(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "n_checks": len(self.checks),
            "n_passed": sum(1 for c in self.checks if c.passed),
            "blockers": [c.as_dict() for c in self.blockers],
            "warnings": [c.as_dict() for c in self.warnings],
            "checks": [c.as_dict() for c in self.checks],
        }

    def blocking_text(self) -> str:
        """给配置委员会看的修订清单。"""
        if not self.blockers:
            return "（无硬性门禁失败）"
        lines = []
        for c in self.blockers:
            lines.append(f"- 【{c.id} {c.name}】{c.detail}"
                         + (f"（当前值 {c.actual}，上限/要求 {c.limit}）"
                            if c.actual is not None or c.limit is not None else ""))
        return "\n".join(lines)


def run_qc(
    weights: dict,
    household: dict,
    proposal: Optional[dict] = None,
    committee: Optional[dict] = None,
    risk_verdict: str = "",
    convergence: Optional[dict] = None,
    plan_reviewed_by_risk: bool = True,
) -> QCReport:
    """执行全部硬门禁。

    plan_reviewed_by_risk: 当前这份方案是否就是风控委员会实际审查过的那一份。
        否决票只对**被审查的那份方案**有效。委员会按否决意见修订后、或系统改用
        确定性兜底方案后，旧否决不再适用于新方案——否则会出现"改了也过不了"的死锁。
        为 False 时 QC-8 降级为软提醒，并在结果里保留 outstanding veto 标记，
        由调用方显式上报（不静默丢弃）。
    """
    rep = QCReport()
    committee = committee or {}
    proposal = proposal or {}

    # ── QC-1 权重完整性 ────────────────────────────────────────────────────
    missing = [k for k in ALLOC_KEYS if k not in (weights or {})]
    negatives = [k for k, v in (weights or {}).items() if isinstance(v, (int, float)) and v < 0]
    total = sum(v for v in (weights or {}).values() if isinstance(v, (int, float)))
    rep.checks.append(Check(
        "QC-1", "权重完整性与归一", "hard",
        (not missing) and (not negatives) and abs(total - 1.0) <= WEIGHT_TOLERANCE,
        (f"缺失大类 {missing}；负权重 {negatives}；合计 {total:.4f}；"
         f"要求：键齐全、非负、合计 1.0±{WEIGHT_TOLERANCE}"),
        actual=round(total, 4), limit=f"1.0±{WEIGHT_TOLERANCE}",
    ))

    w = {k: float(weights.get(k, 0) or 0) for k in ALLOC_KEYS}

    # ── QC-2 权益上限（家庭承受力）────────────────────────────────────────
    eq = w.get("equity_cn", 0) + w.get("equity_global", 0)
    ceiling = float(household.get("equity_ceiling") or 0.35)
    rep.checks.append(Check(
        "QC-2", "权益类不超出家庭承受力上限", "hard",
        eq <= ceiling + EQUITY_TOLERANCE_PP,
        (f"方案权益合计 {eq:.1%}，家庭承受力上限 {ceiling:.1%}"
         f"（容忍带宽 {EQUITY_TOLERANCE_PP:.0%}）。"
         f"上限依据：{household.get('equity_ceiling_reason', '—')}"),
        actual=f"{eq:.1%}", limit=f"≤{ceiling + EQUITY_TOLERANCE_PP:.1%}",
    ))

    # ── QC-3 应急金覆盖 ────────────────────────────────────────────────────
    investable = max(1.0, float(household.get("investable_assets") or 1.0))
    em_need = float(household.get("emergency_gap") or 0.0)
    liquid_w = w.get("cash", 0) + w.get("deposit", 0)
    liquid_amt = investable * liquid_w
    if em_need > 0:
        ok3 = liquid_amt >= em_need * 0.9      # 允许 10% 缺口
        detail3 = (f"应急金缺口 {em_need/10000:.1f} 万元，方案配置的现金+存款 "
                   f"{liquid_amt/10000:.1f} 万元（占比 {liquid_w:.1%}）")
    else:
        ok3 = liquid_w >= MIN_LIQUID * 0.5
        detail3 = f"应急金已达标，方案现金+存款占比 {liquid_w:.1%}"
    rep.checks.append(Check("QC-3", "应急储备覆盖缺口", "hard", ok3, detail3,
                            actual=f"{liquid_amt/10000:.1f}万",
                            limit=f"≥{em_need/10000:.1f}万" if em_need > 0 else f"≥{MIN_LIQUID*0.5:.0%}"))

    # ── QC-4 单一类别集中度 ────────────────────────────────────────────────
    over = []
    for k, v in w.items():
        cap = MAX_BOND_CLASS if k == "bond" else MAX_SINGLE_CLASS
        if v > cap + 1e-9:
            over.append(f"{CLASS_BY_KEY[k]['label']} {v:.1%} > {cap:.0%}")
    rep.checks.append(Check("QC-4", "单一资产类别集中度", "hard", not over,
                            "；".join(over) if over else "各单一类别均在集中度上限内",
                            actual=", ".join(f"{k}={v:.1%}" for k, v in w.items() if v > 0.01),
                            limit=f"非债券类 ≤{MAX_SINGLE_CLASS:.0%}，债券 ≤{MAX_BOND_CLASS:.0%}"))

    # ── QC-5 最低流动性 ────────────────────────────────────────────────────
    rep.checks.append(Check("QC-5", "最低流动性底线", "hard", liquid_w >= MIN_LIQUID,
                            f"现金+存款占比 {liquid_w:.1%}，底线 {MIN_LIQUID:.0%}",
                            actual=f"{liquid_w:.1%}", limit=f"≥{MIN_LIQUID:.0%}"))

    # ── QC-6 保障缺口提示（软）─────────────────────────────────────────────
    gap = float(household.get("insurance_gap") or 0)
    ins = w.get("insurance", 0)
    ok6 = not (gap > 0 and ins < 0.02)
    rep.checks.append(Check("QC-6", "保障缺口的处理", "soft", ok6,
                            (f"测算保障缺口 {gap/10000:.1f} 万元，方案中保险与养老金占比 "
                             f"{ins:.1%}。保障缺口应优先用保障型产品（定期寿险/重疾）解决，"
                             f"而非储蓄型保险") if not ok6 else "保障缺口已覆盖或不存在",
                            actual=f"{ins:.1%}", limit="缺口>0 时建议 ≥2%"))

    # ── QC-7 区间一致性 ────────────────────────────────────────────────────
    out_of_range = []
    for cls in (proposal.get("classes") or []):
        if not cls.get("in_range", True):
            out_of_range.append(
                f"{cls['label']} {cls['weight']:.1%} ∉ [{cls['range'][0]:.1%}, {cls['range'][1]:.1%}]")
    rep.checks.append(Check("QC-7", "权重落在委员会自定区间内", "soft", not out_of_range,
                            "；".join(out_of_range) if out_of_range
                            else "全部大类权重落在委员会给出的区间内",
                            actual=f"{len(out_of_range)} 项越界", limit="0 项"))

    # ── QC-8 风控委员会否决票 ──────────────────────────────────────────────
    # 否决只对**被审查的那一份方案**有效。委员会按否决意见修订后、或系统改用
    # 确定性兜底方案后，旧否决不再适用——否则会出现"改了也过不了"的死锁。
    # 但绝不静默丢弃：降级为软提醒并保留 outstanding veto 标记，由上层显式上报。
    vetoed = "否决" in (risk_verdict or "")
    if vetoed and not plan_reviewed_by_risk:
        rep.checks.append(Check(
            "QC-8", "风控委员会否决权", "soft", False,
            f"风控委员会对**上一版**方案投出否决票。当前方案已按否决意见重新生成"
            f"（旧否决不再适用于本方案），但风控提出的核心关切仍然有效，"
            f"必须向家庭明示。",
            actual="上一版被否决", limit="本版未经风控复审"))
    else:
        rep.checks.append(Check("QC-8", "风控委员会否决权", "hard", not vetoed,
                                ("风控委员会投出否决票，方案必须修改后重新表决"
                                 if vetoed else "风控委员会未否决"),
                                actual="否决" if vetoed else "通过/有条件通过",
                                limit="不得为否决"))

    # ── QC-9 僵持主张的显式裁决 ────────────────────────────────────────────
    need = set()
    for row in (convergence or {}).get("disagreement_map", []):
        if row.get("state") in ("deadlocked", "partial"):
            need.add(row["id"])
    ruled = {str(r.get("argument_id", "")).strip()
             for r in (committee.get("deadlock_rulings") or []) if isinstance(r, dict)}
    missing_rulings = sorted(need - ruled)
    rep.checks.append(Check("QC-9", "未收敛主张必须显式裁决", "hard", not missing_rulings,
                            (f"以下未收敛主张缺少裁决：{missing_rulings}。"
                             f"分歧不是失败，回避分歧才是——委员会必须逐条说明采纳哪一方及理由")
                            if missing_rulings else
                            f"全部 {len(need)} 条未收敛主张均已裁决",
                            actual=f"{len(ruled)} 条裁决", limit=f"需覆盖 {len(need)} 条"))

    # ── QC-10 行动清单与再平衡规则 ─────────────────────────────────────────
    actions = committee.get("actions") or []
    reb = committee.get("rebalance") or {}
    ok10 = bool(actions) and bool(reb.get("rule"))
    rep.checks.append(Check("QC-10", "方案具备可执行性", "soft", ok10,
                            (f"行动清单 {len(actions)} 条；"
                             f"再平衡规则 {'已给出' if reb.get('rule') else '缺失'}。"
                             f"没有落地动作的配置方案等于没有方案"),
                            actual=f"{len(actions)} 条动作", limit="≥1 条动作 + 再平衡规则"))

    return rep


def needs_revision_blockers(rep: QCReport) -> list[str]:
    """返回需要委员会修订的硬门禁 ID（QC-8 风控否决不在此列，它需要改方案而非改文本）。"""
    return [c.id for c in rep.blockers if c.id in {"QC-1", "QC-2", "QC-3", "QC-4", "QC-5", "QC-9"}]
