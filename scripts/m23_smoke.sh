#!/bin/sh
# M2.3 acceptance: real NDVI on real Sentinel-2 data.
set -e
# AOI shifted 0.05 deg east of the M2.2 square -> forces a FRESH download
# through the fixed two-window scene picker (the old cache entry stays for test 3).
AOI_NEW='{"type":"Polygon","coordinates":[[[44.55,40.20],[44.65,40.20],[44.65,40.10],[44.55,40.10]]]}'
AOI_OLD='{"type":"Polygon","coordinates":[[[44.50,40.20],[44.60,40.20],[44.60,40.10],[44.50,40.10]]]}'
Q='"Did vegetation disappear in this area over the last year?"'

ask() { curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":$2,\"aoi\":$1,\"depth\":\"quick\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])'; }
watch() {
  for i in $(seq 1 120); do
    sleep 15
    D=$(curl -s localhost:8000/status/$1)
    S=$(echo "$D" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
    P=$(echo "$D" | python3 -c 'import sys,json;p=json.load(sys.stdin)["progress"];print(p[-1]["message"][:70] if p else "-")')
    echo "  $S | $P"
    [ "$S" = "done" ] && break; [ "$S" = "failed" ] && break
  done
}

echo "== 1. fresh AOI -> fixed scene picker -> real NDVI =="
Q1=$(ask "$AOI_NEW" "$Q"); echo "query_id=$Q1"
watch $Q1
echo "-- scene pair check: dates should be ~a year apart now --"
docker compose logs downloader 2>/dev/null | grep -E "search (early|late)" | tail -6
echo "-- result --"
curl -s localhost:8000/result/$Q1 | python3 -c '
import sys,json; d=json.load(sys.stdin)
print("ANSWER:"); print(d["answer"]); print()
print("artifacts:", [a["url"] for a in d["artifacts"]])'

echo "== 2. NDVI numbers straight from result.json =="
find data/results/$Q1 -name result.json -exec python3 -c '
import json,sys
r=json.load(open(sys.argv[1]))
print("method:",r["method"],"status:",r["status"])
print("stats:",json.dumps(r["summary_stats"]["ndvi"],indent=1))
print("quality:",json.dumps({k:v for k,v in r["quality"].items() if k!="caveats"}))
print("caveats:",r["quality"]["caveats"])' {} \;

echo "== 3. old AOI -> cache HIT -> NDVI on the M2.2 pair (55 days apart -> caveat) =="
Q3=$(ask "$AOI_OLD" "$Q"); echo "query_id=$Q3"
watch $Q3
curl -s localhost:8000/result/$Q3 | python3 -c '
import sys,json; d=json.load(sys.stdin)
print("ANSWER (expect the 55-days-apart caveat surfaced):"); print(d["answer"])'
echo ""
echo "== 4. MANUAL: open the UI, repeat query 1, view the three NDVI maps inline =="
