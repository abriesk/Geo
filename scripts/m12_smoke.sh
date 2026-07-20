#!/bin/sh
# M1.2 acceptance (API side). The UI part is a manual checklist — see below.
set -e
AOI='{"type":"Polygon","coordinates":[[[44.50,40.20],[44.60,40.20],[44.60,40.10],[44.50,40.10]]]}'

echo "== 0. health incl. LLM probe =="
curl -s localhost:8000/health | python3 -m json.tool

echo "== 1. English question -> LLM answer =="
QID=$(curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":\"is the ground moving here?\",\"aoi\":$AOI}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])')
echo "query_id=$QID  (dummy ~10s + LLM up to ~60s)"
for i in $(seq 1 30); do
  sleep 3
  S=$(curl -s localhost:8000/status/$QID | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  echo "  status: $S"
  [ "$S" = "done" ] && break
  [ "$S" = "failed" ] && break
done
curl -s localhost:8000/result/$QID | python3 -c 'import sys,json;d=json.load(sys.stdin);print("--- ANSWER ---");print(d["answer"])'

echo "== 2. Russian question -> answer must be in Russian (SS8.3 rule 7) =="
RID=$(curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d "{\"question\":\"Здесь есть проседание грунта? Насколько это достоверно?\",\"aoi\":$AOI}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])')
for i in $(seq 1 30); do
  sleep 3
  S=$(curl -s localhost:8000/status/$RID | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$S" = "done" ] && break; [ "$S" = "failed" ] && break
done
curl -s localhost:8000/result/$RID | python3 -c 'import sys,json;d=json.load(sys.stdin);print("--- ОТВЕТ ---");print(d["answer"])'

echo ""
echo "== 3. MANUAL UI CHECKLIST (http://<host>:8501) =="
echo "  [ ] sidebar shows backend healthy + LLM ok"
echo "  [ ] draw rectangle near Yerevan -> 'AOI captured'"
echo "  [ ] ask 'is the ground moving here?' -> progress bar climbs"
echo "  [ ] answer appears in chat with the placeholder velocity map PNG"
echo "  [ ] answer mentions ~-4.1 mm/yr, moderate confidence, SYNTHETIC caveat"
echo "  [ ] answer does NOT say 'safe'/'don't worry' (SS8.3)"
echo "  [ ] ask in Russian -> answer in Russian"
echo "  [ ] disclaimer visible top and bottom"
