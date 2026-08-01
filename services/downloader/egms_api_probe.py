#!/usr/bin/env python3
"""egms_api_probe.py — exercise the OFFICIAL EGMS API (M4.1b, corrected).

WHAT WE LEARNED (and why the earlier probe 401'd)
The EGMS API lives at https://egms.land.copernicus.eu/insar-api/archive and is
documented by the official Copernicus notebook (github.com/copernicus-land/
egms-api). Two corrections to our earlier assumptions:

  1. Auth IS the CLMS JWT we already prove-tested — sent as
     `Authorization: Bearer <access_token>`. There is no separate credential.
  2. The `?id=` on the download URL is NOT a user token. It is the QUERY ID
     returned by a POST /search call. That is why a browser-scraped value
     worked and our JWT-as-?id= did not: we were putting the wrong thing there.

So the real flow is fully automatable:
     mint JWT -> POST /search (bbox+levels) -> GET /download/{filename}?id={query_id}

This probe walks that flow end-to-end and reports each step, so any breakage is
isolated to one call before we wire it into run_egms.

Usage (inside the downloader image, key mounted):
    python egms_api_probe.py
    python egms_api_probe.py --level L3 --product-type ORTHO-UP
    python egms_api_probe.py --no-download        # metadata only, skip byte fetch
"""
from __future__ import annotations

import argparse
import json
import sys

API = "https://egms.land.copernicus.eu/insar-api/archive"

# Paris AOI (well inside EGMS coverage). Format per the official notebook:
#   bbox = [[min_lon, min_lat], [max_lon, max_lat]]   (max 5 degrees per side)
PARIS_BBOX = [[2.31, 48.83], [2.39, 48.89]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="/run/secrets/clms_key.json")
    ap.add_argument("--level", default="L3", help="L2A, L2B or L3")
    ap.add_argument("--product-type", default="ORTHO-UP",
                    help="BASIC, CALIBRATED, ORTHO-UP, ORTHO-EAST")
    ap.add_argument("--release", default=None,
                    help="e.g. 2019-2023; default = newest reported by /releases")
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()

    import requests

    # ---- 1. auth: reuse the JWT machinery we already validated ----
    print("== 1. AUTH (CLMS JWT -> access token) ==")
    try:
        from clms_client import ClmsClient
        c = ClmsClient.from_key_file(args.key)
        c._mint_access_token()
        token = c._access_token
    except Exception as e:  # noqa: BLE001
        print(f"FAILED to mint access token: {e}")
        return 1
    print(f"OK: access token minted (len={len(token or '')})")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ---- 2. discovery: what does the API actually offer? ----
    print("\n== 2. DISCOVERY ==")
    info = {}
    for ep in ("levels", "releases", "product_types", "tile_ids"):
        try:
            r = requests.get(f"{API}/{ep}", headers=headers, timeout=90)
            if r.status_code != 200:
                print(f"  {ep:14s} HTTP {r.status_code}: {r.text[:120]}")
                continue
            val = r.json()
            info[ep] = val
            shown = val[:8] if isinstance(val, list) else val
            more = " ..." if isinstance(val, list) and len(val) > 8 else ""
            print(f"  {ep:14s} {shown}{more}"
                  + (f"   (total {len(val)})" if isinstance(val, list) else ""))
        except Exception as e:  # noqa: BLE001
            print(f"  {ep:14s} ERROR: {e}")

    releases = info.get("releases") or []
    release = args.release or (releases[-1] if releases else None)
    if release is None:
        print("\nNo releases reported; cannot continue to search.")
        return 1
    print(f"\n  using release: {release}")

    # ---- 3. search: AOI bbox -> product filenames + query id ----
    print(f"\n== 3. SEARCH (bbox=Paris, level={args.level}, "
          f"productType={args.product_type}) ==")
    query = {
        "id": None,
        "bbox": PARIS_BBOX,
        "levels": [args.level],
        "releases": [release],
    }
    if args.product_type:
        query["productType"] = args.product_type
    print(f"  query: {json.dumps(query)}")
    try:
        r = requests.post(f"{API}/search", headers=headers,
                          data=json.dumps(query), timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"  SEARCH ERROR: {e}")
        return 1
    print(f"  HTTP {r.status_code}")
    try:
        result = r.json()
    except Exception:  # noqa: BLE001
        print(f"  non-JSON response: {r.text[:300]}")
        return 1
    if result.get("message"):
        print(f"  message: {result['message']}")
    hits = result.get("hits") or []
    query_id = result.get("id")
    print(f"  status={result.get('status')}  hits={len(hits)}  id={query_id}")
    if not hits:
        print("  No hits — try a different level/productType/release combination.")
        print(f"  full response: {json.dumps(result)[:400]}")
        return 1
    for h in hits[:5]:
        print(f"    - {h.get('filename')}  "
              f"({h.get('filesize')} bytes, {h.get('productType')}, "
              f"v{h.get('version', '?')})")

    # ---- 4. download link construction + byte check ----
    print("\n== 4. DOWNLOAD LINK ==")
    fname = hits[0]["filename"]
    link = f"{API}/download/{fname}?id={query_id}"
    print(f"  {link}")
    if args.no_download:
        print("  (--no-download: skipping byte fetch)")
        return 0
    try:
        # Range-limit: prove auth + real zip bytes without pulling ~80 MB.
        rr = requests.get(link, headers={**headers, "Range": "bytes=0-2047"},
                          timeout=180, allow_redirects=True)
        magic = rr.content[:2]
        print(f"  HTTP {rr.status_code} | {rr.headers.get('Content-Type')} | "
              f"len={rr.headers.get('Content-Length')} | "
              f"first-bytes={'ZIP!' if magic == b'PK' else repr(rr.content[:4])}")
        if rr.status_code in (200, 206) and magic == b"PK":
            print("\nSUCCESS: fully automated EGMS download works "
                  "(CLMS JWT -> search -> download). No manual token needed.")
            return 0
        print(f"  body: {rr.text[:250]}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"  DOWNLOAD ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
