#!/bin/sh
# M2.1 acceptance: real router (LLM intent + rule fallback + clarification).
# Run from repo root after: docker compose up --build -d backend frontend
set -e
AOI='{"type":"Polygon","coordinates":[[[44.50,40.20],[44.60,40.20],[44.60,40.10],[44.50,40.10]]]}'

ask() {  # $1=question -> prints query_id
  curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
    -d "{\"question\":$1,\"aoi\":$AOI,\"depth\":\"$2\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["query_id"])'
}
wait_done() {  # $1=query_id
  for i in $(seq 1 40); do
    sleep 3
    S=$(curl -s localhost:8000/status/$1 | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
    [ "$S" = "done" ] && break; [ "$S" = "failed" ] && break; [ "$S" = "needs_clarification" ] && break
  done
  echo "final status: $S"
}

echo "== 1. flood question (RU) -> router must pick flood, 1 task =="
Q1=$(ask '"Затопило ли этот район в прошлом месяце?"' standard)
wait_done $Q1
curl -s localhost:8000/status/$Q1 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("tasks:",[(t["name"],t["status"]) for t in d["tasks"]])'
echo "   (check backend log line: [router] ... intent: ['flood'])"

echo "== 2. multi-hazard question, depth=thorough -> up to 3 tasks =="
Q2=$(ask '"Is the ground moving here, and was there flooding, and did vegetation disappear?"' thorough)
wait_done $Q2
curl -s localhost:8000/status/$Q2 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("tasks:",[(t["name"],t["status"]) for t in d["tasks"]])'

echo "== 3. same question, depth=quick -> exactly 1 task =="
Q3=$(ask '"Is the ground moving here, and was there flooding, and did vegetation disappear?"' quick)
wait_done $Q3
curl -s localhost:8000/status/$Q3 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("tasks:",[(t["name"],t["status"]) for t in d["tasks"]])'

echo "== 4. unrelated question -> needs_clarification, no tasks =="
Q4=$(ask '"what is the meaning of life?"' standard)
wait_done $Q4
curl -s localhost:8000/result/$Q4 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("answer:",d["answer"][:120],"...")'

echo ""
echo "== 5. MANUAL: rule-fallback drill =="
echo "   Stop koboldcpp on the LLM host, then:"
echo '   curl -s -X POST localhost:8000/query -H "Content-Type: application/json" -d '"'"'{"question":"тут проседание грунта","aoi":'"$AOI"'}'"'"
echo "   -> backend log must show: LLM intent parse failed ... rule intent: ['deformation']"
echo "   -> query must still complete (answer will carry the LLM-unreachable warning)."
echo "   Restart koboldcpp afterwards."
