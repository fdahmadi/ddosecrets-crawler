from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
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
from .helpers import (
    dir_size_bytes,
    duration_seconds,
    format_ts,
    human_size,
    list_mirrored_domains,
    local_article_path,
    local_article_url,
    release_to_dict,
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

    mirror_root = cfg.mirror_path
    state_path = cfg.state_path
    app.state.config_base = str(base_path)
    app.state.config_local = str(local_path)
    app.state.config_active = str(active_path)
    app.state.using_local = local_path.exists()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # /files is served by browse_files() so directories get an HTML listing
    # (Starlette StaticFiles returns JSON 404 for folder roots without index.html).
    mirror_root.mkdir(parents=True, exist_ok=True)

    def db() -> StateDB:
        return StateDB(state_path)

    def _safe_mirror_path(rel: str) -> Path:
        root = mirror_root.resolve()
        target = (root / rel).resolve() if rel else root
        if target != root and not str(target).startswith(str(root) + "/"):
            raise HTTPException(status_code=403, detail="Path outside mirror")
        return target

    def _list_dir_entries(directory: Path, rel: str) -> list[dict]:
        entries: list[dict] = []
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError:
            return entries
        for child in children:
            if child.name.startswith("."):
                continue
            child_rel = f"{rel}/{child.name}".strip("/") if rel else child.name
            href = f"/files/{child_rel}/" if child.is_dir() else f"/files/{child_rel}"
            size = None
            if child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
            entries.append(
                {
                    "name": child.name + ("/" if child.is_dir() else ""),
                    "href": href,
                    "is_dir": child.is_dir(),
                    "size": size,
                    "size_human": human_size(size) if size is not None else "—",
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
        stats["disk_bytes"] = dir_size_bytes(mirror_root)
        stats["disk_human"] = human_size(stats["disk_bytes"])
        stats["bytes_done_human"] = human_size(stats.get("bytes_done"))
        stats["file_bytes_done_human"] = human_size(stats.get("file_bytes_done"))
        stats["page_bytes_done_human"] = human_size(stats.get("page_bytes_done"))
        return stats

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        database = db()
        try:
            stats = database.crawl_stats()
            recent = database.conn.execute(
                """
                SELECT * FROM releases
                ORDER BY
                  CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END,
                  published_at DESC, title
                LIMIT 12
                """
            ).fetchall()
            recent_items = [release_to_dict(r) for r in recent]
            for item in recent_items:
                item["has_local_html"] = local_article_path(mirror_root, item["slug"]) is not None
        finally:
            database.close()
        stats["disk_bytes"] = dir_size_bytes(mirror_root)
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
                "mirror_root": str(mirror_root),
            },
        )

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_page(request: Request):
        stats = build_dashboard()
        return templates.TemplateResponse(
            request,
            "stats.html",
            {"stats": stats, "mirror_root": str(mirror_root)},
        )

    @app.get("/domains", response_class=HTMLResponse)
    async def domains_page(request: Request):
        database = db()
        try:
            domains = list_mirrored_domains(
                mirror_root,
                database,
                seeds=list(cfg.seeds or []),
            )
        finally:
            database.close()
        return templates.TemplateResponse(
            request,
            "domains.html",
            {"domains": domains, "mirror_root": str(mirror_root), "count": len(domains)},
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
                    " AND (title LIKE ? OR description LIKE ? OR slug LIKE ? "
                    "OR countries LIKE ? OR types LIKE ? OR source LIKE ?)"
                )
                args.extend([like, like, like, like, like, like])
            if country.strip():
                sql += " AND countries LIKE ?"
                args.append(f"%{country.strip()}%")
            if type_.strip():
                sql += " AND types LIKE ?"
                args.append(f"%{type_.strip()}%")
            sql += """
                ORDER BY
                  CASE WHEN published_at IS NULL OR published_at='' THEN 1 ELSE 0 END,
                  published_at DESC, title
                LIMIT 500
            """
            rows = database.conn.execute(sql, args).fetchall()
            items = [release_to_dict(r) for r in rows]
            for item in items:
                item["has_local_html"] = (
                    local_article_path(mirror_root, item["slug"]) is not None
                )
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
            local_html = local_article_path(mirror_root, slug)
            item["has_local_html"] = local_html is not None
            item["local_html_url"] = local_article_url(slug) if local_html else None

            # Related magnet download statuses
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
                sql += " AND (title LIKE ? OR infohash LIKE ?)"
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
                rows = database.conn.execute(
                    """
                    SELECT * FROM releases
                    WHERE title LIKE ? OR description LIKE ? OR slug LIKE ?
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
                # save_form
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
        """Serve files; directories always get a listing (click index.html to open a site)."""
        rel = (file_path or "").strip("/")
        target = _safe_mirror_path(rel)
        if target.is_file():
            return FileResponse(target)
        if target.is_dir():
            parent_rel = "/".join(rel.split("/")[:-1]) if rel else ""
            parent_href = f"/files/{parent_rel}/" if parent_rel else "/files/"
            crumbs = []
            parts = [p for p in rel.split("/") if p]
            acc: list[str] = []
            for part in parts:
                acc.append(part)
                crumbs.append({"name": part, "href": "/files/" + "/".join(acc) + "/"})
            index_href = None
            if (target / "index.html").is_file():
                index_href = f"/files/{rel}/index.html" if rel else "/files/index.html"
            return templates.TemplateResponse(
                request,
                "files_browse.html",
                {
                    "rel": rel,
                    "mirror_root": str(mirror_root),
                    "parent_href": parent_href if rel else None,
                    "crumbs": crumbs,
                    "index_href": index_href,
                    "entries": _list_dir_entries(target, rel),
                },
            )
        raise HTTPException(status_code=404, detail="File not found")

    @app.get("/healthz")
    async def healthz():
        _base, local, active = resolve_config_path(base_path)
        ok_db = state_path.exists()
        return {
            "ok": True,
            "state_db": ok_db,
            "mirror_root": str(mirror_root),
            "config_active": str(active),
            "using_local": local.exists(),
        }

    return app


def _unique_json_values(database: StateDB, column: str) -> list[str]:
    values: set[str] = set()
    rows = database.conn.execute(f"SELECT {column} FROM releases").fetchall()
    for row in rows:
        raw = row[0]
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


def run_server(config_path: str | Path, host: str = "0.0.0.0", port: int = 8090) -> None:
    import uvicorn

    base, local, active = resolve_config_path(config_path)
    print(f"Config base:   {base}")
    print(f"Config local:  {local} ({'exists' if local.exists() else 'missing'})")
    print(f"Config active: {active}")
    app = create_app(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")