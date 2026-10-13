# RetroWeb 桌面版打包套件

把 `retroweb`（Vue 前端）+ `retro`（Flask + RetroChimera 模型后端）打成
**一个解压即用的便携包**：双击 `RetroWeb.exe` 就是原生窗口应用，不经过浏览器，
不出命令行；模型权重（9GB）不进包，进软件后在「模型设置」页下载，也可以手动放置；
CPU/GPU 可切换，GPU 版 PyTorch（约 2.2GB）可稍后按需安装。

## 产物长什么样

```
dist-pkg/
├─ RetroWeb.exe          ← 双击启动（PyInstaller 小壳，本身 8.4MB，无控制台）
├─ _internal/            ← retro 仓库代码 + 前端 dist + 桌面模块
│   ├─ web/                Flask 应用（含本次新增的 5 个桌面模块）
│   ├─ frontend/           retroweb 构建产物（约 35MB）
│   ├─ bootstrap/ scripts/ core/
│   └─ desktop_app.py      桌面编排入口（窗口 + Flask + 生命周期）
├─ python39/             ← 便携 Python 3.9 + 全部依赖（CPU 版 torch）
│   └─ Lib/site-packages/   + sitecustomize.py（CPU/GPU 路径切换在这生效）
├─ data/                 ← 全部可变数据
│   ├─ config.json         计算模式 / 规则子进程数 / 端口
│   ├─ models/             模型权重（下载或手动放入）
│   ├─ gpu-pkgs/           GPU 加速包（按需安装，~2.2GB）
│   ├─ downloads/ wheels-gpu/ model-service/ logs/ outputs/
└─ 使用说明.txt
```

**体积（实测）**：
- **CPU 版** `RetroWeb-1.0-win64.7z`：解压后 1.74GB（不含模型），7z 压缩后 **233MB**。
- **GPU 版（cu121）** `RetroWeb-1.0-gpu-win64.7z`：在 CPU 版基础上**预装 CUDA 12.1 版
  PyTorch 全家桶**（`data/gpu-pkgs`，其中 `torch-2.2.2+cu121` 单 wheel 即 2.28GB，
  含 CUDA 12.1 运行时），解压后约 4GB，7z 压缩后约 1.3–1.6GB。
- 模型按需：**pistachio 4.2GB** / **uspto50k 0.3GB** / **uspto_full 4.6GB** /
  forward 0.8GB（见 `data/models/把模型放这里.txt` 与软件内「模型设置」页）。

> GPU 版与 CPU 版**二进制同源、可互转**：GPU 版只是把 cu121 wheels 装进了
> `data/gpu-pkgs` 并设 `compute=auto`。想自己从 CPU 版造 GPU 版，跑
> `python bake_gpu.py --pkg <解压目录>` 即可（复用软件内同一条安装路径）。

## 交付物清单（`desktop/dist/`）

| 文件 | 说明 |
|---|---|
| `RetroWeb-1.0-win64.7z` | **CPU 版**：解压后双击 `RetroWeb.exe`，无命令行窗口，one-dir，换机器拷文件夹即用。 |
| `RetroWeb-1.0-gpu-win64.7z` | **GPU 版（cu121）**：同上，但已预装 CUDA 12.1 版 PyTorch。需本机有 NVIDIA 显卡 + 驱动。 |

两份都**不带模型**（按设计后下载 / 手动放）。模型来源见 `data/models/把模型放这里.txt`：
- 官方模型网盘（百度）：`https://pan.baidu.com/s/5D5zka_-76R4bFa3YZqtMxQ`
- 软件内「模型设置」页也有「官方模型网盘（百度）」可点击/复制链接，以及 figshare 直连「下载」按钮。

## 怎么构建

构建机：Windows x64，能访问 pypi / download.pytorch.org / data.pyg.org /
python.org，磁盘 ≥ 8GB 空闲，已装 Node.js LTS。

```bash
# 1. 准备两个仓库（与桌面套件放同级目录）
git clone https://github.com/yijiu2025/retroweb
git clone https://github.com/yijiu2025/retro

# 2. 一键构建（自动：下载便携 Python → 装依赖 → 打前端补丁并构建 →
#            装配 _internal → 冻结启动器 → 7z 压缩）
python build_package.py --repo ../retro --web ../retroweb
```

常用参数：

| 参数 | 用途 |
|---|---|
| `--out D:\pkg` | 指定输出目录（默认 `./dist-pkg`） |
| `--skip-deps` | site-packages 已装好时跳过（调试阶段省 10 分钟） |
| `--skip-frontend` | dist 已构建时跳过（需先手动应用前端补丁） |
| `--work D:\cache` | 下载缓存目录（embeddable zip / get-pip.py） |

## 冒烟测试（不起窗口，验证整条链路）

```bash
python smoke_test.py dist-pkg
```

依次验证：headless 起桌面进程 → API 就绪 → 静态托管 / SPA fallback →
GPU 状态 → 下载 uspto50k（约 285MB，断点续传）→ 加载模型 → 阿司匹林单步
逆合成预测。脚本自选空闲端口并通过 `RETRO_PORT` 传给桌面进程。

🔴 构建期易踩的坑：`desktop_app.py` 必须落在 `_internal/` **根下**（它按
`__file__` 推 `_HERE=_internal` 装配 sys.path，launcher 也固定拉起
`_internal\desktop_app.py`）；其余桌面模块进 `_internal/web/`。
放错位置双击 exe 只会弹"文件缺失"，且不装模型发现不了。

依赖安装是**写死**的（与 `retro/web/requirements.txt` 注释的口径一致）：
主索引用 PyTorch 官方 CPU 源、附加 pypi、PyG 家族走 `data.pyg.org` 的
`torch-2.2.0+cpu` 目录（注意目录名是 2.2.0 不是 2.2.2，上游笔误）。
🔴 必须**单条 pip 命令**装完：pip 的 `--target` 解析依赖时不参考目标目录
已装内容，分条安装时 `syntheseus-graphium` 未锁版本的 torch 依赖会把锁好的
2.2.2+cpu 顶成最新版（实测踩过）。构建后自动剥离 `torch/include` 等约 300MB
开发残留。

## 怎么用（最终用户）

1. 解压 zip/7z 到任意位置（**整个文件夹一起拷**，别只拷 exe；路径别带特殊字符）。
2. 双击 `RetroWeb.exe`。首次启动没有模型，窗口会打开「模型设置」页提示下载。
3. 点「下载」：pistachio 约 4.2GB（推荐）或 uspto50k 约 0.3GB（快，质量低一档）。
   下载可断点续传、可取消，关掉软件再开会接着下。
4. 下完点「加载」，40~90 秒后回到工作台使用。
5. 想手动放模型：按 `data\models\把模型放这里.txt` 的目录名规则解压即可。
   模型网盘（百度网盘，官方提供）：
   https://pan.baidu.com/s/5D5zka_-76R4bFa3YZqtMxQ
   从网盘下对应模型的压缩包，解压到 `data\models\` 下对应目录名即可。
6. CPU/GPU 在「模型设置」页切；GPU 加速包在同一页按需下载（约 2.2GB，
   装完点「检测本机 GPU」，有 NVIDIA 驱动才会真正走 GPU）。

## 关键设计（为什么这么做）

**1. 重栈不过 PyInstaller。** torch + PyG + RDKit + syntheseus 这一摞是
PyInstaller 的重灾区（hidden imports、DLL 搜索路径、数据文件、spawn 子进程）。
本方案里它们跑在**便携 Python 的真实 site-packages** 上，打包器完全无感；
被冻结的只有一个 10MB 的启动器壳。模型服务由 API 进程用
`python39\python.exe _internal\web\model_server.py` 直接拉起
（`CREATE_NO_WINDOW`，不弹窗），multiprocessing 的规则子进程也天然继承无控制台。

**2. CPU/GPU 靠 sitecustomize 切换，不重打包。** `python39/Lib/site-packages/
sitecustomize.py` 在每个 Python 进程启动时读 `data/config.json` 的 `compute`
字段，决定是否把 `data/gpu-pkgs` 前置到 `sys.path`。因为整棵进程树
（模型服务、规则子进程、pip 子进程）都经过 site 初始化，路径视图天然一致。
GPU 包安装 = 下载 cu121 wheels → `pip install --no-index --no-deps
--target data/gpu-pkgs`，与 CPU 版共存，互不覆盖。

**3. 同源单端口。** Flask 同时托管 `dist/` 静态文件和 `/api/*`（新增
`desktop_routes.py`），pywebview 窗口只开 `http://127.0.0.1:<随机端口>`。
随机端口避开 8000/8001 被占用；同源免 CORS；免登录档（`VITE_AUTH_BYPASS=true`）
免掉 oauth21 依赖，单机应用不需要它。

**4. 数据全落 data/。** 模型、GPU 包、下载状态、配置、日志都在 `<包根>/data/`，
换机器只拷这一个文件夹就能迁移；想换模型盘符设 `RETRO_MODELS_DIR` 即可。

## 对上游仓库的改动清单

### retro（后端）

| 文件 | 改动 |
|---|---|
| `web/app.py` | +1 处：挂载 `desktop_routes`（try/except ImportError，API-only 部署零影响） |
| `web/model_client.py` | +1 处：桌面模式下模型服务子进程改 `CREATE_NO_WINDOW` |
| `web/desktop_routes.py` | 新增：静态托管 + 模型下载/删除 + GPU 管理 + 配置 + 打开文件夹 |
| `web/model_download.py` | 新增：断点续传下载（复用 scripts/download_model.py 的注册表与解压规则） |
| `web/gpu_manager.py` | 新增：cu121 wheel 解析/下载/离线安装/探测 |
| `web/config_store.py` | 新增：桌面路径与 config.json |
| `web/downloader.py` | 新增：requests 流式下载原语 |
| `web/desktop_app.py` | 新增：窗口 + Flask + 生命周期编排 |
| `web/sitecustomize.py` | 新增：装进便携 Python，CPU/GPU 路径切换 |

### retroweb（前端）

| 文件 | 改动 |
|---|---|
| `src/views/ModelSettingsView.vue` | 新增：模型设置页 |
| `src/api/retro/desktop.ts` | 新增：设置页 API + SSE 订阅 |
| `src/router/index.ts` | +1 条路由 `/settings/models` |
| `src/components/layout/AppShell.vue` | +1 个导航项 + 1 个图标 |

这些改动都通过 `build_package.py` 里的**精确字符串补丁**应用（匹配不上会报错，
不会静默漂移）。要合回上游仓库时，把 `patches/` 与 `patches-frontend/` 里的
新文件拷进去、按 `PATCH_*` 常量做同样替换即可。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| 双击没反应 | 看 `data/logs/launcher.log`；多半是 python39 或 _internal 没拷全 |
| 窗口打开但一直转圈 | `data/logs/desktop.log` 看 API 是否就绪；端口随机一般不会冲突 |
| 模型下载中断 | 正常，重下同一模型即续传（zip 留在 `data/models/`） |
| 模型状态显示「已中断」但文件在 | 上次下载/安装中被杀进程；重新点「下载」会校验已下部分并续传，装好的直接可用 |
| 下载完校验失败 | figshare 偶尔抽风；删掉半截 zip 重下 |
| 切 GPU 后没生效 | 要在「模型设置」页点「保存并重启模型服务」；已加载的模型必须重启才换后端 |
| GPU 检测不可用 | 装完 GPU 包后点「检测本机 GPU」看原因（无 NVIDIA 驱动/显卡过老都会不可用） |
| 16GB 内存机器卡 | `data/config.json` 把 `rule_workers` 降到 2（默认已设 2） |
| 想换模型目录 | 退出软件，设环境变量 `RETRO_MODELS_DIR`，重启 |
| 模型加载报 `No module named 'pkg_resources'` | 便携 Python 的 setuptools 被升到 ≥81（已移除 pkg_resources）。重装/重建时务必锁 `setuptools==80.9.0`；已发生的可进 `python39/` 目录执行 `python -m pip install --target Lib/site-packages "setuptools==80.9.0"` |
| GPU 包下载时 `torch-2.2.2+cu121` 显示 2.28GB | 正常。该 wheel 自带 CUDA 12.1 运行时，单文件就是 2.28GB；全部 GPU wheels 约 2.5–2.7GB，断点续传，慢但能续 |

## 已知边界

- **只承诺 Windows x64**（Linux/macOS 未测；代码路径全相对，理论上可移植，
  但 graphviz、WebView2、CREATE_NO_WINDOW 都是 Windows 假设）。
- **免登录档**：单机应用注定了没有多用户体系，权限模型退化为开发档。
- **CPU 推理慢**：单步预测数十秒是正常现象，不是坏了。
- **WebView2 运行时**：Win10 21H2+/Win11 一般自带；极旧的系统需要单独装
  Evergreen 运行时（微软官网，一次性）。
