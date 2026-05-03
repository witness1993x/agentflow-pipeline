# Workspaces

这个目录存放 `run_git_repo_clone.py` 在 `probe` 或 `publish` 阶段产生的本地工作区。

使用规则：

- 默认每个 case 对应一个 workspace
- 发现同名 workspace 已存在时，执行层会直接报错，避免覆盖
- 这里适合放 clone 下来的上游仓库，或 `new_repo` 初始化出的本地仓库
