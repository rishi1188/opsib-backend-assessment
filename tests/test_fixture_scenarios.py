"""Integration: load the untouched fixture pack through the API and verify the
six required scenario outcomes, finding content and traceability."""

import hashlib
from decimal import Decimal

from tests.conftest import FIXTURES

from app.ingestion.registry import LOAD_ORDER

EXPECTED = {
    "PO-501": ("SHORTFALL", "100", "92", "8"),
    "PO-502": ("NO_VARIANCE", "40", "40", "0"),
    "PO-503": ("NOT_DUE", "50", "20", "30"),
    "PO-504": ("CANCELLED", None, None, None),
    "PO-505": ("SHORTFALL", "120", "96", "24"),
    "PO-506": ("SHORTFALL", "20", "10", "10"),
}


def _num(v):
    return None if v is None else Decimal(v).normalize()


def _load_via_api(client):
    results = {}
    for dataset in LOAD_ORDER:
        with open(FIXTURES / f"{dataset}.csv", "rb") as fh:
            r = client.post(f"/imports/{dataset}", files={"file": (f"{dataset}.csv", fh, "text/csv")})
        assert r.status_code == 201, r.text
        results[dataset] = r.json()
    return results


def test_fixture_files_are_unchanged():
    expected = dict(
        line.split()[::-1] for line in (FIXTURES / "SHA256SUMS").read_text().splitlines() if line.strip()
    )
    for name, digest in expected.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name


def test_import_reports_row_level_outcomes(client):
    results = _load_via_api(client)
    po = results["purchase_order_lines"]
    assert (po["total_rows"], po["accepted_rows"], po["duplicate_rows"], po["rejected_rows"]) == (7, 6, 1, 0)
    dup = [r for r in po["rows"] if r["outcome"] == "DUPLICATE"]
    assert len(dup) == 1 and dup[0]["source_record_id"] == "ERP-PO506-L1"
    gr = results["goods_receipt_lines"]
    assert (gr["accepted_rows"], gr["duplicate_rows"]) == (6, 1)
    assert [r["source_record_id"] for r in gr["rows"] if r["outcome"] == "DUPLICATE"] == ["WMS-GRN506-A-L1"]

    fetched = client.get(f"/imports/{po['id']}", params={"outcome": "duplicate"}).json()
    assert len(fetched["rows"]) == 1


def test_required_scenario_outcomes(client):
    _load_via_api(client)
    run = client.post("/detection/runs").json()
    by_po = {e["po_number"]: e for e in run["evaluations"]}
    assert set(by_po) == set(EXPECTED)
    for po_number, (outcome, expected, received, variance) in EXPECTED.items():
        e = by_po[po_number]
        assert e["outcome"] == outcome, (po_number, e)
        if expected is not None:
            assert _num(e["expected_qty_base"]) == _num(expected), po_number
            assert _num(e["received_qty_base"]) == _num(received), po_number
            assert _num(e["variance_qty_base"]) == _num(variance), po_number
            assert e["base_uom"] == "EA"

    exceptions = client.get("/exceptions").json()
    assert sorted((x["po_number"], _num(x["variance_qty"])) for x in exceptions) == [
        ("PO-501", Decimal(8)), ("PO-505", Decimal(24)), ("PO-506", Decimal(10)),
    ]


def test_finding_is_traceable_and_neutral(client):
    _load_via_api(client)
    client.post("/detection/runs")
    exc = next(x for x in client.get("/exceptions").json() if x["po_number"] == "PO-505")
    detail = client.get(f"/exceptions/{exc['id']}").json()

    assert detail["case_key"] == "receipt_shortfall:ERP:PO-505:1"
    assert detail["status"] == "OPEN"
    assert detail["rule_version"] == "receipt_shortfall/v1"
    assert detail["mapping_version"].startswith("map-")
    assert detail["canonical_product_id"] == "SKU-002"
    assert detail["canonical_location_id"] == "STORE-015"
    assert detail["detected_at"].startswith("2026-10-16T09:30:00")  # frozen time, UTC

    ev = detail["evidence"]
    assert ev["po_line"]["source_record_id"] == "ERP-PO505-L1"
    assert ev["po_line"]["ordered_uom"] == "BOX" and ev["po_line"]["ordered_qty_base"] == "120"
    assert [r["source_record_id"] for r in ev["receipts_counted"]] == ["WMS-GRN505-A-L1"]
    assert all(r["import_id"] for r in [ev["po_line"], *ev["receipts_counted"]])

    sources = {r["source_record_id"]: r for r in detail["source_records"]}
    assert sources["ERP-PO505-L1"]["import_filename"] == "purchase_order_lines.csv"
    assert sources["ERP-PO505-L1"]["row_number"] == 6
    assert sources["WMS-GRN505-A-L1"]["dataset"] == "goods_receipt_lines"

    text = " ".join([detail["explanation"]["summary"], *detail["explanation"]["known"]]).lower()
    assert "supplier fault" not in text and "supplier short" not in text
    assert any("dispatch" in u.lower() for u in detail["explanation"]["unknown"])
    assert "does not claim supplier fault" in detail["explanation"]["not_asserted"]


def test_po_506_counts_repeated_receipt_once(client):
    _load_via_api(client)
    client.post("/detection/runs")
    exc = next(x for x in client.get("/exceptions").json() if x["po_number"] == "PO-506")
    assert _num(exc["received_qty"]) == 10 and _num(exc["variance_qty"]) == 10
    detail = client.get(f"/exceptions/{exc['id']}").json()
    assert len(detail["evidence"]["receipts_counted"]) == 1


def test_data_quality_is_clean_for_fixture_pack(client):
    _load_via_api(client)
    client.post("/detection/runs")
    dq = client.get("/data-quality").json()
    assert dq["rejected_rows"] == [] and dq["open_conflicts"] == []
    assert dq["unmapped_records"] == [] and dq["not_evaluable_po_lines"] == []


def test_regional_employee_keeps_region_scope_without_store(env):
    from sqlalchemy import select

    from app.db.models import Employee

    env.load_all()
    with env.session() as s:
        emp = {e.employee_id: e for e in s.scalars(select(Employee))}
    assert emp["HR-501"].canonical_location_id is None
    assert emp["HR-501"].region_id == "REGION-MH"
    assert emp["HR-501"].mapping_issues == []
    assert emp["HR-102"].canonical_location_id == "STORE-014"  # ST-014 mapped via HRMS alias
    assert emp["HR-401"].canonical_location_id == "STORE-015"
