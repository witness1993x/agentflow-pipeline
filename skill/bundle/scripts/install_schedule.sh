#!/usr/bin/env bash
# install_schedule.sh
# ---------------------------------------------------------------------------
# Turn-key installer for the agentflow-scan twice-daily job.
#
# Wraps `agentflow-schedule install` so a user can copy-paste a single
# command and get launchd (macOS) or systemd-user (linux) scheduling.
#
# Defaults are intentionally fail-closed: without `--apply` this script
# only prints what *would* happen (dry-run). The user must opt in to a
# real install with `--apply`.
#
# Usage:
#   bash scripts/install_schedule.sh [--root PATH] [--label NAME] \
#        [--times "HH:MM,HH:MM"] [--scan-args "..."] [--apply]
#   bash scripts/install_schedule.sh --status
#   bash scripts/install_schedule.sh --uninstall [--apply]
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------- defaults ----------------------------------------------------------

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" >/dev/null 2>&1 && pwd)"
DEFAULT_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"

ROOT="$DEFAULT_ROOT"
LABEL=""             # auto by platform
TIMES="09:00,21:00"
SCAN_ARGS=""
MODE="install"       # install | uninstall | status
APPLY="0"            # 0 = dry-run, 1 = real

# ---------- helpers -----------------------------------------------------------

log() { printf '[install_schedule] %s\n' "$*"; }
err() { printf '[install_schedule] ERROR: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }

usage() {
    cat <<'USAGE'
install_schedule.sh - turn-key installer for agentflow-scan twice-daily job

Modes:
  (default)         install the schedule
  --status          show current scheduler status
  --uninstall       remove the schedule

Options:
  --root PATH       host project root (default: repo root)
  --label NAME      launchd label / systemd unit basename
                    (default: com.agentflow.scan.daily on macOS,
                              agentflow-scan-daily on linux)
  --times CSV       comma-separated HH:MM (default: 09:00,21:00)
  --scan-args STR   extra argv passed to agentflow-scan
  --apply           actually install/uninstall (default is dry-run)
  -h, --help        show this help

Examples:
  bash scripts/install_schedule.sh                     # dry-run, default times
  bash scripts/install_schedule.sh --apply             # real install
  bash scripts/install_schedule.sh \
      --times "06:00,12:00,18:00,23:00" --apply        # custom 4x/day
  bash scripts/install_schedule.sh --status            # show launchctl entry
  bash scripts/install_schedule.sh --uninstall --apply # remove
USAGE
}

# ---------- argument parsing --------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --root)        ROOT="${2:?--root needs a path}"; shift 2 ;;
        --label)       LABEL="${2:?--label needs a value}"; shift 2 ;;
        --times)       TIMES="${2:?--times needs a value}"; shift 2 ;;
        --scan-args)   SCAN_ARGS="${2:?--scan-args needs a value}"; shift 2 ;;
        --apply)       APPLY="1"; shift ;;
        --dry-run)     APPLY="0"; shift ;;
        --status)      MODE="status"; shift ;;
        --uninstall)   MODE="uninstall"; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             die "unknown argument: $1 (use --help)" ;;
    esac
done

# ---------- platform detection ------------------------------------------------

UNAME_S="$(uname -s)"
case "$UNAME_S" in
    Darwin)  PLATFORM="macos" ;;
    Linux)   PLATFORM="linux" ;;
    *)       die "unsupported platform: $UNAME_S (only macOS and linux are supported)" ;;
esac

if [ -z "$LABEL" ]; then
    if [ "$PLATFORM" = "macos" ]; then
        LABEL="com.agentflow.scan.daily"
    else
        LABEL="agentflow-scan-daily"
    fi
fi

log "platform : $PLATFORM"
log "root     : $ROOT"
log "label    : $LABEL"
log "times    : $TIMES"
log "mode     : $MODE"
log "apply    : $([ "$APPLY" = "1" ] && echo yes || echo 'no (dry-run)')"

# ---------- pre-flight: console scripts ---------------------------------------

if ! command -v agentflow-schedule >/dev/null 2>&1; then
    die "'agentflow-schedule' not on PATH; activate your venv first \
(e.g. 'source /tmp/agentflow-venv/bin/activate') and re-run."
fi

# This guard guarantees the plist's ProgramArguments resolves to a real
# absolute path when launchd runs the job. If the user runs this script
# *outside* a venv where agentflow-scan is installed, build_default_scan_spec
# will fall back to the bare name "agentflow-scan" which launchd cannot
# resolve (launchd's PATH does not include venv bin).
if ! command -v agentflow-scan >/dev/null 2>&1; then
    err "'agentflow-scan' not on PATH."
    err ""
    err "  launchd / systemd will record whatever 'shutil.which' finds at"
    err "  install time. Without agentflow-scan visible now, the resulting"
    err "  unit would point at a bare name that launchd cannot resolve at"
    err "  09:00 / 21:00."
    err ""
    err "  Fix: activate the venv that has agentflow installed, e.g."
    err "    source /tmp/agentflow-venv/bin/activate"
    err "  then re-run this script."
    exit 2
fi

SCAN_BIN_RESOLVED="$(command -v agentflow-scan)"
log "scan bin : $SCAN_BIN_RESOLVED"
case "$SCAN_BIN_RESOLVED" in
    /*) ;;
    *)  die "agentflow-scan resolved to non-absolute path '$SCAN_BIN_RESOLVED'; aborting." ;;
esac

# ---------- assemble agentflow-schedule argv ---------------------------------

SCHEDULE_ARGV=("agentflow-schedule")

case "$MODE" in
    install)
        SCHEDULE_ARGV+=("install"
                        "--platform" "$PLATFORM"
                        "--label" "$LABEL"
                        "--root" "$ROOT"
                        "--times" "$TIMES")
        if [ -n "$SCAN_ARGS" ]; then
            SCHEDULE_ARGV+=("--scan-args" "$SCAN_ARGS")
        fi
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
    log "verifying scheduler picked up the unit ..."
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
    log "next auto runs (local time): $TIMES"
    if [ "$PLATFORM" = "macos" ]; then
        log "manual trigger:"
        log "  launchctl kickstart -k gui/\$(id -u)/$LABEL"
    else
        log "manual trigger:"
        log "  systemctl --user start $LABEL.service"
    fi
elif [ "$MODE" = "install" ]; then
    log ""
    log "dry-run complete. nothing was written."
    log "to actually install:"
    log "  bash scripts/install_schedule.sh --apply"
elif [ "$MODE" = "uninstall" ] && [ "$APPLY" = "1" ]; then
    log ""
    log "uninstall OK. plist / unit files removed."
fi

exit 0
