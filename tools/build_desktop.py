#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把桌面壳打包成**单个 exe**（无需 Rust / 编译器，PyInstaller 自带引导器）。

产物：
    dist/智能多维投资系统.exe          单文件，双击即启动后端并打开原生窗口
    dist/智能多维投资系统/             目录版（启动更快，便于排查）

同时生成桌面快捷方式（带自定义图标）。

用法：
    python tools/build_desktop.py            # 打包 + 建快捷方式
    python tools/build_desktop.py --no-build # 只建快捷方式
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
ICON = ROOT / "frontend" / "assets" / "icon.ico"
APP_NAME = "智能多维投资系统"


def build_exe(onefile: bool = True) -> int:
    # 先确认当前解释器真的有 PyInstaller。
    # 本机同时存在多个 Python（venv 的 C:\ov\Scripts\python.exe 与 Windows Store 版），
    # 而 `python` 解析到哪个会随 PATH 变化 —— 解析到没装 PyInstaller 的那个时，
    # 打包只会以一句含糊的失败结束。这里提前把该用哪个解释器说清楚。
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("=" * 68)
        print("  当前 Python 没有安装 PyInstaller")
        print("=" * 68)
        print(f"  正在使用的解释器：{sys.executable}")
        print("\n  本机有多个 Python，请用装了依赖的那个跑本脚本，例如：")
        # 只按位置猜常见安装点，不写死具体用户名
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        for cand in (Path(r"C:\ov\Scripts\python.exe"),
                     *(local / "Programs" / "Python" / f"Python3{v}" / "python.exe"
                       for v in ("12", "11", "13"))):
            if cand.is_file():
                print(f"    {cand}  tools\\build_desktop.py")
        print(f"\n  或先给当前解释器装上：{sys.executable} -m pip install pyinstaller")
        return 1

    if not ICON.is_file():
        print("图标缺失，正在生成…")
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=False)

    # 需要一并打进 exe 的数据：后端代码、前端页面、内置数据集
    sep = ";" if sys.platform.startswith("win") else ":"
    add_data = [
        f"{ROOT / 'backend'}{sep}backend",
        f"{ROOT / 'frontend'}{sep}frontend",
    ]
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--icon", str(ICON),
        "--add-data", add_data[0],
        "--add-data", add_data[1],
        # 运行时动态导入 / 反射用到的模块，需显式声明。
        #
        # 关键坑：backend/app.py 是作为 **数据** 打包的（--add-data），
        # PyInstaller 的静态分析看不到它内部的 import，因此 fastapi/starlette
        # 这些"间接依赖"不会被收集 —— 表现是打包成功、运行时报
        # ModuleNotFoundError: No module named 'fastapi'。
        # 用 --collect-all 把整个 Web 栈连子模块一起收进来。
        "--collect-all", "fastapi",
        "--collect-all", "starlette",
        "--collect-all", "uvicorn",
        "--collect-all", "pydantic",
        "--collect-all", "pydantic_core",
        "--collect-all", "anyio",
        "--collect-all", "qrcode",           # 在函数内延迟导入
        "--collect-all", "webview",          # 原生窗口（缺失时自动回退浏览器）
        # engine/llm.py 同样躺在数据目录里，它的 import 也分析不到 —— 必须显式收
        "--collect-all", "requests",
        "--collect-all", "urllib3",
        "--collect-all", "certifi",
        "--collect-all", "idna",
        "--collect-all", "charset_normalizer",
        "--hidden-import", "PIL.Image",
        # 注意：不要写 --hidden-import engine / data。
        # backend/engine 与 backend/data 是「数据」而非源码树里的包，
        # PyInstaller 找不到它们；真要编译进来还会让 data.market 的
        # Path(__file__).parent/"dataset" 指向错误位置。
        # 正确的做法是让 sys.path 包含 _MEIPASS/backend，从数据目录导入。
        # 排除体积大且用不到的库
        "--exclude-module", "matplotlib", "--exclude-module", "numpy",
        "--exclude-module", "pandas", "--exclude-module", "tkinter",
        "--exclude-module", "PyQt5", "--exclude-module", "PySide6",
        "--exclude-module", "scipy", "--exclude-module", "notebook",
        # 控制台保留（便于看日志；改成 --windowed 可隐藏，但出错时无从排查）
        "--console",
    ]
    if onefile:
        cmd.append("--onefile")
    cmd.append(str(ROOT / "desktop.py"))

    print("正在打包（首次约 1-3 分钟）…")
    print(" ".join(cmd[:12]) + " …")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("\n打包失败。请检查上方输出。")
        return r.returncode

    exe = DIST / f"{APP_NAME}.exe"
    if exe.is_file():
        print(f"\n✅ 打包完成：{exe}  （{exe.stat().st_size / 1024 / 1024:.1f} MB）")
        print("   直接双击即可运行：它会自动启动本地服务并打开应用窗口。")
    return 0


def make_shortcut() -> int:
    """在桌面与开始菜单创建带图标的快捷方式。"""
    if not sys.platform.startswith("win"):
        print("（非 Windows，跳过快捷方式）")
        return 0

    exe = DIST / f"{APP_NAME}.exe"
    bat = ROOT / "启动.bat"
    if exe.is_file():
        target, target_args = str(exe), ""
        workdir = str(DIST)
    else:
        target, target_args = str(bat), ""
        workdir = str(ROOT)
        print("（未找到打包好的 exe，快捷方式将指向 启动.bat）")

    icon = str(ICON) if ICON.is_file() else str(exe) if exe.is_file() else ""
    desktop = Path.home() / "Desktop"
    targets = [desktop / f"{APP_NAME}.lnk"]
    start_menu = (Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
                  / "Start Menu" / "Programs")
    if start_menu.is_dir():
        targets.append(start_menu / f"{APP_NAME}.lnk")

    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$targets = @({",".join("'" + str(t) + "'" for t in targets)})
foreach ($p in $targets) {{
    $sc = $ws.CreateShortcut($p)
    $sc.TargetPath = '{target}'
    $sc.Arguments = '{target_args}'
    $sc.WorkingDirectory = '{workdir}'
    $sc.Description = '智能多维投资系统 · 家庭资产配置多智能体投顾'
    {"$sc.IconLocation = '" + icon + "'" if icon else ""}
    $sc.Save()
    Write-Output "  已创建 $p"
}}
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip() or "  快捷方式创建完成")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true", help="只建快捷方式，不重新打包")
    ap.add_argument("--onedir", action="store_true", help="打包成目录版（启动更快）")
    args = ap.parse_args()

    rc = 0
    if not args.no_build:
        rc = build_exe(onefile=not args.onedir)
        if rc != 0:
            return rc
    make_shortcut()
    print("\n完成。可以双击桌面上的「智能多维投资系统」图标启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
