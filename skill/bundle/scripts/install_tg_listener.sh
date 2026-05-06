#!/usr/bin/env bash
# install_tg_listener.sh
# ---------------------------------------------------------------------------
# Turn-key installer for the agentflow-tg-listen Telegram callback daemon.
#
# Wraps `agentflow-schedule install --mode daemon` so a user can copy-paste
# a single command and get a launchd (macOS) or systemd-user (linux)
# long-running listener that handles inline-keyboard button clicks
# (dry-publish / write-stub / drop / snooze) coming from Telegram.
#
# Defaults are intentionally fail-closed: without `--apply` this script
# only prints what *would* happen (dry-run). The user must opt in to a
# real install with `--apply`.
#
# Usage:
#   bash scripts/install_tg_listener.sh [OPTIONS]
#
# Modes:
#   (default)         install the daemon
#   --status          show current daemon status
#   --uninstall       remove the daemon
#
# Options:
#   --root PATH                       host project root (default: cwd)
#   --label NAME                      launchd label / systemd unit basename
#                                     (default: com.agentflow.tg-listener)
#   --bot-token-env NAME              env var that holds the bot token
#                                     (default: TELEGRAM_BOT_TOKEN)
#   --callback-secret-env NAME        env var that holds the callback secret
#                                     (default: TELEGRAM_CALLBACK_SECRET)
#   --chat-id-allowlist "id1,id2"     comma-separated chat_id whitelist
#   --apply                           actually install (default is dry-run)
#   --dry-run                         force dry-run (default)
#   --status                          status mode
#   --uninstall                       uninstall mode
#   -h, --help                        show this help
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------- defaults ---------------------------------------------------------

ROOT="$(pwd)"
LABEL="com.agentflow.tg-listener"
BOT_TOKEN_ENV="TELEGRAM_BOT_TOKEN"
CALLBACK_SECRET_ENV="TELEGRAM_CALLBACK_SECRET"
CHAT_ID_ALLOWLIST=""
MODE="install"        # install | uninstall | status
APPLY="0"             # 0 = dry-run, 1 = real

# ---------- helpers ----------------------------------------------------------

log() { printf '[install_tg_listener] %s\n' "$*"; }
err() { printf '[install_tg_listener] ERROR: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }

usage() {
    cat <<'USAGE'
install_tg_listener.sh - turn-key installer for the agentflow-tg-listen daemon

Modes:
  (default)                       install the daemon
  --status                        show current daemon status
  --uninstall                     remove the daemon

Options:
  --root PATH                     host project root (default: cwd)
  --label NAME                    launchd label / systemd unit basename
                                  (default: com.agentflow.tg-listener)
  --bot-token-env NAME            env var that holds the bot token
                                  (default: TELEGRAM_BOT_TOKEN)
  --callback-secret-env NAME      env var that holds the callback secret
                                  (default: TELEGRAM_CALLBACK_SECRET)
  --chat-id-allowlist "id1,id2"   comma-separated chat_id whitelist
  --apply                         actually install (default is dry-run)
  --dry-run                       force dry-run (default)
  -h, --help                      show this help

Examples:
  bash scripts/install_tg_listener.sh                       # dry-run
  bash scripts/install_tg_listener.sh --apply               # real install
  bash scripts/install_tg_listener.sh \
      --chat-id-allowlist "12345,67890" --apply
  bash scripts/install_tg_listener.sh --status              # show entry
  bash scripts/install_tg_listener.sh --uninstall --apply   # remove
USAGE
}

# ---------- argument parsing -------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --root)                 ROOT="${2:?--root needs a path}"; shift 2 ;;
        --label)                LABEL="${2:?--label needs a value}"; shift 2 ;;
        --bot-token-env)        BOT_TOKEN_ENV="${2:?--bot-token-env needs a value}"; shift 2 ;;
        --callback-secret-env)  CALLBACK_SECRET_ENV="${2:?--callback-secret-env needs a value}"; shift 2 ;;
        --chat-id-allowlist)    CHAT_ID_ALLOWLIST="${2:?--chat-id-allowlist needs a value}"; shift 2 ;;
        --apply)                APPLY="1"; shift ;;
        --dry-run)              APPLY="0"; shift ;;
        --status)               MODE="status"; shift ;;
        --uninstall)            MODE="uninstall"; shift ;;
        -h|--help)              usage; exit 0 ;;
        *)                      die "unknown argument: $1 (use --help)" ;;
    esac
done

# ---------- platform detection -----------------------------------------------

UNAME_S="$(uname -s)"
case "$UNAME_S" in
    Darwin)  PLATFORM="macos" ;;
    Linux)   PLATFORM="linux" ;;
    *)       die "unsupported platform: $UNAME_S (only macOS and linux are supported)" ;;
esac

log "platform : $PLATFORM"
log "root     : $ROOT"
log "label    : $LABEL"
log "mode     : $MODE"
log "apply    : $([ "$APPLY" = "1" ] && echo yes || echo 'no (dry-run)')"

# ---------- pre-flight: console scripts --------------------------------------

if ! command -v agentflow-schedule >/dev/null 2>&1; then
    die "'agentflow-schedule' not on PATH; activate your venv first \
(e.g. 'source /tmp/agentflow-venv/bin/activate') and re-run."
fi

# Same gotcha as install_schedule.sh: launchd's PATH does not include the
# venv bin/, so the resolved listener binary must be an absolute path.
if [ "$MODE" = "install" ]; then
    if ! command -v agentflow-tg-listen >/dev/null 2>&1; then
        err "'agentflow-tg-listen' not on PATH."
        err ""
        err "  launchd / systemd will record whatever 'shutil.which' finds at"
        err "  install time. Without agentflow-tg-listen visible now, the"
        err "  resulting unit would point at a bare name that launchd cannot"
        err "  resolve at boot."
        err ""
        err "  Fix: activate the venv that has agentflow installed, e.g."
        err "    source /tmp/agentflow-venv/bin/activate"
        err "  then re-run this script."
        exit 2
    fi

    LISTEN_BIN_RESOLVED="$(command -v agentflow-tg-listen)"
    log "listen bin : $LISTEN_BIN_RESOLVED"
    case "$LISTEN_BIN_RESOLVED" in
        /*) ;;
        *)  die "agentflow-tg-listen resolved to non-absolute path '$LISTEN_BIN_RESOLVED'; aborting." ;;
    esac
fi

# ---------- assemble daemon-args ---------------------------------------------

DAEMON_ARGS_PARTS=()
DAEMON_ARGS_PARTS+=("--bot-token-env" "$BOT_TOKEN_ENV")
DAEMON_ARGS_PARTS+=("--callback-secret-env" "$CALLBACK_SECRET_ENV")
if [ -n "$CHAT_ID_ALLOWLIST" ]; then
    DAEMON_ARGS_PARTS+=("--chat-id-allowlist" "$CHAT_ID_ALLOWLIST")
fi
# Concatenate with single spaces; --daemon-args takes a single shell-style
# string the framework re-splits on whitespace.
DAEMON_ARGS_STR="${DAEMON_ARGS_PARTS[*]}"

# ---------- assemble agentflow-schedule argv ---------------------------------

SCHEDULE_ARGV=("agentflow-schedule")

case "$MODE" in
    install)
        SCHEDULE_ARGV+=("install"
                        "--mode" "daemon"
                        "--platform" "$PLATFORM"
                        "--label" "$LABEL"
                        "--root" "$ROOT"
                        "--daemon-args" "$DAEMON_ARGS_STR")
        if [ "$APPLY" = "1" ]; then
            SCHEDULE_ARGV+=("--apply" "--force")
        fi
        ;;
    uninstall)
        SCHEDULE_ARGV+=("uninstall"
                        "--platform" "$PLATFORM"
                        "--label" "$LABEL")
        if [ "$APPLY" = "1" ]; then
            SCHEDULE_ARGV+=("--apply")
        fi
        ;;
    status)
        SCHEDULE_ARGV+=("status"
                        "--platform" "$PLATFORM"
                        "--label" "$LABEL")
        ;;
esac

log "command  : ${SCHEDULE_ARGV[*]}"
echo "----------------------------------------------------------------------"

set +e
"${SCHEDULE_ARGV[@]}"
RC=$?
set -e

echo "----------------------------------------------------------------------"

if [ "$RC" -ne 0 ]; then
    err "agentflow-schedule exited with rc=$RC"
    exit "$RC"
fi

# ---------- post-install verification ----------------------------------------

if [ "$MODE" = "install" ] && [ "$APPLY" = "1" ]; then
    log "verifying scheduler picked up the daemon ..."
    set +e
    agentflow-schedule status --platform "$PLATFORM" --label "$LABEL"
    VRC=$?
    set -e
    if [ "$VRC" -ne 0 ]; then
        err "verification rc=$VRC; check output above"
        exit "$VRC"
    fi

    LOG_DIR="$ROOT/trends/_logs"
    log ""
    log "install OK."
    log "  log dir : $LOG_DIR"
    log "  stdout  : $LOG_DIR/$LABEL.out.log"
    log "  stderr  : $LOG_DIR/$LABEL.err.log"
    log ""
    log "the daemon is now running under your platform's service manager."
    if [ "$PLATFORM" = "macos" ]; then
        log "verify     :  launchctl list | grep ${LABEL##*.}"
        log "tail logs  :  tail -f $LOG_DIR/$LABEL.err.log"
        log "force stop :  launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/$LABEL.plist"
    else
        log "verify     :  systemctl --user status $LABEL.service"
        log "tail logs  :  journalctl --user -u $LABEL.service -f"
        log "force stop :  systemctl --user stop $LABEL.service"
    fi
    log ""
    log "make sure these env vars are reachable to the daemon's launch ctx:"
    log "  $BOT_TOKEN_ENV          (Telegram bot token)"
    log "  $CALLBACK_SECRET_ENV    (random >= 16-char secret prefixed to callback_data)"
    if [ "$PLATFORM" = "macos" ]; then
        log "  on macOS, use 'launchctl setenv NAME VALUE' so launchd sees them,"
        log "  or wrap the daemon in a shell that sources .env first."
    fi
elif [ "$MODE" = "install" ]; then
    log ""
    log "dry-run complete. nothing was written."
    log "to actually install:"
    log "  bash scripts/install_tg_listener.sh --apply"
elif [ "$MODE" = "uninstall" ] && [ "$APPLY" = "1" ]; then
    log ""
    log "uninstall OK. plist / unit files removed."
fi

exit 0
