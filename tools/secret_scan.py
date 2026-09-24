#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""穷尽式密钥扫描：git 全历史 + 工作区 + 发布包。

用途：确认仓库（尤其公开仓库）里不存在任何 API 密钥。
不依赖 git 的文本解码 —— 中文文件名会让 subprocess 的 text 模式抛
UnicodeDecodeError，因此全部按 bytes 处理。

用法：
    python tools/secret_scan.py
    python tools/secret_scan.py --token ghp_xxx     # 额外精确匹配特定令牌
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = [
    ("GitHub PAT", re.compile(rb"ghp_[A-Za-z0-9]{20,}")),
    ("GitHub 细粒度 PAT", re.compile(rb"github_pat_[A-Za-z0-9_]{20,}")),
    ("OpenAI/DeepSeek 风格密钥", re.compile(rb"sk-[A-Za-z0-9]{20,}")),
    ("AWS Access Key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("私钥文件", re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY")),
    ("带密码的连接串", re.compile(rb"://[^/\s:]{1,32}:[^/\s@]{6,}@")),
]

# 这些是误报来源：文档里解释"密钥长什么样"的说明文字、以及本脚本自身
ALLOW_HINT = (b"sk-xxx", b"sk-your", b"sk-\xe4\xbd\xa0", b"ghp_xxx",
              b"sk-...", b"ABCdef", b"your-key")


def _git(*args: bytes) -> bytes:
    return subprocess.run([b"git", *args], cwd=str(ROOT),
                          capture_output=True).stdout


def scan_bytes(data: bytes, where: str) -> list:
    out = []
    for name, rx in PATTERNS:
        for m in rx.finditer(data):
            frag = m.group(0)
            if any(h in frag for h in ALLOW_HINT):
                continue
            head = frag[:10].decode("ascii", "replace")
            out.append((where, name, f"{head}…（{len(frag)} 字符）"))
    return out


def scan_blobs(exact: bytes | None) -> tuple:
    """扫描所有分支与 tag 指向的全部 blob，包含已从工作区删除的历史文件。"""
    listing = _git(b"rev-list", b"--objects", b"--all").split(b"\n")
    shas, names = [], {}
    for line in listing:
        parts = line.split(b" ", 1)
        if len(parts) != 2:
            continue
        sha, path = parts
        shas.append(sha)
        names[sha] = path.decode("utf-8", "replace")

    # 用 --batch 一次性取回全部对象，避免逐条 fork
    proc = subprocess.run([b"git", b"cat-file", b"--batch"],
                          cwd=str(ROOT), input=b"\n".join(shas),
                          capture_output=True)
    blob = proc.stdout
    hits, count = [], 0
    pos = 0
    while pos < len(blob):
        nl = blob.find(b"\n", pos)
        if nl < 0:
            break
        header = blob[pos:nl].split(b" ")
        if len(header) < 3:
            break
        sha, kind, size = header[0], header[1], int(header[2])
        body = blob[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
        if kind != b"blob":
            continue
        count += 1
        label = names.get(sha, sha.decode())
        hits += scan_bytes(body, label)
        if exact:
            if exact in body:
                hits.append((label, "指定令牌精确命中", exact[:10].decode("ascii", "replace") + "…"))
    return count, hits


def scan_worktree(exact: bytes | None) -> list:
    skip = {".git", "__pycache__", "dist", "_lit", "node_modules"}
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(s in p.parts for s in skip):
            continue
        try:
            if p.stat().st_size > 40 * 1024 * 1024:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        rel = str(p.relative_to(ROOT))
        hits += scan_bytes(data, rel)
        if exact and exact in data:
            hits.append((rel, "指定令牌精确命中", exact[:10].decode("ascii", "replace") + "…"))
    return hits


def scan_zip(path: Path, exact: bytes | None) -> list:
    if not path.is_file():
        return []
    hits = []
    with zipfile.ZipFile(path) as z:
        for nm in z.namelist():
            try:
                data = z.read(nm)
            except Exception:  # noqa: BLE001
                continue
            hits += scan_bytes(data, f"{path.name}!{nm}")
            if exact and exact in data:
                hits.append((f"{path.name}!{nm}", "指定令牌精确命中", "…"))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=None,
                    help="额外精确匹配的令牌（不写入任何文件）")
    ap.add_argument("--zip", action="append", default=["智能多维投资系统-源码.zip"])
    args = ap.parse_args()
    exact = args.token.encode() if args.token else None

    print("=" * 68)
    print("【1】git 全历史 blob（所有分支 + tag，含已删除文件）")
    print("=" * 68)
    n, h1 = scan_blobs(exact)
    print(f"  扫描 {n} 个 blob")
    print("  ✅ 无命中" if not h1 else "  ❌ 命中：")
    for x in h1:
        print("     ", x)

    print()
    print("=" * 68)
    print("【2】工作区（含未跟踪与 gitignore 的文件）")
    print("=" * 68)
    h2 = scan_worktree(exact)
    if h2:
        print("  ⚠ 命中：")
        for x in h2:
            print("     ", x)
    else:
        print("  ✅ 无命中")

    print()
    print("=" * 68)
    print("【3】发布包")
    print("=" * 68)
    h3 = []
    for z in args.zip:
        zz = scan_zip(ROOT / z, exact)
        print(f"  {z}: " + ("✅ 无命中" if not zz else f"❌ {len(zz)} 处"))
        h3 += zz
    for x in h3:
        print("     ", x)

    print()
    print("=" * 68)
    print("【4】git 配置与提交信息")
    print("=" * 68)
    cfg = subprocess.run([b"git", b"config", b"--list"], cwd=str(ROOT),
                         capture_output=True).stdout
    msgs = _git(b"log", b"--all", b"--format=%B")
    bad_cfg = scan_bytes(cfg, "git config")
    bad_msg = scan_bytes(msgs, "commit message")
    if exact and (exact in cfg or exact in msgs):
        print("  ❌ 指定令牌出现在 config 或提交信息中")
    else:
        print("  ", "✅ 配置干净" if not bad_cfg else f"❌ {bad_cfg}")
        print("  ", "✅ 提交信息干净" if not bad_msg else f"❌ {bad_msg}")

    total = len(h1) + len(h2) + len(h3) + len(bad_cfg) + len(bad_msg)
    print()
    print("=" * 68)
    print(f"  结论：{'✅ 全部干净，未发现任何密钥' if total == 0 else f'❌ 共 {total} 处命中，需处理'}")
    print("=" * 68)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
