"""RQ jobs for periodic maintenance (stale conversation sweep, image purge)."""

from __future__ import annotations

import asyncio
import logging

from app.agent.orchestrator import CONVERSATION_INACTIVITY_TIMEOUT
from app.agent.service import fermer_conversations_inactives
from app.core.db import AsyncSessionLocal, engine
from app.proofs.service import purger_images_expirees

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


def purge_expired_inbound_images() -> None:
    """RQ entry point. Sync wrapper around the inbound-image file purge."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_purge_expired_inbound_images_async())
    except Exception:
        logger.exception("purge_expired_inbound_images crashed")


async def _purge_expired_inbound_images_async() -> None:
    try:
        async with AsyncSessionLocal() as db:
            purged = await purger_images_expirees(db)
            logger.info("purged %s expired inbound image file(s)", purged)
    finally:
        await engine.dispose()
