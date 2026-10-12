#!/usr/bin/env bash
# RetroChimera API —— Linux/macOS 启动脚本（Windows 请用「启动后端API.bat」）
#
# 与 Windows 版的职责完全对等：
#   1) 探测 Python 解释器（换机器不用改代码）
#   2) 检查 / 安装 Flask
#   3) 端口占用检测（重复启动时最容易踩的坑）
#   4) 判断模型服务是否已在跑（**复用**，不重载 —— 这是拆成独立进程的最大好处）
#   5) 启动 API（模型在后台慢慢加载）
#
# 用法：
#     ./start_api.sh                  # 前台启动（Ctrl+C 停）
#     RETRO_PORT=8011 ./start_api.sh  # 换端口
#     ./start_api.sh --check          # 只做环境体检，不启动
#     ./start_api.sh --model          # 只启动模型服务（常驻，生产建议这样跑）
#
# 生产部署建议：别在前台跑。用 systemd（deploy/retrochimera-api.service）
# 或 docker compose（deploy/docker-compose.yml）。
set -uo pipefail

# 所有路径按脚本自身位置推导 —— 仓库放哪个盘/哪个目录都能跑
cd "$(dirname "$0")"
ROOT="$(pwd)"
LOGFILE="$ROOT/_startup_log.txt"

# ---- 默认配置（都可用环境变量覆盖）----
export RETRO_PORT="${RETRO_PORT:-8000}"
export RETRO_MODEL_PORT="${RETRO_MODEL_PORT:-8001}"
# 规则子进程数：16 个 ≈ 70GB 会打满内存（Linux 上是 OOM Killer 直接杀进程）。
# 默认 4（≈18GB）。服务器内存小就调到 2。
export RETRO_RULE_WORKERS="${RETRO_RULE_WORKERS:-4}"

MODE="${1:-serve}"   # serve | check | model

# ---- 探测解释器 ----
# 优先级：RETRO_PYTHON > 当前 venv > conda retrochimera > PATH
pick_python() {
    if [[ -n "${RETRO_PYTHON:-}" && -x "${RETRO_PYTHON}" ]]; then
        echo "${RETRO_PYTHON}"; return 0
    fi
    if [[ -x "$ROOT/.venv/bin/python" ]]; then
        echo "$ROOT/.venv/bin/python"; return 0
    fi
    if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
        echo "${CONDA_PREFIX}/bin/python"; return 0
    fi
    for cand in "$HOME/miniconda3/envs/retrochimera/bin/python" \
                "$HOME/anaconda3/envs/retrochimera/bin/python"; do
        [[ -x "$cand" ]] && { echo "$cand"; return 0; }
    done
    command -v python3 2>/dev/null && return 0
    command -v python  2>/dev/null && return 0
    return 1
}

if ! ENV_PY="$(pick_python)"; then
    cat >&2 <<'EOF'
[ERROR] 找不到可用的 python。

  解决方式（任选其一）：
    1) 用官方脚本建好环境：  ./scripts/setup_env_linux.sh
    2) 已经建好了就激活它：   source ~/miniconda3/etc/profile.d/conda.sh && conda activate retrochimera
    3) 或显式指定：          RETRO_PYTHON=/path/to/python3.9 ./start_api.sh
EOF
    exit 1
fi

# 🔴 刻意**不用** `echo ... | tee -a`：体检里逐条 log 会高频写同一个文件，
#    在 Windows（Git Bash / WSL 挂载盘）上并发打开会报
#    "Device or resource busy"，把体检输出刷成一堆噪音。
#    `>>` 纯追加更稳；再叠一层容错 —— 日志文件被占用/无权限时**只丢日志、
#    不影响体检本身**（体检结果仍打印到终端）。写入失败只提示一次。
_LOG_OK=1
log()  {
    echo "$*"
    if [[ $_LOG_OK -eq 1 ]]; then
        echo "$*" >>"$LOGFILE" 2>/dev/null || {
            # 第一次失败就永久关掉文件写入，避免刷屏
            _LOG_OK=0
            echo "[note] 日志文件不可写（$LOGFILE），后续只输出到终端" >&2
        }
    fi
}

# ==========================================================================
# 环境体检（--check）
# ==========================================================================
check_env() {
    local bad=0
    log "=================================================================="
    log "  RetroChimera 环境体检"
    log "=================================================================="
    log "  解释器   : $ENV_PY ($("$ENV_PY" --version 2>&1))"
    log "  仓库根   : $ROOT"
    log "  API 端口 : $RETRO_PORT"
    log "  模型端口 : $RETRO_MODEL_PORT"
    log "  规则进程 : $RETRO_RULE_WORKERS"
    log ""

    # --- Python 版本：必须是 3.9.x ---
    # 🔴 torch 2.2.2 是按 3.9 编译的；3.10+ 起 retrochimera/syntheseus 会出现
    #    各种兼容问题（本项目实测组合见 web/requirements.txt 顶部）。
    local pyver
    pyver="$("$ENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    if [[ "$pyver" != "3.9" ]]; then
        log "  [FAIL] Python 版本是 $pyver，本项目需要 3.9.x"
        log "         （torch 2.2.2 + numpy 1.x 是按 3.9 编译的）"
        bad=1
    else
        log "  [ok] Python 3.9.x"
    fi

    # --- 关键依赖 ---
    # 🔴 版本号用 importlib.metadata 取而不是 __version__：Flask 3.2+ 已把
    #    __version__ 标记弃用，直接读会打 DeprecationWarning 混进体检输出。
    "$ENV_PY" -W ignore - <<'PY' || bad=1
import importlib, importlib.metadata as md, sys

def ver(name):
    try:
        return md.version(name)
    except Exception:
        m = importlib.import_module(name)
        return getattr(m, "__version__", "") or "?"

mods = [
    ("flask", "API 服务"), ("requests", "HTTP 客户端"),
    ("retrochimera", "上游模型本体（cd core && pip install -e .）"),
    ("syntheseus", "逆合成搜索框架"),
    ("torch", "深度学习"), ("rdkit", "化学结构解析"),
    ("numpy", "数值计算"),
]
bad = 0
for name, why in mods:
    try:
        importlib.import_module(name)
        print(f"  [ok] {name:16s} {ver(name):12s} ({why})")
    except Exception as e:
        print(f"  [FAIL] {name:16s} ({why}) -> {type(e).__name__}: {e}")
        bad = 1

# 版本硬约束：numpy 必须 1.x
try:
    import numpy
    if numpy.__version__.split(".")[0] != "1":
        print(f"  [FAIL] numpy 是 {numpy.__version__}，必须锁 1.x（torch 2.2.2 按 numpy 1.x 编译）")
        bad = 1
    import torch
    if tuple(int(x) for x in torch.__version__.split("+")[0].split(".")[:2]) >= (2, 3):
        print(f"  [WARN] torch {torch.__version__} 偏新，实测组合是 2.2.2")
except Exception:
    pass
sys.exit(bad)
PY

    # --- GPU ---
    if "$ENV_PY" -c 'import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)' 2>/dev/null; then
        log "  [ok] GPU 可用：$("$ENV_PY" -c 'import torch; print(torch.cuda.get_device_name(0))')"
    else
        log "  [--] 无 GPU（CPU 也能跑，但慢很多；模型权重加载本身不依赖 GPU）"
    fi

    # --- 模型权重 ---
    log ""
    local mdir="${RETRO_MODELS_DIR:-$ROOT/models}"
    if [[ -d "$mdir" ]]; then
        local found
        found="$(find "$mdir" -maxdepth 2 -name "*.ckpt" -o -maxdepth 2 -name "*.pt" 2>/dev/null | head -3)"
        if [[ -n "$found" ]]; then
            log "  [ok] 模型目录有内容：$mdir"
        else
            log "  [WARN] 模型目录是空的：$mdir"
            log "         下载：python scripts/download_model.py"
            bad=1
        fi
    else
        log "  [FAIL] 模型目录不存在：$mdir"
        bad=1
    fi

    # --- 端口 ---
    log ""
    if port_busy "$RETRO_PORT"; then
        log "  [WARN] 端口 $RETRO_PORT 已被占用 —— 可能已经有一个 API 在跑了"
    else
        log "  [ok] 端口 $RETRO_PORT 空闲"
    fi

    log ""
    if [[ $bad -eq 0 ]]; then
        log "体检通过，可以启动：./start_api.sh"
    else
        log "有问题需要处理（见上面 [FAIL] 行）。"
        log "一键建环境：./scripts/setup_env_linux.sh"
    fi
    return $bad
}

port_busy() {
    # 不用 ss/lsof（不同发行版名字不一样），用 python 试 bind，最可靠
    "$ENV_PY" - "$1" <<'PY'
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
    sys.exit(0)      # 绑上了 = 空闲
except OSError:
    sys.exit(1)      # 绑不上 = 被占
finally:
    s.close()
PY
}

case "$MODE" in
    --check|check)
        : > "$LOGFILE" 2>/dev/null || true
        check_env
        exit $?
        ;;
    --model|model)
        log "[model] 单独启动模型服务（常驻）"
        exec "$ENV_PY" -u "$ROOT/web/model_server.py"
        ;;
esac

# ==========================================================================
# 正常启动流程
# ==========================================================================
: > "$LOGFILE" 2>/dev/null || true
log "[$(date '+%F %T')] start_api.sh 启动"
log "  python=$ENV_PY"
log "  RETRO_PORT=$RETRO_PORT  RETRO_MODEL_PORT=$RETRO_MODEL_PORT  RETRO_RULE_WORKERS=$RETRO_RULE_WORKERS"

# --- Flask 检查/安装 ---
if ! "$ENV_PY" -c "import flask" >>"$LOGFILE" 2>&1; then
    log "[warn] 未装 flask，尝试安装……"
    if "$ENV_PY" -m pip install flask -q >>"$LOGFILE" 2>&1; then
        log "[ok] flask 已安装"
    else
        log "[ERROR] flask 安装失败，见 $LOGFILE"
        exit 1
    fi
fi

# --- 端口占用：直接拒绝并说清怎么办 ---
# 🔴 模型端口被占**不是**错误 —— 那正是「复用已加载模型」的前提，不要拦。
if port_busy "$RETRO_PORT"; then
    cat >&2 <<EOF

[ERROR] 端口 $RETRO_PORT 已被占用。

  多半是已经有一个 API 在跑了。关掉它，或者换端口重启：

      RETRO_PORT=8011 RETRO_MODEL_PORT=8012 ./start_api.sh

EOF
    exit 1
fi

if port_busy "$RETRO_MODEL_PORT"; then
    log "[ok] 模型服务已在 :$RETRO_MODEL_PORT 运行 —— **复用**，模型不重载"
else
    log "[--] 模型服务未运行 —— 将由 API 在后台拉起并加载（约 1~2 分钟）"
fi

cat <<EOF

==================================================================
  RetroChimera API
==================================================================
  API    http://127.0.0.1:$RETRO_PORT
  模型   http://127.0.0.1:$RETRO_MODEL_PORT/status   （进度看这里）
  日志   $LOGFILE

  本服务**只提供 API，不托管页面**。前端在 retroweb 仓库（npm run dev，:5177）。
  浏览器打开根路径会看到一份 JSON 端点清单。

  Ctrl+C 只停 API；模型服务是**故意常驻**的 —— 重启 API 时模型不用重载，
  这正是把它拆成独立进程的好处。
==================================================================

EOF

# exec：让 API 接管前台，Ctrl+C 能直接收到 SIGINT
exec "$ENV_PY" -u "$ROOT/run_api.py"
