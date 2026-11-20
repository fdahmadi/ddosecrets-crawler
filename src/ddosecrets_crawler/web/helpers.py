from __future__ import annotations

import json
import time
from typing import Any


def decode_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def human_size(num: int | None) -> str:
    if num is None:
        return "—"
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while n >= 1000 and idx < len(units) - 1:
        n /= 1000.0
        idx += 1
    if idx == 0:
        return f"{int(n)} {units[idx]}"
    return f"{n:.2f} {units[idx]}"


def format_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


def duration_seconds(started: float | None, finished: float | None) -> str:
    if not started or not finished:
        return "—"
    try:
        sec = max(0, int(float(finished) - float(started)))
    except (TypeError, ValueError):
        return "—"
    if sec < 60:
        return f"{sec}s"
    mins, sec = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m {sec}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


_DISK_CACHE: dict[str, tuple[float, int]] = {}


def storage_size_bytes(
    storage, prefix: str = "", *, cache_seconds: float = 30.0
) -> int:
    """Storage size with short TTL cache to keep UI snappy."""
    key = f"{storage.describe()}::{prefix}"
    now = time.time()
    cached = _DISK_CACHE.get(key)
    if cached and (now - cached[0]) < cache_seconds:
        return cached[1]
    try:
        total = int(storage.total_bytes(prefix))
    except Exception:  # noqa: BLE001
        total = 0
    _DISK_CACHE[key] = (now, total)
    return total


def local_article_key(slug: str) -> str:
    return f"site/ddosecrets.org/article/{slug}.html"


def article_exists(storage, slug: str) -> bool:
    return storage.exists(local_article_key(slug))


def local_article_url(slug: str) -> str:
    return f"/files/site/ddosecrets.org/article/{slug}.html"


def _guess_section(host: str) -> str:
    h = (host or "").lower()
    if h.startswith("data."):
        return "data"
    # HTML landing pages for torrents.* live under site/<host>/
    return "site"


def ensure_torrents_domain_mirror(
    storage, host: str = "torrents.ddosecrets.org"
) -> bool:
    """
    Ensure torrents host has a browsable folder.

    Never overwrite a crawled homepage (index.html). Only write a fallback
    indexes.html listing saved CSV/RSS metadata when the real page is absent.
    """
    index_key = f"site/{host}/index.html"
    if storage.exists(index_key):
        return True

    index_files = [
        name
        for name in (
            "magnets.csv",
            "releases.xml",
            "magnet_queue.json",
            "site_rss.xml",
        )
        if storage.exists(f"metadata/{name}")
    ]
    if not index_files:
        # Still listable if any object under the host prefix exists
        return bool(storage.list_dir(f"site/{host}"))

    links = "\n".join(
        f'      <li><a href="/files/metadata/{name}">{name}</a></li>'
        for name in index_files
    )
    # Fallback only — real site HTML comes from torrent-index crawl
    storage.put_text(
        index_key,
        f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>{host} — local mirror</title>
  <style>
    body {{ font-family: Georgia, serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
    a {{ color: #0f5c4c; }}
    .mono {{ font-family: ui-monospace, monospace; font-size: 0.92em; }}
  </style>
</head>
<body>
  <h1>{host}</h1>
  <p>Homepage HTML not mirrored yet. Saved torrent indexes:</p>
  <ul>
{links}
  </ul>
  <p><a href="/magnets">Open magnet queue in UI</a> · <a href="/domains">Back to domains</a></p>
</body>
</html>
""",
        content_type="text/html; charset=utf-8",
    )
    return True


def _domain_entry(
    storage,
    section: str,
    host: str,
    *,
    mirror_port: int | None = None,
    public_base: str = "",
) -> tuple[bool, str | None, str | None]:
    """Return (has_files, open_url, prefix_key)."""
    prefix = f"{section}/{host}"
    entries = storage.list_dir(prefix)
    if not entries and not storage.exists(f"{prefix}/index.html"):
        return False, None, None
    if mirror_port:
        base = (public_base or "").rstrip("/")
        if base:
            open_url = f"{base}:{mirror_port}/"
        else:
            open_url = f"http://127.0.0.1:{mirror_port}/"
        return True, open_url, prefix
    if storage.exists(f"{prefix}/index.html"):
        return True, f"/files/{prefix}/index.html", prefix
    return True, f"/files/{prefix}/", prefix


def _resolve_serve_lookup(serve_cfg) -> tuple[str, Any]:
    """Return (public_base, port_lookup_fn) from AppConfig or ServeConfig."""
    public_base = ""

    def _no_port(_host: str) -> int | None:
        return None

    if serve_cfg is None:
        return public_base, _no_port

    # Full AppConfig
    if hasattr(serve_cfg, "mirror_port_for") and hasattr(serve_cfg, "serve"):
        public_base = (serve_cfg.serve.public_base or "").rstrip("/")
        return public_base, serve_cfg.mirror_port_for

    # Bare ServeConfig
    if hasattr(serve_cfg, "mirrors"):
        public_base = (getattr(serve_cfg, "public_base", "") or "").rstrip("/")
        mirrors = getattr(serve_cfg, "mirrors", {}) or {}

        def port_for(host: str) -> int | None:
            host = (host or "").lower()
            entry = mirrors.get(host)
            if entry is not None:
                return int(entry.port)
            for pattern, mp in mirrors.items():
                pat = (pattern or "").lower()
                if host == pat or host.endswith("." + pat):
                    return int(mp.port)
            return None

        return public_base, port_for

    return public_base, _no_port


def list_mirrored_domains(
    storage,
    database,
    *,
    seeds: list[str] | None = None,  # unused; kept for call-site compatibility
    serve_cfg=None,
) -> list[dict[str, Any]]:
    """Build clickable domain list from DB hosts and storage prefixes only."""
    ensure_torrents_domain_mirror(storage)
    public_base, port_for = _resolve_serve_lookup(serve_cfg)

    stats_by_host: dict[str, dict[str, int]] = {}
    for row in database.conn.execute("""
        SELECT
          host,
          COUNT(*) AS total,
          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
          SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors
        FROM resources
        WHERE host IS NOT NULL AND TRIM(host) != ''
        GROUP BY host
        """):
        stats_by_host[row["host"]] = {
            "total": int(row["total"] or 0),
            "done": int(row["done"] or 0),
            "pending": int(row["pending"] or 0),
            "errors": int(row["errors"] or 0),
        }

    magnets_total = database.conn.execute(
        "SELECT COUNT(*) AS c FROM magnets"
    ).fetchone()["c"]

    hosts: set[str] = set(stats_by_host)
    for section in ("site", "data"):
        for host in storage.list_hosts(section):
            hosts.add(host)

    discovered: dict[str, dict[str, Any]] = {}
    for host in sorted(hosts):
        section = _guess_section(host)
        if host.startswith("torrents."):
            ensure_torrents_domain_mirror(storage, host=host)
            section = "site"
        mirror_port = port_for(host)
        # Admin file-browser URL (always on the admin UI)
        browse_url = None
        has_files, open_url, prefix = _domain_entry(
            storage,
            section,
            host,
            mirror_port=mirror_port,
            public_base=public_base,
        )
        st = stats_by_host.get(host, {})
        if not has_files and section == "site":
            has_data, data_url, data_prefix = _domain_entry(
                storage,
                "data",
                host,
                mirror_port=mirror_port,
                public_base=public_base,
            )
            if has_data:
                section = "data"
                has_files, open_url, prefix = True, data_url, data_prefix
        if has_files and prefix:
            browse_url = f"/files/{prefix}/"
        if not has_files and not st:
            continue
        item = {
            "host": host,
            "section": section,
            "source_url": f"https://{host}/",
            "has_mirror": has_files,
            # Dedicated mirror port when configured; else admin /files/ path
            "open_url": open_url,
            "browse_url": browse_url,
            "mirror_port": mirror_port,
            "local_path": prefix,
            "disk_bytes": storage_size_bytes(storage, prefix) if prefix else 0,
            "total": st.get("total", 0),
            "done": st.get("done", 0),
            "pending": st.get("pending", 0),
            "errors": st.get("errors", 0),
            "magnets_total": (
                int(magnets_total or 0) if host.startswith("torrents.") else None
            ),
        }
        discovered[host] = item

    domains = list(discovered.values())
    for item in domains:
        item["disk_human"] = human_size(item.get("disk_bytes") or 0)
    domains.sort(key=lambda d: (-int(d.get("done") or 0), d.get("host") or ""))
    return domains


def release_to_dict(row) -> dict[str, Any]:
    return {
        "slug": row["slug"],
        "title": row["title"] or row["slug"],
        "article_url": row["article_url"],
        "published_at": row["published_at"],
        "description": row["description"] or "",
        "countries": decode_json_list(row["countries"]),
        "types": decode_json_list(row["types"]),
        "source": row["source"],
        "download_size_bytes": row["download_size_bytes"],
        "download_size_human": human_size(row["download_size_bytes"]),
        "magnets": decode_json_list(row["magnets_json"]),
        "downloads": decode_json_list(row["downloads_json"]),
        "torrents": decode_json_list(row["torrents_json"]),
    }
