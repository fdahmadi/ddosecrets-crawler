from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageEntry:
    key: str
    name: str
    is_dir: bool
    size: int | None = None


class MirrorStorage(Protocol):
    """Object storage for mirrored files (local disk or MinIO)."""

    backend: str

    def ensure_ready(self) -> None: ...

    def exists(self, key: str) -> bool: ...

    def size(self, key: str) -> int | None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def open_stream(self, key: str) -> BinaryIO: ...

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None: ...

    def put_text(
        self,
        key: str,
        text: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None: ...

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
    ) -> None: ...

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        length: int | None = None,
        content_type: str | None = None,
    ) -> None: ...

    def list_dir(self, prefix: str = "") -> list[StorageEntry]: ...

    def list_hosts(self, section: str) -> list[str]: ...

    def total_bytes(self, prefix: str = "") -> int: ...

    def describe(self) -> str: ...


def normalize_key(key: str) -> str:
    key = (key or "").replace("\\", "/").lstrip("/")
    parts = [p for p in key.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError(f"Unsafe storage key: {key}")
    return "/".join(parts)


def mirror_object_key(url: str, *, section: str) -> str:
    """Map URL to storage key under section/host/... (same layout as local mirror)."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    path = unquote(parsed.path or "/")
    if path.endswith("/"):
        rel = f"{host}/{path.lstrip('/')}" + "index.html"
    else:
        body = path.lstrip("/")
        name = Path(body).name
        if section == "site" and name and "." not in name:
            rel = f"{host}/{body}.html"
        else:
            rel = f"{host}/{body}"
    return normalize_key(f"{section}/{rel}")


def data_file_object_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "data.ddosecrets.org"
    path = unquote(parsed.path or "/").lstrip("/")
    return normalize_key(f"data/{host}/{path}")


def guess_content_type(key: str, fallback: str | None = None) -> str:
    ctype, _ = mimetypes.guess_type(key)
    return ctype or fallback or "application/octet-stream"


# S3 allows at most 10_000 parts. 8 MiB is the floor (above the 5 MiB minimum).
_MIN_PART_BYTES = 8 * 1024 * 1024
_UNKNOWN_LENGTH_PART_BYTES = 64 * 1024 * 1024
_MAX_MULTIPART_PARTS = 10_000


def multipart_part_size(length: int | None) -> int:
    """Pick a part size that stays within the S3 10_000-part limit."""
    if length is None or length <= 0:
        return _UNKNOWN_LENGTH_PART_BYTES
    needed = (length + _MAX_MULTIPART_PARTS - 1) // _MAX_MULTIPART_PARTS
    align = 1024 * 1024
    if needed % align:
        needed += align - (needed % align)
    return max(_MIN_PART_BYTES, needed)


class LocalMirrorStorage:
    backend = "local"

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def ensure_ready(self) -> None:
        for sub in ("site", "data", "torrents", "metadata"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        key = normalize_key(key)
        full = (self.root / key).resolve()
        if full != self.root and not str(full).startswith(str(self.root) + "/"):
            raise ValueError(f"Unsafe path for key={key}")
        return full

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def size(self, key: str) -> int | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.stat().st_size

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def open_stream(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def put_text(
        self,
        key: str,
        text: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
    ) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(path).read_bytes())

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        length: int | None = None,
        content_type: str | None = None,
    ) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        written = 0
        try:
            with tmp.open("wb") as fh:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
            if length is not None and written != length:
                raise OSError(f"Incomplete stream for {key}: {written}/{length}")
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def list_dir(self, prefix: str = "") -> list[StorageEntry]:
        prefix = normalize_key(prefix) if prefix else ""
        directory = self.root / prefix if prefix else self.root
        if not directory.is_dir():
            return []
        entries: list[StorageEntry] = []
        for child in sorted(
            directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        ):
            if child.name.startswith("."):
                continue
            child_key = f"{prefix}/{child.name}".strip("/") if prefix else child.name
            if child.is_dir():
                entries.append(
                    StorageEntry(
                        key=child_key + "/", name=child.name + "/", is_dir=True
                    )
                )
            elif child.is_file():
                entries.append(
                    StorageEntry(
                        key=child_key,
                        name=child.name,
                        is_dir=False,
                        size=child.stat().st_size,
                    )
                )
        return entries

    def list_hosts(self, section: str) -> list[str]:
        section_dir = self.root / section
        if not section_dir.is_dir():
            return []
        return sorted(p.name for p in section_dir.iterdir() if p.is_dir())

    def total_bytes(self, prefix: str = "") -> int:
        base = self.root / normalize_key(prefix) if prefix else self.root
        if not base.exists():
            return 0
        total = 0
        if base.is_file():
            return base.stat().st_size
        for path in base.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def describe(self) -> str:
        return f"local:{self.root}"


class MinioMirrorStorage:
    backend = "minio"

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = True,
        region: str | None = None,
        prefix: str = "",
    ):
        from minio import Minio

        endpoint = endpoint.removeprefix("https://").removeprefix("http://")
        self.endpoint = endpoint
        self.bucket = bucket
        self.secure = secure
        self.prefix = normalize_key(prefix) if prefix else ""
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region or None,
        )

    def _object_name(self, key: str) -> str:
        key = normalize_key(key)
        if self.prefix:
            return f"{self.prefix}/{key}"
        return key

    def _from_object_name(self, name: str) -> str:
        if self.prefix and name.startswith(self.prefix + "/"):
            return name[len(self.prefix) + 1 :]
        return name

    def ensure_ready(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
            log.info("Created MinIO bucket %s", self.bucket)

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self.client.stat_object(self.bucket, self._object_name(key))
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise

    def size(self, key: str) -> int | None:
        from minio.error import S3Error

        try:
            stat = self.client.stat_object(self.bucket, self._object_name(key))
            return int(stat.size)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return None
            raise

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(self.bucket, self._object_name(key))
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def open_stream(self, key: str) -> BinaryIO:
        return self.client.get_object(self.bucket, self._object_name(key))

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        import io

        ctype = content_type or guess_content_type(key)
        self.client.put_object(
            self.bucket,
            self._object_name(key),
            io.BytesIO(data),
            length=len(data),
            content_type=ctype,
        )

    def put_text(
        self,
        key: str,
        text: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self.put_bytes(key, text.encode("utf-8"), content_type=content_type)

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str | None = None,
    ) -> None:
        ctype = content_type or guess_content_type(key)
        self.client.fput_object(
            self.bucket,
            self._object_name(key),
            str(path),
            content_type=ctype,
        )

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        length: int | None = None,
        content_type: str | None = None,
    ) -> None:
        """Multipart-upload a readable stream. Unknown length uses length=-1."""
        import io

        ctype = content_type or guess_content_type(key)
        object_name = self._object_name(key)
        if length == 0:
            self.client.put_object(
                self.bucket,
                object_name,
                io.BytesIO(b""),
                length=0,
                content_type=ctype,
            )
            return
        size = -1 if length is None else length
        self.client.put_object(
            self.bucket,
            object_name,
            stream,
            length=size,
            content_type=ctype,
            part_size=multipart_part_size(None if size < 0 else size),
            num_parallel_uploads=1,
        )

    def list_dir(self, prefix: str = "") -> list[StorageEntry]:
        prefix = normalize_key(prefix) if prefix else ""
        object_prefix = self._object_name(prefix)
        if object_prefix and not object_prefix.endswith("/"):
            object_prefix += "/"
        # Root listing
        if not prefix:
            object_prefix = (self.prefix + "/") if self.prefix else ""

        names: dict[str, StorageEntry] = {}
        for obj in self.client.list_objects(
            self.bucket, prefix=object_prefix, recursive=False
        ):
            full = obj.object_name or ""
            rel = self._from_object_name(full)
            if prefix:
                if not rel.startswith(prefix + "/") and rel != prefix:
                    continue
                rest = rel[len(prefix) :].lstrip("/")
            else:
                rest = rel
            if not rest:
                continue
            name = rest.split("/", 1)[0]
            if not name:
                continue
            child_key = f"{prefix}/{name}".strip("/") if prefix else name
            is_dir = "/" in rest or bool(getattr(obj, "is_dir", False))
            if is_dir:
                names[name] = StorageEntry(
                    key=child_key + "/", name=name + "/", is_dir=True
                )
            else:
                names[name] = StorageEntry(
                    key=child_key,
                    name=name,
                    is_dir=False,
                    size=int(obj.size) if obj.size is not None else None,
                )
        return sorted(
            names.values(),
            key=lambda e: (not e.is_dir, e.name.lower()),
        )

    def list_hosts(self, section: str) -> list[str]:
        entries = self.list_dir(section)
        return [e.name.rstrip("/") for e in entries if e.is_dir]

    def total_bytes(self, prefix: str = "") -> int:
        prefix = normalize_key(prefix) if prefix else ""
        object_prefix = self._object_name(prefix)
        if object_prefix and not object_prefix.endswith("/"):
            object_prefix += "/"
        if not prefix and self.prefix:
            object_prefix = self.prefix + "/"
        total = 0
        for obj in self.client.list_objects(
            self.bucket, prefix=object_prefix, recursive=True
        ):
            if obj.is_dir:
                continue
            total += int(obj.size or 0)
        return total

    def describe(self) -> str:
        scheme = "https" if self.secure else "http"
        return f"minio:{scheme}://{self.endpoint}/{self.bucket}"


def build_storage(cfg) -> MirrorStorage:
    """Build storage backend from AppConfig.storage."""
    storage_cfg = cfg.storage
    backend = (storage_cfg.backend or "local").lower()
    if backend == "local":
        store: MirrorStorage = LocalMirrorStorage(Path(storage_cfg.mirror_root))
    elif backend == "minio":
        if not storage_cfg.endpoint or not storage_cfg.bucket:
            raise ValueError(
                "storage.endpoint and storage.bucket are required for MinIO"
            )
        if not storage_cfg.access_key or not storage_cfg.secret_key:
            raise ValueError(
                "storage.access_key and storage.secret_key are required for MinIO"
            )
        store = MinioMirrorStorage(
            endpoint=storage_cfg.endpoint,
            access_key=storage_cfg.access_key,
            secret_key=storage_cfg.secret_key,
            bucket=storage_cfg.bucket,
            secure=bool(storage_cfg.secure),
            region=storage_cfg.region,
            prefix=storage_cfg.prefix or "",
        )
    else:
        raise ValueError(f"Unknown storage.backend: {backend}")
    store.ensure_ready()
    return store


def discard_http_staging_files(staging_dir: Path) -> tuple[int, int]:
    """Delete leftover HTTP downloads. Torrent data under torrents/ is kept."""
    staging_dir = Path(staging_dir)
    if not staging_dir.is_dir():
        return 0, 0
    freed = 0
    count = 0
    for child in staging_dir.iterdir():
        if child.name == "torrents" or not child.is_file():
            continue
        try:
            size = child.stat().st_size
            child.unlink()
        except OSError as exc:
            log.warning("Could not remove staging file %s: %s", child, exc)
            continue
        freed += size
        count += 1
    if count:
        log.info(
            "Removed %d leftover HTTP staging file(s), freed %.2f GiB",
            count,
            freed / (1024**3),
        )
    return freed, count
