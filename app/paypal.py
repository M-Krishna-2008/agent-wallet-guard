"""
PayPal Integration Service.

Supports:
1. Real PayPal Sandbox execution via REST API (OAuth 2.0 with in-memory caching).
2. Payouts API (POST /v1/payments/payouts, GET /v1/payments/payouts/{batch_id}).
3. Fallback Orders v2 API (POST /v2/checkout/orders, GET /v2/checkout/orders/{id}) when PAYPAL_MODE=orders.
4. Timeouts (10s) and single retry for network errors and 5xx only (never retry 4xx).
5. Offline simulation when PAYPAL_MOCK=True.
6. Double-payment prevention guard for requests already marked as PAID.
7. Strict confidentiality: never logs secrets or access tokens.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

# In-memory OAuth token caching
_cached_token: Optional[str] = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


def clear_token_cache() -> None:
    """Clear in-memory token cache (used in testing)."""
    global _cached_token, _token_expires_at
    _cached_token = None
    _token_expires_at = 0.0


async def _execute_with_retry(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    auth: Optional[tuple[str, str]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> httpx.Response:
    """
    Executes an HTTP request with a 10s timeout.
    Performs exactly ONE retry for network errors or 5xx server errors.
    Never retries 4xx client errors.
    """
    timeout = 10.0

    async def _send(c: httpx.AsyncClient) -> httpx.Response:
        return await c.request(
            method=method,
            url=url,
            headers=headers,
            json=json_data,
            data=data,
            auth=auth,
        )

    for attempt in range(2):
        try:
            if client is not None:
                resp = await _send(client)
            else:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    resp = await _send(c)

            # Retry once on 5xx server errors only
            if resp.status_code >= 500 and attempt == 0:
                logger.warning(
                    "PayPal returned HTTP %s for %s. Retrying once...",
                    resp.status_code,
                    url,
                )
                await asyncio.sleep(0.5)
                continue

            return resp

        except (httpx.RequestError, httpx.TimeoutException) as exc:
            if attempt == 0:
                logger.warning(
                    "Network error (%s) contacting %s. Retrying once...",
                    type(exc).__name__,
                    url,
                )
                await asyncio.sleep(0.5)
                continue
            raise


async def get_access_token(client: Optional[httpx.AsyncClient] = None) -> str:
    """
    Obtain PayPal OAuth 2.0 access token via client-credentials grant.
    Caches token in memory until ~60s before expiration.
    """
    global _cached_token, _token_expires_at

    now = time.time()
    if _cached_token and now < _token_expires_at:
        return _cached_token

    async with _token_lock:
        # Re-check inside lock
        now = time.time()
        if _cached_token and now < _token_expires_at:
            return _cached_token

        base_url = settings.paypal_base_url
        oauth_url = f"{base_url}/v1/oauth2/token"

        resp = await _execute_with_retry(
            method="POST",
            url=oauth_url,
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
            data={"grant_type": "client_credentials"},
            auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
            client=client,
        )

        if resp.status_code != 200:
            error_detail = _sanitize_error(resp.text)
            raise RuntimeError(
                f"PayPal OAuth authentication failed (HTTP {resp.status_code}): {error_detail}"
            )

        token_data = resp.json()
        token = token_data["access_token"]
        expires_in = int(token_data.get("expires_in", 3600))

        # Cache until ~60 seconds before actual expiry
        _cached_token = token
        _token_expires_at = now + max(0, expires_in - 60)

        return _cached_token


def _sanitize_error(text: str) -> str:
    """Sanitize any sensitive credentials from error messages before recording."""
    cleaned = text
    if settings.PAYPAL_CLIENT_SECRET and settings.PAYPAL_CLIENT_SECRET != "your_paypal_client_secret_here":
        cleaned = cleaned.replace(settings.PAYPAL_CLIENT_SECRET, "[REDACTED_SECRET]")
    if settings.AGENT_API_KEY:
        cleaned = cleaned.replace(settings.AGENT_API_KEY, "[REDACTED_AGENT_KEY]")
    if _cached_token:
        cleaned = cleaned.replace(_cached_token, "[REDACTED_BEARER_TOKEN]")
    return cleaned[:1000]


async def create_payout(
    request: Any,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Executes a payment for the approved PaymentRequest.

    Enforces:
    - Double payment guard: requests with status='PAID' will not be re-submitted.
    - PAYPAL_MOCK: returns offline mock success.
    - PAYPAL_MODE=orders: fallback Orders v2 creation.
    - Default: Payouts API with sender_batch_id=request.id.
    """
    # 1. Guard against duplicate payments
    current_status = getattr(request, "status", None)
    if current_status == "PAID":
        return {
            "success": False,
            "error": f"Request {request.id} is already PAID. Duplicate payout rejected.",
            "status": "ALREADY_PAID",
            "batch_id": getattr(request, "paypal_batch_id", None),
        }

    req_id_str = str(getattr(request, "id", ""))
    payee_email = str(getattr(request, "payee_email", "")).strip()
    raw_amount = getattr(request, "amount", Decimal("0.00"))
    amount_str = f"{Decimal(str(raw_amount)):.2f}"
    currency = str(getattr(request, "currency", "USD")).strip().upper()
    description = str(getattr(request, "description", "Agent Wallet Guard Payout"))

    # 2. Offline Mock Simulation
    if settings.PAYPAL_MOCK:
        mock_batch_id = f"MOCK_BATCH_{req_id_str}"
        return {
            "success": True,
            "mock": True,
            "mode": "payouts",
            "batch_id": mock_batch_id,
            "status": "SUCCESS",
            "approval_url": None,
            "http_status": 201,
            "raw": {
                "mock": True,
                "batch_header": {
                    "payout_batch_id": mock_batch_id,
                    "batch_status": "SUCCESS",
                    "sender_batch_header": {
                        "sender_batch_id": req_id_str,
                        "email_subject": "Payment approved by Agent Wallet Guard",
                    },
                },
            },
        }

    # 3. Live PayPal Execution
    try:
        token = await get_access_token(client=client)
    except Exception as exc:
        err_msg = _sanitize_error(str(exc))
        return {
            "success": False,
            "error": err_msg,
            "status": "AUTH_FAILED",
            "batch_id": None,
            "approval_url": None,
        }

    base_url = settings.paypal_base_url
    auth_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    # -------------------------------------------------------------------------
    # Mode: Orders v2 Fallback
    # -------------------------------------------------------------------------
    if settings.PAYPAL_MODE.lower() == "orders":
        orders_url = f"{base_url}/v2/checkout/orders"
        orders_payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": req_id_str,
                    "description": description[:127],
                    "amount": {
                        "currency_code": currency,
                        "value": amount_str,
                    },
                    "payee": {
                        "email_address": payee_email,
                    },
                }
            ],
        }

        try:
            resp = await _execute_with_retry(
                method="POST",
                url=orders_url,
                headers=auth_headers,
                json_data=orders_payload,
                client=client,
            )

            if resp.status_code in (200, 201):
                order_data = resp.json()
                order_id = order_data.get("id", req_id_str)
                order_status = order_data.get("status", "CREATED")

                # Extract approval checkout URL
                approval_url = None
                for link in order_data.get("links", []):
                    if link.get("rel") in ("approve", "payer-action"):
                        approval_url = link.get("href")
                        break

                return {
                    "success": True,
                    "mock": False,
                    "mode": "orders",
                    "batch_id": order_id,
                    "status": order_status,
                    "approval_url": approval_url,
                    "http_status": resp.status_code,
                    "raw": order_data,
                }
            else:
                return {
                    "success": False,
                    "mock": False,
                    "error": _sanitize_error(f"Orders API error (HTTP {resp.status_code}): {resp.text}"),
                    "status": "FAILED",
                    "batch_id": None,
                    "approval_url": None,
                    "http_status": resp.status_code,
                }
        except Exception as exc:
            return {
                "success": False,
                "mock": False,
                "error": _sanitize_error(f"Orders network error: {exc}"),
                "status": "ERROR",
                "batch_id": None,
                "approval_url": None,
            }

    # -------------------------------------------------------------------------
    # Mode: Payouts (Default)
    # -------------------------------------------------------------------------
    payouts_url = f"{base_url}/v1/payments/payouts"
    payout_payload = {
        "sender_batch_header": {
            "sender_batch_id": req_id_str,
            "email_subject": "Payment approved by Agent Wallet Guard",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "receiver": payee_email,
                "amount": {
                    "value": amount_str,
                    "currency": currency,
                },
                "note": description[:250],
                "sender_item_id": req_id_str,
            }
        ],
    }

    try:
        resp = await _execute_with_retry(
            method="POST",
            url=payouts_url,
            headers=auth_headers,
            json_data=payout_payload,
            client=client,
        )

        if resp.status_code in (200, 201):
            payout_data = resp.json()
            batch_id = payout_data.get("batch_header", {}).get("payout_batch_id", req_id_str)
            batch_status = payout_data.get("batch_header", {}).get("batch_status", "PENDING")

            return {
                "success": True,
                "mock": False,
                "mode": "payouts",
                "batch_id": batch_id,
                "status": batch_status,
                "approval_url": None,
                "http_status": resp.status_code,
                "raw": payout_data,
            }
        else:
            return {
                "success": False,
                "mock": False,
                "error": _sanitize_error(f"Payouts API error (HTTP {resp.status_code}): {resp.text}"),
                "status": "FAILED",
                "batch_id": None,
                "approval_url": None,
                "http_status": resp.status_code,
            }
    except Exception as exc:
        return {
            "success": False,
            "mock": False,
            "error": _sanitize_error(f"Payouts network exception: {exc}"),
            "status": "ERROR",
            "batch_id": None,
            "approval_url": None,
        }


async def get_payout_status(
    batch_id: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Polls the current status of a PayPal payout or order.
    Returns:
        {
            "batch_status": str,
            "transaction_status": str,
            "raw": dict
        }
    """
    if settings.PAYPAL_MOCK or batch_id.startswith("MOCK_"):
        return {
            "batch_status": "SUCCESS",
            "transaction_status": "SUCCESS",
            "raw": {"mock": True},
        }

    token = await get_access_token(client=client)
    base_url = settings.paypal_base_url
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    # If orders mode
    if settings.PAYPAL_MODE.lower() == "orders":
        order_url = f"{base_url}/v2/checkout/orders/{batch_id}"
        resp = await _execute_with_retry(
            method="GET",
            url=order_url,
            headers=headers,
            client=client,
        )
        if resp.status_code == 200:
            data = resp.json()
            order_status = data.get("status", "UNKNOWN")
            return {
                "batch_status": order_status,
                "transaction_status": order_status,
                "raw": data,
            }
        else:
            return {
                "batch_status": "UNKNOWN",
                "transaction_status": "UNKNOWN",
                "raw": {"error": _sanitize_error(resp.text)},
            }

    # Payouts status
    payout_url = f"{base_url}/v1/payments/payouts/{batch_id}"
    resp = await _execute_with_retry(
        method="GET",
        url=payout_url,
        headers=headers,
        client=client,
    )

    if resp.status_code == 200:
        data = resp.json()
        batch_header = data.get("batch_header", {})
        batch_status = batch_header.get("batch_status", "UNKNOWN")

        items = data.get("items", [])
        if items and isinstance(items, list):
            item_status = items[0].get("transaction_status", batch_status)
        else:
            item_status = batch_status

        return {
            "batch_status": batch_status,
            "transaction_status": item_status,
            "raw": data,
        }
    else:
        return {
            "batch_status": "UNKNOWN",
            "transaction_status": "UNKNOWN",
            "raw": {"error": _sanitize_error(resp.text)},
        }
