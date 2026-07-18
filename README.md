# geohazard-chat

Open-source, self-hostable chat system that answers plain-language questions
about ground hazards (subsidence, landslides, floods) for a drawn area, using
free public satellite data (Copernicus Sentinel, EGMS, COMET-LiCSAR, ASF HyP3).

**It is a first-look triage tool — not an early-warning system, not an official
hazard assessment, and not a substitute for a professional site survey.**

Architecture contract: `geohazard-chat-technical-reference-v2.md` (kept in the
project workspace; §6 of it is encoded in `libs/contracts/`).

## Status: M0 — contracts & scaffolding ✅

What exists:

| Piece | Where |
|---|---|
| §6 contracts as Pydantic models + tests | `libs/contracts/` |
| Generated JSON Schemas | `contracts/schemas/*.schema.json` |
| DB schema (§5.5) | `db/migrations/001_init.sql` |
| Queue topology incl. DLQ (§5.6) | `libs/contracts/geohazard_contracts/queues.py` |
| All 7 services wired in Compose (§4.1) | `docker-compose.yml` |
| Backend: `/health`, contract-validating `POST /query`, `GET /status/{id}` | `services/backend/` |
| Frontend/worker/downloader boot stubs | `services/{frontend,worker,downloader}/` |

Not yet: routing, LLM calls, any real analysis. That's M1+ (§11).

## Run (M0 smoke test)

```bash
cp .env.example .env         # set POSTGRES_PASSWORD and RABBITMQ_DEFAULT_PASS at minimum
docker compose up --build -d
docker compose ps            # db + broker healthy, backend healthy after ~20s

# 1. Health — expect "healthy": true
curl -s localhost:8000/health | python3 -m json.tool

# 2. Submit a contract-valid query (square near Yerevan)
curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' -d '{
  "question": "is the ground moving here?",
  "aoi": {"type":"Polygon","coordinates":[[[44.50,40.20],[44.60,40.20],[44.60,40.10],[44.50,40.10]]]},
  "dates": {"start": null, "end": null},
  "depth": "standard",
  "expert_raw": false
}'
# -> {"query_id":"...","status":"received"}

# 3. Read it back
curl -s localhost:8000/status/<query_id> | python3 -m json.tool

# 4. Invalid AOI (self-intersecting bow-tie) — expect 422
curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' -d '{
  "question": "x",
  "aoi": {"type":"Polygon","coordinates":[[[0,0],[1,1],[1,0],[0,1]]]}
}'

# Frontend stub: http://<host>:8501   RabbitMQ UI: http://<host>:15672
```

## Contracts workflow (§11.3)

- `libs/contracts/` is the single source of truth in code; the technical
  reference §6 is the source of truth in prose. Change the document first.
- After any model change: `python3 scripts/generate_schemas.py` and commit
  the regenerated `contracts/schemas/`.
- Tests: `cd libs/contracts && pip install -e . && pytest tests/`.

## Layout

```
libs/contracts/          shared package: enums, geometry/aoi_hash, §6 models, queue topology
contracts/schemas/       generated JSON Schemas (committed)
db/migrations/           numbered SQL, auto-applied on first postgres init
services/backend/        FastAPI (router/orchestrator)
services/frontend/       Streamlit (map+chat from M1)
services/downloader/     eodag/HyP3/LiCSAR/EGMS fetchers (from M2)
services/worker/         analysis wrappers via subprocess (from M2)
scripts/                 generate_schemas.py, future ops scripts
data/                    /archive (HDD), /scratch (SSD), /results (HDD) — §6.5
LICENSES/                third-party license bundle (§10)
```

## License

GPL-3.0 (see `LICENSE`). Third-party attributions: `LICENSES/`.
Answers produced by the system carry the data attributions required by §10.
