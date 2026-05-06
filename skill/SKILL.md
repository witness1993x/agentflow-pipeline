---
name: agentflow-pipeline
description: Git hotspot search and Git/GitHub repo delivery framework for turning a selected developer/data-source signal into a scaffolded, tested, publishable GitHub repository. Use when the user asks for Git 热点搜索, GitHub trending/repo signal search, bootstrapping an AgentFlow host repo, creating a repo-shipping case, inferring build/test commands, running probe/publish gates, publishing safely to GitHub, installing Git hotspot scan/schedule automation, or managing post-publish GitHub monitoring. This is not the blog/article hotspot package; content ideation and article publishing should use the separate article workflow. Provides 9 console scripts (agentflow-init / agentflow-pipeline / agentflow-scaffold / agentflow-scan / agentflow-schedule / agentflow-trends / agentflow-tg-listen / agentflow-tg-notify / agentflow-lark-bridge), an 8-gate fail-closed publish safety check, multi-agent friendly architecture, and a swappable DataSourcePlugin protocol so non-ChainStream backends (Bitquery, custom) plug in trivially.
---

# AgentFlow Pipeline

This skill packages the `agentflow-pipeline` Python framework — a fully tested
(494 pytest) end-to-end machine for going from a selected project idea to a
public GitHub repository with source code, publish gates, CI scaffolding, and
post-publish operations. Its scan/trends commands are **Git 热点搜索 / Git
hotspot search** inputs for repo selection, not a general blog/article
ideation system.

## When to use this skill

Invoke this skill when the user asks for **Git 热点搜索** or Git/GitHub repo
delivery work such as:

- "Git 热点搜索" / "search GitHub trending signals" / "find repo-worthy developer signals"
- "初始化一个 agentflow host repo" / "set up cases + workspaces for repo shipping"
- "scaffold a new GitHub repo case" / "turn this project idea into a repo case"
- "infer install/build/test commands" / "probe this workspace before publish"
- "run the 8 publish gates" / "dry-run auto-publish safety" / "publish to GitHub safely"
- "reuse this existing workspace and publish" / "seed CI, runbook, monitoring, issue templates"
- "schedule Git hotspot searches" / "promote a Git hotspot candidate into a repo case"
- Any reference to: ChainStream + GitHub workflow, DataSource plugin, GitHub repo
  scaffold/probe/publish/monitor lifecycle, or the shipped reference repos.

Do **not** invoke this skill for blog/article topic selection, article drafting,
newsletter operations, or content publishing. Those belong to the separate
AgentFlow article-hotspot package.

## OpenClaw plugin compatibility

This skill ships a sibling `openclaw.plugin.json` manifest so OpenClaw
runtimes can list AgentFlow as a skill/plugin alongside the official
Lark/Feishu channel plugin. AgentFlow does **not** implement the Feishu
channel itself.

For OpenClaw Lark App interaction, install and configure
`@larksuite/openclaw-lark` (`openclaw-lark`). That plugin owns the
Feishu/Lark App connection, inbound gateway, interactive cards,
permissions, and allowlists. AgentFlow remains the Python framework that
scans, promotes, stores case state, and exposes CLI/skill actions.
Only this OpenClaw Lark App mode can provide a Lark-only closed loop:
receive the Git 热点搜索 card in Lark, click Git-case actions in Lark,
and have OpenClaw forward those actions to AgentFlow.

When run **standalone**, the framework's bundled `lark_notifier` /
`tg_notifier` modules can still POST to Lark Custom Bot webhooks and
Telegram Bot APIs directly. Treat those as fallback transports, not as
the OpenClaw Lark App integration path. Standalone Lark Custom Bot is
receive-only; it cannot guarantee Lark-side operation callbacks.

Interaction vocabulary is Git-case specific:

- Telegram buttons use `case:*` callback data, for example
  `case:dry-publish:HSP-005`, `case:fork-rewrite:HSP-005`,
  `case:write-stub:HSP-005`, `case:snooze:HSP-005:7d`, and
  `case:drop:HSP-005`.
- OpenClaw-forwarded Lark actions use `git_case_*` names:
  `git_case_dry_publish`, `git_case_fork_rewrite`,
  `git_case_write_stub`, `git_case_snooze`, and `git_case_drop`.
- Do not reuse the article package's `A:*` / `B:*` / `C:*` / `D:*`
  Telegram callbacks or `lark_gate_*` Lark tool names here.

See `openclaw.plugin.json` for the configSchema this plugin accepts.

### Lark Bridge Daemon

If OpenClaw calls Python entryPoints directly, `lark_callback` is enough. If
the Lark channel posts card callbacks over HTTP, run the dedicated bridge
instead of reusing blogflow:

```bash
AGENTFLOW_PIPELINE_LARK_BRIDGE_TOKEN="$(openssl rand -hex 16)"
agentflow-lark-bridge --host 127.0.0.1 --port 7871
# OpenClaw target: http://127.0.0.1:7871/api/git-case-commands
# Compatibility path: http://127.0.0.1:7871/api/commands
```

The bridge only accepts `git_case_*` commands and forwards in-process to
`agentflow_pipeline.lark_callback.handle_event`; it rejects article-package
`lark_gate_*` commands.

## Install

The skill bundle ships the Python source in `bundle/`. To install:

```bash
# Option 1: persistent venv install (recommended for repeated use)
python3 -m venv ~/.agentflow-venv
~/.agentflow-venv/bin/pip install -e ~/.claude/skills/agentflow-pipeline/bundle
source ~/.agentflow-venv/bin/activate
# 9 console scripts now on PATH:
#   agentflow-init  agentflow-pipeline  agentflow-scaffold
#   agentflow-scan  agentflow-schedule  agentflow-trends
#   agentflow-tg-listen  agentflow-tg-notify  agentflow-lark-bridge

# Option 2: one-shot (no venv) — runs straight from the bundle
python3 -m agentflow_pipeline.cli --help
python3 -m agentflow_pipeline.scaffold --help
python3 -m agentflow_pipeline.scan_hotspots --help
```

Once installed, ALWAYS run framework commands from the host project root (or
pass `--root <path>` / `AGENTFLOW_ROOT=<path>` env). The CLI auto-corrects
ROOT from `--case-dir` so a wrong cwd no longer creates nested workspaces.

## Quick recipes

### A. "Run Git hotspot search, then pick one to ship"

```bash
agentflow-scan \
  --root . \
  --sources github,hackernews,reddit \
  --queries "solana ai agent,evm whale alert,defi mcp server,perp dex bot" \
  --top-n 30
# → trends/YYYY-MM-DD-HH/scan.{md,json}

agentflow-trends diff --root .   # after the second scan exists
```

### B. "Bootstrap a new host project"

```bash
mkdir my-data-projects && cd my-data-projects
agentflow-init .                 # creates cases/, workspaces/, pipeline-pool.md, CLAUDE.md
```

### C. "Scaffold a GitHub repo case + go end-to-end"

```bash
agentflow-scaffold --hotspot-name "EVM Whale Pulse" --owner me \
  --project-shape data_pipeline --status probe

# fill the case yaml (chainstream_fit, repo_plan, build_commands)…
# write source code in workspaces/HSP-001-…/

agentflow-pipeline --case-dir cases/HSP-001-… --auto-publish-dry-run
agentflow-pipeline --case-dir cases/HSP-001-… \
  --auto-publish --auto-publish-confirm --reuse-existing-workspace
```

In Lark App / Telegram callback mode, prefer `fork+rewrite` before publish for
repo candidates that should be rebuilt around ChainStream data. It writes
`src/chainstream-client.ts`, `src/chainstream-probe.ts`,
`.env.chainstream.example`, `chainstream/probe.graphql`, and
`CHAINSTREAM_REWRITE.md` into `workspaces/<case>/`, then records
`execution_state.chainstream_rewrite` in the case YAML.

### D. "Install twice-daily auto-scan"

```bash
bash bundle/scripts/install_schedule.sh --root . --apply
# macOS launchd: 09:00 + 21:00 → agentflow-scan into <root>/trends/
# verify: bash bundle/scripts/install_schedule.sh --status
# uninstall: bash bundle/scripts/install_schedule.sh --uninstall --apply
```

### E. "Switch off ChainStream to a different data source"

```bash
AGENTFLOW_DATA_SOURCE=bitquery agentflow-pipeline --case-dir … --mode data-probe --execute
# or, per-call:
agentflow-pipeline --data-source bitquery --case-dir … --mode discover --execute
# Implement your own by satisfying the DataSourcePlugin protocol and registering it.
```

### F. "Check ChainStream docs while rewriting a repo"

Use these references when selecting ChainStream cubes, writing GraphQL, or
reviewing the files generated by `fork+rewrite`:

- Docs: https://docs.chainstream.io/
- GraphQL overview: https://docs.chainstream.io/en/graphql/getting-started/overview
- First query guide: https://docs.chainstream.io/en/graphql/getting-started/first-query
- Access methods: https://docs.chainstream.io/en/docs/access-methods/overview
- GraphQL IDE: https://ide.chainstream.io
- LLM reference index: https://docs.chainstream.io/llms.txt
- Endpoint: `https://graphql.chainstream.io/graphql`

## Safety-critical defaults Claude should respect

- **Publish is irreversible**: `agentflow-pipeline --auto-publish` runs the
  8-gate `check_auto_publish_safety` — it will refuse without
  `--auto-publish-confirm`, even when readiness=ready. Always show the
  dry-run output (`--auto-publish-dry-run`) to the user first; never pass
  `--auto-publish-confirm` unprompted.
- **`--apply` everywhere is opt-in**: schedule install, monitoring secrets,
  branch protection — all default to dry-run. Never add `--apply` without
  explicit user authorization in the same turn.
- **API keys go through env vars only**: `CHAINSTREAM_API_KEY`,
  `ANTHROPIC_API_KEY`, etc. Never `--key=...` on the command line. The
  framework does not log keys; Claude must not echo them either.
- **`pool` mode forbids `publish`**: parallel pool runner rejects publish
  to prevent fan-out misfires. This is intentional.
- **`reuse-existing-workspace`** is the right flag when source code was
  written manually before publish (typical for hand-coded ship). Without it
  the framework requires an empty workspace.

## Architecture cheat sheet

The bundle ships ~17 focused modules under `agentflow_pipeline.*`:

| Module | What it does |
|---|---|
| `cli.py` | Main entry, all modes (inspect/discover/data-probe/kafka-probe/probe/publish/pool) |
| `scaffold.py` | Generate case 5-tuple from templates |
| `scan_hotspots.py` | Multi-source single-shot scan → trends/ |
| `trends_diff.py` | New / rising entry detection across scan history |
| `schedule_installer.py` | Generate launchd plist / systemd timer |
| `init_command.py` | One-shot host-project bootstrap |
| `dedup_candidates.py` | URL canonicalize + cross-source merge |
| `extra_sources.py` | HackerNews + Reddit token-free sources |
| `topics_enrichment.py` | gh api repos topics 2nd-pass enrichment |
| `chainstream_query_builder.py` | (chain_group, data_cube) → GraphQL query |
| `data_source.py` | Pluggable DataSourcePlugin protocol |
| `auto_publish.py` | 8-gate fail-closed publish guard |
| `build_command_inference.py` | Manifest scan → install/build/test commands |
| `kafka_probe.py` | Kafka data-probe (confluent-kafka or kafka-python) |
| `monitoring_setup.py` | gh secret set / branch protection / dependabot / RUNBOOK |
| `monitoring_grafana_pagerduty.py` | Grafana dashboard + PagerDuty service |
| `post_publish.py` | Render templates/post-publish/* into a fresh repo |
| `pool_runner.py` / `pool_advancer.py` | Cross-case parallel + readiness-driven advance |

## Reference implementations Claude can mimic

These are real shipped repos that used this framework end-to-end — perfect
templates for new projects in similar shape:

- https://github.com/witness1993x/chainstream-launch-radar (TypeScript, Solana DEXTrades, memecoin launch monitor)
- https://github.com/witness1993x/whale-pulse-evm (TypeScript, EVM Transfers multichain, whale tracker)
- https://github.com/witness1993x/stable-depeg-radar (Python, ChainStream Pairs+DEXTrades, stablecoin depeg early-warning)

Each has the same skeleton: ChainStream client + scanner + format + CLI +
optional Claude reasoning + tests + post-publish scaffolding.

## Troubleshooting

- **Cloudflare 1010 from ChainStream**: client must send a real User-Agent.
  Framework `http_json` already does (`agentflow-pipeline/0.1`). External
  scripts must do the same.
- **Reddit returns nothing**: rate-limited (10 req/min/IP for unauth JSON).
  Other sources still work; the framework marks reddit blocked and continues.
- **`stable-radar` style projects use Pure stdlib runtime** — no `requests`,
  no `aiohttp`. Keeps install footprint tiny and avoids dep churn.

## Don't do

- Don't run `--mode publish --execute --allow-publish` directly without going
  through `--auto-publish-dry-run` first.
- Don't `cd` into a workspace and then run framework CLI without `--root` —
  the auto-correct will save you, but it shouldn't be needed.
- Don't fork an archived candidate; `recommend_fork_or_build` already routes
  archived repos to `build_new` automatically — respect that.
- Don't put API keys in case yamls or memo markdown — only in env vars.
