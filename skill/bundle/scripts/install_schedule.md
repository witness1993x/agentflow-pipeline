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
