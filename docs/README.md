# retro 文档总入口

> **写文档前先读本文决定放哪；找文档也从本文进。** 本文是 `docs/` 的唯一入口，
> 逐目录登记「路径 + 一句话用途」。规范见 fullstack-rules → docs-conventions.md。

## 目录地图

| 目录            | 内容                                             |
| --------------- | ------------------------------------------------ |
| `releases/`     | 版本发布说明（升级必读、破坏性变更、手动步骤）     |

## 放置决策顺序

1. **版本发布说明**（某版升级要注意什么）→ `docs/releases/`，文件名 `vX.Y.Z.md`
2. **部署 / 运维操作手册** → 仓库根目录的使用说明类文档（`使用说明.md`、`上游文档导读.md`）
3. **core/ 包内文档**（CHANGELOG / DEVELOPMENT / VENDORED 等）→ 与代码共置在 `core/`，不集中
4. **API 文档** → 不进 docs，随代码走（路由文件头注释）

## 根目录文档指路

- `README.md` / `README.en.md`：项目总览（中英双语）
- `使用说明.md`：本机启动与使用
- `上游文档导读.md`：core/ 与上游 microsoft/retrochimera 的关系导读
- `core/VENDORED.md`：本仓相对上游改了什么
- `core/DEVELOPMENT.md`：core/ 开发指南（环境、测试）
