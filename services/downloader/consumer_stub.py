"""geohazard-chat downloader — M1 idle stub.

At M0 this stub consumed `tasks` alongside the worker. From M1.1 the worker
runs *real* analysis tasks on that queue, so the downloader must not compete
for them (it would steal and ack analysis messages). Until download tasks
exist (M2), it only declares the topology and heartbeats.
"""
import os
import time

from geohazard_contracts.queues import connect_and_declare

ROLE = os.environ.get("SERVICE_ROLE", "downloader")
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")


def log(msg: str) -> None:
    print(f"[{ROLE}] {msg}", flush=True)


def main() -> None:
    while True:
        try:
            connection, channel = connect_and_declare(AMQP_URL)
            log("connected; idle until M2 (no download tasks exist yet)")
            while True:
                connection.process_data_events(time_limit=30)  # keep heartbeats alive
        except Exception as e:  # noqa: BLE001
            log(f"broker unavailable ({e!r}); retrying in 5 s")
            time.sleep(5)


if __name__ == "__main__":
    main()
