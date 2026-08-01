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

- **Analysed fraction of the AOI is never reported.** (M4.1.3, found on a real
  La Spezia query) The chosen LiCSAR frame can cover only part of the drawn
  AOI, and `find_licsar_frames` accepts frames overlapping as little as 5%.
  Measured case: AOI 9.6597/9.8657/44.1310/44.3278 (~359 km2, independently
  confirmed by EGMS returning 1470 points at 4.1/km2) was clipped to 207x52 px
  ~= 95 km2 — **26% of the drawn area, analysed and presented as "the area"**.
  Fix: report both frame-coverage fraction (clip vs requested) and valid-pixel
  fraction in the quality block, caveat when low, and raise
  `min_overlap_fraction` above 5% or caveat low-overlap frames loudly.
  Deferred with the rest of the wording work: the answer/prompt layer is being
  reworked for multi-hazard synthesis (M5/M6), so these caveats should be
  written once, against the finished picture. Target: M5.

## Architecture — i18n & configurability (M6–M7)

Theme: an operator installing this app for their own region/models should be
able to adapt language and prompts WITHOUT editing source. All four are
post-MVP (M6–M7); grouped because they share the "externalize + edit" shape.

- **Externalize user-facing language strings.** Move all UI/answer phrases to
  external file(s) (e.g. per-locale) so an installer can translate the app to
  their users' language. Precondition for real multi-language support beyond the
  current hardcoded EN/RU examples.
- **Externalize LLM prompts.** Move synthesis/router prompts to external files
  so an installer can fine-tune them for whichever local model they run
  (different models need different prompting). Pairs with the M6 model-agnostic
  work. NOTE: interacts with the M5.2 numeric validator — externalized prompts
  still pass through the same output validation, so this stays safe.
- **Config-editing web UI.** A small admin UI to edit the language-strings and
  prompt files above, so non-developers can adapt the install. Depends on both
  externalizations existing first.
- **LLM fallback for question parsing.** When the deterministic router script
  does NOT catch a question (unrecognized phrasing), fall back to an LLM parse
  for hazard/intent extraction. Must still emit the same strict JSON the script
  does and pass the same validation — the LLM is a fallback parser, not a
  bypass of the deterministic contract. Pairs with M6 LLM temporal extraction.


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

- **result.json has no schema_version -> cached artifacts can outlive the
  schema.** (M5.2, external review) The archive serves result.json back across
  code versions; a breaking change (renamed field, changed units, changed
  meaning of a value) silently mis-reads an old cached artifact. Fix: add
  `schema_version` (default 1) AND a cache-reader compatibility check — the
  field alone is decoration. Additive fields (e.g. no_data_reason) need no bump;
  only breaking changes do. Target: M5.2, next to the cache-correctness items.

- **result.json has no wrapper/tool version stamp.** (M5.2, external review)
  Debugging "which build produced this artifact" is guesswork. Fix: add
  `wrapper_version` + `tool_version` (e.g. "LiCSBAS 1.8.4", "wrapper 0.3.2").
  Descriptive only — never branch on it. Additive, cheap. Target: M5.2.


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

- **Finalize waits on the LLM even when there is nothing to synthesize.**
  (M5.1, confirmed on smoke) A query whose only task failed still enters LLM
  synthesis in `_finalize_query_if_done`, blocks up to `LLM_TIMEOUT_SECONDS`,
  then falls back to the template — delaying the `failed` status for no benefit
  and briefly stranding the query in `summarizing`. Fix: when `results` is empty
  (all-failed) or every result is `no_data:no_coverage`, skip the LLM and go
  straight to the deterministic template. Touches the synthesis path, so do it
  in M5.2 (synthesis rework) or M5.5, not inside a robustness slice. Target: M5.


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

- **[DONE — M5.1] LiCSBAS step-11 "All ifgs are regarded as bad" crashes
  instead of answering.** (M4.1.3) Now caught: `_run_batch` detects the step-11
  signature and emits `NO_DATA(measured_absence)` at exit 0 (no retry). Verified
  live on a Lake Van open-water AOI. When the clipped AOI contains no coherent
  scatterers —
  water, dense vegetation, steep terrain — `n_unw_valid` is 0, every
  interferogram fails the coverage test, and LiCSBAS raises. Verified on a La
  Spezia (harbour + wooded hills) query where EGMS independently found only
  ~4 points/km2 against a ~100/km2 grid, i.e. ~4% of the area was measurable
  at all. Cost: ~20 min of download, then three identical retries of a
  deterministic failure. Fix: catch it, emit an honest "this area has almost no
  usable radar measurements" result, exit 0 so it is not retried. This is
  §11.2's documented "AOI over water -> refuse gracefully" edge case.
  NOTE: unlike the reporting items this is a robustness fix, not a wording one,
  and does not depend on the prompt rework. Target: M5, or fold into any slice
  that touches wrapper failure paths.

## Answer quality (LLM)

- **Post-synthesis numeric/date validator.** (M1.2/M2.3) The sanitizer now
  strips meta/critique/word-count lines and de-loops repeated answers
  (M3.4a), but numbers/dates are still unverified against the input JSON.
  Fix: extract every number/date from the answer, check each appears in
  result.json, regenerate once or fall back to template on mismatch. Turns
  "usually faithful" into "verifiably faithful". Target: M5. Touches §8 -> doc.

## Testing / tooling

- **No golden-hash pin for AOI canonicalization.** (M5.2, external review) The
  AOI hash is the cache key. Existing tests cover invariance (CW==CCW, jitter,
  different-differs, bowtie) but assert only relations, never a pinned value —
  so a canonicalization change preserving all invariances can still shift every
  hash and silently collapse cache hit rates with all tests green. Fix: one test
  asserting `aoi_hash(FIXED_POLYGON) == "<pinned hex>"` with a comment that a
  failure means a deliberate breaking change. (Relocating hashing to a dedicated
  libs/aoi/ package is cosmetic — it's already a library function in
  geometry.py — logged as optional tidy-up, not a task.) Target: M5.2.


- **Smoke scripts assume cache behaviour they don't set up.** (M2.3) Fix:
  dedicated cache tests pinning explicit dates, asserting on backend cache
  HIT/MISS log lines rather than timing. Target: M5.

## Frontend / UX

- **Show example questions for all three hazards in the input box.** (M5.6)
  The greyed-out placeholder / hint currently shows only a deformation example
  ("is the ground moving here? / здесь есть проседание грунта?"). Add flood and
  vegetation examples too, so users discover the app handles more than
  subsidence. Cheap; do it next time the frontend is touched.

- **Status polling is wasteful.** (M5.1 sidenote) A ~90-min job polled every
  3 s is ~1800 requests/query. Quick win: exponential backoff (2 s first minute,
  then 5 s, then 10 s) in the frontend poller and smoke `watch()`. Fuller fix:
  Server-Sent Events (Streamlit-compatible, simpler than WebSockets), pairing
  with the §12 SPA work. Target: backoff M5.6, SSE M6.

- **Answered query -> actionable communication.** (M5.1 sidenote) Let a user
  turn an answer into: a shareable link (static summary + map image), a
  downloadable PDF report (LLM answer + map + disclaimer), and "email to a local
  official". Depends on the answer layer being final (post-M5.2). Target: M7.


- **No map search.** (M4.1.3) The AOI is drawn on a bare map with no place
  search, so picking a test or real area means eyeballing coordinates — which
  is how a La Spezia test AOI ended up largely over the harbour. Fix: a
  geocoder search box (Nominatim or similar) that pans/zooms the map.
  Target: M5.

- **Date-range explainer.** (M3.4b) Prepared in `docs/m34b_frontend_edit.md`,
  not yet applied: help text on the "Limit date range" control plus per-hazard
  window guidance. Target: whenever the frontend is next touched.
