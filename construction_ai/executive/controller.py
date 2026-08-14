from __future__ import annotations
from construction_ai.domain.models import DecisionAction, DecisionState, VerificationResult

MANDATORY_INVOICE_CHECKS = {"vendor_match", "project_match", "po_match", "quote_match", "amount_match", "tax_math", "not_duplicate", "work_confirmed"}


def next_invoice_action(state: DecisionState, verification: VerificationResult | None = None) -> DecisionAction:
    if state.step_count >= state.max_steps:
        return DecisionAction.ESCALATE
    if verification is None:
        return DecisionAction.RETRIEVE
    missing = MANDATORY_INVOICE_CHECKS - set(verification.checks)
    if missing:
        return DecisionAction.RETRIEVE
    if verification.exceptions:
        return DecisionAction.ASK_HUMAN
    if not verification.passed:
        return DecisionAction.VERIFY
    return DecisionAction.ASK_HUMAN
