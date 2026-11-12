"""
Integration tests for FastAPI endpoints (app.routes).
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import AuditEntry, PaymentRequest, RuleSet
from app.rules import ensure_ruleset_seeded

# In-memory SQLite using StaticPool so all connections share the same in-memory DB
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


def override_get_session():
    with Session(test_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture(autouse=True)
def setup_database():
    """Reset the in-memory database and ensure PAYPAL_MOCK=True before every test."""
    app.dependency_overrides[get_session] = override_get_session
    from app.llm import MockLLMClient, set_llm_client
    set_llm_client(MockLLMClient())
    original_mock = settings.PAYPAL_MOCK
    settings.PAYPAL_MOCK = True
    SQLModel.metadata.drop_all(test_engine)
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        ensure_ruleset_seeded(session)
    yield
    settings.PAYPAL_MOCK = original_mock
    set_llm_client(None)
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_serve_static_root(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Agent Wallet Guard" in res.text


def test_payment_request_requires_agent_key(client):
    payload = {
        "agent_id": "agent-007",
        "amount": "20.00",
        "currency": "USD",
        "merchant_name": "Test Store",
        "merchant_category": "office",
        "payee_email": settings.VENDOR_SANDBOX_EMAIL,
        "description": "Test purchase",
        "task_context": "Task 1",
        "idempotency_key": "test-key-1",
    }
    # No header -> 401
    res = client.post("/api/v1/payment-requests", json=payload)
    assert res.status_code == 401

    # Bad key -> 401
    res = client.post(
        "/api/v1/payment-requests",
        json=payload,
        headers={"X-Agent-Key": "wrong-key"},
    )
    assert res.status_code == 401


def test_payment_request_creation_and_idempotency(client):
    headers = {"X-Agent-Key": settings.AGENT_API_KEY}
    payload = {
        "agent_id": "agent-007",
        "amount": "25.00",
        "currency": "USD",
        "merchant_name": "Office Depot",
        "merchant_category": "office",
        "payee_email": settings.VENDOR_SANDBOX_EMAIL,
        "description": "Paper and pens",
        "task_context": "Restock supplies",
        "idempotency_key": "unique-idem-key-12345",
    }

    # First request (Auto-approved and dispatched to PayPal -> status PAID)
    res1 = client.post("/api/v1/payment-requests", json=payload, headers=headers)
    assert res1.status_code == 201
    data1 = res1.json()
    assert data1["decision"] == "APPROVE"
    assert data1["status"] == "PAID"
    assert data1["paypal_status"] == "SUCCESS"
    req_id = data1["id"]

    # Second request with identical idempotency_key
    res2 = client.post("/api/v1/payment-requests", json=payload, headers=headers)
    assert res2.status_code in (200, 201)
    data2 = res2.json()
    assert data2["id"] == req_id
    assert data2["decision"] == "APPROVE"
    assert data2["status"] == "PAID"

    # Verify in DB that only one record exists
    with Session(test_engine) as session:
        records = session.exec(
            select(PaymentRequest).where(
                PaymentRequest.idempotency_key == "unique-idem-key-12345"
            )
        ).all()
        assert len(records) == 1


def test_budget_math_rolling_7_days(client):
    now = datetime.now(timezone.utc)
    ruleset_currency = "USD"

    with Session(test_engine) as session:
        # 1. Transaction 8 days ago (APPROVED) - outside window -> NOT counted
        t_outside = PaymentRequest(
            id=uuid.uuid4(),
            created_at=now - timedelta(days=8),
            agent_id="test",
            amount=Decimal("50.00"),
            currency=ruleset_currency,
            merchant_name="Old Vendor",
            merchant_category="office",
            payee_email="sb-vendor@business.example.com",
            description="Old",
            task_context="Old",
            idempotency_key="t-outside-1",
            decision="APPROVE",
            status="APPROVED",
            reasons=[],
            risk_score=0,
            flags=[],
            decided_by="rules",
            decided_at=now - timedelta(days=8),
        )
        # 2. Transaction 4 days ago (APPROVED) - inside window -> COUNTED
        t_approved = PaymentRequest(
            id=uuid.uuid4(),
            created_at=now - timedelta(days=4),
            agent_id="test",
            amount=Decimal("30.00"),
            currency=ruleset_currency,
            merchant_name="Vendor A",
            merchant_category="office",
            payee_email="sb-vendor@business.example.com",
            description="Mid",
            task_context="Mid",
            idempotency_key="t-mid-1",
            decision="APPROVE",
            status="APPROVED",
            reasons=[],
            risk_score=0,
            flags=[],
            decided_by="rules",
            decided_at=now - timedelta(days=4),
        )
        # 3. Transaction 2 days ago (PAID) - inside window -> COUNTED
        t_paid = PaymentRequest(
            id=uuid.uuid4(),
            created_at=now - timedelta(days=2),
            agent_id="test",
            amount=Decimal("45.00"),
            currency=ruleset_currency,
            merchant_name="Vendor B",
            merchant_category="software",
            payee_email="sb-vendor@business.example.com",
            description="Software",
            task_context="Soft",
            idempotency_key="t-paid-1",
            decision="APPROVE",
            status="PAID",
            reasons=[],
            risk_score=0,
            flags=[],
            decided_by="rules",
            decided_at=now - timedelta(days=2),
        )
        # 4. Transaction 1 day ago (BLOCKED) - inside window -> NOT counted
        t_blocked = PaymentRequest(
            id=uuid.uuid4(),
            created_at=now - timedelta(days=1),
            agent_id="test",
            amount=Decimal("100.00"),
            currency=ruleset_currency,
            merchant_name="Casino",
            merchant_category="gambling",
            payee_email="sb-vendor@business.example.com",
            description="Gamble",
            task_context="Bad",
            idempotency_key="t-blocked-1",
            decision="BLOCK",
            status="BLOCKED",
            reasons=[],
            risk_score=95,
            flags=["blocked_category"],
            decided_by="rules",
            decided_at=now - timedelta(days=1),
        )
        # 5. Transaction 1 day ago (ESCALATED) - inside window -> NOT counted
        t_escalated = PaymentRequest(
            id=uuid.uuid4(),
            created_at=now - timedelta(days=1),
            agent_id="test",
            amount=Decimal("80.00"),
            currency=ruleset_currency,
            merchant_name="Vendor C",
            merchant_category="office",
            payee_email="sb-vendor@business.example.com",
            description="Pending human review",
            task_context="Escalate",
            idempotency_key="t-esc-1",
            decision="ESCALATE",
            status="ESCALATED",
            reasons=[],
            risk_score=50,
            flags=["exceeds_auto_limit"],
            decided_by="rules",
            decided_at=now - timedelta(days=1),
        )

        session.add_all([t_outside, t_approved, t_paid, t_blocked, t_escalated])
        session.commit()

    # Query GET /api/v1/budget
    # Expected spend = 30.00 + 45.00 = 75.00 USD
    # Base weekly budget = 200.00 USD -> Remaining = 125.00 USD
    res = client.get("/api/v1/budget")
    assert res.status_code == 200
    data = res.json()
    assert float(data["spent_this_week"]) == 75.00
    assert float(data["weekly_budget"]) == 200.00
    assert float(data["remaining"]) == 125.00


def test_approve_and_deny_only_from_escalated(client):
    headers = {"X-Agent-Key": settings.AGENT_API_KEY}

    # 1. Create a request that will ESCALATE (amount $75 > $50 auto-approve limit)
    res_esc = client.post(
        "/api/v1/payment-requests",
        json={
            "agent_id": "agent-007",
            "amount": "75.00",
            "currency": "USD",
            "merchant_name": "Tech Corp",
            "merchant_category": "software",
            "payee_email": settings.VENDOR_SANDBOX_EMAIL,
            "description": "Software license upgrade",
            "task_context": "Upgrade dev tools",
            "idempotency_key": "esc-test-1",
        },
        headers=headers,
    )
    assert res_esc.status_code == 201
    esc_data = res_esc.json()
    assert esc_data["decision"] == "ESCALATE"
    assert esc_data["status"] == "ESCALATED"
    req_id_1 = esc_data["id"]

    # 2. Approve ESCALATED request -> 200 OK (Dispatches PayPal and sets status to PAID)
    res_app = client.post(f"/api/v1/requests/{req_id_1}/approve")
    assert res_app.status_code == 200
    approved_data = res_app.json()
    assert approved_data["status"] == "PAID"
    assert approved_data["paypal_status"] == "SUCCESS"
    assert approved_data["decided_by"] == "human"

    # 3. Approving again must fail with 409 Conflict (status is now PAID, not ESCALATED)
    res_app_again = client.post(f"/api/v1/requests/{req_id_1}/approve")
    assert res_app_again.status_code == 409

    # 4. Denying an already PAID/APPROVED request must fail with 409 Conflict
    res_deny_app = client.post(f"/api/v1/requests/{req_id_1}/deny")
    assert res_deny_app.status_code == 409

    # 5. Create another request that ESCALATES
    res_esc_2 = client.post(
        "/api/v1/payment-requests",
        json={
            "agent_id": "agent-007",
            "amount": "85.00",
            "currency": "USD",
            "merchant_name": "Gadgets Inc",
            "merchant_category": "electronics",
            "payee_email": settings.VENDOR_SANDBOX_EMAIL,
            "description": "Testing unit",
            "task_context": "Hardware prototype",
            "idempotency_key": "esc-test-2",
        },
        headers=headers,
    )
    req_id_2 = res_esc_2.json()["id"]

    # 6. Deny the ESCALATED request -> 200 OK
    res_deny = client.post(f"/api/v1/requests/{req_id_2}/deny")
    assert res_deny.status_code == 200
    denied_data = res_deny.json()
    assert denied_data["status"] == "DENIED"
    assert denied_data["decided_by"] == "human"

    # 7. Denying again must fail with 409 Conflict
    assert client.post(f"/api/v1/requests/{req_id_2}/deny").status_code == 409


def test_rules_endpoints_and_audit(client):
    # GET /rules
    res = client.get("/api/v1/rules")
    assert res.status_code == 200
    data = res.json()
    assert float(data["per_txn_auto_limit"]) == 50.00

    # PUT /rules with invalid negative value -> 422
    bad_res = client.put(
        "/api/v1/rules",
        json={"per_txn_auto_limit": "-10.00"},
    )
    assert bad_res.status_code == 422

    # PUT /rules with valid changes -> 200
    good_res = client.put(
        "/api/v1/rules",
        json={"per_txn_auto_limit": "75.00", "hard_block_limit": "500.00"},
    )
    assert good_res.status_code == 200
    updated = good_res.json()
    assert float(updated["per_txn_auto_limit"]) == 75.00
    assert float(updated["hard_block_limit"]) == 500.00

    # Verify audit verify passes
    audit_res = client.get("/api/v1/audit/verify")
    assert audit_res.status_code == 200
    assert audit_res.json()["valid"] is True


def test_refresh_payment_endpoint(client):
    headers = {"X-Agent-Key": settings.AGENT_API_KEY}
    # 1. Create paid request
    res = client.post(
        "/api/v1/payment-requests",
        json={
            "agent_id": "agent-007",
            "amount": "15.00",
            "currency": "USD",
            "merchant_name": "Book Store",
            "merchant_category": "books",
            "payee_email": settings.VENDOR_SANDBOX_EMAIL,
            "description": "Python Guide",
            "task_context": "Learning Python",
            "idempotency_key": "refresh-test-key-1",
        },
        headers=headers,
    )
    assert res.status_code == 201
    req_id = res.json()["id"]
    assert res.json()["status"] == "PAID"

    # 2. Call refresh-payment
    ref_res = client.post(f"/api/v1/requests/{req_id}/refresh-payment")
    assert ref_res.status_code == 200
    ref_data = ref_res.json()
    assert ref_data["paypal_status"] == "SUCCESS"

    # 3. Request without paypal_batch_id fails with 400
    with Session(test_engine) as session:
        unpaid = PaymentRequest(
            id=uuid.uuid4(),
            agent_id="test",
            amount=Decimal("10.00"),
            currency="USD",
            merchant_name="NoPay",
            merchant_category="office",
            payee_email=settings.VENDOR_SANDBOX_EMAIL,
            description="No batch",
            task_context="None",
            idempotency_key="no-batch-key-1",
            decision="BLOCK",
            status="BLOCKED",
            reasons=[],
            risk_score=90,
            flags=[],
            decided_by="rules",
            paypal_batch_id=None,
        )
        session.add(unpaid)
        session.commit()
        unpaid_id = unpaid.id

    bad_ref = client.post(f"/api/v1/requests/{unpaid_id}/refresh-payment")
    assert bad_ref.status_code == 400


def test_paypal_failure_flow(client, monkeypatch):
    """When PayPal fails, status becomes PAYMENT_FAILED, decision remains APPROVE, and audit logs paypal_error."""
    from app import paypal

    async def mock_failed_payout(req, client=None):
        return {
            "success": False,
            "error": "Simulated PayPal failure: insufficient funds",
            "status": "FAILED",
        }

    monkeypatch.setattr(paypal, "create_payout", mock_failed_payout)

    headers = {"X-Agent-Key": settings.AGENT_API_KEY}
    res = client.post(
        "/api/v1/payment-requests",
        json={
            "agent_id": "agent-007",
            "amount": "20.00",
            "currency": "USD",
            "merchant_name": "Store",
            "merchant_category": "office",
            "payee_email": settings.VENDOR_SANDBOX_EMAIL,
            "description": "Item",
            "task_context": "Task",
            "idempotency_key": "fail-test-key-1",
        },
        headers=headers,
    )
    assert res.status_code == 201
    data = res.json()
    assert data["decision"] == "APPROVE"  # Decision preserved!
    assert data["status"] == "PAYMENT_FAILED"
    assert "insufficient funds" in data["paypal_error"]

    # Verify audit chain has paypal_error event
    audit_res = client.get(f"/api/v1/audit?request_id={data['id']}")
    assert audit_res.status_code == 200
    events = [entry["event"] for entry in audit_res.json()]
    assert "paypal_error" in events


def test_lifespan_refuses_live_mode_without_allow_live(monkeypatch):
    """Refuse to start if PAYPAL_ENV is not sandbox unless ALLOW_LIVE=true."""
    import asyncio
    from app.main import lifespan

    monkeypatch.setattr(settings, "PAYPAL_ENV", "live")
    monkeypatch.setattr(settings, "ALLOW_LIVE", False)

    with pytest.raises(RuntimeError, match="Startup blocked"):
        async def run_test():
            async with lifespan(app):
                pass
        asyncio.run(run_test())


def test_lifespan_allows_live_mode_with_allow_live(monkeypatch):
    """Allow startup when ALLOW_LIVE=true even if PAYPAL_ENV=live."""
    import asyncio
    from app.main import lifespan

    monkeypatch.setattr(settings, "PAYPAL_ENV", "live")
    monkeypatch.setattr(settings, "ALLOW_LIVE", True)

    async def run_test():
        async with lifespan(app):
            pass
    asyncio.run(run_test())


