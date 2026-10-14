"""Satpy rendering of a regional crop as PNG.

Deliberately runs in a separate process (see ``render_in_subprocess``):
Satpy/dask do not reliably return memory to the operating system after a
run, and an OOM kill should hit the rendering, not the web server.
"""

from __future__ import annotations

import logging
import multiprocessing
import signal
import warnings
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from ha_satimage.resources import HEAVY_WORK_LOCK
from ha_satimage.render_metrics import render_metrics

logger = logging.getLogger(__name__)

# Custom composite definitions (e.g. natural_color_hrv_with_night_ir).
SATPY_CONFIG_DIR = Path(__file__).resolve().parent.parent / "satpy_config"

# Margin pixels around the computed window so nearest-neighbour resampling
# also finds neighbours at the edges of the target region.
WINDOW_MARGIN_PX = 16
RENDER_TIMEOUT_SECONDS = 300
# netCDF4/HDF5 from the pip wheels is not thread-safe: with dask threads
# fci_l1c_nc crashed with SIGSEGV in ~50 % of runs on a Raspberry Pi,
# never when synchronous - at the same runtime (~7.5 s).
SINGLE_THREADED_READERS = frozenset({"fci_l1c_nc"})


class SatpyRenderError(Exception):
    pass


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


def _crop_to(data, target):
    """Crops a dataset to the window around the target region.

    The window is computed per dataset from its own area so that channels
    of different resolution (HRV ~1 km, others ~3 km) fit.
    """
    rows, cols = source_window(data.attrs["area"], target)
    cropped = data[..., rows, cols]
    cropped.attrs["area"] = data.attrs["area"][rows, cols]
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
    try:
        connection.send(("ok", render_png(request)))
    except Exception as exc:  # pass on to the parent process
        connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


class RenderProcessCrashed(SatpyRenderError):
    """Child process died without a result (signal, memory)."""


class RenderTimeoutError(SatpyRenderError):
    """Child process did not return within its deadline."""


def _describe_exit(exitcode: int | None) -> str:
    if exitcode is not None and exitcode < 0:
        try:
            name = signal.Signals(-exitcode).name
        except ValueError:
            name = f"Signal {-exitcode}"
        hint = ", possibly memory limit" if -exitcode == signal.SIGKILL else ""
        return f"killed by {name}{hint}"
    return f"exited without a result (exit code {exitcode})"


def _run_once(request: RenderRequest, timeout: float):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(request, child), daemon=True)
    with render_metrics.measure(
        "subprocess", composite=request.composite,
        pid=lambda: process.pid, descendants=True,
    ):
        try:
            process.start()
            child.close()
            if not parent.poll(timeout):
                raise RenderTimeoutError(f"Rendering exceeded the time limit of {timeout:.0f}s")
            try:
                status, payload = parent.recv()
            except EOFError as exc:
                process.join(timeout=5)
                raise RenderProcessCrashed(
                    f"Render process {_describe_exit(process.exitcode)}"
                ) from exc
        finally:
            if process.pid is not None:
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join()
            child.close()
            parent.close()
        if status == "error":
            raise SatpyRenderError(payload)
        return payload


def render_in_subprocess(
    request: RenderRequest, timeout: float = RENDER_TIMEOUT_SECONDS, retries: int = 1
):
    """Renders in a child process; retries if it dies without a result."""
    with HEAVY_WORK_LOCK:
        for attempt in range(retries + 1):
            try:
                return _run_once(request, timeout)
            except RenderProcessCrashed as exc:
                if attempt >= retries:
                    raise
                logger.warning("%s - retrying", exc)


__all__ = [
    "RenderRequest",
    "SatpyRenderError",
    "render_in_subprocess",
    "render_png",
    "source_window",
    "target_area",
]
