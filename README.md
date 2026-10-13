# RetroChimera API 后端

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9](https://img.shields.io/badge/python-3.9-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/Flask-3.x-black.svg)](https://flask.palletsprojects.com/)

[RetroChimera](https://github.com/microsoft/retrochimera)（微软的逆合成预测模型）之上的
一层 **Flask API**：单步逆合成预测、结构式解析/绘制、基于大模型的工艺分析。

> 🔴 **本服务只提供 API，不托管任何前端页面。**
> 浏览器直接打开根路径会返回一份 JSON 端点清单。
> 前端界面在另一个仓库：[retroweb](https://github.com/yijiu2025/retroweb).

> 🌐 English version: [README.en.md](README.en.md) ·
> 部署到 Linux/Docker： [deploy/部署Linux.md](deploy/部署Linux.md) ·
> 面向用户的多步搜索全参数： [使用说明.md](使用说明.md)

---

## 1. 它由哪几块组成

```
retrochimera/
├── 启动后端API.bat         ← Windows 一键启动（双击即用）
├── start_api.sh            ← Linux/macOS 启动脚本（`--check` 环境体检 / `--model` 只起模型服务）
├── run_api.py             ← 命令行启动器（清掉干扰 sys.path 的注入路径）
├── Dockerfile              ← Linux 容器镜像（CPU / GPU 双模式）
├── docker-compose.yml      ← 两服务编排：model（不暴露端口）+ api
├── .dockerignore           ← 构建上下文排除项（**关键是 models/ 的 9GB**）
├── web/                    ← API 服务主体（Flask，只出接口、不托管页面）
│   ├── app.py                Flask 应用 + 全部 API 路由
│   ├── jobhub.py             任务队列（长任务不阻塞请求，带阶段/日志/取消）
│   ├── model_runtime.py      模型加载与搜索逻辑（**单一来源**：模型表 + 算法表都在这）
│   ├── model_server.py       常驻模型服务（独立进程，真正 load 模型的地方）
│   ├── model_client.py       API 进程侧的模型服务客户端（HTTP/状态文件双通道）
│   ├── llm.py                大模型入口（只做再导出，不含实现）
│   ├── llm_config.py           端点配置（多套 profiles）+ API Key 池轮换
│   ├── llm_prompts.py          提示词构建与分析模块注册表
│   ├── llm_transport.py        HTTP 传输与响应解析（含 JSON 抽取）
│   ├── llm_stream.py           分析/条件枚举/追问的 SSE 流式编排
│   ├── .env.example          配置模板（复制成 .env 再填）
│   └── requirements.txt      依赖清单（除上游本体）
├── bootstrap/              ← 运行时引导垫片（**必须在第三方 import 之前导入**）
│   ├── envfix.py             补 conda 的 DLL 搜索路径（修 `_ssl` 加载失败）
│   └── limit_workers.py      限制规则子进程数（修 WinError 1455）
├── scripts/                ← 命令行工具（诊断 / 预测 / 搜索 / 自修复 / 下载模型）
│   ├── check_install.py      安装验证：加载模型跑一次真实预测
│   ├── predict.py            单步预测 CLI（置信度分级 + 出反应结构图）
│   ├── search.py             多步合成路线搜索 CLI
│   ├── download_model.py     模型权重下载
│   ├── verify_vendored.py    校验 core/ 与上游一致（防漏文件）
│   └── repair_deps.py        依赖自检自修（坏哪个重装哪个）
├── deploy/                 ← 部署物料
│   ├── environment-win.yml   conda 环境导出文件（Windows）
│   ├── 部署Linux.md           **Linux 部署指南**（裸机 / systemd / Docker）
│   ├── systemd/               生产用单元文件（api + model，含资源上限与加固）
│   └── wheels/               离线 Flask 依赖 wheel（断网时 .bat 靠它装）
├── core/                   ← 上游源码（**已 vendored 进本仓**，见其 VENDORED.md）
│   ├── retrochimera/         上游 Python 包本体
│   └── pyproject.toml        装它：cd core && pip install -e .
└── models/                 ← 模型权重，9GB，**不入库**
    ├── retrochimera_pistachio/
    └── retrochimera_uspto50k/
```

> 🔴 **模型不在 API 进程里**。`app.py` 只做转接：真正 load 模型、跑搜索的是
> `model_server.py` 这个**独立常驻进程**（默认 127.0.0.1:8001）。这样 API 才能秒起，
> 而模型在后台慢慢加载；两者靠 `model_client.py` 通信（HTTP 优先，不通时读状态文件兜底）。
> 代价是：模型加载失败只在模型服务的日志里，API 侧靠 `/api/models` 透出 `error` 字段。
> 想彻底重启模型服务用 `POST /api/model-service/restart`。

**上游源码不需要再单独 clone** —— 它已纳入本仓版本管理（`core/`，约 1.1MB）。
装完依赖后 `cd core && pip install -e .` 即可。

> `core/` 里那 10 篇 md 分别写了什么、上游模型有哪些能力、怎么用、有哪些坑 →
> 见 [`上游文档导读.md`](上游文档导读.md)。

只有 `models/` 是仓库外的东西（9GB 太大），换机器时手动拷过去（见第 3.3 节）。

**全仓不写死盘符** —— 所有路径都按「仓库自身位置」推导，`git clone` 到哪个盘、哪个目录都能跑。
模型位置可用环境变量覆盖，见第 3.3 / 5 节。

---

## 2. 最少需要什么

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 10/11 或 Linux/macOS | 两边都有一键启动脚本（`.bat` / `start_api.sh`）；生产部署见 [`deploy/部署Linux.md`](deploy/部署Linux.md) |
| Python | **3.9**（3.10 理论可用） | 上游 `pyproject.toml` 限制 `>=3.9`；实测 3.9.7 可用。3.9 之外未经完整验证 |
| GPU | NVIDIA，显存 ≥ 6GB | 无 GPU 也能跑，但单步预测会从数秒变成数十秒 |
| CUDA | 12.1 | 对应 `torch==2.2.2+cu121` |
| 磁盘 | ≥ 15GB | 模型 9GB + 依赖约 5GB |
| 内存 | ≥ 16GB | `RETRO_RULE_WORKERS=4` 时约占 18GB；紧张就降到 2 |

---

## 3. 从零开始（换机器照做）

### 3.1 建 conda 环境并装依赖

```bat
conda create -n retrochimera python=3.9 -y
conda activate retrochimera
```

先装 PyTorch（**必须用官方 wheel 源**，否则会拉到 CPU 版；有 GPU 就用 cu121）：

```bat
pip install torch==2.2.2 torchvision==0.17.2 --index-url https://download.pytorch.org/whl/cu121
```

> 没 GPU 就把 `cu121` 换成 `cpu`。

再装其余依赖（在**仓库根**执行；下面用 `<仓库根>` 代指，例如 `F:\retrochimera`）：

```bat
cd <仓库根>
pip install -r web\requirements.txt
```

> 🔴 **`requirements.txt` 里的注释是必读项，不是可选阅读。** 尤其 **torch-geometric**：
> 它的 wheel 要从 `https://data.pyg.org/whl/torch-2.2.0+cu121.html` 拿，源目录名里是
> `torch-2.2.0` 而不是 `2.2.2`（这是上游的笔误，照抄即可）。
>
> 嫌一步步装麻烦，Linux 上直接用自动化脚本（顺序已固化，不会踩坑）：
> `./scripts/setup_env_linux.sh --venv`。

### 3.2 装上游本体（源码已在本仓，无需 clone）

```bat
cd <仓库根>\core
pip install -e .
```

如果卡在 build 依赖上，改用：

```bat
pip install -e . --no-build-isolation
```

验证：

```bat
python -c "import retrochimera; print(retrochimera.__file__)"
```

路径应落在 `<仓库根>\core\retrochimera\__init__.py`。
上游源码的版本信息与更新方式见 `core/VENDORED.md`。

### 3.3 放模型权重

把两个模型目录放到 `models/` 下：

```
models/retrochimera_pistachio/     ~4.2GB，推荐（数据量大）
models/retrochimera_uspto50k/      ~0.3GB，轻量
```

> 如果你手上只有 `.zip`，解压到同名目录即可。`.zip` 本身可以删掉。

模型位置可以用环境变量覆盖，不必非得放在 `models/`：

```bat
set RETRO_MODELS_DIR=D:\models
set RETRO_MODEL_PISTACHIO=E:\my\pistachio_model
```

### 3.4 写配置

```bat
copy web\.env.example web\.env
```

然后编辑 `web\.env`，至少填大模型那一组（不填也能用，只是"AI 工艺分析"按钮会提示未配置）：

```
RETRO_LLM_BASE_URL=https://token.sensenova.cn/v1
RETRO_LLM_API_KEY=sk-你的key
RETRO_LLM_MODEL=glm-5.2
```

`.env` 是 **mtime 热重载**的 —— 改完存盘即生效，**不用重启服务**。

---

## 4. 启动

### 方式一：双击（推荐）

```
<仓库根>\启动后端API.bat
```

它会自动找 Python（顺序：`RETRO_PYTHON` 环境变量 → 当前 conda 环境 → `.venv` →
默认 conda 路径 → PATH），检测端口是否被占用，然后拉起 API 服务。

想用它找别的解释器，先设一下：

```bat
set RETRO_PYTHON=D:\conda\envs\retrochimera\python.exe
```

### 方式二：命令行

```bat
conda activate retrochimera
cd <仓库根>
python web\app.py
```

或者用命令行启动器（会自动清掉干扰 `sys.path` 的注入路径）：

```bat
python run_api.py
set RETRO_PORT=8011 && python run_api.py
```

### 方式三：Linux / macOS

```bash
./scripts/setup_env_linux.sh --venv    # 建环境（CPU；GPU 加 --gpu cu121）
python scripts/download_model.py       # 下模型（约 9GB）
./start_api.sh --check                 # 环境体检：版本/依赖/GPU/权重/端口
./start_api.sh                         # 启动
```

生产部署（systemd / Docker，含反向代理与 GPU 配置）见
**[`deploy/部署Linux.md`](deploy/部署Linux.md)**。

启动后监听 **http://127.0.0.1:8000**。浏览器打开它只会看到 JSON 端点清单
（本服务不托管页面），要界面得把前端跑起来：

```bat
cd <retroweb 仓库>
npm install && npm run dev        # http://localhost:5177
```

> 关窗口时刷出一堆红色 `BrokenPipeError` 是**正常的**（worker 清理），不影响结果。

### 附：随仓库附带的命令行工具

都在 `scripts/` 下，在仓库根执行。它们**不需要**先启动 API 服务，直接加载模型跑。

| 工具 | 用途 | 示例 |
| --- | --- | --- |
| `scripts/check_install.py` | 安装验证：加载模型跑一次真实预测 | `python scripts/check_install.py` |
| `scripts/predict.py` | 单步预测，带置信度分级并生成反应结构图 | `python scripts/predict.py "CC(=O)Oc1ccccc1C(=O)O" 5` |
| `scripts/search.py` | 多步合成路线搜索（Retro*），可输出 JSON | `python scripts/search.py "Oc1ccc(OCc2ccccc2)c(Br)c1" 80 150 3` |
| `scripts/repair_deps.py` | 依赖自检自修：逐个探测导入，坏哪个重装哪个 | `python scripts/repair_deps.py` |
| `scripts/verify_vendored.py` | 校验 `core/` 与上游是否一致（查漏文件，需联网） | `python scripts/verify_vendored.py` |

`predict.py` 生成的结构图写在 `<仓库根>/outputs/`（该目录不入库）。

---

## 5. 配置项

全部走环境变量，优先级：**真实环境变量 > `web/.env` > 仓库根 `.env`**；空值视为未设置。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RETRO_PORT` | `8000` | 监听端口 |
| `RETRO_RULE_WORKERS` | `4` | 规则应用进程数。16（全部核心）会吃 70GB 内存并崩（WinError 1455），别乱调高 |
| `RETRO_MODELS_DIR` | `<仓库根>/models` | 模型根目录 |
| `RETRO_MODEL_PISTACHIO` | — | 单独指定 pistachio 模型路径 |
| `RETRO_MODEL_USPTO50K` | — | 单独指定 uspto50k 模型路径 |
| `RETRO_OUTPUT_DIR` | `<仓库根>/outputs` | `scripts/predict.py` 结构图的输出目录 |
| `RETRO_CORS_ORIGINS` | 空（关闭） | 允许跨域的前端 origin，逗号分隔；`*` 表示全放行。**前后端不同源时必须配** |
| `RETRO_LLM_BASE_URL` | — | OpenAI 兼容端点，如 `https://token.sensenova.cn/v1` |
| `RETRO_LLM_API_KEY` | — | 密钥 |
| `RETRO_LLM_MODEL` | — | 模型名 |
| `RETRO_LLM_MAX_TOKENS` | `16384` | **别调小**。推理模型的思考 token 也算在输出额度里，太小会导致正文一个字没出就截断 |
| `RETRO_LLM_TIMEOUT` | `300` | 单次请求超时（秒）。推理模型单步思考实测可达 55 秒 |
| `RETRO_LLM_JSON_MODE` | `1` | 是否要求 JSON 输出 |
| `RETRO_LLM_TEMPERATURE` | `0.3` | 采样温度 |

---

## 6. 接到前端

前端 `retroweb` 开发时通过 Vite 代理走 `/retro/*` → `http://127.0.0.1:8000/api/*`，同源，**不需要 CORS**。

生产环境如果前后端不同域，必须设：

```bat
set RETRO_CORS_ORIGINS=https://your-frontend.example.com
```

不配的症状是浏览器控制台报跨域、页面数据永远转圈。

> 本服务**不托管前端**：`/` 只返回端点清单 JSON，`/api/*` 才是接口。
> 想快速验证服务活着没，打 `/api/models`。

---

## 7. 大模型接不上时怎么查

按这个顺序，三步能定位九成问题：

1. **验鉴权** —— 直接打端点的模型列表：
   ```bat
   curl -H "Authorization: Bearer sk-xxx" https://你的端点/v1/models
   ```
   `200` 说明 key 有效；`401` 是 key 错。

2. **验 key + 模型是否都在套餐内**：
   ```bat
   curl "http://127.0.0.1:8000/api/llm/status?probe=1"
   ```

3. **列真正可用的模型**：
   ```bat
   curl http://127.0.0.1:8000/api/llm/models
   ```

两个反复踩过的坑：

- **`/v1/models` 会骗人** —— 列表里的模型不一定在你的套餐内。实测 9 个里有 3 个调不通，以第 2 步的 `probe` 结果为准。
- **推理模型只吐 `reasoning_content`** —— 模型先输出思考过程，`delta.content` 长时间为空。
  本项目已单独发 `thinking` SSE 事件，界面上会显示"思考中"；如果你自己接的客户端只读 `content`，会以为卡死了。

限流是**按模型端点**算的，换模型等于换一份独立配额。

当前实测可用配置：

```
RETRO_LLM_BASE_URL=https://token.sensenova.cn/v1
RETRO_LLM_MODEL=glm-5.2
RETRO_LLM_MAX_TOKENS=16384
RETRO_LLM_TIMEOUT=300
```

---

## 8. API 一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/` | 端点清单（JSON，自描述 —— 本服务不托管页面）。含 `search_algorithms`（搜索算法可选项，前端下拉的**唯一**来源） |
| GET | `/api/models` | 模型状态矩阵：每个模型 idle/loading/ready/failed/unavailable + 服务概览（含加载进度 `progress`） |
| POST | `/api/load` | 加载/切换模型（后启动，接口立即返回；进度看 `/api/models`） |
| POST | `/api/forward/load` | 加载正向模型（「可行性过滤」的判据，可热挂，不用重启进程） |
| POST | `/api/model-service/<action>` | 常驻模型服务 start / stop / restart |
| POST | `/api/predict` | **单步**逆合成预测（前端靠反复调它长出整棵路线树） |
| POST | `/api/search` | 一次搜到底的多步路线搜索（同步；`algorithm` 可选 `retro_star`/`bfs`） |
| POST | `/api/parse` | molfile / SMILES → 规范结构 + 分子式 / 分子量 |
| POST | `/api/depict` | SMILES → 带 2D 坐标的 molfile（画板载入用） |
| GET/POST | `/api/jobs` | 长任务提交与列表（`kind=predict|search`） |
| GET | `/api/jobs/<id>` | 查任务状态 / 取结果（`?since=` 增量拉日志） |
| POST | `/api/jobs/<id>/cancel` | 取消任务 |
| GET/POST | `/api/llm/status` | 大模型配置状态（`?probe=1` 会真实请求一次） |
| GET/POST | `/api/llm/models` | 该 key 下可用的模型 |
| GET/POST | `/api/llm/settings` | 端点配置列表 / 保存（多套绑定，立即生效，key 脱敏） |
| POST | `/api/llm/settings/active` | 切换当前端点配置 |
| POST | `/api/llm/keys/reset` | 重试失效 Key |
| POST | `/api/llm/analyze` | 工艺分析（一次性返回） |
| POST | `/api/llm/analyze/stream` | 工艺分析（**SSE 流式**） |
| POST | `/api/llm/conditions/stream` | 候选反应条件枚举（SSE，先选条件再深入分析） |
| POST | `/api/llm/followup/stream` | 工艺分析追问（SSE，带上下文自由回答） |
| GET/POST | `/api/feedback` | 「预测 vs 实测」反馈的写入与读回（落盘 `web/data/feedback.jsonl`） |

> 没有 `/api/health`。想快速判断服务活着没，用 `/api/models`（它同时能看出模型加载到哪一步）。
> 也没有任何 HTML 路由 —— 这是刻意的，前后端分离，后端只管数据。

---

## 9. 已知坑

- **模型跑在独立进程里，API 进程不碰模型**。`app.py` 里没有模型对象，只有
  `ModelServiceClient`。改搜索逻辑要改 `model_runtime.py`（两边共用），
  改完**必须重启模型服务**，否则 API 侧改了、跑搜索的进程还是旧代码。
- **首次加载很慢（冷启动约 2 分钟）**，因为要读几个 GB 权重。`/api/models` 会给
  `progress`（0~100，**估算值**）和 `phase` 文案，前端据此画进度条。
- **`RETRO_RULE_WORKERS` 调高会崩**，见第 5 节。
- **搜索预算 `max_calls` 是「本次新增」的调用数**，不是绝对值。`model_runtime.run_retro_search`
  里用「当前基线 + 本次预算」表达；因为模型服务是常驻的，若直接传绝对值，计数会跨搜索
  一路上涨，涨过上限后**每次搜索开局即 0 结果**。
- **`bootstrap/` 的导入顺序不能动**。`bootstrap.envfix`（DLL 搜索路径）与
  `bootstrap.limit_workers`（子进程数补丁）都是**副作用式**的 —— import 即生效，
  且**必须早于 torch / retrochimera / syntheseus**。把这几行注释掉或挪到后面，
  等于直接卸载修复，症状分别是 `_ssl` 加载失败和 WinError 1455。
  细节见 `bootstrap/__init__.py`。
- **`.gitignore` 的目录规则必须用前导斜杠锚定**（`/models/` 而不是 `models/`）。
  gitignore 的目录模式会匹配**任意层级**的同名目录 —— 写 `models/`（本意排除
  9GB 权重）会连带排掉 `core/retrochimera/models/`，而它在
  `retrochimera/__init__ → inference → models` 的导入链上。
  症状极其隐蔽：**本机完全正常**（retrochimera 是 `pip install -e .` 装的，
  缺的文件被本机副本补上了），但别人 clone 后 `import retrochimera` 直接
  ImportError。改 ignore 规则后请跑 `python scripts/verify_vendored.py` 自检。
- **`core/` 里的上游代码是本项目依赖的一部分**，删了服务起不来；
  它是 vendored 的，重新从 git 拉本仓即可恢复（不需要联网去上游 clone）。

---

## 10. 搜索算法

接口参数名是 **`algorithm`**（不是官方 CLI 的 `search_algorithm`），
取值 **`retro_star`**（默认）或 **`bfs`**，同步 `/api/search` 与异步
`/api/jobs(kind=search)` 都支持。

| key | 界面名 | 行为 | 什么时候选它 |
|---|---|---|---|
| `retro_star` | Retro* | 最佳优先：按「剩余步数估计 + 反应代价」挑最有望的节点先展开 | 默认。预算紧、要尽快出一条靠谱路线 |
| `bfs` | 广度优先 BFS | 逐层铺开，不做取舍 | 想看更多候选，或怀疑最优路线被启发式埋了 |

> 🔴 **MCTS / PDVN 虽然官方支持，但本项目没有开放**：它们的构造里要求 policy 模型
> （`assert self.policy is not None`），仓库里没有对应训练产物，缺了就起不来。
>
> 合法值的唯一判据是 `web/model_runtime.py` 的 `SEARCH_ALGORITHMS`；传别的值不会报错，
> 会静默退回 `retro_star`。前端下拉选项从 `GET /api` 的 `search_algorithms` 取，不要硬编码。

---

## 11. 许可

本项目（`web/`、`bootstrap/`、`scripts/`、`deploy/` 等新增代码）以 **MIT** 许可发布，
完整条款见 [`LICENSE`](LICENSE)，版权属 yijiu2025。

仓库中包含**第三方代码**，分发衍生作品时请一并遵守
[`NOTICE`](NOTICE) 的声明：

- **`core/`** —— RetroChimera v1.3.0 的源码副本（MIT，Copyright (c) Microsoft Corporation）。
  该目录是实质性代码，**必须**保留其原始许可声明（见 `core/LICENSE`）。
- **`core/retrochimera/dgllife/`**、**`core/retrochimera/opennmt/`** ——
  分别来自 dgl-lifesci 与 OpenNMT-py，原始声明保留在各自目录内。
- **运行时依赖** —— PyTorch(BSD-3)、PyG(MIT)、Syntheseus(MIT)、RDKit(BSD)、
  Flask(BSD-3) 等，各有独立许可。
- **模型权重** —— `models/` 下的 checkpoint **不含在本仓库**（9GB，需另行下载），
  其使用条款以[上游仓库](https://github.com/microsoft/retrochimera)为准。
  ⚠️ **MIT 许可覆盖的是代码，不自动涵盖模型权重。**

---

## 12. 免责声明

本项目是**研究用途**的工具。上游模型官方明确声明其预测存在
**幻觉（hallucination）风险** —— 即会生成化学上不合理、甚至不存在的反应。

> 🔴 **不要**将本工具的输出直接用于实际合成、药品生产或任何安全关键决策。
> 生成的路线必须经**具备资质的化学家**独立验证后方可使用。
> 相关声明见 [`core/README.md`](core/README.md) 的「幻觉风险」章节。

本项目按"现状"提供，不对任何用途作担保，包括适销性、特定用途适用性
及不侵权。详见 LICENSE 的免责声明条款。
