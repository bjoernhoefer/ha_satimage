"""Raw data archive for MTG FCI: only the required chunks per slot.

An FCI slot (every 10 minutes) consists of 40 stripes ("chunks") of the
full disk, numbered from south (1) to north (40), ~1 GB in total. For
Europe chunks ~32-40 (~180 MB) are enough. They are stored per slot under
``<storage location>/_archive/<collection>/<slot>/`` and kept for
``retention_hours``, so regions and composites can also be rendered
afterwards (including newly created regions).

Which chunks a region needs follows from the geometry: the bounding box is
transformed into the geostationary projection (satellite over 0°), and the
rows of the 2 km grid (5568 rows) determine the chunk.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ha_satimage.archive import SLOT_FORMAT, clear_archive_directory, cleared_before, slot_name, slot_time
from ha_satimage.config import (
    FCI_CHUNK_COUNT,
    AppConfig,
    EumetsatCredentials,
    RegionConfig,
)

logger = logging.getLogger(__name__)

ARCHIVE_SUBDIR = "_archive"
META_FILENAME = "meta.json"
SEARCH_WINDOW = timedelta(hours=2)

# Geostationary projection (CGMS/PROJ "geos", sweep=y) for MTG over 0°.
_SAT_HEIGHT = 35_786_400.0
_A = 6_378_137.0
_B = 6_356_752.31414
_GRID_LINES = 5568
_GRID_RES = 2000.0

_CHUNK_RE = re.compile(r"_(\d{4})\.nc$")
_SLOT_RE = re.compile(r"^\d{8}T\d{6}Z$")


class ArchiveError(Exception):
    """Error accessing the Data Store or the archive."""


class ClearedCapture(Exception):
    """The newest available scan was deliberately cleared; wait for a new one."""


def _geos_y(lat: float, lon: float, lon_0: float = 0.0) -> float | None:
    """North-south coordinate (m) in the geos projection; None = not visible."""
    phi, lam = math.radians(lat), math.radians(lon - lon_0)
    radius_p, radius_g = _B / _A, 1.0 + _SAT_HEIGHT / _A
    lat_c = math.atan(radius_p * radius_p * math.tan(phi))
    r = radius_p / math.hypot(radius_p * math.cos(lat_c), math.sin(lat_c))
    vx = r * math.cos(lam) * math.cos(lat_c)
    vy = r * math.sin(lam) * math.cos(lat_c)
    vz = r * math.sin(lat_c)
    tmp = radius_g - vx
    # Point lies on the side of the Earth facing away from the satellite.
    if (tmp * vx - vy * vy - vz * vz * (1.0 / (radius_p * radius_p))) < 0:
        return None
    return _SAT_HEIGHT * math.atan(vz / math.hypot(vy, tmp))


def chunk_for_y(y: float) -> int:
    row = (y + _GRID_LINES * _GRID_RES / 2) / _GRID_RES
    chunk = int(row // (_GRID_LINES / FCI_CHUNK_COUNT)) + 1
    return min(max(chunk, 1), FCI_CHUNK_COUNT)


def chunks_for_region(region: RegionConfig) -> set[int]:
    """Chunks covering the region's bounding box (empty = not visible)."""
    bbox = region.bounding_box()
    ys: list[float] = []
    steps = 24
    for i in range(steps + 1):
        lon = bbox.lon_min + (bbox.lon_max - bbox.lon_min) * i / steps
        lat = bbox.lat_min + (bbox.lat_max - bbox.lat_min) * i / steps
        for point in ((bbox.lat_min, lon), (bbox.lat_max, lon), (lat, bbox.lon_min), (lat, bbox.lon_max)):
            y = _geos_y(*point)
            if y is not None:
                ys.append(y)
    if not ys:
        return set()
    return set(range(chunk_for_y(min(ys)), chunk_for_y(max(ys)) + 1))


def wanted_chunks(config: AppConfig) -> set[int]:
    """Configured range plus all chunks of the configured regions."""
    chunks = set(range(config.archive.chunk_min, config.archive.chunk_max + 1))
    for region in config.regions:
        chunks |= chunks_for_region(region)
    return chunks


def format_chunks(chunks: set[int] | list[int]) -> str:
    """{32, 33, 34, 36} -> "32–34, 36"."""
    ordered = sorted(set(chunks))
    parts: list[str] = []
    start = prev = None
    for c in ordered + [None]:
        if start is None:
            start = prev = c
        elif c is not None and c == prev + 1:
            prev = c
        else:
            parts.append(str(start) if start == prev else f"{start}–{prev}")
            start = prev = c
    return ", ".join(parts) or "–"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _safe_name(collection_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", collection_id) or "_"


def chunk_of(filename: str) -> int | None:
    if "TRAIL" in filename:
        return None
    match = _CHUNK_RE.search(filename)
    if not match:
        return None
    chunk = int(match.group(1))
    return chunk if 1 <= chunk <= FCI_CHUNK_COUNT else None


@dataclass(frozen=True)
class Slot:
    name: str
    collection: str
    product_id: str
    sensing_start: datetime
    sensing_end: datetime
    path: Path

    @property
    def archive_time(self) -> datetime:
        """Stable cache identity from the raw directory, including legacy slots.

        Metadata and Satpy times can differ in timezone, seconds or precision.
        The raw slot name is also the viewer URL and must identify its renders.
        """
        return datetime.strptime(self.name, SLOT_FORMAT).replace(tzinfo=timezone.utc)

    def chunk_files(self) -> dict[int, Path]:
        files: dict[int, Path] = {}
        if not self.path.is_dir():
            return files
        for entry in self.path.iterdir():
            chunk = chunk_of(entry.name) if entry.is_file() else None
            if chunk is not None:
                files[chunk] = entry
        return files

    @property
    def chunks(self) -> list[int]:
        return sorted(self.chunk_files())

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "collection": self.collection,
            "product_id": self.product_id,
            "sensing_start": self.sensing_start.isoformat(),
            "sensing_end": self.sensing_end.isoformat(),
            "chunks": self.chunks,
        }


class FciArchive:
    """Downloads the wanted chunks per slot and cleans up after the retention time."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._status: dict[str, dict] = {}

    def clear(self, cutoff: datetime) -> int:
        with self._lock:
            count = sum(len(self.slots(collection)) for collection in self.collections())
            clear_archive_directory(self.root, cutoff)
            self._status.clear()
            logger.info("Cleared %d FCI raw slots; only scans after %s will be downloaded", count, cutoff)
            return count

    # -- Reading -----------------------------------------------------------
    def collection_dir(self, collection: str) -> Path:
        return self.root / _safe_name(collection)

    def _read_slot(self, collection: str, path: Path) -> Slot | None:
        if slot_time(path.name) is None:
            return None
        try:
            meta = json.loads((path / META_FILENAME).read_text(encoding="utf-8"))
            return Slot(
                name=path.name,
                collection=collection,
                product_id=meta["product_id"],
                sensing_start=_as_utc(datetime.fromisoformat(meta["sensing_start"])),
                sensing_end=_as_utc(datetime.fromisoformat(meta["sensing_end"])),
                path=path,
            )
        except (OSError, ValueError, KeyError):
            return None

    def slots(self, collection: str) -> list[Slot]:
        """All completely written slots, newest first."""
        with self._lock:
            directory = self.collection_dir(collection)
            if not directory.is_dir():
                return []
            slots = [
                slot
                for path in directory.iterdir()
                if path.is_dir() and _SLOT_RE.match(path.name)
                and (slot := self._read_slot(collection, path)) is not None
            ]
            return sorted(slots, key=lambda s: s.sensing_end, reverse=True)

    def slot(self, collection: str, name: str) -> Slot | None:
        if not _SLOT_RE.match(name):
            return None
        return self._read_slot(collection, self.collection_dir(collection) / name)

    def status(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._status.items()}

    # -- Download ----------------------------------------------------------
    def sync(
        self,
        collection: str,
        credentials: EumetsatCredentials,
        chunks: set[int],
        retention_hours: int,
    ) -> Slot:
        """Ensure the newest slot (download missing chunks), remove old ones."""
        with self._lock:
            status = self._status.setdefault(collection, {})
            status["last_sync_at"] = datetime.now(timezone.utc).isoformat()
            try:
                slot = self._sync(collection, credentials, chunks)
            except ArchiveError as exc:
                status["last_error"] = str(exc)
                raise
            status["last_error"] = None
            status["newest"] = slot.name
            self.prune(collection, retention_hours)
            return slot

    def _sync(self, collection: str, credentials: EumetsatCredentials, chunks: set[int]) -> Slot:
        if not credentials.consumer_key or not credentials.consumer_secret:
            raise ArchiveError(
                "No EUMETSAT credentials configured (web UI or environment variables)"
            )
        import eumdac  # expensive import, only load when actually needed

        with self._lock:
            try:
                token = eumdac.AccessToken((credentials.consumer_key, credentials.consumer_secret))
                store_collection = eumdac.DataStore(token).get_collection(collection)
                now = datetime.now(timezone.utc)
                results = store_collection.search(
                    dtstart=(now - SEARCH_WINDOW).replace(tzinfo=None),
                    dtend=now.replace(tzinfo=None),
                )
                newest = next(iter(results), None)
            except Exception as exc:
                raise ArchiveError(f"Data Store search failed: {exc}") from exc
            if newest is None:
                raise ArchiveError(f"No product in {collection} within the last 2 hours")

            product_id = str(newest)
            sensing_end = _as_utc(newest.sensing_end)
            cutoff = cleared_before(self.root)
            if cutoff is not None and sensing_end <= cutoff:
                raise ClearedCapture(f"Scan {product_id} predates Clear archive; waiting for a new scan")
            sensing_start = _as_utc(getattr(newest, "sensing_start", None) or sensing_end)
            slot_dir = self.collection_dir(collection) / slot_name(sensing_end)
            slot = Slot(slot_dir.name, collection, product_id, sensing_start, sensing_end, slot_dir)

            present = slot.chunk_files()
            entries = {}
            for entry in newest.entries:
                chunk = chunk_of(entry)
                if chunk in chunks and chunk not in present:
                    entries[chunk] = entry
            if entries:
                logger.info(
                    "FCI %s: downloading chunks %s (%s)",
                    slot.name, format_chunks(list(entries)), collection,
                )
            slot_dir.mkdir(parents=True, exist_ok=True)
            try:
                for chunk, entry in sorted(entries.items()):
                    target = slot_dir / entry.split("/")[-1]
                    partial = target.with_name(target.name + ".part")
                    try:
                        with newest.open(entry=entry) as source, partial.open("wb") as sink:
                            shutil.copyfileobj(source, sink, length=1024 * 1024)
                        partial.rename(target)
                    except Exception:
                        partial.unlink(missing_ok=True)
                        raise
            except Exception as exc:
                if not slot.chunk_files():
                    shutil.rmtree(slot_dir, ignore_errors=True)
                raise ArchiveError(f"Download of {product_id} failed: {exc}") from exc

            meta = {
                "product_id": product_id,
                "sensing_start": sensing_start.isoformat(),
                "sensing_end": sensing_end.isoformat(),
            }
            tmp = slot_dir / (META_FILENAME + ".part")
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            tmp.rename(slot_dir / META_FILENAME)
            return slot

    # -- Cleanup -----------------------------------------------------------
    def prune(self, collection: str, retention_hours: int) -> int:
        """Deletes slots older than the retention time; the newest one is kept."""
        slots = self.slots(collection)
        if not slots:
            return 0
        cutoff = slots[0].sensing_end - timedelta(hours=retention_hours)
        removed = 0
        for slot in slots[1:]:
            if slot.sensing_end <= cutoff:
                shutil.rmtree(slot.path, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("FCI archive %s: removed %d old slots", collection, removed)
        return removed

    def prune_all(self, retention_hours: int) -> int:
        """Clean up all collections in the archive (including no longer configured ones)."""
        if not self.root.is_dir():
            return 0
        return sum(
            self.prune(directory.name.replace("_", ":"), retention_hours)
            for directory in self.root.iterdir()
            if directory.is_dir()
        )

    def collections(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(d.name.replace("_", ":") for d in self.root.iterdir() if d.is_dir())


_archives: dict[Path, FciArchive] = {}
_archives_lock = threading.Lock()


def get_archive(root: Path) -> FciArchive:
    root = Path(root)
    with _archives_lock:
        if root not in _archives:
            _archives[root] = FciArchive(root)
        return _archives[root]
