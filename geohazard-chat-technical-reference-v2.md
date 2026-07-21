# Technical Reference v2: Open-Source Geohazard Chat System

**Status:** Architecture contract for MVP build
**Supersedes:** v1 draft
**Purpose of this document:** Steering contract for iterative, LLM-assisted development. Defines architecture, message/file contracts, data-source routing, resource budgets, and the milestone build order. It is *not* a line-by-line code spec — wrapper implementations must be written against real upstream documentation (see §11.3).

---

## 1. Overview

### 1.1 Mission
Give people in seismically and geohazard-active areas a tool to ask plain-language questions about ground hazards (subsidence, landslides, floods) for a specific area — and get an honest, plain-language answer derived from free, publicly funded satellite data — without needing any geoscience expertise.

The system is a **first-look triage tool**, not an early-warning system, not an official hazard assessment, and not a substitute for a professional site survey. Its job is to tell a person either "there is a measurable signal here worth taking seriously — consult a professional" or "nothing anomalous is visible from space in this period," when the alternative is no information at all.

### 1.2 Supported hazards (MVP)
| Hazard | Primary data | Method |
|---|---|---|
| Ground deformation / subsidence / landslide predisposition | Sentinel-1 InSAR (pre-computed products preferred, see §3) | EGMS lookup / LiCSBAS time series / HyP3 interferograms / (expert) local processing |
| Flood extent | Sentinel-1 radar (+optional Sentinel-2) | FLOODPY statistical mode |
| Vegetation loss / bare-soil exposure (landslide proxy) | Sentinel-2 L2A optical | NDVI change detection with cloud masking |

### 1.3 Core principles
- **Local-first & self-hostable.** Default: everything (download, analysis, LLM) runs on the operator's hardware. No user coordinates leave the machine unless the operator explicitly configures a commercial LLM.
- **Open source.** Project license GPL-3.0 (compatible with LiCSBAS, MintPy, FLOODPY — all GPL-3.0). Third-party tools invoked via subprocess with attributions bundled in `LICENSES/`.
- **Free data only.** Copernicus/CDSE, COMET-LiCSAR, EGMS, ASF HyP3 — all free-with-account. Zero marginal cost per query.
- **Pre-computed before self-computed.** Never run expensive InSAR Stage 1 processing locally when a free pre-computed product covers the AOI (see §3 routing ladder). Local raw processing exists only behind an explicit expert-mode switch.
- **Honest uncertainty.** The system reports *observations with confidence qualifiers*, never safety verdicts. See §8.3 for hard LLM constraints.
- **Async, no timeouts.** Long tasks run through a durable queue; the UI reports progress and survives multi-hour jobs.

---

## 2. Background: the InSAR pipeline in IT terms

(Kept in the reference so future contributors don't repeat v1's mistake of omitting Stage 1.)

- **Stage 0 — Raw SLC scenes.** "Source code." One Sentinel-1 pass ≈ 4–8 GB of complex radar signal. Useless alone.
- **Stage 1 — Interferogram generation.** "Compile step." Co-register two SLCs (needs precise orbit files), remove terrain phase (needs a DEM, Copernicus GLO-30), difference the phases, unwrap (snaphu). Tools: ISCE2 / SNAP / GAMMA. **This is the expensive step**: hours per pair, hundreds of GB of intermediates, dozens of pairs needed per analysis, failure-prone.
- **Stage 2 — Time-series inversion.** "Link & run." Stack of interferograms → per-pixel velocity map + displacement history, atmospheric filtering, reliability masking. Tools: LiCSBAS (consumes LiCSAR products natively), MintPy (consumes ISCE2/HyP3 products). Minutes to ~2 h, RAM-bound, small outputs.

**v1's fatal flaw:** it went "download Sentinel-1 → run MintPy," silently skipping Stage 1. v2 resolves this by sourcing Stage-1 (or Stage-2) products pre-computed wherever possible.

---

## 3. Deformation data-source routing ladder

For any deformation query, the backend resolves the data source in this order. First match wins (subject to depth setting and expert switches).

| Tier | Source | Coverage | Latency to answer | Local cost | Notes |
|---|---|---|---|---|---|
| 1 | **EGMS** (Copernicus Land Monitoring) | EU/EEA Europe | Seconds–minutes | Negligible (download point/tile data) | Final Stage-2 product: calibrated velocities + time series. Annual update cadence — good for "is my area moving," not "what happened last month." |
| 2 | **COMET-LiCSAR** pre-computed interferograms + local **LiCSBAS** | Global tectonic/volcanic priority zones (incl. Caucasus/Armenia) | ~20–90 min | Few–tens of GB, Stage-2 compute only | The workhorse tier for the target audience (seismic areas). |
| 3 | **ASF HyP3 on-demand** interferograms + local **MintPy** | Near-global (ASF-hosted Sentinel-1) | Hours (remote job queue) + Stage-2 locally | Downloads only; Stage 1 runs on NASA's infra, free quota with Earthdata account | Fills gaps where LiCSAR has no frame. |
| 4 | **Local raw processing** (SLCs → ISCE2/hyp3-isce2 containers → MintPy) | Anywhere | Overnight–days | 300–500 GB scratch per stack, all cores | **Expert-mode switch only** (`EXPERT_RAW_PROCESSING=true`). MVP includes the switch and stub; full implementation is Milestone 6. |

**Coverage check implementation:**
- Tier 1: AOI within EGMS product footprint (static boundary polygon shipped with the app).
- Tier 2: query LiCSAR frame metadata API for frames intersecting AOI.
- Tier 3: assume covered if Sentinel-1 acquisitions exist over AOI (CDSE/ASF search).
- Fallback: honest "not covered / expert mode required" answer.

Flood (FLOODPY) and vegetation (NDVI) paths do not use this ladder; they fetch Sentinel data directly (§5.4).

---

## 4. Deployment architecture

### 4.1 Services (Docker Compose)
| Service | Tech | Role |
|---|---|---|
| `frontend` | Streamlit + streamlit-folium | Map AOI selection, chat, progress, results display |
| `backend` | FastAPI | Router, state machine, cache, LLM calls, status API |
| `downloader` | Python + **eodag** (+ tier-specific clients) | Fetch EGMS/LiCSAR/HyP3/CDSE products → shared volume |
| `worker` | Python (single shared container, CPU) | Run analysis wrappers via subprocess |
| `db` | PostgreSQL 16 | Metadata, cache index, query/task state |
| `broker` | RabbitMQ 3.x | Durable queues: `tasks`, `progress`, `results` |
| `llm` | Ollama (external or sidecar) | Local LLM; reached via LiteLLM abstraction |

**Changes from v1:**
- `sentinelsat` **removed** — the SciHub API it targeted was retired in 2023. All Copernicus access goes through **CDSE** via `eodag` (preferred; one API over CDSE + ASF with auth/retry/download handling) or `cdsetool`.
- Frontend↔backend live updates use **polling** (`GET /status/{query_id}` every 3–5 s) instead of WebSockets. Streamlit's rerun execution model fights persistent WebSocket clients; polling is idiomatic and sufficient. WebSockets return if the frontend is ever replaced with an SPA.
- Router is **LLM-first with rule fallback** (§5.2), not rules-only.

### 4.2 Reference hardware (MVP host)
Dell T7910, 2× Xeon E5 v3/v4, 128 GB RAM, 6 TB (SATA HDD + SSD), Proxmox; NVIDIA RTX 3050 (8 GB) for the LLM.

**Layout recommendation:**
- One VM (or LXC) hosting the whole Compose stack. Do not split services across VMs for MVP.
- VM sizing: 16+ vCPU, 64–96 GB RAM (leave headroom for Proxmox; MintPy/LiCSBAS will genuinely use the RAM).
- GPU: PCIe passthrough of the 3050 to the VM (or to a separate Ollama VM); 8 GB VRAM runs 7–8B models at Q4/Q5 at interactive speed (~25–40 t/s) — sufficient for intent parsing + summarization.
- **Storage split (important):**
  - SSD: PostgreSQL data, RabbitMQ, active processing scratch (`/data/scratch`), Ollama models.
  - HDD: downloaded product archive (`/data/archive`), completed results (`/data/results`).
  - SAR/raster processing is scattered-I/O heavy; keeping scratch on HDD is the main avoidable performance mistake on this machine.

### 4.3 Resource budget per query (defines validation limits)
| Path | Disk (peak) | RAM (peak) | Wall clock | Notes |
|---|---|---|---|---|
| EGMS lookup | < 1 GB | < 2 GB | < 2 min | |
| LiCSAR + LiCSBAS | 5–40 GB | 8–24 GB | 20–90 min | Scales with frame count × epochs |
| HyP3 + MintPy | 10–60 GB | 8–32 GB | 1–6 h (mostly remote queue wait) | |
| FLOODPY | 5–30 GB | 4–16 GB | 15–60 min | Includes its own S1 preprocessing (SNAP dependency inside FLOODPY env) |
| NDVI change (S2 L2A) | 2–10 GB | 2–8 GB | 5–20 min | ~1 GB per tile per date |
| Local raw InSAR (expert) | 300–500 GB | 32–64 GB | 12 h–3 days | Milestone 6; SSD scratch mandatory |

**Derived limits (env-configurable):** `MAX_AOI_KM2=1000` (standard), `MAX_AOI_KM2_EXPERT=5000`; max 2 concurrent analysis tasks (`WORKER_CONCURRENCY=2`); reject raw-mode jobs if free scratch < 600 GB.

---

## 5. Component specifications

### 5.1 Frontend (Streamlit)
- Leaflet map (OpenStreetMap tiles) via streamlit-folium: draw polygon/rectangle/point; geocoding via Nominatim (respect usage policy: 1 req/s, proper User-Agent).
- Chat input; optional date-range picker; depth selector: **Quick** (1 method) / **Standard** (2) / **Thorough** (all applicable).
- Progress panel driven by polling `GET /status/{query_id}`.
- Results: LLM text answer + embedded PNG maps/thumbnails from `/data/results/{query_id}/`.
- Persistent disclaimer footer (see §8.3) + attribution footer (§10).

### 5.2 Backend (FastAPI)
Endpoints:
- `POST /query` → validates payload, generates `query_id`, returns immediately.
- `GET /status/{query_id}` → current state, progress messages, percent.
- `GET /result/{query_id}` → final answer + artifact list.
- `GET /health`.

Responsibilities:
1. **Intent parsing (router):** call local LLM with a constrained prompt returning strict JSON `{hazard_types: [...], confidence: 0–1}`. If the call fails or confidence < 0.5 → keyword-rule fallback → if still ambiguous, ask the user to clarify (a valid response type, not a failure).
2. **Source routing:** apply §3 ladder per hazard type + depth setting → produce task list.
3. **Cache check:** query `cached_data` by `aoi_hash` + date range + product type (§6.2 hash definition). Cache hit skips download tasks.
4. **Task orchestration:** enqueue to `tasks`; track per-task state in `tasks` table; a query completes when all its tasks reach terminal state (done/failed).
5. **Aggregation & LLM answer:** collect `result.json` files → build summarization prompt (§8) → return answer.
6. State kept in PostgreSQL (MVP; no Redis).

### 5.3 Downloader
- Consumes `download` tasks; per-tier logic:
  - **EGMS:** fetch product tiles/points for AOI (community-documented API patterns).
  - **LiCSAR:** frame products via LiCSBAS's own download step or direct COMET endpoints.
  - **HyP3:** submit job via `hyp3_sdk`, poll, download results (requires `EARTHDATA_USERNAME/PASSWORD`).
  - **CDSE (S1 GRD for FLOODPY where needed, S2 L2A for NDVI):** via eodag. **Note CDSE free-account throttling/quotas** — implement bounded concurrency (2 parallel downloads) and exponential backoff; surface quota errors as user-visible progress messages, not silent retries.
  - **Auxiliary (raw mode only):** precise orbits, Copernicus GLO-30 DEM.
- Writes to `/data/archive/{product_type}/...`; symlinks/records paths per query; updates `cached_data` (paths, checksums, expiry); publishes progress.

 The InSAR/deformation path is a self-downloading wrapper: wrap_licsbas acquires LiCSAR products via LiCSBAS step 01 within the worker, rather than through the downloader service. The download/analysis split described in this section applies to the optical (Sentinel-2) path only. Rationale: LiCSBAS is designed as an integrated pipeline whose step 01 owns acquisition; forcing it through the downloader would duplicate maintained upstream code.

### 5.4 Worker
- Queues tasks.analysis, tasks.download, progress, results: durable, manual ack. Both task queues dead-letter to tasks.dlq (retry limit 3, prefetch 1). Rationale (M2.2): a single shared tasks queue made worker and downloader compete for each other's message kinds; splitting by kind keeps every §6.4 message contract unchanged while making consumption disjoint.
- **Wrapper contract (every wrapper obeys this):**
  - Input: CLI args `--query-id --aoi <geojson-file> --dates <start,end> --input-dir --output-dir --params <json>`.
  - Output: `result.json` per schema §6.3 + PNGs into `--output-dir`; exit code 0/nonzero; progress lines to stdout in the form `PROGRESS <int> <message>` (worker relays to `progress` queue).
- MVP wrappers:
  - `wrap_egms.py` — spatial subset + stats + map render of EGMS points.
  - `wrap_licsbas.py` — orchestrates LiCSBAS steps 01→15 on downloaded frame data; parses velocity GeoTIFF + time series.
  - `wrap_mintpy.py` — MintPy `smallbaselineApp` on HyP3 stack (HyP3→MintPy is a documented, supported path).
  - `wrap_floodpy.py` — FLOODPY statistical flood mapping.
  - `wrap_ndvi.py` — S2 L2A NDVI differencing with SCL-band cloud masking (rasterio/EOReader); report cloud % in quality block.
  - `wrap_raw_insar.py` — **stub in MVP**: validates expert switch, returns "not implemented until M6."
- All wrappers report a **quality block** (§6.3) — coherence stats, scene count, cloud fraction, masked-pixel fraction — which feeds the LLM's confidence language.

AOI→frame resolution for InSAR uses a static global catalog (licsar_frames.geojson, ~2,600 frames) built once by scripts/build_licsar_catalog.py from each frame's geocoded geo.U.tif bounds (bounding-box approximation; the true tilted footprint lives in the server-side LiCSInfo DB and is not publicly available). At routing time libs/licsar/frames.py computes equal-area AOI overlap and returns the best-covering frame(s), preferring one ascending + one descending. DEFAULT_DEFORM_FRAME remains as a fallback when the catalog is absent or misses. Catalog refresh is an occasional offline job (frames change slowly).

### 5.5 Database schema (MVP)
```sql
queries(
  query_id UUID PK, question TEXT, aoi GEOJSON/JSONB, aoi_hash TEXT,
  dates_start DATE, dates_end DATE, depth TEXT,
  status TEXT,                -- received|routing|downloading|analyzing|summarizing|done|failed|needs_clarification
  answer TEXT NULL, created_at, updated_at
)
tasks(
  task_id UUID PK, query_id FK, kind TEXT,      -- download|analysis
  name TEXT,                                     -- e.g. wrap_licsbas
  status TEXT,                                   -- queued|running|done|failed
  retries INT DEFAULT 0, error TEXT NULL,
  result_path TEXT NULL, created_at, updated_at
)
cached_data(
  id SERIAL PK, aoi_hash TEXT, dates_start DATE, dates_end DATE,
  product_type TEXT, file_paths JSONB, checksums JSONB,
  expiry_ts TIMESTAMPTZ, last_accessed TIMESTAMPTZ
)
```
v1's single `queries.status` could not represent a 3-script thorough run with one failure; `tasks` fixes that.

### 5.6 RabbitMQ policies
- Queues `tasks`, `progress`, `results`: durable, manual ack.
- Per-message retry limit 3 → **dead-letter queue** `tasks.dlq` (v1 had no poison-message protection).
- Prefetch 1 on worker/downloader consumers.

---

## 6. Contracts appendix (normative)

> These schemas are the cross-session consistency anchor for LLM-assisted development. Any change here is a breaking change and must be reflected everywhere.

### 6.1 Query payload (frontend → backend)
```json
{
  "question": "string, required",
  "aoi": { "type": "Polygon", "coordinates": [[[lon, lat], ...]] },
  "dates": { "start": "YYYY-MM-DD|null", "end": "YYYY-MM-DD|null" },
  "depth": "quick|standard|thorough",
  "expert_raw": false
}
```
Rules: AOI is GeoJSON in **EPSG:4326, lon-lat order, right-hand-rule winding**; backend normalizes winding and rejects self-intersections. Null dates → default lookback per hazard (deformation 24 months, flood 3 months, NDVI 12 months).

### 6.2 AOI hash (cache key)
aoi_hash = sha256( canonical_geojson ) where canonical form = EPSG:4326, coordinates rounded to 4 decimal places (~11 m), exterior ring only, right-hand winding, first point deduplicated (closing point dropped), ring rotated to start at the lexicographically smallest (lon, lat) vertex. Serialization is deterministic JSON: sorted keys, separators (",", ":"), no whitespace.
Rationale (found during M0): winding normalization alone maps the same polygon drawn CW vs CCW to the same cycle but a different start vertex, producing different hashes and silently defeating the cache. The start-vertex rotation makes the hash invariant to draw direction, ring closure, and vertex order rotation. Verified live: three byte-different representations of one square → one aoi_hash

### 6.3 `result.json` (every wrapper → backend)
```json
{
  "query_id": "uuid",
  "method": "egms|licsbas|mintpy|floodpy|ndvi",
  "status": "ok|partial|failed",
  "summary_stats": {
    "deformation": {
      "velocity_mm_yr_min": -14.2, "velocity_mm_yr_max": 3.1,
      "velocity_mm_yr_mean_aoi": -4.7,
      "hotspot_fraction": 0.12,
      "trend": "subsiding|uplifting|stable|mixed"
    }
  },
  "quality": {
    "scene_count": 28, "date_coverage": ["2024-06-01","2026-06-15"],
    "coherence_mean": 0.62, "masked_fraction": 0.31,
    "cloud_fraction": null,
    "confidence": "low|moderate|high",
    "caveats": ["strings, machine-generated, passed to LLM verbatim"]
  },
  "artifacts": [
    { "type": "map_png", "path": "velocity_map.png", "caption": "..." },
    { "type": "timeseries_png", "path": "point_ts.png", "caption": "..." }
  ],
  "attribution": ["Contains modified Copernicus Sentinel data [2026]", "..."]
}
```
`summary_stats` keys are method-specific but flat and numeric; the LLM never sees rasters, only this JSON + captions.
Keys are plain stat-group names with no example_ prefix (e.g. deformation, flood, ndvi); values must be flat scalars — number, string, or null — never nested objects or arrays. The contracts package rejects nested values at validation time.

### 6.4 Queue messages
results.result_json_path: for analysis tasks, the result.json path; for download tasks, the downloaded-data directory

```json
// tasks (download)
{ "task_id": "...", "query_id": "...", "kind": "download",
  "tier": "egms|licsar|hyp3|cdse|aux",
  "aoi": {...}, "dates": {...}, "products": ["..."] }

// tasks (analysis)
{ "task_id": "...", "query_id": "...", "kind": "analysis",
  "name": "wrap_licsbas", "input_dir": "...", "output_dir": "...",
  "aoi": {...}, "dates": {...}, "params": {} }

// progress
{ "query_id": "...", "task_id": "...", "message": "string", "percent": 0-100, "ts": "iso8601" }

// results
{ "query_id": "...", "task_id": "...", "status": "done|failed",
  "result_json_path": "...", "error": null }
```

### 6.5 Directory layout
```
/data
  /archive/{egms|licsar|hyp3|s1|s2|aux}/...     # HDD, cache-managed
  /scratch/{query_id}/                          # SSD, deleted after task
  /results/{query_id}/{method}/result.json,*.png # HDD, retention-managed
```

### 6.6 Environment variables (complete MVP list)
LLM_PROVIDER=openai_compatible      # ollama | openai_compatible | (commercial providers later)
LLM_BASE_URL=http://<llm-host>:5001/v1
LLM_API_KEY=not-needed              # koboldcpp ignores it; LiteLLM requires the field
LLM_MODEL=<model name as the server reports it> enabled

In this case:
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://192.168.10.150:5001/v1
LLM_API_KEY=not-needed
LLM_MODEL=koboldcpp/qwen3.5-9b-polaris-highiq-instruct-q4_k_m-imat


---

## 7. Workflow (end-to-end)

1. User draws AOI, types question, picks depth → `POST /query` → `query_id` returned instantly; frontend starts polling.
2. Backend: intent parse → hazard type(s) → routing ladder → task list → cache check → enqueue downloads (if misses).
3. Downloader: fetch per tier → archive + DB → progress events.
4. Backend: on download completion → enqueue analysis task(s).
5. Worker: run wrapper(s) → `result.json` + PNGs → results event.
6. Backend: all tasks terminal → build summarization prompt from all `result.json` (including failed/partial ones) → LLM → store answer → status `done`.
7. Frontend: renders answer + artifacts.

**Error path:** failed task retries ≤3 → DLQ → marked failed; query still completes with partial results; LLM answer explicitly states which method failed and what that means for confidence.

**Cleanup:** daily job deletes `/data/results` older than `DATA_RETENTION_DAYS` and evicts `cached_data` past `expiry_ts` (LRU by `last_accessed` if disk pressure). Scratch is deleted at task end unconditionally.

---

## 8. LLM layer

### 8.1 Abstraction
LiteLLM; provider/model from env. Default local: Qwen3.5-9B-class at Q4/Q5 via any OpenAI-compatible server. Commercial providers optional, with a startup log warning that query locations will leave the machine.

### 8.2 Calls
1. **Intent parse** (router): temperature 0, JSON-only output, schema-validated, one retry on invalid JSON, then rule fallback.
2. **Answer synthesis:** input = user question + all `result.json` contents + coverage/tier notes; output = plain-language answer.

### 8.3 Hard constraints for answer synthesis (product-defining)
The system prompt for synthesis MUST enforce:
- Report **observations, not verdicts**: "the ground in this area moved downward about 12 mm/year between 2024–2026, with moderate confidence" — never "your house is/isn't safe," never "you don't need to worry."
- Always state confidence + its basis (from `quality` blocks) and any caveats verbatim-in-spirit.
- Always state coverage honestly ("no suitable satellite coverage for this period" / "this area is outside pre-computed coverage").
- For any non-trivial detected signal, recommend consulting a qualified professional (geotechnical engineer / local geological survey).
- Never invent numbers not present in `result.json`.
- Match the user's language (the model answers in the language of the question).
- Fixed UI disclaimer (outside LLM control): *"This is an automated first-look analysis of public satellite data. It is not a safety assessment or an official hazard evaluation."*

---

## 9. Reused open-source components (build-vs-buy map)

| Need | Use | License | Mode |
|---|---|---|---|
| LiCSAR download + Stage-2 time series | **LiCSBAS** | GPL-3.0 | subprocess |
| Stage-2 on HyP3 stacks | **MintPy** | GPL-3.0 | subprocess |
| Remote Stage-1 | **ASF HyP3** (`hyp3_sdk`) | BSD | API |
| Flood mapping | **FLOODPY** | GPL-3.0 | subprocess |
| Data access (CDSE/ASF) | **eodag** | Apache-2.0 | library |
| Phase unwrapping (raw mode) | **snaphu** | (bundled per its terms) | subprocess |
| Local Stage-1 (M6) | **hyp3-isce2** containers | BSD/Apache | docker |
| Cloud masking | SCL band / s2cloudless approach | — | reimplemented |
| Map UI | folium/leafmap + streamlit-folium | MIT | library |
| LLM abstraction | LiteLLM | MIT | library |
| AOI→frame/burst selection logic | EZ-InSAR (reference only — MATLAB core) | GPL-3.0 | design reference |
| NL-interface prompt patterns | ESA Φ-lab assistant backends | permissive | design reference |

Docker images: base worker images on official MintPy / FLOODPY / hyp3-isce2 images where published, rather than hand-building dependency stacks.

**Net scope of original code:** FastAPI orchestrator, 6 thin wrappers, LLM prompts, Streamlit UI, Compose — order of 3–5k lines of glue.

---

## 10. Compliance & attribution
- Project: GPL-3.0. `LICENSES/` directory with all third-party licenses.
- Every answer footer: "Powered by Copernicus Sentinel data [year] · COMET-LiCSAR · EGMS © European Union Copernicus Land Monitoring Service · ASF HyP3 · MintPy · LiCSBAS · FLOODPY" (assembled from `attribution` arrays actually used).
- Respect provider ToS: CDSE quotas, Nominatim usage policy, HyP3 fair use.

---

## 11. Build plan (vertical slices — normative order)

> Rule: every milestone ends with a working end-to-end demo. Plumbing is debugged on fast fake tasks before any real geoscience runs.

**M0 — Contracts & scaffolding (weekend)**
Repo layout, Compose with all services, DB migrations, queue setup, this document's §6 encoded as Pydantic models + JSON schema files.

**M1 — Walking skeleton (weekend)**
Frontend (map+chat) → `POST /query` → router *stub* (always "deformation") → queue → **dummy worker** (sleep 10 s, emit fake progress, write a synthetic `result.json` + placeholder PNG) → LLM synthesis with real prompt → answer in UI. *Full loop, zero geoscience.*

**M2 — Real router + first real method: NDVI (evenings, ~1 wk)**
LLM intent parsing with rule fallback; eodag S2 L2A download via CDSE; `wrap_ndvi.py` with SCL cloud masking; cache path live. Fast feedback (minutes/run) hardens download+cache+quality plumbing.

**M3 — Deformation tier 2: LiCSAR + LiCSBAS (~1–2 wk, the grind)**
Frame coverage check; product download; `wrap_licsbas.py` over steps 01→15; velocity/TS parsing; quality block from coherence. Test AOI: a known-deforming, LiCSAR-covered site. *(Caucasus frames make a natural local test case.)*

**M4 — FLOODPY + EGMS + depth logic (~1 wk)**
`wrap_floodpy.py`; `wrap_egms.py`; routing ladder complete; Quick/Standard/Thorough fan-out; multi-method answer synthesis incl. cross-validation phrasing and partial-failure answers.

**M5 — Hardening (ongoing)**
Retention/cleanup job; DLQ handling UI-side; AOI/limit validation; concurrent-query soak test; disclaimer/attribution polish. **← MVP line.**

**M6 — Expert raw mode (post-MVP)**
HyP3 tier (`wrap_mintpy.py` on HyP3 stacks) first — it delivers most of raw mode's value at ~5% of its pain — then local hyp3-isce2 containers behind `EXPERT_RAW_PROCESSING`.

### 11.1 Definition of done (MVP)
A non-expert can draw an AOI in a seismic region, ask "is the ground moving here?", and within ≤90 min receive a plain-language, confidence-qualified, properly-attributed answer with a map — with the system having chosen EGMS/LiCSAR automatically, cached the data, survived a task failure, and never claimed anything is "safe."

### 11.2 Known edge cases (must handle by M5)
Oversized AOI (reject w/ explanation) · AOI over water (deformation methods refuse gracefully) · zero acquisitions in date range · LiCSAR frame exists but has stale/sparse epochs (quality block → low confidence) · CDSE quota exhaustion mid-download (resume/backoff, honest progress message) · ambiguous question (clarification response) · concurrent queries hitting the same cache entry.

### 11.3 Rules for LLM-assisted implementation
1. This document is the contract; §6 schemas are pasted into every coding session.
2. Wrappers are written **with upstream docs in context** (LiCSBAS README/step docs, MintPy smallbaselineApp docs, FLOODPY config docs, eodag provider docs, hyp3_sdk docs) — never from model memory.
3. One vertical slice per session; integration is tested by execution, not by review.
4. Any deviation from a contract discovered during implementation → update this document first.

---

## 12. Future work (unchanged intent from v1, re-scoped)
Separate per-tool worker containers · GPU-accelerated methods · SPA frontend with WebSockets + interactive time-series (InsarViz-style point→graph) · Redis state · wildfire burned-area method · scheduled re-checks ("watch this AOI") · multi-user auth for community-hosted instances.
