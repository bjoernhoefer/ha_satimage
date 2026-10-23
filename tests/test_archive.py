"""Archive of pre-rendered images ("download all sources")."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml
from PIL import Image

from ha_satimage.archive import RenderArchive, region_signature, slot_name, slot_time
from ha_satimage.config import RegionConfig

NOW = datetime(2026, 9, 26, 14, 20, tzinfo=timezone.utc)


def png(color: str = "red", size: int = 8) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (size, size), color).save(out, format="PNG")
    return out.getvalue()


def test_slot_names_round_trip():
    assert slot_name(NOW) == "20260926T142000Z"
    assert slot_time("20260926T142000Z") == NOW
    assert slot_time("not-a-slot") is None
    naive = NOW.replace(tzinfo=None)
    assert slot_name(naive) == "20260926T142000Z"


def test_store_writes_png_and_jpeg_and_is_listed(tmp_path):
    archive = RenderArchive(tmp_path)
    image = archive.store("wien", "msg_seviri", "cloudtop", NOW, png())
    assert image is not None
    assert image.path.read_bytes() == png()
    assert image.jpeg_path.exists()
    assert Image.open(image.jpeg_path).format == "JPEG"

    assert archive.has("wien", "msg_seviri", "cloudtop", NOW) is True
    assert archive.has("wien", "msg_seviri", "cloudtop", NOW + timedelta(minutes=5)) is False
    listed = archive.images("wien", "msg_seviri", "cloudtop")
    assert [i.slot for i in listed] == ["20260926T142000Z"]
    assert listed[0].as_dict("wien")["url"].split("?")[0] == (
        "/regions/wien/archive/msg_seviri/cloudtop/20260926T142000Z.png"
    )
    assert archive.count() == 1 and archive.used_bytes() > 0


def test_archive_cache_headers_require_current_image_version(live_server):
    archive = archive_of(live_server)
    image = archive.store("wien", "dummy", "cloudtop", NOW, png("white"))
    urls = image.as_dict("wien")
    unversioned = urls["url"].split("?")[0]
    assert httpx.get(live_server.url + unversioned).headers["cache-control"] == "no-cache"
    for key in ("url", "jpeg_url"):
        assert "immutable" in httpx.get(live_server.url + urls[key]).headers["cache-control"]
    archive.store("wien", "dummy", "cloudtop", NOW, png("blue"))
    assert httpx.get(live_server.url + urls["url"]).headers["cache-control"] == "no-cache"
    new_url = archive.images("wien", "dummy", "cloudtop")[0].as_dict("wien")["url"]
    assert new_url != urls["url"]
    assert "immutable" in httpx.get(live_server.url + new_url).headers["cache-control"]


def test_images_are_sorted_newest_first(tmp_path):
    archive = RenderArchive(tmp_path)
    for minutes in (0, 10, 5):
        archive.store("wien", "dummy", "natural_color", NOW + timedelta(minutes=minutes), png())
    assert [i.slot for i in archive.images("wien", "dummy", "natural_color")] == [
        "20260926T143000Z", "20260926T142500Z", "20260926T142000Z",
    ]


def test_combinations_and_regions(tmp_path):
    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    archive.store("wien", "dummy", "airmass", NOW, png())
    archive.store("wien", "mtg_fci", "cloudtop", NOW, png())
    archive.store("graz", "dummy", "cloudtop", NOW, png())
    assert archive.regions() == ["graz", "wien"]
    assert archive.combinations("wien") == {
        "dummy": ["airmass", "cloudtop"], "mtg_fci": ["cloudtop"],
    }
    assert archive.combinations("unknown") == {}


def test_invalid_names_are_rejected(tmp_path):
    archive = RenderArchive(tmp_path)
    assert archive.store("../etc", "dummy", "cloudtop", NOW, png()) is None
    assert archive.store("wien", "a/b", "cloudtop", NOW, png()) is None
    assert archive.path_for("wien", "dummy", "cloudtop", "nope") is None
    assert archive.path_for("wien", "dummy", "cloudtop", "20260926T142000Z", "exe") is None
    assert list(tmp_path.rglob("*.png")) == []


def test_prune_by_age_and_storage_limit(tmp_path):
    archive = RenderArchive(tmp_path)
    now = datetime.now(timezone.utc)
    for hours in (30, 20, 1):
        archive.store("wien", "dummy", "cloudtop", now - timedelta(hours=hours), png())
    assert archive.prune(retention_hours=24, max_storage_mb=1000) == 1
    assert len(archive.images("wien", "dummy", "cloudtop")) == 2

    # Storage limit removes the oldest first.
    assert archive.prune(retention_hours=0, max_storage_mb=0.0005) >= 1
    remaining = archive.images("wien", "dummy", "cloudtop")
    assert len(remaining) < 2
    # Retention 0 means "no age limit".
    assert archive.prune(retention_hours=0, max_storage_mb=1000) == 0


def test_forget_removes_regions_that_no_longer_exist(tmp_path):
    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    archive.store("graz", "dummy", "cloudtop", NOW, png())
    assert archive.forget({"wien"}) == 1
    assert archive.regions() == ["wien"]


def test_region_signature_change_clears_the_archive(tmp_path):
    archive = RenderArchive(tmp_path)
    region = RegionConfig(name="wien", lat=48.2, lon=16.4, radius_km=100)
    archive.sync_region("wien", region_signature(region))
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    # Same geometry -> images are kept.
    archive.sync_region("wien", region_signature(region))
    assert archive.count() == 1
    # Different cut-out -> archived images no longer match and are dropped.
    moved = region.model_copy(update={"radius_km": 300})
    archive.sync_region("wien", region_signature(moved))
    assert archive.count() == 0


def test_overview_extent_change_invalidates_archive(tmp_path):
    from ha_satimage.config import europe_overview

    archive = RenderArchive(tmp_path)
    overview = europe_overview()
    archive.sync_region("europe", region_signature(overview))
    archive.store("europe", "dummy", "cloudtop", NOW, png())
    moved = overview.model_copy(update={"lonlat_extent": (-20, 33, 45, 72)})
    archive.sync_region("europe", region_signature(moved))
    assert archive.count() == 0


def test_overview_overlay_uses_lonlat_projection_and_preserves_scans(tmp_path, monkeypatch):
    from ha_satimage.config import europe_overview

    archive = RenderArchive(tmp_path)
    overview = europe_overview()
    archive.sync_region("europe", region_signature(overview))
    archive.store("europe", "dummy", "cloudtop", NOW, png("white", 200))
    calls = []
    monkeypatch.setattr("ha_satimage.overlay.draw_borders_lonlat", lambda image, extent: calls.append(extent))
    bordered = overview.model_copy(update={"borders": True})
    archive.sync_region("europe", region_signature(bordered))
    assert calls == [overview.lonlat_extent]
    assert archive.count() == 1
    assert archive.images("europe", "dummy", "cloudtop")[0].created_at == NOW
    assert not archive.sync_region("europe", region_signature(bordered))
    assert len(calls) == 1
    local = bordered.model_copy(update={"lonlat_extent": None})
    archive.sync_region("europe", region_signature(local))
    assert archive.count() == 0


@pytest.mark.parametrize("previous_borders", [None, False, True])
def test_overlay_upgrade_preserves_all_historical_scans(tmp_path, previous_borders):
    archive = RenderArchive(tmp_path)
    region = RegionConfig(name="mallorca", lat=39.6, lon=2.9, radius_km=100)
    fields = region_signature(region).split("|")[:5]
    if previous_borders is not None:
        fields.append(str(previous_borders))
    directory = tmp_path / region.name
    directory.mkdir()
    (directory / "region.json").write_text("|".join(fields))
    original = png("white", 200)
    for source in ("msg_seviri", "mtg_fci"):
        for minutes in (0, 10):
            archive.store(region.name, source, "cloudtop", NOW + timedelta(minutes=minutes), original)
    previous_url = archive.images(region.name, "msg_seviri", "cloudtop")[0].as_dict(region.name)["url"]
    assert archive.sync_region(region.name, region_signature(region))
    assert archive.count() == 4
    for source in ("msg_seviri", "mtg_fci"):
        assert [image.created_at for image in archive.images(region.name, source, "cloudtop")] == [
            NOW + timedelta(minutes=10), NOW,
        ]
        for image in archive.images(region.name, source, "cloudtop"):
            assert image.path.read_bytes() != original
            assert Image.open(image.path).getextrema()[0][0] < 100
            assert Image.open(image.jpeg_path).getextrema()[0][0] < 100
    migrated = archive.images(region.name, "msg_seviri", "cloudtop")[0]
    assert migrated.as_dict(region.name)["url"] != previous_url
    before = migrated.path.read_bytes()
    assert not archive.sync_region(region.name, region_signature(region))
    assert migrated.path.read_bytes() == before


@pytest.mark.parametrize("failed_extension", [".png", ".jpg"])
def test_interrupted_overlay_upgrade_can_resume_without_redrawing(tmp_path, monkeypatch, failed_extension):
    archive = RenderArchive(tmp_path)
    region = RegionConfig(name="mallorca", lat=39.6, lon=2.9, radius_km=100)
    archive.sync_region(region.name, "|".join(region_signature(region).split("|")[:5]))
    for minutes in (0, 10):
        archive.store(region.name, "msg_seviri", "cloudtop", NOW + timedelta(minutes=minutes), png("white", 200))
    write = archive._write_atomic
    calls = 0

    def fail_second(path, data):
        nonlocal calls
        if path.suffix == failed_extension:
            calls += 1
            if calls == 2:
                raise OSError("disk unavailable")
        return write(path, data)

    monkeypatch.setattr(archive, "_write_atomic", fail_second)
    with pytest.raises(OSError):
        archive.sync_region(region.name, region_signature(region))
    newest = archive.images(region.name, "msg_seviri", "cloudtop")[0].path
    updated = newest.read_bytes()
    monkeypatch.setattr(archive, "_write_atomic", write)
    archive.sync_region(region.name, region_signature(region))
    assert newest.read_bytes() == updated
    assert archive.count() == 2
    for image in archive.images(region.name, "msg_seviri", "cloudtop"):
        assert Image.open(image.jpeg_path).getextrema()[0][0] < 100


def test_summary_reports_images_per_region(tmp_path):
    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    archive.store("wien", "dummy", "cloudtop", NOW + timedelta(minutes=5), png())
    summary = archive.summary()
    assert summary["images"] == 2
    assert summary["regions"]["wien"]["images"] == 2
    assert summary["regions"]["wien"]["combinations"] == 1
    assert summary["regions"]["wien"]["newest"].startswith("2026-09-26T14:25")


def test_available_images_list_is_the_only_lookup(tmp_path):
    """The viewer reads ``available.json`` - no directory scan per request."""
    import json

    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    listed = json.loads((tmp_path / "wien" / "available.json").read_text())["images"]
    assert set(listed["dummy"]["cloudtop"]) == {"20260926T142000Z"}

    # A stray file that was never completed/listed is not offered.
    stray = tmp_path / "wien" / "dummy" / "cloudtop" / "20260926T143000Z.png"
    stray.write_bytes(png())
    assert [i.slot for i in archive.images("wien", "dummy", "cloudtop")] == ["20260926T142000Z"]
    assert not archive.has("wien", "dummy", "cloudtop", NOW + timedelta(minutes=10))

    # Pruning removes the entry from the list as well.
    archive.prune(retention_hours=0, max_storage_mb=0.000001)
    assert archive.images("wien", "dummy", "cloudtop") == []
    assert "dummy" not in json.loads((tmp_path / "wien" / "available.json").read_text())["images"]


def test_missing_available_images_list_is_rebuilt_from_disk(tmp_path):
    """Archives of older versions have no list yet: it is built once."""
    archive = RenderArchive(tmp_path)
    archive.store("wien", "dummy", "cloudtop", NOW, png())
    archive.store("wien", "mtg_fci", "airmass", NOW, png())
    (tmp_path / "wien" / "available.json").unlink()
    fresh = RenderArchive(tmp_path)
    assert fresh.combinations("wien") == {"dummy": ["cloudtop"], "mtg_fci": ["airmass"]}
    assert (tmp_path / "wien" / "available.json").exists()
    (tmp_path / "wien" / "available.json").write_text("{broken")
    assert RenderArchive(tmp_path).count() == 2


def test_archive_renders_yield_to_waiting_previews(tmp_path):
    import threading
    import time

    from ha_satimage.buffer import BufferManager
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.status import StatusStore

    scheduler = RenderScheduler(
        ConfigStore(tmp_path / "config.yaml"), BufferManager(tmp_path / "frames"), StatusStore()
    )
    scheduler._preview_waiting = 1
    started = time.monotonic()
    assert scheduler._acquire_for_archive(timeout=0.6) is False
    assert time.monotonic() - started >= 0.5
    # RLock has no locked() on Python 3.12; check from a different thread
    # so a leaked reentrant acquisition cannot make this pass.
    acquired = []

    def check_unlocked():
        if scheduler._render_lock.acquire(blocking=False):
            acquired.append(True)
            scheduler._render_lock.release()

    checker = threading.Thread(target=check_unlocked)
    checker.start()
    checker.join(timeout=5)
    assert acquired == [True]
    scheduler._preview_waiting = 0
    assert scheduler._acquire_for_archive(timeout=1) is True
    scheduler._render_lock.release()

    # A preview waiting while an archive image renders goes first afterwards.
    scheduler._render_lock.acquire()
    scheduler._preview_waiting = 1
    order = []

    def archive():
        if scheduler._acquire_for_archive(timeout=5):
            order.append("archive")
            scheduler._render_lock.release()

    worker = threading.Thread(target=archive)
    worker.start()
    time.sleep(0.3)
    scheduler._render_lock.release()
    with scheduler._render_lock:
        order.append("preview")
        scheduler._preview_waiting = 0
    worker.join(timeout=5)
    assert order == ["preview", "archive"]


def test_archive_runs_are_coalesced_per_source(tmp_path):
    from ha_satimage.buffer import BufferManager
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import ARCHIVE_EXECUTOR, RenderScheduler
    from ha_satimage.status import StatusStore

    scheduler = RenderScheduler(
        ConfigStore(tmp_path / "config.yaml"), BufferManager(tmp_path / "frames"), StatusStore()
    )
    runs = []
    scheduler.archive_capture = lambda source, sensing: runs.append((source, sensing)) or 0
    for minutes in (0, 5, 10):
        scheduler._queue_archive("msg_seviri", NOW + timedelta(minutes=minutes))
    scheduler._queue_archive("mtg_fci", NOW)
    jobs = [j for j in scheduler._scheduler.get_jobs() if j.id.startswith("archive-")]
    assert len(jobs) == 2
    assert {j.executor for j in jobs} == {ARCHIVE_EXECUTOR}
    seviri = next(j for j in jobs if "msg_seviri" in j.id)
    seviri.func(*seviri.args)  # started: a new capture may queue the next run
    assert runs == [("msg_seviri", NOW)]
    scheduler._queue_archive("msg_seviri", NOW + timedelta(minutes=15))
    assert len([j for j in scheduler._scheduler.get_jobs() if "msg_seviri" in j.id]) == 2


# --------------------------------------------------------------------------
# API


def archive_of(live_server) -> RenderArchive:
    return RenderArchive(live_server.data_dir / "frames" / "_renders")


def test_clear_archive_api_removes_images_raw_data_but_keeps_history(live_server):
    import json

    archive = archive_of(live_server)
    image = archive.store("wien", "dummy", "cloudtop", NOW, png())
    assert archive.count() == 1
    raw = live_server.data_dir / "frames" / "_archive" / "EO_EUM_DAT_0662" / slot_name(NOW)
    raw.mkdir(parents=True)
    (raw / "chunk_0036.nc").write_bytes(b"raw")
    (raw / "meta.json").write_text(json.dumps({
        "product_id": "test", "sensing_start": NOW.isoformat(), "sensing_end": NOW.isoformat(),
    }))
    frames = live_server.ensure_frames("wien", 1)
    config = httpx.get(live_server.url + "/api/config").json()
    response = httpx.post(live_server.url + "/api/archive/clear", json={}, timeout=30)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cleared"
    assert response.json()["images"] == 1
    assert response.json()["raw_slots"] == 1
    assert not raw.exists()
    assert not image.path.exists() and not image.jpeg_path.exists()
    # The same archive instance must not serve its pre-clear index cache.
    assert archive.count() == 0
    assert httpx.get(live_server.url + image.as_dict("wien")["url"]).status_code == 404
    summary = httpx.get(live_server.url + "/api/archive").json()
    assert summary["collections"] == {}
    assert summary["renders"]["images"] == 0
    assert summary["renders"]["cleared_before"] == response.json()["cleared_before"]
    assert live_server.frames("wien") == frames
    assert httpx.get(live_server.url + "/api/config").json() == config
    assert httpx.post(live_server.url + "/api/archive/clear", json={}).status_code == 200


def test_clear_prevents_queued_old_scans_after_restart_but_allows_new_ones(tmp_path, monkeypatch):
    from ha_satimage.buffer import BufferManager
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.sources import RenderedFrame, render_archive_root
    from ha_satimage.status import StatusStore
    from ha_satimage.archive import get_render_archive

    store = ConfigStore(tmp_path / "config.yaml")
    config = store.stored().model_copy(deep=True)
    config.storage.frames_dir = str(tmp_path / "frames")
    config.sources.catalog = [entry for entry in config.sources.catalog if entry.id == "dummy"]
    config.sources.catalog[0].enabled = True
    store.update(config)
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    result = scheduler.clear_archive()
    cutoff = datetime.fromisoformat(result["cleared_before"])
    calls = []

    def render(entry, region, composite, config):
        calls.append((region.name, composite))
        return RenderedFrame(png(), cutoff + timedelta(minutes=10))

    monkeypatch.setattr("ha_satimage.scheduler.render_with", render)
    restarted = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    assert restarted.archive_capture("dummy", cutoff) == 0
    assert restarted.archive_capture("dummy", cutoff - timedelta(minutes=10)) == 0
    assert calls == []
    assert restarted.archive_capture("dummy", cutoff + timedelta(minutes=10)) > 0
    assert get_render_archive(render_archive_root(store.get())).count() == len(calls)


def test_clear_cancels_archive_work_already_waiting_for_render_lock(tmp_path, monkeypatch):
    from ha_satimage.archive import get_render_archive
    from ha_satimage.buffer import BufferManager
    from ha_satimage.config import ConfigStore
    from ha_satimage.scheduler import RenderScheduler
    from ha_satimage.sources import render_archive_root
    from ha_satimage.status import StatusStore

    store = ConfigStore(tmp_path / "config.yaml")
    config = store.stored().model_copy(deep=True)
    config.storage.frames_dir = str(tmp_path / "frames")
    config.sources.get("dummy").enabled = True
    store.update(config)
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())

    def acquire(timeout):
        scheduler.clear_archive()
        return scheduler._render_lock.acquire()

    monkeypatch.setattr(scheduler, "_acquire_for_archive", acquire)
    monkeypatch.setattr("ha_satimage.scheduler.render_with", lambda *a: pytest.fail("stale render"))
    assert scheduler.archive_capture("dummy", datetime.now(timezone.utc)) == 0
    assert get_render_archive(render_archive_root(store.get())).count() == 0


def test_archive_api_lists_sources_composites_and_images(live_server):
    archive = archive_of(live_server)
    archive.store("wien", "dummy", "cloudtop", NOW, png("blue"))
    archive.store("wien", "dummy", "airmass", NOW, png("green"))

    data = httpx.get(f"{live_server.url}/api/regions/wien/archive").json()
    assert data["region"] == "wien"
    assert {s["id"] for s in data["sources"]} >= {"dummy"}
    assert "cloudtop" in data["composites"]  # id -> label
    assert data["images"], data
    # Backwards compatible alias for older clients.
    assert data["slots"] == data["images"]

    listing = httpx.get(
        f"{live_server.url}/api/regions/wien/archive?source=dummy&composite=airmass"
    ).json()
    assert listing["composite"] == "airmass"
    assert [i["name"] for i in listing["images"]] == ["20260926T142000Z"]

    unknown = httpx.get(f"{live_server.url}/api/regions/nope/archive")
    assert unknown.status_code == 404


def test_archive_images_are_served_as_png_and_jpeg(live_server):
    archive_of(live_server).store("wien", "dummy", "cloudtop", NOW, png("blue"))
    base = f"{live_server.url}/regions/wien/archive/dummy/cloudtop/20260926T142000Z"
    image = httpx.get(f"{base}.png")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == png("blue")
    jpeg = httpx.get(f"{base}.jpg")
    assert jpeg.status_code == 200 and jpeg.headers["content-type"] == "image/jpeg"
    assert httpx.get(f"{base}.gif").status_code in (400, 404)
    assert httpx.get(
        f"{live_server.url}/regions/wien/archive/dummy/cloudtop/20200101T000000Z.png"
    ).status_code == 404


def test_render_all_setting_and_manual_run(live_server):
    response = httpx.post(f"{live_server.url}/api/storage", json={"archive_render_all": True})
    assert response.status_code == 200, response.text
    stored = yaml.safe_load(live_server.config_file.read_text())["archive"]
    assert stored["render_all"] is True

    started = httpx.post(f"{live_server.url}/api/archive/render", json={})
    assert started.status_code == 200, started.text
    assert started.json()["sources"] >= 1

    # The dummy source renders quickly - the archive fills up on its own.
    deadline = 30
    for _ in range(deadline * 2):
        if archive_of(live_server).count():
            break
        import time

        time.sleep(0.5)
    archive = archive_of(live_server)
    assert archive.count() > 0, "nothing was archived"
    combos = archive.combinations("wien")
    assert "dummy" in combos
    # Every image type of the driver is archived, not only the configured one.
    assert len(combos["dummy"]) > 1
    listing = httpx.get(f"{live_server.url}/api/regions/wien/archive?source=dummy").json()
    assert listing["images"] and listing["images"][0]["cached"] is True
    image = httpx.get(live_server.url + listing["images"][0]["url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
