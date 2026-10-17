"""Re-imports and re-runs must not change the business result; genuine new
evidence (a late receipt) must update the same exception, not create a new one."""

from sqlalchemy import func, select

from tests.conftest import csv_with

from app.db.models import ExceptionCase, PoLine, ReceiptLine, SourceRecord, Task, TaskAssignment


def _snapshot(env) -> dict:
    with env.session() as s:
        return {
            "source_records": s.scalar(select(func.count()).select_from(SourceRecord)),
            "po_lines": s.scalar(select(func.count()).select_from(PoLine)),
            "receipt_qty": sorted((r.source_record.source_record_id, str(r.accepted_qty_base))
                                  for r in s.scalars(select(ReceiptLine))),
            "exceptions": sorted((e.id, e.case_key, e.status, str(e.expected_qty), str(e.received_qty),
                                  str(e.variance_qty)) for e in s.scalars(select(ExceptionCase))),
            "tasks": sorted((t.id, t.task_key, t.status, t.assignee_id) for t in s.scalars(select(Task))),
            "assignments": s.scalar(select(func.count()).select_from(TaskAssignment)),
        }


def test_importing_everything_twice_does_not_change_the_result(env):
    env.load_all()
    env.detect()
    first = _snapshot(env)

    second_imports = env.load_all()
    env.detect()
    assert _snapshot(env) == first

    from app.db.models import ImportBatch

    with env.session() as s:
        for import_id in second_imports.values():
            b = s.get(ImportBatch, import_id)
            assert b.accepted_rows == 0 and b.conflict_rows == 0 and b.duplicate_rows == b.total_rows


def test_running_detection_repeatedly_creates_no_duplicates(env):
    env.load_all()
    for _ in range(3):
        env.detect()
    with env.session() as s:
        assert s.scalar(select(func.count()).select_from(ExceptionCase)) == 3
        assert s.scalar(select(func.count()).select_from(Task)) == 3


def test_exception_id_is_stable_across_databases(env):
    from app.detection.service import exception_id_for

    env.load_all()
    env.detect()
    with env.session() as s:
        exc = s.scalar(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501"))
        assert exc.id == exception_id_for("default", "receipt_shortfall:ERP:PO-501:1")


LATE_501 = {
    "source_record_id": "WMS-GRN501-C-L1", "receipt_number": "GRN-501-C", "receipt_line": "1",
    "po_number": "PO-501", "po_line_number": "1", "location_code": "WH-MH-014", "product_ref": "WMS-BRC-001",
    "uom": "EA", "received_at": "2026-10-16T13:00:00+05:30", "posting_status": "POSTED",
}


def test_late_partial_receipt_updates_the_same_exception(env):
    env.load_all()
    env.detect()
    env.load("goods_receipt_lines", csv_with("goods_receipt_lines", append=[{**LATE_501, "accepted_qty": "5"}]))
    env.detect()
    with env.session() as s:
        excs = list(s.scalars(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501")))
        assert len(excs) == 1
        exc = excs[0]
        assert (exc.received_qty, exc.variance_qty, exc.status) == (97, 3, "OPEN")
        assert [e.event_type for e in exc.events] == ["DETECTED", "REEVALUATED"]
        assert len(exc.tasks) == 1


def test_late_receipt_closing_the_gap_clears_exception_and_cancels_task(env):
    env.load_all()
    env.detect()
    env.load("goods_receipt_lines", csv_with("goods_receipt_lines", append=[{**LATE_501, "accepted_qty": "8"}]))
    env.detect()
    with env.session() as s:
        exc = s.scalar(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501"))
        assert exc.status == "CLEARED"
        assert exc.tasks[0].status == "CANCELLED"
        assert exc.events[-1].event_type == "CLEARED"
