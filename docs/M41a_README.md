# M4.1a — EGMS routing + analysis (testable half)

Folds the reviewed EGMS skeleton into the real codebase, fixes the contract
bug, and wires the §3 deformation ladder. The LIVE CLMS download is M4.1b.

## Fixed vs the reviewed tarball
- **date_coverage crash FIXED**: wrap_egms now derives the span from EGMS epoch
  columns (YYYYMMDD) with safe fallbacks — never writes "unknown" (which failed
  the contract's `List[date]` on the common blank-dates case).
- **Router prose -> real tested edits** in main.py (_resolve_deform_tier, dl_key
  so EGMS flows through the existing S2 download machinery, kill switch).
- **Downloader reconciled**: _upsert_cache gains a product_type param (was
  hardcoded 's2'); egms tier dispatch added with a LAZY clms import (downloader
  starts fine without CLMS deps until 4.1b); run_egms keeps its (task, emit,
  upsert_cache) signature — correct for a separate module.
- worker: wrap_egms in REAL_WRAPPERS, runs in BASE env (+pandas), no conda.
- backend image: COPY libs/egms; footprint shipped at data/egms_footprint.geojson.

## Verified in-sandbox (passing)
- wrap_egms end-to-end on a synthetic tile: blank dates -> real epoch span,
  valid ResultJson, PNG rendered.
- footprint routing: Paris->EGMS, Yerevan->LiCSBAS, missing footprint->fallback.
- tier resolver incl. kill switch.
- all four services' code parses; main.py/downloader edits integrate cleanly.

## Deploy
    cd /geo && tar xzf geohazard-chat-m4.1a.tar.gz
    # footprint onto the data volume (backend reads /data/egms_footprint.geojson):
    #   it's shipped in data/ already; ensure it's on the mounted ./data
    # compose: add to backend `environment:`  EGMS_FOOTPRINT / EGMS_ENABLED (optional; defaults work)
    docker compose up --build -d backend worker downloader
    sh scripts/m41a_smoke.sh

## NOT in 4.1a (that's 4.1b)
- Live CLMS auth + async download (clms_client / run_egms live path). Shipped
  but only exercised when a real Paris query hits the download with a CLMS key.
