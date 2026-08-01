# geohazard-chat
**Open-source, local-first first-look triage tool for ground hazards**  
(subsidence / landslides, floods, vegetation loss) driven by free public satellite data.

This is the coordination README reflecting the state of the codebase as of the current archive (post-M5.1). The normative architecture contract remains `geohazard-chat-technical-reference-v2.md`.

---

## Mission

Give people in seismically and geohazard-active areas a tool to ask plain-language questions about ground hazards for a specific area and receive an honest, confidence-qualified answer derived from free Copernicus / COMET / EGMS / ASF data — without needing geoscience expertise.

It is a **first-look triage tool**, not an early-warning system, not an official hazard assessment, and not a substitute for a professional site survey. Answers report observations, never safety verdicts.

---

## Current status (what works)

The system is past the walking skeleton and has real end-to-end paths for all three MVP hazards. A non-expert can draw an AOI, ask a natural-language question, and receive a map + plain-language answer with confidence qualifiers and attribution.

### Working end-to-end paths

| Hazard | Method | Status | Notes |
|--------|--------|--------|-------|
| Ground deformation / subsidence | **EGMS** (tier 1) | Live | EU/EEA footprint. L3 ORTHO-UP vertical velocities. Fast. |
| Ground deformation / subsidence | **LiCSAR + LiCSBAS** (tier 2) | Live | Global priority zones (incl. Caucasus/Armenia). Self-downloading wrapper. Coverage pre-check + honest `no_data`. |
| Flood extent | **FLOODPY** (statistical mode) | Live | Dedicated `flood-worker` (SNAP + FLOODPY). ERA5 event probe → baseline/flood windows. |
| Vegetation loss / bare-soil | **Sentinel-2 NDVI change** | Live | CDSE via eodag, SCL cloud masking, quality block. |

- **Depth selector** (quick / standard / thorough) controls method fan-out. For European AOIs, thorough can run EGMS + LiCSBAS for cross-validation.
- **LLM intent router** (constrained JSON + rule fallback + clarification response) + **answer synthesis** with hard §8.3 constraints (observations only, confidence + caveats, never “safe/unsafe”).
- **NO_DATA handling** (M5.1): wrappers can return `status: "no_data"` + `no_data_reason: "measured_absence" | "no_coverage"`. These are legitimate answers (exit 0), not failures. Permanent vs transient errors classified; non-transient go to DLQ on attempt 1.
- **Caching** exists for S2 / EGMS products (aoi_hash + date range). InSAR interferogram re-download still happens per query (known expensive gap).
- **Frontend**: Streamlit + streamlit-folium map (draw polygon/rectangle), chat, depth & optional date range, 3 s polling, results + PNG artifacts, persistent disclaimer.
- **Queues**: `tasks.download`, `tasks.analysis`, `tasks.flood` (dedicated for FLOODPY), `progress`, `results` + DLQs. Prefetch 1, manual ack, retry limit 3 for transients.

HyP3 (tier 3) and local raw InSAR (tier 4 / expert mode) remain stubs / post-MVP (M6).

### Milestone progress vs technical reference §11

| Milestone | Status | Key deliverables present |
|-----------|--------|--------------------------|
| **M0** Contracts & scaffolding | ✅ Done | Pydantic contracts package + JSON schemas, DB migration, Compose, queues, directory layout |
| **M1** Walking skeleton | ✅ Done | Frontend map/chat/poll, backend endpoints, LLM synthesis, dummy worker |
| **M2** Router + NDVI | ✅ Done | Intent parse, eodag CDSE, `wrap_ndvi`, S2 cache |
| **M3** LiCSAR + LiCSBAS | ✅ Done | Frame catalog + resolver, `wrap_licsbas` (steps + quality), coverage checks |
| **M4** EGMS + FLOODPY + depth | ✅ Done | `wrap_egms` + EGMS download, `wrap_floodpy` in flood-worker, depth fan-out, multi-method synthesis |
| **M5.1** Deterministic failure + NO_DATA | ✅ Done | `ResultStatus.NO_DATA` + reasons, permanent-error fast-path to DLQ, live-verified measured_absence / no_coverage paths |
| **M5.2+** Hardening remainder | ⏳ In progress / planned | Synthesis rework, AOI coverage fraction reporting, NDVI confidence caps, numeric validator, InSAR download cache, retention job, concurrent soak, frontend polish |
| **M6** Expert raw / HyP3 | Post-MVP | — |

**MVP Definition of Done** (§11.1) is substantially met for the primary deformation path and the other two hazards. Remaining M5 work is robustness, cost reduction, and answer-quality polish rather than missing core capability.

---

## Architecture (as implemented)

```
frontend (Streamlit :8501)
    ↓ POST /query  ·  GET /status/{id}  ·  GET /result/{id}
backend (FastAPI :8000)
    ├── intent router (LLM + rules)
    ├── deformation ladder (EGMS → LiCSBAS)
    ├── cache probe (aoi_hash)
    ├── task orchestration (PostgreSQL + RabbitMQ)
    └── LLM synthesis (LiteLLM → external OpenAI-compatible server)
         ↓
downloader          worker                 flood-worker
(tasks.download)    (tasks.analysis)       (tasks.flood)
  · CDSE (S2)         · wrap_ndvi            · wrap_floodpy
  · EGMS archive      · wrap_egms              (SNAP + FLOODPY)
                      · wrap_licsbas           ERA5 event logic
                        (self-download)
```

- **LLM** is external (koboldcpp / any OpenAI-compatible). Configured via `LLM_BASE_URL` / `LLM_MODEL`. Locations stay local unless a commercial provider is deliberately configured.
- **Storage split** (recommended): SSD for Postgres, RabbitMQ, scratch; HDD for `/data/archive` and `/data/results`.
- **Contracts** live in `libs/contracts/` (installable package `geohazard-contracts`). Schemas also under `contracts/schemas/`. Any change to §6 is a breaking change.

---

## Quick start (operator)


# 1. Clone / unpack, create .env
cp .env.example .env   # or create with at least:
#   POSTGRES_PASSWORD=...
#   RABBITMQ_DEFAULT_PASS=...
#   LLM_BASE_URL=http://<llm-host>:5001/v1
#   LLM_MODEL=<model as reported by the server>
#   CDSE_USERNAME=... CDSE_PASSWORD=...   # for S2 / flood
#   (optional) EARTHDATA_* for future HyP3

# 2. Place secrets
#   secrets/cdsapirc          # for flood-worker ERA5 if used
#   secrets/clms_key.json     # only if still needed for residual CLMS probes

# 3. Build & run
docker compose up -d --build

# 4. Open
#   Frontend  http://localhost:8501
#   Backend   http://localhost:8000/health
#   RabbitMQ  http://localhost:15672

Smoke scripts live under `scripts/` (`m51_smoke.sh` is the current robustness acceptance).

### Useful environment knobs

| Variable | Default / notes |
|----------|-----------------|
| `MAX_AOI_KM2` | 1000 |
| `WORKER_CONCURRENCY` | 2 |
| `EGMS_ENABLED` | true (kill-switch forces LiCSBAS) |
| `DEFORM_DUAL_METHOD_MIN_DEPTH` | thorough (standard stays EGMS-only in EU) |
| `DATA_RETENTION_DAYS` | 30 (cleanup job still M5) |
| `EXPERT_RAW_PROCESSING` | false |

---

## Project layout (high level)

```
geo/
├── contracts/schemas/          # JSON Schema mirrors of §6
├── libs/
│   ├── contracts/              # geohazard-contracts (Pydantic source of truth)
│   ├── licsar/frames.py        # AOI → LiCSAR frame resolver
│   └── egms/footprint.py       # EGMS coverage test
├── services/
│   ├── backend/app/            # FastAPI + router + synthesis
│   ├── frontend/               # Streamlit UI
│   ├── downloader/             # CDSE + EGMS download consumers
│   ├── worker/                 # analysis wrappers (ndvi, egms, licsbas)
│   └── flood-worker/           # SNAP + FLOODPY isolated consumer
├── db/migrations/001_init.sql
├── scripts/                    # catalog builder, smoke tests, one-shot patches
├── static/licsar_frames.geojson
├── docs/                       # per-milestone notes + BACKLOG.md
└── docker-compose.yml
```

---

## Known gaps & next coordination points

See `docs/BACKLOG.md` and `docs/M5_PLAN.md` for the authoritative deferred list. Highest-value remaining items for the MVP line:

1. **M5.2 synthesis / wording pass** — AOI analysed-fraction reporting, distinct NO_DATA language, NDVI confidence cap, post-synthesis numeric check, `schema_version` + `wrapper_version` stamps.
2. **InSAR frame download cache** — currently the largest latency & disk cost for repeated deformation queries.
3. **Retention / cleanup job** and concurrent-query soak.
4. **Frontend** — Nominatim search box, multi-hazard example prompts, polling backoff.
5. **Date pre-flight** (especially flood event probe tightening) so the system never runs expensive analysis over a dry window when it can know better.

Post-MVP (M6): HyP3 on-demand + MintPy, local raw Stage-1 behind the expert switch, SPA + SSE, etc.

---

## Compliance & licence

- Project: **GPL-3.0** (compatible with LiCSBAS, MintPy, FLOODPY).
- Every answer carries attribution assembled from the methods that actually ran.
- Fixed UI disclaimer (outside LLM control):  
  *“This is an automated first-look analysis of public satellite data. It is not a safety assessment or an official hazard evaluation.”*
- Third-party licences are to be collected under `LICENSES/` as each tool is integrated.

---


For the full architecture, message contracts, resource budgets, and the original milestone order, read the technical reference. For the live deferred list and scientific caveats, read `docs/BACKLOG.md`.
