from importlib.resources import files

import pytest
from PIL import Image

from ha_satimage.config import RegionConfig
from ha_satimage.overlay import border_segments, border_segments_lonlat, draw_borders, draw_borders_lonlat

VIENNA = (48.21, 16.37, 250)


def test_border_data_is_packaged():
    assert files("ha_satimage").joinpath("overlay_data/borders_10m.json.gz").is_file()


def test_vienna_has_borders_near_bratislava():
    segments = border_segments(*VIENNA, 800, 800)
    assert segments
    points = [p for segment in segments for p in segment]
    # The AT/SK border is ~50 km east of Vienna (radius 250 km -> 1.6 px/km):
    # just right of the image center, at mid height.
    assert any(460 < x < 500 and 380 < y < 420 for x, y in points)
    # No outliers far outside the image.
    assert all(-800 <= x <= 1600 and -800 <= y <= 1600 for x, y in points)


@pytest.mark.parametrize("lat,lon,radius", [
    (39.6, 2.9, 100),  # Mallorca
    (38.98, 1.43, 40),  # Ibiza
    (39.98, 4.07, 40),  # Menorca
])
def test_islands_have_coastlines_on_region_and_location_maps(lat, lon, radius):
    assert border_segments(lat, lon, radius, 800, 800, kind="coast_lines")
    assert border_segments_lonlat(
        (lon - 0.5, lat - 0.5, lon + 0.5, lat + 0.5),
        800, 800, kind="coast_lines",
    )
    image = Image.new("RGB", (800, 800), "white")
    draw_borders(image, lat, lon, radius)
    assert image.getextrema()[0][0] < 100


def test_austrian_states_near_vienna():
    points = [p for s in border_segments(*VIENNA, 800, 800, kind="state_lines") for p in s]
    # Vienna is its own state: border with Lower Austria around the image center.
    assert any(abs(x - 400) < 15 and abs(y - 400) < 15 for x, y in points)


def test_administrative_borders_are_available_worldwide():
    # California has internal state/county borders, despite not being in Europe.
    assert border_segments(37.25, -119.5, 400, 800, 800, kind="state_lines")


def test_draw_borders_draws_dark_lines():
    background = (120, 200, 80)
    image = Image.new("RGB", (800, 800), background)
    draw_borders(image, *VIENNA)
    assert image.mode == "RGB"
    pixels = list(image.getdata())
    changed = [p for p in pixels if p != background]
    # Black hairlines: only darkened, never lighter or colored.
    assert changed and all(p[0] <= 120 and p[1] <= 200 and p[2] <= 80 for p in changed)
    assert any(sum(p) < 100 for p in changed)
    assert len(changed) < 0.10 * len(pixels)


@pytest.mark.parametrize("lonlat", [False, True])
@pytest.mark.parametrize("country,regional", [(False, False), (True, False), (False, True), (True, True)])
def test_selected_layers_and_thicker_strokes(monkeypatch, lonlat, country, regional):
    positions = {"lines": 20, "state_lines": 50, "coast_lines": 80}

    def segments(*args):
        x = positions[args[-1]]
        return [[(x, 0), (x, 100)]]

    monkeypatch.setattr("ha_satimage.overlay.border_segments", segments)
    monkeypatch.setattr("ha_satimage.overlay.border_segments_lonlat", segments)
    image = Image.new("RGB", (100, 100), "white")
    if lonlat:
        draw_borders_lonlat(image, (-25, 33, 45, 72), scale=4, country=country, regional=regional)
    else:
        draw_borders(image, *VIENNA, country=country, regional=regional)
    # Integrated darkness measures stroke width even with anti-aliasing.
    for kind, x in positions.items():
        darkness = sum(255 - image.getpixel((col, 50))[0] for col in range(x - 3, x + 4))
        enabled = regional if kind == "state_lines" else country
        expected = 1.75 * 170 if kind == "state_lines" else 2 * 255
        assert darkness == pytest.approx(expected if enabled else 0, abs=2)


def test_region_draws_borders_by_default():
    assert RegionConfig(name="x", lat=48.2, lon=16.4, radius_km=100).borders is True


def test_archive_signature_depends_on_borders():
    from ha_satimage.archive import region_signature

    region = RegionConfig(name="wien", lat=48.2, lon=16.4, radius_km=100)
    without = region.model_copy(update={"borders": False})
    assert region_signature(region) != region_signature(without)
