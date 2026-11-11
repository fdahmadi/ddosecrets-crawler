from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PoliteConfig:
    min_delay: float = 3.0
    max_delay: float = 12.0
    burst_pause_every: int = 25
    burst_pause_min: float = 20.0
    burst_pause_max: float = 90.0
    max_concurrent_http: int = 1
    retry_max: int = 5
    retry_backoff_base: float = 5.0
    timeout: float = 60.0


@dataclass
class SiteConfig:
    enabled: bool = True
    save_html: bool = True
    follow_static: bool = True
    max_pages: int = 0
    respect_query_params: bool = False


@dataclass
class DataConfig:
    enabled: bool = True
    prefer_torrent: bool = True
    http_fallback: bool = True
    max_file_bytes: int = 0
    max_indexes: int = 0  # 0 = unlimited directory pages
    skip_extensions: list[str] = field(default_factory=list)
    recurse_directories: bool = True


@dataclass
class TorrentConfig:
    enabled: bool = True
    magnets_csv: str = "https://torrents.ddosecrets.org/magnets.csv"
    releases_rss: str = "https://torrents.ddosecrets.org/releases.xml"
    site_rss: str = "https://ddosecrets.org/feed/rss.xml"
    download_dir: str = "./mirror/torrents"
    aria2_rpc: str | None = None
    aria2_secret: str | None = None
    use_cli: bool = True
    max_concurrent_torrents: int = 2
    seed_ratio: float = 1.0
    seed_time_minutes: int = 60


@dataclass
class ScheduleConfig:
    site_interval_hours: float = 6
    data_index_interval_hours: float = 12
    torrent_index_interval_hours: float = 6
    full_reconcile_days: float = 7
    # Keep draining the crawl queue without manual restarts (VPS-friendly)
    continuous: bool = True
    batch_pause_seconds: float = 45.0
    idle_sleep_seconds: float = 900.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./logs/crawler.log"


@dataclass
class AppConfig:
    mirror_root: str = "./mirror"
    state_db: str = "./state/mirror.db"
    catalog_db: str = "./state/catalog.db"
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    seeds: list[str] = field(default_factory=list)
    allowed_host_suffixes: list[str] = field(
        default_factory=lambda: ["ddosecrets.org"]
    )
    polite: PoliteConfig = field(default_factory=PoliteConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    data: DataConfig = field(default_factory=DataConfig)
    torrent: TorrentConfig = field(default_factory=TorrentConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def mirror_path(self) -> Path:
        return Path(self.mirror_root).resolve()

    @property
    def state_path(self) -> Path:
        return Path(self.state_db).resolve()

    @property
    def catalog_path(self) -> Path:
        return Path(self.catalog_db).resolve()


def _merge(dc_cls: type, data: dict[str, Any] | None):
    if not data:
        return dc_cls()
    fields = {f.name for f in dc_cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return dc_cls(**{k: v for k, v in data.items() if k in fields})


def local_config_path(base: str | Path) -> Path:
    """
    Map base config -> editable overlay file.

    config.laptop.yaml  -> config.laptop.local.yaml
    config.yaml         -> config.local.yaml
    """
    base = Path(base)
    if base.stem.endswith(".local"):
        return base
    return base.with_name(f"{base.stem}.local{base.suffix}")


def base_config_path(path: str | Path) -> Path:
    """If given a *.local.yaml, return the original base path."""
    path = Path(path)
    if path.stem.endswith(".local"):
        return path.with_name(path.stem[: -len(".local")] + path.suffix)
    return path


def resolve_config_path(path: str | Path) -> tuple[Path, Path, Path]:
    """
    Returns (base_path, local_path, active_path).

    If the `.local.yaml` overlay exists, active_path points to it even when
    the CLI was given the base file via `-c`.
    """
    requested = Path(path)
    base = base_config_path(requested)
    local = local_config_path(base)
    active = local if local.exists() else base
    return base.resolve(), local.resolve(), active.resolve()


def read_config_text(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_local_config(base: str | Path, text: str) -> Path:
    """Validate YAML and write the `.local.yaml` overlay."""
    parsed = yaml.safe_load(text)
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValueError("Config root must be a YAML mapping/object")
    # Round-trip through load to catch structural issues early
    tmp = Path(base)
    local = local_config_path(base_config_path(tmp))
    local.parent.mkdir(parents=True, exist_ok=True)
    # Normalize formatting a bit while preserving user content as given
    local.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    # Ensure it parses with our loader
    load_config_file(local)
    return local.resolve()


def delete_local_config(base: str | Path) -> bool:
    local = local_config_path(base_config_path(Path(base)))
    if local.exists():
        local.unlink()
        return True
    return False


def load_config_file(path: str | Path) -> AppConfig:
    """Load exactly the given file (no .local resolution)."""
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config file {path}: root must be a mapping")

    return AppConfig(
        mirror_root=raw.get("mirror_root", "./mirror"),
        state_db=raw.get("state_db", "./state/mirror.db"),
        catalog_db=raw.get("catalog_db", "./state/catalog.db"),
        user_agent=raw.get(
            "user_agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        ),
        seeds=list(raw.get("seeds") or []),
        allowed_host_suffixes=list(
            raw.get("allowed_host_suffixes") or ["ddosecrets.org"]
        ),
        polite=_merge(PoliteConfig, raw.get("polite")),
        site=_merge(SiteConfig, raw.get("site")),
        data=_merge(DataConfig, raw.get("data")),
        torrent=_merge(TorrentConfig, raw.get("torrent")),
        schedule=_merge(ScheduleConfig, raw.get("schedule")),
        logging=_merge(LoggingConfig, raw.get("logging")),
    )


def load_config(path: str | Path) -> AppConfig:
    """Load config, preferring `*.local.yaml` overlay when present."""
    _base, _local, active = resolve_config_path(path)
    return load_config_file(active)


def ensure_runtime_dirs(cfg: AppConfig) -> None:
    cfg.mirror_path.mkdir(parents=True, exist_ok=True)
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.torrent.download_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
    (cfg.mirror_path / "site").mkdir(parents=True, exist_ok=True)
    (cfg.mirror_path / "data").mkdir(parents=True, exist_ok=True)
    (cfg.mirror_path / "torrents").mkdir(parents=True, exist_ok=True)
    (cfg.mirror_path / "metadata").mkdir(parents=True, exist_ok=True)