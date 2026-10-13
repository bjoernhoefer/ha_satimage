"""Feature "location_change": manage up to four locations on a map of Europe.

A location is a region (``RegionConfig``): centre in plain WGS84
latitude/longitude plus a radius in km - no satellite pixel/line numbers are
needed, the renderer projects the region itself.

The web UI picks locations on a zoomable map of Europe. The map is a simple
lon/lat grid (plate carree), so pixel <-> lat/lon is linear and the browser
needs no projection maths. Two backgrounds exist:

- **outline** - land/sea (Natural Earth 1:50m) with country borders in the
  same style as the region overlay; generated locally, always available.
- **satellite** - rendered on demand from the newest MSG SEVIRI product
  (downloaded if needed) with the same borders; cached in
  ``<data>/location_map/`` until it is rendered again.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import re
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from ha_satimage.config import AppConfig, RegionConfig, default_composite_for

logger = logging.getLogger(__name__)

MAX_LOCATIONS = 4
DEFAULT_RADIUS_KM = 300.0
MIN_RADIUS_KM = 25.0
MAX_RADIUS_KM = 2000.0

# Map of Europe: (lon_min, lat_min, lon_max, lat_max) of the image edges.
EUROPE_EXTENT = (-25.0, 33.0, 45.0, 72.0)
MAP_WIDTH = 1400
# Pixel aspect so that distances look right around the middle latitude.
_MID_LAT = (EUROPE_EXTENT[1] + EUROPE_EXTENT[3]) / 2
MAP_HEIGHT = round(
    MAP_WIDTH * (EUROPE_EXTENT[3] - EUROPE_EXTENT[1])
    / ((EUROPE_EXTENT[2] - EUROPE_EXTENT[0]) * math.cos(math.radians(_MID_LAT)))
)

LAND_FILE = Path(__file__).resolve().parent / "overlay_data" / "europe_land_50m.json.gz"
SEA_COLOR = (172, 203, 228)
LAND_COLOR = (236, 233, 222)
COAST_COLOR = (110, 130, 150)
GRID_COLOR = (150, 170, 190)

# Light ~3 km composite: all of Europe at HRV resolution would need far
# more memory than a small host has.
SATELLITE_COMPOSITE = "natural_color_raw_with_night_ir"
SATELLITE_FILE = "satellite.jpg"
SATELLITE_META = "satellite.json"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class LocationError(ValueError):
    """Invalid location change (limit reached, unknown name, ...)."""


# -- Geometry of the map -----------------------------------------------------------
def lonlat_to_pixel(lat: float, lon: float) -> tuple[float, float]:
    lon_min, lat_min, lon_max, lat_max = EUROPE_EXTENT
    return (
        (lon - lon_min) / (lon_max - lon_min) * MAP_WIDTH,
        (lat_max - lat) / (lat_max - lat_min) * MAP_HEIGHT,
    )


def pixel_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Returns (lat, lon) of a map pixel."""
    lon_min, lat_min, lon_max, lat_max = EUROPE_EXTENT
    return (
        lat_max - y / MAP_HEIGHT * (lat_max - lat_min),
        lon_min + x / MAP_WIDTH * (lon_max - lon_min),
    )


def map_geometry() -> dict:
    lon_min, lat_min, lon_max, lat_max = EUROPE_EXTENT
    return {
        "extent": {"lon_min": lon_min, "lat_min": lat_min, "lon_max": lon_max, "lat_max": lat_max},
        "width": MAP_WIDTH,
        "height": MAP_HEIGHT,
    }


# -- Outline map -------------------------------------------------------------------
@lru_cache(maxsize=1)
def _land() -> list[dict]:
    return json.loads(gzip.decompress(LAND_FILE.read_bytes())).get("polygons", [])


@lru_cache(maxsize=1)
def outline_png() -> bytes:
    """Land/sea map of Europe with graticule and country borders (PNG)."""
    from PIL import Image, ImageDraw

    from ha_satimage.overlay import draw_borders_lonlat

    scale = 2  # supersampled land edges
    image = Image.new("RGB", (MAP_WIDTH * scale, MAP_HEIGHT * scale), SEA_COLOR)
    draw = ImageDraw.Draw(image)

    def _pixels(ring):
        return [
            (x * scale, y * scale)
            for x, y in (lonlat_to_pixel(lat, lon) for lon, lat in ring)
        ]

    for polygon in _land():
        draw.polygon(_pixels(polygon["outer"]), fill=LAND_COLOR, outline=COAST_COLOR)
    for polygon in _land():
        for hole in polygon.get("holes", []):
            draw.polygon(_pixels(hole), fill=SEA_COLOR, outline=COAST_COLOR)
    lon_min, lat_min, lon_max, lat_max = EUROPE_EXTENT
    lons = range(math.ceil(lon_min / 10) * 10, int(lon_max) + 1, 10)
    lats = range(math.ceil(lat_min / 10) * 10, int(lat_max) + 1, 10)
    for lon in lons:
        x = lonlat_to_pixel(lat_min, lon)[0] * scale
        draw.line([(x, 0), (x, MAP_HEIGHT * scale)], fill=GRID_COLOR, width=1)
    for lat in lats:
        y = lonlat_to_pixel(lat, lon_min)[1] * scale
        draw.line([(0, y), (MAP_WIDTH * scale, y)], fill=GRID_COLOR, width=1)
    image = image.resize((MAP_WIDTH, MAP_HEIGHT), Image.Resampling.LANCZOS)
    draw_borders_lonlat(image, EUROPE_EXTENT)
    draw = ImageDraw.Draw(image)
    for lon in lons:
        draw.text((lonlat_to_pixel(lat_min, lon)[0] + 3, 3), f"{abs(lon)}°{'W' if lon < 0 else 'E'}", fill=COAST_COLOR)
    for lat in lats:
        draw.text((3, lonlat_to_pixel(lat, lon_min)[1] + 2), f"{lat}°N", fill=COAST_COLOR)
    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


# -- Satellite map -----------------------------------------------------------------
def map_source(config: AppConfig):
    """Catalog entry used for the satellite map (MSG SEVIRI, preferably in use)."""
    candidates = [e for e in config.sources.catalog if e.driver == "msg_seviri"]
    used = {region.source for region in config.regions}
    candidates.sort(key=lambda e: (not e.enabled, e.id not in used))
    return candidates[0] if candidates else None


class SatelliteMap:
    """On-demand satellite background of the location map (cached on disk)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.last_error: str | None = None

    @property
    def image_path(self) -> Path:
        return self.directory / SATELLITE_FILE

    def info(self) -> dict:
        meta: dict = {}
        meta_path = self.directory / SATELLITE_META
        if self.image_path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        available = bool(meta)
        return {
            "available": available,
            "url": f"/location-map/{SATELLITE_FILE}?t={meta.get('rendered_at', '')}" if available else None,
            "source": meta.get("source"),
            "composite": meta.get("composite"),
            "sensing_time": meta.get("sensing_time"),
            "rendered_at": meta.get("rendered_at"),
            "error": self.last_error,
        }

    def render(self, config: AppConfig) -> dict:
        """Renders the map from the newest product (downloads it if needed).

        The caller must hold the global render lock.
        """
        from ha_satimage.sources import MsgSeviriSource, RenderError, _product_cache
        from ha_satimage.sources.satpy_render import (
            RenderRequest,
            SatpyRenderError,
            render_in_subprocess,
        )

        entry = map_source(config)
        try:
            if entry is None:
                raise RenderError("No MSG SEVIRI source in the catalog")
            collection = config.sources.collection_for(entry.id)
            source = MsgSeviriSource()
            product = _product_cache().cached(collection) or source._download(collection, config)
            lat = (EUROPE_EXTENT[1] + EUROPE_EXTENT[3]) / 2
            lon = (EUROPE_EXTENT[0] + EUROPE_EXTENT[2]) / 2
            request = RenderRequest(
                reader=source.reader,
                filenames=(str(product.path),),
                composite=SATELLITE_COMPOSITE,
                lat=lat,
                lon=lon,
                radius_km=1.0,
                width=MAP_WIDTH,
                height=MAP_HEIGHT,
                label="Europe",
                borders=True,
                lonlat_extent=EUROPE_EXTENT,
            )
            logger.info("Location map: rendering satellite background from %s", entry.id)
            try:
                png, sensing_end = render_in_subprocess(request)
            except SatpyRenderError as exc:
                raise RenderError(str(exc)) from exc
        except RenderError as exc:
            self.last_error = str(exc)
            logger.error("Location map: satellite background failed: %s", exc)
            raise
        if sensing_end.tzinfo is None:
            sensing_end = sensing_end.replace(tzinfo=timezone.utc)
        self._store(png, {
            "source": entry.id,
            "composite": SATELLITE_COMPOSITE,
            "sensing_time": sensing_end.isoformat(),
            "rendered_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
        })
        self.last_error = None
        logger.info("Location map: satellite background of %s stored", sensing_end.isoformat())
        return self.info()

    def _store(self, png: bytes, meta: dict) -> None:
        from PIL import Image

        self.directory.mkdir(parents=True, exist_ok=True)
        out = BytesIO()
        Image.open(BytesIO(png)).convert("RGB").save(out, format="JPEG", quality=85)
        tmp = self.image_path.with_suffix(".part")
        tmp.write_bytes(out.getvalue())
        tmp.replace(self.image_path)
        meta_path = self.directory / SATELLITE_META
        meta_tmp = meta_path.with_suffix(".part")
        meta_tmp.write_text(json.dumps(meta), encoding="utf-8")
        meta_tmp.replace(meta_path)


# -- Locations (regions) -----------------------------------------------------------
def location_key(region: RegionConfig) -> tuple:
    """Everything that defines the image cut-out of a location."""
    return (region.lat, region.lon, region.radius_km, region.width, region.height)


def reset_regions(old: AppConfig, new: AppConfig) -> list[str]:
    """Regions whose frames no longer match: moved/resized or removed."""
    result = []
    for region in old.regions:
        updated = new.region(region.name)
        if updated is None or location_key(updated) != location_key(region):
            result.append(region.name)
    return result


def check_locations(old: AppConfig, new: AppConfig) -> None:
    """At most ``MAX_LOCATIONS`` (more only if they existed before), unique names."""
    names = [region.name for region in new.regions]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise LocationError(f"Location name used more than once: {', '.join(duplicates)}")
    if len(new.regions) > MAX_LOCATIONS and len(new.regions) > len(old.regions):
        raise LocationError(f"At most {MAX_LOCATIONS} locations are possible")


def _number(payload: dict, key: str, default: float | None = None) -> float:
    value = payload.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LocationError(f"'{key}' must be a number") from None
    if not math.isfinite(number):
        raise LocationError(f"'{key}' must be a number")
    return number


def _position(payload: dict, default: RegionConfig | None = None) -> dict:
    lat = _number(payload, "lat", default.lat if default else None)
    lon = _number(payload, "lon", default.lon if default else None)
    radius = _number(payload, "radius_km", default.radius_km if default else DEFAULT_RADIUS_KM)
    if not -90 <= lat <= 90:
        raise LocationError("Latitude must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise LocationError("Longitude must be between -180 and 180")
    if not MIN_RADIUS_KM <= radius <= MAX_RADIUS_KM:
        raise LocationError(f"Radius must be between {MIN_RADIUS_KM:.0f} and {MAX_RADIUS_KM:.0f} km")
    return {"lat": round(lat, 4), "lon": round(lon, 4), "radius_km": round(radius, 1)}


def add_location(config: AppConfig, payload: dict) -> AppConfig:
    """New configuration with an added location (source/image type from defaults)."""
    if len(config.regions) >= MAX_LOCATIONS:
        raise LocationError(f"At most {MAX_LOCATIONS} locations are possible")
    name = str(payload.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise LocationError(
            "Name: 1-32 characters, letters, digits, '-' and '_', not starting with '-' or '_'"
        )
    if config.region(name) is not None:
        raise LocationError(f"Location '{name}' already exists")
    source = str(payload.get("source") or "") or _default_source(config)
    entry = config.sources.get(source)
    if entry is None:
        raise LocationError(f"Unknown source '{source}'")
    region = RegionConfig(
        name=name,
        **_position(payload),
        source=source,
        composite=default_composite_for(entry.driver, entry.collection),
        borders=bool(payload.get("borders", True)),
    )
    return config.model_copy(update={"regions": [*config.regions, region]})


def move_location(config: AppConfig, name: str, payload: dict) -> AppConfig:
    region = config.region(name)
    if region is None:
        raise KeyError(name)
    moved = region.model_copy(update=_position(payload, region))
    return config.model_copy(
        update={"regions": [moved if r.name == name else r for r in config.regions]}
    )


def remove_location(config: AppConfig, name: str) -> AppConfig:
    if config.region(name) is None:
        raise KeyError(name)
    return config.model_copy(update={"regions": [r for r in config.regions if r.name != name]})


def _default_source(config: AppConfig) -> str:
    if not config.sources.catalog:
        raise LocationError("The source catalog is empty")
    if config.regions:
        return config.regions[0].source
    enabled = [e.id for e in config.sources.catalog if e.enabled]
    return enabled[0] if enabled else config.sources.catalog[0].id


__all__ = [
    "EUROPE_EXTENT",
    "MAP_HEIGHT",
    "MAP_WIDTH",
    "MAX_LOCATIONS",
    "LocationError",
    "SatelliteMap",
    "add_location",
    "check_locations",
    "lonlat_to_pixel",
    "map_geometry",
    "map_source",
    "move_location",
    "outline_png",
    "pixel_to_lonlat",
    "remove_location",
    "reset_regions",
]
