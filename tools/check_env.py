#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检：依赖、数据集、API key、端口占用。"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import importlib
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

REQUIRED = ["requests", "fastapi", "uvicorn"]
OPTIONAL = ["qrcode", "PIL", "pydantic", "starlette"]


def main() -> int:
    print("=" * 66)
    print("「智能多维投资系统」环境自检")
    print("=" * 66)
    print(f"Python {sys.version.split()[0]}  @ {sys.executable}")
    print()

    ok = True
    print("【依赖】")
    for m in REQUIRED:
        try:
            mod = importlib.import_module(m)
            print(f"  ✅ {m:12} {getattr(mod, '__version__', '')}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  ❌ {m:12} 缺失 —— pip install {m}")
    for m in OPTIONAL:
        try:
            mod = importlib.import_module(m)
            print(f"  ✅ {m:12} {getattr(mod, '__version__', '')}")
        except Exception:  # noqa: BLE001
            print(f"  ⚠️  {m:12} 未安装（可选，影响二维码/校验）")
    print()

    print("【内置数据集】")
    ds = ROOT / "backend" / "data" / "dataset"
    for fn in ("meta.json", "market_history.json", "market_stats.json"):
        p = ds / fn
        if p.is_file():
            print(f"  ✅ {fn:24} {p.stat().st_size/1024:,.0f} KB")
        else:
            ok = False
            print(f"  ❌ {fn:24} 缺失 —— 运行 python tools/fetch_market_data.py --build")
    meta_p = ds / "meta.json"
    if meta_p.is_file():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        print(f"     数据源：{meta.get('source')}")
        print(f"     抓取于：{meta.get('fetched_at')}｜标的 {meta.get('n_symbols')} 个")
    print()

    print("【LLM 后端】")
    try:
        from engine.llm import resolve_api_key, LLMClient
        key, source = resolve_api_key()
        if key:
            print(f"  ✅ DEEPSEEK_API_KEY 已找到（来源：{source}，{key[:8]}…{key[-4:]}）")
            client = LLMClient()
            probe = client.probe()
            if probe.get("ok"):
                print(f"  ✅ 接口连通：{probe['model']} @ {probe['base_url']}"
                      f"（{probe['latency_ms']} ms）")
            else:
                print(f"  ❌ 接口不通：{probe.get('error')}")
                ok = False
        else:
            ok = False
            print("  ❌ 未找到 DEEPSEEK_API_KEY")
            print("     请在项目根目录创建 .env，写入：DEEPSEEK_API_KEY=sk-xxxx")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  ❌ 检查失败：{exc}")
    print()

    print("【端口】")
    for port in (8760, 8761):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        free = s.connect_ex(("127.0.0.1", port)) != 0
        s.close()
        print(f"  {'✅' if free else '⚠️ '} 127.0.0.1:{port} {'可用' if free else '已被占用'}")
    print()

    print("【局域网地址】（手机连接用）")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan = s.getsockname()[0]
        s.close()
        print(f"  http://{lan}:8760")
    except Exception:  # noqa: BLE001
        print("  （未能探测到局域网 IP）")
    print()

    print("=" * 66)
    print("自检结果：" + ("通过 ✅" if ok else "存在问题 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
