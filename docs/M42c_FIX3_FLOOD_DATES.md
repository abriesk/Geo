# M4.2c fix 3 — flood honours a real date window (MVP: 3-month default)

## The bug (line 545 vs 559)
The GUI map draw sends NULL dates. The download path resolves nulls via
`_effective_dates(payload.dates, lookback)`, but self-downloading wrappers
(wrap_floodpy) build `common` with raw `payload.dates` and skip that branch. So
nulls reached wrap_floodpy, which fell back to its own blind 90-day window —
observed as 2026-05..07, no storm, an honest "no rainfall event" for a window
the user never chose.

## The fix
- `SELF_DOWNLOAD_LOOKBACK = {"wrap_floodpy": 3, "wrap_licsbas": 3}` — per-wrapper
  months for wrappers that self-download and thus miss the download-path
  resolution.
- When building `common`, if the wrapper is in that table, resolve dates through
  the SAME `_effective_dates` used by downloads. Explicit dates pass straight
  through (a resolved range is a no-op), so the CLI Storm-Daniel test with
  2023-07-01..09-30 is unaffected.

MVP decision (agreed): flood defaults to the recent 3 months. This is a stopgap
— see backlog "Flood default date window" and "Pre-analysis to auto-determine
the right window". The proper answer is the frontend date picker plus event-aware
windowing so "was there flooding here?" can find the significant event itself.

## Apply
    cd /geo && tar xzf geohazard-chat-m4.2c-fix3.tar.gz
    python3 scripts/m42c_flood_dates_patch.py services/backend/app/main.py
    #   expect: "wrap_floodpy null dates -> 3-month lookback (MVP)"
    docker compose build backend && docker compose up -d backend

## Verify
GUI flood query (null dates) now screens the recent 3 months:
    docker compose logs -f flood-worker
    # PROGRESS 4 checking rainfall... over a window ending ~today, not a blind one

To reproduce Storm Daniel specifically (until the frontend picker lands), use
the CLI/API with explicit dates — those pass through unchanged:
    curl ... -d '{... "dates":{"start":"2023-07-01","end":"2023-09-30"} ...}'

## Verified in-sandbox
- GUI null dates -> today-90d..today (90-day span).
- Explicit 2023 dates pass through unchanged.
- Idempotent.
