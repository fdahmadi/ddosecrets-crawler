from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..config import load_config_file, resolve_config_path
from ..html_rewrite import rewrite_html_for_mirror_response
from ..storage import build_storage, normalize_key

log = logging.getLogger(__name__)


def create_mirror_app(config_path: str | Path, hostname: str) -> FastAPI:
    """
    Serve one mirrored hostname at URL root on its own port.

    Example: data.ddosecrets.org on :8092
      GET /Foo/bar/  →  storage key data/data.ddosecrets.org/Foo/bar/index.html
    """
    _base, _local, active = resolve_config_path(config_path)
    cfg = load_config_file(active)
    host = (hostname or "").lower().strip()
    section = cfg.mirror_section_for(host)
    root_prefix = normalize_key(f"{section}/{host}")

    storage = build_storage(cfg)
    app = FastAPI(
        title=f"Mirror {host}",
        docs_url=None,
        redoc_url=None,
    )
    app.state.mirror_host = host
    app.state.mirror_section = section

    def _object_key_for_path(url_path: str) -> tuple[str, str]:
        """Return (preferred_file_key, dir_prefix)."""
        raw = unquote(url_path or "/")
        if not raw.startswith("/"):
            raw = "/" + raw
        rel = raw.lstrip("/")
        if not rel or rel.endswith("/"):
            prefix = normalize_key(f"{root_prefix}/{rel}") if rel else root_prefix
            return normalize_key(f"{prefix}/index.html"), prefix
        # site pages may be stored as .html without extension in URL
        key = normalize_key(f"{root_prefix}/{rel}")
        if section == "site" and "." not in Path(rel).name:
            html_key = normalize_key(f"{root_prefix}/{rel}.html")
            return html_key, normalize_key(f"{root_prefix}/{rel}")
        return key, normalize_key(f"{root_prefix}/{rel}")

    def _serve_file(key: str):
        if not storage.exists(key):
            return None
        ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
        if key.endswith(".html"):
            try:
                text = storage.get_bytes(key).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                stream = storage.open_stream(key)
                return StreamingResponse(stream, media_type=ctype)
            fixed = rewrite_html_for_mirror_response(
                text,
                host=host,
                section=section,
                admin_port=cfg.serve.admin_port,
                mirror_ports={
                    h: mp.port for h, mp in (cfg.serve.mirrors or {}).items()
                },
                public_base=cfg.serve.public_base or "",
            )
            return HTMLResponse(fixed, media_type="text/html; charset=utf-8")
        size = storage.size(key)
        stream = storage.open_stream(key)
        headers = {}
        if size is not None:
            headers["content-length"] = str(size)
        return StreamingResponse(stream, media_type=ctype, headers=headers)

    def _dir_listing_html(prefix: str, url_path: str) -> str:
        entries = storage.list_dir(prefix)
        items = []
        parent = "/".join([p for p in url_path.rstrip("/").split("/") if p][:-1])
        parent_href = f"/{parent}/" if parent else "/"
        if url_path not in ("", "/"):
            items.append(f'<li><a href="{parent_href}">../</a></li>')
        for child in entries:
            name = child.name + ("/" if child.is_dir else "")
            href = name if not url_path.endswith("/") else name
            # Build href relative to current path
            base = url_path if url_path.endswith("/") else url_path.rsplit("/", 1)[0] + "/"
            if not base.startswith("/"):
                base = "/" + base
            if url_path in ("", "/"):
                href = f"/{child.name}" + ("/" if child.is_dir else "")
            else:
                href = base + child.name + ("/" if child.is_dir else "")
            items.append(f'<li><a href="{href}">{name}</a></li>')
        body = "\n".join(items) or "<li><em>empty</em></li>"
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Index of {url_path}</title></head>
<body>
<h1>Index of {url_path}</h1>
<ul>
{body}
</ul>
<p><small>local mirror of https://{host}{url_path}</small></p>
</body></html>
"""

    @app.get("/healthz")
    async def healthz():
        return {
            "ok": True,
            "mirror_host": host,
            "section": section,
            "prefix": root_prefix,
            "storage": storage.describe(),
        }

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
    async def mirror_path(request: Request, full_path: str = ""):
        url_path = "/" + (full_path or "")
        if full_path.endswith("/") or full_path == "":
            url_path = "/" if not full_path else "/" + full_path
            if not url_path.endswith("/"):
                url_path += "/"

        file_key, dir_prefix = _object_key_for_path(url_path)

        # Exact file
        resp = _serve_file(file_key)
        if resp is not None:
            return resp

        # Try without forcing .html for site
        if section == "site" and not full_path.endswith("/"):
            alt = normalize_key(f"{root_prefix}/{full_path}")
            resp = _serve_file(alt)
            if resp is not None:
                return resp

        # Directory: prefer index.html, else listing
        index_key = normalize_key(f"{dir_prefix}/index.html")
        if storage.exists(index_key):
            # Enforce trailing slash for directories (like real nginx indexes)
            if full_path and not full_path.endswith("/"):
                return RedirectResponse(url=f"/{full_path}/", status_code=302)
            resp = _serve_file(index_key)
            if resp is not None:
                return resp

        entries = storage.list_dir(dir_prefix)
        if entries:
            if full_path and not full_path.endswith("/"):
                return RedirectResponse(url=f"/{full_path}/", status_code=302)
            html = _dir_listing_html(dir_prefix, url_path if url_path.endswith("/") else url_path + "/")
            return HTMLResponse(html)

        raise HTTPException(status_code=404, detail=f"Not found in mirror {host}: {url_path}")

    return app
