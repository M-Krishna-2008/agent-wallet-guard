"""
Unit tests for app.engine (pure function evaluation).
"""

from decimal import Decimal
import pytest
from app.engine import evaluate
from app.models import RuleSet


@pytest.fixture(autouse=True)
def setup_mock_llm():
    from app.llm import MockLLMClient, set_llm_client
    set_llm_client(MockLLMClient())
    yield
    set_llm_client(None)


@pytest.fixture
def base_ruleset() -> RuleSet:
    return RuleSet(
        id=1,
        per_txn_auto_limit=Decimal("50.00"),
        hard_block_limit=Decimal("300.00"),
        weekly_budget=Decimal("200.00"),
        unknown_payee_limit=Decimal("0.00"),
        allowed_categories=["groceries", "electronics", "books", "software", "office"],
        blocked_categories=["gift_cards", "crypto", "gambling", "adult"],
        allowed_payee_emails=["vendor@approved.com"],
        currency="USD",
    )


def test_rule_clean_approve(base_ruleset):
    request = {
        "amount": "25.00",
        "currency": "USD",
        "merchant_name": "Office Supplies Co",
        "merchant_category": "office",
        "payee_email": "vendor@approved.com",
        "description": "Notebooks and pens",
        "task_context": "Refill stationary",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("10.00"))
    assert result["decision"] == "APPROVE"
    assert result["decided_by"] == "rules"
    assert result["risk_score"] == 0
    assert len(result["flags"]) == 0
    assert any("satisfies all automated policy guardrails" in r["text"] for r in result["reasons"])


def test_rule_block_zero_or_negative_amount(base_ruleset):
    for bad_amt in ["0.00", "-15.00"]:
        request = {
            "amount": bad_amt,
            "currency": "USD",
            "merchant_category": "office",
            "payee_email": "vendor@approved.com",
        }
        result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"))
        assert result["decision"] == "BLOCK"
        assert "invalid_amount" in result["flags"]
        assert result["risk_score"] == 100


def test_rule_block_exceeds_hard_block_limit(base_ruleset):
    request = {
        "amount": "301.00",
        "currency": "USD",
        "merchant_category": "electronics",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"))
    assert result["decision"] == "BLOCK"
    assert "exceeds_hard_block_limit" in result["flags"]
    assert any("exceeds hard block limit" in r["text"] for r in result["reasons"])


def test_rule_block_blocked_category(base_ruleset):
    for cat in ["crypto", "gambling", "adult", "gift_cards"]:
        request = {
            "amount": "20.00",
            "currency": "USD",
            "merchant_category": cat,
            "payee_email": "vendor@approved.com",
        }
        result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"))
        assert result["decision"] == "BLOCK"
        assert "blocked_category" in result["flags"]


def test_rule_block_currency_mismatch(base_ruleset):
    request = {
        "amount": "20.00",
        "currency": "EUR",
        "merchant_category": "software",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"))
    assert result["decision"] == "BLOCK"
    assert "currency_mismatch" in result["flags"]


def test_rule_block_unknown_payee_exceeding_limit(base_ruleset):
    request = {
        "amount": "10.00",
        "currency": "USD",
        "merchant_category": "software",
        "payee_email": "unknown-entity@stranger.org",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"))
    assert result["decision"] == "BLOCK"
    assert "unknown_payee_limit_exceeded" in result["flags"]


def test_rule_escalate_over_auto_limit(base_ruleset):
    request = {
        "amount": "150.00",
        "currency": "USD",
        "merchant_category": "software",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"))
    assert result["decision"] == "ESCALATE"
    assert "exceeds_auto_limit" in result["flags"]
    assert any("above your $50.00 auto-approve limit" in r["text"] for r in result["reasons"])


def test_rule_escalate_over_weekly_budget(base_ruleset):
    request = {
        "amount": "40.00",
        "currency": "USD",
        "merchant_category": "software",
        "payee_email": "vendor@approved.com",
    }
    # spent 180 + 40 = 220 > 200
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("180.00"))
    assert result["decision"] == "ESCALATE"
    assert "exceeds_weekly_budget" in result["flags"]


def test_rule_escalate_unallowed_category(base_ruleset):
    request = {
        "amount": "25.00",
        "currency": "USD",
        "merchant_category": "travel",  # travel is not in allowed or blocked
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"))
    assert result["decision"] == "ESCALATE"
    assert "unallowed_category" in result["flags"]


def test_llm_cannot_loosen_block(base_ruleset):
    """Stub LLM returning APPROVE cannot loosen a BLOCK."""
    def rogue_llm(req, summary):
        return {"decision": "APPROVE", "reason": "Rogue LLM says its fine", "risk_score": 0, "flags": []}

    request = {
        "amount": "350.00",
        "currency": "USD",
        "merchant_category": "crypto",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"), llm_assessor=rogue_llm)
    assert result["decision"] == "BLOCK"
    assert result["decided_by"] == "rules"


def test_llm_cannot_loosen_escalate(base_ruleset):
    """Stub LLM returning APPROVE cannot loosen an ESCALATE."""
    def rogue_llm(req, summary):
        return {"decision": "APPROVE", "reason": "Bypassing guardrail", "risk_score": 0, "flags": []}

    request = {
        "amount": "120.00",  # over 50 auto limit
        "currency": "USD",
        "merchant_category": "software",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"), llm_assessor=rogue_llm)
    assert result["decision"] == "ESCALATE"
    assert result["decided_by"] == "rules"


def test_llm_can_tighten_approve_to_escalate(base_ruleset):
    """LLM can tighten APPROVE to ESCALATE."""
    def tightening_llm(req, summary):
        return {
            "decision": "ESCALATE",
            "reason": "Anomalous purchasing pattern detected in context",
            "risk_score": 65,
            "flags": ["anomalous_context"],
        }

    request = {
        "amount": "20.00",
        "currency": "USD",
        "merchant_category": "software",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"), llm_assessor=tightening_llm)
    assert result["decision"] == "ESCALATE"
    assert result["decided_by"] == "llm"
    assert result["risk_score"] == 65
    assert "anomalous_context" in result["flags"]
    assert any(r["stage"] == "llm" for r in result["reasons"])


def test_llm_can_tighten_approve_to_block(base_ruleset):
    """LLM can tighten APPROVE directly to BLOCK."""
    def blocking_llm(req, summary):
        return {
            "decision": "BLOCK",
            "reason": "Prompt injection detected in task context",
            "risk_score": 99,
            "flags": ["prompt_injection_detected"],
        }

    request = {
        "amount": "20.00",
        "currency": "USD",
        "merchant_category": "software",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"), llm_assessor=blocking_llm)
    assert result["decision"] == "BLOCK"
    assert result["decided_by"] == "llm"
    assert result["risk_score"] == 99
    assert "prompt_injection_detected" in result["flags"]


def test_llm_can_tighten_escalate_to_block(base_ruleset):
    """LLM can tighten ESCALATE to BLOCK."""
    def blocking_llm(req, summary):
        return {
            "decision": "BLOCK",
            "reason": "Suspicious merchant domain detected",
            "risk_score": 90,
            "flags": ["suspicious_domain"],
        }

    request = {
        "amount": "75.00",  # over 50 auto limit -> ESCALATE in rules
        "currency": "USD",
        "merchant_category": "software",
        "payee_email": "vendor@approved.com",
    }
    result = evaluate(request, base_ruleset, spent_this_week=Decimal("0.00"), llm_assessor=blocking_llm)
    assert result["decision"] == "BLOCK"
    assert result["decided_by"] == "llm"
    assert result["risk_score"] == 90
    assert "suspicious_domain" in result["flags"]
    assert "exceeds_auto_limit" in result["flags"]

