from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import yaml

from ..config import (
    delete_local_config,
    load_config_file,
    read_config_text,
    resolve_config_path,
    write_local_config,
)
from ..db import StateDB
from ..storage import build_storage, normalize_key
from .helpers import (
    article_exists,
    duration_seconds,
    format_ts,
    human_size,
    list_mirrored_domains,
    local_article_url,
    release_to_dict,
    storage_size_bytes,
)
from .settings_form import (
    FORM_GROUPS,
    config_dict_from_form,
    dump_config_yaml,
    form_values_from_config,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(config_path: str | Path) -> FastAPI:
    base_path, local_path, active_path = resolve_config_path(config_path)
    cfg = load_config_file(active_path)

    app = FastAPI(title="DDoSecrets Mirror Browser", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["humansize"] = human_size
    templates.env.filters["ts"] = format_ts
    templates.env.filters["duration"] = duration_seconds

    storage = build_storage(cfg)
    app.state.config_base = str(base_path)
    app.state.config_local = str(local_path)
    app.state.config_active = str(active_path)
    app.state.using_local = local_path.exists()
    app.state.storage_desc = storage.describe()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def db() -> StateDB:
        return StateDB.from_config(cfg)

    def _list_dir_entries(rel: str) -> list[dict]:
        entries: list[dict] = []
        for child in storage.list_dir(rel):
            href = f"/files/{child.key}" if child.is_dir else f"/files/{child.key}"
            if child.is_dir and not href.endswith("/"):
                href += "/"
            entries.append(
                {
                    "name": child.name,
                    "href": href,
                    "is_dir": child.is_dir,
                    "size": child.size,
                    "size_human": (
                        human_size(child.size) if child.size is not None else "—"
                    ),
                }
            )
        return entries

    def config_context(extra: dict | None = None) -> dict:
        base, local, active = resolve_config_path(base_path)
        source = active if active.exists() else base
        text = read_config_text(source)
        if not text and base.exists():
            text = read_config_text(base)
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            raw = {}
        ctx = {
            "base_path": str(base),
            "local_path": str(local),
            "active_path": str(active),
            "using_local": local.exists(),
            "config_text": text,
            "form_groups": FORM_GROUPS,
            "form_values": form_values_from_config(raw),
            "tab": "form",
            "message": None,
            "error": None,
        }
        if extra:
            ctx.update(extra)
        return ctx

    def build_dashboard() -> dict:
        database = db()
        try:
            stats = database.crawl_stats()
        finally:
            database.close()
        stats["disk_bytes"] = storage_size_bytes(storage)
        stats["disk_human"] = human_size(stats["disk_bytes"])
        stats["bytes_done_human"] = human_size(stats.get("bytes_done"))
        stats["file_bytes_done_human"] = human_size(stats.get("file_bytes_done"))
        stats["page_bytes_done_human"] = human_size(stats.get("page_bytes_done"))
        stats["storage"] = storage.describe()
        return stats

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        database = db()
        try:
            stats = database.crawl_stats()
            recent = database.conn.execute("""
                SELECT * FROM releases
                ORDER BY
                  CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END,
                  published_at DESC, title
                LIMIT 12
                """).fetchall()
            recent_items = [release_to_dict(r) for r in recent]
            for item in recent_items:
                item["has_local_html"] = article_exists(storage, item["slug"])
        finally:
            database.close()
        stats["disk_bytes"] = storage_size_bytes(storage)
        stats["disk_human"] = human_size(stats["disk_bytes"])
        stats["bytes_done_human"] = human_size(stats.get("bytes_done"))
        stats["file_bytes_done_human"] = human_size(stats.get("file_bytes_done"))
        stats["page_bytes_done_human"] = human_size(stats.get("page_bytes_done"))
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "stats": stats,
                "recent": recent_items,
                "mirror_root": storage.describe(),
            },
        )

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_page(request: Request):
        stats = build_dashboard()
        return templates.TemplateResponse(
            request,
            "stats.html",
            {"stats": stats, "mirror_root": storage.describe()},
        )

    @app.get("/domains", response_class=HTMLResponse)
    async def domains_page(request: Request):
        database = db()
        try:
            domains = list_mirrored_domains(
                storage,
                database,
                seeds=list(cfg.seeds or []),
            )
        finally:
            database.close()
        return templates.TemplateResponse(
            request,
            "domains.html",
            {
                "domains": domains,
                "mirror_root": storage.describe(),
                "count": len(domains),
            },
        )

    @app.get("/releases", response_class=HTMLResponse)
    async def releases(
        request: Request,
        q: str = Query("", max_length=200),
        country: str = Query("", max_length=100),
        type_: str = Query("", alias="type", max_length=100),
    ):
        database = db()
        try:
            sql = "SELECT * FROM releases WHERE 1=1"
            args: list[str] = []
            if q.strip():
                like = f"%{q.strip()}%"
                sql += (
                    " AND (title ILIKE %s OR description ILIKE %s OR slug ILIKE %s "
                    "OR countries ILIKE %s OR types ILIKE %s OR source ILIKE %s)"
                )
                args.extend([like, like, like, like, like, like])
            if country.strip():
                sql += " AND countries ILIKE %s"
                args.append(f"%{country.strip()}%")
            if type_.strip():
                sql += " AND types ILIKE %s"
                args.append(f"%{type_.strip()}%")
            sql += """
                ORDER BY
                  CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END,
                  published_at DESC, title
                LIMIT 500
            """
            # conn.execute already translates ? → %s; avoid double-replace by using %s
            # and calling through raw connection for ILIKE queries with %s already.
            rows = database._raw.execute(sql, args).fetchall()
            items = [release_to_dict(r) for r in rows]
            for item in items:
                item["has_local_html"] = article_exists(storage, item["slug"])
            countries = _unique_json_values(database, "countries")
            types = _unique_json_values(database, "types")
        finally:
            database.close()
        return templates.TemplateResponse(
            request,
            "releases.html",
            {
                "items": items,
                "q": q,
                "country": country,
                "type": type_,
                "countries": countries,
                "types": types,
                "count": len(items),
            },
        )

    @app.get("/releases/{slug}", response_class=HTMLResponse)
    async def release_detail(request: Request, slug: str):
        database = db()
        try:
            row = database.conn.execute(
                "SELECT * FROM releases WHERE slug=?", (slug,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Release not found")
            item = release_to_dict(row)
            has_html = article_exists(storage, slug)
            item["has_local_html"] = has_html
            item["local_html_url"] = local_article_url(slug) if has_html else None

            magnet_rows = []
            for magnet in item["magnets"]:
                from ..urls import magnet_infohash

                ih = magnet_infohash(magnet)
                if not ih:
                    continue
                mrow = database.conn.execute(
                    "SELECT * FROM magnets WHERE infohash=?", (ih,)
                ).fetchone()
                if mrow:
                    magnet_rows.append(dict(mrow))
        finally:
            database.close()
        return templates.TemplateResponse(
            request,
            "release_detail.html",
            {"item": item, "magnet_rows": magnet_rows},
        )

    @app.get("/magnets", response_class=HTMLResponse)
    async def magnets(
        request: Request,
        status: str = Query("", max_length=40),
        q: str = Query("", max_length=200),
    ):
        database = db()
        try:
            sql = "SELECT * FROM magnets WHERE 1=1"
            args: list[str] = []
            if status.strip():
                sql += " AND status=?"
                args.append(status.strip())
            if q.strip():
                like = f"%{q.strip()}%"
                sql += " AND (title ILIKE ? OR infohash ILIKE ?)"
                args.extend([like, like])
            sql += " ORDER BY updated_at DESC LIMIT 500"
            rows = [dict(r) for r in database.conn.execute(sql, args).fetchall()]
            status_counts = {
                r["status"]: r["c"]
                for r in database.conn.execute(
                    "SELECT status, COUNT(*) AS c FROM magnets GROUP BY status"
                )
            }
        finally:
            database.close()
        return templates.TemplateResponse(
            request,
            "magnets.html",
            {
                "items": rows,
                "status": status,
                "q": q,
                "status_counts": status_counts,
                "count": len(rows),
            },
        )

    @app.get("/api/stats")
    async def api_stats():
        return build_dashboard()

    @app.get("/api/releases")
    async def api_releases(q: str = ""):
        database = db()
        try:
            if q.strip():
                like = f"%{q.strip()}%"
                rows = database._raw.execute(
                    """
                    SELECT * FROM releases
                    WHERE title ILIKE %s OR description ILIKE %s OR slug ILIKE %s
                    ORDER BY published_at DESC LIMIT 200
                    """,
                    (like, like, like),
                ).fetchall()
            else:
                rows = database.conn.execute(
                    "SELECT * FROM releases ORDER BY published_at DESC LIMIT 200"
                ).fetchall()
            return [release_to_dict(r) for r in rows]
        finally:
            database.close()

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_get(request: Request, tab: str = Query("form")):
        tab = tab if tab in {"form", "yaml"} else "form"
        return templates.TemplateResponse(
            request, "settings.html", config_context({"tab": tab})
        )

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_post(request: Request):
        form = await request.form()
        action = str(form.get("action") or "save_form")
        tab = str(form.get("tab") or "form")
        if tab not in {"form", "yaml"}:
            tab = "form"

        base, local, _active = resolve_config_path(base_path)
        if action == "reset":
            deleted = delete_local_config(base)
            msg = (
                f"Removed overlay {local}. Base config will be used."
                if deleted
                else "No local overlay to remove."
            )
            return templates.TemplateResponse(
                request,
                "settings.html",
                config_context({"message": msg, "tab": tab}),
            )

        try:
            if action == "save_yaml":
                config_text = str(form.get("config_text") or "")
                written = write_local_config(base, config_text)
            else:
                flat = {str(k): str(v) for k, v in form.items()}
                data = config_dict_from_form(flat)
                config_text = dump_config_yaml(data)
                written = write_local_config(base, config_text)
        except Exception as exc:  # noqa: BLE001
            extra = {
                "tab": tab,
                "error": f"Save failed: {exc}",
            }
            if action == "save_yaml":
                extra["config_text"] = str(form.get("config_text") or "")
            return templates.TemplateResponse(
                request,
                "settings.html",
                config_context(extra),
                status_code=400,
            )

        return templates.TemplateResponse(
            request,
            "settings.html",
            config_context(
                {
                    "tab": tab,
                    "message": (
                        f"Saved to {written}. New crawl/UI processes will load this file. "
                        "Restart the UI process to apply path/runtime changes here."
                    ),
                }
            ),
        )

    @app.get("/files")
    @app.get("/files/")
    @app.get("/files/{file_path:path}")
    async def browse_files(request: Request, file_path: str = ""):
        """Serve objects from mirror storage; directories get an HTML listing."""
        rel = normalize_key(file_path) if file_path else ""
        # Prefer exact file if present
        if rel and storage.exists(rel):
            ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            size = storage.size(rel)
            stream = storage.open_stream(rel)
            headers = {}
            if size is not None:
                headers["content-length"] = str(size)
            return StreamingResponse(stream, media_type=ctype, headers=headers)

        # Directory listing (prefix)
        entries = _list_dir_entries(rel)
        index_key = f"{rel}/index.html".lstrip("/") if rel else "index.html"
        # If prefix has children OR an index, treat as directory
        if entries or storage.exists(index_key):
            parent_rel = "/".join(rel.split("/")[:-1]) if rel else ""
            parent_href = f"/files/{parent_rel}/" if parent_rel else "/files/"
            crumbs = []
            parts = [p for p in rel.split("/") if p]
            acc: list[str] = []
            for part in parts:
                acc.append(part)
                crumbs.append({"name": part, "href": "/files/" + "/".join(acc) + "/"})
            index_href = None
            if storage.exists(index_key):
                index_href = f"/files/{index_key}"
            return templates.TemplateResponse(
                request,
                "files_browse.html",
                {
                    "rel": rel,
                    "mirror_root": storage.describe(),
                    "parent_href": parent_href if rel else None,
                    "crumbs": crumbs,
                    "index_href": index_href,
                    "entries": entries,
                },
            )
        raise HTTPException(status_code=404, detail="File not found")

    @app.get("/healthz")
    async def healthz():
        _base, local, active = resolve_config_path(base_path)
        ok_db = False
        try:
            database = db()
            try:
                database.conn.execute("SELECT 1")
                ok_db = True
            finally:
                database.close()
        except Exception:  # noqa: BLE001
            ok_db = False
        return {
            "ok": ok_db,
            "database": ok_db,
            "storage": storage.describe(),
            "config_active": str(active),
            "using_local": local.exists(),
        }

    return app


def _unique_json_values(database: StateDB, column: str) -> list[str]:
    values: set[str] = set()
    rows = database.conn.execute(f"SELECT {column} FROM releases").fetchall()
    for row in rows:
        raw = row[column]
        if not raw:
            continue
        try:
            parsed = __import__("json").loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed, list):
            for item in parsed:
                if item:
                    values.add(str(item))
    return sorted(values)


def run_server(
    config_path: str | Path, host: str = "0.0.0.0", port: int = 8090
) -> None:
    import uvicorn

    base, local, active = resolve_config_path(config_path)
    print(f"Config base:   {base}")
    print(f"Config local:  {local} ({'exists' if local.exists() else 'missing'})")
    print(f"Config active: {active}")
    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
