#!/usr/bin/env python3
"""
Agent Wallet Guard - PayPal Sandbox Smoke Test Script

This script validates end-to-end PayPal Sandbox connectivity:
1. Loads configuration from .env.
2. Authenticates with PayPal Sandbox via OAuth 2.0 (client credentials grant).
3. Dispatches a $1.00 USD Payout to VENDOR_SANDBOX_EMAIL via POST /v1/payments/payouts.
4. Outputs the full HTTP status and JSON response payload.
5. Polls GET /v1/payments/payouts/{batch_id} once and outputs the batch status.

Usage:
    python scripts/paypal_smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


def load_environment() -> None:
    """Load environment variables from .env if present."""
    # Attempt to use python-dotenv first
    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parent.parent / ".env"
        load_dotenv(dotenv_path=env_path)
        return
    except ImportError:
        pass

    # Simple fallback parser if python-dotenv is not installed yet
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = val


def get_required_env(key: str, placeholder: str | None = None) -> str:
    """Retrieve an environment variable and validate it is not empty or placeholder."""
    val = os.getenv(key, "").strip()
    if not val:
        print(f"[ERROR] Missing required environment variable: {key}", file=sys.stderr)
        print(
            f"        Please set {key} in your .env file (see .env.example).",
            file=sys.stderr,
        )
        sys.exit(1)
    if placeholder and val == placeholder:
        print(
            f"[ERROR] Environment variable {key} is still set to placeholder '{placeholder}'.",
            file=sys.stderr,
        )
        print(
            f"        Please update {key} with your real sandbox credentials in .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


def main() -> None:
    # Ensure dependencies are available
    try:
        import httpx
    except ImportError:
        print(
            "[ERROR] Package 'httpx' is required but not installed.",
            file=sys.stderr,
        )
        print(
            "        Please activate your virtual environment and install dependencies:",
            file=sys.stderr,
        )
        print("          python -m venv .venv", file=sys.stderr)
        print("          .venv\\Scripts\\activate  # on Windows", file=sys.stderr)
        print("          pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    load_environment()

    client_id = get_required_env("PAYPAL_CLIENT_ID", "your_paypal_client_id_here")
    client_secret = get_required_env(
        "PAYPAL_CLIENT_SECRET", "your_paypal_client_secret_here"
    )
    vendor_email = get_required_env(
        "VENDOR_SANDBOX_EMAIL", "sb-vendor@business.example.com"
    )
    paypal_env = os.getenv("PAYPAL_ENV", "sandbox").lower()

    if paypal_env != "sandbox":
        print(
            f"[WARNING] PAYPAL_ENV is set to '{paypal_env}'. This smoke test is strictly designed for 'sandbox'.",
            file=sys.stderr,
        )

    base_url = "https://api-m.sandbox.paypal.com"
    oauth_url = f"{base_url}/v1/oauth2/token"
    payouts_url = f"{base_url}/v1/payments/payouts"

    print("=" * 70)
    print(" Agent Wallet Guard — PayPal Sandbox Smoke Test")
    print("=" * 70)
    print(f"Target Base URL:       {base_url}")
    print(f"Vendor Sandbox Email:  {vendor_email}")
    print(f"Client ID:             {client_id[:8]}...{client_id[-4:]}")
    print("-" * 70)

    # 1. Obtain OAuth 2.0 Access Token
    print("\n[Step 1/3] Requesting OAuth 2.0 Access Token...")
    try:
        with httpx.Client(timeout=30.0) as client:
            token_response = client.post(
                oauth_url,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en_US",
                },
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
            )
    except httpx.RequestError as exc:
        print(f"[FATAL] Network error connecting to {oauth_url}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"HTTP Status: {token_response.status_code}")
    if token_response.status_code != 200:
        print("[ERROR] Failed to obtain access token from PayPal.", file=sys.stderr)
        try:
            print(json.dumps(token_response.json(), indent=2), file=sys.stderr)
        except Exception:
            print(token_response.text, file=sys.stderr)
        sys.exit(1)

    token_data = token_response.json()
    access_token = token_data.get("access_token")
    expires_in = token_data.get("expires_in")
    app_id = token_data.get("app_id")
    print(f"[SUCCESS] OAuth token received. App ID: {app_id}, Expires In: {expires_in}s")

    auth_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    # 2. Dispatch $1.00 USD Payout
    unique_batch_id = f"AWG_SMOKE_{int(time.time())}_{uuid.uuid4().hex[:6].upper()}"
    unique_item_id = f"AWG_ITEM_{int(time.time())}_{uuid.uuid4().hex[:6].upper()}"

    payout_payload = {
        "sender_batch_header": {
            "sender_batch_id": unique_batch_id,
            "email_subject": "Agent Wallet Guard: Sandbox Smoke Test Payout",
            "email_message": "Verification transaction dispatched by Agent Wallet Guard smoke test suite.",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {
                    "value": "1.00",
                    "currency": "USD",
                },
                "receiver": vendor_email,
                "note": "Agent Wallet Guard smoke test ($1.00 USD)",
                "sender_item_id": unique_item_id,
            }
        ],
    }

    print("\n[Step 2/3] Dispatching $1.00 USD Payout...")
    print(f"POST {payouts_url}")
    print(f"Sender Batch ID: {unique_batch_id}")

    try:
        with httpx.Client(timeout=30.0) as client:
            payout_response = client.post(
                payouts_url,
                headers=auth_headers,
                json=payout_payload,
            )
    except httpx.RequestError as exc:
        print(f"[FATAL] Network error connecting to {payouts_url}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"HTTP Status: {payout_response.status_code}")
    try:
        payout_data = payout_response.json()
        print("Response JSON:")
        print(json.dumps(payout_data, indent=2))
    except Exception:
        payout_data = {}
        print("Raw Response Body:")
        print(payout_response.text)

    if payout_response.status_code not in (200, 201):
        print(
            f"[ERROR] Payout creation returned non-success code {payout_response.status_code}.",
            file=sys.stderr,
        )
        sys.exit(1)

    batch_id = payout_data.get("batch_header", {}).get("payout_batch_id")
    if not batch_id:
        print(
            "[ERROR] No 'payout_batch_id' found in batch_header response.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. Poll Payout Batch Status
    poll_url = f"{base_url}/v1/payments/payouts/{batch_id}"
    print(f"\n[Step 3/3] Polling Payout Batch Status for ID '{batch_id}'...")
    print(f"GET {poll_url}")

    # Brief delay to allow PayPal's asynchronous worker to register the batch
    time.sleep(2)

    try:
        with httpx.Client(timeout=30.0) as client:
            poll_response = client.get(poll_url, headers=auth_headers)
    except httpx.RequestError as exc:
        print(f"[FATAL] Network error connecting to {poll_url}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"HTTP Status: {poll_response.status_code}")
    try:
        poll_data = poll_response.json()
        print("Response JSON:")
        print(json.dumps(poll_data, indent=2))
        batch_status = poll_data.get("batch_header", {}).get("batch_status")
        print(f"\n[RESULT] Payout Batch Status: {batch_status}")
    except Exception:
        print("Raw Response Body:")
        print(poll_response.text)

    print("\n" + "=" * 70)
    print(" PayPal Sandbox Smoke Test Completed Successfully!")
    print("=" * 70)


if __name__ == "__main__":
    main()

