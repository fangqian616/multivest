#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波动率预测与「波动目标化」仓位 —— 本系统量化层的主力输出。

模型选择
--------
候选与评估口径见 `tools/quant_race.py`，结论（可复现，见 model.json）：

  · **代理变量**：用 OHLC 的极差类估计量代替"收盘价平方"。Yang-Zhang(2000)
    同时处理隔夜跳空、日内收益与漂移，在四个极差估计量中样本外 QLIKE 最优。
  · **模型**：三个尺度的 HAR（Corsi 2009）。用 Hansen-Lunde-Nason(2011)
    模型置信集检验，90% 置信集 = {HAR, HARQ}；
    持续性基准、EWMA(0.94)、HAR-CJ 与"在方差尺度直接回归"的对照组
    均被淘汰。**结构正确比模型复杂重要。**

这个模块输出什么
----------------
  1. 当前波动状态（历史分位）与 HAR 对未来 20 日的预测
  2. **波动目标化仓位乘数** —— 波动高时收缩、低时放开
  3. 与预测配套的**可检验证据**：模型置信集、VaR 回测、波动率管理回归

第 2 项是波动率预测真正的用法：不猜涨跌，而是让承担的风险保持恒定。
做法源自 Moreira & Muir, *Volatility-Managed Portfolios*, JF 2017；
本项目的预测回归实测 β 不显著（见下），因此文档中同时列出
Cederburg et al. (2020, JF) 的反向证据，不单方面引用支持自己的那一篇。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

# **模块级导入**，不要放进函数里。
# 曾经写成函数内 `from engine.rv import realized_variance`，PyInstaller 没能把
# 这条链上的 numpy 收进 exe，打包后 /api/vol 直接 500：No module named 'numpy'。
# 模块级导入让静态分析一定看得见（同时 build_desktop.py 里也显式声明了 hidden-import）。
from engine.rv import realized_variance

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "dataset" / "model.json"

TARGET_VOL = 0.12
MIN_MULT, MAX_MULT = 0.5, 1.5
TRADING_DAYS = 244.0


class VolEngine:
    """从 model.json 读 HAR 系数并推理。"""

    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.path = Path(path)
        self.data: dict = {}
        self.error = ""
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.is_file():
                self.error = "模型文件不存在，请运行 python tools/train_model.py"
                return
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    @property
    def _diag(self) -> dict:
        return self.data.get("diagnostics") or {}

    @property
    def _vol(self) -> dict:
        """升级后的波动率诊断块（优先）。"""
        return self._diag.get("vol") or {}

    @property
    def available(self) -> bool:
        return bool(self._vol.get("har") or self._diag.get("model_race", {}).get("har"))

    def _har(self) -> dict:
        """HAR 系数。优先取升级块；旧版 model.json 无该块时回落到 model_race。

        回落是为了不破坏"用户手里已有旧 model.json"的情形 ——
        旧模型的 HM/HS 是在 r² 口径下算的，只能配 r² 输入使用，
        因此下面 forecast() 里会按 estimator 字段决定用哪种代理变量。
        """
        h = self._vol.get("har")
        if h:
            return h
        return (self._diag.get("model_race") or {}).get("har") or {}

    # ── 可信度 ────────────────────────────────────────────────────────────
    def _reliability(self) -> dict:
        """HAR 那一行的样本外成绩 + 模型置信集结论。

        注意 `model_race["best"]` 是**模型名的字符串**，不是那一行数据 ——
        写成 `.get("rank_ic")` 会 AttributeError: 'str' object has no attribute 'get'。
        要在 `rows` 里按 label 找。
        """
        vol = self._vol
        rows = vol.get("rows") or (self._diag.get("model_race") or {}).get("rows") or []
        row = next((z for z in rows if str(z.get("label", "")).startswith("HAR（")),
                   rows[0] if rows else {})
        mcs = vol.get("mcs") or {}
        return {
            "rank_ic": row.get("rank_ic"), "r2": row.get("r2"),
            "qlike": row.get("qlike"),
            "n_effective": vol.get("n_effective")
            or (self._diag.get("model_race") or {}).get("n_effective"),
            "estimator": vol.get("best_estimator"),
            "mcs_in_set": mcs.get("in_set"),
            "mcs_eliminated": mcs.get("eliminated"),
            "note": ("HAR 的样本外 R² 仅约 0.02~0.6（视代理变量），统计显著但经济上有限。"
                     "它可靠地告诉你「现在比平时波动大」，说不准大多少。"
                     f"因此乘数被限制在 [{MIN_MULT}, {MAX_MULT}]，"
                     "且只用于风险调节。"),
        }

    def _evidence(self) -> dict:
        """把可检验的证据一并带出，供界面上「凭什么这么说」一栏展示。"""
        vol = self._vol
        if not vol:
            return {}
        out = {"target": vol.get("target"),
               "best_estimator": vol.get("best_estimator"),
               "estimators": vol.get("estimators"),
               "mcs": vol.get("mcs"), "rows": vol.get("rows")}
        vb = vol.get("var_backtest") or {}
        if vb.get("ok"):
            out["var_backtest"] = {
                "level": vb.get("level"), "n": vb.get("n"), "t_df": vb.get("t_df"),
                "variants": vb.get("variants"),
            }
        mm = vol.get("mm_regression") or {}
        if mm.get("ok"):
            out["mm_regression"] = {k: mm.get(k) for k in
                                    ("alpha", "beta", "t_beta", "p_beta", "r2",
                                     "n", "hac_lags", "verdict", "caveat")}
        vt = vol.get("vol_target") or {}
        if vt.get("ok"):
            out["vol_target"] = {k: vt.get(k) for k in
                                 ("strategy", "buy_hold", "avg_multiplier",
                                  "turnover", "deflated_sharpe", "note")}
        al = vol.get("allocation") or {}
        if al.get("ok"):
            out["allocation"] = al
        cn = vol.get("connectedness") or {}
        if cn.get("ok"):
            out["connectedness"] = {k: cn.get(k) for k in
                                    ("total_spillover", "horizon", "lags",
                                     "net", "top_pairs", "names", "note")}
        return out

    # ── 预测 ──────────────────────────────────────────────────────────────
    def _rv_series(self, market) -> tuple:
        """按模型训练时所用的代理变量，取代表标的的日频已实现方差。

        返回 (dates, rv, estimator, rep_code)。
        为什么不是 classes_series 的等权组合：合成组合没有真实的开盘价与最高价，
        而极差类估计量必须以真实 OHLC 为输入。A 股各宽基指数相关性约 0.7，
        波动率的共同因子占主导，用代表标的刻画**类的波动状态**是合理近似；
        这一点在文档中如实标注，不宣称等价于合成组合的波动。
        """
        har = self._har()
        est = har.get("estimator") or self._vol.get("best_estimator") or "yang_zhang"
        rep = har.get("representative") or market.representative("equity_cn") or "sh000300"
        dates, o, h, l, c = market.ohlc(rep)
        if not c:
            return [], [], est, rep
        rv = realized_variance(o, h, l, c, 22, est)
        return dates, [float(x) if x == x else None for x in rv], est, rep

    def forecast(self, market) -> dict:
        """用当日最新数据预测未来 20 日年化波动率。"""
        if not self.available:
            return {"available": False, "error": self.error}
        har = self._har()

        dates, rv, est, rep = self._rv_series(market)
        rv = [x for x in rv if x is not None]
        n = len(rv)
        if n < 30:
            return {"available": False, "error": "历史数据不足"}
        as_of = dates[-1] if dates else har.get("as_of")
        rv1 = rv[-1]
        rv5 = sum(rv[-5:]) / 5.0
        rv22 = sum(rv[-22:]) / 22.0

        z = [(math.log(max(rv1, 1e-10)) - har["hm"][0]) / (har["hs"][0] or 1.0),
             (math.log(max(rv5, 1e-10)) - har["hm"][1]) / (har["hs"][1] or 1.0),
             (math.log(max(rv22, 1e-10)) - har["hm"][2]) / (har["hs"][2] or 1.0)]
        logv = sum(w * x for w, x in zip(har["w"], z)) + har["b"]
        fvar = math.exp(max(-30.0, min(30.0, logv)))
        fvol = math.sqrt(max(fvar, 0.0))

        hist = []
        for i in range(20, n + 1):
            seg = rv[i - 20:i]
            m = sum(seg) / len(seg)
            hist.append(math.sqrt(max(m, 0.0) * TRADING_DAYS))
        cur = hist[-1] if hist else fvol
        pct = (sum(1 for hh in hist if hh < cur) / len(hist)) if hist else 0.5

        raw = TARGET_VOL / fvol if fvol > 1e-6 else 1.0
        mult = max(MIN_MULT, min(MAX_MULT, raw))
        state = ("低" if pct < 0.33 else "中" if pct < 0.67 else "高")

        return {
            "available": True,
            "as_of": as_of,
            "trained_as_of": har.get("as_of"),
            "horizon_days": 20,
            "forecast_vol": round(fvol, 4),
            "current_vol": round(cur, 4),
            "percentile": round(pct, 3),
            "state": state,
            "hist_p33": round(sorted(hist)[int(len(hist) * 0.33)], 4) if hist else None,
            "hist_p67": round(sorted(hist)[int(len(hist) * 0.67)], 4) if hist else None,
            "target_vol": TARGET_VOL,
            "multiplier_raw": round(raw, 3),
            "multiplier": round(mult, 3),
            "multiplier_capped": abs(raw - mult) > 1e-6,
            "bounds": [MIN_MULT, MAX_MULT],
            "model": "HAR（日/周/月三尺度）",
            "estimator": est,
            "representative": rep,
            "reliability": self._reliability(),
            "evidence": self._evidence(),
            "disclaimer": ("本项目量化预测部分仅参考，请务必谨慎用于投资决策。"),
        }


_singleton: Optional[VolEngine] = None


def get_engine() -> VolEngine:
    global _singleton
    if _singleton is None:
        _singleton = VolEngine()
    return _singleton
