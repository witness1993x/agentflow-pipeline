# AgentFlow Git Repo Clone Spec

## Purpose

这套实验内容用于把一个热点、方向或仓库机会，推进成一个可以被 GitHub 承接的数据项目判断与执行流程。

当前优先级是 build 之前的上游发现层：通过 GitHub 搜索/话题、Jina 搜索、X Search 等来源发现机会，再分析这些机会是否适合用 Chainstream API / GraphQL / Kafka 作为数据源和管道。只有通过这层分析的机会，才进入后续 build / publish。

它分成两层：

1. 发现层：从 GitHub / Jina / X 等来源找到机会候选
2. 分析层：判断候选是否适合结合 Chainstream 的 API / GraphQL / Kafka 能力
3. 决策层：判断值不值得做、做成什么、路由到哪类 repo
4. 执行层：按判断结果做 probe、发布与结果回写

## Core Artifacts

- `01-hotspot-intake.md`
- `02-pipeline-gate.yaml`
- `03-publish-decision-memo.md`
- `04-build-probe-run.md`
- `05-review-checkpoint.md`

## Gate Semantics

- `Gate 0: Reframe`
- `Gate 1: Hotspot Signal`
- `Pre-build Analysis: Chainstream Fit + Fork/Build Recommendation`
- `Gate 2: Project Shape`
- `Gate 3: Repo Routing`
- `Gate 4: Buildability`
- `Gate 5: Publish Decision`

## Pre-Build Analysis

这一层是当前阶段的重点。它发生在 build 之前，用来回答：

- 这个机会是否真的需要 Chainstream，而不是普通静态数据或手写 mock？
- 更适合 Chainstream 的哪个能力：REST API、GraphQL、Kafka、WebSocket、SDK、CLI、MCP？
- 如果用 GraphQL，目标 chain group / cube / aggregation / query intent 是什么？
- 如果用 Kafka，是否真的需要流式消费、低延迟和更重的管道？
- public repo 是否可以安全展示数据，还是需要 sample dataset / mock data？
- 现有 GitHub 项目适合 fork/template clone，还是应该 build new？

推荐判断：

- `fork_existing`: 已有项目与 Chainstream 目标能力高度贴合，只需要替换或补充数据源
- `template_clone`: 已有通用模板适合承接，但业务逻辑需要重新写
- `build_new`: 机会强依赖 Chainstream 数据模型，直接新建更清晰
- `drop`: 找不到足够强的 Chainstream 数据适配理由

## Execution Model

执行入口是 `run_pipeline.py`，支持五种模式：

### `discover`

自动用 GitHub / Jina / X 搜索 candidate opportunities，并为每个候选补充 build 前分析字段。

行为：

- 生成搜索 query
- 按 `--discover-sources` 调用 `gh search repos`、Jina Search、X recent search
- 归一化候选仓库
- 为候选仓库生成可解释分数与排序原因
- 为候选仓库生成 Chainstream fit 与 fork/build recommendation
- 生成 `recommended_strategy`
- 在 `--execute` 时写回 `02-pipeline-gate.yaml`

参数：

- `--discover-sources github,jina,x`: 选择搜索源，也可以用 `all`
- `--jina-query`: 覆盖 Jina Search query
- `--x-query`: 覆盖 X Search query
- `JINA_API_KEY`: 可选，用于 Jina Search 更高额度
- `X_BEARER_TOKEN`: 必需，用于 X recent search；缺失时该 source 会被标记为 `blocked`

### `inspect`

只读取 `02-pipeline-gate.yaml` 并输出执行计划，不修改任何内容。

### `data-probe`

在 build 之前验证 ChainStream GraphQL 数据源是否可用。

行为：

- 默认使用 `https://graphql.chainstream.io/graphql`
- 默认读取 `CHAINSTREAM_API_KEY`
- 默认执行轻量 Solana `DEXTrades limit 1` 查询
- 支持 `--chainstream-query` 或 `--chainstream-query-file` 覆盖查询
- 回写 `pre_build_analysis.chainstream_fit.graphql_probe`
- 回写 `execution_state.data_probe`

Reference:

- ChainStream Docs: https://docs.chainstream.io/
- GraphQL overview: https://docs.chainstream.io/en/graphql/getting-started/overview
- First query guide: https://docs.chainstream.io/en/graphql/getting-started/first-query
- Access methods: https://docs.chainstream.io/en/docs/access-methods/overview
- GraphQL IDE: https://ide.chainstream.io
- LLM reference index: https://docs.chainstream.io/llms.txt

安全约束：

- 不会打印 API key
- 未传 `--execute` 时只输出 dry-run plan
- 缺少 API key 时标记为 `blocked`，不中断发现层

### `probe`

根据 `repo_strategy` 准备本地 workspace，并尝试运行：

- install
- build
- test

默认 dry-run，只有加 `--execute` 才真正执行。

### `publish`

在 `probe` 的基础上，尝试执行 GitHub 发布动作。

安全约束：

- 必须显式传 `--execute`
- 必须显式传 `--allow-publish`

策略说明：

- `fork_existing`: 调用 `gh repo fork`
- `template_clone`: clone 上游，整理后创建新 repo
- `new_repo`: 在本地 workspace 初始化 git 仓库，再调用 `gh repo create`

## Repo Plan

`pipeline-gate.yaml` 中的 `repo_plan` 用于执行层：

- `local_workspace`
- `repo_name`
- `github_owner`
- `visibility`
- `default_branch`

如果这些字段为空，执行层会用合理默认值推导。

## Automatic Writeback

当执行层使用 `--execute` 且未传 `--no-writeback` 时，会自动回写：

### 1. `02-pipeline-gate.yaml`

包括：

- `candidate_repos`
- `discovered_query`
- `recommended_strategy`
- `recommended_reason`
- `pre_build_analysis.chainstream_fit`
- `pre_build_analysis.chainstream_fit.graphql_probe`
- `pre_build_analysis.fork_or_build`
- `source_context.discovery_sources`
- `execution_state.discovery`
- `execution_state.data_probe`
- `execution_state.probe`
- `execution_state.publish`

### 2. `04-build-probe-run.md`

包括：

- 实际命令
- build/test 状态
- 推荐下一状态
- 观察与总结

### 3. `pipeline-pool.md`

自动更新当前 `Hotspot ID` 对应的一行摘要。

### 4. `03-publish-decision-memo.md`

同步更新：

- routing decision
- next step
- latest review delta

### 5. `05-review-checkpoint.md`

同步更新：

- latest review meta
- changed evidence
- project shape / repo strategy
- next review date

## Workspace Rules

默认 workspace 根目录是：

- `experimental/agentflow-git-repo-clone/workspaces/`

单次运行目录建议为：

- `HSP-001-YYYY-MM-DD-slug`

## Safety Defaults

- 默认不执行，只打印计划
- 默认不发布，除非显式允许
- 发现 workspace 已存在时直接报错，避免覆盖
- 发现 git 用户身份未配置时拒绝 publish

## Current Scope

当前版本已经能：

- 自动抓取 candidate repos
- 对 candidate repos 做可解释打分与排序
- 对 candidate repos 做轻量 Chainstream fit / fork-or-build 预分析
- 读取 pipeline case
- 准备本地 workspace
- 执行 install/build/test
- 自动回写 gate / probe-run / memo / review / pool
- 在满足条件时调用 `gh` 进行真实发布

当前版本还没有：

- 根据 probe 结果自动重评分与自动推进全部状态
- 更强的 candidate repo 质量打分与排序逻辑

## Recommended Usage

建议按这个顺序使用：

1. 用 `scaffold_pipeline.py` 生成 case
2. 补全 `02-pipeline-gate.yaml`
3. 运行 `run_pipeline.py --mode discover`
4. 决定 candidate repo 与 `repo_strategy`
5. 运行 `run_pipeline.py --mode probe`
6. 根据结果决定是否 `publish`
