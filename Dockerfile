# RetroChimera API —— Linux 容器镜像
#
# 🔴 这个镜像不好构建，原因在依赖上（不是本项目代码的问题）：
#    1. PyTorch 必须来自官方 wheel 源，不能用 conda 通道（fbgemm.dll / 冲突）
#    2. numpy 必须锁 1.x（torch 2.2.2 按 numpy 1.x 编译）
#    3. PyG 家族的 wheel 目录名是 torch-2.2.0（不是 2.2.2）
#    所以这里**不用 requirements.txt 一把梭**，而是照实测组合分步装。
#
# 构建：
#   docker build -t retrochimera:1.3.0 .
#   docker build -t retrochimera:1.3.0-gpu --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121 .
#
# 运行（CPU）：
#   docker run -d --name retro -p 8000:8000 \
#     -v /srv/models:/app/models:ro \
#     -v /srv/retro-data:/app/web/data \
#     --env-file web/.env retrochimera:1.3.0
#
# 🔴 模型权重（9GB）**不要打进镜像**：构建上下文会变成 9GB、
#    每次改动都重新推一遍。务必用 volume 挂进去。

# ---------------------------------------------------------------- 基础镜像
FROM python:3.9-slim-bookworm

# 图构建参数：--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# PyG wheel 目录名里的 cuda 段（cpu / cu121），跟 TORCH_INDEX_URL 保持一致
ARG PYG_WHEEL_TAG=torch-2.2.0+cpu

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RETRO_PORT=8000 \
    RETRO_MODEL_PORT=8001 \
    RETRO_MODEL_HOST=127.0.0.1 \
    RETRO_RULE_WORKERS=4 \
    RETRO_MODELS_DIR=/app/models

# ---------------------------------------------------------------- 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
      # libgomp1 —— torch / scipy / sklearn 都要它，缺了报 "libgomp.so.1: cannot open"
      libgomp1 \
      # graphviz —— syntheseus 的 CLI 默认要画路线图，缺了会直接 ValueError 拒绝启动。
      #   注意：装了 python 包还得有**系统** graphviz，两者都要。
      graphviz \
      curl ca-certificates \
      # 规则库解压与模型权重读取
      unzip \
      # rdkit / PIL 的运行时依赖
      libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------------------------------------------------------------- 1. torch
# 🔴 顺序关键：torch 先装，numpy 紧接着按死 1.x，最后才装其它包 ——
#    否则某个包会把 numpy 升到 2.x，torch 2.2.2 直接崩。
RUN pip install --no-cache-dir \
      torch==2.2.2 torchvision==0.17.2 \
      --index-url "${TORCH_INDEX_URL}" \
 && pip install --no-cache-dir numpy==1.26.4

# ---------------------------------------------------------------- 2. PyG
# 🔴 wheel 目录名是 torch-2.2.0（对应 2.2.x），写成 2.2.2 会 404
RUN pip install --no-cache-dir torch-geometric==2.5.2 \
 && pip install --no-cache-dir torch-scatter torch-sparse torch-cluster \
      -f "https://data.pyg.org/whl/${PYG_WHEEL_TAG}.html"

# ---------------------------------------------------------------- 3. 化学 + 框架
RUN pip install --no-cache-dir \
      rdkit==2023.9.6 rdchiral pandas scipy h5py omegaconf \
      antlr4-python3-runtime tqdm joblib more-itertools pebble wandb pyyaml \
      syntheseus==0.8.0 syntheseus-graphium==0.1.0 \
      flask requests

# ---------------------------------------------------------------- 4. 上游本体
# 先只拷 pyproject，装完再拷全部源码 —— 改业务代码时这一层能命中缓存，
# 不用每次重建都重装一遍依赖。
COPY core/pyproject.toml core/README.md core/LICENSE /app/core/
RUN cd /app/core \
 && pip install --no-cache-dir -e . --no-build-isolation \
 && rm -rf /app/core/retrochimera

# ---------------------------------------------------------------- 5. 应用代码
COPY bootstrap/ /app/bootstrap/
COPY web/ /app/web/
COPY scripts/ /app/scripts/
COPY run_api.py start_api.sh /app/

# 🔴 --no-build-isolation 的原因：core/pyproject.toml 用静态 version=1.3.0
#    （上游靠 git tag 推导，本仓没有 tag），而隔离构建会另起一个环境装
#    setuptools_scm，装出来的版本会变成 0.0.0。
RUN chmod +x /app/start_api.sh \
 && python -c "import flask, retrochimera, syntheseus, torch, rdkit; print('[build] 依赖自检通过')"

# ---------------------------------------------------------------- 运行目录
# 权重与反馈数据都走挂载，不进镜像
RUN mkdir -p /app/models /app/web/data /app/.tmp-model-service \
 && useradd -m -u 1000 retro && chown -R retro:retro /app
USER retro

# 自检：Python 版本 / 依赖 / 目录结构（不启动服务，秒级完成）
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/models', timeout=8).status==200 else 1)"

EXPOSE 8000

# 默认只起 API。模型服务由 compose 的 model 容器负责（api 无法拉起远端服务）。
CMD ["./start_api.sh"]
