#!/usr/bin/env python3
"""clms_diagnostic.py — step-by-step CLMS live check (M4.1b).

The CLMS live path can't be tested in the author's sandbox (no account/net), so
this isolates each step with your real key on your box. Run stages incrementally
so a failure points at ONE thing, not the whole chain.

Usage (inside the downloader image, key mounted):
  # 1. auth only:
  python clms_diagnostic.py --key /run/secrets/clms_key.json auth
  # 2. auth + list ALL EGMS datasets (pick the right L2b UID):
  python clms_diagnostic.py --key ... discover
  # 3. full round-trip for a TINY Paris AOI (real request -> poll -> download):
  python clms_diagnostic.py --key ... request --bbox-order example
  #    if that returns an error/empty, try the other ordering:
  python clms_diagnostic.py --key ... request --bbox-order nesw

The two --bbox-order values resolve the documented contradiction:
  example : [minlon, maxlat, maxlon, minlat]   (from the docs' worked example)
  nesw    : [maxlat, maxlon, minlat, minlon]   (from the docs' prose "[N,E,S,W]")
Whichever returns Finished_ok with non-empty tiles is the correct one; put it in
EGMS_BBOX_ORDER and we're done.
"""
from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

# tiny Paris AOI (well inside EGMS coverage), ~2 km box
PARIS_BBOX = {"minlon": 2.33, "maxlon": 2.36, "minlat": 48.85, "maxlat": 48.87}


def _bbox(order: str) -> list[float]:
    b = PARIS_BBOX
    if order == "example":
        return [b["minlon"], b["maxlat"], b["maxlon"], b["minlat"]]
    return [b["maxlat"], b["maxlon"], b["minlat"], b["minlon"]]  # nesw


def _ms(y, m, d):
    from datetime import datetime, timezone
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["auth", "discover", "request"])
    ap.add_argument("--key", default="/run/secrets/clms_key.json")
    ap.add_argument("--base", default="https://land.copernicus.eu")
    ap.add_argument("--bbox-order", default="example", choices=["example", "nesw"])
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    from clms_client import ClmsClient, ClmsError
    try:
        client = ClmsClient.from_key_file(args.key, base_url=args.base)
    except ClmsError as e:
        print(f"KEY LOAD FAILED: {e}")
        return 2

    # ---- stage 1: auth ----
    print("== AUTH ==")
    try:
        client._mint_access_token()
        print(f"OK: minted access token (len={len(client._access_token or '')}), "
              f"expires ~{int(client._token_exp - time.time())}s")
    except Exception as e:  # noqa: BLE001
        print(f"AUTH FAILED: {e}")
        return 1
    if args.stage == "auth":
        return 0

    # ---- stage 2: discover — list ALL egms datasets so we can pick L2b ----
    print("\n== DISCOVER (all EGMS datasets in catalog) ==")
    path = ("/api/@search?portal_type=DataSet&metadata_fields=UID"
            "&metadata_fields=dataset_download_information&b_size=100")
    found = []
    url, scanned = path, 0
    while url:
        r = client._get(url)
        if r.status_code != 200:
            print(f"@search failed ({r.status_code}): {r.text[:200]}"); return 1
        data = r.json()
        for it in data.get("items", []):
            t = (it.get("title") or "")
            if "egms" in t.lower() or "ground motion" in t.lower():
                dl = it.get("dataset_download_information", {}).get("items", [])
                found.append((t, it.get("UID"), [d.get("@id") for d in dl]))
        scanned += len(data.get("items", []))
        url = data.get("batching", {}).get("next")
    print(f"scanned {scanned} datasets; EGMS-like matches:")
    for t, uid, dls in found:
        print(f"  - {t}\n      UID={uid}  download_ids={dls}")
    if not found:
        print("  (none — the catalog title may differ; paste this output and we adjust)")
        return 1
    if args.stage == "discover":
        print("\nPick the L2b/Basic entry; set EGMS_DATASET_UID + EGMS_DOWNLOAD_INFO_ID "
              "to skip auto-discovery if needed.")
        return 0

    # ---- stage 3: real request -> poll -> download (tiny Paris AOI) ----
    print(f"\n== REQUEST (bbox-order={args.bbox_order}) ==")
    bbox = _bbox(args.bbox_order)
    print(f"BoundingBox sent: {bbox}")
    try:
        task_id = client.request_download(
            bbox_nesw=bbox, start_ms=_ms(2018, 1, 1), end_ms=_ms(2021, 12, 31),
            output_format="GeoJSON")
        print(f"OK: task submitted, TaskID={task_id}")
        url = client.poll_until_ready(task_id, emit=lambda p, m: print(f"  …{m}"),
                                      timeout_s=args.timeout)
        print(f"OK: Finished_ok, DownloadURL={url[:80]}…")
        out = Path("/tmp/clms_diag_out"); out.mkdir(exist_ok=True)
        zp = client.download_zip(url, out)
        with zipfile.ZipFile(zp) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            zf.extractall(out)
        print(f"OK: downloaded + extracted {len(names)} file(s): {names[:6]}")
        if not names:
            print("WARNING: zip is EMPTY — likely the WRONG bbox order or AOI over "
                  "no measurement points. Try --bbox-order "
                  f"{'nesw' if args.bbox_order=='example' else 'example'}.")
            return 1
        print("\nSUCCESS: this bbox order works. Set "
              f"EGMS_BBOX_ORDER={args.bbox_order} in the downloader env.")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"REQUEST FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
