"""
Unit tests for app.paypal:
- In-memory OAuth token caching
- Payout request payload formatting and sender_batch_id
- Fallback Orders v2 mode (PAYPAL_MODE=orders)
- Retries on network error / 5xx, never retrying 4xx
- Guard against duplicate payments for requests already marked as PAID
- Status polling and sanitization of sensitive secrets
"""

import asyncio
import uuid
from decimal import Decimal
import httpx
import pytest
from app.config import settings
from app.models import PaymentRequest
from app.paypal import (
    clear_token_cache,
    create_payout,
    get_access_token,
    get_payout_status,
)


@pytest.fixture(autouse=True)
def reset_paypal_state():
    """Reset token cache and ensure settings defaults before each test."""
    clear_token_cache()
    original_mock = settings.PAYPAL_MOCK
    original_mode = settings.PAYPAL_MODE
    yield
    clear_token_cache()
    settings.PAYPAL_MOCK = original_mock
    settings.PAYPAL_MODE = original_mode


def make_dummy_request(status="APPROVED", amount="20.00") -> PaymentRequest:
    return PaymentRequest(
        id=uuid.uuid4(),
        agent_id="test-agent",
        amount=Decimal(amount),
        currency="USD",
        merchant_name="Vendor Store",
        merchant_category="office",
        payee_email="sb-vendor@business.example.com",
        description="Office stationary",
        task_context="Procurement task",
        idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        decision="APPROVE",
        status=status,
    )


def test_mock_mode_skips_network_calls():
    """When PAYPAL_MOCK=True, create_payout returns mock success immediately."""
    async def _run():
        settings.PAYPAL_MOCK = True
        req = make_dummy_request(status="APPROVED")
        res = await create_payout(req)
        assert res["success"] is True
        assert res["mock"] is True
        assert res["status"] == "SUCCESS"
        assert res["batch_id"].startswith("MOCK_BATCH_")

    asyncio.run(_run())


def test_guard_rejects_already_paid_requests():
    """A request that is already PAID cannot be executed again."""
    async def _run():
        settings.PAYPAL_MOCK = False
        req = make_dummy_request(status="PAID")
        res = await create_payout(req)
        assert res["success"] is False
        assert res["status"] == "ALREADY_PAID"
        assert "already PAID" in res["error"]

    asyncio.run(_run())


def test_oauth_token_caching():
    """Token is fetched once and reused until close to expiry."""
    async def _run():
        settings.PAYPAL_MOCK = False
        call_count = 0

        def mock_transport_handler(request: httpx.Request):
            nonlocal call_count
            if request.url.path == "/v1/oauth2/token":
                call_count += 1
                return httpx.Response(
                    200,
                    json={"access_token": "mocked-token-xyz-123", "expires_in": 3600},
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_transport_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            token1 = await get_access_token(client=client)
            assert token1 == "mocked-token-xyz-123"
            assert call_count == 1

            token2 = await get_access_token(client=client)
            assert token2 == "mocked-token-xyz-123"
            assert call_count == 1

    asyncio.run(_run())


def test_payout_request_body_shape():
    """Verify exact payload shape for Payouts API."""
    async def _run():
        settings.PAYPAL_MOCK = False
        settings.PAYPAL_MODE = "payouts"
        captured_payload = None

        def mock_transport_handler(request: httpx.Request):
            nonlocal captured_payload
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(
                    200,
                    json={"access_token": "test-token", "expires_in": 3600},
                )
            if request.url.path == "/v1/payments/payouts":
                import json
                captured_payload = json.loads(request.content.decode("utf-8"))
                return httpx.Response(
                    201,
                    json={
                        "batch_header": {
                            "payout_batch_id": "PAYPAL_BATCH_999",
                            "batch_status": "PENDING",
                        }
                    },
                )
            return httpx.Response(404)

        req = make_dummy_request(amount="20.00")
        transport = httpx.MockTransport(mock_transport_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await create_payout(req, client=client)
            assert res["success"] is True
            assert res["batch_id"] == "PAYPAL_BATCH_999"
            assert res["status"] == "PENDING"

        assert captured_payload is not None
        assert captured_payload["sender_batch_header"]["sender_batch_id"] == str(req.id)
        assert (
            captured_payload["sender_batch_header"]["email_subject"]
            == "Payment approved by Agent Wallet Guard"
        )
        item = captured_payload["items"][0]
        assert item["recipient_type"] == "EMAIL"
        assert item["receiver"] == "sb-vendor@business.example.com"
        assert item["amount"] == {"value": "20.00", "currency": "USD"}
        assert item["sender_item_id"] == str(req.id)

    asyncio.run(_run())


def test_fallback_orders_v2_mode():
    """Verify Orders v2 fallback when PAYPAL_MODE=orders."""
    async def _run():
        settings.PAYPAL_MOCK = False
        settings.PAYPAL_MODE = "orders"
        captured_payload = None

        def mock_transport_handler(request: httpx.Request):
            nonlocal captured_payload
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(
                    200,
                    json={"access_token": "test-token", "expires_in": 3600},
                )
            if request.url.path == "/v2/checkout/orders":
                import json
                captured_payload = json.loads(request.content.decode("utf-8"))
                return httpx.Response(
                    201,
                    json={
                        "id": "ORDER_12345",
                        "status": "CREATED",
                        "links": [
                            {
                                "rel": "approve",
                                "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER_12345",
                            }
                        ],
                    },
                )
            return httpx.Response(404)

        req = make_dummy_request(amount="35.50")
        transport = httpx.MockTransport(mock_transport_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await create_payout(req, client=client)
            assert res["success"] is True
            assert res["mode"] == "orders"
            assert res["batch_id"] == "ORDER_12345"
            assert res["status"] == "CREATED"
            assert (
                res["approval_url"]
                == "https://www.sandbox.paypal.com/checkoutnow?token=ORDER_12345"
            )

        assert captured_payload is not None
        assert captured_payload["intent"] == "CAPTURE"
        pu = captured_payload["purchase_units"][0]
        assert pu["amount"]["value"] == "35.50"
        assert pu["payee"]["email_address"] == "sb-vendor@business.example.com"

    asyncio.run(_run())


def test_retry_on_network_error_and_5xx_only():
    """Test retries: one retry on 500, zero retries on 400."""
    async def _run():
        settings.PAYPAL_MOCK = False
        settings.PAYPAL_MODE = "payouts"

        # 1. 500 Server error -> retries once, then fails
        payout_attempts = 0

        def mock_500_handler(request: httpx.Request):
            nonlocal payout_attempts
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            if request.url.path == "/v1/payments/payouts":
                payout_attempts += 1
                return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_500_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            req = make_dummy_request()
            res = await create_payout(req, client=client)
            assert res["success"] is False
            assert payout_attempts == 2  # Original attempt + 1 retry

        # 2. 400 Client error -> NEVER retries
        payout_attempts_400 = 0

        def mock_400_handler(request: httpx.Request):
            nonlocal payout_attempts_400
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            if request.url.path == "/v1/payments/payouts":
                payout_attempts_400 += 1
                return httpx.Response(400, text="Bad Request: Invalid receiver")
            return httpx.Response(404)

        transport_400 = httpx.MockTransport(mock_400_handler)
        async with httpx.AsyncClient(transport=transport_400) as client:
            req = make_dummy_request()
            res = await create_payout(req, client=client)
            assert res["success"] is False
            assert payout_attempts_400 == 1  # No retry for 4xx!

    asyncio.run(_run())


def test_get_payout_status_returns_item_status():
    """Test get_payout_status extracts batch and item transaction status."""
    async def _run():
        settings.PAYPAL_MOCK = False

        def mock_handler(request: httpx.Request):
            if request.url.path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            if request.url.path == "/v1/payments/payouts/BATCH_XYZ":
                return httpx.Response(
                    200,
                    json={
                        "batch_header": {"batch_status": "SUCCESS"},
                        "items": [
                            {"transaction_status": "SUCCESS", "receiver": "user@test.com"}
                        ],
                    },
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            status_info = await get_payout_status("BATCH_XYZ", client=client)
            assert status_info["batch_status"] == "SUCCESS"
            assert status_info["transaction_status"] == "SUCCESS"

    asyncio.run(_run())
