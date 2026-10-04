"""Run the RQ cron scheduler for periodic maintenance jobs.

Requires Redis (Compose publishes it on host port 6380; REDIS_URL in `.env`).

    uv run python scripts/run_cron_scheduler.py
    uv run python scripts/run_cron_scheduler.py --interval 30
"""

from __future__ import annotations

import argparse

from redis import Redis
from rq.cron import CronScheduler

from app.core.config import settings
from app.workers.maintenance import close_stale_conversations

DEFAULT_INTERVAL_SECONDS = 900


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enqueue periodic RQ maintenance jobs."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=(
            "Seconds between close_stale_conversations runs "
            f"(default: {DEFAULT_INTERVAL_SECONDS})"
        ),
    )
    args = parser.parse_args()

    connection = Redis.from_url(settings.redis_url)
    scheduler = CronScheduler(connection=connection)
    scheduler.register(
        close_stale_conversations,
        queue_name="maintenance",
        interval=args.interval,
        result_ttl=60,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
