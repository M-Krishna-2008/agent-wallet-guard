"""
Spending policy and ruleset management routes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session
from app.audit import record_audit_event
from app.db import get_session
from app.models import RuleSet, RuleSetUpdate
from app.rules import ensure_ruleset_seeded

router = APIRouter(tags=["Rules"])


@router.get("/rules", response_model=RuleSet)
def get_rules(session: Session = Depends(get_session)):
    """Retrieve the current spending rules and guardrail policies."""
    return ensure_ruleset_seeded(session)


@router.put("/rules", response_model=RuleSet)
def update_rules(
    payload: RuleSetUpdate,
    session: Session = Depends(get_session),
):
    """
    Update ruleset limits, categories, and allowed payees.
    Validates types and ranges, then logs rules_updated to the audit hash chain.
    """
    ruleset = ensure_ruleset_seeded(session)
    changes = {}

    if payload.per_txn_auto_limit is not None:
        if payload.per_txn_auto_limit < Decimal("0.00"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="per_txn_auto_limit must be greater than or equal to 0.00",
            )
        changes["per_txn_auto_limit"] = {
            "from": str(ruleset.per_txn_auto_limit),
            "to": str(payload.per_txn_auto_limit),
        }
        ruleset.per_txn_auto_limit = payload.per_txn_auto_limit

    if payload.hard_block_limit is not None:
        if payload.hard_block_limit < Decimal("0.00"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="hard_block_limit must be greater than or equal to 0.00",
            )
        changes["hard_block_limit"] = {
            "from": str(ruleset.hard_block_limit),
            "to": str(payload.hard_block_limit),
        }
        ruleset.hard_block_limit = payload.hard_block_limit

    if payload.weekly_budget is not None:
        if payload.weekly_budget < Decimal("0.00"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="weekly_budget must be greater than or equal to 0.00",
            )
        changes["weekly_budget"] = {
            "from": str(ruleset.weekly_budget),
            "to": str(payload.weekly_budget),
        }
        ruleset.weekly_budget = payload.weekly_budget

    if payload.unknown_payee_limit is not None:
        if payload.unknown_payee_limit < Decimal("0.00"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="unknown_payee_limit must be greater than or equal to 0.00",
            )
        changes["unknown_payee_limit"] = {
            "from": str(ruleset.unknown_payee_limit),
            "to": str(payload.unknown_payee_limit),
        }
        ruleset.unknown_payee_limit = payload.unknown_payee_limit

    if payload.allowed_categories is not None:
        changes["allowed_categories"] = {
            "from": ruleset.allowed_categories,
            "to": payload.allowed_categories,
        }
        ruleset.allowed_categories = payload.allowed_categories

    if payload.blocked_categories is not None:
        changes["blocked_categories"] = {
            "from": ruleset.blocked_categories,
            "to": payload.blocked_categories,
        }
        ruleset.blocked_categories = payload.blocked_categories

    if payload.allowed_payee_emails is not None:
        changes["allowed_payee_emails"] = {
            "from": ruleset.allowed_payee_emails,
            "to": payload.allowed_payee_emails,
        }
        ruleset.allowed_payee_emails = payload.allowed_payee_emails

    if payload.currency is not None:
        curr = payload.currency.strip().upper()
        if len(curr) != 3:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="currency must be a valid 3-letter ISO code",
            )
        changes["currency"] = {"from": ruleset.currency, "to": curr}
        ruleset.currency = curr

    ruleset.updated_at = datetime.now(timezone.utc)
    session.add(ruleset)
    session.commit()
    session.refresh(ruleset)

    record_audit_event(
        session=session,
        actor="system",
        event="rules_updated",
        request_id=None,
        details={"changes": changes},
    )

    session.refresh(ruleset)
    return ruleset
