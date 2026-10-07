> 📄 本文件是 [`README.md`](README.md) 的中文译本（**非官方**，仅供本仓阅读参考）。如遇歧义，以英文原版为准。

本 README 描述我们当前预印本所用**完整模型流水线**的各个步骤与脚本：从训练数据开始，到最终集成（ensembling）结束。如果数据已经清洗好、或已经划分好，前两步可以跳过。

这条流水线**刻意只做最少的过滤，以尽可能纳入更多反应**。如果你想更严格地限制反应类型，或对「某个反应模板至少出现多少次才收录」设置更严格的下限，请自行修改相应脚本。

# 去重（Deduplication）

- 输入：含映射（mapped）反应 SMILES 的 `*.smi` 文件
- 输出：去重后的含映射反应 SMILES 的 `*.smi` 文件
- 脚本：`./retrochimera/cli/deduplicate_dataset.py`

这个轻量预处理步骤会剔除那些「映射后对应同一个规范（canonical）SMILES」的重复反应 SMILES。输出格式与输入格式相同，且输出仍然使用**原始的（非规范的）SMILES**（规范化只是用来分组）。

# 过滤与划分（Filtering and splitting）

- 输入：含映射反应 SMILES 的 `*.smi` 文件
- 输出：含映射反应 SMILES 的 `{train, val, test}.smi` 文件
- 脚本：`./retrochimera/cli/split_dataset.py`

这一步会对数据做过滤与规范化（例如去掉副产物，过滤掉反应物过多、或反应物与产物规模严重失衡的样本等），然后把它划分为若干折（先把反应按产物分组，再随机划分）。

# 模板抽取（Template extraction）

- 输入：含映射反应 SMILES 的 `{train, val, test}.smi` 文件，或 `{raw_train, raw_val, raw_test}.csv` 文件
- 输出：含处理后数据点的 `{train, val, test}.jsonl` 文件，以及含模板的 `template_lib.json`
- 脚本：`./retrochimera/cli/extract_templates.py`

这一步抽取模板，并把数据转成我们内部的 JSONL 格式。

注意：模板抽取目前用的是标准的 rdchiral。我们有一些初步证据表明，改进模板抽取可以带来更好的效果，但这还需要进一步研究。

# 去 Novo（De-Novo）模型的数据增强

- 输入：含映射反应 SMILES 的 `{train, val, test}.jsonl` 文件
- 输出：使用 R-SMILES 算法增强后的数据点 `{train, val, test}.jsonl` 文件
- 脚本：`./retrochimera/cli/augment_rsmiles.py`

这一步使用 R-SMILES 对产物的原始 SMILES 与反应物做对齐与增强，从而扩充原始数据集（JSONL 格式，含映射反应 SMILES 信息）。该增强旨在提升 RetroChimera 去 Novo 模型的性能。

# 去 Novo（De-Novo）模型的 tokenizer 构建

- 输入：使用 R-SMILES 算法增强后的 `{train, val, test}.jsonl` 文件
- 输出：`vocab.txt` 文件
- 脚本：`./retrochimera/cli/build_tokenizer.py`

这一步基于去 Novo 模型的训练数据集，构建 tokenizer 并生成对应的词表文件。

# 数据特征化（Data featurization）

- 输入：上一步产出的 `{train, val, test}.jsonl` 与 `template_lib.json`
- 输出：含全部特征化折（folds）的 `data.h5` 文件，以及 `template_lib.json`（从输入复制而来）
- 脚本：`./retrochimera/cli/preprocess.py`

这一步把数据进一步转成可供训练的特征化形式。具体形式取决于下游模型，例如对基于图的模型，这一步就是把分子转成图。正因如此，这一步需要设置模型类（以及可能影响数据预处理的其他超参数）。

# 训练（Training）

- 输入：`data.h5` 与 `template_lib.json`（后者对基于 SMILES 的模型会被忽略）
- 输出：`*.ckpt` 模型 checkpoint，以及 `template_lib.json`（从输入复制而来）
- 脚本：`./retrochimera/cli/train.py`

这一步用上一步特征化后的数据训练模型。此处设置的模型类与其他超参数，必须与预处理阶段所设的一致。默认情况下训练会跑若干轮（epoch），沿途保存 checkpoint，然后把最好的那组 checkpoint 取平均，形成 `combined.ckpt`。
我们提供的 checkpoint 所用的设置位于 `retrochimera/cli/config` 下，通过设置 `preset` 参数来取用。

你也可以设置 `finetune_checkpoint_path`，从某个预训练 checkpoint 热启动训练。
模板库与 SMILES token 词表在预训练与微调之间**不必相同**，因为与之相关的层总是会被重新初始化。

# 集成（Ensembling）

- 输入：含「模型在验证集与训练集上的输出转储」的 `*.json` 文件
- 输出：`ensembles_optimized.json` 文件，含集成权重以及各集成的验证/测试准确率
- 脚本：`./retrochimera/cli/optimize_ensembles.py`

最后，这一步可以把多个模型集成为一个更强的模型，例如把 NeuralLoc 与 R-SMILES 2 组合成 RetroChimera。该脚本的输入不是 checkpoint，而是事先在验证与测试数据上运行 `cli/eval.py` 生成的模型输出。
