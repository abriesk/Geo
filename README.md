# geohazard-chat

Open-source, self-hostable chat system that answers plain-language questions
about ground hazards (subsidence, landslides, floods, vegetation change) for a
drawn area, using free public satellite data (Copernicus Sentinel, EGMS,
COMET-LiCSAR, ASF HyP3).

**It is a first-look triage tool — not an early-warning system, not an official
hazard assessment, and not a substitute for a professional site survey.**

Architecture contract: `geohazard-chat-technical-reference-v2.md` (§6 of it is
encoded in `libs/contracts/`).

## Status: M3.4 — real InSAR + NDVI + LLM router/synthesis ✅

What exists and is exercised end-to-end:

| Piece | Where / notes |
|---|---|
| §6 contracts as Pydantic models + tests + generated JSON Schemas | `libs/contracts/`, `contracts/schemas/` |
| DB schema (§5.5) + auto-applied migrations | `db/migrations/001_init.sql` |
| Queue topology (analysis / download / progress / results + DLQ) | `libs/contracts/geohazard_contracts/queues.py` |
| Full Compose stack (db, broker, backend, frontend, downloader, worker) | `docker-compose.yml` |
| Backend: health, contract-validating `POST /query`, `GET /status/{id}`, `GET /result/{id}`, static file serving | `services/backend/` (v0.8.0-m3.3) |
| LLM intent parsing (JSON, temp 0, rule fallback, clarification) + answer synthesis with §8.3 constraints + sanitizers | `services/backend/app/llm.py` (LiteLLM / any OpenAI-compatible server) |
| Cache-aware routing + download→analysis chaining (vegetation) | backend router |
| AOI→LiCSAR frame resolution from global catalog + multi-candidate coverage pre-check | `libs/licsar/frames.py` + `data/licsar_frames.geojson` + `wrap_licsbas` |
| Real `wrap_licsbas` (self-downloading LiCSBAS 01→16, velocity stats with significance gating, calibrated confidence, clean no-data path) | `services/worker/wrappers/wrap_licsbas.py` |
| Real `wrap_ndvi` (S2 L2A NDVI change, SCL cloud mask, dNDVI, quality block) | `services/worker/wrappers/wrap_ndvi.py` |
| CDSE downloader via eodag (S2 L2A, before/after scene selection, cache upsert, quota backoff) | `services/downloader/downloader_main.py` |
| Frontend: Streamlit + folium map (draw AOI) + chat + progress polling + result images | `services/frontend/app.py` |
| Heartbeat-safe long-running consumers (worker + downloader) | both mains |
| Smoke / milestone notes | `scripts/m*.sh`, `scripts/m*_notes.md` |

Still ahead (per §11 of the technical reference):

- M4: FLOODPY, EGMS, full multi-method depth fan-out + cross-validation phrasing
- M5: hardening (retention job, DLQ UI, soak tests, edge cases)
- M6: HyP3 + MintPy, expert raw mode

Dummy wrappers remain for flood (and temporarily for any non-deformation / non-vegetation path).

## Quick start

bash
cp .env.example .env
# Required: POSTGRES_PASSWORD, RABBITMQ_DEFAULT_PASS, LLM_BASE_URL, LLM_MODEL
# For NDVI: CDSE_USERNAME / CDSE_PASSWORD
# Optional: DEFAULT_DEFORM_FRAME (fallback only — catalog is preferred)

docker compose up --build -d
docker compose ps          # wait until backend is healthy (~20–40 s)

# Health
curl -s localhost:8000/health | python3 -m json.tool

# Example query (square near Yerevan) — deformation path uses LiCSBAS
curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' -d '{
  "question": "is the ground moving here?",
  "aoi": {"type":"Polygon","coordinates":[[[44.50,40.20],[44.60,40.20],[44.60,40.10],[44.50,40.10]]]},
  "dates": {"start": null, "end": null},
  "depth": "standard",
  "expert_raw": false
}'

# Poll
curl -s localhost:8000/status/<query_id> | python3 -m json.tool
# Final answer + artifacts
curl -s localhost:8000/result/<query_id> | python3 -m json.tool

# UI: http://<host>:8501
# RabbitMQ management: http://<host>:15672

LiCSBAS runs can take 20–90 min (download + inversion). NDVI is minutes once scenes are cached. Progress is visible in the UI and via /status.
Contracts workflow (§11.3)

libs/contracts/ is the single source of truth in code; the technical reference §6 is the source of truth in prose. Change the document first.
After any model change: python3 scripts/generate_schemas.py and commit the regenerated contracts/schemas/.
Tests: cd libs/contracts && pip install -e . && pytest tests/.

Layout
libs/contracts/          shared package: enums, geometry/aoi_hash, §6 models, queue topology
libs/licsar/             AOI → LiCSAR frame resolver (catalog-driven)
contracts/schemas/       generated JSON Schemas (committed)
db/migrations/           numbered SQL, auto-applied on first postgres init
services/backend/        FastAPI (router, orchestrator, LLM, status API)
services/frontend/       Streamlit + streamlit-folium (map + chat + progress)
services/downloader/     eodag CDSE (S2) — other tiers stubbed until their milestones
services/worker/         analysis wrappers via subprocess (wrap_licsbas, wrap_ndvi live)
scripts/                 generate_schemas, build_licsar_catalog, smoke tests, milestone notes
data/                    archive / scratch / results + licsar_frames.geojson
static/                  small static assets (incl. a lightweight frames sample)
LICENSES/                third-party license bundle (§10)

Key design notes (current)

InSAR path is self-downloading inside wrap_licsbas (LiCSBAS step 01 owns acquisition). The downloader service is used only for the optical (Sentinel-2) path.
Frame selection uses a shipped global catalog (data/licsar_frames.geojson) built by scripts/build_licsar_catalog.py. Multiple candidates are probed for temporal coverage before a long run is started; dead frames are skipped cleanly.
LLM failures never hang a query: synthesis falls back to a deterministic template with a visible warning.
All wrappers emit PROGRESS <pct> <msg> and produce a validated result.json (§6.3) + PNG artifacts.
