"""Configuration handling: YAML persistence in /data with env var override.

The configuration is stored as a YAML file (default path
``/data/config.yaml``, overridable via the ``HA_SATIMAGE_CONFIG``
environment variable). EUMETSAT credentials can additionally be set via
environment variables (``EUMETSAT_CONSUMER_KEY`` / ``EUMETSAT_CONSUMER_SECRET``);
these take precedence over the values stored in the file but are never
written back to it.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ha_satimage.geometry import EUROPE_EXTENT, BoundingBox, bounding_box

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH_ENV = "HA_SATIMAGE_CONFIG"
DEFAULT_CONFIG_PATH = "/data/config.yaml"

ENV_CONSUMER_KEY = "EUMETSAT_CONSUMER_KEY"
ENV_CONSUMER_SECRET = "EUMETSAT_CONSUMER_SECRET"

# Implementations ("drivers") in sources/__init__.py. Which concrete
# sources are available is defined by the source catalog (sources.catalog) -
# several catalog entries may use the same driver (e.g. msg_seviri for 0°,
# Rapid Scan and IODC). "dummy" is a pure test source.
VALID_DRIVERS = ("msg_seviri", "data_tailor", "mtg_fci", "dummy")
# Backwards-compatible alias.
VALID_SOURCES = VALID_DRIVERS

# Natural color by day (sharpened with the ~1 km HRV channel), infrared
# clouds at night; defined in satpy_config/composites/seviri.yaml. The
# obvious "natural_color_with_night_ir" downloads NASA background maps at
# runtime (hash error, see HISTORY.md).
DEFAULT_COMPOSITE = "natural_color_hrv_with_night_ir"

# Choices in the web UI (name -> description); other Satpy composites are
# possible via JSON/API.
COMPOSITES: dict[str, str] = {
    "natural_color_hrv_with_night_ir": "Natural color, HRV-sharpened (~1 km), IR at night – best quality",
    "natural_color_hrv": "Natural color, HRV-sharpened (~1 km), daytime only",
    "natural_color_raw_with_night_ir": "Natural color (~3 km), IR at night",
    "natural_color": "Natural color (~3 km), black at night",
    "hrv_clouds": "HRV clouds (~1 km, daytime)",
    "cloudtop": "Cloud tops (IR)",
    "colorized_ir_clouds": "Colorized IR clouds",
    "convection": "Convection",
    "airmass": "Air mass",
}

# MTG FCI (1 km visible / 2 km IR). Natural color by day, IR clouds at
# night; defined in satpy_config/composites/visir.yaml. true_color and
# airmass exceed the memory limit on a Raspberry Pi 5 (up to 1.7 GB).
DEFAULT_FCI_COMPOSITE = "natural_color_with_night_cloudtop"
FCI_COMPOSITES: dict[str, str] = {
    "natural_color_with_night_cloudtop": "Natural color (~1 km), IR at night – best quality",
    "natural_color": "Natural color (~1 km), black at night",
    "hrv_clouds": "High-resolution clouds (~1 km, daytime)",
    "cloudtop": "Cloud tops (IR, ~2 km)",
    "colorized_ir_clouds": "Colorized IR clouds",
}
# SEVIRI-specific names mapped to an FCI counterpart (so switching the
# source without switching the composite keeps working).
FCI_COMPOSITE_ALIASES: dict[str, str] = {
    "natural_color_hrv_with_night_ir": DEFAULT_FCI_COMPOSITE,
    "natural_color_raw_with_night_ir": DEFAULT_FCI_COMPOSITE,
    "natural_color_hrv": "natural_color",
}

COMPOSITES_BY_DRIVER: dict[str, dict[str, str]] = {"mtg_fci": FCI_COMPOSITES}

# MTG FCI High Resolution Fast Imagery (HRFI): only four channels (vis_06 and
# nir_22 at ~500 m, ir_38 and ir_105 at ~1 km). Natural color and cloud tops
# need channels that only the normal resolution product (FDHSI) contains.
FCI_HRFI_COLLECTION = "EO:EUM:DAT:0665"
DEFAULT_FCI_HRFI_COMPOSITE = "ir_sandwich_with_night_colorized_ir_clouds"
FCI_HRFI_COMPOSITES: dict[str, str] = {
    DEFAULT_FCI_HRFI_COMPOSITE: "IR sandwich (~500 m), colorized IR at night – best quality",
    "ir_sandwich": "IR sandwich: visible ~500 m + colorized cold clouds (daytime)",
    "hrv_clouds": "High-resolution clouds (~500 m, daytime)",
    "colorized_ir_clouds": "Colorized IR clouds (~1 km, day and night)",
}
# FDHSI image types mapped to an HRFI counterpart.
FCI_HRFI_COMPOSITE_ALIASES: dict[str, str] = {
    DEFAULT_FCI_COMPOSITE: DEFAULT_FCI_HRFI_COMPOSITE,
    "natural_color": "ir_sandwich",
    "cloudtop": "colorized_ir_clouds",
}
COMPOSITES_BY_COLLECTION: dict[str, dict[str, str]] = {FCI_HRFI_COLLECTION: FCI_HRFI_COMPOSITES}
DEFAULT_COMPOSITE_BY_COLLECTION: dict[str, str] = {FCI_HRFI_COLLECTION: DEFAULT_FCI_HRFI_COMPOSITE}


def composites_for(driver: str | None, collection: str | None = None) -> dict[str, str]:
    if driver == "mtg_fci" and collection in COMPOSITES_BY_COLLECTION:
        return COMPOSITES_BY_COLLECTION[collection]
    return COMPOSITES_BY_DRIVER.get(driver or "", COMPOSITES)


def default_composite_for(driver: str | None, collection: str | None = None) -> str:
    if driver == "mtg_fci" and collection in DEFAULT_COMPOSITE_BY_COLLECTION:
        return DEFAULT_COMPOSITE_BY_COLLECTION[collection]
    return DEFAULT_FCI_COMPOSITE if driver == "mtg_fci" else DEFAULT_COMPOSITE


def resolve_fci_composite(name: str, collection: str | None = None) -> str:
    name = FCI_COMPOSITE_ALIASES.get(name, name)
    if collection == FCI_HRFI_COLLECTION:
        name = FCI_HRFI_COMPOSITE_ALIASES.get(name, name)
    return name


# Drivers that do not deliver real images yet (placeholder frames).
PLACEHOLDER_DRIVERS = ("data_tailor", "dummy")

DEFAULT_FCI_COLLECTION = "EO:EUM:DAT:0662"
# FCI delivers the full disk in 40 stripes ("chunks", south -> north).
FCI_CHUNK_COUNT = 40

# MSG SEVIRI Rapid Scan (Europe, every 5 minutes, Meteosat-11).
DEFAULT_MSG_COLLECTION = "EO:EUM:DAT:MSG:MSG15-RSS"

# Capture cycle (minutes) of known collections: how often a new product
# appears, and the download cycle. Override per catalog entry via
# ``cycle_minutes``.
COLLECTION_CYCLES: dict[str, int] = {
    DEFAULT_MSG_COLLECTION: 5,
    "EO:EUM:DAT:MSG:HRSEVIRI": 15,
    "EO:EUM:DAT:MSG:HRSEVIRI-IODC": 15,
    DEFAULT_FCI_COLLECTION: 10,
    FCI_HRFI_COLLECTION: 10,
}
DRIVER_CYCLES: dict[str, int] = {"msg_seviri": 15, "mtg_fci": 10}
# Drivers that download products themselves (own download job per source).
DOWNLOAD_DRIVERS = ("msg_seviri", "mtg_fci")

# Default frames in the ring buffer: corresponds (by default) to 60 minutes of history.
DEFAULT_HISTORY_MINUTES = 60
MAX_LOCATIONS = 4


def config_path() -> Path:
    return Path(os.environ.get(DEFAULT_CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))


class EumetsatCredentials(BaseModel):
    consumer_key: str = ""
    consumer_secret: str = ""

    def masked(self) -> "EumetsatCredentials":
        """Return a copy with the secret masked (for the UI/API)."""
        secret = self.consumer_secret
        if not secret:
            masked_secret = ""
        elif len(secret) <= 4:
            masked_secret = "*" * len(secret)
        else:
            masked_secret = "*" * (len(secret) - 4) + secret[-4:]
        return EumetsatCredentials(consumer_key=self.consumer_key, consumer_secret=masked_secret)


class RegionConfig(BaseModel):
    name: str
    lat: float
    lon: float
    radius_km: float = Field(gt=0)
    width: int = 800
    height: int = 800
    composite: str = DEFAULT_COMPOSITE
    # Refers to the ``id`` of an entry in the source catalog.
    source: str = "msg_seviri"
    max_frames: int | None = None
    # Draw country borders into the image (Natural Earth 1:10m, see overlay.py).
    borders: bool = True
    lonlat_extent: tuple[float, float, float, float] | None = None

    @field_validator("lonlat_extent")
    @classmethod
    def _validate_extent(
        cls, value: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if value is not None:
            west, south, east, north = value
            if not all(math.isfinite(v) for v in value) or not (
                -180 <= west < east <= 180 and -90 <= south < north <= 90
            ):
                raise ValueError("Invalid longitude/latitude extent")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or value.startswith("_") or "/" in value or value in (".", ".."):
            raise ValueError(
                f"Invalid region name '{value}' (must not be empty, contain '/' or start with '_')"
            )
        return value

    def bounding_box(self):
        if self.lonlat_extent is not None:
            west, south, east, north = self.lonlat_extent
            return BoundingBox(south, west, north, east)
        return bounding_box(self.lat, self.lon, self.radius_km)

    def effective_max_frames(
        self, poll_interval_minutes: int, history_minutes: int = DEFAULT_HISTORY_MINUTES
    ) -> int:
        if self.max_frames is not None:
            return self.max_frames
        interval = max(poll_interval_minutes, 1)
        return max(history_minutes // interval, 1)


class SourceDefinition(BaseModel):
    """Entry in the source catalog (editable as JSON in the UI)."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    driver: str
    label: str = ""
    collection: str | None = None
    enabled: bool = True
    description: str = ""
    # Capture cycle in minutes; empty = known cycle of the collection or
    # driver (see COLLECTION_CYCLES), otherwise the general poll interval.
    cycle_minutes: int | None = Field(default=None, gt=0)

    def known_cycle(self) -> int | None:
        if self.cycle_minutes:
            return self.cycle_minutes
        if self.collection and self.collection in COLLECTION_CYCLES:
            return COLLECTION_CYCLES[self.collection]
        return DRIVER_CYCLES.get(self.driver)

    @field_validator("driver")
    @classmethod
    def _validate_driver(cls, value: str) -> str:
        if value not in VALID_DRIVERS:
            raise ValueError(
                f"Unknown driver '{value}'. Allowed: {', '.join(VALID_DRIVERS)}"
            )
        return value


def default_catalog() -> list[SourceDefinition]:
    return [
        SourceDefinition(
            id="msg_seviri",
            driver="msg_seviri",
            label="MSG SEVIRI Rapid Scan (Europe, 5 min)",
            collection=DEFAULT_MSG_COLLECTION,
            enabled=True,
        ),
        SourceDefinition(
            id="msg_seviri_0deg",
            driver="msg_seviri",
            label="MSG SEVIRI 0° (Full Disk, 15 min)",
            collection="EO:EUM:DAT:MSG:HRSEVIRI",
            enabled=False,
        ),
        SourceDefinition(
            id="data_tailor",
            driver="data_tailor",
            label="MSG SEVIRI via Data Tailor (server-side cropping)",
            collection="EO:EUM:DAT:MSG:HRSEVIRI",
            enabled=False,
        ),
        SourceDefinition(
            id="mtg_fci",
            driver="mtg_fci",
            label="MTG FCI Level 1c Normal Resolution (10 min)",
            collection=DEFAULT_FCI_COLLECTION,
            enabled=False,
        ),
        SourceDefinition(
            id="dummy",
            driver="dummy",
            label="Test source (placeholder images)",
            enabled=False,
        ),
    ]


class SourcesConfig(BaseModel):
    catalog: list[SourceDefinition] = Field(default_factory=default_catalog)
    poll_interval_minutes: int = Field(default=15, gt=0)
    # Sync of the catalog with the EUMETSAT Data Store; 0 = off.
    auto_sync_hours: int = Field(default=24, ge=0)
    # Check the EUMETSAT RSS maintenance schedule every N hours and pause
    # Data Store queries of affected sources during maintenance; 0 = off.
    maintenance_check_hours: int = Field(default=6, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _migrate_active_list(cls, data: Any) -> Any:
        """Old configurations had ``active: [..]`` and ``msg_collection``
        instead of a catalog."""
        if isinstance(data, dict) and ("active" in data or "msg_collection" in data):
            data = dict(data)
            active = data.pop("active", None)
            msg_collection = data.pop("msg_collection", None)
            if "catalog" not in data:
                catalog = default_catalog()
                for entry in catalog:
                    if active is not None:
                        entry.enabled = entry.id in active
                    if msg_collection and entry.id == "msg_seviri":
                        entry.collection = msg_collection
                data["catalog"] = [entry.model_dump() for entry in catalog]
        return data

    @model_validator(mode="after")
    def _unique_ids(self) -> "SourcesConfig":
        seen: set[str] = set()
        for entry in self.catalog:
            if entry.id in seen:
                raise ValueError(f"Source ID '{entry.id}' is used more than once in the catalog")
            seen.add(entry.id)
        return self

    def get(self, source_id: str) -> SourceDefinition | None:
        for entry in self.catalog:
            if entry.id == source_id:
                return entry
        return None

    def is_enabled(self, source_id: str) -> bool:
        entry = self.get(source_id)
        return bool(entry and entry.enabled)

    def collection_for(self, source_id: str, default: str = DEFAULT_MSG_COLLECTION) -> str:
        entry = self.get(source_id)
        return entry.collection if entry and entry.collection else default

    def cycle_for(self, source_id: str) -> int:
        """Cycle (minutes) at which the source delivers new captures."""
        entry = self.get(source_id)
        return (entry.known_cycle() if entry else None) or self.poll_interval_minutes


class HistoryConfig(BaseModel):
    history_minutes: int = Field(default=DEFAULT_HISTORY_MINUTES, gt=0)
    max_storage_mb: int = Field(default=500, gt=0)


class StorageConfig(BaseModel):
    # Empty = default (<data dir>/frames, i.e. inside the /data volume).
    frames_dir: str = ""

    @field_validator("frames_dir")
    @classmethod
    def _validate_frames_dir(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith("/"):
            raise ValueError("The storage location must be an absolute path (e.g. /mnt/data/ha_satimage)")
        return value.rstrip("/") or ("/" if value else "")


class ArchiveConfig(BaseModel):
    """Raw data archive of MTG FCI chunks + archive of pre-rendered images.

    Downloads chunks ``chunk_min``..``chunk_max`` (default: Europe) plus
    all chunks needed by a configured region.

    With ``render_all`` every enabled source is downloaded at its own cycle
    and, after each new capture, every region is rendered in **every** image
    type of that source ("download all sources"). The results are stored as
    PNG + JPEG in the render archive and browsed there without re-rendering.
    """

    retention_hours: int = Field(default=12, ge=0, le=168)
    chunk_min: int = Field(default=32, ge=1, le=FCI_CHUNK_COUNT)
    chunk_max: int = Field(default=FCI_CHUNK_COUNT, ge=1, le=FCI_CHUNK_COUNT)
    # Download all enabled sources and render all image types per region.
    render_all: bool = False
    render_retention_hours: int = Field(default=24, ge=0, le=720)
    render_max_storage_mb: int = Field(default=2000, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "ArchiveConfig":
        if self.chunk_min > self.chunk_max:
            raise ValueError("Chunk range: 'from' must not be greater than 'to'")
        return self


class ServerConfig(BaseModel):
    mjpeg_fps: float = Field(default=2.0, gt=0)


class AppConfig(BaseModel):
    europe_overview_added: bool = True
    eumetsat: EumetsatCredentials = Field(default_factory=EumetsatCredentials)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    regions: list[RegionConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _regions_reference_catalog(self) -> "AppConfig":
        known = [entry.id for entry in self.sources.catalog]
        for region in self.regions:
            if region.source not in known:
                raise ValueError(
                    f"Region '{region.name}' refers to unknown source '{region.source}'. "
                    f"In catalog: {', '.join(known) or '-'}"
                )
        return self

    def max_frames_for(self, region: RegionConfig) -> int:
        return region.effective_max_frames(
            self.sources.cycle_for(region.source), self.history.history_minutes
        )

    def region(self, name: str) -> RegionConfig | None:
        for region in self.regions:
            if region.name == name:
                return region
        return None

    def masked(self) -> "AppConfig":
        data = self.model_dump()
        data["eumetsat"] = self.eumetsat.masked().model_dump()
        return AppConfig(**data)


def europe_overview(source: str = "msg_seviri") -> RegionConfig:
    return RegionConfig(
        name="europe",
        lat=52.5,
        lon=10,
        radius_km=2000,
        width=1400,
        height=1280,
        composite="natural_color_raw_with_night_ir",
        source=source,
        borders=True,
        lonlat_extent=EUROPE_EXTENT,
    )


def default_config() -> AppConfig:
    """Default configuration with two local regions and a European overview."""
    return AppConfig(
        regions=[
            RegionConfig(
                name="wien",
                lat=48.2082,
                lon=16.3738,
                radius_km=300,
                composite=DEFAULT_COMPOSITE,
                source="msg_seviri",
            ),
            RegionConfig(
                name="mallorca",
                lat=39.6953,
                lon=3.0176,
                radius_km=300,
                composite=DEFAULT_COMPOSITE,
                source="msg_seviri",
            ),
            europe_overview(),
        ]
    )


def env_overrides() -> dict[str, str]:
    """Return the credentials set via environment.

    Empty values do not count as an override: `docker compose` sets an empty
    variable for ``${VAR:-}``, which would otherwise mask the credentials
    saved in the UI.
    """
    overrides: dict[str, str] = {}
    for field, env_name in (
        ("consumer_key", ENV_CONSUMER_KEY),
        ("consumer_secret", ENV_CONSUMER_SECRET),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            overrides[field] = value
    return overrides


def _apply_env_overrides(config: AppConfig) -> AppConfig:
    overrides = env_overrides()
    if not overrides:
        return config
    data = config.model_dump()
    data["eumetsat"].update(overrides)
    return AppConfig(**data)


def load_config(path: Path | None = None) -> AppConfig:
    """Load the configuration from the YAML file (or return the default)."""
    resolved = path or config_path()
    if resolved.exists():
        raw: dict[str, Any] = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        config = _upgrade_config(raw, resolved)
    else:
        config = default_config()
    return _apply_env_overrides(config)


def _upgrade_config(raw: dict[str, Any], path: Path) -> AppConfig:
    config = AppConfig(**raw)
    if "europe_overview_added" not in raw:
        if config.region("europe") is None and len(config.regions) < MAX_LOCATIONS:
            candidates = [
                entry for entry in config.sources.catalog
                if entry.enabled and entry.driver in ("msg_seviri", "mtg_fci")
            ]
            if candidates:
                entry = candidates[0]
                overview = europe_overview(entry.id)
                if entry.driver == "mtg_fci":
                    overview.composite = default_composite_for(entry.driver, entry.collection)
                config.regions.append(overview)
                logger.info("Configuration upgrade: added European overview (borders on)")
            else:
                logger.info("Configuration upgrade: no enabled satellite source for European overview")
        else:
            logger.info("Configuration upgrade: European overview exists or all location slots are used")
        save_config(config, path)
    return config


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Save the configuration as a YAML file (creating the directory if needed)."""
    resolved = path or config_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


class ConfigStore:
    """Thread-safe in-memory access to the persisted configuration.

    Holds two views: ``stored()`` is exactly what is in the YAML file,
    ``get()`` is the effective configuration including env overrides.
    Changes must always build on ``stored()``, otherwise credentials set via
    the environment would be written to the file.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or config_path()
        self._lock = threading.RLock()
        self._stored = self._load_stored()
        self._config = _apply_env_overrides(self._stored)

    def _load_stored(self) -> AppConfig:
        if self._path.exists():
            raw: dict[str, Any] = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            return _upgrade_config(raw, self._path)
        return default_config()

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> AppConfig:
        with self._lock:
            return self._config

    def stored(self) -> AppConfig:
        with self._lock:
            return self._stored

    def update(self, new_config: AppConfig) -> AppConfig:
        with self._lock:
            save_config(new_config, self._path)
            self._stored = new_config
            self._config = _apply_env_overrides(new_config)
            return self._config

    def reload(self) -> AppConfig:
        with self._lock:
            self._stored = self._load_stored()
            self._config = _apply_env_overrides(self._stored)
            return self._config
