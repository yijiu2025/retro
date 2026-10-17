# Pull Request

## 改动类型

- [ ] `feat` 新功能
- [ ] `fix` 缺陷修复
- [ ] `perf` 性能优化
- [ ] `refactor` 重构（行为不变）
- [ ] `test` 测试
- [ ] `docs` 文档
- [ ] `chore` 构建 / 工具 / 杂项
- [ ] `ci` CI 配置
- [ ] `revert` 回滚

## 关联 Issue

closes #<!-- issue 编号，无则删掉本节 -->

## 动机与背景

<!-- 解决什么问题；不写「是什么」，写「为什么」。 -->

## 主要改动

<!-- 按模块列，避免逐行复述 diff：
- web/app.py：…
- core/retrochimera/…：…
-->

## 测试证据

本地跑过的命令与结果（CI 会重复一遍，但本地先绿是对 reviewer 的基本尊重）：

- [ ] `ruff check --config core/pyproject.toml core web scripts desktop`
- [ ] `cd core && pytest`（改了 core/ 或数据管线时必跑）
- [ ] `node scripts/check-docs-index.mjs docs`（新增 / 改名文档后必跑）
- [ ] 起服务手动验证（改了 API / 模型服务时必跑，注明端点与请求）

## 自检清单

- [ ] 未提交 `models/`、`.env`、`__pycache__/` 等应被忽略的文件
- [ ] `.gitignore` 目录规则都带前导斜杠锚定仓库根（防误伤 `core/retrochimera/models/`）
- [ ] 改了 `core/`：[core/VENDORED.md](core/VENDORED.md) 的差异登记已同步
- [ ] 改了 `web/model_runtime.py` / `web/model_server.py`：已注明「需重启模型服务」
- [ ] 加了依赖：`core/environment.yml`（或对应 requirements）已同步，CI 安装口径未破
- [ ] 新端点已随手登记进端点清单；错误响应带可读消息
- [ ] 无新增明文密钥；日志无敏感信息
- [ ] 提交信息遵循 Conventional Commits，一次提交只做一件事

## 给 Reviewer 的提示

<!-- 你最没把握的点、希望重点看的部分；没有就删掉本节。 -->
