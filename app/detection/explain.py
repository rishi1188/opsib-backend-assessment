"""Neutral, deterministic explanation attached to every shortfall finding.

The fixture data has no supplier dispatch / ASN, carrier or proof-of-delivery
records, so a finding can only state that fewer units were *accepted into the
warehouse system* than the PO line ordered. It never attributes cause.
"""

from datetime import datetime

from app.detection.shortfall import Evaluation, PoLineFact


def build_explanation(po: PoLineFact, ev: Evaluation, as_of: datetime) -> dict:
    receipts = ", ".join(
        f"{r.receipt_number}/{r.receipt_line} ({r.accepted_qty_base} {r.base_uom})" for r in ev.counted
    ) or "none"
    conversion = ""
    if po.ordered_uom != po.base_uom:
        conversion = f" ({po.ordered_qty} {po.ordered_uom} converted to {po.ordered_qty_base} {po.base_uom})"
    return {
        "summary": (
            f"Receipt variance: PO {po.po_number} line {po.line_number} expected "
            f"{ev.expected_qty} {ev.base_uom}{conversion}; posted receipts accepted "
            f"{ev.received_qty} {ev.base_uom} as of {as_of.isoformat()}. "
            f"Variance: {ev.variance_qty} {ev.base_uom}."
        ),
        "known": [
            f"ERP PO line {po.ref.source_record_id} ordered {po.ordered_qty} {po.ordered_uom} "
            f"with status {po.status} and deadline {po.expected_receipt_by.isoformat()}.",
            f"Posted WMS receipt lines counted: {receipts}.",
            "The expected-receipt deadline had passed at the evaluation time.",
        ],
        "unknown": [
            "Whether the supplier dispatched fewer units than ordered (no dispatch/ASN data).",
            "Whether stock was lost or damaged in transit (no carrier or proof-of-delivery data).",
            "Whether units arrived but were not yet received or posted in WMS.",
            "Where in the chain the discrepancy occurred.",
        ],
        "evidence_needed": [
            "Supplier dispatch note / ASN quantities for this PO line.",
            "Carrier proof of delivery and any damage or refusal notes.",
            "Store or warehouse check for unposted or mis-keyed receipts.",
        ],
        "not_asserted": "This finding does not claim supplier fault, carrier loss or financial loss.",
    }
