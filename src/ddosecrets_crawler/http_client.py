from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import AppConfig
from .polite import PoliteLimiter

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes
    final_url: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    @property
    def content_length(self) -> int | None:
        raw = self.headers.get("content-length")
        if raw and raw.isdigit():
            return int(raw)
        return len(self.content) if self.content is not None else None


class _StreamReader(io.RawIOBase):
    """Blocking reader fed by the async HTTP body. A short queue applies backpressure."""

    def __init__(self) -> None:
        super().__init__()
        self._queue: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=4)
        self._buf = bytearray()
        self._eof = False
        self._lock = threading.Lock()
        self._failed: BaseException | None = None
        self._consumer_done = threading.Event()

    def readable(self) -> bool:
        return True

    def note_failure(self, exc: BaseException) -> None:
        with self._lock:
            if self._failed is None:
                self._failed = exc

    def mark_consumer_done(self) -> None:
        self._consumer_done.set()

    def feed(self, chunk: bytes) -> None:
        if chunk:
            self._put(chunk)

    def finish(self) -> None:
        self._put(None)

    def fail(self, exc: BaseException) -> None:
        # read() polls this flag, so a full queue cannot block the abort.
        self.note_failure(exc)

    def _put(self, item: bytes | BaseException | None) -> None:
        while True:
            with self._lock:
                failed = self._failed
            if isinstance(item, BaseException):
                pass
            elif failed is not None:
                raise OSError(f"download aborted: {failed}")
            if self._consumer_done.is_set():
                return
            try:
                self._queue.put(item, timeout=0.25)
                return
            except queue.Full:
                continue

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 1024 * 1024
        if size == 0:
            return b""
        while not self._eof and len(self._buf) < size:
            with self._lock:
                failed = self._failed
            if failed is not None:
                self._eof = True
                raise OSError(f"download aborted: {failed}")
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if isinstance(item, BaseException):
                self._eof = True
                raise item
            if item is None:
                self._eof = True
                break
            self._buf.extend(item)
        if len(self._buf) <= size:
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out


class PoliteHttpClient:
    def __init__(self, cfg: AppConfig, limiter: PoliteLimiter | None = None):
        self.cfg = cfg
        self.limiter = limiter or PoliteLimiter(cfg.polite)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept": "*/*"},
            follow_redirects=True,
            timeout=httpx.Timeout(cfg.polite.timeout, connect=30.0),
            http2=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        await self.limiter.wait()

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.cfg.polite.retry_max),
            wait=wait_exponential(
                multiplier=self.cfg.polite.retry_backoff_base, min=2, max=120
            ),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.TimeoutException)
            ),
        )
        async def _do() -> httpx.Response:
            resp = await self._client.request(method, url, headers=headers)
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("retry-after")
                log.warning(
                    "%s %s -> %s (Retry-After=%s)",
                    method,
                    url,
                    resp.status_code,
                    retry_after,
                )
                raise httpx.TransportError(f"rate limited: {resp.status_code}")
            return resp

        return await _do()

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> FetchResult:
        resp = await self.request("GET", url, headers=headers)
        content = await resp.aread()
        result = FetchResult(
            url=url,
            status_code=resp.status_code,
            headers=resp.headers,
            content=content,
            final_url=str(resp.url),
        )
        await resp.aclose()
        return result

    async def head(self, url: str) -> FetchResult:
        try:
            resp = await self.request("HEAD", url)
            result = FetchResult(
                url=url,
                status_code=resp.status_code,
                headers=resp.headers,
                content=b"",
                final_url=str(resp.url),
            )
            await resp.aclose()
            return result
        except Exception as exc:  # noqa: BLE001
            log.debug("HEAD failed for %s: %s; falling back to GET range", url, exc)
            resp = await self.request("GET", url, headers={"Range": "bytes=0-0"})
            result = FetchResult(
                url=url,
                status_code=resp.status_code,
                headers=resp.headers,
                content=b"",
                final_url=str(resp.url),
            )
            await resp.aclose()
            return result

    async def download_to(
        self,
        url: str,
        dest: Path,
        *,
        expected_size: int | None = None,
        resume: bool = True,
    ) -> dict[str, Any]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        headers: dict[str, str] = {}
        mode = "wb"
        existing = 0
        if resume and part.exists():
            existing = part.stat().st_size
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"
                mode = "ab"

        await self.limiter.wait()
        async with self._client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 416:
                # Already complete according to server
                if part.exists():
                    part.replace(dest)
                return {
                    "status_code": 416,
                    "bytes": dest.stat().st_size if dest.exists() else 0,
                }
            if resp.status_code == 200 and existing > 0:
                # Server ignored Range; restart
                mode = "wb"
                existing = 0
            resp.raise_for_status()
            written = existing
            with part.open(mode) as fh:
                async for chunk in resp.aiter_bytes(1024 * 256):
                    fh.write(chunk)
                    written += len(chunk)
            etag = resp.headers.get("etag")
            last_modified = resp.headers.get("last-modified")
            content_type = resp.headers.get("content-type")
            cl = resp.headers.get("content-length")
            content_length = (
                int(cl) + existing
                if cl and cl.isdigit() and existing
                else (int(cl) if cl and cl.isdigit() else written)
            )

        if expected_size and written < expected_size:
            raise IOError(f"Incomplete download {url}: {written}/{expected_size}")

        part.replace(dest)
        return {
            "status_code": resp.status_code,
            "bytes": written,
            "etag": etag,
            "last_modified": last_modified,
            "content_type": content_type,
            "content_length": content_length,
        }

    async def download_to_storage(
        self,
        url: str,
        storage,
        key: str,
        *,
        expected_size: int | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Stream the response body into storage without keeping the object on disk."""
        await self.limiter.wait()
        async with self._client.stream("GET", url) as resp:
            resp.raise_for_status()
            etag = resp.headers.get("etag")
            last_modified = resp.headers.get("last-modified")
            response_type = resp.headers.get("content-type")
            cl = resp.headers.get("content-length")
            content_length = int(cl) if cl and cl.isdigit() else None
            if (
                expected_size is not None
                and content_length is not None
                and expected_size != content_length
            ):
                raise OSError(
                    f"Size mismatch for {url}: "
                    f"response {content_length}, expected {expected_size}"
                )
            length = content_length if content_length is not None else expected_size
            stored_type = content_type or response_type

            if length == 0:
                async for chunk in resp.aiter_raw(1024 * 1024):
                    if chunk:
                        raise OSError(f"Expected empty body for {url}")
                await asyncio.to_thread(
                    storage.put_stream,
                    key,
                    io.BytesIO(b""),
                    length=0,
                    content_type=stored_type,
                )
                return {
                    "status_code": resp.status_code,
                    "bytes": 0,
                    "etag": etag,
                    "last_modified": last_modified,
                    "content_type": response_type,
                    "content_length": 0,
                }

            reader = _StreamReader()

            def _upload() -> None:
                try:
                    storage.put_stream(
                        key,
                        reader,
                        length=length,
                        content_type=stored_type,
                    )
                except BaseException as exc:
                    reader.note_failure(exc)
                    raise
                finally:
                    reader.mark_consumer_done()

            upload_task = asyncio.create_task(asyncio.to_thread(_upload))
            written = 0
            try:
                async for chunk in resp.aiter_raw(1024 * 1024):
                    if upload_task.done():
                        await upload_task
                    if not chunk:
                        continue
                    written += len(chunk)
                    await asyncio.to_thread(reader.feed, chunk)
                if content_length is not None and written != content_length:
                    raise OSError(
                        f"Incomplete download {url}: {written}/{content_length}"
                    )
                if expected_size is not None and written != expected_size:
                    raise OSError(
                        f"Incomplete download {url}: {written}/{expected_size}"
                    )
                await asyncio.to_thread(reader.finish)
                await upload_task
            except BaseException as exc:
                fail_exc = exc if isinstance(exc, Exception) else OSError(str(exc))
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(reader.fail, fail_exc)
                if not upload_task.done():
                    with contextlib.suppress(Exception):
                        await upload_task
                raise

        return {
            "status_code": resp.status_code,
            "bytes": written,
            "etag": etag,
            "last_modified": last_modified,
            "content_type": response_type,
            "content_length": content_length if content_length is not None else written,
        }
