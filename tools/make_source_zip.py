#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把**被 git 跟踪的文件**打成源码分发包。

为什么用 `git ls-files` 而不是遍历目录
--------------------------------------
发布包最容易出的两类事故是「漏文件」和「带密钥」，两者的根因都是按目录遍历：
· 漏文件 —— 遍历时踩到 .gitignore 之外的自定义规则，或中文名被转义后匹配不上
· 带密钥 —— 遍历把 settings.json、账号清单.md 这类本地文件一并装了进去

以 git 的索引为唯一清单，两个问题同时消失：跟踪的就是要发的，
没跟踪的（含所有本地配置与密钥）天然不在包里。

前置检查
--------
1. `core.quotepath=false`。否则 `git ls-files` 会把中文名输出成 `\\345\\234...`，
   按这个名字去读文件会失败 —— 而且是静默跳过。本机实测曾经 74 个跟踪文件
   只打进 5 个，就是栽在这里。
2. 打包前对每个文本文件做一次密钥形状扫描，命中就中止。

用法
----
    python tools/make_source_zip.py                 # 写 智能多维投资系统-源码.zip
    python tools/make_source_zip.py --out x.zip
    python tools/make_source_zip.py --check-only    # 只做前置检查
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "智能多维投资系统-源码.zip"

# 密钥形状。宁可误报也不要漏报 —— 命中即中止，人工确认后再放行。
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI/DeepSeek 风格密钥"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "GitHub Personal Access Token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{30,}"), "GitHub 细粒度 Token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key"),
]

TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".json", ".yml", ".yaml",
                 ".txt", ".bat", ".sh", ".spec", ".toml", ".cfg", ".ini", ".env",
                 ".example", ".gitignore", ""}


def tracked_files() -> list[str]:
    subprocess.run(["git", "config", "core.quotepath", "false"],
                   cwd=ROOT, check=False)
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        print("git ls-files 失败：", out.stderr[:200], file=sys.stderr)
        raise SystemExit(1)
    return [f for f in out.stdout.splitlines() if f.strip()]


def scan(path: Path, rel: str) -> list[str]:
    """返回该文件命中的密钥告警（空列表表示干净）。"""
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    hits = []
    for rx, label in SECRET_PATTERNS:
        for m in rx.finditer(text):
            frag = m.group(0)
            hits.append(f"{rel}: {label} —— {frag[:8]}…{frag[-4:]}（{len(frag)} 字符）")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()

    files = tracked_files()
    print(f"git 跟踪 {len(files)} 个文件")

    missing = [f for f in files if not (ROOT / f).is_file()]
    if missing:
        print(f"  ⚠ {len(missing)} 个文件在工作区不存在（已跳过）：")
        for f in missing[:10]:
            print(f"      {f}")
    present = [f for f in files if (ROOT / f).is_file()]

    # 目录清单自检：中文名必须原样可读，否则说明 quotepath 没生效
    weird = [f for f in present if "\\" in f]
    if weird:
        print(f"  ❌ {len(weird)} 个路径含反斜杠转义，quotepath 未生效，中止")
        for f in weird[:5]:
            print(f"      {f}")
        return 1

    print("扫描密钥形状 …")
    alarms: list[str] = []
    for rel in present:
        alarms.extend(scan(ROOT / rel, rel))
    if alarms:
        print(f"  ❌ 命中 {len(alarms)} 处，已中止：")
        for a in alarms:
            print(f"      {a}")
        return 1
    print("  干净")

    if args.check_only:
        print("仅检查，未打包")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in present:
            z.write(ROOT / rel, rel)

    with zipfile.ZipFile(out) as z:
        n = len(z.namelist())

    size_mb = out.stat().st_size / 1024 / 1024
    print()
    print("=" * 58)
    print(f"  ✅ {out.name}")
    print("=" * 58)
    print(f"  路径  : {out}")
    print(f"  文件  : {n} 个（跟踪 {len(files)}，缺失 {len(missing)}）")
    print(f"  体积  : {size_mb:.2f} MB")
    if n != len(present):
        print("  ❌ 包内条目数与应打包数不一致")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
