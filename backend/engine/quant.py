#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化预测的运行时推理 —— 纯标准库，不依赖 numpy。

权重由 `tools/train_model.py` 离线训练后写入 `data/dataset/model.json`。
桌面应用不打 numpy 进包（否则 exe 从 42 MB 涨到约 75 MB），
而这里只需做点积与一层 tanh，纯 Python 完全够快（毫秒级）。

推理与训练必须用同一份特征逻辑 —— 统一走 `engine/features.py`。

**诚实性原则**：模型有没有用，取决于样本外 AUC，而不是它给出的概率有多高。
一个 AUC≈0.5 的过拟合模型照样能输出 70% 的置信度。因此本模块把
AUC、基准率的对比、以及"可信度评级"一并带出来，前端必须展示它们。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from engine.features import FEATURE_KEYS, FEATURE_LABELS, latest_feature_row

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "dataset" / "model.json"


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _grade(auc: float) -> tuple[str, str]:
    """把样本外 AUC 翻译成人能看懂的档位（没有区间信息时的退化路径）。"""
    if auc >= 0.62:
        return "较强", "样本外有明显预测力，仍应作为参考而非依据"
    if auc >= 0.58:
        return "中等", "样本外有弱预测力，只能作为辅助信号"
    if auc >= 0.54:
        return "弱", "预测力很弱，接近噪声，不建议据此行动"
    return "无预测力", "与随机猜测无异，仅作记录，不应影响决策"


def _verdict_from_ci(row: dict) -> tuple[str, str]:
    """判定必须基于**分块自助**的置信区间，而不是点估计。

    标签是未来 N 日收益却按日采样，相邻样本的前瞻窗口重叠 N-1 天 ——
    它们并不独立。逐点自助会把区间算窄 3~4 倍（本机实测 3.2~3.9×），
    于是 AUC 0.509 看起来"像" 0.5，而真实区间是 [0.407, 0.617]。

    注意这个修正**双向**：区间横跨 0.5 时，既不能说有预测力，
    也不能说没有 —— 只能说这份数据不足以判断。
    """
    u = (row or {}).get("uncertainty") or {}
    ci = u.get("block_ci") or []
    if len(ci) < 2:
        return _grade(float((row or {}).get("oos_auc") or 0.5))
    lo, hi = float(ci[0]), float(ci[1])
    if lo > 0.5:
        return "可能有预测力", f"分块 95% 区间 [{lo:.3f}, {hi:.3f}] 整体在 0.5 之上"
    if hi < 0.5:
        return "反向预测力", f"分块 95% 区间 [{lo:.3f}, {hi:.3f}] 整体在 0.5 之下"
    return "无法判断", (f"分块 95% 区间 [{lo:.3f}, {hi:.3f}] 横跨 0.5 —— "
                        f"样本量不足以区分它与抛硬币")


class QuantModel:
    """从 model.json 加载并推理。文件缺失时 available=False，不影响其他功能。"""

    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.path = Path(path)
        self.data: dict = {}
        self.error: str = ""
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
    def available(self) -> bool:
        return bool(self.data.get("logistic"))

    # ── 推理 ──────────────────────────────────────────────────────────────
    def _standardize(self, raw: list[float]) -> list[float]:
        mu = self.data["standardize"]["mean"]
        sd = self.data["standardize"]["std"]
        return [(raw[i] - mu[i]) / (sd[i] if abs(sd[i]) > 1e-12 else 1.0)
                for i in range(len(raw))]

    def _logistic(self, xs: list[float]) -> float:
        m = self.data["logistic"]
        z = sum(w * x for w, x in zip(m["w"], xs)) + m["b"]
        return _sigmoid(z)

    def _mlp(self, xs: list[float]) -> float:
        m = self.data["mlp"]
        W1, b1, W2, b2 = m["W1"], m["b1"], m["W2"], m["b2"]
        h = len(b1)
        d = len(xs)
        # 形状校验：训练时 W1 的形状是 (特征数 d, 隐层数 h)（numpy 里 H = X @ W1），
        # 也就是 W1[k][j] = 「第 k 个特征 → 第 j 个隐单元」的权重。
        # 写成 W1[j][k] 会 IndexError（h=8 < d=14）。这里先校验，报错才看得懂。
        if len(W1) != d or (W1 and len(W1[0]) != h):
            raise ValueError(
                f"模型权重形状与特征不匹配：W1 为 "
                f"{len(W1)}×{len(W1[0]) if W1 else 0}，期望 {d}×{h}。"
                f"请重新运行 python tools/train_model.py")
        hidden = [math.tanh(sum(W1[k][j] * xs[k] for k in range(d)) + b1[j])
                  for j in range(h)]
        return _sigmoid(sum(W2[j] * hidden[j] for j in range(h)) + b2)

    # ── 对外 ──────────────────────────────────────────────────────────────
    def predict(self, market) -> dict:
        """用当前最新数据做一次预测，并附带可信度评级。"""
        if not self.available:
            return {"available": False, "error": self.error}

        row = latest_feature_row(market)
        if row is None:
            return {"available": False, "error": "历史数据不足，无法构造特征"}

        xs = self._standardize(row["features"])
        p_lr = self._logistic(xs)
        p_mlp = self._mlp(xs)
        ens = (p_lr + p_mlp) / 2.0

        best = self.data.get("best") or {}
        auc = float(best.get("oos_auc") or 0.5)
        grade, grade_note = _verdict_from_ci(best)
        unc = best.get("uncertainty") or {}

        feats = []
        imp_lr = self.data["logistic"].get("importance") or []
        imp_mlp = self.data["mlp"].get("importance") or []
        for i, k in enumerate(FEATURE_KEYS):
            feats.append({
                "key": k, "label": FEATURE_LABELS.get(k, k),
                "value": round(row["features"][i], 6),
                "importance_logistic": round(imp_lr[i], 6) if i < len(imp_lr) else None,
                "importance_mlp": round(imp_mlp[i], 6) if i < len(imp_mlp) else None,
            })

        return {
            "available": True,
            "as_of": row["date"],
            "horizon_days": self.data.get("horizon_days", 20),
            "prob": {"logistic": round(p_lr, 4), "mlp": round(p_mlp, 4),
                     "ensemble": round(ens, 4)},
            "confidence": {
                "grade": grade,
                "note": grade_note,
                "oos_auc": auc,
                "oos_accuracy": best.get("oos_accuracy"),
                "baseline_accuracy": best.get("baseline_accuracy"),
                "edge": best.get("edge"),
                "n_oos": best.get("n_oos"),
                "model": best.get("model"),
                "horizon": best.get("horizon"),
                # 重叠标签修正后的区间 —— 判定与展示都以它为准
                "ci_block": unc.get("block_ci"),
                "ci_naive": unc.get("iid_ci"),
                "ci_widen": (round((unc.get("block_width") or 0)
                                   / (unc.get("iid_width") or 1), 1)
                             if unc.get("iid_width") else None),
                "n_effective": unc.get("n_effective"),
                # 校准提示：概率离 0.5 有多远 ≠ 有多可信
                "warning": (
                    f"样本外 AUC {auc:.3f}（{grade}）。"
                    + (f"分块 95% 区间 [{unc['block_ci'][0]:.3f}, "
                       f"{unc['block_ci'][1]:.3f}]。"
                       if unc.get("block_ci") else "")
                    + f"预测值 {ens:.0%} 的偏离幅度**不能**当作把握度 —— "
                    f"该模型样本外准确率 {best.get('oos_accuracy') or 0:.1%}，"
                    f"而「永远猜涨」的基准率是 "
                    f"{best.get('baseline_accuracy') or 0:.1%}。"),
            },
            "leaderboard": self.data.get("leaderboard") or [],
            "features": feats,
            # 诊断图数据：ROC / 分块带 / 校准曲线 / 分状态 AUC。
            # 这些图的作用是让"它现在不能干什么"变成看得见的形状。
            "diagnostics": self.data.get("diagnostics") or {},
            # 换目标后的模型选型结论（波动率预测的实测赛跑）
            "model_race": (self.data.get("diagnostics") or {}).get("model_race") or {},
            "trained_at": self.data.get("trained_at"),
            "n_samples": self.data.get("n_samples"),
            "date_range": self.data.get("date_range"),
            "history": self.data.get("history") or [],
            "disclaimer": self.data.get("disclaimer", ""),
        }


_singleton: Optional[QuantModel] = None


def get_model() -> QuantModel:
    global _singleton
    if _singleton is None:
        _singleton = QuantModel()
    return _singleton
