"""geohazard-chat backend — M0 scaffolding.

Scope at M0 (§11 M0): prove the contracts, DB and broker are wired.
- GET  /health          -> reports db + broker connectivity
- POST /query           -> validates §6.1 payload, persists row (status=received),
                           returns query_id immediately. No routing yet (M1/M2).
- GET  /status/{id}     -> reads back query + task states (tasks appear from M1).
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row

from geohazard_contracts import QueryPayload, QueryStatus
from geohazard_contracts.queues import connect_and_declare

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://geohazard:geohazard@db:5432/geohazard"
)
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")
MAX_AOI_KM2 = float(os.environ.get("MAX_AOI_KM2", "1000"))
MAX_AOI_KM2_EXPERT = float(os.environ.get("MAX_AOI_KM2_EXPERT", "5000"))
EXPERT_RAW_PROCESSING = os.environ.get("EXPERT_RAW_PROCESSING", "false").lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Declare queue topology at startup so the broker is ready regardless of
    # which service boots first (idempotent, §5.6).
    try:
        conn, _ = connect_and_declare(AMQP_URL)
        conn.close()
    except Exception as e:  # noqa: BLE001 — startup must not crash on slow broker
        print(f"[backend] broker not ready at startup ({e}); will retry via /health")
    yield


app = FastAPI(title="geohazard-chat backend", version="0.1.0-m0", lifespan=lifespan)


def _db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@app.get("/health")
def health():
    status = {"backend": "ok", "db": "unreachable", "broker": "unreachable"}
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM schema_migrations"
            ).fetchone()
            status["db"] = f"ok ({row['n']} migrations)"
    except Exception as e:  # noqa: BLE001
        status["db"] = f"error: {e}"
    try:
        conn, _ = connect_and_declare(AMQP_URL)
        conn.close()
        status["broker"] = "ok"
    except Exception as e:  # noqa: BLE001
        status["broker"] = f"error: {e}"
    healthy = status["db"].startswith("ok") and status["broker"] == "ok"
    return {"healthy": healthy, "services": status}


@app.post("/query", status_code=202)
def submit_query(payload: QueryPayload):
    from geohazard_contracts import aoi_area_km2

    # §4.3 derived limits
    area = aoi_area_km2(payload.aoi.coordinates[0])
    limit = MAX_AOI_KM2_EXPERT if (payload.expert_raw and EXPERT_RAW_PROCESSING) else MAX_AOI_KM2
    if area > limit:
        raise HTTPException(
            status_code=422,
            detail=f"AOI is {area:.0f} km², above the {limit:.0f} km² limit. "
            "Draw a smaller area.",
        )
    if payload.expert_raw and not EXPERT_RAW_PROCESSING:
        raise HTTPException(
            status_code=403,
            detail="expert_raw requested but EXPERT_RAW_PROCESSING is disabled on this instance.",
        )

    query_id = uuid.uuid4()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO queries (query_id, question, aoi, aoi_hash,
                                 dates_start, dates_end, depth, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                query_id,
                payload.question,
                json.dumps(payload.aoi.model_dump()),
                payload.aoi.hash(),
                payload.dates.start,
                payload.dates.end,
                payload.depth.value,
                QueryStatus.RECEIVED.value,
            ),
        )
    # M1 adds: intent parse -> routing ladder -> task fan-out.
    return {"query_id": str(query_id), "status": QueryStatus.RECEIVED.value}


@app.get("/status/{query_id}")
def query_status(query_id: uuid.UUID):
    with _db() as conn:
        q = conn.execute(
            "SELECT query_id, status, depth, created_at, updated_at, answer "
            "FROM queries WHERE query_id = %s",
            (query_id,),
        ).fetchone()
        if q is None:
            raise HTTPException(status_code=404, detail="unknown query_id")
        tasks = conn.execute(
            "SELECT task_id, kind, name, status, retries, error "
            "FROM tasks WHERE query_id = %s ORDER BY created_at",
            (query_id,),
        ).fetchall()
    return {
        "query_id": str(q["query_id"]),
        "status": q["status"],
        "depth": q["depth"],
        "tasks": [dict(t, task_id=str(t["task_id"])) for t in tasks],
        "answer": q["answer"],
    }
