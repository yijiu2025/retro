# RetroChimera API 后端

[RetroChimera](https://github.com/microsoft/retrochimera)（微软的逆合成预测模型）之上的
一层 **Flask API**：单步逆合成预测、结构式解析/绘制、基于大模型的工艺分析。

> 🔴 **本服务只提供 API，不托管任何前端页面。**
> 浏览器直接打开根路径会返回一份 JSON 端点清单。
> 前端界面在另一个仓库：[retroweb](https://github.com/yijiu2025/retroweb)。

---

## 1. 它由哪几块组成

```
retrochimera/
├── 启动后端API.bat         ← Windows 一键启动（双击即用）
├── run_api.py             ← 命令行启动器（清掉干扰 sys.path 的注入路径）
├── web/                    ← API 服务主体（Flask，只出接口、不托管页面）
│   ├── app.py                Flask 应用 + 全部 API 路由
│   ├── llm.py                大模型调用 + SSE 流式分析
│   ├── jobhub.py             任务队列（长任务不阻塞请求）
│   ├── .env.example          配置模板（复制成 .env 再填）
│   └── requirements.txt      依赖清单（除上游本体）
├── bootstrap/              ← 运行时引导垫片（**必须在第三方 import 之前导入**）
│   ├── envfix.py             补 conda 的 DLL 搜索路径（修 `_ssl` 加载失败）
│   └── limit_workers.py      限制规则子进程数（修 WinError 1455）
├── scripts/                ← 命令行工具（诊断 / 预测 / 搜索 / 自修复）
│   ├── check_install.py      安装验证：加载模型跑一次真实预测
│   ├── predict.py            单步预测 CLI（置信度分级 + 出反应结构图）
│   ├── search.py             多步合成路线搜索 CLI
│   └── repair_deps.py        依赖自检自修（坏哪个重装哪个）
├── deploy/                 ← 部署物料
│   ├── environment-win.yml   conda 环境导出文件
│   └── wheels/               离线 Flask 依赖 wheel（断网时 .bat 靠它装）
├── core/                   ← 上游源码（**已 vendored 进本仓**，见其 VENDORED.md）
│   ├── retrochimera/         上游 Python 包本体
│   └── pyproject.toml        装它：cd core && pip install -e .
└── models/                 ← 模型权重，9GB，**不入库**
    ├── retrochimera_pistachio/
    └── retrochimera_uspto50k/
```

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
| 操作系统 | Windows 10/11 | 上游在 Linux 上也能跑，但本项目的启动脚本是 .bat |
| Python | **3.9 – 3.10** | 上游 `pyproject.toml` 限制；实测 3.9.7 可用 |
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

`requirements.txt` 里注释写明了每一步的注意事项，特别留意 **torch-geometric**：
它的 wheel 要从 `https://data.pyg.org/whl/torch-2.2.0+cu121.html` 拿，源目录名里是
`torch-2.2.0` 而不是 `2.2.2`（这是上游的笔误，照抄即可）。

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

1. **探鉴权** —— 直接打端点的模型列表：
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
| GET | `/` | 端点清单（JSON，自描述 —— 本服务不托管页面） |
| GET | `/api/models` | 可用模型列表 + 各自是否已加载 |
| POST | `/api/load` | 预热：提前把模型加载进内存 |
| POST | `/api/predict` | **单步**逆合成预测（前端靠反复调它长出整棵路线树） |
| POST | `/api/search` | 一次搜到底的多步路线搜索（前端**不用**，仅供脚本/调试） |
| POST | `/api/parse` | molfile / SMILES → 规范结构 + 分子式 / 分子量 |
| POST | `/api/depict` | SMILES → 带 2D 坐标的 molfile（画板载入用） |
| GET/POST | `/api/jobs` | 长任务提交与列表 |
| GET | `/api/jobs/<id>` | 查任务状态 / 取结果 |
| POST | `/api/jobs/<id>/cancel` | 取消任务 |
| GET | `/api/llm/status` | 大模型配置状态（`?probe=1` 会真实请求一次） |
| GET | `/api/llm/models` | 该 key 下可用的模型 |
| POST | `/api/llm/analyze` | 工艺分析（一次性返回） |
| POST | `/api/llm/analyze/stream` | 工艺分析（**SSE 流式**） |

> 没有 `/api/health`。想快速判断服务活着没，用 `/api/models`（它同时能看出模型加载到哪一步）。
> 也没有任何 HTML 路由 —— 这是刻意的，前后端分离，后端只管数据。

---

## 9. 已知坑

- **Windows 上模型必须跑在独立进程里**。Flask 直接加载模型会和 spawn 机制死锁，
  所以模型放在一个**非守护单线程** `multiprocessing.Process` worker 里，
  通过 `_REQ_Q` / `_RESP_Q` 两个队列通信。改这块之前先想清楚。
- **首次请求很慢**。模型冷启动要加载数 GB 权重，前端有"预热"按钮可以先把它叫醒。
- **`RETRO_RULE_WORKERS` 调高会崩**，见第 5 节。
- **`bootstrap/` 的导入顺序不能动**。`bootstrap.envfix`（DLL 搜索路径）与
  `bootstrap.limit_workers`（子进程数补丁）都是**副作用式**的 —— import 即生效，
  且**必须早于 torch / retrochimera / syntheseus**。把这几行注释掉或挪到后面，
  等于直接卸载修复，症状分别是 `_ssl` 加载失败和 WinError 1455。
  细节见 `bootstrap/__init__.py`。
- **`core/` 里的上游代码是本项目依赖的一部分**，删了服务起不来；
  它是 vendored 的，重新从 git 拉本仓即可恢复（不需要联网去上游 clone）。

---

## 10. 许可

`web/`、`bootstrap/`、`scripts/` 下的代码是本项目自己的；`core/` 下的 RetroChimera
源码版权归 Microsoft，按其仓库内的 `LICENSE` 使用（MIT，见 `core/LICENSE`）。
