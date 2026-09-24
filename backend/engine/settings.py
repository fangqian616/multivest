#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用内设置：API 密钥、接口地址与模型名。

为什么单独做这个，而不是继续只认 .env
--------------------------------------
`.env` 对开发者没问题，对最终用户是障碍：装完程序要先找到项目目录、手写一个文件、
填对键名（写错一个字母就没有任何反馈），然后重启。桌面应用应该在首次启动时就问，
并且随时能改。

配置解析优先级（**从高到低**）
------------------------------
    1. 调用方显式传入（单次请求覆盖）
    2. <数据目录>/settings.json —— 应用内配置，也就是界面上填的
    3. 环境变量 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
    4. 凭据文件（~/.dsh/.credentials.yaml、项目 .env 等）

应用内配置排在环境变量之前，是因为用户既然在界面上填了，那就是他的明确意图；
而 `/api/settings` 会把「当前生效的来源」一并返回，界面上显示出来，
不会出现"我改了却不生效又不知道为什么"的情况。

安全约定
--------
· 密钥只写入 <数据目录>/settings.json，写入时尽量收紧文件权限
· **绝不通过 HTTP 接口回传明文密钥** —— `public_view()` 只给掩码
· 该文件已列入 .gitignore，不会进入版本库
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_REASONER_MODEL = "deepseek-reasoner"

# 常见可选模型，供界面做下拉建议（不限制用户手填）
MODEL_CHOICES = [
    {"value": "deepseek-chat", "label": "deepseek-chat（通用，速度快）"},
    {"value": "deepseek-reasoner", "label": "deepseek-reasoner（推理，更慢更贵）"},
    {"value": "deepseek-flash", "label": "deepseek-flash（最新通用）"},
]


def _data_root() -> Path:
    """与 app.py 用同一套规则定位数据目录，避免两处各算一份。"""
    for k in ("MULTIVEST_DATA_DIR", "HOMEWEALTH_DATA_DIR"):
        v = os.environ.get(k)
        if v:
            return Path(v)
    return Path(__file__).resolve().parents[2]


def settings_path() -> Path:
    return _data_root() / "settings.json"


def load() -> dict[str, Any]:
    p = settings_path()
    try:
        if p.is_file():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 %s 失败（%s），按未配置处理", p, exc)
    return {}


def save(api_key: Optional[str] = None, base_url: Optional[str] = None,
         model: Optional[str] = None) -> dict[str, Any]:
    """写入配置。

    约定：**传 None 表示"不改这一项"，传空字符串表示"清除这一项"**。
    这样界面上可以只改模型而不动密钥 —— 否则每次保存都要把密钥再传一遍，
    既没必要，也增加了密钥在网络上往返的次数。
    """
    d = load()
    if api_key is not None:
        k = api_key.strip()
        if k:
            d["api_key"] = k
        else:
            d.pop("api_key", None)
    if base_url is not None:
        u = base_url.strip().rstrip("/")
        if u:
            d["base_url"] = u
        else:
            d.pop("base_url", None)
    if model is not None:
        m = model.strip()
        if m:
            d["model"] = m
        else:
            d.pop("model", None)

    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    _harden(p)
    return d


def _harden(p: Path) -> None:
    """尽量收紧文件权限。Windows 上 chmod 语义有限，失败不影响功能。"""
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)      # 600
    except OSError:
        pass


def mask(key: Optional[str]) -> str:
    """给界面看的掩码。绝不回传明文。

    只保留前 6 位与后 4 位 —— 够用户确认"是不是我填的那把"，
    又不足以被拿去使用。短于 12 位的直接全遮。
    """
    if not key:
        return ""
    if len(key) < 12:
        return "•" * len(key)
    return f"{key[:6]}{'•' * 8}{key[-4:]}"


# ─────────────────────────────────────────────────────────────────────────────
# 解析当前生效的配置
# ─────────────────────────────────────────────────────────────────────────────

def _from_env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def _credential_files() -> list[Path]:
    home = Path.home()
    cwd = Path.cwd()
    return [
        Path(os.environ.get("DSH_HOME", home / ".dsh")) / ".credentials.yaml",
        home / ".dsh" / "consensus-pipeline" / ".env",
        cwd / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]


def _read_from_file(path: Path, names: tuple[str, ...]) -> Optional[str]:
    """从 .env / .credentials.yaml 里读某个键。两种分隔符都认。"""
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    want = {n.upper() for n in names}
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                k, _, v = line.partition(sep)
                if k.strip().upper() in want:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
    return None


def resolve() -> dict[str, Any]:
    """返回当前生效的密钥／地址／模型，以及各自的来源。

    来源会显示在界面上 —— 用户改了配置却没生效时，一眼能看出是环境变量
    或凭据文件盖过了它，而不是对着界面猜。
    """
    d = load()

    # 密钥
    key, key_src = None, "未配置"
    if d.get("api_key"):
        key, key_src = d["api_key"], "应用内配置"
    elif _from_env("DEEPSEEK_API_KEY"):
        key, key_src = _from_env("DEEPSEEK_API_KEY"), "环境变量 DEEPSEEK_API_KEY"
    else:
        for p in _credential_files():
            v = _read_from_file(p, ("DEEPSEEK_API_KEY",))
            if v:
                key, key_src = v, f"凭据文件 {p}"
                break

    # 接口地址
    if d.get("base_url"):
        base, base_src = d["base_url"], "应用内配置"
    elif _from_env("DEEPSEEK_BASE_URL"):
        base, base_src = _from_env("DEEPSEEK_BASE_URL"), "环境变量 DEEPSEEK_BASE_URL"
    else:
        base, base_src = DEFAULT_BASE_URL, "默认值"

    # 模型
    if d.get("model"):
        model, model_src = d["model"], "应用内配置"
    elif _from_env("DEEPSEEK_MODEL"):
        model, model_src = _from_env("DEEPSEEK_MODEL"), "环境变量 DEEPSEEK_MODEL"
    else:
        model, model_src = DEFAULT_MODEL, "默认值"

    return {
        "api_key": key, "key_source": key_src, "key_masked": mask(key),
        "base_url": base, "base_url_source": base_src,
        "model": model, "model_source": model_src,
        "configured": bool(key),
        "settings_file": str(settings_path()),
        "settings_file_exists": settings_path().is_file(),
    }


def public_view() -> dict[str, Any]:
    """给接口用的视图 —— 去掉明文密钥。"""
    r = resolve()
    r.pop("api_key", None)
    r["model_choices"] = MODEL_CHOICES
    r["default_base_url"] = DEFAULT_BASE_URL
    r["default_model"] = DEFAULT_MODEL
    return r
