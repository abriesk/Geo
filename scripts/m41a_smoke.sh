#!/bin/sh
# M4.1a acceptance — routing + analysis (no CLMS needed; that's M4.1b).
set -e
PARIS='{"type":"Polygon","coordinates":[[[2.31,48.83],[2.39,48.83],[2.39,48.89],[2.31,48.89],[2.31,48.83]]]}'

echo "== 1. ROUTING: Paris -> EGMS tier 1 (watch backend log) =="
QID=$(curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":\"is the ground moving here?\",\"aoi\":$PARIS,\"depth\":\"quick\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])')
echo "query_id=$QID"
sleep 2
docker compose logs --since 30s backend | grep -iE "\[router\] deformation tier" | tail -3
echo "   expect: 'deformation tier 1: EGMS (AOI within footprint)'"
echo "   (the query will then try the EGMS download and fail without a CLMS key — that's M4.1b)"

echo ""
echo "== 2. ANALYSIS: wrap_egms on a synthetic EGMS tile (proves the analysis path) =="
mkdir -p /tmp/egms_syn && python3 - << 'PY'
import json, random
random.seed(1); feats=[]
for i in range(600):
    lon=2.30+random.random()*0.10; lat=48.82+random.random()*0.08
    vel=random.gauss(-1.0,3.0)
    feats.append({"type":"Feature","properties":{"mean_velocity":vel,
        "20180106":0.0,"20190112":vel*0.3,"20211231":vel*0.9},
        "geometry":{"type":"Point","coordinates":[lon,lat]}})
json.dump({"type":"FeatureCollection","features":feats}, open("/tmp/egms_syn/EGMS_L2b_tile.geojson","w"))
json.dump({"type":"Polygon","coordinates":[[[2.31,48.83],[2.39,48.83],[2.39,48.89],[2.31,48.89],[2.31,48.83]]]}, open("/tmp/egms_syn/aoi.json","w"))
print("synthetic EGMS tile + AOI written")
PY
docker compose run --rm -v /tmp/egms_syn:/syn --entrypoint python worker \
  wrappers/wrap_egms.py --query-id 11111111-1111-1111-1111-111111111111 \
  --aoi /syn/aoi.json --input-dir /syn --output-dir /syn/out --params '{}'
echo "-- result.json --"
python3 -c 'import json;r=json.load(open("/tmp/egms_syn/out/result.json"));print("method:",r["method"],"status:",r["status"]);print("date_coverage:",r["quality"]["date_coverage"]);print("stats:",json.dumps(r["summary_stats"]["deformation"]));print("confidence:",r["quality"]["confidence"]);print("caveats:",len(r["quality"]["caveats"]),"items")'
echo "   expect: method=egms, status=ok, date_coverage=['2018-01-06','2021-12-31'] (from epochs, not 'unknown')"

echo ""
echo "== 3. KILL SWITCH: EGMS_ENABLED=false -> Paris falls back to LiCSBAS =="
echo "   set EGMS_ENABLED=false in .env / compose, 'docker compose up -d backend', re-run step 1;"
echo "   expect: 'deformation tier 2: LiCSBAS'"
