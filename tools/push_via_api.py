#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 GitHub API 推送仓库 —— 当 `git push` 走不通时的替代路径。

为什么需要它
------------
本机 git 全局配置里曾有 `http.proxy=http://127.0.0.1:10809`，指向一个未运行的
代理。`git push` 会走这个代理并在约 2 秒后报
「Failed to connect to github.com:443 over proxy 127.0.0.1」——
错误信息里出现的是 github.com，很容易被误诊成 DNS 或上游故障。

**先在命令行里试一次直连**：

    git -c http.proxy= -c https.proxy= push origin main

能通就说明问题只在代理配置上（全局 proxy 配置已移除）。只有直连确实不通时，
才用本脚本兜底 —— 它走 api.github.com，与 git 的传输路径无关。

Git Data API 正好能完整地建一棵树并提交：
    blobs（每个文件一个） → tree → commit → 更新 ref

这样推出来的仓库与工作区内容一致，且不修改任何系统设置；但**产生的提交与
本地历史不同源** —— API 提交的是工作区原始字节，而本地 git 在
`core.autocrlf=true` 下按 LF 归一化后入账。两边各形成一条时间线，
下一次 `git push` 会被判定为非快进（需要先合并或强推）。故本脚本只作最后手段。

用法
----
    python tools/push_via_api.py --repo fangqian616/multivest --token <TOKEN>

    --repo   owner/name
    --token  Personal Access Token（需要 repo 权限）
    --branch 目标分支，默认 main
    --message 提交信息；不给则用默认
    --dry-run 只统计，不调用写接口

安全
----
token 只出现在命令行参数里，不写入任何文件、不落盘、不进 git config。
用完后请立即吊销该 token。
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import base64
import json
import pathlib
import subprocess
import sys
import time
from typing import Any

import requests

API = "https://api.github.com"
ROOT = pathlib.Path(__file__).resolve().parents[1]

DEFAULT_MESSAGE = """sync: 通过 Git Data API 同步工作区

由 tools/push_via_api.py 生成。内容与本地工作区一致，但提交对象由
GitHub API 建立，与本地 git 历史不同源 —— 详见该脚本开头的说明。
若非必要，请改用 `git -c http.proxy= -c https.proxy= push origin main`。
"""


def tracked_files() -> list[str]:
    """取 git 跟踪的文件清单。

    必须关掉 quotepath，否则中文名会被转义成 \\345\\234... 而找不到文件。
    """
    subprocess.run(["git", "config", "core.quotepath", "false"], cwd=ROOT, check=False)
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    return [f for f in out.stdout.splitlines() if f.strip()]


def local_blob_sha(path: pathlib.Path) -> str:
    """算本地文件的 git blob SHA。

    这是增量推送的关键：git 的内容寻址意味着**同一个文件永远得到同一个 SHA**，
    所以把本地的 blob SHA 和远端 tree 里的比对，就能精确知道哪些文件变了。

    **必须加 `--no-filters`。** 本机 `core.autocrlf=true`，默认的
    `git hash-object` 会先把 CRLF 归一化成 LF 再算 SHA，
    而本脚本推送的是**原始字节**（base64 直传）—— 两边算的不是同一个东西，
    于是每个文件都会被判定为"已变化"，增量永远退化成全量。
    实测：同一文件默认 hash 406fa58…、--no-filters hash 739f3f3…，
    后者才与「按原始字节手算 sha1」一致。
    """
    out = subprocess.run(["git", "hash-object", "--no-filters", str(path)],
                         cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--token", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s = requests.Session()
    s.headers.update({
        "Authorization": f"token {args.token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "multivest-push",
    })

    files = tracked_files()
    print(f"跟踪文件 {len(files)} 个")

    # ── 0. 空仓库需要先初始化（GitHub 不允许在空仓库里建 blob，返回 409）──
    probe = s.get(f"{API}/repos/{args.repo}/contents/.init", timeout=30)
    if probe.status_code == 404:
        print("初始化空仓库 …")
        r = s.put(f"{API}/repos/{args.repo}/contents/.init",
                  json={"message": "chore: 初始化仓库",
                        "content": base64.b64encode(b"init\n").decode("ascii")},
                  timeout=60)
        if r.status_code not in (200, 201):
            print(f"  ❌ 初始化失败: {r.status_code} {r.text[:200]}")
            return 1
        print("  已初始化")

    # ── 1. 取远端现状，算出真正需要传的文件 ─────────────────────────────
    ref = s.get(f"{API}/repos/{args.repo}/git/ref/heads/{args.branch}", timeout=30)
    parent_sha = ref.json()["object"]["sha"] if ref.status_code == 200 else None
    remote: dict[str, str] = {}
    if parent_sha:
        cm = s.get(f"{API}/repos/{args.repo}/git/commits/{parent_sha}", timeout=30).json()
        base_tree = cm["tree"]["sha"]
        tr = s.get(f"{API}/repos/{args.repo}/git/trees/{base_tree}?recursive=1",
                   timeout=60).json()
        remote = {t["path"]: t["sha"] for t in tr.get("tree", []) if t["type"] == "blob"}
    else:
        base_tree = None

    changed, deleted = [], []
    for rel in files:
        p = ROOT / rel
        if not p.is_file():
            continue
        if remote.get(rel) != local_blob_sha(p):
            changed.append(rel)
    for rel in remote:
        if rel not in files:
            deleted.append(rel)

    print(f"  远端已有 {len(remote)} 个 blob，需上传 {len(changed)} 个"
          + (f"，删除 {len(deleted)} 个" if deleted else ""))

    if args.dry_run:
        for rel in changed:
            print(f"    + {rel}")
        for rel in deleted:
            print(f"    - {rel}")
        print("预演结束，未调用写接口")
        return 0

    if not changed and not deleted:
        print("  内容无变化，无需提交")
        return 0

    # ── 2. 只给变化的文件建 blob ─────────────────────────────────────────
    t0 = time.time()
    entries: list[dict[str, Any]] = []
    for i, rel in enumerate(changed, 1):
        p = ROOT / rel
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        r = s.post(f"{API}/repos/{args.repo}/git/blobs",
                   json={"content": data, "encoding": "base64"}, timeout=60)
        if r.status_code not in (200, 201):
            print(f"  ❌ blob 失败 {rel}: {r.status_code} {r.text[:160]}")
            return 1
        entries.append({"path": rel, "mode": "100755" if p.suffix == ".bat" else "100644",
                        "type": "blob", "sha": r.json()["sha"]})
        if i % 20 == 0 or i == len(changed):
            print(f"  已上传 {i}/{len(changed)}  ({time.time()-t0:.0f}s)")
    # 删除：tree 条目里 sha 置 null
    for rel in deleted:
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})

    # ── 3. 建 tree（带 base_tree，未变的文件由 GitHub 复用）──────────────
    print("创建 tree …")
    body: dict[str, Any] = {"tree": entries}
    if base_tree:
        body["base_tree"] = base_tree
    r = s.post(f"{API}/repos/{args.repo}/git/trees", json=body, timeout=90)
    if r.status_code not in (200, 201):
        print(f"  ❌ tree 失败: {r.status_code} {r.text[:200]}")
        return 1
    tree_sha = r.json()["sha"]

    # 3. 建 commit（以初始化提交为父；tree 是完整重建的，不含占位文件）
    print("创建 commit …")
    payload: dict[str, Any] = {"message": args.message, "tree": tree_sha}
    if parent_sha:
        payload["parents"] = [parent_sha]
    r = s.post(f"{API}/repos/{args.repo}/git/commits", json=payload, timeout=60)
    if r.status_code not in (200, 201):
        print(f"  ❌ commit 失败: {r.status_code} {r.text[:200]}")
        return 1
    commit_sha = r.json()["sha"]

    # 4. 把分支指向新提交（force：不要那个只含占位文件的初始化提交作为分支头）
    print(f"指向 refs/heads/{args.branch} …")
    r = s.patch(f"{API}/repos/{args.repo}/git/refs/heads/{args.branch}",
                json={"sha": commit_sha, "force": True}, timeout=60)
    if r.status_code not in (200, 201):
        print(f"  ❌ ref 更新失败: {r.status_code} {r.text[:200]}")
        return 1

    repo = s.get(f"{API}/repos/{args.repo}", timeout=30).json()
    print()
    print("=" * 62)
    print("  ✅ 推送完成")
    print("=" * 62)
    print(f"  仓库  : {repo.get('html_url')}")
    print(f"  分支  : {args.branch}")
    print(f"  提交  : {commit_sha[:12]}")
    print(f"  文件  : {len(changed)} 个变更"
          + (f"，{len(deleted)} 个删除" if deleted else ""))
    print(f"  耗时  : {time.time()-t0:.0f}s")
    print()
    print("  ⚠ 现在就去吊销你刚才发的那个 token：")
    print("     https://github.com/settings/tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
