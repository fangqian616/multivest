#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」家庭财务画像层。

把一张家庭资产负债表 + 现金流 + 目标 + 保障，转换为一组**确定性的**财务事实
（不调用 LLM）。这些事实随后作为所有分析师的共同输入，避免各 agent 各自
臆测家庭状况——这对应 TradingAgents 的 "公司身份在 agent 运行前确定性解析"。

确定性原则：同一份输入 → 同一份画像。所有比率都在这里算好，
分析师只负责解释与主张，不负责算术。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ── 资产科目（顺序即前端表单顺序）────────────────────────────────────────────
ASSET_KEYS: list[tuple[str, str, str]] = [
    ("cash_deposit", "活期与定期存款", "现金类"),
    ("money_fund", "货币基金 / 现金管理", "现金类"),
    ("bank_wealth", "银行理财", "固收类"),
    ("bond_fund", "债券基金", "固收类"),
    ("equity_fund", "股票与权益基金", "权益类"),
    ("pension_account", "个人养老金账户", "养老类"),
    ("insurance_cash", "储蓄型保险现金价值", "保障类"),
    ("property_self_use", "自住房产（市值）", "房产"),
    ("property_invest", "投资性房产（市值）", "房产"),
    ("other", "其他资产", "其他"),
]

LIABILITY_KEYS: list[tuple[str, str]] = [
    ("mortgage_balance", "房贷余额"),
    ("consumer_debt", "消费贷 / 信用卡分期余额"),
    ("other_debt", "其他负债"),
]

LIQUID_KEYS = {"cash_deposit", "money_fund"}


@dataclass
class Goal:
    name: str
    amount: float                 # 目标金额（现值）
    years: float                  # 距今剩余年数
    priority: str = "important"   # critical / important / optional
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class HouseholdInput:
    """家庭财务输入。所有金额单位为元，比率用小数（0.0355 = 3.55%）。"""

    name: str = "我的家庭"
    head_age: int = 35
    retirement_age: int = 60
    dependents: int = 1                  # 需供养人数（子女+老人）
    city_tier: str = "二线"

    assets: dict[str, float] = field(default_factory=dict)
    liabilities: dict[str, float] = field(default_factory=dict)
    mortgage_rate: float = 0.0355
    consumer_rate: float = 0.0700

    monthly_income: float = 30000.0      # 家庭税后月收入
    income_stability: int = 4            # 1-5，1=极不稳定
    monthly_expense: float = 15000.0     # 家庭月基本支出（不含月供）
    monthly_mortgage: float = 8000.0     # 每月还款额

    goals: list[Goal] = field(default_factory=list)

    has_medical_insurance: bool = True
    has_critical_illness: bool = False
    life_sum_assured: float = 0.0

    risk_score: float = 60.0             # 风险测评得分 0-100（主观偏好）
    loss_tolerance_pct: float = 15.0     # 可承受的最大账面回撤 %
    experience_years: float = 3.0

    # ── 反序列化 ──────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, d: dict) -> "HouseholdInput":
        d = dict(d or {})
        goals = [Goal(**g) if isinstance(g, dict) else g for g in (d.get("goals") or [])]
        assets = {k: float(v or 0) for k, v in (d.get("assets") or {}).items()}
        liabilities = {k: float(v or 0) for k, v in (d.get("liabilities") or {}).items()}
        known = {f for f in cls.__dataclass_fields__}          # noqa: SLF001
        payload = {k: v for k, v in d.items()
                   if k in known and k not in {"assets", "liabilities", "goals"}}
        obj = cls(**payload)
        obj.assets = assets
        obj.liabilities = liabilities
        obj.goals = goals
        return obj

    def to_dict(self) -> dict:
        return {
            **{k: v for k, v in asdict(self).items()
               if k not in {"assets", "liabilities", "goals"}},
            "assets": self.assets,
            "liabilities": self.liabilities,
            "goals": [g.as_dict() for g in self.goals],
        }


# ─────────────────────────────────────────────────────────────────────────────
# 画像计算
# ─────────────────────────────────────────────────────────────────────────────

class HouseholdProfile:
    """由 HouseholdInput 推出的确定性财务事实。"""

    def __init__(self, inp: HouseholdInput, rates: Optional[dict] = None):
        self.inp = inp
        self.rates = rates or {}
        self._compute()

    # ── 主计算 ────────────────────────────────────────────────────────────
    def _compute(self) -> None:
        i = self.inp

        self.assets = {k: float(i.assets.get(k, 0) or 0) for k, _, _ in ASSET_KEYS}
        self.liabilities = {k: float(i.liabilities.get(k, 0) or 0) for k, _ in LIABILITY_KEYS}

        self.total_assets = sum(self.assets.values())
        self.total_liabilities = sum(self.liabilities.values())
        self.net_worth = self.total_assets - self.total_liabilities

        self.liquid_assets = sum(self.assets[k] for k in LIQUID_KEYS)
        self.property_assets = self.assets["property_self_use"] + self.assets["property_invest"]
        # 可投资资产 = 总资产 − 自住房 − 应急储备中已含的活期部分之外的部分
        # 口径：自住房不产生现金流，不计入可投资；投资房计入但标注为非流动
        self.investable_assets = max(
            0.0, self.total_assets - self.assets["property_self_use"]
            - self.assets["property_invest"] * 0.0)   # 投资房仍算可配置，只是非流动

        self.monthly_outflow = i.monthly_expense + i.monthly_mortgage
        self.savings = i.monthly_income - self.monthly_outflow
        self.savings_rate = (self.savings / i.monthly_income) if i.monthly_income > 0 else 0.0
        self.annual_savings = self.savings * 12.0

        self.leverage = (self.total_liabilities / self.total_assets
                         if self.total_assets > 0 else 0.0)
        self.mortgage_ratio = (self.liabilities["mortgage_balance"] / self.assets["property_self_use"]
                               if self.assets["property_self_use"] > 0 else 0.0)
        self.debt_to_income = (self.total_liabilities / (i.monthly_income * 12.0)
                               if i.monthly_income > 0 else 0.0)

        # 应急储备覆盖月数：只算真正能立刻变现的
        self.emergency_months = (self.liquid_assets / self.monthly_outflow
                                 if self.monthly_outflow > 0 else 0.0)
        self.emergency_target_months = self._emergency_target()

        # 保障缺口：寿险保额应覆盖 5-10 年家庭支出 + 负债
        self.insurance_gap = max(
            0.0, self.monthly_outflow * 12 * 7 + self.total_liabilities - i.life_sum_assured - self.liquid_assets)

        # 房贷利率 vs 无风险/预期收益（提前还款决策的关键比较基准）
        self.mortgage_rate = i.mortgage_rate
        self.risk_free = float(self.rates.get("deposit_3y", 0.0155))
        self.expected_balanced_return = 0.055   # 稳健组合的名义预期中枢，仅供比较
        self.mortgage_spread = i.mortgage_rate - self.risk_free

        # 生命周期
        self.lifecycle_stage, self.lifecycle_label = self._lifecycle()

        # 风险承受力（客观能力）与偏好（主观意愿）
        self.capacity_score, self.capacity_breakdown = self._capacity_score()
        self.preference_score = max(0.0, min(100.0, i.risk_score))
        self.risk_score = min(self.capacity_score, self.preference_score)
        self.constraint_binding = ("承受能力" if self.capacity_score < self.preference_score
                                   else "主观偏好" if self.preference_score < self.capacity_score
                                   else "两者相当")

        # 权益上限：取客观能力与主观偏好的较小者，再叠加回撤容忍度约束
        eq_by_risk = self._equity_ceiling_from_score(self.risk_score)
        eq_by_drawdown = self._equity_ceiling_from_drawdown()
        self.equity_ceiling = min(eq_by_risk, eq_by_drawdown)
        self.equity_ceiling_reason = (
            f"由风险评分 {self.risk_score:.0f}/100 推出上限 {eq_by_risk:.0%}；"
            f"由可承受回撤 {i.loss_tolerance_pct:.0f}% 推出上限 {eq_by_drawdown:.0%}；"
            f"取较小者 {self.equity_ceiling:.0%}（约束来自{self.constraint_binding}）")

        # 目标缺口
        self.goal_analysis = self._goals()

        # 流动性分层建议
        self.emergency_gap = max(0.0, self.emergency_target_months * self.monthly_outflow
                                 - self.liquid_assets)

    # ── 子计算 ────────────────────────────────────────────────────────────
    def _emergency_target(self) -> float:
        """应急金目标月数：收入越不稳定、负债越高、供养人口越多，要求越高。"""
        base = 6.0
        base += (3 - self.inp.income_stability) * 1.5          # 稳定性 5 → 3 个月；1 → 9 个月
        base += min(3.0, self.leverage * 10)                    # 杠杆加成
        base += min(2.0, self.inp.dependents * 0.7)
        if self.inp.has_medical_insurance:
            base -= 0.5
        return max(3.0, round(base, 1))

    def _lifecycle(self) -> tuple[str, str]:
        age = self.inp.head_age
        years_to_retire = self.inp.retirement_age - age
        if age < 30:
            return "accumulation_early", "财富积累初期"
        if age < 40:
            if self.inp.dependents > 0:
                return "family_formation", "家庭成长期"
            return "accumulation_mid", "财富积累中期"
        if age < 55:
            return "peak_earning", "收入高峰期"
        if years_to_retire > 0:
            return "pre_retirement", "退休准备期"
        return "retirement", "退休支取期"

    def _capacity_score(self) -> tuple[float, list[dict]]:
        """客观风险承受能力评分（0-100）。收入稳定性、期限、杠杆、流动性、供养负担。"""
        i = self.inp
        parts: list[dict] = []

        def add(name: str, score: float, weight: float, detail: str) -> None:
            parts.append({"dimension": name, "score": round(score, 1),
                          "weight": weight, "detail": detail})

        # 1. 投资期限（退休前年数）
        years = max(1.0, i.retirement_age - i.head_age)
        add("投资期限", min(100.0, years / 30.0 * 100), 0.20,
            f"距退休 {years:.0f} 年，期限越长越能消化短期波动")

        # 2. 收入稳定性
        add("收入稳定性", (i.income_stability - 1) / 4.0 * 100, 0.25,
            f"稳定性 {i.income_stability}/5")

        # 3. 杠杆水平（负债/总资产）
        lev_score = max(0.0, 100.0 - self.leverage * 250.0)
        add("杠杆水平", lev_score, 0.20,
            f"负债率 {self.leverage:.1%}，负债率越高承受力越弱")

        # 4. 应急储备
        em_score = min(100.0, self.emergency_months / max(1.0, self.emergency_target_months) * 100)
        add("应急储备", em_score, 0.20,
            f"可覆盖 {self.emergency_months:.1f} 个月（目标 {self.emergency_target_months:.1f} 个月）")

        # 5. 供养负担
        dep_score = max(0.0, 100.0 - i.dependents * 22.0)
        add("供养负担", dep_score, 0.15, f"需供养 {i.dependents} 人")

        total = sum(p["score"] * p["weight"] for p in parts) / sum(p["weight"] for p in parts)
        return round(total, 1), parts

    @staticmethod
    def _equity_ceiling_from_score(score: float) -> float:
        """风险评分 → 权益类上限（含海外权益）。线性映射：0 分→10%，100 分→80%。"""
        return 0.10 + (max(0.0, min(100.0, score)) / 100.0) * 0.70

    def _equity_ceiling_from_drawdown(self) -> float:
        """可承受回撤 → 权益上限。

        用内置数据集的真实历史最大回撤作为权益资产的回撤代理（约 -45%），
        则权益权重 w 对应的组合回撤 ≈ w × 45%。反解得到 w 上限。
        """
        proxy_dd = 0.45
        tol = max(1.0, self.inp.loss_tolerance_pct) / 100.0
        # 组合回撤 ≈ w*proxy_dd（保守假设股债非同步下跌，不做分散化折扣）
        return max(0.05, min(0.85, tol / proxy_dd))

    def _goals(self) -> list[dict]:
        """目标所需年储蓄 vs 家庭实际年储蓄，判断可行性。"""
        out = []
        for g in self.inp.goals:
            if g.years <= 0 or g.amount <= 0:
                continue
            # 用稳健组合预期收益（5.5%）折现，得到每年需存的金额
            r = 0.055
            n = g.years
            fv_factor = ((1 + r) ** n - 1) / r if r > 0 else n
            need_annual = g.amount / fv_factor if fv_factor > 0 else g.amount / max(n, 1)
            feasible = self.annual_savings >= need_annual
            out.append({
                **g.as_dict(),
                "required_annual_saving": round(need_annual, 2),
                "share_of_savings": (round(need_annual / self.annual_savings, 3)
                                     if self.annual_savings > 0 else None),
                "feasible": feasible,
                "target_year": None,
            })
        return out

    # ── 输出 ──────────────────────────────────────────────────────────────
    def risk_grade(self) -> tuple[int, str]:
        """映射到中国投资者风险承受能力等级（C1-C5）。"""
        s = self.risk_score
        if s < 25:
            return 1, "C1 保守型"
        if s < 45:
            return 2, "C2 稳健型"
        if s < 65:
            return 3, "C3 平衡型"
        if s < 82:
            return 4, "C4 成长型"
        return 5, "C5 进取型"

    def as_dict(self) -> dict:
        grade, grade_label = self.risk_grade()
        return {
            "name": self.inp.name,
            "head_age": self.inp.head_age,
            "retirement_age": self.inp.retirement_age,
            "dependents": self.inp.dependents,
            "city_tier": self.inp.city_tier,
            "assets": self.assets,
            "liabilities": self.liabilities,
            "asset_breakdown": [
                {"key": k, "label": lbl, "group": grp, "value": self.assets[k],
                 "share": round(self.assets[k] / self.total_assets, 4) if self.total_assets else 0}
                for k, lbl, grp in ASSET_KEYS
            ],
            "total_assets": round(self.total_assets, 2),
            "total_liabilities": round(self.total_liabilities, 2),
            "net_worth": round(self.net_worth, 2),
            "liquid_assets": round(self.liquid_assets, 2),
            "property_assets": round(self.property_assets, 2),
            "investable_assets": round(self.investable_assets, 2),
            "monthly_income": round(self.inp.monthly_income, 2),
            "monthly_expense": round(self.inp.monthly_expense, 2),
            "monthly_mortgage": round(self.inp.monthly_mortgage, 2),
            "monthly_outflow": round(self.monthly_outflow, 2),
            "monthly_savings": round(self.savings, 2),
            "savings_rate": round(self.savings_rate, 4),
            "annual_savings": round(self.annual_savings, 2),
            "leverage": round(self.leverage, 4),
            "mortgage_ratio": round(self.mortgage_ratio, 4),
            "debt_to_income": round(self.debt_to_income, 4),
            "emergency_months": round(self.emergency_months, 2),
            "emergency_target_months": self.emergency_target_months,
            "emergency_gap": round(self.emergency_gap, 2),
            "insurance_gap": round(self.insurance_gap, 2),
            "mortgage_rate": self.mortgage_rate,
            "risk_free_rate": self.risk_free,
            "mortgage_spread": round(self.mortgage_spread, 4),
            "lifecycle_stage": self.lifecycle_stage,
            "lifecycle_label": self.lifecycle_label,
            "capacity_score": self.capacity_score,
            "capacity_breakdown": self.capacity_breakdown,
            "preference_score": self.preference_score,
            "risk_score": round(self.risk_score, 1),
            "risk_grade": grade,
            "risk_grade_label": grade_label,
            "constraint_binding": self.constraint_binding,
            "equity_ceiling": round(self.equity_ceiling, 4),
            "equity_ceiling_reason": self.equity_ceiling_reason,
            "loss_tolerance_pct": self.inp.loss_tolerance_pct,
            "goals": self.goal_analysis,
            "has_medical_insurance": self.inp.has_medical_insurance,
            "has_critical_illness": self.inp.has_critical_illness,
            "life_sum_assured": self.inp.life_sum_assured,
        }

    def to_prompt_text(self) -> str:
        """把画像渲染成供分析师阅读的紧凑文本（控制 token）。"""
        d = self.as_dict()
        L: list[str] = []
        L.append(f"【家庭】{d['name']}｜户主 {d['head_age']} 岁，计划 {d['retirement_age']} 岁退休，"
                 f"需供养 {d['dependents']} 人，{d['city_tier']}城市｜生命周期：{d['lifecycle_label']}")
        L.append("")
        L.append("【资产】单位：万元")
        for row in d["asset_breakdown"]:
            if row["value"] > 0:
                L.append(f"  - {row['label']}：{row['value']/10000:.1f}"
                         f"（占比 {row['share']*100:.1f}%）")
        L.append(f"  合计总资产 {d['total_assets']/10000:.1f}｜总负债 {d['total_liabilities']/10000:.1f}"
                 f"｜净资产 {d['net_worth']/10000:.1f}｜负债率 {d['leverage']*100:.1f}%")
        L.append(f"  其中可投资资产（剔除自住房）{d['investable_assets']/10000:.1f}｜"
                 f"可即时变现 {d['liquid_assets']/10000:.1f}")
        L.append("")
        L.append("【现金流】月收入 "
                 f"{d['monthly_income']/10000:.2f} 万｜月支出 {d['monthly_expense']/10000:.2f} 万"
                 f"｜月供 {d['monthly_mortgage']/10000:.2f} 万｜月结余 {d['monthly_savings']/10000:.2f} 万"
                 f"（储蓄率 {d['savings_rate']*100:.0f}%）｜年结余 {d['annual_savings']/10000:.1f} 万")
        L.append(f"  应急储备覆盖 {d['emergency_months']:.1f} 个月"
                 f"（按收入稳定性/杠杆/供养负担测算的目标为 {d['emergency_target_months']:.1f} 个月，"
                 f"缺口 {d['emergency_gap']/10000:.1f} 万）")
        L.append("")
        L.append("【风险】客观承受能力评分 "
                 f"{d['capacity_score']:.0f}/100，主观偏好 {d['preference_score']:.0f}/100，"
                 f"综合 {d['risk_score']:.0f}/100 → {d['risk_grade_label']}；"
                 f"约束主要来自{d['constraint_binding']}")
        for p in d["capacity_breakdown"]:
            L.append(f"  - {p['dimension']}：{p['score']:.0f}/100（权重 {p['weight']:.0%}）— {p['detail']}")
        L.append(f"  可承受最大账面回撤：{d['loss_tolerance_pct']:.0f}%｜"
                 f"据此推出的权益类上限：{d['equity_ceiling']*100:.0f}%")
        L.append(f"  {d['equity_ceiling_reason']}")
        L.append("")
        L.append("【负债成本】房贷利率 "
                 f"{d['mortgage_rate']*100:.2f}%，3 年期存款 {d['risk_free_rate']*100:.2f}%，"
                 f"利差 {d['mortgage_spread']*100:.2f} 个百分点 —— 这是「提前还款 vs 投资」的比较基准")
        L.append("")
        L.append("【保障】" +
                 ("有医保" if d["has_medical_insurance"] else "无医保") + "｜" +
                 ("有重疾险" if d["has_critical_illness"] else "无重疾险") + "｜"
                 f"寿险保额 {d['life_sum_assured']/10000:.1f} 万｜"
                 f"按「7 年支出 + 全部负债 − 保额 − 流动资产」测算的保障缺口 {d['insurance_gap']/10000:.1f} 万")
        if d["goals"]:
            L.append("")
            L.append("【理财目标】")
            for g in d["goals"]:
                share = (f"{g['share_of_savings']*100:.0f}% 的家庭年结余"
                         if g["share_of_savings"] is not None else "—")
                L.append(f"  - {g['name']}：目标 {g['amount']/10000:.1f} 万，{g['years']:.0f} 年后，"
                         f"优先级 {g['priority']}｜需年存 {g['required_annual_saving']/10000:.1f} 万"
                         f"（占 {share}）｜{'可行' if g['feasible'] else '当前结余不足以覆盖'}")
        return "\n".join(L)
