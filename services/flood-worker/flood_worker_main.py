"""geohazard-chat flood-worker — M4.2c.

Consumes `tasks.flood` and runs wrap_floodpy (SNAP + FLOODPY). Separate from
the main worker because SNAP is ~2 GB; a container only consumes task kinds it
can actually run (§5.4, same rationale as the M2.2 analysis/download split).

Heartbeat-safe execution, copied deliberately from services/worker: pika's
BlockingConnection cannot service AMQP heartbeats while a handler blocks, and a
flood run is minutes of SNAP preprocessing. So the consumer is a basic_get poll
loop; wrap_floodpy runs in a subprocess whose PROGRESS lines are pumped through
a thread into a thread-safe queue; the main thread drains that queue, publishes,
and calls process_data_events() every second. All AMQP stays on the main thread.

Deterministic-failure policy (§7, M4.2c): a flood run that TIMES OUT or exits
nonzero fails to the DLQ with NO retry. A wedged SNAP run will re-wedge; three
retries waste hours (learned the hard way in M4.2b — a mis-sized run thrashed
20 h). The failure is surfaced as a normal "failed" result so the query
finalises with an honest message instead of hanging.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pika

from geohazard_contracts import (
    AnalysisTaskMessage,
    ProgressMessage,
    ResultJson,
    ResultMessage,
)
from geohazard_contracts.queues import (
    FLOOD_QUEUE,
    PROGRESS_QUEUE,
    RESULTS_QUEUE,
    TASKS_DLQ,
    connect_and_declare,
)

ROLE = os.environ.get("SERVICE_ROLE", "flood-worker")
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")
WRAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrap_floodpy.py")
FLOODPY_PYTHON = os.environ.get("FLOODPY_PYTHON", "/opt/conda/envs/floodpy/bin/python")
# Whole-run ceiling. A flood run is normally minutes; 2 h is generous and still
# bounds a wedge. Per-phase timeouts are a later refinement (backlog).
FLOOD_TIMEOUT_S = int(os.environ.get("FLOOD_TIMEOUT_S", str(2 * 3600)))


def log(msg: str) -> None:
    print(f"[{ROLE}] {msg}", flush=True)


def _publish(channel, q: str, body: str, headers: dict | None = None) -> None:
    channel.basic_publish(
        exchange="", routing_key=q, body=body,
        properties=pika.BasicProperties(delivery_mode=2, headers=headers or {}),
    )


def _publish_progress(channel, task: AnalysisTaskMessage, percent: int, message: str) -> None:
    msg = ProgressMessage(
        query_id=task.query_id, task_id=task.task_id,
        message=message, percent=percent, ts=datetime.now(timezone.utc),
    )
    _publish(channel, PROGRESS_QUEUE, msg.model_dump_json())
    log(f"PROGRESS {percent} {message}")


def _run_wrap_floodpy(task: AnalysisTaskMessage, emit) -> str:
    """Spawn wrap_floodpy, relay PROGRESS lines, gate result.json on §6.3.

    Raises TimeoutError on wedge, RuntimeError on nonzero exit. Both are
    deterministic failures the caller sends straight to the DLQ.
    """
    out_dir = Path(task.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".geojson", delete=False) as f:
        json.dump(task.aoi.model_dump(), f)
        aoi_path = f.name

    cmd = [
        FLOODPY_PYTHON, WRAP,
        "--query-id", str(task.query_id),
        "--aoi", aoi_path,
        "--dates", f"{task.dates.start},{task.dates.end}",
        "--input-dir", task.input_dir,
        "--output-dir", task.output_dir,
        "--params", json.dumps(task.params),
    ]
    log(f"spawning wrap_floodpy (input={task.input_dir}, timeout={FLOOD_TIMEOUT_S}s)")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + FLOOD_TIMEOUT_S
    try:
        for line in proc.stdout:
            line = line.rstrip()
            m = re.match(r"PROGRESS (\d+) (.*)", line)
            if m:
                emit(min(100, int(m.group(1))), m.group(2))
            elif line:
                log(f"[wrap_floodpy] {line[:200]}")
            if time.monotonic() > deadline:
                proc.kill()
                raise TimeoutError(
                    f"wrap_floodpy exceeded {FLOOD_TIMEOUT_S}s and was killed. "
                    "SNAP preprocessing likely wedged; not retried.")
        proc.wait(timeout=60)
    finally:
        try:
            os.unlink(aoi_path)
        except OSError:
            pass

    if proc.returncode != 0:
        raw = (proc.stderr.read() or "").strip()
        last = raw.splitlines()[-1] if raw else "no stderr"
        tail = " ".join(raw[-400:].split())
        raise RuntimeError(f"wrap_floodpy exited {proc.returncode}: {last} || {tail}")

    result_path = out_dir / "result.json"
    ResultJson.model_validate_json(result_path.read_text())
    return str(result_path)


def _execute_with_heartbeat(connection, channel, task: AnalysisTaskMessage) -> str:
    """Run wrap_floodpy in a thread; publish progress and service AMQP
    heartbeats from the main thread. Returns result path or raises."""
    prog_q: "queue.Queue[tuple[int, str]]" = queue.Queue()
    outcome: dict = {}

    def work():
        try:
            outcome["result"] = _run_wrap_floodpy(task, lambda p, m: prog_q.put((p, m)))
        except BaseException as e:  # noqa: BLE001
            outcome["error"] = e

    th = threading.Thread(target=work, daemon=True, name=f"flood-{task.task_id}")
    th.start()
    while th.is_alive() or not prog_q.empty():
        try:
            while True:
                p, m = prog_q.get_nowait()
                _publish_progress(channel, task, p, m)
        except queue.Empty:
            pass
        connection.process_data_events(time_limit=1)
    th.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def _emit_failure_result(task: AnalysisTaskMessage, err: str) -> str:
    """Write an honest failed result.json so the query still finalises.

    A deterministic flood failure (timeout / nonzero exit) should not leave the
    query hanging; the synthesis layer can then tell the user flood analysis
    failed and why, rather than timing out silently.
    """
    out_dir = Path(task.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # date_coverage MUST be two valid ISO dates (§6.3) even on the failure path,
    # or ResultJson validation raises and the failure cannot be recorded at all
    # (which is exactly what happened live). Fall back to a real window.
    from datetime import date, timedelta
    _today = date.today()
    try:
        start = str(task.dates.start) if task.dates and task.dates.start else str(_today - timedelta(days=90))
        end = str(task.dates.end) if task.dates and task.dates.end else str(_today)
        # validate they parse; if not, use the safe window
        date.fromisoformat(start); date.fromisoformat(end)
    except Exception:  # noqa: BLE001
        start, end = str(_today - timedelta(days=90)), str(_today)
    result = ResultJson(
        query_id=task.query_id,
        method="floodpy",
        status="failed",
        summary_stats={},
        quality={
            "scene_count": 0,
            "date_coverage": [start, end],
            "coherence_mean": None, "masked_fraction": None, "cloud_fraction": None,
            "confidence": "low",
            "caveats": [
                "Flood analysis could not be completed for this area.",
                f"Reason: {err[:300]}",
            ],
        },
        artifacts=[],
        attribution=["geohazard-chat flood-worker"],
    )
    result_path = out_dir / "result.json"
    result_path.write_text(result.model_dump_json(indent=2))
    return str(result_path)


def _dlq(channel, body) -> None:
    _publish(channel, TASKS_DLQ, body if isinstance(body, str) else body.decode("utf-8", "replace"))


def _fail_result_msg(channel, task, status, err, result_path=None) -> None:
    _publish(channel, RESULTS_QUEUE, ResultMessage(
        query_id=task.query_id, task_id=task.task_id,
        status=status, result_json_path=result_path, error=err[:500] if err else None,
    ).model_dump_json())


def _handle(connection, channel, method, properties, body) -> None:
    """Process one flood task. Success -> results done + ack. Deterministic
    failure -> honest failed result + DLQ (no retry)."""
    try:
        task = AnalysisTaskMessage.model_validate_json(body)
    except Exception as e:  # noqa: BLE001 — unparseable -> DLQ, never requeue
        log(f"unparseable task -> DLQ ({e})")
        _dlq(channel, body)
        channel.basic_ack(method.delivery_tag)
        return

    if task.name != "wrap_floodpy":
        # Should never happen — only wrap_floodpy routes here — but fail loudly
        # rather than silently running the wrong thing.
        log(f"unexpected wrapper '{task.name}' on {FLOOD_QUEUE} -> DLQ")
        _dlq(channel, body)
        channel.basic_ack(method.delivery_tag)
        return

    log(f"flood task {task.task_id} for query {task.query_id}")
    try:
        result_path = _execute_with_heartbeat(connection, channel, task)
        _publish(channel, RESULTS_QUEUE, ResultMessage(
            query_id=task.query_id, task_id=task.task_id,
            status="done", result_json_path=result_path,
        ).model_dump_json())
        channel.basic_ack(method.delivery_tag)
        log(f"flood task {task.task_id} done -> {result_path}")
    except (TimeoutError, RuntimeError) as e:
        # Deterministic failure: honest result + DLQ, NO retry.
        err = str(e)
        log(f"flood task {task.task_id} FAILED (no retry): {err[:200]}")
        try:
            result_path = _emit_failure_result(task, err)
            _fail_result_msg(channel, task, "failed", err, result_path)
        except Exception as e2:  # noqa: BLE001
            log(f"could not write failure result for {task.task_id}: {e2}")
            _fail_result_msg(channel, task, "failed", err)
        _dlq(channel, body)
        channel.basic_ack(method.delivery_tag)
    except Exception as e:  # noqa: BLE001 — unexpected: still don't hang the queue
        err = f"unexpected flood-worker error: {e!r}"
        log(err)
        _fail_result_msg(channel, task, "failed", err)
        _dlq(channel, body)
        channel.basic_ack(method.delivery_tag)


def main() -> None:
    while True:
        try:
            connection, channel = connect_and_declare(AMQP_URL)
            log(f"connected; consuming {FLOOD_QUEUE} (single consumer, prefetch 1)")
            while True:
                method, properties, body = channel.basic_get(FLOOD_QUEUE, auto_ack=False)
                if method is None:
                    connection.process_data_events(time_limit=1)
                    continue
                _handle(connection, channel, method, properties, body)
        except Exception as e:  # noqa: BLE001
            log(f"broker/loop error ({e!r}); reconnecting in 5 s")
            time.sleep(5)


if __name__ == "__main__":
    main()
