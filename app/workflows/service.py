"""Task creation, routing, transitions, reassignment and verification.

Every state change writes a ``TaskEvent``; every assignment decision writes a
``TaskAssignment`` with the policy reasoning. Exceptions only become RESOLVED
when their tasks have been VERIFIED by someone other than the completer.
Concurrent edits to one task are caught by the ``version`` column (optimistic
locking) and surface as HTTP 409.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import Settings
from app.db.models import Employee, ExceptionCase, ExceptionEvent, Task, TaskAssignment, TaskEvent
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.logging_config import get_logger
from app.routing.policy import MANUAL_QUEUE, POLICY_VERSION
from app.routing.service import check_manual_assignee, get_employee, route_task
from app.workflows import state_machine as sm

log = get_logger("workflows")

SYSTEM_ACTOR = "system"
TASK_TYPE_INVESTIGATE = "INVESTIGATE_RECEIPT_SHORTFALL"
SUPERVISOR_ROLES = frozenset({"store_manager", "regional_operations"})

# Neutral resolution vocabulary: none of these asserts supplier fault.
RESOLUTION_CODES = frozenset(
    {
        "RECEIPT_POSTING_CORRECTED",  # units were on hand; WMS receipt fixed
        "LATE_RECEIPT_POSTED",  # units arrived after the deadline and were received
        "SHORTAGE_CONFIRMED_CLAIM_RAISED",  # shortage confirmed with evidence, claim opened
        "PO_QUANTITY_AMENDED",  # ERP order quantity corrected
        "NO_FURTHER_ACTION",  # investigated; documented reason in note
    }
)


@dataclass
class CommandResult:
    task: Task
    replayed: bool = False


def _max_age(settings: Settings) -> timedelta:
    return timedelta(minutes=settings.availability_max_age_minutes)


def _event(session: Session, task: Task, clock: Clock, event_type: str, actor: str,
           from_status: str | None, to_status: str | None, **data) -> None:
    session.add(
        TaskEvent(
            task_id=task.id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            occurred_at=clock.now(),
            data=data,
        )
    )


def _transition(session: Session, task: Task, clock: Clock, command: str, actor: str, **data) -> None:
    previous = task.status
    task.status = sm.next_status(command, previous)
    task.updated_at = clock.now()
    _event(session, task, clock, command.upper(), actor, previous, task.status, **data)
    log.info("task_transition", extra={"task_id": task.id, "command": command,
                                        "from": previous, "to": task.status, "actor": actor})


def exception_event(session: Session, exc: ExceptionCase, clock: Clock, event_type: str,
                    actor: str = SYSTEM_ACTOR, **data) -> None:
    session.add(ExceptionEvent(exception_id=exc.id, event_type=event_type, actor=actor,
                               occurred_at=clock.now(), data=data))


# --------------------------------------------------------------------------
# Creation and automatic routing
# --------------------------------------------------------------------------


def ensure_investigation_task(session: Session, clock: Clock, settings: Settings,
                              exc: ExceptionCase) -> tuple[Task, bool]:
    """Create the investigation task for an exception exactly once.

    The (tenant_id, task_key) unique constraint backs this up: a second worker
    that slips past the check fails to insert and its transaction rolls back.
    """
    task_key = f"{exc.case_key}:investigate"
    existing = session.scalar(select(Task).where(Task.tenant_id == exc.tenant_id, Task.task_key == task_key))
    if existing is not None:
        return existing, False

    now = clock.now()
    task = Task(
        tenant_id=exc.tenant_id,
        task_key=task_key,
        exception_id=exc.id,
        task_type=TASK_TYPE_INVESTIGATE,
        status=sm.CREATED,
        location_id=exc.canonical_location_id,
        due_at=now + timedelta(hours=settings.task_sla_hours),
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    _event(session, task, clock, "CREATED", SYSTEM_ACTOR, None, sm.CREATED, exception_id=exc.id)

    decision = route_task(session, exc.tenant_id, task.location_id, now, _max_age(settings))
    session.add(
        TaskAssignment(
            task_id=task.id,
            assignee_id=decision.assignee_id,
            queue=decision.queue,
            policy=decision.policy,
            reason=decision.reason,
            candidates=[c.as_dict() for c in decision.candidates],
            assigned_by=SYSTEM_ACTOR,
            assigned_at=now,
        )
    )
    if decision.assignee_id:
        task.assignee_id = decision.assignee_id
        task.queue = None
        _transition(session, task, clock, "assign", SYSTEM_ACTOR,
                    assignee_id=decision.assignee_id, reason=decision.reason)
    else:
        task.queue = MANUAL_QUEUE
        _event(session, task, clock, "NEEDS_MANUAL_ASSIGNMENT", SYSTEM_ACTOR, sm.CREATED, sm.CREATED,
               reason=decision.reason)
    session.flush()
    return task, True


def cancel_open_tasks(session: Session, clock: Clock, exc: ExceptionCase, reason: str) -> None:
    for task in exc.tasks:
        if task.status not in sm.TERMINAL_STATES:
            _transition(session, task, clock, "cancel", SYSTEM_ACTOR, reason=reason)


# --------------------------------------------------------------------------
# Client commands
# --------------------------------------------------------------------------


def get_task(session: Session, tenant_id: str, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.tenant_id != tenant_id:
        raise NotFound(f"Task '{task_id}' not found")
    return task


def _require_actor(session: Session, tenant_id: str, actor_id: str) -> Employee:
    emp = get_employee(session, tenant_id, actor_id)
    if emp is None:
        raise Forbidden(f"Actor '{actor_id}' is not a known employee")
    if emp.employment_status != "ACTIVE":
        raise Forbidden(f"Actor '{actor_id}' is not ACTIVE")
    return emp


def _require_assignee(task: Task, actor_id: str) -> None:
    if task.assignee_id != actor_id:
        raise Forbidden("Only the current assignee can perform this action",
                        {"assignee_id": task.assignee_id, "actor_id": actor_id})


def _require_supervisor(session: Session, task: Task, actor: Employee) -> None:
    if actor.role not in SUPERVISOR_ROLES:
        raise Forbidden(f"Role {actor.role} cannot supervise tasks")
    if actor.role == "store_manager" and actor.canonical_location_id != task.location_id:
        raise Forbidden("Store managers can only supervise tasks at their own store")
    if actor.role == "regional_operations":
        exc = session.get(ExceptionCase, task.exception_id)
        if exc is None or exc.region_id != actor.region_id:
            raise Forbidden("Regional operations can only supervise tasks in their region")


def acknowledge(session: Session, clock: Clock, tenant_id: str, task_id: str, actor_id: str) -> CommandResult:
    task = get_task(session, tenant_id, task_id)
    _require_actor(session, tenant_id, actor_id)
    _require_assignee(task, actor_id)
    _transition(session, task, clock, "acknowledge", actor_id)
    return CommandResult(task)


def start(session: Session, clock: Clock, tenant_id: str, task_id: str, actor_id: str) -> CommandResult:
    task = get_task(session, tenant_id, task_id)
    _require_actor(session, tenant_id, actor_id)
    _require_assignee(task, actor_id)
    _transition(session, task, clock, "start", actor_id)
    return CommandResult(task)


def block(session: Session, clock: Clock, tenant_id: str, task_id: str, actor_id: str, reason: str) -> CommandResult:
    task = get_task(session, tenant_id, task_id)
    _require_actor(session, tenant_id, actor_id)
    _require_assignee(task, actor_id)
    _transition(session, task, clock, "block", actor_id, reason=reason)
    return CommandResult(task)


def unblock(session: Session, clock: Clock, tenant_id: str, task_id: str, actor_id: str) -> CommandResult:
    task = get_task(session, tenant_id, task_id)
    _require_actor(session, tenant_id, actor_id)
    _require_assignee(task, actor_id)
    _transition(session, task, clock, "unblock", actor_id)
    return CommandResult(task)


def complete(session: Session, clock: Clock, tenant_id: str, task_id: str, actor_id: str,
             resolution_code: str, evidence_ref: str, note: str | None = None) -> CommandResult:
    """Complete a task. A repeat of an already-applied completion is a no-op success.

    This covers the "committed but the client timed out" case even without an
    Idempotency-Key: the retry finds the task already completed by the same
    actor with the same resolution and returns it unchanged. A retry with
    different details is a genuine conflict (409), never a silent overwrite.
    """
    if resolution_code not in RESOLUTION_CODES:
        raise ValidationFailed("Unknown resolution_code", {"allowed": sorted(RESOLUTION_CODES)})
    if not evidence_ref or not evidence_ref.strip():
        raise ValidationFailed("evidence_ref is required to complete a task")

    task = get_task(session, tenant_id, task_id)
    _require_actor(session, tenant_id, actor_id)

    if task.status in sm.DONE_STATES:
        same = (task.completed_by == actor_id and task.resolution_code == resolution_code
                and task.evidence_ref == evidence_ref.strip())
        if same:
            return CommandResult(task, replayed=True)
        raise Conflict("Task was already completed with different details",
                       {"status": task.status, "completed_by": task.completed_by,
                        "resolution_code": task.resolution_code})

    _require_assignee(task, actor_id)
    task.completed_by = actor_id
    task.resolution_code = resolution_code
    task.evidence_ref = evidence_ref.strip()
    task.resolution_note = note
    _transition(session, task, clock, "complete", actor_id,
                resolution_code=resolution_code, evidence_ref=task.evidence_ref)
    _transition(session, task, clock, "submit_for_verification", SYSTEM_ACTOR)
    return CommandResult(task)


def verify(session: Session, clock: Clock, tenant_id: str, task_id: str, actor_id: str,
           note: str | None = None) -> CommandResult:
    """Verification criteria: task is PENDING_VERIFICATION with a resolution code and
    evidence reference, and the verifier is an active supervisor in scope who did
    not complete the task. When every non-cancelled task of the exception is
    VERIFIED, the exception becomes RESOLVED."""
    task = get_task(session, tenant_id, task_id)
    actor = _require_actor(session, tenant_id, actor_id)
    if task.status == sm.VERIFIED and task.verified_by == actor_id:
        return CommandResult(task, replayed=True)
    sm.next_status("verify", task.status)  # report a wrong state before anything else
    _require_supervisor(session, task, actor)
    if task.completed_by == actor_id:
        raise Forbidden("The person who completed a task cannot verify it")
    if not task.resolution_code or not task.evidence_ref:
        raise ValidationFailed("Task lacks a resolution code or evidence reference")
    _transition(session, task, clock, "verify", actor_id, note=note)
    task.verified_by = actor_id

    exc = session.get(ExceptionCase, task.exception_id)
    session.flush()
    live = [t for t in exc.tasks if t.status != sm.CANCELLED]
    if live and all(t.status == sm.VERIFIED for t in live) and exc.status == "OPEN":
        exc.status = "RESOLVED"
        exc.resolution_code = task.resolution_code
        exc.resolved_at = clock.now()
        exception_event(session, exc, clock, "RESOLVED", actor_id,
                        resolution_code=task.resolution_code, evidence_ref=task.evidence_ref,
                        verified_task_ids=[t.id for t in live])
    return CommandResult(task)


def reject(session: Session, clock: Clock, tenant_id: str, task_id: str, actor_id: str,
           reason: str) -> CommandResult:
    task = get_task(session, tenant_id, task_id)
    actor = _require_actor(session, tenant_id, actor_id)
    _require_supervisor(session, task, actor)
    _transition(session, task, clock, "reject", actor_id, reason=reason)
    task.completed_by = None
    task.resolution_code = None
    task.evidence_ref = None
    return CommandResult(task)


def reassign(session: Session, clock: Clock, settings: Settings, tenant_id: str, task_id: str,
             actor_id: str, assignee_id: str, reason: str) -> CommandResult:
    """Supervisor reassignment. The new assignee must pass the same eligibility
    policy as automatic routing; the full check is stored with the assignment."""
    if not reason or not reason.strip():
        raise ValidationFailed("A reason is required to reassign a task")
    task = get_task(session, tenant_id, task_id)
    actor = _require_actor(session, tenant_id, actor_id)
    _require_supervisor(session, task, actor)
    if task.assignee_id == assignee_id and task.status == sm.ASSIGNED:
        return CommandResult(task, replayed=True)

    target = get_employee(session, tenant_id, assignee_id)
    if target is None:
        raise NotFound(f"Employee '{assignee_id}' not found")
    check = check_manual_assignee(session, tenant_id, target, task.location_id, clock.now(), _max_age(settings))
    if not check.eligible:
        raise Conflict(f"Employee '{assignee_id}' is not eligible for this task", {"reasons": check.reasons})

    previous_assignee = task.assignee_id
    command = "assign" if task.status == sm.CREATED else "reassign"
    task.assignee_id = assignee_id
    task.queue = None
    session.add(
        TaskAssignment(
            task_id=task.id,
            assignee_id=assignee_id,
            queue=None,
            policy=f"{POLICY_VERSION}:manual",
            reason=reason.strip(),
            candidates=[check.as_dict()],
            assigned_by=actor_id,
            assigned_at=clock.now(),
        )
    )
    _transition(session, task, clock, command, actor_id, previous_assignee=previous_assignee,
                assignee_id=assignee_id, reason=reason.strip())
    return CommandResult(task)


def list_tasks(session: Session, tenant_id: str, assignee_id: str | None = None,
               location_id: str | None = None, status: str | None = None,
               queue: str | None = None) -> list[Task]:
    q = select(Task).where(Task.tenant_id == tenant_id)
    if assignee_id:
        q = q.where(Task.assignee_id == assignee_id)
    if location_id:
        q = q.where(Task.location_id == location_id)
    if status:
        q = q.where(Task.status == status)
    if queue:
        q = q.where(Task.queue == queue)
    return list(session.scalars(q.order_by(Task.due_at, Task.id)))
