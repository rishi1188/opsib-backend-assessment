"""Task lifecycle through the API: valid and invalid transitions, actor
checks, idempotent completion, reassignment, verification and audit history."""

import pytest
from sqlalchemy import func, select

from tests.conftest import csv_with

from app.db.models import TaskEvent

COMPLETE = {"resolution_code": "SHORTAGE_CONFIRMED_CLAIM_RAISED", "evidence_ref": "doc://store-014/count-sheet-77"}


@pytest.fixture
def task(loaded, client):
    tasks = client.get("/tasks", params={"assignee_id": "HR-102"}).json()
    return next(t for t in tasks if t["task_key"].endswith("PO-501:1:investigate"))


def post(client, task_id, command, actor, body=None, key=None):
    headers = {"X-Actor-Id": actor}
    if key:
        headers["Idempotency-Key"] = key
    return client.post(f"/tasks/{task_id}/{command}", json=body, headers=headers)


def to_in_progress(client, task_id, actor="HR-102"):
    assert post(client, task_id, "acknowledge", actor).status_code == 200
    assert post(client, task_id, "start", actor).status_code == 200


def test_tasks_can_be_listed_by_assignee_and_location(task, client):
    by_assignee = client.get("/tasks", params={"assignee_id": "HR-102"}).json()
    assert {t["location_id"] for t in by_assignee} == {"STORE-014"} and len(by_assignee) == 2
    by_location = client.get("/tasks", params={"location_id": "STORE-015"}).json()
    assert [t["assignee_id"] for t in by_location] == ["HR-401"]
    assert by_location[0]["due_at"].startswith("2026-10-17T09:30:00")  # frozen time + 24h SLA


def test_full_lifecycle_resolves_exception_with_audit_trail(task, client):
    to_in_progress(client, task["id"])
    r = post(client, task["id"], "complete", "HR-102", COMPLETE)
    assert r.status_code == 200 and r.json()["status"] == "PENDING_VERIFICATION"
    r = post(client, task["id"], "verify", "HR-201", {"note": "count sheet checked"})
    assert r.status_code == 200 and r.json()["status"] == "VERIFIED"

    exc = client.get(f"/exceptions/{task['exception_id']}").json()
    assert exc["status"] == "RESOLVED" and exc["resolution_code"] == COMPLETE["resolution_code"]
    assert [e["event_type"] for e in exc["events"]] == ["DETECTED", "RESOLVED"]

    detail = client.get(f"/tasks/{task['id']}").json()
    assert [(e["event_type"], e["from_status"], e["to_status"], e["actor"]) for e in detail["events"]] == [
        ("CREATED", None, "CREATED", "system"),
        ("ASSIGN", "CREATED", "ASSIGNED", "system"),
        ("ACKNOWLEDGE", "ASSIGNED", "ACKNOWLEDGED", "HR-102"),
        ("START", "ACKNOWLEDGED", "IN_PROGRESS", "HR-102"),
        ("COMPLETE", "IN_PROGRESS", "COMPLETED", "HR-102"),
        ("SUBMIT_FOR_VERIFICATION", "COMPLETED", "PENDING_VERIFICATION", "system"),
        ("VERIFY", "PENDING_VERIFICATION", "VERIFIED", "HR-201"),
    ]


def test_invalid_transitions_are_rejected(task, client):
    r = post(client, task["id"], "complete", "HR-102", COMPLETE)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"
    assert r.json()["error"]["details"]["current_status"] == "ASSIGNED"
    assert post(client, task["id"], "start", "HR-102").status_code == 409
    assert post(client, task["id"], "verify", "HR-201").status_code == 409


def test_only_the_assignee_can_work_the_task(task, client):
    r = post(client, task["id"], "acknowledge", "HR-101")
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert post(client, task["id"], "acknowledge", "HR-103").status_code == 403  # inactive actor


def test_actor_header_is_required(task, client):
    assert client.post(f"/tasks/{task['id']}/acknowledge").status_code == 422


def test_repeated_completion_is_idempotent(task, client, loaded):
    to_in_progress(client, task["id"])
    first = post(client, task["id"], "complete", "HR-102", COMPLETE)
    assert first.status_code == 200 and first.json()["idempotent_replay"] is False
    with loaded.session() as s:
        events_before = s.scalar(select(func.count()).select_from(TaskEvent))

    retry = post(client, task["id"], "complete", "HR-102", COMPLETE)
    assert retry.status_code == 200
    assert retry.json()["idempotent_replay"] is True and retry.headers["Idempotent-Replay"] == "true"
    assert retry.json()["status"] == "PENDING_VERIFICATION"
    assert retry.json()["version"] == first.json()["version"]
    with loaded.session() as s:
        assert s.scalar(select(func.count()).select_from(TaskEvent)) == events_before


def test_completion_retry_with_different_details_conflicts(task, client):
    to_in_progress(client, task["id"])
    post(client, task["id"], "complete", "HR-102", COMPLETE)
    r = post(client, task["id"], "complete", "HR-102", {**COMPLETE, "resolution_code": "PO_QUANTITY_AMENDED"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


def test_idempotency_key_replays_stored_response(task, client):
    to_in_progress(client, task["id"])
    first = post(client, task["id"], "complete", "HR-102", COMPLETE, key="k-1")
    replay = post(client, task["id"], "complete", "HR-102", COMPLETE, key="k-1")
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    assert {k: v for k, v in replay.json().items() if k != "idempotent_replay"} == \
        {k: v for k, v in first.json().items() if k != "idempotent_replay"}
    reused = post(client, task["id"], "complete", "HR-102", {**COMPLETE, "evidence_ref": "other"}, key="k-1")
    assert reused.status_code == 409


def test_completion_requires_known_resolution_and_evidence(task, client):
    to_in_progress(client, task["id"])
    r = post(client, task["id"], "complete", "HR-102", {**COMPLETE, "resolution_code": "SUPPLIER_AT_FAULT"})
    assert r.status_code == 422
    r = post(client, task["id"], "complete", "HR-102", {**COMPLETE, "evidence_ref": "   "})
    assert r.status_code == 422


def test_completer_cannot_verify_and_associate_cannot_verify(task, client, loaded):
    to_in_progress(client, task["id"])
    post(client, task["id"], "complete", "HR-102", COMPLETE)
    assert post(client, task["id"], "verify", "HR-102").status_code == 403
    assert post(client, task["id"], "verify", "HR-401").status_code == 403  # manager of another store
    assert post(client, task["id"], "verify", "HR-501").status_code == 200  # regional ops, same region


def test_reject_reopens_task_for_the_assignee(task, client):
    to_in_progress(client, task["id"])
    post(client, task["id"], "complete", "HR-102", COMPLETE)
    r = post(client, task["id"], "reject", "HR-201", {"reason": "count sheet missing signature"})
    assert r.status_code == 200 and r.json()["status"] == "IN_PROGRESS" and r.json()["resolution_code"] is None
    assert post(client, task["id"], "complete", "HR-102", COMPLETE).json()["status"] == "PENDING_VERIFICATION"


def test_block_and_unblock(task, client):
    to_in_progress(client, task["id"])
    assert post(client, task["id"], "block", "HR-102", {"reason": "waiting for WMS access"}).json()["status"] == "BLOCKED"
    assert post(client, task["id"], "complete", "HR-102", COMPLETE).status_code == 409
    assert post(client, task["id"], "unblock", "HR-102").json()["status"] == "IN_PROGRESS"


def test_reassignment_records_reason_actor_and_history(task, client, env):
    to_in_progress(client, task["id"])
    # HR-101 is OFF_SHIFT: manual reassignment still enforces eligibility.
    r = post(client, task["id"], "reassign", "HR-201", {"assignee_id": "HR-101", "reason": "Asha knows the vendor"})
    assert r.status_code == 409 and "availability_status is OFF_SHIFT" in r.json()["error"]["details"]["reasons"]
    # The associate cannot reassign their own task.
    r = post(client, task["id"], "reassign", "HR-102", {"assignee_id": "HR-201", "reason": "x"})
    assert r.status_code == 403
    r = post(client, task["id"], "reassign", "HR-201", {"assignee_id": "HR-201", "reason": "taking over"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ASSIGNED" and body["assignee_id"] == "HR-201"

    detail = client.get(f"/tasks/{task['id']}").json()
    assert [a["assignee_id"] for a in detail["assignments"]] == ["HR-102", "HR-201"]
    manual = detail["assignments"][-1]
    assert manual["assigned_by"] == "HR-201" and manual["reason"] == "taking over"
    last = detail["events"][-1]
    assert last["event_type"] == "REASSIGN" and last["data"]["previous_assignee"] == "HR-102"
    # Reassignment did not create a second exception.
    assert len(client.get("/exceptions").json()) == 3


def test_reassign_requires_reason(task, client):
    r = post(client, task["id"], "reassign", "HR-201", {"assignee_id": "HR-201", "reason": ""})
    assert r.status_code == 422


def test_manual_queue_task_can_be_assigned_by_supervisor(env, client):
    off = csv_with("workforce_status",
                   replace=lambda r: {"availability_status": "OFF_SHIFT"} if r["source_employee_id"] == "HR-401" else None)
    env.load_all(overrides={"workforce_status": off})
    env.detect()
    t = client.get("/tasks", params={"location_id": "STORE-015"}).json()[0]
    assert t["status"] == "CREATED"
    # Nobody eligible at STORE-015, so even a supervisor cannot force HR-401 in.
    r = post(client, t["id"], "reassign", "HR-501", {"assignee_id": "HR-401", "reason": "covering"})
    assert r.status_code == 409


def test_unknown_task_is_404(client, loaded):
    r = post(client, "nope", "acknowledge", "HR-102")
    assert r.status_code == 404
