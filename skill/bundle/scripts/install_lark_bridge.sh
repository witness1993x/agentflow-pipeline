#!/usr/bin/env bash
set -euo pipefail

LABEL="${AGENTFLOW_PIPELINE_LARK_BRIDGE_LABEL:-com.agentflow.pipeline.lark-bridge}"
HOST="${AGENTFLOW_PIPELINE_LARK_BRIDGE_HOST:-127.0.0.1}"
PORT="${AGENTFLOW_PIPELINE_LARK_BRIDGE_PORT:-7871}"
ROOT="${AGENTFLOW_ROOT:-$(pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APPLY=0
UNINSTALL=0
STATUS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --status) STATUS=1; shift ;;
    --root) ROOT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ "$STATUS" == "1" ]]; then
  launchctl list | grep "$LABEL" || true
  [[ -f "$PLIST" ]] && echo "$PLIST"
  exit 0
fi

if [[ "$UNINSTALL" == "1" ]]; then
  if [[ "$APPLY" == "1" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled $LABEL"
  else
    echo "dry-run: launchctl unload '$PLIST' && rm -f '$PLIST'"
  fi
  exit 0
fi

mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>-m</string>
    <string>agentflow_pipeline.lark_bridge</string>
    <string>--host</string>
    <string>${HOST}</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AGENTFLOW_ROOT</key>
    <string>${ROOT}</string>
    <key>AGENTFLOW_PIPELINE_LARK_BRIDGE_HOST</key>
    <string>${HOST}</string>
    <key>AGENTFLOW_PIPELINE_LARK_BRIDGE_PORT</key>
    <string>${PORT}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key>
    <true/>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${ROOT}/trends/_lark_bridge.out.log</string>
  <key>StandardErrorPath</key>
  <string>${ROOT}/trends/_lark_bridge.err.log</string>
</dict>
</plist>
EOF

if [[ "$APPLY" == "1" ]]; then
  mkdir -p "${ROOT}/trends"
  mv "$PLIST.tmp" "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "installed $LABEL"
  echo "commands endpoint: http://${HOST}:${PORT}/api/git-case-commands"
  echo "compat endpoint:   http://${HOST}:${PORT}/api/commands"
else
  echo "dry-run: would write $PLIST"
  echo "preview:"
  sed 's/^/  /' "$PLIST.tmp"
  rm -f "$PLIST.tmp"
fi
