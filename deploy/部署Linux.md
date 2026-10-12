# Linux 部署指南

三种方式，按场景挑：

| 方式 | 适合 | 优点 | 代价 |
|---|---|---|---|
| **裸机 + 脚本** | 临时测试、单机自用 | 最直观，出问题好排查 | 得自己管进程守护 |
| **systemd** | 单台生产服务器 | 重启自愈、日志进 journal、资源可限 | 只适用单机 |
| **Docker** | 要编排 / 多环境 / 便于迁移 | 隔离干净、GPU 支持好、换机器快 | 模型权重 9GB 要另想办法 |

---

## 0. 两种进程，先理解这个

部署前**必须**知道本服务是两个进程：

```
[API 服务] :8000            ← Flask，只做转接，秒起、无状态
     │
     │ HTTP（内部）
     ▼
[模型服务] :8001            ← 加载 checkpoint（9GB，约 1~2 分钟）、跑推理与搜索
```

**前端只跟 API 说话**，永远不直连模型服务。模型服务**故意常驻** ——
重启 API 时模型不用重载，这正是把它拆成独立进程的最大好处。

由此推出三条部署原则：

1. **模型服务要先起、给足启动时间**（健康检查 `start_period` ≥ 240s）。
2. **改了两边要分别重启**：`model_server.py` / `model_runtime.py` 改了要重启模型服务，
   `app.py` / `jobhub.py` 改了重启 API。
3. **模型服务是内存大头**：每个规则子进程约 4.4GB，默认 4 个 ≈ 18GB。
   内存 < 16GB 的机器务必设 `RETRO_RULE_WORKERS=2`。

---

## 1. 方式一：裸机 + 脚本

### 1.1 建环境

```bash
git clone <你的仓库> /srv/retrochimera && cd /srv/retrochimera

# CPU 版
./scripts/setup_env_linux.sh --venv

# GPU 版（需要先装好 NVIDIA 驱动）
./scripts/setup_env_linux.sh --venv --gpu cu121
```

> 🔴 **不要用 `pip install -r web/requirements.txt`**。这个项目的依赖对版本和顺序都很挑
> （torch 必须用官方 wheel、numpy 锁 1.x、PyG 的 wheel 目录名是 `torch-2.2.0` 不是 `2.2.2`）。
> 脚本已经把顺序固化了。

### 1.2 下模型（约 9GB）

```bash
/srv/retrochimera/.venv/bin/python scripts/download_model.py
```

### 1.3 体检

```bash
./start_api.sh --check
```

会逐项检查 Python 版本、七个关键依赖、GPU 可用性、模型权重、端口占用，
并对**不满足**的项给出具体修复建议。

### 1.4 启动

```bash
./start_api.sh            # 前台，Ctrl+C 停
```

只想单独起模型服务（生产上推荐，让它常驻）：

```bash
./start_api.sh --model
```

换端口：

```bash
RETRO_PORT=8011 RETRO_MODEL_PORT=8012 ./start_api.sh
```

### 1.5 验证

```bash
curl http://127.0.0.1:8000/api | python3 -m json.tool   # 端点清单
curl http://127.0.0.1:8000/api/models | python3 -m json.tool  # 状态 + 加载进度
```

---

## 2. 方式二：systemd（推荐用于生产单机）

```bash
sudo mkdir -p /var/lib/retrochimera
sudo cp deploy/systemd/*.service /etc/systemd/system/

# 🔴 单元文件里写死了 /srv/retrochimera 和 /srv/retrochimera/.venv/bin/python。
#    你的路径不同就改这两处：
sudo sed -i 's#/srv/retrochimera/.venv/bin/python#你的解释器路径#g' /etc/systemd/system/retrochimera-*.service

sudo systemctl daemon-reload
sudo systemctl enable --now retrochimera-model   # 先起模型服务
sudo systemctl enable --now retrochimera-api      # 再起 API
```

密钥配置见 [`deploy/systemd/README.md`](systemd/README.md)（不要写进单元文件）。

### 常用命令

```bash
systemctl status retrochimera-api
journalctl -u retrochimera-model -f      # 看模型加载到哪一步
journalctl -u retrochimera-api -f        # 看请求日志

systemctl restart retrochimera-model      # 改了模型侧代码后
systemctl restart retrochimera-api         # 改了 API 侧代码后

sudo systemctl edit retrochimera-api      # 改配置（不会覆盖原文件）
```

### 反向代理 + HTTPS

Flask 自带的服务器只适合内网。对外服务请在前面放 nginx：

```nginx
server {
    listen 443 ssl;
    server_name retro.example.com;

    ssl_certificate     /etc/letsencrypt/live/retro.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/retro.example.com/privkey.pem;

    # 多步搜索可能跑几分钟，别让 nginx 先超时
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # SSE 流式分析必须关缓冲，否则前端看不到逐字输出
        proxy_buffering    off;
    }
}
```

别忘了把 `RETRO_CORS_ORIGINS=https://retro.example.com` 配上。

---

## 3. 方式三：Docker

### 3.1 构建

```bash
# CPU 版
docker build -t retrochimera:1.3.0 .

# GPU 版
docker build -t retrochimera:1.3.0-gpu \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121 \
  --build-arg PYG_WHEEL_TAG=torch-2.2.0+cu121 .
```

> 🔴 构建镜像**不含模型权重**。`models/` 已在 `.dockerignore` 里排除
> （不然构建上下文会变成 9GB，每次改动都得重推一遍）。权重必须挂载。

### 3.2 准备权重

```bash
mkdir -p /srv/models
cd /srv/retrochimera && python scripts/download_model.py
# 确认 /srv/retrochimera/models 里有 checkpoint
```

### 3.3 起服务

```bash
docker compose up -d --build
docker compose logs -f model       # 盯模型加载（首次约 2 分钟）
```

compose 里已经把该注意的都配好了：

- `model` 服务**不映射端口**到宿主机 —— 它没有鉴权，只在内部网络可达。
- `api` 容器用 `RETRO_MODEL_HOST=model`（服务名）而不是 `127.0.0.1`，
  因为两个容器是不同网络命名空间。
- 健康检查 `start_period: 240s`，避免模型还在加载就被判定失败并反复重启。

### 3.4 验证

```bash
docker compose ps                                    # 两个都 healthy 才算好
curl http://127.0.0.1:8000/api/models | python3 -m json.tool
```

### 3.5 常用运维

```bash
docker compose logs -f api
docker compose restart api              # 改了 API 代码
docker compose restart model            # 改了模型代码（会重新加载，约 2 分钟）
docker compose stop                     # 停止（模型在内存里，停了就没了）
docker compose down                     # 移除容器（权重在 volume 里，不受影响）
```

### 3.6 GPU 版还需要

放开 compose 里 `model` 服务被注释掉的 `deploy.resources.reservations.devices`，
并确认宿主机装了 NVIDIA Container Toolkit。

---

## 4. 常见问题

**Q：容器反复重启，日志里没有 Python 异常？**
内存不够被 OOM Killer 杀了。规则子进程每个约 4.4GB，把 `RETRO_RULE_WORKERS` 调到 2。

**Q：启动报 `libgomp.so.1: cannot open shared object file`？**
系统缺 OpenMP 运行库。Dockerfile 已装 `libgomp1`；裸机执行 `apt-get install libgomp1`。

**Q：上游 CLI 报 `Could not import visualization code ... graphviz`？**
缺系统 graphviz。`apt-get install graphviz`。注意装了 Python 包还不够，两个都要。

**Q：`/api/models` 一直显示 loading，进度卡住不动？**
`progress` 是**估算值**（按阶段锚点 + 阶段内耗时插值），不是精确百分比。
看 `phase` 字段的真实文案，以及模型服务的日志：
`journalctl -u retrochimera-model -f` 或 `docker compose logs -f model`。

**Q：改了 `web/model_runtime.py` 但行为没变？**
没重启模型服务。它是独立进程，还在跑旧代码。

**Q：多步搜索一直 0 结果？**
搜索预算是「本次新增」调用数。模型服务常驻时计数会累加，
代码里已用「基线 + 本次预算」处理；若你改过这里，注意别传绝对值。

**Q：API 报「模型服务不可达」？**
按部署方式排查：
- 裸机/systemd：`RETRO_MODEL_HOST` 应为 `127.0.0.1`；确认 `systemctl status retrochimera-model` 是 active
- Docker：应为服务名 `model`（不是 `127.0.0.1`）；确认 `docker compose ps` 里 model 是 healthy

**Q：需要跑在公网上吗？安全上要注意什么？**
本服务**没有任何鉴权**。公网暴露等于「任何人都能消耗你的 GPU/CPU 和内存」。
务必：前面加反向代理 + HTTPS、限制来源 IP、必要时加一层 basic auth 或改用 VPN 访问。
