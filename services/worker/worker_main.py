"""geohazard-chat worker — M2.3b.

Consumes `tasks.analysis`. wrap_ndvi runs as a subprocess (§5.4 wrapper
contract); wrap_dummy stays inline for deformation/flood until M3/M4.

Heartbeat-safe execution (fix for the StreamLostError observed live in
M2.2/M2.3): pika's BlockingConnection cannot service AMQP heartbeats while a
handler blocks, so long tasks (minutes for NDVI, up to hours for M3
LiCSBAS) killed the connection and caused silent redeliveries. The consumer
is now a basic_get poll loop; the task body runs in a worker THREAD which
emits progress into a thread-safe queue; the main thread drains that queue,
publishes, and calls process_data_events() every second — keeping the
connection alive for the entire task. All AMQP operations stay on the main
thread (pika channels are not thread-safe).

Retry/poison policy unchanged (§5.6, §7): x-retry-count republish, DLQ
after MAX_TASK_RETRIES, unparseable -> DLQ. Test hook: params.simulate_failure.
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
    Confidence,
    ProgressMessage,
    ResultJson,
    ResultMessage,
)
from geohazard_contracts.queues import (
    ANALYSIS_QUEUE,
    MAX_TASK_RETRIES,
    PROGRESS_QUEUE,
    RESULTS_QUEUE,
    connect_and_declare,
)

ROLE = os.environ.get("SERVICE_ROLE", "worker")
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")
DUMMY_STEP_SECONDS = float(os.environ.get("DUMMY_STEP_SECONDS", "2"))
RETRY_HEADER = "x-retry-count"
WRAPPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wrappers")
REAL_WRAPPERS = {"wrap_ndvi"}  # M3 adds wrap_licsbas etc.


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


# ------------------------------------------------------------------ task cores
# Cores receive emit(percent, message) — never a channel (thread safety).

def _write_placeholder_png(path: Path) -> None:
    from PIL import Image, ImageDraw

    w, h = 480, 360
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (int(255 * x / w), 40, int(255 * (1 - x / w)))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, w - 10, 50], fill=(255, 255, 255))
    d.text((20, 22), "DUMMY velocity map — walking skeleton", fill=(0, 0, 0))
    d.text((20, h - 30), "synthetic data, not a measurement", fill=(255, 255, 255))
    img.save(path)


def run_dummy(task: AnalysisTaskMessage, emit) -> str:
    out_dir = Path(task.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    steps = ["fetching cached stack", "inverting time series", "masking low coherence",
             "computing statistics", "rendering map"]
    for i, step in enumerate(steps):
        emit(int(i * 100 / len(steps)), step)
        time.sleep(DUMMY_STEP_SECONDS)
        if task.params.get("simulate_failure") and i == 2:
            raise RuntimeError("simulated wrapper failure (test hook)")

    png_path = out_dir / "velocity_map.png"
    _write_placeholder_png(png_path)
    result = ResultJson(
        query_id=task.query_id, method="licsbas", status="ok",
        summary_stats={"deformation": {
            "velocity_mm_yr_min": -12.4, "velocity_mm_yr_max": 2.9,
            "velocity_mm_yr_mean_aoi": -4.1, "hotspot_fraction": 0.09,
            "trend": "subsiding",
        }},
        quality={
            "scene_count": 30,
            "date_coverage": [str(task.dates.start or "2024-07-01"),
                              str(task.dates.end or "2026-07-01")],
            "coherence_mean": 0.61, "masked_fraction": 0.27, "cloud_fraction": None,
            "confidence": Confidence.MODERATE,
            "caveats": ["SYNTHETIC RESULT — walking skeleton, numbers are fabricated"],
        },
        artifacts=[{"type": "map_png", "path": "velocity_map.png",
                    "caption": "Synthetic LOS velocity map (placeholder)"}],
        attribution=["Synthetic data (geohazard-chat dummy wrapper)"],
    )
    result_path = out_dir / "result.json"
    result_path.write_text(result.model_dump_json(indent=2))
    emit(100, "done")
    return str(result_path)


def run_wrapper_subprocess(task: AnalysisTaskMessage, emit) -> str:
    """§5.4: spawn wrapper, relay PROGRESS lines, gate result.json on §6.3."""
    out_dir = Path(task.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".geojson", delete=False) as f:
        json.dump(task.aoi.model_dump(), f)
        aoi_path = f.name
    cmd = [
        sys.executable, os.path.join(WRAPPER_DIR, f"{task.name}.py"),
        "--query-id", str(task.query_id),
        "--aoi", aoi_path,
        "--dates", f"{task.dates.start},{task.dates.end}",
        "--input-dir", task.input_dir,
        "--output-dir", task.output_dir,
        "--params", json.dumps(task.params),
    ]
    log(f"spawning: {task.name} (input={task.input_dir})")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            line = line.rstrip()
            m = re.match(r"PROGRESS (\d+) (.*)", line)
            if m:
                emit(min(100, int(m.group(1))), m.group(2))
            elif line:
                log(f"[{task.name}] {line[:200]}")
        proc.wait(timeout=6 * 3600)
    finally:
        os.unlink(aoi_path)
    if proc.returncode != 0:
        # Keep the LAST traceback line (the actual error) plus a flattened
        # tail; single-line so it travels cleanly inside queue messages.
        raw = (proc.stderr.read() or "").strip()
        last = raw.splitlines()[-1] if raw else "no stderr"
        tail = " ".join(raw[-400:].split())
        raise RuntimeError(f"{task.name} exited {proc.returncode}: {last} || {tail}")
    result_path = out_dir / "result.json"
    ResultJson.model_validate_json(result_path.read_text())
    return str(result_path)


def run_task(task: AnalysisTaskMessage, emit) -> str:
    if task.name in REAL_WRAPPERS:
        return run_wrapper_subprocess(task, emit)
    return run_dummy(task, emit)


# ------------------------------------------------------- heartbeat-safe driver
def _execute_with_heartbeat(connection, channel, task: AnalysisTaskMessage):
    """Run the core in a thread; publish its progress and service AMQP
    heartbeats from the main thread. Returns result path or raises."""
    prog_q: queue.Queue = queue.Queue()
    outcome: dict = {}

    def work():
        try:
            outcome["result"] = run_task(task, lambda p, m: prog_q.put((p, m)))
        except BaseException as e:  # noqa: BLE001
            outcome["error"] = e

    t = threading.Thread(target=work, daemon=True, name=f"task-{task.task_id}")
    t.start()
    while t.is_alive() or not prog_q.empty():
        try:
            while True:
                p, m = prog_q.get_nowait()
                _publish_progress(channel, task, p, m)
        except queue.Empty:
            pass
        connection.process_data_events(time_limit=1)  # heartbeats stay alive
    t.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def handle(connection, channel, method, properties, body: bytes) -> None:
    headers = dict(properties.headers or {})
    attempt = int(headers.get(RETRY_HEADER, 0)) + 1

    try:
        task = AnalysisTaskMessage.model_validate_json(body)
    except Exception as e:  # noqa: BLE001 — poison
        log(f"unparseable task -> DLQ: {e}; body={body[:200]!r}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    log(f"task {task.task_id} ({task.name}) attempt {attempt}/{MAX_TASK_RETRIES}")
    try:
        result_path = _execute_with_heartbeat(connection, channel, task)
        _publish(channel, RESULTS_QUEUE, ResultMessage(
            query_id=task.query_id, task_id=task.task_id,
            status="done", result_json_path=result_path,
        ).model_dump_json())
        channel.basic_ack(delivery_tag=method.delivery_tag)
        log(f"task {task.task_id} done -> {result_path}")
    except Exception as e:  # noqa: BLE001
        err_flat = " ".join(str(e).split())[:400]
        log(f"task {task.task_id} failed on attempt {attempt}: {err_flat}")
        if attempt >= MAX_TASK_RETRIES:
            _publish(channel, RESULTS_QUEUE, ResultMessage(
                query_id=task.query_id, task_id=task.task_id,
                status="failed", error=f"{err_flat} (after {attempt} attempts)",
            ).model_dump_json())
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)  # -> DLQ
        else:
            headers[RETRY_HEADER] = attempt
            _publish(channel, ANALYSIS_QUEUE, body.decode(), headers=headers)
            channel.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    while True:
        try:
            connection, channel = connect_and_declare(AMQP_URL)
            log(f"connected; polling '{ANALYSIS_QUEUE}' (heartbeat-safe loop)")
            while True:
                method, properties, body = channel.basic_get(queue=ANALYSIS_QUEUE)
                if method is None:
                    connection.process_data_events(time_limit=2)
                    continue
                handle(connection, channel, method, properties, body)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:  # noqa: BLE001
            log(f"broker unavailable ({e!r}); retrying in 5 s")
            time.sleep(5)


if __name__ == "__main__":
    main()
