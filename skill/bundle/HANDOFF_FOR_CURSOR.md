# Handoff for Cursor — agentflow-pipeline

**Last updated**: 2026-05-06 by Cursor
**State**: v0.4.2 patch in progress; 480 pytest passing; OpenClaw Lark App integration is corrected to use the official `@larksuite/openclaw-lark` Feishu channel plugin. AgentFlow remains a Python skill/framework with standalone webhook/TG fallback transports.

---

## TL;DR

The framework is now a **fully functional Claude Code / OpenClaw skill** that:

1. **Scans** crypto/AI hotspots from GitHub + HackerNews + Reddit on a daily 10:00 launchd timer
2. **Auto-promotes** newly-discovered high-engagement entries into case scaffolds (level-B half-automation, no code generation, no publish)
3. **Notifies** results via standalone Lark webhook / Telegram fallback, or via OpenClaw's official Lark App channel when `@larksuite/openclaw-lark` is installed
4. **Receives interactive callbacks** from Telegram (long-poll daemon) — clicking inline buttons triggers framework actions: `dry-publish`, `write-stub`, `drop`, `snooze`
5. **Ships as a single skill zip** that OpenClaw can list as an AgentFlow skill/plugin alongside the official Lark channel plugin

What remains: commit/push the v0.4.2 patch and publish a GitHub release once verified.

---

## Repository state

**Repo**: https://github.com/witness1993x/agentflow-pipeline
**Local path**: `/Users/witness/Desktop/experimental/agentflow-git-repo-clone`
**Last published release**: v0.4.1 (standalone Lark webhook compatibility + TG deep-link bridge)
**Pending release**: v0.4.2 (OpenClaw Lark App alignment via official `@larksuite/openclaw-lark`)

### Test count history

| Version | Tests | What's in |
|---|---|---|
| v0.1.0 | 270 | namespace package, 3 console scripts |
| v0.2.0 | 371 | + Lark webhook outbound |
| v0.3.0 | 386 | + level-B auto-promote |
| v0.4.0 | 471 | + tg_notifier + tg_callback_listener + case_actions + notification_templates + install.sh + openclaw.plugin.json |
| v0.4.1 | 477 | standalone Lark webhook compatibility + TG `/start` deep-link bridge |
| **v0.4.2** | **480** | OpenClaw Lark App alignment via official `@larksuite/openclaw-lark` |

### Current working-tree changes (uncommitted)

```
M  .env.lark.example                                # +TG block
M  README.md                                        # +TG callback section, +One-shot install
M  pyproject.toml                                   # +agentflow-tg-listen entry script
M  scripts/install_schedule.md                      # +TG callback daemon section
M  skill/SKILL.md                                   # +OpenClaw compatibility section
M  src/agentflow_pipeline/__init__.py               # +new exports (case_actions, tg_*, notification_templates)
M  src/agentflow_pipeline/lark_notifier.py          # rewired to use notification_templates
M  src/agentflow_pipeline/schedule_installer.py     # +daemon mode
M  tests/test_schedule_installer.py                 # +3 daemon tests
?? install.sh                                        # NEW one-shot installer
?? openclaw.plugin.json                              # NEW OpenClaw plugin manifest
?? scripts/install_tg_listener.sh                    # NEW TG daemon installer
?? skill/openclaw.plugin.json                        # NEW (sibling copy)
?? src/agentflow_pipeline/case_actions.py            # NEW (4 handlers)
?? src/agentflow_pipeline/notification_templates.py  # NEW (Lark + TG templates)
?? src/agentflow_pipeline/tg_callback_listener.py    # NEW (long-poll daemon)
?? src/agentflow_pipeline/tg_notifier.py             # NEW (TG outbound)
?? templates/                                        # NEW (lark_scan_card.tpl, tg_scan_card.tpl)
?? tests/test_case_actions.py                        # NEW 19 tests
?? tests/test_notification_templates.py              # NEW 20 tests
?? tests/test_tg_callback_listener.py                # NEW 16 tests
?? tests/test_tg_notifier.py                         # NEW 27 tests
```

---

## What the 6 parallel agents delivered

### L1 — `install.sh` + OpenClaw manifest

- `/install.sh` (chmod +x, ~370 lines bash) — 9-stage idempotent installer: pre-flight → venv → `pip install -e` → verify console scripts → detect OpenClaw skill mode → Lark `.env` setup → optional TG → launchd install → final report
- `/openclaw.plugin.json` + `/skill/openclaw.plugin.json` (mirrored) — declares AgentFlow as a skill/plugin (`skills: ["./skill"]`) and intentionally does **not** claim Feishu channel ownership; OpenClaw Lark App mode requires the official `@larksuite/openclaw-lark` companion plugin
- README has new `## One-shot install` section

### L2 — `notification_templates` abstraction

- `/src/agentflow_pipeline/notification_templates.py` — `string.Template`-based renderer with `DEFAULT_LARK_SCAN_CARD_TPL`, `DEFAULT_TG_SCAN_CARD_TPL`, `resolve_template()`, `render_scan_card()`, plus `render_top_section / shipped / promoted` helpers
- `/templates/notifications/lark_scan_card.tpl` + `tg_scan_card.tpl` — operator-customizable defaults
- `lark_notifier.py` rewired to use the template path, **byte-for-byte v0.3.0 compatible** (verified by `test_notify_scan_complete_omitting_auto_promoted_matches_legacy`)
- Resolution order: `$AGENTFLOW_TEMPLATES_DIR/<name>.tpl` > `<host_root>/templates/notifications/<name>.tpl` > bundled `DEFAULT_*_TPL`
- 20 new pytest

### L3 — `tg_notifier` outbound

- `/src/agentflow_pipeline/tg_notifier.py` — pure stdlib urllib, `send_text / send_card / notify_scan_complete / TgSendResult`
- Key features: MarkdownV2 escape, 4096-char chunking (only first chunk gets reply_markup), `inline_keyboard` 8 buttons / 3 per row cap, callback_data 64-byte limit (UTF-8 safe truncate), 100ms rate-limit lock, fail-quiet
- New entry script: `agentflow-tg-notify` (debug + cron use)
- `__init__.py` exports as `tg_send_text / tg_send_card / tg_notify_scan_complete / TgSendResult` (avoiding lark name conflict)
- 27 new pytest

### L4 — `tg_callback_listener` daemon

- `/src/agentflow_pipeline/tg_callback_listener.py` — pure stdlib long-poll daemon
- API: `TgCallbackListener(...)` with `run_forever()` / `run_once()` / `stats` property
- 3-layer auth: `allowed_chat_ids` whitelist + `callback_secret` prefix + per-chat sliding-window rate-limit (default 10/min)
- SIGTERM/SIGINT graceful exit; URLError exponential backoff (1→30s); stats persist to `<host_root>/trends/_listener.stats.json`
- New entry script: `agentflow-tg-listen` with `--once / --stats / --quiet / --chat-id-allowlist / --callback-secret-env`
- Default action_dispatcher → `case_actions.dispatch_callback_action`
- 16 new pytest

### L5 — `case_actions` handlers

- `/src/agentflow_pipeline/case_actions.py` — 4 callback handlers:
  - `case:dry-publish:HSP-XXX` — runs 8-gate `check_auto_publish_safety`, returns blocker list
  - `case:write-stub:HSP-XXX` — generates minimal TS skeleton (uses Claude Haiku via `ANTHROPIC_API_KEY` if set; static fallback otherwise)
  - `case:drop:HSP-XXX` — sets `decision.final_status="drop"` + appends review_log
  - `case:snooze:HSP-XXX:Nd` — pushes `next_review_date` forward N days (1-30)
- `dispatch_callback_action(callback_data, *, root, actor)` parses + routes
- All handlers idempotent + fail-quiet
- 19 new pytest

### L6 — Daemon plist + docs

- `schedule_installer.py` extended with `mode: "cron" | "daemon"` field on `ScheduleSpec`
- macOS daemon plist: drops `StartCalendarInterval`, adds `RunAtLoad=true / KeepAlive=<dict>` + `ThrottleInterval=10`
- linux daemon: `Type=simple / Restart=on-failure`, no timer
- `build_default_listener_spec()` for `agentflow-tg-listen`
- `/scripts/install_tg_listener.sh` (chmod +x) — turn-key TG daemon installer
- README: `## Lark / Feishu integration` now separates OpenClaw Lark App mode from standalone webhook/TG fallback; `## TG callback (interactive HITL)` covers standalone Telegram actions
- `scripts/install_schedule.md`: `## TG callback daemon (interactive)` section (full plist sample + KeepAlive dict + launchctl setenv gotcha + verify + uninstall)
- `.env.lark.example`: TG block (4 env vars + comments)
- 3 new pytest

---

## Architecture cheat sheet

```
src/agentflow_pipeline/                          # 21 modules
├── cli.py                          v0.1.0  Main runner (all modes)
├── scaffold.py                     v0.1.0  Case 5-tuple generator
├── init_command.py                 v0.1.0  Host-project bootstrap
├── scan_hotspots.py                v0.2-3  Multi-source scan + --notify-lark + --auto-promote
├── trends_diff.py                  v0.2.0  New / rising entry detection
├── schedule_installer.py           v0.4.0  Cron + daemon launchd/systemd  ⬅ NEW: daemon mode
├── dedup_candidates.py             v0.1.0  URL canonicalize + merge
├── extra_sources.py                v0.1.0  HN + Reddit
├── topics_enrichment.py            v0.1.0  gh api topics
├── chainstream_query_builder.py    v0.1.0  (chain, cube) → GraphQL
├── data_source.py                  v0.1.0  DataSourcePlugin protocol
├── auto_publish.py                 v0.1.0  8-gate fail-closed
├── build_command_inference.py      v0.1.0  Manifest scan
├── kafka_probe.py                  v0.1.0  confluent-kafka / kafka-python
├── monitoring_setup.py             v0.1.0  gh secret / branch protect
├── monitoring_grafana_pagerduty.py v0.1.0  Grafana + PagerDuty
├── post_publish.py                 v0.1.0  Render templates/post-publish/
├── pool_runner.py                  v0.1.0  Cross-case parallel
├── pool_advancer.py                v0.1.0  Readiness-driven mode select
├── lark_notifier.py                v0.2.0  Lark webhook + cards     (v0.4: rewired to templates)
├── notification_templates.py       v0.4.0  Template renderer (Lark + TG) ⬅ NEW
├── tg_notifier.py                  v0.4.0  Telegram outbound          ⬅ NEW
├── tg_callback_listener.py         v0.4.0  Telegram callback daemon   ⬅ NEW
└── case_actions.py                 v0.4.0  Callback action handlers   ⬅ NEW

skill/
├── SKILL.md                        Anthropic Agent Skill manifest
├── openclaw.plugin.json            ⬅ NEW: OpenClaw plugin manifest
└── bundle/                         (rebuilt at zip time)

templates/
├── notifications/                  ⬅ NEW
│   ├── lark_scan_card.tpl
│   └── tg_scan_card.tpl
└── post-publish/                   v0.1.0 unchanged

scripts/
├── install_schedule.sh             v0.2.0
├── install_schedule.md             v0.4.0 (+TG callback section)
└── install_tg_listener.sh          ⬅ NEW

install.sh                          ⬅ NEW (root-level one-shot)
openclaw.plugin.json                ⬅ NEW (sibling to skill/)
```

---

## Console scripts (7 total in v0.4.0)

```toml
[project.scripts]
agentflow-pipeline = "agentflow_pipeline.cli:_main_entry"
agentflow-scaffold = "agentflow_pipeline.scaffold:main"
agentflow-init = "agentflow_pipeline.init_command:main"
agentflow-scan = "agentflow_pipeline.scan_hotspots:main"
agentflow-trends = "agentflow_pipeline.trends_diff:main"
agentflow-schedule = "agentflow_pipeline.schedule_installer:main"
agentflow-tg-listen = "agentflow_pipeline.tg_callback_listener:_main_entry"   # NEW
# Note: agentflow-tg-notify mentioned in L3 doc but L3 didn't add it to pyproject.
# Cursor: VERIFY agentflow-tg-notify entry exists; add if missing.
```

⚠️ **Known issue for cursor**: L3 said it added `agentflow-tg-notify` to pyproject but the on-disk pyproject.toml only shows `agentflow-tg-listen`. Either L3 misreported OR a later agent overwrote it. **Verify**:

```bash
grep "agentflow-tg" pyproject.toml
# expected: 2 lines (notify + listen). If only 1, add the missing one:
#   agentflow-tg-notify = "agentflow_pipeline.tg_notifier:_main_entry"
```

---

## Live deployment state

**launchd job currently running** on this user's machine:
- Label: `com.agentflow.scan.daily-10am`
- Plist: `~/Library/LaunchAgents/com.agentflow.scan.daily-10am.plist`
- Schedule: 10:00 daily (StartCalendarInterval Hour=10 Minute=0)
- Args: `--notify-lark --auto-promote --auto-promote-apply --auto-promote-max 1 --auto-promote-min-engagement 150`
- Lark env injected via `launchctl setenv LARK_WEBHOOK_URL ...` (will need re-injection on reboot)

**Lark integration**: standalone webhook fallback is configured in `.env` (`LARK_WEBHOOK_URL` set, KEYWORDS=AgentFlow,热点扫描). OpenClaw Lark App mode must be configured through the official `@larksuite/openclaw-lark` Feishu channel (`channels.feishu`) rather than AgentFlow's Python webhook module.

**TG daemon**: NOT yet installed. User needs to:
1. Create TG bot via @BotFather
2. Add bot to a group + send `/start`
3. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CALLBACK_SECRET` in `.env`
4. Run `bash scripts/install_tg_listener.sh --apply`
5. In standalone mode, Lark Custom Bot cards can deep-link to TG via `--lark-cta-tg-bot @bot_username` or `LARK_CTA_TG_BOT`. In OpenClaw mode, prefer `@larksuite/openclaw-lark` interactive cards and permission policy.

---

## Outstanding integration TODOs (for cursor or next claude)

1. **Done in v0.4.1**: `agentflow-tg-notify` entry script exists in pyproject.toml.
2. **Done in v0.4.1**: `--lark-cta-tg-bot @username` and `LARK_CTA_TG_BOT` are wired for standalone webhook/TG fallback.
3. **v0.4.2 TODO**: keep AgentFlow out of Feishu channel ownership and document official `@larksuite/openclaw-lark` as the OpenClaw Lark App integration path.
4. **v0.4.2 TODO**: bump version + README badges after tests.
5. **Rebuild skill zip** with all new files (run from framework root):
   ```bash
   rm -rf skill/bundle/src skill/bundle/scripts skill/bundle/templates
   cp -r src skill/bundle/
   cp -r scripts skill/bundle/
   cp -r templates skill/bundle/
   cp -f pyproject.toml LICENSE README.md PROGRESS.md FRAMEWORK_SPEC.md WINDOW_GATE_ALIGNMENT.md .env.lark.example install.sh openclaw.plugin.json skill/bundle/
   chmod +x skill/bundle/install.sh skill/bundle/scripts/*.sh
   find skill/bundle -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null
   find skill/bundle -name '*.pyc' -delete 2>/dev/null
   rm -rf skill/bundle/src/agentflow_pipeline.egg-info
   ZIP=dist/agentflow-pipeline-skill-v0.4.2.zip
   ZIP_LATEST=dist/agentflow-pipeline-skill.zip
   rm -f $ZIP $ZIP_LATEST
   ( cd skill && zip -rq ../$ZIP . -x '*.pyc' '__pycache__/*' '*/__pycache__/*' '.DS_Store' '*/.DS_Store' )
   cp $ZIP $ZIP_LATEST
   ```
6. **Defensive secret check** before commit:
   ```bash
   git status --short
   git diff --cached --name-only | xargs grep -lE "open\.feishu\.cn/open-apis/bot/v2/hook/[a-zA-Z0-9-]{20}" 2>/dev/null
   git diff --cached --name-only | xargs grep -lE "[0-9]{8,12}:[A-Za-z0-9_-]{30,}" 2>/dev/null  # TG bot token pattern
   # both should be empty
   ```
7. **Commit + push + GitHub release v0.4.2**:
   ```bash
   git add . && git commit -m "v0.4.2: align OpenClaw Lark App integration

   Major:
   - tg_notifier (outbound), tg_callback_listener (daemon), case_actions (4 handlers)
   - notification_templates abstraction (Lark + TG, user-customizable .tpl)
   - schedule_installer daemon mode (RunAtLoad + KeepAlive for tg-listener)
   - install.sh one-shot installer (9-stage idempotent)
   - openclaw.plugin.json manifest no longer claims Feishu channel ownership

   Tests: 471 passing (baseline 386 + L1=0 + L2=20 + L3=27 + L4=16 + L5=19 + L6=3)"
   git push origin main
   gh release create v0.4.2 dist/agentflow-pipeline-skill-v0.4.2.zip dist/agentflow-pipeline-skill.zip \
     --repo witness1993x/agentflow-pipeline \
     --title "v0.4.2 — OpenClaw Lark App alignment" \
     --notes "..." # see PROGRESS.md or this handoff for content
   ```
8. **Optional smoke verify** after push (no real install of TG daemon, just dry-run):
   ```bash
   /tmp/agentflow-venv/bin/pip install -e . --force-reinstall --no-deps --quiet
   agentflow-tg-listen --once --quiet  # should fail-fast on missing TELEGRAM_BOT_TOKEN
   bash scripts/install_tg_listener.sh --root . --dry-run  # should print plist preview
   ```

---

## Known issues / things to watch

1. **launchd env injection lost on reboot** — `launchctl setenv` is per-session. User will need to re-run after reboot, or add to `~/.zprofile`. Documented in `scripts/install_schedule.md` under "macOS launchd env gotcha".
2. **TG callback daemon needs `TELEGRAM_CALLBACK_SECRET` + chat allowlist** for production safety. `TELEGRAM_CALLBACK_SECRET` protects inline `callback_data`; `TELEGRAM_CHAT_ID` / `--chat-id-allowlist` is the trust boundary for visible Lark deep-link `/start` payloads.
3. **first-day promote will skip** — `_maybe_auto_promote` requires ≥2 scans in history before it can detect "new" entries. First-time installs see "[promote] skip: only 1 scan in history; need ≥2" until the second scan completes.

---

## Reference shipped repos (proof of framework working)

These are real public repos shipped end-to-end with this framework as templates for new projects:

| Repo | Language | ChainStream cube | Pattern |
|---|---|---|---|
| https://github.com/witness1993x/chainstream-launch-radar | TypeScript | Solana DEXTrades | Memecoin launch monitor |
| https://github.com/witness1993x/whale-pulse-evm | TypeScript | EVM Transfers (4 chains) | Whale wallet tracker |
| https://github.com/witness1993x/stable-depeg-radar | Python | Pairs + DEXTrades | Stablecoin depeg early-warning |

---

## How to continue (cursor-friendly recipe)

```bash
cd /Users/witness/Desktop/experimental/agentflow-git-repo-clone

# 1. Verify everything still passes
python3 -m pytest tests/ -q
# Expected: 480 passed

# 2. Rebuild skill zip for v0.4.2
mkdir -p dist
ZIP=dist/agentflow-pipeline-skill-v0.4.2.zip
ZIP_LATEST=dist/agentflow-pipeline-skill.zip
rm -f "$ZIP" "$ZIP_LATEST"
( cd skill && zip -rq "../$ZIP" . -x '*.pyc' '__pycache__/*' '*/__pycache__/*' '.DS_Store' '*/.DS_Store' )
cp "$ZIP" "$ZIP_LATEST"

# 3. Commit + push + create GitHub release v0.4.2 after verification

# 4. Verify release downloadable:
curl -sL -o /tmp/test.zip https://github.com/witness1993x/agentflow-pipeline/releases/download/v0.4.2/agentflow-pipeline-skill.zip
unzip -l /tmp/test.zip | grep -E "(tg_notifier|tg_callback|case_actions|notification_templates|install\.sh|openclaw\.plugin\.json)" | wc -l
# Expected: ≥6 (one match per new file in zip)
```

---

## Future work (not blocking v0.4.2)

These are ideas users have hinted at but haven't requested yet:

- **Phase C (full auto-ship)**: scan → LLM-generate full repo skeleton → auto-publish with 24h abort window. High risk; deferred indefinitely.
- **AgentFlow-specific OpenClaw channel plugin** only if the official `@larksuite/openclaw-lark` channel cannot cover a future workflow. Current v0.4.2 direction is to reuse the official channel.
- **PyPI publish** for `pip install agentflow-pipeline` (currently editable-install only).
- **Browser dashboard** for trends/case state (currently markdown + yaml files only).
- **Cross-source intelligence**: when same hotspot appears in github + hn + reddit simultaneously, boost confidence; track which sources predict eventual winners.

---

## Memory dir

User-level Claude memory pertaining to this project lives at:
`/Users/witness/.claude/projects/-Users-witness-Desktop-experimental-agentflow-git-repo-clone/memory/`

Files:
- `MEMORY.md` — index
- `project_overview.md` — high-level
- `architecture_notes.md` — module structure
- `feedback_style.md` — user prefers terse Chinese, action-over-planning
- `multi_agent_pattern.md` — proven agent dispatch pattern (3-6 parallel + integration)
- `end_to_end_validation.md` — real ship records + sandbox observations
- `framework_packaged.md` — pip install -e . made framework reusable
- `repo_published.md` — initial v0.1.0 release notes

Cursor: read MEMORY.md for the index, then specific files as needed.

---

## Contact / context for cursor

User identity: witness1993x on GitHub, mobius0083x on email. Lark webhook configured in `.env` (don't echo it). TG bot not yet created. For OpenClaw Lark App mode, configure the official `@larksuite/openclaw-lark` plugin instead of adding Feishu channel code to AgentFlow.

If you encounter sandbox denials trying to launchctl/install/git push, that's expected — explicit user authorization required for those (ask in chat).
