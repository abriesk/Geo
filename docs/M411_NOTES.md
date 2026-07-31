# M4.1.1 — multi-method fan-out + cross-validation synthesis

Deformation can now run EGMS **and** LiCSBAS for one query, and the synthesis
compares them correctly rather than naively.

## Shipped as a PATCH SCRIPT, not files
Twice now my sandbox copy of main.py has been stale against what is deployed —
once shipping a main.py that regressed vegetation to the dummy wrapper. So this
slice ships `scripts/m411_patch.py`, which asserts every expected block is
present EXACTLY ONCE and aborts without writing if not. A mismatch is a clean
failure, never a corrupted backend. Re-running is safe (applied edits are
skipped) and `.m411.bak` backups are written.

## What changed
1. `_resolve_deform_tier` -> `_egms_covers` + `_resolve_deform_methods`, which
   returns a LIST of methods gated by depth.
2. Routing loop fans out over methods, with a `MAX_ANALYSIS_TASKS` cap
   (default 4) so "thorough" on a multi-hazard question cannot spawn a pile of
   half-hour LiCSBAS runs.
3. **Results are per-method**: `{query_id}/{hazard}/{method}/result.json`.
   Without this, two deformation methods both wrote
   `.../deformation/result.json` and silently clobbered each other.
   Artifact serving needed no change — `/result` derives URLs from
   `Path(result_path).parent`, so it is path-agnostic.
4. Deferred download->analysis publish now derives the hazard from the wrapper
   (`WRAPPER_TO_HAZARD`). This is the real fix for EGMS results landing in
   `.../vegetation/` — the M4.1c run only *appeared* fixed because that query
   hit the cache and took the direct path instead.
5. Synthesis prompt gains a cross-validation rule (list renumbered 1-11 so no
   two rules share a number).

## Depth semantics
| depth | inside EGMS footprint | outside |
|---|---|---|
| quick | EGMS only (~2 min) | LiCSBAS |
| standard | EGMS + LiCSBAS | LiCSBAS |
| thorough | EGMS + LiCSBAS | LiCSBAS |

`DEFORM_DUAL_METHOD_MIN_DEPTH=thorough` keeps standard-depth queries fast.
Outside the footprint only one method exists, so depth changes nothing.

## Why comparing these two needs care
They are different measurements, not repeat readings, and naive comparison
would manufacture false disagreement:
- **Component**: EGMS L3 is VERTICAL; LiCSBAS is LINE-OF-SIGHT. Vertical motion
  projects into LOS smaller by roughly cos(incidence) ~0.7-0.87 for Sentinel-1.
  The prompt compares direction and pattern, never raw magnitudes.
- **Window**: EGMS is a fixed release (currently 2020-2024); LiCSBAS runs the
  requested window. Different numbers over different years are not a conflict.
- **Reference frame**: EGMS is tied to a Europe-wide GNSS model, LiCSBAS to a
  local pixel. A constant offset is expected and meaningless.

The genuinely valuable case the prompt calls out explicitly: **long-term method
stable, recent method moving** -> the movement is recent rather than
long-standing, which is exactly what warrants a professional look.

## Verified in-sandbox
- Patch applies cleanly to a reconstruction of the deployed main.py; both files
  compile; re-run is a no-op; a non-matching file aborts writing nothing.
- Resolver: Paris quick->[egms], standard/thorough->[egms,licsbas];
  Yerevan->[licsbas] at every depth; EGMS_ENABLED=false forces LiCSBAS;
  DEFORM_DUAL_METHOD_MIN_DEPTH=thorough defers dual-method.
- No `{query_id}/{hazard}` colliding paths remain anywhere.
- Prompt rules numbered 1-11 with no duplicates.

## Apply
    cd /geo && tar xzf geohazard-chat-m4.1.1.tar.gz
    python3 scripts/m411_patch.py
    docker compose up --build -d backend

## Test
    # standard depth on Paris -> BOTH methods
    curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
      -d '{"question":"is the ground sinking here?","aoi":{"type":"Polygon","coordinates":[[[2.31,48.83],[2.39,48.83],[2.39,48.89],[2.31,48.89],[2.31,48.83]]]},"depth":"standard"}'
    docker compose logs --tail 40 backend | grep -i router
    # expect: "deformation: EGMS + LiCSBAS (cross-validation)"
    # after both finish (LiCSBAS ~30 min):
    ls data/results/<query_id>/deformation/     # -> wrap_egms/  wrap_licsbas/

NOTE: this is the first query to exercise the deferred publish path with a
per-method directory AND the first two-method synthesis, so watch for both
result.json files appearing before the answer is written.
# M4.1.1 — multi-method fan-out + cross-validation synthesis

Deformation can now run EGMS **and** LiCSBAS for one query, and the synthesis
compares them correctly rather than naively.

## Shipped as a PATCH SCRIPT, not files
Twice now my sandbox copy of main.py has been stale against what is deployed —
once shipping a main.py that regressed vegetation to the dummy wrapper. So this
slice ships `scripts/m411_patch.py`, which asserts every expected block is
present EXACTLY ONCE and aborts without writing if not. A mismatch is a clean
failure, never a corrupted backend. Re-running is safe (applied edits are
skipped) and `.m411.bak` backups are written.

## What changed
1. `_resolve_deform_tier` -> `_egms_covers` + `_resolve_deform_methods`, which
   returns a LIST of methods gated by depth.
2. Routing loop fans out over methods, with a `MAX_ANALYSIS_TASKS` cap
   (default 4) so "thorough" on a multi-hazard question cannot spawn a pile of
   half-hour LiCSBAS runs.
3. **Results are per-method**: `{query_id}/{hazard}/{method}/result.json`.
   Without this, two deformation methods both wrote
   `.../deformation/result.json` and silently clobbered each other.
   Artifact serving needed no change — `/result` derives URLs from
   `Path(result_path).parent`, so it is path-agnostic.
4. Deferred download->analysis publish now derives the hazard from the wrapper
   (`WRAPPER_TO_HAZARD`). This is the real fix for EGMS results landing in
   `.../vegetation/` — the M4.1c run only *appeared* fixed because that query
   hit the cache and took the direct path instead.
5. Synthesis prompt gains a cross-validation rule (list renumbered 1-11 so no
   two rules share a number).

## Depth semantics
| depth | inside EGMS footprint | outside |
|---|---|---|
| quick | EGMS only (~2 min) | LiCSBAS |
| standard | EGMS + LiCSBAS | LiCSBAS |
| thorough | EGMS + LiCSBAS | LiCSBAS |

`DEFORM_DUAL_METHOD_MIN_DEPTH=thorough` keeps standard-depth queries fast.
Outside the footprint only one method exists, so depth changes nothing.

## Why comparing these two needs care
They are different measurements, not repeat readings, and naive comparison
would manufacture false disagreement:
- **Component**: EGMS L3 is VERTICAL; LiCSBAS is LINE-OF-SIGHT. Vertical motion
  projects into LOS smaller by roughly cos(incidence) ~0.7-0.87 for Sentinel-1.
  The prompt compares direction and pattern, never raw magnitudes.
- **Window**: EGMS is a fixed release (currently 2020-2024); LiCSBAS runs the
  requested window. Different numbers over different years are not a conflict.
- **Reference frame**: EGMS is tied to a Europe-wide GNSS model, LiCSBAS to a
  local pixel. A constant offset is expected and meaningless.

The genuinely valuable case the prompt calls out explicitly: **long-term method
stable, recent method moving** -> the movement is recent rather than
long-standing, which is exactly what warrants a professional look.

## Verified in-sandbox
- Patch applies cleanly to a reconstruction of the deployed main.py; both files
  compile; re-run is a no-op; a non-matching file aborts writing nothing.
- Resolver: Paris quick->[egms], standard/thorough->[egms,licsbas];
  Yerevan->[licsbas] at every depth; EGMS_ENABLED=false forces LiCSBAS;
  DEFORM_DUAL_METHOD_MIN_DEPTH=thorough defers dual-method.
- No `{query_id}/{hazard}` colliding paths remain anywhere.
- Prompt rules numbered 1-11 with no duplicates.

## Apply
    cd /geo && tar xzf geohazard-chat-m4.1.1.tar.gz
    python3 scripts/m411_patch.py
    docker compose up --build -d backend

## Test
    # standard depth on Paris -> BOTH methods
    curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
      -d '{"question":"is the ground sinking here?","aoi":{"type":"Polygon","coordinates":[[[2.31,48.83],[2.39,48.83],[2.39,48.89],[2.31,48.89],[2.31,48.83]]]},"depth":"standard"}'
    docker compose logs --tail 40 backend | grep -i router
    # expect: "deformation: EGMS + LiCSBAS (cross-validation)"
    # after both finish (LiCSBAS ~30 min):
    ls data/results/<query_id>/deformation/     # -> wrap_egms/  wrap_licsbas/

NOTE: this is the first query to exercise the deferred publish path with a
per-method directory AND the first two-method synthesis, so watch for both
result.json files appearing before the answer is written.

---

# M4.1.2 — never analyse a frame that does not cover the AOI

Found immediately by the M4.1.1 fan-out, on the first real Paris standard-depth
query. Apply with `python3 scripts/m412_patch.py`.

## The bug
LiCSAR has no frame over Paris (the archive targets tectonically/volcanically
active regions, not the whole world), so the catalog correctly returned nothing.
The resolver then fell back to `DEFAULT_DEFORM_FRAME` — the **Yerevan test
frame** — with no geographic check at all:

    LiCSBAS05op_clip_unw.py -g 2.3100/2.3900/48.8300/48.8900   <- Paris
      44.0819445/47.5249418/38.4848910/41.1688889 ->           <- frame: Armenia
      Width/Length: 3444/2685 -> -41691/-7660                  <- negative

**The crash was luck.** Had the clip arithmetic landed inside the raster, this
would have produced a velocity map of Armenia and presented it as the answer to
a question about Paris. Confidently wrong about the wrong continent is the worst
failure this system can have — worse than any crash. It then burned 3 retries
(~3 min each) on a deterministic geographic miss.

## Fix (defence in depth, both sides fail safe)
1. **Router**: the single configured fallback frame is used ONLY if the catalog
   confirms it overlaps this AOI. `_frame_covers_aoi` fails CLOSED — missing
   catalog or unknown frame -> False. Declining to analyse beats analysing the
   wrong place.
2. **Router**: no covering frame -> `params["no_insar_coverage"]`, so the
   wrapper answers at once rather than downloading a foreign frame and failing.
3. **Wrapper**: independently drops candidate frames whose catalog footprint
   misses the AOI, whatever put them there (`--frame`, stale params). Here a
   missing catalog fails OPEN, because the router is the authority and an absent
   catalog must not block a legitimate run.
4. Both no-coverage paths exit 0 with an honest result.json, so the query still
   gets a real answer and the deterministic failure is not retried three times.

## Verified in-sandbox (against the real frame footprint from the crash log)
| check | result |
|---|---|
| Yerevan frame vs Paris AOI | False (bug blocked) |
| Yerevan frame vs Yerevan AOI | True (still works) |
| Paris-area frame vs Paris AOI | True |
| unknown frame id | False (fails closed) |
| missing catalog, router side | False (fails closed) |
| missing catalog, wrapper side | True (router is authority) |

## What Paris should now answer
EGMS: stable, high confidence. LiCSBAS: no LiCSAR coverage, stated plainly.
The synthesis has both and should report the EGMS finding while being honest
that the recent-window cross-check was unavailable — which is exactly the
partial-coverage case M4.1.1's prompt work was for.

## Note on test AOIs
Paris is a good EGMS test but a poor LiCSBAS one. For exercising genuine
two-method cross-validation, use an AOI with BOTH: somewhere in the
Mediterranean/Alpine belt inside the EGMS footprint AND inside LiCSAR coverage
(e.g. parts of Italy, Greece or Turkey).
