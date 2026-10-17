"""Identity and assignment: location mapping, off-shift, inactive, fallback
manager, manual queue, stale samples and region-scoped staff."""

from datetime import datetime, timedelta

from sqlalchemy import select

from tests.conftest import csv_with

from app.db.models import ExceptionCase
from app.routing.policy import MANUAL_QUEUE, AvailabilityFact, EmployeeFact, choose_assignee

AS_OF = datetime.fromisoformat("2026-10-16T15:00:00+05:30")
MAX_AGE = timedelta(minutes=60)


def sample(status="ON_SHIFT", observed=AS_OF - timedelta(minutes=10)):
    return [AvailabilityFact(status, AS_OF - timedelta(hours=6), AS_OF + timedelta(hours=3), observed)]


def emp(eid, role="receiving_associate", status="ACTIVE", loc="STORE-014", issues=()):
    return EmployeeFact(eid, status, role, loc, "REGION-MH", issues)


def test_inactive_employee_loses_even_when_on_shift():
    d = choose_assignee([emp("A", status="INACTIVE")], {"A": sample()}, "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id is None and d.queue == MANUAL_QUEUE
    assert "employment_status is INACTIVE" in d.candidates[0].reasons


def test_off_shift_employee_is_skipped():
    d = choose_assignee([emp("A"), emp("B")], {"A": sample("OFF_SHIFT"), "B": sample()}, "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id == "B"


def test_stale_availability_sample_is_not_trusted():
    stale = sample(observed=AS_OF - timedelta(hours=3))
    d = choose_assignee([emp("A")], {"A": stale}, "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id is None
    assert any("stale" in r for r in d.candidates[0].reasons)


def test_future_availability_sample_is_ignored():
    future = sample(observed=AS_OF + timedelta(minutes=5))
    d = choose_assignee([emp("A")], {"A": future}, "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id is None


def test_fallback_to_store_manager_when_no_associate():
    d = choose_assignee([emp("A", status="INACTIVE"), emp("M", role="store_manager")],
                        {"A": sample(), "M": sample()}, "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id == "M" and d.reason.startswith("fallback role store_manager")


def test_primary_role_wins_over_manager():
    d = choose_assignee([emp("M", role="store_manager"), emp("A")], {"A": sample(), "M": sample()},
                        "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id == "A" and d.reason.startswith("primary role receiving_associate")


def test_other_store_staff_and_regional_staff_are_not_selected():
    d = choose_assignee([emp("A", loc="STORE-015"), emp("R", role="regional_operations", loc=None)],
                        {"A": sample(), "R": sample()}, "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id is None
    reasons = {c.employee_id: c.reasons for c in d.candidates}
    assert any("task is at STORE-014" in r for r in reasons["A"])
    assert any("region-scoped" in r for r in reasons["R"])


def test_unmapped_employee_location_is_never_matched():
    d = choose_assignee([emp("A", loc=None, issues=("LOCATION_UNMAPPED:HRMS:ST-999",))], {"A": sample()},
                        "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id is None


def test_ties_break_by_employee_id():
    d = choose_assignee([emp("B"), emp("A")], {"A": sample(), "B": sample()}, "STORE-014", AS_OF, MAX_AGE)
    assert d.assignee_id == "A"


# ---------------------------------------------------------- with fixtures


def _task_for(env, po_number):
    with env.session() as s:
        exc = s.scalar(select(ExceptionCase).where(ExceptionCase.po_number == po_number))
        task = exc.tasks[0]
        return task, list(task.assignments), list(task.events)


def test_fixture_routing_store_014_selects_hr_102(loaded):
    task, assignments, _ = _task_for(loaded, "PO-501")
    assert task.status == "ASSIGNED" and task.assignee_id == "HR-102" and task.location_id == "STORE-014"
    checks = {c["employee_id"]: c for c in assignments[0].candidates}
    assert not checks["HR-101"]["eligible"] and "availability_status is OFF_SHIFT" in checks["HR-101"]["reasons"]
    assert not checks["HR-103"]["eligible"] and "employment_status is INACTIVE" in checks["HR-103"]["reasons"]
    assert checks["HR-102"]["eligible"]
    assert assignments[0].assigned_by == "system" and "primary role" in assignments[0].reason


def test_fixture_routing_store_015_falls_back_to_hr_401(loaded):
    task, assignments, _ = _task_for(loaded, "PO-505")
    assert task.assignee_id == "HR-401" and task.location_id == "STORE-015"
    assert "fallback role store_manager" in assignments[0].reason


def test_no_approved_fallback_leaves_task_visible_for_manual_assignment(env, client):
    off = csv_with("workforce_status",
                   replace=lambda r: {"availability_status": "OFF_SHIFT"} if r["source_employee_id"] == "HR-401" else None)
    env.load_all(overrides={"workforce_status": off})
    env.detect()
    task, assignments, events = _task_for(env, "PO-505")
    assert task.status == "CREATED" and task.assignee_id is None and task.queue == MANUAL_QUEUE
    assert assignments[0].assignee_id is None and "manual assignment" in assignments[0].reason
    assert events[-1].event_type == "NEEDS_MANUAL_ASSIGNMENT"
    listed = client.get("/tasks", params={"queue": MANUAL_QUEUE}).json()
    assert [t["id"] for t in listed] == [task.id]
