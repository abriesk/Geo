"""geohazard-chat backend — M2.2 (cache-aware routing + download chaining).

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
    DownloadTaskMessage,
    ProgressMessage,
    QueryPayload,
    QueryStatus,
    ResultMessage,
    TaskStatus,
)
from geohazard_contracts.queues import (
    ANALYSIS_QUEUE,
    DOWNLOAD_QUEUE,
    PROGRESS_QUEUE,
    RESULTS_QUEUE,
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
            _after_task_terminal(str(msg.query_id), str(msg.task_id))
        except Exception as e:  # noqa: BLE001
            print(f"[backend] post-result handling failed for {msg.query_id}: {e!r}", flush=True)


def _after_task_terminal(query_id: str, task_id: str) -> None:
    """§7 step 4: a finished DOWNLOAD task triggers its query's pending
    analysis tasks (rebuilt from the queries row); a failed download fails
    them. Then the usual all-terminal finalize check runs."""
    with _db() as conn:
        t = conn.execute(
            "SELECT kind, status, result_path, error FROM tasks WHERE task_id=%s",
            (task_id,),
        ).fetchone()
    if t and t["kind"] == "download":
        if t["status"] == "done":
            _publish_pending_analyses(query_id, t["result_path"])
        else:
            with _db() as conn:
                conn.execute(
                    """UPDATE tasks SET status=%s, error=%s
                       WHERE query_id=%s AND kind='analysis' AND status=%s""",
                    (TaskStatus.FAILED.value,
                     f"input data unavailable: {t['error']}",
                     query_id, TaskStatus.QUEUED.value),
                )
    _finalize_query_if_done(query_id)


def _publish_pending_analyses(query_id: str, input_dir: str) -> None:
    """Rebuild AnalysisTaskMessages for still-queued analysis tasks of the
    query. M2.2 scope: the only download-gated hazard is vegetation."""
    from geohazard_contracts import AoiPolygon as _Aoi, DateRange as _DR

    with _db() as conn:
        q = conn.execute(
            "SELECT question, aoi, dates_start, dates_end FROM queries WHERE query_id=%s",
            (query_id,),
        ).fetchone()
        pending = conn.execute(
            """SELECT task_id, name FROM tasks
               WHERE query_id=%s AND kind='analysis' AND status=%s""",
            (query_id, TaskStatus.QUEUED.value),
        ).fetchall()
    if not q or not pending:
        return

    aoi_raw = q["aoi"] if not isinstance(q["aoi"], str) else json.loads(q["aoi"])
    hazard = "vegetation"  # M2.2: sole download-gated hazard; M3 generalizes
    import pika

    conn_mq, channel = connect_and_declare(AMQP_URL)
    for row in pending:
        msg = AnalysisTaskMessage(
            task_id=row["task_id"], query_id=uuid.UUID(query_id),
            name=row["name"], input_dir=input_dir,
            output_dir=f"{RESULTS_ROOT}/{query_id}/{hazard}",
            aoi=_Aoi.model_validate(aoi_raw),
            dates=_DR(start=q["dates_start"], end=q["dates_end"]),
            params={"hazard": hazard,
                    "simulate_failure": "FAIL!" in (q["question"] or "")},
        )
        channel.basic_publish(exchange="", routing_key=ANALYSIS_QUEUE,
                              body=msg.model_dump_json(),
                              properties=pika.BasicProperties(delivery_mode=2))
    conn_mq.close()
    with _db() as conn:
        conn.execute("UPDATE queries SET status=%s WHERE query_id=%s",
                     (QueryStatus.ANALYZING.value, query_id))
    print(f"[backend] query {query_id}: download done -> {len(pending)} analysis task(s) released",
          flush=True)


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


app = FastAPI(title="geohazard-chat backend", version="0.8.0-m3.3", lifespan=lifespan)
Path(RESULTS_ROOT).mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=RESULTS_ROOT), name="files")



# ---------------------------------------------------------------- router (M2.1)
# §5.2 responsibilities 1-2 (partial): intent parse (LLM, rule fallback,
# clarification) + depth fan-out. All hazards still run the dummy wrapper;
# M2.3/M3/M4 swap in real wrapper names per hazard.
HAZARD_TO_WRAPPER = {
    "deformation": "wrap_licsbas", # M3.2: real InSAR
    "flood": "wrap_dummy",         # M4: wrap_floodpy
    "vegetation": "wrap_dummy",    # M2.3: wrap_ndvi
}
DEPTH_MAX_METHODS = {"quick": 1, "standard": 2, "thorough": len(HAZARD_TO_WRAPPER)}


# M3.2: InSAR frame configuration. wrap_licsbas is self-downloading (no
# downloader chain); it needs a LiCSAR frame id in params. Automated AOI->frame
# resolution is a backlog item (needs a frames catalog); for now a configured
# default frame is used for deformation queries (the Yerevan test frame).
DEFAULT_DEFORM_FRAME = os.environ.get("DEFAULT_DEFORM_FRAME", "")
LICSAR_CATALOG = os.environ.get("LICSAR_CATALOG", "/data/licsar_frames.geojson")


def _resolve_deform_frames(aoi_geojson: dict) -> list[str]:
    """AOI -> LiCSAR frame id(s) from the shipped catalog (§ frame resolution).
    Falls back to DEFAULT_DEFORM_FRAME when the catalog is absent or misses."""
    try:
        import sys as _sys
        if "/libs" not in _sys.path:
            _sys.path.insert(0, "/libs")
        from licsar.frames import find_licsar_frames
        frames = find_licsar_frames(aoi_geojson, catalog_path=LICSAR_CATALOG)
        ids = [f["frame_id"] for f in frames]
        if ids:
            print(f"[router] resolved {len(ids)} InSAR frame(s): {ids}", flush=True)
            return ids
        print("[router] no catalog frame matched AOI", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[router] frame resolution error ({e!r}); using fallback", flush=True)
    if DEFAULT_DEFORM_FRAME:
        print(f"[router] fallback frame {DEFAULT_DEFORM_FRAME}", flush=True)
        return [DEFAULT_DEFORM_FRAME]
    return []


def _deform_params(question: str, aoi_geojson: dict) -> dict:
    p = {"hazard": "deformation", "simulate_failure": "FAIL!" in question}
    frames = _resolve_deform_frames(aoi_geojson)
    if frames:
        # MVP: run the best (first) frame — ascending, largest overlap. Running
        # asc+desc as two tasks is a straightforward extension (BACKLOG).
        p["frame_id"] = frames[0]
        p["candidate_frames"] = frames
    return p


NEEDS_DOWNLOAD = {
    # hazard -> (cached product_type, download tier, products list, default lookback months §6.1)
    "vegetation": ("s2", "cdse", ["S2_MSI_L2A"], 12),
}


def _effective_dates(dates, lookback_months: int):
    """§6.1: null dates -> per-hazard default lookback."""
    from datetime import date as _date, timedelta as _td
    end = dates.end or _date.today()
    start = dates.start or (end - _td(days=30 * lookback_months))
    return start, end


def _cache_lookup(a_hash: str, product_type: str, start, end):
    """§6.2 probe: same AOI hash, requested range within cached range.
    Returns the cached data dir (str) or None; touches last_accessed."""
    with _db() as conn:
        row = conn.execute(
            """SELECT id, file_paths FROM cached_data
               WHERE aoi_hash=%s AND product_type=%s
                 AND dates_start<=%s AND dates_end>=%s
               ORDER BY last_accessed DESC LIMIT 1""",
            (a_hash, product_type, start, end),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE cached_data SET last_accessed=now() WHERE id=%s", (row["id"],))
    fp = row["file_paths"]
    if isinstance(fp, str):
        fp = json.loads(fp)
    return fp.get("dir")


def _resolve_intent(question: str) -> list[str]:
    """LLM first; rule fallback on failure or low confidence (§5.2)."""
    try:
        hazards, conf = llm.parse_intent(question)
        if hazards and conf >= llm.INTENT_CONFIDENCE_THRESHOLD:
            print(f"[router] LLM intent: {hazards} (conf {conf:.2f})", flush=True)
            return hazards
        print(f"[router] LLM low-confidence ({hazards}, {conf:.2f}); trying rules", flush=True)
    except llm.IntentParseFailed as e:
        print(f"[router] LLM intent parse failed ({e}); trying rules", flush=True)
    hazards = llm.rule_intent(question)
    print(f"[router] rule intent: {hazards}", flush=True)
    return hazards


def _route_and_enqueue(query_id: uuid.UUID, payload: QueryPayload) -> None:
    try:
        hazards = _resolve_intent(payload.question)

        if not hazards:
            with _db() as conn:
                conn.execute(
                    "UPDATE queries SET status=%s, answer=%s WHERE query_id=%s",
                    (QueryStatus.NEEDS_CLARIFICATION.value, llm.CLARIFICATION_TEXT, query_id),
                )
            print(f"[router] query {query_id}: needs_clarification", flush=True)
            return

        hazards = hazards[: DEPTH_MAX_METHODS.get(payload.depth.value, 2)]

        analysis_msgs, download_msgs, deferred_rows = [], [], []
        for hazard in hazards:
            wrapper = HAZARD_TO_WRAPPER[hazard]
            common = dict(
                query_id=query_id,
                aoi=payload.aoi,
                dates=payload.dates,
            )
            if hazard in NEEDS_DOWNLOAD:
                product_type, tier, products, lookback = NEEDS_DOWNLOAD[hazard]
                eff_start, eff_end = _effective_dates(payload.dates, lookback)
                a_hash = payload.aoi.hash()
                cached = _cache_lookup(a_hash, product_type, eff_start, eff_end)
                if cached:
                    print(f"[router] cache HIT s2 for {a_hash[:12]}… -> skip download", flush=True)
                    analysis_msgs.append(AnalysisTaskMessage(
                        task_id=uuid.uuid4(), name=wrapper,
                        input_dir=cached, output_dir=f"{RESULTS_ROOT}/{query_id}/{hazard}",
                        params={"hazard": hazard,
                                "simulate_failure": "FAIL!" in payload.question},
                        **common,
                    ))
                else:
                    print(f"[router] cache MISS s2 for {a_hash[:12]}… -> download via {tier}", flush=True)
                    from geohazard_contracts import DateRange as _DR
                    download_msgs.append(DownloadTaskMessage(
                        task_id=uuid.uuid4(), tier=tier, products=products,
                        query_id=query_id, aoi=payload.aoi,
                        dates=_DR(start=eff_start, end=eff_end),
                    ))
                    # Analysis row exists now but its message is built & published
                    # only when the download completes (M2.2: vegetation only;
                    # M3 generalizes this linkage).
                    deferred_rows.append((uuid.uuid4(), wrapper))
            else:
                params = ({"hazard": hazard,
                           "simulate_failure": "FAIL!" in payload.question}
                          if hazard != "deformation"
                          else _deform_params(payload.question, payload.aoi.model_dump()))
                analysis_msgs.append(AnalysisTaskMessage(
                    task_id=uuid.uuid4(), name=wrapper,
                    input_dir="/data/scratch",
                    output_dir=f"{RESULTS_ROOT}/{query_id}/{hazard}",
                    params=params,
                    **common,
                ))

        with _db() as conn:
            for t in analysis_msgs:
                conn.execute(
                    """INSERT INTO tasks (task_id, query_id, kind, name, status)
                       VALUES (%s,%s,'analysis',%s,%s)""",
                    (t.task_id, query_id, t.name, TaskStatus.QUEUED.value),
                )
            for d in download_msgs:
                conn.execute(
                    """INSERT INTO tasks (task_id, query_id, kind, name, status)
                       VALUES (%s,%s,'download',%s,%s)""",
                    (d.task_id, query_id, f"download_{d.tier.value}", TaskStatus.QUEUED.value),
                )
            for task_id, wrapper in deferred_rows:
                conn.execute(
                    """INSERT INTO tasks (task_id, query_id, kind, name, status)
                       VALUES (%s,%s,'analysis',%s,%s)""",
                    (task_id, query_id, wrapper, TaskStatus.QUEUED.value),
                )

        import pika

        conn_mq, channel = connect_and_declare(AMQP_URL)
        for t in analysis_msgs:
            channel.basic_publish(exchange="", routing_key=ANALYSIS_QUEUE,
                                  body=t.model_dump_json(),
                                  properties=pika.BasicProperties(delivery_mode=2))
        for d in download_msgs:
            channel.basic_publish(exchange="", routing_key=DOWNLOAD_QUEUE,
                                  body=d.model_dump_json(),
                                  properties=pika.BasicProperties(delivery_mode=2))
        conn_mq.close()

        new_status = QueryStatus.DOWNLOADING if download_msgs else QueryStatus.ANALYZING
        with _db() as conn:
            conn.execute("UPDATE queries SET status=%s WHERE query_id=%s",
                         (new_status.value, query_id))
        print(f"[router] query {query_id}: {len(analysis_msgs)} analysis + "
              f"{len(download_msgs)} download task(s) enqueued -> {new_status.value}", flush=True)
    except Exception as e:  # noqa: BLE001 — routing must never leave a query stuck
        print(f"[router] routing failed for {query_id}: {e!r}", flush=True)
        try:
            with _db() as conn:
                conn.execute(
                    "UPDATE queries SET status=%s, answer=%s WHERE query_id=%s",
                    (QueryStatus.FAILED.value,
                     f"Internal routing error: {e!r}", query_id),
                )
        except Exception as e2:  # noqa: BLE001
            print(f"[router] could not mark {query_id} failed: {e2!r}", flush=True)


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

    # §5.2: POST /query returns immediately; routing (which includes an LLM
    # call) happens off the request path.
    threading.Thread(
        target=_route_and_enqueue, args=(query_id, payload), daemon=True
    ).start()
    return {"query_id": str(query_id), "status": QueryStatus.ROUTING.value}


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
        if q["status"] not in ("done", "failed", "needs_clarification"):
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
