# 上游源码（vendored）

本目录（`core/`）是 [microsoft/retrochimera](https://github.com/microsoft/retrochimera)
的**源码副本**，由本项目直接纳入版本管理 —— 部署时**不再需要 `git clone`**。

- **来源**：https://github.com/microsoft/retrochimera
- **版本**：`v1.3.0`（commit `bf5ec59`）
- **纳入方式**：`git archive v1.3.0 | tar -x`（只含被上游跟踪的文件；不带 `.git`）

## 目录内容

| 路径 | 说明 |
| --- | --- |
| `retrochimera/` | 上游 Python 包本体（模型与化学逻辑） |
| `tests/` | 上游自带的单元测试 |
| `pyproject.toml` | 打包与依赖声明 —— `pip install -e .` 就靠它 |
| `environment.yml` | 上游的 conda 环境定义（仅供参考，实际以 `web/requirements.txt` 为准） |
| `LICENSE` | 上游许可证（MIT），版权归 Microsoft |
| `*.zh-CN.md` | 各 md 的中文译本（**本仓新增文件**，不是上游文件；英文原件保留不动） |

## 怎么用

安装（可编辑模式，后续改这里的代码立即生效）：

```bat
cd core
pip install -e .
```

> 若卡在 build 依赖上：
> ```bat
> pip install -e . --no-build-isolation
> ```

验证：

```bat
python -c "import retrochimera; print(retrochimera.__file__)"
```

路径应当落在本目录下的 `retrochimera/__init__.py`。

> 装漏了也能跑：`web/app.py` 启动时会把本目录挂到 `sys.path` **末尾**兜底，
> 优先级低于任何已安装版本，不会覆盖显式安装。

## 与 `web/` 的关系

```
core/              ← 上游：模型推理与化学逻辑（本目录，别人写的）
web/               ← 本项目：Flask API 封装（我们写的），import retrochimera
```

`web/app.py` 里的 worker 进程会 `import retrochimera` 来跑预测。
**`web/` 只做 API 暴露，不含任何上游逻辑的拷贝。**

## 本仓对上游做的本地化改动

除下表外，本目录的**已有文件**与上游 `v1.3.0` **内容一致**（表中 `*.zh-CN.md` 属本仓新增文件，不是对上游文件的改动）。

> ⚠️ 关于「一致」的准确含义：本仓的 `.gitattributes` 声明了 `*.md text eol=lf`，
> 而上游仓库里部分 `.md`（如 `SUPPORT.md`）是 CRLF。所以**换行符被规范化过**，
> 用 `git hash-object` 与上游 blob SHA 逐个比对时，这些文件会显示为「不一致」，
> 但那只是 `\r\n` → `\n` 的差异，内容完全相同。校验方法见下方「如何自检」。
>
> 这也是本仓新增 `.gitattributes` 的副作用，属预期行为。

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `pyproject.toml` | `dynamic = ["version"]` 改为 `version = "1.3.0"`，并移除 `[tool.setuptools_scm]` | 本仓是没有 `v1.3.0` tag 的源码副本，scm 推导会装成 `0.0.0` |
| `README.zh-CN.md`、`retrochimera/README.zh-CN.md` 等 **9 个** `*.zh-CN.md` | **新增**中文译本（英文原件保持不动） | 上游文档为英文，团队阅读成本高；就地并列存放，原版保留以便同步上游时做 diff |

> 同步新版本上游时，记得把这条改动重新应用一遍。

## 更新上游版本

```bat
git clone --depth 1 --branch v1.3.1 https://github.com/microsoft/retrochimera.git _tmp_up
```

然后用它覆盖本目录（保留本文件）：

```bat
robocopy _tmp_up . /E /XD .git
```

### ⚠️ 覆盖后必须自检（否则漏文件你不会知道）

`robocopy` 只会**新增和覆盖**已存在的文件。如果新版本新增了源码文件、
而你本仓的 `.gitignore` 恰好把它误伤了（比如目录名撞上 `models/`），
它会「复制成功但没进 git」—— 症状是别人 clone 后 `ImportError`，
而你本机一切正常（因为本地文件还在，且包是以 `-e` 装的）。

**每次同步完，按下面三步查一遍：**

```bat
:: 1) 有多少文件被 git 忽略（确认没有源码混进去）
git status --ignored --porcelain -uall | findstr /B "!!" | findstr /V "__pycache__ .pyc .egg-info models\ web\data .tmp- .workbuddy"

:: 2) 与上游逐文件比对（脚本化，见下）
python scripts/verify_vendored.py
```

> 上面第 1 步的过滤串是**白名单式**的：只有全部命中才算正常。
> 若看到任何 `.py` 结尾的行，说明有源码被误伤，立刻检查 `.gitignore`。

仓库里提供了 `scripts/verify_vendored.py` 做第 2 步：拉取上游 `v1.3.0`
的文件树与 blob SHA，与本仓逐个比对，报告**缺失**、**未跟踪**、
**内容不一致**三类问题，并自动忽略换行符与已在
「本地化改动」表中登记的文件。

## 为什么不是 git submodule

- 部署机器往往没装 git，或用的是内网镜像；vendored 后**开箱即用**。
- 上游代码量小（约 1.1MB / 100 余文件），纳入本仓的代价可以接受。
- 代价是**不会自动跟随上游更新** —— 需要手动同步（见上）。
