# geohazard-chat — M5 plan (Hardening / MVP line)

M5 is §11's **Hardening** milestone — the MVP line. It does not add hazards.
It makes the working end-to-end system trustworthy (honest about coverage and
dates), durable (survives failures without burning budget), and cheaper (stops
re-downloading GBs). Everything inferential or agentic — LLM date inference,
optical flood pre-screening, agentic clarification/padding — is explicitly
**deferred to M6+** (see bottom).

Style/format follows BACKLOG.md. Slices are sized to one session per §11.3
rule 3. Doc-first (§11.3 rule 4) is flagged where a contract changes.

Slice order (locked): **M5.1 → M5.2 → then the rest.** Rationale: M5.1 adds the
`NO_DATA` status and honest failure paths that M5.2's wording depends on;
M5.2 reworks synthesis once, against the finished multi-hazard + pre-flight
picture, so every caveat is written a single time.

---

## M5.1 — Deterministic-failure robustness + NO_DATA  [FIRST]  ✅ DONE

**Status: complete, verified end-to-end on live data (2026-07).** All paths
fired on the real stack: permanent→1-attempt-DLQ, transient→3-attempt-DLQ,
no-frame→no_data/no_coverage, step-11 all-ifgs-bad→no_data/measured_absence,
normal→ok. Three bugs were surfaced by live testing that sandbox verification
missed: (1) missing-frame AOIs hard-errored because the frame-id gate ran before
the no-coverage handler; (2) measured_absence has TWO doorways — the step-11
pre-inversion abort (common: water/veg) and post-inversion zero-pixels (rare) —
the first initially slipped past the post-inversion guard; (3) the transient
test stimulus wasn't actually transient under the allowlist classifier. All
fixed and re-verified. Smoke: 8/8 + live measured_absence on a Lake Van
open-water AOI.


Stop burning ~20 min × 3 retries on failures that will never succeed, and let
the system say "no data" distinctly from "method broke." No prompt dependency;
highest cost-per-failure. Three coupled pieces.

- **Add `NO_DATA` to `ResultStatus`.** Currently OK/PARTIAL/FAILED
  (`libs/contracts/geohazard_contracts/enums.py:52`). Add `NO_DATA = "no_data"`.
  *Contract change → §6.3 doc-first.* Regenerate schemas
  (`scripts/generate_schemas.py`). Everything downstream that switches on status
  (backend finalize, LLM synthesis input) must treat NO_DATA as a terminal,
  non-error outcome — a real answer, not a failure.

- **Convert the last raising no-data path in wrap_licsbas.** The wrapper
  *already* handles most no-data cases as clean exit-0 results
  (`_no_coverage_result`, the in-window IFG probe at ~L400–430). The one
  remaining `raise` is the **post-inversion empty case**
  (`wrap_licsbas.py:212`, "no valid (unmasked, coherent) pixels in AOI after
  inversion") — this is the La Spezia step-11 "All ifgs are regarded as bad"
  crash (harbour + wooded hills, ~4 pts/km² measurable). Fix: replace the
  `raise` with a `NO_DATA` ResultJson via the existing helper pattern, honest
  caveat ("this area has almost no usable radar measurements — water, dense
  vegetation, or steep terrain"), **exit 0** so it is not retried. This is
  §11.2's "AOI over water → refuse gracefully." (M4.1.3 backlog item.)

- **Classify errors in the worker retry loop.** `worker_main.py:244` is a
  blanket `except Exception` that requeues *everything* up to
  `MAX_TASK_RETRIES`, then DLQs — so a missing lib or "tier not implemented"
  (deterministic) burns all 3 retries identically to a transient network blip.
  Fix: a small `is_transient(exc)` classifier; non-transient → straight to DLQ
  (nack, no requeue) with a `failed` ResultMessage on the first attempt;
  transient → existing retry path unchanged. Keep the `simulate_failure` test
  hook; add a `simulate_permanent_failure` hook so both branches are testable.
  (M2.3 backlog item; partially addressed by M3.4a's coverage pre-check.)

Env/knobs: none new required. `MAX_TASK_RETRIES` stays.
Touches: §6.3 (enum), §5.6/§7 (retry/DLQ policy note — doc the transient vs
permanent split).

### How M5.1 gets written
Patch-script discipline (§11.3, post-M4.1 lesson — no full-file replacement):
1. `m51_enums_patch.py` — add NO_DATA, regenerate schemas, assert round-trip.
2. `m51_licsbas_nodata_patch.py` — swap the L212 raise for a NO_DATA result
   built the same way as `_no_coverage_result`; keep exit 0.
3. `m51_worker_classify_patch.py` — insert `is_transient()` + branch in the
   `except` at worker_main.py:244; add the permanent-failure test hook.
Each patch is idempotent and self-checks (re-run = no-op).

### How M5.1 gets tested — `scripts/m51_smoke.sh`
Offline-first, following the m41a_smoke.sh convention (curl → poll → grep logs).

1. **Contract round-trip (offline, in sandbox before shipping):**
   validate a `ResultJson` with `status:"no_data"` parses; assert
   `NO_DATA` is in the regenerated JSON schema enum. Fails build if not.
2. **Permanent-failure → DLQ on attempt 1 (no retries):** submit a task with
   `simulate_permanent_failure`; assert the backend log shows **one** attempt
   then DLQ, **not** attempt 1/2/3. This is the core regression guard — the
   whole point is the retries *don't* happen.
3. **Transient-failure → still retries:** submit with the existing
   `simulate_failure`; assert attempts 1→2→3 then DLQ, i.e. the classifier
   didn't over-eagerly kill retryable work.
4. **NO_DATA end-to-end (live, the La Spezia reproduction):** an AOI over
   water/dense vegetation with a covering-but-incoherent frame → assert the
   result reaches the UI with `status: no_data`, a plain-language "almost no
   usable radar measurements" caveat, and — critically — that the worker log
   shows **exit 0 / a single attempt**, no 3× re-download. If a live
   incoherent frame is expensive to hit, stand in a fixture GEOC stack whose
   inversion yields zero valid pixels so the L212 path fires without a full
   download.
5. **Regression:** a normal deforming AOI still returns `ok` — NO_DATA didn't
   swallow real results.

Acceptance: (2) shows one attempt, (3) shows three, (4) shows exit 0 + a
readable no-data answer in the UI, (1) and (5) green.

---

## M5.2 — Synthesis/prompt rework + dependent wording  [SECOND]

The linchpin. Rework the answer layer for the finished multi-hazard + pre-flight
picture, then land the four items that were deliberately parked until the
wording could be written once against it.

- **AOI-coverage reporting.** Surface frame-coverage fraction (clip vs
  requested) and valid-pixel fraction in the quality block; caveat loudly when
  low; raise `find_licsar_frames` `min_overlap_fraction` above 5%. (La Spezia:
  26% of the drawn AOI analysed and presented as "the area".) *May touch §6.3
  quality block → doc-first if a new field is added.* (M4.1.3.)
- **NDVI-change confidence cap.** Cap a single 2-scene pair at "moderate";
  separate data-quality from method-certainty as InSAR already does (M3.4a).
  *Touches §6.3 → doc-first.* (M2.3.)
- **NO_DATA wording.** Now that the enum exists (M5.1), give the LLM/template
  distinct language for "legitimately no data here" vs "method broke". *§6.3.*
- **Post-synthesis numeric/date validator.** Extract every number/date from the
  answer, assert each appears in `result.json`; regenerate once or fall back to
  template on mismatch (`llm.py` `_sanitize`/`synthesize_answer` region).
  Turns "usually faithful" into "verifiably faithful". *Touches §8 → doc.*
  (M1.2/M2.3.)
- **Date pre-flight caveats.** The wording surface for the M5.x acquisition
  probe (below) — "nearest usable scene is N days outside your window",
  "tightened to known flood event on DATE", "no known major event, used
  default window" — lives here so it's written with the rest.

### Contract hardening (rides the §6.3 reopening — cheap, do it here)

These three surfaced from an external review; all touch the result contract or
the cache, so they land with the synthesis/contract work rather than as their
own slice. None is load-bearing for M5's core; all are latent-debt insurance.

- **`wrapper_version` + `tool_version` in result.json.** Stamp each result with
  the wrapper's own version and the underlying tool's (e.g. "LiCSBAS 1.8.4",
  "wrapper 0.3.2"; EGMS archive-API date; FLOODPY commit). DESCRIPTIVE ONLY —
  for humans debugging "which build produced this artifact" (the same class of
  problem as the M4.1 stale-main.py incidents). Never branch on it in code, or
  it becomes an informal second schema. *Additive §6.3 fields → doc-first,
  optional, no version bump needed.*
- **`schema_version` in result.json (default 1) + cache-reader check.** The
  real justification is NOT "we'll add fields" (additive fields need no bump —
  see `no_data_reason`), it's **cached artifacts outliving the schema**: the
  archive serves result.json back across code versions, so a BREAKING change
  (renamed field, changed units, changed meaning of `confidence`) silently
  mis-reads an old cached artifact. `schema_version` lets the cache reader
  detect "this artifact predates the change" and skip/regenerate. MUST be added
  as a PAIR: the field alone is decoration — the cache-artifact read path has to
  assert compatibility. Files next to the M5.4 cache-correctness items.
  *§6.3 → doc-first.*
- **Golden-hash regression test for AOI canonicalization.** The AOI hash is the
  cache key; the existing tests (`libs/contracts/tests/test_contracts.py`) cover
  invariance (CW==CCW, jitter-tolerant, different-differs, bowtie-rejected) but
  NOT a pinned value — so a canonicalization change that preserves all
  invariances can still shift every hash, silently collapsing cache hit rates
  with all tests green (the reviewer's exact scenario). Fix: one test asserting
  `aoi_hash(FIXED_POLYGON) == "<pinned hex>"`, commented "if this fails,
  canonicalization changed and ALL cached AOIs will miss — bump deliberately."
  This one test is the actual safeguard; relocating hashing to a dedicated
  `libs/aoi/` package (the reviewer's other suggestion) is cosmetic — it already
  lives in `geometry.py` as a library function — and is logged as optional
  tidy-up, not a task.

### How M5.2 gets tested
Golden-answer fixtures: hand-built `result.json` inputs (low-coverage,
2-scene-NDVI, no_data, snapped-window) → assert the synthesized answer
contains the required caveat and contains **no** number/date absent from the
input. The numeric validator gets an adversarial fixture (a result whose
"correct" answer would tempt rounding) and must catch it. Offline, no queue.

---

## M5.x — Acquisition-availability pre-flight (date framing)

Deterministic, cheap "what data actually exists near what you asked for?" probe,
run before committing to an expensive analysis. Same shape/stage as the M3.4a
coverage pre-check, generalised from space to time. **Additive, never gating:**
it snaps the window or reports honestly; it never *concludes* absence.
Slots after M5.2 (it feeds M5.2's wording) or folds into it if convenient.

Per-hazard, metadata-only (no product download):
- **Deformation** → LiCSAR epoch list (reuse `check_coverage`).
- **Vegetation** → CDSE/S2 tile catalog: are there low-cloud acquisitions in
  the window? Also subsumes the "default-date cache-miss daily" item if default
  windows snap to a coarse acquisition boundary.
- **Flood** → GDACS/ReliefWeb event lookup (full spec below).

Two honest outcomes per hazard: **snap** (adjust window, say so) or **report
empty with the reason** (nearest data / event is N days away).

### Flood event probe (fully specified)
Runs on flood queries only, before dispatch to flood-worker.
- **Source order:** GDACS → ReliefWeb → fall through to default.
- **Filter:** flood-category events only, overlapping the AOI by
  ≥ `FLOOD_EVENT_MIN_OVERLAP` (default ~0.3 — deliberately higher than
  LiCSAR's burned 0.05; overlap-fraction match, like frames).
- **Branch 1 — no dates, or user window ≤ 12mo:** search a fixed 12-month span
  (`FLOOD_EVENT_LOOKBACK_MONTHS`, default 12; the no-dates default window).
  If the user gave a short window, report events within ~2mo of it as "nearby".
  If no dates, report the most-recent event found.
- **Branch 2 — user window > 12mo:** search **exactly** the user's window, no
  pad, honored literally (a flood just before their start is an accepted MVP
  gap; padding/negotiation is M6). Report events found inside.
- **Multi-event disclosure set (~5), ordered-dedup selection.** Analyze the
  most-recent event; the answer names up to ~5, chosen to guarantee both
  recency and severity get a slot, in this priority order:
  1. most recent
  2. most severe in period
  3. 2nd most severe
  4. 2nd most recent
  5. 3rd most severe
  Build as an ordered dedup: walk the slots; for each, take the
  highest-priority not-yet-selected candidate on that axis; stop at 5 or when
  both lists exhaust. **Recency wins ties.** If fewer than 5 distinct events
  exist, report only those found. If >5 total, the answer says "…and N more".
- **Branch B (user gave dates), GDACS annotates, never overrides:** event
  *inside* window → confirm/enrich; event in the ~2mo margin → surface as
  "nearby, want it?"; no match → run the user's dates as-is, honest empty
  wording. GDACS silence never means "no flood" (magnitude floor).

Ops note: GDACS/ReliefWeb domains are **not** in the current bash/network
allowlist — adding them is a deliberate ops decision, not a free add.
Knobs: `FLOOD_EVENT_MIN_OVERLAP` (~0.3), `FLOOD_EVENT_LOOKBACK_MONTHS` (12),
event-cap (~5), all §6.6-style env vars.

### How the flood probe gets tested
Mock the GDACS/ReliefWeb responses (recorded JSON fixtures — no live calls in
the smoke test): assert branch selection by window length; assert the
overlap-fraction filter drops a same-country-but-non-overlapping event; assert
the 5-slot ordered-dedup produces the expected set for a monsoon fixture with
the recency/severity collision case; assert a miss falls through to the user's
dates without editorialising.

---

## M5.3 — InSAR frame-download cache

Biggest InSAR latency/cost sink. wrap_licsbas uses a per-query workdir, so every
InSAR query re-downloads the frame's GEOC interferograms (GBs, tens of minutes).
Fix: content-addressed frame download cache keyed by **frame+date**, shared
across queries; clip/TS stay per-AOI. (M3.2 backlog item — highest-value cache.)

## M5.4 — Cache correctness & storage dedup

- **Default-date cache-miss daily.** Null dates resolve to a window ending
  today; the §6.2 probe requires requested-within-cached, so yesterday's entry
  never matches. Fix: tolerance on the range check, or snap default windows to a
  coarse boundary (largely subsumed by the M5.x pre-flight snapping). *May touch
  §6.2 → doc-first.* (M2.3.)
- **Multi-GB scenes duplicated across ranges.** `{aoi_hash}/{range}/` stores
  overlapping products twice. Fix: content-addressed per-product storage with
  references. (M2.2/M2.3.)

## M5.5 — Lifecycle & reliability ops

- Retention/cleanup job (§11 DoD).
- Stuck-query sweeper: re-finalize queries stranded in `summarizing` after a
  mid-synthesis crash (M1.2).
- **Skip LLM synthesis when there is nothing to synthesize.** (M5.1, confirmed
  on the smoke.) A query whose only task failed still calls the LLM in finalize,
  waits up to `LLM_TIMEOUT_SECONDS`, then falls back to the template — so a
  permanent failure's `failed` status is delayed by the full LLM timeout for no
  benefit. Fix: when `results` is empty (all tasks failed) OR every result is
  `no_data:no_coverage`, skip the LLM call and go straight to the deterministic
  template answer. Cheap, removes dead latency, and de-risks finalize. Deferred
  out of M5.1 deliberately (it touches the synthesis path, which M5.2 reworks —
  do it there or here, not inside the robustness slice). Target: M5.2 or M5.5.
- DLQ handling UI-side.
- AOI/limit validation polish + disclaimer/attribution polish.
- Concurrent-query soak test (validates §11.2 same-cache-entry edge case; also
  exercises M5.3/M5.4 under contention).

## M5.6 — Frontend & testing hardening (parallelizable, low-risk)

- Map geocoder search box (Nominatim) — prevents the "AOI accidentally over the
  harbour" error class. (M4.1.3.)
- Apply the prepared date-range explainer (`docs/m34b_frontend_edit.md`).
- Cache tests pinning explicit dates, asserting on backend HIT/MISS log lines
  rather than timing. (M2.3.)
- **Poll backoff on the status endpoint.** (M5.1 sidenote.) A ~90-min job polled
  every 3 s is ~1800 requests/query. Quick win: exponential backoff — 2 s for
  the first minute, then 5 s, then 10 s — in the frontend status poller (and the
  smoke `watch()`). The fuller fix (Server-Sent Events, which Streamlit
  supports and is simpler than WebSockets) is M6 frontend modernization, pairing
  with the §12 "SPA + WebSockets" future work. Target: backoff M5.6, SSE M6.

---

## Deferred to M6+ (scope creep lands here)

- **Shareable/actionable output.** (M5.1 sidenote.) Turn an answered query into
  communication: a shareable link (static summary + map image), a downloadable
  PDF report (LLM answer + map + disclaimer), and "email it to a local
  official". This is the "query → actionable communication" cap on the product
  and depends on the answer layer being final (post-M5.2). Target: M7.
- **LLM temporal extraction** in the intent router ("during the rains last
  April" → dates). Safer than generic auto-framing (it's transcription atop an
  existing parse, output validated by the M5.x probe + GDACS + numeric
  validator), but it's still the LLM entering the dates-that-drive-analysis
  path — hardens *on top of* the deterministic M5 spine, so it waits.
- **Empty-window widening ladder (Case A only).** Extend the M5.x pre-flight
  probe: if the requested/default window is empty but the probe sees data in a
  wider span, try 12mo then 24mo before giving up — CAPPED. Critically, gate it
  on the probe's verdict: only widen for **Case A** (data exists, just not in
  this window); **Case B** (genuinely unmeasurable — Tatvan-over-water,
  no coherent scatterers in ANY window) stays fail-fast (measured_absence /
  no_coverage), because widening there just multiplies expensive runs for the
  same honest "can't measure here". A naive widen-and-retry that can't tell A
  from B would be a cost sink and invert M5.1's fail-fast. The widening MUST be
  surfaced in the answer ("your 3-month window had no data; widened to 24 months
  and found N"), never silent (same honesty rule as the AOI-fraction clip).
  Pairs with LLM temporal extraction. Target: M6.
- **Server-Sent Events for live status** (replaces poll backoff; §12 SPA work).
- **Optical (NDWI) flood pre-screen.** Cloud-blind exactly when needed (floods
  = storms = cloud); inverts the ladder's reason for using radar. At best an
  M6+ tertiary fallback.
- **Agentic clarification / padding / negotiation** around flood events (ask the
  user, widen the pad, pick among events interactively). Needs an agentic layer
  that can hold a back-and-forth; not M5.
- asc+desc dual-frame decomposition (M3.2/M3.4a); catalog latest-epoch
  enrichment / refresh cadence / true footprints (M3.3/M3.4a); coherence/vstd
  per-region mask tuning (ongoing); HyP3 raw-expert tier (M6, per §11).
