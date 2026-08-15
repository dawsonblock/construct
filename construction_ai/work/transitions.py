"""rc9: Formal state-machine validation for work confirmations.

Legal transitions:
    CONFIRMED -> SUPERSEDED
    CONFIRMED -> REVOKED
    CONFIRMED -> RETRACTED

Illegal transitions (no dedicated correction flow exists):
    REVOKED   -> SUPERSEDED
    RETRACTED -> SUPERSEDED
    SUPERSEDED-> SUPERSEDED
    REVOKED   -> REVOKED
    RETRACTED-> RETRACTED
    SUPERSEDED-> REVOKED
    SUPERSEDED-> RETRACTED
    REVOKED   -> RETRACTED
    RETRACTED-> REVOKED

This validator is used by the repository so that transition rules are
encoded in one place, not scattered across callers.
"""
from __future__ import annotations

# Legal status values.
CONFIRMED = "confirmed"
SUPERSEDED = "superseded"
REVOKED = "revoked"
RETRACTED = "retracted"

# Legal transitions: (from_status, to_status).
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    (CONFIRMED, SUPERSEDED),
    (CONFIRMED, REVOKED),
    (CONFIRMED, RETRACTED),
})


class InvalidTransitionError(ValueError):
    """Raised when a work-confirmation status transition is illegal."""

    def __init__(self, current_status: str, target_status: str):
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(
            f"invalid work-confirmation transition: "
            f"{current_status} -> {target_status}. "
            f"Legal transitions from {current_status!r}: "
            f"{[t for (f, t) in LEGAL_TRANSITIONS if f == current_status]}"
        )


def validate_transition(current_status: str, target_status: str) -> None:
    """Validate that a status transition is legal.

    Raises:
        InvalidTransitionError: if the transition is not in LEGAL_TRANSITIONS.
    """
    if (current_status, target_status) not in LEGAL_TRANSITIONS:
        raise InvalidTransitionError(current_status, target_status)


def can_transition(current_status: str, target_status: str) -> bool:
    """Check whether a transition is legal without raising."""
    return (current_status, target_status) in LEGAL_TRANSITIONS
