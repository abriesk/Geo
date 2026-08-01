"""EGMS download tier — M4.1b.

Fetches the EGMS L3 ORTHO-UP (vertical) product covering the AOI from the EGMS
archive API and unpacks it into the shared download cache, exactly like the CDSE
tier does for Sentinel-2. wrap_egms then reads the CSV from that directory.

History worth keeping: the first implementation targeted the CLMS
@datarequest_post flow. That flow does not serve EGMS at all — every EGMS
dataset returns an empty dataset_download_information.items, verified live. The
real distribution is the EGMS archive API (see egms_api.py).
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

from geohazard_contracts import DownloadTaskMessage

ARCHIVE_ROOT = Path(os.environ.get("ARCHIVE_ROOT", "/data/archive"))
CLMS_SERVICE_KEY = os.environ.get("CLMS_SERVICE_KEY", "/run/secrets/clms_key.json")


def _log(msg: str) -> None:
    print(f"[downloader][egms] {msg}", flush=True)


def run_egms(task: DownloadTaskMessage, emit, upsert_cache) -> dict:
    """Download the EGMS tile(s) covering the AOI.

    emit(pct, msg) reports progress; upsert_cache(task, out_dir, paths,
    product_type) registers the result in the §6.2 cache.
    """
    from egms_api import EgmsClient, EgmsError, aoi_to_bbox_lonlat

    if not Path(CLMS_SERVICE_KEY).exists():
        raise RuntimeError(
            f"CLMS service key not found at {CLMS_SERVICE_KEY}. Save the service-key "
            "JSON from the CLMS website there (and mount it into the downloader) to "
            "enable the EGMS tier, or set EGMS_ENABLED=false to always use LiCSBAS."
        )

    emit(3, "authenticating with Copernicus Land (CLMS)")
    client = EgmsClient.from_key_file(CLMS_SERVICE_KEY)

    bbox = aoi_to_bbox_lonlat(task.aoi)
    level = os.environ.get("EGMS_LEVEL", "L3")
    product_type = os.environ.get("EGMS_PRODUCT_TYPE", "ORTHO-UP")

    emit(8, "searching the EGMS archive")

    def _search():
        return client.search(bbox, level=level, product_type=product_type)

    query_id, hits = _search()
    if not hits:
        raise EgmsError(
            "The EGMS archive returned no products for this area. EGMS covers the "
            "EU plus Norway, UK, Iceland and Switzerland; outside that footprint "
            "the LiCSBAS path should be used instead."
        )
    release = hits[0].get("release") or "?"
    _log(f"{len(hits)} product(s), release={release}, "
         f"level={level}, type={product_type}")

    out_dir = ARCHIVE_ROOT / task.aoi.hash() / f"{task.dates.start}_{task.dates.end}"
    out_dir.mkdir(parents=True, exist_ok=True)

    extracted: list[str] = []
    for i, hit in enumerate(hits, start=1):
        fname = hit["filename"]
        size_mb = (hit.get("filesize") or 0) / 1e6
        emit(20, f"fetching EGMS tile {i}/{len(hits)} ({size_mb:.0f} MB)")
        _log(f"downloading {fname} ({size_mb:.1f} MB)")
        zpath = out_dir / fname
        # Skip a re-download if we already hold a plausible copy (the tile is
        # large and the archive is immutable per release/sub_release).
        if zpath.exists() and hit.get("filesize") and zpath.stat().st_size == hit["filesize"]:
            _log(f"already present, skipping download: {fname}")
        else:
            client.download(fname, query_id, zpath, emit=emit, research=_search)

        emit(75, "unpacking EGMS tile")
        try:
            with zipfile.ZipFile(zpath) as zf:
                for member in zf.namelist():
                    if member.endswith("/"):
                        continue
                    # The CSV carries the point data; the tiff/xml are extras we
                    # keep for provenance but do not parse.
                    zf.extract(member, out_dir)
                    extracted.append(str(out_dir / member))
        except zipfile.BadZipFile as e:
            raise EgmsError(
                f"EGMS returned a file that is not a valid zip ({fname}): {e}"
            ) from e
        # The zip is redundant once extracted and each one is ~80 MB.
        if os.environ.get("EGMS_KEEP_ZIP", "false").lower() != "true":
            try:
                zpath.unlink()
            except OSError:
                pass

    csvs = [p for p in extracted if p.lower().endswith(".csv")]
    if not csvs:
        raise EgmsError(
            f"EGMS archive unpacked but contained no CSV point file "
            f"(members: {[Path(p).name for p in extracted][:5]})"
        )

    emit(92, "updating cache index")
    upsert_cache(task, out_dir, extracted, "egms")
    emit(100, f"download done: {len(csvs)} EGMS file(s)")
    _log(f"ready: {len(csvs)} CSV file(s) in {out_dir}")

    # Must return the output directory as a STRING: the caller puts this
    # straight into ResultMessage.result_json_path, exactly as run_cdse does.
    return str(out_dir)
