#!/usr/bin/env bash
# install.sh
# ---------------------------------------------------------------------------
# One-shot installer for agentflow-pipeline. Detects host environment, sets
# up a Python venv, installs the framework, validates the registered console
# scripts, walks the user through Lark / Telegram env, and (optionally)
# installs the daily 10:00 launchd job.
#
# Idempotent: re-running skips already-completed steps (venv exists, .env
# filled, launchd loaded) and only refreshes what changed.
#
# Usage:
#   bash install.sh [OPTIONS]
#     --venv-dir <path>      Python venv location (default: ./.venv)
#     --skip-venv            Use existing Python (PEP 668 risk on macOS)
#     --skip-launchd         Skip 10:00 daily launchd install
#     --root <host-root>     Host project root (default: cwd)
#     --tg                   Also enable Telegram outbound
#     --no-lark              Skip Lark setup
#     --auto-promote         Enable level-B auto-promote in launchd job
#     -h, --help             Show this help
#
# Companion: ./openclaw.plugin.json declares this framework as an OpenClaw
# plugin (channels=feishu+telegram). Re-run install.sh after editing .env.
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------- defaults ----------------------------------------------------------

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" >/dev/null 2>&1 && pwd)"

VENV_DIR="$SCRIPT_DIR/.venv"
SKIP_VENV="0"
SKIP_LAUNCHD="0"
HOST_ROOT="$(pwd)"
ENABLE_TG="0"
DISABLE_LARK="0"
AUTO_PROMOTE="0"

# Detect TG flag implicitly from env
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    ENABLE_TG="1"
fi

# ---------- helpers -----------------------------------------------------------

log()  { printf '[install] %s\n' "$*"; }
ok()   { printf '[install] OK   %s\n' "$*"; }
warn() { printf '[install] WARN %s\n' "$*" >&2; }
err()  { printf '[install] ERR  %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
    cat <<'USAGE'
install.sh - one-shot installer for agentflow-pipeline

Usage: bash install.sh [OPTIONS]
  --venv-dir <path>      Python venv location (default: ./.venv)
  --skip-venv            Use existing Python (PEP 668 risk on macOS — venv recommended)
  --skip-launchd         Skip 10:00 daily launchd install (otherwise prompts)
  --root <host-root>     Host project root for cases/workspaces (default: cwd)
  --tg                   Also enable Telegram outbound (default: enabled if TELEGRAM_BOT_TOKEN env set)
  --no-lark              Skip Lark setup
  --auto-promote         Enable level-B auto-promote in the launchd job
  -h, --help             Show this help

Sequence:
  1. Pre-flight (Python 3.11+, git, gh)
  2. Create / reuse venv
  3. pip install -e (this dir or ./bundle/)
  4. Verify console scripts (agentflow-scan/scaffold/pipeline/schedule/trends/init/lark-bridge)
  5. Detect OpenClaw skill mode (~/.claude/skills/agentflow-pipeline or ~/.openclaw/skills)
  6. Lark env (cp .env.lark.example .env if needed; pause for user fill)
  7. Telegram env (optional)
  8. launchd install (default prompts y/n)
  9. Final report
USAGE
}

# ---------- argument parsing --------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --venv-dir)      VENV_DIR="${2:?--venv-dir needs a path}"; shift 2 ;;
        --skip-venv)     SKIP_VENV="1"; shift ;;
        --skip-launchd)  SKIP_LAUNCHD="1"; shift ;;
        --root)          HOST_ROOT="${2:?--root needs a path}"; shift 2 ;;
        --tg)            ENABLE_TG="1"; shift ;;
        --no-lark)       DISABLE_LARK="1"; shift ;;
        --auto-promote)  AUTO_PROMOTE="1"; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "unknown argument: $1 (use --help)" ;;
    esac
done

# Normalise host root to absolute
HOST_ROOT="$(cd "$HOST_ROOT" >/dev/null 2>&1 && pwd)" || die "--root '$HOST_ROOT' is not a directory"

# ---------- 1. pre-flight -----------------------------------------------------

log "==== 1/9  pre-flight ===================================================="

UNAME_S="$(uname -s)"
case "$UNAME_S" in
    Darwin)  PLATFORM="macos" ;;
    Linux)   PLATFORM="linux" ;;
    *)       die "unsupported platform: $UNAME_S (only macOS and linux are supported)" ;;
esac
ok "platform: $PLATFORM"

# Python 3.11+
if command -v python3 >/dev/null 2>&1; then
    PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    PY_OK="$(python3 -c 'import sys;print(1 if sys.version_info>=(3,11) else 0)')"
    if [ "$PY_OK" = "1" ]; then
        ok "python3: $PY_VER"
    else
        die "python3 $PY_VER detected; framework requires >= 3.11"
    fi
else
    die "python3 not found on PATH; install Python 3.11+ first"
fi

# git
if command -v git >/dev/null 2>&1; then
    ok "git: $(git --version | head -1)"
else
    die "git not found on PATH; required for repo / scaffolding workflows"
fi

# gh (warn-only)
if command -v gh >/dev/null 2>&1; then
    ok "gh:  $(gh --version | head -1)"
else
    warn "gh CLI not found; required for --auto-publish and gh-search hotspot source"
    warn "  install: https://cli.github.com (brew install gh / apt install gh)"
fi

log "host root: $HOST_ROOT"

# Detect whether we're running from framework root, ./bundle/, or skill ship dir
INSTALL_SOURCE="$SCRIPT_DIR"
if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    INSTALL_SOURCE="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/bundle/pyproject.toml" ]; then
    INSTALL_SOURCE="$SCRIPT_DIR/bundle"
elif [ -f "$SCRIPT_DIR/skill/bundle/pyproject.toml" ]; then
    INSTALL_SOURCE="$SCRIPT_DIR/skill/bundle"
else
    die "cannot locate pyproject.toml (looked in $SCRIPT_DIR, ./bundle/, ./skill/bundle/)"
fi
ok "install source: $INSTALL_SOURCE"

# ---------- 2. venv -----------------------------------------------------------

log "==== 2/9  venv =========================================================="

if [ "$SKIP_VENV" = "1" ]; then
    warn "--skip-venv: using system python3; PEP 668 may block 'pip install -e'"
    PY="python3"
    PIP="python3 -m pip"
else
    if [ -f "$VENV_DIR/bin/activate" ]; then
        ok "venv exists: $VENV_DIR (reusing)"
    else
        log "creating venv at $VENV_DIR ..."
        python3 -m venv "$VENV_DIR"
        ok "venv created"
    fi
    PY="$VENV_DIR/bin/python"
    PIP="$VENV_DIR/bin/pip"
fi

# ---------- 3. pip install ----------------------------------------------------

log "==== 3/9  pip install -e $INSTALL_SOURCE ================================"

# Already-installed shortcut: if dist-info present, skip reinstall
ALREADY_INSTALLED="0"
if "$PY" -c "import agentflow_pipeline" 2>/dev/null; then
    INSTALLED_VER="$("$PY" -c 'import importlib.metadata as m;print(m.version("agentflow-pipeline"))' 2>/dev/null || echo unknown)"
    ok "agentflow-pipeline already installed (v$INSTALLED_VER) — re-running editable install to refresh"
    ALREADY_INSTALLED="1"
fi

"$PIP" install --quiet --upgrade pip || warn "pip self-upgrade failed (continuing)"
"$PIP" install --quiet -e "$INSTALL_SOURCE" || die "pip install -e failed"
ok "pip install -e $INSTALL_SOURCE complete"

# ---------- 4. verify console scripts -----------------------------------------

log "==== 4/9  verify console scripts ========================================"

if [ "$SKIP_VENV" = "1" ]; then
    BIN_DIR="$(python3 -c 'import sysconfig;print(sysconfig.get_path("scripts"))')"
else
    BIN_DIR="$VENV_DIR/bin"
fi

REQUIRED_SCRIPTS=(
    agentflow-init
    agentflow-pipeline
    agentflow-scaffold
    agentflow-scan
    agentflow-schedule
    agentflow-trends
)
OPTIONAL_SCRIPTS=(
    agentflow-tg-notify
    agentflow-tg-listen
    agentflow-lark-bridge
)

MISSING=0
for s in "${REQUIRED_SCRIPTS[@]}"; do
    if [ -x "$BIN_DIR/$s" ]; then
        if "$BIN_DIR/$s" --help >/dev/null 2>&1; then
            ok "$s --help"
        else
            # some scripts may exit nonzero on --help due to argparse quirks; still counted as present
            warn "$s present but --help exited nonzero"
        fi
    else
        err "$s NOT FOUND in $BIN_DIR"
        MISSING=$((MISSING + 1))
    fi
done

for s in "${OPTIONAL_SCRIPTS[@]}"; do
    if [ -x "$BIN_DIR/$s" ]; then
        ok "$s --help (optional)"
    else
        log "skipped optional: $s (not yet registered — L3/L4 work)"
    fi
done

if [ "$MISSING" -gt 0 ]; then
    die "$MISSING required console script(s) missing — pip install probably failed silently"
fi

# ---------- 5. mode detect ----------------------------------------------------

log "==== 5/9  mode detect ==================================================="

OPENCLAW_SKILL_DIRS=(
    "$HOME/.claude/skills/agentflow-pipeline"
    "$HOME/.openclaw/skills/agentflow-pipeline"
)
OPENCLAW_DETECTED="0"
for d in "${OPENCLAW_SKILL_DIRS[@]}"; do
    if [ "$SCRIPT_DIR" = "$d" ]; then
        OPENCLAW_DETECTED="1"
        OPENCLAW_DIR="$d"
        break
    fi
done

if [ "$OPENCLAW_DETECTED" = "1" ]; then
    ok "running inside OpenClaw skill location: $OPENCLAW_DIR"
    log "  → prefer routing through host's OpenClaw plugin runtime when available"
    log "  → openclaw.plugin.json manifest is sibling-loaded by OpenClaw"
    MODE="openclaw"
else
    ok "running in standalone mode (not inside ~/.claude/skills/ or ~/.openclaw/skills/)"
    MODE="standalone"
fi

# ---------- 6. Lark env -------------------------------------------------------

log "==== 6/9  Lark env ======================================================"

ENV_FILE="$SCRIPT_DIR/.env"
LARK_EXAMPLE="$SCRIPT_DIR/.env.lark.example"
[ -f "$LARK_EXAMPLE" ] || LARK_EXAMPLE="$INSTALL_SOURCE/.env.lark.example"

if [ "$DISABLE_LARK" = "1" ]; then
    log "--no-lark: skipping Lark setup"
elif [ -f "$ENV_FILE" ] && grep -qE '^LARK_WEBHOOK_URL=.+' "$ENV_FILE" 2>/dev/null; then
    ok ".env already has LARK_WEBHOOK_URL — skipping"
else
    if [ -f "$ENV_FILE" ]; then
        log ".env exists but LARK_WEBHOOK_URL is empty"
    elif [ -f "$LARK_EXAMPLE" ]; then
        cp "$LARK_EXAMPLE" "$ENV_FILE"
        ok "copied .env.lark.example → .env"
    else
        warn "no .env.lark.example found; create $ENV_FILE manually"
    fi
    cat <<EOF

  ┌─────────────────────────────────────────────────────────────────────┐
  │  ACTION REQUIRED                                                    │
  │                                                                     │
  │  Edit $ENV_FILE and paste your Lark webhook URL into:               │
  │      LARK_WEBHOOK_URL=https://open.larksuite.com/...                │
  │                                                                     │
  │  Then re-run:                                                       │
  │      bash install.sh${AUTO_PROMOTE:+ --auto-promote}                                          │
  │                                                                     │
  │  (Get the URL: Lark group → 设置 → 群机器人 → 添加 → 自定义机器人)  │
  └─────────────────────────────────────────────────────────────────────┘
EOF
    log "stopping here so you can edit .env. re-run install.sh after."
    exit 0
fi

# ---------- 7. Telegram env (optional) ----------------------------------------

log "==== 7/9  Telegram env (optional) ======================================="

if [ "$ENABLE_TG" = "1" ]; then
    if [ -f "$ENV_FILE" ] && grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE" 2>/dev/null; then
        ok "TELEGRAM_BOT_TOKEN already in .env"
    elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
        ok "TELEGRAM_BOT_TOKEN found in shell env (will be picked up at runtime)"
    else
        warn "TG enabled but no TELEGRAM_BOT_TOKEN — add to .env or shell env before scheduled run"
        cat <<'EOF'

  Required for Telegram outbound:
    TELEGRAM_BOT_TOKEN=<from @BotFather>
    TELEGRAM_CHAT_ID=<your chat id>
    TELEGRAM_CALLBACK_SECRET=<random hex; only if running tg-listen daemon>

EOF
    fi
else
    log "Telegram disabled (pass --tg or set TELEGRAM_BOT_TOKEN to enable)"
fi

# ---------- 8. launchd install ------------------------------------------------

log "==== 8/9  launchd / systemd install ====================================="

if [ "$SKIP_LAUNCHD" = "1" ]; then
    log "--skip-launchd: skipping schedule install"
else
    if [ "$PLATFORM" = "macos" ]; then
        SCHED_LABEL="com.agentflow.scan.daily-10am"
    else
        SCHED_LABEL="agentflow-scan-daily-10am"
    fi

    SCAN_ARGS_BASE="--notify-lark"
    if [ "$AUTO_PROMOTE" = "1" ]; then
        SCAN_ARGS_BASE="--notify-lark --auto-promote --auto-promote-apply --auto-promote-max 1 --auto-promote-min-engagement 150"
        log "auto-promote enabled in scan-args"
    fi

    # Already installed?
    SCHED_BIN="$BIN_DIR/agentflow-schedule"
    SCHED_INSTALLED="0"
    if [ -x "$SCHED_BIN" ]; then
        if "$SCHED_BIN" status --platform "$PLATFORM" --label "$SCHED_LABEL" >/dev/null 2>&1; then
            SCHED_INSTALLED="1"
        fi
    fi

    if [ "$SCHED_INSTALLED" = "1" ]; then
        ok "schedule '$SCHED_LABEL' already installed (use --apply --force on agentflow-schedule to refresh)"
    else
        # Prompt y/n unless we're non-interactive
        if [ ! -t 0 ]; then
            warn "non-interactive shell; skipping launchd install. To install later run:"
            log "  $SCHED_BIN install --platform $PLATFORM --label $SCHED_LABEL --root $HOST_ROOT --times 10:00 --scan-args=\"$SCAN_ARGS_BASE\" --apply --force"
        else
            printf '\n  Install daily 10:00 %s job? [y/N] ' "$PLATFORM"
            read -r REPLY
            case "$REPLY" in
                y|Y|yes|YES)
                    log "installing schedule..."
                    "$SCHED_BIN" install \
                        --platform "$PLATFORM" \
                        --label "$SCHED_LABEL" \
                        --root "$HOST_ROOT" \
                        --times "10:00" \
                        --scan-args="$SCAN_ARGS_BASE" \
                        --apply --force \
                        || die "agentflow-schedule install failed"
                    ok "schedule installed: $SCHED_LABEL"
                    ;;
                *)
                    log "skipping schedule install (you can re-run with --auto-promote later)"
                    ;;
            esac
        fi
    fi
fi

# ---------- 9. final report ---------------------------------------------------

log "==== 9/9  done =========================================================="
cat <<EOF

  agentflow-pipeline installed.

  Mode               : $MODE
  Install source     : $INSTALL_SOURCE
  venv               : ${VENV_DIR}
  Host root          : $HOST_ROOT
  Console scripts    : $BIN_DIR/agentflow-{init,pipeline,scaffold,scan,schedule,trends}
  Logs (after 1st run):
    $HOST_ROOT/trends/_logs/<label>.out.log
    $HOST_ROOT/trends/_logs/<label>.err.log

  Verify:
    source $VENV_DIR/bin/activate
    agentflow-scan --help

  Docs:
    README.md  → "## One-shot install"
    skill/SKILL.md  → "## OpenClaw plugin compatibility"
    openclaw.plugin.json  → channels=feishu+telegram, configSchema for OpenClaw runtimes

  Next:
    1. agentflow-init <host-project-dir>   # bootstrap a fresh host project
    2. agentflow-scan --notify-lark        # run a one-shot scan and post to Lark
    3. (or wait for the 10:00 launchd run if you installed the schedule)
EOF

exit 0
