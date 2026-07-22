# geohazard-chat — deferred work backlog

Items found during build but deliberately deferred so a milestone could close.
Each notes where it surfaced, why it was deferred, and the intended fix.
Newest-at-top within each section. [DONE] items kept briefly for provenance.

## Scientific correctness

- **NDVI-change confidence overstates methodological certainty.** (M2.3) A
  2-scene NDVI difference can report high confidence on low cloud/mask
  fractions, but two images say little about an interannual trend (Aug-vs-Jul
  reads as seasonal phenology). Fix: cap NDVI-change confidence at "moderate"
  for a single image pair; separate data-quality from method-certainty as the
  InSAR path now does (M3.4a). Target: M5. Touches §6.3 -> doc-first.

- **InSAR coherence/vstd mask tuning.** (M3.4a, from eyeballing the velocity
  map) LiCSBAS default masks (p15_coh_thre 0.05, p15_vstd_thre 100) are very
  permissive and leave decorrelation speckle in the velocity field. M3.4b made
  p15_coh_thre tunable (default 0.3); p15_vstd_thre is a second available knob.
  Needs per-region calibration by eyeballing maps. Target: ongoing tuning.

- **asc+desc dual-frame runs.** (M3.2/M3.4a) The resolver returns both
  ascending and descending candidates, but the wrapper runs only the first
  live frame. Running both geometries and comparing (or decomposing toward
  vertical) is better science and helps distinguish real motion from noise.
  Target: M5/M6.

## Frame catalog & coverage

- **Catalog latest-epoch enrichment.** (M3.4a) The catalog stores footprint
  bbox only, so the resolver can rank a temporally-dead frame (processing
  stopped years ago) above a live one. Runtime probing (M3.4a coverage
  pre-check) handles this correctly but re-checks every query. Better: record
  each frame's latest interferogram date at build time (one extra listing per
  frame) so dead frames are down-ranked before selection. Target: next catalog
  refresh. Cheap enrichment to build_licsar_catalog.py.

- **Catalog refresh cadence.** (M3.3) licsar_frames.geojson is a static
  snapshot; frames change slowly but do change. Establish an occasional
  (quarterly?) re-crawl, ideally automated. Target: ops task.

- **True frame footprints vs bbox.** (M3.3) The catalog uses geo.U.tif
  bounding boxes, which over-include the tilted frame's nodata corners. The
  true parallelogram lives in the server-side LiCSInfo DB (not public). bbox
  over-return is the safe direction; revisit only if false candidates become a
  problem. Target: none unless needed.

- **[DONE M3.3] Automated AOI->frame resolution.** Global catalog
  (build_licsar_catalog.py, 2611 frames) + equal-area overlap resolver
  (libs/licsar/frames.py). Retired the manual DEFAULT_DEFORM_FRAME (now
  fallback only).

## Caching & storage

- **LiCSBAS interferogram caching.** (M3.2) wrap_licsbas uses a per-query
  workdir, so every InSAR query re-downloads the frame's interferograms
  (potentially GBs, tens of minutes). The GEOC download is frame+date-level
  cacheable and independent of the AOI clip. Fix: content-addressed frame
  download cache keyed by frame+date, shared across queries; clip/TS stay
  per-AOI. High value — this is the biggest InSAR latency/cost sink.
  Target: M5.

- **Default-date queries cache-miss daily.** (M2.3) Null dates resolve to a
  window ending today; the §6.2 probe requires requested-within-cached, so
  yesterday's entry never matches. Fix: tolerance on the range check or snap
  default windows to a coarser boundary. Target: M5. May touch §6.2 -> doc.

- **Multi-GB scenes duplicated across ranges.** (M2.2/M2.3) Archive layout is
  {aoi_hash}/{range}/, so overlapping ranges store the same product twice.
  Fix: content-addressed per-product storage with references. Target: M5.

## Reliability & error handling

- **Deterministic failures retried 3x before DLQ.** (M2.3) Partially fixed:
  the empty-frame InSAR case now fails fast via the coverage pre-check (M3.4a).
  General case remains — a missing lib or "tier not implemented" still burns
  all retries. Fix: classify errors; non-transient -> straight to DLQ.
  Target: M5.

- **NO_DATA result status.** (M3.4a) ResultStatus is OK/PARTIAL/FAILED; the
  "no InSAR data for this area/period" case currently uses FAILED with a
  benign caveat. A dedicated NO_DATA status would let the UI/LLM distinguish
  "method broke" from "legitimately no data here". Fix: add enum value.
  Target: M5. Touches §6.3 -> doc-first.

- **Crash during LLM synthesis leaves query in `summarizing`.** (M1.2, by
  design) Results acked before synthesis to survive the heartbeat window; a
  backend crash mid-synthesis strands the query. Fix: startup/periodic sweeper
  re-finalizing queries stuck past a timeout. Target: M5.

## Answer quality (LLM)

- **Post-synthesis numeric/date validator.** (M1.2/M2.3) The sanitizer now
  strips meta/critique/word-count lines and de-loops repeated answers
  (M3.4a), but numbers/dates are still unverified against the input JSON.
  Fix: extract every number/date from the answer, check each appears in
  result.json, regenerate once or fall back to template on mismatch. Turns
  "usually faithful" into "verifiably faithful". Target: M5. Touches §8 -> doc.

## Testing / tooling

- **Smoke scripts assume cache behaviour they don't set up.** (M2.3) Fix:
  dedicated cache tests pinning explicit dates, asserting on backend cache
  HIT/MISS log lines rather than timing. Target: M5.
