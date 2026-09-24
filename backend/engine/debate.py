#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」审议编排器。

流水线（对应 TradingAgents 的 Analyst → Debate → Risk → PM，但辩论阶段
换成了 consensus-pipeline 的**可测量收敛**机制）：

  P0 家庭画像        确定性计算，不调用 LLM
  P1 首轮独立发言     5 位分析师并行，各自基于同一份事实基础给出判断
  P2 议题抽取         从首轮发言抽取 3-7 条可辩论的配置主张
  P3 多轮辩论         每轮：看到他人发言 → 修正立场 → 独立表态 → 算 CV/W → 终止判定
                      终止轨道：收敛 / 连续平台 / 高位僵持（三轨道，非写死轮数）
  P4 配置委员会初稿   基于收敛报告产出权重草案（JSON）
  P5 方案接地         确定性层展开：工具、金额、压力测试、风险预算
  P6 风控对抗审查     逐情景金额化，检查集中度/流动性/杠杆，给通过/有条件通过/否决
  P7 配置委员会终裁   消化风控意见与 QC 失败清单，产出最终方案（含僵持裁决）
  P8 质量门禁         10 项确定性硬门禁；不过则修订一次，再不过退化为达标兜底方案

全过程通过 emit(event) 实时推流给前端。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from data.market import MarketData
from engine.agents import (
    AGENTS, RISK_AGENT, COMMITTEE_AGENT, ALLOC_CLASSES, ALLOC_KEYS, CLASS_BY_KEY,
    build_analyst_prompt, build_market_context, build_risk_prompt,
    build_committee_prompt, build_committee_repair_prompt, render_transcript, agent_by_id,
)
from engine.household import HouseholdInput, HouseholdProfile
from engine.llm import LLMClient, LLMError, extract_json
from engine.portfolio import (
    build_proposal, baseline_weights, conservative_weights, normalize_weights,
)
from engine.qc import run_qc
from engine.stance import StanceTracker

logger = logging.getLogger(__name__)

Emit = Callable[[dict], None]

DEFAULT_MAX_ROUNDS = 5
MIN_ROUNDS = 2          # 至少跑 2 轮，否则 CV 无历史可比


# ─────────────────────────────────────────────────────────────────────────────
# 提示词片段渲染
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_wan(v: float) -> str:
    return f"{float(v or 0) / 10000:,.1f} 万"


def format_convergence_block(summary: dict) -> str:
    L = []
    track_labels = {
        "converged": "已收敛（CV 与排序一致性双达标）",
        "composite_deadlock": "高位僵持（多数主张无收敛动作且分歧仍高）",
        "soft_deadlock": "连续平台（分歧冻结，多轮无变化）",
        "unresolved": "达到轮数上限仍未收敛",
        "blocked": "信号不可信（表态解析失败率过高）",
        "continue": "继续辩论",
    }
    L.append(f"辩论轮数：{summary.get('rounds')} 轮")
    L.append(f"终止轨道：{track_labels.get(summary.get('termination_track'), '—')}")
    L.append(f"终止原因：{summary.get('termination_reason') or '（未触发终止）'}")
    fq = summary.get("final_quality") or {}
    L.append(f"最终分歧度 CV = {summary.get('final_cv')}"
             f"（阈值 {summary['thresholds']['eps1_converged']} 为收敛线，越小越一致）"
             f"｜排序一致性 W = {summary.get('final_w')}"
             f"（阈值 {summary['thresholds']['w_threshold']}，越大越一致）"
             f"｜质量评级：{fq.get('label', '—')}")
    L.append("")
    L.append("【逐条主张的最终状态】")
    state_label = {"converged": "✅ 已收敛", "partial": "🟡 部分收敛",
                   "deadlocked": "🔴 高位僵持", "unresolved": "⬜ 未决"}
    for row in summary.get("disagreement_map", []):
        cv = row.get("final_cv")
        L.append(f"  {row['id']} [{state_label.get(row['state'], row['state'])}] "
                 f"CV={cv if cv is None else round(cv, 4)} 均分={row.get('mean')}")
        L.append(f"      主张：{row['text']}")
        per = row.get("stance_by_agent") or {}
        if per:
            L.append("      各分析师打分：" +
                     "、".join(f"{k}={v}" for k, v in per.items() if v is not None))
    return "\n".join(L)


def format_proposal_block(proposal: dict) -> str:
    L = ["大类权重草案："]
    for c in proposal.get("classes", []):
        L.append(f"  - {c['label']}：{c['weight']*100:.1f}%"
                 f"（约 {_fmt_wan(c['amount'])}）"
                 + (f"｜理由：{c['rationale']}" if c.get("rationale") else ""))
    L.append(f"  风险资产合计 {proposal.get('risk_assets_weight', 0)*100:.1f}%，"
             f"防御资产合计 {proposal.get('defensive_assets_weight', 0)*100:.1f}%")
    hist = proposal.get("historical") or {}
    if hist.get("ok"):
        L.append(f"  历史回放（真实数据 {hist.get('window', {}).get('start')} ~ "
                 f"{hist.get('window', {}).get('end')}）：年化 {hist['annual_return']*100:.2f}%，"
                 f"波动 {hist['annual_vol']*100:.2f}%，最大回撤 {hist['max_drawdown']*100:.2f}%")
    fwd = proposal.get("forward") or {}
    if fwd:
        L.append(f"  前瞻测算（假设见系统）：预期年化 {fwd['expected_return']*100:.2f}%，"
                 f"预期波动 {fwd['expected_vol']*100:.2f}%")
    return "\n".join(L)


def format_stress_block(stress: list[dict], investable: float) -> str:
    if not stress:
        return "（无压力测试结果）"
    L = ["情景（真实历史窗口回放）｜组合区间收益｜最大回撤｜折算金额"]
    for s in stress:
        if not s.get("covered"):
            L.append(f"  - {s['name']}：数据窗口未覆盖，跳过")
            continue
        loss = float(s["return"]) * investable
        L.append(f"  - {s['name']}（{s['start']} ~ {s['end']}，{s['days']} 个交易日）："
                 f"{s['return']*100:+.2f}%｜回撤 {s['max_drawdown']*100:.2f}%｜"
                 f"折算 {loss/10000:+,.1f} 万元")
    return "\n".join(L)


def format_risk_budget_block(rb: list[dict]) -> str:
    if not rb:
        return "（无风险预算数据）"
    L = ["标的｜权重｜风险贡献占比（权重小但贡献大 = 隐藏敞口）"]
    for r in rb[:12]:
        share = r.get("risk_share")
        L.append(f"  - {r['name']}：权重 {r['weight']*100:.1f}%｜"
                 f"风险贡献 {share*100:.1f}%" if share is not None
                 else f"  - {r['name']}：权重 {r['weight']*100:.1f}%")
    return "\n".join(L)


def format_classes_block() -> str:
    L = ["必须恰好使用以下 key："]
    for c in ALLOC_CLASSES:
        L.append(f"  - {c['key']}：{c['label']}｜{c['desc']}｜风险 {c['risk']}｜流动性 {c['liquidity']}")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# 编排器
# ─────────────────────────────────────────────────────────────────────────────

class AdvisoryOrchestrator:
    def __init__(
        self,
        household: HouseholdInput,
        market: MarketData,
        llm: LLMClient,
        emit: Emit,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        run_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
    ):
        self.inp = household
        self.market = market
        self.llm = llm
        self.emit = emit
        self.max_rounds = max(MIN_ROUNDS, int(max_rounds))
        # run_id 必须由调用方（API 层）传入并与对外暴露的 id 一致，
        # 否则结果会落在 <api_id> 之外的目录里，历史记录永远列不出来。
        self.run_id = run_id or f"fam_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.run_dir = Path(run_dir or Path(__file__).resolve().parents[1] / "runs") / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.profile = HouseholdProfile(household, rates=market.rate_table)
        self.profile_dict = self.profile.as_dict()
        self.market_context = build_market_context(market, self.profile_dict)
        self.tracker = StanceTracker(run_id=self.run_id, topic=household.name,
                                     log_dir=self.run_dir)
        self._lock = threading.Lock()
        self.t0 = time.time()
        self.events: list[dict] = []

    # ── 事件 ──────────────────────────────────────────────────────────────
    def _emit(self, ev: dict) -> None:
        ev = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), **ev}
        with self._lock:
            self.events.append(ev)
        try:
            self.emit(ev)
        except Exception:  # noqa: BLE001
            logger.exception("emit 回调异常")

    def _phase(self, key: str, label: str, detail: str = "") -> None:
        self._emit({"type": "phase", "phase": key, "label": label, "detail": detail})

    # ── P1 首轮发言 ───────────────────────────────────────────────────────
    def _speak(self, spec, history_block: str, peers_block: str,
               stance_block: str = "") -> dict:
        self._emit({"type": "agent_start", "agent": spec.id, "name": spec.name,
                    "emoji": spec.emoji})
        prompt = build_analyst_prompt(
            spec, self.profile.to_prompt_text(), self.market_context,
            history_block=history_block, peers_block=peers_block,
            stance_block=stance_block,
        )
        try:
            text = self.llm.chat(prompt, temperature=spec.temperature,
                                 max_tokens=spec.max_tokens, label=spec.id)
        except LLMError as exc:
            text = f"（本轮发言失败：{exc}）"
        out = {"agent": spec.id, "name": spec.name, "emoji": spec.emoji,
               "role": spec.role, "text": text.strip()}
        self._emit({"type": "agent_done", **out})
        return out

    def _run_parallel(self, fn, items: list) -> dict:
        results: dict = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(items)))) as pool:
            futs = {pool.submit(fn, it): it for it in items}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    results[it.id] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("并行任务失败 %s", getattr(it, "id", it))
                    results[it.id] = {"agent": it.id, "name": it.name,
                                      "text": f"（执行失败：{exc}）"}
        return results

    # ── P3 独立表态采集 ───────────────────────────────────────────────────
    def _collect_stance(self, spec, speech: str) -> None:
        if not self.tracker.arg_ids:
            return
        prompt = self.tracker.get_stance_ask_prompt(spec.name, speech)
        try:
            # 预算放宽：现在要求逐条给出 12 字以内的理由（why），
            # 400 tokens 在 7 条主张下会截断，导致整块表态丢失。
            raw = self.llm.chat(prompt, temperature=0.2, max_tokens=1000,
                                label=f"stance:{spec.id}")
        except LLMError as exc:
            raw = ""
            logger.warning("表态采集失败 %s: %s", spec.id, exc)
        parsed = self.tracker.record_agent_stance(spec.id, raw)
        if parsed:
            self._emit({"type": "stance_recorded", "agent": spec.id, "name": spec.name,
                        "stance": parsed.get("stance", {}), "ok": parsed.get("ok", False),
                        "why": parsed.get("why", {})})

    # ── 主流程 ────────────────────────────────────────────────────────────
    def run(self) -> dict:
        try:
            return self._run_inner()
        except Exception as exc:  # noqa: BLE001
            logger.exception("审议流程异常")
            self._emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            raise

    def _run_inner(self) -> dict:
        # ── P0 画像 ───────────────────────────────────────────────────────
        self._phase("profile", "家庭财务画像", "确定性计算，不调用模型")
        self._emit({"type": "profile", "data": self.profile_dict})
        self._emit({"type": "market", "catalog": self.market.catalog(),
                    "meta": self.market.meta, "rates": self.market.rate_table})

        # ── P1 首轮发言 ───────────────────────────────────────────────────
        self._phase("round1", "第 1 轮 · 独立发言",
                    "5 位分析师基于同一份家庭事实基础各自给出专业判断")
        r1 = self._run_parallel(
            lambda s: self._speak(s, "", ""), AGENTS)
        r1_speeches = {k: v for k, v in r1.items() if v}

        transcript = "\n\n".join(
            f"── {v.get('name', k)} ──\n{v.get('text', '')}" for k, v in r1_speeches.items())

        # ── P2 议题抽取 ───────────────────────────────────────────────────
        self._phase("arguments", "议题抽取", "把首轮发言归纳为可辩论、可量化的配置主张")
        args = self.tracker.extract_arguments(
            transcript, self.llm.make_callable(max_tokens=1200, temperature=0.2,
                                               label="extract_args"))
        if not args:
            self._emit({"type": "warning",
                        "message": "议题抽取失败，辩论量化阶段将降级为记录模式（不判收敛）"})
        self._emit({"type": "arguments", "arguments": args})

        # ── P3 多轮辩论 ───────────────────────────────────────────────────
        all_rounds: list[dict] = [{"round": 1, "speeches": r1_speeches}]
        termination = {"track": "unresolved", "reason": None}
        round_result: Optional[dict] = None

        for rnd in range(2, self.max_rounds + 1):
            self._phase(f"round{rnd}", f"第 {rnd} 轮 · 交叉辩论",
                        "看到他人发言后修正立场，并对每条主张独立表态")
            history_block = self._build_history_block(all_rounds)
            self._emit({"type": "round_start", "round": rnd})

            def _round_speech(spec, _rnd=rnd, _hist=history_block):
                peers = render_transcript(
                    all_rounds[-1]["speeches"], exclude=spec.id)
                return self._speak(spec, _hist, f"【上一轮其他分析师的发言】\n{peers}",
                                   stance_block=self.tracker.get_stance_block())

            speeches = self._run_parallel(_round_speech, AGENTS)
            all_rounds.append({"round": rnd, "speeches": speeches})

            # 独立表态（单独一问，避免长发言截断表态块）
            if self.tracker.arg_ids:
                self.tracker.begin_round(len(AGENTS))
                with ThreadPoolExecutor(max_workers=5) as pool:
                    futs = [pool.submit(self._collect_stance, s,
                                        speeches.get(s.id, {}).get("text", ""))
                            for s in AGENTS]
                    for f in as_completed(futs):
                        f.result()
                round_result = self.tracker.finish_round(rnd)
                self._emit({"type": "convergence", "round": rnd, "data": round_result})
                self._report_stance_quality(round_result)
                if round_result["should_stop"]:
                    termination = {"track": round_result["track"],
                                   "reason": round_result["termination_reason"]}
                    break

        summary = self.tracker.summary()
        if not summary.get("termination_reason"):
            summary["termination_reason"] = (
                f"达到轮数上限 {self.max_rounds} 轮仍未收敛，交配置委员会显式裁决")
            summary["termination_track"] = "unresolved"
        self._emit({"type": "convergence_summary", "data": summary})

        # ── P4 委员会初稿 ─────────────────────────────────────────────────
        self._phase("draft", "配置委员会 · 草案",
                    "基于收敛报告产出大类权重草案")
        full_transcript = self._build_history_block(all_rounds, max_chars=1400)
        conv_block = format_convergence_block(summary)
        must_rule = self._must_rule_ids(summary)
        draft, draft_raw = self._call_committee_safe(
            conv_block, full_transcript, "（尚未进行风控审查）",
            "（草案阶段尚无压力测试）", "", must_rule)
        draft_weights = self._extract_weights(draft)
        if not draft_weights:
            self._emit({"type": "warning",
                        "message": "草案未获得有效权重，方案接地阶段将使用中性基准配置"
                                   "（仅用于生成风控审查对象，最终方案由终裁决定）"})
            draft_weights = baseline_weights()
        self._emit({"type": "draft", "committee": draft, "weights": draft_weights,
                    "raw": draft_raw})

        # ── P5 方案接地 ───────────────────────────────────────────────────
        self._phase("grounding", "方案接地", "展开工具、金额、历史回放与风险预算")
        proposal = build_proposal(
            draft_weights, self.market, self.profile_dict,
            ranges=draft.get("ranges") if draft else None,
            class_rationale=draft.get("class_rationale") if draft else None,
        )
        self._emit({"type": "proposal_draft", "data": proposal})

        # ── P6 风控审查 ───────────────────────────────────────────────────
        self._phase("risk", "风控委员会 · 对抗性审查",
                    "把损失换算成金额，检查集中度、流动性与杠杆")
        risk_text, verdict = self._run_risk_review(proposal, conv_block)
        risk_sig = self._weights_sig(proposal["weights"])
        self._emit({"type": "risk_verdict", "verdict": verdict})

        # ── P7 委员会终裁 ─────────────────────────────────────────────────
        self._phase("final", "配置委员会 · 终裁",
                    "消化风控意见与门禁清单，产出最终方案并逐条裁决未收敛主张")
        final, final_raw = self._call_committee_safe(
            conv_block, full_transcript, risk_text,
            format_stress_block(proposal["stress"], proposal["investable_assets"]),
            "", must_rule)
        final_weights = self._extract_weights(final)
        if not final_weights:
            self._emit({"type": "warning",
                        "message": "终裁未获得有效权重，将在门禁阶段走达标兜底方案。"})
            final_weights = baseline_weights()

        proposal = build_proposal(
            final_weights, self.market, self.profile_dict,
            ranges=(final or {}).get("ranges"),
            class_rationale=(final or {}).get("class_rationale"),
        )
        self._emit({"type": "proposal_final", "data": proposal, "committee": final,
                    "raw": final_raw})

        # ── P8 质量门禁 ───────────────────────────────────────────────────
        self._phase("qc", "质量门禁", "10 项确定性硬门禁校验")
        review = {"sig": risk_sig, "verdict": verdict, "text": risk_text}
        report = self._qc(proposal, final, review, summary)

        revision_attempts = 0
        while not report.passed and revision_attempts < 1:
            revision_attempts += 1
            self._phase("revise", f"门禁未通过 · 第 {revision_attempts} 次修订",
                        "把失败清单交回配置委员会重新裁决")
            blocking = report.blocking_text()
            revised, revised_raw = self._call_committee_safe(
                conv_block, full_transcript, review["text"],
                format_stress_block(proposal["stress"], proposal["investable_assets"]),
                blocking, must_rule)
            rw = self._extract_weights(revised)
            if rw:
                final, final_raw = revised, revised_raw
                proposal = build_proposal(
                    rw, self.market, self.profile_dict,
                    ranges=revised.get("ranges"),
                    class_rationale=revised.get("class_rationale"))
                self._emit({"type": "proposal_final", "data": proposal,
                            "committee": revised, "raw": revised_raw, "revised": True})
                # 修订后方案已变：若原判定是否决，必须对新方案重新审查，
                # 否则旧否决会永远挂在方案上（"改了也过不了"的死锁）。
                if "否决" in review["verdict"] and \
                        self._weights_sig(proposal["weights"]) != review["sig"]:
                    self._phase("risk2", "风控委员会 · 复审",
                                "方案已按否决意见修改，风控需要对新方案重新表态")
                    risk_text, verdict = self._run_risk_review(
                        proposal, conv_block, tag=":rereview")
                    review = {"sig": self._weights_sig(proposal["weights"]),
                              "verdict": verdict, "text": risk_text}
                    self._emit({"type": "risk_verdict", "verdict": verdict,
                                "rereview": True})
            report = self._qc(proposal, final, review, summary, attempt=revision_attempts)

        fallback_used = False
        if not report.passed:
            # 兜底：用确定性达标方案，绝不交付违反家庭约束的配置
            self._emit({"type": "warning",
                        "message": "模型方案连续未通过硬门禁，已切换为达标兜底方案"
                                   "（按家庭应急金与权益上限确定性生成）"})
            fb = conservative_weights(self.market, self.profile_dict)
            proposal = build_proposal(fb, self.market, self.profile_dict,
                                      ranges=None, class_rationale={
                                          k: "由系统按家庭约束确定性生成（兜底方案）"
                                          for k in ALLOC_KEYS})
            fallback_used = True
            report = self._qc(proposal, final, review, summary, fallback=True)

        # ── 收尾 ──────────────────────────────────────────────────────────
        usage = self.llm.usage.as_dict()
        result = {
            "run_id": self.run_id,
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "elapsed_sec": round(time.time() - self.t0, 1),
            "household": self.profile_dict,
            "convergence": summary,
            # 逐轮的原始表态：前端「团队分工」页用它算每个分析师的立场稳定性
            # （谁在辩论中反复摇摆 —— 摇摆的立场对收敛指标贡献的是噪声而非信号）。
            "round_results": [
                {
                    "round": r.get("round"),
                    "cv_overall": r.get("cv_overall"),
                    "kendall_w": r.get("kendall_w"),
                    "mean_stance": r.get("mean_stance"),
                    "track": r.get("track"),
                    "stance_by_agent": r.get("stance_by_agent") or {},
                    "why": r.get("why") or {},
                }
                for r in self.tracker.round_results
            ],
            "speeches": {r["round"]: {k: {"name": v.get("name"), "text": v.get("text"),
                                          "role": v.get("role"), "emoji": v.get("emoji")}
                                      for k, v in r["speeches"].items()}
                         for r in all_rounds},
            "committee_draft": draft,
            "committee_final": final,
            "risk_review": {"verdict": review.get("verdict"), "text": review.get("text"),
                            "applies_to_current_plan":
                                self._weights_sig(proposal["weights"]) == review.get("sig")},
            "risk_veto_outstanding": "否决" in (review.get("verdict") or ""),
            "proposal": proposal,
            "qc": report.as_dict(),
            "fallback_used": fallback_used,
            "usage": usage,
            "data_meta": self.market.meta,
        }
        (self.run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        self._emit({"type": "done", "data": result})

        # 若 QC 仍不过（理论上不会），也必须如实暴露
        if not report.passed:
            self._emit({"type": "warning",
                        "message": "存在未通过的硬门禁：" + report.blocking_text()})
        return result

    # ── 表态数据质量上报 ──────────────────────────────────────────────────
    def _report_stance_quality(self, rr: dict) -> None:
        """上报响应风格坍缩等量表效度问题。

        明确不改变收敛判定——系统不替分析师决定他们的立场；
        但必须让使用者知道这一轮的打分可信度如何。
        """
        collapse = rr.get("style_collapse") or []
        dupes = rr.get("duplicate_vectors") or []
        if not collapse and not dupes:
            return
        parts = []
        if collapse:
            detail = "、".join(f"{c['agent']}(全{c['constant_score']}分)" for c in collapse)
            parts.append(f"{len(collapse)} 位分析师对本轮全部 {len(self.tracker.arg_ids)} 条"
                         f"主张给出完全相同的分数：{detail}")
        if dupes:
            detail = "；".join("=".join(g) for g in dupes)
            parts.append(f"打分向量完全相同的分析师组：{detail}")
        msg = ("量表效度提示：" + "；".join(parts) +
               "。真实判断很难在内容迥异的主张上产生同分，这通常是响应风格坍缩"
               "（把某个分数当作默认值），而非真实的共识或分歧。"
               "本轮收敛指标仅供参考，请人工复核各分析师的原文发言。")
        self._emit({"type": "warning", "message": msg, "kind": "stance_quality",
                    "round": rr.get("round")})
        logger.warning("量表效度提示(第%s轮)：collapse=%s dupes=%s",
                       rr.get("round"), collapse, dupes)

    # ── 风控审查 ──────────────────────────────────────────────────────────
    def _run_risk_review(self, proposal: dict, conv_block: str,
                         tag: str = "") -> tuple[str, str]:
        """跑一次风控审查，返回 (意见全文, 结论)。tag 非空表示复审。"""
        suffix = "（复审）" if tag else ""
        prompt = build_risk_prompt(
            self.profile.to_prompt_text(), self.market_context, conv_block,
            format_proposal_block(proposal),
            format_stress_block(proposal["stress"], proposal["investable_assets"]),
            format_risk_budget_block(proposal["risk_budget"]),
        )
        self._emit({"type": "agent_start", "agent": RISK_AGENT.id,
                    "name": RISK_AGENT.name + suffix, "emoji": RISK_AGENT.emoji})
        try:
            text = self.llm.chat(prompt, temperature=RISK_AGENT.temperature,
                                 max_tokens=RISK_AGENT.max_tokens, label="risk" + tag)
        except LLMError as exc:
            text = f"（风控审查失败：{exc}）"
        self._emit({"type": "agent_done", "agent": RISK_AGENT.id,
                    "name": RISK_AGENT.name + suffix, "emoji": RISK_AGENT.emoji,
                    "role": RISK_AGENT.role, "text": text})
        return text, self._extract_verdict(text)

    @staticmethod
    def _weights_sig(weights: dict) -> tuple:
        """权重指纹，用于判断方案是否已相对风控审查时发生变化。"""
        return tuple(round(float(weights.get(k, 0) or 0), 4) for k in ALLOC_KEYS)

    def _qc(self, proposal: dict, committee: dict, review: dict, summary: dict,
            attempt: int = 0, fallback: bool = False):
        """跑门禁并推流。

        plan_reviewed_by_risk 决定 QC-8 是硬门禁还是软提醒：
        否决票只对风控实际审查过的那一版方案有效。
        """
        plan_reviewed = self._weights_sig(proposal["weights"]) == review.get("sig")
        report = run_qc(proposal["weights"], self.profile_dict, proposal, committee,
                        review.get("verdict", ""), summary,
                        plan_reviewed_by_risk=plan_reviewed)
        ev: dict = {"type": "qc", "data": report.as_dict(),
                    "plan_reviewed_by_risk": plan_reviewed}
        if attempt:
            ev["attempt"] = attempt
        if fallback:
            ev["fallback"] = True
        self._emit(ev)
        return report

    # ── 辅助 ──────────────────────────────────────────────────────────────
    def _build_history_block(self, all_rounds: list[dict], max_chars: int = 1600) -> str:
        """构建「历轮辩论摘要」区块。"""
        if not all_rounds:
            return ""
        L = []
        for r in all_rounds:
            L.append(f"═══ 第 {r['round']} 轮 ═══")
            for aid, sp in r["speeches"].items():
                text = (sp.get("text") or "").strip()
                if len(text) > max_chars:
                    text = text[:max_chars] + "…（略）"
                L.append(f"── {sp.get('name', aid)} ──\n{text}")
        return "\n\n".join(L)

    def _call_committee(self, convergence_block: str, transcript_block: str,
                        risk_block: str, stress_block: str,
                        qc_blocking: str) -> str:
        prompt = build_committee_prompt(
            self.profile.to_prompt_text(), self.market_context, convergence_block,
            transcript_block, risk_block, stress_block, format_classes_block())
        if qc_blocking:
            prompt += ("\n\n【上一次产出的方案未通过系统硬门禁，必须修正以下问题后重新输出】\n"
                       + qc_blocking +
                       "\n请确保新的 target_weights 满足上述全部约束。")
        self._emit({"type": "agent_start", "agent": COMMITTEE_AGENT.id,
                    "name": COMMITTEE_AGENT.name, "emoji": COMMITTEE_AGENT.emoji})
        try:
            # json_mode=True：强制模型输出合法 JSON。
            # 早期版本让模型先写长篇分析再给 JSON，结果长文吃光 token 预算、
            # JSON 被截断，草案权重静默退化为等权 —— 由内置哨兵在 _call_committee_safe
            # 中兜住，但根因是输出预算与格式，故此处直接用 JSON 模式。
            text = self.llm.chat(prompt, temperature=COMMITTEE_AGENT.temperature,
                                 max_tokens=COMMITTEE_AGENT.max_tokens,
                                 json_mode=True, label="committee")
        except LLMError as exc:
            text = f"（配置委员会调用失败：{exc}）"
        self._emit({"type": "agent_done", "agent": COMMITTEE_AGENT.id,
                    "name": COMMITTEE_AGENT.name, "emoji": COMMITTEE_AGENT.emoji,
                    "role": COMMITTEE_AGENT.role, "text": text})
        return text

    def _call_committee_safe(self, convergence_block: str, transcript_block: str,
                             risk_block: str, stress_block: str,
                             qc_blocking: str,
                             must_rule: list[str]) -> tuple[dict, str]:
        """带哨兵与定向修复的委员会调用。

        返回 (committee_dict, raw_text)。committee_dict 为空 dict 表示彻底失败。
        哨兵条件：必须能解析出至少一个大类权重，且若存在未收敛主张，
        deadlock_rulings 不得为空。任一不满足即发起一次**定向修复调用**——
        用更短的 prompt 只要 JSON，绕开长文截断。
        """
        raw = self._call_committee(convergence_block, transcript_block, risk_block,
                                   stress_block, qc_blocking)
        committee = self._parse_committee_json(raw)
        weights = self._extract_weights(committee)
        has_rulings = bool(committee.get("deadlock_rulings"))

        if weights and (not must_rule or has_rulings):
            return committee, raw

        # ── 哨兵触发：定向修复 ──────────────────────────────────────────
        reason = ("未解析出大类权重" if not weights
                  else f"缺少 {len(must_rule)} 条未收敛主张的裁决")
        self._emit({"type": "warning",
                    "message": f"配置委员会输出{reason}（原始输出 {len(raw)} 字），"
                               f"正在发起一次定向修复调用…"})
        logger.warning("委员会输出不合格(%s)，raw_len=%d", reason, len(raw))

        repair_prompt = build_committee_repair_prompt(
            self.profile.to_prompt_text(), convergence_block, risk_block,
            format_classes_block(), must_rule)
        self._emit({"type": "agent_start", "agent": COMMITTEE_AGENT.id,
                    "name": COMMITTEE_AGENT.name + "（修复）",
                    "emoji": COMMITTEE_AGENT.emoji})
        try:
            raw2 = self.llm.chat(repair_prompt, temperature=0.2, max_tokens=6000,
                                 json_mode=True, label="committee:repair")
        except LLMError as exc:
            raw2 = ""
            logger.warning("委员会修复调用失败：%s", exc)
        self._emit({"type": "agent_done", "agent": COMMITTEE_AGENT.id,
                    "name": COMMITTEE_AGENT.name + "（修复）",
                    "emoji": COMMITTEE_AGENT.emoji, "role": COMMITTEE_AGENT.role,
                    "text": raw2})

        c2 = self._parse_committee_json(raw2)
        if self._extract_weights(c2):
            self._emit({"type": "warning", "message": "定向修复成功，已获得可解析的配置方案 JSON。"})
            return c2, raw2
        self._emit({"type": "warning",
                    "message": "定向修复仍未得到可解析的 JSON，将使用已有信息继续（必要时走兜底方案）。"})
        return committee, raw

    @staticmethod
    def _parse_committee_json(raw: str) -> dict:
        data = extract_json(raw or "")
        if isinstance(data, dict):
            # 容忍模型把方案包在 {"plan": {...}} / {"result": {...}} 里
            if "target_weights" not in data:
                for k in ("plan", "result", "output", "final", "allocation"):
                    inner = data.get(k)
                    if isinstance(inner, dict) and "target_weights" in inner:
                        return inner
            return data
        return {}

    @staticmethod
    def _must_rule_ids(summary: dict) -> list[str]:
        """必须被委员会逐条裁决的主张 ID（未收敛的）。"""
        return [r["id"] for r in (summary or {}).get("disagreement_map", [])
                if r.get("state") in ("deadlocked", "partial")]

    @staticmethod
    def _extract_weights(committee: dict) -> dict:
        if not committee:
            return {}
        tw = committee.get("target_weights")
        if not isinstance(tw, dict):
            return {}
        out = {}
        for k in ALLOC_KEYS:
            if k in tw:
                try:
                    out[k] = float(tw[k])
                except (TypeError, ValueError):
                    out[k] = 0.0
        # 至少要有一个正权重才算有效，否则视为解析失败
        return out if any(v > 1e-9 for v in out.values()) else {}

    @staticmethod
    def _extract_verdict(risk_text: str) -> str:
        """从风控意见里抽结论。

        取**最后一次**出现的结论词，避免"我不会否决该方案"这类表述被误判为否决；
        同时优先识别带"结论"标记的行。
        """
        t = (risk_text or "").strip()
        if not t:
            return "未表态"

        def last_index(needle: str) -> int:
            return t.rfind(needle)

        # 优先：明确以「最终结论」「结论」引导的判定
        for marker in ("最终结论", "结论"):
            idx = t.rfind(marker)
            if idx != -1:
                tail = t[idx:idx + 120]
                for word in ("否决", "有条件通过", "通过"):
                    if word in tail:
                        return word

        # 退化：全局最后一次出现
        positions = {w: last_index(w) for w in ("否决", "有条件通过", "通过")}
        best = max(positions, key=lambda w: positions[w])
        return best if positions[best] != -1 else "未表态"


# ─────────────────────────────────────────────────────────────────────────────
# 便捷入口
# ─────────────────────────────────────────────────────────────────────────────

def run_advisory(
    household_dict: dict,
    market: MarketData,
    llm: LLMClient,
    emit: Emit,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    run_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> dict:
    inp = HouseholdInput.from_dict(household_dict)
    orch = AdvisoryOrchestrator(inp, market, llm, emit, max_rounds=max_rounds,
                                run_dir=run_dir, run_id=run_id)
    return orch.run()
