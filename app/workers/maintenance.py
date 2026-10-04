"""RQ jobs for periodic maintenance (stale conversation sweep)."""

from __future__ import annotations

import asyncio
import logging

from app.agent.orchestrator import CONVERSATION_INACTIVITY_TIMEOUT
from app.agent.service import fermer_conversations_inactives
from app.core.db import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)


def close_stale_conversations() -> None:
    """RQ entry point. Sync wrapper around the async sweep."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_close_stale_conversations_async())
    except Exception:
        logger.exception("close_stale_conversations crashed")


async def _close_stale_conversations_async() -> None:
    try:
        async with AsyncSessionLocal() as db:
            closed = await fermer_conversations_inactives(
                db, CONVERSATION_INACTIVITY_TIMEOUT
            )
            logger.info("closed %s inactive conversation(s)", closed)
    finally:
        await engine.dispose()
