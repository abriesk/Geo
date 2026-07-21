"""LiCSAR frame catalog lookup (§3 Tier-2 coverage check).

Turns an AOI polygon into a ranked list of intersecting LiCSAR frame IDs.

Design (Option 1 — static local catalog):
- Catalog is a versioned GeoJSON FeatureCollection shipped with the repo.
- Lookup is pure, offline, and deterministic.
- Ranking prefers highest overlap fraction, then centroid coverage.
- Empty result means "no LiCSAR coverage" → fall through to Tier 3 (HyP3)
  or an honest "not covered" answer.

Catalog path resolution order:
1. LICSAR_FRAMES_GEOJSON env var (absolute or relative path)
2. /static/licsar_frames.geojson  (Docker / production layout)
3. <package>/../../static/licsar_frames.geojson  (dev layout from libs/)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from .geometry import AoiValidationError, validate_exterior_ring

Coord = Tuple[float, float]
GeoJsonPolygon = dict  # {"type": "Polygon", "coordinates": [...]}

# Defaults matching the technical reference spirit
DEFAULT_MIN_OVERLAP = 0.05   # 5 % of AOI must be covered
DEFAULT_MAX_FRAMES = 3


@dataclass(frozen=True, slots=True)
class FrameMatch:
    """One LiCSAR frame that intersects the query AOI."""

    frame_id: str
    track: int
    orbit: str                  # "A" or "D"
    overlap_fraction: float     # intersection_area / aoi_area  (0–1)
    intersection_area_deg2: float
    covers_centroid: bool

    def __str__(self) -> str:
        return (
            f"{self.frame_id} (track {self.track}{self.orbit}, "
            f"overlap={self.overlap_fraction:.1%}, centroid={self.covers_centroid})"
        )


def _resolve_catalog_path() -> Path:
    env = os.environ.get("LICSAR_FRAMES_GEOJSON")
    if env:
        return Path(env).expanduser().resolve()

    # Production / Docker layout
    candidates = [
        Path("/static/licsar_frames.geojson"),
        Path(__file__).resolve().parents[3] / "static" / "licsar_frames.geojson",
        Path(__file__).resolve().parents[2] / "static" / "licsar_frames.geojson",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(
        "LiCSAR frame catalog not found. Set LICSAR_FRAMES_GEOJSON or place "
        "static/licsar_frames.geojson in the expected location."
    )


@lru_cache(maxsize=1)
def _load_catalog(path_str: str) -> List[Tuple[str, int, str, Polygon]]:
    """Load and cache the catalog. Returns list of (frame_id, track, orbit, geom)."""
    path = Path(path_str)
    with path.open(encoding="utf-8") as f:
        fc = json.load(f)

    if fc.get("type") != "FeatureCollection":
        raise ValueError(f"{path} is not a FeatureCollection")

    frames: List[Tuple[str, int, str, Polygon]] = []
    for feat in fc.get("features", []):
        props = feat.get("properties") or {}
        frame_id = props.get("frame_id")
        if not frame_id:
            continue
        track = int(props.get("track", frame_id[:3]))
        orbit = str(props.get("orbit", frame_id[3:4])).upper()
        if orbit not in ("A", "D"):
            orbit = "A" if "A" in frame_id[3:4] else "D"

        geom = shape(feat["geometry"])
        if not isinstance(geom, Polygon) or geom.is_empty:
            continue
        frames.append((frame_id, track, orbit, geom))

    if not frames:
        raise ValueError(f"No valid frames loaded from {path}")
    return frames


def _aoi_to_shapely(aoi: Union[GeoJsonPolygon, Sequence[Sequence[float]], Polygon]) -> Polygon:
    """Accept GeoJSON dict, exterior ring, or already-constructed Shapely Polygon."""
    if isinstance(aoi, Polygon):
        return aoi
    if isinstance(aoi, dict):
        if aoi.get("type") != "Polygon":
            raise AoiValidationError("AOI must be a GeoJSON Polygon")
        ring = aoi["coordinates"][0]
        coords = validate_exterior_ring(ring)
        return Polygon(coords)
    # bare ring
    coords = validate_exterior_ring(aoi)
    return Polygon(coords)


def find_frames(
    aoi: Union[GeoJsonPolygon, Sequence[Sequence[float]], Polygon],
    *,
    catalog_path: Optional[Union[str, Path]] = None,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> List[FrameMatch]:
    """Return LiCSAR frames that intersect the AOI, ranked by usefulness.

    Parameters
    ----------
    aoi :
        GeoJSON Polygon dict, exterior-ring coordinates, or Shapely Polygon.
        Must already satisfy §6.1 (validated here as a safety net).
    catalog_path :
        Override the default catalog location.
    min_overlap :
        Minimum fraction of the *AOI* that must be covered (default 5 %).
    max_frames :
        Maximum number of frames to return (default 3).

    Returns
    -------
    list[FrameMatch]
        Sorted best-first. Empty list means no usable LiCSAR coverage.
    """
    poly = _aoi_to_shapely(aoi)
    if poly.area <= 0:
        return []

    path = Path(catalog_path) if catalog_path else _resolve_catalog_path()
    catalog = _load_catalog(str(path.resolve()))

    centroid = poly.centroid
    aoi_area = poly.area
    matches: List[FrameMatch] = []

    for frame_id, track, orbit, frame_geom in catalog:
        # Cheap reject
        if not poly.bounds or not frame_geom.bounds:
            continue
        if (poly.bounds[2] < frame_geom.bounds[0] or
                poly.bounds[0] > frame_geom.bounds[2] or
                poly.bounds[3] < frame_geom.bounds[1] or
                poly.bounds[1] > frame_geom.bounds[3]):
            continue

        inter: BaseGeometry = poly.intersection(frame_geom)
        if inter.is_empty:
            continue

        inter_area = inter.area
        frac = inter_area / aoi_area
        if frac < min_overlap:
            continue

        matches.append(
            FrameMatch(
                frame_id=frame_id,
                track=track,
                orbit=orbit,
                overlap_fraction=round(frac, 4),
                intersection_area_deg2=inter_area,
                covers_centroid=frame_geom.contains(centroid),
            )
        )

    # Rank: highest overlap first, then prefer centroid coverage,
    # then larger absolute intersection.
    matches.sort(
        key=lambda m: (m.overlap_fraction, m.covers_centroid, m.intersection_area_deg2),
        reverse=True,
    )
    return matches[:max_frames]


def find_frames_for_point(
    lon: float,
    lat: float,
    *,
    catalog_path: Optional[Union[str, Path]] = None,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> List[FrameMatch]:
    """Convenience: frames that contain a single point (tiny synthetic AOI)."""
    # ~100 m box around the point so area math still works
    d = 0.001
    ring = [
        (lon - d, lat - d),
        (lon + d, lat - d),
        (lon + d, lat + d),
        (lon - d, lat + d),
        (lon - d, lat - d),
    ]
    return find_frames(
        {"type": "Polygon", "coordinates": [ring]},
        catalog_path=catalog_path,
        min_overlap=0.5,          # point-like → require strong containment
        max_frames=max_frames,
    )
