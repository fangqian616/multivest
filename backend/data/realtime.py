#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」实时行情层。

数据源：腾讯行情
    · 实时快照  https://qt.gtimg.cn/q=<codes>          （GBK 编码）
    · 当日分时  https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=<code>

设计要点
--------
1. **永不抛异常**：行情接口是外部依赖，任何一次失败都不能让首页白屏。
   取不到就回退到内置数据集的最后一根日线，并标记 `stale=True`。
2. **带 TTL 的缓存**：交易时段 15 秒、非交易时段 5 分钟。
   避免前端轮询把上游打爆，也避免休市时反复请求同一份不变的数据。
3. **市场状态自算**：A 股 09:30–11:30 / 13:00–15:00（周一至周五），
   另识别集合竞价（09:15–09:25）与午间休市。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Iterable, Optional

import requests

logger = logging.getLogger(__name__)

SNAPSHOT_URL = "https://qt.gtimg.cn/q="
MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}

TTL_TRADING = 15.0      # 交易时段缓存秒数
TTL_CLOSED = 300.0      # 非交易时段

# 腾讯快照的字段下标（已按实际返回逐位核对）
F_NAME, F_CODE, F_PRICE, F_PREV, F_OPEN = 1, 2, 3, 4, 5
F_VOLUME, F_TIME = 6, 30
F_CHANGE, F_CHANGE_PCT = 31, 32
F_HIGH, F_LOW = 33, 34
F_AMOUNT, F_TURNOVER = 37, 38
F_PE, F_AMPLITUDE = 39, 43
F_FLOAT_CAP, F_TOTAL_CAP, F_PB = 44, 45, 46

SNAPSHOT_RE = re.compile(r'v_([A-Za-z0-9_]+)="([^"]*)"')

# 首页「行情总览」的核心观察标的（分组展示）。
# 与内置数据集的 22 个标的有重叠，但也包含 A 股大盘指数等仅用于观察的品种。
BENCHMARKS: list[dict] = [
    {"code": "sh000001", "label": "上证指数", "group": "A股宽基"},
    {"code": "sh000300", "label": "沪深300", "group": "A股宽基"},
    {"code": "sh000905", "label": "中证500", "group": "A股宽基"},
    {"code": "sh000852", "label": "中证1000", "group": "A股宽基"},
    {"code": "sh000016", "label": "上证50", "group": "A股宽基"},
    {"code": "sz399006", "label": "创业板指", "group": "A股成长"},
    {"code": "sh000688", "label": "科创50", "group": "A股成长"},
    {"code": "sh000922", "label": "中证红利", "group": "A股策略"},
    {"code": "hkHSI", "label": "恒生指数", "group": "港股"},
    {"code": "sh510900", "label": "H股ETF", "group": "港股"},
    {"code": "sh513180", "label": "恒生科技ETF", "group": "港股"},
    {"code": "sh513100", "label": "纳指ETF", "group": "海外"},
    {"code": "sh513500", "label": "标普500ETF", "group": "海外"},
    {"code": "sh000012", "label": "上证国债", "group": "债券"},
    {"code": "sh511010", "label": "国债ETF", "group": "债券"},
    {"code": "sh511260", "label": "十年国债ETF", "group": "债券"},
    {"code": "sh000832", "label": "中证转债", "group": "债券"},
    {"code": "sh511380", "label": "可转债ETF", "group": "债券"},
    {"code": "sh518880", "label": "黄金ETF", "group": "商品"},
    {"code": "sh000819", "label": "有色金属", "group": "商品"},
    {"code": "sh511990", "label": "华宝添益", "group": "现金"},
]


# ─────────────────────────────────────────────────────────────────────────────
# 市场状态
# ─────────────────────────────────────────────────────────────────────────────

def market_status(now: Optional[datetime] = None) -> dict:
    """判断 A 股市场状态。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return {"state": "closed", "label": "周末休市", "tradeable": False}

    t = now.time()
    if dtime(9, 15) <= t < dtime(9, 25):
        return {"state": "auction", "label": "集合竞价", "tradeable": False}
    if dtime(9, 25) <= t < dtime(9, 30):
        return {"state": "auction", "label": "开盘前", "tradeable": False}
    if dtime(9, 30) <= t < dtime(11, 30):
        return {"state": "open", "label": "交易中", "tradeable": True}
    if dtime(11, 30) <= t < dtime(13, 0):
        return {"state": "break", "label": "午间休市", "tradeable": False}
    if dtime(13, 0) <= t < dtime(15, 0):
        return {"state": "open", "label": "交易中", "tradeable": True}
    if t >= dtime(15, 0):
        return {"state": "closed", "label": "已收盘", "tradeable": False}
    return {"state": "closed", "label": "未开盘", "tradeable": False}


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Quote:
    code: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0          # 手
    amount: float = 0.0          # 元
    turnover: float = 0.0        # %
    amplitude: float = 0.0       # %
    pe: float = 0.0
    pb: float = 0.0
    total_cap: float = 0.0       # 亿元
    ts: str = ""                 # 上游时间戳 YYYY-MM-DD HH:MM:SS
    stale: bool = False          # True = 上游不可用，回退到内置数据集
    source: str = "tencent"

    def as_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name,
            "price": round(self.price, 4),
            "prev_close": round(self.prev_close, 4),
            "open": round(self.open, 4),
            "high": round(self.high, 4),
            "low": round(self.low, 4),
            "change": round(self.change, 4),
            "change_pct": round(self.change_pct / 100.0, 6),   # 统一成小数
            "volume": self.volume, "amount": self.amount,
            "turnover": self.turnover, "amplitude": self.amplitude,
            "pe": self.pe, "pb": self.pb, "total_cap": self.total_cap,
            "ts": self.ts, "stale": self.stale, "source": self.source,
        }


def _f(parts: list[str], idx: int, default: float = 0.0) -> float:
    try:
        v = parts[idx]
        return float(v) if v not in ("", "-", "null") else default
    except (IndexError, ValueError, TypeError):
        return default


def _s(parts: list[str], idx: int, default: str = "") -> str:
    try:
        return parts[idx].strip()
    except (IndexError, AttributeError, TypeError):
        return default


def _fmt_ts(raw: str) -> str:
    """20260922092000 -> 2026-09-22 09:20:00"""
    raw = (raw or "").strip()
    if len(raw) >= 14 and raw[:14].isdigit():
        return (f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} "
                f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}")
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# 行情客户端
# ─────────────────────────────────────────────────────────────────────────────

class RealtimeFeed:
    """带缓存与降级的实时行情客户端（线程安全）。"""

    def __init__(self, market=None):
        self.market = market                    # data.market.MarketData，用于降级
        self._lock = threading.Lock()
        self._cache: dict[str, Quote] = {}
        self._cache_at: float = 0.0
        self._minute_cache: dict[str, tuple[float, dict]] = {}
        self._last_error: str = ""
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ── 快照 ──────────────────────────────────────────────────────────────
    def quotes(self, codes: Iterable[str], force: bool = False) -> dict[str, Quote]:
        codes = [c for c in dict.fromkeys(codes) if c]
        if not codes:
            return {}

        status = market_status()
        ttl = TTL_TRADING if status["tradeable"] else TTL_CLOSED
        now = time.time()

        with self._lock:
            cached_ok = (now - self._cache_at) < ttl
            missing = [c for c in codes if c not in self._cache]
        if not force and cached_ok and not missing:
            with self._lock:
                return {c: self._cache[c] for c in codes}

        fetched = self._fetch(codes)
        with self._lock:
            if fetched:
                self._cache.update(fetched)
                self._cache_at = now
            # 上游缺失的用缓存/内置数据补齐
            out: dict[str, Quote] = {}
            for c in codes:
                q = self._cache.get(c) or fetched.get(c)
                if q is None:
                    q = self._fallback(c)
                out[c] = q
        return out

    def _fetch(self, codes: list[str]) -> dict[str, Quote]:
        """批量拉取快照。分批以控制 URL 长度。"""
        out: dict[str, Quote] = {}
        for i in range(0, len(codes), 60):
            batch = codes[i:i + 60]
            try:
                r = self.session.get(SNAPSHOT_URL + ",".join(batch), timeout=8)
                if r.status_code != 200:
                    self._last_error = f"HTTP {r.status_code}"
                    continue
                text = r.content.decode("gbk", errors="replace")
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("实时行情拉取失败：%s", self._last_error)
                continue

            for m in SNAPSHOT_RE.finditer(text):
                code, payload = m.group(1), m.group(2)
                if not payload or "~" not in payload:
                    continue
                parts = payload.split("~")
                if len(parts) < 35:
                    continue
                price = _f(parts, F_PRICE)
                prev = _f(parts, F_PREV)
                if price <= 0 and prev <= 0:
                    continue
                q = Quote(
                    code=code,
                    name=_s(parts, F_NAME) or code,
                    price=price if price > 0 else prev,
                    prev_close=prev,
                    open=_f(parts, F_OPEN),
                    high=_f(parts, F_HIGH),
                    low=_f(parts, F_LOW),
                    change=_f(parts, F_CHANGE),
                    change_pct=_f(parts, F_CHANGE_PCT),
                    volume=_f(parts, F_VOLUME),
                    amount=_f(parts, F_AMOUNT) * 10000.0,   # 上游单位：万元
                    turnover=_f(parts, F_TURNOVER),
                    amplitude=_f(parts, F_AMPLITUDE),
                    pe=_f(parts, F_PE),
                    pb=_f(parts, F_PB),
                    total_cap=_f(parts, F_TOTAL_CAP),
                    ts=_fmt_ts(_s(parts, F_TIME)),
                )
                out[code] = q
        return out

    def _fallback(self, code: str) -> Quote:
        """上游取不到时，用内置数据集的最后一根日线兜底。"""
        q = Quote(code=code, name=code, stale=True, source="dataset")
        try:
            m = self.market
            if m is None:
                return q
            sym = m.symbols.get(code)
            if sym:
                q.name = sym.name
            bars = (m._history.get(code) or {}).get("bars") or []
            if len(bars) >= 1:
                q.price = float(bars[-1]["c"])
                q.ts = str(bars[-1]["d"])
            if len(bars) >= 2:
                q.prev_close = float(bars[-2]["c"])
                q.change = q.price - q.prev_close
                if q.prev_close:
                    q.change_pct = q.change / q.prev_close * 100.0
        except Exception:  # noqa: BLE001
            pass
        return q

    # ── 分时 ──────────────────────────────────────────────────────────────
    def minute(self, code: str, force: bool = False) -> dict:
        """当日分时序列。返回 {"code","prev_close","points":[{"t","p","v"}],"stale"}"""
        now = time.time()
        with self._lock:
            hit = self._minute_cache.get(code)
        if hit and not force and (now - hit[0]) < TTL_TRADING:
            return hit[1]

        result = {"code": code, "prev_close": 0.0, "points": [], "stale": False}
        try:
            r = self.session.get(MINUTE_URL, params={"code": code}, timeout=8)
            data = r.json()
            node = (data.get("data") or {}).get(code) or {}
            prev = float(node.get("prec") or 0)
            rows = ((node.get("data") or {}).get("data")) or []
            points = []
            for row in rows:
                # 格式："0930 4539.35 12345 6789.0"（时间 价格 成交量 成交额）
                seg = str(row).split()
                if len(seg) < 2:
                    continue
                hhmm = seg[0]
                if len(hhmm) == 4 and hhmm.isdigit():
                    hhmm = f"{hhmm[:2]}:{hhmm[2:]}"
                try:
                    points.append({"t": hhmm, "p": float(seg[1]),
                                   "v": float(seg[2]) if len(seg) > 2 else 0.0})
                except ValueError:
                    continue
            # 上游在开盘初期不给 prec 字段，回退到快照的昨收，
            # 否则前端的红绿分区与涨跌幅会全错。
            if prev <= 0:
                snap = self.quotes([code]).get(code)
                prev = float(snap.prev_close or 0) if snap else 0.0
            result.update({"prev_close": prev, "points": points})
        except Exception as exc:  # noqa: BLE001
            logger.warning("分时拉取失败 %s：%s", code, exc)
            result["stale"] = True

        with self._lock:
            self._minute_cache[code] = (now, result)
        return result

    # ── 状态 ──────────────────────────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            n = len(self._cache)
            at = self._cache_at
        return {
            "market": market_status(),
            "cached_symbols": n,
            "cache_age_sec": round(time.time() - at, 1) if at else None,
            "last_error": self._last_error,
            "ttl_sec": TTL_TRADING if market_status()["tradeable"] else TTL_CLOSED,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 组装：首页「行情总览」所需的完整快照
# ─────────────────────────────────────────────────────────────────────────────

def _spark(market, code: str, n: int = 60) -> list[float]:
    """最近 n 个收盘价（用于卡片里的迷你走势线）。"""
    try:
        bars = (market._history.get(code) or {}).get("bars") or []
        return [round(float(b["c"]), 4) for b in bars[-n:]]
    except Exception:  # noqa: BLE001
        return []


def build_board(feed: RealtimeFeed, market) -> dict:
    """把实时快照 + 内置数据集的统计量拼成前端直接可渲染的结构。

    前端不需要再做任何行情计算 —— 分组、排行、汇总、迷你走势都在这里算好，
    这样手机上也不会有计算延迟。
    """
    codes = list(dict.fromkeys([b["code"] for b in BENCHMARKS]
                               + list(market.symbols.keys())))
    quotes = feed.quotes(codes)

    # 分组（附迷你走势）
    groups: dict[str, list[dict]] = {}
    for b in BENCHMARKS:
        q = quotes.get(b["code"])
        if not q:
            continue
        row = q.as_dict()
        row["label"] = b["label"]
        row["group"] = b["group"]
        row["spark"] = _spark(market, b["code"])
        groups.setdefault(b["group"], []).append(row)

    # 全部标的（含数据集里的可投工具）
    universe = []
    for code, sym in market.symbols.items():
        q = quotes.get(code)
        if not q:
            continue
        row = q.as_dict()
        row.update({"label": sym.name, "asset_class": sym.asset_class,
                    "sub_class": sym.sub_class, "tradable": sym.tradable,
                    "annual_vol": round(sym.annual_vol, 4),
                    "max_drawdown": round(sym.max_drawdown, 4)})
        universe.append(row)

    universe.sort(key=lambda r: r["change_pct"], reverse=True)

    # 当日汇总
    ups = [r for r in universe if r["change_pct"] > 0]
    downs = [r for r in universe if r["change_pct"] < 0]
    flats = [r for r in universe if r["change_pct"] == 0]
    ranked = sorted(universe, key=lambda r: r["change_pct"], reverse=True)

    # 按大类聚合当日表现（等权平均）
    cls_map: dict[str, list[float]] = {}
    for r in universe:
        cls_map.setdefault(r["asset_class"], []).append(r["change_pct"])
    by_class = [{"asset_class": k,
                 "change_pct": round(sum(v) / len(v), 6),
                 "n": len(v),
                 "up": sum(1 for x in v if x > 0),
                 "down": sum(1 for x in v if x < 0)}
                for k, v in cls_map.items()]
    by_class.sort(key=lambda r: r["change_pct"], reverse=True)

    stale = any(r["stale"] for r in universe) if universe else False
    return {
        "status": feed.status(),
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "stale": stale,
        "groups": [{"group": g, "items": items} for g, items in groups.items()],
        "universe": universe,
        "by_class": by_class,
        "breadth": {
            "up": len(ups), "down": len(downs), "flat": len(flats),
            "total": len(universe),
            "up_ratio": round(len(ups) / len(universe), 4) if universe else 0,
            "avg_change": (round(sum(r["change_pct"] for r in universe) / len(universe), 6)
                           if universe else 0),
        },
        "top_gainers": ranked[:5],
        "top_losers": ranked[-5:][::-1],
        "benchmarks": BENCHMARKS,
    }
