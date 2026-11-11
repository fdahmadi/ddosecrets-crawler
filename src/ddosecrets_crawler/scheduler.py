from __future__ import annotations

import asyncio
import logging
import time

from .config import AppConfig, ensure_runtime_dirs
from .db import StateDB
from .pipeline import run_pipeline

log = logging.getLogger(__name__)


def _pending_followable(db: StateDB) -> tuple[int, int]:
    """Return (pending_articles, pending_other) for the main site host."""
    articles = 0
    other = 0
    for row in db.iter_resources(status="pending"):
        url = row["url"] or ""
        host = (row["host"] or "").lower()
        if host not in ("ddosecrets.org", "www.ddosecrets.org"):
            # data/torrents pending is handled by their own modes
            continue
        if "/article/" in url:
            articles += 1
        else:
            other += 1
    return articles, other


async def run_scheduler(cfg: AppConfig, *, once: bool = False) -> None:
    """
    Continuous incremental worker for VPS / unattended use.

    - Runs torrent-index / site / data on their intervals
    - When schedule.continuous is true, keeps running site batches while the
      pending queue still has followable URLs (no manual restarts)
    - Sleeps briefly between batches; longer idle sleep when the queue is empty
    """
    ensure_runtime_dirs(cfg)
    last_site = 0.0
    last_data = 0.0
    last_torrent = 0.0
    last_full = 0.0

    site_iv = max(60.0, cfg.schedule.site_interval_hours * 3600)
    data_iv = max(60.0, cfg.schedule.data_index_interval_hours * 3600)
    torrent_iv = max(60.0, cfg.schedule.torrent_index_interval_hours * 3600)
    full_iv = max(3600.0, cfg.schedule.full_reconcile_days * 86400)
    batch_pause = max(5.0, float(cfg.schedule.batch_pause_seconds or 45))
    idle_sleep = max(30.0, float(cfg.schedule.idle_sleep_seconds or 900))
    continuous = bool(cfg.schedule.continuous)

    log.info(
        "Scheduler started continuous=%s batch_pause=%.0fs idle_sleep=%.0fs "
        "site_iv=%.0fs data_iv=%.0fs torrent_iv=%.0fs",
        continuous,
        batch_pause,
        idle_sleep,
        site_iv,
        data_iv,
        torrent_iv,
    )

    while True:
        now = time.time()
        ran_anything = False

        if cfg.torrent.enabled and (now - last_torrent >= torrent_iv):
            log.info("Scheduler starting mode=torrent-index")
            try:
                await run_pipeline(cfg, mode="torrent-index")
            except Exception:  # noqa: BLE001
                log.exception("Scheduler mode torrent-index failed")
            last_torrent = time.time()
            ran_anything = True
            await asyncio.sleep(min(30.0, batch_pause))

        # data.* indexes (seeded separately; site crawler never follows data hosts)
        if cfg.data.enabled and (time.time() - last_data >= data_iv):
            log.info("Scheduler starting mode=data")
            try:
                await run_pipeline(cfg, mode="data")
            except Exception:  # noqa: BLE001
                log.exception("Scheduler mode data failed")
            last_data = time.time()
            ran_anything = True
            await asyncio.sleep(min(30.0, batch_pause))

        # Site: always run when due, or keep draining while continuous + pending
        while cfg.site.enabled:
            db = StateDB(cfg.state_path)
            try:
                pending_articles, pending_other = _pending_followable(db)
            finally:
                db.close()
            pending_total = pending_articles + pending_other
            due = (time.time() - last_site) >= site_iv
            first_run = last_site == 0.0
            should_run = due or (continuous and pending_total > 0) or first_run
            if not should_run:
                break

            # On interval (not every continuous drain batch), re-check stale done pages
            if due or first_run:
                batch = int(cfg.site.max_pages or 100)
                db = StateDB(cfg.state_path)
                try:
                    n = db.requeue_for_revalidation(
                        hosts=["ddosecrets.org", "www.ddosecrets.org"],
                        older_than_seconds=site_iv,
                        limit=max(batch * 2, 50),
                        kinds=["page", "article", "feed", "static"],
                    )
                    if n:
                        log.info(
                            "Requeued %d stale site pages for conditional revalidation",
                            n,
                        )
                    pending_articles, pending_other = _pending_followable(db)
                finally:
                    db.close()

            log.info(
                "Scheduler starting mode=site pending_articles=%d pending_other=%d "
                "max_pages=%s",
                pending_articles,
                pending_other,
                cfg.site.max_pages or "unlimited-batch",
            )
            try:
                await run_pipeline(cfg, mode="site")
            except Exception:  # noqa: BLE001
                log.exception("Scheduler mode site failed")
            last_site = time.time()
            ran_anything = True

            if not continuous:
                break

            db = StateDB(cfg.state_path)
            try:
                pending_articles, pending_other = _pending_followable(db)
            finally:
                db.close()
            if pending_articles + pending_other <= 0:
                log.info("Site pending queue drained")
                break

            # Do not starve data/torrent: yield after each site batch when they are due.
            # data.* is NOT discovered by the site crawler — DataMirror runs as mode=data.
            now_chk = time.time()
            data_due = cfg.data.enabled and (now_chk - last_data >= data_iv)
            torrent_due = cfg.torrent.enabled and (now_chk - last_torrent >= torrent_iv)
            if data_due or torrent_due:
                log.info(
                    "Yielding site drain (articles=%d other=%d) for due jobs "
                    "data=%s torrent=%s",
                    pending_articles,
                    pending_other,
                    data_due,
                    torrent_due,
                )
                break

            log.info(
                "Site queue still has work (articles=%d other=%d); "
                "pausing %.0fs before next batch",
                pending_articles,
                pending_other,
                batch_pause,
            )
            await asyncio.sleep(batch_pause)

        if time.time() - last_full >= full_iv:
            log.info("Scheduler starting full reconcile (revalidate done pages)")
            db = StateDB(cfg.state_path)
            try:
                n = db.requeue_for_revalidation(
                    hosts=["ddosecrets.org", "www.ddosecrets.org"],
                    older_than_seconds=0.0,
                    limit=5000,
                    kinds=["page", "article", "feed", "static"],
                )
            finally:
                db.close()
            if n:
                log.info("Full reconcile requeued %d site pages", n)
            try:
                await run_pipeline(cfg, mode="full")
            except Exception:  # noqa: BLE001
                log.exception("Scheduler mode full failed")
            last_full = time.time()
            ran_anything = True

        if once:
            break

        # Idle: wait until the next due job, but not longer than idle_sleep when continuous
        now = time.time()
        next_due = min(
            last_torrent + torrent_iv,
            last_site + site_iv,
            last_data + data_iv,
            last_full + full_iv,
        )
        sleep_for = max(30.0, next_due - now)
        if continuous:
            sleep_for = min(sleep_for, idle_sleep)
            # If anything is still pending, wake sooner
            db = StateDB(cfg.state_path)
            try:
                pa, po = _pending_followable(db)
            finally:
                db.close()
            if pa + po > 0:
                sleep_for = min(sleep_for, batch_pause)

        log.info(
            "Scheduler idle sleeping %.0fs (ran_anything=%s)",
            sleep_for,
            ran_anything,
        )
        await asyncio.sleep(sleep_for)
