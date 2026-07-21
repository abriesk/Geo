#!/bin/sh
# M3.2 acceptance: real InSAR deformation via LiCSBAS on frame 174A_05018_131313.
# WARNING: downloads many unwrapped interferograms + runs NSBAS inversion.
# Use a SHORT window first to validate the pipeline fast (~20-40 min), THEN
# a full 24-month run. Set the test frame in .env: DEFAULT_DEFORM_FRAME=174A_05018_131313
AOI='{"type":"Polygon","coordinates":[[[44.45,40.25],[44.60,40.25],[44.60,40.12],[44.45,40.12]]]}'

ask() { curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":\"is the ground moving in this area?\",\"aoi\":$AOI,\"dates\":{\"start\":\"$1\",\"end\":\"$2\"},\"depth\":\"quick\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])'; }

echo "== SHORT-WINDOW validation run (6 months) =="
QID=$(ask 2024-01-01 2025-07-01)
echo "query_id=$QID   (watch: docker compose logs -f worker)"
echo "polling every 30s (InSAR is slow — download + inversion)..."
for i in $(seq 1 240); do
  sleep 30
  D=$(curl -s localhost:8000/status/$QID)
  S=$(printf '%s' "$D" | python3 -c 'import sys,json;
try:
 d=json.loads(sys.stdin.buffer.read().decode("utf-8","replace")); p=d.get("progress") or []
 print(d.get("status"),"|",(p[-1]["message"][:60] if p else "-"))
except Exception as e: print("parse:",e)')
  echo "  [$i] $S"
  echo "$S" | grep -qE "^done|^failed" && break
done
echo "-- result --"
curl -s localhost:8000/result/$QID | python3 -c '
import sys,json; d=json.load(sys.stdin)
print("STATUS:",d["status"]); print("ANSWER:\n",d.get("answer","")); print("artifacts:",[a["url"] for a in d.get("artifacts",[])])'
echo "-- raw deformation stats --"
find data/results/$QID -name result.json -exec python3 -c '
import json,sys; r=json.load(open(sys.argv[1]))
print("method:",r["method"],"status:",r["status"])
print(json.dumps(r["summary_stats"],indent=1))
print("quality:",json.dumps({k:v for k,v in r["quality"].items() if k!="caveats"}))
print("caveats:",r["quality"]["caveats"])' {} \; 2>/dev/null
