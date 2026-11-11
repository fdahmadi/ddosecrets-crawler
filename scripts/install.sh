#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

if ! command -v aria2c >/dev/null 2>&1; then
  echo "NOTE: aria2c not found. Install with: sudo apt install aria2"
fi

mkdir -p mirror state logs
echo "Installed. Activate with: source .venv/bin/activate"
echo "Test crawl: ddosecrets-crawler run site --max-pages 5 --no-torrent-download"