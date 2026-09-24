#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检视一次运行：把分析师发言与其表态并排打印，用于判断"分歧"是真实的还是响应噪声。

用法：python tools/inspect_run.py [run_id] [--round 2] [--agent behavioral]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "backend" / "runs"


def latest_run() -> Path | None:
    ds = sorted([d for d in RUNS.glob("*") if (d / "result.json").is_file()],
                key=lambda d: d.stat().st_mtime, reverse=True)
    return ds[0] if ds else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--round", type=int, default=0, help="0 = 全部轮次")
    ap.add_argument("--agent", default="", help="只显示某个分析师")
    ap.add_argument("--width", type=int, default=1500)
    args = ap.parse_args()

    d = (RUNS / args.run_id) if args.run_id else latest_run()
    if not d or not (d / "result.json").is_file():
        print("找不到运行结果")
        return 1
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))

    print("=" * 100)
    print(f"运行 {r['run_id']}  家庭：{r['household']['name']}  "
          f"轮数 {r['convergence']['rounds']}  轨道 {r['convergence']['termination_track']}")
    print("=" * 100)

    # 表态矩阵
    print("\n【表态矩阵】（行=主张，列=分析师）")
    agents = ["macro", "valuation", "lifecycle", "taxfee", "behavioral"]
    short = {"macro": "宏观", "valuation": "定价", "lifecycle": "期限",
             "taxfee": "税费", "behavioral": "行为"}
    stances_by_round: dict[int, dict] = {}
    for res in r["convergence"].get("cv_track", []):
        pass
    # 从 speeches 无法取表态，改从 stance_log.jsonl 读
    log = d / "stance_log.jsonl"
    if log.is_file():
        cur = None
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if ev.get("event") == "round_summary":
                cur = ev.get("round")
            if ev.get("event") == "round_stance" and cur is not None:
                stances_by_round.setdefault(cur, {})[ev["agent"]] = ev["stance"]

    args_list = r["convergence"].get("arguments", [])
    print(f"{'主张':<6}{'均分':>5}  " + "".join(f"{short[a]:>7}" for a in agents))
    for a in args_list:
        vals = [stances_by_round.get(max(stances_by_round) or 0, {}).get(ag, {}).get(a["id"])
                for ag in agents]
        got = [v for v in vals if v]
        print(f"{a['id']:<6}{(sum(got)/len(got) if got else 0):>5.1f}  "
              + "".join(f"{(v if v else '·'):>7}" for v in vals)
              + f"   {a['text'][:70]}")
    print()

    # 并排打印：发言 vs 表态
    for rnd, speeches in sorted(r["speeches"].items(), key=lambda kv: int(kv[0])):
        if args.round and int(rnd) != args.round:
            continue
        print("\n" + "═" * 100)
        print(f"第 {rnd} 轮")
        print("═" * 100)
        st = stances_by_round.get(int(rnd), {})
        for ag, sp in speeches.items():
            if args.agent and ag != args.agent:
                continue
            print(f"\n┌─ {sp.get('name', ag)}  "
                  f"表态：{st.get(ag, {})}")
            print("└" + "─" * 96)
            txt = (sp.get("text") or "").strip()
            print(txt[:args.width] + ("…（截断）" if len(txt) > args.width else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
