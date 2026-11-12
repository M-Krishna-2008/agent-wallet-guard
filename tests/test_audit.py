"""
Unit tests for cryptographic audit hash-chaining and verification (app.audit).
"""

import uuid
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
import pytest
from app.audit import record_audit_event, verify_audit_chain
from app.models import AuditEntry


@pytest.fixture
def db_session():
    """In-memory SQLite database session fixture."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        yield session


def test_empty_audit_chain_is_valid(db_session):
    res = verify_audit_chain(db_session)
    assert res["valid"] is True
    assert res["checked"] == 0
    assert res["first_bad_id"] is None


def test_audit_chain_recording_and_verification(db_session):
    req_id = uuid.uuid4()
    
    # Record multiple events
    e1 = record_audit_event(db_session, actor="agent", event="request_received", request_id=req_id, details={"amount": "25.00"})
    e2 = record_audit_event(db_session, actor="rules", event="rules_evaluated", request_id=req_id, details={"decision": "APPROVE"})
    e3 = record_audit_event(db_session, actor="llm", event="llm_assessed", request_id=req_id, details={"risk_score": 0})
    e4 = record_audit_event(db_session, actor="rules", event="decision_made", request_id=req_id, details={"status": "APPROVED"})
    
    # Verify unbroken chain
    res = verify_audit_chain(db_session)
    assert res["valid"] is True
    assert res["checked"] == 4
    assert res["first_bad_id"] is None
    
    # Check link integrity
    assert e1.prev_hash == "0" * 64
    assert e2.prev_hash == e1.hash
    assert e3.prev_hash == e2.hash
    assert e4.prev_hash == e3.hash


def test_audit_chain_fails_on_tampering_details(db_session):
    record_audit_event(db_session, actor="agent", event="request_received", details={"amount": "10.00"})
    e2 = record_audit_event(db_session, actor="rules", event="decision_made", details={"status": "APPROVED"})
    record_audit_event(db_session, actor="system", event="rules_updated", details={"change": "none"})
    
    # Chain is initially valid
    assert verify_audit_chain(db_session)["valid"] is True
    
    # Tamper with row 2's details without updating its hash
    tampered_row = db_session.get(AuditEntry, e2.id)
    tampered_row.details = {"status": "TAMPERED_INJECTED_STATUS"}
    db_session.add(tampered_row)
    db_session.commit()
    
    # Verify must fail and flag row 2
    res = verify_audit_chain(db_session)
    assert res["valid"] is False
    assert res["first_bad_id"] == e2.id


def test_audit_chain_fails_on_tampering_hash(db_session):
    e1 = record_audit_event(db_session, actor="agent", event="request_received", details={"amount": "10.00"})
    record_audit_event(db_session, actor="rules", event="decision_made", details={"status": "APPROVED"})
    
    # Tamper with row 1's hash directly
    tampered_row = db_session.get(AuditEntry, e1.id)
    tampered_row.hash = "f" * 64
    db_session.add(tampered_row)
    db_session.commit()
    
    res = verify_audit_chain(db_session)
    assert res["valid"] is False
    assert res["first_bad_id"] == e1.id
