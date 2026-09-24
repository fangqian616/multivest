#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」多智能体阵容与提示词。

设计原则
--------
1. **数字接地**：所有财务数字与大类资产历史统计都由系统确定性提供并写进提示词，
   分析师只能引用、不得杜撰。这一条对应 TradingAgents v0.5 的 "价格接地"。
2. **角色互补而非重复**：五位分析师各自回答一个家庭配置中不可回避的问题
   （方向 / 定价 / 期限 / 净收益 / 承受力），不制造同质化的"多空双方"。
3. **可辩论**：每位分析师都必须给出能落到比例或动作上的立场，
   否则后续的立场量化（CV / Kendall's W）就没有可测的输入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# 配置大类（配置方案的作用对象 = 可投资金融资产）
# ─────────────────────────────────────────────────────────────────────────────

ALLOC_CLASSES: list[dict] = [
    {"key": "equity_cn", "label": "A股权益", "dataset_class": "equity_cn",
     "desc": "沪深300/中证500/红利等宽基与策略指数工具",
     "risk": "高", "liquidity": "T+1"},
    {"key": "equity_global", "label": "海外与港股权益", "dataset_class": "equity_global",
     "desc": "纳指/标普500/H股/恒生科技等 QDII 工具",
     "risk": "高", "liquidity": "T+1（QDII 有溢价风险）"},
    {"key": "bond", "label": "债券", "dataset_class": "bond",
     "desc": "国债ETF/十年国债ETF/可转债ETF",
     "risk": "中低", "liquidity": "T+1"},
    {"key": "gold", "label": "黄金", "dataset_class": "gold",
     "desc": "黄金ETF（华安/易方达）",
     "risk": "中高", "liquidity": "T+1"},
    {"key": "commodity", "label": "商品与另类", "dataset_class": "commodity",
     "desc": "有色金属等商品指数工具",
     "risk": "高", "liquidity": "T+1"},
    {"key": "cash", "label": "现金管理", "dataset_class": "cash",
     "desc": "货币ETF、同业存单、国债逆回购",
     "risk": "极低", "liquidity": "T+0/T+1"},
    {"key": "deposit", "label": "存款与银行理财", "dataset_class": None,
     "desc": "定期存款、大额存单、固收类银行理财",
     "risk": "极低-低", "liquidity": "约定期限"},
    {"key": "insurance", "label": "保险与养老金", "dataset_class": None,
     "desc": "个人养老金账户、储蓄型保险、年金",
     "risk": "低", "liquidity": "长期锁定"},
]

ALLOC_KEYS = [c["key"] for c in ALLOC_CLASSES]
CLASS_BY_KEY = {c["key"]: c for c in ALLOC_CLASSES}


# ─────────────────────────────────────────────────────────────────────────────
# 市场情境文本（给分析师的共同事实基础）
# ─────────────────────────────────────────────────────────────────────────────

def build_market_context(market, household_dict: Optional[dict] = None) -> str:
    """把内置数据集渲染成分析师可读的紧凑事实文本。

    分析师看到的每一个数字都来自这里，不允许自行编造。
    """
    L: list[str] = []
    L.append("【大类资产历史事实】（真实前复权日线，2018-06 至 2026-09，年化按 244 交易日折算）")
    L.append(f"{'资产类别':<12}{'代表工具':<16}{'年化收益':>9}{'年化波动':>9}"
             f"{'最大回撤':>10}{'夏普(无风险=0)':>14}")

    shown = 0
    for cls in ALLOC_CLASSES:
        dc = cls["dataset_class"]
        if not dc:
            continue
        syms = sorted([s for s in market.symbols.values() if s.asset_class == dc],
                      key=lambda s: -s.annual_return)
        # 每类最多展示 3 个代表，控制 token
        for s in syms[:3]:
            sharpe = s.stats.get("sharpe_rf0")
            L.append(f"{cls['label']:<12}{s.name:<16}"
                     f"{s.annual_return*100:>8.2f}%{s.annual_vol*100:>8.2f}%"
                     f"{s.max_drawdown*100:>9.2f}%"
                     f"{(f'{sharpe:.2f}' if sharpe is not None else '—'):>14}")
            shown += 1
    L.append("")

    L.append("【关键相关性】（日收益皮尔逊相关系数，用于判断分散化是否真实有效）")
    pairs = [
        ("sh000300", "sh000012", "沪深300 vs 上证国债"),
        ("sh000300", "sh518880", "沪深300 vs 黄金"),
        ("sh000300", "sh513500", "沪深300 vs 标普500ETF"),
        ("sh000300", "sh510900", "沪深300 vs H股ETF"),
        ("sh000300", "sh000832", "沪深300 vs 中证转债"),
        ("sh000300", "sh000922", "沪深300 vs 中证红利"),
        ("sh000012", "sh518880", "上证国债 vs 黄金"),
    ]
    for a, b, label in pairs:
        if a in market.symbols and b in market.symbols:
            L.append(f"  - {label}：{market.correlation(a, b):+.2f}")
    L.append("  （负相关或低相关才提供真实分散化；A股与港股/美股的高相关意味着"
             "「出海」不等于「分散」）")
    L.append("")

    L.append("【无连续行情类资产的基准假设】（人工维护，非实时报价，仅供测算）")
    rt = market.rate_table
    L.append(f"  - 1 年期定期存款 {rt.get('deposit_1y', 0)*100:.2f}%｜"
             f"3 年期定期存款 {rt.get('deposit_3y', 0)*100:.2f}%")
    L.append(f"  - 货币基金 7 日年化中枢 {rt.get('money_fund', 0)*100:.2f}%｜"
             f"1 年期银行理财业绩基准中枢 {rt.get('bank_wealth_1y', 0)*100:.2f}%"
             f"（净值波动约 {rt.get('bank_wealth_vol', 0)*100:.2f}%）")
    L.append(f"  - 商业养老年金 IRR 中枢 {rt.get('annuity_irr', 0)*100:.2f}%｜"
             f"CPI 同比中枢 {rt.get('inflation_cpi', 0)*100:.2f}%")
    L.append(f"  - 5 年期以上 LPR（房贷基准）{rt.get('mortgage_lpr_5y', 0)*100:.2f}%")
    L.append("")

    L.append("【压力情景】（用真实历史窗口回放，非假设参数）")
    from data.market import MARKET_SCENARIOS
    for sc in MARKET_SCENARIOS:
        L.append(f"  - {sc.name}（{sc.start} ~ {sc.end}）：{sc.note}")
    L.append("")

    if household_dict:
        L.append("【配置的作用对象】")
        L.append(f"  本方案只对**可投资金融资产**（剔除自住房）做配置，"
                 f"规模约 {household_dict.get('investable_assets', 0)/10000:.1f} 万元；"
                 f"自住房 {household_dict.get('assets', {}).get('property_self_use', 0)/10000:.1f} 万元"
                 f"不作为可配置资产。")
        L.append(f"  家庭另有负债 {household_dict.get('total_liabilities', 0)/10000:.1f} 万元，"
                 f"房贷利率 {household_dict.get('mortgage_rate', 0)*100:.2f}%。"
                 f"「是否提前还款」是配置问题的一部分，必须在方案中给出结论。")

    return "\n".join(L)


def render_transcript(speeches: dict[str, dict], exclude: Optional[str] = None,
                      max_chars_per_speech: int = 2200) -> str:
    """把本轮/历轮发言渲染成紧凑文本。"""
    lines = []
    for aid, sp in speeches.items():
        if aid == exclude:
            continue
        text = (sp.get("text") or "").strip()
        if len(text) > max_chars_per_speech:
            text = text[:max_chars_per_speech] + "…（略）"
        lines.append(f"── {sp.get('name', aid)} ──\n{text}\n")
    return "\n".join(lines) if lines else "（本轮暂无其他分析师发言）"


# ─────────────────────────────────────────────────────────────────────────────
# 分析师定义
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentSpec:
    id: str
    name: str
    role: str
    duty: str
    output_spec: str
    temperature: float = 0.6
    max_tokens: int = 1600
    phase: str = "analyst"          # analyst / risk / committee
    emoji: str = ""


COMMON_RULES = """\
【共同规则 —— 必须遵守】
1. 上面给出的家庭财务数字与资产历史统计是**既定事实**，你只能引用，不得修改或杜撰新数字。
   需要新数字时，明确写出你的推算过程与假设。
2. 你的每一条主张都必须**可判断支持/反对**，且能落到具体的比例、金额或动作上。
   不要输出"要注意风险""建议多元化"这类无法被检验的话。
3. 不得回避分歧。如果你与其他分析师意见不同，直接点名说明分歧在哪、你的依据是什么。
4. 不要写客套话、不要复述任务、不要总结别人的发言。直接给出你的专业判断。
5. 结尾用一行「本轮核心主张：」列出你希望写进议题清单的 1-2 条主张。
"""


AGENTS: list[AgentSpec] = [
    AgentSpec(
        id="macro",
        name="宏观与利率分析师",
        role="宏观与利率分析师",
        emoji="🌐",
        duty=(
            "判断当前所处的宏观与利率环境，给出大类资产的**方向性倾斜**："
            "哪些类别应当超配、哪些应当低配，以及理由。"
            "你必须回答的问题：\n"
            "  (a) 当前利率处于什么位置？这对债券久期、存款与理财的相对吸引力意味着什么？\n"
            "  (b) 通胀与汇率环境如何影响黄金、海外资产对人民币投资者的实际回报？\n"
            "  (c) 房地产周期的位置如何影响该家庭的房产敞口与房贷决策？\n"
            "  (d) 在这样的宏观环境下，一个大类资产的中枢配置应该相对标准配置（权益 30% / "
            "债券 35% / 黄金 8% / 现金及存款 22% / 其他 5%）向哪个方向偏离多少？"
        ),
        output_spec=(
            "输出结构：\n"
            "① 宏观定位（2-3 句，必须给出你的判断而非罗列新闻）\n"
            "② 各大类倾斜建议（逐条：类别 → 超配/标配/低配 → 相对标准配置的偏离百分点 → 依据）\n"
            "③ 对本家庭的具体含义（把宏观判断翻译成这个家庭该做什么）"
        ),
    ),
    AgentSpec(
        id="valuation",
        name="大类资产估值与定价分析师",
        role="大类资产估值与定价分析师",
        emoji="📊",
        duty=(
            "基于上面给出的**真实历史统计**（年化收益、波动率、最大回撤、相关系数），"
            "给出各资产类别的前瞻预期收益与风险估计，并评估组合层面的分散化效果。"
            "你必须回答的问题：\n"
            "  (a) 历史年化收益哪些可信、哪些是特定周期的产物？给出你调整后的前瞻预期收益。\n"
            "  (b) 相关性数据说明「分散化」在这个资产池里哪些是真、哪些是假？\n"
            "  (c) 各资产类别的合理权重区间是多少？给出区间上下限而不只是点估计。\n"
            "  (d) 用给定数据算一个参考组合（例如权益 30%/债券 35%/黄金 8%/现金及存款 27%）"
            "的历史年化、波动与最大回撤，说明它是否落在该家庭的承受范围内。"
        ),
        output_spec=(
            "输出结构：\n"
            "① 前瞻预期收益表（逐类：类别 → 前瞻名义年化 → 前瞻波动 → 与历史值的偏离及原因）\n"
            "② 分散化评估（哪些相关性是假分散，点名说明）\n"
            "③ 权重区间（逐类给出下限-上限，合计中值应接近 100%）"
        ),
    ),
    AgentSpec(
        id="lifecycle",
        name="家庭生命周期与负债分析师",
        role="家庭生命周期与负债分析师",
        emoji="🏠",
        duty=(
            "从家庭的现金流、期限结构、负债成本与人生阶段出发，判断配置的**期限匹配**。"
            "你必须回答的问题：\n"
            "  (a) 「用结余提前偿还房贷」还是「拿去投资」？给出明确结论与比较基准"
            "（房贷利率 vs 你能稳定实现的税后收益率），并说明这个结论对风险偏好的依赖。\n"
            "  (b) 家庭各目标（教育金、养老金、购房等）应如何按**期限分层**配置？"
            "哪些目标必须用低波动资产锁定，不得配置权益？\n"
            "  (c) 应急储备是否需要补足？补足的钱应该从哪一类资产来？\n"
            "  (d) 保险缺口是否应当在配置之前解决？如果是，给出优先顺序与预算来源。"
        ),
        output_spec=(
            "输出结构：\n"
            "① 期限分层方案（短期 0-3 年 / 中期 3-10 年 / 长期 10 年以上，各自对应的资产与占比）\n"
            "② 房贷决策结论（明确写「提前还款」或「不提前还款」，并给出临界利率）\n"
            "③ 保障与应急金的处理顺序"
        ),
    ),
    AgentSpec(
        id="taxfee",
        name="税务、费用与工具落地分析师",
        role="税务、费用与工具落地分析师",
        emoji="🧾",
        duty=(
            "关注**净收益**而非名义收益：账户层级、税费、产品费率与流动性摩擦。"
            "你必须回答的问题：\n"
            "  (a) 个人养老金账户的税优额度是否值得用满？对这个家庭的实际节税金额是多少？"
            "（按家庭适用税率估算，写明假设）\n"
            "  (b) 同样暴露于沪深300，不同工具（场内ETF / 场外指数基金 / 银行理财 / "
            "主动基金）的费率与跟踪误差差异有多大？一年吃掉多少收益？\n"
            "  (c) QDII 工具的溢价风险与额度限制如何影响「海外配置」的可执行性？\n"
            "  (d) 银行理财的「业绩比较基准」与「实际到手收益」之间有哪些隐性成本？"
        ),
        output_spec=(
            "输出结构：\n"
            "① 账户层级建议（先放哪个账户、放什么、为什么）\n"
            "② 费用拖累测算（给出年化费用百分点，说明它如何改变配置结论）\n"
            "③ 具体工具类型建议（指明工具类别与选择标准，不必给出具体产品代码）"
        ),
    ),
    AgentSpec(
        id="behavioral",
        name="行为金融与风险承受力分析师",
        role="行为金融与风险承受力分析师",
        emoji="🧠",
        duty=(
            "校准配置方案与该家庭**真实承受力**的匹配度，并设计防止行为失控的机制。"
            "这是唯一有权直接否决配置比例的角色。你必须回答的问题：\n"
            "  (a) 系统的客观承受能力评分与家庭主观风险偏好是否背离？背离了该听谁的？\n"
            "  (b) 按当前方案的权益比例，在历史最差情景下这个家庭会看到多少账面亏损"
            "（换算成具体金额）？他们真的能扛住吗？\n"
            "  (c) 这个家庭最可能犯的行为错误是什么（追涨、杀跌、频繁交易、听消息）？"
            "针对每一种给出制度化的防线。\n"
            "  (d) 你主张的权益类上限是多少？如果与其他人不同，明确指出分歧。"
        ),
        output_spec=(
            "输出结构：\n"
            "① 承受力诊断（客观评分 vs 主观偏好，背离方向与处理原则）\n"
            "② 最差情景的金额化描述（把百分比翻译成「账面上会少多少钱」）\n"
            "③ 行为防线清单（每条必须是可执行的制度，如「设置自动再平衡，每年只看一次账户」）\n"
            "④ 你主张的权益上限（一个明确数字）"
        ),
    ),
]


RISK_AGENT = AgentSpec(
    id="risk",
    name="风控委员会",
    role="风控委员会",
    emoji="🛡️",
    phase="risk",
    temperature=0.4,
    max_tokens=1800,
    duty=(
        "对审议形成的配置方案做**对抗性审查**。你的职责不是同意，而是找出它会在什么情况下失败。"
        "你必须逐项完成：\n"
        "  1. 压力测试解读：把系统给出的每个历史情景损失，换算成该家庭的**具体金额**，"
        "并判断是否击穿应急金、是否触发被迫卖出。\n"
        "  2. 集中度检查：单一资产/单一市场/单一币种的最大敞口是否过高？"
        "特别检查「海外配置」是否真的分散（看相关系数）。\n"
        "  3. 流动性检查：若家庭在未来 12 个月内需要用钱（失业/医疗/教育），"
        "现有流动性分层能否覆盖而不被迫卖出权益？\n"
        "  4. 杠杆与负债交互：方案是否隐含「用借来的钱投资」？房贷与投资的现金流是否冲突？\n"
        "  5. 给出结论：**通过** / **有条件通过**（必须列出强制约束条件）/ **否决**（说明必须改什么）。"
    ),
    output_spec=(
        "输出结构：\n"
        "① 压力测试解读（逐情景：损失金额 → 是否击穿应急金 → 家庭是否会被迫卖出）\n"
        "② 三项检查结论（集中度 / 流动性 / 杠杆）\n"
        "③ 最终结论：通过 / 有条件通过 / 否决\n"
        "④ 若为有条件通过或否决，用「强制约束：」开头逐条列出不可协商的条件（含具体比例或金额上限）"
    ),
)


COMMITTEE_AGENT = AgentSpec(
    id="committee",
    name="配置委员会",
    role="资产配置委员会主席",
    emoji="⚖️",
    phase="committee",
    temperature=0.3,
    max_tokens=8000,
    duty=(
        "综合全部辩论、收敛结果与风控委员会意见，做出最终裁决并产出可执行的配置方案。"
        "你的职责：\n"
        "  1. 对**已经收敛**的主张采纳共识值。\n"
        "  2. 对**高位僵持/未收敛**的主张，你必须显式裁决——说明你采纳哪一方、依据是什么、"
        "以及你为什么认为另一方的顾虑不构成否决理由。不得回避分歧、不得和稀泥。\n"
        "  3. 严格遵守风控委员会的强制约束（若为否决则必须修改方案直至满足）。\n"
        "  4. 产出的权重必须合计 100%，且落在每位分析师给出的合理区间内（越界需说明理由）。"
    ),
    output_spec=(
        "**直接输出一个 JSON 对象**。不要输出任何前言、分析过程、标题或代码块标记——"
        "你所有的判断都必须放进 JSON 的字段里（class_rationale 放逐类理由，"
        "deadlock_rulings 放逐条分歧裁决，summary 放给家庭的总结）。"
        "输出被截断会导致方案作废，因此务必精炼。"
    ),
)


COMMITTEE_JSON_SCHEMA = """\
必须输出这样的 JSON 对象（键名与层级完全一致）：

{
  "target_weights": {
    "equity_cn": 0.25, "equity_global": 0.08, "bond": 0.32, "gold": 0.07,
    "commodity": 0.00, "cash": 0.12, "deposit": 0.11, "insurance": 0.05
  },
  "ranges": {
    "equity_cn": [0.20, 0.30], "bond": [0.27, 0.37]
  },
  "class_rationale": {
    "equity_cn": "为什么是这个权重（一句话，引用辩论中的具体论据）"
  },
  "rebalance": {
    "rule": "具体的再平衡规则（必须给出）",
    "threshold": 0.05,
    "calendar": "每半年检查一次",
    "note": "执行注意事项"
  },
  "actions": [
    {"when": "本月", "what": "具体动作", "why": "依据", "amount": 30000}
  ],
  "deadlock_rulings": [
    {"argument_id": "P1", "argument_text": "主张原文",
     "ruling": "采纳/部分采纳/不采纳", "reason": "你的裁决理由"}
  ],
  "risk_notes": ["必须向家庭明示的风险点"],
  "summary": "给这个家庭的一段话总结（150 字以内，直接对他们说）"
}

硬性要求：
- target_weights 必须包含上面全部 8 个键（未配置的写 0），且合计 = 1.0（±0.005）。
- deadlock_rulings 必须**逐条覆盖**收敛报告中所有 state 为 deadlocked 或 partial 的主张，
  一条都不能漏；缺失任何一条都会导致方案被质量门禁驳回。
- **每条 reason / class_rationale 控制在 60 字以内**。输出有长度上限，写太长会导致
  JSON 被截断、整个方案作废，必须精炼。
- rebalance.rule 不能为空。
"""


# ─────────────────────────────────────────────────────────────────────────────
# 提示词组装
# ─────────────────────────────────────────────────────────────────────────────

ANALYST_PROMPT = """\
你是「智能多维投资系统」家庭资产配置审议委员会的{role}。

【你的职责】
{duty}

【家庭财务事实】（由系统确定性计算，你不得改动这些数字）
{profile_text}

{market_context}

{history_block}

{peers_block}

【你的任务】
{output_spec}

{stance_block}

{common_rules}
"""


def build_analyst_prompt(
    spec: AgentSpec,
    profile_text: str,
    market_context: str,
    history_block: str = "",
    peers_block: str = "",
    stance_block: str = "",
) -> str:
    return ANALYST_PROMPT.format(
        role=spec.role,
        duty=spec.duty,
        profile_text=profile_text,
        market_context=market_context,
        history_block=history_block,
        peers_block=peers_block,
        output_spec=spec.output_spec,
        stance_block=stance_block,
        common_rules=COMMON_RULES,
    )


RISK_PROMPT = """\
你是「智能多维投资系统」审议流程的{role}。研究员团队已经完成辩论，形成了下面这份配置方案。
你的职责是**对抗性审查**——不是同意它，而是找出它会在什么情况下让这个家庭受伤。

【你的职责】
{duty}

【家庭财务事实】
{profile_text}

{market_context}

【辩论收敛结果】
{convergence_block}

【待审查的配置方案】
{proposal_block}

【系统压力测试结果】（基于真实历史窗口回放，数字由系统计算，你只能引用）
{stress_block}

【风险预算】（各资产的波动贡献，用于识别"权重小但风险大"的隐藏敞口）
{risk_budget_block}

【你的任务】
{output_spec}

{common_rules}
"""


def build_risk_prompt(
    profile_text: str,
    market_context: str,
    convergence_block: str,
    proposal_block: str,
    stress_block: str,
    risk_budget_block: str,
) -> str:
    return RISK_PROMPT.format(
        role=RISK_AGENT.role,
        duty=RISK_AGENT.duty,
        profile_text=profile_text,
        market_context=market_context,
        convergence_block=convergence_block,
        proposal_block=proposal_block,
        stress_block=stress_block,
        risk_budget_block=risk_budget_block,
        output_spec=RISK_AGENT.output_spec,
        common_rules=COMMON_RULES,
    )


COMMITTEE_PROMPT = """\
你是「智能多维投资系统」审议流程的{role}。辩论已经结束，风控委员会已经出具意见，现在由你做最终裁决。

【你的职责】
{duty}

【家庭财务事实】
{profile_text}

{market_context}

【辩论收敛报告】
{convergence_block}

【全部辩论发言摘要】
{transcript_block}

【风控委员会意见】
{risk_block}

【系统压力测试结果】
{stress_block}

【可配置的大类资产】
{classes_block}

【产出格式】
{output_spec}

{json_schema}

注意：
- 你的输出会被程序直接解析。**请只输出 JSON 对象本身**，不要 markdown 代码块标记（不要 ```），
  不要标题，不要前言或后记。任何 JSON 之外的文字都可能导致方案解析失败。
- target_weights 的键必须恰好是上面列出的大类 key，值合计必须等于 1.0（允许 ±0.005 误差）。
- 未配置的大类请显式写 0，不要省略键。
- 每个大类必须在 class_rationale 里给出一句话理由，且理由要引用辩论中的具体论据。
- deadlock_rulings 必须**逐条覆盖**收敛报告中所有 state 为 deadlocked 或 partial 的主张。
- 全文控制在 2500 字以内，务必在长度内把 JSON 闭合完整。
"""


def build_committee_prompt(
    profile_text: str,
    market_context: str,
    convergence_block: str,
    transcript_block: str,
    risk_block: str,
    stress_block: str,
    classes_block: str,
) -> str:
    return COMMITTEE_PROMPT.format(
        role=COMMITTEE_AGENT.role,
        duty=COMMITTEE_AGENT.duty,
        profile_text=profile_text,
        market_context=market_context,
        convergence_block=convergence_block,
        transcript_block=transcript_block,
        risk_block=risk_block,
        stress_block=stress_block,
        classes_block=classes_block,
        output_spec=COMMITTEE_AGENT.output_spec,
        json_schema=COMMITTEE_JSON_SCHEMA,
        common_rules=COMMON_RULES,
    )


def all_analyst_ids() -> list[str]:
    return [a.id for a in AGENTS]


COMMITTEE_REPAIR_PROMPT = """\
你此前为「智能多维投资系统」家庭资产配置审议产出最终方案时，输出无法被程序解析
（最可能是长度超限被截断，JSON 没有闭合）。

现在请**重新输出**，并且**只输出一个 JSON 对象**——不要任何其他文字，不要 markdown 代码块标记。

【家庭财务事实】
{profile_text}

{convergence_block}

【风控委员会意见摘要（你必须遵守其中的强制约束）】
{risk_block}

【可配置的大类（键名必须完全一致）】
{classes_block}

【必须逐条裁决的未收敛主张】
{must_rule}

【输出格式】
{json_schema}

再次强调：只输出 JSON 本身，全文控制在 2000 字以内，确保 JSON 完整闭合。
"""


def build_committee_repair_prompt(profile_text: str, convergence_block: str,
                                  risk_block: str, classes_block: str,
                                  must_rule: list[str]) -> str:
    return COMMITTEE_REPAIR_PROMPT.format(
        profile_text=profile_text,
        convergence_block=convergence_block,
        risk_block=(risk_block or "")[:3000],
        classes_block=classes_block,
        must_rule="、".join(must_rule) if must_rule else "（无）",
        json_schema=COMMITTEE_JSON_SCHEMA,
    )


def agent_by_id(aid: str) -> Optional[AgentSpec]:
    for a in AGENTS:
        if a.id == aid:
            return a
    if aid == RISK_AGENT.id:
        return RISK_AGENT
    if aid == COMMITTEE_AGENT.id:
        return COMMITTEE_AGENT
    return None
