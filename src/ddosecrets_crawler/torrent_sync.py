from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree as ET

import feedparser
from bs4 import BeautifulSoup

from .config import AppConfig
from .db import StateDB
from .html_rewrite import rewrite_html_links
from .http_client import PoliteHttpClient
from .storage import MirrorStorage, mirror_object_key, normalize_key
from .urls import host_of, magnet_infohash, sha256_bytes

log = logging.getLogger(__name__)
CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)


class TorrentSync:
    def __init__(
        self,
        cfg: AppConfig,
        db: StateDB,
        http: PoliteHttpClient,
        storage: MirrorStorage,
    ):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.storage = storage
        self.stats = {
            "csv_rows": 0,
            "new_magnets": 0,
            "rss_items": 0,
            "started": 0,
            "errors": 0,
        }

    async def sync_indexes(self) -> dict:
        await self._sync_torrents_home()
        await self._sync_magnets_csv()
        await self._sync_releases_rss()
        await self._sync_site_rss()
        self._copy_torrent_index_sidecars()
        return self.stats

    def _copy_torrent_index_sidecars(self) -> None:
        """Expose magnets.csv / releases.xml next to the torrents homepage."""
        home_key = "site/torrents.ddosecrets.org/index.html"
        if not self.storage.exists(home_key):
            return
        for name in ("magnets.csv", "releases.xml"):
            src_key = f"metadata/{name}"
            if self.storage.exists(src_key):
                dest_key = f"site/torrents.ddosecrets.org/{name}"
                self.storage.put_bytes(dest_key, self.storage.get_bytes(src_key))

    async def _sync_torrents_home(self) -> None:
        """Save torrents subdomain homepage so Domains can open a local mirror."""
        url = "https://torrents.ddosecrets.org/"
        try:
            result = await self.http.get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("torrents home fetch failed: %s", exc)
            return
        if result.status_code >= 400:
            log.warning("torrents home HTTP %s", result.status_code)
            return
        key = mirror_object_key(url, section="site")
        host = host_of(url)
        rewritten = rewrite_html_links(
            result.text,
            page_url=url,
            allowed_host_suffixes=self.cfg.allowed_host_suffixes,
            mode=(
                "mirror" if self.cfg.mirror_port_for(host) is not None else "admin"
            ),
            mirror_ports={
                h: mp.port for h, mp in (self.cfg.serve.mirrors or {}).items()
            },
            public_base=self.cfg.serve.public_base or "",
        )
        self.storage.put_text(key, rewritten, content_type="text/html; charset=utf-8")
        self.db.upsert_resource(
            url=url,
            kind="page",
            host=host_of(url),
            local_path=key,
            content_type=result.content_type,
            etag=result.etag,
            last_modified=result.last_modified,
            content_length=result.content_length,
            status="done",
            http_status=result.status_code,
            error=None,
        )
        log.info("Saved torrents homepage → %s", key)
        await self._sync_torrents_page_assets(url, result.text)

    async def _sync_torrents_page_assets(self, page_url: str, html: str) -> None:
        """Download CSS/JS/images referenced by the torrents homepage (relative paths)."""
        soup = BeautifulSoup(html, "lxml")
        asset_urls: list[str] = []
        for tag, attr in (("link", "href"), ("script", "src"), ("img", "src")):
            for el in soup.find_all(tag):
                href = el.get(attr)
                if not href or href.startswith(("magnet:", "data:", "#", "mailto:")):
                    continue
                abs_u = urljoin(page_url, href)
                if host_of(abs_u) != "torrents.ddosecrets.org":
                    continue
                path = urlparse(abs_u).path.lower()
                if (
                    path.endswith(
                        (
                            ".css",
                            ".js",
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".gif",
                            ".svg",
                            ".ico",
                            ".woff",
                            ".woff2",
                        )
                    )
                    or "/css/" in path
                    or "/img/" in path
                ):
                    asset_urls.append(abs_u)

        # Common files linked relatively from the page body
        for name in ("css/platinum.css", "img/ddosecrets_small.png", "img/about.png"):
            asset_urls.append(urljoin(page_url, name))

        seen: set[str] = set()
        for asset_url in asset_urls:
            if asset_url in seen:
                continue
            seen.add(asset_url)
            rel = unquote(urlparse(asset_url).path).lstrip("/")
            if not rel:
                continue
            key = normalize_key(f"site/torrents.ddosecrets.org/{rel}")
            try:
                res = await self.http.get(asset_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("torrents asset fetch failed %s: %s", asset_url, exc)
                continue
            if res.status_code >= 400:
                log.warning("torrents asset HTTP %s for %s", res.status_code, asset_url)
                continue
            self.storage.put_bytes(key, res.content, content_type=res.content_type)
            self.db.upsert_resource(
                url=asset_url,
                kind="static",
                host=host_of(asset_url),
                local_path=key,
                content_type=res.content_type,
                status="done",
                http_status=res.status_code,
                sha256=sha256_bytes(res.content),
            )
            log.info("Saved torrents asset → %s", key)

            # Pull url(...) references from CSS
            if rel.lower().endswith(".css"):
                text = res.content.decode("utf-8", errors="replace")
                for match in CSS_URL_RE.findall(text):
                    ref = match.strip()
                    if not ref or ref.startswith(("data:", "#")):
                        continue
                    nested = urljoin(asset_url, ref)
                    if host_of(nested) != "torrents.ddosecrets.org":
                        continue
                    if nested not in seen:
                        asset_urls.append(nested)

    async def _sync_magnets_csv(self) -> None:
        url = self.cfg.torrent.magnets_csv
        log.info("Fetching magnets CSV: %s", url)
        result = await self.http.get(url)
        if result.status_code >= 400:
            self.stats["errors"] += 1
            log.error("magnets.csv HTTP %s", result.status_code)
            return
        text = result.text
        self.storage.put_text(
            "metadata/magnets.csv", text, content_type="text/csv; charset=utf-8"
        )

        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                continue
            # Formats observed: title,date,magnet  OR title,magnet
            magnet = None
            title = row[0].strip() if row else None
            for cell in row:
                cell = cell.strip()
                if cell.startswith("magnet:"):
                    magnet = cell
                    break
            if not magnet:
                continue
            self.stats["csv_rows"] += 1
            ih = magnet_infohash(magnet)
            if not ih:
                continue
            if self.db.upsert_magnet(ih, magnet, title=title, source=url):
                self.stats["new_magnets"] += 1

    async def _sync_releases_rss(self) -> None:
        url = self.cfg.torrent.releases_rss
        try:
            result = await self.http.get(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("releases RSS fetch failed: %s", exc)
            return
        if result.status_code >= 400:
            log.warning("releases RSS HTTP %s", result.status_code)
            return
        self.storage.put_bytes(
            "metadata/releases.xml",
            result.content,
            content_type=result.content_type or "application/xml",
        )
        self._ingest_rss_bytes(result.content, source=url)

    async def _sync_site_rss(self) -> None:
        url = self.cfg.torrent.site_rss
        result = await self.http.get(url)
        if result.status_code >= 400:
            log.warning("site RSS HTTP %s", result.status_code)
            return
        self.storage.put_bytes(
            "metadata/site_rss.xml",
            result.content,
            content_type=result.content_type or "application/xml",
        )
        self._ingest_site_rss(result.text, source=url)

    def _ingest_rss_bytes(self, content: bytes, *, source: str) -> None:
        parsed = feedparser.parse(content)
        for entry in parsed.entries:
            self.stats["rss_items"] += 1
            title = entry.get("title")
            link = entry.get("link")
            if link and link.startswith("magnet:"):
                ih = magnet_infohash(link)
                if ih and self.db.upsert_magnet(ih, link, title=title, source=source):
                    self.stats["new_magnets"] += 1
            # Also scan description / links
            for l in entry.get("links", []) or []:
                href = l.get("href") or ""
                if href.startswith("magnet:"):
                    ih = magnet_infohash(href)
                    if ih and self.db.upsert_magnet(
                        ih, href, title=title, source=source
                    ):
                        self.stats["new_magnets"] += 1

    def _ingest_site_rss(self, text: str, *, source: str) -> None:
        # Custom ddos:link elements
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            self._ingest_rss_bytes(text.encode("utf-8"), source=source)
            return
        ns = {"ddos": "https://ddosecrets.org/ns/links"}
        for item in root.findall("./channel/item"):
            self.stats["rss_items"] += 1
            title = (item.findtext("title") or "").strip()
            for link in item.findall("ddos:link", ns):
                href = (link.text or "").strip()
                ltype = link.attrib.get("type")
                if ltype == "magnet" and href.startswith("magnet:"):
                    ih = magnet_infohash(href)
                    if ih and self.db.upsert_magnet(
                        ih, href, title=title, source=source
                    ):
                        self.stats["new_magnets"] += 1

    async def start_downloads(self) -> dict:
        if not self.cfg.torrent.enabled:
            return self.stats
        download_dir = Path(self.cfg.torrent.download_dir).resolve()
        download_dir.mkdir(parents=True, exist_ok=True)

        if self.cfg.torrent.aria2_rpc:
            await self._start_via_rpc(download_dir)
        elif self.cfg.torrent.use_cli:
            await self._start_via_cli(download_dir)
        else:
            log.warning("Torrent downloads enabled but no aria2 RPC/CLI configured")
        return self.stats

    async def _start_via_cli(self, download_dir: Path) -> None:
        if not shutil.which("aria2c"):
            log.error("aria2c not found in PATH; install aria2")
            self.stats["errors"] += 1
            return
        limit = self.cfg.torrent.max_concurrent_torrents
        queued = self.db.queued_magnets(limit=limit)
        for row in queued:
            magnet = row["magnet"]
            infohash = row["infohash"]
            title = row["title"] or infohash
            out_subdir = download_dir / infohash
            out_subdir.mkdir(parents=True, exist_ok=True)
            cmd = [
                "aria2c",
                "--dir",
                str(out_subdir),
                "--seed-ratio",
                str(self.cfg.torrent.seed_ratio),
                "--seed-time",
                str(self.cfg.torrent.seed_time_minutes),
                "--bt-enable-lpd=true",
                "--enable-dht=true",
                "--continue=true",
                "--max-connection-per-server=4",
                "--summary-interval=30",
                magnet,
            ]
            log.info("Starting aria2c for %s (%s)", title, infohash)
            try:
                # Non-blocking start; user/systemd can supervise long downloads
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self.db.update_magnet(
                    infohash,
                    status="downloading",
                    aria2_gid=str(proc.pid),
                    local_path=str(out_subdir),
                    error=None,
                )
                self.stats["started"] += 1
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.db.update_magnet(infohash, status="error", error=str(exc))
                log.exception("Failed to start torrent %s: %s", infohash, exc)

    async def _start_via_rpc(self, download_dir: Path) -> None:
        import httpx

        rpc = self.cfg.torrent.aria2_rpc
        secret = self.cfg.torrent.aria2_secret
        queued = self.db.queued_magnets(limit=self.cfg.torrent.max_concurrent_torrents)
        async with httpx.AsyncClient(timeout=30.0) as client:
            for row in queued:
                magnet = row["magnet"]
                infohash = row["infohash"]
                params = [[magnet], {"dir": str(download_dir / infohash)}]
                payload = {
                    "jsonrpc": "2.0",
                    "id": infohash,
                    "method": "aria2.addUri",
                    "params": ([f"token:{secret}"] + params) if secret else params,
                }
                try:
                    resp = await client.post(rpc, json=payload)
                    data = resp.json()
                    gid = data.get("result")
                    self.db.update_magnet(
                        infohash,
                        status="downloading",
                        aria2_gid=gid,
                        local_path=str(download_dir / infohash),
                        error=None,
                    )
                    self.stats["started"] += 1
                except Exception as exc:  # noqa: BLE001
                    self.stats["errors"] += 1
                    self.db.update_magnet(infohash, status="error", error=str(exc))
                    log.exception("aria2 RPC add failed for %s: %s", infohash, exc)

    def export_magnet_queue(self, key: str = "metadata/magnet_queue.json") -> int:
        rows = self.db.conn.execute(
            "SELECT infohash, title, magnet, status FROM magnets ORDER BY title"
        ).fetchall()
        items = [dict(r) for r in rows]
        self.storage.put_text(
            key,
            json.dumps(items, indent=2),
            content_type="application/json; charset=utf-8",
        )
        return len(items)
