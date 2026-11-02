"""Rolling ring buffer for frames per region.

Stores PNG frames under ``/data/frames/{region}/`` and maintains an
index (``_index.json``) with their order. When new frames are added,
older frames beyond the configured count or beyond a storage limit (in
MB) are removed automatically. Files on disk that are not (or no longer)
in the index ("orphans") are cleaned up as well.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ha_satimage.atomic import write_bytes_atomic

INDEX_FILENAME = "_index.json"
# Derived media (thumbnails, animations), see media.py.
CACHE_DIRNAME = "_cache"

logger = logging.getLogger(__name__)


@dataclass
class Frame:
    filename: str
    created_at: str
    # Origin (catalog ID of the source + composite). After a source switch
    # only the newest frame of the same origin counts as "already rendered".
    source: str | None = None
    composite: str | None = None
    borders: bool | None = None

    def as_dict(self) -> dict:
        data = {"filename": self.filename, "created_at": self.created_at}
        if self.source is not None:
            data["source"] = self.source
        if self.composite is not None:
            data["composite"] = self.composite
        if self.borders is not None:
            data["borders"] = self.borders
        return data

    def path(self, region_dir: Path) -> Path:
        return region_dir / self.filename


@dataclass
class RingBuffer:
    """Ring buffer for a single region."""

    region_dir: Path
    max_frames: int = 4
    max_storage_mb: float = 500
    _frames: list[Frame] = field(default_factory=list, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        self.region_dir = Path(self.region_dir)
        self.region_dir.mkdir(parents=True, exist_ok=True)
        self._frames = self._load_index()
        self._cleanup()

    # -- Persistence -------------------------------------------------
    def _index_path(self) -> Path:
        return self.region_dir / INDEX_FILENAME

    def _load_index(self) -> list[Frame]:
        index_path = self._index_path()
        if not index_path.exists():
            return []
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            known = {"filename", "created_at", "source", "composite", "borders"}
            frames = [Frame(**{k: v for k, v in entry.items() if k in known}) for entry in raw]
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            # Never treat the frames as orphans because the index is broken
            # (e.g. power loss while writing): rebuild it from the files.
            logger.warning("Index of %s unreadable (%s) - rebuilding from files", self.region_dir, exc)
            return self._frames_from_files()
        # Keep only frames whose file actually exists.
        return [f for f in frames if f.path(self.region_dir).exists()]

    def _frames_from_files(self) -> list[Frame]:
        frames = []
        for path in sorted(self.region_dir.glob("*.png")):
            stem = path.stem.split("-", 1)[0]
            try:
                created = datetime.strptime(stem, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            frames.append(Frame(filename=path.name, created_at=created.isoformat()))
        return frames

    def _save_index(self) -> None:
        payload = [f.as_dict() for f in self._frames]
        write_bytes_atomic(self._index_path(), json.dumps(payload).encode("utf-8"))

    # -- Public API ------------------------------------------------
    def add_frame(
        self,
        data: bytes,
        timestamp: datetime | None = None,
        source: str | None = None,
        composite: str | None = None,
        borders: bool | None = None,
    ) -> Frame:
        """Add a new frame (PNG bytes) as the newest one and clean up.

        Order is insertion order: after switching to a source with an older
        capture (e.g. Rapid Scan -> 0°) the newly
        gerenderte Bild trotzdem Frame 0.
        """
        with self._lock:
            ts = timestamp or datetime.now(timezone.utc)
            stem = ts.strftime('%Y%m%dT%H%M%S%fZ')
            filename = f"{stem}.png"
            counter = 1
            while (self.region_dir / filename).exists():
                filename = f"{stem}-{counter}.png"
                counter += 1
            # Complete or not at all: an interrupted write leaves only a
            # ".part" file, which the orphan cleanup removes.
            write_bytes_atomic(self.region_dir / filename, data)
            frame = Frame(
                filename=filename, created_at=ts.isoformat(), source=source, composite=composite,
                borders=borders,
            )
            self._frames.append(frame)
            self._cleanup()
            self._save_index()
            return frame

    def frames_newest_first(self) -> list[Frame]:
        with self._lock:
            return list(reversed(self._frames))

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def get(self, index: int) -> Frame | None:
        """Return the frame at the index; 0 = newest, higher = older."""
        with self._lock:
            frames = self.frames_newest_first()
            if 0 <= index < len(frames):
                return frames[index]
            return None

    def by_filename(self, filename: str) -> Frame | None:
        with self._lock:
            for frame in self._frames:
                if frame.filename == filename:
                    return frame
            return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)

    # -- Cleanup -------------------------------------------------------
    def _cleanup(self) -> None:
        self._enforce_max_frames()
        self._enforce_storage_limit()
        self._remove_orphans()

    def _enforce_max_frames(self) -> None:
        while len(self._frames) > self.max_frames:
            oldest = self._frames.pop(0)
            self._delete_file(oldest.filename)

    def _enforce_storage_limit(self) -> None:
        limit_bytes = self.max_storage_mb * 1024 * 1024
        while self._frames and self._total_size() > limit_bytes:
            oldest = self._frames.pop(0)
            self._delete_file(oldest.filename)

    def _remove_orphans(self) -> None:
        known = {f.filename for f in self._frames}
        known.add(INDEX_FILENAME)
        for path in self.region_dir.iterdir():
            if path.is_file() and path.name not in known:
                path.unlink(missing_ok=True)
        # Thumbnails of deleted frames ("<stem>-w240.jpg"); animations are
        # managed by media.py itself.
        cache = self.region_dir / CACHE_DIRNAME
        if cache.is_dir():
            stems = {Path(f.filename).stem for f in self._frames}
            for path in cache.iterdir():
                if path.suffix == ".jpg" and path.stem.rsplit("-", 1)[0] not in stems:
                    path.unlink(missing_ok=True)

    def _total_size(self) -> int:
        total = 0
        for f in self._frames:
            path = f.path(self.region_dir)
            if path.exists():
                total += path.stat().st_size
        return total

    def _delete_file(self, filename: str) -> None:
        (self.region_dir / filename).unlink(missing_ok=True)

    def update_limits(self, max_frames: int, max_storage_mb: float) -> None:
        """Update max frames / storage limit and clean up right away."""
        with self._lock:
            self.max_frames = max_frames
            self.max_storage_mb = max_storage_mb
            self._cleanup()
            self._save_index()


class BufferManager:
    """Manages one RingBuffer per region."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self._buffers: dict[str, RingBuffer] = {}
        self._lock = threading.RLock()

    def get(self, region_name: str, max_frames: int, max_storage_mb: float) -> RingBuffer:
        with self._lock:
            buf = self._buffers.get(region_name)
            if buf is None:
                buf = RingBuffer(
                    region_dir=self.base_dir / region_name,
                    max_frames=max_frames,
                    max_storage_mb=max_storage_mb,
                )
                self._buffers[region_name] = buf
            else:
                buf.update_limits(max_frames, max_storage_mb)
            return buf

    def drop(self, region_name: str) -> None:
        """Delete all frames of a region (e.g. its location has changed)."""
        base = self.base_dir.resolve()
        target = (self.base_dir / region_name).resolve()
        if not region_name or region_name.startswith("_") or target.parent != base:
            raise ValueError(f"Invalid region name '{region_name}'")
        with self._lock:
            self._buffers.pop(region_name, None)
            shutil.rmtree(target, ignore_errors=True)

    def relocate(self, new_base_dir: Path, move_existing: bool) -> int:
        """Change the storage location; optionally move existing frames.

        If a region already exists at the target, frames are merged (index
        sorted by timestamp). Returns the number of moved frames.
        """
        new_base_dir = Path(new_base_dir)
        with self._lock:
            old_base_dir = self.base_dir
            moved = 0
            if move_existing and old_base_dir.exists() and old_base_dir.resolve() != new_base_dir.resolve():
                for region_dir in sorted(p for p in old_base_dir.iterdir() if p.is_dir()):
                    if region_dir.name.startswith("_"):
                        # Internal directories (e.g. FCI raw data archive) have
                        # no frame index and are moved as a whole.
                        _move_internal_dir(region_dir, new_base_dir / region_dir.name)
                        continue
                    moved += _merge_region_dir(region_dir, new_base_dir / region_dir.name)
            self.base_dir = new_base_dir
            self._buffers.clear()
            return moved


def _move_internal_dir(source: Path, target: Path) -> None:
    """Move subdirectories missing at the target; discard the rest."""
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir() and destination.exists():
            _move_internal_dir(child, destination)
        elif not destination.exists():
            shutil.move(str(child), str(destination))
    shutil.rmtree(source, ignore_errors=True)


def _merge_region_dir(source: Path, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)

    def _read(directory: Path) -> list[dict]:
        try:
            return json.loads((directory / INDEX_FILENAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    merged = {entry["filename"]: entry for entry in _read(target)}
    moved = 0
    for entry in _read(source):
        src_file = source / entry["filename"]
        if not src_file.exists():
            continue
        if entry["filename"] not in merged:
            shutil.move(str(src_file), str(target / entry["filename"]))
            merged[entry["filename"]] = entry
            moved += 1
    # Without existing frames at the target, insertion order is kept
    # (it differs from chronological order after source switches).
    entries = list(merged.values())
    if len(entries) != moved:
        entries.sort(key=lambda e: e["created_at"])
    (target / INDEX_FILENAME).write_text(json.dumps(entries), encoding="utf-8")
    shutil.rmtree(source, ignore_errors=True)
    logger.info("Moved %d frames from %s to %s", moved, source, target)
    return moved
