"""libs/licsar/frames.py — resolve an AOI to LiCSAR frame(s) from the catalog.

Adapted from the community-proposed find_licsar_frames, with changes for our
bbox-approximate catalog (frame footprints are geo.U.tif bounding boxes, not
the true tilted parallelograms — see build_licsar_catalog.py):

  * over-return: a lower default min_overlap and a candidate cap higher than
    what we ultimately use, because bbox footprints over-state coverage and
    the real LiCSBAS run is the final arbiter of whether a frame is usable.
  * per-orbit selection: prefer one ascending + one descending frame (two
    viewing geometries), returning the best of each rather than only the
    single largest overlap.
  * equal-area intersection (EPSG:6933) so overlap fractions aren't distorted
    by latitude.

The catalog is loaded once and cached. If the catalog file is missing, callers
should fall back to a configured default frame.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

EQUAL_AREA = "EPSG:6933"
_WGS84_TO_EA = Transformer.from_crs("EPSG:4326", EQUAL_AREA, always_xy=True).transform


def _to_ea(geom):
    return transform(_WGS84_TO_EA, geom)


@lru_cache(maxsize=4)
def _load_catalog(catalog_path: str) -> tuple:
    data = json.loads(Path(catalog_path).read_text())
    frames = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        frames.append((
            p.get("frame_id"),
            p.get("orbit"),
            int(p.get("track", 0)),
            _to_ea(shape(feat["geometry"])),
        ))
    return tuple(frames)


def find_licsar_frames(
    aoi_geojson: dict,
    catalog_path: str = "data/licsar_frames.geojson",
    min_overlap_fraction: float = 0.05,
    max_frames_per_orbit: int = 3,
) -> list[dict[str, Any]]:
    """Return ordered candidate frames for the AOI. Empty list if none.

    Returns up to max_frames_per_orbit frames PER orbit, ranked by overlap
    desc. Spatially-redundant frames are intentionally KEPT (not pruned): two
    frames covering the same AOI are not interchangeable because one may be
    temporally dead (processing stopped, no recent interferograms). The caller
    (wrap_licsbas) probes candidates in order and uses the first with data, so
    it needs redundant alternatives to fall through to. Ascending frames are
    listed first (arbitrary but stable; both geometries are useful).
    """
    if not Path(catalog_path).exists():
        return []
    aoi_ea = _to_ea(shape(aoi_geojson))
    aoi_area = aoi_ea.area
    if aoi_area <= 0:
        return []

    cands: list[dict] = []
    for frame_id, orbit, track, geom_ea in _load_catalog(catalog_path):
        inter = geom_ea.intersection(aoi_ea)
        if inter.is_empty:
            continue
        frac = inter.area / aoi_area
        if frac >= min_overlap_fraction:
            cands.append({"frame_id": frame_id, "orbit": orbit, "track": track,
                          "overlap_fraction": round(frac, 4)})
    if not cands:
        return []

    cands.sort(key=lambda c: c["overlap_fraction"], reverse=True)

    selected: list[dict] = []
    per_orbit = {"A": 0, "D": 0}
    for c in cands:
        orb = c["orbit"]
        if orb not in per_orbit or per_orbit[orb] >= max_frames_per_orbit:
            continue
        selected.append(c)
        per_orbit[orb] += 1
    # ascending first, then by overlap desc (stable candidate order for fallback)
    selected.sort(key=lambda s: (s["orbit"] != "A", -s["overlap_fraction"]))
    return selected
