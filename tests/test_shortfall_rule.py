"""Unit tests for the pure shortfall rule: partial receipts, deadlines,
cancellation, duplicates, posting status and ambiguous evidence."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.detection.shortfall import (
    CANCELLED,
    NO_VARIANCE,
    NOT_DUE,
    NOT_EVALUABLE,
    OVER_RECEIVED,
    SHORTFALL,
    PoLineFact,
    ReceiptFact,
    SourceRef,
    evaluate_po_line,
)

AS_OF = datetime.fromisoformat("2026-10-16T15:00:00+05:30")
DEADLINE = datetime.fromisoformat("2026-10-15T12:00:00+05:30")


def po(**kw) -> PoLineFact:
    base = dict(
        ref=SourceRef("ERP", "PO-L1", "imp", "h"), po_number="PO-1", line_number=1, supplier_code="S",
        canonical_location_id="STORE-014", canonical_product_id="SKU-001", ordered_qty=Decimal(100),
        ordered_uom="EA", ordered_qty_base=Decimal(100), base_uom="EA", status="OPEN",
        expected_receipt_by=DEADLINE,
    )
    return PoLineFact(**{**base, **kw})


def rc(rid: str, qty, **kw) -> ReceiptFact:
    base = dict(
        ref=SourceRef("WMS", rid, "imp", "h"), receipt_number=f"G-{rid}", receipt_line=1,
        canonical_location_id="STORE-014", canonical_product_id="SKU-001", accepted_qty=Decimal(qty),
        uom="EA", accepted_qty_base=Decimal(qty), base_uom="EA",
        received_at=DEADLINE - timedelta(hours=1), posting_status="POSTED",
    )
    return ReceiptFact(**{**base, **kw})


def test_partial_receipts_are_summed():
    ev = evaluate_po_line(po(), [rc("A", 60), rc("B", 32)], AS_OF)
    assert ev.outcome == SHORTFALL
    assert (ev.expected_qty, ev.received_qty, ev.variance_qty) == (100, 92, 8)
    assert {r.ref.source_record_id for r in ev.counted} == {"A", "B"}


def test_full_receipt_has_no_variance():
    assert evaluate_po_line(po(), [rc("A", 100)], AS_OF).outcome == NO_VARIANCE


def test_no_receipts_after_deadline_is_full_shortfall():
    ev = evaluate_po_line(po(), [], AS_OF)
    assert ev.outcome == SHORTFALL and ev.variance_qty == 100


def test_deadline_equal_to_evaluation_time_is_not_yet_actionable():
    ev = evaluate_po_line(po(expected_receipt_by=AS_OF), [rc("A", 10)], AS_OF)
    assert ev.outcome == NOT_DUE


def test_deadline_one_second_before_evaluation_time_is_actionable():
    ev = evaluate_po_line(po(expected_receipt_by=AS_OF - timedelta(seconds=1)), [rc("A", 10)], AS_OF)
    assert ev.outcome == SHORTFALL


def test_future_deadline_is_not_due_even_with_partial_receipt():
    ev = evaluate_po_line(po(expected_receipt_by=AS_OF + timedelta(days=1)), [rc("A", 20)], AS_OF)
    assert ev.outcome == NOT_DUE
    assert ev.received_qty == 20  # informational only


def test_cancelled_line_never_raises_shortfall():
    ev = evaluate_po_line(po(status="CANCELLED"), [], AS_OF)
    assert ev.outcome == CANCELLED


def test_repeated_source_record_is_counted_once():
    ev = evaluate_po_line(po(ordered_qty_base=Decimal(20)), [rc("A", 10), rc("A", 10)], AS_OF)
    assert ev.outcome == SHORTFALL and ev.variance_qty == 10


def test_same_receipt_line_under_two_source_ids_is_ambiguous():
    a = rc("A", 10, receipt_number="GRN-1")
    b = rc("B", 10, receipt_number="GRN-1")
    ev = evaluate_po_line(po(), [a, b], AS_OF)
    assert ev.outcome == NOT_EVALUABLE
    assert ev.reasons[0].startswith("AMBIGUOUS_RECEIPT_LINE")


@pytest.mark.parametrize("status", ["PENDING", "REVERSED"])
def test_unposted_receipts_are_excluded(status):
    ev = evaluate_po_line(po(), [rc("A", 60), rc("B", 40, posting_status=status)], AS_OF)
    assert ev.outcome == SHORTFALL and ev.received_qty == 60
    assert ev.excluded[0][1] == f"posting_status={status}"


def test_receipt_after_evaluation_time_is_ignored():
    late = rc("B", 40, received_at=AS_OF + timedelta(minutes=1))
    ev = evaluate_po_line(po(), [rc("A", 60), late], AS_OF)
    assert ev.received_qty == 60


def test_late_receipt_before_evaluation_time_counts():
    late = rc("B", 40, received_at=DEADLINE + timedelta(hours=2))
    assert evaluate_po_line(po(), [rc("A", 60), late], AS_OF).outcome == NO_VARIANCE


def test_unmapped_po_is_data_quality_not_exception():
    ev = evaluate_po_line(po(canonical_product_id=None, ordered_qty_base=None,
                             mapping_issues=("PRODUCT_UNMAPPED:ERP:X",)), [], AS_OF)
    assert ev.outcome == NOT_EVALUABLE and ev.reasons == ["PRODUCT_UNMAPPED:ERP:X"]


def test_unmapped_receipt_blocks_evaluation():
    bad = rc("B", 40, canonical_product_id=None, accepted_qty_base=None, mapping_issues=("PRODUCT_UNMAPPED:WMS:Y",))
    assert evaluate_po_line(po(), [rc("A", 60), bad], AS_OF).outcome == NOT_EVALUABLE


def test_receipt_for_a_different_product_blocks_evaluation():
    ev = evaluate_po_line(po(), [rc("A", 100, canonical_product_id="SKU-999")], AS_OF)
    assert ev.outcome == NOT_EVALUABLE and ev.reasons[0].startswith("PRODUCT_MISMATCH")


def test_receipt_at_a_different_location_blocks_evaluation():
    ev = evaluate_po_line(po(), [rc("A", 100, canonical_location_id="STORE-015")], AS_OF)
    assert ev.outcome == NOT_EVALUABLE and ev.reasons[0].startswith("LOCATION_MISMATCH")


def test_open_conflict_blocks_evaluation():
    assert evaluate_po_line(po(has_open_conflict=True), [], AS_OF).outcome == NOT_EVALUABLE
    assert evaluate_po_line(po(), [rc("A", 10, has_open_conflict=True)], AS_OF).outcome == NOT_EVALUABLE


def test_duplicate_po_business_key_blocks_evaluation():
    assert evaluate_po_line(po(), [], AS_OF, duplicate_po_keys=True).outcome == NOT_EVALUABLE


def test_tolerance_is_configurable():
    assert evaluate_po_line(po(), [rc("A", 98)], AS_OF, tolerance=Decimal(2)).outcome == NO_VARIANCE
    assert evaluate_po_line(po(), [rc("A", 97)], AS_OF, tolerance=Decimal(2)).outcome == SHORTFALL


def test_over_receipt_is_not_a_shortfall():
    assert evaluate_po_line(po(), [rc("A", 110)], AS_OF).outcome == OVER_RECEIVED


def test_naive_evaluation_time_is_refused():
    with pytest.raises(ValueError):
        evaluate_po_line(po(), [], datetime(2026, 10, 16, 15))
