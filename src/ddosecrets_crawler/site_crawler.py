from __future__ import annotations

import logging
from collections import deque
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import AppConfig
from .db import StateDB
from .http_client import PoliteHttpClient
from .metadata import extract_article_metadata, store_article_metadata
from .html_rewrite import rewrite_html_links
from .urls import (
    absolutize,
    host_of,
    is_allowed_host,
    is_probably_html,
    mirror_local_path,
    normalize_url,
    sha256_bytes,
)

log = logging.getLogger(__name__)

SKIP_PREFIXES = (
    "/search",
    "/login",
    "/logout",
    "/submit",
)


class SiteCrawler:
    def __init__(self, cfg: AppConfig, db: StateDB, http: PoliteHttpClient):
        self.cfg = cfg
        self.db = db
        self.http = http
        self.stats = {
            "visited": 0,
            "saved": 0,
            "skipped": 0,
            "errors": 0,
            "articles": 0,
        }

    def _seed_urls(self) -> list[str]:
        seeds = []
        for s in self.cfg.seeds:
            host = host_of(s)
            if host in ("ddosecrets.org", "www.ddosecrets.org"):
                seeds.append(normalize_url(s))
        if not seeds:
            seeds = ["https://ddosecrets.org/"]
        # High-value index pages
        extras = [
            "https://ddosecrets.org/",
            "https://ddosecrets.org/about",
            "https://ddosecrets.org/all_categories",
            "https://ddosecrets.org/all_articles/recent",
            "https://ddosecrets.org/feed/rss.xml",
        ]
        for u in extras:
            if u not in seeds:
                seeds.append(u)
        return seeds

    def _should_follow(self, url: str) -> bool:
        if url.startswith("magnet:"):
            return False
        if not is_allowed_host(url, self.cfg.allowed_host_suffixes):
            return False
        host = host_of(url)
        # Site crawler focuses on the main CMS host; data/torrents handled elsewhere
        if host not in ("ddosecrets.org", "www.ddosecrets.org"):
            return False
        path = urlparse(url).path or "/"
        if any(path.startswith(p) for p in SKIP_PREFIXES):
            return False
        if path.startswith("/static/"):
            if self.cfg.site.follow_static:
                return True
            # Laptop-friendly: always pull CSS/JS/favicon so offline pages keep style
            lower = path.lower()
            return lower.endswith((".css", ".js", ".ico", ".png", ".svg", ".woff", ".woff2"))
        return True

    def _extract_links(self, base_url: str, html: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        out: list[str] = []
        for tag, attr in (("a", "href"), ("link", "href"), ("script", "src"), ("img", "src")):
            for el in soup.find_all(tag):
                href = el.get(attr)
                abs_url = absolutize(base_url, href) if href else None
                if not abs_url or abs_url.startswith("magnet:"):
                    continue
                if not self.cfg.site.respect_query_params:
                    abs_url = normalize_url(abs_url, keep_query=False)
                else:
                    abs_url = normalize_url(abs_url, keep_query=True)
                if self._should_follow(abs_url):
                    out.append(abs_url)
        return out

    @staticmethod
    def _is_article(url: str) -> bool:
        return "/article/" in urlparse(url).path

    def _push(self, queue: deque[str], url: str, *, front: bool = False) -> None:
        if front:
            queue.appendleft(url)
        else:
            queue.append(url)

    async def crawl(self) -> dict:
        queue: deque[str] = deque()
        seen: set[str] = set()

        # Priority for visible progress:
        # 1) pending articles  2) seeds  3) capped batch of other pending
        # Scheduler requeues stale done pages → pending for conditional revalidation.
        pending_articles: list[str] = []
        pending_other: list[str] = []
        for row in self.db.iter_resources(status="pending"):
            url = row["url"]
            if not self._should_follow(url):
                continue
            if self._is_article(url):
                pending_articles.append(url)
            else:
                pending_other.append(url)

        for url in pending_articles:
            self._push(queue, url)
        for url in self._seed_urls():
            self._push(queue, url)

        # max_pages acts as a batch size for continuous scheduler runs
        other_limit = self.cfg.site.max_pages or 100
        for url in pending_other[: max(other_limit, 20)]:
            self._push(queue, url)

        max_pages = self.cfg.site.max_pages
        log.info(
            "Crawl queue primed: pending_articles=%d pending_other_batch=%d max_pages=%s",
            len(pending_articles),
            min(len(pending_other), max(other_limit, 20)),
            max_pages or "unlimited",
        )

        while queue:
            if max_pages and self.stats["visited"] >= max_pages:
                log.info("Reached max_pages=%s", max_pages)
                break
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                await self._fetch_one(url, queue, seen)
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                log.exception("Site crawl failed for %s: %s", url, exc)
                self.db.mark_resource(url, "error", error=str(exc), kind="page")

        return self.stats

    async def _fetch_one(self, url: str, queue: deque[str], seen: set[str]) -> None:
        existing = self.db.get_resource(url)
        conditional: dict[str, str] = {}
        if existing and existing["etag"]:
            conditional["If-None-Match"] = existing["etag"]
        if existing and existing["last_modified"]:
            conditional["If-Modified-Since"] = existing["last_modified"]

        result = await self.http.get(url, headers=conditional or None)

        if result.status_code == 304:
            self.stats["skipped"] += 1
            # Keep status done and rotate revalidation window
            if existing:
                self.db.touch_resource(url, status="done", http_status=304)
            log.debug("Not modified: %s", url)
            return
        if result.status_code >= 400:
            self.stats["visited"] += 1
            self.stats["errors"] += 1
            self.db.mark_resource(
                url,
                "error",
                kind="page",
                http_status=result.status_code,
                error=f"HTTP {result.status_code}",
            )
            return

        local = mirror_local_path(self.cfg.mirror_path, url, section="site")
        content_hash = sha256_bytes(result.content)
        if (
            existing
            and existing["sha256"] == content_hash
            and local.exists()
        ):
            self.stats["skipped"] += 1
            self.db.touch_resource(
                url,
                status="done",
                http_status=result.status_code,
                sha256=content_hash,
            )
            return

        # Count only real work toward max_pages so skipped static does not stall progress
        self.stats["visited"] += 1

        kind = "page"
        path = urlparse(url).path
        html_text = result.text if is_probably_html(result.content_type, url) else None

        if self.cfg.site.save_html:
            local.parent.mkdir(parents=True, exist_ok=True)
            if html_text is not None:
                rewritten = rewrite_html_links(
                    html_text,
                    page_url=result.final_url or url,
                    allowed_host_suffixes=self.cfg.allowed_host_suffixes,
                )
                local.write_text(rewritten, encoding="utf-8")
            else:
                local.write_bytes(result.content)
            self.stats["saved"] += 1

        if path.startswith("/article/") and html_text is not None:
            kind = "article"
            meta = extract_article_metadata(url, html_text)
            store_article_metadata(self.db, meta)
            self.stats["articles"] += 1
            # Export per-article JSON sidecar
            sidecar = self.cfg.mirror_path / "metadata" / "articles" / f"{meta['slug']}.json"
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            import json

            sidecar.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif path.startswith("/static/"):
            kind = "static"
        elif path.endswith(".xml"):
            kind = "feed"

        self.db.upsert_resource(
            url=url,
            kind=kind,
            host=host_of(url),
            local_path=str(local),
            content_type=result.content_type,
            etag=result.etag,
            last_modified=result.last_modified,
            content_length=result.content_length,
            sha256=content_hash,
            status="done",
            http_status=result.status_code,
            error=None,
        )

        if html_text is not None:
            for link in self._extract_links(result.final_url, html_text):
                if link in seen:
                    continue
                existing = self.db.get_resource(link)
                # Never re-queue finished URLs here — revalidation marks them pending
                # on a schedule. Re-fetching done pages stalls progress (polite delays
                # with no pages_saved increase and no max_pages countdown).
                if existing is not None and existing["status"] in (
                    "done",
                    "deferred_torrent",
                    "error",
                ):
                    continue
                # Prefer crawling newly discovered articles / assets immediately
                if self._is_article(link) or urlparse(link).path.startswith("/static/"):
                    queue.appendleft(link)
                    kind_hint = "article" if self._is_article(link) else "static"
                else:
                    queue.append(link)
                    kind_hint = "page"
                if existing is None:
                    self.db.upsert_resource(
                        url=link,
                        kind=kind_hint,
                        host=host_of(link),
                        status="pending",
                    )