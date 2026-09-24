#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」每晚研判引擎。

每个交易日收盘后自动运行：采集当日行情 → 计算确定性市场指标 → 多智能体研判
→ 产出可视化看板 + 投资建议判断。

设计原则（与家庭配置模块一致）
------------------------------
**确定性指标由系统算，判断交给模型。**
动量、资金、情绪、风险四个维度的分数完全由行情数据推导，模型不得改动；
模型只负责「宏观」维度与对四个客观分数的**解读与调整理由**，
以及大类观点、关键分歧、行动建议。

这样做的理由：每天跑一次的系统，如果每次的分数都不同，用户就无法建立任何直觉。
客观分数稳定可复现，主观判断才允许有观点。

对外产物
--------
    <data_dir>/daily/YYYY-MM-DD.json     当日研判（前端直接渲染）
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional

from data.realtime import BENCHMARKS, RealtimeFeed, build_board, market_status
from engine.llm import LLMClient, LLMError, extract_json
from engine.stance import StanceTracker

logger = logging.getLogger(__name__)

Emit = Callable[[dict], None]

# 六大研判维度（雷达图轴）
DIMENSIONS = ["估值", "动量", "资金", "情绪", "宏观", "风险"]

# 大类观点档位
VIEW_LEVELS = ["超配", "标配", "低配"]

ALLOC_LABELS = {
    "equity_cn": "A股权益", "equity_global": "海外与港股权益", "bond": "债券",
    "gold": "黄金", "commodity": "商品与另类", "cash": "现金管理",
    "deposit": "存款与理财", "insurance": "保险与养老金",
}


# ─────────────────────────────────────────────────────────────────────────────
# 一、确定性市场指标（不调用 LLM）
# ─────────────────────────────────────────────────────────────────────────────

def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) if b else 0.0


def compute_market_metrics(market, board: dict) -> dict:
    """从内置数据集 + 当日实时快照推导客观指标与四个维度分数。

    所有分数都映射到 0-100，50 为中性。
    """
    universe = {r["code"]: r for r in board.get("universe", [])}
    per_symbol: list[dict] = []

    for code, sym in market.symbols.items():
        bars = (market._history.get(code) or {}).get("bars") or []
        closes = [float(b["c"]) for b in bars]
        if len(closes) < 25:
            continue
        rt = universe.get(code) or {}
        last = rt.get("price") or closes[-1]
        prev = rt.get("prev_close") or closes[-2]

        def ret(n: int) -> float:
            if len(closes) <= n:
                return 0.0
            return _pct(closes[-1], closes[-1 - n])

        hi = max(closes[-244:]) if len(closes) >= 20 else max(closes)
        lo = min(closes[-244:]) if len(closes) >= 20 else min(closes)
        daily = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        vol20 = (statistics.pstdev(daily[-20:]) * math.sqrt(244)) if len(daily) >= 20 else 0.0
        vol60 = (statistics.pstdev(daily[-60:]) * math.sqrt(244)) if len(daily) >= 60 else vol20

        per_symbol.append({
            "code": code, "name": sym.name, "asset_class": sym.asset_class,
            "price": round(last, 4),
            "d1": round(rt.get("change_pct", 0.0), 6),
            "d5": round(ret(5), 6), "d20": round(ret(20), 6), "d60": round(ret(60), 6),
            "vol20": round(vol20, 4), "vol60": round(vol60, 4),
            "vol_regime": round(vol20 / vol60, 3) if vol60 > 1e-9 else 1.0,
            "from_high": round(_pct(last, hi), 4),
            "from_low": round(_pct(last, lo), 4),
            "amount": rt.get("amount", 0.0),
        })

    # ── 大盘聚合 ──────────────────────────────────────────────────────────
    eq = [s for s in per_symbol if s["asset_class"] in ("equity_cn", "equity_global")]
    allx = per_symbol
    d1s = [s["d1"] for s in allx] or [0.0]

    breadth = board.get("breadth", {})
    avg1 = statistics.fmean(d1s)
    avg20 = statistics.fmean([s["d20"] for s in eq]) if eq else 0.0
    avg60 = statistics.fmean([s["d60"] for s in eq]) if eq else 0.0
    vol_regime = statistics.fmean([s["vol_regime"] for s in eq]) if eq else 1.0
    above_ma20 = sum(1 for s in allx if s["price"] > 0 and s["d20"] > 0)
    breadth20 = above_ma20 / len(allx) if allx else 0.5

    # ── 四维客观评分（0-100，50 中性）────────────────────────────────────
    def clamp(x: float) -> float:
        return max(0.0, min(100.0, x))

    # 动量：中期趋势为主，短期为辅
    momentum = clamp(50 + (avg20 * 100) * 1.6 + (avg60 * 100) * 0.8 + (avg1 * 100) * 2.0)
    # 资金：上涨家数占比 + 当日均涨跌
    capital = clamp(50 + (breadth.get("up_ratio", 0.5) - 0.5) * 90
                    + (breadth.get("avg_change", 0.0) * 100) * 6)
    # 情绪：中期宽度 + 距一年高点的位置（越接近高点越乐观）
    near_high = statistics.fmean([1 + s["from_high"] for s in allx]) if allx else 0.5
    sentiment = clamp(50 + (breadth20 - 0.5) * 70 + (near_high - 0.85) * 60)
    # 风险：波动率抬升则风险高，即该维度得分低。
    # 语义统一为「分高＝这个维度对投资有利」：风险维度分高 = 风险环境可控。
    # 基准 62、系数克制，避免像 78-90Δ 那样一动就顶到 100（饱和值没有信息量）。
    risk = clamp(62 - (vol_regime - 1.0) * 55 - abs(avg1 * 100) * 1.5)

    # 估值：用「距一年低点 / 高低区间」的百分位近似（无 PE 全样本，故用价格分位）
    pos = []
    for s in allx:
        rng = s["from_low"] - s["from_high"]
        pos.append((s["from_low"] / rng) if rng > 1e-9 else 0.5)
    valuation_pct = statistics.fmean(pos) if pos else 0.5
    # 价格分位越高 → 越贵 → 估值维度分越低
    valuation = clamp(100 - valuation_pct * 100)

    objective = {"估值": round(valuation, 1), "动量": round(momentum, 1),
                 "资金": round(capital, 1), "情绪": round(sentiment, 1),
                 "风险": round(risk, 1)}

    return {
        "per_symbol": sorted(per_symbol, key=lambda s: -s["d1"]),
        "aggregate": {
            "avg_d1": round(avg1, 6),
            "avg_d20_equity": round(avg20, 6),
            "avg_d60_equity": round(avg60, 6),
            "breadth20": round(breadth20, 4),
            "vol_regime": round(vol_regime, 4),
            "valuation_percentile": round(valuation_pct, 4),
            "n_symbols": len(allx),
        },
        "objective_scores": objective,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 二、多智能体阵容（当日研判）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReviewAgent:
    id: str
    name: str
    duty: str
    output: str
    temperature: float = 0.6
    max_tokens: int = 1400
    tint: str = "#D92B2B"


REVIEW_AGENTS: list[ReviewAgent] = [
    ReviewAgent(
        id="technical", name="技术面分析师", tint="#D92B2B",
        duty=("解读当日与近期的价格结构。必须回答：\n"
              "  (a) 各主要指数当前处于什么趋势结构（上行/震荡/下行），关键支撑与阻力在哪里？\n"
              "  (b) 今日的涨跌是趋势的延续还是反转信号？依据是什么？\n"
              "  (c) 动量与波动率的关系说明了什么（放量突破 / 缩量反弹 / 恐慌抛售）？"),
        output=("① 趋势结构判断（逐指数：结构 + 关键位 + 一句话依据）\n"
                "② 今日走势的性质（延续/反转/噪音）与依据\n"
                "③ 明日需要盯住的技术位（给出具体点位或百分比）"),
    ),
    ReviewAgent(
        id="flow", name="资金面分析师", tint="#C8A02E",
        duty=("从成交额、上涨家数、量价配合判断资金动向。必须回答：\n"
              "  (a) 今日资金是净流入还是净流出？哪些大类在吸金、哪些在失血？\n"
              "  (b) 上涨的标的有没有量能配合？有没有「指数涨但个股不涨」的背离？\n"
              "  (c) 这种资金结构通常领先价格多久、指向什么？"),
        output=("① 资金流向判断（按大类：流入/流出/持平 + 依据）\n"
                "② 量价配合体检（指出至少一处背离或确认）\n"
                "③ 资金结构对未来 1-2 周的指向"),
    ),
    ReviewAgent(
        id="macro", name="宏观与政策分析师", tint="#2563EB",
        duty=("把当日行情放进宏观背景里解释，并识别外部变量。必须回答：\n"
              "  (a) 今日走势能否用宏观因素解释？如果只是情绪，说明这一点。\n"
              "  (b) 利率、汇率、海外市场（美股/港股）当前对 A 股是助力还是拖累？\n"
              "  (c) 未来一周有哪些宏观事件或政策窗口需要提前防御？"),
        output=("① 宏观归因（今日走势的宏观解释力有多强）\n"
                "② 外部变量清单（利率/汇率/海外/商品，逐项给出方向）\n"
                "③ 一周内的宏观日程与应对"),
    ),
    ReviewAgent(
        id="valuation", name="估值与性价比分析师", tint="#0B8A5C",
        duty=("判断各大类资产当前的相对性价比。必须回答：\n"
              "  (a) 用给出的价格分位数据，哪些资产处在历史区间的便宜端、哪些在贵端？\n"
              "  (b) 股债性价比（权益 vs 债券）当前偏向哪一边？\n"
              "  (c) 如果只能加一类资产、减一类资产，你选哪两类？给出比例建议。"),
        output=("① 各类资产估值分位表（便宜/中性/贵，逐类给出）\n"
                "② 股债性价比判断\n"
                "③ 加/减仓建议（具体类别 + 方向 + 理由）"),
    ),
    ReviewAgent(
        id="risk", name="风险与仓位分析师", tint="#7C3AED",
        duty=("这是有权直接下调建议仓位的角色。必须回答：\n"
              "  (a) 当前波动率环境相对过去 60 天是抬升还是收敛？对仓位意味着什么？\n"
              "  (b) 最坏情况下，如果出现类似历史级别的回撤，各类资产会跌多少？\n"
              "  (c) 你主张的总仓位是多少（0-100%）？如果与其他人不同，明确说明分歧。"),
        output=("① 波动率环境与仓位含义\n"
                "② 尾部风险情景（给出具体跌幅与触发条件）\n"
                "③ 你主张的总仓位（一个明确数字）+ 止损或减仓触发条件"),
    ),
]


REVIEW_OFFICER = ReviewAgent(
    id="officer", name="首席研判官", tint="#1B2A4A", temperature=0.3, max_tokens=4000,
    duty=("综合全部研判意见与收敛结果，产出当日研判看板与投资建议。"),
    output="必须输出一个 JSON 对象（格式见下）。",
)


COMMON_RULES = """\
【共同规则】
1. 上面给出的行情数据与客观分数是**既定事实**，只能引用，不得改动或杜撰新数字。
   需要新数字时写出推算过程与假设。
2. 每条判断必须可证伪：给出依据、给出可观察的验证条件。不要写"注意风险""保持谨慎"这类无法检验的话。
3. 不与其他人重复。如果你的判断与别人冲突，直接点名说明分歧在哪。
4. 不要客套话、不要复述任务。直接给专业判断。
"""


REVIEW_JSON_SCHEMA = """\
{
  "summary": "给用户的一段话当日总结（120 字以内）",
  "scores": {
    "估值": {"adjust": 0, "reason": "为什么接受或调整系统给的分"},
    "动量": {"adjust": 0, "reason": "..."},
    "资金": {"adjust": 0, "reason": "..."},
    "情绪": {"adjust": 0, "reason": "..."},
    "宏观": {"score": 55, "reason": "宏观维度系统不给分，由你判定"},
    "风险": {"adjust": 0, "reason": "..."}
  },
  "stance": {
    "equity_cn": {"view": "标配", "conviction": 0.55, "reason": "一句话依据"},
    "equity_global": {"view": "超配", "conviction": 0.6, "reason": "..."},
    "bond": {"view": "标配", "conviction": 0.5, "reason": "..."},
    "gold": {"view": "标配", "conviction": 0.5, "reason": "..."},
    "commodity": {"view": "低配", "conviction": 0.5, "reason": "..."},
    "cash": {"view": "标配", "conviction": 0.5, "reason": "..."}
  },
  "position": {
    "suggested": 0.6,
    "range": [0.5, 0.7],
    "reason": "仓位建议的依据"
  },
  "disagreements": [
    {"topic": "争点", "bull": "看多方的理由", "bear": "看空方的理由",
     "resolution": "你的裁决与理由"}
  ],
  "actions": [
    {"priority": "高", "action": "具体动作", "trigger": "触发条件", "reason": "依据"}
  ],
  "risks": ["必须明示的风险点"],
  "watchlist": ["明日需要盯住的具体指标或点位"]
}
"""


# 研判各阶段的进度权重（合计 100）。
# 各阶段耗时差异很大 —— 采集行情几秒、辩论占大头，所以按实测耗时分布配权，
# 而不是平均分。前端据此画进度条与阶段条，服务端负责累积。
STEP_WEIGHTS: list[tuple[str, str, float]] = [
    ("collect",  "采集行情",   6.0),
    ("analysts", "分析师研判", 30.0),
    ("topics",   "提炼争点",   6.0),
    ("debate",   "交叉辩论",   40.0),
    ("officer",  "首席研判官", 14.0),
    ("finalize", "落盘",       4.0),
]
STEP_LABELS: list[list[str]] = [[k, lbl] for k, lbl, _w in STEP_WEIGHTS]
_W: dict[str, float] = {k: w for k, _l, w in STEP_WEIGHTS}


def _fmt_pct(v: float, d: int = 2) -> str:
    return f"{v * 100:+.{d}f}%"


def build_market_block(board: dict, metrics: dict) -> str:
    """把当日行情渲染成分析师可读的紧凑事实文本。"""
    L: list[str] = []
    st = board.get("status", {}).get("market", {})
    L.append(f"【市场状态】{st.get('label', '—')}　数据时间 {board.get('server_time', '')}"
             + ("　⚠ 上游不可用，以下为内置数据集收盘价" if board.get("stale") else ""))
    L.append("")

    L.append("【核心指数当日表现】")
    for g in board.get("groups", []):
        for it in g["items"]:
            L.append(f"  {it['label']:<12}{it['price']:>11.2f}  {_fmt_pct(it['change_pct']):>8}"
                     f"  高 {it['high']:.2f}  低 {it['low']:.2f}"
                     f"  额 {(it['amount'] / 1e8):.1f}亿")
        L.append("")

    b = board.get("breadth", {})
    L.append(f"【市场宽度】上涨 {b.get('up', 0)} / 下跌 {b.get('down', 0)} / "
             f"平盘 {b.get('flat', 0)}（共 {b.get('total', 0)}）"
             f"　上涨占比 {b.get('up_ratio', 0):.0%}　平均涨跌 {_fmt_pct(b.get('avg_change', 0))}")
    L.append("")

    L.append("【大类资产当日表现】")
    for c in board.get("by_class", []):
        L.append(f"  {ALLOC_LABELS.get(c['asset_class'], c['asset_class']):<14}"
                 f"{_fmt_pct(c['change_pct']):>8}　(样本 {c['n']}，涨 {c['up']} 跌 {c['down']})")
    L.append("")

    ag = metrics["aggregate"]
    L.append("【客观指标】")
    L.append(f"  权益类 20 日平均 {_fmt_pct(ag['avg_d20_equity'])}，"
             f"60 日平均 {_fmt_pct(ag['avg_d60_equity'])}")
    L.append(f"  20 日宽度（价格高于 20 日前的比例）{ag['breadth20']:.0%}")
    L.append(f"  波动率环境（20日/60日）{ag['vol_regime']:.2f}"
             f"（>1 表示波动抬升，<1 表示收敛）")
    L.append(f"  价格分位（0=一年最低，1=一年最高）{ag['valuation_percentile']:.2f}")
    L.append("")

    L.append("【系统计算的四维客观分】（0-100，50 为中性；风险维度分高＝环境健康）")
    for k, v in metrics["objective_scores"].items():
        L.append(f"  {k}：{v:.0f}")
    L.append("")

    L.append("【各标的动量表】（1日 / 5日 / 20日 / 60日涨跌，距一年高点）")
    for s in metrics["per_symbol"]:
        L.append(f"  {s['name']:<14}{_fmt_pct(s['d1']):>8}{_fmt_pct(s['d5']):>8}"
                 f"{_fmt_pct(s['d20']):>8}{_fmt_pct(s['d60']):>8}"
                 f"　距高 {_fmt_pct(s['from_high']):>8}")
    return "\n".join(L)


REVIEW_PROMPT = """\
你是「智能多维投资系统」的{name}，正在参加**每交易日收盘后的研判会**。

【你的职责】
{duty}

{market_block}

【你的任务】
{output}

{common_rules}
"""

OFFICER_PROMPT = """\
你是「智能多维投资系统」的{name}。五位分析师已完成今日研判，现在由你产出最终看板。

【你的职责】
{duty}

{market_block}

【辩论收敛报告】
{convergence_block}

【全部研判意见】
{transcript_block}

【维度说明】
· 系统已给出「估值、动量、资金、情绪、风险」五个**客观分**，你必须接受为基准，
  只允许通过 adjust 字段做小幅修正（-15 到 +15），并给出理由。
· 「宏观」维度系统不给分，由你判定 0-100。
· 风险维度：**分高表示环境健康、分低表示风险偏高**（不要搞反）。

【输出格式】
{output_spec}

{schema}

硬性要求：
- 只输出 JSON 对象本身，不要 markdown 代码块标记，不要前言后记。
- stance 必须覆盖 equity_cn / equity_global / bond / gold / commodity / cash 六个键。
- disagreements 必须至少 1 条 —— 今天有真实分歧就写真实分歧，没有就写"最接近分歧的争点"。
- actions 每条必须带可执行的 trigger，不要写"关注市场变化"这类空话。
- 全文控制在 3000 字以内，确保 JSON 完整闭合。
"""


def build_objective_dashboard(metrics: dict, board: dict, note: str) -> dict:
    """**降级看板**：模型不可用时，只用确定性指标产出的看板。

    设计立场：第三方 API 挂掉不应该让整个页面变空。客观分（动量/资金/情绪/风险/估值）
    本来就完全由行情数据推导，与模型无关，任何时候都应该能算出来。

    但**不编造主观部分**：大类观点、行动建议、关键分歧一律留空并注明原因。
    用一条机械规则（比如"近 20 日涨得多就超配"）去填满这些字段，
    会让人误以为那是经过研判的建议 —— 那比留空更糟。
    """
    obj = metrics["objective_scores"]
    scores = {}
    for dim in DIMENSIONS:
        if dim == "宏观":
            scores[dim] = {"score": 50.0, "base": None, "adjust": None,
                           "reason": "宏观维度需模型判定；模型不可用，暂取中性值 50"}
        else:
            v = float(obj.get(dim, 50.0))
            scores[dim] = {"score": round(v, 1), "base": round(v, 1), "adjust": 0.0,
                           "reason": "由行情数据确定性计算（模型不可用，未做主观调整）"}

    ag = metrics.get("aggregate", {})
    br = board.get("breadth", {})
    text = (f"【降级模式】{note}　本页仅包含系统按行情数据确定性计算的客观指标，"
            f"不含任何模型研判内容。当日全标的平均涨跌 {ag.get('avg_d1', 0) * 100:+.2f}%，"
            f"上涨 {br.get('up', 0)} / 下跌 {br.get('down', 0)}；"
            f"权益类 20 日平均 {ag.get('avg_d20_equity', 0) * 100:+.2f}%、"
            f"60 日平均 {ag.get('avg_d60_equity', 0) * 100:+.2f}%，"
            f"波动率环境 {ag.get('vol_regime', 1):.2f}，"
            f"价格分位 {ag.get('valuation_percentile', 0.5):.2f}。")

    return {
        "summary": text,
        "scores": scores,
        "stance": {k: {"label": v, "view": "—", "conviction": 0.0,
                       "reason": "模型不可用，未产出观点"}
                   for k, v in ALLOC_LABELS.items() if k in
                   ("equity_cn", "equity_global", "bond", "gold", "commodity", "cash")},
        "position": {"suggested": 0.0, "range": [0.0, 0.0], "reason": "模型不可用，未给出仓位建议"},
        "disagreements": [],
        "actions": [],
        "risks": [note],
        "watchlist": [],
        "parse_ok": False,
        "degraded": True,
        "degraded_note": note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 三、编排
# ─────────────────────────────────────────────────────────────────────────────

class NightlyReview:
    def __init__(self, feed: RealtimeFeed, market, llm: LLMClient, emit: Emit,
                 daily_dir: Path, max_rounds: int = 3,
                 run_date: Optional[str] = None, offline: bool = False):
        self.feed = feed
        self.market = market
        self.llm = llm
        self.emit = emit
        self.daily_dir = Path(daily_dir)
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.max_rounds = max(2, int(max_rounds))
        self.offline = offline          # True = 跳过全部 LLM 调用，只出客观看板
        self.date = run_date or date.today().isoformat()
        self.run_id = f"nightly_{self.date.replace('-', '')}"
        self.t0 = time.time()
        self.tracker = StanceTracker(run_id=self.run_id, topic=f"{self.date} 每日研判",
                                     log_dir=self.daily_dir)
        self._pct = 0.0
        self._sub_counts: dict[str, int] = {}

    def _emit(self, ev: dict) -> None:
        ev = {"ts": datetime.now().isoformat(timespec="seconds"), **ev}
        try:
            self.emit(ev)
        except Exception:  # noqa: BLE001
            logger.exception("emit 回调异常")

    def _phase(self, key: str, label: str, detail: str = "") -> None:
        self._emit({"type": "phase", "phase": key, "label": label, "detail": detail})

    # ── 进度 ──────────────────────────────────────────────────────────────
    def _progress(self, phase: str, label: str, add: float = 0.0,
                  detail: str = "", sub: str = "", final: bool = False) -> None:
        """推进并广播一次进度。

        进度由**服务端累积**而不是前端按事件猜 —— 只有编排器知道总步数
        （分析师数、轮数、表态数）。前端只负责画。
        """
        self._pct = 100.0 if final else min(99.0, self._pct + add)
        self._emit({
            "type": "progress",
            "phase": phase,
            "label": label,
            "detail": detail,
            "sub": sub,
            "pct": round(self._pct, 1),
            "steps": STEP_LABELS,
            "elapsed": round(time.time() - self.t0, 1),
        })

    def _tick(self, key: str) -> int:
        """同一阶段内的计数（用于「分析师 3/5」这类子计数）。"""
        self._sub_counts[key] = self._sub_counts.get(key, 0) + 1
        return self._sub_counts[key]

    # ── 主流程 ────────────────────────────────────────────────────────────
    def run(self) -> dict:
        self._phase("collect", "采集当日行情", "拉取实时快照并计算客观指标")
        self._progress("collect", "采集行情", detail="拉取实时快照与分时，计算客观指标")
        board = build_board(self.feed, self.market)
        metrics = compute_market_metrics(self.market, board)
        self._emit({"type": "board", "data": board})
        self._emit({"type": "metrics", "data": metrics["aggregate"],
                    "objective_scores": metrics["objective_scores"]})
        self._progress("collect", "采集行情", add=_W["collect"],
                       detail=f"已取 {len(board.get('universe') or [])} 个标的的实时快照")

        market_block = build_market_block(board, metrics)

        # ── 降级模式：模型不可用时只出客观看板 ────────────────────────────
        if self.offline:
            note = "已按离线模式运行：未调用模型。"
            self._phase("offline", "离线模式", note)
            return self._finish(board, metrics, [], None,
                                build_objective_dashboard(metrics, board, note))

        # ── 五位分析师并行发言 ────────────────────────────────────────────
        self._phase("analysts", "分析师独立研判", "五位分析师各写一份今日解读")
        self._progress("analysts", "分析师研判", detail="五位分析师并行撰写今日解读")
        per_agent = _W["analysts"] / max(1, len(REVIEW_AGENTS))
        speeches: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = {pool.submit(self._speak, a, market_block): a for a in REVIEW_AGENTS}
            for fut in as_completed(futs):
                a = futs[fut]
                try:
                    speeches[a.id] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("研判发言失败 %s", a.id)
                    speeches[a.id] = {"agent": a.id, "name": a.name,
                                      "text": f"（发言失败：{exc}）", "tint": a.tint}
                n = self._tick("analysts")
                self._progress("analysts", "分析师研判", add=per_agent,
                               detail=f"{a.name} 已完成",
                               sub=f"{n}/{len(REVIEW_AGENTS)}")

        # 全部发言都失败 → 上游不可用，降级出客观看板，而不是产出一份空报告
        ok_speeches = [s for s in speeches.values()
                       if s.get("text") and not s["text"].startswith("（发言失败")]
        if not ok_speeches:
            note = ("模型后端当前不可用（五路研判调用全部失败），"
                    "已自动降级为仅客观指标模式。")
            self._emit({"type": "warning", "message": note})
            self._phase("degraded", "降级模式", note)
            # 把已经拿到的（可能全部失败的）发言保留成第 1 轮，便于用户看到失败原因。
            # 注意 _finish 的第三个形参是 rounds（列表），不是 speeches（字典）——
            # 传错会在这里埋一个 TypeError，而且只在降级路径才触发。
            return self._finish(board, metrics,
                                [{"round": 1, "speeches": speeches}], None,
                                build_objective_dashboard(metrics, board, note))

        transcript = "\n\n".join(
            f"── {v.get('name', k)} ──\n{v.get('text', '')}" for k, v in speeches.items())

        # ── 议题抽取 ──────────────────────────────────────────────────────
        self._phase("topics", "提炼争点", "把研判意见归纳为可辩论的明日操作主张")
        self._progress("topics", "提炼争点", detail="归纳成可辩论的明日操作主张")
        args = self.tracker.extract_arguments(
            transcript, self.llm.make_callable(max_tokens=1000, temperature=0.2,
                                               label="nightly:args"))
        if not args:
            self._emit({"type": "warning", "message": "争点提炼失败，本轮不做收敛量化"})
        self._progress("topics", "提炼争点", add=_W["topics"],
                       detail=f"提炼出 {len(args)} 条争点")
        self._emit({"type": "arguments", "arguments": args})

        # ── 交叉辩论 ──────────────────────────────────────────────────────
        rounds: list[dict] = [{"round": 1, "speeches": speeches}]
        # 辩论阶段的总步数 = 轮数 × (5 发言 + 5 表态)；据此把 40 分权重切成等份。
        # 提前算出来才能让进度条平滑推进，而不是每轮跳一大格。
        debate_steps = max(1, self.max_rounds * len(REVIEW_AGENTS) * 2)
        per_step = _W["debate"] / debate_steps
        if self.tracker.arg_ids:
            for rnd in range(2, self.max_rounds + 1):
                self._phase(f"round{rnd}", f"第 {rnd} 轮交叉辩论",
                            "看到他人意见后修正判断并逐条表态")
                self._emit({"type": "round_start", "round": rnd})
                self._progress("debate", f"第 {rnd} 轮交叉辩论",
                               detail="看到他人意见后修正判断并逐条表态")
                history = self._history_block(rounds)
                r_speeches: dict[str, dict] = {}
                with ThreadPoolExecutor(max_workers=5) as pool:
                    futs = {
                        pool.submit(self._speak, a, market_block,
                                    peers=rounds[-1]["speeches"],
                                    history=history,
                                    stance_block=self.tracker.get_stance_block()): a
                        for a in REVIEW_AGENTS
                    }
                    for fut in as_completed(futs):
                        a = futs[fut]
                        try:
                            r_speeches[a.id] = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            r_speeches[a.id] = {"agent": a.id, "name": a.name,
                                                "text": f"（发言失败：{exc}）", "tint": a.tint}
                        n = self._tick(f"r{rnd}s")
                        self._progress("debate", f"第 {rnd} 轮 · 发言", add=per_step,
                                       detail=f"{a.name} 已发言",
                                       sub=f"{n}/{len(REVIEW_AGENTS)}")
                rounds.append({"round": rnd, "speeches": r_speeches})

                self.tracker.begin_round(len(REVIEW_AGENTS))
                with ThreadPoolExecutor(max_workers=5) as pool:
                    futs2 = [pool.submit(self._stance, a, r_speeches.get(a.id, {}).get("text", ""))
                             for a in REVIEW_AGENTS]
                    for f in as_completed(futs2):
                        f.result()
                        n = self._tick(f"r{rnd}t")
                        self._progress("debate", f"第 {rnd} 轮 · 表态", add=per_step,
                                       sub=f"{n}/{len(REVIEW_AGENTS)}")
                rr = self.tracker.finish_round(rnd)
                self._emit({"type": "convergence", "round": rnd, "data": rr})
                if rr["should_stop"]:
                    self._progress("debate", f"第 {rnd} 轮终止判定",
                                   detail=(rr.get("termination_reason") or "")[:120])
                    break

        summary = self.tracker.summary()
        self._emit({"type": "convergence_summary", "data": summary})
        # 辩论提前终止时把剩余权重一次性补上，避免进度条卡在 80% 跳到 100%
        if self._pct < _W["collect"] + _W["analysts"] + _W["topics"] + _W["debate"]:
            remain = (_W["collect"] + _W["analysts"] + _W["topics"] + _W["debate"]) - self._pct
            self._progress("debate", "交叉辩论", add=remain, detail="辩论结束")

        # ── 首席研判官产出看板 ────────────────────────────────────────────
        self._phase("officer", "首席研判官产出看板", "合并意见、量化分歧、给出可执行建议")
        self._progress("officer", "首席研判官", detail="合并意见、量化分歧、给出可执行建议")
        officer_raw = self._call_officer(market_block, summary, transcript)
        officer = self._parse_officer(officer_raw, metrics)
        if not officer.get("parse_ok"):
            self._emit({"type": "warning",
                        "message": "研判官输出无法解析，看板主观部分将为空（客观指标仍保留）"})
        self._emit({"type": "officer", "data": officer, "raw": officer_raw})
        self._progress("officer", "首席研判官", add=_W["officer"],
                       detail="看板已生成")

        return self._finish(board, metrics, rounds, summary, officer)

    # ── 落盘 ──────────────────────────────────────────────────────────────
    def _finish(self, board: dict, metrics: dict, rounds: list[dict],
                summary: Optional[dict], officer: dict) -> dict:
        """两条路径（正常 / 降级）共用的收尾：组装、落盘、推流。

        参数顺序是 (board, metrics, rounds, summary, officer)。
        rounds 必须是 list[{"round", "speeches"}]；降级路径同样要传列表。
        这里对 rounds 做了类型兜底 —— 传错类型时降级为"无发言记录"，
        而不是抛 TypeError 把整次研判带崩（那条路径平时跑不到，最容易埋雷）。
        """
        if summary is None:
            summary = self.tracker.summary()
        if not isinstance(rounds, list):
            logger.warning("_finish 收到非列表的 rounds（%s），已忽略", type(rounds).__name__)
            rounds = []
        speeches_out = {}
        for r in rounds:
            if isinstance(r, dict) and "round" in r:
                speeches_out[str(r["round"])] = r.get("speeches") or {}
        result = {
            "run_id": self.run_id,
            "date": self.date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed_sec": round(time.time() - self.t0, 1),
            "degraded": bool(officer.get("degraded")),
            "board": board,
            "metrics": metrics.get("aggregate", {}),
            "per_symbol": metrics.get("per_symbol", []),
            "objective_scores": metrics.get("objective_scores", {}),
            "convergence": summary,
            "speeches": speeches_out,
            "dashboard": officer,
            "usage": self.llm.usage.as_dict(),
        }
        out = self.daily_dir / f"{self.date}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        self._progress("finalize", "落盘", detail=f"报告已写入 {out.name}", final=True)
        logger.info("每日研判已写入 %s（degraded=%s）", out, result["degraded"])
        self._emit({"type": "done", "data": {"date": self.date, "path": str(out),
                                             "degraded": result["degraded"],
                                             "dashboard": officer}})
        return result

    # ── 单次发言 ──────────────────────────────────────────────────────────
    def _speak(self, agent: ReviewAgent, market_block: str, peers: dict | None = None,
               history: str = "", stance_block: str = "") -> dict:
        self._emit({"type": "agent_start", "agent": agent.id, "name": agent.name,
                    "tint": agent.tint})
        prompt = REVIEW_PROMPT.format(
            name=agent.name, duty=agent.duty, market_block=market_block,
            output=agent.output, common_rules=COMMON_RULES)
        if peers:
            lines = []
            for k, v in peers.items():
                if k == agent.id:
                    continue
                txt = (v.get("text") or "")[:1800]
                lines.append(f"── {v.get('name', k)} ──\n{txt}")
            prompt += "\n\n【上一轮其他分析师的发言】\n" + "\n\n".join(lines)
        if history:
            prompt += "\n\n【历轮辩论摘要】\n" + history
        if stance_block:
            prompt += "\n\n" + stance_block
        try:
            text = self.llm.chat(prompt, temperature=agent.temperature,
                                 max_tokens=agent.max_tokens, label=f"nightly:{agent.id}")
        except LLMError as exc:
            text = f"（发言失败：{exc}）"
        out = {"agent": agent.id, "name": agent.name, "text": text.strip(),
               "tint": agent.tint, "role": agent.name}
        self._emit({"type": "agent_done", **out})
        return out

    def _stance(self, agent: ReviewAgent, speech: str) -> None:
        if not self.tracker.arg_ids:
            return
        prompt = self.tracker.get_stance_ask_prompt(agent.name, speech)
        try:
            raw = self.llm.chat(prompt, temperature=0.2, max_tokens=900,
                                label=f"nightly:stance:{agent.id}")
        except LLMError as exc:
            raw = ""
            logger.warning("表态失败 %s: %s", agent.id, exc)
        parsed = self.tracker.record_agent_stance(agent.id, raw)
        if parsed:
            self._emit({"type": "stance_recorded", "agent": agent.id,
                        "name": agent.name, "stance": parsed.get("stance", {}),
                        "why": parsed.get("why", {}), "ok": parsed.get("ok", False)})

    def _history_block(self, rounds: list[dict], max_chars: int = 1500) -> str:
        L = []
        for r in rounds:
            L.append(f"═══ 第 {r['round']} 轮 ═══")
            for k, v in r["speeches"].items():
                t = (v.get("text") or "").strip()
                L.append(f"── {v.get('name', k)} ──\n{t[:max_chars]}")
        return "\n\n".join(L)

    def _call_officer(self, market_block: str, summary: dict, transcript: str) -> str:
        from engine.debate import format_convergence_block  # 复用既有渲染
        self._emit({"type": "agent_start", "agent": REVIEW_OFFICER.id,
                    "name": REVIEW_OFFICER.name, "tint": REVIEW_OFFICER.tint})
        prompt = OFFICER_PROMPT.format(
            name=REVIEW_OFFICER.name, duty=REVIEW_OFFICER.duty,
            market_block=market_block,
            convergence_block=format_convergence_block(summary),
            transcript_block=transcript[:14000],
            output_spec=REVIEW_OFFICER.output, schema=REVIEW_JSON_SCHEMA)
        try:
            text = self.llm.chat(prompt, temperature=REVIEW_OFFICER.temperature,
                                 max_tokens=REVIEW_OFFICER.max_tokens,
                                 json_mode=True, label="nightly:officer")
        except LLMError as exc:
            text = f"（研判官调用失败：{exc}）"
        self._emit({"type": "agent_done", "agent": REVIEW_OFFICER.id,
                    "name": REVIEW_OFFICER.name, "tint": REVIEW_OFFICER.tint,
                    "text": text})
        return text

    # ── 解析与兜底 ────────────────────────────────────────────────────────
    @staticmethod
    def _parse_officer(raw: str, metrics: dict) -> dict:
        """解析研判官 JSON，并把客观分的 adjust 合并成最终六维分。"""
        data = extract_json(raw or "")
        if not isinstance(data, dict):
            data = {}
        obj = metrics["objective_scores"]

        scores: dict[str, dict] = {}
        raw_scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
        for dim in DIMENSIONS:
            item = raw_scores.get(dim) if isinstance(raw_scores.get(dim), dict) else {}
            base = float(obj.get(dim, 50))
            if dim == "宏观":
                val = item.get("score")
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    val = 50.0
                scores[dim] = {"score": round(max(0, min(100, val)), 1), "base": None,
                               "adjust": None, "reason": str(item.get("reason") or "")}
                continue
            try:
                adj = float(item.get("adjust") or 0)
            except (TypeError, ValueError):
                adj = 0.0
            adj = max(-15.0, min(15.0, adj))
            scores[dim] = {"score": round(max(0, min(100, base + adj)), 1),
                           "base": round(base, 1), "adjust": round(adj, 1),
                           "reason": str(item.get("reason") or "")}

        # stance 归一化
        stance_raw = data.get("stance") if isinstance(data.get("stance"), dict) else {}
        stance = {}
        for k, label in ALLOC_LABELS.items():
            it = stance_raw.get(k) if isinstance(stance_raw.get(k), dict) else {}
            view = str(it.get("view") or "标配")
            if view not in VIEW_LEVELS:
                view = "标配"
            try:
                conv = float(it.get("conviction"))
            except (TypeError, ValueError):
                conv = 0.5
            stance[k] = {"label": label, "view": view,
                         "conviction": round(max(0.0, min(1.0, conv)), 3),
                         "reason": str(it.get("reason") or "")}

        pos = data.get("position") if isinstance(data.get("position"), dict) else {}
        try:
            suggested = float(pos.get("suggested"))
        except (TypeError, ValueError):
            suggested = 0.5
        rng = pos.get("range")
        if not (isinstance(rng, list) and len(rng) == 2):
            rng = [max(0.0, suggested - 0.1), min(1.0, suggested + 0.1)]

        def _list(key: str, sub: Optional[list[str]] = None) -> list:
            v = data.get(key)
            return v if isinstance(v, list) else []

        return {
            "summary": str(data.get("summary") or ""),
            "scores": scores,
            "stance": stance,
            "position": {"suggested": round(max(0.0, min(1.0, suggested)), 3),
                         "range": [round(float(rng[0]), 3), round(float(rng[1]), 3)],
                         "reason": str(pos.get("reason") or "")},
            "disagreements": [d for d in _list("disagreements") if isinstance(d, dict)],
            "actions": [a for a in _list("actions") if isinstance(a, dict)],
            "risks": [str(x) for x in _list("risks")],
            "watchlist": [str(x) for x in _list("watchlist")],
            "parse_ok": bool(data),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 四、定时调度
# ─────────────────────────────────────────────────────────────────────────────

# ── 运行注册表的外部视图 ──────────────────────────────────────────────────
# 注册表本身在 app.py（RUNS），这里只需要一个只读快照，
# 用来回答"此刻有没有一次研判真的在跑"。用注入而不是 import，避免循环依赖。
_RUNS_PROVIDER: Optional[Callable[[], dict]] = None


def set_runs_provider(fn: Callable[[], dict]) -> None:
    global _RUNS_PROVIDER
    _RUNS_PROVIDER = fn


def _RUNS_SNAPSHOT() -> dict:
    if _RUNS_PROVIDER is None:
        return {}
    try:
        return _RUNS_PROVIDER()
    except Exception:  # noqa: BLE001
        return {}


class NightlyScheduler:
    """每交易日到点自动跑一次研判。

    不引入 APScheduler 等依赖：一个后台线程每分钟检查一次即可。
    判重条件是「当天结果文件是否已存在」，所以重启程序不会重复跑，
    手动补跑（force）也不会被这里挡住。
    """

    def __init__(self, feed, market, llm_factory: Callable[[], LLMClient],
                 daily_dir: Path, at: str = "20:30", max_rounds: int = 3,
                 emit: Optional[Emit] = None, enabled: bool = True,
                 max_run_sec: int = 900):
        self.feed = feed
        self.market = market
        self.llm_factory = llm_factory
        self.daily_dir = Path(daily_dir)
        self.at = at
        self.max_rounds = max_rounds
        self.emit = emit or (lambda ev: None)
        self.enabled = enabled
        # 单次研判的硬上限：正常一次 3~5 分钟，超过 15 分钟基本可以判定卡住。
        # 没有这个上限时，一次挂住的运行会让 /api/daily/status 永远报告
        # "正在运行"，而客户端只能无限等待（实测在界面上报到了 2171 秒）。
        self.max_run_sec = max_run_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_run: Optional[str] = None
        self.last_error: str = ""

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="nightly-scheduler")
        self._thread.start()
        logger.info("每晚研判调度已启动，触发时间 %s（每交易日一次）", self.at)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(60):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("调度 tick 异常")

    def tick(self, now: Optional[datetime] = None) -> bool:
        """到点且今天还没跑过 → 跑一次。返回是否真的跑了。"""
        now = now or datetime.now()
        if now.weekday() >= 5:               # 周末不跑
            return False
        hh, mm = (self.at.split(":") + ["0"])[:2]
        if (now.hour, now.minute) < (int(hh), int(mm)):
            return False
        today = now.date().isoformat()
        if (self.daily_dir / f"{today}.json").is_file():
            return False
        self.run_once(today)
        return True

    def run_once(self, day: Optional[str] = None) -> dict:
        day = day or date.today().isoformat()
        try:
            review = NightlyReview(self.feed, self.market, self.llm_factory(),
                                   self.emit, self.daily_dir,
                                   max_rounds=self.max_rounds, run_date=day)
            res = review.run()
            self.last_run = day
            self.last_error = ""
            return res
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("每日研判失败")
            self.emit({"type": "error", "message": self.last_error})
            raise

    def status(self) -> dict:
        """注意 `running` 的语义。

        早先写的是 `self._thread.is_alive()` —— 那是**调度器守护线程**
        （一个 while True: sleep 循环），**永远为真**。于是 /api/daily/status
        无条件报告"正在运行"，任何依赖它的判断都被误导。

        正确的含义是"**此刻有一次研判真的在跑**"：检查注册表里有没有
        已启动但尚未结束的运行。调度器线程是否存活另用一个字段表达。
        """
        today = date.today().isoformat()
        now = time.time()
        active = None
        for rid, st in list(_RUNS_SNAPSHOT().items()):
            if not st.get("done") and (now - st.get("started", now)) < self.max_run_sec:
                active = {"run_id": rid, "elapsed": round(now - st.get("started", now), 1)}
                break
        return {
            "enabled": self.enabled, "at": self.at,
            "last_run": self.last_run, "last_error": self.last_error,
            "today_done": (self.daily_dir / f"{today}.json").is_file(),
            "n_reports": len(list(self.daily_dir.glob("*.json"))),
            "running": active is not None,          # ← 真的有一次运行在跑
            "active_run": active,
            "scheduler_alive": bool(self._thread and self._thread.is_alive()),
            "max_run_sec": self.max_run_sec,
        }
