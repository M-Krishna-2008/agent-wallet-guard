"""
Decision Engine for Agent Wallet Guard.

This is a pure, side-effect-free evaluation function with NO direct database access.
It takes request attributes, current ruleset parameters, and rolling budget spend,
evaluates the deterministic rule pipeline, calls the LLM risk assessor, and enforces
that the LLM can only tighten decisions, never loosen them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from app import llm as default_llm
from app.models import RuleSet

SEVERITY_HIERARCHY: Dict[str, int] = {
    "APPROVE": 0,
    "ESCALATE": 1,
    "BLOCK": 2,
}


def _extract_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def evaluate(
    request: Any,
    rules: RuleSet,
    spent_this_week: Decimal,
    llm_assessor: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Pure evaluation function.

    Rule Evaluation:
    1. BLOCK if:
       - amount <= 0
       - amount > hard_block_limit
       - category in blocked_categories
       - currency mismatch
       - payee not in allowed_payee_emails and amount > unknown_payee_limit
    2. ESCALATE if:
       - amount > per_txn_auto_limit
       - spent_this_week + amount > weekly_budget
       - category not in allowed_categories
    3. Otherwise APPROVE.

    Then calls llm.assess(request, rule_result).
    The LLM result may ONLY tighten (APPROVE -> ESCALATE/BLOCK, ESCALATE -> BLOCK).
    """
    # Normalize inputs
    raw_amount = _extract_field(request, "amount", Decimal("0.00"))
    amount = Decimal(str(raw_amount))

    raw_currency = _extract_field(request, "currency", "USD")
    currency = str(raw_currency).strip().upper()
    rule_currency = str(rules.currency).strip().upper()

    raw_category = _extract_field(request, "merchant_category", "")
    category = str(raw_category).strip().lower()

    raw_payee = _extract_field(request, "payee_email", "")
    payee_email = str(raw_payee).strip().lower()

    blocked_categories = [c.strip().lower() for c in rules.blocked_categories]
    allowed_categories = [c.strip().lower() for c in rules.allowed_categories]
    allowed_payees = [e.strip().lower() for e in rules.allowed_payee_emails]

    block_reasons: List[str] = []
    block_flags: List[str] = []
    block_risks: List[int] = []

    # -------------------------------------------------------------------------
    # 1. Deterministic BLOCK Rules
    # -------------------------------------------------------------------------
    if amount <= Decimal("0.00"):
        block_reasons.append(f"Amount ${amount:.2f} must be strictly greater than zero.")
        block_flags.append("invalid_amount")
        block_risks.append(100)

    if amount > rules.hard_block_limit:
        block_reasons.append(
            f"Amount ${amount:.2f} exceeds hard block limit of ${rules.hard_block_limit:.2f}."
        )
        block_flags.append("exceeds_hard_block_limit")
        block_risks.append(90)

    if category in blocked_categories:
        block_reasons.append(
            f"Merchant category '{category}' is in the blocked categories list."
        )
        block_flags.append("blocked_category")
        block_risks.append(95)

    if currency != rule_currency:
        block_reasons.append(
            f"Currency '{currency}' does not match policy currency '{rule_currency}'."
        )
        block_flags.append("currency_mismatch")
        block_risks.append(85)

    if payee_email not in allowed_payees and amount > rules.unknown_payee_limit:
        block_reasons.append(
            f"Payee '{payee_email}' is not in allowed payees list and amount ${amount:.2f} "
            f"exceeds unknown payee limit of ${rules.unknown_payee_limit:.2f}."
        )
        block_flags.append("unknown_payee_limit_exceeded")
        block_risks.append(80)

    # -------------------------------------------------------------------------
    # 2. Decision Logic for Rules Stage
    # -------------------------------------------------------------------------
    if block_reasons:
        rule_decision = "BLOCK"
        rule_risk = max(block_risks) if block_risks else 90
        rule_flags = block_flags
        rule_reasons = [{"stage": "rules", "text": text} for text in block_reasons]
    else:
        escalate_reasons: List[str] = []
        escalate_flags: List[str] = []
        escalate_risks: List[int] = []

        if amount > rules.per_txn_auto_limit:
            escalate_reasons.append(
                f"Amount ${amount:.2f} is above your ${rules.per_txn_auto_limit:.2f} auto-approve limit."
            )
            escalate_flags.append("exceeds_auto_limit")
            escalate_risks.append(50)

        if spent_this_week + amount > rules.weekly_budget:
            remaining = max(Decimal("0.00"), rules.weekly_budget - spent_this_week)
            escalate_reasons.append(
                f"Transaction of ${amount:.2f} exceeds remaining weekly budget of ${remaining:.2f} "
                f"(spent ${spent_this_week:.2f} of ${rules.weekly_budget:.2f})."
            )
            escalate_flags.append("exceeds_weekly_budget")
            escalate_risks.append(60)

        if category not in allowed_categories:
            escalate_reasons.append(
                f"Merchant category '{category}' is not in allowed categories list."
            )
            escalate_flags.append("unallowed_category")
            escalate_risks.append(40)

        if escalate_reasons:
            rule_decision = "ESCALATE"
            rule_risk = max(escalate_risks) if escalate_risks else 50
            rule_flags = escalate_flags
            rule_reasons = [{"stage": "rules", "text": text} for text in escalate_reasons]
        else:
            rule_decision = "APPROVE"
            rule_risk = 0
            rule_flags = []
            rule_reasons = [
                {
                    "stage": "rules",
                    "text": "Transaction satisfies all automated policy guardrails.",
                }
            ]

    rule_summary = {
        "decision": rule_decision,
        "reasons": rule_reasons,
        "risk_score": rule_risk,
        "flags": rule_flags,
    }

    # -------------------------------------------------------------------------
    # 3. LLM Risk Assessment (Can only tighten, never loosen)
    # -------------------------------------------------------------------------
    assessor = llm_assessor or default_llm.assess
    req_dict = {
        "amount": str(amount),
        "currency": currency,
        "merchant_name": _extract_field(request, "merchant_name", ""),
        "merchant_category": category,
        "payee_email": payee_email,
        "description": _extract_field(request, "description", ""),
        "task_context": _extract_field(request, "task_context", ""),
        "source_url": _extract_field(request, "source_url", None),
        "page_excerpt": _extract_field(request, "page_excerpt", None),
    }

    llm_result = assessor(req_dict, rule_summary)
    llm_decision = str(llm_result.get("decision", "APPROVE")).strip().upper()
    llm_reason_text = str(llm_result.get("reason", "")).strip()
    llm_risk = int(llm_result.get("risk_score", 0))
    llm_flags = list(llm_result.get("flags", []))

    rule_sev = SEVERITY_HIERARCHY.get(rule_decision, 0)
    llm_sev = SEVERITY_HIERARCHY.get(llm_decision, 0)

    # Enforce strict monotonicity: LLM can ONLY make it stricter
    if llm_sev > rule_sev:
        final_decision = llm_decision
        decided_by = "llm"
    else:
        final_decision = rule_decision
        decided_by = "rules"

    # Merge reasons
    merged_reasons = list(rule_reasons)
    if llm_reason_text:
        merged_reasons.append({"stage": "llm", "text": llm_reason_text})

    # Combine risk score and flags
    final_risk = max(rule_risk, llm_risk)
    final_flags = list(dict.fromkeys(rule_flags + llm_flags))

    return {
        "decision": final_decision,
        "decided_by": decided_by,
        "reasons": merged_reasons,
        "risk_score": final_risk,
        "flags": final_flags,
        "rule_stage": rule_summary,
        "llm_stage": {
            "decision": llm_decision,
            "reason": llm_reason_text,
            "risk_score": llm_risk,
            "flags": llm_flags,
        },
    }

