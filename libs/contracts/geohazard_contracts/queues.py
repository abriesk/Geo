"""RabbitMQ topology (§5.6, amended M2.2).

Kind-split task queues: `tasks.analysis` (worker) and `tasks.download`
(downloader), both durable, manual ack, dead-lettering to `tasks.dlq`.
`progress` and `results` unchanged. Message contracts (§6.4) unchanged.

Every service declares this topology idempotently at startup.
Migration note: the pre-M2.2 `tasks` queue is left untouched by this code;
delete it manually once drained:
  docker compose exec broker rabbitmqctl delete_queue tasks
"""
from __future__ import annotations

ANALYSIS_QUEUE = "tasks.analysis"
DOWNLOAD_QUEUE = "tasks.download"
PROGRESS_QUEUE = "progress"
RESULTS_QUEUE = "results"
TASKS_DLQ = "tasks.dlq"

MAX_TASK_RETRIES = 3  # §5.6 / §7 error path
PREFETCH_COUNT = 1    # §5.6, worker & downloader consumers

_DLQ_ARGS = {"x-dead-letter-exchange": "", "x-dead-letter-routing-key": TASKS_DLQ}


def declare_topology(channel) -> None:
    """Idempotently declare all queues on a pika channel."""
    channel.queue_declare(queue=TASKS_DLQ, durable=True)
    channel.queue_declare(queue=ANALYSIS_QUEUE, durable=True, arguments=dict(_DLQ_ARGS))
    channel.queue_declare(queue=DOWNLOAD_QUEUE, durable=True, arguments=dict(_DLQ_ARGS))
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
