# DDoSecrets Crawler / Mirror

Polite, incremental mirror for **Distributed Denial of Secrets** public content:

| Source | What is mirrored |
| --- | --- |
| `https://ddosecrets.org/` | Articles, pages, selected static assets, RSS |
| `https://data.ddosecrets.org/` | Directory indexes (+ page logo); bulk files deferred to torrents |
| `https://torrents.ddosecrets.org/` | Homepage assets, `magnets.csv`, `releases.xml`, magnet queue |

Designed for Threat Intelligence archival: structured metadata catalog + local mirror, with **human-like delays**, **incremental / conditional revalidation**, and a **local FastAPI UI**.

> **Capacity warning:** a complete public mirror is typically **tens of terabytes**. Prefer torrents for bulk datasets. Start with laptop preview (`config.laptop.yaml`).

**Full runbook:** see [HOW_TO_RUN.md](HOW_TO_RUN.md).

## Features

- Continuous scheduler (`schedule`) — drains pending queues without manual restarts
- Conditional revalidation (`ETag` / `If-Modified-Since` / sha256)
- Random polite delays + burst pauses; single concurrent HTTP by default
- SQLite state for incremental sync
- Offline HTML link rewrite to `/files/...`
- Local UI: Overview, Stats (with Stat help toggle), Domains, Releases, Magnets, Files browser, Settings
- Docker Compose: UI + scheduler by default; laptop or full config via `CONFIG_NAME`
- aria2c integration for torrent downloads (VPS / full config)

## Quick start (laptop)

```bash
bash scripts/install.sh
source .venv/bin/activate
bash scripts/run_ui_and_crawl.sh
# → http://127.0.0.1:8090
# stop: bash scripts/stop_ui_and_crawl.sh
```

### Docker

```bash
# Laptop preview (no bulk downloads)
CONFIG_NAME=config.docker.laptop.yaml docker compose up -d --build

# VPS / full
docker compose up -d --build

docker compose down
```

## Install

```bash
cd /path/to/ddosecrets-crawler
bash scripts/install.sh
source .venv/bin/activate
# optional for torrent downloads:
sudo apt install aria2
```

## Manual crawl modes

```bash
python -m ddosecrets_crawler run torrent-index -c config.laptop.yaml --no-torrent-download
python -m ddosecrets_crawler run site -c config.laptop.yaml --no-torrent-download
python -m ddosecrets_crawler run data -c config.laptop.yaml --no-torrent-download
python -m ddosecrets_crawler status -c config.laptop.yaml
python -m ddosecrets_crawler schedule -c config.laptop.yaml   # continuous
```

## Output layout

```
mirror/
  site/ddosecrets.org/...
  site/torrents.ddosecrets.org/...
  data/data.ddosecrets.org/...
  torrents/<infohash>/...          # if torrent downloads enabled
  metadata/catalog.json
  metadata/magnets.csv
  metadata/magnet_queue.json
  metadata/articles/<slug>.json
state/mirror.db
logs/
```

## Incremental updates

| Object | Behavior |
| --- | --- |
| Site / data HTML | Conditional GET; rewrite local file only when content changes |
| Discovery | New links become `pending`; already-`done` URLs are not re-queued mid-crawl |
| Magnets | Indexed from CSV/RSS/articles; downloads only if torrent engine enabled |
| Scheduler | `continuous` drains pending; intervals revalidate stale pages |

## Prefer torrents for bulk data

Laptop configs defer / refuse large HTTP payloads. Full configs can start aria2 (`torrent.use_cli` or RPC). See [HOW_TO_RUN.md](HOW_TO_RUN.md).

## CLI

```bash
python -m ddosecrets_crawler run all|site|data|torrent-index|torrent-start|catalog
python -m ddosecrets_crawler status
python -m ddosecrets_crawler schedule [--once]
python -m ddosecrets_crawler serve -c config.laptop.yaml --host 127.0.0.1 --port 8090
```

## Legal / operational notes

- Only public downloadable releases are targeted; Limited Distribution ticket flows are not scraped for payloads.
- Ensure lawful basis, retention policy, and access controls for stored datasets.
- Seeding after torrent download helps keep the public archive available (`seed_ratio` / `seed_time_minutes`).
