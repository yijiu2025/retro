> 📄 本文件是 [`README.md`](README.md) 的中文译本（**非官方**，仅供阅读参考）。如遇歧义，以英文原版为准。

本目录下的代码来自 [`dgllife` 库](https://github.com/awslabs/dgl-lifesci)（v0.3.0）。

由于我们只需要该库很小的一部分（`utils/featurizers.py` 中的一部分），所以我们直接复制了相关代码，
而不是把它作为正式依赖引入。这样可以避免拉进 `dgllife` 的一堆重型依赖（最典型的是 `dgl`）。
