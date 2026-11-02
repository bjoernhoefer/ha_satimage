"""Satpy rendering of a regional crop as PNG.

Deliberately runs in a separate process (see ``render_in_subprocess``):
Satpy/dask do not reliably return memory to the operating system after a
run, and an OOM kill should hit the rendering, not the web server.
"""

from __future__ import annotations

import logging
import math
import multiprocessing
import os
import signal
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from ha_satimage.resources import HEAVY_WORK_LOCK

logger = logging.getLogger(__name__)

# Custom composite definitions (e.g. natural_color_hrv_with_night_ir).
SATPY_CONFIG_DIR = Path(__file__).resolve().parent.parent / "satpy_config"

# Margin pixels around the computed window so nearest-neighbour resampling
# also finds neighbours at the edges of the target region.
WINDOW_MARGIN_PX = 16
RENDER_TIMEOUT_SECONDS = 300
# A source window with more than this many pixels per target pixel is
# averaged down by an integer factor before resampling: nearest-neighbour
# resampling builds a KD-tree over every window pixel (~100 B/pixel). The
# HRV channel (1 km) over the European overview is a ~12 Mpx window for a
# 1.8 Mpx image and needed ~1.3 GiB; coarsened by 2 it needs about half.
# Small windows are never touched (memory is no issue there and the HRV
# detail of regular regions, e.g. 300 km at 800 px, is kept).
MAX_SOURCE_OVERSAMPLING = float(os.environ.get("HA_SATIMAGE_MAX_SOURCE_OVERSAMPLING", "4") or 4)
COARSEN_MIN_PIXELS = 4_000_000
# After a crash that is not caused by resources (e.g. SIGSEGV in HDF5) one
# retry after this pause; never after a timeout or a memory abort.
RETRY_DELAY_SECONDS = 10.0
# Interval of the memory watchdog of the render child process.
WATCHDOG_INTERVAL_SECONDS = 0.25
# Headroom of the container (web server, forkserver, page cache) that the
# render child must leave when its budget is derived from the cgroup limit.
MEMORY_RESERVE_MB = 192
# Imported once into the forkserver: each render child is forked from it
# instead of importing Satpy/dask/pyresample again (~200 MiB of files from
# the image layers per render with "spawn", re-read from disk whenever the
# page cache was evicted under memory pressure).
FORKSERVER_PRELOAD = [
    "ha_satimage.sources.satpy_render",
    "numpy",
    "xarray",
    "dask.array",
    "pyproj",
    "pyresample",
    "satpy",
    "satpy.scene",
    "PIL.Image",
]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"IEND\xaeB`\x82"
# netCDF4/HDF5 from the pip wheels is not thread-safe: with dask threads
# fci_l1c_nc crashed with SIGSEGV in ~50 % of runs on a Raspberry Pi,
# never when synchronous - at the same runtime (~7.5 s).
SINGLE_THREADED_READERS = frozenset({"fci_l1c_nc"})


class SatpyRenderError(Exception):
    pass


class RenderResourceError(SatpyRenderError):
    """Aborted for resources (time limit, memory): never retried at once."""


@dataclass(frozen=True)
class RenderRequest:
    reader: str
    filenames: tuple[str, ...]
    composite: str
    lat: float
    lon: float
    radius_km: float
    width: int
    height: int
    label: str
    borders: bool = False
    # Lon/lat (plate carree) map instead of the region square: extent
    # (lon_min, lat_min, lon_max, lat_max); lat/lon/radius are then unused.
    # Used for the location map of Europe (location_change.py).
    lonlat_extent: tuple[float, float, float, float] | None = None


def source_window(source_area, target_area, margin: int = WINDOW_MARGIN_PX):
    """Pixel window of the source area that fully covers the target region.

    Replaces pyresample's own pre-cropping (``reduce_data``) and
    ``Scene.crop``: both return a far too small window for the mirrored
    SEVIRI Rapid Scan area, leaving e.g. Vienna 99 % black (see
    HISTORY.md). ``get_array_indices_from_lonlat`` computes it correctly.
    """
    import numpy as np

    lons, lats = target_area.get_lonlats()
    edge_lons = np.concatenate([lons[0], lons[-1], lons[:, 0], lons[:, -1]])
    edge_lats = np.concatenate([lats[0], lats[-1], lats[:, 0], lats[:, -1]])
    cols, rows = source_area.get_array_indices_from_lonlat(edge_lons, edge_lats)
    valid = ~(np.ma.getmaskarray(cols) | np.ma.getmaskarray(rows))
    cols, rows = np.ma.getdata(cols)[valid], np.ma.getdata(rows)[valid]
    if cols.size == 0:
        raise SatpyRenderError("Region is outside the satellite's field of view")
    height, width = source_area.shape
    return (
        slice(max(int(rows.min()) - margin, 0), min(int(rows.max()) + margin + 1, height)),
        slice(max(int(cols.min()) - margin, 0), min(int(cols.max()) + margin + 1, width)),
    )


def target_area(request: RenderRequest):
    from pyresample import create_area_def

    if request.lonlat_extent is not None:
        return create_area_def(
            request.label,
            {"proj": "longlat", "datum": "WGS84"},
            width=request.width,
            height=request.height,
            area_extent=tuple(request.lonlat_extent),
            units="degrees",
        )
    radius_m = request.radius_km * 1000
    return create_area_def(
        request.label,
        {"proj": "laea", "lat_0": request.lat, "lon_0": request.lon, "ellps": "WGS84"},
        width=request.width,
        height=request.height,
        area_extent=(-radius_m, -radius_m, radius_m, radius_m),
        units="m",
    )


def coarsen_factor(window_pixels: int, target_pixels: int,
                   max_ratio: float = MAX_SOURCE_OVERSAMPLING,
                   min_pixels: int = COARSEN_MIN_PIXELS) -> int:
    """Integer factor that brings a large window (more than ``min_pixels``)
    to at most ``max_ratio`` × target pixels."""
    if (
        max_ratio <= 0 or target_pixels <= 0 or window_pixels <= min_pixels
        or window_pixels <= max_ratio * target_pixels
    ):
        return 1
    return max(1, math.ceil(math.sqrt(window_pixels / (max_ratio * target_pixels))))


def _coarsen(data, factor: int):
    """Block-averages the last two dimensions; area keeps the same extent."""
    from pyresample.geometry import AreaDefinition

    area = data.attrs["area"]
    ydim, xdim = data.dims[-2:]
    rows = (data.sizes[ydim] // factor) * factor
    cols = (data.sizes[xdim] // factor) * factor
    if rows == 0 or cols == 0:
        return data
    trimmed = data.isel({ydim: slice(0, rows), xdim: slice(0, cols)})
    trimmed = trimmed.drop_vars([c for c in trimmed.coords if c not in trimmed.dims])
    attrs = dict(data.attrs)
    coarse = trimmed.coarsen({ydim: factor, xdim: factor}, boundary="trim").mean(keep_attrs=True)
    coarse = coarse.astype(data.dtype, copy=False) if data.dtype.kind == "f" else coarse
    # Not pyresample's strided slicing: AreaDefinition[::n] miscomputes the extent.
    full = area[0:rows, 0:cols]
    attrs["area"] = AreaDefinition(
        f"{full.area_id}_coarse{factor}", full.description, full.proj_id, full.crs,
        cols // factor, rows // factor, full.area_extent,
    )
    coarse.attrs = attrs
    return coarse


def _crop_to(data, target):
    """Crops a dataset to the window around the target region.

    The window is computed per dataset from its own area so that channels
    of different resolution (HRV ~1 km, others ~3 km) fit. Windows that are
    much finer than the target are averaged down (``coarsen_factor``).
    """
    from pyresample.geometry import AreaDefinition

    rows, cols = source_window(data.attrs["area"], target)
    cropped = data[..., rows, cols]
    cropped.attrs["area"] = data.attrs["area"][rows, cols]
    area = cropped.attrs["area"]
    if isinstance(area, AreaDefinition):
        factor = coarsen_factor(area.width * area.height, target.width * target.height)
        if factor > 1:
            logger.info(
                "Source window %s of %s is %dx%d px for a %dx%d image - averaging %dx%d",
                data.attrs.get("name"), target.area_id, area.width, area.height,
                target.width, target.height, factor, factor,
            )
            cropped = _coarsen(cropped, factor)
    return cropped


def _annotate(image, text: str):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    left, top, right, bottom = draw.textbbox((0, 0), text)
    pad = 4
    box = (6, image.height - (bottom - top) - 2 * pad - 6, 6 + right - left + 2 * pad, image.height - 6)
    draw.rectangle(box, fill=(0, 0, 0))
    draw.text((box[0] + pad, box[1] + pad - top), text, fill=(255, 255, 255))
    return image


def render_png(request: RenderRequest) -> tuple[bytes, datetime]:
    """Renders the composite for the region and returns (PNG, sensing end)."""
    warnings.filterwarnings("ignore")
    import dask

    if request.reader in SINGLE_THREADED_READERS:
        dask.config.set(scheduler="synchronous")
    else:
        # Two threads: enough on small hosts (e.g. Raspberry Pi 5) without starving other services.
        dask.config.set(scheduler="threads", num_workers=2)
    import satpy
    from satpy import Scene

    satpy.config.set(config_path=[str(SATPY_CONFIG_DIR)])
    # fill_disk: otherwise HRV in the full disk (0°) comes as two stacked
    # windows (StackedAreaDefinition) - cropping and sunz_corrected fail on
    # that. Filling is lazy, the crop only loads the window.
    scene = Scene(
        reader=request.reader,
        filenames=list(request.filenames),
        reader_kwargs={"fill_disk": True} if request.reader == "seviri_l1b_native" else None,
    )
    available = set(scene.available_composite_names()) | {
        str(name) for name in scene.available_dataset_names()
    }
    if request.composite not in available:
        raise SatpyRenderError(
            f"Composite '{request.composite}' is not available for {request.reader}"
        )
    # Satpy only generates composites of channels with different resolution
    # (e.g. HRV + VIS) during resampling; until then only the channels exist.
    scene.load([request.composite], generate=False)
    area = target_area(request)
    for key in list(scene.keys()):
        scene._datasets[key] = _crop_to(scene[key], area)
    local = scene.resample(area, resampler="nearest", reduce_data=False)
    if request.composite not in local:
        raise SatpyRenderError(f"Composite '{request.composite}' could not be generated")
    data = local[request.composite]

    from satpy.writers import get_enhanced_image

    pil_image = get_enhanced_image(data).pil_image().convert("RGB")
    sensing_end = data.attrs["end_time"]
    if request.borders and request.lonlat_extent is not None:
        from ha_satimage.overlay import draw_borders_lonlat

        draw_borders_lonlat(pil_image, request.lonlat_extent)
    elif request.borders:
        from ha_satimage.overlay import draw_borders

        draw_borders(pil_image, request.lat, request.lon, request.radius_km)
    _annotate(pil_image, f"{request.label} · {sensing_end:%Y-%m-%d %H:%M} UTC · {request.composite}")
    out = BytesIO()
    pil_image.save(out, format="PNG", optimize=True)
    return out.getvalue(), sensing_end


def _worker(request: RenderRequest, connection) -> None:
    import resource

    try:
        png, sensing_end = render_png(request)
        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        connection.send(("ok", (png, sensing_end), peak_kb))
    except MemoryError:
        connection.send(("error", "MemoryError: not enough memory", None))
    except Exception as exc:  # pass on to the parent process
        connection.send(("error", f"{type(exc).__name__}: {exc}", None))
    finally:
        connection.close()


class RenderProcessCrashed(SatpyRenderError):
    """Child process died without a result (signal, memory)."""


def _describe_exit(exitcode: int | None) -> str:
    if exitcode is not None and exitcode < 0:
        try:
            name = signal.Signals(-exitcode).name
        except ValueError:
            name = f"Signal {-exitcode}"
        hint = ", possibly memory limit" if -exitcode == signal.SIGKILL else ""
        return f"killed by {name}{hint}"
    return f"exited without a result (exit code {exitcode})"


# -- Memory budget and diagnostics ---------------------------------------------------
CGROUP_ROOT = Path("/sys/fs/cgroup")


def cgroup_memory_limit(root: Path | None = None) -> int | None:
    """Memory limit of the container in bytes (cgroup v2 or v1), else None."""
    root = root or CGROUP_ROOT
    for name in ("memory.max", "memory/memory.limit_in_bytes"):
        try:
            value = (root / name).read_text().strip()
        except OSError:
            continue
        if value.isdigit() and int(value) < 1 << 60:
            return int(value)
        return None
    return None


def cgroup_oom_kills(root: Path | None = None) -> int | None:
    """``oom_kill`` counter of the container's cgroup, if readable."""
    root = root or CGROUP_ROOT
    for name in ("memory.events", "memory/memory.oom_control"):
        try:
            for line in (root / name).read_text().splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill" and value.strip().isdigit():
                    return int(value)
        except OSError:
            continue
    return None


def memory_budget_bytes(environ=None, root: Path | None = None) -> int | None:
    """Maximum RSS of a render child; ``HA_SATIMAGE_RENDER_MEMORY_MB`` (0 = off),
    otherwise the cgroup limit minus ``MEMORY_RESERVE_MB``, otherwise none."""
    environ = os.environ if environ is None else environ
    configured = str(environ.get("HA_SATIMAGE_RENDER_MEMORY_MB", "")).strip()
    if configured:
        try:
            megabytes = int(configured)
        except ValueError:
            logger.warning("HA_SATIMAGE_RENDER_MEMORY_MB=%r is not a number - ignored", configured)
        else:
            return megabytes * 1024 * 1024 if megabytes > 0 else None
    limit = cgroup_memory_limit(root)
    if limit is None:
        return None
    return max(limit - MEMORY_RESERVE_MB * 1024 * 1024, 256 * 1024 * 1024)


def process_rss(pid: int) -> int | None:
    """Resident memory of a process in bytes (Linux ``/proc``), else None."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class RenderStats:
    """Counters of render child processes for logs and ``/api/diagnostics``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.runs = 0
            self.succeeded = 0
            self.failures: dict[str, int] = {}
            self.active = 0
            self.max_active = 0
            self.peak_rss_mb: float | None = None
            self.last: dict | None = None

    def started(self) -> None:
        with self._lock:
            self.runs += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def finished(self, outcome: str, label: str, seconds: float, peak_rss: int | None) -> None:
        with self._lock:
            self.active -= 1
            if outcome == "ok":
                self.succeeded += 1
            else:
                self.failures[outcome] = self.failures.get(outcome, 0) + 1
            peak_mb = round(peak_rss / 2**20, 1) if peak_rss else None
            if peak_mb is not None:
                self.peak_rss_mb = max(self.peak_rss_mb or 0, peak_mb)
            self.last = {
                "label": label,
                "outcome": outcome,
                "seconds": round(seconds, 1),
                "peak_rss_mb": peak_mb,
                "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
            }

    def snapshot(self) -> dict:
        with self._lock:
            budget = memory_budget_bytes()
            return {
                "runs": self.runs,
                "succeeded": self.succeeded,
                "failures": dict(self.failures),
                "active": self.active,
                "max_parallel": self.max_active,
                "max_peak_rss_mb": self.peak_rss_mb,
                "memory_budget_mb": round(budget / 2**20) if budget else None,
                "start_method": start_method(),
                "last": dict(self.last) if self.last else None,
            }


STATS = RenderStats()


def start_method() -> str:
    method = os.environ.get("HA_SATIMAGE_RENDER_START_METHOD", "forkserver").strip() or "forkserver"
    if method not in multiprocessing.get_all_start_methods():
        return "spawn"
    return method


_context_lock = threading.Lock()
_contexts: dict[str, object] = {}


def _context():
    method = start_method()
    with _context_lock:
        context = _contexts.get(method)
        if context is None:
            context = multiprocessing.get_context(method)
            if method == "forkserver":
                context.set_forkserver_preload(FORKSERVER_PRELOAD)
            _contexts[method] = context
        return context


def _complete_png(png) -> bool:
    return (
        isinstance(png, (bytes, bytearray))
        and png.startswith(PNG_SIGNATURE)
        and png.rstrip(b"\x00").endswith(PNG_END)
    )


def _run_once(request: RenderRequest, timeout: float):
    budget = memory_budget_bytes()
    oom_before = cgroup_oom_kills()
    context = _context()
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(request, child), daemon=True)
    label = f"{getattr(request, 'label', '?')}/{getattr(request, 'composite', '?')}"
    started = time.monotonic()
    peak = 0
    outcome = "crash"
    STATS.started()
    try:
        process.start()
        child.close()
        while True:
            rss = process_rss(process.pid) if process.pid else None
            if rss:
                peak = max(peak, rss)
            if budget and rss and rss > budget:
                process.kill()
                outcome = "memory_budget"
                raise RenderResourceError(
                    f"Render process exceeded the memory budget of {budget / 2**20:.0f} MiB "
                    f"(RSS {rss / 2**20:.0f} MiB) and was stopped"
                )
            if parent.poll(WATCHDOG_INTERVAL_SECONDS):
                break
            if time.monotonic() - started > timeout:
                process.kill()
                outcome = "timeout"
                raise RenderResourceError(f"Rendering exceeded the time limit of {timeout:.0f}s")
        try:
            message = parent.recv()
        except EOFError as exc:
            process.join(timeout=5)
            oom_after = cgroup_oom_kills()
            if process.exitcode == -signal.SIGKILL:
                outcome = "oom_killed"
                detail = (
                    "killed by the kernel OOM killer (container memory limit)"
                    if oom_before is not None and oom_after is not None and oom_after > oom_before
                    else _describe_exit(process.exitcode)
                )
                raise RenderResourceError(
                    f"Render process {detail}; peak RSS {peak / 2**20:.0f} MiB"
                ) from exc
            raise RenderProcessCrashed(
                f"Render process {_describe_exit(process.exitcode)}"
            ) from exc
        status, payload = message[0], message[1]
        child_peak = message[2] if len(message) > 2 else None
        if child_peak:
            peak = max(peak, int(child_peak) * 1024)
        if status == "error":
            outcome = "memory_error" if str(payload).startswith("MemoryError") else "error"
            if outcome == "memory_error":
                raise RenderResourceError(str(payload))
            raise SatpyRenderError(payload)
        png = payload[0] if isinstance(payload, tuple) and payload else None
        if not _complete_png(png):
            outcome = "incomplete"
            raise SatpyRenderError("Render process returned an incomplete image")
        outcome = "ok"
        return payload
    finally:
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()
        seconds = time.monotonic() - started
        STATS.finished(outcome, label, seconds, peak or None)
        log = logger.info if outcome == "ok" else logger.warning
        log(
            "Render process %s: %s after %.1fs, peak RSS %s MiB, budget %s",
            label, outcome, seconds,
            f"{peak / 2**20:.0f}" if peak else "?",
            f"{budget / 2**20:.0f} MiB" if budget else "none",
        )


def render_in_subprocess(
    request: RenderRequest, timeout: float = RENDER_TIMEOUT_SECONDS, retries: int = 1
):
    """Renders in a child process.

    One retry (after ``RETRY_DELAY_SECONDS``) only when the child crashed
    for another reason than resources; a time limit, an OOM kill or the
    memory budget are reported at once - repeating would hit the same
    limit again and only double the disk and memory load.
    """
    with HEAVY_WORK_LOCK:
        for attempt in range(retries + 1):
            try:
                return _run_once(request, timeout)
            except RenderProcessCrashed as exc:
                if attempt >= retries:
                    raise
                logger.warning("%s - retrying in %.0fs", exc, RETRY_DELAY_SECONDS)
                time.sleep(RETRY_DELAY_SECONDS)


__all__ = [
    "RenderRequest",
    "RenderResourceError",
    "SatpyRenderError",
    "STATS",
    "coarsen_factor",
    "memory_budget_bytes",
    "render_in_subprocess",
    "render_png",
    "source_window",
    "target_area",
]
