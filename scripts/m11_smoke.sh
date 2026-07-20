#!/bin/sh
# M1.1 acceptance script. Run from repo root after: docker compose up --build -d
set -e
AOI='{"type":"Polygon","coordinates":[[[44.50,40.20],[44.60,40.20],[44.60,40.10],[44.50,40.10]]]}'

echo "== 1. happy path =="
QID=$(curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":\"is the ground moving here?\",\"aoi\":$AOI}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])')
echo "query_id=$QID"
for i in 1 2 3 4 5 6 7 8; do
  sleep 3
  curl -s localhost:8000/status/$QID | python3 -c 'import sys,json;d=json.load(sys.stdin);p=d["progress"];print(d["status"], p[-1]["percent"] if p else "-", p[-1]["message"] if p else "-")'
done
echo "-- final result --"
curl -s localhost:8000/result/$QID | python3 -m json.tool

echo "== 2. failure path (retry x3 -> DLQ -> failed answer) =="
FID=$(curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":\"FAIL! test the retry path\",\"aoi\":$AOI}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])')
echo "query_id=$FID (watch: docker compose logs -f worker)"
sleep 45
curl -s localhost:8000/result/$FID | python3 -m json.tool
echo "-- DLQ should now hold 1 message --"
docker compose exec broker rabbitmqctl list_queues name messages | grep dlq
