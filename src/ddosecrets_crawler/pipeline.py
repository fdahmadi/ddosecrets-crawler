from __future__ import annotations

import logging
from typing import Any

from rich.console import Console
from rich.table import Table

from .config import AppConfig, ensure_runtime_dirs
from .data_mirror import DataMirror
from .db import StateDB
from .http_client import PoliteHttpClient
from .metadata import export_catalog_json
from .polite import PoliteLimiter
from .site_crawler import SiteCrawler
from .torrent_sync import TorrentSync

log = logging.getLogger(__name__)
console = Console()


async def run_pipeline(cfg: AppConfig, *, mode: str = "all") -> dict[str, Any]:
    """
    Modes:
      all            - site + torrent indexes + data mirror + start torrents
      site           - website pages/articles only
      data           - data.* HTTP directory mirror
      torrent-index  - magnets.csv / RSS only
      torrent-start  - start queued magnet downloads via aria2
      catalog        - export searchable catalog JSON
      full           - same as all (used by scheduler reconcile)
      status         - print DB stats
    """
    ensure_runtime_dirs(cfg)
    db = StateDB(cfg.state_path)
    run_id = db.start_run(mode)
    limiter = PoliteLimiter(cfg.polite)
    http = PoliteHttpClient(cfg, limiter)
    summary: dict[str, Any] = {"mode": mode}

    try:
        if mode == "status":
            summary["stats"] = db.stats()
            _print_stats(summary["stats"])
            return summary

        if mode in ("all", "full", "torrent-index", "torrent-start"):
            if cfg.torrent.enabled:
                ts = TorrentSync(cfg, db, http)
                if mode != "torrent-start":
                    summary["torrent_index"] = await ts.sync_indexes()
                if mode in ("all", "full", "torrent-start"):
                    summary["torrent_start"] = await ts.start_downloads()
                ts.export_magnet_queue(cfg.mirror_path / "metadata" / "magnet_queue.json")

        if mode in ("all", "full", "site") and cfg.site.enabled:
            crawler = SiteCrawler(cfg, db, http)
            summary["site"] = await crawler.crawl()

        if mode in ("all", "full", "data") and cfg.data.enabled:
            mirror = DataMirror(cfg, db, http)
            summary["data"] = await mirror.mirror()

        if mode in ("all", "full", "catalog", "site"):
            n = export_catalog_json(
                db, cfg.mirror_path / "metadata" / "catalog.json"
            )
            summary["catalog_count"] = n

        summary["db"] = db.stats()
        _print_stats(summary.get("db") or {})
        return summary
    except Exception as exc:  # noqa: BLE001
        summary["error"] = str(exc)
        raise
    finally:
        try:
            db.finish_run(run_id, summary)
        except Exception:  # noqa: BLE001
            log.exception("Failed to finish run #%s", run_id)
        await http.aclose()
        db.close()


def _print_stats(stats: dict[str, Any]) -> None:
    table = Table(title="Mirror state")
    table.add_column("Bucket")
    table.add_column("Detail")
    for key, value in stats.items():
        table.add_row(str(key), str(value))
    console.print(table)