from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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
            retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
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

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult:
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
            resp = await self.request(
                "GET", url, headers={"Range": "bytes=0-0"}
            )
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
                return {"status_code": 416, "bytes": dest.stat().st_size if dest.exists() else 0}
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
            content_length = int(cl) + existing if cl and cl.isdigit() and existing else (
                int(cl) if cl and cl.isdigit() else written
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