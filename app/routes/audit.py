"""
Audit log query and cryptographic hash-chain verification routes.
"""

from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select
from app.audit import verify_audit_chain
from app.db import get_session
from app.models import AuditEntry, AuditVerifyResponse

router = APIRouter(tags=["Audit"])


@router.get("/audit", response_model=List[AuditEntry])
def list_audit_entries(
    request_id: Optional[str] = Query(None, description="Filter by payment request ID"),
    session: Session = Depends(get_session),
):
    """
    List immutable audit log records in reverse chronological order (newest first).
    Optionally filters by request_id.
    """
    query = select(AuditEntry).order_by(AuditEntry.id.desc())
    if request_id:
        query = query.where(AuditEntry.request_id == request_id.strip())
    return session.exec(query).all()


@router.get("/audit/verify", response_model=AuditVerifyResponse)
def verify_audit_log(
    session: Session = Depends(get_session),
):
    """
    Cryptographically verifies the SHA-256 hash chain from genesis to the latest record.
    Returns whether the chain is unbroken and valid, the number of records checked,
    and the ID of the first tampered entry (if any).
    """
    return verify_audit_chain(session)

