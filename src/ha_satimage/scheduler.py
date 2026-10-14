"""Non-blocking scheduler: download at the capture cycle, render afterwards.

Uses APScheduler with its own thread pool so the FastAPI web server
(asyncio) never waits for downloads or rendering.

- **Download** per active source with a download driver (``msg_seviri``,
  ``mtg_fci``) at the source's cycle (Rapid Scan 5 min, FCI 10 min, 0° 15
  min; ``SourcesConfig.cycle_for``). Timing follows the last capture:
  end of capture + cycle + observed delivery latency. If the product is
  not there yet, it is polled every minute. Downloads do not hold the
  render lock (I/O only).
- **Rendering** is triggered by a new capture (for all regions of the
  source) and uses the product already stored locally. A global lock
  ensures only one render runs at a time ("one render at a time") -
  important on small hosts that share resources with other services.
  Sources without downloads (placeholders) keep rendering at a fixed
  interval.
- **Maintenance:** during a scheduled EUMETSAT maintenance window
  (``maintenance.py``) affected sources are not queried; their regions
  show a black "Currently unavailable due to maintenance" frame instead.
  Once per hour the source is still queried to verify it is really down;
  if it delivers a new capture, the window is ended early for that source.
- **Archive** ("download all sources") renders every region in every image
  type after a new capture. It runs in its own single-thread executor, at
  most one queued run per source, and yields the render lock to waiting
  preview renders after every image - the preview of the regions never
  waits for a long archive run.
- After each new frame, thumbnail, JPEG and animations are pre-generated
  (``media.prewarm``) so playback starts immediately.

Errors (missing slots, API timeouts, invalid credentials) are logged and
recorded in the StatusStore but never crash the service; the next
scheduled run retries (simple retry).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from ha_satimage import media
from ha_satimage.archive import get_render_archive, region_signature
from ha_satimage.buffer import BufferManager
from ha_satimage.config import (
    DOWNLOAD_DRIVERS,
    AppConfig,
    ConfigStore,
    SourceDefinition,
)
from ha_satimage.maintenance import (
    MAINTENANCE_COMPOSITE,
    MaintenanceSchedule,
    Window,
    render_maintenance_png,
)
from ha_satimage.sources import (
    SOURCE_REGISTRY,
    NoNewData,
    RenderError,
    archive_composites,
    get_source,
    archive_root,
    render_archive_root,
    render_fci_slot,
    render_with,
)
from ha_satimage.resources import HEAVY_WORK_LOCK
from ha_satimage.status import StatusStore

if TYPE_CHECKING:
    from ha_satimage.source_sync import SourceSync

logger = logging.getLogger(__name__)

JOB_ID_PREFIX = "render-"
DOWNLOAD_JOB_PREFIX = "download-"
MEDIA_JOB_PREFIX = "media-"
ARCHIVE_JOB_PREFIX = "archive-"
SYNC_JOB_ID = "source-sync"
# Archive renders run in their own executor so they never occupy the
# threads of the preview renders and downloads.
ARCHIVE_EXECUTOR = "archive"
DOWNLOAD_EXECUTOR = "download"
MEDIA_EXECUTOR = "media"
# Download (~100 MB) + rendering one region stays well below this.
LOCK_TIMEOUT_SECONDS = 15 * 60
# Download jobs check at this interval whether they are due (no network).
TICK_SECONDS = 20
# Expected product not there yet: poll again this often.
RETRY_INTERVAL = timedelta(minutes=1)
# After an error (auth, timeout, ...) retry at the latest after this.
ERROR_RETRY = timedelta(minutes=5)
MIN_GAP = timedelta(seconds=15)
# During maintenance: query the source this often to verify it is still down.
MAINTENANCE_PROBE_INTERVAL = timedelta(hours=1)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class DownloadState:
    """Cycle state of a source (in memory only)."""

    due: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc))
    last_sensing: datetime | None = None
    last_check_at: datetime | None = None
    last_new_at: datetime | None = None
    next_expected: datetime | None = None
    last_error: str | None = None
    # Active maintenance window (no Data Store queries meanwhile).
    maintenance: Window | None = None
    # Hourly verification query during maintenance.
    next_probe_at: datetime | None = None
    last_probe_at: datetime | None = None
    last_probe_result: str | None = None
    # Window during which the source delivered again (ended early).
    maintenance_override: Window | None = None
    # Delay between end of capture and availability (recent products);
    # the minimum approximates the real delivery latency.
    latencies: deque = field(default_factory=lambda: deque(maxlen=12))

    def latency(self) -> timedelta:
        return min(self.latencies) if self.latencies else timedelta(0)


class RenderScheduler:
    def __init__(
        self,
        config_store: ConfigStore,
        buffer_manager: BufferManager,
        status_store: StatusStore,
        source_sync: "SourceSync | None" = None,
        maintenance: MaintenanceSchedule | None = None,
    ) -> None:
        self._config_store = config_store
        self._buffers = buffer_manager
        self._status = status_store
        self._source_sync = source_sync
        self._maintenance = maintenance
        self._render_lock = HEAVY_WORK_LOCK
        # Preview renders waiting for the render lock (archive renders yield).
        self._preview_waiting = 0
        self._preview_lock = threading.Lock()
        # Sources with an archive run queued but not started yet.
        self._archive_queued: set[str] = set()
        self._scheduler = BackgroundScheduler(executors={
            "default": ThreadPoolExecutor(10),
            ARCHIVE_EXECUTOR: ThreadPoolExecutor(1),
            DOWNLOAD_EXECUTOR: ThreadPoolExecutor(2),
            MEDIA_EXECUTOR: ThreadPoolExecutor(1),
        })
        logger.info(
            "Resource protection: one render/animation at a time; "
            "download workers=2, media workers=1"
        )
        # Region -> settings at the last setup (detects changes).
        self._signatures: dict[str, tuple[int, str]] = {}
        self._downloads: dict[str, DownloadState] = {}
        self._download_lock = threading.Lock()
        # Source -> lock of its running FCI archive run (one at a time).
        self._archive_locks: dict[str, threading.Lock] = {}
        self._job_counter = itertools.count()
        self._initialized = False

    def start(self) -> None:
        self._reschedule()
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def reload(self) -> None:
        """After configuration changes: set up jobs again."""
        self._reschedule()

    @contextmanager
    def exclusive(self, timeout: float = 120.0):
        """Block render runs (e.g. while changing the storage location)."""
        if not self._render_lock.acquire(timeout=timeout):
            raise TimeoutError("A render is still running, please try again later")
        try:
            yield
        finally:
            self._render_lock.release()

    def _acquire_for_archive(self, timeout: float) -> bool:
        """Render lock for one archive image; waiting preview renders go first."""
        deadline = time.monotonic() + timeout
        while True:
            if self._preview_waiting == 0 and self._render_lock.acquire(timeout=0.5):
                if self._preview_waiting == 0:
                    return True
                self._render_lock.release()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    # -- Setup ---------------------------------------------------------------
    @staticmethod
    def _downloads_for(driver: str) -> bool:
        cls = SOURCE_REGISTRY.get(driver)
        return driver in DOWNLOAD_DRIVERS and cls is not None and cls.downloads

    def download_sources(self, config: AppConfig) -> list[str]:
        """Sources with their own download job.

        Normally only sources a region actually uses (plus raw data
        archives). With ``archive.render_all`` **every** enabled source with
        a download driver runs at its own cycle, so the archive holds all
        sources in all image types.
        """
        used = {region.source for region in config.regions}
        render_all = config.archive.render_all and bool(config.regions)
        return [
            entry.id
            for entry in config.sources.catalog
            if entry.enabled
            and self._downloads_for(entry.driver)
            and (
                render_all
                or entry.id in used
                or SOURCE_REGISTRY[entry.driver].archive_always
            )
        ]

    def _reschedule(self) -> None:
        previous_runs: dict[str, datetime | None] = {}
        for job in self._scheduler.get_jobs():
            if job.id.startswith((JOB_ID_PREFIX, DOWNLOAD_JOB_PREFIX)) or job.id == SYNC_JOB_ID:
                if job.id.startswith(f"{JOB_ID_PREFIX}now-"):
                    continue  # pending one-off renders stay
                previous_runs[job.id] = getattr(job, "next_run_time", None)
                job.remove()
        config = self._config_store.get()
        self._schedule_sync(config.sources.auto_sync_hours)
        now = datetime.now(timezone.utc)

        download_ids = self.download_sources(config)
        for source_id in download_ids:
            self._downloads.setdefault(source_id, DownloadState())
            self._scheduler.add_job(
                self._tick_download,
                "interval",
                seconds=TICK_SECONDS,
                args=[source_id],
                id=f"{DOWNLOAD_JOB_PREFIX}{source_id}",
                executor=DOWNLOAD_EXECUTOR,
                next_run_time=now,
                max_instances=1,
                coalesce=True,
            )
        cycles = ", ".join(
            f"{sid} {config.sources.cycle_for(sid)} min" for sid in download_ids
        ) or "none"

        for region in config.regions:
            if not config.sources.is_enabled(region.source):
                logger.info(
                    "Region %s: source '%s' is disabled - no automatic runs",
                    region.name, region.source,
                )
                self._signatures.pop(region.name, None)
                continue
            interval = config.sources.cycle_for(region.source)
            signature = (interval, region.model_dump_json())
            changed = self._signatures.get(region.name) != signature
            known = region.name in self._signatures
            self._signatures[region.name] = signature
            if region.source in download_ids:
                # Rendering happens once the download has a new capture.
                # Changed/new regions render immediately (at startup the
                # first download takes care of it).
                if changed and self._initialized:
                    if known:
                        logger.info("Region %s changed - rendering immediately", region.name)
                    self._queue_render(region.name)
                continue
            # Sources without download (placeholders): fixed interval.
            job_id = f"{JOB_ID_PREFIX}{region.name}"
            next_run = previous_runs.get(job_id)
            if next_run is None or changed:
                if known and changed:
                    logger.info("Region %s changed - rendering immediately", region.name)
                next_run = now
            self._scheduler.add_job(
                self._run_region,
                "interval",
                minutes=interval,
                args=[region.name],
                id=job_id,
                next_run_time=next_run,
                max_instances=1,
                coalesce=True,
            )
        if not self._initialized:
            # Upgrades can change overlay signatures without an API save.
            # With "download all sources" missing images are rendered as well
            # (e.g. FCI raw slots retained over a restart).
            existing = get_render_archive(render_archive_root(config))
            refresh = {
                region.name for region in config.regions
                if region.name in existing.regions() or config.archive.render_all
            }
            if refresh:
                self.refresh_archived_regions(refresh)
        self._initialized = True
        logger.info(
            "Scheduler reconfigured: download cycles %s; otherwise poll interval %d min",
            cycles, config.sources.poll_interval_minutes,
        )

    def _schedule_sync(self, hours: int) -> None:
        if self._source_sync is None or hours <= 0:
            return
        last = self._source_sync.last_run_at()
        now = datetime.now(timezone.utc)
        first = now + timedelta(seconds=30)
        if last is not None and last + timedelta(hours=hours) > first:
            first = last + timedelta(hours=hours)
        self._scheduler.add_job(
            self._source_sync.run,
            "interval",
            hours=hours,
            id=SYNC_JOB_ID,
            next_run_time=first,
            max_instances=1,
            coalesce=True,
        )

    # -- Maintenance -----------------------------------------------------------
    def _maintenance_window(self, config: AppConfig, entry: SourceDefinition | None) -> Window | None:
        """Active maintenance window, unless the source proved to be back early."""
        if self._maintenance is None or entry is None or entry.driver != "msg_seviri":
            return None
        window = self._maintenance.window_for(
            entry.collection, config.sources.maintenance_check_hours
        )
        state = self._downloads.get(entry.id)
        if window is not None and state is not None and state.maintenance_override == window:
            return None
        return window

    def _probe_during_maintenance(
        self, config: AppConfig, entry: SourceDefinition, state: DownloadState, window: Window
    ) -> datetime | None:
        """Hourly query to verify the source is still down.

        Returns the sensing time of a new capture if the source delivers
        again (the window is then ended early for this source), else ``None``.
        Failures are expected during maintenance and only logged at INFO.
        """
        now = datetime.now(timezone.utc)
        if state.maintenance != window or state.next_probe_at is None:
            # Window just started: first verification after one interval.
            state.next_probe_at = now + MAINTENANCE_PROBE_INTERVAL
            state.last_probe_at = None
            state.last_probe_result = None
            return None
        if now < state.next_probe_at:
            return None
        state.next_probe_at = now + MAINTENANCE_PROBE_INTERVAL
        state.last_probe_at = now
        try:
            sensing = get_source(entry.driver).fetch(entry, config)
        except Exception as exc:
            state.last_probe_result = "down"
            logger.info(
                "Source %s: maintenance check - still unavailable (%s), next check %s",
                entry.id, exc, state.next_probe_at.strftime("%H:%M"),
            )
            return None
        if sensing.tzinfo is None:
            sensing = sensing.replace(tzinfo=timezone.utc)
        baseline = max(state.last_sensing or window.start, window.start)
        if sensing <= baseline:
            state.last_probe_result = "down"
            logger.info(
                "Source %s: maintenance check - no new capture (newest %s), next check %s",
                entry.id, sensing.strftime("%Y-%m-%d %H:%M"), state.next_probe_at.strftime("%H:%M"),
            )
            return None
        state.last_probe_result = "up"
        state.maintenance_override = window
        logger.info(
            "Source %s: new capture %s although maintenance is announced until %s - "
            "resuming downloads",
            entry.id, sensing.strftime("%Y-%m-%d %H:%M"), window.end.strftime("%Y-%m-%d %H:%M UTC"),
        )
        return sensing

    def _pause_for_maintenance(
        self, config: AppConfig, entry: SourceDefinition, state: DownloadState, window: Window
    ) -> None:
        """Skip the Data Store query; show the maintenance frame once per window."""
        now = datetime.now(timezone.utc)
        cycle = timedelta(minutes=config.sources.cycle_for(entry.id))
        state.last_check_at = now
        state.last_error = None
        state.next_expected = window.end
        # Re-check at the cycle (cheap, no network) in case the schedule changes.
        due = min(window.end, now + cycle)
        if state.next_probe_at is not None:
            due = min(due, state.next_probe_at)
        state.due = max(due, now + MIN_GAP)
        if state.maintenance == window:
            return
        state.maintenance = window
        regions = [
            r.name for r in config.regions
            if r.source == entry.id and config.sources.is_enabled(r.source)
        ]
        logger.info(
            "Source %s: scheduled maintenance %s - Data Store queries paused, "
            "showing maintenance image for %s",
            entry.id, window.describe(), ", ".join(regions) or "no region",
        )
        for name in regions:
            self._queue_render(name)

    # -- Download --------------------------------------------------------------
    def _tick_download(self, source_id: str) -> None:
        state = self._downloads.setdefault(source_id, DownloadState())
        if datetime.now(timezone.utc) < state.due:
            return
        self.download(source_id)

    def download(self, source_id: str) -> bool:
        """Fetch the source's newest capture; render regions on a new capture.

        Returns ``True`` if a new capture is available.
        """
        config = self._config_store.get()
        entry = config.sources.get(source_id)
        if entry is None or not entry.enabled or not self._downloads_for(entry.driver):
            return False
        with self._download_lock:
            state = self._downloads.setdefault(source_id, DownloadState())
        window = self._maintenance_window(config, entry)
        probed = None
        if window is not None:
            probed = self._probe_during_maintenance(config, entry, state, window)
            if probed is None:
                self._pause_for_maintenance(config, entry, state, window)
                return False
        if state.maintenance is not None:
            if probed is None:
                logger.info("Source %s: maintenance over - resuming downloads", source_id)
            state.maintenance = None
            state.next_probe_at = None
        cycle = timedelta(minutes=config.sources.cycle_for(source_id))
        checked = datetime.now(timezone.utc)
        state.last_check_at = checked
        try:
            sensing = probed or get_source(entry.driver).fetch(entry, config)
        except Exception as exc:
            if isinstance(exc, RenderError):
                logger.error("Download %s failed: %s", source_id, exc)
            else:  # pragma: no cover - defensive
                logger.exception("Download %s: unexpected error", source_id)
            state.last_error = str(exc)
            state.due = checked + min(cycle, ERROR_RETRY)
            return False
        if sensing.tzinfo is None:
            sensing = sensing.replace(tzinfo=timezone.utc)
        state.last_error = None
        new = state.last_sensing is None or sensing > state.last_sensing
        if new:
            state.latencies.append(max(checked - sensing, timedelta(0)))
            state.last_sensing = sensing
            state.last_new_at = checked
        expected = state.last_sensing + cycle + state.latency()
        now = datetime.now(timezone.utc)
        if expected > now:
            due = expected
        elif now - expected < cycle:
            due = now + RETRY_INTERVAL
        else:
            due = now + cycle  # data stream stalled: don't poll every minute
        state.due = max(due, now + MIN_GAP)
        state.next_expected = expected
        if new:
            regions = [
                r.name for r in config.regions
                if r.source == source_id and config.sources.is_enabled(r.source)
            ]
            logger.info(
                "Download %s: new capture %s (cycle %d min, latency %d s), "
                "next expected %s, rendering %s",
                source_id, sensing.strftime("%H:%M"), cycle.total_seconds() // 60,
                state.latency().total_seconds(), expected.strftime("%H:%M:%S"),
                ", ".join(regions) or "no region",
            )
            for name in regions:
                self._queue_render(name)
            if config.archive.render_all:
                self._queue_archive(source_id, sensing)
        else:
            logger.debug("Download %s: no new capture yet", source_id)
        return new

    def download_status(self) -> dict[str, dict]:
        config = self._config_store.get()
        active = set(self.download_sources(config))
        result = {}
        for entry in config.sources.catalog:
            state = self._downloads.get(entry.id)
            result[entry.id] = {
                "cycle_minutes": config.sources.cycle_for(entry.id),
                "cycle_auto": entry.cycle_minutes is None,
                "downloads": self._downloads_for(entry.driver),
                "active": entry.id in active,
                "last_sensing": _iso(state.last_sensing) if state else None,
                "last_check_at": _iso(state.last_check_at) if state else None,
                "next_check_at": _iso(state.due) if state and entry.id in active
                and state.last_check_at else None,
                "next_expected": _iso(state.next_expected) if state else None,
                "latency_seconds": int(state.latency().total_seconds())
                if state and state.latencies else None,
                "last_error": state.last_error if state else None,
                "maintenance": self._maintenance_info(config, entry),
            }
        return result

    def _maintenance_info(self, config: AppConfig, entry: SourceDefinition) -> dict | None:
        """Active or next maintenance window of the source (no fetch)."""
        if self._maintenance is None or entry.driver != "msg_seviri":
            return None
        window = self._maintenance.next_window_for(
            entry.collection, config.sources.maintenance_check_hours
        )
        if window is None:
            return None
        state = self._downloads.get(entry.id)
        overridden = state is not None and state.maintenance_override == window
        paused = state is not None and state.maintenance == window
        return {
            **window.as_dict(),
            "active": window.active(datetime.now(timezone.utc)) and not overridden,
            "ended_early": overridden,
            "last_probe_at": _iso(state.last_probe_at) if state else None,
            "last_probe_result": state.last_probe_result if state else None,
            "next_probe_at": _iso(state.next_probe_at) if paused else None,
        }

    # -- Render ---------------------------------------------------------------
    def _queue_render(self, region_name: str) -> None:
        self._scheduler.add_job(
            self._run_region,
            args=[region_name],
            id=f"{JOB_ID_PREFIX}now-{region_name}-{next(self._job_counter)}",
            misfire_grace_time=None,
        )

    def trigger_now(self, region_name: str) -> None:
        """For the UI button "Refresh now"."""
        self._scheduler.add_job(
            self._run_region,
            args=[region_name],
            id=f"manual-{region_name}-{next(self._job_counter)}",
            max_instances=1,
        )

    # -- Archive ("download all sources") --------------------------------------
    def _queue_archive(self, source_id: str, sensing: datetime) -> None:
        """Queue an archive run; one pending run per source is enough.

        A queued run renders the newest capture (FCI: every raw slot still
        missing), so further triggers before it started add nothing.
        """
        with self._download_lock:
            if source_id in self._archive_queued:
                logger.debug("Archive %s: run already queued", source_id)
                return
            self._archive_queued.add(source_id)
        self._scheduler.add_job(
            self._run_queued_archive,
            args=[source_id, sensing],
            id=f"{ARCHIVE_JOB_PREFIX}{source_id}-{next(self._job_counter)}",
            executor=ARCHIVE_EXECUTOR,
            misfire_grace_time=None,
        )

    def _run_queued_archive(self, source_id: str, sensing: datetime) -> int:
        with self._download_lock:
            self._archive_queued.discard(source_id)
        state = self._downloads.get(source_id)
        if state is not None and state.last_sensing is not None and state.last_sensing > sensing:
            sensing = state.last_sensing
        return self.archive_capture(source_id, sensing)

    def archive_sources(self, config: AppConfig) -> list[str]:
        """Enabled sources that are archived (all image types per region)."""
        return [entry.id for entry in config.sources.catalog if entry.enabled]

    def archive_all_now(self) -> int:
        """Archive the newest capture of every enabled source (UI button)."""
        config = self._config_store.get()
        sources = self.archive_sources(config)
        for source_id in sources:
            state = self._downloads.get(source_id)
            sensing = state.last_sensing if state else None
            self._scheduler.add_job(
                self._archive_source,
                args=[source_id, sensing],
                id=f"{ARCHIVE_JOB_PREFIX}manual-{source_id}-{next(self._job_counter)}",
                executor=ARCHIVE_EXECUTOR,
                misfire_grace_time=None,
            )
        logger.info("Archive run requested for %s", ", ".join(sources) or "no source")
        return len(sources)

    def refresh_archived_regions(self, region_names: set[str]) -> None:
        """Update archived images after a region's cut-out or borders changed."""
        self._scheduler.add_job(
            self._refresh_archived_regions,
            args=[region_names],
            id=f"{ARCHIVE_JOB_PREFIX}borders-{next(self._job_counter)}",
            executor=ARCHIVE_EXECUTOR,
            misfire_grace_time=None,
        )

    def _refresh_archived_regions(self, region_names: set[str]) -> None:
        """Sync overlays in place; with "download all sources" fill the gaps.

        Without ``archive.render_all`` nothing is rendered - the archive only
        holds images created right after a download.
        """
        config = self._config_store.get()
        archive = get_render_archive(render_archive_root(config))
        regions = {region.name: region for region in config.regions}
        with self.exclusive(timeout=LOCK_TIMEOUT_SECONDS):
            for name in region_names:
                region = regions.get(name)
                if region is None:
                    continue
                archive.sync_region(name, region_signature(region))
        if not config.archive.render_all:
            return
        for source_id in sorted(self.archive_sources(config)):
            state = self._downloads.get(source_id)
            self._archive_source(
                source_id, state.last_sensing if state else None, allow_disabled=True
            )

    def _archive_source(
        self, source_id: str, sensing: datetime | None, allow_disabled: bool = False
    ) -> None:
        """Fetch the capture first if it is not known yet, then archive it."""
        config = self._config_store.get()
        entry = config.sources.get(source_id)
        if entry is None or (not entry.enabled and not allow_disabled):
            return
        if not self._downloads_for(entry.driver):
            # Placeholder sources have no capture time of their own.
            self.archive_capture(
                source_id, datetime.now(timezone.utc), allow_disabled=allow_disabled
            )
            return
        if allow_disabled and entry.driver == "mtg_fci":
            self.archive_capture(
                source_id, sensing or datetime.now(timezone.utc), allow_disabled=True
            )
            return
        if sensing is None:
            if self._maintenance_window(config, entry) is not None:
                logger.info("Archive %s skipped: source under maintenance", source_id)
                return
            if allow_disabled:
                try:
                    sensing = get_source(entry.driver).fetch(entry, config)
                except Exception as exc:
                    logger.error("Archive refresh %s could not fetch a capture: %s", source_id, exc)
                    return
            else:
                self.download(source_id)
                state = self._downloads.get(source_id)
                sensing = state.last_sensing if state else None
            if sensing is None:
                return
            # A fresh download already queued the archive run when render_all is on.
            if config.archive.render_all and not allow_disabled:
                return
        self.archive_capture(source_id, sensing, allow_disabled=allow_disabled)

    def archive_capture(
        self, source_id: str, sensing: datetime, allow_disabled: bool = False
    ) -> int:
        """Render every region in every image type of the source (one at a time).

        Runs under the global render lock like any other render, so it never
        competes with the scheduled runs for memory.
        """
        config = self._config_store.get()
        entry = config.sources.get(source_id)
        if entry is None or (not entry.enabled and not allow_disabled) or not config.regions:
            return 0
        archive = get_render_archive(render_archive_root(config))
        composites = archive_composites(entry)
        if entry.driver == "mtg_fci":
            stored = self._archive_fci_slots(entry, config, archive, composites)
        else:
            stored = self._archive_newest(entry, config, archive, composites, sensing)
        if stored:
            archive.forget({r.name for r in config.regions})
            archive.prune(
                config.archive.render_retention_hours,
                config.archive.render_max_storage_mb,
            )
        return stored

    def _archive_newest(self, entry, config, archive, composites, sensing) -> int:
        """Render the newest capture of a source in every region and image type."""
        source_id = entry.id
        stored = 0
        for region in config.regions:
            with self.exclusive(timeout=LOCK_TIMEOUT_SECONDS):
                if self._config_store.get() is not config:
                    return stored
                archive.sync_region(region.name, region_signature(region))
            for composite in composites:
                if archive.has(region.name, source_id, composite, sensing):
                    continue
                if not self._acquire_for_archive(LOCK_TIMEOUT_SECONDS):
                    logger.warning("Archive %s: render lock busy, skipping", source_id)
                    return stored
                started = time.monotonic()
                try:
                    if self._config_store.get() is not config:
                        return stored
                    rendered = render_with(entry, region, composite, config)
                    if self._config_store.get() is not config:
                        return stored
                    image = archive.store(
                        region.name, source_id, composite, rendered.sensing_time, rendered.png
                    )
                except NoNewData:
                    continue
                except RenderError as exc:
                    logger.error(
                        "Archive %s / %s / %s failed: %s",
                        region.name, source_id, composite, exc,
                    )
                    continue
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "Archive %s / %s / %s: unexpected error",
                        region.name, source_id, composite,
                    )
                    continue
                finally:
                    self._render_lock.release()
                if image is not None:
                    stored += 1
                    logger.info(
                        "Archived %s / %s / %s %s in %.1f s (%d KB)",
                        region.name, source_id, composite, image.slot,
                        time.monotonic() - started, len(rendered.png) // 1024,
                    )
        return stored

    def _archive_fci_slots(self, entry, config, archive, composites) -> int:
        """Render **every** archived FCI raw slot that still lacks images.

        Not only the newest capture: slots missed while an archive run lagged
        behind, the render lock was busy, the service restarted or before
        "download all sources" was enabled are rendered as well, each from
        its own slot. Newest slots first; the slot list is re-read after
        every image, so captures arriving meanwhile are picked up by the
        running loop (a second run for the same source just returns).
        """
        from ha_satimage.config import DEFAULT_FCI_COLLECTION
        from ha_satimage.sources.fci_archive import chunks_for_region, get_archive

        with self._download_lock:
            guard = self._archive_locks.setdefault(entry.id, threading.Lock())
        if not guard.acquire(blocking=False):
            logger.debug("Archive %s: run already in progress", entry.id)
            return 0
        try:
            raw = get_archive(archive_root(config))
            collection = entry.collection or DEFAULT_FCI_COLLECTION
            retention = config.archive.render_retention_hours
            needed = {region.name: chunks_for_region(region) for region in config.regions}
            with self.exclusive(timeout=LOCK_TIMEOUT_SECONDS):
                if self._config_store.get() is not config:
                    return 0
                for region in config.regions:
                    archive.sync_region(region.name, region_signature(region))
            # Combinations already rendered, existing or failed in this run
            # (keeps the re-scan after every image cheap).
            handled: set[tuple[str, str, str]] = set()

            def next_task():
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(hours=retention)
                    if retention > 0 else None
                )
                for slot in raw.slots(collection):
                    if cutoff is not None and slot.sensing_end < cutoff:
                        break  # slots are sorted newest first
                    chunks = set(slot.chunks)
                    for region in config.regions:
                        if not needed[region.name] or not needed[region.name] <= chunks:
                            continue
                        for composite in composites:
                            key = (slot.name, region.name, composite)
                            if key in handled:
                                continue
                            handled.add(key)
                            if not archive.has(region.name, entry.id, composite, slot.archive_time):
                                return slot, region, composite
                return None

            stored = 0
            while (task := next_task()) is not None:
                if self._config_store.get() is not config:
                    # Regions/sources changed: the next archive run continues.
                    logger.info("Archive %s: configuration changed, stopping this run", entry.id)
                    break
                slot, region, composite = task
                if not self._acquire_for_archive(LOCK_TIMEOUT_SECONDS):
                    logger.warning("Archive %s: render lock busy, skipping", entry.id)
                    break
                started = time.monotonic()
                try:
                    if archive.has(region.name, entry.id, composite, slot.archive_time):
                        continue  # rendered on request meanwhile; finally releases the lock
                    rendered = render_fci_slot(slot, region, composite, collection)
                    if self._config_store.get() is not config:
                        break
                    # Named after the raw slot, so the viewer finds it as pre-rendered.
                    image = archive.store(region.name, entry.id, composite, slot.archive_time, rendered.png)
                except (RenderError, FileNotFoundError) as exc:
                    logger.error(
                        "Archive %s / %s / %s %s failed: %s",
                        region.name, entry.id, composite, slot.name, exc,
                    )
                    continue
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "Archive %s / %s / %s %s: unexpected error",
                        region.name, entry.id, composite, slot.name,
                    )
                    continue
                finally:
                    self._render_lock.release()
                if image is None:
                    continue
                stored += 1
                logger.info(
                    "Archived %s / %s / %s %s in %.1f s (%d KB)",
                    region.name, entry.id, composite, image.slot,
                    time.monotonic() - started, len(rendered.png) // 1024,
                )
            return stored
        finally:
            guard.release()

    def _next_run_for(self, config: AppConfig, source_id: str) -> datetime:
        state = self._downloads.get(source_id)
        if state is not None and state.next_expected is not None:
            return max(state.next_expected, datetime.now(timezone.utc))
        return datetime.now(timezone.utc) + timedelta(minutes=config.sources.cycle_for(source_id))

    def _run_region(self, region_name: str) -> None:
        config = self._config_store.get()
        region = config.region(region_name)
        if region is None:
            logger.error("Region %s no longer exists, skipping run", region_name)
            return

        next_run_at = self._next_run_for(config, region.source)
        new_frame = None
        # Checked before taking the lock: may fetch the schedule page.
        window = self._maintenance_window(config, config.sources.get(region.source))

        with self._preview_lock:
            self._preview_waiting += 1
        try:
            acquired = self._render_lock.acquire(timeout=LOCK_TIMEOUT_SECONDS)
        finally:
            with self._preview_lock:
                self._preview_waiting -= 1
        if not acquired:
            logger.warning("Could not acquire render lock, skipping %s this cycle", region_name)
            return
        try:
            definition = config.sources.get(region.source)
            if definition is None:
                raise RenderError(f"Source '{region.source}' is not in the catalog")
            started = time.monotonic()
            source = get_source(definition.driver)
            buffer = self._buffers.get(
                region.name, config.max_frames_for(region), config.history.max_storage_mb
            )
            latest = buffer.latest()
            if window is not None:
                new_frame = self._add_maintenance_frame(region, buffer, latest, window)
                next_run_at = window.end
            else:
                # Only compare if the newest frame comes from the same source with the
                # same composite - otherwise (source/composite switch) render
                # immediately, even if the capture is older (0° lags Rapid Scan by
                # ~15 min).
                same_origin = (
                    latest is not None
                    and latest.source == region.source
                    and latest.composite == region.composite
                    and bool(latest.borders) == region.borders
                )
                last_sensing = _parse_timestamp(latest.created_at) if same_origin else None
                logger.info(
                    "Render %s started (source %s, driver %s, composite %s)",
                    region.name, definition.id, definition.driver, region.composite,
                )
                try:
                    rendered = source.render(region, config, last_sensing)
                except NoNewData as info:
                    logger.info("No new capture for %s: %s", region_name, info)
                else:
                    new_frame = buffer.add_frame(
                        rendered.png,
                        timestamp=rendered.sensing_time,
                        source=region.source,
                        composite=region.composite,
                        borders=region.borders,
                    )
                    logger.info(
                        "Render %s finished in %.1f s: %s (%d KB), %d frames in buffer",
                        region.name, time.monotonic() - started, new_frame.filename,
                        len(rendered.png) // 1024, len(buffer),
                    )
            self._status.record_success(region.name, len(buffer), next_run_at)
        except RenderError as exc:
            logger.error("Rendering for %s failed: %s", region_name, exc)
            self._status.record_error(region.name, str(exc), next_run_at)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected error while rendering %s", region_name)
            self._status.record_error(region.name, str(exc), next_run_at)
        finally:
            self._render_lock.release()

        if new_frame is not None:
            # Separate job per region (pending ones are coalesced):
            # Uses its own worker; encoding shares the render memory budget.
            self._scheduler.add_job(
                self._prewarm,
                args=[region_name, buffer, config.server.mjpeg_fps],
                id=f"{MEDIA_JOB_PREFIX}{region_name}",
                executor=MEDIA_EXECUTOR,
                replace_existing=True,
                misfire_grace_time=None,
            )
            # Sources without a download job (placeholders) archive here.
            if (
                config.archive.render_all and window is None
                and region.source not in self.download_sources(config)
            ):
                self._queue_archive(region.source, datetime.now(timezone.utc))

    @staticmethod
    def _add_maintenance_frame(region, buffer, latest, window: Window):
        """Black "unavailable due to maintenance" frame, once per window."""
        if (
            latest is not None
            and latest.composite == MAINTENANCE_COMPOSITE
            and latest.source == region.source
            and (_parse_timestamp(latest.created_at) or window.start) >= window.start
        ):
            return None
        frame = buffer.add_frame(
            render_maintenance_png(region.width, region.height, window),
            timestamp=datetime.now(timezone.utc),
            source=region.source,
            composite=MAINTENANCE_COMPOSITE,
            borders=region.borders,
        )
        logger.info(
            "Region %s: source %s under maintenance (%s) - maintenance image shown",
            region.name, region.source, window.describe(),
        )
        return frame

    def _prewarm(self, region_name: str, buffer, fps: float) -> None:
        try:
            media.prewarm(buffer, fps)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not create preview/animation for %s", region_name)
