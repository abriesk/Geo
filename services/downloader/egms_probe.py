#!/usr/bin/env python3
"""egms_probe.py — probe the REAL EGMS distribution endpoint (M4.1b pivot).

WHY THIS EXISTS
The CLMS @datarequest_post flow does NOT serve EGMS: every EGMS dataset returns
dataset_download_information.items == [] (verified live on this box). EGMS has
its own distribution — the EGMS Explorer — with a different endpoint and,
apparently, a different credential:

    https://egms.land.copernicus.eu/insar-api/archive/download/{FILE}.zip?id={TOKEN}

(endpoint + naming read from the EGMS-toolkit source, github.com/alexisInSAR/
EGMStoolkit, which is the published/peer-reviewed access route.)

The open question this probe answers: WHICH credential does that endpoint want?
We already proved the CLMS JWT service key mints a valid access token. If that
token also works here, EGMS stays fully automated. If it does not, EGMS needs a
temporary token copied from the EGMS Explorer website by hand — a materially
different operational story that we must know about before building on it.

L3 TILE NAMING (verified against the toolkit's own worked example: the Dublin
ROI yields EGMS_L3_E32N34_100km_U):
    EGMS_L3_E{floor(easting/1e5)}N{floor(northing/1e5)}_100km_{U|E}{rel}.zip
    with EPSG:3035 (LAEA Europe) coordinates, and release suffix:
        2015_2021 -> ''      2018_2022 -> '_2018_2022_1'      2019_2023 -> '_2019_2023_1'
Paris (2.35E, 48.86N) -> EPSG:3035 x=3760650 y=2889878 -> E37N28.

Usage (inside the downloader image):
    python egms_probe.py                        # try all auth strategies, Paris tile
    python egms_probe.py --token XXXX           # also try an EGMS Explorer token
    python egms_probe.py --tile E32N34          # a different tile
"""
from __future__ import annotations

import argparse
import sys

BASE = "https://egms.land.copernicus.eu/insar-api/archive/download"
RELEASES = {"2015_2021": "", "2018_2022": "_2018_2022_1", "2019_2023": "_2019_2023_1"}


def l3_name(tile: str, component: str, release: str) -> str:
    return f"EGMS_L3_{tile}_100km_{component}{RELEASES[release]}.zip"


def _describe(r) -> str:
    ct = r.headers.get("Content-Type", "?")
    cl = r.headers.get("Content-Length", "?")
    head = r.content[:4] if r.content else b""
    kind = "ZIP!" if head[:2] == b"PK" else ("html/json" if head[:1] in (b"<", b"{") else repr(head))
    return f"HTTP {r.status_code} | {ct} | len={cl} | first-bytes={kind}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tile", default="E37N28", help="L3 tile, e.g. E37N28 (Paris)")
    ap.add_argument("--component", default="U", choices=["U", "E"])
    ap.add_argument("--release", default="2018_2022", choices=list(RELEASES))
    ap.add_argument("--token", default=None, help="temporary EGMS Explorer token")
    ap.add_argument("--key", default="/run/secrets/clms_key.json")
    args = ap.parse_args()

    import requests

    fname = l3_name(args.tile, args.component, args.release)
    url = f"{BASE}/{fname}"
    print(f"target file : {fname}")
    print(f"target url  : {url}\n")

    # Mint the CLMS JWT access token we already proved works.
    jwt_token = None
    try:
        from clms_client import ClmsClient
        c = ClmsClient.from_key_file(args.key)
        c._mint_access_token()
        jwt_token = c._access_token
        print(f"[ok] CLMS JWT access token minted (len={len(jwt_token or '')})\n")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not mint CLMS token ({e}); skipping JWT strategies\n")

    strategies = [("A. no auth at all", {}, {})]
    if jwt_token:
        strategies += [
            ("B. CLMS JWT as ?id=", {"id": jwt_token}, {}),
            ("C. CLMS JWT as Bearer header", {}, {"Authorization": f"Bearer {jwt_token}"}),
        ]
    if args.token:
        strategies += [
            ("D. EGMS Explorer token as ?id=", {"id": args.token}, {}),
        ]

    ok_any = False
    for label, params, headers in strategies:
        try:
            # Range-limit so we never pull a whole tile just to test auth.
            h = {"Range": "bytes=0-2047", **headers}
            r = requests.get(url, params=params, headers=h, timeout=90,
                             allow_redirects=True)
            print(f"{label}\n    {_describe(r)}")
            if r.status_code in (200, 206) and r.content[:2] == b"PK":
                print("    ^^ THIS ONE WORKS (real zip bytes)")
                ok_any = True
            elif r.content[:1] in (b"<", b"{"):
                print(f"    body: {r.text[:180].strip()}")
        except Exception as e:  # noqa: BLE001
            print(f"{label}\n    ERROR: {e}")
        print()

    if not ok_any:
        print("No strategy returned zip bytes.")
        print("If A-C all failed and you have not passed --token, get a temporary")
        print("token: open https://egms.land.copernicus.eu , start any download,")
        print("and copy the value after '?id=' at the end of the download link.")
        print("Then re-run:  python egms_probe.py --token <THAT_VALUE>")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
