#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 core/ 的 vendored 副本与上游是否一致 —— 专治「漏文件」。

背景
----
`core/` 是上游 microsoft/retrochimera 的源码副本（vendored，见 core/VENDORED.md）。
历史上栽过一次：`git add` 时 `.gitignore` 里的 `models/`（本意排除 9GB 权重）
是**无锚定模式**，连带把 `core/retrochimera/models/` 整个源码包排掉了 ——
而它在 `retrochimera/__init__ → inference → models` 的导入链上，
结果 clone 下来的仓库 `import retrochimera` 直接 ImportError，
**本机却完全正常**（retrochimera 是 `pip install -e .` 装的，缺的文件被本机副本补上了）。

这个脚本把当时的手工审计固化下来，让同步上游后能一条命令查出：
    1. 缺失   —— 上游有、本仓没有
    2. 未跟踪 —— 本地存在，但没进 git（正是上面那个坑的表现）
    3. 不一致 —— 内容与上游不同（换行符差异会自动忽略）

用法
----
    python scripts/verify_vendored.py                 # 校验 v1.3.0
    python scripts/verify_vendored.py --ref v1.3.1    # 校验别的 tag
    python scripts/verify_vendored.py --ref main      # 校验上游主分支

退出码：0 = 全部一致（或差异都在白名单里）；1 = 有问题。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# 🔴 导入顺序有讲究：**必须先修 SSL，再 import urllib.request**。
#    conda 环境未激活时 _ssl 的 DLL 找不到，urllib 会报
#    "URLError: unknown url type: https" —— 看着像网络问题，其实是 DLL。
#    （同 bootstrap/envfix.py 修的坑；scripts/download_model.py 也是靠
#    先 import model_runtime 间接修好的。）
#    所以 urllib 放在函数内延迟导入，不在模块顶层。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
try:
    from bootstrap import envfix  # noqa: F401
except Exception:  # noqa: BLE001 - 非 conda 环境或不在仓库内，静默跳过
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(ROOT, "core")

UPSTREAM = "microsoft/retrochimera"

#: 同步上游时**刻意**会不同的地方（见 core/VENDORED.md「本地化改动」表）。
#: 这里只列「内容确实不同」的；*.zh-CN.md 是本仓新增，不参与比对。
EXPECTED_DIFF = {
    "pyproject.toml",
}

#: 上游的 CI / 开发配置，不该搬进本仓：
#:   - release.yml 会往 PyPI 发布并打 tag，搬过来会误发布、误打标签
#:   - ci.yml / dependabot.yml / pre-commit 引用的是上游仓库地址，跑不对目标
EXPECTED_ABSENT = {
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".pre-commit-config.yaml",
    # 本仓自写文件，不是上游的
    "VENDORED.md",
}

#: 只比对源码与文档；权重/二进制不在 vendored 范围
CODE_EXT = (".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".in")

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def _fetch(url: str) -> bytes:
    # 延迟导入 urllib.request：必须晚于上面的 envfix，否则 SSL 不可用
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "retrochimera-audit"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def upstream_tree(ref: str) -> dict:
    """取上游某 ref 下的文件 → blob SHA 映射（只保留源码与文档）。"""
    url = f"https://api.github.com/repos/{UPSTREAM}/git/trees/{ref}?recursive=1"
    data = json.loads(_fetch(url).decode("utf-8"))
    if "tree" not in data:
        raise SystemExit(
            f"取上游文件树失败：{data.get('message', '未知错误')}\n"
            f"  可能 ref「{ref}」不存在（试试 --list-tags 看可用版本）"
        )
    return {
        t["path"]: t["sha"]
        for t in data["tree"]
        if t["type"] == "blob" and t["path"].endswith(CODE_EXT)
    }


def local_blob_sha(path: str) -> str:
    """算本地文件的 git blob SHA（与上游同一算法，可直接比对）。"""
    return subprocess.run(
        ["git", "hash-object", path], capture_output=True, text=True, cwd=ROOT
    ).stdout.strip()


def _cmp_content(path: str, ref: str) -> bool:
    """把本地与上游同一文件的内容归一化换行后逐字节比较。

    🔴 必须比**内容**而不是重算 blob SHA：上游是 LF，本地工作区可能是
       CRLF（`.gitattributes` 只在入库/检出时转换，工作区文件不动）。
       把本地归一化成 LF 再算 SHA，得到的仍是「本地 LF 版」的 SHA，
       与上游未必相等 —— SUPPORT.md 就是这样被误报的。
    """
    import urllib.request

    rel = os.path.relpath(path, CORE).replace("\\", "/")
    url = f"https://raw.githubusercontent.com/{UPSTREAM}/{ref}/{rel}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "retrochimera-audit"})
        with urllib.request.urlopen(req, timeout=60) as r:
            up = r.read()
    except Exception as e:  # noqa: BLE001 - 拿不到上游就放弃这次比对
        print(f"   (取上游原文失败，无法比对内容：{e})")
        return False

    with open(path, "rb") as f:
        local = f.read()
    return local.replace(b"\r\n", b"\n") == up.replace(b"\r\n", b"\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 core/ 与上游 microsoft/retrochimera 是否一致")
    ap.add_argument("--ref", default="v1.3.0", help="上游 ref（tag / branch，默认 v1.3.0）")
    ap.add_argument("--list-tags", action="store_true", help="列出上游可用 tag 后退出")
    args = ap.parse_args()

    if args.list_tags:
        tags = json.loads(_fetch(f"https://api.github.com/repos/{UPSTREAM}/tags").decode())
        print("上游可用 tag：")
        for t in tags[:20]:
            print("  ", t["name"])
        return 0

    print(f"校验 core/ ↔ {UPSTREAM}@{args.ref}")
    print("=" * 60)

    try:
        ups = upstream_tree(args.ref)
    except SystemExit as e:
        print(e)
        return 1

    tracked = set(subprocess.run(
        ["git", "ls-files", "core/"], capture_output=True, text=True, cwd=ROOT
    ).stdout.split("\n"))

    missing, untracked, differ = [], [], []

    for up_path, sha in sorted(ups.items()):
        rel = os.path.join("core", up_path).replace("\\", "/")

        if up_path in EXPECTED_ABSENT:
            continue
        if up_path in EXPECTED_DIFF:
            continue
        # 本仓自有的中文译本不是上游文件
        if up_path.endswith(".zh-CN.md"):
            continue

        abs_path = os.path.join(CORE, up_path)
        if not os.path.exists(abs_path):
            missing.append(rel)
            continue
        if rel not in tracked:
            untracked.append(rel)
            continue
        if local_blob_sha(abs_path) != sha and not _cmp_content(abs_path, args.ref):
            differ.append(rel)

    ok = True

    if missing:
        ok = False
        print(f"{RED}[缺失] 上游有、本仓没有（{len(missing)} 个）{RESET}")
        for f in missing:
            print("   ", f)
        print("   → 复制过来后记得 git add（先确认 .gitignore 没误伤）\n")

    if untracked:
        ok = False
        print(f"{RED}[未跟踪] 本地存在但没进 git（{len(untracked)} 个）{RESET}")
        for f in untracked:
            print("   ", f)
        print("   → 这正是 .gitignore 误伤的典型症状：git add 后本地能跑，"
              "别人 clone 就 ImportError\n")

    if differ:
        ok = False
        print(f"{YELLOW}[不一致] 内容与上游不同（{len(differ)} 个）{RESET}")
        for f in differ:
            print("   ", f)
        print("   → 若是有意改动，请登记到 core/VENDORED.md 的「本地化改动」表\n")

    print("=" * 60)
    if ok:
        print(f"{GREEN}✓ 全部一致{RESET}（差异仅限 EXPECTED_DIFF / EXPECTED_ABSENT 中登记的条目）")
        print(f"  比对文件数：{len(ups)}")
        print(f"  已登记的有意差异：{sorted(EXPECTED_DIFF)}")
        print(f"  已登记的不搬文件：{len(EXPECTED_ABSENT)} 个（上游 CI 配置等）")
        return 0

    print(f"{RED}✗ 发现 {len(missing) + len(untracked) + len(differ)} 处问题，请处理{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
