"""M0 queue consumer stub, shared by worker and downloader.

Connects to RabbitMQ (with retry), declares the SS5.6 topology, then
consumes its queue and logs+acks every message. Proves durable-queue
plumbing end to end before any real logic exists (M1 replaces the
handler with the dummy task runner; M2+ with real wrappers/downloads).
"""
import json
import os
import sys
import time

from geohazard_contracts.queues import TASKS_QUEUE, connect_and_declare

ROLE = os.environ.get("SERVICE_ROLE", "worker")
AMQP_URL = os.environ.get("AMQP_URL", "amqp://guest:guest@broker:5672/%2F")


def log(msg: str) -> None:
    print(f"[{ROLE}] {msg}", flush=True)


def handle(channel, method, properties, body: bytes) -> None:
    try:
        payload = json.loads(body)
        kind = payload.get("kind", "?")
    except json.JSONDecodeError:
        payload, kind = None, "unparseable"
    # M0 policy: this stub only observes. Messages for the *other* role are
    # requeued once visible-logged; real kind-based dispatch arrives in M1.
    log(f"received message kind={kind}: {str(payload)[:200]}")
    channel.basic_ack(delivery_tag=method.delivery_tag)


def main() -> None:
    while True:
        try:
            connection, channel = connect_and_declare(AMQP_URL)
            log(f"connected to broker; consuming '{TASKS_QUEUE}' (M0 stub, log+ack)")
            channel.basic_consume(queue=TASKS_QUEUE, on_message_callback=handle)
            channel.start_consuming()
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:  # noqa: BLE001
            log(f"broker unavailable ({e}); retrying in 5 s")
            time.sleep(5)


if __name__ == "__main__":
    main()
