from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from .config import PoliteConfig

log = logging.getLogger(__name__)


@dataclass
class PoliteLimiter:
    """Serialize HTTP requests and insert human-like random delays."""

    cfg: PoliteConfig
    _lock: asyncio.Lock | None = None
    _count: int = 0
    _last_at: float = 0.0

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        assert self._lock is not None
        async with self._lock:
            now = time.monotonic()
            delay = random.uniform(self.cfg.min_delay, self.cfg.max_delay)
            since = now - self._last_at
            if since < delay:
                sleep_for = delay - since
                log.debug("Polite delay %.1fs", sleep_for)
                await asyncio.sleep(sleep_for)

            self._count += 1
            if (
                self.cfg.burst_pause_every > 0
                and self._count % self.cfg.burst_pause_every == 0
            ):
                pause = random.uniform(
                    self.cfg.burst_pause_min, self.cfg.burst_pause_max
                )
                log.info("Burst pause after %d requests: %.1fs", self._count, pause)
                await asyncio.sleep(pause)

            self._last_at = time.monotonic()

    def sync_wait(self) -> None:
        """Blocking variant for non-async helpers."""
        now = time.monotonic()
        delay = random.uniform(self.cfg.min_delay, self.cfg.max_delay)
        since = now - self._last_at
        if since < delay:
            time.sleep(delay - since)
        self._count += 1
        if (
            self.cfg.burst_pause_every > 0
            and self._count % self.cfg.burst_pause_every == 0
        ):
            pause = random.uniform(self.cfg.burst_pause_min, self.cfg.burst_pause_max)
            log.info("Burst pause after %d requests: %.1fs", self._count, pause)
            time.sleep(pause)
        self._last_at = time.monotonic()