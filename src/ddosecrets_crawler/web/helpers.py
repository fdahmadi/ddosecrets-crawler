from __future__ import annotations

import json
import time
from pathlib import Path
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


def dir_size_bytes(path: Path, *, cache_seconds: float = 30.0) -> int:
    """Directory size with short TTL cache to keep UI snappy."""
    key = str(path.resolve()) if path.exists() else str(path)
    now = time.time()
    cached = _DISK_CACHE.get(key)
    if cached and (now - cached[0]) < cache_seconds:
        return cached[1]
    if not path.exists():
        _DISK_CACHE[key] = (now, 0)
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    _DISK_CACHE[key] = (now, total)
    return total


def local_article_path(mirror_root: Path, slug: str) -> Path | None:
    path = mirror_root / "site" / "ddosecrets.org" / "article" / f"{slug}.html"
    return path if path.exists() else None


def local_article_url(slug: str) -> str:
    return f"/files/site/ddosecrets.org/article/{slug}.html"


def _guess_section(host: str) -> str:
    h = (host or "").lower()
    if h.startswith("data."):
        return "data"
    # HTML landing pages for torrents.* live under mirror/site/<host>/
    return "site"


def ensure_torrents_domain_mirror(mirror_root: Path, host: str = "torrents.ddosecrets.org") -> Path | None:
    """
    Ensure torrents host has a browsable folder.

    Never overwrite a crawled homepage (index.html). Only write a fallback
    indexes.html listing saved CSV/RSS metadata when the real page is absent.
    """
    meta = mirror_root / "metadata"
    index_files = [
        name
        for name in (
            "magnets.csv",
            "releases.xml",
            "magnet_queue.json",
            "site_rss.xml",
        )
        if (meta / name).is_file()
    ]
    dest_dir = mirror_root / "site" / host
    index = dest_dir / "index.html"
    if index.is_file():
        return dest_dir
    if not index_files:
        return dest_dir if dest_dir.is_dir() else None

    dest_dir.mkdir(parents=True, exist_ok=True)
    links = "\n".join(
        f'      <li><a href="/files/metadata/{name}">{name}</a></li>' for name in index_files
    )
    # Fallback only — real site HTML comes from torrent-index crawl
    index.write_text(
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
        encoding="utf-8",
    )
    return dest_dir


def _domain_entry(mirror_root: Path, section: str, host: str) -> tuple[bool, str | None, Path | None]:
    """Return (has_files, open_url, local_dir)."""
    local_dir = mirror_root / section / host
    if not local_dir.is_dir():
        return False, None, None
    index = local_dir / "index.html"
    if index.is_file():
        return True, f"/files/{section}/{host}/index.html", local_dir
    return True, f"/files/{section}/{host}/", local_dir


def list_mirrored_domains(
    mirror_root: Path,
    database,
    *,
    seeds: list[str] | None = None,  # unused; kept for call-site compatibility
) -> list[dict[str, Any]]:
    """Build clickable domain list from DB hosts and on-disk mirror folders only."""
    # Ensure torrents.* is browsable even though indexes live under metadata/
    ensure_torrents_domain_mirror(mirror_root)

    stats_by_host: dict[str, dict[str, int]] = {}
    for row in database.conn.execute(
        """
        SELECT
          host,
          COUNT(*) AS total,
          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
          SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors
        FROM resources
        WHERE host IS NOT NULL AND TRIM(host) != ''
        GROUP BY host
        """
    ):
        stats_by_host[row["host"]] = {
            "total": int(row["total"] or 0),
            "done": int(row["done"] or 0),
            "pending": int(row["pending"] or 0),
            "errors": int(row["errors"] or 0),
        }

    magnets_total = database.conn.execute("SELECT COUNT(*) AS c FROM magnets").fetchone()["c"]

    # Only real mirrored/tracked hosts — not empty config seeds
    hosts: set[str] = set(stats_by_host)
    for section in ("site", "data"):
        section_dir = mirror_root / section
        if not section_dir.is_dir():
            continue
        for host_dir in section_dir.iterdir():
            if host_dir.is_dir():
                hosts.add(host_dir.name)

    discovered: dict[str, dict[str, Any]] = {}
    for host in sorted(hosts):
        section = _guess_section(host)
        if host.startswith("torrents."):
            ensure_torrents_domain_mirror(mirror_root, host=host)
            section = "site"
        has_files, open_url, local_dir = _domain_entry(mirror_root, section, host)
        st = stats_by_host.get(host, {})
        # Prefer data section path when host only exists under data/
        if not has_files and section == "site":
            has_data, data_url, data_dir = _domain_entry(mirror_root, "data", host)
            if has_data:
                section = "data"
                has_files, open_url, local_dir = True, data_url, data_dir
        # Skip ghost hosts with nothing on disk and nothing in the DB
        if not has_files and not st:
            continue
        item = {
            "host": host,
            "section": section,
            "source_url": f"https://{host}/",
            "has_mirror": has_files,
            "open_url": open_url,
            "browse_url": f"/files/{section}/{host}/" if has_files else None,
            "local_path": str(local_dir) if local_dir else None,
            "disk_bytes": dir_size_bytes(local_dir) if local_dir else 0,
            "total": st.get("total", 0),
            "done": st.get("done", 0),
            "pending": st.get("pending", 0),
            "errors": st.get("errors", 0),
            "magnets_total": int(magnets_total or 0) if host.startswith("torrents.") else None,
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