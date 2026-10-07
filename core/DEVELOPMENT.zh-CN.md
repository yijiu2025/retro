> 📄 本文件是 [`DEVELOPMENT.md`](DEVELOPMENT.md) 的中文译本（**非官方**，仅供阅读参考）。如遇歧义，以英文原版为准。

要发布 `retrochimera` 的新稳定版本，需要完成以下步骤：

1. 创建一个 PR，编辑 `CHANGELOG.md`（注意它需要在**三处**做修改）。
2. 运行[「Release Stable Version」工作流](https://github.com/microsoft/retrochimera/actions/workflows/release.yml)，
   把版本号作为参数传入（使用 `x.y.z` 格式，**不要**带前面的 "v"），并把分支设为 `main`。
3. 请一位有资格的审批人（[@microsoft/msrai4s-dd](https://github.com/orgs/microsoft/teams/msrai4s-dd) 团队中的任意成员）
   批准该工作流。除其他事项外，他们应确认步骤 (1) 已完成，并复核包版本号。
4. 批准之后，发布工作流会推送 tag、构建包，并通过
   [OIDC 可信发布（trusted publishing）](https://docs.pypi.org/trusted-publishers/)发布到 PyPI。
5. 基于[新建的 tag](https://github.com/microsoft/retrochimera/tags)创建一个 GitHub Release。
   名称设为 `retrochimera x.y.z`。描述应为从变更日志复制过来的变更列表。
   可以考虑在变更列表前加一段简短说明，描述本次发布的主旨。
