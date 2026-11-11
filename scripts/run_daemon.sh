#!/usr/bin/env bash
# Unattended continuous crawler (same entrypoint as systemd unit).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CONFIG="${CONFIG:-$ROOT/config.yaml}"
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
exec python -m ddosecrets_crawler schedule -c "$CONFIG"
