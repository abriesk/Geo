#!/usr/bin/env python3
"""m412_patch.py — M4.1.2: never analyse a frame that does not cover the AOI.

THE BUG (found by the M4.1.1 fan-out, on a real Paris query)
LiCSAR has no frame over Paris, so the catalog correctly returned nothing. The
resolver then fell back to DEFAULT_DEFORM_FRAME — the Yerevan test frame — with
no geographic check whatsoever, and LiCSBAS tried to clip a Paris bounding box
out of a Caucasus frame:

    LiCSBAS05op_clip_unw.py -g 2.3100/2.3900/48.8300/48.8900   <- Paris
      44.0819445/47.5249418/38.4848910/41.1688889 ->           <- frame: Armenia
      Width/Length: 3444/2685 -> -41691/-7660                  <- negative

The crash was LUCKY. Had the clip arithmetic landed inside the raster, this
would have produced a velocity map of Armenia and reported it as the answer to
a query about Paris — confidently wrong about the wrong continent, which is the
most dangerous failure mode this system has. It then burned three retries.

THE FIX (defence in depth)
1. Router: the single configured fallback frame is used ONLY when the catalog
   confirms it overlaps this AOI. Otherwise: no InSAR coverage, stated plainly.
2. Router: when no frame covers the AOI, mark params so the wrapper can answer
   immediately instead of downloading and failing.
3. Wrapper: independently drop any candidate frame whose catalog footprint does
   not overlap the AOI, whatever put it there — so a stale config or a
   hand-passed frame id can never analyse the wrong place either.
4. Both no-coverage paths exit 0 with an honest result.json, so the query gets
   a real answer and the deterministic failure is not retried three times.

Run from the repo root:   python3 scripts/m412_patch.py
Re-running is safe. Aborts without writing if the files differ from expected.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

MAIN = Path("services/backend/app/main.py")
WRAP = Path("services/worker/wrappers/wrap_licsbas.py")

# --------------------------------------------------------------------------
# 1. main.py — geographic validation of the fallback frame
# --------------------------------------------------------------------------
M_OLD = '''    if DEFAULT_DEFORM_FRAME:
        print(f"[router] fallback frame {DEFAULT_DEFORM_FRAME}", flush=True)
        return [DEFAULT_DEFORM_FRAME]
    return []
'''
M_NEW = '''    if DEFAULT_DEFORM_FRAME:
        # M4.1.2: the fallback is ONE fixed frame (originally a test frame).
        # Using it for an AOI it does not cover would analyse the WRONG PLACE
        # and report the result under the user's query. Only use it when the
        # catalog confirms it actually overlaps this AOI.
        if _frame_covers_aoi(DEFAULT_DEFORM_FRAME, aoi_geojson):
            print(f"[router] fallback frame {DEFAULT_DEFORM_FRAME}", flush=True)
            return [DEFAULT_DEFORM_FRAME]
        print(f"[router] fallback frame {DEFAULT_DEFORM_FRAME} does not cover "
              "this AOI -> no InSAR coverage here", flush=True)
    return []
'''

# --------------------------------------------------------------------------
# 2. main.py — the coverage helper + no-coverage flag in params
# --------------------------------------------------------------------------
D_OLD = '''def _deform_params(question: str, aoi_geojson: dict) -> dict:
    p = {"hazard": "deformation", "simulate_failure": "FAIL!" in question}
    frames = _resolve_deform_frames(aoi_geojson)
    if frames:
        # MVP: run the best (first) frame — ascending, largest overlap. Running
        # asc+desc as two tasks is a straightforward extension (BACKLOG).
        p["frame_id"] = frames[0]
        p["candidate_frames"] = frames
    return p
'''
D_NEW = '''def _frame_covers_aoi(frame_id: str, aoi_geojson: dict) -> bool:
    """Does this frame's catalog footprint overlap the AOI at all? (M4.1.2)

    Deliberately fails CLOSED: if the catalog is missing or the frame is not in
    it, we cannot prove coverage, so we answer False. Analysing the wrong place
    is far worse than declining to analyse.
    """
    try:
        with open(LICSAR_CATALOG) as fh:
            data = json.load(fh)
        ring = aoi_geojson["coordinates"][0]
        a_lons = [c[0] for c in ring]
        a_lats = [c[1] for c in ring]
        for feat in data.get("features", []):
            if (feat.get("properties") or {}).get("frame_id") != frame_id:
                continue
            f_ring = feat["geometry"]["coordinates"][0]
            f_lons = [c[0] for c in f_ring]
            f_lats = [c[1] for c in f_ring]
            return not (max(a_lons) < min(f_lons) or min(a_lons) > max(f_lons)
                        or max(a_lats) < min(f_lats) or min(a_lats) > max(f_lats))
        print(f"[router] frame {frame_id} not found in catalog", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[router] cannot verify frame coverage ({e!r})", flush=True)
    return False


def _deform_params(question: str, aoi_geojson: dict) -> dict:
    p = {"hazard": "deformation", "simulate_failure": "FAIL!" in question}
    frames = _resolve_deform_frames(aoi_geojson)
    if frames:
        # MVP: run the best (first) frame — ascending, largest overlap. Running
        # asc+desc as two tasks is a straightforward extension (BACKLOG).
        p["frame_id"] = frames[0]
        p["candidate_frames"] = frames
    else:
        # M4.1.2: no frame covers this AOI. Tell the wrapper so it can answer
        # "no InSAR coverage here" immediately instead of downloading a frame
        # from somewhere else and failing three times over.
        p["no_insar_coverage"] = True
    return p
'''

# --------------------------------------------------------------------------
# 3. wrap_licsbas.py — catalog helpers (appended after _aoi_bbox)
# --------------------------------------------------------------------------
H_OLD = '''def run_licsbas(args, params: dict) -> int:
'''
H_NEW = '''def _frame_bbox_from_catalog(frame_id: str, catalog_path: str):
    """(min_lon, min_lat, max_lon, max_lat) for a frame, or None if unknown."""
    try:
        with open(catalog_path) as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    for feat in data.get("features", []):
        if (feat.get("properties") or {}).get("frame_id") != frame_id:
            continue
        try:
            ring = feat["geometry"]["coordinates"][0]
        except Exception:  # noqa: BLE001
            return None
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        return (min(lons), min(lats), max(lons), max(lats))
    return None


def _frame_covers_aoi(frame_id: str, aoi_bbox, catalog_path: str) -> bool:
    """Does the frame footprint overlap the AOI? aoi_bbox is (lon1, lon2, lat1,
    lat2) as returned by _aoi_bbox.

    Defence in depth (M4.1.2): the router already validates, but a frame id can
    also arrive from --frame or a stale params blob. Clipping an AOI out of a
    frame on another continent yields negative raster dimensions at best, and a
    plausible map of the wrong place at worst. If the frame is absent from the
    catalog we return True — the router is the authority, and a missing catalog
    must not block a legitimate run.
    """
    fb = _frame_bbox_from_catalog(frame_id, catalog_path)
    if fb is None:
        return True
    lon1, lon2, lat1, lat2 = aoi_bbox
    return not (lon2 < fb[0] or lon1 > fb[2] or lat2 < fb[1] or lat1 > fb[3])


def _no_coverage_result(args, start: str, end: str, reason: str) -> int:
    """Honest 'no InSAR coverage' result. Exit 0 so a deterministic geographic
    miss is not retried three times."""
    sys.path.insert(0, "/libs/contracts")
    from geohazard_contracts import ResultJson

    os.makedirs(args.output_dir, exist_ok=True)
    result = ResultJson.model_validate({
        "query_id": args.query_id,
        "method": "licsbas",
        "status": "failed",
        "summary_stats": {},
        "quality": {
            "scene_count": 0,
            "date_coverage": [_iso_date(start), _iso_date(end)],
            "coherence_mean": None, "masked_fraction": None,
            "cloud_fraction": None, "confidence": "low",
            "caveats": [
                reason,
                "The LiCSAR archive processes Sentinel-1 data over selected "
                "regions — mainly tectonically and volcanically active areas — "
                "rather than the whole world, so many places have no frame.",
            ],
        },
        "artifacts": [],
        "attribution": [
            "LiCSAR contains modified Copernicus Sentinel data analysed by COMET.",
        ],
    })
    with open(os.path.join(args.output_dir, "result.json"), "w") as f:
        f.write(result.model_dump_json(indent=2))
    progress(100, "no InSAR coverage for this area")
    return 0


def run_licsbas(args, params: dict) -> int:
'''

# --------------------------------------------------------------------------
# 4. wrap_licsbas.py — use the guards instead of erroring out
# --------------------------------------------------------------------------
G_OLD = '''    candidates = [c for c in candidates if c]
    if not candidates:
        print("ERROR no candidate frames provided", file=sys.stderr)
        return 2
'''
G_NEW = '''    candidates = [c for c in candidates if c]

    # M4.1.2: the router found no frame covering this AOI -> answer immediately.
    if params.get("no_insar_coverage"):
        return _no_coverage_result(
            args, start, end,
            "No InSAR frame in the LiCSAR archive covers this area, so a "
            "radar time-series could not be computed for it.")

    # M4.1.2: independently drop candidates whose footprint misses the AOI.
    catalog_path = params.get("licsar_catalog", "/data/licsar_frames.geojson")
    aoi_bbox = (lon1, lon2, lat1, lat2)
    covering = [c for c in candidates
                if _frame_covers_aoi(c, aoi_bbox, catalog_path)]
    if len(covering) != len(candidates):
        dropped = [c for c in candidates if c not in covering]
        print(f"[wrap_licsbas] dropping frame(s) that do not cover the AOI: "
              f"{dropped}", flush=True)
    candidates = covering

    if not candidates:
        return _no_coverage_result(
            args, start, end,
            "No InSAR frame covering this area was available, so a radar "
            "time-series could not be computed for it.")
'''


def apply(path: Path, edits: list[tuple[str, str, str]]) -> list[str]:
    if not path.exists():
        sys.exit(f"ABORT: {path} not found — run from the repo root.")
    text = path.read_text()
    planned, done = [], []
    for label, old, new in edits:
        if new in text:
            done.append(f"  = {label}: already applied, skipping")
            continue
        n = text.count(old)
        if n != 1:
            sys.exit(
                f"ABORT: {path}: expected exactly one match for '{label}', found {n}.\n"
                "Nothing was written."
            )
        planned.append((label, old, new))
    for label, old, new in planned:
        text = text.replace(old, new, 1)
        done.append(f"  + {label}")
    if planned:
        shutil.copy(path, path.with_suffix(path.suffix + ".m412.bak"))
        path.write_text(text)
    return done


def main() -> int:
    print("M4.1.2 patch — never analyse a frame that does not cover the AOI\n")
    print(f"{MAIN}:")
    for line in apply(MAIN, [
        ("fallback frame must cover the AOI", M_OLD, M_NEW),
        ("_frame_covers_aoi helper + no_insar_coverage flag", D_OLD, D_NEW),
    ]):
        print(line)
    print(f"\n{WRAP}:")
    for line in apply(WRAP, [
        ("catalog helpers + _no_coverage_result", H_OLD, H_NEW),
        ("spatial guard on candidate frames", G_OLD, G_NEW),
    ]):
        print(line)

    import py_compile
    for p in (MAIN, WRAP):
        try:
            py_compile.compile(str(p), doraise=True)
        except py_compile.PyCompileError as e:
            sys.exit(f"\nABORT: {p} does not compile:\n{e}\nRestore from {p}.m412.bak")
    print("\nBoth files compile. Backups written as *.m412.bak")
    print("Next:  docker compose up --build -d backend worker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
