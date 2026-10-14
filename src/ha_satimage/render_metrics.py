"""Bounded, in-memory render telemetry; RSS is sampled without loading Satpy."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
SAMPLE_INTERVAL = 0.1
MAX_GROUPS = 256
_labels = ContextVar("render_metric_labels", default={})


def rss_bytes(pid: int, descendants: bool = False) -> int | None:
    """Linux RSS (shared pages counted per process), optionally including children."""
    try:
        fields = Path(f"/proc/{pid}/statm").read_text().split()
        total = int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None
    if descendants:
        try:
            children = set()
            for task in Path(f"/proc/{pid}/task").iterdir():
                try:
                    children.update(task.joinpath("children").read_text().split())
                except OSError:
                    continue
        except OSError:
            children = set()
        for child in children:
            total += rss_bytes(int(child), True) or 0
    return total


class RenderMetrics:
    def __init__(self):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._lock = threading.Lock()
        self._groups: dict[tuple, dict] = {}

    @contextmanager
    def measure(self, kind, *, source="", region="", composite="", pid=None,
                descendants=False, skip_exceptions=()):
        labels = {**_labels.get(), "kind": kind}
        labels.update({k: v for k, v in (
            ("source", source), ("region", region), ("composite", composite)
        ) if v})
        labels = {k: str(labels.get(k, ""))[:128]
                  for k in ("kind", "source", "region", "composite")}
        key = tuple(labels.values())
        with self._lock:
            if key not in self._groups and len(self._groups) >= MAX_GROUPS:
                labels = dict(kind="overflow", source="", region="", composite="")
                key = tuple(labels.values())
            row = self._groups.setdefault(key, {
                **labels, "runs": 0, "failures": 0, "skipped": 0, "active": 0,
                "rss_samples": 0, "rss_sum_bytes": 0,
                "rss_min_bytes": None, "rss_max_bytes": None,
                "duration_sum_seconds": 0.0, "duration_min_seconds": None,
                "duration_max_seconds": None, "last_error": None,
            })
            row["active"] += 1
        token = _labels.set(labels)
        stop = threading.Event()
        pid = os.getpid() if pid is None else pid

        def sample():
            target = pid() if callable(pid) else pid
            value = rss_bytes(target, descendants) if target else None
            if value is not None:
                with self._lock:
                    row["rss_samples"] += 1
                    row["rss_sum_bytes"] += value
                    row["rss_min_bytes"] = (
                        value if row["rss_min_bytes"] is None else min(row["rss_min_bytes"], value))
                    row["rss_max_bytes"] = (
                        value if row["rss_max_bytes"] is None else max(row["rss_max_bytes"], value))

        def poll():
            while not stop.wait(SAMPLE_INTERVAL):
                sample()

        started = time.monotonic()
        sample()
        thread = threading.Thread(target=poll, name="render-rss", daemon=True)
        thread.start()
        error = None
        skipped = False
        try:
            yield
        except BaseException as exc:
            skipped = isinstance(exc, skip_exceptions)
            if not skipped:
                # Do not retain exception text, which can contain credentials/paths.
                error = type(exc).__name__
            raise
        finally:
            stop.set()
            thread.join()
            sample()
            duration = time.monotonic() - started
            with self._lock:
                row["active"] -= 1
                row["runs"] += 1
                row["failures"] += int(error is not None)
                row["skipped"] += int(skipped)
                row["duration_sum_seconds"] += duration
                row["duration_min_seconds"] = min(
                    row["duration_min_seconds"] or duration, duration)
                row["duration_max_seconds"] = max(
                    row["duration_max_seconds"] or duration, duration)
                row["last_error"] = error
            _labels.reset(token)
            logger.info("Render metrics %s/%s/%s/%s: %.2fs, %s",
                        *labels.values(), duration, error or ("skipped" if skipped else "ok"))

    def snapshot(self):
        with self._lock:
            rows = [dict(row) for row in self._groups.values()]
        for row in rows:
            row["rss_avg_bytes"] = (
                row["rss_sum_bytes"] / row["rss_samples"] if row["rss_samples"] else None)
            row["duration_avg_seconds"] = (
                row["duration_sum_seconds"] / row["runs"] if row["runs"] else None)
        return {"started_at": self.started_at, "sample_interval_seconds": SAMPLE_INTERVAL,
                "processes": rows}

    def prometheus(self):
        rows = self.snapshot()["processes"]
        lines = []
        fields = {
            "runs": ("attempts_total", "counter"),
            "failures": ("failures_total", "counter"),
            "skipped": ("skipped_total", "counter"),
            "active": ("active", "gauge"),
            "rss_samples": ("rss_samples_total", "counter"),
            "rss_sum_bytes": ("rss_sample_bytes_total", "counter"),
            **{f"rss_{s}_bytes": (f"rss_{s}_bytes", "gauge") for s in ("min", "avg", "max")},
            "duration_sum_seconds": ("duration_seconds_total", "counter"),
            **{f"duration_{s}_seconds": (f"duration_{s}_seconds", "gauge")
               for s in ("min", "avg", "max")},
        }
        for field, (suffix, metric_type) in fields.items():
            name = f"ha_satimage_render_{suffix}"
            lines.extend([f"# HELP {name} Render {field}.", f"# TYPE {name} {metric_type}"])
            for row in rows:
                if row[field] is not None:
                    labels = ",".join(f'{k}="{_escape_label(row[k])}"'
                                      for k in ("kind", "source", "region", "composite"))
                    lines.append(f"{name}{{{labels}}} {row[field]}")
        return "\n".join(lines) + "\n"


def _escape_label(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


render_metrics = RenderMetrics()
