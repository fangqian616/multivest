#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布 GitHub Release 并上传产物（exe / 源码包）。

用法
----
    set GITHUB_TOKEN=ghp_xxx
    python tools/make_release.py --repo fangqian616/multivest --tag v1.0.0 \
        --name "v1.0.0 · 首个公开版本" --notes-file RELEASE.md \
        --asset dist/智能多维投资系统.exe --asset 智能多维投资系统-源码.zip

    --repo         owner/name
    --tag          标签名；已存在则复用该 release（不重复建）
    --name         标题，默认与 tag 相同
    --notes        正文（Markdown）
    --notes-file   从文件读正文（与 --notes 二选一）
    --asset        要上传的文件，可重复
    --draft        建为草稿
    --prerelease   标记为预发布
    --replace      同名资产已存在时先删再传

说明
----
上传走 uploads.github.com；若该域名不可达会明确报错而不是静默失败。
大文件按流式上传（不是 base64），44 MB 的 exe 实测约 1~3 分钟。
token 只从环境变量读取。
"""
from __future__ import annotations

import console  # noqa: F401  —— 见 tools/console.py：GBK 控制台下 emoji 不再中断脚本
import argparse
import os
import sys
import urllib.parse
from pathlib import Path

import requests

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def session() -> requests.Session:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("缺少环境变量 GITHUB_TOKEN", file=sys.stderr)
        raise SystemExit(2)
    s = requests.Session()
    s.headers.update({
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "multivest-release",
    })
    return s


def get_or_create_release(s: requests.Session, repo: str, args) -> dict | None:
    r = s.get(f"{API}/repos/{repo}/releases/tags/{args.tag}", timeout=30)
    if r.status_code == 200:
        rel = r.json()
        print(f"  复用已有 release {args.tag} (id={rel['id']})")
        return rel
    if r.status_code not in (404,):
        print(f"  ❌ 查询 release 失败: {r.status_code} {r.text[:200]}")
        return None

    body = {
        "tag_name": args.tag,
        "name": args.name or args.tag,
        "draft": args.draft,
        "prerelease": args.prerelease,
    }
    if args.notes_file:
        body["body"] = Path(args.notes_file).read_text(encoding="utf-8")
    elif args.notes:
        body["body"] = args.notes

    r = s.post(f"{API}/repos/{repo}/releases", json=body, timeout=60)
    if r.status_code not in (200, 201):
        print(f"  ❌ 创建 release 失败: {r.status_code} {r.text[:300]}")
        return None
    rel = r.json()
    print(f"  已创建 release {args.tag} (id={rel['id']})")
    return rel


def existing_assets(s: requests.Session, repo: str, rel: dict) -> dict:
    r = s.get(f"{API}/repos/{repo}/releases/{rel['id']}/assets", timeout=30)
    return {a["name"]: a for a in r.json()} if r.status_code == 200 else {}


def upload(s: requests.Session, repo: str, rel: dict, path: Path,
           name: str, replace: bool, have: dict) -> bool:
    if name in have:
        if not replace:
            print(f"  ⏭  {name} 已存在，跳过")
            return True
        aid = have[name]["id"]
        d = s.delete(f"{API}/repos/{repo}/releases/assets/{aid}", timeout=30)
        if d.status_code not in (204, 200):
            print(f"  ❌ 删除旧资产失败 {name}: {d.status_code}")
            return False
        print(f"  已删除旧资产 {name}")

    # 资产名走 query string，并**自己按 UTF-8 百分号编码**。
    # 交给 requests 的 params 传中文名时，GitHub 侧会拿到一个丢掉了非 ASCII
    # 字符的名字：实测「智能多维投资系统.exe」被存成 default.exe、
    # 「智能多维投资系统-源码.zip」被存成 -.zip，且**不报错**。
    # 因此这里除了自行编码，还在拿到响应后回校验名字是否被改动。
    q = urllib.parse.quote(name, safe="")
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"  ⬆ 上传 {name}（{size_mb:.1f} MB，源 {path.name}）…")
    with path.open("rb") as fh:
        r = requests.post(
            f"{UPLOADS}/repos/{repo}/releases/{rel['id']}/assets?name={q}",
            headers={
                "Authorization": s.headers["Authorization"],
                "Content-Type": "application/octet-stream",
                "User-Agent": "multivest-release",
            },
            data=fh,
            timeout=(30, 1800),
        )
    if r.status_code not in (200, 201):
        print(f"  ❌ 上传失败 {name}: {r.status_code} {r.text[:300]}")
        return False
    a = r.json()
    if a.get("name") != name:
        print(f"  ❌ 资产名被服务端改动：请求 {name}，实际 {a.get('name')}")
        return False
    print(f"     ✅ {a['browser_download_url']}"
          f"（{a['size'] / 1024 / 1024:.1f} MB）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--name")
    ap.add_argument("--notes")
    ap.add_argument("--notes-file")
    ap.add_argument("--asset", action="append", default=[],
                    help="要上传的文件，可重复。写法 `路径` 或 `路径=资产名`；"
                         "资产名建议用 ASCII，见 upload() 里的说明。")
    ap.add_argument("--drop", action="append", default=[],
                    help="按名字删除该 release 上已有的资产，可重复。")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--replace", action="store_true")
    args = ap.parse_args()

    s = session()
    print(f"── {args.repo} @ {args.tag} ──")
    rel = get_or_create_release(s, args.repo, args)
    if rel is None:
        return 1

    have = existing_assets(s, args.repo, rel)
    ok = True

    for name in args.drop:
        if name not in have:
            print(f"  ⏭  {name} 不存在，无需删除")
            continue
        d = s.delete(f"{API}/repos/{args.repo}/releases/assets/{have[name]['id']}",
                     timeout=30)
        if d.status_code in (200, 204):
            print(f"  已删除 {name}")
            have.pop(name, None)
        else:
            print(f"  ❌ 删除失败 {name}: {d.status_code}")
            ok = False

    for spec in args.asset:
        raw, _, alias = spec.partition("=")
        p = Path(raw)
        if not p.is_file():
            print(f"  ❌ 文件不存在：{p}")
            ok = False
            continue
        ok &= upload(s, args.repo, rel, p, alias or p.name,
                     args.replace, have)
    print()
    print(f"  页面：{rel['html_url']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
