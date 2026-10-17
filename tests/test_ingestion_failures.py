"""Ingestion failure paths: rejected rows, missing columns, conflicting
duplicate IDs, unknown mappings and load-order independence."""

from sqlalchemy import select

from tests.conftest import csv_with

from app.db.models import ImportBatch, PoLine, SourceRecord
from app.detection.shortfall import NOT_EVALUABLE


def _batch(env, import_id) -> ImportBatch:
    with env.session() as s:
        b = s.get(ImportBatch, import_id)
        b.rows  # load
        return b


def test_invalid_rows_are_rejected_with_reasons_and_others_still_load(env):
    content = csv_with("purchase_order_lines", append=[
        {"source_record_id": "ERP-BAD-1", "po_number": "PO-900", "line_number": "1", "supplier_code": "S",
         "location_code": "MUM-014", "item_code": "ERP-SKU-001", "ordered_qty": "-5", "ordered_uom": "EA",
         "status": "OPEN", "expected_receipt_by": "2026-10-15T12:00:00+05:30"},
        {"source_record_id": "ERP-BAD-2", "po_number": "PO-901", "line_number": "1", "supplier_code": "S",
         "location_code": "MUM-014", "item_code": "ERP-SKU-001", "ordered_qty": "5", "ordered_uom": "EA",
         "status": "OPEN", "expected_receipt_by": "2026-10-15 12:00"},
        {"source_record_id": "ERP-BAD-3", "po_number": "PO-902", "line_number": "1", "supplier_code": "S",
         "location_code": "MUM-014", "item_code": "ERP-SKU-001", "ordered_qty": "5", "ordered_uom": "EA",
         "status": "SHIPPED", "expected_receipt_by": "2026-10-15T12:00:00+05:30"},
        {"source_record_id": "", "po_number": "PO-903", "line_number": "1", "supplier_code": "S",
         "location_code": "MUM-014", "item_code": "ERP-SKU-001", "ordered_qty": "5", "ordered_uom": "EA",
         "status": "OPEN", "expected_receipt_by": "2026-10-15T12:00:00+05:30"},
    ])
    b = _batch(env, env.load("purchase_order_lines", content))
    assert b.status == "COMPLETED_WITH_ISSUES"
    assert (b.accepted_rows, b.duplicate_rows, b.rejected_rows) == (6, 1, 4)
    rejected = {r.row_number: r for r in b.rows if r.outcome == "REJECTED"}
    reasons = " | ".join(" ".join(r.reasons) for r in rejected.values())
    assert "ordered_qty" in reasons
    assert "expected_receipt_by" in reasons and "timezone" in reasons.lower()
    assert "status" in reasons
    assert "source_record_id" in reasons
    assert all(r.raw for r in rejected.values())  # the bad input stays visible


def test_missing_required_columns_fail_the_import_visibly(env, client):
    r = client.post("/imports/goods_receipt_lines",
                    files={"file": ("bad.csv", b"source_record_id,po_number\nX,PO-1\n", "text/csv")})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "FAILED" and "Missing required columns" in body["error"]
    assert client.get("/data-quality").json()["failed_imports"][0]["import_id"] == body["id"]


def test_unknown_dataset_is_404(client):
    r = client.post("/imports/invoices", files={"file": ("x.csv", b"a\n1\n", "text/csv")})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_conflicting_duplicate_id_is_flagged_not_overwritten(env, client):
    env.load_all()
    changed = csv_with("goods_receipt_lines",
                       replace=lambda r: {"accepted_qty": "100"} if r["source_record_id"] == "WMS-GRN501-A-L1" else None)
    b = _batch(env, env.load("goods_receipt_lines", changed))
    assert b.status == "COMPLETED_WITH_ISSUES"
    assert b.conflict_rows == 1
    conflict = next(r for r in b.rows if r.outcome == "CONFLICT")
    assert conflict.source_record_id == "WMS-GRN501-A-L1" and "accepted_qty" in conflict.reasons[0]

    with env.session() as s:
        rec = s.scalar(select(SourceRecord).where(SourceRecord.source_record_id == "WMS-GRN501-A-L1"))
        assert rec.payload["accepted_qty"] == "60"  # original kept

    env.detect()
    dq = client.get("/data-quality").json()
    assert dq["open_conflicts"][0]["changed_fields"] == ["accepted_qty"]
    blocked = {(x["po_number"]): x for x in dq["not_evaluable_po_lines"]}
    assert "PO-501" in blocked
    assert blocked["PO-501"]["reasons"][0].startswith("CONFLICTING_SOURCE_RECORD")
    # The same conflicting row again does not open a second conflict.
    env.load("goods_receipt_lines", changed)
    assert len(client.get("/data-quality").json()["open_conflicts"]) == 1


def test_unknown_product_mapping_is_data_quality_not_exception(env, client):
    env.load_all(overrides={"purchase_order_lines": csv_with(
        "purchase_order_lines",
        replace=lambda r: {"item_code": "ERP-SKU-404"} if r["po_number"] == "PO-501" else None)})
    env.detect()
    dq = client.get("/data-quality").json()
    assert {"record_type": "purchase_order_line", "record": "PO-501/1",
            "issues": ["PRODUCT_UNMAPPED:ERP:ERP-SKU-404"]} in dq["unmapped_records"]
    assert [x["po_number"] for x in dq["not_evaluable_po_lines"]] == ["PO-501"]
    assert "PO-501" not in [x["po_number"] for x in client.get("/exceptions").json()]


def test_unmapped_location_for_employee_is_reported(env, client):
    env.load_all(overrides={"employees": csv_with(
        "employees", replace=lambda r: {"location_code": "ST-999"} if r["source_employee_id"] == "HR-102" else None)})
    dq = client.get("/data-quality").json()
    assert {"record_type": "employee", "record": "HR-102", "issues": ["LOCATION_UNMAPPED:HRMS:ST-999"]} \
        in dq["unmapped_records"]


def test_alias_arriving_after_facts_resolves_them(env):
    env.load("purchase_order_lines")
    with env.session() as s:
        assert all(p.mapping_issues for p in s.scalars(select(PoLine)))
    env.load("product_aliases")
    env.load("location_aliases")
    with env.session() as s:
        lines = list(s.scalars(select(PoLine)))
        assert lines and all(not p.mapping_issues for p in lines)
        assert {p.po_number: p.ordered_qty_base for p in lines}["PO-505"] == 120


def test_reformatted_but_identical_row_is_a_duplicate(env):
    env.load_all()
    reformatted = csv_with("purchase_order_lines", replace=lambda r: {
        "ordered_qty": f"{r['ordered_qty']}.00",
        "expected_receipt_by": "2026-10-15T06:30:00+00:00" if r["expected_receipt_by"] == "2026-10-15T12:00:00+05:30"
        else r["expected_receipt_by"],
    })
    b = _batch(env, env.load("purchase_order_lines", reformatted))
    assert (b.accepted_rows, b.conflict_rows, b.duplicate_rows) == (0, 0, 7)


def test_not_evaluable_outcome_constant_is_used_in_report(env, client):
    env.load_all(overrides={"goods_receipt_lines": csv_with(
        "goods_receipt_lines",
        replace=lambda r: {"product_ref": "WMS-UNKNOWN"} if r["source_record_id"] == "WMS-GRN502-A-L1" else None)})
    run = client.post("/detection/runs").json()
    outcome = {e["po_number"]: e["outcome"] for e in run["evaluations"]}
    assert outcome["PO-502"] == NOT_EVALUABLE
