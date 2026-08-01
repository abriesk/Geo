# M4.2c fix 2 — S1C scenes + failure-path validation

Routing now works: the task reached flood-worker and ran real FLOODPY. Two bugs
surfaced.

## Bug A — FLOODPY died on a Sentinel-1C scene
    ValueError: missions argument must be "S1A" or "S1B"
The download list included an `S1C_...` scene. Sentinel-1C is operational (2025)
and in the CDSE catalogue, but FLOODPY's orbit downloader only understands
S1A/S1B. Fix (wrapper, no FLOODPY edit): `_drop_unsupported_missions` filters
`query_S1_df` to S1A/S1B right after query_S1_data(), BEFORE the flood date is
chosen, with a logged caveat. If it removes the only image near the peak, the
existing "no usable image" path fires honestly. Verified: mixed A/C frame keeps
A, drops C; all-C frame empties and routes to the honest no-image result.
Proper S1C support is backlogged.

**But note:** the scenes were dated 2026, not the 2023 you requested. That means
the flood task did not carry your --dates — see Bug C below, still open pending
one grep.

## Bug B — the honest-failure path crashed on its own validation
    2 validation errors for ResultJson: quality.date_coverage input 'unknown'
`_emit_failure_result` wrote `date_coverage: ["unknown","unknown"]`, but §6.3
requires two valid ISO dates. So when a flood run failed, recording that failure
ALSO failed, and the query could not finalise. Fixed: the failure path now
always writes valid ISO dates (task dates if present and parseable, else a
today-90d..today window). Verified with a no-dates task -> valid result.json.

## Bug C — STILL OPEN: your 2023 dates became 2026
The downloaded scenes are 2026, so the backend isn't passing the request dates
to the flood task (or a "quick" flood defaults to the recent 3-month window per
§6.1). Please run:

    grep -n "dates\|DateRange\|lookback\|default" services/backend/app/main.py | head -20

and paste the flood/analysis task-construction lines. Likely the flood task is
built with the default date window instead of payload.dates. Once I see it, it's
a one-line routing of the payload dates into the flood task (or a doc decision
that flood honours an explicit range).

## Apply (fixes A + B now)
    cd /geo && tar xzf geohazard-chat-m4.2c-fix2.tar.gz
    docker compose build flood-worker && docker compose up -d flood-worker
    # re-test; expect either a real 2023 flood result, or — until Bug C — a
    # clean 2026 run that now survives S1C by dropping it.

Once Bug C is fixed and dates are 2023, Storm Daniel should map as it did
standalone (the AOI is smaller, so km² will differ).
