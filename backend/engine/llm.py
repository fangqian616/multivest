#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」LLM 客户端。

后端：OpenAI 兼容的 /chat/completions 接口（默认 DeepSeek）。
仅依赖 requests，不引入 openai SDK，便于打包与离线部署。
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from engine import settings as _settings

logger = logging.getLogger(__name__)

# ── TLS 证书来源 ──────────────────────────────────────────────────────────
# 这台机器（以及很多企业/校园网环境）装了 TLS 拦截：curl 与浏览器走**系统证书库**
# 所以一切正常，而 requests 默认走 certifi 的证书包，会报
#   SSLCertVerificationError: self-signed certificate in certificate chain
# 正确修法是把校验切到操作系统证书库（truststore），而不是关掉校验。
#
# 只有在显式设置 MULTIVEST_INSECURE_SSL=1 时才降级为不校验 —— 那会把 API Key
# 暴露给中间人，所以绝不做默认行为，并且启动时就打一条警告。
_SSL_MODE = "certifi"
if os.environ.get("MULTIVEST_INSECURE_SSL", "").strip() in ("1", "true", "yes"):
    _SSL_MODE = "insecure"
    logger.warning("⚠ 已按 MULTIVEST_INSECURE_SSL 关闭 TLS 证书校验 —— "
                   "仅在自签证书的内网环境下使用，公网会泄露 API Key")
else:
    try:
        import truststore  # type: ignore

        truststore.inject_into_ssl()
        _SSL_MODE = "system"
    except ImportError:
        logger.info("未安装 truststore，TLS 沿用 certifi 证书包；"
                    "若报 self-signed certificate，执行 pip install truststore")
    except Exception as _exc:  # noqa: BLE001
        logger.warning("启用系统证书库失败（%s），沿用 certifi", _exc)

VERIFY: bool = _SSL_MODE != "insecure"
SSL_MODE: str = _SSL_MODE

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_REASONER_MODEL = "deepseek-reasoner"


# ─────────────────────────────────────────────────────────────────────────────
# 凭据解析
# ─────────────────────────────────────────────────────────────────────────────

def _candidate_credential_files() -> list[Path]:
    home = Path.home()
    return [
        Path(os.environ.get("DSH_HOME", home / ".dsh")) / ".credentials.yaml",
        home / ".dsh" / "consensus-pipeline" / ".env",
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]


def _read_key_from_file(path: Path) -> Optional[str]:
    """从 .env / .credentials.yaml 里找 DEEPSEEK_API_KEY。"""
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                k, _, v = line.partition(sep)
                if k.strip().upper() == "DEEPSEEK_API_KEY":
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
    return None


def resolve_api_key(explicit: Optional[str] = None) -> tuple[Optional[str], str]:
    """按优先级解析 API key，返回 (key, 来源说明)。

    实现委托给 `engine.settings` —— 优先级链只保留一处定义。
    曾是这里一份、settings 一份，两份迟早会漂移。
    """
    if explicit:
        return explicit, "请求参数"
    r = _settings.resolve()
    return r["api_key"], r["key_source"] if r["api_key"] else "未找到"


# ─────────────────────────────────────────────────────────────────────────────
# 用量与调用
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    failures: int = 0
    latencies: list[float] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "avg_latency_ms": (round(1000 * sum(self.latencies) / len(self.latencies))
                               if self.latencies else None),
        }


class LLMError(RuntimeError):
    """LLM 调用失败（重试耗尽）。"""


class LLMClient:
    """线程安全的 LLM 客户端（配置审议中多个分析师并行调用）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 120,
        max_retries: int = 3,
        temperature: float = 0.6,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        key, source = resolve_api_key(api_key)
        self.api_key = key
        self.key_source = source
        # 地址与模型的解析统一交给 settings 模块 —— 三者的优先级链只写一处，
        # 否则「密钥认应用内配置、模型却认环境变量」这类不一致迟早会出现。
        cfg = _settings.resolve()
        self.base_url = (base_url or cfg["base_url"] or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or cfg["model"] or DEFAULT_MODEL
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.usage = Usage()
        self._lock = threading.Lock()
        self._on_event = on_event

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _emit(self, event: dict) -> None:
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                pass

    def chat(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 2048,
        json_mode: bool = False,
        label: str = "",
    ) -> str:
        """单轮对话。失败会重试，重试耗尽抛 LLMError。"""
        if not self.api_key:
            raise LLMError(
                "未配置 DEEPSEEK_API_KEY。请在项目根目录 .env 中写入 "
                "DEEPSEEK_API_KEY=sk-xxx，或设置同名环境变量。")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            t0 = time.time()
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                    verify=VERIFY,
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                data = r.json()

                content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                u = data.get("usage") or {}
                dt = time.time() - t0
                with self._lock:
                    self.usage.calls += 1
                    self.usage.prompt_tokens += int(u.get("prompt_tokens") or 0)
                    self.usage.completion_tokens += int(u.get("completion_tokens") or 0)
                    self.usage.latencies.append(dt)
                self._emit({"type": "llm_call", "label": label, "ok": True,
                            "latency_ms": round(dt * 1000),
                            "prompt_tokens": u.get("prompt_tokens"),
                            "completion_tokens": u.get("completion_tokens")})
                return content

            except Exception as exc:  # noqa: BLE001
                last_err = exc
                wait = min(8.0, 1.2 * (2 ** attempt)) + random.uniform(0, 0.5)
                logger.warning("LLM 调用失败(第 %d/%d 次, %s): %s，%.1fs 后重试",
                               attempt + 1, self.max_retries, label, exc, wait)
                if attempt < self.max_retries - 1:
                    time.sleep(wait)

        with self._lock:
            self.usage.failures += 1
        self._emit({"type": "llm_call", "label": label, "ok": False, "error": str(last_err)})
        raise LLMError(f"重试 {self.max_retries} 次仍失败（{label}）: {last_err}")

    def make_callable(self, *, system: Optional[str] = None, max_tokens: int = 2048,
                      temperature: Optional[float] = None, label: str = "") -> Callable[[str], str]:
        """返回 fn(prompt) -> str，供 stance 模块的 llm_call_fn 形参使用。"""
        def _fn(prompt: str) -> str:
            return self.chat(prompt, system=system, max_tokens=max_tokens,
                             temperature=temperature, label=label)
        return _fn

    def probe(self) -> dict:
        """连通性自检。"""
        if not self.api_key:
            return {"ok": False, "error": f"未找到 API key（{self.key_source}）"}
        try:
            t0 = time.time()
            out = self.chat("只回复两个字：就绪", max_tokens=16, temperature=0.0,
                            label="probe")
            return {"ok": True, "latency_ms": round((time.time() - t0) * 1000),
                    "model": self.model, "base_url": self.base_url,
                    "key_source": self.key_source, "reply": out.strip()[:40]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "model": self.model,
                    "base_url": self.base_url, "key_source": self.key_source}


def extract_json(text: str) -> Optional[dict]:
    """从模型输出里提取第一个完整 JSON 对象（容错）。"""
    if not text:
        return None
    import re
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for blk in fenced:
        try:
            return json.loads(blk)
        except Exception:  # noqa: BLE001
            continue
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:  # noqa: BLE001
                    return None
    return None
