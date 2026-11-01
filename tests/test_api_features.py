"""API tests: source catalog, sync, storage location, history, logs, live."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import FAKE_LATEST


def _stored(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))


def _wait_for_thumbnail(server) -> Path:
    """Wait until the scheduler pre-generated the newest frame's thumbnail.

    Re-reads the newest frame each poll: a further render may replace it,
    and the scheduler only prewarms the newest frame.
    """
    deadline = time.monotonic() + 20
    thumbnail = None
    while time.monotonic() < deadline:
        thumbnail = Path(server.frames("wien")[0]["filename"]).stem + "-w240.jpg"
        matches = [
            path
            for root in (server.data_dir, server.storage_root)
            for path in root.rglob(f"wien/_cache/{thumbnail}")
        ]
        if matches:
            return matches[0]
        time.sleep(0.1)
    raise AssertionError(f"Scheduler did not generate thumbnail {thumbnail} within 20s")


def test_catalog_can_be_replaced_and_is_validated(live_server):
    catalog = httpx.get(f"{live_server.url}/api/sources").json()["catalog"]
    catalog.append({"id": "iodc", "driver": "msg_seviri", "collection": "EO:EUM:DAT:MSG:HRSEVIRI-IODC"})
    response = httpx.post(f"{live_server.url}/api/config", json={"sources": {"catalog": catalog}})
    assert response.status_code == 200
    assert "iodc" in [e["id"] for e in _stored(live_server)["sources"]["catalog"]]

    # Removing a source still used by a region -> 400.
    without_dummy = [e for e in catalog if e["id"] != "dummy"]
    response = httpx.post(f"{live_server.url}/api/config", json={"sources": {"catalog": without_dummy}})
    assert response.status_code == 400
    assert "dummy" in response.json()["detail"]

    response = httpx.post(
        f"{live_server.url}/api/config",
        json={"sources": {"catalog": [*catalog, {"id": "bad", "driver": "nope"}]}},
    )
    assert response.status_code == 400


def test_sync_reports_availability_and_discovers_collections(start_server, fake_eumetsat):
    server = start_server(env={"HA_SATIMAGE_EUMETSAT_API": fake_eumetsat})
    result = httpx.post(f"{server.url}/api/sources/sync", timeout=30).json()
    assert result["error"] is None
    assert result["sources"]["msg_seviri"]["status"] == "ok"
    assert result["sources"]["msg_seviri"]["latest_product_at"] == FAKE_LATEST
    assert result["sources"]["dummy"]["status"] == "local"
    discovered = {d["collection"] for d in result["discovered"]}
    # HRSEVIRI, MSG15-RSS and 0662 are known; Cloud Mask matches no driver.
    assert discovered == {"EO:EUM:DAT:MSG:HRSEVIRI-IODC", "EO:EUM:DAT:0665"}

    adopted = httpx.post(
        f"{server.url}/api/sources/adopt", json={"collection": "EO:EUM:DAT:0665"}
    ).json()
    assert adopted["driver"] == "mtg_fci"
    assert adopted["enabled"] is False
    sources = httpx.get(f"{server.url}/api/sources").json()
    assert adopted["id"] in [e["id"] for e in sources["catalog"]]
    assert "EO:EUM:DAT:0665" not in {d["collection"] for d in sources["sync"]["discovered"]}
    assert (server.data_dir / "source_sync.json").exists()


def test_sync_failure_is_reported_not_raised(live_server):
    result = httpx.post(f"{live_server.url}/api/sources/sync", timeout=30).json()
    assert result["error"]


def test_disabled_source_is_not_scheduled_but_manual_refresh_works(live_server):
    catalog = httpx.get(f"{live_server.url}/api/sources").json()["catalog"]
    for entry in catalog:
        entry["enabled"] = entry["id"] == "dummy"
    regions = httpx.get(f"{live_server.url}/api/config").json()["regions"]
    for region in regions:
        region["source"] = "dummy" if region["name"] == "wien" else "msg_seviri"
    response = httpx.post(
        f"{live_server.url}/api/config", json={"sources": {"catalog": catalog}, "regions": regions}
    )
    assert response.status_code == 200
    assert live_server.ensure_frames("mallorca", 1)


def test_history_endpoints(live_server):
    frames = live_server.ensure_frames("wien", 3)
    assert [f["index"] for f in frames] == list(range(len(frames)))
    assert frames[0]["created_at"] > frames[-1]["created_at"]

    full = httpx.get(live_server.url + frames[1]["url"])
    assert full.status_code == 200 and full.headers["content-type"] == "image/png"
    thumb = httpx.get(live_server.url + frames[1]["url"] + "?w=120")
    assert thumb.headers["content-type"] == "image/jpeg"
    assert len(thumb.content) < len(full.content)

    # Full-size JPEG (viewer default), cached.
    assert frames[1]["jpeg_url"] == frames[1]["url"][: -len(".png")] + ".jpg"
    jpeg = httpx.get(live_server.url + frames[1]["jpeg_url"])
    assert jpeg.status_code == 200 and jpeg.headers["content-type"] == "image/jpeg"
    assert jpeg.content.startswith(b"\xff\xd8")
    latest = httpx.get(f"{live_server.url}/regions/wien/latest.jpg")
    assert latest.status_code == 200 and latest.headers["content-type"] == "image/jpeg"
    assert httpx.get(f"{live_server.url}/regions/nope/latest.jpg").status_code == 404

    for bad in ("../config.yaml", "_index.json", "nope.png", "nope.jpg", "_index.jpg"):
        assert httpx.get(f"{live_server.url}/regions/wien/history/{bad}").status_code == 404


def test_animations_and_thumbnails_are_cached(live_server):
    live_server.ensure_frames("wien", 2)
    thumbnail = _wait_for_thumbnail(live_server)
    first = httpx.get(f"{live_server.url}/regions/wien/animation.gif", timeout=30)
    assert first.status_code == 200 and first.content.startswith(b"GIF")
    assert httpx.get(f"{live_server.url}/regions/wien/animation.gif").content == first.content
    mp4 = httpx.get(f"{live_server.url}/regions/wien/animation.mp4", timeout=60)
    assert mp4.status_code == 200 and mp4.headers["content-type"] == "video/mp4"
    region_dirs = [
        p for root in (live_server.data_dir, live_server.storage_root) for p in root.rglob("wien/_cache")
    ]
    assert region_dirs
    names = {p.name for p in region_dirs[0].iterdir()}
    assert any(n.startswith("animation-") and n.endswith(".gif") for n in names)
    assert thumbnail.name in names


def test_scheduler_generates_thumbnail_without_image_requests(live_server):
    live_server.ensure_frames("wien", 1)
    thumbnail = _wait_for_thumbnail(live_server)
    assert thumbnail.read_bytes().startswith(b"\xff\xd8")


def test_sources_report_cycle_and_download_state(live_server):
    downloads = httpx.get(f"{live_server.url}/api/sources").json()["downloads"]
    assert downloads["msg_seviri"]["cycle_minutes"] == 5
    assert downloads["msg_seviri_0deg"]["cycle_minutes"] == 15
    assert downloads["mtg_fci"]["cycle_minutes"] == 10
    assert downloads["mtg_fci"]["downloads"] is True
    # Test server: only the placeholder source is active -> no download job.
    assert downloads["dummy"]["downloads"] is False
    assert not any(d["active"] for d in downloads.values())


def test_storage_relocation_moves_frames(live_server):
    live_server.ensure_frames("wien", 2)
    before = live_server.frames("wien")
    target = live_server.storage_root / "data2" / "ha_satimage"
    (live_server.storage_root / "data2").mkdir()

    info = httpx.get(f"{live_server.url}/api/storage").json()
    assert str(target) in [c["path"] for c in info["candidates"]]
    assert info["current"] == str(live_server.data_dir / "frames")

    response = httpx.post(
        f"{live_server.url}/api/storage",
        json={"frames_dir": str(target), "move_existing": True, "history_minutes": 120},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    assert response.json()["moved_frames"] >= len(before)
    assert _stored(live_server)["storage"]["frames_dir"] == str(target)
    assert _stored(live_server)["history"]["history_minutes"] == 120
    assert (target / "wien" / before[0]["filename"]).exists()
    assert not (live_server.data_dir / "frames" / "wien").exists()
    after = {f["filename"] for f in live_server.frames("wien")}
    assert {f["filename"] for f in before} <= after
    assert httpx.get(f"{live_server.url}/regions/wien/latest.png").status_code == 200

    # Back to the default: empty path.
    response = httpx.post(f"{live_server.url}/api/storage", json={"frames_dir": ""}, timeout=30)
    assert response.json()["current"] == str(live_server.data_dir / "frames")
    assert _stored(live_server)["storage"]["frames_dir"] == ""


def test_storage_rejects_relative_and_unwritable_paths(live_server):
    response = httpx.post(f"{live_server.url}/api/storage", json={"frames_dir": "relative"})
    assert response.status_code == 400
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions; CI runs this check unprivileged")
    locked = live_server.storage_root / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        response = httpx.post(
            f"{live_server.url}/api/storage", json={"frames_dir": str(locked / "sub")}
        )
        assert response.status_code == 400
        assert "not writable" in response.json()["detail"]
        assert _stored_or_default(live_server) == ""
    finally:
        locked.chmod(0o700)


def _stored_or_default(server) -> str:
    if not server.config_file.exists():
        return ""
    return _stored(server)["storage"]["frames_dir"]


def test_logs_endpoint_shows_render_activity(live_server):
    live_server.ensure_frames("wien", 1)
    entries = httpx.get(f"{live_server.url}/api/logs").json()["entries"]
    messages = [e["message"] for e in entries]
    assert any("Render wien finished" in m for m in messages)
    last_id = entries[-1]["id"]
    newer = httpx.get(f"{live_server.url}/api/logs?after={last_id}").json()["entries"]
    assert all(e["id"] > last_id for e in newer)
    text = httpx.get(f"{live_server.url}/api/logs?format=text&limit=0").text
    assert "Render wien" in text
    assert (live_server.data_dir / "logs" / "ha_satimage.log").exists()


def test_live_redirect_and_mjpeg_stream(live_server):
    live_server.ensure_frames("wien", 1)
    response = httpx.get(f"{live_server.url}/live/wien", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/#live=wien"
    assert httpx.get(f"{live_server.url}/live/nope", follow_redirects=False).status_code == 404

    with httpx.stream("GET", f"{live_server.url}/regions/wien/mjpeg", timeout=10) as stream:
        assert stream.headers["content-type"].startswith("multipart/x-mixed-replace")
        chunk = next(stream.iter_bytes())
        assert b"--frame" in chunk
