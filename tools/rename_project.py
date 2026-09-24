#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目改名工具 —— 一次把所有出现位置改干净。

为什么需要它：改一个名字要动的地方散在三十来个文件里（界面标题、窗口标题、
打包脚本、启动脚本、桌面壳、缓存名、环境变量前缀、文档……），
手工改必然漏掉几处，而漏掉的表现往往是"某个角落还写着旧名字"。

用法
----
    # 只改英文名（中文名保持不变）——这是最常用的场景
    python tools/rename_project.py --en Multivest --prefix MULTIVEST_ --module multivest
    python tools/rename_project.py --en Multivest --prefix MULTIVEST_ --module multivest --apply

    # 中文名也一起改
    python tools/rename_project.py --name 新中文名 --en NewEnglish --apply

    --name    新的中文显示名；**不给则中文名保持不变**
    --en      新的英文显示名（同时处理全大写变体）
    --prefix  新的大写环境变量前缀（替换 IMIS_），例如 MULTIVEST_
    --module  新的小写标识（替换线程名/logger/缓存名里的 imis）
    --apply   真正写入；不加则只预演

设计取舍
--------
· **默认预演**。改名是不可逆的机械操作，先看清楚要动哪些地方。
· **中文名是可选的**。项目常见的情况是"中文名不动、只换英文品牌"，
  所以 --name 不给就跳过中文替换，而不是强迫填一个。
· **不碰 .git / dist / 截图 / 二进制 / 本脚本自身**。
· 改完提示重新打包与重跑自检 —— 这两件事改名后必须做。
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 当前值（改名前的真相）
CUR_NAME = "智能多维投资系统"
CUR_EN_TITLE = "Intelligent Multi-dimensional Investment System"
CUR_EN_UPPER = "INTELLIGENT MULTI-DIMENSIONAL INVESTMENT"
CUR_PREFIX = "IMIS_"
CUR_MODULE = "imis"

SKIP_DIRS = {".git", "__pycache__", "node_modules", "build", "dist",
             ".venv", "venv", "_pkg_staging"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".ico", ".exe", ".pdf", ".woff", ".woff2",
               ".zip", ".pyc", ".pyd", ".so", ".dll"}
SKIP_PARTS = {"docs/界面截图"}


def iter_files():
    """遍历所有应处理的文本文件。

    跳过本脚本自身：它的常量正是要被替换的字符串，
    把自己改掉会让脚本失去作用（再跑一次就找不到旧名了）。
    """
    me = pathlib.Path(__file__).resolve()
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.resolve() == me:
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(part in p.parts for part in SKIP_DIRS):
            continue
        if any(s in rel for s in SKIP_PARTS):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        yield p, rel


def build_rules(args) -> list[tuple[str, str, str]]:
    rules: list[tuple[str, str, str]] = []
    if args.name:
        rules.append((re.escape(CUR_NAME), args.name,
                      f"中文名：{CUR_NAME} → {args.name}"))
    if args.en:
        # 全大写变体必须先替换，否则被标题式替换吃掉一部分
        rules.append((re.escape(CUR_EN_UPPER), args.en.upper(),
                      f"英文全大写：{CUR_EN_UPPER} → {args.en.upper()}"))
        rules.append((re.escape(CUR_EN_TITLE), args.en,
                      f"英文标题式：{CUR_EN_TITLE} → {args.en}"))
    if args.prefix:
        pfx = args.prefix if args.prefix.endswith("_") else args.prefix + "_"
        rules.append((re.escape(CUR_PREFIX), pfx,
                      f"环境变量：{CUR_PREFIX} → {pfx}"))
    if args.module:
        rules.append((rf"\b{re.escape(CUR_MODULE)}\b", args.module,
                      f"内部标识：{CUR_MODULE} → {args.module}"))
    return rules


def main() -> int:
    ap = argparse.ArgumentParser(description="项目改名工具")
    ap.add_argument("--name", default="",
                    help=f"新的中文显示名；不给则保持「{CUR_NAME}」不变")
    ap.add_argument("--en", default="",
                    help=f"新的英文显示名（替换「{CUR_EN_TITLE}」）")
    ap.add_argument("--prefix", default="",
                    help=f"新的大写环境变量前缀（替换 {CUR_PREFIX}）")
    ap.add_argument("--module", default="",
                    help=f"新的小写标识（替换 {CUR_MODULE}）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    args = ap.parse_args()

    rules = build_rules(args)
    if not rules:
        print("没有任何替换规则。至少给一个：--name / --en / --prefix / --module")
        return 2

    total, changed = 0, []
    for path, rel in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new, n = text, 0
        for pat, rep, _d in rules:
            new, k = re.subn(pat, rep, new)
            n += k
        if n:
            total += n
            changed.append((rel, n))
            if args.apply:
                path.write_text(new, encoding="utf-8")

    print()
    print("=" * 68)
    print(f"  {'已应用' if args.apply else '预演（未写入）'}")
    print("=" * 68)
    for _p, _r, desc in rules:
        print(f"  · {desc}")
    print()
    if not changed:
        print("  没有匹配到任何内容 —— 名字可能已经改过了。")
        return 0
    print(f"  共 {total} 处，涉及 {len(changed)} 个文件：")
    for rel, n in sorted(changed, key=lambda x: -x[1]):
        print(f"    {n:>4} 处  {rel}")
    print()
    if not args.apply:
        print("  这只是预演。确认无误后加 --apply 执行。")
    else:
        print("  已写入。接下来必须做：")
        print("    1. 重新打包：  python tools/build_desktop.py")
        print("    2. 重跑自检：  python tools/test_offline.py")
        print("  另外 frontend/sw.js 的缓存名已改，"
              "浏览器里若仍显示旧名，强制刷新一次（Ctrl+Shift+R）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
