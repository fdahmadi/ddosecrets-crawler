from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    url TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    host TEXT,
    local_path TEXT,
    content_type TEXT,
    etag TEXT,
    last_modified TEXT,
    content_length INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    http_status INTEGER,
    error TEXT,
    discovered_at REAL,
    updated_at REAL,
    downloaded_at REAL,
    extra_json TEXT
);

CREATE TABLE IF NOT EXISTS releases (
    slug TEXT PRIMARY KEY,
    title TEXT,
    article_url TEXT,
    published_at TEXT,
    description TEXT,
    countries TEXT,
    types TEXT,
    source TEXT,
    download_size_bytes INTEGER,
    magnets_json TEXT,
    downloads_json TEXT,
    torrents_json TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS magnets (
    infohash TEXT PRIMARY KEY,
    title TEXT,
    magnet TEXT NOT NULL,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    aria2_gid TEXT,
    local_path TEXT,
    discovered_at REAL,
    updated_at REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    finished_at REAL,
    mode TEXT,
    stats_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status);
CREATE INDEX IF NOT EXISTS idx_resources_kind ON resources(kind);
CREATE INDEX IF NOT EXISTS idx_magnets_status ON magnets(status);
"""


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=30000;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert_resource(self, **fields: Any) -> None:
        now = time.time()
        fields.setdefault("discovered_at", now)
        fields["updated_at"] = now
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        # Never clobber a completed download back to pending via rediscovery
        update_parts = []
        for c in cols:
            if c in ("url", "discovered_at"):
                continue
            if c == "status":
                update_parts.append(
                    "status=CASE WHEN resources.status='done' THEN resources.status "
                    "ELSE excluded.status END"
                )
            elif c == "updated_at":
                # Do not bump activity timestamps for pure rediscovery of known URLs
                update_parts.append(
                    "updated_at=CASE "
                    "WHEN excluded.status='pending' "
                    " AND resources.status IN ('done','pending','deferred_torrent') "
                    " THEN resources.updated_at "
                    "ELSE excluded.updated_at END"
                )
            else:
                update_parts.append(f"{c}=excluded.{c}")
        updates = ", ".join(update_parts)
        sql = (
            f"INSERT INTO resources ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(url) DO UPDATE SET {updates}, "
            f"discovered_at=COALESCE(resources.discovered_at, excluded.discovered_at)"
        )
        self.conn.execute(sql, [fields[c] for c in cols])
        self.conn.commit()

    def get_resource(self, url: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM resources WHERE url=?", (url,))
        return cur.fetchone()

    def mark_resource(
        self,
        url: str,
        status: str,
        *,
        error: str | None = None,
        **extra: Any,
    ) -> None:
        existing = self.get_resource(url)
        kind = extra.pop("kind", None) or (
            existing["kind"] if existing is not None else "unknown"
        )
        fields = {"status": status, "error": error, **extra}
        if status == "done":
            fields["downloaded_at"] = time.time()
        self.upsert_resource(url=url, kind=kind, **fields)

    def touch_resource(
        self,
        url: str,
        *,
        status: str = "done",
        http_status: int | None = None,
        sha256: str | None = None,
    ) -> None:
        """Confirm a resource is still current after revalidation (e.g. HTTP 304)."""
        now = time.time()
        sets = ["status=?", "updated_at=?", "error=NULL"]
        args: list[Any] = [status, now]
        if http_status is not None:
            sets.append("http_status=?")
            args.append(http_status)
        if sha256 is not None:
            sets.append("sha256=?")
            args.append(sha256)
        args.append(url)
        self.conn.execute(
            f"UPDATE resources SET {', '.join(sets)} WHERE url=?",
            args,
        )
        self.conn.commit()

    def requeue_for_revalidation(
        self,
        *,
        hosts: list[str] | None = None,
        older_than_seconds: float = 0.0,
        limit: int = 200,
        kinds: list[str] | None = None,
    ) -> int:
        """
        Mark completed resources as pending so the crawler re-fetches them
        with conditional headers (If-None-Match / If-Modified-Since).
        Bypasses the done→pending protection in upsert_resource.
        """
        if limit <= 0:
            return 0
        now = time.time()
        cutoff = now - max(0.0, older_than_seconds)
        clauses = ["status='done'", "updated_at<=?"]
        args: list[Any] = [cutoff]
        if hosts:
            placeholders = ", ".join("?" for _ in hosts)
            clauses.append(f"LOWER(COALESCE(host,'')) IN ({placeholders})")
            args.extend(h.lower() for h in hosts)
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            args.extend(kinds)
        where = " AND ".join(clauses)
        rows = self.conn.execute(
            f"SELECT url FROM resources WHERE {where} "
            f"ORDER BY updated_at ASC LIMIT ?",
            [*args, int(limit)],
        ).fetchall()
        if not rows:
            return 0
        urls = [r["url"] for r in rows]
        self.conn.executemany(
            "UPDATE resources SET status='pending', updated_at=?, error=NULL WHERE url=?",
            [(now, u) for u in urls],
        )
        self.conn.commit()
        return len(urls)

    def iter_resources(
        self, *, kind: str | None = None, status: str | None = None
    ) -> Iterable[sqlite3.Row]:
        clauses: list[str] = []
        args: list[Any] = []
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if status:
            clauses.append("status=?")
            args.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = self.conn.execute(f"SELECT * FROM resources {where}", args)
        yield from cur.fetchall()

    def upsert_release(self, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        for key in ("countries", "types", "magnets_json", "downloads_json", "torrents_json"):
            val = fields.get(key)
            if isinstance(val, (list, dict)):
                fields[key] = json.dumps(val, ensure_ascii=False)
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "slug")
        sql = (
            f"INSERT INTO releases ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(slug) DO UPDATE SET {updates}"
        )
        self.conn.execute(sql, [fields[c] for c in cols])
        self.conn.commit()

    def upsert_magnet(
        self,
        infohash: str,
        magnet: str,
        *,
        title: str | None = None,
        source: str | None = None,
        status: str = "queued",
    ) -> bool:
        """Return True if newly inserted or magnet URL changed."""
        now = time.time()
        existing = self.conn.execute(
            "SELECT magnet, status FROM magnets WHERE infohash=?", (infohash,)
        ).fetchone()
        if existing and existing["magnet"] == magnet and existing["status"] == "done":
            return False
        self.conn.execute(
            """
            INSERT INTO magnets (infohash, title, magnet, source, status, discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(infohash) DO UPDATE SET
                title=COALESCE(excluded.title, magnets.title),
                magnet=excluded.magnet,
                source=COALESCE(excluded.source, magnets.source),
                status=CASE
                    WHEN magnets.status='done' AND magnets.magnet=excluded.magnet THEN magnets.status
                    ELSE excluded.status
                END,
                updated_at=excluded.updated_at
            """,
            (infohash, title, magnet, source, status, now, now),
        )
        self.conn.commit()
        return existing is None or existing["magnet"] != magnet

    def queued_magnets(self, limit: int = 50) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM magnets WHERE status IN ('queued','error') "
            "ORDER BY discovered_at ASC LIMIT ?",
            (limit,),
        )
        return list(cur.fetchall())

    def update_magnet(self, infohash: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE magnets SET {sets} WHERE infohash=?",
            [*fields.values(), infohash],
        )
        self.conn.commit()

    def start_run(self, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, mode) VALUES (?, ?)",
            (time.time(), mode),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, stats_json=? WHERE id=?",
            (time.time(), json.dumps(stats), run_id),
        )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for table in ("resources", "magnets"):
            rows = self.conn.execute(
                f"SELECT status, COUNT(*) AS c FROM {table} GROUP BY status"
            ).fetchall()
            out[table] = {r["status"]: r["c"] for r in rows}
            out[f"{table}_total"] = sum(out[table].values())
        out["releases_total"] = self.conn.execute(
            "SELECT COUNT(*) AS c FROM releases"
        ).fetchone()["c"]
        return out

    def crawl_stats(self) -> dict[str, Any]:
        """Detailed crawler metrics for the UI dashboard."""
        base = self.stats()
        by_kind: dict[str, dict[str, int]] = {}
        for row in self.conn.execute(
            "SELECT kind, status, COUNT(*) AS c FROM resources GROUP BY kind, status"
        ):
            by_kind.setdefault(row["kind"] or "unknown", {})[row["status"]] = row["c"]

        bytes_row = self.conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status='done' THEN content_length ELSE 0 END), 0) AS bytes_done,
              COALESCE(SUM(CASE WHEN status='done' AND kind='data_file' THEN content_length ELSE 0 END), 0) AS file_bytes_done,
              COALESCE(SUM(CASE WHEN status='done' AND kind IN ('page','article','feed','static') THEN content_length ELSE 0 END), 0) AS page_bytes_done,
              COUNT(CASE WHEN status='done' AND kind='data_file' THEN 1 END) AS files_downloaded,
              COUNT(CASE WHEN status='done' AND kind IN ('page','article','feed','static') THEN 1 END) AS pages_saved,
              COUNT(CASE WHEN status='done' AND kind='article' THEN 1 END) AS articles_saved,
              COUNT(CASE WHEN status='done' AND kind='data_index' THEN 1 END) AS indexes_saved,
              COUNT(CASE WHEN status='pending' THEN 1 END) AS pending,
              COUNT(CASE WHEN status='error' THEN 1 END) AS errors,
              COUNT(CASE WHEN status='deferred_torrent' THEN 1 END) AS deferred_torrent
            FROM resources
            """
        ).fetchone()

        last_runs = [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT id, mode, started_at, finished_at, stats_json
                FROM runs
                ORDER BY id DESC
                LIMIT 8
                """
            ).fetchall()
        ]
        for run in last_runs:
            try:
                run["stats"] = json.loads(run["stats_json"] or "{}")
            except json.JSONDecodeError:
                run["stats"] = {}
            run.pop("stats_json", None)

        recent_rows = self.conn.execute(
            """
            SELECT url, kind, status, updated_at
            FROM resources
            WHERE updated_at IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 12
            """
        ).fetchall()
        recent_activity = [dict(r) for r in recent_rows]
        last_activity_at = recent_activity[0]["updated_at"] if recent_activity else None

        # Close out ancient unfinished rows so the UI does not stay "active" forever
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at = COALESCE(finished_at, ?),
                stats_json = COALESCE(stats_json, '{"abandoned": true}')
            WHERE finished_at IS NULL AND started_at < ?
            """,
            (now := time.time(), now - 30 * 60),
        )
        self.conn.commit()

        open_run = self.conn.execute(
            """
            SELECT id, mode, started_at
            FROM runs
            WHERE finished_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        # Active only when resources were touched recently (not just a stale open run)
        active = bool(last_activity_at and (now - float(last_activity_at)) < 45)

        pending_articles = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM resources
            WHERE status='pending' AND (kind='article' OR url LIKE '%/article/%')
            """
        ).fetchone()["c"]

        return {
            **base,
            "by_kind": by_kind,
            "pages_saved": bytes_row["pages_saved"] or 0,
            "articles_saved": bytes_row["articles_saved"] or 0,
            "indexes_saved": bytes_row["indexes_saved"] or 0,
            "files_downloaded": bytes_row["files_downloaded"] or 0,
            "pending": bytes_row["pending"] or 0,
            "pending_articles": pending_articles or 0,
            "errors": bytes_row["errors"] or 0,
            "deferred_torrent": bytes_row["deferred_torrent"] or 0,
            "bytes_done": int(bytes_row["bytes_done"] or 0),
            "file_bytes_done": int(bytes_row["file_bytes_done"] or 0),
            "page_bytes_done": int(bytes_row["page_bytes_done"] or 0),
            "last_runs": last_runs,
            "recent_activity": recent_activity,
            "last_activity_at": last_activity_at,
            "crawler_active": active,
            "open_run": dict(open_run) if open_run else None,
            "server_time": now,
        }


def url_needs_download(
    row: sqlite3.Row | None,
    *,
    etag: str | None,
    last_modified: str | None,
    content_length: int | None,
    local_path: Path | None,
) -> bool:
    """Incremental check: skip if unchanged and local file exists."""
    if row is None or row["status"] != "done":
        return True
    if local_path is not None and not local_path.exists():
        return True
    if etag and row["etag"] and etag == row["etag"]:
        return False
    if (
        content_length is not None
        and row["content_length"] is not None
        and int(content_length) == int(row["content_length"])
        and local_path is not None
        and local_path.exists()
        and local_path.stat().st_size == int(content_length)
    ):
        return False
    if (
        last_modified
        and row["last_modified"]
        and last_modified == row["last_modified"]
        and local_path is not None
        and local_path.exists()
    ):
        return False
    # If we have a completed local file and no strong validators changed unknowns,
    # still re-check when content_length differs.
    if (
        content_length is not None
        and row["content_length"] is not None
        and int(content_length) != int(row["content_length"])
    ):
        return True
    if local_path is not None and local_path.exists() and row["status"] == "done":
        # Conservative: if validators missing but file present, skip
        if not etag and not last_modified and content_length is None:
            return False
    return True