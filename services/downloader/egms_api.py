"""EGMS archive API client — M4.1b.

The EGMS products are NOT served by the CLMS @datarequest_post / FME flow (every
EGMS dataset reports an empty dataset_download_information.items — verified
live). They have their own archive API:

    https://egms.land.copernicus.eu/insar-api/archive

documented by the official Copernicus notebook at
github.com/copernicus-land/egms-api. Flow, all verified live on a real box:

    1. Mint a CLMS access token from the service key (same JWT flow as the rest
       of CLMS — clms_client owns this and is reused here unchanged).
    2. GET /levels /releases /product_types  — discovery.
    3. POST /search {bbox, levels, releases, productType} -> {hits[], id}
       where each hit carries a `filename`, and `id` is a QUERY id.
    4. GET /download/{filename}?id={query_id}

Two live behaviours this client handles, both observed empirically:

  * The download token is not valid the instant a search returns. The first GET
    can 401 with "rerun your search to obtain new links". Retry with a short
    backoff, re-searching for a fresh query id. (Observed: 2 x 401 then success.)
  * The server permits AT MOST 2 CONCURRENT DOWNLOADS, answering 429 otherwise.
    We back off and retry rather than failing the task.

Note the `?id=` is a query/session id, NOT a credential — the Authorization
header is what authenticates. Sending the Bearer header on the download is
harmless (verified) and we keep it for consistency.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

import requests

API_BASE = os.environ.get(
    "EGMS_API_BASE", "https://egms.land.copernicus.eu/insar-api/archive"
)

# Defaults chosen for the "is the ground moving here?" question:
#   L3 / ORTHO-UP is the ortho-rectified VERTICAL (up-down) component on a
#   regular 100 m grid. Unlike the L2 line-of-sight products it needs no
#   burst-ID map, and reporting genuine vertical motion lets the answer drop the
#   "LOS cannot separate vertical from horizontal" caveat entirely.
DEFAULT_LEVEL = os.environ.get("EGMS_LEVEL", "L3")
DEFAULT_PRODUCT_TYPE = os.environ.get("EGMS_PRODUCT_TYPE", "ORTHO-UP")

# CLMS caps the search bbox at 5 degrees per side.
MAX_BBOX_DEG = 5.0


class EgmsError(RuntimeError):
    """EGMS archive API error with an actionable message."""


class EgmsClient:
    def __init__(self, clms_client, base_url: str = API_BASE):
        self._clms = clms_client
        self._base = base_url.rstrip("/")
        self._s = requests.Session()

    @classmethod
    def from_key_file(cls, path: str, base_url: str = API_BASE) -> "EgmsClient":
        from clms_client import ClmsClient
        return cls(ClmsClient.from_key_file(path), base_url=base_url)

    # ---- auth -------------------------------------------------------------
    def _headers(self, accept_json: bool = True) -> dict:
        # Delegates token minting/refresh to the proven clms_client logic.
        h = dict(self._clms._auth_header())
        if not accept_json:
            h.pop("Accept", None)
        return h

    # ---- discovery --------------------------------------------------------
    def _get_list(self, endpoint: str) -> list:
        r = self._s.get(f"{self._base}/{endpoint}", headers=self._headers(), timeout=90)
        if r.status_code != 200:
            raise EgmsError(f"EGMS /{endpoint} failed ({r.status_code}): {r.text[:200]}")
        val = r.json()
        if not isinstance(val, list):
            raise EgmsError(f"EGMS /{endpoint} returned unexpected payload: {val!r}")
        return val

    def newest_release(self) -> str:
        """Latest release string, e.g. '2020-2024'. Releases are returned in
        ascending order; we take the last rather than hard-coding, so a new EGMS
        release is picked up automatically."""
        rel = self._get_list("releases")
        if not rel:
            raise EgmsError("EGMS reported no releases")
        return rel[-1]

    # ---- search -----------------------------------------------------------
    def search(self, bbox_lonlat: list, level: str = DEFAULT_LEVEL,
               product_type: Optional[str] = DEFAULT_PRODUCT_TYPE,
               release: Optional[str] = None) -> tuple[str, list]:
        """bbox_lonlat is [[min_lon, min_lat], [max_lon, max_lat]].
        Returns (query_id, hits)."""
        release = release or self.newest_release()
        query = {
            "id": None,
            "bbox": bbox_lonlat,
            "levels": [level],
            "releases": [release],
        }
        if product_type:
            query["productType"] = product_type
        r = self._s.post(f"{self._base}/search", headers=self._headers(),
                         data=json.dumps(query), timeout=120)
        if r.status_code != 200:
            raise EgmsError(f"EGMS search failed ({r.status_code}): {r.text[:250]}")
        body = r.json()
        if body.get("message") and not body.get("hits"):
            raise EgmsError(f"EGMS search rejected the query: {body['message']}")
        return body.get("id"), (body.get("hits") or [])

    # ---- download ---------------------------------------------------------
    def download(self, filename: str, query_id: str, dest: Path,
                 emit: Optional[Callable[[int, str], None]] = None,
                 research: Optional[Callable[[], tuple[str, list]]] = None,
                 max_attempts: int = 6) -> Path:
        """Stream one product zip to `dest`. Handles the 401 token-not-live-yet
        and 429 concurrency responses by backing off and retrying."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        qid = query_id
        for attempt in range(1, max_attempts + 1):
            url = f"{self._base}/download/{filename}?id={qid}"
            resp = self._s.get(url, headers=self._headers(accept_json=False),
                               stream=True, timeout=1800)
            try:
                if resp.status_code == 401:
                    # Download token not live yet; a fresh search re-arms it.
                    if emit:
                        emit(25, "waiting for EGMS download link to become valid")
                    time.sleep(min(5 * attempt, 20))
                    if research is not None:
                        qid, _ = research()
                    continue
                if resp.status_code == 429:
                    # At most 2 concurrent downloads server-side.
                    if emit:
                        emit(25, "EGMS busy (concurrent-download limit); waiting")
                    time.sleep(min(30 * attempt, 120))
                    continue
                if resp.status_code != 200:
                    raise EgmsError(
                        f"EGMS download failed ({resp.status_code}): {resp.text[:250]}")
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                last_pct = -1
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        f.write(chunk)
                        got += len(chunk)
                        if emit and total:
                            # map bytes onto a 30..70 progress band
                            pct = 30 + int(40 * got / total)
                            if pct != last_pct:
                                emit(pct, f"downloading EGMS tile "
                                          f"({got/1e6:.0f}/{total/1e6:.0f} MB)")
                                last_pct = pct
                return dest
            finally:
                resp.close()
        raise EgmsError(
            f"EGMS download of {filename} did not succeed after {max_attempts} "
            "attempts (download link kept expiring or the server stayed busy)."
        )


def aoi_to_bbox_lonlat(aoi) -> list:
    """AOI polygon -> [[min_lon, min_lat], [max_lon, max_lat]] for /search.

    NOTE this is a plain lon/lat pair-of-corners format — the contradictory
    [N,E,S,W] ordering in the CLMS @datarequest_post docs does NOT apply to the
    EGMS archive API.
    """
    ring = aoi.coordinates[0] if hasattr(aoi, "coordinates") else aoi["coordinates"][0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    if (max(lons) - min(lons)) > MAX_BBOX_DEG or (max(lats) - min(lats)) > MAX_BBOX_DEG:
        raise EgmsError(
            f"AOI spans more than {MAX_BBOX_DEG} degrees, which the EGMS search "
            "API rejects. Draw a smaller area."
        )
    return [[min(lons), min(lats)], [max(lons), max(lats)]]
