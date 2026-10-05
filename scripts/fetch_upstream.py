# -*- coding: utf-8 -*-
"""拉取上游 RetroChimera 源码到 repo/ 并装成可编辑包。

背景
----
`repo/` 是 microsoft/retrochimera 的独立克隆，**不入本仓库**（见 .gitignore）。
换机器后必须先把上游源码拉下来并 `pip install -e .`，否则 `import retrochimera` 会失败。

用法
----
    python scripts/fetch_upstream.py                # 默认 v1.3.0
    python scripts/fetch_upstream.py --ref v1.3.0   # 指定 tag / 分支 / commit
    python scripts/fetch_upstream.py --no-install   # 只拉源码，不装包
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

UPSTREAM_URL = "https://github.com/microsoft/retrochimera.git"
DEFAULT_REF = "v1.3.0"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.join(_ROOT, "repo")


def _run(cmd: list[str], cwd: str | None = None) -> None:
    print(f"  $ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch upstream RetroChimera source")
    ap.add_argument("--ref", default=DEFAULT_REF, help=f"tag/branch/commit (default {DEFAULT_REF})")
    ap.add_argument("--no-install", action="store_true", help="skip `pip install -e .`")
    args = ap.parse_args()

    if os.path.isdir(os.path.join(_REPO_DIR, ".git")):
        print(f"[1/2] repo/ already exists, fetching ref {args.ref} ...")
        _run(["git", "fetch", "--all", "--tags"], cwd=_REPO_DIR)
        _run(["git", "checkout", args.ref], cwd=_REPO_DIR)
    else:
        print(f"[1/2] cloning {UPSTREAM_URL} (ref {args.ref}) ...")
        _run(["git", "clone", "--branch", args.ref, UPSTREAM_URL, _REPO_DIR])

    if args.no_install:
        print("[2/2] skipped install (--no-install)")
        return 0

    print("[2/2] installing retrochimera in editable mode ...")
    try:
        _run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=_REPO_DIR)
    except subprocess.CalledProcessError:
        print("\n[ERROR] `pip install -e .` failed.")
        print("        Usually a missing build dependency. Try:")
        print("          python -m pip install -e . --no-build-isolation")
        return 1

    print("\nDone. Verify with:  python -c \"import retrochimera; print(retrochimera.__file__)\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
