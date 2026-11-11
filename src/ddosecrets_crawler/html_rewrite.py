from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .urls import absolutize, host_of, is_allowed_host, normalize_url


def section_for_host(host: str) -> str:
    h = (host or "").lower()
    if h.startswith("data."):
        return "data"
    return "site"


def remote_url_to_files_href(url: str, *, allowed_host_suffixes: list[str]) -> str | None:
    """Map an absolute remote URL on an allowed host to a /files/... UI path."""
    if not url or url.startswith(("magnet:", "mailto:", "javascript:", "data:", "tel:", "#")):
        return None
    if not is_allowed_host(url, allowed_host_suffixes):
        return None

    host = host_of(url)
    section = section_for_host(host)
    path = urlparse(url).path or "/"

    if path.endswith("/") or path == "":
        rel = f"{host}/{path.lstrip('/')}index.html"
    else:
        body = path.lstrip("/")
        name = Path(body).name
        if section == "site" and name and "." not in name:
            rel = f"{host}/{body}.html"
        else:
            rel = f"{host}/{body}"
    return f"/files/{section}/{rel}"


def rewrite_html_links(
    html: str,
    *,
    page_url: str,
    allowed_host_suffixes: list[str],
) -> str:
    """
    Rewrite absolute/root-relative links for allowed hosts to local /files/... paths.

    Relative same-directory links (e.g. data listings) are left untouched.
    Magnets and third-party URLs stay as-is.
    """
    soup = BeautifulSoup(html, "lxml")
    attrs = (
        ("a", "href"),
        ("link", "href"),
        ("script", "src"),
        ("img", "src"),
        ("source", "src"),
        ("iframe", "src"),
        ("form", "action"),
    )
    for tag_name, attr in attrs:
        for el in soup.find_all(tag_name):
            raw = el.get(attr)
            if not raw or raw.startswith(("magnet:", "mailto:", "javascript:", "data:", "tel:", "#")):
                continue
            if raw.startswith("?"):
                continue
            # Already rewritten for the local UI
            if raw.startswith("/files/"):
                continue
            # Keep relative same-folder links (data listings, local images) as-is.
            # Only rewrite absolute or root-relative URLs onto /files/...
            if not (
                raw.startswith("http://")
                or raw.startswith("https://")
                or raw.startswith("//")
                or raw.startswith("/")
            ):
                continue
            abs_url = absolutize(page_url, raw)
            if not abs_url or abs_url.startswith("magnet:"):
                continue
            # Guard against absolutizing an already-local /files/... path
            if urlparse(abs_url).path.startswith("/files/"):
                continue
            mapped = remote_url_to_files_href(
                normalize_url(abs_url, keep_query=True),
                allowed_host_suffixes=allowed_host_suffixes,
            )
            if mapped:
                frag = urlparse(abs_url).fragment
                el[attr] = f"{mapped}#{frag}" if frag else mapped

    for base in soup.find_all("base"):
        base.decompose()

    return str(soup)


def rewrite_mirror_tree(
    mirror_root: Path,
    *,
    allowed_host_suffixes: list[str],
) -> int:
    """Rewrite links in all saved HTML files under the mirror root. Returns count."""
    changed = 0
    for path in mirror_root.rglob("*.html"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = path.relative_to(mirror_root)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2 or parts[0] not in ("site", "data"):
            continue
        _section, host, *rest = parts
        if not rest:
            continue
        if rest[-1] == "index.html":
            url_path = "/" + "/".join(rest[:-1])
            if not url_path.endswith("/"):
                url_path += "/"
            if url_path == "//":
                url_path = "/"
        else:
            name = rest[-1]
            if name.endswith(".html"):
                name = name[: -len(".html")]
            url_path = "/" + "/".join([*rest[:-1], name])
        page_url = f"https://{host}{url_path}"
        rewritten = rewrite_html_links(
            text,
            page_url=page_url,
            allowed_host_suffixes=allowed_host_suffixes,
        )
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            changed += 1
    return changed
