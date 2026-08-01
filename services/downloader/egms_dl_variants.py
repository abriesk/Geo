#!/usr/bin/env python3
"""egms_dl_variants.py — find the exact download-request shape EGMS accepts.

Search works and returns a query id, but GET /download/{file}?id={id} returned
401 "You need a valid download token". The likely culprits, in order:

  1. The Authorization: Bearer header on the DOWNLOAD request. The official
     notebook hands the link to curl/wget, which send NO auth header — and a
     browser-scraped id worked for us with no header. The server may reject a
     link when an unexpected Bearer token is also present.
  2. Session/cookie affinity: the search POST may set a cookie that the
     download must carry. Module-level requests.get() shares no cookie jar with
     the earlier post; a requests.Session() does.
  3. The Accept: application/json header (download returns a zip).

This runs ONE fresh search, then tries the download several ways and reports
which shape returns zip bytes. Uses stream=True and reads only the first chunk,
so no variation pulls the full ~80 MB.
"""
from __future__ import annotations

import argparse
import json
import sys

API = "https://egms.land.copernicus.eu/insar-api/archive"
PARIS_BBOX = [[2.31, 48.83], [2.39, 48.89]]


def peek(resp) -> tuple[bool, str]:
    """Read only the first chunk; return (is_zip, description)."""
    try:
        chunk = next(resp.iter_content(chunk_size=2048), b"")
    except Exception as e:  # noqa: BLE001
        return False, f"stream error: {e}"
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    is_zip = chunk[:2] == b"PK"
    desc = (f"HTTP {resp.status_code} | {resp.headers.get('Content-Type')} | "
            f"len={resp.headers.get('Content-Length')} | "
            f"{'ZIP!' if is_zip else repr(chunk[:80])}")
    return is_zip, desc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="/run/secrets/clms_key.json")
    ap.add_argument("--level", default="L3")
    ap.add_argument("--product-type", default="ORTHO-UP")
    ap.add_argument("--release", default=None)
    args = ap.parse_args()

    import requests

    from clms_client import ClmsClient
    c = ClmsClient.from_key_file(args.key)
    c._mint_access_token()
    token = c._access_token
    auth = {"Authorization": f"Bearer {token}"}
    print(f"[ok] access token minted (len={len(token or '')})")

    # newest release unless told otherwise
    release = args.release
    if not release:
        rr = requests.get(f"{API}/releases", headers={**auth, "Accept": "application/json"},
                          timeout=60)
        release = (rr.json() or ["2020-2024"])[-1]
    print(f"[ok] release = {release}")

    query = {"id": None, "bbox": PARIS_BBOX, "levels": [args.level],
             "releases": [release]}
    if args.product_type:
        query["productType"] = args.product_type

    # A shared session for the variants that need cookie affinity.
    sess = requests.Session()
    sess.headers.update(auth)

    def do_search(via_session: bool):
        h = {**auth, "Accept": "application/json"}
        if via_session:
            r = sess.post(f"{API}/search", data=json.dumps(query), timeout=120)
        else:
            r = requests.post(f"{API}/search", headers=h,
                              data=json.dumps(query), timeout=120)
        j = r.json()
        return j.get("id"), (j.get("hits") or [])

    print("\n== fresh search (module-level) ==")
    qid, hits = do_search(via_session=False)
    if not hits:
        print("no hits; aborting"); return 1
    fname = hits[0]["filename"]
    print(f"  id={qid}\n  file={fname} ({hits[0].get('filesize')} bytes)")
    link = f"{API}/download/{fname}?id={qid}"

    variants = [
        ("1. NO auth header, no extras (curl-like)",
         lambda: requests.get(link, stream=True, timeout=180)),
        ("2. NO auth header + Range",
         lambda: requests.get(link, headers={"Range": "bytes=0-2047"},
                              stream=True, timeout=180)),
        ("3. WITH auth header, no Accept/Range",
         lambda: requests.get(link, headers=auth, stream=True, timeout=180)),
        ("4. WITH auth + Accept: application/json (what failed before)",
         lambda: requests.get(link, headers={**auth, "Accept": "application/json"},
                              stream=True, timeout=180)),
    ]
    winner = None
    for label, fn in variants:
        try:
            ok, desc = peek(fn())
            print(f"\n{label}\n    {desc}")
            if ok and winner is None:
                winner = label
                print("    ^^ WORKS")
        except Exception as e:  # noqa: BLE001
            print(f"\n{label}\n    ERROR: {e}")

    # Session-affinity variants: search and download on the SAME session.
    print("\n== fresh search (shared Session) ==")
    try:
        qid2, hits2 = do_search(via_session=True)
        print(f"  id={qid2}")
        if hits2:
            link2 = f"{API}/download/{hits2[0]['filename']}?id={qid2}"
            for label, fn in [
                ("5. same Session, session default headers (incl. auth)",
                 lambda: sess.get(link2, stream=True, timeout=180)),
                ("6. same Session, auth header stripped for download",
                 lambda: sess.get(link2, headers={"Authorization": None},
                                  stream=True, timeout=180)),
            ]:
                try:
                    ok, desc = peek(fn())
                    print(f"\n{label}\n    {desc}")
                    if ok and winner is None:
                        winner = label
                        print("    ^^ WORKS")
                except Exception as e:  # noqa: BLE001
                    print(f"\n{label}\n    ERROR: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"  session search failed: {e}")

    print("\n" + "=" * 60)
    if winner:
        print(f"WINNER: {winner}\nBuild run_egms with exactly this request shape.")
        return 0
    print("No variant returned zip bytes. Paste this output; next hypotheses are\n"
          "a per-file download token in the hit object, or a separate\n"
          "/download-token style endpoint.")
    print("Hit keys available were:", list(hits[0].keys()))
    return 1


if __name__ == "__main__":
    sys.exit(main())
