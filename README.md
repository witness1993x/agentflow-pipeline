# agentflow-pipeline

**End-to-end pipeline framework for turning crypto/AI hotspots into shipped GitHub repos backed by [ChainStream](https://chainstream.io) (or pluggable) on-chain data.**

[![tests](https://img.shields.io/badge/tests-339%20passing-brightgreen)]() [![python](https://img.shields.io/badge/python-3.11%2B-blue)]() [![license](https://img.shields.io/badge/license-MIT-lightgrey)]()

> Why this exists: the path from "I noticed Pump.fun radars are trending" to "a public GitHub repo doing something useful with that signal" usually involves 30+ disconnected manual steps — gh search, HN scraping, Reddit scraping, dedup, market analysis, scaffold, write, npm/pip install, build, test, gh repo create, secrets, CI, runbook, monitoring. This framework collapses that into 6 console scripts + a fail-closed 8-gate publish guard, with a pluggable data-source layer so it isn't married to one chain explorer.
>
> Battle-tested on three real shipped repos (links below).

## Install

```bash
git clone https://github.com/witness1993x/agentflow-pipeline
cd agentflow-pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Six console scripts now on PATH:

| Script | Purpose |
|---|---|
| `agentflow-init <dir>` | Bootstrap a host project (`cases/`, `workspaces/`, `pipeline-pool.md`, `CLAUDE.md`, `.agentflow.toml`) |
| `agentflow-scaffold` | Generate a new case skeleton (5 files: intake / gate yaml / memo / probe-run / review) |
| `agentflow-pipeline` | Main runner — `inspect / discover / data-probe / kafka-probe / probe / publish / pool` modes |
| `agentflow-scan` | Single-shot multi-source hotspot scan → `<root>/trends/YYYY-MM-DD-HH/scan.{md,json}` |
| `agentflow-trends` | Diff scan history, detect new / rising entries, optionally promote to a case |
| `agentflow-schedule` | Generate launchd plist (macOS) or systemd timer (linux) for twice-daily auto-scan |

## Quick tour

```bash
# 1. Make a host project
mkdir my-data-projects && cd my-data-projects
agentflow-init .

# 2. Find what's hot (multi-source, dedup, no token needed for HN/Reddit)
agentflow-scan --sources github,hackernews,reddit \
  --queries "solana ai agent,evm whale alert,defi mcp server" --top-n 30

# 3. Scaffold a case from a hotspot
agentflow-scaffold --hotspot-name "EVM Whale Pulse" --owner me \
  --project-shape data_pipeline --status probe

# 4. Fill the case yaml (chainstream_fit, repo_plan, build_commands), write source

# 5. Dry-run the 8 publish gates first — never blind-publish
agentflow-pipeline --case-dir cases/HSP-001-… --auto-publish-dry-run

# 6. When all gates pass, with explicit confirmation
agentflow-pipeline --case-dir cases/HSP-001-… \
  --auto-publish --auto-publish-confirm --reuse-existing-workspace

# 7. Optional: install twice-daily auto-scan
bash scripts/install_schedule.sh --apply
```

## Key features

- **Multi-source discovery**: GitHub (gh CLI), HackerNews (Algolia), Reddit (JSON), Jina, X/Twitter — token-free where possible, graceful per-source degradation
- **Cross-source candidate dedup**: URL canonicalize + score merge across `github_search / jina_search / hackernews / reddit` etc.
- **Pluggable DataSource**: Swap ChainStream for Bitquery (or your own) by satisfying the `DataSourcePlugin` Protocol; CLI `--data-source <name>` or `AGENTFLOW_DATA_SOURCE` env
- **8-gate fail-closed publish**: readiness=ready, never-published-before, repo_plan filled, no veto, no kill-signals triggered, chainstream_fit verdict pass — all checked, all opt-out only via explicit double flag
- **Cwd-safe**: CLI auto-corrects `ROOT` from `--case-dir` so a wrong cwd never creates nested workspaces
- **Build command auto-inference**: scans `package.json / pyproject.toml / Cargo.toml / go.mod / Makefile / Dockerfile` + `candidate.language` fallback, with confidence threshold + only-if-empty default
- **Pool mode**: parallel cross-case execution via subprocess isolation; `--pool-auto-advance` selects next mode per case from `publish_readiness`
- **Twice-daily scheduler**: `agentflow-schedule install --apply` writes a launchd plist or systemd timer that runs `agentflow-scan` at 09:00 + 21:00 local
- **Trends diff**: `agentflow-trends diff` finds new / rising entries across scan history, optionally `promote --apply` straight into a new case
- **Post-publish scaffolding**: CI workflow + ISSUE/PR templates + CODEOWNERS + RUNBOOK + MONITORING + README badges all rendered into the freshly created repo
- **Real monitoring hooks** (opt-in): `gh secret set` + branch protection + dependabot + Grafana dashboard + PagerDuty service; all default to dry-run; `integration_key` redacted in logs / state
- **339 pytest, 0 flaky, all offline** (no network calls in tests)

## Real shipped reference repos

These three were shipped end-to-end with this framework on different days, deliberately covering different chains / cubes / languages — perfect templates to fork or mimic:

| Repo | Language | ChainStream cube | Pattern |
|---|---|---|---|
| [chainstream-launch-radar](https://github.com/witness1993x/chainstream-launch-radar) | TypeScript | Solana DEXTrades | Memecoin launch monitor |
| [whale-pulse-evm](https://github.com/witness1993x/whale-pulse-evm) | TypeScript | EVM Transfers (4 chains) | Whale wallet tracker |
| [stable-depeg-radar](https://github.com/witness1993x/stable-depeg-radar) | Python | Pairs + DEXTrades | Stablecoin depeg early-warning |

## Schedule (twice-daily auto-scan)

After `pip install -e .`, with the venv activated:

```bash
bash scripts/install_schedule.sh --root /path/to/host-project --apply
```

Default: 09:00 + 21:00 local time, label `com.agentflow.scan.daily` (macOS) / `agentflow-scan-daily` (linux). Verify with `bash scripts/install_schedule.sh --status`. Uninstall with `--uninstall --apply`.

See [`scripts/install_schedule.md`](scripts/install_schedule.md) for the macOS PATH gotcha, custom times, and systemd setup details.

## Use as a library in another project

The framework resolves `cases/`, `workspaces/`, and `pipeline-pool.md` relative to a configurable host root. Resolution order: `--root <path>` > `AGENTFLOW_ROOT` env > `Path.cwd()`. The CLI also auto-detects framework root from `--case-dir` / `--gate-file`, so you can run `agentflow-pipeline` from any cwd (including inside a workspace) without `--root` and the path won't get nested.

```bash
pip install -e /path/to/agentflow-pipeline
mkdir my-host-project && cd my-host-project
agentflow-init .
agentflow-scaffold --hotspot-name "My Hotspot" --owner me
agentflow-pipeline --case-dir cases/HSP-001-… --mode discover --execute
```

## As a Claude Code / OpenClaw skill

The `skill/` directory is a self-contained Claude Code Agent Skill. Drop the prebuilt zip into `~/.claude/skills/`:

```bash
# Download the latest skill release zip (also at GitHub Releases)
curl -L -o /tmp/agentflow-skill.zip \
  https://github.com/witness1993x/agentflow-pipeline/releases/latest/download/agentflow-pipeline-skill.zip
unzip /tmp/agentflow-skill.zip -d ~/.claude/skills/agentflow-pipeline
# Then in Claude Code, simply invoke any of the recipes — Claude auto-discovers via SKILL.md frontmatter.
```

For details on what the skill exposes, see [`skill/SKILL.md`](skill/SKILL.md).

## Architecture

```
src/agentflow_pipeline/
├── cli.py                          # main runner (~2200 lines)
├── scaffold.py                     # generate case 5-tuple
├── init_command.py                 # host-project bootstrap
├── scan_hotspots.py                # multi-source single-shot scan
├── trends_diff.py                  # new / rising detection
├── schedule_installer.py           # launchd plist + systemd timer
├── dedup_candidates.py             # URL canonicalize + merge
├── extra_sources.py                # HN + Reddit
├── topics_enrichment.py            # gh api topics
├── chainstream_query_builder.py    # (chain_group, data_cube) → GraphQL
├── data_source.py                  # DataSourcePlugin protocol + ChainStream + Bitquery
├── auto_publish.py                 # 8-gate fail-closed guard
├── build_command_inference.py      # manifest scan
├── kafka_probe.py                  # confluent-kafka / kafka-python
├── monitoring_setup.py             # gh secret / branch protection / RUNBOOK
├── monitoring_grafana_pagerduty.py # Grafana dashboard + PagerDuty service
├── post_publish.py                 # render templates/post-publish into new repo
├── pool_runner.py                  # cross-case subprocess parallel
├── pool_advancer.py                # readiness-driven mode selection
└── templates/                      # case scaffolding + post-publish templates
```

339 pytest covering every module, 0.31s end-to-end, zero network calls.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [ChainStream](https://chainstream.io) for the on-chain GraphQL backend that makes single-query multichain queries possible
- [Anthropic Claude Code](https://docs.anthropic.com/en/docs/claude-code) for the Agent Skill spec we package against
- [OpenClaw / free-claude-code](https://github.com/Alishahryar1/free-claude-code) for the OSS Claude Code-compatible host that loads this skill
