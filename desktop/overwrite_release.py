#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""覆盖 GitHub Release 上的 CPU 分发件（删旧 asset + 传新 7z）。

用法：
    # 用环境变量里的 token（repo 权限的 PAT）跑：
    GH_TOKEN=ghp_xxx python overwrite_release.py --file dist-pkg.7z
    # 或显式传 token：
    python overwrite_release.py --token ghp_xxx --file dist-pkg.7z

说明：
    - 默认仓库 yijiu2025/retro，tag desktop-v1.0.0，asset 名 RetroWeb-1.0-win64.7z
    - 先按 tag 取 release，再删同名旧 asset，最后上传新文件（同一下载链接保持）
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "yijiu2025/retro"
TAG = "desktop-v1.0.0"
ASSET_NAME = "RetroWeb-1.0-win64.7z"


def _req(method: str, url: str, token: str, data=None, headers=None, binary=False):
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "retro-build",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if headers:
        h.update(headers)
    if isinstance(data, str):
        data = data.encode("utf-8")
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            if binary:
                return resp.read(), resp.headers
            return resp.read().decode("utf-8"), resp.headers
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:600]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN"))
    ap.add_argument("--file", default=None)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--asset", default=ASSET_NAME)
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("需要 GitHub token：传 --token 或设置环境变量 GH_TOKEN（需 repo 权限）")

    f = args.file
    if not f:
        for cand in ("dist-pkg.7z", args.asset):
            if os.path.isfile(cand):
                f = cand
                break
    if not f or not os.path.isfile(f):
        raise SystemExit(f"找不到要上传的 7z（用 --file 指定，当前目录无 dist-pkg.7z / {args.asset}）")

    # 1) 取 release
    body, _ = _req("GET", f"{API}/repos/{args.repo}/releases/tags/{args.tag}", args.token)
    rel = json.loads(body)
    rel_id = rel["id"]
    print(f"release: {rel.get('tag_name')} (id={rel_id})")

    # 2) 删旧 asset（同名）
    for a in rel.get("assets", []):
        if a["name"] == args.asset:
            _req("DELETE", f"{API}/repos/{args.repo}/releases/assets/{a['id']}", args.token)
            print(f"  已删除旧 asset: {a['name']} ({a['size']/1e6:.0f} MB)")

    # 3) 上传新文件
    size = os.path.getsize(f)
    with open(f, "rb") as fh:
        data = fh.read()
    url = f"{API}/repos/{args.repo}/releases/{rel_id}/assets?name={args.asset}"
    _req("POST", url, args.token, data=data,
         headers={"Content-Type": "application/octet-stream"}, binary=True)
    print(f"  上传完成: {args.asset} ({size/1e6:.0f} MB)")
    print(f"  下载页: {rel.get('html_url')}")


if __name__ == "__main__":
    main()
