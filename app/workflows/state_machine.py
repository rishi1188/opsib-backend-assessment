"""Server-enforced task lifecycle.

    CREATED --assign--> ASSIGNED --acknowledge--> ACKNOWLEDGED --start--> IN_PROGRESS
    IN_PROGRESS --block--> BLOCKED --unblock--> IN_PROGRESS
    IN_PROGRESS --complete--> COMPLETED --(automatic)--> PENDING_VERIFICATION
    PENDING_VERIFICATION --verify--> VERIFIED                (terminal)
    PENDING_VERIFICATION --reject--> IN_PROGRESS             (reopened, same assignee)
    any open state --reassign--> ASSIGNED                     (acknowledgement resets)
    any open state --cancel--> CANCELLED                      (system only, terminal)

Clients send commands, never target statuses.
"""

from app.errors import InvalidTransition

CREATED = "CREATED"
ASSIGNED = "ASSIGNED"
ACKNOWLEDGED = "ACKNOWLEDGED"
IN_PROGRESS = "IN_PROGRESS"
BLOCKED = "BLOCKED"
COMPLETED = "COMPLETED"
PENDING_VERIFICATION = "PENDING_VERIFICATION"
VERIFIED = "VERIFIED"
CANCELLED = "CANCELLED"

OPEN_STATES = frozenset({CREATED, ASSIGNED, ACKNOWLEDGED, IN_PROGRESS, BLOCKED})
DONE_STATES = frozenset({COMPLETED, PENDING_VERIFICATION, VERIFIED})
TERMINAL_STATES = frozenset({VERIFIED, CANCELLED})

TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "assign": (frozenset({CREATED}), ASSIGNED),
    "acknowledge": (frozenset({ASSIGNED}), ACKNOWLEDGED),
    "start": (frozenset({ACKNOWLEDGED}), IN_PROGRESS),
    "block": (frozenset({IN_PROGRESS}), BLOCKED),
    "unblock": (frozenset({BLOCKED}), IN_PROGRESS),
    "complete": (frozenset({IN_PROGRESS}), COMPLETED),
    "submit_for_verification": (frozenset({COMPLETED}), PENDING_VERIFICATION),
    "verify": (frozenset({PENDING_VERIFICATION}), VERIFIED),
    "reject": (frozenset({PENDING_VERIFICATION}), IN_PROGRESS),
    "reassign": (OPEN_STATES, ASSIGNED),
    "cancel": (OPEN_STATES | frozenset({COMPLETED, PENDING_VERIFICATION}), CANCELLED),
}


def next_status(command: str, current: str) -> str:
    try:
        allowed_from, target = TRANSITIONS[command]
    except KeyError as exc:
        raise InvalidTransition(f"Unknown command '{command}'") from exc
    if current not in allowed_from:
        raise InvalidTransition(
            f"Cannot {command} a task in status {current}",
            {"command": command, "current_status": current, "allowed_from": sorted(allowed_from)},
        )
    return target
