from dataclasses import dataclass

@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_human: bool
    reason: str

FINANCIAL_ACTIONS = {"PAY", "RUN_PAYROLL", "APPROVE_INVOICE", "SUBMIT_ERP_TRANSACTION"}
V1_ALLOWED = {"READ", "CLASSIFY", "EXTRACT", "LINK", "SEARCH", "COMPARE", "CALCULATE", "FLAG", "DRAFT", "CREATE_INTERNAL_TASK", "PREPARE_TRANSACTION", "REQUEST_HUMAN_APPROVAL"}


def decide(action: str, *, evidence_valid: bool, confidence_satisfied: bool, approval_satisfied: bool = False) -> PolicyDecision:
    if not evidence_valid:
        return PolicyDecision(False, False, "evidence_invalid")
    if not confidence_satisfied:
        return PolicyDecision(False, True, "confidence_below_threshold")
    if action in FINANCIAL_ACTIONS:
        if not approval_satisfied:
            return PolicyDecision(False, True, "human_approval_required")
        return PolicyDecision(True, False, "approved_financial_action")
    if action in V1_ALLOWED:
        return PolicyDecision(True, False, "v1_allowed")
    return PolicyDecision(False, True, "action_not_authorized")
