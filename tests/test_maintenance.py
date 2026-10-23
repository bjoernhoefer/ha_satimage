"""Tests for the EUMETSAT RSS maintenance schedule (maintenance.py) and how
the scheduler pauses Data Store queries during a maintenance window."""

from __future__ import annotations

import io
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from ha_satimage import maintenance
from ha_satimage.buffer import BufferManager
from ha_satimage.config import ConfigStore
from ha_satimage.maintenance import (
    MAINTENANCE_COMPOSITE,
    MaintenanceSchedule,
    Window,
    affects,
    parse_schedule,
    render_maintenance_png,
)
from ha_satimage.scheduler import RenderScheduler
from ha_satimage.sources import RenderedFrame
from ha_satimage.status import StatusStore

UTC = timezone.utc


def _w(start: str, end: str) -> Window:
    return Window(datetime.fromisoformat(start), datetime.fromisoformat(end))


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (
            "<p>Last updated: 01 October 2026</p><table><tr><th>Start</th><th>End</th></tr>"
            "<tr><td>2026/09/08 09:00 UTC</td><td>2026/09/10 09:00 UTC</td></tr>"
            "<tr><td>13 October 2026, 09:00</td><td>15 October 2026, 09:00</td></tr></table>",
            [
                _w("2026-09-08T09:00+00:00", "2026-09-10T09:00+00:00"),
                _w("2026-10-13T09:00+00:00", "2026-10-15T09:00+00:00"),
            ],
        ),
        (
            "<dl><dt>Start Time:</dt><dd>2026-11-10T09:00:00Z</dd>"
            "<dt>End Time:</dt><dd>2026-11-12T09:00:00Z</dd></dl>",
            [_w("2026-11-10T09:00+00:00", "2026-11-12T09:00+00:00")],
        ),
        (
            "<li>Tuesday 8 December 2026 09:00 - 13:30 UTC</li>",
            [_w("2026-12-08T09:00+00:00", "2026-12-08T13:30+00:00")],
        ),
        (
            "<li>08.12.2026 &ndash; 09.12.2026</li>",
            [_w("2026-12-08T00:00+00:00", "2026-12-10T00:00+00:00")],
        ),
        (
            '<script>{"start":"2026-12-01T08:00:00.000Z","end":"2026-12-03T08:00:00.000Z"}</script>',
            [_w("2026-12-01T08:00+00:00", "2026-12-03T08:00+00:00")],
        ),
        (
            "<p>December 1, 2026 at 08:00 to December 3, 2026 at 08:00</p>",
            [_w("2026-12-01T08:00+00:00", "2026-12-03T08:00+00:00")],
        ),
        # Dates far apart are not a window; a page without dates has none.
        ("<p>2026-01-01 10:00</p><p>2026-06-01 10:00</p>", []),
        ("<p>No maintenance planned.</p>", []),
    ],
)
def test_parse_schedule(page, expected):
    assert parse_schedule(page) == expected


def test_only_rss_collections_are_affected():
    assert affects("EO:EUM:DAT:MSG:MSG15-RSS")
    assert not affects("EO:EUM:DAT:MSG:HRSEVIRI")
    assert not affects("EO:EUM:DAT:0662")
    assert not affects(None)


def test_maintenance_image_is_black_with_text():
    window = _w("2026-11-10T09:00+00:00", "2026-11-12T09:00+00:00")
    for size in ((800, 800), (320, 200)):
        image = Image.open(io.BytesIO(render_maintenance_png(*size, window))).convert("L")
        assert image.size == size
        histogram = image.histogram()
        assert histogram[0] > size[0] * size[1] * 0.8  # mostly black
        assert sum(histogram[200:]) > 0  # white text
        # Text stays inside the image horizontally.
        assert image.getpixel((0, size[1] // 2)) == 0
        assert image.getpixel((size[0] - 1, size[1] // 2)) == 0


class _Page(BaseHTTPRequestHandler):
    body = ""
    requests = 0

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests += 1
        data = type(self).body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def schedule_page(monkeypatch):
    handler = type("Handler", (_Page,), {"body": "", "requests": 0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(maintenance.URL_ENV, f"http://127.0.0.1:{server.server_address[1]}/rss")
    yield handler
    server.shutdown()


def _page_for(start: datetime, end: datetime) -> str:
    fmt = "%Y/%m/%d %H:%M"
    return f"<table><tr><td>{start.strftime(fmt)}</td><td>{end.strftime(fmt)}</td></tr></table>"


def test_schedule_is_fetched_lazily_cached_and_persisted(tmp_path, schedule_page):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    schedule_page.body = _page_for(now - timedelta(hours=1), now + timedelta(hours=2))
    schedule = MaintenanceSchedule(tmp_path / "maintenance.json")

    # Unaffected collections never fetch the page; "0 hours" switches it off.
    assert schedule.window_for("EO:EUM:DAT:MSG:HRSEVIRI", 6) is None
    assert schedule.window_for("EO:EUM:DAT:MSG:MSG15-RSS", 0) is None
    assert schedule_page.requests == 0

    window = schedule.window_for("EO:EUM:DAT:MSG:MSG15-RSS", 6)
    assert window == Window(now - timedelta(hours=1), now + timedelta(hours=2))
    assert schedule.window_for("EO:EUM:DAT:MSG:MSG15-RSS", 6) == window
    assert schedule_page.requests == 1  # cached until the next check

    # Survives a restart without fetching again.
    restarted = MaintenanceSchedule(tmp_path / "maintenance.json")
    assert restarted.window_for("EO:EUM:DAT:MSG:MSG15-RSS", 6) == window
    assert schedule_page.requests == 1
    assert restarted.window_for("EO:EUM:DAT:MSG:MSG15-RSS", 6, now + timedelta(hours=3)) is None


def test_fetch_error_keeps_known_windows(tmp_path, monkeypatch, schedule_page):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    schedule_page.body = _page_for(now - timedelta(hours=1), now + timedelta(hours=2))
    schedule = MaintenanceSchedule(tmp_path / "maintenance.json")
    assert schedule.window_for("EO:EUM:DAT:MSG:MSG15-RSS", 6) is not None

    monkeypatch.setenv(maintenance.URL_ENV, "http://127.0.0.1:9/rss")
    schedule.refresh(6, force=True)
    assert schedule.state()["error"]
    assert schedule.window_for("EO:EUM:DAT:MSG:MSG15-RSS", 6) is not None


class _FetchSource:
    def __init__(self):
        self.fetches = 0
        self.renders = 0

    def fetch(self, entry, config):
        self.fetches += 1
        return datetime.now(UTC) - timedelta(minutes=2)

    def render(self, region, config, last_sensing=None):
        self.renders += 1
        return RenderedFrame(b"real", datetime.now(UTC))


def test_scheduler_pauses_queries_and_shows_maintenance_frame(tmp_path, monkeypatch, schedule_page):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    schedule_page.body = _page_for(now - timedelta(hours=1), now + timedelta(hours=2))
    store = ConfigStore(tmp_path / "config.yaml")  # regions on Rapid Scan
    buffers = BufferManager(tmp_path / "frames")
    schedule = MaintenanceSchedule(tmp_path / "maintenance.json")
    scheduler = RenderScheduler(store, buffers, StatusStore(), maintenance=schedule)
    source = _FetchSource()
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)
    queued: list[str] = []
    monkeypatch.setattr(scheduler, "_queue_render", queued.append)

    # During maintenance the Data Store is not queried; regions get the image.
    assert scheduler.download("msg_seviri") is False
    assert source.fetches == 0
    assert sorted(queued) == ["europe", "mallorca", "wien"]
    state = scheduler._downloads["msg_seviri"]
    assert state.last_error is None
    assert state.due <= now + timedelta(hours=2)
    status = scheduler.download_status()["msg_seviri"]
    assert status["maintenance"]["active"] is True
    assert status["maintenance"]["end"] == (now + timedelta(hours=2)).isoformat()

    # Same window again: no new renders queued.
    queued.clear()
    assert scheduler.download("msg_seviri") is False
    assert queued == []

    scheduler._run_region("wien")
    buffer = buffers.get("wien", 10, 500)
    latest = buffer.latest()
    assert latest.composite == MAINTENANCE_COMPOSITE and source.renders == 0
    image = Image.open(latest.path(buffer.region_dir))
    assert image.size == (store.get().region("wien").width, store.get().region("wien").height)
    # Only one maintenance frame per window.
    scheduler._run_region("wien")
    assert len(buffer) == 1

    # Maintenance over: queries resume and the region renders real data again.
    schedule_page.body = "<p>No maintenance planned.</p>"
    schedule.refresh(6, force=True)
    assert scheduler.download("msg_seviri") is True
    assert source.fetches == 1
    assert state.maintenance is None
    scheduler._run_region("wien")
    assert buffer.latest().composite != MAINTENANCE_COMPOSITE
    assert source.renders == 1
    assert scheduler.download_status()["msg_seviri"]["maintenance"] is None


def test_upcoming_maintenance_is_reported_but_not_applied(tmp_path, monkeypatch, schedule_page):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    schedule_page.body = _page_for(now + timedelta(days=1), now + timedelta(days=2))
    store = ConfigStore(tmp_path / "config.yaml")
    schedule = MaintenanceSchedule(tmp_path / "maintenance.json")
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore(), maintenance=schedule)
    source = _FetchSource()
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)
    monkeypatch.setattr(scheduler, "_queue_render", lambda name: None)

    assert scheduler.download("msg_seviri") is True
    assert source.fetches == 1
    info = scheduler.download_status()["msg_seviri"]["maintenance"]
    assert info["active"] is False
    assert info["start"] == (now + timedelta(days=1)).isoformat()
    # Other collections are not affected by the RSS schedule.
    assert scheduler.download_status()["msg_seviri_0deg"]["maintenance"] is None


def test_source_is_checked_hourly_during_maintenance(tmp_path, monkeypatch, schedule_page):
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    schedule_page.body = _page_for(now - timedelta(hours=1), now + timedelta(hours=5))
    store = ConfigStore(tmp_path / "config.yaml")
    buffers = BufferManager(tmp_path / "frames")
    schedule = MaintenanceSchedule(tmp_path / "maintenance.json")
    scheduler = RenderScheduler(store, buffers, StatusStore(), maintenance=schedule)
    source = _FetchSource()
    old_scan = now - timedelta(hours=2)  # newest product before the window
    source.fetch = lambda entry, config: (setattr(source, "fetches", source.fetches + 1), old_scan)[1]
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)
    queued: list[str] = []
    monkeypatch.setattr(scheduler, "_queue_render", queued.append)

    # Window starts: no query, the first check follows after one hour.
    assert scheduler.download("msg_seviri") is False
    state = scheduler._downloads["msg_seviri"]
    assert source.fetches == 0
    assert abs((state.next_probe_at - (datetime.now(UTC) + timedelta(hours=1))).total_seconds()) < 5
    assert state.due <= state.next_probe_at
    # Before the hour is up: still no query.
    assert scheduler.download("msg_seviri") is False
    assert source.fetches == 0

    # Hour is up, the source delivers nothing new: still down, no error.
    state.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
    queued.clear()
    assert scheduler.download("msg_seviri") is False
    assert source.fetches == 1
    assert state.last_error is None and state.last_probe_result == "down"
    assert state.next_probe_at > datetime.now(UTC) + timedelta(minutes=59)
    assert queued == []  # maintenance image stays
    info = scheduler.download_status()["msg_seviri"]["maintenance"]
    assert info["active"] is True and info["last_probe_result"] == "down"
    assert info["next_probe_at"]

    # Query fails (typical during maintenance): still down, no error recorded.
    def failing(entry, config):
        source.fetches += 1
        raise RuntimeError("no product")

    source.fetch = failing
    state.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
    assert scheduler.download("msg_seviri") is False
    assert source.fetches == 2 and state.last_error is None

    # Source delivers again before the announced end: maintenance ended early.
    fresh = datetime.now(UTC) - timedelta(minutes=2)
    source.fetch = lambda entry, config: (setattr(source, "fetches", source.fetches + 1), fresh)[1]
    state.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
    assert scheduler.download("msg_seviri") is True
    assert source.fetches == 3  # the check's result is used, no second query
    assert sorted(queued) == ["europe", "mallorca", "wien"]
    assert state.maintenance is None and state.last_sensing == fresh
    info = scheduler.download_status()["msg_seviri"]["maintenance"]
    assert info["active"] is False and info["ended_early"] is True

    # Normal operation for the rest of the window: real frames, regular downloads.
    scheduler._run_region("wien")
    assert buffers.get("wien", 10, 500).latest().composite != MAINTENANCE_COMPOSITE
    scheduler.download("msg_seviri")
    assert source.fetches == 4
