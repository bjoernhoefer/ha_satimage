"""Tests for the MTG FCI driver: chunk geometry, raw data archive, rendering.

``eumdac`` and the Satpy child process are mocked; the real Data Store is
never contacted.
"""

from __future__ import annotations

import io
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

from ha_satimage import sources
from ha_satimage.buffer import BufferManager
from ha_satimage.config import (
    DEFAULT_FCI_COMPOSITE,
    AppConfig,
    ArchiveConfig,
    EumetsatCredentials,
    RegionConfig,
    default_config,
)
from ha_satimage.sources import (
    MtgFciSource,
    NoNewData,
    RenderError,
    RenderedFrame,
    render_fci_slot,
)
from ha_satimage.sources.fci_archive import (
    ArchiveError,
    FciArchive,
    _geos_y,
    chunk_of,
    chunks_for_region,
    format_chunks,
    wanted_chunks,
)

CREDS = EumetsatCredentials(consumer_key="key", consumer_secret="secret")
SENSING = datetime(2026, 9, 26, 14, 20, tzinfo=timezone.utc)
COLLECTION = "EO:EUM:DAT:0662"


def _entry(chunk: int, kind: str = "BODY") -> str:
    return (
        f"W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--CHK-{kind}---NC4E_C_EUMT_"
        f"20260926142500_IDPFI_OPE_20260926141000_20260926142000_N_JLS_C_0086_{chunk:04d}.nc"
    )


class FakeFciProduct:
    def __init__(self, product_id: str, sensing_end: datetime, fail_on: int | None = None):
        self._id = product_id
        self.sensing_start = (sensing_end - timedelta(minutes=10)).replace(tzinfo=None)
        self.sensing_end = sensing_end.replace(tzinfo=None)
        self.entries = [_entry(c) for c in range(1, 41)] + [_entry(41, "TRAIL"), "manifest.xml"]
        self.opened: list[str] = []
        self.fail_on = fail_on

    def __str__(self) -> str:
        return self._id

    def open(self, entry: str):
        chunk = chunk_of(entry)
        if chunk == self.fail_on:
            raise OSError("connection aborted")
        self.opened.append(entry)
        return io.BytesIO(f"chunk{chunk}".encode())


def install_fake_eumdac(monkeypatch, products):
    module = types.ModuleType("eumdac")

    class Collection:
        def search(self, dtstart, dtend):
            return iter(products)

    class DataStore:
        def __init__(self, token):
            pass

        def get_collection(self, collection_id):
            return Collection()

    module.AccessToken = lambda credentials: "token"
    module.DataStore = DataStore
    monkeypatch.setitem(sys.modules, "eumdac", module)


def _region(name="wien", lat=48.2082, lon=16.3738, radius_km=300) -> RegionConfig:
    return RegionConfig(name=name, lat=lat, lon=lon, radius_km=radius_km, source="mtg_fci")


# -- Geometrie ------------------------------------------------------------------
def test_geos_projection_matches_pyproj():
    pyproj = pytest.importorskip("pyproj")
    proj = pyproj.Proj(proj="geos", h=35786400, lon_0=0, a=6378137, b=6356752.31414, sweep="y")
    for lat, lon in ((48.2, 16.4), (39.7, 3.0), (-30.0, 40.0), (60.0, -50.0)):
        assert _geos_y(lat, lon) == pytest.approx(proj(lon, lat)[1], abs=1.0)


def test_chunks_for_region_known_locations():
    # Verified against real data: Vienna renders completely from 36-37.
    assert chunks_for_region(_region()) == {36, 37}
    assert chunks_for_region(_region("mallorca", 39.6953, 3.0176)) == {34, 35}
    assert chunks_for_region(_region("oslo", 59.9, 10.7)) == {38, 39}
    assert chunks_for_region(_region("kapstadt", -33.9, 18.4)) == {7, 8, 9}
    # Outside the full disk (satellite over 0°).
    assert chunks_for_region(_region("tokio", 35.7, 139.7)) == set()


def test_wanted_chunks_is_range_plus_regions():
    config = default_config()
    config.archive = ArchiveConfig(chunk_min=38, chunk_max=40)
    config.regions.append(_region("kapstadt", -33.9, 18.4))
    assert wanted_chunks(config) == {7, 8, 9, 34, 35, 36, 37, 38, 39, 40}


def test_format_chunks():
    assert format_chunks({32, 33, 34, 36, 38, 39}) == "32–34, 36, 38–39"
    assert format_chunks([]) == "–"


def test_chunk_of_ignores_trail_and_other_files():
    assert chunk_of(_entry(36)) == 36
    assert chunk_of(_entry(41, "TRAIL")) is None
    assert chunk_of("manifest.xml") is None
    assert chunk_of(_entry(36) + ".part") is None


def test_archive_config_validation():
    with pytest.raises(ValueError, match="(?i)chunk"):
        ArchiveConfig(chunk_min=39, chunk_max=33)
    with pytest.raises(ValueError):
        ArchiveConfig(chunk_min=0)
    assert AppConfig().archive.retention_hours == 12


def test_region_names_must_not_collide_with_internal_dirs():
    with pytest.raises(ValueError, match="_"):
        RegionConfig(name="_archive", lat=0, lon=0, radius_km=10)
    with pytest.raises(ValueError):
        RegionConfig(name="a/b", lat=0, lon=0, radius_km=10)


# -- Archiv -------------------------------------------------------------------
def test_sync_downloads_only_wanted_chunks_once(tmp_path, monkeypatch):
    product = FakeFciProduct("FCI-A", SENSING)
    install_fake_eumdac(monkeypatch, [product])
    archive = FciArchive(tmp_path)

    slot = archive.sync(COLLECTION, CREDS, {36, 37, 41}, retention_hours=12)
    assert slot.name == "20260926T142000Z"
    assert slot.sensing_end == SENSING
    assert slot.chunks == [36, 37]  # 41 = TRAIL is never downloaded
    assert slot.chunk_files()[36].read_bytes() == b"chunk36"
    assert len(product.opened) == 2

    again = archive.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12)
    assert again == slot and len(product.opened) == 2

    # Extended range: only the missing chunks are downloaded.
    archive.sync(COLLECTION, CREDS, {35, 36, 37}, retention_hours=12)
    assert len(product.opened) == 3
    assert [s.name for s in archive.slots(COLLECTION)] == [slot.name]
    assert archive.status()[COLLECTION]["last_error"] is None


def test_clear_raw_archive_survives_restart_and_does_not_redownload_old_scan(tmp_path, monkeypatch):
    from ha_satimage.sources.fci_archive import ClearedCapture

    product = FakeFciProduct("FCI-A", SENSING)
    products = [product]
    install_fake_eumdac(monkeypatch, products)
    archive = FciArchive(tmp_path / "_archive")
    slot = archive.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12)
    cutoff = SENSING + timedelta(minutes=1)
    assert archive.clear(cutoff) == 1
    assert not slot.path.exists()
    assert archive.status() == {}
    restarted = FciArchive(archive.root)
    with pytest.raises(ClearedCapture):
        restarted.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12)
    assert len(product.opened) == 2
    assert restarted.slots(COLLECTION) == []
    newer = FakeFciProduct("FCI-B", SENSING + timedelta(minutes=10))
    products[:] = [newer]
    assert restarted.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12).product_id == "FCI-B"
    assert len(newer.opened) == 2


def test_cleared_raw_scan_is_no_new_data_not_a_download_error(tmp_path, monkeypatch):
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.status import StatusStore
    from ha_satimage.sources.fci_archive import get_archive

    store = ConfigStore(tmp_path / "config.yaml")
    config = store.stored().model_copy(deep=True)
    config.storage.frames_dir = str(tmp_path / "frames")
    config.sources.get("mtg_fci").enabled = True
    config.eumetsat = CREDS
    store.update(config)
    product = FakeFciProduct("FCI-A", SENSING)
    install_fake_eumdac(monkeypatch, [product])
    get_archive(sources.archive_root(store.get())).clear(SENSING)
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    assert scheduler.download("mtg_fci") is False
    assert scheduler.download_status()["mtg_fci"]["last_error"] is None
    assert product.opened == []
    with pytest.raises(NoNewData):
        sources.sync_fci_archive(store.get(), COLLECTION)


@pytest.mark.parametrize("offset", [-10, 0, 2, 10])
def test_download_slot_identity_is_utc_across_timezones(tmp_path, monkeypatch, offset):
    sensing = datetime(2026, 9, 26, 23, 59, 59, 395000, tzinfo=timezone.utc)
    product = FakeFciProduct("FCI-A", sensing)
    product.sensing_end = sensing.astimezone(timezone(timedelta(hours=offset)))
    install_fake_eumdac(monkeypatch, [product])
    archive = FciArchive(tmp_path)
    slot = archive.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12)
    assert slot.name == "20260926T235959Z"
    assert slot.sensing_end.tzinfo == timezone.utc
    assert slot.sensing_end == sensing
    assert FciArchive(tmp_path).slots(COLLECTION)[0] == slot
    assert archive.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12) == slot
    assert len(product.opened) == 2


def test_sync_prunes_slots_older_than_retention(tmp_path, monkeypatch):
    products: list[FakeFciProduct] = []
    install_fake_eumdac(monkeypatch, products)
    archive = FciArchive(tmp_path)
    for i in range(4):  # 0 h, 1 h, 2 h, 3 h
        products.insert(0, FakeFciProduct(f"FCI-{i}", SENSING + timedelta(hours=i)))
        archive.sync(COLLECTION, CREDS, {36}, retention_hours=2)
    names = [s.name for s in archive.slots(COLLECTION)]
    assert names == ["20260926T172000Z", "20260926T162000Z"]

    assert archive.prune(COLLECTION, 0) == 1
    assert [s.name for s in archive.slots(COLLECTION)] == ["20260926T172000Z"]


def test_sync_failure_cleans_up_and_is_reported(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [FakeFciProduct("FCI-A", SENSING, fail_on=36)])
    archive = FciArchive(tmp_path)
    with pytest.raises(ArchiveError, match="aborted"):
        archive.sync(COLLECTION, CREDS, {36, 37}, retention_hours=12)
    assert archive.slots(COLLECTION) == []
    assert not list(tmp_path.rglob("*.part"))
    assert "aborted" in archive.status()[COLLECTION]["last_error"]


def test_sync_requires_credentials(tmp_path):
    with pytest.raises(ArchiveError, match="credentials"):
        FciArchive(tmp_path).sync(COLLECTION, EumetsatCredentials(), {36}, 12)


def test_slot_lookup_rejects_path_tricks(tmp_path):
    archive = FciArchive(tmp_path)
    assert archive.slot(COLLECTION, "../../etc") is None
    assert archive.slot(COLLECTION, "20260926T142000Z") is None


def test_invalid_calendar_slot_is_not_offered_for_rendering(tmp_path):
    archive = FciArchive(tmp_path)
    directory = archive.collection_dir(COLLECTION) / "20260230T142000Z"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(json.dumps({
        "product_id": "invalid",
        "sensing_start": SENSING.isoformat(),
        "sensing_end": SENSING.isoformat(),
    }))
    assert archive.slot(COLLECTION, directory.name) is None
    assert archive.slots(COLLECTION) == []


# -- Treiber ------------------------------------------------------------------
def _fci_config(tmp_path) -> AppConfig:
    config = default_config()
    config.storage.frames_dir = str(tmp_path / "frames")
    for entry in config.sources.catalog:
        entry.enabled = entry.id == "mtg_fci"
    for region in config.regions:
        region.source = "mtg_fci"
    config.eumetsat = CREDS
    return config


def test_mtg_fci_renders_only_region_chunks(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [FakeFciProduct("FCI-A", SENSING)])
    requests = []

    def fake_render(request):
        requests.append(request)
        return b"PNG", SENSING.replace(tzinfo=None)

    monkeypatch.setattr("ha_satimage.sources.satpy_render.render_in_subprocess", fake_render)
    config = _fci_config(tmp_path)
    region = config.region("wien")
    region.composite = "natural_color_hrv_with_night_ir"  # SEVIRI name -> FCI counterpart

    frame = MtgFciSource().render(region, config)
    assert frame.png == b"PNG"
    assert frame.sensing_time == SENSING
    request = requests[0]
    assert request.reader == "fci_l1c_nc"
    assert request.composite == DEFAULT_FCI_COMPOSITE
    assert [chunk_of(f) for f in request.filenames] == [36, 37]

    # The Europe range was fully archived although Vienna needs only 2 chunks.
    slot = sources.fci_archive.get_archive(sources.archive_root(config)).slots("EO:EUM:DAT:0662")[0]
    assert slot.chunks == list(range(32, 41))
    assert slot.path.is_relative_to(tmp_path / "frames" / "_archive")

    with pytest.raises(NoNewData):
        MtgFciSource().render(region, config, last_sensing=SENSING)


def test_render_fci_slot_reports_missing_chunks(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [FakeFciProduct("FCI-A", SENSING)])
    slot = FciArchive(tmp_path).sync(COLLECTION, CREDS, {36}, 12)
    with pytest.raises(RenderError, match="missing 37"):
        render_fci_slot(slot, _region(), DEFAULT_FCI_COMPOSITE)
    with pytest.raises(RenderError, match="outside"):
        render_fci_slot(slot, _region("tokio", 35.7, 139.7), DEFAULT_FCI_COMPOSITE)


def test_fci_is_no_placeholder_and_has_own_composites():
    from ha_satimage.config import PLACEHOLDER_DRIVERS, composites_for, default_composite_for

    assert "mtg_fci" not in PLACEHOLDER_DRIVERS
    assert DEFAULT_FCI_COMPOSITE in composites_for("mtg_fci")
    assert "natural_color_hrv" not in composites_for("mtg_fci")
    assert default_composite_for("msg_seviri") == "natural_color_hrv_with_night_ir"


def test_fci_hrfi_collection_has_own_composites(tmp_path, monkeypatch):
    """HRFI (EO:EUM:DAT:0665) has only vis_06/nir_22/ir_38/ir_105: no natural color/cloud tops."""
    from ha_satimage.config import (
        DEFAULT_FCI_HRFI_COMPOSITE,
        FCI_HRFI_COLLECTION,
        SourceDefinition,
        composites_for,
        default_composite_for,
        resolve_fci_composite,
    )
    from ha_satimage.sources import archive_composites

    hrfi = composites_for("mtg_fci", FCI_HRFI_COLLECTION)
    assert DEFAULT_FCI_HRFI_COMPOSITE in hrfi
    assert {"natural_color", "cloudtop", DEFAULT_FCI_COMPOSITE}.isdisjoint(hrfi)
    assert default_composite_for("mtg_fci", FCI_HRFI_COLLECTION) == DEFAULT_FCI_HRFI_COMPOSITE
    assert default_composite_for("mtg_fci", COLLECTION) == DEFAULT_FCI_COMPOSITE
    assert composites_for("mtg_fci", COLLECTION) == composites_for("mtg_fci")
    for name in ("natural_color_hrv_with_night_ir", DEFAULT_FCI_COMPOSITE, "natural_color", "cloudtop"):
        assert resolve_fci_composite(name, FCI_HRFI_COLLECTION) in hrfi
    assert resolve_fci_composite("cloudtop", COLLECTION) == "cloudtop"

    entry = SourceDefinition(id="mtg_fci_0665", driver="mtg_fci", collection=FCI_HRFI_COLLECTION)
    assert archive_composites(entry) == list(hrfi)

    # Rendering maps an FDHSI image type to its HRFI counterpart.
    install_fake_eumdac(monkeypatch, [FakeFciProduct("FCI-A", SENSING)])
    requests = []

    def fake_render(request):
        requests.append(request)
        return b"PNG", SENSING.replace(tzinfo=None)

    monkeypatch.setattr("ha_satimage.sources.satpy_render.render_in_subprocess", fake_render)
    slot = FciArchive(tmp_path).sync(COLLECTION, CREDS, {36, 37}, 12)
    render_fci_slot(slot, _region(), DEFAULT_FCI_COMPOSITE, FCI_HRFI_COLLECTION)
    assert requests[-1].composite == DEFAULT_FCI_HRFI_COMPOSITE
    render_fci_slot(slot, _region(), DEFAULT_FCI_COMPOSITE)
    assert requests[-1].composite == DEFAULT_FCI_COMPOSITE


def test_fci_composite_is_registered_in_satpy_config():
    pytest.importorskip("satpy")
    from ha_satimage.sources.satpy_render import SATPY_CONFIG_DIR

    text = (SATPY_CONFIG_DIR / "composites" / "visir.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text.replace("!!python/name:", ""))["composites"][DEFAULT_FCI_COMPOSITE]


# -- Speicherort-Wechsel --------------------------------------------------------
def test_relocate_moves_archive_as_whole(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    slot_dir = old / "_archive" / "EO_EUM_DAT_0662" / "20260926T142000Z"
    slot_dir.mkdir(parents=True)
    (slot_dir / "meta.json").write_text("{}")
    manager = BufferManager(old)
    manager.get("wien", 4, 100).add_frame(b"png")

    assert manager.relocate(new, move_existing=True) == 1
    assert (new / "_archive" / "EO_EUM_DAT_0662" / "20260926T142000Z" / "meta.json").exists()
    assert not (new / "_archive" / "_index.json").exists()
    assert not old.joinpath("_archive").exists()


# -- API ------------------------------------------------------------------------
def write_fci_slot(frames_dir, name="20260926T142000Z", chunks=range(32, 41), cached=None):
    """Creates an archived FCI slot; ``cached`` = {(region, composite): PNG}."""
    slot_dir = frames_dir / "_archive" / "EO_EUM_DAT_0662" / name
    slot_dir.mkdir(parents=True)
    end = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    (slot_dir / "meta.json").write_text(json.dumps({
        "product_id": "FCI-A", "sensing_start": end.isoformat(), "sensing_end": end.isoformat(),
    }))
    for c in chunks:
        (slot_dir / _entry(c)).write_bytes(b"x")
    if cached:
        from ha_satimage.archive import RenderArchive

        archive = RenderArchive(frames_dir / "_renders")
        for key, png in cached.items():
            region, composite = key if len(key) == 2 else key[:2]
            source = key[2] if len(key) > 2 else "mtg_fci"
            archive.store(region, source, composite, end, png)
    return slot_dir


def test_archive_api_lists_slots_and_serves_cached_renders(live_server):
    """The archive lists only rendered images; raw FCI slots are never rendered on request."""
    frames_dir = live_server.data_dir / "frames"
    write_fci_slot(frames_dir, "20260926T141000Z", chunks=[36])  # Vienna incomplete
    write_fci_slot(frames_dir)
    # Make the FCI source selectable in the archive.
    config = yaml.safe_load(live_server.config_file.read_text())
    for entry in config["sources"]["catalog"]:
        entry["enabled"] = entry["id"] in ("dummy", "mtg_fci")
    httpx.post(f"{live_server.url}/api/config", json={"sources": config["sources"]})

    info = httpx.get(f"{live_server.url}/api/archive").json()
    assert info["retention_hours"] == 12
    assert info["chunks_text"] == "32–40"
    assert info["collections"]["EO:EUM:DAT:0662"]["slots"] == 2

    listing = httpx.get(f"{live_server.url}/api/regions/wien/archive?source=mtg_fci").json()
    assert listing["source"] == "mtg_fci"
    assert listing["composite"] == DEFAULT_FCI_COMPOSITE
    # Nothing rendered yet: raw slots alone are not offered.
    assert listing["images"] == []
    assert {s["id"] for s in listing["sources"]} >= {"dummy", "mtg_fci"}
    missing = httpx.get(
        f"{live_server.url}/regions/wien/archive/mtg_fci/{DEFAULT_FCI_COMPOSITE}/20260926T142000Z.png"
    )
    assert missing.status_code == 404

    # An already rendered image is served from the archive (without Satpy).
    from ha_satimage.archive import RenderArchive

    archive = RenderArchive(frames_dir / "_renders")
    archive.store("wien", "mtg_fci", "cloudtop", SENSING, _tiny_png())
    response = httpx.get(
        f"{live_server.url}/regions/wien/archive/mtg_fci/cloudtop/"
        f"{SENSING.strftime('%Y%m%dT%H%M%SZ')}.png"
    )
    assert response.status_code == 200 and response.content == _tiny_png()
    jpeg = httpx.get(
        f"{live_server.url}/regions/wien/archive/mtg_fci/cloudtop/"
        f"{SENSING.strftime('%Y%m%dT%H%M%SZ')}.jpg"
    )
    assert jpeg.status_code == 200 and jpeg.headers["content-type"] == "image/jpeg"
    listing = httpx.get(f"{live_server.url}/api/regions/wien/archive?source=mtg_fci&composite=cloudtop").json()
    assert listing["images"][0]["cached"] is True

    base = f"{live_server.url}/regions/wien/archive/mtg_fci"
    assert httpx.get(f"{base}/cloudtop/20260101T000000Z.png").status_code == 404
    assert httpx.get(f"{base}/a%2Fb/20260926T142000Z.png").status_code in (400, 404)
    assert httpx.get(f"{base}/x;y/20260926T142000Z.png").status_code == 400
    # Vienna lacks chunk 37 in the older slot -> not offered at all.
    names = [s["name"] for s in listing["images"]]
    assert "20260926T141000Z" not in names


def test_archive_never_renders_raw_slots_on_request(fci_server):
    """Only images in the list of available images are served - no rendering."""
    from ha_satimage.archive import RenderArchive

    url, calls, frames, names = fci_server
    base = f"{url}/regions/wien/archive/mtg_fci/{DEFAULT_FCI_COMPOSITE}"
    for ext in ("png", "jpg"):
        assert httpx.get(f"{base}/{names[0]}.{ext}").status_code == 404
    listing = httpx.get(f"{url}/api/regions/wien/archive", params={
        "source": "mtg_fci", "composite": DEFAULT_FCI_COMPOSITE,
    }).json()
    assert listing["images"] == []

    # The scheduler stores an image after the download: listed and served.
    slot_time = datetime.strptime(names[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    RenderArchive(frames / "_renders").store("wien", "mtg_fci", DEFAULT_FCI_COMPOSITE, slot_time, _tiny_png())
    listing = httpx.get(f"{url}/api/regions/wien/archive", params={
        "source": "mtg_fci", "composite": DEFAULT_FCI_COMPOSITE,
    }).json()
    assert [(item["name"], item["cached"]) for item in listing["images"]] == [(names[0], True)]
    for ext in ("png", "jpg"):
        assert httpx.get(f"{base}/{names[0]}.{ext}").status_code == 200
    assert httpx.get(f"{base}/{names[1]}.png").status_code == 404
    assert calls == []


def _tiny_png() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, format="PNG")
    return out.getvalue()


def test_storage_api_sets_archive_settings_and_prunes(live_server):
    frames_dir = live_server.data_dir / "frames"
    write_fci_slot(frames_dir, "20260926T000000Z")
    write_fci_slot(frames_dir, "20260926T142000Z")
    response = httpx.post(f"{live_server.url}/api/storage", json={
        "archive_retention_hours": 1, "archive_chunk_min": 30, "archive_chunk_max": 40,
    })
    assert response.status_code == 200, response.text
    stored = yaml.safe_load(live_server.config_file.read_text())["archive"]
    assert stored == {
        "retention_hours": 1, "chunk_min": 30, "chunk_max": 40,
        "render_all": False, "render_retention_hours": 24, "render_max_storage_mb": 2000,
    }
    remaining = sorted(p.name for p in (frames_dir / "_archive" / "EO_EUM_DAT_0662").iterdir())
    assert remaining == ["20260926T142000Z"]

    bad = httpx.post(f"{live_server.url}/api/storage", json={"archive_chunk_min": 40, "archive_chunk_max": 30})
    assert bad.status_code == 400

    # Settings of the rendered-image archive, including retention pruning.
    from ha_satimage.archive import RenderArchive

    renders = RenderArchive(frames_dir / "_renders")
    renders.store("wien", "dummy", "cloudtop", datetime(2026, 9, 20, tzinfo=timezone.utc), _tiny_png())
    renders.store("wien", "dummy", "cloudtop", datetime.now(timezone.utc), _tiny_png())
    response = httpx.post(f"{live_server.url}/api/storage", json={
        "archive_render_all": True, "archive_render_retention_hours": 6,
        "archive_render_max_storage_mb": 500,
    })
    assert response.status_code == 200, response.text
    stored = yaml.safe_load(live_server.config_file.read_text())["archive"]
    assert stored["render_all"] is True
    assert stored["render_retention_hours"] == 6
    assert stored["render_max_storage_mb"] == 500
    assert len(renders.images("wien", "dummy", "cloudtop")) == 1
    summary = response.json()["archive"]
    assert summary["render_all"] is True and summary["renders"]["images"] == 1


# -- Archive of pre-rendered images ----------------------------------------------
@pytest.mark.parametrize("metadata_offset", [0, 37, 7200])
def test_archive_run_prerenders_every_archived_fci_slot(tmp_path, monkeypatch, metadata_offset):
    """Not only the newest capture: every raw slot without images gets rendered."""
    from ha_satimage.archive import RenderArchive
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.sources import archive_composites
    from ha_satimage.status import StatusStore

    store = ConfigStore(tmp_path / "config.yaml")
    config = _fci_config(tmp_path)
    config.archive.render_all = True
    store.update(config)
    frames = tmp_path / "frames"
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    names = [(now - timedelta(minutes=m)).strftime("%Y%m%dT%H%M%SZ") for m in (0, 10, 20)]
    for name in names:
        directory = write_fci_slot(frames, name)
        metadata = json.loads((directory / "meta.json").read_text())
        end = datetime.fromisoformat(metadata["sensing_end"]) + timedelta(seconds=metadata_offset, microseconds=395000)
        metadata["sensing_end"] = end.astimezone(timezone(timedelta(hours=2))).isoformat()
        (directory / "meta.json").write_text(json.dumps(metadata))
    # Older than the render retention: would be pruned at once, not rendered.
    write_fci_slot(frames, (now - timedelta(hours=30)).strftime("%Y%m%dT%H%M%SZ"))
    renders = RenderArchive(frames / "_renders")
    from ha_satimage.archive import region_signature

    for region in store.get().regions:
        renders.sync_region(region.name, region_signature(region))
    # One combination already exists (e.g. rendered on request): not again.
    renders.store("wien", "mtg_fci", DEFAULT_FCI_COMPOSITE,
                  datetime.strptime(names[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
                  _tiny_png())

    calls = []

    def fake_render(slot, region, composite, collection=None):
        calls.append((slot.name, region.name, composite))
        if composite == "cloudtop" and slot.name == names[2]:
            raise RenderError("broken slot")
        return sources.RenderedFrame(_tiny_png(), slot.sensing_end)

    monkeypatch.setattr("ha_satimage.scheduler.render_fci_slot", fake_render)
    scheduler = RenderScheduler(store, BufferManager(frames), StatusStore())
    composites = archive_composites(store.get().sources.get("mtg_fci"))
    regions = [r.name for r in store.get().regions]

    # The capture time passed in is only the trigger; all slots are covered.
    stored = scheduler.archive_capture("mtg_fci", now)
    expected = len(names) * len(regions) * len(composites) - 1  # minus the existing one
    assert len(calls) == expected
    assert stored == expected - len(regions)  # broken cloudtop slot per region
    # Newest slot first, each image rendered from its own slot.
    assert calls[0][0] == names[0]
    assert {c[0] for c in calls} == set(names)
    for name in names:
        for region in regions:
            for composite in composites:
                path = renders.path_for(region, "mtg_fci", composite, name)
                assert path.exists() == (not (composite == "cloudtop" and name == names[2]))

    # Nothing left: a second run renders nothing (failed ones are retried).
    calls.clear()
    assert scheduler.archive_capture("mtg_fci", now) == 0
    assert {c[2] for c in calls} == {"cloudtop"} and len(calls) == len(regions)
    calls.clear()
    restarted = RenderScheduler(store, BufferManager(frames), StatusStore())
    assert restarted.archive_capture("mtg_fci", now) == 0
    assert {c[2] for c in calls} == {"cloudtop"} and len(calls) == len(regions)


def test_clear_cancels_selected_fci_task_before_it_can_render(tmp_path, monkeypatch):
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.status import StatusStore

    store = ConfigStore(tmp_path / "config.yaml")
    store.update(_fci_config(tmp_path))
    frames = tmp_path / "frames"
    slot = write_fci_slot(frames, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    scheduler = RenderScheduler(store, BufferManager(frames), StatusStore())

    def acquire(timeout):
        scheduler.clear_archive()
        return scheduler._render_lock.acquire()

    monkeypatch.setattr(scheduler, "_acquire_for_archive", acquire)
    monkeypatch.setattr(
        "ha_satimage.scheduler.render_fci_slot",
        lambda *a: pytest.fail("selected raw slot was cleared while waiting for the lock"),
    )
    assert scheduler.archive_capture("mtg_fci", datetime.now(timezone.utc)) == 0
    assert not slot.exists()


def test_archive_run_for_fci_does_not_run_twice_at_once(tmp_path, monkeypatch):
    import threading

    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.status import StatusStore

    store = ConfigStore(tmp_path / "config.yaml")
    store.update(_fci_config(tmp_path))
    write_fci_slot(tmp_path / "frames", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    monkeypatch.setattr(
        "ha_satimage.scheduler.render_fci_slot",
        lambda *a: pytest.fail("must not render while another run is active"),
    )
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    guard = scheduler._archive_locks.setdefault("mtg_fci", threading.Lock())
    with guard:
        assert scheduler.archive_capture("mtg_fci", datetime.now(timezone.utc)) == 0


@pytest.mark.parametrize("render_all", [True, False])
def test_startup_renders_retained_fci_slots_only_with_render_all(tmp_path, monkeypatch, render_all):
    from ha_satimage.archive import RenderArchive
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.sources import archive_composites
    from ha_satimage.status import StatusStore

    store = ConfigStore(tmp_path / "config.yaml")
    config = _fci_config(tmp_path)
    config.archive.render_all = render_all
    store.update(config)
    frames = tmp_path / "frames"
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    names = [(now - timedelta(minutes=m)).strftime("%Y%m%dT%H%M%SZ") for m in (0, 10)]
    for name in names:
        write_fci_slot(frames, name)
    calls = []

    def fake_render(slot, region, composite, collection=None):
        calls.append((slot.name, region.name, composite))
        return RenderedFrame(_tiny_png(), slot.sensing_end)

    monkeypatch.setattr("ha_satimage.scheduler.render_fci_slot", fake_render)
    monkeypatch.setattr(
        sources.MtgFciSource, "fetch",
        lambda *args: pytest.fail("recovery must not require a network download"),
    )
    scheduler = RenderScheduler(store, BufferManager(frames), StatusStore())
    scheduler._reschedule()
    recovery = [job for job in scheduler._scheduler.get_jobs() if job.id.startswith("archive-borders-")]
    if not render_all:
        # No archive: nothing is synced or rendered at startup.
        assert recovery == [] and calls == []
        return
    assert len(recovery) == 1
    recovery[0].func(*recovery[0].args)
    composites = archive_composites(store.get().sources.get("mtg_fci"))
    assert len(calls) == len(names) * len(store.get().regions) * len(composites)
    assert RenderArchive(frames / "_renders").count() == len(calls)
    scheduler._reschedule()
    assert len([job for job in scheduler._scheduler.get_jobs() if job.id.startswith("archive-borders-")]) == 1


@pytest.mark.parametrize("collection", ["../outside", "/tmp/outside", "..", ".", "a/b", "a\\b"])
def test_collection_directory_cannot_escape_archive_root(tmp_path, collection):
    from ha_satimage.sources.fci_archive import FciArchive

    archive = FciArchive(tmp_path / "_archive")
    directory = archive.collection_dir(collection)
    assert directory.parent == archive.root
    assert directory.resolve().is_relative_to(archive.root.resolve())
    assert archive.slots(collection) == []
