# Work Report — DDoSecrets Mirror Crawler

**Date:** 11 November 2026  
**Repository:** `/home/farhad/Desktop/ddosecrets-crawler`  
**Latest commit:** `602dcb6` — Add polite DDoSecrets mirror crawler with local browse UI.

---

## 1. Project goal

Build a **local mirror** of public Distributed Denial of Secrets content with these constraints:

- **Polite** crawling with human-like delays (reduce rate-limit risk)
- **Incremental** sync (do not re-download unchanged content)
- Prefer **torrents** for bulk datasets (avoid multi-TB HTTP downloads)
- Support **offline browsing** via a local UI

Target sources:

| Host | Role |
| --- | --- |
| `ddosecrets.org` | Articles, categories, static assets, RSS |
| `data.ddosecrets.org` | Directory indexes + files (bounded) |
| `torrents.ddosecrets.org` | `magnets.csv`, RSS, magnet queue |

---

## 2. Deliverables

### Crawler core
- Polite HTTP client (`httpx`) with random delays and burst pauses
- SQLite state (etag / last-modified / sha256 / status)
- Three crawl paths:
  - **site** — main website pages and articles
  - **data** — data-server directory indexes
  - **torrent-index** — magnet indexes without starting bulk downloads
- HTML link rewriting to local `/files/...` paths for offline browsing
- Automatic CSS/JS/favicon fetch even in laptop preview mode

### Local UI (port `8090`)
- Overview with live stats (no full-page refresh)
- Crawl Stats + recent activity + active/idle indicator
- Domains — only hosts that actually have mirror data, with Open mirror links
- Releases / Magnets / Settings (form + YAML overlay via `*.local.yaml`)
- Served mirror files under `/files/...`

### Run / deploy
- CLI: `ddosecrets-crawler run|status|serve|schedule`
- Configs: `config.yaml`, `config.laptop.yaml`, Docker variants
- `Dockerfile` + `docker-compose.yml`
- Docs: `README.md`, `HOW_TO_RUN.md`
- Helper scripts under `scripts/`

---

## 3. Architecture (short)

```
CLI / schedule
    → pipeline (site | data | torrent-index | …)
        → PoliteHttpClient
        → SQLite state DB
        → mirror/{site,data,torrents,metadata}

UI (FastAPI + Jinja)
    → read state + serve mirror from /files
```

Data directories (gitignored):

- `mirror/` — HTML / indexes / metadata
- `state/` — crawl state database
- `logs/` — run logs
- `*.local.yaml` — local user overrides

---

## 4. Issues fixed during development

| Issue | Fix |
| --- | --- |
| Stats page jumped to top and looked stuck | Removed meta-refresh; AJAX updates every 5s |
| Activity visible but counters frozen | Fixed active detection; prioritize pending articles in queue |
| External links inside mirrored HTML | Rewrite allowed-host links to `/files/...` on save |
| Mirrored pages had no styles | Fetch CSS/JS and rewrite asset paths |
| Ghost host `data.ddosecrets.com` in Domains | Removed from seeds; Domains lists real hosts only |
| Torrents page showed a stub instead of the real site | Stop overwriting crawled `index.html` |

---

## 5. Current test status (after wipe + fresh crawl)

Laptop preview run with limits:

1. `torrent-index` (no torrent downloads)
2. `site --max-pages 25`
3. `data --max-indexes 2`

Approximate results at report time:

| Metric | Value |
| --- | --- |
| Pages saved | 26 |
| Articles / Releases | 16 |
| Data indexes | 2 |
| Magnets indexed | 353 |
| Pending queue | ~107 |
| Files downloaded (HTTP bulk) | 0 (intentional; torrent-first) |
| Approx. mirror size | ~2.3 MB |

UI: `http://127.0.0.1:8090`

---

## 6. Quick start

```bash
cd /home/farhad/Desktop/ddosecrets-crawler
source .venv/bin/activate

# UI
python -m ddosecrets_crawler serve -c config.laptop.yaml --host 127.0.0.1 --port 8090

# Preview crawl (separate terminal)
python -m ddosecrets_crawler run torrent-index -c config.laptop.yaml --no-torrent-download
python -m ddosecrets_crawler run site -c config.laptop.yaml --max-pages 25 --no-torrent-download
python -m ddosecrets_crawler run data -c config.laptop.yaml --max-indexes 2 --no-torrent-download
```

More detail: `HOW_TO_RUN.md`

---

## 7. Limits and suggested next steps

**Limits**
- Default runs are batch jobs; they stop at `--max-pages` until the next run
- A full public mirror is typically tens of TB; laptop mode is sample/metadata only
- Real torrent downloads need `aria2` and `torrent-start`

**Suggested next steps**
- Start/Stop crawl controls in the UI
- Continuous operation via `schedule` or systemd
- Broader coverage of torrents/data subpages and static assets
- Clean leftover `data.ddosecrets.com` mentions in docs if any remain

---

## 8. Conclusion

A custom polite incremental crawler for DDoSecrets was delivered, with a local browse UI, offline link rewriting, laptop/Docker configs, and a successful from-scratch preview crawl. A custom crawler was preferred over Scrapy because politeness control, filesystem mirroring, and an integrated UI were the main requirements.
