#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型置信集（Model Confidence Set, MCS）。

为什么需要它
------------
原来比较波动率模型的方式是：按 QLIKE 排序，再对每个模型做一次「与 HAR 的
分块自助区间」。这有两个问题：

1. **多重比较**。六个模型两两比就产生 15 个区间，任何一个不覆盖 0 都不足为奇，
   但报告里读起来像是「发现了差异」。
2. **缺少一个「哪个集合无法被区分」的结论**。用户真正想知道的是
   「HAR 是不是显著最好的那个」，而不是六个孤立区间。

MCS 直接回答这个问题：在给定置信水平下，构造一个**包含真实最优模型**
的集合，集合外的模型可以被统计地淘汰。它同时处理多重比较，且不预设哪个是基准。

参考
----
Hansen, P.R., Lunde, A. & Nason, J.M. (2011). The Model Confidence Set.
Econometrica 79(2), 453-497.

实现要点
--------
· 损失矩阵 L（T×m，越小越好），逐行做**分块**自助以保留重叠窗口的相关性
· 距离 d̄_ij = L̄_i − L̄_j；方差 V̂(d̄_ij) 由自助分布估计
· 两个统计量：极差 T_R 与半二次 T_SQ
· 若在水平 α 下拒绝原假设，**淘汰标准化超额损失最大的模型**，重复
· 每个模型最终得到一个 MCS p 值；p ≥ α 者留在集合内
"""
from __future__ import annotations

import math

import numpy as np

from engine.statlib import block_indices  # type: ignore


def _pairwise_stats(L: np.ndarray) -> tuple:
    """返回 (d̄_ij 矩阵, 每列均值 L̄_i)。"""
    Lbar = L.mean(axis=0)
    D = Lbar[:, None] - Lbar[None, :]          # D[i, j] = L̄_i − L̄_j
    return D, Lbar


def _statistics(Z: np.ndarray, V: np.ndarray) -> tuple:
    """由中心化距离 Z 与方差 V 计算 T_R 与 T_SQ。

    V 为 0（两个模型损失逐点完全相同）时该对被跳过，
    否则会出现 0/0 —— 这不是异常，而是「两者无法区分」的极端情形。
    """
    m = Z.shape[0]
    mask = (V > 1e-16) & ~np.eye(m, dtype=bool)
    if not mask.any():
        return 0.0, 0.0
    ratio = np.where(mask, np.abs(Z) / np.sqrt(np.where(mask, V, 1.0)), 0.0)
    t_r = float(ratio.max())
    t_sq = float((np.where(mask, Z, 0.0) ** 2 / np.where(mask, V, 1.0)).sum())
    return t_r, t_sq


def model_confidence_set(losses: np.ndarray, labels: list, block: int = 20,
                         alpha: float = 0.10, reps: int = 1000,
                         stat: str = "T_SQ", seed: int = 17) -> dict:
    """构造模型置信集 M*_{α}。

    参数
    ----
    losses : (T, m) 损失矩阵，**越小越好**（本项目用 QLIKE）
    labels : 长度 m 的模型名
    block  : 自助块长，取不小于预测窗口 H
    alpha  : 显著性水平，0.10 对应 90% 置信集
    reps   : 自助次数
    stat   : "T_SQ"（半二次，对多个偏离方向更敏感）或 "T_R"（极差）

    返回
    ----
    dict，含：
      in_set     留在集合内的模型名
      eliminated 被淘汰的模型名（按淘汰顺序）
      p_values   每个模型的 MCS p 值
      path       每一步的统计量、p 值与被淘汰者
    """
    L = np.asarray(losses, float)
    T, m = L.shape
    if m < 2:
        return {"in_set": list(labels), "eliminated": [], "path": [],
                "p_values": {labels[0]: 1.0}, "alpha": alpha, "stat": stat}

    rng = np.random.default_rng(seed)
    idx_sets = block_indices(T, min(block, T), reps, rng)

    alive = list(range(m))
    p_values = {lab: 0.0 for lab in labels}
    eliminated: list = []
    path: list = []
    running_p = 0.0

    while len(alive) > 1:
        Ls = L[:, alive]
        D, Lbar = _pairwise_stats(Ls)
        # 自助分布：每次重采样后重新计算 d̄*，再中心化
        boot = np.empty((reps, len(alive), len(alive)))
        for b, idx in enumerate(idx_sets):
            Lb = Ls[idx, :]
            boot[b] = Lb.mean(axis=0)[:, None] - Lb.mean(axis=0)[None, :]
        Z = boot - D[None, :, :]                      # 中心化
        V = (Z ** 2).mean(axis=0)                     # V̂(d̄_ij)
        T_obs = _statistics(D, V)[0 if stat == "T_R" else 1]
        T_boot = np.array([_statistics(Z[b], V)[0 if stat == "T_R" else 1]
                           for b in range(reps)])
        p = float((T_boot >= T_obs).mean())
        running_p = max(running_p, p)

        # 标准化超额损失：d̄_i· = 对 j≠i 的平均距离
        k = len(alive)
        Dm = D.copy()
        np.fill_diagonal(Dm, 0.0)
        excess = Dm.sum(axis=1) / (k - 1)
        se = np.sqrt(np.where(V > 1e-16, V, np.nan))
        with np.errstate(invalid="ignore"):
            std_excess = np.nanmean(np.where(np.isfinite(se), Dm / se, 0.0), axis=1)

        path.append({"n_models": k, "stat": round(T_obs, 4),
                     "p": round(p, 4),
                     "alive": [labels[i] for i in alive]})

        if p >= alpha:
            for i in alive:
                p_values[labels[i]] = max(running_p, p)
            break

        # 淘汰标准化超额损失最大的那个
        worst_local = int(np.argmax(std_excess))
        worst = alive[worst_local]
        p_values[labels[worst]] = running_p
        eliminated.append(labels[worst])
        path[-1]["eliminated"] = labels[worst]
        alive.pop(worst_local)

    if len(alive) == 1:
        p_values[labels[alive[0]]] = 1.0

    return {
        "in_set": [labels[i] for i in alive],
        "eliminated": eliminated,
        "p_values": {lab: round(v, 4) for lab, v in p_values.items()},
        "path": path,
        "alpha": alpha,
        "stat": stat,
        "n": int(T),
        "n_effective": int(T // max(1, block)),
        "reps": reps,
    }
