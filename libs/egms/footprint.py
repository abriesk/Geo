"""EGMS coverage check (§3 ladder tier 1) — M4.1.

AOI-in-footprint test against the shipped static EGMS boundary polygon
(EEA-39 / Copernicus participating states). Conservative by design: when in
doubt, report NOT covered so the deformation query falls back to LiCSBAS
(tier 2). A false 'covered' would route a European-edge or non-European AOI
to a tier that has no data there and burn a full async CLMS download job to
discover it. A false 'not covered' merely costs us the (better) EGMS tier and
falls back to the working LiCSBAS path — the safe direction.

Placement mirrors libs/licsar/frames.py so the backend import style is
identical: `from egms.footprint import aoi_in_egms` with /libs on sys.path.
"""
from __future__ import annotations

import json
from pathlib import Path

# shapely is used elsewhere in the backend for AOI winding / self-intersection
# validation (§6.1). If it is somehow not present in the backend image, add
# `shapely>=2.0` to services/backend/requirements.txt.
from shapely.geometry import shape
from shapely.prepared import prep

# Module-level cache: the prepared footprint geometry is reused across queries
# in the long-lived backend process. Keyed on the path so a config change is
# picked up without a restart in tests.
_PREPARED = None
_PREPARED_PATH: str | None = None


def _load(footprint_path: str):
    """Load + prepare the footprint geometry once, then cache it."""
    global _PREPARED, _PREPARED_PATH
    if _PREPARED is not None and _PREPARED_PATH == footprint_path:
        return _PREPARED
    gj = json.loads(Path(footprint_path).read_text())
    t = gj.get("type")
    if t == "FeatureCollection":
        # union all feature geometries (footprint may be multi-part)
        from shapely.ops import unary_union
        geom = unary_union([shape(f["geometry"]) for f in gj["features"]])
    elif t == "Feature":
        geom = shape(gj["geometry"])
    else:
        geom = shape(gj)  # bare geometry (Polygon / MultiPolygon)
    _PREPARED = prep(geom)
    _PREPARED_PATH = footprint_path
    return _PREPARED


def aoi_in_egms(aoi_geojson: dict, footprint_path: str) -> bool:
    """True iff the AOI is fully within the EGMS footprint.

    'Fully within' (contains), not merely intersecting: a partly-covered AOI
    would yield EGMS stats over only part of the drawn area and silently
    under-report. Partial-coverage handling is deferred (BACKLOG -> M5).

    Any failure (missing/unreadable footprint, bad AOI geometry) returns False
    so the deformation query falls back to LiCSBAS rather than crashing.
    """
    try:
        fp = _load(footprint_path)
    except Exception as e:  # noqa: BLE001
        print(f"[egms] footprint load failed ({e!r}); treating as NOT covered", flush=True)
        return False
    try:
        aoi = shape(aoi_geojson)
        return bool(fp.contains(aoi))
    except Exception as e:  # noqa: BLE001
        print(f"[egms] coverage test failed ({e!r}); NOT covered", flush=True)
        return False
