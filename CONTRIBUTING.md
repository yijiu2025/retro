# 贡献指南

感谢你愿意为 retro 出力。这份指南帮你少踩坑：这个仓库是什么、怎么把环境跑起来、哪些红线不能碰、提交与 PR 走什么流程。**动手前花五分钟读完，尤其是「代码红线」一节。**

## 这个仓库是什么

逆合成预测服务的**后端部分**：基于 RetroChimera 模型的预测 API、常驻模型服务、桌面一键打包工具链，以及一份 vendored 的上游内核（`core/`）。**不托管前端页面**（只返回 JSON）——界面在另一个仓库 [retroweb](https://github.com/yijiu2025/retroweb)。

```
retro/
├── core/            # vendored 上游包 retrochimera（pip 包本体，含测试与环境定义）
├── web/             # API 服务（Flask）：预测 / 搜索 / LLM 辅助 / 任务中心
├── scripts/         # 运维脚本（模型下载、安装自检、文档校验）
├── desktop/         # 桌面一键打包工具链（CPU/GPU 可切换）
├── deploy/          # 部署资产（systemd / wheels / 环境定义）
├── models/          # 模型权重（9GB，🔴 绝不入库）
└── docs/            # 发布说明（入口见 docs/README.md）
```

- 技术栈：Python 3.9 + Flask + conda（pytorch 2.2.2 / rdchiral_cpp / pyg 2.5.2）
- 协议：[MIT](LICENSE)
- `core/` 与上游的关系（改了什么、为什么）：[core/VENDORED.md](core/VENDORED.md)

## 一、上手

前置依赖：conda（Miniconda / Anaconda）、Git、约 15GB 磁盘（CUDA 版 torch 约 3GB + 模型权重另算）。

```bash
git clone https://github.com/yijiu2025/retro.git
cd retro

# 1. 参考环境（单一来源 = core/environment.yml，与 CI 同口径）
conda env create -f core/environment.yml
conda activate retrochimera

# 2. 安装包本体（editable + all extras）
pip install -e "core[all]"

# 3. 起服务（模型权重放 models/，不入库）
python web/app.py          # 或双击 启动后端API.bat
# 浏览器打开 http://127.0.0.1:8000 只返回端点清单 JSON
```

> **Windows 注意**：直接用绝对路径调用 `python.exe` 而未激活 conda 环境时，`_ssl` 会因找不到 OpenSSL DLL 报错——`web/app.py` 头部已做 DLL 预加载修正；遇到 `DLL load failed while importing _ssl` 先检查是不是绕过了激活步骤。

## 二、常用命令

| 命令                                                                  | 作用                                           |
| --------------------------------------------------------------------- | ---------------------------------------------- |
| `ruff check --config core/pyproject.toml core web scripts desktop`    | 静态检查（CI 规范层同款，改代码前后各跑一次）   |
| `cd core && pytest`                                                   | 全量单测（含包内 doctest，与 CI 同口径）        |
| `node scripts/check-docs-index.mjs docs`                              | 文档入口校验（新增/改名文档后必跑）             |
| `python scripts/check_install.py`                                     | 安装完整性自检                                 |

## 三、代码红线

- 🔴 **`models/` 绝不入库**（9GB 权重）；`.gitignore` 的目录规则一律带**前导斜杠**锚定仓库根——`models/` 会误伤 `core/retrochimera/models/` 与 `core/tests/models/`，这个事故有过专帖（v1.1.1）
- 🔴 **改 `core/` 先读 [core/VENDORED.md](core/VENDORED.md)**：vendored 副本与上游的差异必须登记；纯上游缺陷优先上游修复
- 🔴 **改 `web/model_runtime.py` / `web/model_server.py` 后必须重启模型服务**：模型跑在独立常驻进程，API 进程不加载模型（v1.1.0 立此规）
- 🔴 **不提交真实密钥**：`.env` / `web/.env` 已入 `.gitignore`，只提交 `.example` 模板
- 🔴 **环境定义单一来源是 `core/environment.yml`**：加依赖先改它，本机 env 漂移不算数，以 CI 口径为权威
- DataLoader `num_workers > 0` 在 Windows + DDP 子重启组合下有已知限制（守护进程不允许再有子进程）；相关测试在 Linux CI 跑，本地只做参考

## 四、提交与 PR

遵循 Conventional Commits（`<type>(<scope>): <subject>`），一个提交只做一件事：

| 类型       | 示例                                        |
| ---------- | ------------------------------------------- |
| `feat`     | `feat(api): 新增批量预测端点`               |
| `fix`      | `fix(model): 修复模型服务重启后句柄泄漏`     |
| `refactor` | `refactor(core): 收拢导出面`                |
| `docs`     | `docs: 补 v1.2.0 发布说明`                  |
| `test`     | `test: 补 token 校验边界用例`               |
| `ci`       | `ci: 接入家族规范 CI 骨架`                  |
| `chore`    | `chore: ruff 静态检查清理`                  |

流程：从 `main` 切短支线（`feat/*` / `fix/*` / `chore/*` …）→ 小步提交 → 推送开 PR → CI 绿 → 合并（merge commit 保留拓扑）→ 删支线。**禁止直推 `main`**（受分支保护）。

PR 标题同样要符合 Conventional Commits——squash 合并时它会成为 main 上的提交信息。

## 五、发版

tag 格式 `vX.Y.Z`（annotated，message 写版本摘要）；发布说明落 `docs/releases/vX.Y.Z.md`
并登记进 `docs/releases/README.md`。升级必读事项（手动步骤、破坏性变更）写进入发布说明，
不要只写在 commit 里。

## 六、报告问题

- 缺陷 / 功能建议：走 [issue 模板](.github/ISSUE_TEMPLATE)（选缺陷报告或功能建议）
- 安全漏洞：**勿开公开 issue**，走私密渠道，见 [SECURITY.md](SECURITY.md)
- `core/` 包的缺陷也可向上游报告：<https://github.com/microsoft/retrochimera/issues>
