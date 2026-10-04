import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete

from app.agent.prompts import build_system_prompt
from app.catalogue.models import Merchant
from app.core.db import AsyncSessionLocal
from app.main import app
from app.merchants.models import MerchantPreferences
from app.merchants.service import (
    MerchantPreferencesData,
    get_preferences,
    update_preferences,
)


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _auth(clerk_user_id: str):
    return patch(
        "app.auth.deps.verify_clerk_session_token",
        return_value=clerk_user_id,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid.token"}


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(MerchantPreferences).where(
                MerchantPreferences.merchant_id.in_(merchant_ids)
            )
        )
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_merchant(name: str, clerk_user_id: str) -> Merchant:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name, clerk_user_id=clerk_user_id)
        db.add(merchant)
        await db.commit()
        await db.refresh(merchant)
        return merchant


@pytest.mark.asyncio
async def test_get_preferences_defaults_when_no_row() -> None:
    merchant = await _seed_merchant(
        f"pytest-prefs-default-{uuid.uuid4()}",
        f"user_prefs_default_{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            prefs = await get_preferences(db, merchant.id)
            row = await db.get(MerchantPreferences, merchant.id)
        assert row is None
        assert prefs.accepts_cash_on_delivery is True
        assert prefs.accepts_online_payment is True
        assert prefs.timezone == "Africa/Dakar"
        assert prefs.shop_address is None
        assert prefs.opening_hours is None
        assert prefs.return_policy is None
        assert prefs.delivery_fee_note is None
        assert prefs.extra_info is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_update_preferences_upserts_and_is_merchant_scoped() -> None:
    owner = await _seed_merchant(
        f"pytest-prefs-owner-{uuid.uuid4()}",
        f"user_prefs_owner_{uuid.uuid4()}",
    )
    other = await _seed_merchant(
        f"pytest-prefs-other-{uuid.uuid4()}",
        f"user_prefs_other_{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            created = await update_preferences(
                db,
                owner.id,
                MerchantPreferencesData(
                    accepts_cash_on_delivery=True,
                    accepts_online_payment=False,
                    timezone="Europe/Paris",
                    shop_address="Rue 10",
                    opening_hours="9h-19h",
                    return_policy=None,
                    delivery_fee_note="1 500 F",
                    extra_info="Parking",
                ),
            )
            other_prefs = await get_preferences(db, other.id)
        assert created.accepts_online_payment is False
        assert created.timezone == "Europe/Paris"
        assert created.shop_address == "Rue 10"
        assert other_prefs.shop_address is None
        assert other_prefs.accepts_online_payment is True

        async with AsyncSessionLocal() as db:
            updated = await update_preferences(
                db,
                owner.id,
                MerchantPreferencesData(
                    accepts_cash_on_delivery=True,
                    accepts_online_payment=True,
                    timezone="Africa/Dakar",
                    shop_address="Nouvelle adresse",
                    opening_hours=None,
                    return_policy="7 jours",
                    delivery_fee_note=None,
                    extra_info=None,
                ),
            )
            owner_row = await get_preferences(db, owner.id)
            still_other = await get_preferences(db, other.id)
        assert updated.shop_address == "Nouvelle adresse"
        assert updated.return_policy == "7 jours"
        assert updated.opening_hours is None
        assert owner_row.shop_address == "Nouvelle adresse"
        assert still_other.shop_address is None
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_preferences_routes_require_auth_and_validate() -> None:
    clerk_user_id = f"user_prefs_routes_{uuid.uuid4()}"
    merchant = await _seed_merchant(
        f"pytest-prefs-routes-{uuid.uuid4()}", clerk_user_id
    )
    try:
        async with await _client() as client:
            unauth = await client.get("/merchants/me/preferences")
            assert unauth.status_code == 401

        with _auth(clerk_user_id):
            async with await _client() as client:
                defaults = await client.get(
                    "/merchants/me/preferences", headers=_headers()
                )
                assert defaults.status_code == 200
                assert defaults.json()["accepts_cash_on_delivery"] is True
                assert defaults.json()["timezone"] == "Africa/Dakar"
                assert defaults.json()["shop_address"] is None

                both_false = await client.put(
                    "/merchants/me/preferences",
                    headers=_headers(),
                    json={
                        "accepts_cash_on_delivery": False,
                        "accepts_online_payment": False,
                    },
                )
                assert both_false.status_code == 422

                too_long = await client.put(
                    "/merchants/me/preferences",
                    headers=_headers(),
                    json={
                        "accepts_cash_on_delivery": True,
                        "accepts_online_payment": True,
                        "shop_address": "x" * 1001,
                    },
                )
                assert too_long.status_code == 422

                invalid_tz = await client.put(
                    "/merchants/me/preferences",
                    headers=_headers(),
                    json={
                        "accepts_cash_on_delivery": True,
                        "accepts_online_payment": True,
                        "timezone": "Mars/Olympus",
                    },
                )
                assert invalid_tz.status_code == 422

                blanked = await client.put(
                    "/merchants/me/preferences",
                    headers=_headers(),
                    json={
                        "accepts_cash_on_delivery": True,
                        "accepts_online_payment": False,
                        "timezone": "Europe/Paris",
                        "shop_address": "   ",
                        "opening_hours": " 9h-19h ",
                        "return_policy": "",
                    },
                )
                assert blanked.status_code == 200, blanked.text
                body = blanked.json()
                assert body["shop_address"] is None
                assert body["return_policy"] is None
                assert body["opening_hours"] == "9h-19h"
                assert body["timezone"] == "Europe/Paris"
                assert body["accepts_online_payment"] is False
    finally:
        await _cleanup(merchant.id)


def test_build_system_prompt_merchant_settings_and_rule_9() -> None:
    now = datetime(2026, 10, 4, 21, 30, tzinfo=timezone.utc)
    both = build_system_prompt(
        "Boutique Test",
        MerchantPreferencesData(),
        available_cities=["Dakar", "Thiès"],
        now=now,
    )["content"]
    assert "Never offer online payment" not in both
    assert "Never offer cash on delivery" not in both
    assert "payment link here on WhatsApp" in both
    assert "proof of payment" in both
    assert "Cities we deliver to: Dakar, Thiès" in both
    assert "Touba" not in both
    assert "Grand Yoff" not in both.split("Merchant settings:")[0]
    rule_9 = [line for line in both.splitlines() if line.startswith("9.")][0]
    assert "Dakar" not in rule_9
    assert "Grand Yoff" not in rule_9
    assert "<neighbourhood>" in rule_9
    assert "Current time at the shop (Africa/Dakar): dimanche 4 octobre 2026, 21:30" in both
    assert "never state, estimate, or invent a delivery fee" in both
    assert "<shop_info>\n</shop_info>" in both or "<shop_info></shop_info>" in both
    assert "dès que possible" in both
    assert "Opening hours are set (see Horaires)" not in both

    paris = build_system_prompt(
        "Boutique Test",
        MerchantPreferencesData(timezone="Europe/Paris"),
        now=now,
    )["content"]
    assert "Current time at the shop (Europe/Paris): dimanche 4 octobre 2026, 23:30" in paris

    cod_only = build_system_prompt(
        "Boutique Test",
        MerchantPreferencesData(
            accepts_online_payment=False,
            shop_address="Sacré-Cœur",
            opening_hours="Lundi au samedi, 9h à 19h",
            return_policy="7 jours",
            delivery_fee_note="Livraison 1 500 F",
            extra_info="Parking derrière",
        ),
        available_cities=["Dakar"],
        now=now,
    )["content"]
    assert "Never offer online payment" in cod_only
    assert "Never offer cash on delivery" not in cod_only
    assert "payment link here on WhatsApp" not in cod_only
    assert "proof of payment" not in cod_only
    assert "<shop_info>" in cod_only
    assert "Adresse: Sacré-Cœur" in cod_only
    assert "Horaires: Lundi au samedi, 9h à 19h" in cod_only
    assert "Retours et échanges: 7 jours" in cod_only
    assert "Autres informations: Parking derrière" in cod_only
    assert "<delivery_fee_note>" in cod_only
    assert "Livraison 1 500 F" in cod_only
    assert "Opening hours are set (see Horaires)" in cod_only
    assert "dès que possible" not in cod_only

    online_only = build_system_prompt(
        "Boutique Test",
        MerchantPreferencesData(accepts_cash_on_delivery=False),
        now=now,
    )["content"]
    assert "Never offer cash on delivery" in online_only
    assert "Never offer online payment" not in online_only
    assert "payment link after the order" in online_only
    assert "payment link here on WhatsApp" in online_only
    assert "proof of payment" in online_only
