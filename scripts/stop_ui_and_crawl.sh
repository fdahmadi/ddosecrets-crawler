#!/usr/bin/env bash
# Stop UI + continuous scheduler started by run_ui_and_crawl.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8090}"
stopped=0

stop_pidfile() {
  local pidfile="$1"
  local name="$2"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Stopping $name (pid $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      stopped=1
    fi
    rm -f "$pidfile"
  fi
}

stop_pidfile "logs/ui.${PORT}.pid" "ui"
stop_pidfile "logs/scheduler.pid" "scheduler"

# Fallback: any leftover crawler processes for this project
for pid in $(pgrep -f "python -m ddosecrets_crawler (serve|schedule)" || true); do
  echo "Stopping leftover pid $pid..."
  kill "$pid" 2>/dev/null || true
  stopped=1
done
sleep 1
for pid in $(pgrep -f "python -m ddosecrets_crawler (serve|schedule)" || true); do
  kill -9 "$pid" 2>/dev/null || true
done

if [[ "$stopped" -eq 0 ]]; then
  echo "Nothing was running."
else
  echo "Stopped."
fi
