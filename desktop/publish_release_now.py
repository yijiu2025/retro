#!/usr/bin/env python3
"""发布修复版 GitHub Release（desktop-v1.0.1）并清理旧版 desktop-v1.0.0。

token 从本机 Git Credential Manager 取出，不落盘、不打印明文。
"""
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

REPO = "yijiu2025/retro"
GCM = r"C:/Users/22701/.workbuddy/binaries/PortableGit/versions/1.3.0/mingw64/bin/git-credential-manager.exe"
SEVEN_Z = r"F:/retrochimera/desktop/dist-pkg.7z"
ASSET_NAME = "RetroWeb-1.0.1-win64.7z"
NEW_TAG = "desktop-v1.0.1"
OLD_TAG = "desktop-v1.0.0"
NEW_NAME = "RetroWeb 1.0.1 (CPU, fixed)"
NEW_BODY = (
    "修复：关闭窗口后模型服务进程树（含规则子进程）未退出、单实例锁仍被占用，"
    "导致无法二次启动的问题。\n\n"
    "CPU 版（便携 Python 3.9 + torch CPU）。解压到**纯英文路径**后双击 RetroWeb.exe 即可使用。"
)

API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"


def get_token() -> str:
    inp = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run([GCM, "get"], input=inp, capture_output=True, text=True, timeout=40).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[9:].strip()
    raise SystemExit("无法从 GCM 取得 token")


def gh(method: str, url: str, token: str, data=None, is_upload=False):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "retro-publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = data  # raw bytes for upload
            headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        return e.code, {"_error": detail}


def main():
    token = get_token()
    print(f"[1] token 取得（长度 {len(token)}），校验中…")

    # 校验 + 看 scope
    st, info = gh("GET", f"{API}/user", token)
    if st != 200:
        print("  token 校验失败：", info.get("_error", "")[:300])
        sys.exit(1)
    print(f"    登录账号：{info.get('login')}")

    # 看 scope（从创建 release 试一次更准，这里先打个招呼）
    # 2) 建新 release
    print(f"[2] 创建 Release {NEW_TAG} …")
    st, info = gh("POST", f"{API}/repos/{REPO}/releases", token, {
        "tag_name": NEW_TAG,
        "name": NEW_NAME,
        "body": NEW_BODY,
        "draft": False,
        "prerelease": False,
    })
    if st not in (200, 201):
        msg = info.get("_error", "")
        if '"already_exists"' in msg:
            print("    tag 已存在，尝试获取已有 release …")
            st, info = gh("GET", f"{API}/repos/{REPO}/releases/tags/{NEW_TAG}", token)
        else:
            print("    创建失败：", msg[:400])
            sys.exit(1)
    rel_id = info.get("id")
    print(f"    Release id={rel_id}")

    # 3) 上传 7z（大文件用 curl 流式）
    print(f"[3] 上传 {ASSET_NAME}（{os.path.getsize(SEVEN_Z)/1e6:.0f} MB）…")
    if not os.path.isfile(SEVEN_Z):
        print("    7z 不存在：", SEVEN_Z)
        sys.exit(1)
    up_url = f"{UPLOAD}/repos/{REPO}/releases/{rel_id}/assets?name={ASSET_NAME}"
    curl = subprocess.run(
        ["curl", "-sS", "-w", "\nHTTP_STATUS:%{http_code}",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/octet-stream",
         "--data-binary", f"@{SEVEN_Z}", up_url],
        capture_output=True, text=True, timeout=600,
    )
    out = curl.stdout
    if "\nHTTP_STATUS:" in out:
        body_txt, _, code = out.rpartition("\nHTTP_STATUS:")
        code = code.strip()
    else:
        body_txt, code = out, "?"
    print("    curl 返回 HTTP", code)
    if code not in ("200", "201"):
        print("    上传失败：", body_txt[:400])
        sys.exit(1)
    try:
        j = json.loads(body_txt)
        print(f"    资产已上传：{j.get('name')} ({j.get('size')} bytes)")
    except Exception:
        print("    资产已上传（无法解析响应，但 HTTP 200）")

    # 4) 删旧 release + tag
    print(f"[4] 清理旧 Release {OLD_TAG} …")
    st, info = gh("GET", f"{API}/repos/{REPO}/releases/tags/{OLD_TAG}", token)
    if st == 200 and info.get("id"):
        old_id = info["id"]
        st2, _ = gh("DELETE", f"{API}/repos/{REPO}/releases/{old_id}", token)
        print(f"    删除旧 release id={old_id} -> HTTP {st2}")
    else:
        print(f"    旧 release 不存在或已删除（{st}），跳过")

    # 删旧 tag
    st, _ = gh("DELETE", f"{API}/repos/{REPO}/git/refs/tags/{OLD_TAG}", token)
    print(f"    删除旧 tag {OLD_TAG} -> HTTP {st}")

    print("\n完成 ✅")
    print(f"  https://github.com/{REPO}/releases/tag/{NEW_TAG}")


if __name__ == "__main__":
    main()
