#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按注册表下载 + 解压模型 checkpoint（官方 figshare 源，**可断点续传**）。

为什么要有它
------------
模型表（`web/model_runtime.py` 的 `MODELS` / `FORWARD_MODELS`）里每一项都记了它的
figshare 文件 id，但**不记「下没下」**——文件在不在由 `models_catalog()` 现查。于是
「这个模型不能用」有两类原因，界面上长得一样、实际处理完全不同：

    unavailable  文件缺失  →  跑这个脚本
    idle         文件在、没加载  →  在界面点「加载」

从前只有文档里两行裸链接，用户得自己找目录、自己认 zip 名、断了只能从头再来
（USPTO-FULL 有 4.6 GB，figshare 到国内的速率经常只有几十 KB/s ⇒ 断一次就前功尽弃）。
本脚本把「下载 → 续传 → 解压 → 校验」串成一条命令。

用法
----
    python scripts/download_model.py --list                 # 看有哪些、下没下、多大
    python scripts/download_model.py uspto_full             # 下载并解压 USPTO-FULL
    python scripts/download_model.py forward_pistachio      # 下载正向模型（可行性过滤用）
    python scripts/download_model.py --all                  # 把缺的都补上
    python scripts/download_model.py uspto_full --download-only   # 只下 zip，不解压

断点续传：zip 留在 `models/` 下，中断后**再跑同一条命令**即可接着下（curl -C -）。
想重下就加 `--force`（会先删掉半截的 zip）。

🔴 解压目标：zip 内是**平铺**的（`models.json` / `smiles_transformer/` … 直接在根），
   所以解到 `<模型根>/<目录名>/`。少数打包方式会自带同名顶层目录，那种情况改解到
   `<模型根>/`，免得出现 `retrochimera_xxx/retrochimera_xxx/`。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # 仓库根
WEB = os.path.join(ROOT, "web")
if WEB not in sys.path:
    sys.path.insert(0, WEB)

import model_runtime  # noqa: E402


def _mb(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n else "?"


def registry() -> dict:
    """两张注册表合成一份：key -> {label, path, figshare_file, zip_bytes, kind}。"""
    out: dict = {}
    for key, info in model_runtime.MODELS.items():
        out[key] = {**info, "kind": "backward"}
    for key, info in model_runtime.FORWARD_MODELS.items():
        out[key] = {**info, "kind": "forward"}
    return out


def zip_path_for(info: dict) -> str:
    """zip 落在模型根目录下，名字就是模型目录名 + .zip（与本机已有的一致）。"""
    return os.path.join(model_runtime.models_root(), os.path.basename(info["path"]) + ".zip")


def is_present(info: dict) -> bool:
    path = info["path"]
    return os.path.isdir(path) and bool(os.listdir(path))


def verify(info: dict) -> tuple[bool, str]:
    """解压完的目录「像不像」一个能用的 checkpoint。

    只查特征文件，不加载模型（加载要几十秒 + 十几 GB）。
    """
    path = info["path"]
    if not os.path.isdir(path):
        return False, f"目录不存在: {path}"
    names = set(os.listdir(path))
    if info["kind"] == "backward":
        if "models.json" not in names:
            return False, f"缺 models.json（RetroChimeraModel 靠它找子模型）: {path}"
        return True, "OK（含 models.json）"
    ckpts = [n for n in names if n.endswith(".ckpt")]
    txts = [n for n in names if n.endswith(".txt")]
    if not ckpts:
        return False, f"缺 *.ckpt（正向模型权重）: {path}"
    if not txts:
        return False, f"缺 *.txt（vocab）: {path}"
    return True, f"OK（{ckpts[0]} + {txts[0]}）"


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def _curl_available() -> bool:
    return shutil.which("curl") is not None


def download_with_curl(url: str, dest: str) -> None:
    """curl -C - 续传。--retry-all-errors 让「连接被重置」这类也能重试。

    🔴 不加 `--noproxy '*'`：本机可能确实需要走代理才能出海，交给 curl 按环境变量
       自己决定即可。（本项目内部访问 127.0.0.1 的地方才必须绕代理，那是另一回事。）
    """
    cmd = [
        "curl", "-L", "-C", "-", "--retry", "8", "--retry-delay", "5",
        "--retry-all-errors", "-o", dest, url,
    ]
    print("$ " + " ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"curl 退出码 {rc} —— 重跑同一条命令即可续传（zip 已留在 {dest}）")


def download_with_urllib(url: str, dest: str) -> None:
    """没有 curl 时的兜底：urllib + Range 续传（分段打印进度）。"""
    import urllib.request

    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0) + have
        mode = "ab" if have and resp.status == 206 else "wb"
        if mode == "wb":
            have = 0
        done = have
        with open(dest, mode) as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 / total
                    print(f"\r  {pct:5.1f}%  {_mb(done)} / {_mb(total)}", end="", flush=True)
    print()


def download(info: dict, *, force: bool = False) -> str:
    dest = zip_path_for(info)
    url = f"https://ndownloader.figshare.com/files/{info['figshare_file']}"

    # 🔴 判「下完了」只看文件大小对不对齐：figshare 直链**不给 Content-Length**
    #    （它先回 302，真正的长度在 S3 那一段），所以没法靠 HTTP 头判断。
    #    checkpoint 是固定发布物，比对 zip_bytes 是可靠且零成本的判据。
    want = int(info.get("zip_bytes") or 0)
    if force and os.path.exists(dest):
        print(f"  --force：删掉已存在的 {dest}")
        os.remove(dest)
    if os.path.exists(dest):
        got = os.path.getsize(dest)
        if want and got >= want:
            print(f"  zip 已完整（{_mb(got)}），跳过下载：{dest}")
            return dest
        print(f"  发现半截 zip（{_mb(got)} / {_mb(want)}），续传……")
    else:
        print(f"  开始下载 {url}\n  → {dest}（约 {_mb(want)}）")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if _curl_available():
        download_with_curl(url, dest)
    else:
        download_with_urllib(url, dest)

    got = os.path.getsize(dest)
    if want and got != want:
        raise SystemExit(
            f"下载不完整：{_mb(got)} / 期望 {_mb(want)}。重跑同一条命令续传。"
        )
    print(f"  ✓ 下载完成（{_mb(got)}）")
    return dest


# --------------------------------------------------------------------------
# 解压
# --------------------------------------------------------------------------
def extract(zip_file: str, info: dict) -> str:
    dest_dir = info["path"]
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_file) as zf:
        names = [n for n in zf.namelist() if not n.startswith("__MACOSX")]
        tops = {n.split("/")[0] for n in names if n.strip("/")}
        self_wrapped = len(tops) == 1 and next(iter(tops)) == os.path.basename(dest_dir)
        target = model_runtime.models_root() if self_wrapped else dest_dir
        print(f"  解压 → {target}（{len(names)} 项）……")
        zf.extractall(target)
    return dest_dir


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cmd_list() -> None:
    reg = registry()
    root = model_runtime.models_root()
    print(f"模型根目录：{root}\n")
    print(f"{'key':22} {'种类':10} {'文件':6} {'解压后':10} {'zip':9} 说明")
    print("-" * 96)
    for key, info in reg.items():
        ok, why = verify(info) if is_present(info) else (False, "未下载")
        zf = zip_path_for(info)
        zsize = _mb(os.path.getsize(zf)) if os.path.exists(zf) else "—"
        size = _mb(model_runtime.dir_size_bytes(info["path"])) if is_present(info) else "—"
        print(
            f"{key:22} {info['kind']:10} {'✓' if ok else '✗':6} {size:10} {zsize:9} "
            f"{info['label']}｜{why}"
        )
    print("\n下载：python scripts/download_model.py <key>     （--all 把缺的都补上）")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="按注册表下载 + 解压模型 checkpoint（可断点续传）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python scripts/download_model.py --list",
    )
    ap.add_argument("models", nargs="*", help="要下载的模型 key（见 --list）；可写多个")
    ap.add_argument("--list", action="store_true", help="只列出模型与下载状态")
    ap.add_argument("--all", action="store_true", help="下载所有**缺失**的模型")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载（先删半截 zip）")
    ap.add_argument("--download-only", action="store_true", help="只下 zip，不解压")
    ap.add_argument("--no-keep-zip", action="store_true", help="解压成功后删掉 zip（省空间）")
    args = ap.parse_args()

    reg = registry()

    if args.list or not (args.models or args.all):
        cmd_list()
        if not (args.models or args.all):
            return

    keys = list(args.models)
    if args.all:
        keys += [k for k, info in reg.items() if not is_present(info)]

    # 去重保序
    seen: set = set()
    keys = [k for k in keys if not (k in seen or seen.add(k))]
    if not keys:
        print("没有要下载的模型（都齐了）。")
        return

    unknown = [k for k in keys if k not in reg]
    if unknown:
        raise SystemExit(
            f"未知模型: {', '.join(unknown)}\n可用: {', '.join(reg)}（--list 看详情）"
        )

    failed: list = []
    for key in keys:
        info = reg[key]
        print(f"\n=== {key}（{info['label']}）===")
        if is_present(info) and not args.force:
            ok, why = verify(info)
            print(f"  已存在（{why}），跳过。要重下加 --force。")
            if not ok:
                failed.append((key, why))
            continue
        try:
            zf = download(info, force=args.force)
            if args.download_only:
                print(f"  --download-only：只下载，未解压。解压：去掉该参数重跑")
                continue
            extract(zf, info)
            ok, why = verify(info)
            print(f"  {'✓' if ok else '✗'} 校验：{why}")
            if not ok:
                failed.append((key, why))
            elif args.no_keep_zip:
                os.remove(zf)
                print(f"  已删除 zip（--no-keep-zip）：{zf}")
        except SystemExit as e:
            print(f"  ✗ {e}")
            failed.append((key, str(e)))
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {type(e).__name__}: {e}")
            failed.append((key, f"{type(e).__name__}: {e}"))

    print()
    if failed:
        print("以下模型未就绪：")
        for key, why in failed:
            print(f"  ✗ {key}：{why}")
        sys.exit(1)
    print("全部就绪。到界面上点「加载」即可使用（正向模型在「拆分约束 → 可行性过滤」里加载）。")


if __name__ == "__main__":
    main()
