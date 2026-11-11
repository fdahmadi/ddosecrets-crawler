from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .db import StateDB
from .urls import magnet_infohash, parse_size_to_bytes, slug_from_article_url

log = logging.getLogger(__name__)


def extract_article_metadata(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title_el = None
    for sel in ("main .content h1", "main h1", "article h1", ".content h1"):
        title_el = soup.select_one(sel)
        if title_el and title_el.get_text(strip=True):
            # Skip site brand heading in the header
            text = title_el.get_text(strip=True)
            if text.lower() != "distributed denial of secrets":
                break
            title_el = None
    title = title_el.get_text(strip=True) if title_el else slug_from_article_url(url)

    published = None
    for meta in soup.select(".content p.meta"):
        text = meta.get_text(" ", strip=True)
        m = re.search(r"Published on\s+(\d{4}-\d{2}-\d{2})", text, re.I)
        if m:
            published = m.group(1)
            break
    if not published:
        # Fallback: og/title area date patterns
        for meta in soup.select("main p.meta"):
            text = meta.get_text(" ", strip=True)
            if "Published on" in text:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
                if m:
                    published = m.group(1)
                    break

    description = None
    desc_meta = soup.find("meta", attrs={"name": "description"})
    if desc_meta and desc_meta.get("content"):
        description = desc_meta["content"].strip()

    # Prefer og:title "Name - Distributed Denial of Secrets"
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        og_title = og["content"].strip()
        if og_title.lower().endswith(" - distributed denial of secrets"):
            title = og_title[: -len(" - Distributed Denial of Secrets")].strip()

    countries: list[str] = []
    types: list[str] = []
    source = None
    download_size = None
    downloads: list[str] = []
    magnets: list[str] = []
    torrents: list[str] = []

    details = soup.select_one("div.metadata")
    if details:
        for p in details.find_all("p"):
            label = p.select_one("span.label")
            if not label:
                continue
            label_text = label.get_text(strip=True).lower().rstrip(":")
            if label_text == "countries":
                countries = [a.get_text(strip=True) for a in p.find_all("a")]
            elif label_text == "type":
                types = [a.get_text(strip=True) for a in p.find_all("a")]
            elif label_text == "source":
                a = p.find("a")
                source = a.get_text(strip=True) if a else p.get_text(" ", strip=True)
            elif label_text == "download size":
                raw = p.get_text(" ", strip=True)
                raw = re.sub(r"(?i)download size:\s*", "", raw)
                download_size = parse_size_to_bytes(raw)
            elif label_text == "download":
                for a in p.find_all("a", href=True):
                    downloads.append(a["href"])
            elif label_text == "magnet":
                for a in p.find_all("a", href=True):
                    if a["href"].startswith("magnet:"):
                        magnets.append(a["href"])
            elif label_text == "torrent":
                for a in p.find_all("a", href=True):
                    href = a["href"]
                    if href.endswith(".torrent") or "/torrent" in href.lower():
                        torrents.append(href)

    # Fallback: magnet/.torrent links inside the article content only
    content_root = soup.select_one("div.content") or soup.select_one("main") or soup
    if not magnets:
        for a in content_root.find_all("a", href=True):
            if a["href"].startswith("magnet:"):
                magnets.append(a["href"])
    if not torrents:
        for a in content_root.find_all("a", href=True):
            href = a["href"]
            if href.endswith(".torrent") and href.startswith("http"):
                torrents.append(href)

    return {
        "slug": slug_from_article_url(url),
        "title": title,
        "article_url": url,
        "published_at": published,
        "description": description,
        "countries": countries,
        "types": types,
        "source": source,
        "download_size_bytes": download_size,
        "downloads": downloads,
        "magnets": magnets,
        "torrents": torrents,
    }


def store_article_metadata(db: StateDB, meta: dict) -> None:
    db.upsert_release(
        slug=meta["slug"],
        title=meta.get("title"),
        article_url=meta.get("article_url"),
        published_at=meta.get("published_at"),
        description=meta.get("description"),
        countries=meta.get("countries") or [],
        types=meta.get("types") or [],
        source=meta.get("source"),
        download_size_bytes=meta.get("download_size_bytes"),
        magnets_json=meta.get("magnets") or [],
        downloads_json=meta.get("downloads") or [],
        torrents_json=meta.get("torrents") or [],
    )
    for magnet in meta.get("magnets") or []:
        ih = magnet_infohash(magnet)
        if ih:
            db.upsert_magnet(
                ih,
                magnet,
                title=meta.get("title"),
                source=meta.get("article_url"),
            )


def export_catalog_json(db: StateDB, path: Path) -> int:
    rows = db.conn.execute(
        "SELECT * FROM releases ORDER BY "
        "CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END, "
        "published_at DESC, title"
    ).fetchall()
    items = []
    for r in rows:
        items.append(
            {
                "slug": r["slug"],
                "title": r["title"],
                "article_url": r["article_url"],
                "published_at": r["published_at"],
                "description": r["description"],
                "countries": json.loads(r["countries"] or "[]"),
                "types": json.loads(r["types"] or "[]"),
                "source": r["source"],
                "download_size_bytes": r["download_size_bytes"],
                "magnets": json.loads(r["magnets_json"] or "[]"),
                "downloads": json.loads(r["downloads_json"] or "[]"),
                "torrents": json.loads(r["torrents_json"] or "[]"),
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def host_bucket(url: str) -> str:
    return (urlparse(url).hostname or "unknown").lower()