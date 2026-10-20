#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布修复后的 CPU 包为新 Release，并删除旧的 Release + git tag。

用法:
  python publish_new_release.py --token <PAT> \
      --asset "dist-pkg/RetroWeb-1.0-win64.7z" \
      --new-tag desktop-v1.0.1 --new-name "RetroWeb 1.0.1 (CPU, fixed)" \
      --old-tag desktop-v1.0.0

token 也可通过环境变量 GH_TOKEN 传入。
必须: PAT 具备 repo 权限（含 delete:packages 不需要，但 public_repo / repo 即可）。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

REPO = "yijiu2025/retro"
API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "retro-publish",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api(method: str, url: str, token: str, data=None, binary=False):
    headers = _headers(token)
    body = None
    if data is not None:
        if binary:
            body = data
            headers["Content-Type"] = "application/octet-stream"
        else:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            return r.status, (raw.decode("utf-8") if not binary else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get_release_by_tag(token: str, tag: str):
    status, body = _api("GET", f"{API}/repos/{REPO}/releases/tags/{tag}", token)
    if status == 200:
        return json.loads(body)
    return None


def create_release(token: str, tag: str, name: str, body_text: str):
    status, resp = _api(
        "POST",
        f"{API}/repos/{REPO}/releases",
        token,
        data={"tag_name": tag, "name": name, "body": body_text, "draft": False,
              "prerelease": False},
    )
    if status not in (200, 201):
        print(f"[ERROR] 创建 Release 失败: {status}\n{resp}")
        sys.exit(1)
    return json.loads(resp)


def upload_asset(token: str, release: dict, asset_path: str):
    name = os.path.basename(asset_path)
    upload_url = release["upload_url"].split("{", 1)[0]
    url = f"{upload_url}?name={urllib.parse.quote(name)}&label={urllib.parse.quote(name)}"
    with open(asset_path, "rb") as fh:
        data = fh.read()
    print(f"  上传 {name} ({len(data)/1e6:.1f} MB) ...")
    status, resp = _api("POST", url, token, data=data, binary=True)
    if status not in (200, 201):
        print(f"[ERROR] 上传失败: {status}\n{resp}")
        sys.exit(1)
    print(f"  上传完成: {json.loads(resp)['browser_download_url']}")


def delete_release(token: str, release: dict):
    rid = release["id"]
    status, resp = _api("DELETE", f"{API}/repos/{REPO}/releases/{rid}", token)
    if status not in (200, 204):
        print(f"[WARN] 删除旧 Release 失败: {status}\n{resp}")
        return False
    print(f"  已删除旧 Release (id {rid})")
    return True


def delete_git_tag(token: str, tag: str):
    # git push --delete 走 GCM 凭据，不需要 API token
    try:
        out = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)),
             "push", "origin", "--delete", tag],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0:
            print(f"  已删除旧 git tag {tag}")
        else:
            print(f"[WARN] 删除 git tag 失败: {out.stderr.strip()}")
    except Exception as e:
        print(f"[WARN] 删除 git tag 异常: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    ap.add_argument("--asset", required=True)
    ap.add_argument("--new-tag", default="desktop-v1.0.1")
    ap.add_argument("--new-name", default="RetroWeb 1.0.1 (CPU, fixed)")
    ap.add_argument("--old-tag", default="desktop-v1.0.0")
    ap.add_argument("--notes", default="修复：关闭窗口后模型服务进程树未退出、单实例锁导致无法重启的问题。")
    ap.add_argument("--skip-delete-old", action="store_true")
    args = ap.parse_args()

    if not args.token:
        print("[ERROR] 缺少 token（--token 或环境变量 GH_TOKEN）")
        sys.exit(1)
    if not os.path.isfile(args.asset):
        print(f"[ERROR] 找不到附件: {args.asset}")
        sys.exit(1)


    print(f"[1] 创建新 Release {args.new_tag} ...")
    rel = create_release(args.token, args.new_tag, args.new_name, args.notes)
    print(f"    新 Release 已建: {rel['html_url']}")

    print("[2] 上传附件 ...")
    upload_asset(args.token, rel, args.asset)

    if not args.skip_delete_old:
        print(f"[3] 删除旧 Release {args.old_tag} ...")
        old = get_release_by_tag(args.token, args.old_tag)
        if old:
            delete_release(args.token, old)
            delete_git_tag(args.token, args.old_tag)
        else:
            print(f"    未找到旧 Release {args.old_tag}，跳过")

    print("\n完成：新版本已发布，旧版本已删除。")


if __name__ == "__main__":
    main()
