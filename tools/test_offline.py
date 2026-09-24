#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：不调用 LLM，直接验证数据层与组合数学。

覆盖：大类序列构建、组合历史回放、真实情景压力测试、风险预算、
家庭画像确定性测算、质量门禁。

用法：python tools/test_offline.py
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from data.market import MarketData                                  # noqa: E402
from engine.household import HouseholdInput, HouseholdProfile        # noqa: E402
from engine.portfolio import (baseline_weights, build_proposal,     # noqa: E402
                              conservative_weights, normalize_weights)
from engine.qc import run_qc                                        # noqa: E402
from engine.stance import (StanceTracker, check_termination,        # noqa: E402
                           detect_style_collapse, kendall_w, round_cv)

PASS, FAIL = "✅", "❌"
fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global fails
    if not cond:
        fails += 1
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("=" * 78)
    print("「智能多维投资系统」离线自检（不调用 LLM）")
    print("=" * 78)

    m = MarketData()
    print(f"\n【数据集】{len(m.symbols)} 个标的｜{m.meta.get('fetched_at')}")

    # ── 1. 大类序列 ──────────────────────────────────────────────────────
    print("\n【1. 大类序列与窗口覆盖】")
    spans = {}
    for k in ("equity_cn", "equity_global", "bond", "gold", "commodity", "cash"):
        d, r = m.class_series(k)
        if d:
            spans[k] = (d[0], d[-1], len(d))
            print(f"  {k:<14} {len(d):>5} 日  {d[0]} → {d[-1]}")
        else:
            print(f"  {k:<14} （无序列）")
    check("每个大类都有序列", len(spans) == 6, f"{len(spans)}/6")
    check("大类序列覆盖 2018 年",
          all(s[0] <= "2018-12-31" for s in spans.values()),
          f"最早起点 {min(s[0] for s in spans.values())}")

    # ── 2. 组合历史回放 ──────────────────────────────────────────────────
    print("\n【2. 组合历史回放】")
    w = baseline_weights()
    st = m.portfolio_stats(w)
    check("回放成功", st.get("ok") is True, st.get("error", ""))
    if st.get("ok"):
        print(f"  窗口 {st['window']['start']} → {st['window']['end']}（{st['n_obs']} 日）")
        print(f"  年化 {st['annual_return']*100:+.2f}%  波动 {st['annual_vol']*100:.2f}%  "
              f"最大回撤 {st['max_drawdown']*100:.2f}%  夏普 {st['sharpe_rf0']}")
        print(f"  年度收益：{ {k: f'{v*100:+.1f}%' for k, v in sorted(st['yearly_returns'].items())} }")
        check("窗口覆盖疫情崩盘（2020-03 之前起）",
              st["window"]["start"] <= "2020-01-01", st["window"]["start"])
        check("最大回撤为负且合理（-5% ~ -40%）",
              -0.40 < st["max_drawdown"] < -0.005, f"{st['max_drawdown']*100:.2f}%")
        check("净值曲线长度 = 观测数+1",
              len(st["curve"]) == st["n_obs"] + 1)

    # ── 3. 真实情景压力测试 ──────────────────────────────────────────────
    print("\n【3. 历史情景压力测试】")
    stress = m.stress_test(w)
    covered = [s for s in stress if s.get("covered")]
    for s in stress:
        if s.get("covered"):
            print(f"  {s['name']:<24} {s['start']} ~ {s['end']}  "
                  f"{s['return']*100:>7.2f}%  回撤 {s['max_drawdown']*100:>6.2f}%  "
                  f"({s['days']} 日)")
        else:
            print(f"  {s['name']:<24} 未覆盖")
    check("至少 5 个情景被覆盖", len(covered) >= 5, f"{len(covered)}/{len(stress)}")
    check("存在显著为负的情景（说明真的在压力测试）",
          any(s["return"] < -0.03 for s in covered))

    # ── 4. 风险预算 ──────────────────────────────────────────────────────
    print("\n【4. 风险预算】")
    from engine.portfolio import _instruments_weights
    rb = m.marginal_risk(_instruments_weights(w, m))
    top = rb[:3]
    for r in top:
        print(f"  {r['name']:<16} 权重 {r['weight']*100:>5.1f}%  →  风险贡献 "
              f"{(r['risk_share'] or 0)*100:>5.1f}%")
    check("风险贡献合计 ≈ 100%",
          abs(sum(r["risk_share"] or 0 for r in rb) - 1.0) < 0.02,
          f"{sum(r['risk_share'] or 0 for r in rb):.4f}")
    check("存在权重<风险贡献的隐藏敞口",
          any((r["risk_share"] or 0) > r["weight"] * 1.3 for r in rb))

    # ── 5. 家庭画像 ──────────────────────────────────────────────────────
    print("\n【5. 家庭画像（确定性）】")
    inp = HouseholdInput.from_dict({
        "name": "自检家庭", "head_age": 36, "retirement_age": 60, "dependents": 2,
        "assets": {"cash_deposit": 220000, "money_fund": 80000, "bank_wealth": 150000,
                   "bond_fund": 60000, "equity_fund": 180000, "pension_account": 45000,
                   "insurance_cash": 60000, "property_self_use": 2600000, "other": 30000},
        "liabilities": {"mortgage_balance": 1450000, "consumer_debt": 20000},
        "monthly_income": 42000, "income_stability": 4,
        "monthly_expense": 16000, "monthly_mortgage": 7800,
        "mortgage_rate": 0.0355, "risk_score": 58, "loss_tolerance_pct": 15,
        "goals": [{"name": "教育金", "amount": 800000, "years": 14, "priority": "critical"}],
    })
    p = HouseholdProfile(inp, rates=m.rate_table).as_dict()
    print(f"  总资产 {p['total_assets']/1e4:.1f}万  净资产 {p['net_worth']/1e4:.1f}万  "
          f"可投资 {p['investable_assets']/1e4:.1f}万")
    print(f"  负债率 {p['leverage']:.1%}  月结余 {p['monthly_savings']/1e4:.2f}万  "
          f"应急 {p['emergency_months']:.1f}/{p['emergency_target_months']} 月")
    print(f"  承受能力 {p['capacity_score']}  偏好 {p['preference_score']}  "
          f"综合 {p['risk_score']} → {p['risk_grade_label']}")
    print(f"  权益上限 {p['equity_ceiling']:.1%}  生命周期 {p['lifecycle_label']}")
    check("可投资资产 = 总资产 − 自住房",
          abs(p["investable_assets"] - (p["total_assets"] - p["assets"]["property_self_use"])) < 1)
    check("权益上限落在 [5%, 85%]", 0.05 <= p["equity_ceiling"] <= 0.85,
          f"{p['equity_ceiling']:.1%}")
    check("权益上限 ≤ 由回撤推出的上限",
          p["equity_ceiling"] <= inp.loss_tolerance_pct / 100 / 0.45 + 1e-9)
    check("风险综合分 = min(客观, 主观)",
          abs(p["risk_score"] - min(p["capacity_score"], p["preference_score"])) < 0.05)
    # 幂等性
    p2 = HouseholdProfile(HouseholdInput.from_dict(inp.to_dict()),
                          rates=m.rate_table).as_dict()
    check("画像幂等（同输入同输出）",
          p == p2)

    # ── 6. 质量门禁 ──────────────────────────────────────────────────────
    print("\n【6. 质量门禁】")
    good = {"equity_cn": 0.20, "equity_global": 0.10, "bond": 0.32, "gold": 0.08,
            "commodity": 0.0, "cash": 0.12, "deposit": 0.13, "insurance": 0.05}
    prop = build_proposal(good, m, p, ranges=None,
                          class_rationale={k: "自检" for k in good})
    rep = run_qc(prop["weights"], p, prop, {"deadlock_rulings": []}, "通过", {})
    print(f"  合规方案：{rep.passed}  {len(rep.checks)} 项检查，"
          f"{len(rep.blockers)} 项硬失败，{len(rep.warnings)} 项软提醒")
    check("合规方案应通过", rep.passed, rep.blocking_text())

    bad = dict(good); bad["equity_cn"] = 0.60; bad["bond"] = 0.05
    wbad, _ = normalize_weights(bad)
    prop2 = build_proposal(wbad, m, p)
    rep2 = run_qc(prop2["weights"], p, prop2, {}, "通过", {})
    print(f"  超限方案：{rep2.passed}  硬失败项 "
          f"{[c.id for c in rep2.blockers]}")
    check("权益超上限必须被拦下", not rep2.passed)
    check("QC-2 被触发", any(c.id == "QC-2" for c in rep2.blockers))

    rv = run_qc(prop["weights"], p, prop, {}, "风控委员会结论：否决", {})
    check("风控否决必须拦下（QC-8）",
          any(c.id == "QC-8" for c in rv.blockers))

    conv = {"disagreement_map": [{"id": "P1", "state": "deadlocked"},
                                 {"id": "P2", "state": "converged"}]}
    r9 = run_qc(prop["weights"], p, prop, {"deadlock_rulings": []}, "通过", conv)
    check("未收敛主张缺裁决必须拦下（QC-9）",
          any(c.id == "QC-9" for c in r9.blockers))
    r9b = run_qc(prop["weights"], p, prop,
                 {"deadlock_rulings": [{"argument_id": "P1", "reason": "x"}]}, "通过", conv)
    check("补上裁决后 QC-9 通过",
          not any(c.id == "QC-9" for c in r9b.blockers))

    fb = conservative_weights(m, p)
    prop3 = build_proposal(fb, m, p)
    rep3 = run_qc(prop3["weights"], p, prop3, {}, "通过", {})
    check("兜底方案自身必须合规", rep3.passed, rep3.blocking_text())

    # ── 7. 收敛机制 ──────────────────────────────────────────────────────
    print("\n【7. 收敛机制（纯函数）】")
    # 完全一致 → CV 0
    same = {a: {"P1": 4, "P2": 2} for a in ("a1", "a2", "a3")}
    cv = round_cv(same, ["P1", "P2"])
    check("全一致 → CV=0", cv["overall"] == 0.0, str(cv["overall"]))
    check("全一致 → W=1.0", abs((kendall_w(same, ["P1", "P2"]) or 0) - 1.0) < 1e-9)

    ok, reason, track = check_termination([0.05], w=None)
    check("CV<ε1 判收敛", ok and track == "converged", reason or "")

    # CV 达标但 W 不达标 → 疑似假收敛，继续
    ok2, reason2, track2 = check_termination([0.05], w=0.2)
    check("CV 达标但 W 低 → 拒绝收敛", (not ok2) and "cv_low_but_w_low" in (reason2 or ""),
          (reason2 or "")[:60])

    # 集体中立 → 非中立守卫拦截
    neutral = {a: {"P1": 3, "P2": 3} for a in ("a1", "a2", "a3")}
    ok3, reason3, _ = check_termination([0.0], w=1.0, mean_stance_value=3.0, sub_ratio=0.0)
    check("集体中立 → 非中立守卫拦截",
          (not ok3) and "neutral_collapse" in (reason3 or ""), (reason3 or "")[:60])

    # 解析失败率门
    ok4, reason4, track4 = check_termination([0.02], fail_rate=0.4)
    check("解析失败率≥20% → 跳过判定",
          (not ok4) and track4 == "blocked", (reason4 or "")[:60])

    # 连续平台
    ok5, reason5, track5 = check_termination([0.20, 0.205, 0.203])
    check("连续平台 → soft_deadlock",
          ok5 and track5 == "soft_deadlock", (reason5 or "")[:70])

    # 复合僵局：(flat+up)/total >= 0.75 且 CV 仍高
    per_arg = [{"P1": 0.30, "P2": 0.30, "P3": 0.30, "P4": 0.30},
               {"P1": 0.305, "P2": 0.31, "P3": 0.30, "P4": 0.32}]
    ok6, reason6, track6 = check_termination([0.30, 0.31], per_arg_history=per_arg)
    check("复合僵局 → composite_deadlock",
          ok6 and track6 == "composite_deadlock", (reason6 or "")[:70])

    # 响应风格坍缩检测
    col = detect_style_collapse({"a": {"P1": 2, "P2": 2, "P3": 2},
                                 "b": {"P1": 4, "P2": 2, "P3": 5}}, ["P1", "P2", "P3"])
    check("能检出全同分（响应风格坍缩）",
          len(col) == 1 and col[0]["agent"] == "a", str(col))

    # StanceTracker 端到端（不打网络）
    tr = StanceTracker(run_id="t", topic="t", log_dir=ROOT / "backend" / "runs" / "_selftest")
    tr.arguments = [{"id": "P1", "text": "x"}, {"id": "P2", "text": "y"}]
    tr.arg_ids = ["P1", "P2"]
    tr.begin_round(2)
    tr.record_agent_stance("a", '{"stance": {"P1": 4, "P2": 2}, "why": {"P1": "r1"}}')
    tr.record_agent_stance("b", '```json\n{"stance": {"P1": 4, "P2": 2}}\n```')
    r = tr.finish_round(2)
    check("StanceTracker 解析两种格式并结算",
          r["cv_overall"] == 0.0 and r["n_parsed"] == 2, str(r["cv_overall"]))
    check("why 字段被捕获", r["why"].get("a", {}).get("P1") == "r1")

    print("\n" + "=" * 78)
    if fails:
        print(f"{FAIL} 自检失败：{fails} 项未通过")
    else:
        print(f"{PASS} 全部自检通过")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
