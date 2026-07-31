# M4.2c fix — complete the routing (main site) + worker guard

## What went wrong
The first backend patch injected `_analysis_route` and rewired the deferred-
release publish (line 258), but MISSED the main router's publish loop
(line ~620). That site is indented 8 spaces (inside a try/for); patch 1's
anchor assumed 4, so the replacement silently no-op'd. Patch 1 treated "one
site replaced" as success — a partial patch, which is worse than a clean abort.

Result seen live: a fresh flood query published wrap_floodpy to `tasks.analysis`
-> the main `worker` picked it up and ran `run_dummy` -> a fake "subsiding"
result ("fetching cached stack / inverting time series" in the logs).

## Two fixes

### 1. Complete the routing — `scripts/m42c_backend_patch2.py`
Rewrites the remaining main-router site (`for t in analysis_msgs:`, matched
byte-for-byte at 8-space indent) to `_analysis_route(t.name)`, then ASSERTS no
bare `routing_key=ANALYSIS_QUEUE` publish remains — so it cannot leave a partial
fix. Idempotent.

### 2. Worker guard — `scripts/m42c_worker_guard_patch.py`
Even with routing fixed, the main worker should never fake a flood result. If
wrap_floodpy ever reaches `tasks.analysis` (misroute, replay), run_task now
RAISES instead of dummying it — the task dead-letters and the misroute is
visible, rather than a confident synthetic "subsiding" lie. This is the deeper
safety fix: a wrong answer is worse than a failed one.

## Apply
```bash
# 1. complete backend routing
python3 scripts/m42c_backend_patch2.py services/backend/app/main.py
#    expect: "Rewired main-router publish site" + "no bare ... remain"

# 2. guard the main worker
python3 scripts/m42c_worker_guard_patch.py services/worker/worker_main.py
#    expect: "wrap_floodpy on tasks.analysis now fails loudly instead of faking"

# 3. rebuild BOTH (the worker container is 17h old and still dummies flood)
docker compose build backend worker
docker compose up -d backend worker

# 4. re-test with the <1000 km2 AOI
curl -s -X POST http://localhost:8000/query -H 'content-type: application/json' -d '{
  "question": "Was there flooding here?",
  "aoi": {"type":"Polygon","coordinates":[[[21.82,39.42],[22.12,39.42],[22.12,39.565],[21.82,39.565],[21.82,39.42]]]},
  "dates": {"start":"2023-07-01","end":"2023-09-30"},
  "depth": "quick", "expert_raw": false
}'
```

Now watch the RIGHT worker:
    docker compose logs -f flood-worker   # PROGRESS 4 checking rainfall... etc.
    docker compose logs -f worker         # should NOT show this task at all

Success = flood-worker runs it, status done, answer cites the flooded area +
map. The main worker rebuild matters: until then it still maps flood->dummy.

## Backlog raised
- The two-site publish routing is fragile to re-indentation. When the backend
  is next refactored, fold routing into a single publish helper so there is one
  site, not two. (Noted for M5 hardening.)
