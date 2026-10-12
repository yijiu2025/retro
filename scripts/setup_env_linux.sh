#!/usr/bin/env bash
# 在 Linux 上从零建好 RetroChimera 运行环境。
#
# 🔴 这个项目对依赖组合的要求很挑，**顺序不能乱** —— 不是那种
#    `pip install -r requirements.txt` 一把梭就能对的：
#      1. PyTorch 必须用 pip 官方 wheel（不能用 conda 通道，见 deploy/environment-win.yml 里的教训）
#      2. numpy 必须锁 1.x（torch 2.2.2 是按 numpy 1.x 编译的）
#      3. PyG 家族的 wheel 目录名是 torch-2.2.0（不是 2.2.2），写错就 404
#      4. retrochimera 本体在 core/，要源码装
#
# 用法：
#     ./scripts/setup_env_linux.sh                # 装 CPU 版
#     ./scripts/setup_env_linux.sh --gpu cu121    # 装 CUDA 12.1 版（需要 nvidia 驱动）
#     ./scripts/setup_env_linux.sh --venv         # 装到仓库内的 .venv（推荐，不污染系统）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYVER="3.9"
TORCH_VER="2.2.2"
NUMPY_VER="1.26.4"
PYG_VER="2.5.2"
CUDA_TAG="cpu"
USE_VENV=0
DO_RDKIT_CHIRA=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gpu)  CUDA_TAG="${2:-cu121}"; shift 2 ;;
        --venv) USE_VENV=1; shift ;;
        --system) USE_VENV=0; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "未知参数: $1（用 --help 看用法）"; exit 1 ;;
    esac
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 1. 解释器
say "1/5 检查 Python $PYVER"
BASE_PY=""
for cand in "python$PYVER" python3.9 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        v="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        if [[ "$v" == "$PYVER" ]]; then BASE_PY="$(command -v "$cand")"; break; fi
    fi
done

if [[ -z "$BASE_PY" ]]; then
    warn "系统里没有 Python $PYVER。"
    cat <<EOF

  三选一：
    A) 用 conda（最省事，版本可控）：
         conda create -n retrochimera python=$PYVER -y
         conda activate retrochimera
         再重新跑一次本脚本

    B) 用 pyenv / deadsnakes 自己装 $PYVER

    C) 系统 Python 恰好是 $PYVER —— 那就直接跑本脚本

  为什么非要 $PYVER：torch $TORCH_VER + numpy $NUMPY_VER 是按 3.9 编译的，
  3.10+ 会出现难以定位的兼容问题。
EOF
    exit 1
fi
echo "  用 $BASE_PY ($("$BASE_PY" --version 2>&1))"

# ---------------------------------------------------------------- 2. venv
if [[ $USE_VENV -eq 1 ]]; then
    say "2/5 创建 .venv"
    "$BASE_PY" -m venv "$ROOT/.venv"
    PY="$ROOT/.venv/bin/python"
    "$PY" -m pip install --upgrade pip setuptools wheel -q
else
    say "2/5 使用当前环境（$("$BASE_PY" -c 'import sys; print(sys.prefix)')）"
    PY="$BASE_PY"
    "$PY" -m pip install --upgrade pip setuptools wheel -q
fi
echo "  PY=$PY"

# ---------------------------------------------------------------- 3. torch
say "3/5 安装 PyTorch $TORCH_VER（$CUDA_TAG）"
if [[ "$CUDA_TAG" == "cpu" ]]; then
    "$PY" -m pip install "torch==$TORCH_VER" torchvision==0.17.2 \
        --index-url https://download.pytorch.org/whl/cpu
else
    "$PY" -m pip install "torch==$TORCH_VER" torchvision==0.17.2 \
        --index-url "https://download.pytorch.org/whl/${CUDA_TAG}"
fi

# 🔴 必须在 torch 之后、其它包之前把 numpy 按住，否则可能被某个包升到 2.x
say "    锁定 numpy $NUMPY_VER"
"$PY" -m pip install "numpy==$NUMPY_VER"

# ---------------------------------------------------------------- 4. PyG
say "4/5 安装 PyTorch Geometric $PYG_VER"
"$PY" -m pip install "torch-geometric==$PYG_VER"
# 🔴 wheel 目录名是 torch-2.2.0（对应 torch 2.2.x），不是 2.2.2 —— 写成 2.2.2 会 404
PYG_WHEEL_TAG="torch-2.2.0+${CUDA_TAG}"
"$PY" -m pip install torch-scatter torch-sparse torch-cluster \
    -f "https://data.pyg.org/whl/${PYG_WHEEL_TAG}.html"

# ---------------------------------------------------------------- 5. 其余
say "5/5 安装化学与框架依赖"
"$PY" -m pip install \
    "rdkit==2023.9.6" rdchiral pandas scipy h5py omegaconf \
    antlr4-python3-runtime tqdm joblib more-itertools pebble wandb pyyaml

say "    安装逆合成框架 syntheseus"
"$PY" -m pip install "syntheseus==0.8.0" "syntheseus-graphium==0.1.0"

say "    安装 RetroChimera 本体（core/ 源码）"
cd "$ROOT/core"
# 🔴 --no-build-isolation：避免 pip 另起一个隔离环境去装 setuptools_scm，
#    那样装出来的版本会变成 0.0.0（上游用 git tag 推导版本，本仓没有 tag）
"$PY" -m pip install -e . --no-build-isolation
cd "$ROOT"

say "    安装 API 服务"
"$PY" -m pip install flask requests

# ---------------------------------------------------------------- 验证
say "验证安装结果"
if "$PY" - <<'PY'
import importlib, sys
bad = 0
for name in ("flask", "retrochimera", "syntheseus", "torch", "rdkit", "numpy"):
    try:
        m = importlib.import_module(name)
        print(f"  [ok] {name:16s} {getattr(m, '__version__', '')}")
    except Exception as e:
        print(f"  [FAIL] {name:16s} {type(e).__name__}: {e}")
        bad = 1
import numpy, torch
if numpy.__version__.split(".")[0] != "1":
    print(f"  [FAIL] numpy={numpy.__version__}，必须 1.x"); bad = 1
print(f"  GPU 可用: {torch.cuda.is_available()}")
sys.exit(bad)
PY
then
    cat <<EOF

==================================================================
  环境就绪 ✓
==================================================================
  解释器：$PY
  下一步：
    1) 下载模型权重（约 9GB）：
         $PY scripts/download_model.py
    2) 体检：
         ./start_api.sh --check
    3) 启动：
         ./start_api.sh

  提示：服务里用 RETRO_RULE_WORKERS 控制规则子进程数（默认 4 ≈ 18GB）。
       内存小于 16GB 的机器请设成 2，否则容易被 OOM Killer 杀掉。
EOF
else
    warn "上面有 [FAIL] 项。逐个修，或对照 web/requirements.txt 顶部的实测组合。"
    exit 1
fi
