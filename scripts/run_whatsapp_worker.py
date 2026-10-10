"""Run the RQ worker that processes inbound WhatsApp jobs.

Requires Redis (Compose publishes it on host port 6380; REDIS_URL in `.env`).

Burst batching uses RQ 2.x delayed jobs (`Queue.enqueue_in`). The worker
MUST be started with the scheduler so those flushes run:

    uv run python scripts/run_whatsapp_worker.py

That is `SimpleWorker(...).work(with_scheduler=True)`. Without the
scheduler, delayed flushes stay queued and a burst is never answered
unless a later immediate enqueue happens.
"""

from __future__ import annotations

from redis import Redis
from rq import Queue, SimpleWorker

from app.core.config import settings
from app.workers import maintenance as maintenance_jobs  # noqa: F401
from app.workers import whatsapp as whatsapp_jobs  # noqa: F401


def main() -> None:
    connection = Redis.from_url(settings.redis_url)
    queues = [
        Queue("whatsapp", connection=connection),
        Queue("maintenance", connection=connection),
    ]
    SimpleWorker(queues, connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
