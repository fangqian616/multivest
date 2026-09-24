#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把单文件 exe 打成一个**双击即装**的安装包（7-Zip 自解压格式）。

为什么用 7z SFX 而不是 Inno Setup / NSIS
----------------------------------------
本机没有 Inno Setup，也没有 NSIS；而 7-Zip **已经装了**，自带 `7z.sfx`
自解压模块。SFX 的配置块支持 `RunProgram`，因此可以在解压后自动执行
一个安装脚本，效果与专业安装器在"装到用户目录 + 建快捷方式 + 可卸载"
这三件事上没有区别，且**不需要新增任何依赖**。

产物
----
    dist/Multivest-Setup-<版本>.exe     双击 → 解压到临时目录 → 运行安装脚本

安装脚本做什么
--------------
    1. 复制主程序到 %LOCALAPPDATA%\\Programs\\Multivest\\（无需管理员权限）
    2. 建桌面快捷方式与开始菜单快捷方式（带图标）
    3. 写一个卸载脚本，并注册到「应用和功能」列表（HKCU，不需要管理员）
    4. 询问是否立即启动

为什么装在 LOCALAPPDATA 而不是 Program Files
--------------------------------------------
Program Files 需要管理员权限，会弹 UAC。这是一个个人向的研究工具，
装在用户目录下更顺 —— 双击即装、双击即卸，不触碰系统目录。

用法
----
    python tools/make_installer.py                 # 用 dist/ 下最新的 exe
    python tools/make_installer.py --version 1.0.0
    python tools/make_installer.py --exe dist/x.exe --out dist/y.exe
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP_NAME = "智能多维投资系统"
APP_NAME_EN = "Multivest"
DEFAULT_VERSION = "1.1.0"

SFX_CANDIDATES = [
    Path(r"C:\Program Files\7-Zip\7z.sfx"),
    Path(r"C:\Program Files (x86)\7-Zip\7z.sfx"),
]
SEVEN_ZIP_CANDIDATES = [
    Path(r"C:\Program Files\7-Zip\7z.exe"),
    Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
]


def find(cands: list, what: str) -> Path:
    for c in cands:
        if c.is_file():
            return c
    print(f"❌ 找不到 {what}，候选路径：")
    for c in cands:
        print(f"     {c}")
    raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 安装脚本（解压后由 SFX 执行）
# ─────────────────────────────────────────────────────────────────────────────

SETUP_PS1 = r"""# Multivest 安装脚本 —— 由 7z 自解压包在解压后调用。
# 全部操作限于当前用户目录与 HKCU，不请求管理员权限。
$ErrorActionPreference = 'Stop'
$src      = Split-Path -Parent $MyInvocation.MyCommand.Path
$appDir   = Join-Path $env:LOCALAPPDATA 'Programs\Multivest'
$exeName  = '__EXE_NAME__'
$version  = '__VERSION__'
$appName  = '__APP_NAME__'
$appEn    = '__APP_NAME_EN__'

Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkGray
Write-Host "  $appName · $appEn  $version" -ForegroundColor White
Write-Host '============================================================' -ForegroundColor DarkGray
Write-Host ''

New-Item -ItemType Directory -Force -Path $appDir | Out-Null
$dst = Join-Path $appDir $exeName
Write-Host "  正在安装到 $appDir ..."
Copy-Item -Force (Join-Path $src $exeName) $dst

# 图标：优先用 exe 自带的图标
$icon = "$dst,0"

$sh = New-Object -ComObject WScript.Shell

function New-Lnk([string]$path, [string]$target, [string]$workdir) {
    $l = $sh.CreateShortcut($path)
    $l.TargetPath       = $target
    $l.WorkingDirectory = $workdir
    $l.IconLocation     = $icon
    $l.Description      = "$appName · $appEn"
    $l.Save()
}

$desktop = [Environment]::GetFolderPath('Desktop')
New-Lnk (Join-Path $desktop "$appName.lnk") $dst $appDir

$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
New-Lnk (Join-Path $startMenu "$appName.lnk") $dst $appDir

# ── 卸载脚本 + 注册到「应用和功能」─────────────────────────────────────────
$uninst = Join-Path $appDir 'uninstall.ps1'
@"
`$ErrorActionPreference = 'SilentlyContinue'
`$appDir = '$appDir'
Remove-Item -Force (Join-Path ([Environment]::GetFolderPath('Desktop')) '$appName.lnk')
Remove-Item -Force (Join-Path `$env:APPDATA 'Microsoft\Windows\Start Menu\Programs\$appName.lnk')
Remove-Item -Recurse -Force `$appDir
Remove-Item -Recurse -Force 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Multivest'
Write-Host '已卸载 $appName。' -ForegroundColor Green
"@ | Set-Content -Path $uninst -Encoding UTF8

$uninstLnk = Join-Path $startMenu "$appName 卸载.lnk"
$l = $sh.CreateShortcut($uninstLnk)
$l.TargetPath = 'powershell.exe'
$l.Arguments  = "-NoProfile -ExecutionPolicy Bypass -File `"$uninst`""
$l.IconLocation = $icon
$l.Save()

$reg = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Multivest'
New-Item -Force -Path $reg | Out-Null
Set-ItemProperty -Path $reg -Name DisplayName     -Value "$appName ($appEn)"
Set-ItemProperty -Path $reg -Name DisplayVersion  -Value $version
Set-ItemProperty -Path $reg -Name InstallLocation -Value $appDir
Set-ItemProperty -Path $reg -Name UninstallString -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninst`""
Set-ItemProperty -Path $reg -Name Publisher       -Value 'fangqian616'
Set-ItemProperty -Path $reg -Name NoModify        -Value 1 -Type DWord
Set-ItemProperty -Path $reg -Name NoRepair        -Value 1 -Type DWord

Write-Host ''
Write-Host '  安装完成。' -ForegroundColor Green
Write-Host "    程序位置：$dst"
Write-Host '    桌面与开始菜单已创建快捷方式'
Write-Host "    卸载：开始菜单 →「$appName 卸载」，或「设置 → 应用」"
Write-Host ''
$ans = Read-Host '  现在启动吗？(Y/n)'
if ($ans -eq '' -or $ans -match '^[Yy]') { Start-Process $dst }
"""

SETUP_CMD = r"""@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
"""


def build(exe: Path, out: Path, version: str) -> int:
    sfx = find(SFX_CANDIDATES, "7z.sfx（7-Zip 自解压模块）")
    seven = find(SEVEN_ZIP_CANDIDATES, "7z.exe")
    print(f"7z.sfx  : {sfx}")
    print(f"7z.exe  : {seven}")
    print(f"源 exe  : {exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB）")

    staging = Path(tempfile.mkdtemp(prefix="multivest-sfx-"))
    try:
        shutil.copy2(exe, staging / exe.name)
        ps1 = (SETUP_PS1
               .replace("__EXE_NAME__", exe.name)
               .replace("__VERSION__", version)
               .replace("__APP_NAME__", APP_NAME)
               .replace("__APP_NAME_EN__", APP_NAME_EN))
        # PowerShell 读取 UTF-8 脚本需要 BOM，否则中文会乱码
        (staging / "install.ps1").write_text(ps1, encoding="utf-8-sig")
        (staging / "setup.cmd").write_text(SETUP_CMD, encoding="utf-8")
        # 保留日期便于排查；安装脚本本身不依赖这个文件
        (staging / "VERSION.txt").write_text(
            f"{APP_NAME} {APP_NAME_EN}\n版本 {version}\n"
            f"构建 {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}\n",
            encoding="utf-8")

        archive = staging / "payload.7z"
        print("  正在压缩 …")
        r = subprocess.run([str(seven), "a", "-t7z", "-mx=7", "-bso0", "-bsp0",
                            str(archive), str(staging / "*")],
                           capture_output=True)
        if r.returncode != 0 or not archive.is_file():
            print("  ❌ 压缩失败：", r.stderr.decode("utf-8", "replace")[:400])
            return 1
        print(f"  payload.7z {archive.stat().st_size / 1024 / 1024:.1f} MB")

        config = (
            ";!@Install@!UTF-8!\n"
            f'Title="{APP_NAME} {APP_NAME_EN} 安装程序"\n'
            f'BeginPrompt="即将安装 {APP_NAME} {version} 到您的用户目录。\\n'
            f'无需管理员权限，可随时从「应用和功能」卸载。\\n\\n继续吗？"\n'
            'ExtractTitle="正在解压安装文件…"\n'
            'ExtractDialogText="正在准备…"\n'
            'RunProgram="setup.cmd"\n'
            "GUIMode=\"2\"\n"
            ";!@InstallEnd@!\n"
        ).encode("utf-8")

        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as fh:
            fh.write(sfx.read_bytes())
            fh.write(config)
            fh.write(archive.read_bytes())

        if not out.is_file() or out.stat().st_size < 1024:
            print("  ❌ 产物异常")
            return 1
        print()
        print("=" * 60)
        print(f"  ✅ {out.name}")
        print("=" * 60)
        print(f"  路径 : {out}")
        print(f"  体积 : {out.stat().st_size / 1024 / 1024:.1f} MB")
        print(f"  版本 : {version}")
        print("  用法 : 双击 → 自动解压 → 安装到 "
              "%LOCALAPPDATA%\\Programs\\Multivest\\ → 桌面快捷方式 → 询问启动")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--version", default=DEFAULT_VERSION)
    args = ap.parse_args()

    exe = Path(args.exe) if args.exe else DIST / f"{APP_NAME}.exe"
    if not exe.is_file():
        print(f"❌ 找不到主程序：{exe}\n   先运行 python tools/build_desktop.py")
        return 1
    out = Path(args.out) if args.out else DIST / f"{APP_NAME_EN}-Setup-{args.version}.exe"
    return build(exe, out, args.version)


if __name__ == "__main__":
    sys.exit(main())
