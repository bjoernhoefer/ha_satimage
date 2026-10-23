"""Tests for the data sources: download cache, dedup and crop window.

``eumdac`` and the Satpy child process are mocked; the crop test uses the
real geometry of the SEVIRI Rapid Scan (mirrored area, 3712x1392) without
needing a ~100 MB product.
"""

from __future__ import annotations

import io
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ha_satimage import sources
from ha_satimage.buffer import BufferManager
from ha_satimage.config import COMPOSITES, AppConfig, ConfigStore, EumetsatCredentials, default_config
from ha_satimage.scheduler import RenderScheduler
from ha_satimage.sources import (
    MsgSeviriSource,
    NoNewData,
    RenderedFrame,
    RenderError,
)
from ha_satimage.sources.eumetsat import DataStoreError, Product, ProductCache
from ha_satimage.status import StatusStore

CREDS = EumetsatCredentials(consumer_key="key", consumer_secret="secret")
SENSING = datetime(2026, 9, 26, 10, 40, tzinfo=timezone.utc)


# -- Fake eumdac -----------------------------------------------------------
class FakeProduct:
    def __init__(self, product_id: str, sensing_end: datetime, payload: bytes = b"NAT"):
        self._id = product_id
        self.sensing_end = sensing_end.replace(tzinfo=None)
        self.entries = [f"{product_id}.nat", "manifest.xml"]
        self.payload = payload
        self.open_calls = 0

    def __str__(self) -> str:
        return self._id

    def open(self, entry: str):
        assert entry.endswith(".nat")
        self.open_calls += 1
        return io.BytesIO(self.payload)


def install_fake_eumdac(monkeypatch, products, search_error: Exception | None = None):
    module = types.ModuleType("eumdac")
    calls = {"tokens": []}

    def access_token(credentials):
        calls["tokens"].append(credentials)
        return "token"

    class Collection:
        def search(self, dtstart, dtend):
            if search_error:
                raise search_error
            assert dtstart < dtend
            return iter(products)

    class DataStore:
        def __init__(self, token):
            assert token == "token"

        def get_collection(self, collection_id):
            calls["collection"] = collection_id
            return Collection()

    module.AccessToken = access_token
    module.DataStore = DataStore
    monkeypatch.setitem(sys.modules, "eumdac", module)
    return calls


# -- ProductCache ------------------------------------------------------------
def test_product_cache_downloads_once_and_keeps_only_latest(tmp_path, monkeypatch):
    first = FakeProduct("MSG4-A", SENSING, b"first")
    products = [first]
    calls = install_fake_eumdac(monkeypatch, products)
    cache = ProductCache(tmp_path)

    product = cache.latest("EO:EUM:DAT:MSG:MSG15-RSS", CREDS, ".nat")
    again = cache.latest("EO:EUM:DAT:MSG:MSG15-RSS", CREDS, ".nat")

    assert product == again
    assert product.path.read_bytes() == b"first"
    assert product.sensing_end == SENSING
    assert first.open_calls == 1
    assert calls["tokens"][0] == ("key", "secret")
    assert calls["collection"] == "EO:EUM:DAT:MSG:MSG15-RSS"

    newer = FakeProduct("MSG4-B", SENSING + timedelta(minutes=5), b"second")
    products.insert(0, newer)
    latest = cache.latest("EO:EUM:DAT:MSG:MSG15-RSS", CREDS, ".nat")
    assert latest.product_id == "MSG4-B"
    assert cache.cached("EO:EUM:DAT:MSG:MSG15-RSS") == latest
    # Previous product is kept for a render that may still be running.
    assert sorted(p.name for p in latest.path.parent.iterdir()) == ["MSG4-A.nat", "MSG4-B.nat"]

    products.insert(0, FakeProduct("MSG4-C", SENSING + timedelta(minutes=10), b"third"))
    third = cache.latest("EO:EUM:DAT:MSG:MSG15-RSS", CREDS, ".nat")
    assert sorted(p.name for p in third.path.parent.iterdir()) == ["MSG4-B.nat", "MSG4-C.nat"]
    assert cache.cached("andere-collection") is None


def test_product_cache_requires_credentials(tmp_path):
    with pytest.raises(DataStoreError, match="credentials"):
        ProductCache(tmp_path).latest("c", EumetsatCredentials(), ".nat")


def test_product_cache_wraps_search_errors(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [], search_error=RuntimeError("401 Unauthorized"))
    with pytest.raises(DataStoreError, match="401"):
        ProductCache(tmp_path).latest("c", CREDS, ".nat")


def test_product_cache_reports_empty_search(tmp_path, monkeypatch):
    install_fake_eumdac(monkeypatch, [])
    with pytest.raises(DataStoreError, match="No product"):
        ProductCache(tmp_path).latest("c", CREDS, ".nat")


def test_product_cache_removes_partial_download(tmp_path, monkeypatch):
    broken = FakeProduct("MSG4-X", SENSING)

    def failing_open(entry):
        raise TimeoutError("read timed out")

    broken.open = failing_open
    install_fake_eumdac(monkeypatch, [broken])
    with pytest.raises(DataStoreError, match="timed out"):
        ProductCache(tmp_path).latest("c", CREDS, ".nat")
    assert not any(tmp_path.rglob("*.part"))


# -- MsgSeviriSource -----------------------------------------------------------
class StubCache:
    def __init__(self, product: Product | None = None, error: Exception | None = None):
        self.product = product
        self.error = error
        self.latest_calls = 0
        self.local: Product | None = None

    def latest(self, collection_id, credentials, suffix):
        self.latest_calls += 1
        if self.error:
            raise self.error
        return self.product

    def cached(self, collection_id):
        return self.local


@pytest.fixture
def config() -> AppConfig:
    data = default_config().model_dump()
    data["eumetsat"] = CREDS.model_dump()
    return AppConfig(**data)


def test_msg_seviri_renders_new_product(tmp_path, monkeypatch, config):
    product = Product("MSG4-A", SENSING, tmp_path / "a.nat")
    monkeypatch.setattr(sources, "_product_cache", lambda: StubCache(product))
    seen = {}

    def fake_render(request):
        seen["request"] = request
        return b"PNG", SENSING.replace(tzinfo=None)

    monkeypatch.setattr("ha_satimage.sources.satpy_render.render_in_subprocess", fake_render)

    frame = MsgSeviriSource().render(config.regions[0], config, SENSING - timedelta(minutes=5))

    assert frame == RenderedFrame(b"PNG", SENSING)
    assert seen["request"].filenames == (str(product.path),)
    assert seen["request"].composite == config.regions[0].composite
    assert seen["request"].reader == "seviri_l1b_native"
    assert seen["request"].borders is True


def test_msg_seviri_skips_already_rendered_product(tmp_path, monkeypatch, config):
    product = Product("MSG4-A", SENSING, tmp_path / "a.nat")
    monkeypatch.setattr(sources, "_product_cache", lambda: StubCache(product))
    with pytest.raises(NoNewData):
        MsgSeviriSource().render(config.regions[0], config, SENSING)


@pytest.mark.parametrize("composite", list(COMPOSITES))
def test_europe_overview_passes_extent_for_every_msg_variant(tmp_path, monkeypatch, composite):
    config = default_config()
    region = config.region("europe")
    region.composite = composite
    product = Product("MSG4-A", SENSING, tmp_path / "a.nat")
    monkeypatch.setattr(sources, "_product_cache", lambda: StubCache(product))
    requests = []

    def fake_render(request):
        requests.append(request)
        return b"PNG", SENSING

    monkeypatch.setattr("ha_satimage.sources.satpy_render.render_in_subprocess", fake_render)
    MsgSeviriSource().render(region, config)
    [render] = requests
    assert render.lonlat_extent == (-25, 33, 45, 72)
    assert render.composite == composite
    assert render.borders is False
    assert (render.width, render.height) == (1400, 1280)


def test_msg_seviri_maps_errors_to_render_error(tmp_path, monkeypatch, config):
    monkeypatch.setattr(
        sources, "_product_cache", lambda: StubCache(error=DataStoreError("broken"))
    )
    with pytest.raises(RenderError, match="broken"):
        MsgSeviriSource().render(config.regions[0], config, None)


def test_msg_seviri_renders_downloaded_product_without_network(tmp_path, monkeypatch, config):
    local = Product("MSG4-L", SENSING, tmp_path / "l.nat")
    stub = StubCache(error=DataStoreError("must not be queried"))
    stub.local = local
    monkeypatch.setattr(sources, "_product_cache", lambda: stub)
    seen = {}

    def fake_render(request):
        seen["files"] = request.filenames
        return b"PNG", SENSING

    monkeypatch.setattr("ha_satimage.sources.satpy_render.render_in_subprocess", fake_render)
    MsgSeviriSource().render(config.regions[0], config, None)
    assert seen["files"] == (str(local.path),)
    assert stub.latest_calls == 0

    # fetch(), in contrast, always downloads via the Data Store.
    stub.error, stub.product = None, Product("MSG4-N", SENSING + timedelta(minutes=5), tmp_path / "n")
    entry = config.sources.get("msg_seviri")
    assert MsgSeviriSource().fetch(entry, config) == SENSING + timedelta(minutes=5)
    assert stub.latest_calls == 1


# -- Scheduler -------------------------------------------------------------------
class SlowSource:
    """Renders slowly and checks that two runs never overlap."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.rendered: list[str] = []
        self.guard = threading.Lock()

    def render(self, region, config, last_sensing=None):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.2)
        with self.guard:
            self.active -= 1
            self.rendered.append(region.name)
        if last_sensing is not None and last_sensing >= SENSING:
            raise NoNewData("gleiches Produkt")
        return RenderedFrame(b"PNG-" + region.name.encode(), SENSING)


def test_concurrent_regions_wait_for_lock_instead_of_skipping(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")
    buffers = BufferManager(tmp_path / "frames")
    status = StatusStore()
    scheduler = RenderScheduler(store, buffers, status)
    source = SlowSource()
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)

    threads = [
        threading.Thread(target=scheduler._run_region, args=(name,))
        for name in ("wien", "mallorca")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(source.rendered) == ["mallorca", "wien"]
    assert source.max_active == 1
    for name in ("wien", "mallorca"):
        buffer = buffers.get(name, 4, 500)
        assert len(buffer) == 1
        assert datetime.fromisoformat(buffer.latest().created_at) == SENSING
        assert status.get(name).last_error is None

    # Second run: same acquisition -> no new frame, but no error either.
    scheduler._run_region("wien")
    assert len(buffers.get("wien", 4, 500)) == 1
    assert status.get("wien").last_error is None


# -- Crop-Fenster ------------------------------------------------------------------
pyresample = pytest.importorskip("pyresample")

RSS_PROJ = (
    "+proj=geos +lon_0=9.5 +h=35785831 +x_0=0 +y_0=0 +a=6378169 "
    "+rf=295.488065897014 +units=m +no_defs"
)
# As in the product: descending x extent (mirrored), only the northern stripe.
RSS_EXTENT = (5567248.0, 5570248.5, -5570248.5, 1393687.2)


@pytest.fixture(scope="module")
def rss_area():
    from pyresample.geometry import AreaDefinition

    return AreaDefinition("rss", "rss", "rss", RSS_PROJ, 3712, 1392, RSS_EXTENT)


@pytest.mark.parametrize(
    ("name", "lat", "lon"),
    [("wien", 48.2082, 16.3738), ("mallorca", 39.6953, 3.0176), ("oslo", 59.91, 10.75)],
)
def test_source_window_covers_whole_target_region(rss_area, name, lat, lon):
    from ha_satimage.sources.satpy_render import RenderRequest, source_window, target_area

    request = RenderRequest("r", ("f",), "c", lat, lon, 300, 200, 200, name)
    area = target_area(request)
    rows, cols = source_window(rss_area, area)

    lons, lats = area.get_lonlats()
    all_cols, all_rows = rss_area.get_array_indices_from_lonlat(lons[::10, ::10], lats[::10, ::10])
    assert not np.ma.is_masked(all_cols)
    assert rows.start <= int(all_rows.min()) and int(all_rows.max()) < rows.stop
    assert cols.start <= int(all_cols.min()) and int(all_cols.max()) < cols.stop
    # 600 km at ~3-5 km pixels: the window must be well over 100 pixels
    # (pyresample's reduce_data returned only 26x4 for Vienna).
    assert rows.stop - rows.start > 100
    assert cols.stop - cols.start > 100


def test_source_window_rejects_region_outside_disk(rss_area):
    from ha_satimage.sources.satpy_render import (
        RenderRequest,
        SatpyRenderError,
        source_window,
        target_area,
    )

    request = RenderRequest("r", ("f",), "c", 35.0, 139.0, 300, 50, 50, "tokio")
    with pytest.raises(SatpyRenderError, match="outside"):
        source_window(rss_area, target_area(request))


# -- HRV sharpening ----------------------------------------------------------------
HRV_EXTENT = (5566247.7, 5571249.0, -5571249.0, 1392686.9)


def test_crop_uses_own_window_per_resolution(rss_area):
    import xarray as xr
    from pyresample.geometry import AreaDefinition

    from ha_satimage.sources.satpy_render import RenderRequest, _crop_to, target_area

    hrv_area = AreaDefinition("hrv", "hrv", "hrv", RSS_PROJ, 3712 * 3, 1392 * 3, HRV_EXTENT)
    target = target_area(RenderRequest("r", ("f",), "c", 48.2, 16.37, 300, 200, 200, "wien"))
    vis = xr.DataArray(np.zeros(rss_area.shape), dims=("y", "x"), attrs={"area": rss_area})
    hrv = xr.DataArray(np.zeros(hrv_area.shape), dims=("y", "x"), attrs={"area": hrv_area})

    vis_crop, hrv_crop = _crop_to(vis, target), _crop_to(hrv, target)

    assert vis_crop.attrs["area"].shape == vis_crop.shape
    assert hrv_crop.attrs["area"].shape == hrv_crop.shape
    # same area, triple resolution (± margin)
    assert abs(hrv_crop.shape[0] - 3 * vis_crop.shape[0]) < 3 * 2 * 16
    assert abs(hrv_crop.shape[1] - 3 * vis_crop.shape[1]) < 3 * 2 * 16


def _band(values, name):
    import xarray as xr

    return xr.DataArray(
        np.array(values, dtype=float), dims=("y", "x"), attrs={"name": name, "units": "%"}
    )


def test_hrv_sharpening_scales_brightness_and_keeps_hue():
    pytest.importorskip("satpy")
    from ha_satimage.sources.hrv_composite import MAX_RATIO, HrvLuminanceSharpenedRGB

    red, green, blue = _band([[10, 10]], "IR_016"), _band([[20, 20]], "VIS008"), _band([[40, 40]], "VIS006")
    hrv = _band([[60, 300]], "HRV")  # Faktor 2 bzw. 10 (-> gekappt)

    result = HrvLuminanceSharpenedRGB("natural_color_hrv")((red, green, blue), optional_datasets=(hrv,))

    assert result.sel(bands="R").values.tolist() == [[20, 10 * MAX_RATIO]]
    assert result.sel(bands="G").values.tolist() == [[40, 20 * MAX_RATIO]]
    assert result.sel(bands="B").values.tolist() == [[80, 40 * MAX_RATIO]]


def test_hrv_sharpening_ignores_invalid_hrv_and_waits_for_resampling():
    pytest.importorskip("satpy")
    from satpy.composites.core import IncompatibleAreas

    from ha_satimage.sources.hrv_composite import HrvLuminanceSharpenedRGB

    compositor = HrvLuminanceSharpenedRGB("natural_color_hrv")
    rgb = (_band([[10]], "IR_016"), _band([[20]], "VIS008"), _band([[40]], "VIS006"))
    result = compositor(rgb, optional_datasets=(_band([[np.nan]], "HRV"),))
    assert result.sel(bands="G").values.tolist() == [[20]]

    with pytest.raises(IncompatibleAreas):
        compositor(rgb, optional_datasets=(_band([[1, 2, 3]], "HRV"),))


def test_default_composite_is_defined_for_seviri():
    satpy = pytest.importorskip("satpy")
    from satpy.composites.config_loader import load_compositor_configs_for_sensors

    from ha_satimage.config import DEFAULT_COMPOSITE
    from ha_satimage.sources.satpy_render import SATPY_CONFIG_DIR

    with satpy.config.set(config_path=[str(SATPY_CONFIG_DIR)]):
        compositors, _ = load_compositor_configs_for_sensors(["seviri"])
    names = {key["name"] for key in compositors["seviri"]}
    assert {DEFAULT_COMPOSITE, "natural_color_hrv"} <= names


def test_source_window_ignores_points_with_only_one_valid_index():
    from ha_satimage.sources.satpy_render import (
        RenderRequest,
        SatpyRenderError,
        source_window,
        target_area,
    )

    class HalfValidArea:
        """Like the southern HRV window in the full disk: columns valid, rows not."""

        shape = (100, 100)

        def get_array_indices_from_lonlat(self, lons, lats):
            cols = np.ma.masked_array(np.full(lons.shape, 50), mask=False)
            rows = np.ma.masked_array(np.zeros(lons.shape, dtype=int), mask=True)
            return cols, rows

    request = RenderRequest("r", ("f",), "c", 48.2, 16.37, 300, 20, 20, "wien")
    with pytest.raises(SatpyRenderError, match="outside"):
        source_window(HalfValidArea(), target_area(request))


# -- Quellen-/Kompositwechsel ----------------------------------------------------
class OriginSource:
    """Returns a fixed sensing time per source (0° lags behind Rapid Scan)."""

    SENSING_BY_SOURCE = {"msg_seviri": SENSING, "msg_seviri_0deg": SENSING - timedelta(minutes=13)}

    def __init__(self):
        self.calls: list[tuple[str, str, datetime | None]] = []

    def render(self, region, config, last_sensing=None):
        sensing = self.SENSING_BY_SOURCE[region.source]
        self.calls.append((region.source, region.composite, last_sensing))
        if last_sensing is not None and sensing <= last_sensing:
            raise NoNewData("already rendered")
        return RenderedFrame(f"{region.source}/{region.composite}".encode(), sensing)


def _switch(store: ConfigStore, **changes) -> None:
    config = store.get()
    regions = [r.model_copy(update=changes) if r.name == "wien" else r for r in config.regions]
    store.update(config.model_copy(update={"regions": regions}))


def test_switching_source_or_composite_renders_even_older_product(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")
    buffers = BufferManager(tmp_path / "frames")
    scheduler = RenderScheduler(store, buffers, StatusStore())
    source = OriginSource()
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)
    buffer = lambda: buffers.get("wien", 10, 500)  # noqa: E731

    scheduler._run_region("wien")
    assert len(buffer()) == 1

    # Switch to 0°: the acquisition is older than the Rapid Scan frame, but is
    # still rendered and becomes the newest frame.
    _switch(store, source="msg_seviri_0deg")
    scheduler._run_region("wien")
    assert len(buffer()) == 2
    assert buffer().latest().source == "msg_seviri_0deg"
    assert buffer().get(0).path(buffer().region_dir).read_bytes().startswith(b"msg_seviri_0deg/")

    # Same source again: no duplicate frame.
    scheduler._run_region("wien")
    assert len(buffer()) == 2

    # Only change the composite: same acquisition, still a new image.
    _switch(store, composite="natural_color_hrv")
    scheduler._run_region("wien")
    assert len(buffer()) == 3
    assert buffer().latest().composite == "natural_color_hrv"

    # Back to Rapid Scan: an image right away again.
    _switch(store, source="msg_seviri")
    scheduler._run_region("wien")
    assert buffer().latest().source == "msg_seviri"
    assert len(buffer()) == 4

    # Turn off borders: same acquisition, still a new image.
    calls = len(source.calls)
    _switch(store, borders=False)
    scheduler._run_region("wien")
    assert source.calls[calls][2] is None
    assert buffer().latest().borders is False
    newest = buffer().latest().filename
    scheduler._run_region("wien")
    assert source.calls[calls + 1][2] is not None
    assert buffer().latest().filename == newest


def _job_ids(scheduler) -> list[str]:
    return [job.id for job in scheduler._scheduler.get_jobs()]


def test_reload_sets_up_download_jobs_and_renders_changed_regions(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    scheduler._scheduler.start(paused=True)
    try:
        scheduler.reload()
        ids = _job_ids(scheduler)
        # Rapid Scan is used by regions -> its own download job; the regions
        # only render after the download (no interval job).
        assert "download-msg_seviri" in ids
        assert "download-msg_seviri_0deg" not in ids  # deaktiviert
        assert not [i for i in ids if i.startswith("render-")]

        _switch(store, composite="natural_color_hrv")
        scheduler.reload()
        renders = [i for i in _job_ids(scheduler) if i.startswith("render-now-")]
        assert len(renders) == 1 and "-wien-" in renders[0]

        # FCI downloads into the archive even without a region; 0° without a region does not.
        config = store.get()
        for entry in config.sources.catalog:
            entry.enabled = entry.id in ("msg_seviri", "msg_seviri_0deg", "mtg_fci")
        store.update(config)
        scheduler.reload()
        ids = _job_ids(scheduler)
        assert "download-mtg_fci" in ids
        assert "download-msg_seviri_0deg" not in ids
    finally:
        scheduler._scheduler.shutdown(wait=False)


def test_reload_keeps_interval_of_unchanged_placeholder_regions(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    config = store.get()
    for entry in config.sources.catalog:
        entry.enabled = entry.id == "dummy"
    for region in config.regions:
        region.source = "dummy"
    store.update(config)
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    scheduler._scheduler.start(paused=True)
    try:
        scheduler.reload()
        assert not [i for i in _job_ids(scheduler) if i.startswith("download-")]
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        for name in ("wien", "mallorca"):
            scheduler._scheduler.get_job(f"render-{name}").modify(next_run_time=later)

        _switch(store, composite="natural_color_hrv")
        scheduler.reload()

        wien = scheduler._scheduler.get_job("render-wien").next_run_time
        mallorca = scheduler._scheduler.get_job("render-mallorca").next_run_time
        assert wien < later - timedelta(minutes=5)  # changed -> immediately
        assert mallorca == later  # unchanged -> cycle stays
    finally:
        scheduler._scheduler.shutdown(wait=False)


class FetchSource:
    """Download driver with a controllable newest acquisition."""

    def __init__(self, sensing: datetime):
        self.sensing = sensing
        self.error: Exception | None = None
        self.fetches = 0

    def fetch(self, entry, config):
        self.fetches += 1
        if self.error:
            raise self.error
        return self.sensing


def _close(actual: datetime, expected: datetime, tolerance: float = 5.0) -> bool:
    return abs((actual - expected).total_seconds()) < tolerance


def test_download_follows_cycle_and_triggers_renders_only_for_new_data(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")  # regions on Rapid Scan (5 min)
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), StatusStore())
    now = datetime.now(timezone.utc)
    source = FetchSource(now - timedelta(minutes=3))  # 3 min delivery delay
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)
    queued: list[str] = []
    monkeypatch.setattr(scheduler, "_queue_render", queued.append)

    assert scheduler.download("msg_seviri") is True
    assert sorted(queued) == ["europe", "mallorca", "wien"]
    state = scheduler._downloads["msg_seviri"]
    # Next download: sensing end + cycle + delay = now + 5 min.
    assert _close(state.due, source.sensing + timedelta(minutes=8))
    status = scheduler.download_status()["msg_seviri"]
    assert status["cycle_minutes"] == 5 and status["active"] is True
    assert status["latency_seconds"] == pytest.approx(180, abs=5)

    # Before it is due: the cycle job does not query the Data Store.
    scheduler._tick_download("msg_seviri")
    assert source.fetches == 1

    # Same acquisition -> no renders.
    queued.clear()
    assert scheduler.download("msg_seviri") is False
    assert queued == []

    # Expected acquisition overdue (not there yet): ask again every minute.
    state.last_sensing -= timedelta(minutes=6)
    source.sensing = state.last_sensing
    scheduler.download("msg_seviri")
    assert _close(state.due, datetime.now(timezone.utc) + timedelta(minutes=1))
    # More than one cycle overdue (data stream stalled): only once per cycle.
    state.last_sensing -= timedelta(minutes=10)
    source.sensing = state.last_sensing
    scheduler.download("msg_seviri")
    assert _close(state.due, datetime.now(timezone.utc) + timedelta(minutes=5))

    # New acquisition: regions are rendered.
    source.sensing = now
    assert scheduler.download("msg_seviri") is True
    assert sorted(queued) == ["europe", "mallorca", "wien"]

    # Error: no crash, retry after 5 min at the latest.
    source.error = RenderError("401 Unauthorized")
    assert scheduler.download("msg_seviri") is False
    assert state.last_error == "401 Unauthorized"
    assert _close(state.due, datetime.now(timezone.utc) + timedelta(minutes=5))
    assert scheduler.download_status()["msg_seviri"]["last_error"] == "401 Unauthorized"

    # Disabled source: no download.
    assert scheduler.download("msg_seviri_0deg") is False


def test_render_subprocess_retries_once_after_crash(monkeypatch):
    import signal

    from ha_satimage.sources import satpy_render

    calls = []

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise satpy_render.RenderProcessCrashed("Render process killed by SIGSEGV")
        return b"png", "t"

    monkeypatch.setattr(satpy_render, "_run_once", flaky)
    assert satpy_render.render_in_subprocess(object()) == (b"png", "t")
    assert len(calls) == 2

    def always_crash(request, timeout):
        raise satpy_render.RenderProcessCrashed("broken")

    monkeypatch.setattr(satpy_render, "_run_once", always_crash)
    with pytest.raises(satpy_render.SatpyRenderError, match="broken"):
        satpy_render.render_in_subprocess(object())

    def timeout(request, timeout):
        calls.append(1)
        raise satpy_render.SatpyRenderError("time limit")

    calls.clear()
    monkeypatch.setattr(satpy_render, "_run_once", timeout)
    with pytest.raises(satpy_render.SatpyRenderError, match="time limit"):
        satpy_render.render_in_subprocess(object())
    assert len(calls) == 1  # no retry on timeout

    assert "SIGKILL" in satpy_render._describe_exit(-signal.SIGKILL)
    assert "memory limit" in satpy_render._describe_exit(-signal.SIGKILL)
    assert "SIGSEGV" in satpy_render._describe_exit(-signal.SIGSEGV)
    assert "exit code 1" in satpy_render._describe_exit(1)
