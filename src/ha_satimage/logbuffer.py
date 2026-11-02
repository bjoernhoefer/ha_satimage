"""In-memory log for the web UI (``GET /api/logs``).

A ``logging.Handler`` keeps the latest entries in a ring buffer. Each entry
gets a sequential ID so the UI can fetch only new lines via ``?after=<id>``.
Optionally, logs are also written to a rotating file under
``<data dir>/logs/`` (bounded: ``FILE_MAX_BYTES`` × ``FILE_BACKUPS``) so
they survive a restart.

Standard output goes to Docker's log driver, i.e. Docker's data directory
(on the Raspberry Pi the SD card). It only needs what the file does not
already hold, so its level is set separately.

Levels: ``HA_SATIMAGE_LOG_LEVEL`` (UI log + file, default ``INFO``) and
``HA_SATIMAGE_STDOUT_LOG_LEVEL`` (container output, the image sets
``WARNING``) are the defaults; the ``logging`` section of the configuration
(web UI) overrides both at runtime via :func:`apply_levels`. ``DEBUG`` only
affects ha_satimage's own loggers, never third-party libraries.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Access logs from UI polling would drown out everything else.
_IGNORED_LOGGERS = ("uvicorn.access",)
FILE_MAX_BYTES = 1_000_000
FILE_BACKUPS = 3
STDOUT_LEVEL_ENV = "HA_SATIMAGE_STDOUT_LOG_LEVEL"
LEVEL_ENV = "HA_SATIMAGE_LOG_LEVEL"
APP_LOGGER = "ha_satimage"

# Handlers created by install(), adjusted by apply_levels().
_stream_handler: logging.Handler | None = None
_record_handlers: list[logging.Handler] = []


def _parse_level(raw: str | None, default: int) -> int:
    level = logging.getLevelName((raw or "").strip().upper()) if raw else None
    return level if isinstance(level, int) else default


def stdout_level(configured: str | None = None) -> int:
    """Level of the container output: configuration, else environment, else INFO."""
    return _parse_level(configured, _parse_level(os.environ.get(STDOUT_LEVEL_ENV), logging.INFO))


def record_level(configured: str | None = None) -> int:
    """Level of the UI log and the log file: configuration, else environment, else INFO."""
    return _parse_level(configured, _parse_level(os.environ.get(LEVEL_ENV), logging.INFO))


def apply_levels(level: str | None = None, stdout: str | None = None) -> tuple[str, str]:
    """Set the levels at runtime (``None``/empty = environment default).

    Returns the effective level names (record, stdout)."""
    record, out = record_level(level), stdout_level(stdout)
    for handler in _record_handlers:
        handler.setLevel(record)
    if _stream_handler is not None:
        _stream_handler.setLevel(out)
    # DEBUG only for our own loggers; libraries (satpy, urllib3, ...) stay at INFO.
    lowest = min(record, out)
    logging.getLogger(APP_LOGGER).setLevel(logging.DEBUG if lowest < logging.INFO else logging.NOTSET)
    return logging.getLevelName(record), logging.getLevelName(out)


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = 1000) -> None:
        super().__init__(level=record_level())
        self._entries: deque[dict] = deque(maxlen=capacity)
        self._next_id = 1
        self._entries_lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_IGNORED_LOGGERS)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            with self._entries_lock:
                self._entries.append(
                    {
                        "id": self._next_id,
                        "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": message,
                    }
                )
                self._next_id += 1
        except Exception:  # pragma: no cover - logging must never crash
            self.handleError(record)

    def entries(self, after: int = 0, limit: int = 500) -> list[dict]:
        with self._entries_lock:
            result = [entry for entry in self._entries if entry["id"] > after]
        return result[-limit:] if limit > 0 else result


def install(capacity: int = 1000, log_dir: Path | None = None) -> LogBuffer:
    """Attach the buffer (and optionally a log file) to the root and uvicorn loggers."""
    global _stream_handler
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        stream.setLevel(stdout_level())
        root.addHandler(stream)
        _stream_handler = stream

    # APScheduler logs every job run at INFO - that drowns out the actual
    # render messages in the UI.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    buffer = LogBuffer(capacity)
    handlers: list[logging.Handler] = [buffer]
    if log_dir is not None:
        try:
            # Only the "logs" directory itself: a missing data directory
            # (disk not mounted) must not be created here.
            log_dir.mkdir(exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "ha_satimage.log", maxBytes=FILE_MAX_BYTES,
                backupCount=FILE_BACKUPS, encoding="utf-8",
            )
            file_handler.setLevel(record_level())
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            file_handler.addFilter(buffer.filter)
            handlers.append(file_handler)
        except OSError as exc:
            logging.getLogger(__name__).warning("Cannot write log file in %s: %s", log_dir, exc)

    _record_handlers[:] = handlers
    apply_levels()
    for handler in handlers:
        root.addHandler(handler)
        # uvicorn.error does not propagate to the root logger.
        logging.getLogger("uvicorn.error").addHandler(handler)
    return buffer
