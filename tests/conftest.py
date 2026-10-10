"""Shared fixtures. WhatsApp burst tests never talk to real Redis/RQ."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.whatsapp.batch import MemoryBatchBackend, set_backend, set_scheduler


@pytest.fixture(autouse=True)
def whatsapp_batch_test_env(monkeypatch):
    """Kill-switch on in unit tests: quiet 0 flushes in the ingest job.

    Memory backend + a recorder scheduler so delayed paths never hit RQ.
    Tests that need a window set quiet > 0 and read `scheduled`.
    """
    monkeypatch.setattr(settings, "whatsapp_batch_quiet_seconds", 0.0)
    backend = MemoryBatchBackend()
    scheduled: list[tuple] = []

    def _record(merchant_id, customer_phone, token, phone_number_id, delay):
        scheduled.append(
            (merchant_id, customer_phone, token, phone_number_id, delay)
        )

    set_backend(backend)
    set_scheduler(_record)
    yield {"backend": backend, "scheduled": scheduled}
    set_backend(None)
    set_scheduler(None)
