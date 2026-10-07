> 📄 本文件是 [`README.md`](README.md) 的中文译本（**非官方**，仅供本仓阅读参考）。
> 原文由 Microsoft 以 MIT 许可发布，版权归原作者所有；如遇歧义，**以英文原版为准**。

<div align="center">

# RetroChimera

<p align="center">
  由 <a href="https://github.com/microsoft/syntheseus">Syntheseus</a> 驱动 •
  <a href="https://arxiv.org/abs/2412.05269">论文</a>
</p>

[![CI](https://github.com/microsoft/retrochimera/actions/workflows/ci.yml/badge.svg)](https://github.com/microsoft/retrochimera/actions/workflows/ci.yml)
[![Python Version](https://img.shields.io/badge/python-3.9-blue.svg)](https://www.python.org/downloads/)
[![pypi](https://img.shields.io/pypi/v/retrochimera.svg)](https://pypi.org/project/retrochimera/)
[![code style](https://img.shields.io/badge/code%20style-black-202020.svg)](https://github.com/ambv/black)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/microsoft/retrochimera/blob/main/LICENSE)

</div>

#

RetroChimera 是一个前沿的**逆合成（retrosynthesis）模型**，做法是集成（ensembling）两个具有互补归纳偏置（inductive bias）的全新组件。
它大幅超越现有模型，在每一类反应只有极少量样本时也能学到东西；在盲测中，工业有机化学家更喜欢它的预测，甚至超过了对训练数据中真实反应的偏好。

## 使用 RetroChimera

要在本地安装 `retrochimera`，运行

```bash
conda env create -f environment.yml
conda activate retrochimera

pip install retrochimera
```

然后可以这样跑推理

```python
from retrochimera import RetroChimeraModel
from syntheseus import Molecule

model = RetroChimeraModel(model_dir="/model/checkpoint/dir/")
mol = Molecule("Oc1ccc(OCc2ccccc2)c(Br)c1")

predictions = model([mol], num_results=3)

for p in predictions[0]:
    print(p, f"({100. * p.metadata['probability']:.2f}%)")
```

安装时还有两个可选依赖组：`dev` 用于跑测试，`graphium` 用于构建我们在 USPTO-50K 上使用的模型架构；如果你需要运行 USPTO-50K 的 checkpoint，就得用 `pip install retrochimera[graphium]` 安装。

如果你想训练自己的 checkpoint，请按 [`retrochimera/README.md`](retrochimera/README.md) 里的步骤操作。

## RetroChimera 1 的 checkpoint

我们发布的**主 checkpoint**（也是最强的一个）是在 [Pistachio](https://figshare.com/ndownloader/files/59468882) 上训练的。
为了做基准测试，我们还提供了（较弱的）在 [USPTO-50K](https://figshare.com/ndownloader/files/59511926) 和 [USPTO-FULL](https://figshare.com/ndownloader/files/59494598) 上训练的 checkpoint。

如果你要**精确复现**论文里的 USPTO-* 结果，请务必使用扩展数据表 3 和表 4 中列出的推理超参数。
默认情况下，这些参数被设为针对 Pistachio checkpoint 优化过的取值。

最后，我们还发布了一个[正向反应预测（forward）模型 checkpoint](https://figshare.com/ndownloader/files/66654872)，它使用与 RetroChimera 中基于 SMILES 的子模型相同的架构，同样在 Pistachio 上训练。

> [!WARNING]
> RetroChimera 1 的发布目的是研究与实验——我们希望你尝试用各种方式把它「搞坏」，并把结果反馈给我们。和任何机器学习模型一样，它**并非不会出错，也可能产生幻觉（hallucinate）**，尤其在输入超出训练分布时。我们期待与社区合作，让这个模型对所有人来说都越来越好！
>
> 在任何真实场景中使用这些预测结果之前，它们**必须由化学专家独立进行风险评估与验证**。
> 特别地，**输出列表中排名越靠后的反应，越有可能是幻觉**；
> 我们建议每个输入最多请求 5～10 个反应，除非同时配合严格的筛选（参见例如 [[1]](https://www.nature.com/articles/nature25978)[[2]](https://pubs.acs.org/doi/10.1021/acs.accounts.5c00155)）
>
> 尽管 Pistachio checkpoint 已经相当强大，用户仍应注意以下局限与建议：
>
> 1. 该 checkpoint 训练所用的反应数据截止到 2023 年，因此不包含更新的化学进展。
> 2. 由于 Pistachio 数据集含有一定噪声，预测结果应结合相关文献并独立验证。
> 3. 对于与 Pistachio 训练分布差异较大的化学领域（包括专门的学术化学与天然产物化学），性能可能较低。
> 4. 我们建议把 RetroChimera 与一个[反应可行性模型](https://github.com/microsoft/syntheseus/blob/f48d33c00050100d5315c1965c5e198ac8460a1f/syntheseus/reaction_prediction/filters/wrapper.py#L13)配合使用，并启用[共识模式（consensus mode）](https://github.com/microsoft/retrochimera/pull/26)。
> 5. 对于特定领域的应用，我们建议按照论文中的描述，用相关的电子实验记录本（ELN）数据或文献反应对 checkpoint 进行微调。
>
> 如果你发现 RetroChimera 1 在你关注的类药分子上表现不好，请发邮件到 retrochimera@microsoft.com 告诉我们，以便我们在下一个模型版本中改进。

## 引用

如果你在研究中使用了 RetroChimera，请考虑引用我们的
[arXiv 预印本](https://arxiv.org/abs/2412.05269)
（BibTeX 见下）。

```
@article{maziarz2025chemist,
  title={Chemist-aligned retrosynthesis by ensembling diverse inductive bias models},
  author={Maziarz, Krzysztof and Liu, Guoqing and Misztela, Hubert and Tripp, Austin and Li, Junren and Kornev, Aleksei and Gai{\'n}ski, Piotr and Hoefling, Holger and Fortunato, Mike and Gupta, Rishi and Segler, Marwin},
  journal={arXiv preprint arXiv:2412.05269},
  year={2025}
}
```

## 参与贡献

本项目欢迎贡献与建议。大多数贡献都要求你同意一份贡献者许可协议（CLA），声明你有权（并且确实会）授予我们使用你贡献内容的权利。详情请访问 https://cla.opensource.microsoft.com 。

当你提交 pull request 时，CLA 机器人会自动判断你是否需要提供 CLA，并相应地标注该 PR（例如状态检查、评论）。按机器人给出的提示操作即可。在所有使用我们 CLA 的仓库中，你只需完成一次。

本项目采用了[《微软开源行为准则》](https://opensource.microsoft.com/codeofconduct/)。
更多信息请见[行为准则 FAQ](https://opensource.microsoft.com/codeofconduct/faq/)，或通过 [opencode@microsoft.com](mailto:opencode@microsoft.com) 联系以咨询其他问题或意见。

## 商标

本项目可能包含项目、产品或服务的商标或徽标。对微软商标或徽标的授权使用，须遵守并遵循
[《微软商标与品牌指南》](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general)。
在本项目的修改版本中使用微软商标或徽标，不得引起混淆或暗示微软的赞助。
任何第三方商标或徽标的使用，均受相应第三方的政策约束。
