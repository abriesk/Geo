"""geohazard-chat downloader — M2.2 (§5.3, CDSE tier via eodag).

Consumes `tasks.download`. Implemented tier: `cdse` (Sentinel-2 L2A for the
NDVI path). Tiers egms/licsar/hyp3/aux answer with a clean failure until
their milestones (M3/M4/M6).

Scene selection policy (M2.2, NDVI change detection needs a "before" and an
"after" image): search S2_MSI_L2A over the AOI and date range with
cloudCover <= CLOUD_MAX; pick the lowest-cloud scene in the EARLIEST third
of the range and the lowest-cloud scene in the LATEST third. If a third is
empty, fall back to the two lowest-cloud scenes overall at least 30 days
apart; 1 scene -> download it (wrapper will report partial); 0 -> fail
honestly ("zero acquisitions", §11.2 edge case).

Cache (§5.2 resp 3 / §6.2): after download, upsert cached_data keyed on
aoi_hash + product_type 's2' + the *requested* date range; file_paths =
{"dir": <archive dir>, "scenes": [paths]}. Expiry 90 days.

Retry policy: same x-retry-count republish pattern as the worker; CDSE
quota/throttle errors get exponential backoff within the attempt and an
honest progress message (§5.3), not a silent retry.

Built against eodag 3.x API (productType=...); eodag pinned <4 in
requirements (4.x renamed productType -> collection).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pika

from geohazard_contracts import DownloadTaskMessage, ProgressMessage, ResultMessage, aoi_hash
from geohazard_contracts.queues import (
    DOWNLOAD_QUEUE,
    MAX_TASK_RETRIES,
    PROGRESS_QUEUE,
    RESULTS_QUEUE,
    connect_and_declare,
)

ROLE = os.environ.get("SERVICE_ROLE", "downloader")
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://geohazard:geohazard@db:5432/geohazard")
ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", "/data/archive")
CLOUD_MAX = int(os.environ.get("S2_CLOUD_MAX", "40"))
CACHE_EXPIRY_DAYS = int(os.environ.get("CACHE_EXPIRY_DAYS", "90"))
RETRY_HEADER = "x-retry-count"

# eodag reads CDSE credentials from its own env names (provider docs).
os.environ.setdefault(
    "EODAG__COP_DATASPACE__AUTH__CREDENTIALS__USERNAME", os.environ.get("CDSE_USERNAME", "")
)
os.environ.setdefault(
    "EODAG__COP_DATASPACE__AUTH__CREDENTIALS__PASSWORD", os.environ.get("CDSE_PASSWORD", "")
)


def log(msg: str) -> None:
    print(f"[{ROLE}] {msg}", flush=True)


def _publish(channel, queue: str, body: str, headers: dict | None = None) -> None:
    channel.basic_publish(
        exchange="",
        routing_key=queue,
        body=body,
        properties=pika.BasicProperties(delivery_mode=2, headers=headers or {}),
    )


def _aoi_wkt(task: DownloadTaskMessage) -> str:
    ring = task.aoi.coordinates[0]
    pts = list(ring)
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    inner = ", ".join(f"{lon} {lat}" for lon, lat in pts)
    return f"POLYGON (({inner}))"


def _pick_scenes(products) -> list:
    """Earliest-third + latest-third lowest-cloud picks (see module docstring)."""
    def cloud(p):
        return p.properties.get("cloudCover") or 0.0

    def sensing(p):
        return p.properties.get("startTimeFromAscendingNode") or p.properties.get("title", "")

    prods = sorted(products, key=sensing)
    if len(prods) <= 2:
        return prods
    third = max(1, len(prods) // 3)
    early = sorted(prods[:third], key=cloud)
    late = sorted(prods[-third:], key=cloud)
    picks = [early[0], late[0]]
    if picks[0] is picks[1]:
        picks = sorted(prods, key=cloud)[:2]
    return picks


def _upsert_cache(task: DownloadTaskMessage, out_dir: Path, scene_paths: list[str]) -> None:
    import psycopg

    a_hash = aoi_hash(task.aoi.coordinates[0])
    file_paths = json.dumps({"dir": str(out_dir), "scenes": scene_paths})
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """INSERT INTO cached_data
               (aoi_hash, dates_start, dates_end, product_type, file_paths, expiry_ts)
               VALUES (%s, %s, %s, 's2', %s, now() + %s * interval '1 day')""",
            (a_hash, task.dates.start, task.dates.end, file_paths, CACHE_EXPIRY_DAYS),
        )
    log(f"cache upsert: {a_hash[:12]}… s2 {task.dates.start}..{task.dates.end}")


def run_cdse(task: DownloadTaskMessage, emit) -> str:
    """Search + download S2 L2A. Returns the archive dir path."""
    from eodag import EODataAccessGateway, setup_logging

    setup_logging(2)
    a_hash = aoi_hash(task.aoi.coordinates[0])
    out_dir = Path(ARCHIVE_ROOT) / "s2" / a_hash[:16] / f"{task.dates.start}_{task.dates.end}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Two targeted window searches instead of one range search: eodag 3.x
    # returns only the first result page, which over a 12-month range
    # truncates to the earliest weeks and breaks before/after pairing
    # (observed live in M2.2). Windows also spare the CDSE quota.
    dag = EODataAccessGateway()
    dag.set_preferred_provider("cop_dataspace")
    wkt = _aoi_wkt(task)

    def search_window(w_start, w_end, label):
        emit(5, f"searching Copernicus catalog ({label} window)")
        res = dag.search(productType="S2_MSI_L2A", geom=wkt,
                         start=str(w_start), end=str(w_end), cloudCover=CLOUD_MAX)
        prods = list(res)
        log(f"search {label} {w_start}..{w_end}: {len(prods)} products")
        return prods

    def lowest_cloud(prods):
        return sorted(prods, key=lambda p: p.properties.get("cloudCover") or 0.0)[0] if prods else None

    span = (task.dates.end - task.dates.start).days
    win = min(60, max(20, span // 4))
    early = lowest_cloud(search_window(task.dates.start,
                                       task.dates.start + timedelta(days=win), "early"))
    late = lowest_cloud(search_window(task.dates.end - timedelta(days=win),
                                      task.dates.end, "late"))
    if early is None and span > 2 * win:  # widen once
        early = lowest_cloud(search_window(task.dates.start,
                                           task.dates.start + timedelta(days=2 * win), "early+"))
    if late is None and span > 2 * win:
        late = lowest_cloud(search_window(task.dates.end - timedelta(days=2 * win),
                                          task.dates.end, "late+"))

    picks = [p for p in (early, late) if p is not None]
    # dedupe identical product
    if len(picks) == 2 and picks[0].properties.get("title") == picks[1].properties.get("title"):
        picks = picks[:1]
    if not picks:
        raise RuntimeError(
            f"zero Sentinel-2 acquisitions with cloud cover <= {CLOUD_MAX}% "
            f"in {task.dates.start}..{task.dates.end} for this area"
        )
    emit(15, f"selected {len(picks)} scene(s) "
              f"({'early+late' if len(picks) == 2 else 'single window only'})")

    scene_paths: list[str] = []
    for i, product in enumerate(picks):
        title = product.properties.get("title", f"scene{i}")
        emit(20 + i * 35, f"downloading {title[:60]}")
        backoff = 30
        for attempt_dl in range(4):
            try:
                path = dag.download(product, output_dir=str(out_dir), extract=True)
                scene_paths.append(str(path))
                break
            except Exception as e:  # noqa: BLE001
                text = f"{type(e).__name__}: {e}"
                quota = any(k in text.lower() for k in ("429", "quota", "too many", "throttl"))
                if quota and attempt_dl < 3:
                    emit(20 + i * 35,
                         f"CDSE quota/throttle hit; backing off {backoff}s (§5.3)")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise

    emit(92, "updating cache index")
    _upsert_cache(task, out_dir, scene_paths)
    emit(100, f"download done: {len(scene_paths)} scene(s)")
    return str(out_dir)


def _execute_with_heartbeat(connection, channel, task: DownloadTaskMessage):
    """Run the download in a thread; publish progress + service heartbeats
    from the main thread (fix for StreamLostError on long downloads)."""
    import queue as _q
    import threading as _t

    prog_q: _q.Queue = _q.Queue()
    outcome: dict = {}

    def work():
        try:
            if task.tier.value != "cdse":
                raise RuntimeError(
                    f"download tier '{task.tier.value}' not implemented until its milestone (§11)"
                )
            outcome["result"] = run_cdse(task, lambda p, m: prog_q.put((p, m)))
        except BaseException as e:  # noqa: BLE001
            outcome["error"] = e

    th = _t.Thread(target=work, daemon=True, name=f"dl-{task.task_id}")
    th.start()
    while th.is_alive() or not prog_q.empty():
        try:
            while True:
                p, m = prog_q.get_nowait()
                _publish_progress(channel, task, p, m)
        except _q.Empty:
            pass
        connection.process_data_events(time_limit=1)
    th.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def _publish_progress(channel, task: DownloadTaskMessage, percent: int, message: str) -> None:
    msg = ProgressMessage(
        query_id=task.query_id, task_id=task.task_id, message=message,
        percent=percent, ts=datetime.now(timezone.utc),
    )
    _publish(channel, PROGRESS_QUEUE, msg.model_dump_json())
    log(f"PROGRESS {percent} {message}")


def handle(connection, channel, method, properties, body: bytes) -> None:
    headers = dict(properties.headers or {})
    attempt = int(headers.get(RETRY_HEADER, 0)) + 1

    try:
        task = DownloadTaskMessage.model_validate_json(body)
    except Exception as e:  # noqa: BLE001 — poison
        log(f"unparseable download task -> DLQ: {e}; body={body[:200]!r}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    log(f"download task {task.task_id} tier={task.tier.value} attempt {attempt}/{MAX_TASK_RETRIES}")
    try:
        out_dir = _execute_with_heartbeat(connection, channel, task)
        _publish(channel, RESULTS_QUEUE, ResultMessage(
            query_id=task.query_id, task_id=task.task_id,
            status="done", result_json_path=out_dir,  # §6.4 note: data dir for downloads
        ).model_dump_json())
        channel.basic_ack(delivery_tag=method.delivery_tag)
        log(f"download task {task.task_id} done -> {out_dir}")
    except Exception as e:  # noqa: BLE001
        log(f"download task {task.task_id} failed on attempt {attempt}: {e!r}")
        if attempt >= MAX_TASK_RETRIES:
            _publish(channel, RESULTS_QUEUE, ResultMessage(
                query_id=task.query_id, task_id=task.task_id,
                status="failed", error=f"{e} (after {attempt} attempts)",
            ).model_dump_json())
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)  # -> DLQ
        else:
            headers[RETRY_HEADER] = attempt
            _publish(channel, DOWNLOAD_QUEUE, body.decode(), headers=headers)
            channel.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    if not os.environ.get("CDSE_USERNAME"):
        log("WARNING: CDSE_USERNAME empty — cdse downloads will fail at auth")
    while True:
        try:
            connection, channel = connect_and_declare(AMQP_URL)
            log(f"connected; polling '{DOWNLOAD_QUEUE}' (M2.3b heartbeat-safe loop)")
            while True:
                method, properties, body = channel.basic_get(queue=DOWNLOAD_QUEUE)
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
