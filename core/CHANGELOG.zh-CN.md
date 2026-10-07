> 📄 本文件是 [`CHANGELOG.md`](CHANGELOG.md) 的中文译本（**非官方**，仅供本仓阅读参考）。如遇歧义，以英文原版为准。

# 变更日志

本项目所有值得记录的变更都记在这个文件里。

格式遵循 [Common Changelog](https://common-changelog.org/)，
项目遵循[语义化版本（Semantic Versioning）](https://semver.org/spec/v2.0.0.html)。

## [未发布]

## [1.3.0] - 2026-09-21

### 变更

- 让模板抽取更健壮（[#28](https://github.com/microsoft/retrochimera/pull/28)）（[@kmaziarz]）
- 改进数据过滤脚本（[#30](https://github.com/microsoft/retrochimera/pull/30)）（[@kmaziarz]）
- 移除对 `syntheseus-root-aligned` 的依赖，并把相关工具 vendored 进来（[#27](https://github.com/microsoft/retrochimera/pull/27)）（[@lgeiger]）
- SMILES Transformer 训练时不再加载模板库（[#29](https://github.com/microsoft/retrochimera/pull/29)）（[@lgeiger]）
- 把 `protobuf` 依赖升级到 5.29.6（[#25](https://github.com/microsoft/retrochimera/pull/25)）（[@kmaziarz]）
- 为嵌套 dataclass 字段使用默认工厂（[#31](https://github.com/microsoft/retrochimera/pull/31)）（[@lgeiger]）
- 移除未使用的依赖并解除对 W&B 的版本钉死（[#37](https://github.com/microsoft/retrochimera/pull/37)）（[@lgeiger]）

### 新增

- 支持模型微调（[#21](https://github.com/microsoft/retrochimera/pull/21)、[#24](https://github.com/microsoft/retrochimera/pull/24)）（[@kmaziarz]）
- 实现用于集成（ensembling）的共识模式（[#26](https://github.com/microsoft/retrochimera/pull/26)）（[@kmaziarz]）
- 允许集成消费以可续跑（resumable）模式产出的评估结果（[#32](https://github.com/microsoft/retrochimera/pull/32)）（[@kmaziarz]）
- 对外暴露正向模型类（[#23](https://github.com/microsoft/retrochimera/pull/23)）（[@kmaziarz]）
- 新增配置项，用于设置 torch fp32 矩阵乘法的精度（[#36](https://github.com/microsoft/retrochimera/pull/36)）（[@lgeiger]）

### 修复

- 修复规则应用服务（rule application server）的进程泄漏（[#33](https://github.com/microsoft/retrochimera/pull/33)）（[@kmaziarz]）

## [1.2.0] - 2026-06-17

### 变更

- 加速定位（localization）模型的加载与推理（[#15](https://github.com/microsoft/retrochimera/pull/15)、[#16](https://github.com/microsoft/retrochimera/pull/16)）（[@kmaziarz]）
- 加速 SMILES Transformer 模型推理（[#17](https://github.com/microsoft/retrochimera/pull/17)）（[@kmaziarz]）
- 并行运行各子模型的推理（[#18](https://github.com/microsoft/retrochimera/pull/18)）（[@kmaziarz]）
- 去掉显式的 TensorBoard 依赖（[#12](https://github.com/microsoft/retrochimera/pull/12)）（[@kmaziarz]）

### 新增

- 为基于模板的模型开放 `num_processes` 设置（[#13](https://github.com/microsoft/retrochimera/pull/13)）（[@kmaziarz]）

### 修复

- 修复一个 `weakref` 缺陷，该缺陷导致基于模板的模型无法被垃圾回收（[#14](https://github.com/microsoft/retrochimera/pull/14)）（[@kmaziarz]）

## [1.1.0] - 2026-03-12

### 变更

- 避免在集成进 `syntheseus` 时出现的循环导入（[#6](https://github.com/microsoft/retrochimera/pull/6)）（[@kmaziarz]）
- 让子模型类基于 `syntheseus` 的 `ExternalBackwardReactionModel`（[#7](https://github.com/microsoft/retrochimera/pull/7)）（[@kmaziarz]）
- 用面向外部的名称暴露各子模型类（[#9](https://github.com/microsoft/retrochimera/pull/9)）（[@kmaziarz]）

## [1.0.0] - 2025-11-30

:seedling: 首次公开发布。

[Unreleased]: https://github.com/microsoft/retrochimera/compare/v1.3.0...HEAD
[1.0.0]: https://github.com/microsoft/retrochimera/releases/tag/v1.0.0
[1.1.0]: https://github.com/microsoft/retrochimera/releases/tag/v1.1.0
[1.2.0]: https://github.com/microsoft/retrochimera/releases/tag/v1.2.0
[1.3.0]: https://github.com/microsoft/retrochimera/releases/tag/v1.3.0

[@kmaziarz]: https://github.com/kmaziarz
[@lgeiger]: https://github.com/lgeiger
