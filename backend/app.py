#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」后端服务（FastAPI）。

端点概览
--------
    GET  /                     前端单页（桌面与手机同一套）
    GET  /api/health           服务与依赖状态
    GET  /api/catalog          资产池目录
    GET  /api/example          示例家庭（一键填充演示）
    POST /api/analyze          启动一次配置审议（后台线程）
    GET  /api/stream/{run_id}  事件流（SSE，实时推流）
    GET  /api/result/{run_id}  最终结果
    GET  /api/runs             历史运行列表
    GET  /api/runs/{run_id}    读取指定历史运行
    GET  /api/qrcode           局域网访问二维码（PNG）
    POST /api/household        保存家庭档案
    GET  /api/household        读取已保存档案

设计说明：审议是长任务（数分钟），因此 POST /api/analyze 立即返回 run_id，
前端随即连 SSE 看进度。事件同时落内存历史，支持断线重连补发。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent          # backend/
PROJECT = ROOT.parent                            # 项目根
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request                     # noqa: E402
from fastapi.middleware.cors import CORSMiddleware                      # noqa: E402
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,  # noqa: E402
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles                             # noqa: E402
from pydantic import BaseModel                                           # noqa: E402

from data.market import MarketData                                       # noqa: E402
from data.realtime import RealtimeFeed, build_board, market_status       # noqa: E402
from engine.debate import DEFAULT_MAX_ROUNDS, run_advisory               # noqa: E402
from engine.household import HouseholdInput, HouseholdProfile           # noqa: E402
from engine.llm import LLMClient, resolve_api_key                        # noqa: E402
from engine.nightly import NightlyReview, NightlyScheduler               # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("multivest")

APP_NAME = "智能多维投资系统"
APP_NAME_EN = "Multivest"

FRONTEND = PROJECT / "frontend"

# 可写数据的落盘位置。
# PyInstaller onefile 模式下代码被解压到临时目录（_MEIPASS），退出即清空 ——
# 若把记录写在那里，用户关掉程序就丢数据。因此由桌面壳通过
# MULTIVEST_DATA_DIR（兼容旧名 HOMEWEALTH_DATA_DIR）指定一个持久目录。
_DATA_ROOT = Path(os.environ.get("MULTIVEST_DATA_DIR")
                  or os.environ.get("HOMEWEALTH_DATA_DIR") or ROOT)
RUNS_DIR = _DATA_ROOT / "runs"
PROFILES_DIR = _DATA_ROOT / "profiles"
DAILY_DIR = _DATA_ROOT / "daily"
for _d in (RUNS_DIR, PROFILES_DIR, DAILY_DIR):
    _d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=f"{APP_NAME} · 多智能体投研与家庭配置", version="2.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_market: Optional[MarketData] = None
_market_lock = threading.RLock()      # 注意：必须是可重入锁。
# get_feed() / get_scheduler() 会在持锁状态下调用 get_market()，
# 用普通 Lock 会自死锁（表现为服务卡在 "Waiting for application startup"）。
_feed: Optional[RealtimeFeed] = None


def get_market() -> MarketData:
    global _market
    with _market_lock:
        if _market is None:
            _market = MarketData()
        return _market


def get_feed() -> RealtimeFeed:
    global _feed
    with _market_lock:
        if _feed is None:
            _feed = RealtimeFeed(market=get_market())
        return _feed


# ─────────────────────────────────────────────────────────────────────────────
# 运行注册表
# ─────────────────────────────────────────────────────────────────────────────

class RunState:
    def __init__(self, run_id: str, req: dict, max_rounds: int):
        self.run_id = run_id
        self.req = req
        self.max_rounds = max_rounds
        self.events: list[dict] = []
        self.subscribers: list[queue.Queue] = []
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.done = threading.Event()
        self.started_at = time.time()
        self.lock = threading.Lock()

    def emit(self, ev: dict) -> None:
        with self.lock:
            self.events.append(ev)
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def snapshot(self, since: int = 0) -> list[dict]:
        with self.lock:
            return list(self.events[since:])


RUNS: dict[str, RunState] = {}
RUNS_LOCK = threading.Lock()


def _runs_snapshot() -> dict:
    """给 NightlyScheduler.status() 用的只读快照。

    它需要知道"此刻有没有一次研判真的在跑"，但注册表在本模块 ——
    用注入而不是反向 import，避免循环依赖。
    """
    with RUNS_LOCK:
        return {rid: {"done": st.done.is_set(), "started": st.started_at}
                for rid, st in RUNS.items()}


try:
    from engine.nightly import set_runs_provider as _set_runs_provider

    _set_runs_provider(_runs_snapshot)
except Exception:  # noqa: BLE001
    pass


def _execute_run(state: RunState, llm: LLMClient) -> None:
    try:
        market = get_market()
        result = run_advisory(
            household_dict=state.req,
            market=market,
            llm=llm,
            emit=state.emit,
            max_rounds=state.max_rounds,
            run_dir=RUNS_DIR,
            run_id=state.run_id,      # 与对外暴露的 id 一致，否则历史记录列不出来
        )
        state.result = result
    except Exception as exc:  # noqa: BLE001
        logger.exception("运行 %s 失败", state.run_id)
        state.error = f"{type(exc).__name__}: {exc}"
        state.emit({"type": "error", "message": state.error})
    finally:
        state.done.set()
        state.emit({"type": "__end__"})


# ─────────────────────────────────────────────────────────────────────────────
# 请求模型
# ─────────────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    household: dict
    max_rounds: int = DEFAULT_MAX_ROUNDS


# ─────────────────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────────────────

def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

# ── 应用内设置：密钥与模型 ────────────────────────────────────────────────────
# 桌面应用应该在首次启动时就问密钥，而不是让用户去找 .env 手写。
# 安全约定：GET 只返回掩码，绝不回传明文。

@app.get("/api/settings")
def settings_get() -> dict:
    """当前生效的配置。密钥以掩码形式返回（sk-ff9••••••••e504）。"""
    from engine import settings as S

    return S.public_view()


class SettingsIn(BaseModel):
    api_key: Optional[str] = None      # None = 不改；"" = 清除
    base_url: Optional[str] = None
    model: Optional[str] = None


@app.post("/api/settings")
def settings_save(body: SettingsIn) -> dict:
    """保存配置。

    传 `null` 的字段保持不变，传空串则清除 —— 这样界面上改模型时
    不必把密钥再传一遍。
    """
    from engine import settings as S

    S.save(api_key=body.api_key, base_url=body.base_url, model=body.model)
    # 说明：这里不需要清理任何缓存 —— LLMClient 每次都是新建的，
    # 配置在构造时读取，所以下一次调用自然用上新值。
    logger.info("应用内设置已更新（model=%s, base_url=%s, key=%s）",
                body.model, body.base_url, S.mask(body.api_key) or "未改动")
    return S.public_view()


@app.post("/api/settings/test")
def settings_test(body: SettingsIn) -> dict:
    """试连一次，返回结果与延迟。不保存。

    允许带上尚未保存的候选值 —— 用户应当能"先试再存"，而不是存错了再回退。
    """
    from engine import settings as S

    cur = S.resolve()
    key = body.api_key.strip() if (body.api_key and body.api_key.strip()) else cur["api_key"]
    base = (body.base_url or cur["base_url"]).rstrip("/")
    model = body.model or cur["model"]
    if not key:
        return {"ok": False, "error": "未提供 API Key"}
    try:
        c = LLMClient(api_key=key, base_url=base, model=model,
                      timeout=30, max_retries=1)
        r = c.probe()
        return {**r, "model": model, "base_url": base}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "model": model, "base_url": base}


# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health(request: Request) -> dict:
    key, source = resolve_api_key()
    try:
        m = get_market()
        meta = m.meta
        n_symbols = len(m.symbols)
    except Exception as exc:  # noqa: BLE001
        meta, n_symbols = {"error": str(exc)}, 0
    port = request.url.port or 8760
    return {
        "ok": True,
        "app": APP_NAME,
        "app_en": APP_NAME_EN,
        "version": app.version,
        "llm": {"available": bool(key), "source": source,
                "model": LLMClient().model, "base_url": LLMClient().base_url},
        "dataset": {"n_symbols": n_symbols, **meta},
        "lan_url": f"http://{lan_ip()}:{port}",
        "local_url": f"http://127.0.0.1:{port}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 每晚研判
# ─────────────────────────────────────────────────────────────────────────────

_scheduler: Optional[NightlyScheduler] = None
NIGHTLY_AT = os.environ.get("MULTIVEST_NIGHTLY_AT", "20:30")


def get_scheduler() -> NightlyScheduler:
    global _scheduler
    with _market_lock:
        if _scheduler is None:
            _scheduler = NightlyScheduler(
                feed=get_feed(), market=get_market(),
                llm_factory=LLMClient, daily_dir=DAILY_DIR,
                at=NIGHTLY_AT, max_rounds=3,
                enabled=os.environ.get("MULTIVEST_NIGHTLY_ENABLED", "1") != "0",
            )
        return _scheduler


@app.on_event("startup")
def _startup() -> None:
    try:
        get_scheduler().start()
    except Exception:  # noqa: BLE001
        logger.exception("调度器启动失败")


def _daily_files() -> list[Path]:
    return sorted((p for p in DAILY_DIR.glob("*.json") if p.stem[:4].isdigit()),
                  reverse=True)


@app.get("/api/daily/status")
def daily_status() -> dict:
    return get_scheduler().status()


@app.get("/api/daily/list")
def daily_list() -> dict:
    out = []
    for p in _daily_files()[:120]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        d = r.get("dashboard") or {}
        sc = d.get("scores") or {}
        out.append({
            "date": r.get("date") or p.stem,
            "generated_at": r.get("generated_at"),
            "elapsed_sec": r.get("elapsed_sec"),
            "degraded": bool(r.get("degraded")),
            "summary": d.get("summary", ""),
            "scores": {k: v.get("score") for k, v in sc.items()},
            "position": (d.get("position") or {}).get("suggested"),
            "n_actions": len(d.get("actions") or []),
            "avg_change": (r.get("board") or {}).get("breadth", {}).get("avg_change"),
        })
    return {"reports": out}


def _read_daily(day: str) -> Optional[dict]:
    p = DAILY_DIR / f"{day}.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


@app.get("/api/daily/latest")
def daily_latest() -> dict:
    files = _daily_files()
    if not files:
        raise HTTPException(404, "还没有任何研判报告")
    return json.loads(files[0].read_text(encoding="utf-8"))


@app.get("/api/daily/{day}")
def daily_get(day: str) -> dict:
    r = _read_daily(day)
    if r is None:
        raise HTTPException(404, f"未找到 {day} 的研判报告")
    return r


@app.post("/api/daily/run")
def daily_run(day: str = "", force: bool = False, offline: bool = False) -> dict:
    """手动触发一次研判（默认今天）。返回 run_id，随后连 SSE 看进度。

    offline=1：跳过全部模型调用，只出由行情数据确定性计算的客观看板。
              模型后端不可用、或只想看客观指标时使用。
    """
    if not offline:
        key, source = resolve_api_key()
        if not key:
            raise HTTPException(400, f"未配置 DEEPSEEK_API_KEY（{source}）。"
                                     f"如需仅客观指标，请传 offline=1")

    target = day or datetime.now().date().isoformat()
    if not force and _read_daily(target) is not None:
        return {"run_id": target, "status": "exists",
                "message": f"{target} 的研判已存在，若要重跑请传 force=1"}

    run_id = f"nightly_{target.replace('-', '')}" + ("_offline" if offline else "")
    state = RunState(run_id, {"date": target}, 3)
    with RUNS_LOCK:
        RUNS[run_id] = state

    def _job() -> None:
        try:
            review = NightlyReview(get_feed(), get_market(),
                                   LLMClient(on_event=state.emit), state.emit,
                                   DAILY_DIR, max_rounds=3, run_date=target,
                                   offline=offline)
            state.result = review.run()
        except Exception as exc:  # noqa: BLE001
            logger.exception("研判运行失败")
            state.error = f"{type(exc).__name__}: {exc}"
            state.emit({"type": "error", "message": state.error})
        finally:
            state.done.set()
            state.emit({"type": "__end__"})

    threading.Thread(target=_job, daemon=True, name=f"nightly-{target}").start()
    logger.info("手动触发研判 %s（offline=%s）", target, offline)
    return {"run_id": run_id, "status": "started", "date": target, "offline": offline}


# ─────────────────────────────────────────────────────────────────────────────
# 实时行情
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/board")
def board(force: bool = False) -> dict:
    """首页「行情总览」的完整数据：分组报价、涨跌宽度、大类聚合、涨跌排行。"""
    try:
        return build_board(get_feed(), get_market())
    except Exception as exc:  # noqa: BLE001
        logger.exception("行情总览组装失败")
        raise HTTPException(502, f"行情拉取失败：{exc}") from exc


@app.get("/api/minute/{code}")
def minute(code: str) -> dict:
    """单个标的的当日分时序列（用于分时图）。"""
    return get_feed().minute(code)


@app.get("/api/market")
def market_now() -> dict:
    return {"market": market_status(), **(get_feed().status())}


@app.get("/api/candles/{code}")
def candles(code: str, limit: int = 160) -> dict:
    """日 K 线（来自内置数据集，含完整 OHLCV，用于蜡烛图）。"""
    m = get_market()
    node = m._history.get(code)
    if not node:
        raise HTTPException(404, f"未找到标的 {code}")
    bars = node.get("bars", [])[-max(20, min(limit, 800)):]
    out = []
    for b in bars:
        c = float(b["c"])
        out.append({
            "d": b["d"],
            "o": float(b.get("o") or c),
            "h": float(b.get("h") or c),
            "l": float(b.get("l") or c),
            "c": c,
            "v": float(b.get("v") or 0),
        })
    return {"code": code, "name": node.get("name", code),
            "asset_class": node.get("asset_class"), "bars": out}


@app.get("/api/vol")
def vol_forecast() -> dict:
    """波动率预测、波动目标化仓位乘数，以及配套的可检验证据。

    与 /api/quant 的分工：
      · /api/quant 预测涨跌方向 —— 样本外 AUC 的 95% 分块区间跨越 0.5，
        保留它是为了公开诊断结果，不作为决策依据。
      · /api/vol 度量风险状态 —— HAR（Corsi 2009）配 Yang-Zhang 极差估计量，
        经模型置信集检验属于 90% 置信集，并附 VaR 回测与波动率管理回归的实测结果。

    **本项目量化预测部分仅参考，请务必谨慎用于投资决策。**
    """
    from engine.vol import get_engine

    try:
        return get_engine().forecast(get_market())
    except Exception as exc:  # noqa: BLE001
        logger.exception("波动率预测失败")
        raise HTTPException(500, f"波动率预测失败：{exc}") from exc


@app.get("/api/quant")
def quant_predict() -> dict:
    """量化预测：逻辑回归 + 小型神经网络，附样本外 AUC 与可信度评级。"""
    from engine.quant import get_model

    try:
        return get_model().predict(get_market())
    except Exception as exc:  # noqa: BLE001
        logger.exception("量化预测失败")
        raise HTTPException(500, f"量化预测失败：{exc}") from exc


@app.get("/api/correlation")
def correlation(codes: str = "") -> dict:
    """相关系数矩阵。默认返回数据集全部标的（前端用于热力图）。"""
    m = get_market()
    if codes:
        want = [c.strip() for c in codes.split(",") if c.strip()]
        use = [c for c in want if c in m.symbols]
    else:
        # 按数据长度降序，保证矩阵对齐时窗口尽量长
        use = [s.code for s in sorted(m.symbols.values(), key=lambda x: -x.n_obs)]
    if len(use) < 2:
        raise HTTPException(400, "可用标的不足")
    matrix = [[round(m.correlation(a, b), 4) for b in use] for a in use]
    return {"codes": use, "labels": [m.symbols[c].name for c in use], "matrix": matrix}


@app.get("/api/trend")
def trend(codes: str = "", days: int = 120) -> dict:
    """多标的归一化走势（起点=100），用于对比折线图。"""
    m = get_market()
    if codes:
        use = [c.strip() for c in codes.split(",") if c.strip() and c.strip() in m.symbols]
    else:
        use = ["sh000300", "sh000905", "sz399006", "sh000922", "sh518880", "sh000012"]
        use = [c for c in use if c in m.symbols]
    if not use:
        raise HTTPException(400, "可用标的不足")
    days = max(20, min(int(days), 600))

    series, common = [], None
    for c in use:
        bars = (m._history.get(c) or {}).get("bars") or []
        bars = bars[-days:]
        if len(bars) < 5:
            continue
        closes = [float(b["c"]) for b in bars]
        base = closes[0] or 1.0
        dates = [b["d"] for b in bars]
        series.append({"name": m.symbols[c].name, "code": c,
                       "values": [round(v / base * 100, 3) for v in closes],
                       "dates": dates})
        common = dates if common is None else common
    return {"series": series, "days": days}


@app.get("/api/catalog")
def catalog() -> dict:
    m = get_market()
    return {"catalog": m.catalog(), "rates": m.rate_table, "meta": m.meta}


@app.get("/api/example")
def example() -> dict:
    """示例家庭：一线城市双职工、有房贷、一个孩子。"""
    return {
        "name": "示例家庭 · 三口之家",
        "head_age": 36, "retirement_age": 60, "dependents": 2, "city_tier": "新一线",
        "assets": {
            "cash_deposit": 220000, "money_fund": 80000, "bank_wealth": 150000,
            "bond_fund": 60000, "equity_fund": 180000, "pension_account": 45000,
            "insurance_cash": 60000, "property_self_use": 2600000,
            "property_invest": 0, "other": 30000,
        },
        "liabilities": {"mortgage_balance": 1450000, "consumer_debt": 20000, "other_debt": 0},
        "mortgage_rate": 0.0355, "consumer_rate": 0.07,
        "monthly_income": 42000, "income_stability": 4,
        "monthly_expense": 16000, "monthly_mortgage": 7800,
        "goals": [
            {"name": "子女大学教育金", "amount": 800000, "years": 14,
             "priority": "critical", "note": "国内本科+部分海外交流"},
            {"name": "补充养老金", "amount": 2000000, "years": 24,
             "priority": "important", "note": "社保之外的补充"},
        ],
        "has_medical_insurance": True, "has_critical_illness": False,
        "life_sum_assured": 500000,
        "risk_score": 58, "loss_tolerance_pct": 15, "experience_years": 4,
    }


@app.post("/api/preview")
def preview(payload: dict) -> dict:
    """只做确定性画像计算，不调用模型。前端据此实时显示风险测算与配置边界。"""
    try:
        inp = HouseholdInput.from_dict(payload or {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"家庭数据格式有误：{exc}") from exc
    prof = HouseholdProfile(inp, rates=get_market().rate_table)
    return prof.as_dict()


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    key, source = resolve_api_key()
    if not key:
        raise HTTPException(400, f"未配置 DEEPSEEK_API_KEY（{source}）。"
                                 f"请在项目根目录 .env 中写入后重启服务。")
    # 校验输入
    try:
        inp = HouseholdInput.from_dict(req.household or {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"家庭数据格式有误：{exc}") from exc
    if inp.monthly_income <= 0:
        raise HTTPException(400, "家庭月收入必须大于 0")

    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    state = RunState(run_id, inp.to_dict(), max(2, min(10, int(req.max_rounds or DEFAULT_MAX_ROUNDS))))
    with RUNS_LOCK:
        RUNS[run_id] = state

    llm = LLMClient(on_event=state.emit)
    t = threading.Thread(target=_execute_run, args=(state, llm), daemon=True,
                         name=f"advisory-{run_id}")
    t.start()
    logger.info("启动审议 %s（家庭：%s，最多 %d 轮）", run_id, inp.name, state.max_rounds)
    return {"run_id": run_id, "status": "started", "max_rounds": state.max_rounds,
            "household_name": inp.name}


@app.get("/api/stream/{run_id}")
def stream(run_id: str, request: Request) -> StreamingResponse:
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(404, f"未找到运行 {run_id}")

    def gen():
        cursor = 0
        q = state.subscribe()
        try:
            # 先补发历史事件（支持中途刷新页面/断线重连）
            for ev in state.snapshot(0):
                yield _sse(ev)
                cursor += 1
                if ev.get("type") == "__end__":
                    return
            last_beat = time.time()
            while True:
                if state.done.is_set() and q.empty():
                    yield _sse({"type": "__end__"})
                    return
                try:
                    ev = q.get(timeout=1.0)
                except queue.Empty:
                    # 心跳，防止代理断开
                    if time.time() - last_beat > 15:
                        last_beat = time.time()
                        yield ": keep-alive\n\n"
                    continue
                cursor += 1
                yield _sse(ev)
                if ev.get("type") == "__end__":
                    return
        finally:
            state.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"


@app.get("/api/result/{run_id}")
def result(run_id: str) -> dict:
    state = RUNS.get(run_id)
    if state:
        if state.error:
            raise HTTPException(500, state.error)
        if not state.done.is_set():
            return {"status": "running", "run_id": run_id,
                    "elapsed_sec": round(time.time() - state.started_at, 1)}
        if state.result:
            return {"status": "done", "run_id": run_id, **state.result}
    # 回落到磁盘
    p = RUNS_DIR / run_id / "result.json"
    if p.is_file():
        return {"status": "done", "run_id": run_id,
                **json.loads(p.read_text(encoding="utf-8"))}
    raise HTTPException(404, f"未找到结果 {run_id}")


@app.get("/api/runs")
def list_runs() -> dict:
    out = []
    # 不按前缀过滤：只要目录里有 result.json 就算一次审议。
    # （早期版本把结果写成 fam_* 而 API 用 run_*，前缀过滤导致历史记录永远为空。）
    for d in sorted((p for p in RUNS_DIR.iterdir() if p.is_dir()), reverse=True):
        f = d / "result.json"
        if not f.is_file():
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        prop = r.get("proposal") or {}
        out.append({
            "run_id": r.get("run_id") or d.name,
            "dir": d.name,
            "generated_at": r.get("generated_at"),
            "household_name": (r.get("household") or {}).get("name"),
            "elapsed_sec": r.get("elapsed_sec"),
            "rounds": (r.get("convergence") or {}).get("rounds"),
            "termination_track": (r.get("convergence") or {}).get("termination_track"),
            "final_cv": (r.get("convergence") or {}).get("final_cv"),
            "qc_passed": (r.get("qc") or {}).get("passed"),
            "weights": prop.get("weights"),
            "expected_return": (prop.get("forward") or {}).get("expected_return"),
            "expected_vol": (prop.get("forward") or {}).get("expected_vol"),
        })
    return {"runs": out[:100]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    """按 run_id（或目录名）取一次审议结果。"""
    p = RUNS_DIR / run_id / "result.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    # 回退：按内容里的 run_id 匹配目录名
    for d in RUNS_DIR.iterdir():
        f = d / "result.json"
        if not f.is_file():
            continue
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if r.get("run_id") == run_id:
            return r
    raise HTTPException(404, f"未找到 {run_id}")


@app.get("/api/qrcode")
def qrcode_png(request: Request, url: str = "") -> Response:
    """生成局域网访问地址的二维码 PNG，供手机扫码连接。"""
    target = url or f"http://{lan_ip()}:{request.url.port or 8760}"
    try:
        import io

        import qrcode  # type: ignore
        img = qrcode.make(target, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})
    except ImportError:
        # 无 qrcode 库时返回可读的 SVG 占位（前端会降级为纯文本显示地址）
        raise HTTPException(501, "未安装 qrcode 库，请运行 pip install qrcode")


@app.post("/api/household")
def save_household(payload: dict) -> dict:
    name = str(payload.get("name") or "未命名家庭").strip()
    safe = "".join(c for c in name if c.isalnum() or c in "-_（）()") or "household"
    p = PROFILES_DIR / f"{safe}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "path": str(p), "name": name}


@app.get("/api/household")
def load_household(name: str = "") -> dict:
    if name:
        safe = "".join(c for c in name if c.isalnum() or c in "-_（）()") or "household"
        p = PROFILES_DIR / f"{safe}.json"
        if not p.is_file():
            raise HTTPException(404, f"未找到档案 {name}")
        return json.loads(p.read_text(encoding="utf-8"))
    items = []
    for f in sorted(PROFILES_DIR.glob("*.json")):
        try:
            items.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return {"profiles": items}


@app.get("/api/market/{code}")
def market_symbol(code: str) -> dict:
    m = get_market()
    s = m.symbols.get(code)
    if not s:
        raise HTTPException(404, f"未找到标的 {code}")
    return {"symbol": s.as_dict(),
            "stats": m._stats.get(code, {}),
            "bars": (m._history.get(code) or {}).get("bars", [])[-500:]}


# ─────────────────────────────────────────────────────────────────────────────
# 静态前端
# ─────────────────────────────────────────────────────────────────────────────

if (FRONTEND / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND / "assets")), name="assets")


@app.get("/", response_class=HTMLResponse)
def index() -> Response:
    p = FRONTEND / "index.html"
    if not p.is_file():
        return HTMLResponse("<h1>智能多维投资系统</h1><p>前端文件缺失：frontend/index.html</p>",
                            status_code=500)
    return FileResponse(str(p), media_type="text/html; charset=utf-8")


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    p = FRONTEND / "manifest.webmanifest"
    if p.is_file():
        return FileResponse(str(p), media_type="application/manifest+json")
    return Response("{}", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> Response:
    p = FRONTEND / "sw.js"
    if p.is_file():
        return FileResponse(str(p), media_type="application/javascript")
    return Response("// no service worker", media_type="application/javascript")


@app.get("/{path:path}")
def static_or_index(path: str) -> Response:
    """兜底：命中 frontend 下的静态文件，否则回落到单页。"""
    cand = (FRONTEND / path).resolve()
    try:
        cand.relative_to(FRONTEND.resolve())
    except ValueError:
        raise HTTPException(403, "非法路径")
    if cand.is_file():
        return FileResponse(str(cand))
    return index()


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="智能多维投资系统 后端服务")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（0.0.0.0 允许手机访问）")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    print("=" * 68)
    print(f"  {APP_NAME}")
    print(f"  {APP_NAME_EN}")
    print("=" * 68)
    print(f"  本机访问：  http://127.0.0.1:{args.port}")
    print(f"  手机访问：  http://{lan_ip()}:{args.port}   （需在同一 WiFi）")
    print("=" * 68)
    uvicorn.run("app:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
