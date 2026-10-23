"""Geometry helpers for regions.

Computes a simple equidistant bounding box in degrees from a center point
(lat/lon) and a radius (km). The approximation uses the classic
"degrees per kilometer" conversion (1° latitude ≈ 111.32 km) and corrects
the longitude extent with the cosine of the latitude. For this project's
purpose (cropping satellite images to a few hundred kilometers) this
spherical approximation is accurate enough and needs no extra
dependencies (e.g. pyproj).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

KM_PER_DEGREE_LAT = 111.32
EUROPE_EXTENT = (-25.0, 33.0, 45.0, 72.0)
# Smallest value below which cos(lat) is no longer used as a divisor
# (avoids division by (nearly) zero at the poles).
_MIN_COS_LAT = 1e-6


@dataclass(frozen=True)
class BoundingBox:
    """Bounding box in degrees, WGS84."""

    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.lat_min, self.lon_min, self.lat_max, self.lon_max)


def bounding_box(lat: float, lon: float, radius_km: float) -> BoundingBox:
    """Compute a bounding box around (lat, lon) with the given radius in km.

    Args:
        lat: Latitude of the center in degrees (-90..90).
        lon: Longitude of the center in degrees (-180..180).
        radius_km: Radius in kilometers (must be > 0).

    Returns:
        BoundingBox with lat_min/lon_min/lat_max/lon_max in degrees.
    """
    if radius_km <= 0:
        raise ValueError("radius_km must be greater than 0")
    if not -90.0 <= lat <= 90.0:
        raise ValueError("lat must be between -90 and 90")
    if not -180.0 <= lon <= 180.0:
        raise ValueError("lon must be between -180 and 180")

    delta_lat = radius_km / KM_PER_DEGREE_LAT
    cos_lat = max(abs(math.cos(math.radians(lat))), _MIN_COS_LAT)
    delta_lon = radius_km / (KM_PER_DEGREE_LAT * cos_lat)

    lat_min = max(lat - delta_lat, -90.0)
    lat_max = min(lat + delta_lat, 90.0)
    lon_min = lon - delta_lon
    lon_max = lon + delta_lon

    return BoundingBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)
