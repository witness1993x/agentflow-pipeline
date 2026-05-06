# Install Schedule (twice-daily auto-scan)

A turn-key wrapper around `agentflow-schedule install` that lets you put
`agentflow-scan` on a recurring schedule with a single command.

## What this does

Installs an OS-level recurring job that runs `agentflow-scan --root <root>`
twice a day at **09:00** and **21:00** local time. Output writes into
`<root>/trends/` (the same directory `agentflow-trends diff` reads from).

* macOS: a LaunchAgent plist at
  `~/Library/LaunchAgents/com.agentflow.scan.daily.plist`
  loaded with `launchctl bootstrap gui/<uid>`.
* Linux: a user-scope systemd `service` + `timer` pair under
  `~/.config/systemd/user/agentflow-scan-daily.{service,timer}`,
  enabled with `systemctl --user enable --now`.

The script is **fail-closed**: without `--apply` it only prints what
would be written. The user must opt in to a real install.

## Prerequisites

1. The framework is installed and `agentflow-scan` + `agentflow-schedule`
   are on `PATH`. The recommended setup is a virtualenv:

   ```bash
   python3 -m venv /tmp/agentflow-venv
   source /tmp/agentflow-venv/bin/activate
   pip install -e /Users/witness/Desktop/experimental/agentflow-git-repo-clone
   ```

   After this `command -v agentflow-scan` should print an absolute path
   (e.g. `/tmp/agentflow-venv/bin/agentflow-scan`). If it doesn't, the
   installer aborts with a clear error.

2. **You run the install script from a shell where the venv is active.**
   The plist's `ProgramArguments[0]` is captured at install time via
   `shutil.which("agentflow-scan")`. If the venv is not on PATH at that
   moment, no usable absolute path can be recorded and launchd will not
   be able to locate the binary at 09:00 / 21:00. See the macOS PATH
   gotcha below.

## Quick install (macOS)

Dry-run first (default; no files touched):

```bash
bash scripts/install_schedule.sh --root /path/to/host-project
```

Then for real:

```bash
bash scripts/install_schedule.sh --root /path/to/host-project --apply
```

Expected output (truncated):

```
[install_schedule] platform : macos
[install_schedule] root     : /path/to/host-project
[install_schedule] label    : com.agentflow.scan.daily
[install_schedule] times    : 09:00,21:00
[install_schedule] mode     : install
[install_schedule] apply    : yes
[install_schedule] scan bin : /tmp/agentflow-venv/bin/agentflow-scan
...
[agentflow-schedule] status   = installed
[install_schedule] install OK.
[install_schedule]   log dir : /path/to/host-project/trends/_logs
[install_schedule]   stdout  : .../com.agentflow.scan.daily.out.log
[install_schedule]   stderr  : .../com.agentflow.scan.daily.err.log
[install_schedule] next auto runs (local time): 09:00,21:00
[install_schedule] manual trigger:
[install_schedule]   launchctl kickstart -k gui/$(id -u)/com.agentflow.scan.daily
```

## Verify

```bash
# Is the unit loaded?
launchctl list | grep agentflow

# Same thing via the framework CLI:
agentflow-schedule status --label com.agentflow.scan.daily

# Tail the run logs (created on first scan):
ls -la /path/to/host-project/trends/_logs/
tail -f /path/to/host-project/trends/_logs/com.agentflow.scan.daily.out.log

# Force an immediate run instead of waiting for 09:00 / 21:00:
launchctl kickstart -k gui/$(id -u)/com.agentflow.scan.daily
```

## First scan after install

The job will fire at the next `09:00` or `21:00` local time after the
install. You can also trigger it manually with the `launchctl kickstart`
command above. Each run appends to:

* `<root>/trends/_logs/com.agentflow.scan.daily.out.log`
* `<root>/trends/_logs/com.agentflow.scan.daily.err.log`

and writes the structured scan output into `<root>/trends/`.

## View results

After at least two scans have completed:

```bash
agentflow-trends diff --root /path/to/host-project
```

`agentflow-trends diff` reads the timestamped scan output under
`<root>/trends/` and reports new / dropped hotspots between adjacent
runs.

## Customize times

```bash
# Four times a day
bash scripts/install_schedule.sh \
    --times "06:00,12:00,18:00,23:00" \
    --root /path/to/host-project \
    --apply

# Different label (lets you run two parallel schedules)
bash scripts/install_schedule.sh \
    --label com.agentflow.scan.morning \
    --times "07:30" \
    --root /path/to/host-project \
    --apply

# Pass extra args to agentflow-scan
bash scripts/install_schedule.sh \
    --scan-args "--sources github,hackernews,reddit --query 'evm whale'" \
    --root /path/to/host-project \
    --apply
```

`--apply --force` is sent under the hood for installs, so re-running
with the same `--label` overwrites the existing plist with the new
times / args cleanly.

## Uninstall

```bash
bash scripts/install_schedule.sh --uninstall              # dry-run preview
bash scripts/install_schedule.sh --uninstall --apply      # actually remove
```

This runs `launchctl bootout gui/<uid> <plist>` and unlinks the plist
(macOS), or `systemctl --user disable --now <label>.timer` and unlinks
the unit files (linux).

## macOS PATH gotcha (important)

When `launchd` runs your job, the environment is **not** the same as
your interactive shell. In particular:

* `$PATH` is the system default (`/usr/bin:/bin:/usr/sbin:/sbin`).
* Your virtualenv's `bin/` directory is **not** on it.
* Login shell `rc` files (`~/.zshrc`, `~/.bash_profile`) are **not**
  sourced.

If the plist's `ProgramArguments` referenced the bare name
`agentflow-scan`, launchd would silently fail to find the binary at
09:00 / 21:00 and you would only notice when no logs appear.

The fix is to record an **absolute path** in `ProgramArguments[0]`.
This is what `agentflow-schedule install` already does: it calls
`shutil.which("agentflow-scan")` at install time and writes the result
into the plist. Inspect the dry-run output and confirm you see
something like:

```xml
<key>ProgramArguments</key>
<array>
    <string>/tmp/agentflow-venv/bin/agentflow-scan</string>
    ...
</array>
```

The wrapper script enforces this guarantee with a pre-flight check:
if `command -v agentflow-scan` does not resolve to an absolute path
(e.g. you forgot to activate the venv) the script exits with rc=2
**before** writing anything. So:

1. Open a shell.
2. `source /tmp/agentflow-venv/bin/activate`.
3. Confirm `command -v agentflow-scan` prints `/tmp/agentflow-venv/bin/agentflow-scan`.
4. Run `bash scripts/install_schedule.sh --apply`.

If you ever move or rebuild the venv, re-run the installer with
`--apply` and the plist will be rewritten with the new absolute path.

## Linux systemd equivalent

On Linux the same script generates a user-scope service + timer pair:

```ini
# ~/.config/systemd/user/agentflow-scan-daily.service
[Unit]
Description=agentflow-scan twice-daily run at 09:00,21:00
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/host-project
ExecStart=/tmp/agentflow-venv/bin/agentflow-scan --root /path/to/host-project
StandardOutput=append:/path/to/host-project/trends/_logs/agentflow-scan-daily.out.log
StandardError=append:/path/to/host-project/trends/_logs/agentflow-scan-daily.err.log

[Install]
WantedBy=default.target
```

```ini
# ~/.config/systemd/user/agentflow-scan-daily.timer
[Unit]
Description=agentflow-scan twice-daily run at 09:00,21:00 (timer)

[Timer]
OnCalendar=*-*-* 09:00:00
OnCalendar=*-*-* 21:00:00
Persistent=true
Unit=agentflow-scan-daily.service

[Install]
WantedBy=timers.target
```

Install commands (these are what the script runs for you):

```bash
bash scripts/install_schedule.sh --apply
# under the hood:
#   systemctl --user daemon-reload
#   systemctl --user enable --now agentflow-scan-daily.timer
```

Verify and trigger:

```bash
systemctl --user list-timers | grep agentflow
systemctl --user start agentflow-scan-daily.service     # manual run
journalctl --user -u agentflow-scan-daily.service -n 50 # logs
```

`Persistent=true` on the timer means a missed run while the machine
was off will execute as soon as it boots, instead of being skipped
until the next 09:00 / 21:00.

## All flags

```
bash scripts/install_schedule.sh [options]

  --root PATH       host project root (default: repo root)
  --label NAME      launchd label / systemd unit basename
                    (default: com.agentflow.scan.daily on macOS,
                              agentflow-scan-daily on linux)
  --times CSV       comma-separated HH:MM (default: 09:00,21:00)
  --scan-args STR   extra argv passed to agentflow-scan
  --apply           actually install/uninstall (default is dry-run)
  --uninstall       uninstall mode
  --status          status mode
  -h, --help        show help
```

## Lark / Feishu integration

AgentFlow has two Lark integration modes:

- **OpenClaw Lark App mode**: install/configure the official
  `@larksuite/openclaw-lark` plugin. It provides the Feishu channel, Lark App
  gateway, interactive cards, permission policies, and allowlists. AgentFlow is
  only the skill/CLI that scans and manages case state.
- **Standalone webhook fallback**: `agentflow-scan ... --notify-lark` posts a
  one-way Lark Custom Bot card via `LARK_WEBHOOK_URL`.

### OpenClaw Lark App mode

Use OpenClaw's Feishu channel config for Lark App credentials and security:

```bash
npm install -g openclaw
openclaw plugins install npm:@larksuite/openclaw-lark
openclaw feishu-diagnose
```

Do not configure AgentFlow as a Feishu channel provider. Its
`openclaw.plugin.json` intentionally declares no channel capability; it should
be installed alongside `openclaw-lark`.

### Standalone Lark webhook fallback

The default scan job (`agentflow-scan ... --notify-lark`) can post a Lark card after every
scan. The card includes:

- Top N hotspots from the scan (each with source + engagement)
- Already-shipped framework repos (auto-discovered from `cases/HSP-*/02-pipeline-gate.yaml`
  where `decision.final_status == "publish"`)
- Buttons linking to the framework repo + each shipped repo + (optional) the scan markdown URL

### Setup

1. In your Lark group: 设置 → 群机器人 → 添加 → 自定义机器人 → 给个名字 (e.g. "AgentFlow Scan") → 复制 webhook URL
2. (Optional but recommended) enable 自定义关键词 with `AgentFlow` as one of the keywords.
3. (Optional) enable 签名校验 and copy the secret.
4. Copy the env template and fill in the URL:
   ```bash
   cp .env.lark.example .env
   # edit .env, paste the URL into LARK_WEBHOOK_URL
   # optionally set LARK_WEBHOOK_SECRET / KEYWORDS / BRAND_PREFIX
   ```
5. Source the env in the same shell that owns the launchd job:
   ```bash
   set -a && source .env && set +a
   ```
6. Install the daily 10:00 job:
   ```bash
   bash scripts/install_schedule.sh \
     --times "10:00" \
     --scan-args "--notify-lark" \
     --label com.agentflow.scan.daily-10am \
     --apply
   ```

### macOS launchd env gotcha

`launchd` does **not** inherit the env vars of the shell that ran the install. Without an
extra step, `agentflow-scan --notify-lark` will run with `LARK_WEBHOOK_URL` empty and
silently skip the Lark post.

Two options to make the env survive:

A. **Use `launchctl setenv`** (per-user, lasts until reboot — needs to be re-run on each
boot via a `~/.zprofile` line or a separate `RunAtLoad` plist):
```bash
launchctl setenv LARK_WEBHOOK_URL "$LARK_WEBHOOK_URL"
launchctl setenv LARK_WEBHOOK_SECRET "$LARK_WEBHOOK_SECRET"
launchctl setenv LARK_WEBHOOK_KEYWORDS "$LARK_WEBHOOK_KEYWORDS"
launchctl setenv LARK_CTA_TG_BOT "$LARK_CTA_TG_BOT"  # optional bridge
```

B. **Wrap `agentflow-scan` in a shell script that sources `.env` first**, and point the
plist at that wrapper instead of the bare binary. Cleanest long-term solution.

### Verify (without waiting for 10:00)

Trigger the job immediately:
```bash
launchctl kickstart -k gui/$(id -u)/com.agentflow.scan.daily-10am
```
Then check `~/Library/LaunchAgents/com.agentflow.scan.daily-10am.plist` is loaded:
```bash
launchctl list | grep agentflow
```
And confirm the scan output + Lark card both arrived:
```bash
ls <root>/trends/$(date -u +%Y-%m-%d)-10/   # or check Lark group
```

### Dry-run the card without posting

```bash
LARK_WEBHOOK_DRY_RUN=true agentflow-scan --root . --notify-lark --queries solana --top-n 5
# → builds the card, prints the JSON plan, doesn't POST
```

### Disable the Lark side without uninstalling the job

```bash
launchctl unsetenv LARK_WEBHOOK_URL
# next run: scan still produces trends/, the Lark step silently skips
```

## Auto-promote (level B)

`agentflow-scan` can auto-scaffold a new case (the 5-tuple under
`<root>/cases/HSP-XXX-.../`) for any newly-discovered hotspot whose
engagement is high enough. It does **not** write source code and does
**not** publish — it only generates the case skeleton so the operator
can review the auto-filled hotspot intake and decide whether to write
code + ship.

### Safety model

- `--auto-promote` alone is dry-run (prints what it would create)
- `--auto-promote --auto-promote-apply` is the real-create double flag
- Hard cap per scan run: `--auto-promote-max N` (default 1)
- Engagement floor: `--auto-promote-min-engagement N` (default 150 —
  sum of GitHub stars + HN points + Reddit score across appearances)
- "New" means: not in any of the past `--auto-promote-baseline-window`
  scans (default 14 ≈ 1 week at twice-daily) AND not already a shipped
  repo (filtered via `discover_shipped_repos`)
- Owner string in the generated case meta:
  `--auto-promote-owner <name>` (default `agentflow-auto`)
- Promotion failures never break the scan; scan still exits 0

### Daily 10:00 launchd upgrade command

To upgrade your existing `com.agentflow.scan.daily-10am` job to also
auto-promote (with Lark enabled):

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

`--apply --force` overwrites the existing plist cleanly with the new
`ProgramArguments`, then re-bootstraps the LaunchAgent. The Lark env
gotcha above still applies — make sure `LARK_WEBHOOK_URL` is reachable
to the launchd context (option A or B in the previous section).

### First kickstart caveat

Auto-promote needs ≥ 2 historical scans to detect "new" entries — the
very first run after install has nothing to diff against, so it will
report `[auto-promote] no history yet, skipping` and create no cases.
The second scheduled scan (or a manual `launchctl kickstart`) is when
promotion first becomes active. If you want to seed history without
waiting, run `agentflow-scan --root <root>` manually a couple of times
before relying on the launchd-driven promotion.

### Verify promoted cases

After a run that should have promoted something, list the most recent
case dirs under your host project:

```bash
ls -lt /path/to/host-project/cases/HSP-*/ | head -5
```

Each promoted case will contain the standard 5-tuple
(`01-hotspot-intake.md`, `02-pipeline-gate.yaml`,
`03-publish-decision-memo.md`, `04-build-probe-run.md`,
`05-review-checkpoint.md`) with the hotspot URL + engagement
pre-filled. Open `02-pipeline-gate.yaml` to see the auto-filled
`owner` (defaults to `agentflow-auto`) and `status: probe`.

The Lark card from the same scan will include a `📝 自动 promote 了 N
个 case` section + a `📝 promoted [N]` button linking back to the
source URL, so you can spot the new cases without grepping the
filesystem.

### Disable promotion without uninstalling the schedule

Edit the plist's `--scan-args` to drop `--auto-promote-apply` (keep
`--auto-promote` for dry-run reporting only), or remove both flags
entirely. Then re-run the same `agentflow-schedule install ...
--apply --force` command above to push the change.

## TG callback daemon (interactive)

`agentflow-tg-listen` is the long-running counterpart to
`agentflow-tg-notify`: where the latter pushes a single card and
exits, the listener stays connected to Telegram's `getUpdates` long-
poll and dispatches inline-keyboard button clicks
(`📊 dry-publish` / `🤖 write-stub` / `🚮 drop` / `💤 snooze 7d`) to
the framework's `case_actions` handlers. Because it's a daemon, the
schedule installer needs a different unit shape than the cron-style
twice-daily scan.

### What the installer generates

`agentflow-schedule install --mode daemon` writes a unit that
**runs continuously**, not on a calendar:

- **macOS launchd plist** — no `StartCalendarInterval`. Instead:
  - `RunAtLoad=true` — start the daemon as soon as the LaunchAgent
    is bootstrapped (and on every user login afterwards).
  - `KeepAlive` is a *dict*, not a bool:
    ```xml
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>     <!-- exit 0 → don't restart (lets the operator
                          stop the daemon gracefully) -->
        <key>Crashed</key>
        <true/>      <!-- non-zero exit / signal → restart -->
        <key>NetworkState</key>
        <true/>      <!-- only run when network is up -->
    </dict>
    ```
  - `ThrottleInterval=10` (seconds) — prevents rapid-restart loops if
    the daemon dies immediately on launch (e.g. missing token).
- **linux systemd service** — no `.timer` companion. The `.service`
  file is `Type=simple` with `Restart=on-failure` and
  `RestartSec=10`. `systemctl --user enable --now <label>.service`
  starts it now and on every user-session boot.

### Quick install (macOS)

```bash
# Activate the venv that has agentflow-tg-listen installed
source /tmp/agentflow-venv/bin/activate

# Make sure the bot creds are reachable to the launchd context
launchctl setenv TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
launchctl setenv TELEGRAM_CALLBACK_SECRET "$TELEGRAM_CALLBACK_SECRET"

# Dry-run the installer first (default)
bash scripts/install_tg_listener.sh

# Then apply
bash scripts/install_tg_listener.sh --apply
```

Expected dry-run output (truncated):

```
[install_tg_listener] platform : macos
[install_tg_listener] root     : /path/to/host-project
[install_tg_listener] label    : com.agentflow.tg-listener
[install_tg_listener] mode     : install
[install_tg_listener] apply    : no (dry-run)
[install_tg_listener] listen bin : /tmp/agentflow-venv/bin/agentflow-tg-listen
[install_tg_listener] command  : agentflow-schedule install --mode daemon \
        --platform macos --label com.agentflow.tg-listener \
        --root /path/to/host-project \
        --daemon-args "--bot-token-env TELEGRAM_BOT_TOKEN \
                       --callback-secret-env TELEGRAM_CALLBACK_SECRET"
----------------------------------------------------------------------
[agentflow-schedule] platform = macos
[agentflow-schedule] label    = com.agentflow.tg-listener
[agentflow-schedule] status   = dry_run
[agentflow-schedule] plist    = ~/Library/LaunchAgents/com.agentflow.tg-listener.plist

----- plist -----
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC ...>
<plist version="1.0">
<dict>
    <key>Label</key><string>com.agentflow.tg-listener</string>
    <key>ProgramArguments</key>
    <array>
        <string>/tmp/agentflow-venv/bin/agentflow-tg-listen</string>
        <string>--root</string><string>/path/to/host-project</string>
        <string>--bot-token-env</string><string>TELEGRAM_BOT_TOKEN</string>
        <string>--callback-secret-env</string><string>TELEGRAM_CALLBACK_SECRET</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><false/>
        <key>Crashed</key><true/>
        <key>NetworkState</key><true/>
    </dict>
    <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>

----- actions -----
  - would mkdir -p /path/to/host-project/trends/_logs
  - would write plist to ~/Library/LaunchAgents/com.agentflow.tg-listener.plist
  - would run: launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.agentflow.tg-listener.plist
```

### Verify

```bash
# Is the daemon loaded?
launchctl list | grep tg-listener

# Same thing via the framework CLI:
agentflow-schedule status --label com.agentflow.tg-listener

# Tail the run logs (the daemon writes here on every callback):
tail -f /path/to/host-project/trends/_logs/com.agentflow.tg-listener.err.log

# Force a restart:
launchctl kickstart -k gui/$(id -u)/com.agentflow.tg-listener
```

### env gotcha (same as cron mode)

`launchd` does **not** inherit your shell's env. Without `launchctl
setenv`, the daemon will start, fail to read `TELEGRAM_BOT_TOKEN`,
exit non-zero, get throttle-restarted every 10 seconds, and burn CPU
while writing nothing to the chat. Belt-and-suspenders:

```bash
# Set in launchd's user-session env (lasts until reboot):
launchctl setenv TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
launchctl setenv TELEGRAM_CALLBACK_SECRET "$TELEGRAM_CALLBACK_SECRET"

# To make this survive reboot, add the same launchctl setenv lines to
# ~/.zprofile (zsh) or ~/.bash_profile (bash), or wrap the daemon
# binary in a shell that sources .env first.
```

After the first crash + restart, the auto-restart loop will also
pick up newly-set env vars (it doesn't cache them).

### Restrict the daemon to specific chats

```bash
bash scripts/install_tg_listener.sh \
  --chat-id-allowlist "12345678,87654321" \
  --apply
```

Without an allowlist the daemon will respond to inline buttons from
any chat the bot has been invited to. The allowlist plus the
`TELEGRAM_CALLBACK_SECRET` prefix on every `callback_data` form
defence-in-depth: an attacker would need both to spoof a click.

### Uninstall

```bash
bash scripts/install_tg_listener.sh --uninstall              # dry-run
bash scripts/install_tg_listener.sh --uninstall --apply      # for real
```

This runs `launchctl bootout gui/<uid>
~/Library/LaunchAgents/com.agentflow.tg-listener.plist` and unlinks
the plist (macOS), or `systemctl --user disable --now
com.agentflow.tg-listener.service` and unlinks the service file
(linux). Any in-flight callback is dropped; subsequent button clicks
go unanswered until the daemon is reinstalled.

### All flags

```
bash scripts/install_tg_listener.sh [options]

  --root PATH                       host project root (default: cwd)
  --label NAME                      launchd label / systemd unit basename
                                    (default: com.agentflow.tg-listener)
  --bot-token-env NAME              env var that holds the bot token
                                    (default: TELEGRAM_BOT_TOKEN)
  --callback-secret-env NAME        env var that holds the callback secret
                                    (default: TELEGRAM_CALLBACK_SECRET)
  --chat-id-allowlist "id1,id2"     comma-separated chat_id whitelist
  --apply                           actually install (default is dry-run)
  --status                          show current daemon status
  --uninstall                       uninstall mode
  -h, --help                        show this help
```

### Standalone Lark → TG deep link bridge

Lark Custom Bot is push-only — its inline buttons cannot trigger
callbacks back to the framework. To preserve interactivity end-to-
end while keeping Lark as the primary "look at this" channel, the
framework rewrites the same set of buttons into Telegram **deep
links** when posting to Lark outside OpenClaw:

```
[📊 dry-publish] → https://t.me/<bot_username>?start=case_HSP-042_dry_publish
```

The deep link opens the user's Telegram client, focuses the bot
chat, and sends `/start <payload>` automatically. The
`agentflow-tg-listen` daemon recognises these `/start` payloads and
dispatches them to the same `case_actions` handlers used for in-chat
inline-button clicks. Because Telegram `/start` payloads are visible
URLs rather than hidden `callback_data`, production installs should set
`--chat-id-allowlist` / `TELEGRAM_CHAT_ID` so only trusted chats can
trigger deep-link actions.

This means a single click in Lark performs the same action as a
click in TG — the daemon just becomes the universal action router.
Configure with `--lark-cta-tg-bot @your_bot_username` on the daily
scan, e.g.:

```bash
bash scripts/install_schedule.sh \
  --times "10:00" \
  --scan-args "--notify-lark --lark-cta-tg-bot @your_bot_username" \
  --label com.agentflow.scan.daily-10am \
  --apply
```

For an already-installed Lark schedule, you can also set
`LARK_CTA_TG_BOT=@your_bot_username` in the launchd / systemd environment
and leave the existing `--scan-args "--notify-lark"` untouched. If neither
the flag nor env var is set, the promoted button keeps the legacy
`source_url` behavior. In OpenClaw Lark App mode, prefer
`@larksuite/openclaw-lark` interactive cards instead of this fallback bridge.
