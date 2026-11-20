from __future__ import annotations

from pathlib import Path
from typing import Mapping
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


def remote_url_to_mirror_href(
    url: str,
    *,
    page_host: str,
    allowed_host_suffixes: list[str],
    mirror_ports: Mapping[str, int] | None = None,
    public_base: str = "",
) -> str | None:
    """
    Map a remote URL so browsing stays inside the local mirror.

    - Same host → keep original path only (/foo/bar) for dedicated mirror ports
    - Other mirrored hosts → http(s)://{public_base}:{port}/path when configured
    - Otherwise None (leave link unchanged)
    """
    if not url or url.startswith(("magnet:", "mailto:", "javascript:", "data:", "tel:", "#")):
        return None
    if not is_allowed_host(url, allowed_host_suffixes):
        return None

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    frag = f"#{parsed.fragment}" if parsed.fragment else ""
    page_host = (page_host or "").lower()

    if host == page_host or host.endswith("." + page_host) or page_host.endswith("." + host):
        return f"{path}{query}{frag}"

    ports = {k.lower(): int(v) for k, v in (mirror_ports or {}).items()}
    port = ports.get(host)
    if port is None:
        for pattern, p in ports.items():
            if host == pattern or host.endswith("." + pattern):
                port = p
                break
    if port is None or not public_base:
        # Stay on this mirror's host by not sending the browser to the live site.
        # Drop cross-host navigation to path-only only when same site family is unknown.
        return None

    base = public_base.rstrip("/")
    return f"{base}:{port}{path}{query}{frag}"


def rewrite_html_links(
    html: str,
    *,
    page_url: str,
    allowed_host_suffixes: list[str],
    mode: str = "admin",
    mirror_ports: Mapping[str, int] | None = None,
    public_base: str = "",
) -> str:
    """
    Rewrite links in saved HTML.

    mode=admin  → map allowed hosts onto /files/... (admin file browser)
    mode=mirror → keep same-host paths so a dedicated mirror port looks like
                  the original site (fixes "leaving the mirror")
    """
    soup = BeautifulSoup(html, "lxml")
    page_host = host_of(page_url)
    attrs = (
        ("a", "href"),
        ("link", "href"),
        ("script", "src"),
        ("img", "src"),
        ("source", "src"),
        ("iframe", "src"),
        ("form", "action"),
    )
    use_mirror = (mode or "admin").strip().lower() == "mirror"

    for tag_name, attr in attrs:
        for el in soup.find_all(tag_name):
            raw = el.get(attr)
            if not raw or raw.startswith(("magnet:", "mailto:", "javascript:", "data:", "tel:", "#")):
                continue
            if raw.startswith("?"):
                continue
            if raw.startswith("/files/"):
                if use_mirror and page_host:
                    # Old admin rewrites → restore original path under this host
                    prefix = f"/files/{section_for_host(page_host)}/{page_host}"
                    if raw.startswith(prefix + "/") or raw == prefix:
                        rest = raw[len(prefix) :] or "/"
                        el[attr] = rest
                        continue
                continue
            # Keep relative same-folder links as-is (they already work on mirror ports)
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
            if urlparse(abs_url).path.startswith("/files/"):
                continue

            if use_mirror:
                # Root-relative on same site: leave unchanged (works on mirror port)
                if raw.startswith("/") and not raw.startswith("//"):
                    continue
                mapped = remote_url_to_mirror_href(
                    normalize_url(abs_url, keep_query=True),
                    page_host=page_host,
                    allowed_host_suffixes=allowed_host_suffixes,
                    mirror_ports=mirror_ports,
                    public_base=public_base,
                )
            else:
                mapped = remote_url_to_files_href(
                    normalize_url(abs_url, keep_query=True),
                    allowed_host_suffixes=allowed_host_suffixes,
                )
            if mapped:
                frag = urlparse(abs_url).fragment
                if frag and "#" not in mapped:
                    el[attr] = f"{mapped}#{frag}"
                else:
                    el[attr] = mapped

    for base in soup.find_all("base"):
        base.decompose()

    return str(soup)


def rewrite_html_for_mirror_response(
    html: str,
    *,
    host: str,
    section: str,
    admin_port: int = 8090,
    mirror_ports: Mapping[str, int] | None = None,
    public_base: str = "",
) -> str:
    """
    Fix links when serving on a dedicated mirror port so the browser stays
    inside the mirror (same-host paths) instead of jumping to live site or
    broken /files/... paths on the wrong port.
    """
    prefix = f"/files/{section}/{host}"
    ports = {k.lower(): int(v) for k, v in (mirror_ports or {}).items()}
    base = (public_base or "").rstrip("/") or "http://127.0.0.1"
    admin_origin = f"{base}:{int(admin_port)}"

    def _port_for(h: str) -> int | None:
        h = h.lower()
        if h in ports:
            return ports[h]
        for pattern, p in ports.items():
            if h == pattern or h.endswith("." + pattern):
                return p
        return None

    soup = BeautifulSoup(html, "lxml")
    for tag_name, attr in (
        ("a", "href"),
        ("link", "href"),
        ("script", "src"),
        ("img", "src"),
        ("source", "src"),
        ("iframe", "src"),
        ("form", "action"),
    ):
        for el in soup.find_all(tag_name):
            raw = el.get(attr)
            if not raw:
                continue
            # This host's admin-rewritten paths → original path on this port
            if raw.startswith(prefix + "/") or raw == prefix:
                el[attr] = raw[len(prefix) :] or "/"
                continue
            # Other /files/... links (e.g. release pages) → admin UI or that host's port
            if raw.startswith("/files/"):
                parts = [p for p in raw.split("/") if p]
                # files / section / hostname / ...
                if len(parts) >= 3:
                    other_host = parts[2]
                    other_port = _port_for(other_host)
                    rest = "/" + "/".join(parts[3:]) if len(parts) > 3 else "/"
                    if other_port:
                        el[attr] = f"{base}:{other_port}{rest}"
                    else:
                        el[attr] = f"{admin_origin}{raw}"
                else:
                    el[attr] = f"{admin_origin}{raw}"
                continue
            # Absolute live URLs for this host → path only (stay on mirror port)
            if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("//"):
                parsed = urlparse(raw if "://" in raw else "https:" + raw)
                remote_host = (parsed.hostname or "").lower()
                path = parsed.path or "/"
                q = f"?{parsed.query}" if parsed.query else ""
                f = f"#{parsed.fragment}" if parsed.fragment else ""
                if remote_host == host.lower():
                    el[attr] = f"{path}{q}{f}"
                    continue
                other_port = _port_for(remote_host)
                if other_port:
                    el[attr] = f"{base}:{other_port}{path}{q}{f}"
                    continue
                # Known ddosecrets hosts without a mirror port → admin files browser
                if remote_host.endswith("ddosecrets.org"):
                    sec = section_for_host(remote_host)
                    if path.endswith("/") or path == "":
                        rel = f"{remote_host}/{path.lstrip('/')}index.html"
                    else:
                        body = path.lstrip("/")
                        name = Path(body).name
                        if sec == "site" and name and "." not in name:
                            rel = f"{remote_host}/{body}.html"
                        else:
                            rel = f"{remote_host}/{body}"
                    el[attr] = f"{admin_origin}/files/{sec}/{rel}{q}{f}"
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
            mode="mirror" if _section == "data" else "admin",
        )
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            changed += 1
    return changed
