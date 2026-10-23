"""Feature "location_change": map of Europe, location limits, map rendering."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pytest
import yaml
from PIL import Image

from ha_satimage import location_change as lc
from ha_satimage.config import AppConfig, default_config
from ha_satimage.overlay import border_segments_lonlat, draw_borders_lonlat
from ha_satimage.sources import RenderError

VIENNA = (48.2082, 16.3738)
BERLIN = (52.52, 13.405)


# -- Map geometry ------------------------------------------------------------------
def test_map_covers_europe_with_sensible_aspect():
    lon_min, lat_min, lon_max, lat_max = lc.EUROPE_EXTENT
    assert lon_min <= -24 and lon_max >= 40 and lat_min <= 35 and lat_max >= 71
    # ~1 km per km in both directions around the middle latitude.
    km_x = (lon_max - lon_min) * 111.32 * np.cos(np.radians((lat_min + lat_max) / 2)) / lc.MAP_WIDTH
    km_y = (lat_max - lat_min) * 111.32 / lc.MAP_HEIGHT
    assert abs(km_x / km_y - 1) < 0.01


@pytest.mark.parametrize(("lat", "lon"), [VIENNA, BERLIN, (40.0, -20.0), (70.0, 40.0)])
def test_pixel_and_lonlat_round_trip(lat, lon):
    x, y = lc.lonlat_to_pixel(lat, lon)
    assert 0 <= x <= lc.MAP_WIDTH and 0 <= y <= lc.MAP_HEIGHT
    back = lc.pixel_to_lonlat(x, y)
    assert back == pytest.approx((lat, lon))


def test_map_corners():
    assert lc.pixel_to_lonlat(0, 0) == pytest.approx((lc.EUROPE_EXTENT[3], lc.EUROPE_EXTENT[0]))
    assert lc.pixel_to_lonlat(lc.MAP_WIDTH, lc.MAP_HEIGHT) == pytest.approx(
        (lc.EUROPE_EXTENT[1], lc.EUROPE_EXTENT[2])
    )


# -- Outline map (land, sea, borders) ---------------------------------------------
@pytest.fixture(scope="module")
def outline() -> Image.Image:
    image = Image.open(BytesIO(lc.outline_png()))
    image.load()
    return image.convert("RGB")


def _pixel(image, lat, lon):
    x, y = lc.lonlat_to_pixel(lat, lon)
    return image.getpixel((int(x), int(y)))


def test_outline_map_has_map_size(outline):
    assert outline.size == (lc.MAP_WIDTH, lc.MAP_HEIGHT)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(47.5, 14.5), (52.3, 10.6), (40.3, -3.7), (46.5, 2.5), (59.5, 15.0), (52.3, -1.5)],
)
def test_outline_map_draws_land(outline, lat, lon):
    assert _pixel(outline, lat, lon) == lc.LAND_COLOR


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(45.0, -15.0), (39.5, 5.0), (43.0, 34.0), (56.0, 3.0), (42.5, 15.5)],  # Atlantic, Med, Black Sea, North Sea, Adriatic
)
def test_outline_map_draws_sea(outline, lat, lon):
    assert _pixel(outline, lat, lon) == lc.SEA_COLOR


def test_outline_map_has_country_borders_in_overlay_style(outline):
    # AT/SK border at ~17° E, ~48.1° N (east of Vienna): dark hairline nearby.
    x, y = lc.lonlat_to_pixel(48.1, 16.95)
    window = [outline.getpixel((int(x) + dx, int(y) + dy)) for dx in range(-8, 9) for dy in range(-8, 9)]
    assert any(sum(p) < 200 for p in window)
    # The Austrian states are drawn as well (Vienna / Lower Austria).
    x, y = lc.lonlat_to_pixel(*VIENNA)
    window = [outline.getpixel((int(x) + dx, int(y) + dy)) for dx in range(-6, 7) for dy in range(-6, 7)]
    assert any(sum(p) < 600 for p in window)
    # Open sea: no lines.
    x, y = lc.lonlat_to_pixel(45.0, -15.0)
    window = [outline.getpixel((int(x) + dx, int(y) + dy)) for dx in range(-8, 9) for dy in range(-8, 9)]
    assert all(p == lc.SEA_COLOR for p in window)


def test_border_segments_lonlat_are_linear_in_lon_and_lat():
    extent = (10.0, 45.0, 20.0, 50.0)
    segments = border_segments_lonlat(extent, 1000, 500)
    points = [p for s in segments for p in s]
    assert points
    # AT/SK/HU tripoint ~ (17.16 E, 48.01 N) -> x ~ 716, y ~ 199.
    assert any(abs(x - 716) < 6 and abs(y - 199) < 6 for x, y in points)


def test_draw_borders_lonlat_draws_thin_black_lines():
    background = (120, 200, 80)
    image = Image.new("RGB", (700, 390), background)
    draw_borders_lonlat(image, lc.EUROPE_EXTENT)
    pixels = list(image.get_flattened_data()) if hasattr(image, "get_flattened_data") else list(image.getdata())
    changed = [p for p in pixels if p != background]
    assert changed and all(p[0] <= 120 and p[1] <= 200 and p[2] <= 80 for p in changed)
    # Coastlines add many hairlines around Europe's islands and fjords.
    assert len(changed) < 0.25 * len(pixels)


# -- Satellite map -----------------------------------------------------------------
class _Product:
    def __init__(self, path: Path):
        self.path = path
        self.sensing_end = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class _Cache:
    def __init__(self, product):
        self.product = product
        self.downloads = 0

    def cached(self, collection):
        return None

    def latest(self, collection, credentials, suffix):
        self.downloads += 1
        return self.product


def _png(size=(lc.MAP_WIDTH, lc.MAP_HEIGHT)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (30, 60, 90)).save(out, format="PNG")
    return out.getvalue()


def test_satellite_map_renders_lonlat_europe_from_latest_product(tmp_path, monkeypatch):
    cache = _Cache(_Product(tmp_path / "product.nat"))
    monkeypatch.setattr("ha_satimage.sources._product_cache", lambda: cache)
    requests = []

    def fake_render(request, *args, **kwargs):
        requests.append(request)
        return _png(), datetime(2026, 10, 1, 12, 0)

    monkeypatch.setattr("ha_satimage.sources.satpy_render.render_in_subprocess", fake_render)
    config = default_config()
    satellite = lc.SatelliteMap(tmp_path / "map")
    assert satellite.info()["available"] is False

    info = satellite.render(config)

    assert cache.downloads == 1  # downloaded on demand
    [request] = requests
    assert request.lonlat_extent == lc.EUROPE_EXTENT
    assert (request.width, request.height) == (lc.MAP_WIDTH, lc.MAP_HEIGHT)
    assert request.borders is True
    assert request.reader == "seviri_l1b_native"
    assert request.composite == lc.SATELLITE_COMPOSITE
    assert request.filenames == (str(tmp_path / "product.nat"),)
    assert info["available"] is True
    assert info["source"] == "msg_seviri"
    assert info["sensing_time"].startswith("2026-10-01T12:00:00")
    assert info["url"].startswith("/location-map/satellite.jpg")
    stored = Image.open(satellite.image_path)
    assert stored.format == "JPEG" and stored.size == (lc.MAP_WIDTH, lc.MAP_HEIGHT)


def test_satellite_map_error_is_reported(tmp_path, monkeypatch):
    from ha_satimage.sources.eumetsat import DataStoreError

    class FailingCache(_Cache):
        def latest(self, *args):
            raise DataStoreError("no credentials")

    monkeypatch.setattr("ha_satimage.sources._product_cache", lambda: FailingCache(None))
    satellite = lc.SatelliteMap(tmp_path / "map")
    with pytest.raises(RenderError, match="no credentials"):
        satellite.render(default_config())
    assert satellite.info() == {**satellite.info(), "available": False, "error": "no credentials"}


def test_map_source_prefers_enabled_msg_seviri_in_use():
    config = default_config()
    assert lc.map_source(config).id == "msg_seviri"
    data = config.model_dump()
    for entry in data["sources"]["catalog"]:
        entry["enabled"] = entry["id"] == "msg_seviri_0deg"
    assert lc.map_source(AppConfig(**data)).id == "msg_seviri_0deg"
    data["sources"]["catalog"] = [e for e in data["sources"]["catalog"] if e["driver"] != "msg_seviri"]
    data["regions"] = []
    assert lc.map_source(AppConfig(**data)) is None


def test_lonlat_target_area_matches_map_geometry():
    pytest.importorskip("pyresample")
    from ha_satimage.sources.satpy_render import RenderRequest, target_area

    request = RenderRequest("r", ("f",), "c", 0, 0, 1, lc.MAP_WIDTH, lc.MAP_HEIGHT, "Europe",
                            lonlat_extent=lc.EUROPE_EXTENT)
    area = target_area(request)
    assert area.shape == (lc.MAP_HEIGHT, lc.MAP_WIDTH)
    for lat, lon in (VIENNA, BERLIN, (40.0, -20.0)):
        col, row = area.get_array_indices_from_lonlat(np.array([lon]), np.array([lat]))
        x, y = lc.lonlat_to_pixel(lat, lon)
        assert abs(int(np.ravel(col)[0]) - x) <= 1 and abs(int(np.ravel(row)[0]) - y) <= 1


def test_rapid_scan_data_resamples_onto_the_whole_europe_map():
    """Real resampling: a synthetic Rapid Scan field (value = latitude) ends up
    at the right place of the map and covers it completely."""
    pytest.importorskip("pyresample")
    from pyresample.geometry import AreaDefinition
    from pyresample.kd_tree import resample_nearest

    from ha_satimage.sources.satpy_render import RenderRequest, source_window, target_area

    rss = AreaDefinition(
        "rss", "rss", "rss",
        "+proj=geos +lon_0=9.5 +h=35785831 +x_0=0 +y_0=0 +a=6378169 +rf=295.488065897014 +units=m +no_defs",
        3712, 1392, (5567248.0, 5570248.5, -5570248.5, 1393687.2),
    )
    width, height = lc.MAP_WIDTH // 7, lc.MAP_HEIGHT // 7
    area = target_area(RenderRequest("r", ("f",), "c", 0, 0, 1, width, height, "Europe",
                                     lonlat_extent=lc.EUROPE_EXTENT))
    rows, cols = source_window(rss, area)
    window = rss[rows, cols]
    _, lats = window.get_lonlats()
    result = resample_nearest(window, np.nan_to_num(lats, nan=-999.0), area,
                              radius_of_influence=20000, fill_value=np.nan)
    assert np.isfinite(result).mean() > 0.99
    x, y = lc.lonlat_to_pixel(*VIENNA)
    assert result[int(y / 7), int(x / 7)] == pytest.approx(VIENNA[0], abs=0.3)


# -- Locations (limit, changes) ----------------------------------------------------
def test_add_location_uses_defaults_and_validates():
    config = default_config()
    added = lc.add_location(config, {"name": "berlin", "lat": BERLIN[0], "lon": BERLIN[1]})
    region = added.region("berlin")
    assert (region.lat, region.lon, region.radius_km) == (52.52, 13.405, lc.DEFAULT_RADIUS_KM)
    assert region.source == config.regions[0].source and region.borders is True

    for bad in (
        {"name": "wien", "lat": 1, "lon": 1},          # exists
        {"name": "_x", "lat": 1, "lon": 1},            # reserved
        {"name": "a/b", "lat": 1, "lon": 1},
        {"name": "ok", "lat": 91, "lon": 1},
        {"name": "ok", "lat": 1, "lon": 181},
        {"name": "ok", "lat": "x", "lon": 1},
        {"name": "ok", "lat": 1},
        {"name": "ok", "lat": 1, "lon": 1, "radius_km": 5},
        {"name": "ok", "lat": 1, "lon": 1, "radius_km": 5000},
    ):
        with pytest.raises(lc.LocationError):
            lc.add_location(config, bad)


def test_at_most_four_locations():
    config = default_config()
    for name in ("a",):
        config = lc.add_location(config, {"name": name, "lat": 50, "lon": 10})
    assert len(config.regions) == lc.MAX_LOCATIONS == 4
    with pytest.raises(lc.LocationError, match="At most 4"):
        lc.add_location(config, {"name": "c", "lat": 50, "lon": 10})

    five = config.model_copy(update={"regions": [*config.regions, config.regions[0].model_copy(update={"name": "e"})]})
    with pytest.raises(lc.LocationError):
        lc.check_locations(config, five)
    # Existing configurations with more locations stay loadable/saveable.
    lc.check_locations(five, five)
    dup = config.model_copy(update={"regions": [*config.regions[:3], config.regions[0]]})
    with pytest.raises(lc.LocationError, match="more than once"):
        lc.check_locations(config, dup)


def test_move_and_remove_location_and_reset_detection():
    config = default_config()
    moved = lc.move_location(config, "wien", {"lat": 47.07, "lon": 15.44, "radius_km": 150})
    assert (moved.region("wien").lat, moved.region("wien").radius_km) == (47.07, 150.0)
    assert moved.region("mallorca") == config.region("mallorca")
    assert lc.reset_regions(config, moved) == ["wien"]
    # Only the radius is kept if not given.
    only_center = lc.move_location(config, "wien", {"lat": 47.07, "lon": 15.44})
    assert only_center.region("wien").radius_km == config.region("wien").radius_km
    # Composite/source changes keep the history.
    recolored = config.model_copy(update={"regions": [
        r.model_copy(update={"composite": "airmass"}) for r in config.regions
    ]})
    assert lc.reset_regions(config, recolored) == []
    removed = lc.remove_location(config, "mallorca")
    assert [r.name for r in removed.regions] == ["wien", "europe"]
    assert lc.reset_regions(config, removed) == ["mallorca"]
    with pytest.raises(KeyError):
        lc.move_location(config, "nowhere", {"lat": 1, "lon": 1})
    with pytest.raises(KeyError):
        lc.remove_location(config, "nowhere")


def test_moving_overview_converts_to_local_crop_and_resets_history():
    config = default_config()
    moved = lc.move_location(config, "europe", {"lat": 52.5, "lon": 10, "radius_km": 2000})
    assert moved.region("europe").lonlat_extent is None
    assert lc.reset_regions(config, moved) == ["europe"]


# -- API ---------------------------------------------------------------------------
def _stored(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))


def test_location_map_api(live_server):
    info = httpx.get(f"{live_server.url}/api/location-map").json()
    assert info["max_locations"] == 4
    assert (info["width"], info["height"]) == (lc.MAP_WIDTH, lc.MAP_HEIGHT)
    assert info["extent"]["lon_min"] == lc.EUROPE_EXTENT[0]
    assert {loc["name"] for loc in info["locations"]} == {"wien", "mallorca", "europe"}
    assert next(loc for loc in info["locations"] if loc["name"] == "europe")["lonlat_extent"] == list(lc.EUROPE_EXTENT)
    assert info["satellite"]["available"] is False
    outline = httpx.get(f"{live_server.url}{info['outline_url']}")
    assert outline.status_code == 200 and outline.headers["content-type"] == "image/png"
    assert Image.open(BytesIO(outline.content)).size == (lc.MAP_WIDTH, lc.MAP_HEIGHT)
    assert httpx.get(f"{live_server.url}/location-map/satellite.jpg").status_code == 404
    # No credentials in the test: the on-demand render fails cleanly.
    response = httpx.post(f"{live_server.url}/api/location-map/satellite", timeout=30)
    assert response.status_code == 502
    assert httpx.get(f"{live_server.url}/api/location-map").json()["satellite"]["error"]


def test_add_move_remove_locations_via_api(live_server):
    url = live_server.url
    added = httpx.post(f"{url}/api/regions", json={"name": "berlin", "lat": 52.52, "lon": 13.405, "radius_km": 200})
    assert added.status_code == 200, added.text
    assert added.json()["source"] == "dummy"
    assert [r["name"] for r in _stored(live_server)["regions"]] == ["wien", "mallorca", "europe", "berlin"]
    live_server.ensure_frames("berlin", 1)

    assert httpx.post(f"{url}/api/regions", json={"name": "berlin", "lat": 1, "lon": 1}).status_code == 400
    assert httpx.post(f"{url}/api/regions", json={"name": "graz", "lat": 47.07, "lon": 15.44}).status_code == 400
    fifth = httpx.post(f"{url}/api/regions", json={"name": "rome", "lat": 41.9, "lon": 12.5})
    assert fifth.status_code == 400 and "At most 4" in fifth.json()["detail"]
    # The limit also applies to the general configuration endpoint.
    regions = _stored(live_server)["regions"]
    extra = {**regions[0], "name": "rome"}
    assert httpx.post(f"{url}/api/config", json={"regions": [*regions, extra]}).status_code == 400

    live_server.ensure_frames("wien", 2)
    moved = httpx.put(f"{url}/api/regions/wien", json={"lat": 47.8, "lon": 13.04, "radius_km": 120})
    assert moved.status_code == 200
    wien = next(r for r in _stored(live_server)["regions"] if r["name"] == "wien")
    assert (wien["lat"], wien["lon"], wien["radius_km"]) == (47.8, 13.04, 120.0)
    # History of the old cut-out is gone; the new one renders right away.
    frames = live_server.ensure_frames("wien", 1)
    assert len(frames) <= 2
    assert httpx.put(f"{url}/api/regions/nowhere", json={"lat": 1, "lon": 1}).status_code == 404
    assert httpx.put(f"{url}/api/regions/wien", json={"lat": 100, "lon": 1}).status_code == 400

    assert httpx.delete(f"{url}/api/regions/berlin").status_code == 200
    assert "berlin" not in [r["name"] for r in _stored(live_server)["regions"]]
    assert httpx.get(f"{url}/api/regions/berlin/frames").status_code == 404
    assert httpx.delete(f"{url}/api/regions/berlin").status_code == 404
    logs = httpx.get(f"{url}/api/logs?format=text").text
    assert "Location berlin added" in logs and "Location berlin removed" in logs


def test_moving_a_location_drops_its_frames_on_disk(live_server):
    live_server.ensure_frames("mallorca", 2)
    region_dir = live_server.data_dir / "frames" / "mallorca"
    old = {p.name for p in region_dir.glob("*.png")}
    assert old
    httpx.put(f"{live_server.url}/api/regions/mallorca", json={"lat": 39.0, "lon": 1.4})
    new_frames = live_server.ensure_frames("mallorca", 1)
    assert not {f["filename"] for f in new_frames} & old
    index = json.loads((region_dir / "_index.json").read_text())
    assert index
