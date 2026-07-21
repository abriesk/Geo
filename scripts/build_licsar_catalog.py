#!/usr/bin/env python3
"""build_licsar_catalog.py — one-time global LiCSAR frame catalog builder.

Crawls the public LiCSAR product tree and records every frame's footprint
(bbox) into a GeoJSON the routing layer reads to resolve AOI -> frame(s).
This is an OFFLINE, occasional job — run once, commit the output, ship it.
End users never run this; they consume the shipped licsar_frames.geojson.

Footprint source (verified via recon): metadata.txt has NO coordinates, so
the footprint comes from the geocoded {frame}.geo.U.tif read via GDAL
/vsicurl (header only — no full download). The bbox over-includes the tilted
frame's nodata corners, which is the safe direction for candidate selection.

Design for a multi-thousand-frame global sweep:
  * concurrency  — thread pool of header reads (JASMIN serves these fine)
  * resumable    — per-track checkpoint; re-running skips finished tracks
  * robust       — the portal is mid-migration; 404/timeout on a frame is
                   logged and skipped, never fatal

Run in the licsbas env (needs GDAL):
  /opt/conda/envs/licsbas/bin/python build_licsar_catalog.py \
      --out data/licsar_frames.geojson --workers 16
  # quick pipeline test on a few tracks first:
  ... --tracks 174,175,6
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import requests

ROOTS = [
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products",
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products.future",
]
FRAME_RE = re.compile(r"\d{3}[AD]_\d{5}_\d{6}")
N_TRACKS = 175
LIST_TIMEOUT = 60
_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _get(url: str, timeout: int):
    try:
        return requests.get(url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None


def list_frames(track: int) -> tuple[str | None, list[str]]:
    """Return (root_that_worked, [frame_ids]) for a track, trying both roots."""
    for root in ROOTS:
        r = _get(f"{root}/{track}/", LIST_TIMEOUT)
        if r is not None and r.status_code == 200:
            frames = sorted(set(FRAME_RE.findall(r.text)))
            if frames:
                return root, frames
    return None, []


def frame_bbox(root: str, track: int, frame: str) -> dict | None:
    """Read {frame}.geo.U.tif bounds via /vsicurl (header only). None on failure."""
    from osgeo import gdal

    gdal.UseExceptions()
    url = f"/vsicurl/{root}/{track}/{frame}/metadata/{frame}.geo.U.tif"
    try:
        ds = gdal.Open(url)
        if ds is None:
            return None
        gt = ds.GetGeoTransform()
        nx, ny = ds.RasterXSize, ds.RasterYSize
        lon1, lon2 = gt[0], gt[0] + gt[1] * nx
        lat2, lat1 = gt[3], gt[3] + gt[5] * ny
        ds = None
        lon_min, lon_max = sorted((lon1, lon2))
        lat_min, lat_max = sorted((lat1, lat2))
        # sanity: valid geographic bbox
        if not (-180 <= lon_min < lon_max <= 180 and -90 <= lat_min < lat_max <= 90):
            return None
        return {
            "frame_id": frame,
            "orbit": frame[3],                       # 'A' or 'D'
            "track": track,
            "bbox": [round(lon_min, 4), round(lat_min, 4),
                     round(lon_max, 4), round(lat_max, 4)],
        }
    except Exception:  # noqa: BLE001
        return None


def _feature(rec: dict) -> dict:
    lon_min, lat_min, lon_max, lat_max = rec["bbox"]
    return {
        "type": "Feature",
        "properties": {"frame_id": rec["frame_id"], "orbit": rec["orbit"],
                       "track": rec["track"]},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[lon_min, lat_min], [lon_max, lat_min],
                             [lon_max, lat_max], [lon_min, lat_max],
                             [lon_min, lat_min]]],
        },
    }


def process_track(track: int, workers: int) -> list[dict]:
    root, frames = list_frames(track)
    if not frames:
        log(f"track {track:>3}: no frames (or unreachable)")
        return []
    recs: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(frame_bbox, root, track, f): f for f in frames}
        for fut in cf.as_completed(futs):
            rec = fut.result()
            if rec:
                recs.append(rec)
    log(f"track {track:>3}: {len(recs)}/{len(frames)} frames captured")
    return recs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/licsar_frames.geojson")
    ap.add_argument("--workers", type=int, default=16, help="parallel tif reads per track")
    ap.add_argument("--tracks", default="", help="comma list to limit (e.g. 174,175); default all 175")
    ap.add_argument("--checkpoint", default="", help="checkpoint dir (default alongside --out)")
    args = ap.parse_args()

    # Quiet the harmless PROJ warning seen in recon.
    os.environ.setdefault("PROJ_LIB", "/opt/conda/envs/licsbas/share/proj")

    tracks = ([int(t) for t in args.tracks.split(",") if t.strip()]
              if args.tracks else list(range(1, N_TRACKS + 1)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ckdir = Path(args.checkpoint) if args.checkpoint else out.parent / "_catalog_ckpt"
    ckdir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    all_recs: list[dict] = []
    for i, track in enumerate(tracks, 1):
        ckf = ckdir / f"track_{track:03d}.json"
        if ckf.exists():                       # resume: skip finished tracks
            recs = json.loads(ckf.read_text())
            log(f"track {track:>3}: cached ({len(recs)} frames)")
        else:
            recs = process_track(track, args.workers)
            ckf.write_text(json.dumps(recs))
        all_recs.extend(recs)
        if i % 10 == 0:
            log(f"... {i}/{len(tracks)} tracks, {len(all_recs)} frames so far, "
                f"{time.time() - t0:.0f}s")

    fc = {"type": "FeatureCollection",
          "properties": {"source": "LiCSAR product tree (geo.U.tif bounds, bbox-approx)",
                         "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "frame_count": len(all_recs)},
          "features": [_feature(r) for r in all_recs]}
    out.write_text(json.dumps(fc))
    log(f"\nDONE: {len(all_recs)} frames across {len(tracks)} tracks "
        f"in {time.time() - t0:.0f}s -> {out}")
    log(f"(checkpoints in {ckdir}; delete to force a full rebuild)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
