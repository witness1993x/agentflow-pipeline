# Window Gate Alignment

这个文件说明 `agentflow-git-repo-clone` 如何从 `window-gate-framework` 的骨架迁移而来。

## Why A Separate Folder

两者都使用：

- `README`
- `templates`
- `examples`
- `cases`
- `pool`
- `scaffold`

但关注问题不同：

- `window-gate-framework`: 判断机会值不值得做
- `agentflow-git-repo-clone`: 判断热点是否值得被做成 Chainstream 数据驱动的 GitHub 项目，并推进到 clone / probe / publish

## Structural Mapping

| Window Gate | AgentFlow Git Repo Clone |
|---|---|
| Opportunity Intake | Hotspot Intake |
| Gate 1 Signal | Gate 1 Hotspot Signal |
| Gate 2 Demand Window | Pre-build Chainstream Fit + Gate 2 Project Shape |
| Gate 3 Product Window | Gate 3 Repo Routing + Gate 4 Buildability |
| Gate 4 Action | Gate 5 Publish Decision |
| Probe Run | Build Probe Run |
| Decision Memo | Publish Decision Memo |
| Opportunity Pool | Pipeline Pool |

## Semantic Shift

### From Opportunity To Repository

原框架问：

- 这个机会值不值得做？

新框架问：

- 这个热点值不值得被表达成 GitHub 项目？
- 这个机会是否适合用 Chainstream API / GraphQL / Kafka 做数据源？
- 这个仓库应该长什么样？
- 应该 route 到哪种 repo strategy？

### From Build To Publish

原框架中的 `build` 更接近“开始做产品”。

这里的 `publish` 更接近“已经具备公开仓库价值，可以真正发到 GitHub”。

## Shared Patterns

两者共享以下工作方式：

- 用 YAML 作为结构化判断的事实源
- 用 memo 输出本轮结论
- 用 probe 记录关键实验
- 用 review checkpoint 记录状态变化
- 用 pool 管理多个并行 case
- 用脚手架生成标准化工作目录

## New Concepts In Pipeline

这个目录相对 `window-gate-framework` 新增了这些概念：

- `project_shape`
- `pre_build_analysis`
- `repo_strategy`
- `candidate_repos`
- `build_commands`
- `repo_plan`
- `publish` 执行层

## Execution Boundary

`window-gate-framework` 到模板和脚手架为止。

`agentflow-git-repo-clone` 继续往前走一层，通过 `run_pipeline.py` 进入：

- local workspace
- clone/init repo
- build/test
- optional GitHub publish

所以这两个 experimental 目录的关系可以理解成：

- `window-gate-framework` 是通用决策骨架
- `agentflow-git-repo-clone` 是面向 GitHub 仓库抓取、构建和发布的垂直化实现
