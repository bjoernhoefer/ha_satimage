"""Archive of pre-rendered images: every source in every image type.

While the ring buffer (``buffer.py``) keeps the *configured* image type of
a region, the archive keeps **all combinations**: for every region, every
enabled source and every image type (composite) of that source's driver one
image per capture. They are rendered right after a source delivered a new
capture ("download all sources"), so the archive viewer is as fast as the
normal viewer - nothing is ever rendered while browsing.

Layout (below the frame storage location)::

    _renders/<region>/available.json
    _renders/<region>/<source>/<composite>/<YYYYmmddTHHMMSSZ>.png
                                          /<YYYYmmddTHHMMSSZ>.jpg

``available.json`` is the list of available images of a region. It is
updated whenever an image is stored, updated or removed, and it is the only
thing the archive viewer reads - no directory lookups while browsing. A
missing or unreadable list (e.g. after an upgrade) is rebuilt once from the
files on disk.

The slot name is the capture time (end of scan), so the same capture is
rendered only once per combination. Cleanup is by age
(``archive.render_retention_hours``) and by a storage limit
(``archive.render_max_storage_mb``, oldest first).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ha_satimage.overlay import OVERLAY_VERSION

logger = logging.getLogger(__name__)

RENDERS_SUBDIR = "_renders"
INDEX_FILE = "available.json"
SLOT_FORMAT = "%Y%m%dT%H%M%SZ"

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SLOT_RE = re.compile(r"^\d{8}T\d{6}Z$")


def slot_name(sensing: datetime) -> str:
    if sensing.tzinfo is None:
        sensing = sensing.replace(tzinfo=timezone.utc)
    return sensing.astimezone(timezone.utc).strftime(SLOT_FORMAT)


def slot_time(name: str) -> datetime | None:
    if not _SLOT_RE.match(name):
        return None
    try:
        return datetime.strptime(name, SLOT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _valid(segment: str) -> bool:
    return bool(_SEGMENT_RE.match(segment)) and segment not in (".", "..")


def region_signature(region) -> str:
    """Cut-out of a region; a change invalidates its archived images."""
    return "|".join(
        str(v)
        for v in (
            region.lat, region.lon, region.radius_km,
            region.width, region.height, region.borders, OVERLAY_VERSION,
        )
    )


@dataclass(frozen=True)
class ArchivedImage:
    region: str
    source: str
    composite: str
    slot: str
    created_at: datetime
    path: Path
    version: int | None = None
    jpeg_version: int | None = None

    @property
    def jpeg_path(self) -> Path:
        return self.path.with_suffix(".jpg")

    def as_dict(self, region_name: str) -> dict:
        base = (
            f"/regions/{region_name}/archive/{self.source}/{self.composite}/{self.slot}"
        )
        version = self.version or 0
        jpeg_version = self.jpeg_version
        return {
            "name": self.slot,
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "composite": self.composite,
            "available": True,
            "cached": True,
            "url": f"{base}.png?v={version}",
            "jpeg_url": f"{base}.jpg?v={jpeg_version}" if jpeg_version else f"{base}.png?v={version}",
        }


class RenderArchive:
    """Stores and serves pre-rendered images per region/source/composite."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        # Region -> (file stamp, parsed list): avoids re-parsing per lookup.
        self._index_cache: dict[str, tuple[tuple[int, int], dict]] = {}

    # -- Paths -------------------------------------------------------------
    def dir_for(self, region: str, source: str, composite: str) -> Path | None:
        if not all(_valid(part) for part in (region, source, composite)):
            return None
        return self.root / region / source / composite

    def path_for(
        self, region: str, source: str, composite: str, slot: str, ext: str = "png"
    ) -> Path | None:
        directory = self.dir_for(region, source, composite)
        if directory is None or not _SLOT_RE.match(slot) or ext not in ("png", "jpg"):
            return None
        return directory / f"{slot}.{ext}"

    def has(self, region: str, source: str, composite: str, sensing: datetime) -> bool:
        """Whether the image is in the list of available images."""
        path = self.path_for(region, source, composite, slot_name(sensing))
        if path is None or not (self.root / region).is_dir():
            return False
        slots = self._read_index(region).get(source, {}).get(composite, {})
        return slot_name(sensing) in slots

    # -- Writing -----------------------------------------------------------
    def store(
        self,
        region: str,
        source: str,
        composite: str,
        sensing: datetime,
        png: bytes,
    ) -> ArchivedImage | None:
        """Write PNG (plus a JPEG for fast browsing); ``None`` on invalid names."""
        slot = slot_name(sensing)
        path = self.path_for(region, source, composite, slot)
        if path is None:
            logger.warning(
                "Archive: invalid name %s/%s/%s - not stored", region, source, composite
            )
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(path, png)
        jpeg = path.with_suffix(".jpg")
        try:
            from ha_satimage.media import to_jpeg

            self._write_atomic(jpeg, to_jpeg(png, None))
        except Exception:  # pragma: no cover - defensive, PNG stays usable
            logger.exception("Archive: could not create the JPEG for %s", path.name)
            jpeg.unlink(missing_ok=True)
        # Listed only once both files are complete.
        versions = self._versions(path)
        with self._lock:
            index = self._read_index(region)
            index.setdefault(source, {}).setdefault(composite, {})[slot] = versions
            self._write_index(region, index)
        return ArchivedImage(
            region, source, composite, slot, slot_time(slot) or sensing, path,
            versions["png"], versions["jpg"],
        )

    @staticmethod
    def _versions(path: Path) -> dict:
        jpeg = path.with_suffix(".jpg")
        return {
            "png": path.stat().st_mtime_ns,
            "jpg": jpeg.stat().st_mtime_ns if jpeg.exists() else None,
        }

    # -- List of available images -------------------------------------------
    def _index_path(self, region: str) -> Path:
        return self.root / region / INDEX_FILE

    def _read_index(self, region: str) -> dict:
        """Source -> composite -> slot -> file versions of the region."""
        path = self._index_path(region)
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = self._index_cache.get(region)
            if cached is not None and cached[0] == stamp:
                return cached[1]
            data = json.loads(path.read_text(encoding="utf-8"))
            images = data["images"]
            if isinstance(images, dict):
                self._index_cache[region] = (stamp, images)
                return images
            raise TypeError("images is not an object")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Archive %s: list of available images unreadable - rebuilding", region)
        with self._lock:
            # A reader must never overwrite a list a writer created meanwhile.
            return self._rebuild_index(region, only_if_missing=True)

    def _rebuild_index(self, region: str, only_if_missing: bool = False) -> dict:
        """Build the list from the files on disk (upgrade / repair only)."""
        base = self.root / region
        images: dict = {}
        if not base.is_dir():
            return images
        for source_dir in base.iterdir():
            if not source_dir.is_dir() or not _valid(source_dir.name):
                continue
            for composite_dir in source_dir.iterdir():
                if not composite_dir.is_dir() or not _valid(composite_dir.name):
                    continue
                for path in composite_dir.glob("*.png"):
                    if slot_time(path.stem) is None:
                        continue
                    images.setdefault(source_dir.name, {}).setdefault(
                        composite_dir.name, {}
                    )[path.stem] = self._versions(path)
        self._write_index(region, images, only_if_missing)
        return images

    def _write_index(self, region: str, images: dict, only_if_missing: bool = False) -> None:
        cleaned = {
            source: {c: slots for c, slots in composites.items() if slots}
            for source, composites in images.items()
        }
        cleaned = {s: c for s, c in cleaned.items() if c}
        path = self._index_path(region)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"images": cleaned}, sort_keys=True).encode("utf-8")
        if only_if_missing:
            partial = self._partial(path)
            partial.write_bytes(data)
            try:
                os.link(partial, path)
            except FileExistsError:
                return
            finally:
                partial.unlink(missing_ok=True)
        else:
            self._write_atomic(path, data)
        stat = path.stat()
        self._index_cache[region] = ((stat.st_mtime_ns, stat.st_size), cleaned)

    @staticmethod
    def _partial(path: Path) -> Path:
        # Unique per writer: concurrent writers must not share a temp file.
        return path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        partial = RenderArchive._partial(path)
        partial.write_bytes(data)
        os.replace(partial, path)

    # -- Reading -----------------------------------------------------------
    def images(self, region: str, source: str, composite: str) -> list[ArchivedImage]:
        """All archived images of the combination, newest first."""
        directory = self.dir_for(region, source, composite)
        if directory is None or not (self.root / region).is_dir():
            return []
        slots = self._read_index(region).get(source, {}).get(composite, {})
        images = []
        for slot, versions in slots.items():
            created = slot_time(slot)
            if created is None or not isinstance(versions, dict):
                continue
            images.append(ArchivedImage(
                region, source, composite, slot, created, directory / f"{slot}.png",
                versions.get("png"), versions.get("jpg"),
            ))
        return sorted(images, key=lambda i: i.created_at, reverse=True)

    def combinations(self, region: str) -> dict[str, list[str]]:
        """Source ID -> image types with archived images (for the UI)."""
        if not _valid(region):
            return {}
        if not (self.root / region).is_dir():
            return {}
        result: dict[str, list[str]] = {}
        for source, composites in sorted(self._read_index(region).items()):
            names = sorted(name for name, slots in composites.items() if slots)
            if names:
                result[source] = names
        return result

    def regions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    # -- Region signature ---------------------------------------------------
    def sync_region(self, region_name: str, signature: str) -> bool:
        """Update overlays in place; drop images only for incompatible cut-outs.

        Geometry changes and removing baked-in borders invalidate the archive.
        Adding borders must not delete historical scans: SEVIRI raw products
        are not retained. Legacy five-field signatures predate borders.
        Returns ``True`` when the signature changed.
        """
        if not _valid(region_name):
            return False
        with self._lock:
            base = self.root / region_name
            marker = base / "region.json"
            try:
                current = marker.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == signature:
                return False
            old = current.split("|") if current else []
            new = signature.split("|")
            same_geometry = len(old) in (5, 6, 7) and old[:5] == new[:5]
            had_borders = len(old) >= 6 and old[5] == "True"
            has_borders = len(new) >= 6 and new[5] == "True"
            if same_geometry and (has_borders or not had_borders):
                if has_borders:
                    self._update_overlays(region_name, signature, had_borders)
            elif current is not None or (base.is_dir() and any(p.is_dir() for p in base.iterdir())):
                shutil.rmtree(base, ignore_errors=True)
                logger.info("Archive %s: cut-out changed - archived images removed", region_name)
            base.mkdir(parents=True, exist_ok=True)
            self._write_atomic(marker, signature.encode("utf-8"))
            return True

    def _update_overlays(self, region: str, signature: str, had_borders: bool) -> None:
        from PIL import Image, PngImagePlugin

        from ha_satimage.media import to_jpeg
        from ha_satimage.overlay import draw_borders, draw_coastlines

        lat, lon, radius = map(float, signature.split("|")[:3])
        draw = draw_coastlines if had_borders else draw_borders
        updated = 0
        for source, composites in self.combinations(region).items():
            for composite in composites:
                for archived in self.images(region, source, composite):
                    with Image.open(archived.path) as original:
                        # A retry after an interrupted migration must not darken
                        # already updated anti-aliased lines again.
                        if original.info.get("archive_overlay") == signature:
                            self._write_atomic(
                                archived.jpeg_path, to_jpeg(archived.path.read_bytes(), None)
                            )
                            continue
                        image = original.convert("RGB")
                    draw(image, lat, lon, radius)
                    metadata = PngImagePlugin.PngInfo()
                    metadata.add_text("archive_overlay", signature)
                    png = io.BytesIO()
                    image.save(png, format="PNG", pnginfo=metadata)
                    data = png.getvalue()
                    self._write_atomic(archived.path, data)
                    # Keep the region marker unchanged if either write fails,
                    # so an interrupted upgrade resumes on the next run.
                    self._write_atomic(archived.jpeg_path, to_jpeg(data, None))
                    updated += 1
        self._rebuild_index(region)
        logger.info("Archive %s: updated borders on %d historical images", region, updated)

    def used_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def count(self) -> int:
        return sum(
            len(slots)
            for region in self.regions()
            for composites in self._read_index(region).values()
            for slots in composites.values()
        )

    def summary(self) -> dict:
        regions = {}
        for region in self.regions():
            images = [
                image
                for source, composites in self.combinations(region).items()
                for composite in composites
                for image in self.images(region, source, composite)
            ]
            if not images:
                continue
            regions[region] = {
                "images": len(images),
                "combinations": sum(
                    len(c) for c in self.combinations(region).values()
                ),
                "newest": max(i.created_at for i in images).isoformat(),
                "oldest": min(i.created_at for i in images).isoformat(),
            }
        return {
            "dir": str(self.root),
            "images": self.count(),
            "used_bytes": self.used_bytes(),
            "regions": regions,
        }

    # -- Cleanup -----------------------------------------------------------
    def _all_images(self) -> list[ArchivedImage]:
        return [
            image
            for region in self.regions()
            for source, composites in self.combinations(region).items()
            for composite in composites
            for image in self.images(region, source, composite)
        ]

    def _remove(self, images: list[ArchivedImage]) -> None:
        # Unlisted first, so the viewer never offers a file being deleted.
        for region in {image.region for image in images}:
            index = self._read_index(region)
            for image in images:
                if image.region == region:
                    index.get(image.source, {}).get(image.composite, {}).pop(image.slot, None)
            self._write_index(region, index)
        for image in images:
            image.path.unlink(missing_ok=True)
            image.jpeg_path.unlink(missing_ok=True)

    def prune(self, retention_hours: int, max_storage_mb: float) -> int:
        """Remove images older than the retention time and beyond the limit."""
        with self._lock:
            images = self._all_images()
            if not images:
                return 0
            newest = max(i.created_at for i in images)
            cutoff = newest - timedelta(hours=max(retention_hours, 0))
            doomed: list[ArchivedImage] = []
            keep: list[ArchivedImage] = []
            for image in images:
                if image.created_at < cutoff:
                    doomed.append(image)
                else:
                    keep.append(image)
            limit = max_storage_mb * 1024 * 1024
            keep.sort(key=lambda i: i.created_at)
            total = sum(self._size(i) for i in keep)
            while keep and total > limit:
                oldest = keep.pop(0)
                total -= self._size(oldest)
                doomed.append(oldest)
            removed = len(doomed)
            if doomed:
                self._remove(doomed)
            self._drop_empty_dirs()
            if removed:
                logger.info("Archive: removed %d rendered images", removed)
            return removed

    def forget(self, regions: set[str]) -> int:
        """Remove archive directories of regions that no longer exist."""
        removed = 0
        for region in self.regions():
            if region not in regions:
                shutil.rmtree(self.root / region, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("Archive: removed %d directories of deleted regions", removed)
        return removed

    @staticmethod
    def _size(image: ArchivedImage) -> int:
        total = 0
        for path in (image.path, image.jpeg_path):
            if path.exists():
                total += path.stat().st_size
        return total

    def _drop_empty_dirs(self) -> None:
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("*"), key=lambda p: -len(p.parts)):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()


_archives: dict[Path, RenderArchive] = {}
_archives_lock = threading.Lock()


def get_render_archive(root: Path) -> RenderArchive:
    root = Path(root)
    with _archives_lock:
        if root not in _archives:
            _archives[root] = RenderArchive(root)
        return _archives[root]
