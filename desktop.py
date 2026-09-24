#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智能多维投资系统」桌面壳。

启动本地后端 → 等待就绪 → 用 WebView2 打开**原生窗口** → 窗口关闭时优雅停止后端。

为什么不用 Pake/Tauri：Pake 需要 Rust + MSVC 生成工具链（约 2-4 GB，且需管理员权限）。
本机已自带 WebView2 运行时（Windows 10/11 标配），pywebview 通过 .NET 调用它，
零编译依赖即可得到同等体验：无浏览器地址栏、独立任务栏图标、独立窗口。

打包为单个 exe（可选）：
    python tools/build_desktop.py
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
ASSETS = ROOT / "frontend" / "assets"
ICON = ASSETS / "icon.ico"
APP_TITLE = "智能多维投资系统"
APP_TITLE_EN = "Multivest"

# Windows 控制台的默认编码是 GBK（取决于区域设置），打印 ◎ 之类的字符会抛
# UnicodeEncodeError 并让整个应用**静默退出** —— 表现是"双击了、服务起来一瞬、
# 然后什么都没了"，极难排查。冻结模式下 PYTHONIOENCODING 来不及生效，
# 必须在解释器起来后立即 reconfigure。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

FROZEN = bool(getattr(sys, "frozen", False))

# 打包成单文件 exe 后，代码被解压到临时目录（_MEIPASS），退出即清空。
# 记录与档案必须落在**持久位置**，否则用户关掉程序就丢数据。
if FROZEN:
    BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
    BACKEND = BUNDLE / "backend"
    ASSETS = BUNDLE / "frontend" / "assets"
    ICON = ASSETS / "icon.ico"
    DATA_ROOT = Path(sys.executable).resolve().parent / "智能多维数据"
else:
    DATA_ROOT = ROOT / "backend"
DATA_ROOT.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────

def free_port(preferred: int = 8760, tries: int = 12) -> int:
    for p in range(preferred, preferred + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
        finally:
            s.close()
    return preferred


def wait_health(port: int, timeout: float = 60.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.4)
    return False


class Backend:
    """把后端作为子进程管理，确保窗口关闭后不会残留孤儿进程。

    冻结（exe）模式下 sys.executable 是应用自身、没有独立解释器可 fork，
    因此改为**进程内**用 uvicorn 线程启动 —— 这也是 onefile 打包唯一可行的方式。
    """

    def __init__(self, port: int, host: str = "0.0.0.0", verbose: bool = False):
        self.port = port
        self.host = host
        self.proc: subprocess.Popen | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.verbose = verbose
        self._log_buf: list[str] = []

    # ── 启动 ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        os.environ["MULTIVEST_DATA_DIR"] = str(DATA_ROOT)
        os.environ["PYTHONIOENCODING"] = "utf-8"
        os.environ["PYTHONUNBUFFERED"] = "1"
        if str(BACKEND) not in sys.path:
            sys.path.insert(0, str(BACKEND))
        if str(DATA_ROOT) not in sys.path:
            sys.path.insert(0, str(DATA_ROOT))
        if FROZEN:
            self._start_inprocess()
        else:
            self._start_subprocess()

    def _start_inprocess(self) -> None:
        import uvicorn  # noqa: PLC0415

        import app as app_module  # noqa: PLC0415

        cfg = uvicorn.Config(app_module.app, host=self.host, port=self.port,
                             log_level="info" if self.verbose else "warning",
                             access_log=self.verbose)
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="multivest-server")
        self._thread.start()
        self._log_buf.append(f"[in-process] uvicorn 已启动于 {self.host}:{self.port}")
        self._log_buf.append(f"[in-process] 数据目录 {DATA_ROOT}")

    def _start_subprocess(self) -> None:
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" and not self.verbose else 0
        self.proc = subprocess.Popen(
            [sys.executable, str(BACKEND / "app.py"),
             "--host", self.host, "--port", str(self.port)],
            cwd=str(BACKEND), env=dict(os.environ),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creation, text=True, encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._log_buf.append(line.rstrip())
            if len(self._log_buf) > 400:
                self._log_buf.pop(0)
            if self.verbose:
                print(line, end="")

    def tail(self, n: int = 25) -> str:
        return "\n".join(self._log_buf[-n:])

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._thread:
                self._thread.join(timeout=6)
            return
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=6)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def fatal(msg: str, backend: Backend | None = None) -> int:
    print("\n" + "=" * 68)
    print("  启动失败")
    print("=" * 68)
    print(msg)
    if backend:
        tail = backend.tail()
        if tail:
            print("\n--- 后端输出（尾部）---")
            print(tail)
    print()
    try:
        input("按回车键退出…")
    except EOFError:
        pass
    return 1


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="智能多维投资系统 桌面客户端")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 允许手机访问同一后端")
    ap.add_argument("--browser", action="store_true", help="强制用系统浏览器而不是原生窗口")
    ap.add_argument("--verbose", action="store_true", help="显示后端日志")
    args = ap.parse_args()

    port = free_port(args.port)
    backend = Backend(port, host=args.host, verbose=args.verbose)

    # 启动期的输出全部 flush=True。
    # 被重定向到管道时 Python 的 stdout 是块缓冲：不 flush 的话
    # 「服务已就绪 / 端口 8760」这些行会一直留在缓冲区里，
    # 外部（包括自动化测试与桌面启动器）无法知道程序到底起没起来、用的是哪个端口。
    # —— 我自己的打包测试就因为看不到端口而写死了 8761，把正常运行的实例误判成失败。
    print("=" * 68, flush=True)
    print(f"  {APP_TITLE}", flush=True)
    print(f"  {APP_TITLE_EN}", flush=True)
    print("=" * 68, flush=True)
    print(f"  正在启动本地服务（端口 {port}）…", flush=True)
    backend.start()

    if not wait_health(port, timeout=60):
        return fatal(
            "后端在 60 秒内未就绪。常见原因：\n"
            "  · 未配置 DEEPSEEK_API_KEY（服务仍会启动，但审议不可用）\n"
            "  · 依赖缺失：pip install fastapi uvicorn requests qrcode\n"
            "  · 内置数据集缺失：python tools/fetch_market_data.py --build",
            backend)

    url = f"http://127.0.0.1:{port}"
    print(f"  ✅ 服务已就绪", flush=True)
    print(f"     本机访问： {url}", flush=True)
    print(f"     手机访问： http://{lan_ip()}:{port}   （需同一 WiFi）", flush=True)
    print("=" * 68, flush=True)

    # 原生窗口（WebView2）
    if not args.browser:
        try:
            import webview  # type: ignore

            # WebView2 的用户数据目录必须**持久且稳定**。
            # 早期放在 ROOT/.desktop-profile，而冻结模式下 ROOT 是 _MEIPASS
            # （每次启动都是全新的临时目录）—— 于是每次运行都新建一个用户数据
            # 目录，上一次遗留的孤儿 msedgewebview2.exe 还锁着旧目录，新的一次
            # 就以 0x800700AA（资源在使用中）初始化失败。更麻烦的是这属于
            # **原生层崩溃**，Python 的 try/except 接不住，整个进程连同后端一起
            # 消失（表现为"双击了、服务起来一瞬、然后什么都没了"）。
            storage = DATA_ROOT / "webview2"
            storage.mkdir(parents=True, exist_ok=True)
            webview.create_window(
                APP_TITLE,
                url,
                width=1480, height=980, min_size=(980, 660),
                background_color="#F5F6F8",
                text_select=True,
                confirm_close=False,
            )
            print("\n  窗口已打开。关闭窗口即退出程序。\n")
            webview.start(
                private_mode=False,          # 保留本地存储（记住上次填写的档案）
                storage_path=str(storage),
                icon=str(ICON) if ICON.is_file() else None,
                debug=args.verbose,
            )
            backend.stop()
            print("  已退出。")
            return 0
        except ImportError:
            print("\n  ⚠ 未安装 pywebview，回退到系统浏览器。")
            print("    安装原生窗口：pip install pywebview")
        except Exception as exc:  # noqa: BLE001
            print(f"\n  ⚠ 原生窗口启动失败（{type(exc).__name__}: {exc}），回退到系统浏览器。")

    webbrowser.open(url)
    print("\n  已在默认浏览器中打开。")
    print("  提示：关闭本窗口即停止服务（手机端将同时断开）。")
    print("  按 Ctrl+C 退出。\n")
    try:
        while True:
            if backend._server is not None:              # noqa: SLF001 进程内模式
                if not (backend._thread and backend._thread.is_alive()):
                    break
            elif backend.proc is not None and backend.proc.poll() is not None:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        # 桌面应用的失败默认不可见（双击后一闪而过）。这里保证异常一定被看到，
        # 并且同目录留下崩溃日志，便于事后排查。
        import traceback
        tb = traceback.format_exc()
        print("\n" + "=" * 68)
        print("  智能多维投资系统 启动失败")
        print("=" * 68)
        print(tb)
        try:
            log = (Path(sys.executable).resolve().parent if FROZEN else ROOT) / "崩溃日志.txt"
            log.write_text(tb, encoding="utf-8")
            print(f"崩溃详情已写入：{log}")
        except Exception:  # noqa: BLE001
            pass
        try:
            input("按回车键退出…")
        except EOFError:
            pass
        sys.exit(1)
