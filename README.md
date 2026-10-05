# RetroChimera Web 后端

给 [microsoft/retrochimera](https://github.com/microsoft/retrochimera) 加的一层 **Flask Web API**，
对外提供单步逆合成预测、结构式解析/绘制、以及基于大模型的工艺分析。

前端在另一个仓库：[retroweb](https://github.com/yijiu2025/retroweb)。

---

## 1. 它由哪几块组成

```
retrochimera/
├── web/                     ← 本项目主体（我们自己写的）
│   ├── app.py                Flask 应用 + 全部 API 路由
│   ├── llm.py                大模型调用 + SSE 流式分析
│   ├── jobhub.py             任务队列（长任务不阻塞请求）
│   ├── .env.example          配置模板（复制成 .env 再填）
│   ├── requirements.txt      依赖清单（除上游本体）
│   └── 启动网页版.bat         Windows 一键启动
├── scripts/fetch_upstream.py 拉取上游源码（repo/ 不入库）
├── models/                  ← 模型权重，9GB，**不入库**
│   ├── retrochimera_pistachio/
│   └── retrochimera_uspto50k/
└── repo/                    ← 上游源码（独立 git 仓，**不入库**）
```

`models/` 和 `repo/` 都被 `.gitignore` 排除 —— 前者体积太大，后者是别人的仓库。
换机器时这两块都要重新准备（见第 3 节）。

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

再装其余依赖：

```bat
cd F:\retrochimera
pip install -r web\requirements.txt
```

`requirements.txt` 里注释写明了每一步的注意事项，特别留意 **torch-geometric**：
它的 wheel 要从 `https://data.pyg.org/whl/torch-2.2.0+cu121.html` 拿，源目录名里是
`torch-2.2.0` 而不是 `2.2.2`（这是上游的笔误，照抄即可）。

### 3.2 拉上游源码

```bat
python scripts\fetch_upstream.py
```

它会 clone `microsoft/retrochimera` v1.3.0 到 `repo/` 并执行 `pip install -e .`。
如果卡在 build 依赖上，改用：

```bat
python scripts\fetch_upstream.py --no-install
cd repo && pip install -e . --no-build-isolation
```

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
web\启动网页版.bat
```

它会自动找 Python（顺序：`RETRO_PYTHON` 环境变量 → 当前 conda 环境 → `.venv` →
默认 conda 路径 → PATH），然后拉起服务并打开浏览器。

想用它找别的解释器，先设一下：

```bat
set RETRO_PYTHON=D:\conda\envs\retrochimera\python.exe
```

### 方式二：命令行

```bat
conda activate retrochimera
cd F:\retrochimera
python web\app.py
```

或者用会话内启动器（会自动清掉干扰 `sys.path` 的注入路径）：

```bat
python web\_run_web.py
set RETRO_PORT=8011 && python web\_run_web.py
```

启动后监听 **http://127.0.0.1:8000**。

> 关窗口时刷出一堆红色 `BrokenPipeError` 是**正常的**（worker 清理），不影响结果。

---

## 5. 配置项

全部走环境变量，优先级：**真实环境变量 > `web/.env` > 仓库根 `.env`**；空值视为未设置。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RETRO_PORT` | `8000` | 监听端口 |
| `RETRO_RULE_WORKERS` | `4` | 规则应用进程数。16（全部核心）会吃 70GB 内存并崩（WinError 1455），别乱调高 |
| `RETRO_MODELS_DIR` | `<root>/models` | 模型根目录 |
| `RETRO_MODEL_PISTACHIO` | — | 单独指定 pistachio 模型路径 |
| `RETRO_MODEL_USPTO50K` | — | 单独指定 uspto50k 模型路径 |
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
| GET | `/api/health` | 健康检查 |
| GET | `/api/models` | 可用模型列表 |
| POST | `/api/predict` | **单步**逆合成预测（前端靠反复调它长出整棵路线树） |
| POST | `/api/parse` | molfile / SMILES → 规范结构 + 分子式 / 分子量 |
| POST | `/api/depict` | SMILES → 带 2D 坐标的 molfile（画板载入用） |
| GET | `/api/llm/status` | 大模型配置状态（`?probe=1` 会真实请求一次） |
| GET | `/api/llm/models` | 该 key 下可用的模型 |
| POST | `/api/llm/analyze` | 工艺分析（SSE 流式返回） |

---

## 9. 已知坑

- **Windows 上模型必须跑在独立进程里**。Flask 直接加载模型会和 spawn 机制死锁，
  所以模型放在一个**非守护单线程** `multiprocessing.Process` worker 里，
  通过 `_REQ_Q` / `_RESP_Q` 两个队列通信。改这块之前先想清楚。
- **首次请求很慢**。模型冷启动要加载数 GB 权重，前端有"预热"按钮可以先把它叫醒。
- **`REQ_RULE_WORKERS` 调高会崩**，见第 5 节。
- **`repo/` 里的上游代码是本项目依赖的一部分**，删了服务起不来，重新跑 `scripts/fetch_upstream.py` 即可。

---

## 10. 许可

`web/` 下的代码是本项目自己的；`repo/` 下的 RetroChimera 源码版权归 Microsoft，
按其仓库内的 LICENSE 使用（MIT）。
