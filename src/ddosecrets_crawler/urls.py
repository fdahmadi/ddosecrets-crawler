from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urldefrag, urljoin, urlparse, urlunparse

MAGNET_INFOHASH_RE = re.compile(r"xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")


def normalize_url(url: str, *, keep_query: bool = False) -> str:
    url, _ = urldefrag(url.strip())
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        # Keep trailing slash for directory indexes on data server
        pass
    query = ""
    if keep_query and parsed.query:
        # Stable query order
        items = sorted(parse_qsl(parsed.query, keep_blank_values=True))
        query = "&".join(f"{k}={v}" for k, v in items)
    return urlunparse((scheme, netloc, path, "", query, ""))


def is_allowed_host(url: str, suffixes: list[str]) -> bool:
    host = urlparse(url).hostname or ""
    host = host.lower()
    for suffix in suffixes:
        suffix = suffix.lower().lstrip(".")
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def absolutize(base: str, href: str) -> str | None:
    if not href or href.startswith(("mailto:", "javascript:", "data:", "tel:")):
        return None
    if href.startswith("magnet:"):
        return href
    return urljoin(base, href)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_probably_html(content_type: str | None, url: str) -> bool:
    if content_type and "html" in content_type.lower():
        return True
    path = urlparse(url).path.lower()
    return path.endswith(("/", ".html", ".htm")) or "." not in Path(path).name


def is_directory_listing_url(url: str) -> bool:
    path = urlparse(url).path
    return path.endswith("/") or path == ""


def mirror_local_path(mirror_root: Path, url: str, *, section: str) -> Path:
    """Map URL to local filesystem path under mirror_root/section/host/..."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    # Decode %20 etc so local paths match what browsers request after URL-decoding.
    path = unquote(parsed.path or "/")
    if path.endswith("/"):
        rel = Path(host) / path.lstrip("/") / "index.html"
    else:
        rel = Path(host) / path.lstrip("/")
        if rel.suffix == "":
            rel = rel.with_name(rel.name + ".html") if section == "site" else rel
    # Prevent path traversal
    full = (mirror_root / section / rel).resolve()
    root = (mirror_root / section).resolve()
    if not str(full).startswith(str(root)):
        raise ValueError(f"Unsafe path for url={url}")
    return full


def data_file_local_path(mirror_root: Path, url: str) -> Path:
    parsed = urlparse(url)
    host = parsed.hostname or "data.ddosecrets.org"
    path = parsed.path.lstrip("/")
    full = (mirror_root / "data" / host / path).resolve()
    root = (mirror_root / "data").resolve()
    if not str(full).startswith(str(root)):
        raise ValueError(f"Unsafe path for url={url}")
    return full


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def magnet_infohash(magnet: str) -> str | None:
    m = MAGNET_INFOHASH_RE.search(magnet)
    if not m:
        return None
    value = m.group(1)
    if len(value) == 40:
        return value.lower()
    # Base32 infohash -> hex
    import base64

    raw = base64.b32decode(value.upper())
    return raw.hex()


def slug_from_article_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] or path


def parse_size_to_bytes(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KMGTPE]?i?B)?$", text, re.I)
    if not m:
        if text.isdigit():
            return int(text)
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    mult = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "PB": 1000**5,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
        "PIB": 1024**5,
    }.get(unit, 1)
    return int(num * mult)