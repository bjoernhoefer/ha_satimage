"""Source abstraction for the various EUMETSAT data sources.

Each source implements ``render(region, config, last_sensing) ->
RenderedFrame``. ``msg_seviri`` and ``mtg_fci`` load real data from the
EUMETSAT Data Store and render it with Satpy; ``data_tailor`` still
delivers placeholder images ("dummy frames") until it is implemented.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from ha_satimage.config import AppConfig, RegionConfig

if TYPE_CHECKING:
    from ha_satimage.config import SourceDefinition
    from ha_satimage.sources.eumetsat import ProductCache

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised when a frame could not be produced."""


class NoNewData(Exception):
    """No new acquisition since the last frame - not an error."""


@dataclass(frozen=True)
class RenderedFrame:
    png: bytes
    # Sensing time (end of scan) - becomes the frame timestamp.
    sensing_time: datetime


def data_dir() -> Path:
    return Path(os.environ.get("HA_SATIMAGE_DATA_DIR", "/data"))


class Source(ABC):
    """Base class for interchangeable image sources."""

    name: str = "base"
    # Separate download step (fetch) at the source's cycle?
    downloads: bool = False
    # Download even if no region uses the source (raw data archive).
    archive_always: bool = False

    def fetch(self, entry: "SourceDefinition", config: AppConfig) -> datetime:
        """Make the newest product available locally (idempotent, no rendering).

        Returns the sensing end of the newest locally available product.
        """
        raise NotImplementedError

    @abstractmethod
    def render(
        self,
        region: RegionConfig,
        config: AppConfig,
        last_sensing: datetime | None = None,
    ) -> RenderedFrame:
        """Produces a frame for the region.

        ``last_sensing`` is the sensing time of the newest frame in the
        buffer; if there is no newer acquisition, ``NoNewData`` is raised.
        """
        raise NotImplementedError

    def _dummy(self, region: RegionConfig) -> RenderedFrame:
        return RenderedFrame(self._dummy_frame(region), datetime.now(timezone.utc))

    def _dummy_frame(self, region: RegionConfig) -> bytes:
        """Shared placeholder renderer until the real source is available."""
        bbox = region.bounding_box()
        image = Image.new("RGB", (region.width, region.height), color=(10, 20, 40))
        draw = ImageDraw.Draw(image)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            f"ha_satimage ({self.name})",
            f"Region: {region.name}",
            f"Composite: {region.composite}",
            f"BBox: {bbox.lat_min:.2f},{bbox.lon_min:.2f} .. {bbox.lat_max:.2f},{bbox.lon_max:.2f}",
            timestamp,
        ]
        y = 20
        for line in lines:
            draw.text((20, y), line, fill=(255, 255, 255))
            y += 24
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


class DummySource(Source):
    """Pure test source that only produces placeholder images."""

    name = "dummy"

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        return self._dummy(region)


_product_caches: dict[Path, ProductCache] = {}


def _product_cache() -> ProductCache:
    from ha_satimage.sources.eumetsat import ProductCache

    cache_dir = data_dir() / "cache"
    if cache_dir not in _product_caches:
        _product_caches[cache_dir] = ProductCache(cache_dir)
    return _product_caches[cache_dir]


class MsgSeviriSource(Source):
    """MSG/SEVIRI from the Data Store + local Satpy rendering.

    A product (~100 MB) is downloaded once and used by all regions;
    rendering happens in a child process.
    """

    name = "msg_seviri"
    reader = "seviri_l1b_native"
    entry_suffix = ".nat"
    downloads = True

    def _download(self, collection: str, config: AppConfig):
        from ha_satimage.sources.eumetsat import DataStoreError

        try:
            return _product_cache().latest(collection, config.eumetsat, self.entry_suffix)
        except DataStoreError as exc:
            raise RenderError(str(exc)) from exc

    def fetch(self, entry, config) -> datetime:
        return self._download(entry.collection or "", config).sensing_end

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        from ha_satimage.sources.satpy_render import (
            RenderRequest,
            SatpyRenderError,
            render_in_subprocess,
        )

        collection = config.sources.collection_for(region.source)
        # Normally the download job has just fetched the product; only download
        # it here if there is no local product (e.g. right after startup).
        product = _product_cache().cached(collection) or self._download(collection, config)
        if last_sensing is not None and product.sensing_end <= last_sensing:
            raise NoNewData(f"{product.product_id} already rendered")

        request = RenderRequest(
            reader=self.reader,
            filenames=(str(product.path),),
            composite=region.composite,
            lat=region.lat,
            lon=region.lon,
            radius_km=region.radius_km,
            width=region.width,
            height=region.height,
            label=region.name,
            borders=region.borders,
        )
        try:
            png, sensing_end = render_in_subprocess(request)
        except SatpyRenderError as exc:
            raise RenderError(str(exc)) from exc
        if sensing_end.tzinfo is None:
            sensing_end = sensing_end.replace(tzinfo=timezone.utc)
        return RenderedFrame(png, sensing_end)


class DataTailorSource(Source):
    """Server-side cropping via EUMETSAT Data Tailor (to come)."""

    name = "data_tailor"

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        logger.warning(
            "data_tailor: integration not implemented yet, delivering dummy frame for %s",
            region.name,
        )
        return self._dummy(region)


def render_with(
    entry: "SourceDefinition",
    region: RegionConfig,
    composite: str,
    config: AppConfig,
) -> RenderedFrame:
    """Render a region with an explicit source and image type.

    Used by the archive ("download all sources"): the same locally stored
    product is rendered once per image type, independent of the region's
    own configuration.
    """
    from ha_satimage.config import resolve_fci_composite

    if entry.driver == "mtg_fci":
        composite = resolve_fci_composite(composite, entry.collection)
    variant = region.model_copy(update={"source": entry.id, "composite": composite})
    return get_source(entry.driver).render(variant, config, None)


def archive_composites(entry: "SourceDefinition") -> list[str]:
    """Image types archived for a source (all choices of its driver)."""
    from ha_satimage.config import composites_for, resolve_fci_composite

    names = list(composites_for(entry.driver, entry.collection))
    if entry.driver == "mtg_fci":
        names = list(dict.fromkeys(resolve_fci_composite(n, entry.collection) for n in names))
    return names


def frames_root(config: AppConfig) -> Path:
    return Path(config.storage.frames_dir) if config.storage.frames_dir else data_dir() / "frames"


def archive_root(config: AppConfig) -> Path:
    from ha_satimage.sources.fci_archive import ARCHIVE_SUBDIR

    return frames_root(config) / ARCHIVE_SUBDIR


def render_archive_root(config: AppConfig) -> Path:
    from ha_satimage.archive import RENDERS_SUBDIR

    return frames_root(config) / RENDERS_SUBDIR


def render_fci_slot(
    slot, region: RegionConfig, composite: str, collection: str | None = None
) -> RenderedFrame:
    """Renders a region from an archived FCI slot."""
    from ha_satimage.config import resolve_fci_composite
    from ha_satimage.sources.fci_archive import chunks_for_region, format_chunks
    from ha_satimage.sources.satpy_render import (
        RenderRequest,
        SatpyRenderError,
        render_in_subprocess,
    )

    needed = chunks_for_region(region)
    if not needed:
        raise RenderError(f"Region {region.name} is outside the FCI full disk")
    files = slot.chunk_files()
    missing = needed - set(files)
    if missing:
        raise RenderError(
            f"Slot {slot.name} does not contain all chunks for {region.name} "
            f"(needs {format_chunks(needed)}, missing {format_chunks(missing)})"
        )
    request = RenderRequest(
        reader="fci_l1c_nc",
        filenames=tuple(str(files[c]) for c in sorted(needed)),
        composite=resolve_fci_composite(composite, collection),
        lat=region.lat,
        lon=region.lon,
        radius_km=region.radius_km,
        width=region.width,
        height=region.height,
        label=region.name,
        borders=region.borders,
    )
    try:
        png, sensing_end = render_in_subprocess(request)
    except SatpyRenderError as exc:
        raise RenderError(str(exc)) from exc
    if sensing_end.tzinfo is None:
        sensing_end = sensing_end.replace(tzinfo=timezone.utc)
    return RenderedFrame(png, sensing_end)


def sync_fci_archive(config: AppConfig, collection: str):
    """Download the newest FCI slot into the archive (idempotent), returns the slot."""
    from ha_satimage.config import DEFAULT_FCI_COLLECTION
    from ha_satimage.sources.fci_archive import ArchiveError, ClearedCapture, get_archive, wanted_chunks

    try:
        return get_archive(archive_root(config)).sync(
            collection or DEFAULT_FCI_COLLECTION,
            config.eumetsat,
            wanted_chunks(config),
            config.archive.retention_hours,
        )
    except ClearedCapture as exc:
        raise NoNewData(str(exc)) from exc
    except ArchiveError as exc:
        raise RenderError(str(exc)) from exc


class MtgFciSource(Source):
    """MTG/FCI (1 km visible, 2 km IR) from the chunk archive.

    Per slot only the required chunks are downloaded (Europe ~180 MB instead
    of ~1 GB) and kept for ``archive.retention_hours``; only the region's
    chunks are rendered (keeps memory below ~500 MB).
    """

    name = "mtg_fci"
    downloads = True
    archive_always = True

    def fetch(self, entry, config) -> datetime:
        return sync_fci_archive(config, entry.collection or "").sensing_end

    def render(self, region, config, last_sensing=None) -> RenderedFrame:
        from ha_satimage.config import DEFAULT_FCI_COLLECTION
        from ha_satimage.sources.fci_archive import chunks_for_region, get_archive

        collection = config.sources.collection_for(region.source, DEFAULT_FCI_COLLECTION)
        # Newest archived slot with all of the region's chunks (downloaded by
        # the download job); if missing, download it here.
        needed = chunks_for_region(region)
        slot = next(
            (s for s in get_archive(archive_root(config)).slots(collection)[:1]
             if needed and needed <= set(s.chunk_files())),
            None,
        ) or sync_fci_archive(config, collection)
        if last_sensing is not None and slot.sensing_end <= last_sensing:
            raise NoNewData(f"{slot.product_id} already rendered")
        return render_fci_slot(slot, region, region.composite, collection)


SOURCE_REGISTRY: dict[str, type[Source]] = {
    "msg_seviri": MsgSeviriSource,
    "data_tailor": DataTailorSource,
    "mtg_fci": MtgFciSource,
    "dummy": DummySource,
}


def get_source(name: str) -> Source:
    try:
        return SOURCE_REGISTRY[name]()
    except KeyError as exc:
        raise RenderError(f"Unknown source: {name}") from exc
