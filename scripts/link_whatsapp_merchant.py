"""Attach Boutique Awa to the Cloud API Phone Number ID in `.env`.

    uv run python scripts/link_whatsapp_merchant.py
"""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.merchants.service import (
    DemoMerchantNotFoundError,
    link_demo_whatsapp_phone_number,
)


async def main() -> None:
    phone_id = settings.whatsapp_phone_number_id.strip()
    if not phone_id:
        raise SystemExit(
            "WHATSAPP_PHONE_NUMBER_ID is empty. Set it in .env first."
        )
    async with AsyncSessionLocal() as db:
        try:
            merchant = await link_demo_whatsapp_phone_number(db, phone_id)
        except DemoMerchantNotFoundError as exc:
            raise SystemExit(str(exc)) from exc
    print(
        f"linked merchant_id={merchant.id} name={merchant.name} "
        f"whatsapp_phone_number_id={merchant.whatsapp_phone_number_id}"
    )


if __name__ == "__main__":
    asyncio.run(main())
