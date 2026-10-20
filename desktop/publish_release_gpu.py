#!/usr/bin/env python3
"""发布 GPU(cu121) 版 GitHub Release。

token 从本机 Git Credential Manager 取出（password= 格式），不落盘、不打印明文。
与 publish_release_now.py 同机制，只是 tag / 资产名 / 文案不同，且不动旧 Release。
"""
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

REPO = "yijiu2025/retro"
GCM = r"C:/Users/22701/.workbuddy/binaries/PortableGit/versions/1.3.0/mingw64/bin/git-credential-manager.exe"
SEVEN_Z = r"F:/retrochimera/desktop/dist-pkg-gpu.7z"
ASSET_NAME = "RetroWeb-1.0.1-gpu-win64.7z"
NEW_TAG = "desktop-v1.0.1-gpu"
NEW_NAME = "RetroWeb 1.0.1 (GPU, cu121)"
NEW_BODY = (
    "GPU 加速版（CUDA 12.1 / cu121，NVIDIA 显卡）。在 CPU 版基础上已内置约 2GB 的 "
    "cu121 PyTorch 加速包，软件内「模型设置」页把计算后端切到 GPU 即可启用；无显卡时 "
    "自动回退 CPU（compute=auto）。\n\n"
    "CPU 版（无显卡机器用）：见 Release desktop-v1.0.1。\n\n"
    "解压到**纯英文路径**后双击 RetroWeb.exe 即可使用。"
)
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"


def get_token():
    inp = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run([GCM, "get"], input=inp, capture_output=True, text=True, timeout=40).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[9:].strip()
    raise SystemExit("无法从 GCM 取得 token: " + repr(out[:80]))


def gh(method, url, token, data=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "retro-publisher", "X-GitHub-Api-Version": "2022-11-28"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode("utf-8", "replace")}


def main():
    token = get_token()
    print(f"[1] token 取得（长度 {len(token)}），校验中…")
    st, info = gh("GET", f"{API}/user", token)
    if st != 200:
        print("  token 校验失败：", info.get("_error", "")[:300])
        sys.exit(1)
    print(f"    登录账号：{info.get('login')}")

    print(f"[2] 创建 Release {NEW_TAG} …")
    st, info = gh("POST", f"{API}/repos/{REPO}/releases", token,
                  {"tag_name": NEW_TAG, "name": NEW_NAME, "body": NEW_BODY,
                   "draft": False, "prerelease": False})
    if st not in (200, 201):
        msg = info.get("_error", "")
        if '"already_exists"' in msg:
            print("    tag 已存在，获取已有 release …")
            st, info = gh("GET", f"{API}/repos/{REPO}/releases/tags/{NEW_TAG}", token)
        else:
            print("    创建失败：", msg[:400])
            sys.exit(1)
    rel_id = info.get("id")
    print(f"    Release id={rel_id}")

    print(f"[3] 上传 {ASSET_NAME}（{os.path.getsize(SEVEN_Z)/1e6:.0f} MB）…")
    if not os.path.isfile(SEVEN_Z):
        print("    7z 不存在：", SEVEN_Z)
        sys.exit(1)
    up_url = f"{UPLOAD}/repos/{REPO}/releases/{rel_id}/assets?name={ASSET_NAME}"
    curl = subprocess.run(["curl", "-sS", "-w", "\nHTTP_STATUS:%{http_code}",
                           "-H", f"Authorization: Bearer {token}",
                           "-H", "Content-Type: application/octet-stream",
                           "--data-binary", f"@{SEVEN_Z}", up_url],
                          capture_output=True, text=True, timeout=600)
    out = curl.stdout
    body_txt, _, code = out.rpartition("\nHTTP_STATUS:")
    code = code.strip()
    print("    curl HTTP", code)
    if code not in ("200", "201"):
        print("    上传失败：", body_txt[:400])
        sys.exit(1)
    try:
        j = json.loads(body_txt)
        print(f"    资产已上传：{j.get('name')} ({j.get('size')} bytes)")
    except Exception:
        print("    资产已上传（响应无法解析，但 HTTP 200）")

    print("\n完成 ✅  https://github.com/{}/releases/tag/{}".format(REPO, NEW_TAG))


if __name__ == "__main__":
    main()
