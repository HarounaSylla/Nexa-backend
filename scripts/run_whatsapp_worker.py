"""Run the RQ worker that processes inbound WhatsApp jobs.

Requires Redis (Compose publishes it on host port 6380; REDIS_URL in `.env`).

    uv run python scripts/run_whatsapp_worker.py
"""

from __future__ import annotations

from redis import Redis
from rq import Queue, SimpleWorker

from app.core.config import settings
from app.workers import whatsapp as whatsapp_jobs  # noqa: F401


def main() -> None:
    connection = Redis.from_url(settings.redis_url)
    queues = [Queue("whatsapp", connection=connection)]
    SimpleWorker(queues, connection=connection).work()


if __name__ == "__main__":
    main()
