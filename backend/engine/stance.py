#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」分歧量化与共识收敛引擎。

机制来源
--------
本模块是 consensus-pipeline（https://github.com/fangqian616/consensus-pipeline）
v2 分歧量化模块 `stance_quant_v2.py` 的移植版本，保留其全部判定逻辑与阈值：

  • 收敛判定：CV < ε1（0.07）
  • 僵局判定：|ΔCV| < ε2（0.01）视为走平，连续 N_PLATEAU 轮判「连续平台」
  • 复合僵局：(flat+up)/总论点 ≥ 0.75 且 CV ≥ ε1 → 判「高位僵持」
  • 联合收敛：CV 达标必须同时 Kendall's W > 0.5，否则判「疑似假收敛」继续辩论
  • 解析失败率门：最新轮失败率 ≥ 20% 时跳过一切判定，防止 CV 假低信号

领域适配
--------
原版处理的是学术辩论（部门 vs 部门，论点是学术主张）；
本版处理的是家庭资产配置审议（分析师 vs 分析师，论点是配置主张）。
差异仅在三处：

  1. 论点必须是**可被家庭财务事实支撑或反驳的配置主张**
     （如「该家庭权益上限应为 40%」），而非宏观预测或事实陈述。
  2. 增加**非中立守卫**：全员打 3 分（中立）会人为制造 CV=0 的假收敛。
     原版靠 QC 层拦，本版在判定层直接拦截并给出独立原因码。
  3. 增加 `convergence_quality` 供前端展示收敛质量，不参与判定。

依赖：仅 Python 标准库。
"""
from __future__ import annotations

import ast
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 默认参数（与 consensus-pipeline v2 保持一致）
# ─────────────────────────────────────────────────────────────────────────────
EPS1 = 0.07                  # 收敛阈值：CV < ε1 判收敛
EPS2 = 0.01                  # 僵局阈值：|ΔCV| < ε2 视为走平
N_PLATEAU = 2                # 连续 N 轮满足即判僵局
FLAT_UP_RATIO_TH = 0.75      # 复合僵局：(flat+up)/总论点 阈值
MAX_PARSE_FAIL_RATE = 0.20   # 解析失败率门
W_THRESHOLD = 0.5            # Kendall's W 联合收敛阈值
W_STRONG = 0.7               # W 强一致（仅报告分档）

NEUTRAL_SCORE = 3            # Likert 1-5 的中立点
NEUTRAL_BAND = 0.35          # 平均表态落在 3±0.35 视为「集体中立」
MIN_SUBSTANTIVE_RATIO = 0.5  # 至少一半论点非中立，才算实质收敛


# ─────────────────────────────────────────────────────────────────────────────
# §1 论点清单抽取
# ─────────────────────────────────────────────────────────────────────────────

ARGUMENT_EXTRACT_PROMPT = """\
【配置主张抽取】

下面是家庭资产配置审议第 1 轮各分析师的独立意见。请从中抽取 3-7 条**核心配置主张**，
它们将作为后续轮次所有分析师必须逐一表态的议题清单。

合格主张的标准（务必严格）：
1. 必须是**可被这个家庭的财务事实支撑或反驳的配置判断**，而不是宏观预测或数据描述。
   - 合格：「该家庭的权益类资产上限应设为可投资资产的 40%」
   - 合格：「应当优先偿还房贷而非增配权益资产」
   - 合格：「教育金必须用低波动资产锁定，不得配置权益」
   - 不合格（宏观预测）：「未来一年 A 股会跑赢债券」
   - 不合格（事实描述）：「该家庭负债率 35%」——这是数据，不是主张
2. 每条主张必须是一句可判断支持/反对的陈述句，且**能落到具体配置动作或比例上**。
3. 主张之间不得重叠；语义相同的合并为一条。
4. 必须同时覆盖：主要分歧点（分析师之间意见不一致的）+ 1-2 条明显共识点
   （共识点在量化中充当对照组，用来验证收敛信号是否可信）。
5. 用稳定 ID 编号：P1, P2, P3……一旦确定，后续轮次沿用不变。

输出格式（严格输出一个 JSON 代码块，不要任何其他内容）：
```json
{{
  "arguments": [
    {{"id": "P1", "text": "……"}},
    {{"id": "P2", "text": "……"}}
  ]
}}
```

第 1 轮全部发言：
{round1_transcript}
"""

ARGUMENT_EXTRACT_PROMPT_LENIENT = """\
从下面这段家庭资产配置审议发言中，提炼 3-7 条分析师之间存在分歧或需要拍板的配置主张。
每条写成一句可判断支持/反对的陈述句，编号 P1、P2、P3……

严格按此格式输出（不要其他内容）：
```json
{{"arguments": [{{"id": "P1", "text": "……"}}]}}
```

发言全文：
{round1_transcript}
"""

ARGUMENT_FILTER_PROMPT = """\
判断以下每条配置主张是「可被财务事实支撑或反驳的配置判断」还是「宏观预测 / 数据描述」。

只保留前者。若是后两者，尽量改写为可判断的配置主张；无法改写则丢弃。

判断标准：一条主张必须能对应到一个具体的配置动作（买/不买、比例上限、优先顺序、
期限匹配），否则不合格。

主张列表：
{arg_list}

输出 JSON（沿用原 ID）：
{{"keep": [{{"id": "P1", "text": "改写后的主张"}}]}}
"""


def extract_json_block(raw_text: str, required_key: str = '"stance"') -> Optional[dict]:
    """通用 JSON 块提取。

    策略1：取最后一个含 required_key 的围栏代码块；
    策略2：退化为裸文本括号配对（容忍单/双引号 key）。
    解析时先 json.loads，失败再用 ast.literal_eval 容错（容忍尾逗号与单引号）。
    """
    keys = [required_key, required_key.replace('"', "'")]
    candidate = None

    for blk in reversed(FENCED_RE.findall(raw_text)):
        if any(k in blk for k in keys):
            candidate = blk
            break

    if candidate is None:
        idx = max(raw_text.rfind(k) for k in keys)
        if idx != -1:
            start = raw_text.rfind("{", 0, idx)
            if start != -1:
                candidate = _balance_braces(raw_text, start)

    if candidate is None:
        return None

    for parser in (json.loads, _loose_parse):
        try:
            return parser(candidate)
        except Exception:  # noqa: BLE001
            continue
    return None


FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _balance_braces(text: str, start: int) -> Optional[str]:
    """从 start 处的 '{' 开始做括号配对，返回完整 JSON 子串；失败返回 None。"""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _loose_parse(s: str):
    """宽松解析：去尾逗号后用 ast.literal_eval（容忍单引号）。"""
    s2 = re.sub(r",\s*([}\]])", r"\1", s)
    return ast.literal_eval(s2)


def _try_parse_arguments(raw: str) -> list[dict]:
    """从文本中尽力提取论点列表（三级降级策略）。"""
    # 策略1：直接 json 解析
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("arguments"), list):
            return _clean_args(data["arguments"])
    except Exception:  # noqa: BLE001
        pass

    # 策略2：正则找 arguments 数组
    try:
        for m in re.finditer(r'\{[^{}]{20,}?"arguments"\s*:\s*\[.*?\].*?\}', raw, re.DOTALL):
            try:
                data = json.loads(m.group())
                if isinstance(data, dict) and isinstance(data.get("arguments"), list):
                    cleaned = _clean_args(data["arguments"])
                    if cleaned:
                        return cleaned
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass

    # 策略3：P1/P2/P3 行首正则
    try:
        matches = re.findall(r"(?:P1|P2|P3)\s*[:\.)]\s*(.+?)(?=\n(?:P1|P2|P3)\s*[:\.)]|$)",
                             raw, re.DOTALL)
        if matches:
            cleaned = []
            for i, m in enumerate(matches[:10], 1):
                text = m.strip()
                if text and len(text) > 5:
                    cleaned.append({"id": f"P{i}", "text": text})
            if cleaned:
                return cleaned
    except Exception:  # noqa: BLE001
        pass

    return []


def _clean_args(items) -> list[dict]:
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("id") and item.get("text"):
            out.append({"id": str(item["id"]), "text": str(item["text"])})
    return out


def filter_debatable_arguments(arguments: list[dict], llm_call_fn) -> list[dict]:
    """后验过滤：剔除宏观预测/数据描述，只留可判断的配置主张。

    这是论点抽取的保险丝。失败时原样返回（宁可不滤，不误删）。
    """
    if not arguments or not llm_call_fn:
        return arguments

    arg_list = "\n".join(f'{a["id"]}: {a["text"]}' for a in arguments)
    prompt = ARGUMENT_FILTER_PROMPT.format(arg_list=arg_list)
    try:
        raw = llm_call_fn(prompt)
        data = extract_json_block(raw, '"keep"')
        if isinstance(data, dict) and isinstance(data.get("keep"), list):
            kept = _clean_args(data["keep"])
            if kept:
                return kept
    except Exception:  # noqa: BLE001
        pass
    return arguments


def extract_arguments(
    round1_transcript: str,
    llm_call_fn,
    run_id: str = "",
    topic: str = "",
    log_path: Optional[Path] = None,
) -> list[dict]:
    """R1 后抽取核心配置主张清单。失败时返回空列表。"""
    result: dict = {"ok": False, "arguments": [], "error": None}

    try:
        raw = llm_call_fn(ARGUMENT_EXTRACT_PROMPT.format(round1_transcript=round1_transcript))
        data = extract_json_block(raw, '"arguments"')
        if isinstance(data, dict) and isinstance(data.get("arguments"), list):
            cleaned = _clean_args(data["arguments"])
            if cleaned:
                result["ok"] = True
                result["arguments"] = cleaned
            else:
                result["error"] = "no_valid_arguments_in_response"
        else:
            fallback = _try_parse_arguments(raw)
            if fallback:
                result["ok"] = True
                result["arguments"] = fallback
            else:
                result["error"] = "no_valid_arguments_block"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"exception: {exc}"

    # 宽松 prompt 重试
    if not result["ok"]:
        try:
            raw2 = llm_call_fn(
                ARGUMENT_EXTRACT_PROMPT_LENIENT.format(round1_transcript=round1_transcript))
            fallback2 = _try_parse_arguments(raw2)
            if fallback2:
                result["ok"] = True
                result["arguments"] = fallback2
                result["error"] = None
        except Exception:  # noqa: BLE001
            pass

    # 后验过滤
    if result["ok"] and result["arguments"]:
        try:
            result["arguments"] = filter_debatable_arguments(result["arguments"], llm_call_fn)
        except Exception:  # noqa: BLE001
            pass

    if log_path:
        write_stance_log(Path(log_path), {
            "event": "argument_extraction",
            "run_id": run_id,
            "topic": topic,
            "ok": result["ok"],
            "n_arguments": len(result["arguments"]),
            "arguments": result["arguments"],
            "error": result["error"],
        })

    return result["arguments"]


# ─────────────────────────────────────────────────────────────────────────────
# §2 表态块 prompt 渲染
# ─────────────────────────────────────────────────────────────────────────────

STANCE_BLOCK_TEMPLATE = """\
【结构化表态要求】

本轮发言结束后，你必须对下列核心配置主张逐一给出你的独立态度。

主张清单：
{argument_list_rendered}

评分标尺（Likert 1-5 级，整数）—— **请按"你会不会在方案里这么做"来打分，而不是按"这话听起来对不对"**：
1 = 我会在方案中明确违背这条主张（有明确的家庭财务事实或数据支持相反做法）
2 = 我倾向不同意，但证据不够充分，不会主动违背
3 = 我既不支持也不反对；这条主张不影响我的方案
4 = 我倾向同意，愿意把它写进方案，但保留了调整空间
5 = 我主张按这条执行，并愿意把它作为方案中不可协商的硬约束写死

打分自查（避免踩坑）：
- 不要因为"表述风格不合我意"就打低分，只针对**配置动作本身**打分。
- 不要为了避免冲突而一律给 3 分。集体中立会掩盖真实分歧，使收敛判定失效。
- 如果一条主张其实是你自己上一轮提出的，你应当给 4 或 5，除非你已改变判断
  （改变判断是可以的，但请在发言中说明为什么改）。

输出格式（严格遵守）：
```json
{{
  "stance": {{
{stance_example_lines}
  }}
}}
```

规则：
1. 必须对清单中每一条主张打分，不得遗漏，也不得新增主张 ID。
2. 分数必须是 1-5 的整数。
3. JSON 代码块放在本轮发言的最后；代码块之后不要再写任何内容。
4. 评分基于你听完本轮全部发言后的最新判断。允许与上轮不同——**立场变化本身是重要信号**，
   不要为了显得"一致"而维持旧分。
"""


def render_stance_block(arguments: list[dict]) -> str:
    """渲染表态块 prompt（R2 起拼在每个分析师每轮 prompt 末尾）。"""
    if not arguments:
        return ""

    argument_list_rendered = "\n".join(f"{a['id']}: {a['text']}" for a in arguments)
    example_lines = [f'    "{a["id"]}": 3' + ("," if i < len(arguments) - 1 else "")
                     for i, a in enumerate(arguments)]
    return STANCE_BLOCK_TEMPLATE.format(
        argument_list_rendered=argument_list_rendered,
        stance_example_lines="\n".join(example_lines),
    )


STANCE_ASK_PROMPT = """\
你是本场家庭资产配置审议的一位分析师：{agent_name}。

下面是本轮全部主张的清单，以及你自己本轮的发言。
请**只**输出一个 JSON 对象，不要输出任何其他内容。

评分标尺（整数 1-5）—— 按「你会不会在方案里这么做」打分，而不是「这话听起来对不对」：
  5 = 我主张按这条执行，愿意把它作为方案中不可协商的硬约束写死
  4 = 我倾向同意，愿意写进方案，但保留调整空间
  3 = 我既不支持也不反对；这条主张不影响我的方案
  2 = 我倾向不同意，但不会主动违背
  1 = 我会在方案中明确违背这条

【打分前必须逐条做的自查】（这是防止"一律打同一个分"的关键）
对每一条主张，先问自己：**这条主张和我本轮发言里的立场一致吗？**
  · 如果这条主张与我自己提出或支持的做法**实质相同、或比我的主张更保守** → 应为 4 或 5
  · 如果它与我发言中的立场**方向相反** → 应为 1 或 2，并在 why 里点明冲突在哪
  · 只有当我确实没有相关判断、且它不影响我的方案时 → 才是 3
**必须给每条主张写一句 12 字以内的理由（why 字段）**，理由要具体到配置动作，
不允许写"同意""不同意""合理""需考虑"这类空话。

常见错误（务必避免）：
- 因为主张不是我提出的就给低分 —— 判断标准是**内容**，不是出处。
- 一律给 2 或一律给 3 —— 这会让收敛判定失效，等于放弃发表意见。
- 因为表述风格、宏观预测不同就打低分 —— 只针对**配置动作本身**打分。

主张清单：
{argument_list}

你本轮的发言：
{speech}

严格按此格式输出（键必须覆盖全部主张 ID，why 每条都要写）。
下例**只是格式示例，其中的分数是无意义的占位值，请勿模仿**：

{example_json}
"""


def build_stance_ask_prompt(agent_name: str, arguments: list[dict], speech: str,
                            arg_ids: list[str]) -> str:
    """构造「独立表态采集」prompt。

    独立一问的收益：长发言 + 表态块同行输出极易被 max_tokens 截断，
    导致整块表态丢失、解析失败率飙升、进而触发解析失败率门使收敛信号失效。
    单独一问输出短、稳。

    示例值的坑：早期版本用清一色 `"P1": 3` 作格式示例，结果把模型锚定到中立，
    人为制造出"集体打 3 分"的假收敛；换成一串多样化数值并显式声明"数值无意义"后，
    响应风格坍缩明显缓解。
    """
    pattern = [4, 2, 5, 3, 4, 2, 5, 3, 4, 2]
    stance_lines = ",\n".join(
        f'    "{aid}": {pattern[i % len(pattern)]}' for i, aid in enumerate(arg_ids))
    why_lines = ",\n".join(
        f'    "{aid}": "（12字以内、具体到配置动作的理由）"' for aid in arg_ids)
    example_json = ('{\n  "stance": {\n' + stance_lines + '\n  },\n'
                    '  "why": {\n' + why_lines + '\n  }\n}')
    return STANCE_ASK_PROMPT.format(
        agent_name=agent_name,
        argument_list="\n".join(f"{a['id']}: {a['text']}" for a in arguments),
        speech=speech,
        example_json=example_json,
    )


# ─────────────────────────────────────────────────────────────────────────────
# §3 stance 解析（容错，永不抛异常）
# ─────────────────────────────────────────────────────────────────────────────

def parse_stance(raw_text: str, expected_ids: list[str]) -> dict:
    """解析单个分析师一轮的表态块。

    返回 {"ok", "stance", "missing", "fixes", "error"}。
    部分成功也算成功（ok=True）—— 缺失的论点会在 CV 计算时被自然排除。
    """
    res: dict = {"ok": False, "stance": {}, "missing": [], "fixes": [], "error": None,
                 "why": {}}

    data = extract_json_block(raw_text, '"stance"')
    if not isinstance(data, dict) or not isinstance(data.get("stance"), dict):
        res["error"] = "no_valid_stance_block"
        res["missing"] = list(expected_ids)
        return res

    raw_stance = data["stance"]

    for aid in expected_ids:
        v = raw_stance.get(aid)
        if v is None:
            res["missing"].append(aid)
            continue
        try:
            v = int(round(float(v)))  # 容忍 4.0 / "4"
        except (TypeError, ValueError):
            res["missing"].append(aid)
            continue
        if not 1 <= v <= 5:
            res["fixes"].append(f"{aid}:{v}->clamped")
            v = max(1, min(5, v))
        res["stance"][aid] = v

    # 逐条理由（可选，用于人工核查与前端展示；缺失不影响评分）
    why_raw = data.get("why")
    if isinstance(why_raw, dict):
        res["why"] = {str(k): str(v)[:80] for k, v in why_raw.items()
                      if str(k) in set(expected_ids)}

    extra = set(raw_stance) - set(expected_ids)
    if extra:
        res["fixes"].append(f"extra_ids_ignored:{sorted(extra)}")

    res["ok"] = len(res["stance"]) > 0
    return res


# ─────────────────────────────────────────────────────────────────────────────
# §4 CV 计算
# ─────────────────────────────────────────────────────────────────────────────

def argument_cv(scores: list) -> Optional[float]:
    """单主张：本轮全部分析师评分的变异系数 CV = σ/μ。有效样本 <2 或 μ=0 返回 None。"""
    vals = [s for s in scores if s is not None]
    if len(vals) < 2:
        return None
    mu = mean(vals)
    if mu == 0:
        return None
    return pstdev(vals) / mu  # 总体标准差：n = 分析师数（少样本），用 pstdev


def round_cv(stance_by_agent: dict[str, dict[str, int]], arg_ids: list[str]) -> dict:
    """计算一轮的 CV 聚合。

    返回 {"overall": 轮级 CV（各主张 CV 的简单均值）, "per_argument": {...}}。
    """
    per_arg = {}
    for aid in arg_ids:
        scores = [s.get(aid) for s in stance_by_agent.values()]
        per_arg[aid] = argument_cv(scores)

    valid = [v for v in per_arg.values() if v is not None]
    return {
        "overall": mean(valid) if valid else None,
        "per_argument": per_arg,
    }


# ─────────────────────────────────────────────────────────────────────────────
# §5 一致性度量 Kendall's W（联合收敛的交叉验证）
# ─────────────────────────────────────────────────────────────────────────────

def kendall_w(stance_by_agent: dict, arg_ids: list) -> Optional[float]:
    """Kendall's W 协调系数（0-1，排序一致性，带并列秩校正）。

    W 抗「打分高位压缩」，与 CV 互补：CV 看**离散度**，W 看**排序一致**。
    容忍部分表态缺失，两阶段降级：
      阶段1 剔除「缺失任一主张」的分析师（整块 JSON 解析失败场景）；
      阶段2 若仍不足 2 名，退回剔除「任一分析师缺失」的主张列（个别主张漏评场景）。
    两阶段后若 K<2 或 N<2 才返回 None。
    """
    agents = list(stance_by_agent.keys())
    if len(agents) < 2 or len(arg_ids) < 2:
        return None

    complete = [d for d in agents
                if all(stance_by_agent[d].get(aid) is not None for aid in arg_ids)]
    if len(complete) >= 2:
        agents, cols = complete, list(arg_ids)
    else:
        cols = [aid for aid in arg_ids
                if all(stance_by_agent[d].get(aid) is not None for d in agents)]
        if len(cols) < 2:
            return None

    matrix = [[stance_by_agent[d].get(aid) for aid in cols] for d in agents]
    K, N = len(matrix), len(cols)
    if K < 2 or N < 2:
        return None

    def _avg_rank(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    ranked = [_avg_rank(row) for row in matrix]
    R = [sum(ranked[j][i] for j in range(K)) for i in range(N)]
    Rbar = K * (N + 1) / 2
    S = sum((Ri - Rbar) ** 2 for Ri in R)
    T = 0
    for row in matrix:
        for t in Counter(row).values():
            T += t ** 3 - t
    denom = K ** 2 * (N ** 3 - N) - K * T
    if denom <= 0:
        all_same = len({v for row in matrix for v in row}) == 1
        return 1.0 if all_same else None
    return max(0.0, min(1.0, 12 * S / denom))


def mean_stance(stance_by_agent: dict, arg_ids: list) -> Optional[float]:
    """轮级平均表态分（非中立守卫用）。"""
    vals = [s.get(aid) for s in stance_by_agent.values()
            for aid in arg_ids if s.get(aid) is not None]
    return sum(vals) / len(vals) if vals else None


def substantive_ratio(stance_by_agent: dict, arg_ids: list) -> Optional[float]:
    """非中立主张占比。

    集体打 3 分会让 CV→0，制造"完美收敛"假象。该指标衡量有多少条主张
    的表态真正离开了中立带，用于拦截这种假收敛。
    """
    flags = []
    for aid in arg_ids:
        vals = [s.get(aid) for s in stance_by_agent.values() if s.get(aid) is not None]
        if len(vals) < 2:
            continue
        flags.append(abs(mean(vals) - NEUTRAL_SCORE) > NEUTRAL_BAND)
    if not flags:
        return None
    return sum(1 for f in flags if f) / len(flags)


def detect_style_collapse(stance_by_agent: dict, arg_ids: list) -> list[dict]:
    """检测**响应风格坍缩**：某个分析师对本轮全部主张给出完全相同的分数。

    这不是"强烈的共识/分歧"，而是**没有逐条评估**的证据。真实判断几乎不可能
    在 7 条内容迥异的主张上产生完全相同的分数——逐条同分通常意味着模型在套模板
    （例如把"2"当作"这条不是我提的"的默认值）。

    实测案例：行为金融分析师在发言中明确支持"权益上限 33% 是硬约束"和
    "保障缺口补齐前收紧权益"，却对这两条主张都打了 2 分，并与另外两位分析师
    给出逐字节相同的全 2 向量。这是量表效度问题，不是真实分歧。

    检测到坍缩时不会改变判定结果（避免系统替分析师做决定），
    而是作为数据质量指标上报，并在前端提示人工复核。
    """
    out = []
    for aid, st in stance_by_agent.items():
        vals = [st.get(a) for a in arg_ids if st.get(a) is not None]
        if len(vals) >= 3 and len(set(vals)) == 1:
            out.append({"agent": aid, "constant_score": vals[0], "n_arguments": len(vals)})
    return out


def duplicate_stance_vectors(stance_by_agent: dict, arg_ids: list) -> list[list[str]]:
    """找出打分向量**完全相同**的分析师组。

    不同角色的分析师有各自的职责与信息侧重，给出逐字节相同的 7 维打分向量
    在真实判断下几乎不可能发生，通常指向响应风格坍缩。
    """
    groups: dict[tuple, list[str]] = {}
    for aid, st in stance_by_agent.items():
        vec = tuple(st.get(a) for a in arg_ids)
        if all(v is None for v in vec):
            continue
        groups.setdefault(vec, []).append(aid)
    return [v for v in groups.values() if len(v) > 1]


# ─────────────────────────────────────────────────────────────────────────────
# §6 终止判定（双轨 + 两个守门）
# ─────────────────────────────────────────────────────────────────────────────

def _interval_down_flat_up(prev: dict, cur: dict, eps2: float) -> tuple[int, int, int, int]:
    """逐主张相邻轮转移分类计数。

    ΔCV < -eps2 → down（收敛中）；|ΔCV| ≤ eps2 → flat（持平）；ΔCV > +eps2 → up（反升）。
    """
    down = flat = up = total = 0
    for aid, v_cur in cur.items():
        v_prev = prev.get(aid)
        if v_prev is None or v_cur is None:
            continue
        total += 1
        d = v_cur - v_prev
        if d > eps2:
            up += 1
        elif d < -eps2:
            down += 1
        else:
            flat += 1
    return down, flat, up, total


def check_termination(
    cv_history: list[Optional[float]],
    eps1: float = EPS1,
    eps2: float = EPS2,
    n: int = N_PLATEAU,
    per_arg_history: Optional[list[dict]] = None,
    flat_up_ratio_th: float = FLAT_UP_RATIO_TH,
    fail_rate: Optional[float] = None,
    w: Optional[float] = None,
    mean_stance_value: Optional[float] = None,
    sub_ratio: Optional[float] = None,
) -> tuple[bool, Optional[str], str]:
    """双轨终止判定。返回 (should_stop, reason, track)。

    track ∈ {"converged", "composite_deadlock", "soft_deadlock", "unresolved", "blocked", "continue"}

    轨道1  收敛：最新一轮 CV < ε1 且 W > 0.5
    轨道2a 连续平台：最近 n 个 |ΔCV| 全部 < ε2
    轨道2b 高位僵持：当前 CV > ε1 且最近间隔 (flat+up)/总主张 ≥ 0.75
           （高位震荡型僵局会被一次显著下降打断连续性后反升，连续平台轨道对其不敏感）
    守门1  解析失败率门：最新轮失败率 ≥ 20% 时 CV 信号不可信，跳过一切判定
    守门2  非中立守卫：集体中立造成的 CV→0 不算收敛，继续辩论
    """
    hist = [c for c in cv_history if c is not None]
    if not hist:
        return False, None, "continue"

    # ── 守门1：解析失败率门 ────────────────────────────────────────────────
    if fail_rate is not None and fail_rate >= MAX_PARSE_FAIL_RATE:
        return (False,
                f"blocked_by_parse_fail_gate: fail_rate={fail_rate:.2%} >= {MAX_PARSE_FAIL_RATE:.0%}",
                "blocked")

    # ── 轨道1：收敛 ────────────────────────────────────────────────────────
    if hist[-1] < eps1:
        # 守门2：非中立守卫 —— 集体中立不是收敛
        if (mean_stance_value is not None
                and abs(mean_stance_value - NEUTRAL_SCORE) <= NEUTRAL_BAND
                and sub_ratio is not None and sub_ratio < MIN_SUBSTANTIVE_RATIO):
            return (False,
                    f"neutral_collapse: CV={hist[-1]:.4f} < eps1={eps1} 但全体平均表态="
                    f"{mean_stance_value:.2f} 落在中立带 ±{NEUTRAL_BAND}，"
                    f"非中立主张占比仅 {sub_ratio:.0%}（疑似集体弃权式假收敛，继续辩论）",
                    "continue")

        if w is None:
            return (True,
                    f"converged: CV={hist[-1]:.4f} < eps1={eps1} "
                    f"(W 无法计算：表态缺失/网络波动，退化为只看 CV)",
                    "converged")
        if w > W_THRESHOLD:
            return (True,
                    f"converged: CV={hist[-1]:.4f} < eps1={eps1} 且 W={w:.3f} > {W_THRESHOLD}",
                    "converged")
        # CV 达标但排序不一致 → 疑似假收敛
        return (False,
                f"cv_low_but_w_low: CV={hist[-1]:.4f} < eps1={eps1} 但 W={w:.3f} "
                f"<= {W_THRESHOLD}（疑似假收敛，继续）",
                "continue")

    # ── 轨道2b：高位僵持（复合僵局，优先于 2a）──────────────────────────────
    if per_arg_history is not None and len(per_arg_history) >= 2 and hist[-1] > eps1:
        prev, cur = per_arg_history[-2], per_arg_history[-1]
        down, flat, up, total = _interval_down_flat_up(prev, cur, eps2)
        if total and (flat + up) / total >= flat_up_ratio_th:
            ratio = (flat + up) / total
            return (True,
                    f"composite_deadlock: CV={hist[-1]:.4f} > eps1={eps1} 且 "
                    f"(flat+up)/total={flat + up}/{total}={ratio:.2f} >= {flat_up_ratio_th}"
                    f"（多数主张无收敛动作且分歧仍高位，交配置委员会显式裁决）",
                    "composite_deadlock")

    # ── 轨道2a：连续平台 ───────────────────────────────────────────────────
    if len(hist) >= n + 1:
        deltas = [abs(hist[i] - hist[i - 1]) for i in range(len(hist) - n, len(hist))]
        if all(d < eps2 for d in deltas):
            return (True,
                    f"soft_deadlock: |ΔCV| < eps2={eps2} 连续 {n} 轮（分歧冻结，交裁决）",
                    "soft_deadlock")

    return False, None, "continue"


def convergence_quality(cv: Optional[float], w: Optional[float]) -> dict:
    """给前端展示的收敛质量画像（不参与判定）。"""
    if cv is None:
        return {"grade": "unknown", "label": "无信号"}
    if cv < 0.05 and (w is None or w > W_STRONG):
        grade, label = "strong", "强共识"
    elif cv < EPS1 and (w is None or w > W_THRESHOLD):
        grade, label = "good", "已收敛"
    elif cv < 0.12:
        grade, label = "partial", "部分收敛"
    else:
        grade, label = "weak", "分歧显著"
    return {"grade": grade, "label": label, "cv": round(cv, 4),
            "w": (round(w, 4) if w is not None else None)}


# ─────────────────────────────────────────────────────────────────────────────
# §7 JSONL 日志
# ─────────────────────────────────────────────────────────────────────────────

def write_stance_log(log_path: Path, event_data: dict) -> None:
    """追加写一行到 stance 日志（JSONL）。"""
    if "ts" not in event_data:
        event_data["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event_data, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# §8 状态管理器
# ─────────────────────────────────────────────────────────────────────────────

class StanceTracker:
    """串联全部量化逻辑的状态管理器。

    用法::

        tracker = StanceTracker(run_id="fam_001", topic="...", log_dir=...)
        args = tracker.extract_arguments(round1_text, llm_call_fn)
        for rnd in range(2, max_rounds + 1):
            suffix = tracker.get_stance_block()
            # ... 各分析师基于 suffix 产出本轮发言 ...
            tracker.begin_round()
            for aid, speech in speeches.items():
                stance_json = llm_call_fn(build_stance_ask_prompt(...))
                tracker.record_agent_stance(aid, stance_json)
            result = tracker.finish_round(rnd)
            if result["should_stop"]:
                break
    """

    def __init__(self, run_id: str, topic: str, log_dir: str | Path = "."):
        self.run_id = run_id
        self.topic = topic
        self.log_path = Path(log_dir) / "stance_log.jsonl"
        self.arguments: list[dict] = []
        self.arg_ids: list[str] = []
        self.cv_history: list[Optional[float]] = []
        self.w_history: list[Optional[float]] = []
        self.per_arg_history: list[dict] = []
        self.round_fail_rates: list[float] = []
        self.round_results: list[dict] = []
        self.termination_reason: Optional[str] = None
        self.termination_track: str = "unresolved"
        self._current: dict[str, dict[str, int]] = {}
        self._why: dict[str, dict[str, str]] = {}
        self._expected = 0

    # ── 论点 ──────────────────────────────────────────────────────────────
    def extract_arguments(self, round1_transcript: str, llm_call_fn) -> list[dict]:
        self.arguments = extract_arguments(
            round1_transcript=round1_transcript,
            llm_call_fn=llm_call_fn,
            run_id=self.run_id,
            topic=self.topic,
            log_path=self.log_path,
        )
        self.arg_ids = [a["id"] for a in self.arguments]
        return self.arguments

    def get_stance_block(self) -> str:
        return render_stance_block(self.arguments)

    def get_stance_ask_prompt(self, agent_name: str, speech: str) -> str:
        return build_stance_ask_prompt(agent_name, self.arguments, speech, self.arg_ids)

    # ── 每轮 ──────────────────────────────────────────────────────────────
    def begin_round(self, expected_agents: int) -> None:
        self._current = {}
        self._why = {}
        self._expected = max(1, expected_agents)

    def record_agent_stance(self, agent_id: str, raw_output: str,
                            prompt_tokens: int = 0, completion_tokens: int = 0) -> dict:
        """记录单个分析师一轮的表态解析结果，返回解析结果。"""
        if not self.arg_ids:
            return {}
        parsed = parse_stance(raw_output, self.arg_ids)
        self._current[agent_id] = parsed["stance"]
        self._why[agent_id] = parsed.get("why", {})
        write_stance_log(self.log_path, {
            "event": "round_stance",
            "run_id": self.run_id,
            "agent": agent_id,
            "stance": parsed["stance"],
            "why": parsed.get("why", {}),
            "parse_ok": parsed["ok"],
            "missing": parsed["missing"],
            "fixes": parsed["fixes"],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        })
        return parsed

    def finish_round(self, round_num: int) -> dict:
        """结算本轮：算 CV / W / 非中立占比 → 终止判定。"""
        cv = round_cv(self._current, self.arg_ids)
        w = kendall_w(self._current, self.arg_ids)
        ms = mean_stance(self._current, self.arg_ids)
        sr = substantive_ratio(self._current, self.arg_ids)

        n_ok = sum(1 for s in self._current.values() if s)
        fail_rate = 1.0 - (n_ok / self._expected) if self._expected else 0.0

        # 数据质量指标：响应风格坍缩 / 重复打分向量（不影响判定，只上报）
        collapse = detect_style_collapse(self._current, self.arg_ids)
        dupes = duplicate_stance_vectors(self._current, self.arg_ids)

        self.cv_history.append(cv["overall"])
        self.w_history.append(w)
        self.per_arg_history.append(cv["per_argument"])
        self.round_fail_rates.append(fail_rate)

        should_stop, reason, track = check_termination(
            cv_history=self.cv_history,
            per_arg_history=self.per_arg_history,
            fail_rate=fail_rate,
            w=w,
            mean_stance_value=ms,
            sub_ratio=sr,
        )

        result = {
            "round": round_num,
            "cv_overall": cv["overall"],
            "cv_per_argument": cv["per_argument"],
            "kendall_w": w,
            "mean_stance": ms,
            "substantive_ratio": sr,
            "parse_fail_rate": fail_rate,
            "n_parsed": n_ok,
            "n_expected": self._expected,
            "should_stop": should_stop,
            "termination_reason": reason,
            "track": track,
            "quality": convergence_quality(cv["overall"], w),
            "stance_by_agent": {k: dict(v) for k, v in self._current.items()},
            "style_collapse": collapse,
            "duplicate_vectors": dupes,
            "why": {k: dict(v) for k, v in self._why.items()},
        }
        self.round_results.append(result)

        if should_stop or track in ("converged", "composite_deadlock", "soft_deadlock"):
            self.termination_reason = reason
            self.termination_track = track

        write_stance_log(self.log_path, {"event": "round_summary", "run_id": self.run_id,
                                         **{k: v for k, v in result.items()
                                            if k != "stance_by_agent"}})
        return result

    # ── 汇总 ──────────────────────────────────────────────────────────────
    def summary(self) -> dict:
        """整场审议的收敛报告。"""
        last = self.round_results[-1] if self.round_results else {}
        cv_track = [{"round": r["round"], "cv": r["cv_overall"], "w": r["kendall_w"],
                     "fail_rate": r["parse_fail_rate"], "mean_stance": r["mean_stance"]}
                    for r in self.round_results]

        # 分歧地图：每条主张的最终状态
        per_arg_final = self.per_arg_history[-1] if self.per_arg_history else {}
        disagreement_map = []
        for a in self.arguments:
            aid = a["id"]
            per_agent = {k: v.get(aid) for k, v in (last.get("stance_by_agent") or {}).items()}
            cfg = per_arg_final.get(aid)
            if cfg is None:
                state = "unresolved"
            elif cfg < EPS1:
                state = "converged"
            elif cfg < 0.15:
                state = "partial"
            else:
                state = "deadlocked"
            disagreement_map.append({
                "id": aid,
                "text": a["text"],
                "final_cv": cfg,
                "state": state,
                "stance_by_agent": per_agent,
                "mean": (round(mean([v for v in per_agent.values() if v is not None]), 2)
                         if any(v is not None for v in per_agent.values()) else None),
            })

        return {
            "run_id": self.run_id,
            "topic": self.topic,
            "arguments": self.arguments,
            "cv_track": cv_track,
            "rounds": len(self.round_results),
            "termination_reason": self.termination_reason,
            "termination_track": self.termination_track,
            "final_cv": last.get("cv_overall"),
            "final_w": last.get("kendall_w"),
            "final_quality": last.get("quality"),
            "disagreement_map": disagreement_map,
            "thresholds": {
                "eps1_converged": EPS1, "eps2_flat": EPS2, "n_plateau": N_PLATEAU,
                "flat_up_ratio": FLAT_UP_RATIO_TH, "w_threshold": W_THRESHOLD,
                "max_parse_fail_rate": MAX_PARSE_FAIL_RATE,
                "neutral_band": NEUTRAL_BAND,
            },
        }
