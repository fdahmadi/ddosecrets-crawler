from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .config import AppConfig
from .db import StateDB, url_needs_download
from .html_rewrite import rewrite_html_links
from .http_client import PoliteHttpClient
from .urls import (
    data_file_local_path,
    host_of,
    is_allowed_host,
    magnet_infohash,
    normalize_url,
    parse_size_to_bytes,
    sha256_bytes,
)

log = logging.getLogger(__name__)

SIZE_RE = re.compile(
    r"^([\d.]+)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)$", re.I
)


@dataclass
class ListingEntry:
    name: str
    href: str
    is_dir: bool
    size: int | None
    date: str | None
    article_url: str | None = None
    magnet: str | None = None


class DataMirror:
    """Recursively mirror nginx-style directory indexes on data.* hosts."""

    def __init__(self, cfg: AppConfig, db: StateDB, http: PoliteHttpClient):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.stats = {
            "indexes": 0,
            "files_downloaded": 0,
            "files_skipped": 0,
            "dirs_queued": 0,
            "torrents_queued": 0,
            "errors": 0,
        }

    def _data_seeds(self) -> list[str]:
        seeds = []
        for s in self.cfg.seeds:
            host = host_of(s)
            if host.startswith("data."):
                seeds.append(normalize_url(s if s.endswith("/") else s + "/"))
        if not seeds:
            seeds = ["https://data.ddosecrets.org/"]
        return seeds

    def parse_listing(self, base_url: str, html: str) -> list[ListingEntry]:
        soup = BeautifulSoup(html, "lxml")
        entries: list[ListingEntry] = []
        table = soup.find("table", id="list") or soup.find("table")
        if not table:
            return entries
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue
            link = tds[0].find("a", href=True)
            if not link:
                continue
            href = link["href"]
            name = unquote(link.get_text(strip=True) or href)
            if name in (".", "..") or href in ("../", "./") or "Parent directory" in name:
                continue
            abs_url = urljoin(base_url, href)
            if not is_allowed_host(abs_url, self.cfg.allowed_host_suffixes):
                continue
            is_dir = href.endswith("/") or name.endswith("/")
            size = None
            date = None
            article_url = None
            magnet = None

            # Root index has: File Name | Release Page | Magnet | Date
            # Nested indexes have: File Name | File Size | Date
            if len(tds) >= 4:
                art = tds[1].find("a", href=True)
                if art:
                    article_url = art["href"]
                mag = tds[2].find("a", href=True)
                if mag and mag["href"].startswith("magnet:"):
                    magnet = mag["href"]
                date = tds[3].get_text(strip=True)
                if date == "Unavailable":
                    date = None
            elif len(tds) >= 3:
                size_text = tds[1].get_text(strip=True)
                if size_text not in ("-", ""):
                    size = parse_size_to_bytes(size_text)
                date = tds[2].get_text(strip=True)

            entries.append(
                ListingEntry(
                    name=name.rstrip("/"),
                    href=normalize_url(abs_url if not is_dir else (abs_url if abs_url.endswith("/") else abs_url + "/")),
                    is_dir=is_dir,
                    size=size,
                    date=date,
                    article_url=article_url,
                    magnet=magnet,
                )
            )
        return entries

    async def mirror(self) -> dict:
        queue: deque[str] = deque(self._data_seeds())
        seen_dirs: set[str] = set()

        while queue:
            if (
                self.cfg.data.max_indexes
                and self.stats["indexes"] >= self.cfg.data.max_indexes
            ):
                log.info("Reached max_indexes=%s", self.cfg.data.max_indexes)
                break
            index_url = queue.popleft()
            if index_url in seen_dirs:
                continue
            seen_dirs.add(index_url)
            try:
                await self._process_index(index_url, queue)
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                log.exception("Data index failed %s: %s", index_url, exc)
                self.db.mark_resource(
                    index_url, "error", kind="data_index", error=str(exc)
                )
        return self.stats

    async def _process_index(self, index_url: str, queue: deque[str]) -> None:
        result = await self.http.get(index_url)
        self.stats["indexes"] += 1
        if result.status_code >= 400:
            self.stats["errors"] += 1
            self.db.mark_resource(
                index_url,
                "error",
                kind="data_index",
                http_status=result.status_code,
                error=f"HTTP {result.status_code}",
            )
            return

        # Save index HTML for offline browsing
        local_index = data_file_local_path(
            self.cfg.mirror_path,
            index_url if index_url.endswith("/") else index_url + "/",
        )
        # data_file_local_path for trailing slash ends without filename; fix:
        if index_url.endswith("/"):
            local_index = (
                self.cfg.mirror_path
                / "data"
                / host_of(index_url)
                / urlparse(index_url).path.lstrip("/")
                / "index.html"
            )
        local_index.parent.mkdir(parents=True, exist_ok=True)
        rewritten = rewrite_html_links(
            result.text,
            page_url=index_url if index_url.endswith("/") else index_url + "/",
            allowed_host_suffixes=self.cfg.allowed_host_suffixes,
        )
        local_index.write_text(rewritten, encoding="utf-8")
        self.db.upsert_resource(
            url=index_url,
            kind="data_index",
            host=host_of(index_url),
            local_path=str(local_index),
            content_type=result.content_type,
            etag=result.etag,
            last_modified=result.last_modified,
            content_length=result.content_length,
            status="done",
            http_status=result.status_code,
        )
        # Logo / static assets in the index chrome (not listed as downloadable files)
        await self._sync_page_assets(
            index_url if index_url.endswith("/") else index_url + "/",
            result.text,
        )

        entries = self.parse_listing(index_url, result.text)
        for entry in entries:
            if entry.magnet:
                ih = magnet_infohash(entry.magnet)
                if ih:
                    changed = self.db.upsert_magnet(
                        ih,
                        entry.magnet,
                        title=entry.name,
                        source=index_url,
                    )
                    if changed:
                        self.stats["torrents_queued"] += 1

            if entry.is_dir:
                if self.cfg.data.recurse_directories:
                    queue.append(entry.href)
                    self.stats["dirs_queued"] += 1
                continue

            try:
                await self._maybe_download_file(entry)
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                log.warning("File handling failed for %s: %s", entry.href, exc)
                self.db.mark_resource(
                    entry.href, "error", kind="data_file", error=str(exc)
                )

    async def _sync_page_assets(self, page_url: str, html: str) -> None:
        """Download small page chrome assets (logo/CSS/icons) referenced by an index."""
        host = host_of(page_url)
        asset_urls: list[str] = []
        soup = BeautifulSoup(html, "lxml")
        for tag, attr in (("link", "href"), ("script", "src"), ("img", "src")):
            for el in soup.find_all(tag):
                href = el.get(attr)
                if not href or href.startswith(("magnet:", "data:", "#", "mailto:")):
                    continue
                abs_u = urljoin(page_url, href)
                if host_of(abs_u) != host:
                    continue
                path = urlparse(abs_u).path.lower()
                if path.endswith(
                    (
                        ".css",
                        ".js",
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".gif",
                        ".svg",
                        ".ico",
                        ".webp",
                        ".woff",
                        ".woff2",
                    )
                ):
                    asset_urls.append(abs_u)

        # Known header logo on data.ddosecrets.org
        if host.startswith("data."):
            asset_urls.append(urljoin(page_url, "index/ddosecrets-small.png"))

        seen: set[str] = set()
        for asset_url in asset_urls:
            if asset_url in seen:
                continue
            seen.add(asset_url)
            rel = unquote(urlparse(asset_url).path).lstrip("/")
            if not rel:
                continue
            # Mirror under data/<host>/... so relative img src stays valid
            host_root = self.cfg.mirror_path / "data" / host
            dest = host_root / rel
            if dest.exists() and dest.stat().st_size > 0:
                continue
            try:
                res = await self.http.get(asset_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("data asset fetch failed %s: %s", asset_url, exc)
                continue
            if res.status_code >= 400:
                log.warning("data asset HTTP %s for %s", res.status_code, asset_url)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(res.content)
            self.db.upsert_resource(
                url=asset_url,
                kind="static",
                host=host,
                local_path=str(dest),
                content_type=res.content_type,
                status="done",
                http_status=res.status_code,
                sha256=sha256_bytes(res.content),
            )
            log.info("Saved data page asset → %s", dest)

    async def _maybe_download_file(self, entry: ListingEntry) -> None:
        url = entry.href
        ext = "." + url.rsplit(".", 1)[-1].lower() if "." in urlparse(url).path.split("/")[-1] else ""
        if ext and ext in {e.lower() for e in self.cfg.data.skip_extensions}:
            self.stats["files_skipped"] += 1
            return

        # Prefer torrent path: if magnet known and prefer_torrent, skip HTTP bulk files
        # Still download small .torrent metadata files over HTTP.
        is_torrent_file = url.lower().endswith(".torrent")
        if (
            self.cfg.data.prefer_torrent
            and entry.magnet
            and not is_torrent_file
            and not self.cfg.data.http_fallback
        ):
            self.db.upsert_resource(
                url=url,
                kind="data_file",
                host=host_of(url),
                status="deferred_torrent",
                content_length=entry.size,
                extra_json=None,
            )
            self.stats["files_skipped"] += 1
            return

        if self.cfg.data.prefer_torrent and entry.magnet and not is_torrent_file:
            # Defer large payloads to torrent engine; still record discovery
            if entry.size and entry.size > 50 * 1024 * 1024:
                self.db.upsert_resource(
                    url=url,
                    kind="data_file",
                    host=host_of(url),
                    status="deferred_torrent",
                    content_length=entry.size,
                )
                self.stats["files_skipped"] += 1
                log.info("Deferring large file to torrent: %s (%s bytes)", url, entry.size)
                return

        if self.cfg.data.max_file_bytes and entry.size and entry.size > self.cfg.data.max_file_bytes:
            log.info(
                "Skipping %s: size %s > max_file_bytes %s",
                url,
                entry.size,
                self.cfg.data.max_file_bytes,
            )
            self.stats["files_skipped"] += 1
            return

        local = data_file_local_path(self.cfg.mirror_path, url)
        existing = self.db.get_resource(url)

        # Cheap HEAD/GET probe for validators when size unknown
        etag = None
        last_modified = None
        content_length = entry.size
        try:
            head = await self.http.head(url)
            etag = head.etag
            last_modified = head.last_modified
            if head.content_length:
                content_length = head.content_length
        except Exception as exc:  # noqa: BLE001
            log.debug("Probe failed for %s: %s", url, exc)

        if self.cfg.data.max_file_bytes and content_length and content_length > self.cfg.data.max_file_bytes:
            self.stats["files_skipped"] += 1
            return

        if not url_needs_download(
            existing,
            etag=etag,
            last_modified=last_modified,
            content_length=content_length,
            local_path=local,
        ):
            self.stats["files_skipped"] += 1
            return

        try:
            meta = await self.http.download_to(
                url, local, expected_size=content_length, resume=True
            )
            self.db.upsert_resource(
                url=url,
                kind="data_file",
                host=host_of(url),
                local_path=str(local),
                content_type=meta.get("content_type"),
                etag=meta.get("etag") or etag,
                last_modified=meta.get("last_modified") or last_modified,
                content_length=meta.get("content_length") or content_length,
                status="done",
                http_status=meta.get("status_code"),
                error=None,
            )
            self.stats["files_downloaded"] += 1
            log.info("Downloaded %s -> %s (%s bytes)", url, local, meta.get("bytes"))
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.db.mark_resource(url, "error", kind="data_file", error=str(exc))
            log.exception("Download failed %s: %s", url, exc)