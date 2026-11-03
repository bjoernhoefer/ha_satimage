"""Bounded retries with exponential backoff for failing render combinations.

A combination (region, source, image type) that keeps failing - in
particular because the render child ran out of memory or time - must not
be retried for every new scan or every archived raw slot: each attempt
re-reads the product from disk and pushes the host into memory pressure
again. After a failure the combination pauses for ``base`` and the pause
doubles with every further failure up to ``maximum``. A success or a
configuration change resets it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BASE_DELAY = timedelta(minutes=15)
MAX_DELAY = timedelta(hours=6)

logger = logging.getLogger(__name__)


def is_resource_error(exc: BaseException | None) -> bool:
    """Whether an error (or one of its causes) was a time/memory abort."""
    from ha_satimage.sources.satpy_render import RenderResourceError

    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, RenderResourceError):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


@dataclass
class _Entry:
    failures: int
    until: datetime
    error: str


class FailureBackoff:
    def __init__(self, base: timedelta = BASE_DELAY, maximum: timedelta = MAX_DELAY) -> None:
        self.base = base
        self.maximum = maximum
        self._entries: dict[tuple, _Entry] = {}
        self._lock = threading.Lock()

    def blocked_until(self, key: tuple, now: datetime | None = None) -> datetime | None:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            entry = self._entries.get(key)
            return entry.until if entry is not None and entry.until > now else None

    def failure(self, key: tuple, error: str, now: datetime | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            entry = self._entries.get(key)
            failures = (entry.failures if entry else 0) + 1
            delay = min(self.base * (2 ** (failures - 1)), self.maximum)
            until = now + delay
            self._entries[key] = _Entry(failures, until, error)
        if failures > 1:
            logger.warning(
                "%s failed %d times in a row - pause now %d min",
                "/".join(str(part) for part in key), failures, delay.total_seconds() // 60,
            )
        return until

    def success(self, key: tuple) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            logger.info(
                "%s works again after %d failure(s) - pause lifted",
                "/".join(str(part) for part in key), entry.failures,
            )

    def clear(self) -> None:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        if count:
            logger.info("Configuration changed - %d paused combination(s) are retried", count)

    def snapshot(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            return [
                {
                    "key": "/".join(str(part) for part in key),
                    "failures": entry.failures,
                    "paused_until": entry.until.isoformat() if entry.until > now else None,
                    "error": entry.error,
                }
                for key, entry in sorted(self._entries.items())
            ]
