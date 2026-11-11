# How to Run — DDoSecrets Crawler

## Recommended: UI + continuous crawl (laptop)

This is the usual local workflow: open the browser UI and let the scheduler keep draining the queue (torrent index → data indexes → site pages), with polite delays and no bulk file downloads.

```bash
cd /path/to/ddosecrets-crawler
bash scripts/install.sh          # once
source .venv/bin/activate

bash scripts/run_ui_and_crawl.sh
```

Open:

| URL | Purpose |
| --- | --- |
| [http://127.0.0.1:8090](http://127.0.0.1:8090) | Overview + live counters |
| [http://127.0.0.1:8090/stats](http://127.0.0.1:8090/stats) | Full crawl stats |
| [http://127.0.0.1:8090/domains](http://127.0.0.1:8090/domains) | Mirrored hosts + Open mirror |
| [http://127.0.0.1:8090/files/](http://127.0.0.1:8090/files/) | Browse `mirror/` folders (file list) |
| [http://127.0.0.1:8090/settings](http://127.0.0.1:8090/settings) | Edit config → `*.local.yaml` |

Stop:

```bash
bash scripts/stop_ui_and_crawl.sh
```

> Port **8090** by default. Override with `HOST=127.0.0.1 PORT=8090 CONFIG=config.laptop.yaml bash scripts/run_ui_and_crawl.sh`.

### Same with Docker (laptop preview)

```bash
CONFIG_NAME=config.docker.laptop.yaml docker compose up -d --build
# open http://localhost:8090
docker compose down
```

### Docker (VPS / full settings)

```bash
docker compose up -d --build
# uses config.docker.yaml (can download torrents via aria2 in the image)
docker compose logs -f scheduler
docker compose down
```

---

## What the continuous worker does

With `schedule.continuous: true` (default in laptop + docker configs):

1. **torrent-index** — refresh magnets/RSS + torrents homepage assets (CSS/logo)
2. **data** — mirror a few `data.ddosecrets.org` directory indexes (+ page logo); skip bulk files on laptop
3. **site** — crawl `ddosecrets.org` in batches; keep going while pending URLs remain
4. **revalidation** — on site/data intervals, re-check stale pages with conditional GET (`304` / rewrite if changed)

Notes:

- Site crawler only follows `ddosecrets.org` (not `data.*` / `torrents.*` — those have their own modes).
- Already-`done` URLs are not re-queued during discovery (avoids stalling “Pages saved”).
- Laptop configs set `max_file_bytes: 1` and `use_cli: false` so datasets/torrents are **not** downloaded.

---

## 1) One-time setup (local Python)

```bash
cd /path/to/ddosecrets-crawler
bash scripts/install.sh
source .venv/bin/activate
```

Optional (VPS torrent downloads):

```bash
sudo apt install aria2
```

---

## 2) Manual one-shot crawl (optional)

Use when you want a single mode instead of the continuous scheduler.

```bash
source .venv/bin/activate

python -m ddosecrets_crawler run torrent-index -c config.laptop.yaml --no-torrent-download
python -m ddosecrets_crawler run site -c config.laptop.yaml --no-torrent-download
python -m ddosecrets_crawler run data -c config.laptop.yaml --no-torrent-download
python -m ddosecrets_crawler status -c config.laptop.yaml
```

### Output layout

```text
mirror/
  site/ddosecrets.org/…              HTML + (some) static
  site/torrents.ddosecrets.org/…     torrents homepage + CSS/img
  data/data.ddosecrets.org/…         directory indexes + logo asset
  metadata/catalog.json              release catalog
  metadata/magnets.csv
  metadata/magnet_queue.json
  torrents/<infohash>/…              only if torrent downloads enabled
state/mirror.db                      crawl state
logs/                                UI + scheduler + crawler logs
```

---

## 3) Web UI pages

| Path | What you see |
| --- | --- |
| `/` | Overview (auto-refresh). Toggle **Stat help** for metric explanations |
| `/stats` | Full stats, recent activity, run history |
| `/domains` | Mirrored hosts; **Open mirror** / browse |
| `/releases` | Searchable release catalog |
| `/magnets` | Magnet queue (often `queued` on laptop — downloads off) |
| `/files/` | Directory listing of `mirror/` (click `index.html` to open a site page) |
| `/settings` | Form/YAML editor → writes `*.local.yaml` overlay |
| `/api/stats` | JSON for monitoring |
| `/healthz` | Health check |

### Config overlay (`*.local.yaml`)

- Base: `config.laptop.yaml` (or docker equivalents)
- Overlay: `config.laptop.local.yaml` if present is loaded automatically
- Settings UI writes only the overlay; **Reset** deletes it
- Overlay files are gitignored

---

## 4) Docker details

| File | Role |
| --- | --- |
| `Dockerfile` | Python 3.12 + aria2 + package install |
| `docker-compose.yml` | `ui` + `scheduler` (default), `crawler` (manual profile) |
| `config.docker.laptop.yaml` | Slow preview, no bulk downloads (`/data` paths) |
| `config.docker.yaml` | Full VPS settings (`/data` paths) |

Host mounts: `./mirror`, `./state`, `./logs` → `/data/...`

### Build

```bash
docker compose build
```

### Choose config

| Goal | Command |
| --- | --- |
| Laptop preview | `CONFIG_NAME=config.docker.laptop.yaml docker compose up -d` |
| Full / VPS | `docker compose up -d` (default `config.docker.yaml`) |

### One-shot commands (manual profile)

```bash
docker compose --profile manual run --rm crawler run torrent-index \
  -c /app/config.docker.laptop.yaml --no-torrent-download

docker compose --profile manual run --rm crawler run site \
  -c /app/config.docker.laptop.yaml --max-pages 5 --no-torrent-download

docker compose --profile manual run --rm crawler status \
  -c /app/config.docker.laptop.yaml
```

### Logs / shell

```bash
docker compose logs -f ui scheduler
docker compose --profile manual run --rm --entrypoint bash crawler
```

> Tip: on VPS, put a large disk behind `./mirror` before enabling torrent downloads.

---

## 5) VPS without Docker

1. Copy the project; point `mirror_root` in `config.yaml` at a large disk.
2. `bash scripts/install.sh` + `sudo apt install aria2`
3. Enable continuous workers:

```bash
# helper
CONFIG=config.yaml bash scripts/run_ui_and_crawl.sh

# or systemd (edit paths in deploy/*.service)
sudo cp deploy/ddosecrets-ui.service deploy/ddosecrets-crawler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ddosecrets-ui ddosecrets-crawler
```

4. Watch [http://\<host\>:8090/stats](http://127.0.0.1:8090/stats)
5. When ready for payloads: `python -m ddosecrets_crawler run torrent-start -c config.yaml`

---

## 6) Useful CLI flags

| Flag | Meaning |
| --- | --- |
| `-c CONFIG` | Config file (overlay `*.local.yaml` still applies if present) |
| `--max-pages N` | Cap site pages this run |
| `--max-indexes N` | Cap data.* directory pages |
| `--max-file-bytes N` | Skip HTTP files larger than N |
| `--no-torrent-download` | Do not start aria2 |
| `--http-only` | Prefer HTTP instead of deferring to torrents |

```bash
python -m ddosecrets_crawler run site -c config.laptop.yaml --max-pages 10 --no-torrent-download
python -m ddosecrets_crawler schedule -c config.laptop.yaml --once
```

---

## 7) Config files

| File | Use |
| --- | --- |
| `config.laptop.yaml` | Local laptop preview |
| `config.yaml` | Local / VPS full |
| `config.docker.laptop.yaml` | Docker laptop (`/data` paths) |
| `config.docker.yaml` | Docker VPS full (`/data` paths) |

Important keys:

- `schedule.continuous` — keep draining pending site queue
- `schedule.site_interval_hours` — when to revalidate stale site pages
- `polite.min_delay` / `max_delay` — request pacing
- `site.max_pages` — batch size (`0` = unlimited)
- `data.max_indexes` / `data.max_file_bytes` — data mirror limits
- `torrent.use_cli` / `max_concurrent_torrents` — aria2 downloads

---

## 8) Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| UI “Not Found” JSON on `/files/` | Old process; restart UI (fixed: folders list files) |
| Pages saved stuck while “active” | Was re-fetching done URLs; update + restart scheduler |
| `data.*` never appears | Wait for `mode=data` (runs after torrent-index); check seeds |
| Magnets stay `queued` | Expected on laptop (`use_cli: false`) |
| Wrong port / foreign page | Use **8090**, not 8080 |
