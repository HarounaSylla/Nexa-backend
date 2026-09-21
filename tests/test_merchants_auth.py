import uuid
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete

from app.auth.clerk import InvalidClerkTokenError
from app.catalogue.models import Merchant
from app.core.db import AsyncSessionLocal
from app.main import app


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_get_current_merchant_missing_token_is_401() -> None:
    async with await _client() as client:
        response = await client.get("/merchants/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "missing_token"


@pytest.mark.asyncio
async def test_get_current_merchant_malformed_token_is_401() -> None:
    with patch(
        "app.auth.deps.verify_clerk_session_token",
        side_effect=InvalidClerkTokenError("Invalid session token"),
    ):
        async with await _client() as client:
            response = await client.get(
                "/merchants/me", headers={"Authorization": "Bearer not-a-jwt"}
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_current_merchant_expired_token_is_401() -> None:
    with patch(
        "app.auth.deps.verify_clerk_session_token",
        side_effect=InvalidClerkTokenError("Token expired"),
    ):
        async with await _client() as client:
            response = await client.get(
                "/merchants/me",
                headers={"Authorization": "Bearer expired.token"},
            )
    assert response.status_code == 401
    assert response.json()["detail"] == "Token expired"


@pytest.mark.asyncio
async def test_get_current_merchant_valid_token_without_merchant_is_404() -> None:
    with patch(
        "app.auth.deps.verify_clerk_session_token",
        return_value=f"user_not_onboarded_{uuid.uuid4()}",
    ):
        async with await _client() as client:
            response = await client.get(
                "/merchants/me", headers={"Authorization": "Bearer valid.token"}
            )
    assert response.status_code == 404
    assert response.json()["detail"] == "merchant_not_onboarded"


@pytest.mark.asyncio
async def test_onboarding_is_idempotent_per_clerk_user() -> None:
    clerk_user_id = f"user_onboard_{uuid.uuid4()}"
    headers = {"Authorization": "Bearer valid.token"}
    body = {"name": f"Shop {uuid.uuid4()}"}
    merchant_id = None
    try:
        with patch(
            "app.auth.deps.verify_clerk_session_token",
            return_value=clerk_user_id,
        ):
            async with await _client() as client:
                first = await client.post(
                    "/merchants/onboarding", json=body, headers=headers
                )
                second = await client.post(
                    "/merchants/onboarding", json=body, headers=headers
                )
        assert first.status_code == 200, first.text
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["clerk_user_id"] == clerk_user_id
        merchant_id = uuid.UUID(first.json()["id"])
    finally:
        if merchant_id is not None:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(Merchant).where(Merchant.id == merchant_id))
                await db.commit()


@pytest.mark.asyncio
async def test_link_demo_succeeds_once_then_409_for_second_caller() -> None:
    demo_name = f"pytest-demo-{uuid.uuid4()}"
    first_user = f"user_link_a_{uuid.uuid4()}"
    second_user = f"user_link_b_{uuid.uuid4()}"
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=demo_name)
        db.add(merchant)
        await db.commit()
        await db.refresh(merchant)
        merchant_id = merchant.id
    try:
        with patch("app.merchants.service.DEMO_MERCHANT_NAME", demo_name):
            with patch(
                "app.auth.deps.verify_clerk_session_token",
                return_value=first_user,
            ):
                async with await _client() as client:
                    first = await client.post(
                        "/merchants/link-demo",
                        headers={"Authorization": "Bearer token-a"},
                    )
            assert first.status_code == 200, first.text
            assert first.json()["id"] == str(merchant_id)
            assert first.json()["clerk_user_id"] == first_user

            with patch(
                "app.auth.deps.verify_clerk_session_token",
                return_value=second_user,
            ):
                async with await _client() as client:
                    second = await client.post(
                        "/merchants/link-demo",
                        headers={"Authorization": "Bearer token-b"},
                    )
            assert second.status_code == 409
            assert second.json()["detail"] == "demo_merchant_already_linked"
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Merchant).where(Merchant.id == merchant_id))
            await db.commit()
