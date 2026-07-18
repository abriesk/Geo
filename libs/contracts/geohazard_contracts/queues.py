"""RabbitMQ topology (§5.6).

Queues `tasks`, `progress`, `results`: durable, manual ack.
`tasks` dead-letters to `tasks.dlq` (poison-message protection).
Per-message retry limit (3) is enforced by consumers reading the
x-death header before requeue/reject — that logic lands with the
real consumers (M1+); the topology is fixed here.

Every service declares this topology idempotently at startup, so
boot order doesn't matter.
"""
from __future__ import annotations

TASKS_QUEUE = "tasks"
PROGRESS_QUEUE = "progress"
RESULTS_QUEUE = "results"
TASKS_DLQ = "tasks.dlq"

MAX_TASK_RETRIES = 3  # §5.6 / §7 error path
PREFETCH_COUNT = 1    # §5.6, worker & downloader consumers


def declare_topology(channel) -> None:
    """Idempotently declare all queues on a pika channel."""
    channel.queue_declare(queue=TASKS_DLQ, durable=True)
    channel.queue_declare(
        queue=TASKS_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": TASKS_DLQ,
        },
    )
    channel.queue_declare(queue=PROGRESS_QUEUE, durable=True)
    channel.queue_declare(queue=RESULTS_QUEUE, durable=True)


def connect_and_declare(amqp_url: str):
    """Blocking connection + declared topology. Returns (connection, channel)."""
    import pika

    params = pika.URLParameters(amqp_url)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.basic_qos(prefetch_count=PREFETCH_COUNT)
    declare_topology(channel)
    return connection, channel
