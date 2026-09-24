#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」数据层：加载内置历史数据集，提供组合统计与情景压力测试。

数据集由 tools/fetch_market_data.py 生成，来源为腾讯证券行情接口的真实前复权日线。
本模块不做任何网络请求——所有数值计算都基于已落盘的离线数据集，
以保证结果可复现（同 weight 同数据 → 同输出）。
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

TRADING_DAYS = 244.0
DEFAULT_DATASET = Path(__file__).resolve().parent / "dataset"


# ─────────────────────────────────────────────────────────────────────────────
# 情景定义（用真实历史窗口做压力测试，而非假设参数）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Scenario:
    key: str
    name: str
    start: str
    end: str
    note: str


MARKET_SCENARIOS: list[Scenario] = [
    Scenario("bear2018", "2018 去杠杆熊市", "2018-01-24", "2019-01-04",
             "中美贸易摩擦 + 金融去杠杆，A股全年单边下行"),
    Scenario("covid2020", "2020 疫情崩盘", "2020-01-13", "2020-03-23",
             "新冠疫情全球扩散，风险资产同步暴跌"),
    Scenario("core2021", "2021 核心资产杀估值", "2021-02-10", "2021-03-25",
             "抱团股瓦解，机构重仓资产快速回撤"),
    Scenario("rate2022", "2022 全球加息", "2022-01-04", "2022-10-31",
             "美联储激进加息 + 俄乌冲突，股债双杀"),
    Scenario("longbear2324", "2023-24 A股长熊", "2023-08-01", "2024-09-13",
             "信心低迷、外资流出，漫长的阴跌与磨底"),
    Scenario("rally2024", "2024Q4 政策底急涨", "2024-09-18", "2024-10-08",
             "超预期政策组合拳，A股单周暴力反弹（检验踏空风险）"),
    Scenario("bond2013", "2013 钱荒（债券承压）", "2013-05-01", "2013-12-31",
             "银行间流动性骤紧，债券价格大幅调整 —— 若数据窗口不覆盖则自动跳过"),
]

# 家庭特有冲击（非市场情景，作用于现金流而非价格）
CASHFLOW_SHOCKS = [
    {"key": "jobloss", "name": "主要收入来源中断 6 个月",
     "desc": "按家庭月支出与现有应急金测算，检验应急储备覆盖月数"},
    {"key": "medical", "name": "重大医疗自费支出 20 万元",
     "desc": "检验保障缺口与流动性底线是否被击穿"},
    {"key": "house_drop", "name": "自住房价下跌 20%",
     "desc": "检验净值缩水后杠杆率与心理承受力的变化"},
]


# ─────────────────────────────────────────────────────────────────────────────
# 标的
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Symbol:
    code: str
    name: str
    asset_class: str
    sub_class: str
    tradable: bool
    stats: dict = field(default_factory=dict)

    @property
    def annual_return(self) -> float:
        return float(self.stats.get("annual_return") or 0.0)

    @property
    def annual_vol(self) -> float:
        return float(self.stats.get("annual_vol") or 0.0)

    @property
    def max_drawdown(self) -> float:
        return float(self.stats.get("max_drawdown") or 0.0)

    @property
    def n_obs(self) -> int:
        return int(self.stats.get("n_obs") or 0)

    def as_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name,
            "asset_class": self.asset_class, "sub_class": self.sub_class,
            "tradable": self.tradable,
            "annual_return": round(self.annual_return, 4),
            "annual_vol": round(self.annual_vol, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "sortino": self.stats.get("sortino"),
            "var95_daily": self.stats.get("var95_daily"),
            "n_obs": self.n_obs,
            "start": self.stats.get("start"), "end": self.stats.get("end"),
            "return_basis": self.stats.get("return_basis", "price"),
        }


CLASS_LABELS = {
    "equity_cn": "A股权益",
    "equity_global": "海外与港股权益",
    "bond": "债券",
    "gold": "黄金",
    "commodity": "商品",
    "cash": "现金管理",
    "deposit": "存款与理财",
    "insurance": "保险与养老金",
    "property": "房产",
}

# 配置大类 → 数据集大类（与 engine/agents.py 的 ALLOC_CLASSES 保持一致）
CLASS_TO_DATASET: dict[str, Optional[str]] = {
    "equity_cn": "equity_cn",
    "equity_global": "equity_global",
    "bond": "bond",
    "gold": "gold",
    "commodity": "commodity",
    "cash": "cash",
    "deposit": None,       # 无连续行情，按利率表建模
    "insurance": None,
}

# 无行情大类 → 利率表键（用于构造恒定日收益序列）
RATE_BASED_CLASSES: dict[str, str] = {
    "deposit": "bank_wealth_1y",
    "insurance": "annuity_irr",
}


# ─────────────────────────────────────────────────────────────────────────────
# 数据集
# ─────────────────────────────────────────────────────────────────────────────

class MarketData:
    """内置数据集的只读访问层。"""

    def __init__(self, dataset_dir: str | Path = DEFAULT_DATASET):
        self.dir = Path(dataset_dir)
        self.meta: dict = {}
        self.rate_table: dict = {}
        self._history: dict[str, dict] = {}
        self._stats: dict[str, dict] = {}
        self._corr: dict[str, dict] = {}
        self._returns: dict[str, list[float]] = {}
        self._dates: dict[str, list[str]] = {}
        self.symbols: dict[str, Symbol] = {}
        self._load()

    # ── 加载 ──────────────────────────────────────────────────────────────
    def _load(self) -> None:
        meta_p = self.dir / "meta.json"
        hist_p = self.dir / "market_history.json"
        stats_p = self.dir / "market_stats.json"
        if not hist_p.is_file():
            raise FileNotFoundError(
                f"内置数据集缺失：{hist_p}\n"
                f"请先运行：python tools/fetch_market_data.py --build")
        self.meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() else {}
        self._history = json.loads(hist_p.read_text(encoding="utf-8"))
        sj = json.loads(stats_p.read_text(encoding="utf-8")) if stats_p.is_file() else {}
        self._stats = sj.get("stats", {})
        self._corr = sj.get("correlation", {})
        self.rate_table = sj.get("rate_table", {})

        for code, node in self._history.items():
            bars = node.get("bars") or []
            closes = [float(b["c"]) for b in bars if b.get("c")]
            rets = [closes[i] / closes[i - 1] - 1.0
                    for i in range(1, len(closes)) if closes[i - 1] > 0]
            self._returns[code] = rets
            self._dates[code] = [str(b["d"]) for b in bars[1:]]
            self.symbols[code] = Symbol(
                code=code, name=node.get("name", code),
                asset_class=node.get("asset_class", "other"),
                sub_class=node.get("sub_class", ""),
                tradable=bool(node.get("tradable")),
                stats=self._stats.get(code, {}),
            )

    # ── 查询 ──────────────────────────────────────────────────────────────
    def get(self, code: str) -> Optional[Symbol]:
        return self.symbols.get(code)

    def returns(self, code: str) -> list[float]:
        return self._returns.get(code, [])

    def dates(self, code: str) -> list[str]:
        return self._dates.get(code, [])

    def correlation(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        return float(self._corr.get(a, {}).get(b, self._corr.get(b, {}).get(a, 0.0)))

    def rate(self, key: str, default: float = 0.0) -> float:
        v = self.rate_table.get(key, default)
        return float(v) if isinstance(v, (int, float)) else default

    def by_class(self, asset_class: str) -> list[Symbol]:
        return [s for s in self.symbols.values() if s.asset_class == asset_class]

    def catalog(self) -> list[dict]:
        """按大类分组的标的目录（给前端用）。"""
        groups: dict[str, list[dict]] = {}
        for s in self.symbols.values():
            groups.setdefault(s.asset_class, []).append(s.as_dict())
        return [
            {"asset_class": k, "label": CLASS_LABELS.get(k, k),
             "symbols": sorted(v, key=lambda x: -x["annual_return"])}
            for k, v in sorted(groups.items(), key=lambda kv: kv[0])
        ]

    # ── 大类级序列（回测与压力测试的基础）──────────────────────────────────
    def class_series(self, alloc_key: str) -> tuple[list[str], list[float]]:
        """某个配置大类的日收益序列（该类下全部**可投**工具的等权组合）。

        与"取所有标的最短公共窗口"不同，这里按**日期并集**构建：
        某个工具尚未上市时，该日仅对其余成员等权。这样做的意义是——
        压力测试要覆盖 2018 去杠杆熊市、2020 疫情崩盘、2022 全球加息，
        若按最短公共窗口对齐（恒生科技ETF 2021 年才上市），这些情景会全部失效。

        无连续行情的固收类（存款/理财、保险/养老金）返回空序列，
        由 portfolio_series 按利率表折算成恒定日收益。
        """
        ds = CLASS_TO_DATASET.get(alloc_key)
        if ds is None:
            return [], []

        members = [s for s in self.symbols.values() if s.tradable and s.asset_class == ds]
        if not members:
            # 该类没有可投工具（如纯指数类）时，退化为用全部成员
            members = [s for s in self.symbols.values() if s.asset_class == ds]
        if not members:
            return [], []

        per_date: dict[str, list[float]] = {}
        for s in members:
            for d, r in zip(self._dates.get(s.code, []), self._returns.get(s.code, [])):
                per_date.setdefault(d, []).append(r)
        if not per_date:
            return [], []

        dates = sorted(per_date)
        rets = [statistics.fmean(per_date[d]) for d in dates]
        return dates, rets

    def ohlc(self, code: str) -> tuple:
        """某标的的开高低收序列。

        波动率建模需要 OHLC：只用收盘价会丢掉日内的高低信息，
        而极差类估计量（Parkinson / Garman-Klass / Rogers-Satchell /
        Yang-Zhang）正是靠这些信息把估计效率提高数倍。
        详见 `backend/engine/rv.py`。
        """
        bars = (self._history.get(code) or {}).get("bars") or []
        d = [str(b["d"]) for b in bars]
        o = [float(b["o"]) for b in bars]
        h = [float(b["h"]) for b in bars]
        l = [float(b["l"]) for b in bars]
        c = [float(b["c"]) for b in bars]
        return d, o, h, l, c

    def representative(self, alloc_key: str) -> Optional[str]:
        """某个配置大类里 **K 线最长**的成员，用作该类波动率的代表标的。

        为什么需要一个代表标的：`class_series` 构造的是等权合成组合，
        它没有"开盘价/最高价"这种东西 —— 合成组合的最高价并不等于各成员
        最高价的平均。而波动率建模需要真实的 OHLC。
        取 K 线最长的成员，等价于选取该类里历史最深、最具代表性的那一个。

        A 股各宽基指数之间相关性约 0.7，波动率的共同因子占主导，
        因此用代表标的刻画**类的波动状态**是合理的近似；
        这在文档中如实标注，不宣称等价于合成组合的波动。
        """
        ds = CLASS_TO_DATASET.get(alloc_key)
        if ds is None:
            return None
        members = [s for s in self.symbols.values() if s.tradable and s.asset_class == ds]
        if not members:
            members = [s for s in self.symbols.values() if s.asset_class == ds]
        if not members:
            return None
        return max(members, key=lambda s: s.n_obs or len(self._returns.get(s.code, []))).code

    def _rate_daily(self, alloc_key: str) -> float:
        return self.rate(RATE_BASED_CLASSES.get(alloc_key, ""), 0.0) / TRADING_DAYS

    def portfolio_series(self, weights: dict[str, float]) -> tuple[list[str], list[float]]:
        """给定**大类权重**，返回组合日收益序列。

        日期轴取所有有行情大类序列的**交集**（保证每个大类在窗口内都有真实数据），
        无行情大类以恒定日收益参与。
        """
        active = {k: float(w) for k, w in (weights or {}).items() if abs(float(w)) > 1e-9}
        if not active:
            return [], []

        parts: dict[str, dict[str, float]] = {}
        for k in active:
            d, r = self.class_series(k)
            parts[k] = dict(zip(d, r)) if d else {}

        market_keys = [k for k, m in parts.items() if m]
        if not market_keys:
            return [], []

        axis: Optional[set[str]] = None
        for k in market_keys:
            s = set(parts[k])
            axis = s if axis is None else (axis & s)
        dates = sorted(axis or [])
        if len(dates) < 30:
            return [], []

        total_w = sum(active.values())
        rets = []
        for d in dates:
            r = 0.0
            for k, w in active.items():
                m = parts[k]
                r += w * (m[d] if m else self._rate_daily(k))
            rets.append(r / total_w if abs(total_w) > 1e-9 else 0.0)
        return dates, rets

    def portfolio_stats(self, weights: dict[str, float]) -> dict:
        """组合的历史统计：年化收益、波动、最大回撤、Sortino、VaR、夏普。"""
        dates, rets = self.portfolio_series(weights)
        if len(rets) < 30:
            return {"ok": False, "error": "共同历史窗口过短，无法计算组合统计"}

        n = len(rets)
        years = n / TRADING_DAYS
        cum = 1.0
        curve = [1.0]
        for r in rets:
            cum *= (1.0 + r)
            curve.append(cum)

        ann_ret = cum ** (1.0 / years) - 1.0 if years > 0 and cum > 0 else 0.0
        mu = statistics.fmean(rets)
        var = sum((x - mu) ** 2 for x in rets) / max(1, n - 1)
        ann_vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)

        peak = curve[0]
        max_dd = 0.0
        for c in curve:
            peak = max(peak, c)
            max_dd = min(max_dd, c / peak - 1.0)

        downside = [x for x in rets if x < 0]
        dvol = (math.sqrt(sum(x * x for x in downside) / max(1, len(downside)))
                * math.sqrt(TRADING_DAYS)) if downside else 0.0
        srt = sorted(rets)
        var95 = srt[max(0, int(0.05 * len(srt)) - 1)]
        cvar95 = (statistics.fmean(srt[:max(1, int(0.05 * len(srt)))])
                  if srt else 0.0)

        # 年度收益
        yearly: dict[str, float] = {}
        if dates:
            cur_year = dates[0][:4]
            base = 1.0
            acc = 1.0
            for d, r in zip(dates, rets):
                y = d[:4]
                if y != cur_year:
                    yearly[cur_year] = round(acc / base - 1.0, 4)
                    cur_year, base, acc = y, acc, acc
                acc *= (1.0 + r)
            yearly[cur_year] = round(acc / base - 1.0, 4)

        return {
            "ok": True,
            "annual_return": round(ann_ret, 6),
            "annual_vol": round(ann_vol, 6),
            "max_drawdown": round(max_dd, 6),
            "sortino": round(ann_ret / dvol, 4) if dvol > 1e-9 else None,
            "sharpe_rf0": round(ann_ret / ann_vol, 4) if ann_vol > 1e-9 else None,
            "calmar": round(ann_ret / abs(max_dd), 4) if max_dd < -1e-9 else None,
            "var95_daily": round(var95, 6),
            "cvar95_daily": round(cvar95, 6),
            "yearly_returns": yearly,
            "n_obs": n,
            "window": {"start": dates[0] if dates else None,
                       "end": dates[-1] if dates else None},
            "curve": [round(x, 6) for x in curve],
            "curve_dates": dates,
        }

    def stress_test(self, weights: dict[str, float]) -> list[dict]:
        """在每个真实历史情景窗口内回放该组合，给出区间收益与最大回撤。"""
        dates, rets = self.portfolio_series(weights)
        if not dates:
            return []
        out = []
        for sc in MARKET_SCENARIOS:
            idx = [i for i, d in enumerate(dates) if sc.start <= d <= sc.end]
            if len(idx) < 3:
                out.append({"key": sc.key, "name": sc.name, "covered": False,
                            "note": sc.note, "desc": sc.desc if hasattr(sc, "desc") else sc.note})
                continue
            seg = [rets[i] for i in idx]
            cum = 1.0
            curve = [1.0]
            for r in seg:
                cum *= (1.0 + r)
                curve.append(cum)
            peak, mdd = curve[0], 0.0
            for c in curve:
                peak = max(peak, c)
                mdd = min(mdd, c / peak - 1.0)
            out.append({
                "key": sc.key, "name": sc.name, "covered": True,
                "start": dates[idx[0]], "end": dates[idx[-1]],
                "days": len(seg),
                "return": round(cum - 1.0, 6),
                "max_drawdown": round(mdd, 6),
                "note": sc.note,
            })
        return out

    def marginal_risk(self, weights: dict[str, float]) -> list[dict]:
        """各标的对组合波动的贡献（风险预算视角：权重 ≠ 风险贡献）。

        weights 是**标的级**权重（由大类权重在类内展开得到）。
        组合波动用解析式 σ_p = sqrt(ΣΣ wᵢwⱼσᵢσⱼρᵢⱼ) 现算，
        而不是复用 portfolio_stats —— 后者吃的是大类权重，
        两者的口径必须解耦，否则会出现"权重全是标的代码、算不出组合波动"的空结果。
        """
        codes = [c for c, w in weights.items()
                 if abs(float(w)) > 1e-9 and c in self.symbols]
        if not codes:
            return []

        var = 0.0
        for a in codes:
            for b in codes:
                var += (float(weights[a]) * float(weights[b])
                        * self.symbols[a].annual_vol * self.symbols[b].annual_vol
                        * self.correlation(a, b))
        port_vol = math.sqrt(max(0.0, var))
        if port_vol <= 1e-9:
            return []

        out = []
        for c in codes:
            s = self.symbols[c]
            w = float(weights[c])
            # MCTR_i = ∂σ_p/∂w_i = σ_i · (Σ_j w_j σ_j ρ_ij) / σ_p
            # 注意必须乘 σ_i —— 漏掉它会让 Σ(risk_share) ≠ 1（实测会得到 450%）。
            cov_sum = sum(float(weights[o]) * self.correlation(c, o)
                          * s.annual_vol * self.symbols[o].annual_vol
                          for o in codes)
            mctr = cov_sum / port_vol
            rc = w * mctr
            out.append({
                "code": c, "name": s.name, "asset_class": s.asset_class,
                "weight": round(w, 4),
                "risk_contribution": round(rc, 4),
                "risk_share": round(rc / port_vol, 4) if port_vol > 1e-9 else None,
            })
        return sorted(out, key=lambda x: -(x["risk_share"] or 0))
