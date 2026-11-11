#!/usr/bin/env bash
# Start UI + continuous scheduler (unattended / VPS-friendly).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CONFIG="${CONFIG:-config.laptop.yaml}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8090}"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

mkdir -p logs mirror state

start_bg() {
  local name="$1"
  shift
  local pidfile="logs/${name}.pid"
  local logfile="logs/${name}.log"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return 0
  fi
  echo "Starting $name..."
  nohup "$@" >"$logfile" 2>&1 &
  echo $! >"$pidfile"
  echo "$name pid $(cat "$pidfile") → $logfile"
}

if ! curl -fsS "http://${HOST}:${PORT}/healthz" >/dev/null 2>&1 \
  && ! curl -fsS "http://${HOST}:${PORT}/api/stats" >/dev/null 2>&1; then
  start_bg "ui.${PORT}" python -m ddosecrets_crawler serve -c "$CONFIG" --host "$HOST" --port "$PORT"
  sleep 1
else
  echo "UI already reachable on http://${HOST}:${PORT}"
fi

start_bg "scheduler" python -m ddosecrets_crawler schedule -c "$CONFIG"

echo
echo "UI:     http://${HOST}:${PORT}"
echo "Stats:  http://${HOST}:${PORT}/stats"
echo "Files:  http://${HOST}:${PORT}/files/"
echo "Worker: continuous scheduler (see logs/scheduler.log)"
echo "Stop:   bash scripts/stop_ui_and_crawl.sh"
