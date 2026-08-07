#!/usr/bin/env bash
# install-agent.sh — keep `budsmp daemon` running, so the fix is re-applied every
# time the buds connect.
#
# The buds forget asVer whenever they power down (../docs/experiments.md), which
# turns `apply` into a per-power-session chore. A LaunchAgent watching for connect
# events makes that go away: the daemon sits idle until the buds appear, writes
# one frame, and goes back to sleep.
#
#   ./install-agent.sh                    install and start it
#   ./install-agent.sh --name "Buds2 Pro" ... with extra budsmp options
#   ./install-agent.sh status             is it running, and what did it log
#   ./install-agent.sh uninstall          stop it and remove the plist
#
# Nothing here needs sudo — it is a per-user agent, not a system daemon.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

LABEL="io.github.galaxy-buds-multipoint.budsmp"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG="${HOME}/Library/Logs/galaxy-buds-multipoint-daemon.log"
APP="$(pwd)/build/BudsMP.app"
BIN="${APP}/Contents/MacOS/BudsMP"
DOMAIN="gui/$(id -u)"

action="install"
if [[ $# -gt 0 ]]; then
  case "$1" in
    install|uninstall|status|log) action="$1"; shift ;;
    -h|--help) sed -n '2,15p' "$0" | cut -c3-; exit 0 ;;
  esac
fi

stop_agent () {
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null \
    || launchctl unload "$PLIST" 2>/dev/null \
    || true
}

case "$action" in

uninstall)
  stop_agent
  rm -f "$PLIST"
  echo "removed ${PLIST}"
  echo
  echo "The buds keep whatever asVer they currently hold — this only stops the"
  echo "re-applying. Run ./budsmp revert if you want the stock behaviour back."
  ;;

status)
  echo "plist : ${PLIST}"
  echo "log   : ${LOG}"
  if [[ ! -f "$PLIST" ]]; then
    echo "state : not installed"
    exit 1
  fi
  if info="$(launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null)"; then
    pid="$(printf '%s' "$info" | awk -F'= ' '/^[[:space:]]*pid = /{print $2; exit}')"
    echo "state : loaded${pid:+, running as pid ${pid}}"
  else
    echo "state : installed but not loaded"
  fi
  if [[ -s "$LOG" ]]; then
    echo
    echo "--- last 15 log lines --------------------------------------------"
    tail -n 15 "$LOG"
  fi
  ;;

log)
  exec tail -f "$LOG"
  ;;

install)
  [[ -x "$BIN" ]] || ./build.sh

  # build.sh clears this marker, because its ad-hoc signature changes with the
  # binary and TCC treats that as a different app. Without the grant the daemon
  # blocks inside IOBluetooth waiting for a prompt nobody can answer, so its
  # watchdog gives up and launchd restarts it in a slow loop. Say so up front.
  if [[ ! -f build/.tcc-granted ]]; then
    echo "note: run './budsmp apply' once first and click Allow on the Bluetooth" >&2
    echo "      prompt. A background agent cannot answer that itself, and this" >&2
    echo "      build has not been granted access yet — a rebuild resets it." >&2
    echo >&2
  fi

  # Build the <array> by hand so that forwarded options survive spaces.
  args_xml=""
  for a in "$BIN" daemon --log "$LOG" "$@"; do
    esc="${a//&/&amp;}"; esc="${esc//</&lt;}"; esc="${esc//>/&gt;}"
    args_xml+="    <string>${esc}</string>"$'\n'
  done

  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
${args_xml}  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- The daemon only exits on an error, so back off instead of spinning. -->
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST

  stop_agent
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"

  echo "installed ${PLIST}"
  echo "logging to ${LOG}"
  sleep 2

  if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    echo
    echo "--- first log lines ----------------------------------------------"
    tail -n 12 "$LOG" 2>/dev/null || echo "(nothing logged yet)"
    echo "------------------------------------------------------------------"
    echo
    echo "It is running. Connect the buds — or put them in the case and take"
    echo "them out — and the log should show a write within a few seconds."
    echo "Check on it later with:  ./install-agent.sh status"
  else
    echo "warning: launchctl did not report the agent as loaded." >&2
    echo "         try: launchctl bootstrap ${DOMAIN} ${PLIST}" >&2
    exit 1
  fi
  ;;

esac
