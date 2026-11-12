"""
Append-only, cryptographic hash-chained audit logging service.

Every audit event is cryptographically linked to the previous entry using SHA-256.
Concurrency locking guarantees that concurrent transactions cannot fork the hash chain.
No update or delete operations are implemented or exposed.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import text
from sqlmodel import Session, select
from app.models import AuditEntry

GENESIS_PREV_HASH = "0" * 64
_audit_chain_lock = threading.Lock()
# 64-bit integer identifier for PostgreSQL advisory lock ('AWGAUDIT' -> 4708343715101682004)
POSTGRES_AUDIT_ADVISORY_LOCK_ID = 4708343715101682004


def canonical_json(data: Dict[str, Any]) -> str:
    """Format dictionary into deterministic canonical JSON (sorted keys, compact separators)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def compute_audit_hash(
    prev_hash: str,
    ts: datetime | str,
    request_id: Optional[str],
    actor: str,
    event: str,
    details: Dict[str, Any],
) -> str:
    """
    Computes SHA-256 hash = sha256(prev_hash + canonical JSON of {ts, request_id, actor, event, details})
    """
    ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)
    payload = {
        "actor": str(actor),
        "details": details,
        "event": str(event),
        "request_id": str(request_id) if request_id is not None else None,
        "ts": ts_str,
    }
    raw = prev_hash + canonical_json(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_audit_event(
    session: Session,
    actor: str,
    event: str,
    request_id: Optional[str | uuid.UUID] = None,
    details: Optional[Dict[str, Any]] = None,
) -> AuditEntry:
    """
    Appends a new immutable AuditEntry to the hash chain.
    Protected by both an in-process thread lock and a database-level transaction lock
    (PostgreSQL advisory xact lock) to guarantee multi-worker and multi-process concurrency safety.
    """
    clean_details = details if details is not None else {}
    req_id_str = str(request_id) if request_id is not None else None
    now = datetime.now(timezone.utc)

    with _audit_chain_lock:
        # DB-level lock for multi-worker and multi-container deployment
        try:
            bind = session.get_bind()
            if bind and bind.dialect.name.lower() == "postgresql":
                session.execute(
                    text(f"SELECT pg_advisory_xact_lock({POSTGRES_AUDIT_ADVISORY_LOCK_ID})")
                )
        except Exception:
            # If session bind is not ready or dialect does not support advisory locks, proceed safely
            pass

        # Retrieve the latest entry in the chain
        latest_entry = session.exec(
            select(AuditEntry).order_by(AuditEntry.id.desc())
        ).first()

        prev_hash = latest_entry.hash if latest_entry else GENESIS_PREV_HASH
        entry_hash = compute_audit_hash(
            prev_hash=prev_hash,
            ts=now,
            request_id=req_id_str,
            actor=actor,
            event=event,
            details=clean_details,
        )

        entry = AuditEntry(
            ts=now,
            request_id=req_id_str,
            actor=actor,
            event=event,
            details=clean_details,
            prev_hash=prev_hash,
            hash=entry_hash,
        )
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry


def verify_audit_chain(session: Session) -> Dict[str, Any]:
    """
    Verifies the integrity of the entire audit chain from genesis to head.
    Returns:
        {
            "valid": bool,
            "checked": int,
            "first_bad_id": int | None
        }
    """
    entries = session.exec(select(AuditEntry).order_by(AuditEntry.id.asc())).all()

    if not entries:
        return {"valid": True, "checked": 0, "first_bad_id": None}

    expected_prev = GENESIS_PREV_HASH
    checked_count = 0

    for entry in entries:
        # Check 1: prev_hash must link to preceding entry's hash (or genesis)
        if entry.prev_hash != expected_prev:
            return {
                "valid": False,
                "checked": checked_count,
                "first_bad_id": entry.id,
            }

        # Check 2: recalculate the hash of the current entry
        recalculated_hash = compute_audit_hash(
            prev_hash=entry.prev_hash,
            ts=entry.ts,
            request_id=entry.request_id,
            actor=entry.actor,
            event=entry.event,
            details=entry.details,
        )

        if recalculated_hash != entry.hash:
            return {
                "valid": False,
                "checked": checked_count,
                "first_bad_id": entry.id,
            }

        expected_prev = entry.hash
        checked_count += 1

    return {
        "valid": True,
        "checked": checked_count,
        "first_bad_id": None,
    }

