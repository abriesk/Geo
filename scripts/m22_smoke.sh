#!/bin/sh
# M2.2 acceptance: real CDSE download + cache. Requires CDSE creds in .env.
# NOTE: first run downloads ~2 GB of Sentinel-2 data — allow 5-20+ min.
set -e
AOI='{"type":"Polygon","coordinates":[[[44.50,40.20],[44.60,40.20],[44.60,40.10],[44.50,40.10]]]}'
Q='"Did vegetation disappear in this area over the last year?"'

ask() { curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":$1,\"aoi\":$AOI,\"depth\":\"quick\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])'; }
watch() {
  for i in $(seq 1 120); do
    sleep 15
    D=$(curl -s localhost:8000/status/$1)
    S=$(echo "$D" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
    P=$(echo "$D" | python3 -c 'import sys,json;p=json.load(sys.stdin)["progress"];print(p[-1]["message"][:70] if p else "-")')
    echo "  $S | $P"
    [ "$S" = "done" ] && break; [ "$S" = "failed" ] && break; [ "$S" = "needs_clarification" ] && break
  done
}

echo "== 1. vegetation query -> download via CDSE -> analysis (dummy until M2.3) =="
Q1=$(ask "$Q"); echo "query_id=$Q1"
watch $Q1
curl -s localhost:8000/status/$Q1 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("tasks:",[(t["kind"],t["name"],t["status"]) for t in d["tasks"]])'

echo "== 2. cache row exists =="
docker compose exec db psql -U geohazard -d geohazard \
  -c "SELECT aoi_hash, product_type, dates_start, dates_end, expiry_ts::date FROM cached_data;"

echo "== 3. archive populated =="
ls -la data/archive/s2/*/*/ | head -20

echo "== 4. SAME query again -> cache HIT, no new download, fast =="
Q2=$(ask "$Q"); echo "query_id=$Q2"
watch $Q2
curl -s localhost:8000/status/$Q2 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("tasks:",[(t["kind"],t["name"],t["status"]) for t in d["tasks"]])'
echo "   expect: NO download task in the list; backend log shows 'cache HIT'"
echo "   check:  docker compose logs backend | grep cache"

echo "== 5. old 'tasks' queue can now be deleted =="
echo "   docker compose exec broker rabbitmqctl delete_queue tasks"
