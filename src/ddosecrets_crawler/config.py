from __future__ import annotations

import os
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
    download_dir: str = "./staging/torrents"
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
class DatabaseConfig:
    """PostgreSQL connection (typically on a dedicated DB host)."""

    url: str = ""


@dataclass
class StorageConfig:
    """
    Mirror object storage.

    backend=minio  → remote MinIO/S3 (typically another host)
    backend=local  → local filesystem under mirror_root (dev/fallback)
    """

    backend: str = "minio"
    mirror_root: str = "./mirror"
    endpoint: str = ""
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "ddosecrets-mirror"
    secure: bool = True
    region: str | None = None
    prefix: str = ""
    staging_dir: str = "./staging"


@dataclass
class AppConfig:
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    seeds: list[str] = field(default_factory=list)
    allowed_host_suffixes: list[str] = field(default_factory=lambda: ["ddosecrets.org"])
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    polite: PoliteConfig = field(default_factory=PoliteConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    data: DataConfig = field(default_factory=DataConfig)
    torrent: TorrentConfig = field(default_factory=TorrentConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # Legacy aliases kept for templates / migration messages
    @property
    def mirror_root(self) -> str:
        return self.storage.mirror_root

    @property
    def mirror_path(self) -> Path:
        return Path(self.storage.mirror_root).resolve()

    @property
    def staging_path(self) -> Path:
        return Path(self.storage.staging_dir).resolve()


def _merge(dc_cls: type, data: dict[str, Any] | None):
    if not data:
        return dc_cls()
    fields = {f.name for f in dc_cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return dc_cls(**{k: v for k, v in data.items() if k in fields})


def _env_or(value: str | None, env_key: str, default: str = "") -> str:
    env = os.environ.get(env_key)
    if env is not None and env != "":
        return env
    return value if value is not None else default


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
    tmp = Path(base)
    local = local_config_path(base_config_path(tmp))
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    load_config_file(local)
    return local.resolve()


def delete_local_config(base: str | Path) -> bool:
    local = local_config_path(base_config_path(Path(base)))
    if local.exists():
        local.unlink()
        return True
    return False


def _load_database(raw: dict[str, Any]) -> DatabaseConfig:
    db_raw = raw.get("database")
    if isinstance(db_raw, dict):
        cfg = _merge(DatabaseConfig, db_raw)
    else:
        # Legacy: state_db path is no longer supported as SQLite.
        cfg = DatabaseConfig(url=str(raw.get("database_url") or ""))
    cfg.url = _env_or(cfg.url, "DATABASE_URL", cfg.url)
    return cfg


def _load_storage(raw: dict[str, Any]) -> StorageConfig:
    st_raw = raw.get("storage")
    if isinstance(st_raw, dict):
        cfg = _merge(StorageConfig, st_raw)
    else:
        # Legacy mirror_root → local backend
        cfg = StorageConfig(
            backend="local",
            mirror_root=str(raw.get("mirror_root") or "./mirror"),
        )
    cfg.endpoint = _env_or(cfg.endpoint, "MINIO_ENDPOINT", cfg.endpoint)
    cfg.access_key = _env_or(cfg.access_key, "MINIO_ACCESS_KEY", cfg.access_key)
    cfg.secret_key = _env_or(cfg.secret_key, "MINIO_SECRET_KEY", cfg.secret_key)
    cfg.bucket = _env_or(cfg.bucket, "MINIO_BUCKET", cfg.bucket or "ddosecrets-mirror")
    if os.environ.get("STORAGE_BACKEND"):
        cfg.backend = os.environ["STORAGE_BACKEND"]
    return cfg


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
        user_agent=raw.get(
            "user_agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        ),
        seeds=list(raw.get("seeds") or []),
        allowed_host_suffixes=list(
            raw.get("allowed_host_suffixes") or ["ddosecrets.org"]
        ),
        database=_load_database(raw),
        storage=_load_storage(raw),
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
    # Free disk before creating anything else. HTTP objects are streamed to
    # storage; files left here are incomplete downloads from the old path.
    from .storage import discard_http_staging_files

    discard_http_staging_files(cfg.staging_path)
    Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
    cfg.staging_path.mkdir(parents=True, exist_ok=True)
    Path(cfg.torrent.download_dir).mkdir(parents=True, exist_ok=True)
    if cfg.storage.backend == "local":
        cfg.mirror_path.mkdir(parents=True, exist_ok=True)
        (cfg.mirror_path / "site").mkdir(parents=True, exist_ok=True)
        (cfg.mirror_path / "data").mkdir(parents=True, exist_ok=True)
        (cfg.mirror_path / "torrents").mkdir(parents=True, exist_ok=True)
        (cfg.mirror_path / "metadata").mkdir(parents=True, exist_ok=True)
