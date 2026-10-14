"""FastAPI application: configuration UI, REST API and image endpoints."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from ha_satimage import __version__, location_change, logbuffer, media, storage
from ha_satimage.archive import RenderArchive, get_render_archive
from ha_satimage.buffer import BufferManager
from ha_satimage.config import (
    COMPOSITES,
    COMPOSITES_BY_COLLECTION,
    DEFAULT_COMPOSITE_BY_COLLECTION,
    PLACEHOLDER_DRIVERS,
    VALID_DRIVERS,
    AppConfig,
    ConfigStore,
    EumetsatCredentials,
    composites_for,
    default_composite_for,
    env_overrides,
    resolve_fci_composite,
)
from ha_satimage.scheduler import RenderScheduler
from ha_satimage.sources import (
    RenderError,
    archive_root,
    render_archive_root,
)
from ha_satimage.sources.fci_archive import (
    format_chunks,
    get_archive,
    wanted_chunks,
)
from ha_satimage.source_sync import SourceSync
from ha_satimage.status import StatusStore
from ha_satimage.maintenance import MaintenanceSchedule

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HA_SATIMAGE_DATA_DIR", "/data"))
DEFAULT_FRAMES_DIR = DATA_DIR / "frames"

log_buffer = logbuffer.install(log_dir=DATA_DIR / "logs")
logger = logging.getLogger(__name__)


def frames_dir_for(config: AppConfig) -> Path:
    return Path(config.storage.frames_dir) if config.storage.frames_dir else DEFAULT_FRAMES_DIR


config_store = ConfigStore()
buffer_manager = BufferManager(frames_dir_for(config_store.get()))
status_store = StatusStore()
source_sync = SourceSync(config_store, DATA_DIR / "source_sync.json")
maintenance_schedule = MaintenanceSchedule(DATA_DIR / "maintenance.json")
scheduler = RenderScheduler(
    config_store, buffer_manager, status_store, source_sync, maintenance_schedule
)
satellite_map = location_change.SatelliteMap(DATA_DIR / "location_map")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "ha_satimage %s starting: data in %s, frames in %s (UID %d / GID %d)",
        __version__, DATA_DIR, buffer_manager.base_dir, os.getuid(), os.getgid(),
    )
    try:
        storage.ensure_writable(buffer_manager.base_dir)
    except storage.StorageError as exc:
        logger.error("%s", exc)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="ha_satimage", version=__version__, lifespan=lifespan)


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return FileResponse(BASE_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


def _get_region_or_404(config: AppConfig, region_name: str):
    region = config.region(region_name)
    if region is None:
        raise HTTPException(status_code=404, detail=f"Unknown region: {region_name}")
    return region


def _buffer_for(region_name: str):
    config = config_store.get()
    region = _get_region_or_404(config, region_name)
    return buffer_manager.get(region.name, config.max_frames_for(region), config.history.max_storage_mb)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    config = config_store.get()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "config": config,
            "masked": config.masked(),
            "env_overrides": env_overrides().keys(),
            "status": status_store.all(),
            "drivers": VALID_DRIVERS,
            "composites": COMPOSITES,
            "composites_by_driver": {d: composites_for(d) for d in VALID_DRIVERS},
            "default_composites": {d: default_composite_for(d) for d in VALID_DRIVERS},
            "composites_by_collection": COMPOSITES_BY_COLLECTION,
            "default_composite_by_collection": DEFAULT_COMPOSITE_BY_COLLECTION,
            "composites_by_source": {
                e.id: composites_for(e.driver, e.collection) for e in config.sources.catalog
            },
            "driver_of": {e.id: e.driver for e in config.sources.catalog},
            "placeholder_drivers": PLACEHOLDER_DRIVERS,
            "max_locations": location_change.MAX_LOCATIONS,
            "version": __version__,
        },
    )


@app.get("/live/{region_name}")
async def live_redirect(region_name: str):
    """Memorable address for the live stream in the browser."""
    _get_region_or_404(config_store.get(), region_name)
    return RedirectResponse(url=f"/#live={region_name}")


@app.get("/api/config")
async def get_config():
    return JSONResponse(config_store.get().masked().model_dump())


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursive merge: nested dicts are merged field by field instead of
    being replaced entirely (e.g. ``eumetsat`` or individual ``sources``
    fields); lists (e.g. ``regions``) are taken over as a whole if given."""
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _is_masked_echo(value: object, stored_secret: str) -> bool:
    """Detects whether a client sends the masked secret back unchanged."""
    if not isinstance(value, str) or not value:
        return False
    return value == EumetsatCredentials(consumer_secret=stored_secret).masked().consumer_secret


@app.post("/api/config")
async def post_config(payload: dict):
    stored = config_store.stored()
    credentials = payload.get("eumetsat")
    if isinstance(credentials, dict):
        credentials = dict(credentials)
        secret = credentials.get("consumer_secret")
        # An empty or masked secret means "keep unchanged" - otherwise every
        # save without re-entering it would destroy the real secret.
        if secret is None or secret == "" or _is_masked_echo(secret, stored.eumetsat.consumer_secret):
            credentials.pop("consumer_secret", None)
        payload = {**payload, "eumetsat": credentials}
    merged = _deep_merge(stored.model_dump(), payload)
    try:
        new_config = AppConfig(**merged)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _save_config(stored, new_config, move_existing=False)
    changed = sorted(key for key in payload if key in AppConfig.model_fields)
    logger.info("Configuration saved (%s)", ", ".join(changed) or "no change")
    return JSONResponse(config_store.get().masked().model_dump())


async def _save_config(old: AppConfig, new: AppConfig, move_existing: bool) -> int:
    """Saves the configuration; switches the storage location if needed."""
    old_regions = {region.name: region for region in old.regions}
    changed_borders = {
        region.name
        for region in new.regions
        if region.name in old_regions and region.borders != old_regions[region.name].borders
    }
    try:
        location_change.check_locations(old, new)
    except location_change.LocationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    old_dir, new_dir = frames_dir_for(old), frames_dir_for(new)
    moved = 0
    if old_dir != new_dir or buffer_manager.base_dir != new_dir:
        def _relocate() -> int:
            storage.ensure_writable(new_dir)
            with scheduler.exclusive():
                return buffer_manager.relocate(new_dir, move_existing)

        try:
            moved = await run_in_threadpool(_relocate)
        except (storage.StorageError, TimeoutError, OSError) as exc:
            logger.error("Switching storage location to %s failed: %s", new_dir, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info(
            "Storage location changed: %s -> %s (%d frames moved)", old_dir, new_dir, moved
        )
    reset = location_change.reset_regions(old, new)
    if reset:
        # Frames of a moved (or removed) location no longer match: drop them,
        # so the history does not mix locations and the new cut-out renders
        # right away instead of waiting for the next scan.
        def _apply() -> None:
            with scheduler.exclusive():
                for name in reset:
                    buffer_manager.drop(name)
                    status_store.forget(name)
                config_store.update(new)

        try:
            await run_in_threadpool(_apply)
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        logger.info("Location changed or removed - history reset: %s", ", ".join(reset))
    else:
        config_store.update(new)
    scheduler.reload()
    if changed_borders:
        scheduler.refresh_archived_regions(changed_borders)
    return moved


@app.get("/api/storage")
async def get_storage():
    config = config_store.get()
    current = frames_dir_for(config)

    def _collect() -> dict:
        return {
            "current": str(current),
            "default": str(DEFAULT_FRAMES_DIR),
            "configured": config.storage.frames_dir,
            "used_bytes": storage.directory_size(current),
            "uid": os.getuid(),
            "gid": os.getgid(),
            "candidates": storage.candidates(DEFAULT_FRAMES_DIR, current, DATA_DIR),
            "history_minutes": config.history.history_minutes,
            "max_storage_mb": config.history.max_storage_mb,
            "archive": _archive_summary(config),
        }

    return JSONResponse(await run_in_threadpool(_collect))


@app.post("/api/storage")
async def post_storage(payload: dict):
    """Set the storage location (and history limits), optionally move frames."""
    stored = config_store.stored()
    data = stored.model_dump()
    frames_dir = str(payload.get("frames_dir", data["storage"]["frames_dir"]) or "").strip()
    if frames_dir and Path(frames_dir) == DEFAULT_FRAMES_DIR:
        frames_dir = ""
    data["storage"]["frames_dir"] = frames_dir
    for key in ("history_minutes", "max_storage_mb"):
        if payload.get(key) is not None:
            data["history"][key] = payload[key]
    for key in ("retention_hours", "chunk_min", "chunk_max", "render_all",
                "render_retention_hours", "render_max_storage_mb"):
        if payload.get(f"archive_{key}") is not None:
            data["archive"][key] = payload[f"archive_{key}"]
    try:
        new_config = AppConfig(**data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    moved = await _save_config(stored, new_config, move_existing=bool(payload.get("move_existing", True)))
    # A shorter retention takes effect immediately, not only at the next download.
    await run_in_threadpool(
        get_archive(archive_root(new_config)).prune_all, new_config.archive.retention_hours
    )
    await run_in_threadpool(
        _render_archive(new_config).prune,
        new_config.archive.render_retention_hours,
        new_config.archive.render_max_storage_mb,
    )
    return {
        "current": str(frames_dir_for(new_config)),
        "moved_frames": moved,
        "archive": await run_in_threadpool(_archive_summary, new_config),
    }


def _render_archive(config: AppConfig) -> "RenderArchive":
    return get_render_archive(render_archive_root(config))


def _archive_summary(config: AppConfig) -> dict:
    archive = get_archive(archive_root(config))
    collections = {}
    for collection in archive.collections():
        slots = archive.slots(collection)
        collections[collection] = {
            "slots": len(slots),
            "newest": slots[0].sensing_end.isoformat() if slots else None,
            "oldest": slots[-1].sensing_end.isoformat() if slots else None,
            "used_bytes": storage.directory_size(archive.collection_dir(collection)),
        }
    chunks = wanted_chunks(config)
    return {
        "dir": str(archive.root),
        "retention_hours": config.archive.retention_hours,
        "chunk_min": config.archive.chunk_min,
        "chunk_max": config.archive.chunk_max,
        "chunks": sorted(chunks),
        "chunks_text": format_chunks(chunks),
        "enabled_sources": [
            e.id for e in config.sources.catalog if e.driver == "mtg_fci" and e.enabled
        ],
        "collections": collections,
        "status": archive.status(),
        "render_all": config.archive.render_all,
        "render_retention_hours": config.archive.render_retention_hours,
        "render_max_storage_mb": config.archive.render_max_storage_mb,
        "archived_sources": [e.id for e in config.sources.catalog if e.enabled],
        "renders": _render_archive(config).summary(),
    }


@app.get("/api/archive")
async def get_archive_info():
    """FCI raw data archive: retention, chunks, slots and disk usage."""
    return JSONResponse(await run_in_threadpool(_archive_summary, config_store.get()))


_COMPOSITE_RE = re.compile(r"^[A-Za-z0-9_]+$")


@app.get("/api/regions/{region_name}/archive")
async def list_archive(
    region_name: str, source: str | None = None, composite: str | None = None
):
    """Archive of a region: available sources, image types and images.

    Only reads the region's list of available images (``available.json``):
    everything listed is already rendered (PNG + JPEG), nothing is rendered
    while browsing.
    """
    config = config_store.get()
    region = _get_region_or_404(config, region_name)
    archive = _render_archive(config)
    sources = await run_in_threadpool(_archive_sources, config, region_name)
    if not sources:
        return {
            "region": region_name, "source": None, "composite": None,
            "sources": [], "composites": {}, "images": [], "slots": [],
            "retention_hours": config.archive.render_retention_hours,
        }
    source = source if any(s["id"] == source for s in sources) else sources[0]["id"]
    entry = config.sources.get(source)
    composites = next(s["composites"] for s in sources if s["id"] == source)
    explicit_composite = bool(composite and composite in composites)
    if not explicit_composite:
        composite = _default_archive_composite(config, region, entry)
        if composite not in composites:
            composite = next(iter(composites), "")
    if composite and not _COMPOSITE_RE.match(composite):
        raise HTTPException(status_code=400, detail="Invalid composite name")

    def _archived_names() -> list[str]:
        return archive.combinations(region_name).get(source, [])

    if not explicit_composite and not await run_in_threadpool(
        archive.images, region_name, source, composite
    ):
        # Open on an image type that actually has archived images.
        stored_names = await run_in_threadpool(_archived_names)
        if stored_names:
            composite = stored_names[0]

    def _collect() -> list[dict]:
        return [
            image.as_dict(region_name)
            for image in archive.images(region_name, source, composite)
        ]

    images = await run_in_threadpool(_collect)
    return {
        "region": region_name,
        "source": source,
        "composite": composite,
        "sources": sources,
        "composites": composites,
        "retention_hours": config.archive.render_retention_hours,
        "images": images,
        # Backwards-compatible alias of the previous FCI-only archive API.
        "slots": images,
    }


def _archive_sources(config: AppConfig, region_name: str) -> list[dict]:
    """Sources offered in the archive: enabled ones plus already archived ones."""
    stored = _render_archive(config).combinations(region_name)
    result: list[dict] = []
    for entry in config.sources.catalog:
        composites = dict(composites_for(entry.driver, entry.collection))
        for name in stored.get(entry.id, []):
            composites.setdefault(name, name)
        has_data = entry.id in stored
        if not entry.enabled and not has_data:
            continue
        result.append({
            "id": entry.id,
            "label": entry.label or entry.id,
            "driver": entry.driver,
            "composites": composites,
            "has_data": has_data,
        })
    # Sources with images first, so the viewer opens on something useful.
    result.sort(key=lambda s: not s["has_data"])
    return result


def _default_archive_composite(config: AppConfig, region, entry) -> str:
    if entry is not None and entry.id == region.source:
        return (
            resolve_fci_composite(region.composite, entry.collection)
            if entry.driver == "mtg_fci"
            else region.composite
        )
    if entry is None:
        return default_composite_for(None)
    return default_composite_for(entry.driver, entry.collection)


@app.get("/regions/{region_name}/archive/{source}/{composite}/{slot_name}.{ext}")
async def archive_image(
    region_name: str, source: str, composite: str, slot_name: str, ext: str,
    v: str | None = None,
):
    """Archived image (PNG or JPEG) - served from disk, never rendered on request."""
    if ext not in ("png", "jpg"):
        raise HTTPException(status_code=404, detail="Unknown format")
    _get_region_or_404(config_store.get(), region_name)
    if not _COMPOSITE_RE.match(composite):
        raise HTTPException(status_code=400, detail="Invalid composite name")
    archive = _render_archive(config_store.get())
    path = archive.path_for(region_name, source, composite, slot_name, ext)
    if path is None or not path.resolve().is_relative_to(archive.root.resolve()):
        raise HTTPException(status_code=400, detail="Invalid archive address")
    try:
        payload = await run_in_threadpool(path.read_bytes)
        version = str(path.stat().st_mtime_ns)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Image not (or no longer) in the archive") from exc
    # Archive overlays can be updated at the same scan URL.
    headers = {"Cache-Control": "no-cache"}
    if v is not None and v == version:
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    media_type = "image/png" if ext == "png" else "image/jpeg"
    return Response(content=payload, media_type=media_type, headers=headers)


@app.post("/api/archive/render")
async def archive_render_now():
    """Archive the newest capture of every enabled source, in all image types."""
    count = await run_in_threadpool(scheduler.archive_all_now)
    return {"status": "scheduled", "sources": count}


@app.post("/api/archive/clear")
async def clear_archive():
    """Delete archived images and FCI raw data, without rebuilding old scans."""
    try:
        result = await run_in_threadpool(scheduler.clear_archive)
    except TimeoutError as exc:
        logger.warning("Could not clear archive: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Could not clear archive")
        raise HTTPException(status_code=500, detail=f"Could not clear archive: {exc}") from exc
    return {"status": "cleared", **result}


@app.get("/api/sources")
async def get_sources():
    config = config_store.get()
    return {
        "catalog": [entry.model_dump() for entry in config.sources.catalog],
        "auto_sync_hours": config.sources.auto_sync_hours,
        "maintenance_check_hours": config.sources.maintenance_check_hours,
        "maintenance": maintenance_schedule.state(),
        "sync": source_sync.state(),
        "downloads": scheduler.download_status(),
    }


@app.post("/api/sources/sync")
async def sync_sources():
    return JSONResponse(await run_in_threadpool(source_sync.run))


@app.post("/api/sources/adopt")
async def adopt_source(payload: dict):
    collection = payload.get("collection")
    stored = config_store.stored()
    definition = source_sync.definition_for(
        str(collection or ""), {entry.id for entry in stored.sources.catalog}
    )
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection} was not discovered")
    data = stored.model_dump()
    data["sources"]["catalog"].append(definition.model_dump())
    new_config = AppConfig(**data)
    await _save_config(stored, new_config, move_existing=False)
    source_sync.forget_discovered(definition.collection or "")
    logger.info("Source %s (%s) added to the catalog", definition.id, definition.collection)
    return definition.model_dump()


@app.get("/api/logs")
async def get_logs(after: int = 0, limit: int = 500, format: str = "json"):
    entries = log_buffer.entries(after=after, limit=limit)
    if format == "text":
        lines = [f"{e['ts']} {e['level']:<7} {e['logger']}: {e['message']}" for e in entries]
        return PlainTextResponse("\n".join(lines) + "\n")
    return {"entries": entries}


@app.get("/api/status")
async def get_status():
    return JSONResponse(status_store.all())


@app.get("/api/version")
async def get_version():
    return {"version": __version__}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# -- Locations ("location_change") ---------------------------------------------------
@app.get("/api/location-map")
async def location_map_info():
    """Map of Europe for picking locations: geometry, backgrounds, locations."""
    config = config_store.get()
    entry = location_change.map_source(config)
    return {
        **location_change.map_geometry(),
        "max_locations": location_change.MAX_LOCATIONS,
        "radius_km": {
            "default": location_change.DEFAULT_RADIUS_KM,
            "min": location_change.MIN_RADIUS_KM,
            "max": location_change.MAX_RADIUS_KM,
        },
        "outline_url": "/location-map/outline.png",
        "satellite": {**satellite_map.info(), "map_source": entry.id if entry else None},
        "locations": [
            {"name": r.name, "lat": r.lat, "lon": r.lon, "radius_km": r.radius_km}
            for r in config.regions
        ],
    }


@app.get("/location-map/outline.png")
async def location_map_outline():
    data = await run_in_threadpool(location_change.outline_png)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/location-map/satellite.jpg")
async def location_map_satellite():
    path = satellite_map.image_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="No satellite map rendered yet")
    return Response(content=path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


# Time to wait for the render lock (one scheduled run incl. download).
MAP_RENDER_LOCK_TIMEOUT = 600


@app.post("/api/location-map/satellite")
async def location_map_render_satellite():
    """Renders the satellite background now (downloads the newest product if needed)."""
    config = config_store.get()

    def _render() -> dict:
        with scheduler.exclusive(timeout=MAP_RENDER_LOCK_TIMEOUT):
            return satellite_map.render(config)

    try:
        info = await run_in_threadpool(_render)
    except RenderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return info


async def _apply_location_change(change, *args) -> AppConfig:
    stored = config_store.stored()
    try:
        updated = change(stored, *args)
        new_config = AppConfig(**updated.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown location: {exc.args[0]}") from exc
    except ValueError as exc:  # LocationError, pydantic ValidationError
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _save_config(stored, new_config, move_existing=False)
    return new_config


def _region_dict(config: AppConfig, name: str) -> dict:
    region = config.region(name)
    return region.model_dump() if region else {}


@app.post("/api/regions")
async def add_location(payload: dict):
    """Add a location (max. ``MAX_LOCATIONS``): name, lat, lon, radius_km."""
    new_config = await _apply_location_change(location_change.add_location, payload)
    name = str(payload.get("name") or "").strip()
    region = new_config.region(name)
    logger.info(
        "Location %s added: %.4f, %.4f, %.0f km (source %s)",
        name, region.lat, region.lon, region.radius_km, region.source,
    )
    return _region_dict(new_config, name)


@app.put("/api/regions/{region_name}")
async def move_location(region_name: str, payload: dict):
    """Change position/radius of a location (its history is reset)."""
    new_config = await _apply_location_change(location_change.move_location, region_name, payload)
    region = new_config.region(region_name)
    logger.info(
        "Location %s changed: %.4f, %.4f, %.0f km",
        region_name, region.lat, region.lon, region.radius_km,
    )
    return _region_dict(new_config, region_name)


@app.delete("/api/regions/{region_name}")
async def remove_location(region_name: str):
    await _apply_location_change(location_change.remove_location, region_name)
    logger.info("Location %s removed", region_name)
    return {"status": "removed", "region": region_name}


@app.post("/api/regions/{region_name}/refresh")
async def refresh_region(region_name: str):
    config = config_store.get()
    _get_region_or_404(config, region_name)
    scheduler.trigger_now(region_name)
    logger.info("Manual refresh requested for %s", region_name)
    return {"status": "scheduled", "region": region_name}


@app.get("/api/regions/{region_name}/frames")
async def list_frames(region_name: str):
    """History: all frames in the buffer, newest first."""
    buffer = _buffer_for(region_name)
    return {
        "region": region_name,
        "frames": [
            {
                "index": i,
                "filename": frame.filename,
                "created_at": frame.created_at,
                "source": frame.source,
                "composite": frame.composite,
                "borders": frame.borders,
                "url": f"/regions/{region_name}/history/{frame.filename}",
                "jpeg_url": f"/regions/{region_name}/history/{Path(frame.filename).stem}.jpg",
            }
            for i, frame in enumerate(buffer.frames_newest_first())
        ],
    }


@app.get("/regions/{region_name}/history/{filename}")
async def history_frame(region_name: str, filename: str, w: int | None = None):
    """Frame by (stable) file name.

    ``<name>.png`` returns the original, ``<name>.jpg`` the cached full-size
    JPEG (much smaller, faster in the viewer); ``?w=240`` a JPEG thumbnail.
    """
    buffer = _buffer_for(region_name)
    as_jpeg = filename.endswith(".jpg")
    if as_jpeg:
        filename = filename[: -len(".jpg")] + ".png"
    frame = buffer.by_filename(filename)
    path = frame.path(buffer.region_dir) if frame else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Frame not (or no longer) available")
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if w or as_jpeg:
        width = max(32, min(w, 1600)) if w else None
        data = await run_in_threadpool(media.jpeg, buffer, frame, width)
        return Response(content=data, media_type="image/jpeg", headers=headers)
    return Response(content=path.read_bytes(), media_type="image/png", headers=headers)


@app.get("/regions/{region_name}/latest.png")
async def latest_png(region_name: str):
    buffer = _buffer_for(region_name)
    frame = buffer.latest()
    if frame is None:
        raise HTTPException(status_code=404, detail="No frames available yet")
    return Response(
        content=frame.path(buffer.region_dir).read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/regions/{region_name}/latest.jpg")
async def latest_jpg(region_name: str):
    """Latest single image as cached JPEG (faster than the PNG)."""
    buffer = _buffer_for(region_name)
    frame = buffer.latest()
    if frame is None:
        raise HTTPException(status_code=404, detail="No frames available yet")
    try:
        data = await run_in_threadpool(media.jpeg, buffer, frame)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Frame not (or no longer) available") from exc
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.get("/regions/{region_name}/frames/{index}.png")
async def frame_png(region_name: str, index: int):
    buffer = _buffer_for(region_name)
    frame = buffer.get(index)
    if frame is None:
        raise HTTPException(status_code=404, detail="Frame index not available")
    return Response(content=frame.path(buffer.region_dir).read_bytes(), media_type="image/png")


@app.get("/regions/{region_name}/animation.gif")
async def animation_gif(region_name: str):
    buffer = _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    payload = await run_in_threadpool(media.animation, buffer, "gif", fps)
    if payload is None:
        raise HTTPException(status_code=404, detail="No frames available yet")
    return Response(content=payload, media_type="image/gif")


@app.get("/regions/{region_name}/animation.mp4")
async def animation_mp4(region_name: str):
    buffer = _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    try:
        payload = await run_in_threadpool(media.animation, buffer, "mp4", fps)
    except ImportError as exc:  # pragma: no cover - depends on optional dependency
        raise HTTPException(
            status_code=501, detail="MP4 export requires the imageio-ffmpeg package"
        ) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="No frames available yet")
    return Response(content=payload, media_type="video/mp4")


@app.get("/regions/{region_name}/mjpeg")
async def mjpeg(region_name: str):
    _buffer_for(region_name)
    fps = config_store.get().server.mjpeg_fps
    delay = 1.0 / fps if fps > 0 else 1.0

    async def _generate():
        while True:
            # Re-fetch on every pass: new frames and a storage location changed
            # in the meantime are picked up without a reconnect.
            try:
                buffer = _buffer_for(region_name)
            except HTTPException:
                return
            frames = buffer.frames_newest_first()
            if not frames:
                await asyncio.sleep(delay)
                continue
            for frame in reversed(frames):  # play oldest first, then loop
                try:
                    jpeg_bytes = await run_in_threadpool(media.jpeg, buffer, frame)
                except FileNotFoundError:
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n\r\n"
                    + jpeg_bytes + b"\r\n"
                )
                await asyncio.sleep(delay)

    return StreamingResponse(_generate(), media_type="multipart/x-mixed-replace; boundary=frame")
