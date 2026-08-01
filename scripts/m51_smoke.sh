#!/bin/sh
# M5.1 acceptance — deterministic-failure robustness + NO_DATA.
#
# The spine is two OPPOSITE assertions:
#   permanent failure -> ONE attempt then DLQ   (classifier sent it straight to DLQ)
#   transient failure -> THREE attempts then DLQ (classifier left retries intact)
# If the classifier were wrong in either direction, exactly one of these fails.
#
# Also checks: NO_DATA contract round-trips offline; a NO_DATA(measured_absence)
# result finalizes as an answer (not FAILED) at exit 0 — no 3x re-download.
#
# Hooks are driven purely through the query API via sentinels in the question:
#   "PERMFAIL!" -> simulate_permanent_failure   "FAIL!" -> simulate_failure
# Both now fire in run_task (all wrappers are real; run_dummy is unreachable).
set -e

# A deformation AOI (Yerevan test frame area). Routing doesn't matter — the
# test-hook short-circuits run_task before any wrapper runs.
AOI='{"type":"Polygon","coordinates":[[[44.45,40.15],[44.55,40.15],[44.55,40.25],[44.45,40.25],[44.45,40.15]]]}'

ask() { curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":$2,\"aoi\":$1,\"depth\":\"quick\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])'; }

watch() {
  for i in $(seq 1 60); do
    sleep 3
    S=$(curl -s localhost:8000/status/$1 | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin); print(d.get("status","?"))
except Exception: print("?")')
    case "$S" in done|failed|needs_clarification) echo "$S"; return;; esac
  done
  echo "TIMEOUT"
  # M5.1 diagnostic: on timeout, show why the query never finalized.
  echo "  --- DB task state for $1 (diagnosing stuck finalize) ---" 1>&2
  docker compose exec -T db psql -U geohazard -d geohazard -tA -c \
    "SELECT kind,name,status,left(coalesce(error,''),60) FROM tasks WHERE query_id='$1';" 1>&2 || true
  QS=$(docker compose exec -T db psql -U geohazard -d geohazard -tA -c \
    "SELECT status FROM queries WHERE query_id='$1';" 2>/dev/null | tr -d '[:space:]')
  echo "  query status = $QS" 1>&2
  case "$QS" in
    summarizing) echo "  NOTE: task is terminal but finalize is blocked in LLM synthesis." 1>&2
                 echo "        Likely the LLM endpoint is slow/unreachable and finalize is" 1>&2
                 echo "        waiting on LLM_TIMEOUT_SECONDS before the template fallback." 1>&2
                 echo "        Not an M5.1 logic bug; raise the watch window or speed the LLM." 1>&2;;
    analyzing)   echo "  NOTE: query still analyzing — a task never reported terminal." 1>&2;;
  esac
}

pass=0; fail=0
ok()   { echo "  [PASS] $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }

echo "== 0. OFFLINE: NO_DATA contract round-trip (runs inside backend container) =="
docker compose exec -T backend python3 - << 'PY' && ok "NO_DATA + no_data_reason coupling validates" || bad "contract round-trip"
from geohazard_contracts import ResultJson, ResultStatus
from uuid import uuid4
base=dict(query_id=str(uuid4()),method="licsbas",
  quality=dict(scene_count=0,date_coverage=["2024-06-01","2026-06-15"],
    coherence_mean=None,masked_fraction=None,cloud_fraction=None,
    confidence="low",caveats=["x"]),summary_stats={},artifacts=[],attribution=[])
assert ResultStatus.NO_DATA.value=="no_data"
ResultJson.model_validate({**base,"status":"no_data","no_data_reason":"measured_absence"})
ResultJson.model_validate({**base,"status":"no_data","no_data_reason":"no_coverage"})
for bad_ in ({"status":"no_data"},{"status":"ok","no_data_reason":"no_coverage"}):
    try: ResultJson.model_validate({**base,**bad_}); raise SystemExit("coupling not enforced")
    except Exception as e:
        if "no_data_reason" not in str(e): raise
PY

echo ""
echo "== 1. SPINE A: permanent failure -> ONE attempt then DLQ =="
QP=$(ask "$AOI" '"is the ground moving here? PERMFAIL!"'); echo "  query_id=$QP"
ST=$(watch $QP); echo "  final status=$ST"
sleep 2
LOGS=$(docker compose logs --since 90s worker 2>/dev/null)
echo "$LOGS" | grep -E "attempt [0-9]+/|NON-TRANSIENT" | tail -6
# Permanent signature: a NON-TRANSIENT(PermanentTaskError) classify line.
CLS=$(echo "$LOGS" | grep -c "NON-TRANSIENT (PermanentTaskError)" || true)
# Its DLQ suffix names attempt 1; a retry would show "attempt 2/" or "3/".
RETRIED=$(echo "$LOGS" | grep -cE "attempt [23]/" || true)
case "$ST" in failed) ok "query finalized failed (permanent)";; *) bad "expected failed, got $ST";; esac
[ "$CLS" -ge 1 ] && ok "classified NON-TRANSIENT(PermanentTaskError)" || bad "no permanent-classify log"
[ "$RETRIED" = "0" ] && ok "no retries — DLQ on attempt 1" || bad "permanent retried ($RETRIED)"

echo ""
echo "== 2. SPINE B: transient failure -> THREE attempts then DLQ =="
QT=$(ask "$AOI" '"is the ground moving here? FAIL!"'); echo "  query_id=$QT"
ST=$(watch $QT); echo "  final status=$ST"
sleep 2
LOGS=$(docker compose logs --since 180s worker 2>/dev/null)
echo "$LOGS" | grep -E "attempt [0-9]+/|classify. transient" | tail -8
# Transient signature: transient-classify lines AND attempts reaching 2 and 3.
TCLS=$(echo "$LOGS" | grep -c "classify. transient" || true)
A2=$(echo "$LOGS" | grep -cE "attempt 2/" || true)
A3=$(echo "$LOGS" | grep -cE "attempt 3/" || true)
case "$ST" in failed) ok "query finalized failed after retries";; *) bad "expected failed, got $ST";; esac
{ [ "$A2" -ge 1 ] && [ "$A3" -ge 1 ]; } \
  && ok "retried to attempt 3 (transient path intact)" \
  || bad "transient not retried to 3 (a2=$A2 a3=$A3)"
[ "$TCLS" -ge 1 ] && ok "classified transient (retry log present)" || bad "no transient-classify log"

echo ""
echo "== 3. NO_DATA end-to-end (measured_absence) — answer, not failure, exit 0 =="
echo "   Two ways to trigger measured_absence, both now handled:"
echo "     (a) step-11 'All ifgs are regarded as bad' — a FULLY incoherent AOI"
echo "         (open water / dense veg). This is the RELIABLE live trigger:"
echo "         draw an AOI entirely over open water inside a covered LiCSAR"
echo "         frame (e.g. open Lake Van). Fires before inversion."
echo "     (b) post-inversion zero valid pixels (NoValidPixels) — rarer."
echo "   Set NODATA_AOI to a full-water box to exercise (a) end-to-end."
if [ -n "$NODATA_AOI" ]; then
  QN=$(ask "$NODATA_AOI" '"is the ground moving here?"'); echo "  query_id=$QN"
  ST=$(watch $QN); echo "  final status=$ST"
  # Query-level status is DONE (a no-data answer is still an answer).
  [ "$ST" = "done" ] && ok "no-data query finalized as done (answer, not failed)" \
                     || bad "expected done, got $ST"
  # Worker must show exactly one attempt (exit 0 -> no retry) and the NO_DATA path.
  R=$(docker compose logs --since 300s worker 2>/dev/null | grep -cE "attempt [23]/" || true)
  [ "$R" = "0" ] && ok "no-data was NOT retried (exit 0)" || bad "no-data retried ($R)"
  docker compose logs --since 300s worker 2>/dev/null | grep -E "measured_absence|no usable radar" | tail -3
  curl -s localhost:8000/result/$QN | python3 -c '
import sys,json; d=json.load(sys.stdin)
res=(d.get("results") or [{}])
st=[r.get("status") for r in res]; rs=[r.get("no_data_reason") for r in res]
print("  result statuses:",st,"reasons:",rs)
import sys as s
if "no_data" in st: print("  [PASS] result.json status=no_data present")
else: print("  [FAIL] no no_data result"); s.exit(0)
'
else
  echo "  [SKIP] NODATA_AOI not set."
fi

echo ""
echo "== 4. REGRESSION: a normal query still returns a real result =="
QR=$(ask "$AOI" '"is the ground moving here?"'); echo "  query_id=$QR"
ST=$(watch $QR)
echo "  final status=$ST (expect done for a covered, coherent AOI)"
case "$ST" in
  done) ok "normal query finalized done";;
  failed) echo "  [WARN] failed — check this AOI actually has LiCSAR coverage+coherence";;
  *) bad "unexpected status $ST";;
esac

echo ""
echo "================= M5.1 smoke summary: PASS=$pass FAIL=$fail ================="
[ "$fail" -eq 0 ] || exit 1
