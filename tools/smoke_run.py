#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端冒烟测试：启动一次真实审议并实时打印事件流。

用法：
    python tools/smoke_run.py                # 用示例家庭，3 轮
    python tools/smoke_run.py --rounds 5     # 5 轮
    python tools/smoke_run.py --port 8760
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"
    ex = requests.get(f"{base}/api/example", timeout=20).json()

    t0 = time.time()
    r = requests.post(f"{base}/api/analyze",
                      json={"household": ex, "max_rounds": args.rounds}, timeout=30)
    if r.status_code != 200:
        print(f"启动失败 HTTP {r.status_code}: {r.text[:400]}")
        return 1
    run_id = r.json()["run_id"]
    print(f"启动审议 run_id={run_id}  最多 {args.rounds} 轮\n" + "=" * 78)

    last_phase = ""
    counts: dict[str, int] = {}
    with requests.get(f"{base}/api/stream/{run_id}", stream=True,
                      timeout=(20, args.timeout)) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            try:
                ev = json.loads(raw[6:])
            except Exception:  # noqa: BLE001
                continue
            t = ev.get("type", "?")
            counts[t] = counts.get(t, 0) + 1
            el = time.time() - t0

            if t == "phase":
                last_phase = ev.get("label", "")
                print(f"\n[{el:6.1f}s] ▶▶ {ev.get('label')}  —  {ev.get('detail', '')}")
            elif t == "agent_done":
                body = (ev.get("text") or "").replace("\n", " ")
                print(f"[{el:6.1f}s]   ✓ {ev.get('name'):<16} {len(ev.get('text') or '')}字 │ {body[:88]}…")
            elif t == "arguments":
                print(f"[{el:6.1f}s]   ◆ 抽取到 {len(ev.get('arguments') or [])} 条配置主张：")
                for a in (ev.get("arguments") or []):
                    print(f"        {a['id']}: {a['text'][:84]}")
            elif t == "convergence":
                d = ev["data"]
                flag = "★终止" if d.get("should_stop") else ""
                print(f"[{el:6.1f}s]   ▸ 第{d['round']}轮 CV={d['cv_overall'] if d['cv_overall'] is None else round(d['cv_overall'],4)}"
                      f"  W={d['kendall_w'] if d['kendall_w'] is None else round(d['kendall_w'],3)}"
                      f"  失败率={d['parse_fail_rate']:.0%}  {flag}")
                if d.get("termination_reason"):
                    print(f"         ⚑ {d['termination_reason']}")
            elif t == "stance_recorded":
                print(f"[{el:6.1f}s]     · {ev.get('name'):<16} 表态 {ev.get('stance')}")
            elif t == "draft":
                w = ev.get("weights") or {}
                print(f"[{el:6.1f}s]   ✎ 委员会草案：" +
                      "、".join(f"{k}={v:.0%}" for k, v in w.items() if v))
            elif t == "risk_verdict":
                print(f"[{el:6.1f}s]   ⚖ 风控结论：{ev.get('verdict')}")
            elif t == "qc":
                d = ev["data"]
                print(f"[{el:6.1f}s]   ▣ 质量门禁 {d['n_passed']}/{d['n_checks']} 通过"
                      f"  passed={d['passed']}")
                for b in d.get("blockers", []):
                    print(f"         ⛔ {b['id']} {b['name']}: {b['detail'][:96]}")
            elif t == "warning":
                print(f"[{el:6.1f}s]   ⚠ {ev.get('message')}")
            elif t == "error":
                print(f"[{el:6.1f}s]   ✗ 错误：{ev.get('message')}")
            elif t == "done":
                print(f"\n{'='*78}\n审议完成，总用时 {el:.1f}s")
            elif t == "__end__":
                break

    # 取最终结果
    res = requests.get(f"{base}/api/result/{run_id}", timeout=60).json()
    if res.get("status") != "done":
        print(f"\n未完成：{res}")
        return 1

    print("=" * 78)
    print("【配置方案】")
    prop = res["proposal"]
    for c in prop["classes"]:
        if c["weight"] > 0.0005:
            print(f"  {c['label']:<14} {c['weight']*100:>6.1f}%   {c['amount']/10000:>9.1f}万   "
                  f"{(c.get('rationale') or '')[:56]}")
    print(f"  合计 {sum(c['weight'] for c in prop['classes'])*100:.1f}%")
    f = prop.get("forward") or {}
    h = prop.get("historical") or {}
    print(f"\n  前瞻预期年化 {f.get('expected_return',0)*100:.2f}%  "
          f"预期波动 {f.get('expected_vol',0)*100:.2f}%")
    if h.get("ok"):
        print(f"  历史回放年化 {h['annual_return']*100:.2f}%  波动 {h['annual_vol']*100:.2f}%  "
              f"最大回撤 {h['max_drawdown']*100:.2f}%  ({h['window']['start']} ~ {h['window']['end']})")

    print("\n【压力测试】")
    for s in prop.get("stress", []):
        if s.get("covered"):
            print(f"  {s['name']:<22} {s['return']*100:>7.2f}%  "
                  f"折算 {s['return']*prop['investable_assets']/10000:>9.1f}万  "
                  f"回撤 {s['max_drawdown']*100:>6.2f}%")

    print("\n【共识收敛】")
    cv = res["convergence"]
    print(f"  轮数 {cv['rounds']}  轨道 {cv['termination_track']}  "
          f"最终CV {cv['final_cv'] if cv['final_cv'] is None else round(cv['final_cv'],4)}  "
          f"W {cv['final_w'] if cv['final_w'] is None else round(cv['final_w'],3)}")
    print(f"  {cv['termination_reason']}")
    for row in cv.get("disagreement_map", []):
        print(f"    {row['id']} [{row['state']:<11}] CV={row['final_cv']} 均分={row['mean']}")

    print(f"\n【用量】{res['usage']}")
    print(f"【门禁】passed={res['qc']['passed']}  "
          f"{res['qc']['n_passed']}/{res['qc']['n_checks']}  兜底={res['fallback_used']}")
    print(f"\n结果文件：{ROOT / 'backend' / 'runs' / run_id / 'result.json'}")
    print(f"事件统计：{counts}")
    return 0 if res["qc"]["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
