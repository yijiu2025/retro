# 上游源码（vendored）

本目录是 [microsoft/retrochimera](https://github.com/microsoft/retrochimera) 的**源码副本**，
由本项目直接纳入版本管理 —— 部署时**不再需要 `git clone`**。

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

## 怎么用

安装（可编辑模式，后续改这里的代码立即生效）：

```bat
cd upstream
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

## 与 `web/` 的关系

```
upstream/          ← 上游：模型推理与化学逻辑（本目录，别人写的）
web/               ← 本项目：Flask API 封装（我们写的），import retrochimera
```

`web/app.py` 里的 worker 进程会 `import retrochimera` 来跑预测。
**`web/` 只做 API 暴露，不含任何上游逻辑的拷贝。**

## 更新上游版本

```bat
git clone --depth 1 --branch v1.3.1 https://github.com/microsoft/retrochimera.git _tmp_up
```

然后用它覆盖本目录（保留本文件）：

```bat
robocopy _tmp_up . /E /XD .git
```

## 为什么不是 git submodule

- 部署机器往往没装 git，或用的是内网镜像；vendored 后**开箱即用**。
- 上游代码量小（约 1.1MB / 100 余文件），纳入本仓的代价可以接受。
- 代价是**不会自动跟随上游更新** —— 需要手动同步（见上）。
