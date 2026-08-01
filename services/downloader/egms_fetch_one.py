#!/usr/bin/env python3
"""egms_fetch_one.py — full EGMS flow + inspect the REAL file structure.

Proven so far: JWT auth, /releases, /search, and /download all work. Two
behaviours the variant probe revealed, handled here:

  * The download token is not live the instant a search returns — the first
    attempt can 401 with "rerun your search". We retry with a short backoff and,
    if needed, re-search for a fresh id.
  * The server allows AT MOST 2 CONCURRENT DOWNLOADS (429 otherwise), so we
    stream one file at a time and always drain/close the connection.

The open question this answers: what is actually INSIDE an L3 tile? L3 is the
ortho (vertical / east-west) product, and it is very likely gridded in EPSG:3035
easting/northing rather than lon/lat — which wrap_egms does not yet handle. We
read the real header rather than guessing.

Usage:
    python egms_fetch_one.py                      # ORTHO-UP tile over Paris
    python egms_fetch_one.py --keep               # leave the extracted files
    python egms_fetch_one.py --product-type ORTHO-EAST
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile

API = "https://egms.land.copernicus.eu/insar-api/archive"
PARIS_BBOX = [[2.31, 48.83], [2.39, 48.89]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="/run/secrets/clms_key.json")
    ap.add_argument("--level", default="L3")
    ap.add_argument("--product-type", default="ORTHO-UP")
    ap.add_argument("--release", default=None)
    ap.add_argument("--out", default="/tmp/egms_fetch")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    import requests
    from clms_client import ClmsClient

    c = ClmsClient.from_key_file(args.key)
    c._mint_access_token()
    auth = {"Authorization": f"Bearer {c._access_token}"}
    print(f"[1/5] auth ok (token len={len(c._access_token or '')})")

    release = args.release
    if not release:
        r = requests.get(f"{API}/releases", headers={**auth, "Accept": "application/json"},
                         timeout=60)
        release = (r.json() or [])[-1]
    print(f"[2/5] release = {release}")

    def search():
        q = {"id": None, "bbox": PARIS_BBOX, "levels": [args.level],
             "releases": [release], "productType": args.product_type}
        rr = requests.post(f"{API}/search", headers={**auth, "Accept": "application/json"},
                           data=json.dumps(q), timeout=120)
        j = rr.json()
        return j.get("id"), (j.get("hits") or [])

    qid, hits = search()
    if not hits:
        print("no hits"); return 1
    hit = hits[0]
    fname = hit["filename"]
    size_mb = (hit.get("filesize") or 0) / 1e6
    print(f"[3/5] search ok: {fname} ({size_mb:.1f} MB)")
    print(f"      hit keys: {sorted(hit.keys())}")

    os.makedirs(args.out, exist_ok=True)
    zpath = os.path.join(args.out, fname)

    # ---- download with 401 backoff + re-search, one connection at a time ----
    print(f"[4/5] downloading -> {zpath}")
    t0 = time.time()
    done = False
    for attempt in range(1, 4):
        link = f"{API}/download/{fname}?id={qid}"
        resp = requests.get(link, headers=auth, stream=True, timeout=1800)
        if resp.status_code == 401:
            resp.close()
            print(f"      attempt {attempt}: 401 (token not live yet); "
                  "waiting 5s then re-searching")
            time.sleep(5)
            qid, _ = search()
            continue
        if resp.status_code == 429:
            resp.close()
            print(f"      attempt {attempt}: 429 concurrent-download limit; waiting 30s")
            time.sleep(30)
            continue
        if resp.status_code != 200:
            body = resp.text[:200]; resp.close()
            print(f"      attempt {attempt}: HTTP {resp.status_code}: {body}")
            return 1
        got = 0
        with open(zpath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk); got += len(chunk)
        resp.close()
        done = True
        print(f"      downloaded {got/1e6:.1f} MB in {time.time()-t0:.0f}s")
        break
    if not done:
        print("      download did not succeed after retries"); return 1

    # ---- inspect: what is actually in the tile? ----
    print("[5/5] inspecting archive")
    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        print("      NOT a zip — first bytes:", open(zpath, "rb").read(60)); return 1
    names = [n for n in zf.namelist() if not n.endswith("/")]
    print(f"      {len(names)} member(s):")
    for n in names[:10]:
        print(f"        {n}  ({zf.getinfo(n).file_size/1e6:.1f} MB uncompressed)")

    csvs = [n for n in names if n.lower().endswith(".csv")]
    target = csvs[0] if csvs else (names[0] if names else None)
    if target:
        print(f"\n      --- structure of {target} ---")
        with zf.open(target) as fh:
            header = fh.readline().decode("utf-8", "replace").strip()
            cols = header.split(",")
            print(f"      column count: {len(cols)}")
            print(f"      first 18 cols: {cols[:18]}")
            print(f"      last 5 cols  : {cols[-5:]}")
            for i in range(2):
                row = fh.readline().decode("utf-8", "replace").strip()
                if not row:
                    break
                vals = row.split(",")
                print(f"      sample row {i+1} (first 18): {vals[:18]}")
        # flag the coordinate convention explicitly
        low = [x.lower() for x in cols]
        if any("easting" in x or "northing" in x for x in low):
            print("\n      >>> COORDS ARE EPSG:3035 easting/northing "
                  "(wrap_egms needs a reprojection step)")
        elif any(x in ("lon", "longitude", "lat", "latitude") for x in low):
            print("\n      >>> COORDS ARE lon/lat (wrap_egms works as-is)")
        else:
            print("\n      >>> coordinate columns unclear — see header above")

    if not args.keep:
        os.remove(zpath)
        print(f"\n      removed {zpath} (use --keep to retain)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
