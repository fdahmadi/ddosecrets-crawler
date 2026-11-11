from __future__ import annotations

from typing import Any

import yaml

FORM_GROUPS: list[dict[str, Any]] = [
    {
        "id": "general",
        "title": "General",
        "fields": [
            {"key": "mirror_root", "label": "Mirror root", "type": "text"},
            {"key": "state_db", "label": "State DB path", "type": "text"},
            {"key": "catalog_db", "label": "Catalog DB path", "type": "text"},
            {"key": "user_agent", "label": "User-Agent", "type": "text"},
            {
                "key": "seeds",
                "label": "Seeds (one URL per line)",
                "type": "list",
            },
            {
                "key": "allowed_host_suffixes",
                "label": "Allowed host suffixes (one per line)",
                "type": "list",
            },
        ],
    },
    {
        "id": "polite",
        "title": "Polite delays",
        "section": "polite",
        "fields": [
            {"key": "min_delay", "label": "Min delay (sec)", "type": "float"},
            {"key": "max_delay", "label": "Max delay (sec)", "type": "float"},
            {"key": "burst_pause_every", "label": "Burst pause every N requests", "type": "int"},
            {"key": "burst_pause_min", "label": "Burst pause min (sec)", "type": "float"},
            {"key": "burst_pause_max", "label": "Burst pause max (sec)", "type": "float"},
            {"key": "max_concurrent_http", "label": "Max concurrent HTTP", "type": "int"},
            {"key": "retry_max", "label": "Retry max", "type": "int"},
            {"key": "retry_backoff_base", "label": "Retry backoff base", "type": "float"},
            {"key": "timeout", "label": "Timeout (sec)", "type": "float"},
        ],
    },
    {
        "id": "site",
        "title": "Website crawl",
        "section": "site",
        "fields": [
            {"key": "enabled", "label": "Enabled", "type": "bool"},
            {"key": "save_html", "label": "Save HTML", "type": "bool"},
            {"key": "follow_static", "label": "Follow static assets", "type": "bool"},
            {"key": "max_pages", "label": "Max pages (0=unlimited)", "type": "int"},
            {"key": "respect_query_params", "label": "Respect query params", "type": "bool"},
        ],
    },
    {
        "id": "data",
        "title": "Data server mirror",
        "section": "data",
        "fields": [
            {"key": "enabled", "label": "Enabled", "type": "bool"},
            {"key": "prefer_torrent", "label": "Prefer torrent", "type": "bool"},
            {"key": "http_fallback", "label": "HTTP fallback", "type": "bool"},
            {"key": "max_file_bytes", "label": "Max file bytes (0=unlimited)", "type": "int"},
            {"key": "max_indexes", "label": "Max indexes (0=unlimited)", "type": "int"},
            {
                "key": "skip_extensions",
                "label": "Skip extensions (one per line, e.g. .mp4)",
                "type": "list",
            },
            {"key": "recurse_directories", "label": "Recurse directories", "type": "bool"},
        ],
    },
    {
        "id": "torrent",
        "title": "Torrent / magnet",
        "section": "torrent",
        "fields": [
            {"key": "enabled", "label": "Enabled", "type": "bool"},
            {"key": "magnets_csv", "label": "Magnets CSV URL", "type": "text"},
            {"key": "releases_rss", "label": "Releases RSS URL", "type": "text"},
            {"key": "site_rss", "label": "Site RSS URL", "type": "text"},
            {"key": "download_dir", "label": "Download dir", "type": "text"},
            {"key": "aria2_rpc", "label": "aria2 RPC URL (optional)", "type": "text_nullable"},
            {"key": "aria2_secret", "label": "aria2 RPC secret (optional)", "type": "text_nullable"},
            {"key": "use_cli", "label": "Use aria2 CLI", "type": "bool"},
            {"key": "max_concurrent_torrents", "label": "Max concurrent torrents", "type": "int"},
            {"key": "seed_ratio", "label": "Seed ratio", "type": "float"},
            {"key": "seed_time_minutes", "label": "Seed time (minutes)", "type": "int"},
        ],
    },
    {
        "id": "schedule",
        "title": "Scheduler",
        "section": "schedule",
        "fields": [
            {"key": "site_interval_hours", "label": "Site interval (hours)", "type": "float"},
            {
                "key": "data_index_interval_hours",
                "label": "Data index interval (hours)",
                "type": "float",
            },
            {
                "key": "torrent_index_interval_hours",
                "label": "Torrent index interval (hours)",
                "type": "float",
            },
            {"key": "full_reconcile_days", "label": "Full reconcile (days)", "type": "float"},
            {
                "key": "continuous",
                "label": "Continuous drain (keep crawling pending queue)",
                "type": "bool",
            },
            {
                "key": "batch_pause_seconds",
                "label": "Pause between batches (seconds)",
                "type": "float",
            },
            {
                "key": "idle_sleep_seconds",
                "label": "Idle sleep when queue empty (seconds)",
                "type": "float",
            },
        ],
    },
    {
        "id": "logging",
        "title": "Logging",
        "section": "logging",
        "fields": [
            {"key": "level", "label": "Level", "type": "text"},
            {"key": "file", "label": "Log file", "type": "text"},
        ],
    },
]


def _get_value(data: dict[str, Any], section: str | None, key: str) -> Any:
    if section:
        return (data.get(section) or {}).get(key)
    return data.get(key)


def form_values_from_config(data: dict[str, Any]) -> dict[str, str]:
    """Flat form values for template inputs."""
    out: dict[str, str] = {}
    for group in FORM_GROUPS:
        section = group.get("section")
        for field in group["fields"]:
            name = f"{section}.{field['key']}" if section else field["key"]
            raw = _get_value(data, section, field["key"])
            ftype = field["type"]
            if ftype == "list":
                if isinstance(raw, list):
                    out[name] = "\n".join(str(x) for x in raw)
                elif raw is None:
                    out[name] = ""
                else:
                    out[name] = str(raw)
            elif ftype == "bool":
                out[name] = "true" if bool(raw) else "false"
            elif ftype == "text_nullable":
                out[name] = "" if raw is None else str(raw)
            else:
                out[name] = "" if raw is None else str(raw)
    return out


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_list(value: str) -> list[str]:
    items = []
    for line in value.splitlines():
        item = line.strip()
        if item:
            items.append(item)
    return items


def config_dict_from_form(form: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for group in FORM_GROUPS:
        section = group.get("section")
        target = data
        if section:
            target = data.setdefault(section, {})
        for field in group["fields"]:
            name = f"{section}.{field['key']}" if section else field["key"]
            raw = form.get(name, "")
            ftype = field["type"]
            if ftype == "bool":
                # unchecked checkboxes may be missing; use hidden+checkbox pattern value
                target[field["key"]] = _parse_bool(raw)
            elif ftype == "list":
                target[field["key"]] = _parse_list(raw)
            elif ftype == "int":
                target[field["key"]] = int(raw or 0)
            elif ftype == "float":
                target[field["key"]] = float(raw or 0)
            elif ftype == "text_nullable":
                text = raw.strip()
                target[field["key"]] = None if text == "" else text
            else:
                target[field["key"]] = raw
    return data


def dump_config_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )