"""Run the inbound-image file purge once (same job as the daily RQ cron).

    uv run python scripts/purge_expired_inbound_images.py
"""

from app.workers.maintenance import purge_expired_inbound_images

if __name__ == "__main__":
    purge_expired_inbound_images()
