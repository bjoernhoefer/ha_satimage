"""Country and administrative borders as an overlay on a rendered region image.

Country and administrative borders and coastlines worldwide. The lines come from Natural
Earth 1:10m (public domain, generated with
``scripts/build_borders.py``) and are shipped packed in the package. They
are projected into the same Lambert azimuthal projection as the target
region (``satpy_render.target_area``), so lines and image match exactly -
without pycoast and its ~150 MB GSHHS dataset.
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

BORDERS_FILE = Path(__file__).resolve().parent / "overlay_data" / "borders_10m.json.gz"
OVERLAY_VERSION = "coastlines-v1"
# Anti-aliased hairlines: drawn at SUPERSAMPLE times the size and
# downscaled. Width in target pixels = line width / SUPERSAMPLE.
SUPERSAMPLE = 4
BORDER_COLOR = (0, 0, 0, 255)
BORDER_WIDTH = 4  # ~1 px
STATE_COLOR = (0, 0, 0, 170)
STATE_WIDTH = 3  # ~0.75 px
# Points farther from the center than this multiple of the radius are
# dropped (the line is split there) - keeps pixel values small.
_CLIP_FACTOR = 1.5


@lru_cache(maxsize=1)
def _border_data() -> dict:
    return json.loads(gzip.decompress(BORDERS_FILE.read_bytes()))


@lru_cache(maxsize=3)
def _border_lines(kind: str = "lines"):
    import numpy as np

    lines = [np.asarray(line, dtype=float) for line in _border_data().get(kind, [])]
    if not lines:
        return [], np.empty((0, 4))
    boxes = np.array([(l[:, 0].min(), l[:, 1].min(), l[:, 0].max(), l[:, 1].max()) for l in lines])
    return lines, boxes


def border_segments(
    lat: float, lon: float, radius_km: float, width: int, height: int, kind: str = "lines"
):
    """Border lines as pixel polylines [(x, y), ...] in the target image.

    ``kind``: ``"lines"`` = country borders, ``"state_lines"`` = states,
    ``"coast_lines"`` = coastlines (including islands).
    """
    import numpy as np
    from pyproj import Transformer

    from ha_satimage.geometry import bounding_box

    lines, boxes = _border_lines(kind)
    if not lines:
        return []
    box = bounding_box(lat, min(max(lon, -180.0), 180.0), radius_km * _CLIP_FACTOR)
    hit = (
        (boxes[:, 2] >= box.lon_min) & (boxes[:, 0] <= box.lon_max)
        & (boxes[:, 3] >= box.lat_min) & (boxes[:, 1] <= box.lat_max)
    )
    if not hit.any():
        return []
    transformer = Transformer.from_crs(
        "EPSG:4326",
        {"proj": "laea", "lat_0": lat, "lon_0": lon, "ellps": "WGS84"},
        always_xy=True,
    )
    radius_m = radius_km * 1000
    limit = radius_m * _CLIP_FACTOR
    segments = []
    for index in np.flatnonzero(hit):
        line = lines[index]
        x, y = transformer.transform(line[:, 0], line[:, 1])
        inside = np.isfinite(x) & np.isfinite(y) & (np.abs(x) <= limit) & (np.abs(y) <= limit)
        cols = (x + radius_m) / (2 * radius_m) * width
        rows = (radius_m - y) / (2 * radius_m) * height
        # Contiguous segments inside the clip area.
        start = None
        for i, ok in enumerate(np.append(inside, False)):
            if ok and start is None:
                start = i
            elif not ok and start is not None:
                if i - start >= 2:
                    segments.append(list(zip(cols[start:i].tolist(), rows[start:i].tolist())))
                start = None
    return segments


def border_segments_lonlat(extent, width: int, height: int, kind: str = "lines"):
    """Border lines as pixel polylines on a lon/lat (plate carree) map.

    ``extent`` = (lon_min, lat_min, lon_max, lat_max) of the image edges.
    Used by the location map of Europe (``location_change.py``).
    """
    import numpy as np

    lon_min, lat_min, lon_max, lat_max = extent
    lines, boxes = _border_lines(kind)
    if not lines:
        return []
    hit = (
        (boxes[:, 2] >= lon_min) & (boxes[:, 0] <= lon_max)
        & (boxes[:, 3] >= lat_min) & (boxes[:, 1] <= lat_max)
    )
    x_scale = width / (lon_max - lon_min)
    y_scale = height / (lat_max - lat_min)
    return [
        list(zip(((lines[i][:, 0] - lon_min) * x_scale).tolist(),
                 ((lat_max - lines[i][:, 1]) * y_scale).tolist()))
        for i in np.flatnonzero(hit)
    ]


def _draw_layers(image, layers, scale: int = SUPERSAMPLE):
    """Draws (segments, color, width) layers as anti-aliased hairlines.

    Line widths are given for ``SUPERSAMPLE``; a smaller ``scale`` (less
    memory for large images) keeps the same width in target pixels.
    """
    from PIL import Image, ImageDraw

    if not any(segments for segments, _, _ in layers):
        return image
    width, height = image.size
    overlay = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for segments, color, line_width in layers:
        line_width = max(1, round(line_width * scale / SUPERSAMPLE))
        for segment in segments:
            draw.line([(x * scale, y * scale) for x, y in segment], fill=color,
                      width=line_width, joint="curve")
    overlay = overlay.resize((width, height), Image.Resampling.BOX)
    image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))
    return image


def draw_borders(image, lat: float, lon: float, radius_km: float):
    """Draw state/country borders and coastlines as anti-aliased hairlines."""
    width, height = image.size
    return _draw_layers(image, [
        (border_segments(lat, lon, radius_km, width, height, "state_lines"), STATE_COLOR, STATE_WIDTH),
        (border_segments(lat, lon, radius_km, width, height, "lines"), BORDER_COLOR, BORDER_WIDTH),
        (border_segments(lat, lon, radius_km, width, height, "coast_lines"), BORDER_COLOR, BORDER_WIDTH),
    ])


def draw_coastlines(image, lat: float, lon: float, radius_km: float):
    """Add only the new layer to archived images that already have borders."""
    return _draw_layers(image, [
        (border_segments(lat, lon, radius_km, *image.size, "coast_lines"), BORDER_COLOR, BORDER_WIDTH),
    ])


def draw_borders_lonlat(image, extent, scale: int = 2):
    """Same borders (style as ``draw_borders``) on a lon/lat map image.

    ``scale`` 2 instead of 4: the Europe map is large, 4x supersampling
    would need ~100 MB for the overlay alone.
    """
    width, height = image.size
    return _draw_layers(image, [
        (border_segments_lonlat(extent, width, height, "state_lines"), STATE_COLOR, STATE_WIDTH),
        (border_segments_lonlat(extent, width, height, "lines"), BORDER_COLOR, BORDER_WIDTH),
        (border_segments_lonlat(extent, width, height, "coast_lines"), BORDER_COLOR, BORDER_WIDTH),
    ], scale)


def draw_coastlines_lonlat(image, extent):
    return _draw_layers(image, [
        (border_segments_lonlat(extent, *image.size, "coast_lines"), BORDER_COLOR, BORDER_WIDTH),
    ], scale=2)


__all__ = ["border_segments", "border_segments_lonlat", "draw_borders", "draw_borders_lonlat"]
