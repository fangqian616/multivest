#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」内置历史数据集构建器。

数据源：腾讯证券行情接口 web.ifzq.gtimg.cn（东财 push2his 在本机返回空 body，弃用）
产出：
    backend/data/dataset/market_history.json   日线收盘价序列（前复权）
    backend/data/dataset/market_stats.json     年化收益/波动/最大回撤/相关系数矩阵
    backend/data/dataset/meta.json             数据口径与免责声明

用法：
    python tools/fetch_market_data.py --probe      # 只探测可用性
    python tools/fetch_market_data.py --build      # 抓取并写入数据集
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "backend" / "data" / "dataset"

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
# 非复权接口。关键差异（实测）：fqkline 对 ETF 只返回约 639 条（2024 年起），
# 而 kline 返回满 2000 条（2018-06 起）。压力测试依赖 2024 年之前的历史情景，
# 因此以 kline 为主源、fqkline 为备源。
# 代价：kline 不复权。对本资产池影响可控——黄金ETF/纳指ETF 基本不分红，
# 国债与企业债指数本身即全价指数，货币ETF 的净值增长本就是计息累积。
KLINE_URL_RAW = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}

# ── 资产池：面向中国家庭的七大类 ──────────────────────────────────────────────
# (代码, 中文名, 大类, 细分, 是否可投工具)
UNIVERSE: list[tuple[str, str, str, str, bool]] = [
    # A 股权益
    ("sh000300", "沪深300",            "equity_cn", "A股大盘核心", True),
    ("sh000905", "中证500",            "equity_cn", "A股中盘",     True),
    ("sh000852", "中证1000",           "equity_cn", "A股小盘",     True),
    ("sh000016", "上证50",             "equity_cn", "A股大盘蓝筹", True),
    ("sz399006", "创业板指",           "equity_cn", "A股成长",     True),
    ("sh000688", "科创50",             "equity_cn", "A股科技",     True),
    ("sh000922", "中证红利",           "equity_cn", "A股红利",     True),
    # 港股 / 海外权益
    ("sh510900", "H股ETF",             "equity_global", "港股价值", True),
    ("sh513180", "恒生科技ETF",        "equity_global", "港股成长", True),
    ("sh513100", "纳指ETF",            "equity_global", "美股科技", True),
    ("sh513500", "标普500ETF",         "equity_global", "美股宽基", True),
    ("hkHSI",    "恒生指数",           "equity_global", "港股基准", False),
    # 债券
    ("sh000012", "上证国债指数",       "bond", "利率债",       False),
    ("sh000013", "上证企债指数",       "bond", "信用债",       False),
    ("sh511010", "国债ETF",            "bond", "利率债工具",   True),
    ("sh511260", "十年国债ETF",        "bond", "长久期工具",   True),
    ("sh000832", "中证转债指数",       "bond", "可转债",       False),
    ("sh511380", "可转债ETF",          "bond", "可转债工具",   True),
    # 黄金与商品
    ("sh518880", "黄金ETF(华安)",      "gold", "黄金",       True),
    ("sz159934", "黄金ETF(易方达)",    "gold", "黄金",       True),
    ("sh000819", "有色金属指数",       "commodity", "工业金属", False),
    # 现金管理
    # 注意：GC001(sh204001) 是国债逆回购**利率报价**而非资产价格序列，
    # 直接当价格序列会得到 267% 的荒谬波动率，故不作为资产收录；
    # 逆回购收益改由 RATE_TABLE 的货币市场利率表达。
    # 银华日利(sh511880) 已剔除：其份额折算频繁且幅度小于拆分检测阈值，
    # 残留伪影使波动率虚高到 2.03%（华宝添益仅 0.21%），且与华宝添益功能重复。
    ("sh511990", "华宝添益",           "cash", "货币ETF", True),
]

# 货币类工具的净值近似恒定（100 元面值上下微幅波动），
# 其真实收益来自**每日计息**而非净值涨跌。对这类标的必须用利率表
# 推导预期收益，不能用价格序列几何年化（否则会得到 0.00% 的假象）。
CASH_CODES = {"sh511990"}

# 存款/理财/保险的基准利率表（无公开连续行情，按监管公开数据人工维护）
RATE_TABLE = {
    "_note": "以下为无连续行情的固收类基准，按公开监管/行业数据人工维护，非实时报价。",
    "as_of": "2026-09",
    "deposit_1y": 0.0110,          # 1年期定期存款挂牌利率
    "deposit_3y": 0.0155,          # 3年期定期存款
    "money_fund": 0.0145,          # 货币基金 7 日年化中枢
    "bank_wealth_1y": 0.0230,      # 1年期银行理财业绩比较基准中枢
    "bank_wealth_vol": 0.0060,     # 理财净值波动（近似）
    "annuity_irr": 0.0250,         # 商业养老年金 IRR 中枢
    "mortgage_lpr_5y": 0.0355,     # 5年期以上 LPR（房贷基准）
    "cpf_like": 0.0150,            # 公积金存款利率
    "inflation_cpi": 0.0060,       # CPI 同比中枢（用于实际购买力折算）
}

TRADING_DAYS = 244.0

# 份额折算 / 拆分检测阈值。
# 非复权价序列里，基金份额折算（如 2:1 拆分）会造成单日价格腰斩，
# 在统计上表现为一个不存在的巨大回撤。实测：纳指ETF 513100 的原始序列
# 最大回撤 -85.5%、年化 -0.67%，而同期纳斯达克大幅上涨 —— 明显是拆分伪影。
SPLIT_DROP_TH = -0.25     # 单日跌幅超过 25% → 判为份额折算
SPLIT_RISE_TH = 1.00      # 单日涨幅超过 100% → 判为反向折算

# 腾讯接口单次返回上限：2000 条可用，2600 报 "param error"。
# 2000 交易日 ≈ 8.2 年，覆盖 2018 熊市 / 2020 疫情 / 2022 加息 / 2023-24 A股长熊 /
# 2024 政策底，足以支撑压力测试的情景选取。
MAX_BARS = 2000


# ── 抓取 ────────────────────────────────────────────────────────────────────

def fetch_kline(code: str, count: int = MAX_BARS, retries: int = 3) -> dict | None:
    """抓取单标的日线。

    先试 kline/kline（非复权，历史更长，ETF 可回溯到 2018-06），
    再退到 fqkline/get（前复权，但 ETF 只有约 639 条）。

    count 上限 MAX_BARS；超过上限接口会返回
    {"code":0,"msg":"param error","data":[]}，故此处主动钳制。
    """
    count = max(30, min(int(count), MAX_BARS))
    attempts = [
        (KLINE_URL_RAW, {"param": f"{code},day,,,{count}"}, "raw"),
        (KLINE_URL, {"param": f"{code},day,,,{count},qfq"}, "qfq"),
    ]
    last_err = None
    for url, params, mode in attempts:
        for attempt in range(retries):
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=25)
                r.raise_for_status()
                payload = r.json()
                if payload.get("code") != 0:
                    last_err = f"code={payload.get('code')} msg={payload.get('msg')}"
                    time.sleep(0.6 * (attempt + 1))
                    continue
                data = payload.get("data")
                # 无数据时 data 为 []（param error / 标的不存在），不是 dict
                if not isinstance(data, dict):
                    last_err = "data 为空"
                    break
                node = data.get(code)
                if not isinstance(node, dict):
                    last_err = "标的不存在"
                    break
                # 字段名随标的类型与接口变化：day / qfqday / hfqday
                series = None
                for key in ("day", "qfqday", "hfqday"):
                    if node.get(key):
                        series = node[key]
                        break
                if not series:
                    last_err = "无 K 线序列"
                    break

                bars = []
                for row in series:
                    # 列序：[日期, 开, 收, 高, 低, 成交量, ...]
                    # 存完整 OHLCV —— 只存收盘价会让前端画不出真正的蜡烛图。
                    if not isinstance(row, (list, tuple)) or len(row) < 6:
                        continue
                    try:
                        bars.append({
                            "d": str(row[0]),
                            "o": float(row[1]),
                            "c": float(row[2]),
                            "h": float(row[3]),
                            "l": float(row[4]),
                            "v": float(row[5]),
                        })
                    except (ValueError, TypeError):
                        continue
                if len(bars) < 30:
                    last_err = f"样本不足({len(bars)})"
                    break
                return {"code": code, "bars": bars, "n": len(bars), "mode": mode}
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                time.sleep(0.6 * (attempt + 1))
    return {"code": code, "error": last_err}


# ── 统计 ────────────────────────────────────────────────────────────────────

def repair_splits(bars: list[dict]) -> tuple[list[dict], list[dict]]:
    """检测并修复份额折算/拆分造成的价格跳变。

    做法：找出单日异常跳变点（跌幅 > 25% 或涨幅 > 100%），把该点**之前**的
    全部价格按跳变比例缩放，使序列连续；最后整体缩放到与原始最新收盘价一致，
    这样近期价格水平保持自然。

    只影响价格水平，不影响收益率序列的正确性 —— 修复前那些"假崩盘"会
    污染波动率、最大回撤与相关系数矩阵。

    开、高、低三个价格必须与收盘价同步缩放，否则蜡烛图会出现假的长影线。

    返回 (修复后的 bars, 事件列表)。
    """
    if len(bars) < 3:
        return bars, []
    closes = [float(b["c"]) for b in bars]
    n = len(closes)

    jumps: list[tuple[int, float]] = []
    for i in range(1, n):
        prev, cur = closes[i - 1], closes[i]
        if prev <= 0:
            continue
        r = cur / prev - 1.0
        if r < SPLIT_DROP_TH or r > SPLIT_RISE_TH:
            jumps.append((i, cur / prev))

    if not jumps:
        return bars, []

    factors = [1.0] * n
    for i, k in jumps:
        for j in range(i):
            factors[j] *= k

    adjusted = [closes[i] * factors[i] for i in range(n)]
    if adjusted[-1] > 1e-9:
        scale = closes[-1] / adjusted[-1]
        adjusted = [c * scale for c in adjusted]

    events = []
    for i, k in jumps:
        events.append({
            "date": bars[i]["d"],
            "prev_close": round(closes[i - 1], 4),
            "close": round(closes[i], 4),
            "ratio": round(k, 6),
            "apparent_return": round(k - 1.0, 4),
        })

    out = []
    for i, b in enumerate(bars):
        f = factors[i] * (closes[-1] / adjusted[-1] if adjusted[-1] > 1e-9 else 1.0)
        row = {"d": b["d"], "c": round(float(b["c"]) * f, 6)}
        for k in ("o", "h", "l"):
            if b.get(k) is not None:
                row[k] = round(float(b[k]) * f, 6)
        if b.get("v") is not None:
            row["v"] = float(b["v"])
        out.append(row)
    return out, events

def compute_stats(bars: list[dict]) -> dict:
    """由收盘价序列计算年化收益、年化波动、最大回撤、日收益序列。"""
    closes = [b["c"] for b in bars if b["c"] > 0]
    dates = [b["d"] for b in bars if b["c"] > 0]
    if len(closes) < 60:
        return {}

    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    n = len(rets)

    # 年化收益：几何
    years = n / TRADING_DAYS
    total = closes[-1] / closes[0]
    ann_ret = total ** (1.0 / years) - 1.0 if years > 0 and total > 0 else 0.0

    # 年化波动
    mu = statistics.fmean(rets)
    var = sum((x - mu) ** 2 for x in rets) / max(1, n - 1)
    ann_vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)

    # 最大回撤
    peak = closes[0]
    max_dd = 0.0
    dd_start = dd_end = dates[0]
    cur_peak_date = dates[0]
    for i, c in enumerate(closes):
        if c > peak:
            peak = c
            cur_peak_date = dates[i]
        dd = c / peak - 1.0
        if dd < max_dd:
            max_dd = dd
            dd_start, dd_end = cur_peak_date, dates[i]

    # 下行波动 / Sortino
    downside = [x for x in rets if x < 0]
    dvol = (math.sqrt(sum(x * x for x in downside) / max(1, len(downside)))
            * math.sqrt(TRADING_DAYS)) if downside else 0.0

    # VaR 95（历史模拟，日频）
    srt = sorted(rets)
    var95 = srt[max(0, int(0.05 * len(srt)) - 1)]

    return {
        "annual_return": round(ann_ret, 6),
        "annual_vol": round(ann_vol, 6),
        "max_drawdown": round(max_dd, 6),
        "max_dd_start": dd_start,
        "max_dd_end": dd_end,
        "downside_vol": round(dvol, 6),
        "sortino": round(ann_ret / dvol, 4) if dvol > 1e-9 else None,
        "var95_daily": round(var95, 6),
        "sharpe_rf0": round(ann_ret / ann_vol, 4) if ann_vol > 1e-9 else None,
        "start": dates[0],
        "end": dates[-1],
        "n_obs": n,
        "first_close": closes[0],
        "last_close": closes[-1],
        "_daily_returns": [round(x, 8) for x in rets],
        "_dates": dates[1:],
    }


def correlation_matrix(returns_by_code: dict[str, list[float]]) -> dict:
    """按共同日期对齐后计算皮尔逊相关系数矩阵。"""
    codes = [c for c in returns_by_code if returns_by_code[c]]
    if len(codes) < 2:
        return {}
    # 统一长度：取最短的尾部对齐（各序列交易日历基本一致）
    m = min(len(returns_by_code[c]) for c in codes)
    series = {c: returns_by_code[c][-m:] for c in codes}
    # 尾部对齐后按同索引配对（交易日历在 A 股内部一致；跨市场会有偏差，标注为近似）
    means = {c: statistics.fmean(series[c]) for c in codes}
    sds = {}
    for c in codes:
        v = sum((x - means[c]) ** 2 for x in series[c]) / max(1, m - 1)
        sds[c] = math.sqrt(v)

    matrix = {}
    for i, a in enumerate(codes):
        row = {}
        for b in codes[i:]:
            if sds[a] < 1e-12 or sds[b] < 1e-12:
                rho = 0.0
            else:
                cov = sum((series[a][k] - means[a]) * (series[b][k] - means[b])
                          for k in range(m)) / max(1, m - 1)
                rho = cov / (sds[a] * sds[b])
            row[b] = round(rho, 4)
            matrix.setdefault(b, {})[a] = round(rho, 4)
        matrix.setdefault(a, {}).update(row)
    return matrix


# ── 主流程 ──────────────────────────────────────────────────────────────────

def probe() -> int:
    print(f"探测 {len(UNIVERSE)} 个标的（腾讯 gtimg）…\n")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda u: (u, fetch_kline(u[0], count=30)), UNIVERSE))

    ok = [(u, r) for u, r in results if r and r.get("bars")]
    bad = [(u, r) for u, r in results if not (r and r.get("bars"))]

    print(f"{'代码':<12}{'名称':<20}{'大类':<16}{'细分':<16}{'条数':>6}  {'区间'}")
    print("-" * 104)
    for (code, name, cls, sub, tradable), r in ok:
        marks = "★" if tradable else " "
        print(f"{marks}{code:<11}{name:<20}{cls:<16}{sub:<16}{r['n']:>6}  "
              f"{r['bars'][0]['d']} → {r['bars'][-1]['d']}")
    if bad:
        print(f"\n不可用 {len(bad)} 个：")
        for (code, name, *_rest), r in bad:
            print(f"  {code:<12}{name:<20}{(r or {}).get('error', 'no data')[:50]}")
    print(f"\n可用 {len(ok)}/{len(UNIVERSE)}")
    return 0


def build(count: int = MAX_BARS) -> int:
    print(f"抓取 {len(UNIVERSE)} 个标的历史日线（每标的 {count} 条）…\n")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda u: (u, fetch_kline(u[0], count=count)), UNIVERSE))

    DATASET_DIR.mkdir(parents=True, exist_ok=True)

    history: dict[str, dict] = {}
    stats: dict[str, dict] = {}
    returns_by_code: dict[str, list[float]] = {}
    failed: list[str] = []
    split_log: dict[str, dict] = {}

    for (code, name, cls, sub, tradable), r in results:
        if not (r and r.get("bars")):
            failed.append(f"{code} {name}: {(r or {}).get('error', 'no data')}")
            continue
        raw_bars = r["bars"]
        bars, split_events = repair_splits(raw_bars)
        if split_events:
            split_log[code] = {"name": name, "n_events": len(split_events),
                               "events": split_events}
            print(f"  ⚙ {code:<11}{name:<20} 检测到 {len(split_events)} 次份额折算，已修复："
                  + "；".join(f"{e['date']} 比例{e['ratio']}" for e in split_events[:3]))
        st = compute_stats(bars)
        if not st:
            failed.append(f"{code} {name}: 样本不足")
            continue
        returns_by_code[code] = st.pop("_daily_returns")
        st.pop("_dates", None)

        # 货币类：收益口径改为「每日计息」推导，价格序列仅用于波动与相关
        if code in CASH_CODES:
            st["return_basis"] = "rate"
            st["price_annual_return"] = st["annual_return"]
            st["annual_return"] = RATE_TABLE["money_fund"]
            st["annual_vol"] = max(st["annual_vol"], 0.0015)
            st["sharpe_rf0"] = round(st["annual_return"] / st["annual_vol"], 4)
        else:
            st["return_basis"] = "price"

        history[code] = {
            "name": name, "asset_class": cls, "sub_class": sub,
            "tradable": tradable, "bars": bars,
            "price_adjust": r.get("mode", "raw"),
        }
        stats[code] = st
        print(f"  ✓ {code:<11}{name:<20}{st['n_obs']:>6} obs  "
              f"年化 {st['annual_return']*100:>7.2f}%  波动 {st['annual_vol']*100:>6.2f}%  "
              f"最大回撤 {st['max_drawdown']*100:>7.2f}%  "
              f"[{st['return_basis']}/{r.get('mode','raw')}]  {st['start']}→{st['end']}")

    corr = correlation_matrix(returns_by_code)

    (DATASET_DIR / "market_history.json").write_text(
        json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATASET_DIR / "market_stats.json").write_text(
        json.dumps({"stats": stats, "correlation": corr, "rate_table": RATE_TABLE},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (DATASET_DIR / "meta.json").write_text(json.dumps({
        "source": "腾讯证券行情接口 web.ifzq.gtimg.cn（日线；主源 kline/kline 非复权，"
                  "备源 fqkline/get 前复权）",
        "adjust_note": "ETF 在 fqkline 接口只返回约 639 条（2024 年起），改用 kline 接口可回溯至 "
                       "2018-06。kline 为非复权价；本资产池中黄金ETF/纳指ETF 基本不分红，"
                       "国债与企业债指数本身即全价指数，故影响可控。逐标的的口径见 "
                       "market_history.json 的 price_adjust 字段。",
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "n_symbols": len(history),
        "trading_days_per_year": TRADING_DAYS,
        "split_repair": {
            "note": "非复权序列中的份额折算/拆分已自动检测并修复（单日跌幅>25% 或涨幅>100% 判为折算）。"
                    "修复只调整价格水平，不影响收益率序列的正确性；未修复时这些假跳变会严重污染"
                    "波动率、最大回撤与相关系数矩阵。",
            "n_symbols_affected": len(split_log),
            "detail": split_log,
        },
        "rate_table_note": RATE_TABLE["_note"],
        "disclaimer": "历史统计不代表未来收益。本数据集仅用于配置模型的参数估计与压力测试，"
                      "不构成任何投资建议。",
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n写入 {DATASET_DIR}")
    print(f"  标的 {len(history)} 个，相关矩阵 {len(corr)}×{len(corr)}")
    if failed:
        print(f"  失败 {len(failed)}：")
        for f in failed:
            print(f"    - {f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="智能多维投资系统 · 内置数据集构建器")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--probe", action="store_true", help="只探测可用性")
    g.add_argument("--build", action="store_true", help="抓取并写入数据集")
    ap.add_argument("--count", type=int, default=MAX_BARS, help="每标的交易日条数上限")
    args = ap.parse_args()
    return probe() if args.probe else build(count=args.count)


if __name__ == "__main__":
    sys.exit(main())
