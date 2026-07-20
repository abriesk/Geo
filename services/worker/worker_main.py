"""geohazard-chat worker — M1.1 (walking skeleton).

Replaces the M0 log+ack stub. Consumes `tasks` (kind=analysis only for now),
runs the dummy wrapper inline: ~10 s of work, real ProgressMessages to the
`progress` queue, a schema-valid synthetic result.json + placeholder PNG,
then a ResultMessage to `results`.

Retry / poison-message policy (§5.6, §7):
- Transient failure: republish the message with an incremented
  `x-retry-count` header and ack the original (delivery order is not a
  contract requirement, so republish-at-tail is fine).
- After MAX_TASK_RETRIES attempts: publish a failed ResultMessage, then
  basic_nack(requeue=False) -> broker dead-letters to `tasks.dlq`.
- Unparseable body: straight to DLQ (no retry can fix poison).

Test hook: params.simulate_failure=true makes the wrapper raise mid-run
(backend sets it when the question contains "FAIL!"). This is how we
exercise the retry->DLQ path end to end.

M2 replaces run_dummy() with dispatch to real wrapper subprocesses.
"""
from __future__ import annotations

import json
import os
import sys
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
    MAX_TASK_RETRIES,
    PROGRESS_QUEUE,
    RESULTS_QUEUE,
    TASKS_QUEUE,
    connect_and_declare,
)

ROLE = os.environ.get("SERVICE_ROLE", "worker")
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")
DUMMY_STEP_SECONDS = float(os.environ.get("DUMMY_STEP_SECONDS", "2"))
RETRY_HEADER = "x-retry-count"


def log(msg: str) -> None:
    print(f"[{ROLE}] {msg}", flush=True)


def _publish(channel, queue: str, body: str, headers: dict | None = None) -> None:
    channel.basic_publish(
        exchange="",
        routing_key=queue,
        body=body,
        properties=pika.BasicProperties(delivery_mode=2, headers=headers or {}),
    )


def _progress(channel, task: AnalysisTaskMessage, percent: int, message: str) -> None:
    msg = ProgressMessage(
        query_id=task.query_id,
        task_id=task.task_id,
        message=message,
        percent=percent,
        ts=datetime.now(timezone.utc),
    )
    _publish(channel, PROGRESS_QUEUE, msg.model_dump_json())
    log(f"PROGRESS {percent} {message} (task {task.task_id})")


def _write_placeholder_png(path: Path) -> None:
    """A recognizable fake 'velocity map' so the M1.2 UI has something to show."""
    from PIL import Image, ImageDraw

    w, h = 480, 360
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            # blue->red horizontal ramp, like a velocity colormap
            px[x, y] = (int(255 * x / w), 40, int(255 * (1 - x / w)))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, w - 10, 50], fill=(255, 255, 255))
    d.text((20, 22), "DUMMY velocity map — M1 walking skeleton", fill=(0, 0, 0))
    d.text((20, h - 30), "synthetic data, not a measurement", fill=(255, 255, 255))
    img.save(path)


def run_dummy(channel, task: AnalysisTaskMessage) -> str:
    """The M1 dummy wrapper. Returns path to result.json."""
    out_dir = Path(task.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    steps = ["fetching cached stack", "inverting time series", "masking low coherence",
             "computing statistics", "rendering map"]
    for i, step in enumerate(steps):
        _progress(channel, task, int(i * 100 / len(steps)), step)
        time.sleep(DUMMY_STEP_SECONDS)
        if task.params.get("simulate_failure") and i == 2:
            raise RuntimeError("simulated wrapper failure (test hook)")

    png_path = out_dir / "velocity_map.png"
    _write_placeholder_png(png_path)

    result = ResultJson(
        query_id=task.query_id,
        method="licsbas",  # dummy impersonates the M3 method; see caveats
        status="ok",
        summary_stats={
            "deformation": {
                "velocity_mm_yr_min": -12.4,
                "velocity_mm_yr_max": 2.9,
                "velocity_mm_yr_mean_aoi": -4.1,
                "hotspot_fraction": 0.09,
                "trend": "subsiding",
            }
        },
        quality={
            "scene_count": 30,
            "date_coverage": [
                str(task.dates.start or "2024-07-01"),
                str(task.dates.end or "2026-07-01"),
            ],
            "coherence_mean": 0.61,
            "masked_fraction": 0.27,
            "cloud_fraction": None,
            "confidence": Confidence.MODERATE,
            "caveats": [
                "SYNTHETIC RESULT — M1 walking skeleton, numbers are fabricated",
            ],
        },
        artifacts=[
            {
                "type": "map_png",
                "path": "velocity_map.png",
                "caption": "Synthetic LOS velocity map (placeholder)",
            }
        ],
        attribution=["Synthetic data (geohazard-chat M1 dummy wrapper)"],
    )
    result_path = out_dir / "result.json"
    result_path.write_text(result.model_dump_json(indent=2))
    _progress(channel, task, 100, "done")
    return str(result_path)


def handle(channel, method, properties, body: bytes) -> None:
    headers = dict(properties.headers or {})
    attempt = int(headers.get(RETRY_HEADER, 0)) + 1

    try:
        task = AnalysisTaskMessage.model_validate_json(body)
    except Exception as e:  # noqa: BLE001 — poison message: no retry can fix it
        log(f"unparseable task -> DLQ: {e}; body={body[:200]!r}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    log(f"task {task.task_id} ({task.name}) attempt {attempt}/{MAX_TASK_RETRIES}")
    try:
        result_path = run_dummy(channel, task)
        _publish(
            channel,
            RESULTS_QUEUE,
            ResultMessage(
                query_id=task.query_id,
                task_id=task.task_id,
                status="done",
                result_json_path=result_path,
            ).model_dump_json(),
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
        log(f"task {task.task_id} done -> {result_path}")
    except Exception as e:  # noqa: BLE001
        log(f"task {task.task_id} failed on attempt {attempt}: {e!r}")
        if attempt >= MAX_TASK_RETRIES:
            _publish(
                channel,
                RESULTS_QUEUE,
                ResultMessage(
                    query_id=task.query_id,
                    task_id=task.task_id,
                    status="failed",
                    error=f"{e!r} (after {attempt} attempts)",
                ).model_dump_json(),
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)  # -> DLQ
            log(f"task {task.task_id} dead-lettered after {attempt} attempts")
        else:
            headers[RETRY_HEADER] = attempt
            _publish(channel, TASKS_QUEUE, body.decode(), headers=headers)
            channel.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    while True:
        try:
            connection, channel = connect_and_declare(AMQP_URL)
            log(f"connected; consuming '{TASKS_QUEUE}' (M1.1 dummy wrapper)")
            channel.basic_consume(queue=TASKS_QUEUE, on_message_callback=handle)
            channel.start_consuming()
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:  # noqa: BLE001
            log(f"broker unavailable ({e!r}); retrying in 5 s")
            time.sleep(5)


if __name__ == "__main__":
    main()
