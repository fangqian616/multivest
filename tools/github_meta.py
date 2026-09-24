#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置 GitHub 仓库的元信息（描述 / 主页 / topics）。

与 push_via_api.py 同源：本机 git 走 HTTPS 时曾因失效代理不可用，
但 REST API 一直可达，故仓库元信息也走 API 维护。

用法
----
    set GITHUB_TOKEN=ghp_xxx
    python tools/github_meta.py --repo fangqian616/multivest \
        --description "..." --topics a,b,c

    --repo          owner/name，可重复传入
    --description   仓库描述；省略则不改
    --homepage      主页地址；省略则不改
    --topics        逗号分隔的 topics；省略则不改（全量替换）

token 只从环境变量读取，不落盘、不进 git。
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import os
import sys

import requests

API = "https://api.github.com"


def session() -> requests.Session:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("缺少环境变量 GITHUB_TOKEN", file=sys.stderr)
        raise SystemExit(2)
    s = requests.Session()
    s.headers.update({
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "multivest-meta",
    })
    return s


def show(s: requests.Session, repo: str) -> None:
    r = s.get(f"{API}/repos/{repo}", timeout=30)
    if r.status_code != 200:
        print(f"  ❌ 读取失败 {repo}: {r.status_code} {r.text[:160]}")
        return
    d = r.json()
    print(f"  {d['full_name']}")
    print(f"    描述     : {d.get('description') or '(空)'}")
    print(f"    主页     : {d.get('homepage') or '(空)'}")
    print(f"    默认分支 : {d.get('default_branch')}")
    print(f"    公开     : {d.get('private') is False}")
    t = s.get(f"{API}/repos/{repo}/topics", timeout=30)
    print(f"    topics   : {', '.join(t.json().get('names', [])) or '(空)'}"
          if t.status_code == 200 else "    topics   : (读取失败)")


def patch(s: requests.Session, repo: str, body: dict) -> bool:
    if not body:
        return True
    r = s.patch(f"{API}/repos/{repo}", json=body, timeout=30)
    if r.status_code != 200:
        print(f"  ❌ 更新失败 {repo}: {r.status_code} {r.text[:200]}")
        return False
    return True


def put_topics(s: requests.Session, repo: str, topics: list[str]) -> bool:
    r = s.put(f"{API}/repos/{repo}/topics",
              json={"names": topics}, timeout=30)
    if r.status_code != 200:
        print(f"  ❌ topics 失败 {repo}: {r.status_code} {r.text[:200]}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", action="append", required=True)
    ap.add_argument("--description")
    ap.add_argument("--homepage")
    ap.add_argument("--topics", action="append",
                    help="逗号分隔，全量替换。可重复：第 i 个与第 i 个 --repo 配对；"
                         "只给一个则应用到所有仓库。")
    args = ap.parse_args()

    s = session()

    def topics_for(i: int) -> list[str] | None:
        if not args.topics:
            return None
        raw = args.topics[i] if i < len(args.topics) else args.topics[-1]
        return [t.strip() for t in raw.split(",") if t.strip()]

    ok = True
    for i, repo in enumerate(args.repo):
        topics = topics_for(i)
        print(f"── {repo} ──")
        before = {}
        r = s.get(f"{API}/repos/{repo}", timeout=30)
        if r.status_code == 200:
            before = r.json()
        print("  更新前：")
        show(s, repo)

        body: dict = {}
        if args.description is not None:
            body["description"] = args.description
        if args.homepage is not None:
            body["homepage"] = args.homepage
        if body:
            ok &= patch(s, repo, body)
        if topics is not None:
            ok &= put_topics(s, repo, topics)

        print("  更新后：")
        show(s, repo)
        print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
