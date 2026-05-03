# AgentFlow Git Repo Clone

这个目录是当前 GitHub 抓取、话题发现、Chainstream 数据适配分析、clone、fork、template、build、publish 相关能力的统一入口。

目标是先把“适合用 Chainstream API / GraphQL / Kafka 作为数据源和管道的 GitHub 项目机会”发现出来，再在后续阶段推进到 build 和 public repo 发布。

现在这个目录已经包含本地可运行的核心脚手架和执行脚本。

## 当前进度

当前阶段已经完成：

- 热点到 GitHub 项目的结构化判断
- candidate repo 自动抓取
- candidate repo 可解释打分与排序
- build 前 Chainstream fit / fork-or-build 分析
- `fork_existing / template_clone / new_repo` 三种路由
- 本地 `probe` 执行
- GitHub `publish` 执行
- 自动回写：
  - gate
  - probe-run
  - memo
  - review
  - pool

还没有做满的部分：

- X Search 仍需 `X_BEARER_TOKEN`（HackerNews + Reddit 已是 token-free 替代）
- Reddit 未鉴权 JSON 限流（高强度 discover 时已优雅降级为 blocked）
- 真实 publish/probe 端到端未跑（需消耗 ChainStream credits 与创建真 GitHub repo，留给人工触发）

辅助模块（与 `run_pipeline.py` 同目录、被自动 import）：

- `topics_enrichment.py`：GitHub topics 二次拉取 + chainstream_fit 加权
- `kafka_probe.py`：`--mode kafka-probe`，confluent-kafka/kafka-python 双后端
- `auto_publish.py`：`--auto-publish / --auto-publish-confirm / --auto-publish-dry-run`，fail-closed 8 条门槛
- `post_publish.py`：publish 成功后自动渲染 `templates/post-publish/` 到 workspace（幂等）
- `dedup_candidates.py`：跨源 candidate 去重（URL/owner-name 规范化）
- `extra_sources.py`：HackerNews + Reddit 无 token 来源
- `monitoring_setup.py`：`--apply-monitoring`，gh secret set / branch protection / dependabot / chainstream-credits cron / RUNBOOK
- `monitoring_grafana_pagerduty.py`：`--apply-external-monitoring`，Grafana dashboard + PagerDuty service，integration_key 防泄漏
- `pool_runner.py`：`--mode pool`，跨 case subprocess 并行执行（禁止 publish 并行）
- `pool_advancer.py`：`--pool-auto-advance`，按每个 case 的 publish_readiness 自动选下一 mode（discover→data-probe→probe），三道闸门防 publish
- `build_command_inference.py`：`--auto-infer-build-commands`，扫 manifest + language fallback 自动填 build/install/test，confidence 阈值 + only-if-empty 防覆盖
- `chainstream_query_builder.py`：`--chainstream-auto-build-query`，根据 chain_groups/data_cubes 动态构建 GraphQL probe，未知组合走 introspection 0 credits
- `tests/`：218 个 pytest test，`pytest tests/ -q` 0.32s 全过

## 目录定位

这个目录现在主要提供：

- 当前进度说明
- 使用入口
- 规范说明
- 对齐说明
- 独立的 `templates/`
- 独立的 `examples/`
- 独立的 `cases/`
- 独立的 `workspaces/`
- 独立的 `pipeline-pool.md`
- 本地可调用的脚本入口

## 快速开始

### 1. 生成一个新 case

```bash
python3 experimental/agentflow-git-repo-clone/scaffold_pipeline.py \
  --hotspot-name "AI MCP Server"
```

### 2. 自动抓取 candidate repos 并做 build 前分析

```bash
python3 experimental/agentflow-git-repo-clone/run_pipeline.py \
  --case-dir "experimental/agentflow-git-repo-clone/cases/HSP-001-YYYY-MM-DD-your-hotspot" \
  --mode discover \
  --discover-sources github,jina,x \
  --execute
```

`discover` 阶段会优先服务上游发现：输出 GitHub candidate repos、Jina 搜索结果、X 近期搜索结果、Chainstream fit、GraphQL/Kafka/API 适配方向，以及更适合 fork/template clone 还是 build new 的建议。

可选环境变量：

- `JINA_API_KEY`: 提高 Jina Search 额度；不设置时仍可尝试基础调用。
- `X_BEARER_TOKEN`: 使用 X recent search 必需；未设置时会把 X source 标记为 `blocked`，不会中断其他来源。

### 3. 跑本地 probe

在 build 之前，建议先跑 ChainStream GraphQL data probe：

```bash
export CHAINSTREAM_API_KEY="..."

python3 experimental/agentflow-git-repo-clone/run_pipeline.py \
  --case-dir "experimental/agentflow-git-repo-clone/cases/HSP-001-YYYY-MM-DD-your-hotspot" \
  --mode data-probe \
  --execute
```

默认 probe 会调用 `https://graphql.chainstream.io/graphql`，用一个轻量 Solana `DEXTrades limit 1` 查询验证认证、GraphQL endpoint、cube 响应和 credits 信息。也可以用 `--chainstream-query-file ./query.graphql` 传入自定义查询。

### 4. 跑本地 build probe

```bash
python3 experimental/agentflow-git-repo-clone/run_pipeline.py \
  --case-dir "experimental/agentflow-git-repo-clone/cases/HSP-001-YYYY-MM-DD-your-hotspot" \
  --mode probe \
  --execute
```

### 5. 真正发布到 GitHub

```bash
python3 experimental/agentflow-git-repo-clone/run_pipeline.py \
  --case-dir "experimental/agentflow-git-repo-clone/cases/HSP-001-YYYY-MM-DD-your-hotspot" \
  --mode publish \
  --execute \
  --allow-publish
```

## Use as a library in another project

The framework now resolves `cases/`, `workspaces/`, and `pipeline-pool.md`
relative to a configurable **host-project root**, so you can `pip install` it
once and run the CLIs from any project directory.

Resolution order for the root, highest → lowest:

1. `--root <path>` CLI flag
2. `AGENTFLOW_ROOT` environment variable
3. `Path.cwd()` (the directory you ran the command from)

Templates and other package-internal files always come from the installed
package itself; only host-state paths follow the root.

```bash
# 1. Install the framework once.
pip install -e /path/to/agentflow-git-repo-clone

# 2. Make a new host project anywhere on disk.
mkdir my-host-project && cd my-host-project

# 3. Scaffold a case — it lands in ./cases/, not in the framework checkout.
agentflow-scaffold --hotspot-name "My Hotspot" --owner me

# 4. Run the pipeline against the case in this host project.
agentflow-pipeline \
    --case-dir cases/HSP-001-YYYY-MM-DD-my-hotspot \
    --mode discover \
    --execute
```

`--root` is a global flag accepted by both CLIs; the previously documented
`--workspace-root` and `--pool-file` flags still take precedence over the
root-derived defaults when you need to relocate them individually. See
`src/agentflow_pipeline/path_audit.md` for the full path-bucket inventory.

The CLI auto-detects framework root from `--case-dir` / `--gate-file` so you
can run `agentflow-pipeline` from any cwd (including inside a workspace)
without `--root`. When the inferred root differs from the resolved cwd, a
single `[agentflow] auto-corrected ROOT: <old> -> <new>` warning is written
to stderr (stdout stays machine-readable). Pass `--root` explicitly or set
`AGENTFLOW_ROOT` to opt out of self-correction.

## Schedule (twice-daily auto-scan)

A turn-key wrapper installs `agentflow-scan` as an OS-level recurring
job. Default behaviour: run twice a day at **09:00** and **21:00** local
time, writing scan output into `<root>/trends/`.

```bash
# preview (dry-run; nothing written)
bash scripts/install_schedule.sh --root /path/to/host-project

# real install (macOS launchd / linux systemd-user)
bash scripts/install_schedule.sh --root /path/to/host-project --apply

# inspect new vs dropped hotspots once two scans have run
agentflow-trends diff --root /path/to/host-project
```

The script is fail-closed: without `--apply` it only prints the plist /
unit content and the launchctl / systemctl commands it would run. It
also pre-flights `command -v agentflow-scan` so the installed plist
always points at an absolute venv path (launchd does not inherit your
shell `PATH`).

Full step-by-step docs, customization, verification, uninstall, and the
macOS PATH gotcha live in [`scripts/install_schedule.md`](scripts/install_schedule.md).

## 文件说明

- `PROGRESS.md`: 当前实现阶段与能力清单
- `FRAMEWORK_SPEC.md`: 这套目录的完整运行说明
- `WINDOW_GATE_ALIGNMENT.md`: 与 `window-gate-framework` 的映射关系
- `pipeline-pool.md`: 这个入口目录自己的 pool
- `templates/`: 本地模板
- `examples/`: 本地示例
- `cases/`: 这个入口目录自己的 case 工作副本
- `workspaces/`: 这个入口目录自己的本地执行目录
- `scaffold_pipeline.py`: 兼容旧命名的脚手架入口
- `run_pipeline.py`: 兼容旧命名的执行入口
- `scaffold_git_repo_clone.py`: 聚焦命名的脚手架入口
- `run_git_repo_clone.py`: 聚焦命名的执行入口

## 快速说明

如果你准备在新窗口继续聊这件事，后续可以默认直接围绕这个目录展开。

当前主要入口就是：

- `experimental/agentflow-git-repo-clone/scaffold_pipeline.py`
- `experimental/agentflow-git-repo-clone/run_pipeline.py`
