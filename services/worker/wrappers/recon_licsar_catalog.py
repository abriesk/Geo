#!/usr/bin/env python3
"""recon_licsar_catalog.py — gather the facts needed to design the frame-catalog builder.

Run on the T7910. It answers three questions I can't check from my sandbox
(JASMIN isn't reachable there):

  1. What does a TRACK directory listing look like? (so we parse frame IDs)
  2. What fields does a frame's metadata.txt contain? (cheap center coords for
     regional pre-filtering — or do we need the geotiff for the footprint?)
  3. Can we read a frame's geocoded .geo.U.tif BOUNDS cheaply via GDAL /vsicurl
     (header only, no full download)? (the authoritative footprint source)

Usage (needs `requests`; part 3 needs GDAL — run in the worker licsbas env):
  # parts 1 & 2 (any python with requests):
  python3 recon_licsar_catalog.py
  # part 3 (GDAL) — run inside the worker image's licsbas env:
  docker compose run --rm --entrypoint /opt/conda/envs/licsbas/bin/python worker \
     wrappers/recon_licsar_catalog.py --tif
"""
from __future__ import annotations

import argparse
import re
import sys

ROOT = "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products"
SAMPLE_TRACK = "174"
SAMPLE_FRAME = "174A_05018_131313"
FRAME_RE = re.compile(r"\d{3}[AD]_\d{5}_\d{6}")


def part1_track_listing() -> None:
    import requests
    print("=" * 60)
    print(f"[1] TRACK LISTING  {ROOT}/{SAMPLE_TRACK}/")
    print("=" * 60)
    r = requests.get(f"{ROOT}/{SAMPLE_TRACK}/", timeout=30)
    print(f"HTTP {r.status_code}, {len(r.text)} bytes")
    frames = sorted(set(FRAME_RE.findall(r.text)))
    print(f"frame IDs found on track {SAMPLE_TRACK}: {len(frames)}")
    for f in frames[:12]:
        print("   ", f)
    if len(frames) > 12:
        print(f"    ... (+{len(frames) - 12} more)")
    # show a raw snippet so we see the exact HTML/anchor structure
    idx = r.text.find(frames[0]) if frames else -1
    if idx >= 0:
        print("\n  raw listing snippet around first frame:")
        print("  " + repr(r.text[max(0, idx - 60):idx + 60]))


def part2_metadata() -> None:
    import requests
    print("\n" + "=" * 60)
    print(f"[2] METADATA.TXT  {SAMPLE_FRAME}")
    print("=" * 60)
    url = f"{ROOT}/{SAMPLE_TRACK}/{SAMPLE_FRAME}/metadata/metadata.txt"
    r = requests.get(url, timeout=30)
    print(f"HTTP {r.status_code}")
    print("--- full contents ---")
    print(r.text.strip())
    print("--- end ---")
    # highlight any lat/lon/corner fields
    hits = [ln for ln in r.text.splitlines()
            if re.search(r"(lon|lat|corner|center|centre|extent|bound)", ln, re.I)]
    print("\n  location-relevant lines:", hits or "(none — will need geotiff bounds)")


def part3_tif_bounds() -> None:
    print("\n" + "=" * 60)
    print(f"[3] GEOTIFF BOUNDS via /vsicurl  {SAMPLE_FRAME}.geo.U.tif")
    print("=" * 60)
    try:
        from osgeo import gdal
    except ImportError:
        print("GDAL not importable here — rerun this part in the licsbas env "
              "(see header usage). Skipping.")
        return
    gdal.UseExceptions()
    url = (f"/vsicurl/{ROOT}/{SAMPLE_TRACK}/{SAMPLE_FRAME}/metadata/"
           f"{SAMPLE_FRAME}.geo.U.tif")
    print(f"opening {url}")
    ds = gdal.Open(url)
    if ds is None:
        print("could not open — /vsicurl may be blocked or path wrong")
        return
    gt = ds.GetGeoTransform()
    nx, ny = ds.RasterXSize, ds.RasterYSize
    lon_min = gt[0]
    lon_max = gt[0] + gt[1] * nx
    lat_max = gt[3]
    lat_min = gt[3] + gt[5] * ny
    print(f"  size: {nx} x {ny}")
    print(f"  bbox lon: {min(lon_min, lon_max):.4f} .. {max(lon_min, lon_max):.4f}")
    print(f"  bbox lat: {min(lat_min, lat_max):.4f} .. {max(lat_min, lat_max):.4f}")
    print(f"  CRS: {ds.GetProjection()[:60]}...")
    print("  -> reading header only; no full-file download needed. GOOD.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tif", action="store_true", help="run part 3 (needs GDAL)")
    args = ap.parse_args()
    try:
        part1_track_listing()
        part2_metadata()
    except Exception as e:  # noqa: BLE001
        print(f"parts 1/2 error: {e}")
    if args.tif:
        part3_tif_bounds()
    else:
        print("\n[3] skipped — rerun with --tif in the licsbas env for geotiff bounds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
