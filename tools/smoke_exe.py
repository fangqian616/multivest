#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对打包好的 exe 做一次不花钱的冒烟测试。

验证的是"打出来的东西能不能用"，而不是业务逻辑（那是 test_offline.py 的活）：
  1. 进程能起来并打印出实际端口
  2. /api/health 返回 ok
  3. /api/settings 结构完整，且**不含明文 api_key**
  4. 首页返回 HTML，且引用的静态资源都存在
  5. 退出码可控，进程树被清理干净

刻意不做的事：不调用大模型、不发起真实审议 —— 那些既慢又花钱。

用法
----
    python tools/smoke_exe.py                       # 测 dist/智能多维投资系统.exe
    python tools/smoke_exe.py --exe dist/x.exe --port 8790
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "智能多维投资系统.exe"


def get(url: str, timeout: float = 10.0):
    with urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def wait_health(port: int, timeout: float) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            status, body = get(url, timeout=3)
            if status == 200 and json.loads(body).get("ok"):
                return True
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--boot-timeout", type=float, default=150.0)
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.is_file():
        print(f"❌ 找不到 {exe}")
        return 1
    print(f"被测 exe：{exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB）")

    tmp = Path(tempfile.mkdtemp(prefix="multivest-smoke-"))
    env = dict(os.environ)
    env["MULTIVEST_DATA_DIR"] = str(tmp)   # 隔离，别碰用户真实数据
    env["MULTIVEST_NIGHTLY_ENABLED"] = "0"

    log = tmp / "stdout.txt"
    with log.open("wb") as fh:
        proc = subprocess.Popen([str(exe), "--port", str(args.port)],
                                stdout=fh, stderr=subprocess.STDOUT,
                                env=env, cwd=str(exe.parent))
    print(f"已启动 pid={proc.pid}，等待就绪 …")

    port, t0 = None, time.time()
    try:
        # 从 stdout 里读实际端口：8760 被占用时会自动往后找
        while time.time() - t0 < args.boot_timeout:
            if proc.poll() is not None:
                print(f"❌ 进程提前退出，退出码 {proc.returncode}")
                print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
                return 1
            text = log.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"正在启动本地服务（端口 (\d+)）", text)
            if m:
                port = int(m.group(1))
                break
            time.sleep(0.5)
        if port is None:
            print("❌ 等待端口超时")
            print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1

        print(f"  端口 {port}（{time.time() - t0:.1f}s）")
        if not wait_health(port, args.boot_timeout - (time.time() - t0)):
            print("❌ /api/health 未就绪")
            print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        print("  ✅ /api/health")

        base = f"http://127.0.0.1:{port}"
        status, body = get(f"{base}/api/settings")
        s = json.loads(body)
        assert status == 200
        print("  ✅ /api/settings")
        for k in ("configured", "key_masked", "key_source", "base_url",
                  "model", "model_choices", "settings_file"):
            if k not in s:
                print(f"     ❌ 缺字段 {k}")
                return 1
        if "api_key" in s:
            print("     ❌ 接口回传了明文 api_key")
            return 1
        print(f"     已配置={s['configured']}  掩码={s['key_masked'] or '(无)'}"
              f"  模型={s['model']}  来源={s['key_source']}")

        status, html = get(f"{base}/")
        page = html.decode("utf-8", errors="replace")
        if status != 200 or "行情总览" not in page:
            print(f"     ❌ 首页异常 status={status} len={len(page)}")
            return 1
        print(f"  ✅ 首页 {len(page)} 字节")

        for asset in ("/app.js", "/style.css", "/charts.js"):
            try:
                st, b = get(f"{base}{asset}")
            except URLError as exc:
                print(f"     ❌ {asset} 取不到：{exc}")
                return 1
            if st != 200 or not b:
                print(f"     ❌ {asset} status={st}")
                return 1
            print(f"  ✅ {asset}（{len(b)} 字节）")

        print()
        print("=" * 58)
        print("  ✅ exe 冒烟测试通过")
        print("=" * 58)
        return 0
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
