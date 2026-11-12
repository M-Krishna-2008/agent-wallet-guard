"""
Payment request handling, human approval workflow, and budget tracking routes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlmodel import Session, select
from app import engine as decision_engine
from app import paypal
from app.audit import record_audit_event
from app.config import settings
from app.db import get_session
from app.models import (
    BudgetResponse,
    PaymentRequest,
    PaymentRequestCreate,
    PaymentRequestResponse,
)
from app.rules import ensure_ruleset_seeded

router = APIRouter(tags=["Payments"])


def verify_agent_api_key(
    x_agent_key: Optional[str] = Header(None, alias="X-Agent-Key"),
) -> str:
    """Validate X-Agent-Key header against configured AGENT_API_KEY."""
    if not x_agent_key or x_agent_key.strip() != settings.AGENT_API_KEY.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing X-Agent-Key header",
        )
    return x_agent_key


@router.post(
    "/payment-requests",
    response_model=PaymentRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_payment_request(
    payload: PaymentRequestCreate,
    session: Session = Depends(get_session),
    _key: str = Depends(verify_agent_api_key),
):
    """
    Submit an agent payment request.
    Evaluates policy guardrails, runs LLM assessment, logs all stages in the hash chain,
    and automatically executes PayPal payout if approved.
    If idempotency_key was already used, returns the previously stored decision immediately.
    """
    # 1. Idempotency Check
    existing = session.exec(
        select(PaymentRequest).where(
            PaymentRequest.idempotency_key == payload.idempotency_key
        )
    ).first()

    if existing:
        return PaymentRequestResponse(
            id=existing.id,
            decision=existing.decision,
            status=existing.status,
            reasons=existing.reasons,
            risk_score=existing.risk_score,
            flags=existing.flags,
            paypal_batch_id=existing.paypal_batch_id,
            paypal_status=existing.paypal_status,
            paypal_approval_url=existing.paypal_approval_url,
            paypal_error=existing.paypal_error,
        )

    # 2. Seed / fetch current ruleset
    ruleset = ensure_ruleset_seeded(session)

    # 3. Audit: request_received
    record_audit_event(
        session=session,
        actor="agent",
        event="request_received",
        request_id=None,
        details={
            "agent_id": payload.agent_id,
            "amount": str(payload.amount),
            "currency": payload.currency,
            "merchant_name": payload.merchant_name,
            "merchant_category": payload.merchant_category,
            "payee_email": payload.payee_email,
            "idempotency_key": payload.idempotency_key,
        },
    )

    # 4. Calculate rolling 7-day spend
    window_start = datetime.now(timezone.utc) - timedelta(days=7)
    budget_query = select(PaymentRequest).where(
        PaymentRequest.status.in_(["APPROVED", "PAID"]),
        PaymentRequest.created_at >= window_start,
        PaymentRequest.currency == payload.currency.upper(),
    )
    matching_txs = session.exec(budget_query).all()
    spent_this_week = sum(
        (tx.amount for tx in matching_txs),
        Decimal("0.00"),
    )

    # 5. Evaluate Decision Engine
    eval_res = decision_engine.evaluate(
        request=payload,
        rules=ruleset,
        spent_this_week=spent_this_week,
    )

    # 6. Audit: rules_evaluated and llm_assessed
    record_audit_event(
        session=session,
        actor="rules",
        event="rules_evaluated",
        request_id=None,
        details=eval_res["rule_stage"],
    )

    record_audit_event(
        session=session,
        actor="llm",
        event="llm_assessed",
        request_id=None,
        details=eval_res["llm_stage"],
    )

    # 7. Map Decision to Initial Status
    decision = eval_res["decision"]
    if decision == "APPROVE":
        initial_status = "APPROVED"
    elif decision == "BLOCK":
        initial_status = "BLOCKED"
    else:
        initial_status = "ESCALATED"

    # 8. Persist PaymentRequest
    new_request_id = uuid.uuid4()
    now_utc = datetime.now(timezone.utc)

    db_request = PaymentRequest(
        id=new_request_id,
        created_at=now_utc,
        agent_id=payload.agent_id,
        amount=payload.amount,
        currency=payload.currency.upper(),
        merchant_name=payload.merchant_name,
        merchant_category=payload.merchant_category,
        payee_email=payload.payee_email,
        description=payload.description,
        task_context=payload.task_context,
        source_url=payload.source_url,
        page_excerpt=payload.page_excerpt,
        idempotency_key=payload.idempotency_key,
        decision=decision,
        status=initial_status,
        reasons=eval_res["reasons"],
        risk_score=eval_res["risk_score"],
        flags=eval_res["flags"],
        decided_by=eval_res["decided_by"],
        decided_at=now_utc,
        paypal_batch_id=None,
        paypal_status=None,
        paypal_approval_url=None,
        paypal_error=None,
    )
    session.add(db_request)
    session.commit()
    session.refresh(db_request)

    # 9. Audit: decision_made
    record_audit_event(
        session=session,
        actor=eval_res["decided_by"],
        event="decision_made",
        request_id=str(new_request_id),
        details={
            "decision": decision,
            "status": initial_status,
            "risk_score": eval_res["risk_score"],
            "flags": eval_res["flags"],
        },
    )

    # 10. Automatic PayPal Payment Execution on APPROVE
    if decision == "APPROVE":
        payout_res = await paypal.create_payout(db_request)
        if payout_res.get("success"):
            db_request.status = "PAID"
            db_request.paypal_batch_id = payout_res.get("batch_id")
            db_request.paypal_status = payout_res.get("status")
            db_request.paypal_approval_url = payout_res.get("approval_url")
            db_request.paypal_error = None
            session.add(db_request)
            session.commit()
            session.refresh(db_request)

            record_audit_event(
                session=session,
                actor="paypal",
                event="paypal_payout_created",
                request_id=str(db_request.id),
                details={
                    "batch_id": payout_res.get("batch_id"),
                    "status": payout_res.get("status"),
                    "mode": payout_res.get("mode"),
                },
            )
        else:
            db_request.status = "PAYMENT_FAILED"
            db_request.paypal_error = payout_res.get("error")
            db_request.paypal_status = "FAILED"
            session.add(db_request)
            session.commit()
            session.refresh(db_request)

            record_audit_event(
                session=session,
                actor="paypal",
                event="paypal_error",
                request_id=str(db_request.id),
                details={"error": payout_res.get("error")},
            )

    return PaymentRequestResponse(
        id=db_request.id,
        decision=db_request.decision,
        status=db_request.status,
        reasons=db_request.reasons,
        risk_score=db_request.risk_score,
        flags=db_request.flags,
        paypal_batch_id=db_request.paypal_batch_id,
        paypal_status=db_request.paypal_status,
        paypal_approval_url=db_request.paypal_approval_url,
        paypal_error=db_request.paypal_error,
    )


@router.get("/requests", response_model=List[PaymentRequest])
def list_requests(
    status: Optional[str] = Query(None, description="Filter by status"),
    session: Session = Depends(get_session),
):
    """List payment requests, newest first."""
    query = select(PaymentRequest).order_by(PaymentRequest.created_at.desc())
    if status:
        query = query.where(PaymentRequest.status == status.strip().upper())
    return session.exec(query).all()


@router.get("/requests/{id}", response_model=PaymentRequest)
def get_request(
    id: uuid.UUID,
    session: Session = Depends(get_session),
):
    """Retrieve details of a single payment request."""
    req = session.get(PaymentRequest, id)
    if not req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment request '{id}' not found",
        )
    return req


@router.post("/requests/{id}/approve", response_model=PaymentRequest)
async def approve_request(
    id: uuid.UUID,
    session: Session = Depends(get_session),
):
    """
    Human approval for an ESCALATED request.
    Transitions decision to APPROVED, records human action in the audit log,
    and executes PayPal payout.
    """
    req = session.get(PaymentRequest, id)
    if not req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment request '{id}' not found",
        )

    if req.status != "ESCALATED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve request with status '{req.status}'. Only ESCALATED requests can be approved.",
        )

    now_utc = datetime.now(timezone.utc)
    req.status = "APPROVED"
    req.decided_by = "human"
    req.decided_at = now_utc

    # Append human reason
    updated_reasons = list(req.reasons)
    updated_reasons.append({"stage": "human", "text": "Approved by human reviewer."})
    req.reasons = updated_reasons

    session.add(req)
    session.commit()
    session.refresh(req)

    record_audit_event(
        session=session,
        actor="human",
        event="human_approved",
        request_id=str(req.id),
        details={"previous_status": "ESCALATED", "new_status": "APPROVED"},
    )

    # Execute PayPal payout on approval
    payout_res = await paypal.create_payout(req)
    if payout_res.get("success"):
        req.status = "PAID"
        req.paypal_batch_id = payout_res.get("batch_id")
        req.paypal_status = payout_res.get("status")
        req.paypal_approval_url = payout_res.get("approval_url")
        req.paypal_error = None
        session.add(req)
        session.commit()
        session.refresh(req)

        record_audit_event(
            session=session,
            actor="paypal",
            event="paypal_payout_created",
            request_id=str(req.id),
            details={
                "batch_id": payout_res.get("batch_id"),
                "status": payout_res.get("status"),
                "mode": payout_res.get("mode"),
            },
        )
    else:
        req.status = "PAYMENT_FAILED"
        req.paypal_error = payout_res.get("error")
        req.paypal_status = "FAILED"
        session.add(req)
        session.commit()
        session.refresh(req)

        record_audit_event(
            session=session,
            actor="paypal",
            event="paypal_error",
            request_id=str(req.id),
            details={"error": payout_res.get("error")},
        )

    session.refresh(req)
    return req


@router.post("/requests/{id}/deny", response_model=PaymentRequest)
def deny_request(
    id: uuid.UUID,
    session: Session = Depends(get_session),
):
    """
    Human denial for an ESCALATED request.
    Transitions status to DENIED and records human action in the audit log.
    """
    req = session.get(PaymentRequest, id)
    if not req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment request '{id}' not found",
        )

    if req.status != "ESCALATED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot deny request with status '{req.status}'. Only ESCALATED requests can be denied.",
        )

    now_utc = datetime.now(timezone.utc)
    req.status = "DENIED"
    req.decided_by = "human"
    req.decided_at = now_utc

    # Append human reason
    updated_reasons = list(req.reasons)
    updated_reasons.append({"stage": "human", "text": "Denied by human reviewer."})
    req.reasons = updated_reasons

    session.add(req)
    session.commit()
    session.refresh(req)

    record_audit_event(
        session=session,
        actor="human",
        event="human_denied",
        request_id=str(req.id),
        details={"previous_status": "ESCALATED", "new_status": "DENIED"},
    )

    session.refresh(req)
    return req


@router.post("/requests/{id}/refresh-payment", response_model=PaymentRequest)
async def refresh_payment_status(
    id: uuid.UUID,
    session: Session = Depends(get_session),
):
    """
    Re-polls status from PayPal for an existing payout/order and updates paypal_status.
    """
    req = session.get(PaymentRequest, id)
    if not req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment request '{id}' not found",
        )

    if not req.paypal_batch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment request '{id}' has no associated PayPal batch or order ID to refresh.",
        )

    status_data = await paypal.get_payout_status(req.paypal_batch_id)
    new_paypal_status = (
        status_data.get("transaction_status")
        or status_data.get("batch_status")
        or "UNKNOWN"
    )
    prev_paypal_status = req.paypal_status

    req.paypal_status = new_paypal_status
    if req.status == "PAYMENT_FAILED" and new_paypal_status in ("SUCCESS", "COMPLETED"):
        req.status = "PAID"

    session.add(req)
    session.commit()
    session.refresh(req)

    record_audit_event(
        session=session,
        actor="paypal",
        event="paypal_status_refreshed",
        request_id=str(req.id),
        details={
            "batch_id": req.paypal_batch_id,
            "previous_paypal_status": prev_paypal_status,
            "new_paypal_status": new_paypal_status,
        },
    )

    session.refresh(req)
    return req


@router.get("/budget", response_model=BudgetResponse)
def get_budget(
    session: Session = Depends(get_session),
):
    """
    Calculates current rolling 7-day budget metrics:
    - weekly_budget
    - spent_this_week (sum of APPROVED / PAID transactions in past 7 days)
    - remaining
    - window_start
    """
    ruleset = ensure_ruleset_seeded(session)
    window_start = datetime.now(timezone.utc) - timedelta(days=7)

    matching_txs = session.exec(
        select(PaymentRequest).where(
            PaymentRequest.status.in_(["APPROVED", "PAID"]),
            PaymentRequest.created_at >= window_start,
            PaymentRequest.currency == ruleset.currency.upper(),
        )
    ).all()

    spent_this_week = sum(
        (tx.amount for tx in matching_txs),
        Decimal("0.00"),
    )
    remaining = max(Decimal("0.00"), ruleset.weekly_budget - spent_this_week)

    return BudgetResponse(
        weekly_budget=ruleset.weekly_budget,
        spent_this_week=spent_this_week,
        remaining=remaining,
        window_start=window_start,
    )
