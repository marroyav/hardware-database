"""Authoritative DAPHNE production lifecycle and transition rules."""

from __future__ import annotations

from .errors import ProductionError


STATES = (
    "received",
    "discovered",
    "allocated",
    "provisioned",
    "qa_running",
    "qa_passed",
    "released",
    "quarantined",
    "service",
    "retired",
)

NORMAL_TRANSITIONS = {
    "received": {"discovered", "quarantined", "retired"},
    "discovered": {"allocated", "quarantined", "retired"},
    "allocated": {"provisioned", "quarantined", "retired"},
    "provisioned": {"qa_running", "quarantined", "service", "retired"},
    "qa_running": {"qa_passed", "quarantined", "service", "retired"},
    "qa_passed": {"released", "quarantined", "service", "retired"},
    "released": {"quarantined", "service", "retired"},
    "quarantined": {"retired"},
    "service": {"received", "quarantined", "retired"},
    "retired": set(),
}


def require_state(actual: str, allowed: set[str], operation: str) -> None:
    if actual not in allowed:
        expected = ", ".join(sorted(allowed))
        raise ProductionError(
            f"cannot {operation} while asset is {actual!r}; expected one of: {expected}"
        )


def check_transition(current: str, target: str) -> None:
    if current not in STATES or target not in STATES:
        raise ProductionError(f"unknown lifecycle transition: {current!r} -> {target!r}")
    if current == target:
        return
    if target not in NORMAL_TRANSITIONS[current]:
        raise ProductionError(f"lifecycle transition is not allowed: {current} -> {target}")
