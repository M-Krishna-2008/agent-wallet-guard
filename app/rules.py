"""
Default ruleset seeding and rule repository functions.
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from sqlmodel import Session, select
from app.config import settings
from app.models import RuleSet


def get_default_ruleset() -> RuleSet:
    """Return a default RuleSet initialized with system configuration defaults."""
    allowed_emails = []
    if settings.VENDOR_SANDBOX_EMAIL and settings.VENDOR_SANDBOX_EMAIL.strip():
        allowed_emails.append(settings.VENDOR_SANDBOX_EMAIL.strip())

    return RuleSet(
        id=1,
        per_txn_auto_limit=Decimal("50.00"),
        hard_block_limit=Decimal("300.00"),
        weekly_budget=Decimal("200.00"),
        unknown_payee_limit=Decimal("0.00"),
        allowed_categories=[
            "groceries",
            "electronics",
            "books",
            "software",
            "office",
        ],
        blocked_categories=[
            "gift_cards",
            "crypto",
            "gambling",
            "adult",
        ],
        allowed_payee_emails=allowed_emails,
        currency="USD",
        updated_at=datetime.now(timezone.utc),
    )


def ensure_ruleset_seeded(session: Session) -> RuleSet:
    """Ensure that the single RuleSet row (id=1) exists in the database."""
    ruleset = session.exec(select(RuleSet).where(RuleSet.id == 1)).first()
    if not ruleset:
        ruleset = get_default_ruleset()
        session.add(ruleset)
        session.commit()
        session.refresh(ruleset)
    return ruleset

