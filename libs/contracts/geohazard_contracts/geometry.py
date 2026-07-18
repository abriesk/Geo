"""AOI geometry rules (§6.1) and cache-key hashing (§6.2).

§6.1: AOI is a GeoJSON Polygon in EPSG:4326, lon-lat order,
right-hand-rule winding (RFC 7946: exterior ring counter-clockwise).
Backend normalizes winding and rejects self-intersections.

§6.2: aoi_hash = sha256(canonical_geojson) where canonical form =
EPSG:4326, coordinates rounded to 4 decimal places (~11 m),
exterior ring only, right-hand winding, first point deduplicated
(i.e. the closing point equal to the first point is dropped).
"""
from __future__ import annotations

import hashlib
import json
from typing import List, Sequence, Tuple

from shapely.geometry import Polygon as ShapelyPolygon

Coord = Tuple[float, float]

WGS84_LON_RANGE = (-180.0, 180.0)
WGS84_LAT_RANGE = (-90.0, 90.0)
COORD_DECIMALS = 4  # §6.2, ~11 m


class AoiValidationError(ValueError):
    """Raised when an AOI polygon violates §6.1 rules."""


def _signed_area(ring: Sequence[Coord]) -> float:
    """Shoelace signed area. Positive = counter-clockwise (RFC 7946 RHR)."""
    area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _open_ring(ring: Sequence[Sequence[float]]) -> List[Coord]:
    """Return ring as list of (lon, lat) with the closing point removed."""
    coords = [(float(c[0]), float(c[1])) for c in ring]
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return coords


def validate_exterior_ring(ring: Sequence[Sequence[float]]) -> List[Coord]:
    """Validate one exterior ring per §6.1. Returns the open ring, CCW-normalized.

    Raises AoiValidationError on: too few points, out-of-range lon/lat,
    zero-area/degenerate geometry, self-intersection.
    """
    coords = _open_ring(ring)
    if len(coords) < 3:
        raise AoiValidationError("AOI exterior ring needs at least 3 distinct points")
    for lon, lat in coords:
        if not (WGS84_LON_RANGE[0] <= lon <= WGS84_LON_RANGE[1]):
            raise AoiValidationError(f"longitude {lon} outside EPSG:4326 range")
        if not (WGS84_LAT_RANGE[0] <= lat <= WGS84_LAT_RANGE[1]):
            raise AoiValidationError(f"latitude {lat} outside EPSG:4326 range")

    poly = ShapelyPolygon(coords)
    if not poly.is_valid:
        raise AoiValidationError("AOI polygon is invalid (self-intersecting or degenerate)")
    if poly.area == 0.0:
        raise AoiValidationError("AOI polygon has zero area")

    # Normalize winding to RFC 7946 right-hand rule (exterior CCW).
    if _signed_area(coords) < 0:
        coords = list(reversed(coords))
    return coords


def canonical_aoi_geojson(ring: Sequence[Sequence[float]]) -> dict:
    """Canonical form per §6.2 (rounded, open, CCW, exterior only).

    Amendment to §6.2 (discovered during M0, per §11.3 rule 4): the canonical
    ring additionally starts at the lexicographically smallest (lon, lat)
    vertex. Without this, the same polygon drawn CW vs CCW canonicalizes to
    the same cycle but a different start vertex, producing different hashes
    and defeating the cache key.
    """
    coords = validate_exterior_ring(ring)
    rounded = [(round(lon, COORD_DECIMALS), round(lat, COORD_DECIMALS)) for lon, lat in coords]
    # Rounding can collapse the closing point back onto the first point.
    if len(rounded) >= 2 and rounded[0] == rounded[-1]:
        rounded = rounded[:-1]
    # Rounding can also flip near-degenerate windings; re-check.
    if _signed_area(rounded) < 0:
        rounded = list(reversed(rounded))
    # Canonical start vertex: rotate so the smallest (lon, lat) comes first.
    start = rounded.index(min(rounded))
    rounded = rounded[start:] + rounded[:start]
    return {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in rounded]]}


def aoi_hash(ring: Sequence[Sequence[float]]) -> str:
    """sha256 hex digest of the canonical GeoJSON (§6.2 cache key)."""
    canonical = canonical_aoi_geojson(ring)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def aoi_area_km2(ring: Sequence[Sequence[float]]) -> float:
    """Approximate AOI area in km² (equirectangular, adequate for limit checks
    against MAX_AOI_KM2; not for scientific use)."""
    import math

    coords = validate_exterior_ring(ring)
    mean_lat = sum(lat for _, lat in coords) / len(coords)
    km_per_deg_lat = 111.32
    km_per_deg_lon = 111.32 * math.cos(math.radians(mean_lat))
    projected = [(lon * km_per_deg_lon, lat * km_per_deg_lat) for lon, lat in coords]
    return abs(_signed_area(projected))
