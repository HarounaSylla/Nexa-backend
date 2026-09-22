"""Background jobs. WhatsApp inbound uses RQ on REDIS_URL (Compose: 6380).

Celery is still a declared dependency but unused — RQ is the webhook
queue because nothing in this package was wired to Celery.
"""
