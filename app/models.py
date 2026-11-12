"""
SQLModel database models and API schemas for Agent Wallet Guard.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


# ==============================================================================
# Database Models
# ==============================================================================


class PaymentRequest(SQLModel, table=True):
    __tablename__ = "payment_requests"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
        nullable=False,
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    agent_id: str = Field(index=True, nullable=False)
    amount: Decimal = Field(
        default=Decimal("0.00"),
        max_digits=12,
        decimal_places=2,
        nullable=False,
    )
    currency: str = Field(default="USD", max_length=10, nullable=False)
    merchant_name: str = Field(nullable=False)
    merchant_category: str = Field(index=True, nullable=False)
    payee_email: str = Field(index=True, nullable=False)
    description: str = Field(nullable=False)
    task_context: str = Field(nullable=False)
    source_url: Optional[str] = Field(default=None, nullable=True)
    page_excerpt: Optional[str] = Field(default=None, nullable=True)

    idempotency_key: str = Field(
        unique=True,
        index=True,
        nullable=False,
    )

    # Decisions: APPROVE | BLOCK | ESCALATE
    decision: str = Field(nullable=False)
    # Statuses: PENDING | APPROVED | BLOCKED | ESCALATED | DENIED | PAID
    status: str = Field(index=True, nullable=False)

    reasons: List[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, default=[]),
    )
    risk_score: int = Field(default=0, ge=0, le=100, nullable=False)
    flags: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, default=[]),
    )

    # Actor who made the decision: rules | llm | human
    decided_by: str = Field(default="rules", nullable=False)
    decided_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    paypal_batch_id: Optional[str] = Field(default=None, nullable=True)
    paypal_status: Optional[str] = Field(default=None, nullable=True)
    paypal_approval_url: Optional[str] = Field(default=None, nullable=True)
    paypal_error: Optional[str] = Field(default=None, nullable=True)


class RuleSet(SQLModel, table=True):
    __tablename__ = "rulesets"

    id: int = Field(default=1, primary_key=True)
    per_txn_auto_limit: Decimal = Field(
        default=Decimal("50.00"),
        max_digits=12,
        decimal_places=2,
        nullable=False,
    )
    hard_block_limit: Decimal = Field(
        default=Decimal("300.00"),
        max_digits=12,
        decimal_places=2,
        nullable=False,
    )
    weekly_budget: Decimal = Field(
        default=Decimal("200.00"),
        max_digits=12,
        decimal_places=2,
        nullable=False,
    )
    unknown_payee_limit: Decimal = Field(
        default=Decimal("0.00"),
        max_digits=12,
        decimal_places=2,
        nullable=False,
    )
    allowed_categories: List[str] = Field(
        default_factory=lambda: [
            "groceries",
            "electronics",
            "books",
            "software",
            "office",
        ],
        sa_column=Column(JSON, nullable=False),
    )
    blocked_categories: List[str] = Field(
        default_factory=lambda: ["gift_cards", "crypto", "gambling", "adult"],
        sa_column=Column(JSON, nullable=False),
    )
    allowed_payee_emails: List[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    currency: str = Field(default="USD", max_length=10, nullable=False)
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class AuditEntry(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        index=True,
        nullable=False,
    )
    request_id: Optional[str] = Field(default=None, index=True, nullable=True)
    # actor: agent | rules | llm | human | paypal | system
    actor: str = Field(nullable=False)
    # event: request_received | rules_evaluated | llm_assessed | decision_made | human_approved | human_denied | rules_updated
    event: str = Field(index=True, nullable=False)
    details: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    prev_hash: str = Field(nullable=False)
    hash: str = Field(nullable=False, index=True)


# ==============================================================================
# API Request / Response Schemas
# ==============================================================================


class PaymentRequestCreate(BaseModel):
    agent_id: str
    amount: Decimal
    currency: str = "USD"
    merchant_name: str
    merchant_category: str
    payee_email: str
    description: str
    task_context: str
    source_url: Optional[str] = None
    page_excerpt: Optional[str] = None
    idempotency_key: str


class ReasonItem(BaseModel):
    stage: str
    text: str


class PaymentRequestResponse(BaseModel):
    id: uuid.UUID
    decision: str
    status: str
    reasons: List[dict[str, Any]]
    risk_score: int
    flags: List[str]
    paypal_batch_id: Optional[str] = None
    paypal_status: Optional[str] = None
    paypal_approval_url: Optional[str] = None
    paypal_error: Optional[str] = None


class RuleSetUpdate(BaseModel):
    per_txn_auto_limit: Optional[Decimal] = None
    hard_block_limit: Optional[Decimal] = None
    weekly_budget: Optional[Decimal] = None
    unknown_payee_limit: Optional[Decimal] = None
    allowed_categories: Optional[List[str]] = None
    blocked_categories: Optional[List[str]] = None
    allowed_payee_emails: Optional[List[str]] = None
    currency: Optional[str] = None


class BudgetResponse(BaseModel):
    weekly_budget: Decimal
    spent_this_week: Decimal
    remaining: Decimal
    window_start: datetime


class AuditVerifyResponse(BaseModel):
    valid: bool
    checked: int
    first_bad_id: Optional[int] = None

