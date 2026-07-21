#!/usr/bin/env python3
"""licsar_probe.py — check whether a LiCSAR frame is live and downloadable.

Standalone (only needs `requests`); run on the T7910 to verify a frame BEFORE
we build the heavy LiCSBAS image or commit to an hours-long run.

URL structure is taken verbatim from comet-licsar/LiCSBAS
bin/LiCSBAS01_get_geotiff.py (fetched into context this session), not guessed:
  LiCSARweb = https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products/
  trackID   = str(int(frameID[0:3]))          # strips leading zeros
  metadata  = {web}/{track}/{frame}/metadata/metadata.txt
  ifg list  = {web}/{track}/{frame}/interferograms/

Portal is mid-migration (LiCSAR_products -> LiCSAR_products.future); this probe
checks BOTH roots so we learn which serves your frame today.

Usage:
  python3 licsar_probe.py 021D_04972_131213 [-s 20240101] [-e 20260101]

Find your frame ID by clicking the AOI on the portal map:
  https://comet.nerc.ac.uk/comet-lics-portal/   (Select a frame -> ID shown)
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date

import requests

ROOTS = [
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products",
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products.future",
]
FRAME_RE = re.compile(r"^\d{3}[AD]_\d{5}_\d{6}$")
PAIR_RE = re.compile(r"(\d{8})_(\d{8})")
TIMEOUT = 20


def track_of(frame: str) -> str:
    return str(int(frame[0:3]))


def probe_root(root: str, frame: str, start: int, end: int) -> dict:
    track = track_of(frame)
    base = f"{root}/{track}/{frame}"
    out: dict = {"root": root, "base": base}

    # 1. metadata.txt — cheap liveness check
    try:
        r = requests.get(f"{base}/metadata/metadata.txt", timeout=TIMEOUT)
        out["metadata_http"] = r.status_code
        out["metadata_ok"] = r.status_code == 200 and "master" in r.text.lower()
    except Exception as e:  # noqa: BLE001
        out["metadata_http"] = f"error: {e}"
        out["metadata_ok"] = False

    # 2. interferograms listing — count pairs, and pairs within [start,end]
    try:
        r = requests.get(f"{base}/interferograms/", timeout=TIMEOUT)
        if r.status_code == 200:
            pairs = set(PAIR_RE.findall(r.text))
            in_range = [
                (a, b) for (a, b) in pairs
                if start <= int(a) <= end and start <= int(b) <= end
            ]
            out["ifg_total"] = len(pairs)
            out["ifg_in_range"] = len(in_range)
            if in_range:
                epochs = sorted({e for p in in_range for e in p})
                out["epoch_span"] = f"{epochs[0]}..{epochs[-1]}"
        else:
            out["ifg_total"] = 0
            out["ifg_http"] = r.status_code
    except Exception as e:  # noqa: BLE001
        out["ifg_total"] = 0
        out["ifg_http"] = f"error: {e}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("frame", help="LiCSAR frame ID, e.g. 021D_04972_131213")
    ap.add_argument("-s", type=int, default=20240101, help="start yyyymmdd")
    ap.add_argument("-e", type=int, default=int(date.today().strftime("%Y%m%d")),
                    help="end yyyymmdd")
    args = ap.parse_args()

    if not FRAME_RE.match(args.frame):
        print(f"ERROR: '{args.frame}' is not a valid frame ID "
              "(expected e.g. 021D_04972_131213)", file=sys.stderr)
        return 2

    print(f"Frame {args.frame} (track {track_of(args.frame)}), "
          f"window {args.s}..{args.e}\n")
    live_any = False
    for root in ROOTS:
        r = probe_root(root, args.frame, args.s, args.e)
        tag = root.rsplit("/", 1)[-1]
        print(f"[{tag}]")
        print(f"  metadata.txt : {r.get('metadata_http')} "
              f"({'OK' if r.get('metadata_ok') else 'not usable'})")
        total = r.get("ifg_total", 0)
        if total:
            print(f"  interferograms: {total} total, "
                  f"{r.get('ifg_in_range', 0)} in window "
                  f"({r.get('epoch_span', 'n/a')})")
            live_any = live_any or (r.get("metadata_ok") and r.get("ifg_in_range", 0) > 0)
        else:
            print(f"  interferograms: none ({r.get('ifg_http', 'empty')})")
        print()

    if live_any:
        print("RESULT: frame is LIVE and has interferograms in your window. "
              "Good to proceed to the LiCSBAS build (M3.1) and run (M3.2).")
        return 0
    print("RESULT: frame not usable at either root for this window. "
          "Try a wider date range, re-check the frame ID on the portal map, "
          "or pick the other track (A/D) covering your AOI.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
