# GitHub 方案进度

## 已完成

### 1. 决策框架

已经把 GitHub 方向抽象成一条可执行 pipeline，而不是只停留在文档层：

- `Gate 0: Reframe`
- `Gate 1: Hotspot Signal`
- `Gate 2: Project Shape`
- `Gate 3: Repo Routing`
- `Gate 4: Buildability`
- `Gate 5: Publish Decision`

### 2. 脚手架

已经支持用脚手架快速生成完整 case：

- `01-hotspot-intake.md`
- `02-pipeline-gate.yaml`
- `03-publish-decision-memo.md`
- `04-build-probe-run.md`
- `05-review-checkpoint.md`

### 3. GitHub candidate 自动发现

已经支持：

- 自动生成 GitHub 搜索 query
- 调用 `gh search repos`
- 归一化候选仓库
- 为候选仓库自动打分
- 输出 `recommended_strategy`

### 4. Build 前 Chainstream 分析层

已经支持模板和发现结果表达：

- GitHub / Jina / X 等多源发现输入
- Chainstream REST API / GraphQL / Kafka / WebSocket 能力适配
- GraphQL chain group、cube、aggregation、query intent 预分析
- public demo 数据安全性判断
- `fork_existing / template_clone / build_new / drop` 的 build 前建议
- ChainStream GraphQL data probe，默认验证 Solana `DEXTrades limit 1`

当前自动化实现已经覆盖 GitHub candidate、Jina Search、X recent search 的轻量 Chainstream fit 与 fork/build recommendation。X Search 依赖 `X_BEARER_TOKEN`，未配置时会记录为 blocked source。

### 5. 仓库路由

已经支持三种主要路径：

- `fork_existing`
- `template_clone`
- `new_repo`

### 6. 本地执行

已经支持：

- 准备本地 workspace
- 执行 install
- 执行 build
- 执行 test

### 7. 发布动作

已经支持：

- `fork_existing` 时调用 `gh repo fork`
- `template_clone` / `new_repo` 时初始化本地 git 并调用 `gh repo create`

### 8. 自动回写

已经支持执行后自动回写：

- `02-pipeline-gate.yaml`
- `04-build-probe-run.md`
- `03-publish-decision-memo.md`
- `05-review-checkpoint.md`
- `pipeline-pool.md`

### 9. 更精细的 candidate 质量模型

`gh search repos` 现在抓取更完整字段：`forksCount / openIssuesCount / pushedAt / isArchived / isFork / language / defaultBranch / homepage`。打分逻辑加入：

- 已归档强降权 (-40)、派生仓库降权 (-10)
- 用 `pushed_at` 优先于 `updated_at` 计算活跃度，并细化到「两周 / 一月 / 三月 / 半年 / 一年以上」分层
- fork 数量与 issue 活跃度信号
- 语言 ↔ Chainstream SDK 生态匹配 (`typescript / javascript / python / go / rust / java / kotlin`)
- 每个 candidate 现在还附带 `quality_signals` 字段透明记录得分组成

`assess_chainstream_fit` 也接入了 `homepage / language` 信号、`subgraph` 等新词项，并对归档仓库扣分。`recommend_fork_or_build` 在归档仓库时直接走 `build_new`。

### 10. Probe 驱动的状态机

- `data-probe` 与 `probe` 都会自动写回 `decision.next_review_date`，按结果区分 cadence (`passed→+7d / blocked→+3d / failed→+14d`)
- 当 `data_probe / build_probe` 都通过且 `chainstream_fit.verdict == pass` 时，自动推进 `decision.final_status` 到 `publish_ready`
- `gate_4_buildability.kill_signals` 现在会在 build/test 失败时和 stderr/stdout 子串匹配，命中后写入 `kill_signals_triggered` 并把 `decision.next_action` 指向需要解决的 kill signal
- 新增 `execution_state.publish_readiness` 字段 (`not_started / in_progress / blocked_data_probe / blocked_kafka_probe / blocked_buildability / ready / published`)，作为发布前 gate 的统一信号

### 11. GitHub Topics 二次拉取

`topics_enrichment.py`（独立模块）在 `discover_candidates` 末尾对前 N（默认 5）个 `github_search` 来源的 candidate 调 `gh api repos/{owner}/{name} --jq .topics`。命中 `chainstream/blockchain/dex/solana/ethereum/web3/onchain/defi/indexer/graphql/kafka` 中关键词时给 `chainstream_fit_score` 加权（每个 +5，单 candidate 最多 +25）。统计写到 `source_context.topics_enrichment`。`gh` 不可用或限流时静默返回 `[]`，不影响主流程。

### 12. Chainstream Kafka data probe

新增 `--mode kafka-probe` 与 6 个 `--kafka-*` 参数。`kafka_probe.py`（独立模块）实现：

- 优先 `confluent-kafka`，fallback `kafka-python`，两者都缺失时返回 `blocked` 而不是抛 ImportError
- result schema 与 GraphQL probe 对齐 (`status / endpoint / query_source / summary / response_keys / credits`)
- 真实消费时按 `min(1s, 剩余 timeout)` 循环 poll，到 `--kafka-timeout-seconds` 没拿到消息则 `failed`
- 凭证从环境变量读，不进 stdout/summary
- `update_gate_after_kafka_probe` 写到 `pre_build_analysis.chainstream_fit.kafka_probe` 与 `execution_state.kafka_probe`，并且**只在 `target_capability == "kafka"` 时**把 verdict 推进到 publish 链路
- `evaluate_publish_readiness` 已识别 `kafka_required = (target_capability == "kafka")`，未通过时 readiness 走 `blocked_kafka_probe`

### 13. Readiness-gated 自动 publish

`auto_publish.py`（独立模块）+ `--auto-publish / --auto-publish-confirm / --auto-publish-dry-run` 三 flag。fail-closed 8 条门槛：readiness=ready、未发布过（幂等）、meta 完整、`repo_strategy != undecided`、`repo_plan.github_owner/repo_name` 非空、`decision.veto_from_gate` 为空、`kill_signals_triggered` 为空、`chainstream_fit.verdict == pass`。任一不通过都给中文阻断原因；`--auto-publish` 单 flag 只做检查不发布，**双 flag 才真发**。

### 14. 发布后运营脚手架

`templates/post-publish/` 含 `.github/workflows/ci.yml`、`ISSUE_TEMPLATE/{bug_report,feature_request}.md`、`PULL_REQUEST_TEMPLATE.md`、`CODEOWNERS`、`README_BADGES.md`、`MONITORING.md`。`post_publish.py`（独立模块）实现 `apply_post_publish_templates(workspace, config, ...)`：

- 占位符白名单替换 (`github_owner / repo_name / install_command / build_command / test_command / default_branch / language`)，未在白名单的 `{{...}}`（如 GitHub Actions `${{ matrix.os }}`）保留原样
- 已存在的目标文件**跳过不覆盖**，幂等
- 在 `--mode publish --execute --allow-publish` 成功后自动调用，结果写到 `execution_state.publish.post_publish`

### 15. 跨源 candidate 去重

`dedup_candidates.py`（独立模块）在所有 source 收齐后、第一次排序之前调用，`canonicalize_url` 折叠：`http→https`、host 去 `www.`、去 trailing slash 与 `.git`、去 `#fragment`、剥 `utm_*/gclid/fbclid/mc_*/ref*` 等跟踪参数；`github.com/owner/name/...` 一律折叠到 `https://github.com/owner/name`。同 key 合并：高分为 base，`sources_seen` 取并集，`fit_reason` 用分号合并并大小写无关去重，`stars/forks/open_issues` 取较大非空值，`pushed_at/updated_at` 取较新值。统计写到 `source_context.dedup`。

### 16. 多源补充：HackerNews + Reddit（无须 token）

`extra_sources.py`（独立模块）+ `--mode discover --discover-sources hackernews,reddit` + `--hackernews-query / --reddit-query / --reddit-subreddits`：

- HackerNews：Algolia 公开 API (`hn.algolia.com/api/v1/search?tags=story`)，`stars = points + num_comments`，`url` 优先 story_url（很多 Show HN 直挂 GitHub repo）
- Reddit：`reddit.com/[r/{sub}/]search.json`，强制 `User-Agent: agentflow-git-repo-clone/0.1`（缺则 429）。多 subreddit 串行查询 + `id` 去重。selftext/url/title 用正则抽 `github.com/...` 链接，命中则作为 `url`，让 GitHub repo 直接进入主 dedup/打分链路
- 任何 HTTP/JSON 失败抛 `ExtraSourceError`，主程序 catch 并标记 source 为 blocked，**单源失败不让 discover 整体崩**

### 17. 真实监控自动化

`monitoring_setup.py`（独立模块）+ `--apply-monitoring / --monitoring-secret-from-env / --monitoring-protect-branch / --monitoring-required-checks / --monitoring-required-reviews`，5 步真实接入：

- `apply_repo_secrets`：`gh secret set --repo` 设 secrets，**value 永不进 stdout/返回值**，dry-run 与 execute 路径都验证不泄漏
- `enable_branch_protection`：`gh api -X PUT repos/.../branches/.../protection`（required_status_checks strict + contexts、enforce_admins、required PR reviews + dismiss_stale、禁 force-push）
- `enable_security_features`：启用 vulnerability-alerts + automated-security-fixes
- `seed_credits_check_workflow`：写 `.github/workflows/chainstream-credits.yml`，cron 每天调 ChainStream API 查 credits，低于阈值时 fail 触发 GitHub 通知
- `seed_runbook`：写 `RUNBOOK.md`（含监控地址、应急联系人、kill switch、credits 阈值），已存在则 skip
- 默认 dry-run，**仅 `--apply-monitoring` 显式传时**才真调 `gh`；repo_ref 不合法时 fail-closed 强制 dry-run；任意 step 失败都 catch 不向上抛

### 18. pytest 测试套件

`tests/` 下 131 个测试覆盖：score_candidate / assess_chainstream_fit / recommend_fork_or_build / evaluate_publish_readiness（6 种状态）/ detect_kill_signal_triggers / compute_next_review_date / parse_discovery_sources / dedupe_terms / discovery_query / check_auto_publish_safety（fail-closed 7 路径）/ apply_post_publish_templates（幂等 + 占位符白名单）/ topics_enrichment 全部 / kafka_probe（dry + 缺参数 blocked + summary 不含 sasl_password）/ Grafana+PagerDuty（24 test，含 integration_key 不泄漏 capsys 验证）/ pool_runner（17 test，含禁止 publish 并行）。`pytest tests/ -q` 0.37s 全过，零网络访问。

### 19. Grafana + PagerDuty 监控接入

`monitoring_grafana_pagerduty.py`（独立模块）+ `--apply-external-monitoring / --grafana-url / --grafana-token-env / --grafana-folder-uid / --pagerduty-token-env / --pagerduty-service-name / --pagerduty-escalation-policy-id`：

- `apply_grafana_dashboard`：POST `/api/dashboards/db`，Bearer 鉴权
- `apply_pagerduty_service`：先 GET 查重再 POST 创建 service + integration（generic events API）
- `seed_chainstream_grafana_template`：内置 Grafana v10 dashboard JSON（schemaVersion=38，3 panel：credits stat / workflow runs / stargazers）
- 默认 dry-run；`--apply-external-monitoring` 才真调外部 SaaS（与 `--apply-monitoring` 解耦）
- **integration_key 4 道防线**：函数自身不打印；`_summarize_external` 显式 redact；token 不进 result dict；`run_pipeline.py` 集成层在写 yaml 前再 sanitize 一次（替换为 `<redacted: rotate via gh secret PAGERDUTY_INTEGRATION_KEY>`）

### 20. 跨 case 并行执行（pool 模式）

`pool_runner.py`（独立模块）+ 新 `--mode pool` + `--pool-cases-dir / --pool-mode / --pool-status-filter / --pool-name-glob / --pool-max-workers / --pool-timeout-per-case / --pool-extra-args / --pool-execute`：

- `find_pool_cases`：扫 cases/ 子目录，按 status/name_glob 过滤，按 hotspot_id 字典序排序
- `run_case_subprocess`：用 `subprocess.run` 跑 `python3 run_pipeline.py --case-dir X --mode Y`，进程级隔离避开 argparse + globals 串扰
- `run_pool_parallel`：`ThreadPoolExecutor` 并发（IO bound），返回 pass/fail/timeout 计数 + slowest 3 + duration
- **publish 模式被禁止并行**（防误发布），argparse 层 `--pool-mode` choices 已剔除 publish；运行时 `mode == "publish"` 抛 ValueError 双保险
- 真实端到端验证：2 个 case `inspect` 并行，wall=0.27s（串行需 ~0.5s），subprocess 隔离干净

### 21. 真实端到端 case 验证（2026-05-01）

用真 `gh` (witness1993x 已认证) + 真 HackerNews Algolia + 真 Reddit JSON + 真 ChainStream API 跑 `Solana DEX Aggregator` 与 `Wallet PnL Tracker` 两个 case：

- discover 真调 `gh search repos` 拿到 5 个候选；HN 0 命中（这次没 Show HN 相关）；Reddit blocked（命中 rate limit，优雅降级）
- topics_enrichment 真调 `gh api` 5 次，本次仓库都是小项目无 topics（`candidates_enriched=0` 是合理数据，不是 bug）
- dedup `unique=5 duplicates_merged=0`（这次跨源命中没重叠，符合预期）
- **ChainStream GraphQL data-probe 真跑通**：默认 Solana DEXTrades limit 1 query，返回真实数据 + credits（20.72 CU/row），yaml 完整回写 `pre_build_analysis.chainstream_fit.{graphql_probe,verdict,score}` 与 `execution_state.data_probe`；`chainstream_fit.verdict` 自动从 `hold` → `pass`；`decision.final_status` 自动从 `watch` → `probe`；`next_action` 自动写出"run build probe after ChainStream GraphQL data probe passed"；`next_review_date` 按 cadence 推进 +7d
- yaml 全字段回写：`source_context.discovery_sources / topics_enrichment / dedup`、`gate_3_repo_routing.candidate_repos[].sources_seen / language / quality_signals`、`execution_state.discovery / data_probe / publish_readiness`、`decision.next_review_date / next_action`
- auto-publish-dry-run 准确给出阻断原因
- pool 模式 inspect 跨 2 case 并行（wall=0.27s，subprocess 隔离正确）
- **API key 防御性验证**：grep `cs_live_` 跨 `cases/`、`workspaces/`、所有 .py / .md 均零命中，确认敏感凭证**不写入任何持久化位置**

### 22. http_json User-Agent 修复（真实 bug）

`run_pipeline.py` 的 `http_json` helper 默认用 Python 的 `Python-urllib/3.x` UA，被 ChainStream / Cloudflare 1010 直接屏蔽。这条 bug **只能通过真实端到端 probe 才能发现**——是这次推 1 的实际收益之一。修复：加 `DEFAULT_HTTP_USER_AGENT = "agentflow-git-repo-clone/0.1"` 作为默认 UA，调用方传的 headers 仍可覆盖。Jina / X / ChainStream GraphQL 三处共用 `http_json` 都自动受益。

### 23. Build commands 自动推断

`build_command_inference.py`（独立模块）+ `--auto-infer-build-commands / --auto-infer-confidence-threshold / --auto-infer-overwrite`。优先级：`package.json` > `pyproject.toml`(含 poetry 检测) > `requirements.txt` > `Cargo.toml` > `go.mod` > `Makefile`(扫顶层 target) > `Dockerfile`。无 manifest 时按 candidate.language 走 fallback（confidence ≤30）。confidence ≥ 阈值才写入 `gate_4_buildability.build_commands`，**默认只填空字段**（`only_if_empty=True`），保留用户已显式配置的内容。跨 35 个 pytest 验证，含 JVM 拒绝、多 manifest 优先级、空字段 vs 已配置等边界。`gate_4_buildability.inference` 字段始终记录推断元数据（language_detected / evidence / confidence / last_inferred_at）便于审计。

### 24. Pool 按 readiness 自动 advance

`pool_advancer.py`（独立模块）+ `--pool-auto-advance / --pool-auto-advance-max-rounds / --pool-auto-advance-include-publish`。决策树（fail-closed）：drop → None；published/ready/blocked_* → None；discovery 空 → discover；data_probe 空 + repo_strategy != undecided → data-probe；probe 空 + data_probe.passed → probe。**默认不自动 advance 到 publish**（必须显式 `--pool-auto-advance-include-publish`）。多轮调度：每轮按 next_mode 分组 → `pool_runner.run_pool_parallel` 并发跑同 mode case → re-read yaml 进入下轮。三道 publish 闸门：next_mode_for 默认拒返 publish + pool_runner FORBIDDEN_POOL_MODES + auto-advance orchestrator 短路 publish。25 个 pytest 覆盖所有决策路径与 max_rounds 终止。**实测 dry-run**：2 个真实 case 被准确分到 probe/discover 两组并行。

### 25.4 Framework 抽离成可重复使用 namespace package（2026-05-01）

**P/Q/R/S 4 路（P 串行，Q/R/S 并行）完成抽离 4 件套**：

- **P (namespace + 打包)**：13 个 module + cli.py + scaffold.py 移到 `src/agentflow_pipeline/` 并改 relative imports；写 `pyproject.toml` 注册 `agentflow-pipeline / agentflow-scaffold` 2 个 console scripts；`pip install -e .` 真 work；`from agentflow_pipeline import ...` 53 个 exports；旧入口 `run_pipeline.py / scaffold_pipeline.py` 保留为 thin shim 向后兼容；conftest 加 `src/` 进 sys.path；218 测试零回归
- **Q (DataSource plugin 抽象)**：新增 `data_source.py` 定义 `DataSourcePlugin(Protocol)` (7 method + 2 attribute)；`ChainStreamDataSource` 把原 `assess_chainstream_fit` / `infer_chainstream_targets` / `post_chainstream_graphql` 逐字搬过来作为默认实现；`BitqueryDataSource` 作为示例第二实现（不同 `gate_field=bitquery_fit`、不同关键词、不同 doc refs）；module-level registry + `register_data_source / get_data_source / default_data_source`；`--data-source` CLI flag + `AGENTFLOW_DATA_SOURCE` env；cli.py 8 处关键 diff（`assess_chainstream_fit` 等改 thin wrapper，`update_pre_build_analysis` / `update_gate_after_data_probe` 用 `plugin.gate_field` 写 yaml）；33 个新测试，0 回归
- **R (路径解耦)**：`_resolve_root()` 优先级 `--root > AGENTFLOW_ROOT > os.getcwd()`；`cli.py` 与 `scaffold.py` 都加 `--root` flag；`--workspace-root` / `--pool-file` 默认空串 + main 里 lazy fallback；`_run_pool_branch` 把相对路径解到 `ROOT/cases`；新增 `path_audit.md` 把所有 `Path(__file__)` / `__file__` / `PACKAGE_DIR` / `ROOT` 用法分两类（PACKAGE_DIR-relative 保留 / ROOT-relative 解耦）；6 个 host-project subprocess 测试
- **S (init bootstrap)**：新增 `init_command.py` + 第三个 console script `agentflow-init`；`init_host_project(target_dir, *, force, skip_pool, skip_claude_md)` 创建 cases/ workspaces/ pipeline-pool.md `.agentflow.toml` CLAUDE.md（**追加非覆盖**语义：CLAUDE.md 已存在时追加 `## AgentFlow Pipeline` 一节而不破坏原内容；force=True 才覆盖；skip_claude_md 跳过）；幂等（重复跑同目录全 skip）；13 个新测试

**总测试数：270 passed in 1.92s**（baseline 218 + S 13 + R 6 + Q 33）。

**实测 host-project demo（framework 目录之外）**：在 `/tmp/host-demo`（**完全干净的目录**）：
```bash
$ agentflow-init .                            # 7 artefact 创建
$ agentflow-scaffold --hotspot-name "Demo"    # case 落在 /tmp/host-demo/cases/
$ agentflow-pipeline --case-dir cases/HSP-001-... --mode inspect  # workspace 指向 /tmp/host-demo/workspaces/
$ AGENTFLOW_DATA_SOURCE=bitquery python -c "from agentflow_pipeline.data_source import default_data_source; ds=default_data_source(); print(ds.name, ds.gate_field)"
# → bitquery bitquery_fit
```
零 framework 目录污染。

### 25.5 真实 ship 案例：chainstream-launch-radar

**2026-05-01 完成首次端到端真实发布**。从市场调研到 GitHub repo 上线完整闭环：

- 6 路并行真实数据搜集（gh search / HN Algolia / GitHub trending / WebSearch）→ 选定「Pump.fun realtime analytics + ChainStream + Claude AI reasoning」方向（0 hit 真空白）
- 新 framework 能力：加 `--reuse-existing-workspace` flag，允许用户预先准备好 workspace 后跳过 `ensure_empty_workspace` 检查；保留默认 fail-safe
- 正规 8-gate auto-publish 路径全过：`--auto-publish --auto-publish-confirm --reuse-existing-workspace` → prepare_workspace(reuse) → npm install/build/test passed → `gh repo create` → 7 个 post-publish 模板渲染 → monitoring dry-run → writeback
- 上线 repo：https://github.com/witness1993x/chainstream-launch-radar （public, MIT, TypeScript, 8 topics, 13 单元测试，可用 `launch-radar scan` CLI）
- 已知小问题：post_publish 模板渲染发生在 `gh repo create --push` 之后，需要二次 commit + push 才上 GitHub —— 后续可把 post_publish 调用挪到 publish workflow 之前

### 26. ChainStream 动态 query 构建

`chainstream_query_builder.py`（独立模块）+ `--chainstream-auto-build-query / --chainstream-probe-chain-group / --chainstream-probe-data-cube`。覆盖 12 个 (chain_group, data_cube) 组合：solana 侧 7 个（DEXTrades / Transfers / BalanceUpdates / TokenHolders / WalletTokenPnL / Tokens / Pairs），evm 侧 5 个（DEXTrades / Transfers / BalanceUpdates / Tokens / Pairs）。优先级 inline > file > auto > default。`chainstream_query_from_args` 签名扩展为 `(args, config | None)`，从 `pre_build_analysis.chainstream_fit.chain_groups[0]` + `data_cubes[0]` 自动推断。**未知组合 fallback 到 `{ __schema { queryType { name } } }` introspection（0 credits）**——天然防止下游 fit 推断错误时刷掉配额。limit 参数 clamp 到 ≥1。27 个 pytest 覆盖。

### 25.6 第二个真实 ship 案例：whale-pulse-evm（2026-05-02）

**通过 framework 自身的 `agentflow-scan` 真实热点扫描决策方向**：6 路并行扫拿到 70 真实 candidates，分析显示 Polymarket / Kalshi 已饱和；ReaperProtocol/Reaper (25★) 太小；evm whale tracking 有 Solana 版（chainstream-launch-radar）但**无 EVM 多链 ChainStream-backed 等价物**——选定 `whale-pulse-evm` 作为差异化 ship。

- TypeScript / Node 20+ / ESM；ChainStream EVM Transfers 多链 (ethereum/polygon/bsc/arbitrum) GraphQL；30 个内置 wallet labels (Binance/Coinbase/Polygon Bridge/Wormhole/Uniswap Treasury 等)；7 种 transfer pattern (`exchange_inflow / outflow / cex_to_cex / bridge_movement / treasury_movement / wallet_to_wallet / unknown`); 可选 Claude Haiku 4.5 reasoning
- 25 单元测试全过；npm install/build/test 真跑 passed
- `--auto-publish-dry-run` 8 gate 全过；`--auto-publish --auto-publish-confirm --reuse-existing-workspace` 真发布
- 上线 repo: https://github.com/witness1993x/whale-pulse-evm （public, MIT, TypeScript, 9 topics）
- **真实发现 framework cwd-sensitive bug**：用户先 `cd workspace && npm install` 再不 cd 回 framework root 就跑 `agentflow-pipeline` 时，R agent 引入的 `_resolve_root() = os.getcwd()` 把 ROOT 算成 nested workspace 路径，导致 `mkdir <root>/workspaces/HSP-003-.../workspaces/HSP-003-...` 的双层嵌套。framework 仍执行 git init + push（push 到了 wrong content 的 nested repo）但用户的实际源码没上 GitHub。手动修复方式：在外层真实 workspace 用 `git init + remote add + force push` 覆盖；以及 `git rm --cached -r workspaces pipeline-pool.md` 清理误进的 framework artefacts。**待修复**：cli.py 应该在 `_run_probe_or_publish_branch` 入口处校验 case_dir 与 workspace_root 是否一致 / 当 case_dir 在 framework root 下但 cwd 不在时 fallback 到 case_dir 的 framework root

### 25.7 第三个真实 ship + cwd bug 修复 + schedule 落地（2026-05-02）

**3 路并行 W/X/Y 全部完成，339 framework pytest 全过**：

- **W (cwd bug 修复)**：`cli.py` 加 `_auto_correct_root_from_case_dir(args, current_root)` —— 当 `--case-dir` 在 canonical `cases/` 布局下且 cwd 不在该 framework root 时，自动 fallback ROOT 并 stderr warning（显式 `--root` / `AGENTFLOW_ROOT` env / `--mode pool` 都不触发）。`tests/test_cwd_fallback.py` 10 个 test 含 1 个 subprocess integration 验证 nested cwd 真实场景。HSP-003 ship 时遇到的「`mkdir <root>/workspaces/.../workspaces/...`」double-nest 故障**根因消除**。
- **X (schedule turn-key install)**：`scripts/install_schedule.{sh,md}` 写完。bash `set -euo pipefail`，9 个 flag (`--root / --label / --times / --scan-args / --apply / --dry-run / --status / --uninstall / --help`)，default fail-closed dry-run。**PATH gotcha pre-flight 守卫**：`command -v agentflow-scan` 失败立即 exit 2 并提示 `source venv/bin/activate`，先于任何写盘动作；macOS 自动 `--force` 覆盖；装完自动 `agentflow-schedule status` 校验。README.md 加 `## Schedule (twice-daily auto-scan)` 一节。
- **Y (第 3 个真实 ship: stable-depeg-radar)**：scaffold HSP-004 → 完整 Python 项目（pure stdlib runtime，0 第三方 deps；`anthropic` 是 optional `[ai-reasoning]` extra）→ `pip install -e ".[dev]"` → 72 pytest 0.08s 全过（token registry / GraphQL client / Pairs query 构造与解析 / detector 阈值各档 / 中位数抗 outlier / format pretty+JSON / cli argparse / reasoning client 注入）→ `gh repo create` + `gh api topics` 一键 11 topics → framework `apply_post_publish_templates` 渲染 7 模板（修了 ci.yml 的 `pip install build` 步骤）+ 5 README 徽章插顶部 → 二次 commit + push。Repo: https://github.com/witness1993x/stable-depeg-radar （public, MIT, Python, 11 topics）。**Y 是首个用 framework 跑出 Python 而非 TypeScript 项目**，证明 framework 不绑定语言。

**3 个独立 ship 真 portfolio**：
- chainstream-launch-radar (TypeScript) — Solana DEXTrades launch monitor
- whale-pulse-evm (TypeScript) — Multichain EVM Transfers whale tracker  
- stable-depeg-radar (Python) — Multichain stablecoin depeg early-warning

每个对应 ChainStream 不同 cube (DEXTrades / Transfers / Pairs+DEXTrades)、不同 chain group (Solana / EVM-multichain / EVM-multichain)、不同 alert pattern (launch / transfer / price-deviation)、不同语言 (TS / TS / Python)。覆盖面足够证明 framework 是真 reusable platform 而非 one-off tool。

## 当前还缺什么

- X Search 仍依赖 `X_BEARER_TOKEN`（HackerNews + Reddit 已是 token-free 替代）
- Reddit 未鉴权 JSON 限流（实测高频 discover 容易触发，已优雅降级）
- 真实 build probe 端到端：当前案例 `gate_4_buildability.build_commands` 都为空，probe 全 skip 时 verdict 停留 `hold` —— 后续可加"全 skip 时给 hold 而非 in_progress"的 readiness 注解
- 真实 publish 仍待人工触发（需明确 `repo_plan.github_owner / repo_name`，会创建真 GitHub repo，是不可逆操作）

## 当前结论

如果只问“GitHub 方案目前做到哪”，答案是：

已经从“框架设计”推进到“可运行的执行层”了，且 `discover -> pre-build analysis -> probe -> publish -> writeback` 这一条主链路已经打通。

另外，当前与这个需求直接相关的内容也已经基本收拢到 `experimental/agentflow-git-repo-clone` 下，包括：

- 规范文档
- 对齐文档
- 模板
- 示例
- pool
- cases/workspaces 入口
- 本地脚本入口
- 本地核心执行逻辑
