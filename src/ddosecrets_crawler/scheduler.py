from __future__ import annotations

import asyncio
import logging
import time

from .config import AppConfig, ensure_runtime_dirs, mode_priority
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
            continue
        if "/article/" in url:
            articles += 1
        else:
            other += 1
    return articles, other


def _pending_data_indexes(db: StateDB) -> int:
    """Pending directory indexes on data.* hosts."""
    n = 0
    for row in db.iter_resources(kind="data_index", status="pending"):
        host = (row["host"] or "").lower()
        if host.startswith("data."):
            n += 1
    return n


def _mode_enabled(cfg: AppConfig, mode: str) -> bool:
    if mode == "data":
        return bool(cfg.data.enabled)
    if mode == "site":
        return bool(cfg.site.enabled)
    if mode == "torrent":
        return bool(cfg.torrent.enabled)
    return False


def _ordered_modes(cfg: AppConfig) -> list[str]:
    """Enabled crawl modes sorted by host_priorities (highest first)."""
    modes = [m for m in ("data", "site", "torrent") if _mode_enabled(cfg, m)]
    # Stable tie-break: data, site, torrent when priorities are equal
    tie = {"data": 0, "site": 1, "torrent": 2}
    return sorted(
        modes,
        key=lambda m: (-mode_priority(cfg, m), tie.get(m, 99)),
    )


def _mode_has_pending(cfg: AppConfig, db: StateDB, mode: str) -> bool:
    if mode == "data":
        return _pending_data_indexes(db) > 0
    if mode == "site":
        a, o = _pending_followable(db)
        return (a + o) > 0
    return False


def _higher_priority_pending(
    cfg: AppConfig, db: StateDB, mode: str, *, continuous: bool
) -> bool:
    """True if a higher-priority mode still has continuous work to drain."""
    if not continuous:
        return False
    my_p = mode_priority(cfg, mode)
    for other in _ordered_modes(cfg):
        if other == mode:
            continue
        if mode_priority(cfg, other) <= my_p:
            continue
        if _mode_has_pending(cfg, db, other):
            return True
    return False


async def run_scheduler(cfg: AppConfig, *, once: bool = False) -> None:
    """
    Continuous incremental worker for VPS / unattended use.

    Mode order comes from config host_priorities (higher number first).
    While continuous=true, lower-priority modes wait until higher ones have
    no pending work.
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

    order = _ordered_modes(cfg)
    log.info(
        "Scheduler started continuous=%s batch_pause=%.0fs idle_sleep=%.0fs "
        "site_iv=%.0fs data_iv=%.0fs torrent_iv=%.0fs mode_order=%s",
        continuous,
        batch_pause,
        idle_sleep,
        site_iv,
        data_iv,
        torrent_iv,
        [
            f"{m}:{mode_priority(cfg, m)}"
            for m in order
        ],
    )

    while True:
        ran_anything = False

        for mode in _ordered_modes(cfg):
            if mode == "data":
                did = await _run_data(
                    cfg,
                    last_data=last_data,
                    data_iv=data_iv,
                    batch_pause=batch_pause,
                    continuous=continuous,
                )
                if did:
                    last_data = time.time()
                    ran_anything = True
            elif mode == "torrent":
                did = await _run_torrent(
                    cfg,
                    last_torrent=last_torrent,
                    torrent_iv=torrent_iv,
                    batch_pause=batch_pause,
                    continuous=continuous,
                )
                if did:
                    last_torrent = time.time()
                    ran_anything = True
            elif mode == "site":
                did, last_site = await _run_site(
                    cfg,
                    last_site=last_site,
                    last_data=last_data,
                    last_torrent=last_torrent,
                    site_iv=site_iv,
                    data_iv=data_iv,
                    torrent_iv=torrent_iv,
                    batch_pause=batch_pause,
                    continuous=continuous,
                )
                if did:
                    ran_anything = True

        if time.time() - last_full >= full_iv:
            log.info("Scheduler starting full reconcile (revalidate done pages)")
            db = StateDB.from_config(cfg)
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
            db = StateDB.from_config(cfg)
            try:
                any_pending = any(
                    _mode_has_pending(cfg, db, m) for m in _ordered_modes(cfg)
                )
            finally:
                db.close()
            if any_pending:
                sleep_for = min(sleep_for, batch_pause)

        log.info(
            "Scheduler idle sleeping %.0fs (ran_anything=%s)",
            sleep_for,
            ran_anything,
        )
        await asyncio.sleep(sleep_for)


async def _run_data(
    cfg: AppConfig,
    *,
    last_data: float,
    data_iv: float,
    batch_pause: float,
    continuous: bool,
) -> bool:
    ran = False
    while cfg.data.enabled:
        db = StateDB.from_config(cfg)
        try:
            if _higher_priority_pending(cfg, db, "data", continuous=continuous):
                break
            pending_data = _pending_data_indexes(db)
        finally:
            db.close()

        due = (time.time() - last_data) >= data_iv
        first_run = last_data == 0.0
        should_run = due or first_run or (continuous and pending_data > 0)
        if not should_run:
            break

        if due or first_run:
            db = StateDB.from_config(cfg)
            try:
                n = db.requeue_for_revalidation(
                    hosts=["data.ddosecrets.org"],
                    older_than_seconds=data_iv,
                    limit=max(int(cfg.data.max_indexes or 100) * 2, 50),
                    kinds=["data_index"],
                )
                if n:
                    log.info(
                        "Requeued %d stale data indexes for conditional revalidation",
                        n,
                    )
                pending_data = _pending_data_indexes(db)
            finally:
                db.close()

        log.info(
            "Scheduler starting mode=data priority=%d pending_indexes=%d max_indexes=%s",
            mode_priority(cfg, "data"),
            pending_data,
            cfg.data.max_indexes or "unlimited",
        )
        try:
            await run_pipeline(cfg, mode="data")
        except Exception:  # noqa: BLE001
            log.exception("Scheduler mode data failed")
        last_data = time.time()
        ran = True

        if not continuous:
            break

        db = StateDB.from_config(cfg)
        try:
            pending_data = _pending_data_indexes(db)
        finally:
            db.close()
        if pending_data <= 0:
            log.info("Data pending index queue drained")
            break

        log.info(
            "Data queue still has %d pending indexes; pausing %.0fs before next batch",
            pending_data,
            batch_pause,
        )
        await asyncio.sleep(batch_pause)
    return ran


async def _run_torrent(
    cfg: AppConfig,
    *,
    last_torrent: float,
    torrent_iv: float,
    batch_pause: float,
    continuous: bool,
) -> bool:
    db = StateDB.from_config(cfg)
    try:
        if _higher_priority_pending(cfg, db, "torrent", continuous=continuous):
            log.info(
                "Deferring torrent-index; higher-priority mode still has pending work"
            )
            return False
    finally:
        db.close()

    if time.time() - last_torrent < torrent_iv and last_torrent != 0.0:
        return False

    log.info(
        "Scheduler starting mode=torrent-index priority=%d",
        mode_priority(cfg, "torrent"),
    )
    try:
        await run_pipeline(cfg, mode="torrent-index")
    except Exception:  # noqa: BLE001
        log.exception("Scheduler mode torrent-index failed")
    await asyncio.sleep(min(30.0, batch_pause))
    return True


async def _run_site(
    cfg: AppConfig,
    *,
    last_site: float,
    last_data: float,
    last_torrent: float,
    site_iv: float,
    data_iv: float,
    torrent_iv: float,
    batch_pause: float,
    continuous: bool,
) -> tuple[bool, float]:
    ran = False
    while cfg.site.enabled:
        db = StateDB.from_config(cfg)
        try:
            if _higher_priority_pending(cfg, db, "site", continuous=continuous):
                log.info(
                    "Deferring site crawl; higher-priority mode still has pending work"
                )
                break
            pending_articles, pending_other = _pending_followable(db)
        finally:
            db.close()

        pending_total = pending_articles + pending_other
        due = (time.time() - last_site) >= site_iv
        first_run = last_site == 0.0
        should_run = due or (continuous and pending_total > 0) or first_run
        if not should_run:
            break

        if due or first_run:
            batch = int(cfg.site.max_pages or 100)
            db = StateDB.from_config(cfg)
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
            "Scheduler starting mode=site priority=%d pending_articles=%d "
            "pending_other=%d max_pages=%s",
            mode_priority(cfg, "site"),
            pending_articles,
            pending_other,
            cfg.site.max_pages or "unlimited-batch",
        )
        try:
            await run_pipeline(cfg, mode="site")
        except Exception:  # noqa: BLE001
            log.exception("Scheduler mode site failed")
        last_site = time.time()
        ran = True

        if not continuous:
            break

        db = StateDB.from_config(cfg)
        try:
            pending_articles, pending_other = _pending_followable(db)
            if _higher_priority_pending(cfg, db, "site", continuous=continuous):
                log.info(
                    "Yielding site drain for higher-priority pending work "
                    "(articles=%d other=%d)",
                    pending_articles,
                    pending_other,
                )
                break
        finally:
            db.close()

        if pending_articles + pending_other <= 0:
            log.info("Site pending queue drained")
            break

        # Also yield if a higher-priority mode becomes due on interval
        now_chk = time.time()
        for other in _ordered_modes(cfg):
            if mode_priority(cfg, other) <= mode_priority(cfg, "site"):
                continue
            if other == "data" and (now_chk - last_data >= data_iv):
                log.info("Yielding site drain; data mode is due")
                return ran, last_site
            if other == "torrent" and (now_chk - last_torrent >= torrent_iv):
                log.info("Yielding site drain; torrent mode is due")
                return ran, last_site

        log.info(
            "Site queue still has work (articles=%d other=%d); "
            "pausing %.0fs before next batch",
            pending_articles,
            pending_other,
            batch_pause,
        )
        await asyncio.sleep(batch_pause)

    return ran, last_site
