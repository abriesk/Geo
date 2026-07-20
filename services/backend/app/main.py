"""geohazard-chat backend — M1.2 (walking skeleton complete: LLM synthesis).

Flow implemented here (§7 steps 1-2, 4-6 with a template instead of the LLM):
  POST /query -> validate -> persist -> routing stub (always one dummy
  analysis task, hazard=deformation) -> publish AnalysisTaskMessage ->
  status=analyzing. A background thread consumes `progress` and `results`;
  when every task of a query is terminal, the answer is synthesized
  (M1.1: deterministic template built from result.json; M1.2 swaps in the
  §8.3-constrained LLM call via LiteLLM) and status becomes done/failed.

Progress messages are kept in-memory (ring buffer per query) for /status.
Deliberate M1 simplification: a backend restart loses the progress *display*
only — query/task state lives in PostgreSQL and remains correct.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row

from . import llm
from geohazard_contracts import (
    AnalysisTaskMessage,
    ProgressMessage,
    QueryPayload,
    QueryStatus,
    ResultMessage,
    TaskStatus,
)
from geohazard_contracts.queues import (
    PROGRESS_QUEUE,
    RESULTS_QUEUE,
    TASKS_QUEUE,
    connect_and_declare,
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://geohazard:geohazard@db:5432/geohazard"
)
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")
MAX_AOI_KM2 = float(os.environ.get("MAX_AOI_KM2", "1000"))
MAX_AOI_KM2_EXPERT = float(os.environ.get("MAX_AOI_KM2_EXPERT", "5000"))
EXPERT_RAW_PROCESSING = os.environ.get("EXPERT_RAW_PROCESSING", "false").lower() == "true"
RESULTS_ROOT = os.environ.get("RESULTS_ROOT", "/data/results")

# ---------------------------------------------------------------- progress buffer
_PROGRESS: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
_PROGRESS_LOCK = threading.Lock()


def _db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# ---------------------------------------------------------------- answer synthesis
def _template_answer(question: str, results: list[dict], failures: list[dict]) -> str:
    """M1.1 deterministic answer. M1.2 replaces this with the LiteLLM call;
    the *inputs* (result.json contents + failure notes) are already exactly
    what the §8 synthesis prompt will receive, so the interface won't move."""
    lines = ["[M1.1 template answer — LLM synthesis arrives in M1.2]", ""]
    for r in results:
        stats = r.get("summary_stats", {})
        q = r.get("quality", {})
        for group, s in stats.items():
            lines.append(
                f"Method {r.get('method')}: {group} mean velocity "
                f"{s.get('velocity_mm_yr_mean_aoi')} mm/yr, trend '{s.get('trend')}', "
                f"confidence {q.get('confidence')} "
                f"(scenes: {q.get('scene_count')}, coherence: {q.get('coherence_mean')})."
            )
        for c in q.get("caveats", []):
            lines.append(f"Caveat: {c}")
    for f in failures:
        lines.append(
            f"Method task {f.get('name')} FAILED: {f.get('error')} — "
            "this reduces overall confidence."
        )
    if not results and failures:
        lines.append("All analysis methods failed; no observation can be reported.")
    lines.append("")
    lines.append(
        "This is an automated first-look analysis of public satellite data. "
        "It is not a safety assessment or an official hazard evaluation."
    )
    return "\n".join(lines)


def _finalize_query_if_done(query_id: str) -> None:
    """Phase 1 (short DB txn): check terminal, mark summarizing, gather inputs.
    Phase 2 (no DB held): LLM synthesis, may take tens of seconds (§8.2).
    Phase 3 (short DB txn): store answer + final status.
    Falls back to the deterministic template if the LLM is unreachable —
    a dead GPU box must never hang or fail a query (§8 failure policy)."""
    with _db() as conn:
        tasks = conn.execute(
            "SELECT name, status, result_path, error FROM tasks WHERE query_id = %s",
            (query_id,),
        ).fetchall()
        if not tasks or any(t["status"] in ("queued", "running") for t in tasks):
            return
        conn.execute(
            "UPDATE queries SET status=%s WHERE query_id=%s",
            (QueryStatus.SUMMARIZING.value, query_id),
        )
        q = conn.execute(
            "SELECT question FROM queries WHERE query_id=%s", (query_id,)
        ).fetchone()

    results, failures = [], []
    for t in tasks:
        if t["status"] == "done" and t["result_path"]:
            try:
                results.append(json.loads(Path(t["result_path"]).read_text()))
            except Exception as e:  # noqa: BLE001
                failures.append({"name": t["name"], "error": f"result.json unreadable: {e}"})
        else:
            failures.append({"name": t["name"], "error": t["error"]})

    try:
        answer = llm.synthesize_answer(q["question"], results, failures)
        print(f"[backend] query {query_id}: LLM synthesis ok", flush=True)
    except llm.LlmUnavailable as e:
        print(f"[backend] query {query_id}: LLM unavailable ({e}); template fallback", flush=True)
        answer = (
            "⚠ The language model was unreachable; this is an automatic raw summary.\n\n"
            + _template_answer(q["question"], results, failures)
        )

    final = QueryStatus.DONE if results else QueryStatus.FAILED
    with _db() as conn:
        conn.execute(
            "UPDATE queries SET status=%s, answer=%s WHERE query_id=%s",
            (final.value, answer, query_id),
        )
    print(f"[backend] query {query_id} finalized: {final.value}", flush=True)


# ---------------------------------------------------------------- queue consumers
def _on_progress(channel, method, properties, body: bytes) -> None:
    try:
        msg = ProgressMessage.model_validate_json(body)
        with _PROGRESS_LOCK:
            _PROGRESS[str(msg.query_id)].append(
                {"ts": msg.ts.isoformat(), "percent": msg.percent, "message": msg.message}
            )
        if msg.task_id is not None:
            with _db() as conn:
                conn.execute(
                    "UPDATE tasks SET status=%s WHERE task_id=%s AND status=%s",
                    (TaskStatus.RUNNING.value, msg.task_id, TaskStatus.QUEUED.value),
                )
    except Exception as e:  # noqa: BLE001
        print(f"[backend] bad progress message dropped: {e}", flush=True)
    channel.basic_ack(delivery_tag=method.delivery_tag)


def _on_result(channel, method, properties, body: bytes) -> None:
    msg = None
    try:
        msg = ResultMessage.model_validate_json(body)
        with _db() as conn:
            conn.execute(
                "UPDATE tasks SET status=%s, result_path=%s, error=%s WHERE task_id=%s",
                (
                    TaskStatus.DONE.value if msg.status == "done" else TaskStatus.FAILED.value,
                    msg.result_json_path,
                    msg.error,
                    msg.task_id,
                ),
            )
    except Exception as e:  # noqa: BLE001
        print(f"[backend] bad result message dropped: {e}", flush=True)
    # Ack BEFORE synthesis: the LLM call can exceed the AMQP heartbeat
    # window; a dropped connection after this point must not redeliver.
    # Crash-during-synthesis leaves status=summarizing (M5 adds a sweeper).
    channel.basic_ack(delivery_tag=method.delivery_tag)
    if msg is not None:
        try:
            _finalize_query_if_done(str(msg.query_id))
        except Exception as e:  # noqa: BLE001
            print(f"[backend] finalize failed for {msg.query_id}: {e!r}", flush=True)


def _consumer_loop() -> None:
    while True:
        try:
            connection, channel = connect_and_declare(AMQP_URL)
            channel.basic_consume(queue=PROGRESS_QUEUE, on_message_callback=_on_progress)
            channel.basic_consume(queue=RESULTS_QUEUE, on_message_callback=_on_result)
            print("[backend] consuming progress+results", flush=True)
            channel.start_consuming()
        except Exception as e:  # noqa: BLE001
            print(f"[backend] consumer loop error ({e!r}); retrying in 5 s", flush=True)
            time.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_consumer_loop, daemon=True, name="queue-consumer").start()
    yield


app = FastAPI(title="geohazard-chat backend", version="0.3.0-m1.2", lifespan=lifespan)
Path(RESULTS_ROOT).mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=RESULTS_ROOT), name="files")


@app.get("/health")
def health():
    status = {"backend": "ok", "db": "unreachable", "broker": "unreachable"}
    try:
        with _db() as conn:
            row = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()
            status["db"] = f"ok ({row['n']} migrations)"
    except Exception as e:  # noqa: BLE001
        status["db"] = f"error: {e}"
    try:
        conn, _ = connect_and_declare(AMQP_URL)
        conn.close()
        status["broker"] = "ok"
    except Exception as e:  # noqa: BLE001
        status["broker"] = f"error: {e}"
    status["llm"] = llm.llm_reachable()  # informational only (§8 fallback exists)
    healthy = status["db"].startswith("ok") and status["broker"] == "ok"
    return {"healthy": healthy, "services": status}


@app.post("/query", status_code=202)
def submit_query(payload: QueryPayload):
    from geohazard_contracts import aoi_area_km2

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
    task_id = uuid.uuid4()
    output_dir = f"{RESULTS_ROOT}/{query_id}/dummy"

    # M1 routing stub (§11 M1): always one deformation analysis task.
    # M2 replaces this block with LLM intent parse -> §3 ladder -> fan-out.
    task_msg = AnalysisTaskMessage(
        task_id=task_id,
        query_id=query_id,
        name="wrap_dummy",
        input_dir="/data/scratch",
        output_dir=output_dir,
        aoi=payload.aoi,
        dates=payload.dates,
        params={"simulate_failure": "FAIL!" in payload.question},  # test hook
    )

    with _db() as conn:
        conn.execute(
            """INSERT INTO queries (query_id, question, aoi, aoi_hash,
                                    dates_start, dates_end, depth, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                query_id,
                payload.question,
                json.dumps(payload.aoi.model_dump()),
                payload.aoi.hash(),
                payload.dates.start,
                payload.dates.end,
                payload.depth.value,
                QueryStatus.ROUTING.value,
            ),
        )
        conn.execute(
            """INSERT INTO tasks (task_id, query_id, kind, name, status)
               VALUES (%s,%s,'analysis','wrap_dummy',%s)""",
            (task_id, query_id, TaskStatus.QUEUED.value),
        )

    try:
        import pika

        conn_mq, channel = connect_and_declare(AMQP_URL)
        channel.basic_publish(
            exchange="",
            routing_key=TASKS_QUEUE,
            body=task_msg.model_dump_json(),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        conn_mq.close()
        with _db() as conn:
            conn.execute(
                "UPDATE queries SET status=%s WHERE query_id=%s",
                (QueryStatus.ANALYZING.value, query_id),
            )
        status = QueryStatus.ANALYZING
    except Exception as e:  # noqa: BLE001 — broker down: query stays queued in DB
        print(f"[backend] enqueue failed for {query_id}: {e!r}", flush=True)
        with _db() as conn:
            conn.execute(
                "UPDATE queries SET status=%s WHERE query_id=%s",
                (QueryStatus.FAILED.value, query_id),
            )
            conn.execute(
                "UPDATE tasks SET status=%s, error=%s WHERE task_id=%s",
                (TaskStatus.FAILED.value, f"enqueue failed: {e!r}", task_id),
            )
        raise HTTPException(status_code=503, detail="task broker unavailable, try again")

    return {"query_id": str(query_id), "status": status.value}


@app.get("/status/{query_id}")
def query_status(query_id: uuid.UUID):
    with _db() as conn:
        q = conn.execute(
            "SELECT query_id, status, depth, created_at, updated_at "
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
    with _PROGRESS_LOCK:
        progress = list(_PROGRESS.get(str(query_id), []))
    return {
        "query_id": str(q["query_id"]),
        "status": q["status"],
        "depth": q["depth"],
        "tasks": [dict(t, task_id=str(t["task_id"])) for t in tasks],
        "progress": progress,
    }


@app.get("/result/{query_id}")
def query_result(query_id: uuid.UUID):
    with _db() as conn:
        q = conn.execute(
            "SELECT status, answer FROM queries WHERE query_id=%s", (query_id,)
        ).fetchone()
        if q is None:
            raise HTTPException(status_code=404, detail="unknown query_id")
        if q["status"] not in ("done", "failed"):
            raise HTTPException(status_code=409, detail=f"query is still {q['status']}")
        tasks = conn.execute(
            "SELECT name, status, result_path, error FROM tasks WHERE query_id=%s",
            (query_id,),
        ).fetchall()

    artifacts = []
    for t in tasks:
        if t["status"] == "done" and t["result_path"]:
            try:
                rj = json.loads(Path(t["result_path"]).read_text())
                base = Path(t["result_path"]).parent
                for a in rj.get("artifacts", []):
                    rel = (base / a["path"]).relative_to(RESULTS_ROOT)
                    artifacts.append(
                        {"type": a["type"], "caption": a["caption"], "url": f"/files/{rel}"}
                    )
            except Exception as e:  # noqa: BLE001
                print(f"[backend] artifact scan failed: {e}", flush=True)
    return {
        "query_id": str(query_id),
        "status": q["status"],
        "answer": q["answer"],
        "artifacts": artifacts,
        "tasks": [dict(t) for t in tasks],
    }
