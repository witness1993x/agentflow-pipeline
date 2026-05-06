# agentflow-pipeline

**End-to-end pipeline framework for turning crypto/AI hotspots into shipped GitHub repos backed by [ChainStream](https://chainstream.io) (or pluggable) on-chain data.**

[![tests](https://img.shields.io/badge/tests-480%20passing-brightgreen)]() [![python](https://img.shields.io/badge/python-3.11%2B-blue)]() [![license](https://img.shields.io/badge/license-MIT-lightgrey)]() [![version](https://img.shields.io/badge/version-0.4.2-blue)]()

> Why this exists: the path from "I noticed Pump.fun radars are trending" to "a public GitHub repo doing something useful with that signal" usually involves 30+ disconnected manual steps — gh search, HN scraping, Reddit scraping, dedup, market analysis, scaffold, write, npm/pip install, build, test, gh repo create, secrets, CI, runbook, monitoring. This framework collapses that into 8 console scripts + a fail-closed 8-gate publish guard, with a pluggable data-source layer so it isn't married to one chain explorer.
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
- **480 pytest, 0 flaky, all offline** (no network calls in tests)

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

## Lark / Feishu integration

AgentFlow supports two Lark paths with different ownership boundaries:

- **OpenClaw Lark App mode (recommended for OpenClaw)**: install and configure
  the official [`@larksuite/openclaw-lark`](https://github.com/larksuite/openclaw-lark)
  channel plugin. It owns the Lark App connection, message gateway,
  interactive cards, permissions, and allowlists. AgentFlow remains the Python
  skill / CLI that scans, promotes, and handles case actions.
- **Standalone webhook fallback**: when running outside OpenClaw,
  `agentflow-scan --notify-lark` can still post a one-way Lark Custom Bot card
  through `LARK_WEBHOOK_URL`.

### OpenClaw Lark App mode

Use this when you want real Lark App interaction instead of a webhook-only bot.
Install/configure the official OpenClaw plugin first, then install AgentFlow as
a companion skill:

```bash
# Requirements from the official plugin:
# - Node.js >= 22
# - OpenClaw >= 2026.2.26
npm install -g openclaw
openclaw plugins install npm:@larksuite/openclaw-lark

# Then install AgentFlow skill zip as usual.
curl -L -o /tmp/agentflow-skill.zip \
  https://github.com/witness1993x/agentflow-pipeline/releases/latest/download/agentflow-pipeline-skill.zip
unzip /tmp/agentflow-skill.zip -d ~/.claude/skills/agentflow-pipeline
```

Configure the Lark App credentials, connection mode, and allowlists in
OpenClaw's `channels.feishu` config. In this mode AgentFlow does **not** claim
to provide a Feishu channel; OpenClaw routes Lark messages/cards through
`@larksuite/openclaw-lark`, and AgentFlow provides the skill commands and case
state the agent operates on.

### Standalone webhook fallback

After every `agentflow-scan` run, optionally post a summary card to a Lark /
Feishu group through a Lark Custom Bot webhook:

```bash
cp .env.lark.example .env
# edit .env, paste your Lark webhook URL into LARK_WEBHOOK_URL

set -a && source .env && set +a
bash scripts/install_schedule.sh \
  --times "10:00" \
  --scan-args "--notify-lark" \
  --label com.agentflow.scan.daily-10am \
  --apply
```

The card includes the day's top hotspots, all shipped framework repos
(auto-discovered from `cases/HSP-*/`), and one-click buttons to the
framework repo + each shipped repo. See `scripts/install_schedule.md`
for the full env reference + macOS launchd PATH/env gotchas.

## TG callback (interactive HITL)

After receiving a Lark / TG card, click an inline button to invoke a
framework action **without leaving the chat**:

- **`📊 dry-publish`** — runs the 8-gate `check_auto_publish_safety`
  for that case; replies with pass/fail + blocker list
- **`🤖 write-stub`** — generates a minimal TypeScript skeleton (uses
  Claude Haiku via `ANTHROPIC_API_KEY` if set; static fallback otherwise)
- **`🚮 drop`** — marks the case `final_status=drop` + appends review log
- **`💤 snooze 7d`** — pushes `next_review_date` forward 7 days

In OpenClaw Lark App mode, use the official `openclaw-lark` channel for real
Lark card interaction and permission policy. The Telegram listener below is a
standalone fallback for deployments that do not run OpenClaw's Lark App
channel.

### Setup (one-time)

```bash
# 1. Create a Telegram bot via @BotFather, copy the token
# 2. Add the bot to your group chat, then in the same chat send "/start"
#    so the bot can fetch chat history
# 3. Set env:
echo 'TELEGRAM_BOT_TOKEN=…' >> .env
echo 'TELEGRAM_CHAT_ID=…' >> .env
echo 'TELEGRAM_CALLBACK_SECRET=…(random ≥16-char string)' >> .env
set -a && source .env && set +a
launchctl setenv TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
launchctl setenv TELEGRAM_CALLBACK_SECRET "$TELEGRAM_CALLBACK_SECRET"

# 4. Install the daemon (dry-run first, then --apply)
bash scripts/install_tg_listener.sh
bash scripts/install_tg_listener.sh --apply
```

The installer wraps `agentflow-schedule install --mode daemon`, which
generates a launchd plist with `RunAtLoad=true` + `KeepAlive` (dict
form: restart on crash / wait for network / honour clean exits) +
`ThrottleInterval=10s` to prevent rapid-restart loops. On linux the
equivalent is a `Type=simple` systemd service with
`Restart=on-failure`. Verify with:

```bash
launchctl list | grep tg-listener        # macOS
systemctl --user status com.agentflow.tg-listener.service  # linux
```

### Standalone Lark → TG deep link bridge

Lark Custom Bot is push-only — buttons can't trigger callbacks. Outside
OpenClaw, configure the daily scan with
`--lark-cta-tg-bot @your_bot_username`: the auto-promoted-cases button
on the Lark card will become a deep link
`https://t.me/your_bot?start=case_HSP-XXX_dry_publish`. The
`agentflow-tg-listen` daemon handles that `/start` payload and dispatches
the same case action as an inline TG button. This way Lark stays as the
high-signal push channel while interactive HITL flows through the TG
daemon. This is not a replacement for the OpenClaw Lark App channel.

For existing Lark schedules, you can set `LARK_CTA_TG_BOT=@your_bot_username`
instead of changing `--scan-args`. When neither the flag nor env var is set,
Lark cards keep the legacy source-url promoted button.

### Security model

Always set `TELEGRAM_CALLBACK_SECRET` (a random ≥16-char string injected
into every `callback_data`). The daemon ignores any callback whose data
doesn't start with the secret prefix, blocking spoofed callbacks.
For Lark deep links, also keep `TELEGRAM_CHAT_ID` / `--chat-id-allowlist`
configured: `/start` payloads are visible URLs, so chat allowlisting is
the trust boundary for those actions. `install_tg_listener.sh` uses
`TELEGRAM_CHAT_ID` as the default allowlist; pass
`--chat-id-allowlist "group_id,user_id"` when you need both group
callbacks and private deep-link clicks:

```bash
bash scripts/install_tg_listener.sh \
  --chat-id-allowlist "12345678,87654321" \
  --apply
```

Without an allowlist any chat the bot is invited to can fire actions,
which is fine for solo use but unsafe for multi-tenant.

## Auto-promote (level B half-automation, optional)

`agentflow-scan --auto-promote` lets the scanner automatically scaffold a
new case (the 5-tuple of `01-hotspot-intake.md / 02-pipeline-gate.yaml /
03-publish-decision-memo.md / 04-build-probe-run.md / 05-review-checkpoint.md`)
for any newly-discovered hotspot whose engagement is high enough. **It does
not write source code and does not publish** — it only generates the case
skeleton so the operator can review the auto-filled hotspot intake and decide
whether to write code + ship.

### Safety model (mirrors auto-publish)

- `--auto-promote` alone is dry-run (prints what it would create)
- `--auto-promote --auto-promote-apply` is the real-create double flag
- Hard cap per scan run: `--auto-promote-max 1` (default 1)
- Engagement floor: `--auto-promote-min-engagement 150` (default 150 — sum of
  GitHub stars + HN points + Reddit score across the entry's appearances)
- "New" means: not in any of the past `--auto-promote-baseline-window`
  scans (default 14 ≈ 1 week at twice-daily) AND not already a shipped repo
  (`discover_shipped_repos` filter)
- Promotion failures never break the scan; scan still exits 0

### Daily 10:00 launchd config (recommended)

To upgrade your existing `com.agentflow.scan.daily-10am` job:

```bash
source /tmp/agentflow-venv/bin/activate
agentflow-schedule install \
  --platform macos \
  --label com.agentflow.scan.daily-10am \
  --root /path/to/host-project \
  --times "10:00" \
  --scan-args="--notify-lark --auto-promote --auto-promote-apply --auto-promote-max 1 --auto-promote-min-engagement 150" \
  --apply --force
```

After install, the daily 10:00 run will:
1. Scan all configured sources → write `<root>/trends/YYYY-MM-DD-10/scan.{md,json}`
2. Detect new high-engagement entries vs the past 14 scans
3. Auto-scaffold up to 1 new case (the 5-tuple in `<root>/cases/HSP-XXX-...`)
4. Post a Lark card containing top hotspots + shipped repos + the new case
   listing + a "📝 promoted [N]" button linking to the source URL

You then review the auto-generated case yamls — at your leisure — and decide
which (if any) to write code for and `--auto-publish` later.

### Tuning

- Make threshold higher → fewer auto-promoted cases:
  `--auto-promote-min-engagement 300`
- Lower the cap to 0 → all promotion paths become "report only":
  `--auto-promote-max 0`
- Different default owner string (shows up in the case meta):
  `--auto-promote-owner my-team`

### Disable promotion without uninstalling the schedule

Edit the plist's `--scan-args` to drop `--auto-promote-apply` (keep
`--auto-promote` for dry-run reporting), or just remove both. Re-run
`agentflow-schedule install ... --apply --force` to push the change.

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

## One-shot install

After downloading the skill zip from a release, run the bundled installer:

```bash
unzip agentflow-pipeline-skill.zip -d ~/.claude/skills/agentflow-pipeline
cd ~/.claude/skills/agentflow-pipeline
bash install.sh                    # detects macOS / linux, creates venv, registers entry points
# … then edit .env to add LARK_WEBHOOK_URL / TELEGRAM_BOT_TOKEN, then re-run:
bash install.sh --auto-promote     # also enables level-B auto-promote in the daily 10:00 job
```

The installer is idempotent — re-running it skips already-completed steps
(venv exists, .env filled, launchd loaded) and only refreshes what changed.

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

480 pytest covering every module, zero network calls.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [ChainStream](https://chainstream.io) for the on-chain GraphQL backend that makes single-query multichain queries possible
- [Anthropic Claude Code](https://docs.anthropic.com/en/docs/claude-code) for the Agent Skill spec we package against
- [OpenClaw / free-claude-code](https://github.com/Alishahryar1/free-claude-code) for the OSS Claude Code-compatible host that loads this skill
