# M4.1b — EGMS live CLMS download

Lights up the one part 4.1a couldn't test: the live CLMS auth + async download.
Built against the official docs (eea.github.io/clms-api-docs), fetched into
context. clms_client.py was already solid; this slice adds a step-by-step live
diagnostic and resolves the one real ambiguity (bounding-box order).

## The one known unknown: BoundingBox order
The CLMS docs CONTRADICT themselves — prose says [N,E,S,W]; the worked example
decodes to [minlon,maxlat,maxlon,minlat]. EGMS_BBOX_ORDER picks which (default
'example'). The diagnostic resolves it empirically.

## Operator setup (once)
1. Save your CLMS service-key JSON at ./secrets/clms_key.json
2. Mount it into the downloader (compose): 
     services.downloader.volumes:  ./secrets/clms_key.json:/run/secrets/clms_key.json:ro
   and set (downloader environment):  CLMS_SERVICE_KEY=/run/secrets/clms_key.json
3. Rebuild: docker compose build downloader

## Bring it up incrementally (isolate any failure to ONE step)
    # 1. AUTH only — proves the JWT/token exchange with your key:
    docker compose run --rm --entrypoint python downloader \
      clms_diagnostic.py --key /run/secrets/clms_key.json auth

    # 2. DISCOVER — lists ALL EGMS datasets so we pick the right L2b UID:
    docker compose run --rm --entrypoint python downloader \
      clms_diagnostic.py --key /run/secrets/clms_key.json discover

    # 3. FULL round-trip on a tiny Paris AOI (real request->poll->download):
    docker compose run --rm --entrypoint python downloader \
      clms_diagnostic.py --key /run/secrets/clms_key.json request --bbox-order example
    #    if the zip comes back EMPTY or errors, try the other order:
    docker compose run --rm --entrypoint python downloader \
      clms_diagnostic.py --key /run/secrets/clms_key.json request --bbox-order nesw

    # whichever order yields non-empty tiles -> set in downloader env:
    #    EGMS_BBOX_ORDER=example   (or nesw)

## Then the real end-to-end
    docker compose up -d
    # a Paris query now flows: EGMS tier -> CLMS download -> wrap_egms -> answer
    # (paste the diagnostic output for each stage; we iterate on whatever the
    #  live API does differently from the docs — expected for a first contact)

## What I expect may need iteration (all live-only)
- BoundingBox order (above) — the diagnostic settles it.
- EGMS dataset title match: discover prints all EGMS datasets; if auto-match
  picks wrong, set EGMS_DATASET_UID + EGMS_DOWNLOAD_INFO_ID explicitly.
- OutputFormat: 'GeoJSON' vs 'Geojson' spelling, or EGMS native CSV — wrap_egms
  reads both, but the request format string may need tweaking.
- Poll status strings / DownloadURL field name under real responses.
# M4.1b — EGMS live download (via the real EGMS archive API)

## What changed, and why (the pivot)
M4.1b was originally built against the CLMS `@datarequest_post` / FME flow.
**That flow does not serve EGMS.** Verified live on the box: every EGMS dataset
returns `dataset_download_information.items == []`, even fetched by UID with
`fullobjects=1`. It serves Land Cover / CLC / HRL, not EGMS.

The real distribution is the **EGMS archive API**, documented by the official
Copernicus notebook (github.com/copernicus-land/egms-api):

    https://egms.land.copernicus.eu/insar-api/archive
      GET  /levels /releases /product_types /tile_ids
      POST /search   {bbox, levels, releases, productType} -> {hits[], id}
      GET  /download/{filename}?id={query_id}

Crucially the `?id=` is **not a credential** — it is the QUERY id from /search.
Auth is the same CLMS JWT service key we already had working, sent as
`Authorization: Bearer`. So EGMS is **fully automated**; no hand-pasted token.

## Live behaviours handled (all observed empirically, not assumed)
- The download link is not valid the instant /search returns: the first GET can
  401 "rerun your search". We retry with backoff and re-search. (Seen: 2x401
  then success.)
- The server allows **at most 2 concurrent downloads** (429 otherwise) — we back
  off and retry rather than failing the task.
- Newest release is read from /releases (currently 2020-2024), not hard-coded.

## Product choice: L3 ORTHO-UP (a real improvement)
Switched from L2b to **L3 ORTHO-UP**:
- L2b needs the Sentinel-1 burst-ID map (heavy aux data) and is line-of-sight.
- L3 is a regular 100 m EPSG:3035 grid, no aux data, and is the **vertical
  (up-down)** component — so the answer reports genuine vertical motion and
  DROPS the "LOS cannot separate vertical from horizontal" caveat that weakens
  every LiCSBAS answer. East-west is a separate product (ORTHO-EAST), not
  fetched.

## Real file structure (read off a live tile, not guessed)
    EGMS_L3_E37N28_100km_U_2020_2024_1.csv — 315 cols, ~1M rows, 340 MB
      pid, easting, northing, height_ortho, rmse_ts, mean_velocity,
      mean_velocity_std, acceleration*, seasonality*, gnss_velocity_n/e/u,
      + ~301 epoch columns YYYYMMDD (6-day cadence)
Consequences, all implemented:
- Coordinates are **EPSG:3035 easting/northing**, not lon/lat -> the AOI is
  projected ONCE into 3035 and points filtered there (never reproject 1M points).
- 340 MB is streamed in chunks reading only the 4 needed columns.
- `mean_velocity_std` gives the SAME significance test as LiCSBAS
  (|v| > 1.96 sigma), so "significant" means one thing across methods.

## Files
- `services/downloader/egms_api.py`   NEW — archive API client (search/download,
  401 + 429 handling, bbox helper with the 5-degree cap).
- `services/downloader/run_egms.py`   REWRITTEN — search -> download -> unzip ->
  cache. Returns `str(out_dir)` (matches run_cdse; the caller puts it straight
  into ResultMessage.result_json_path).
- `services/downloader/clms_client.py` TRIMMED to the auth core; the dead
  @datarequest_post request/poll/download code was removed rather than left to
  mislead.
- `services/worker/wrappers/wrap_egms.py` REWRITTEN for L3 (see above).
- `services/worker/requirements.txt`  +pyproj (EPSG:3035 transforms).
- `services/downloader/egms_fetch_one.py` diagnostic kept in the image.

## Verified in-sandbox (passing)
- wrap_egms on a synthetic tile mirroring the REAL 315-column header:
  points seeded OUTSIDE the AOI (+20 mm/yr) are correctly excluded, so the mean
  is the true inside signal (-2.48), proving EPSG:3035 subsetting; component =
  vertical; LOS caveat absent; date_coverage from real epochs; significance from
  mean_velocity_std.
- run_egms against a mocked archive API: returns a str, ResultMessage validates,
  cache registered with product_type='egms', zip cleaned up, progress 3->100.

## Deploy
    cd /geo && tar xzf geohazard-chat-m4.1b.tar.gz
    docker compose up --build -d downloader worker
    # then a real Paris query end-to-end:
    curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
      -d '{"question":"is the ground sinking here?","aoi":{"type":"Polygon","coordinates":[[[2.31,48.83],[2.39,48.83],[2.39,48.89],[2.31,48.89],[2.31,48.83]]]},"depth":"quick"}'
    docker compose logs -f downloader worker

Expect: router -> EGMS tier 1, downloader search+fetch (~81 MB, ~15-30 s),
unzip, worker streams the CSV and answers with VERTICAL velocity.

## Known / backlog
- **Tile-level caching**: the cache is keyed by AOI hash, so two different Paris
  AOIs each re-download the same 81 MB tile. Keying by tile filename would be
  much better. Added to BACKLOG next to the LiCSBAS interferogram cache.
- **Concurrency**: EGMS allows only 2 simultaneous downloads; DOWNLOAD_CONCURRENCY
  is 2, so EGMS + anything else can hit 429 (handled by retry, but worth a
  dedicated semaphore later).
- The 340 MB CSV parse is the slow step (~20-40 s); the zip also ships a 4 MB
  GeoTIFF that could serve the map artifact far more cheaply.
