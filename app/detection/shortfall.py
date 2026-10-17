"""Receipt-shortfall rule (pure, deterministic, no database access).

For one canonical PO line and the receipt lines linked to it by
(po_number, po_line_number), decide whether there is an actionable receipt
shortfall at ``as_of``. Grain: one evaluation per PO line; receipts are summed
per distinct source record, so a repeated source row is counted once and split
GRNs are added together.

Outcomes:
  SHORTFALL       accepted < expected (beyond tolerance) after the deadline
  NO_VARIANCE     accepted == expected (within tolerance)
  OVER_RECEIVED   accepted > expected; reported, not an exception under this rule
  NOT_DUE         the expected-receipt deadline has not passed at ``as_of``
  CANCELLED       the PO line is cancelled in ERP
  NOT_EVALUABLE   evidence is unmapped, conflicting or ambiguous -> data quality
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

RULE_ID = "receipt_shortfall"
RULE_VERSION = "receipt_shortfall/v1"

SHORTFALL = "SHORTFALL"
NO_VARIANCE = "NO_VARIANCE"
OVER_RECEIVED = "OVER_RECEIVED"
NOT_DUE = "NOT_DUE"
CANCELLED = "CANCELLED"
NOT_EVALUABLE = "NOT_EVALUABLE"


@dataclass(frozen=True)
class SourceRef:
    source_system: str
    source_record_id: str
    import_id: str
    content_hash: str


@dataclass(frozen=True)
class PoLineFact:
    ref: SourceRef
    po_number: str
    line_number: int
    supplier_code: str
    canonical_location_id: str | None
    canonical_product_id: str | None
    ordered_qty: Decimal
    ordered_uom: str
    ordered_qty_base: Decimal | None
    base_uom: str | None
    status: str
    expected_receipt_by: datetime
    mapping_issues: tuple[str, ...] = ()
    has_open_conflict: bool = False


@dataclass(frozen=True)
class ReceiptFact:
    ref: SourceRef
    receipt_number: str
    receipt_line: int
    canonical_location_id: str | None
    canonical_product_id: str | None
    accepted_qty: Decimal
    uom: str
    accepted_qty_base: Decimal | None
    base_uom: str | None
    received_at: datetime
    posting_status: str
    mapping_issues: tuple[str, ...] = ()
    has_open_conflict: bool = False


@dataclass
class Evaluation:
    outcome: str
    reasons: list[str] = field(default_factory=list)
    expected_qty: Decimal | None = None
    received_qty: Decimal | None = None
    variance_qty: Decimal | None = None
    base_uom: str | None = None
    counted: list[ReceiptFact] = field(default_factory=list)
    excluded: list[tuple[ReceiptFact, str]] = field(default_factory=list)


def evaluate_po_line(
    po: PoLineFact,
    receipts: list[ReceiptFact],
    as_of: datetime,
    tolerance: Decimal = Decimal("0"),
    duplicate_po_keys: bool = False,
) -> Evaluation:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    if po.has_open_conflict:
        return Evaluation(NOT_EVALUABLE, [f"CONFLICTING_SOURCE_RECORD:{po.ref.source_record_id}"])
    if duplicate_po_keys:
        return Evaluation(
            NOT_EVALUABLE,
            [f"AMBIGUOUS_PO_LINE:{po.po_number}/{po.line_number} has more than one source record"],
        )
    if po.status == "CANCELLED":
        return Evaluation(CANCELLED, ["PO line is CANCELLED in ERP; no receipt is expected"])
    if po.mapping_issues or po.ordered_qty_base is None:
        return Evaluation(NOT_EVALUABLE, list(po.mapping_issues) or ["PO quantity not in base unit"])

    result = Evaluation(outcome="", expected_qty=po.ordered_qty_base, base_uom=po.base_uom)

    blocking: list[str] = []
    seen_ids: set[str] = set()
    seen_keys: dict[tuple[str, int], str] = {}
    for receipt in sorted(receipts, key=lambda r: r.ref.source_record_id):
        rid = receipt.ref.source_record_id
        if rid in seen_ids:  # defensive: the raw layer already enforces this
            continue
        seen_ids.add(rid)
        business_key = (receipt.receipt_number, receipt.receipt_line)
        if business_key in seen_keys:
            blocking.append(
                f"AMBIGUOUS_RECEIPT_LINE:{receipt.receipt_number}/{receipt.receipt_line} "
                f"appears under source IDs {seen_keys[business_key]} and {rid}"
            )
            continue
        seen_keys[business_key] = rid

        if receipt.posting_status != "POSTED":
            result.excluded.append((receipt, f"posting_status={receipt.posting_status}"))
            continue
        if receipt.received_at > as_of:
            result.excluded.append((receipt, "received after the evaluation time"))
            continue
        if receipt.has_open_conflict:
            blocking.append(f"CONFLICTING_SOURCE_RECORD:{rid}")
            continue
        if receipt.mapping_issues or receipt.accepted_qty_base is None:
            blocking.extend(receipt.mapping_issues or [f"RECEIPT_NOT_IN_BASE_UNIT:{rid}"])
            continue
        if receipt.canonical_product_id != po.canonical_product_id:
            blocking.append(
                f"PRODUCT_MISMATCH:{rid} is {receipt.canonical_product_id}, "
                f"PO line is {po.canonical_product_id}"
            )
            continue
        if receipt.canonical_location_id != po.canonical_location_id:
            blocking.append(
                f"LOCATION_MISMATCH:{rid} is {receipt.canonical_location_id}, "
                f"PO line is {po.canonical_location_id}"
            )
            continue
        if receipt.base_uom != po.base_uom:
            blocking.append(f"BASE_UOM_MISMATCH:{rid} {receipt.base_uom}!={po.base_uom}")
            continue
        result.counted.append(receipt)

    received = sum((r.accepted_qty_base for r in result.counted), Decimal("0"))
    result.received_qty = received
    result.variance_qty = po.ordered_qty_base - received

    if as_of <= po.expected_receipt_by:
        result.outcome = NOT_DUE
        result.reasons.append(
            f"Expected-receipt deadline {po.expected_receipt_by.isoformat()} has not passed "
            f"at evaluation time {as_of.isoformat()}"
        )
        return result
    if blocking:
        result.outcome = NOT_EVALUABLE
        result.reasons.extend(blocking)
        return result

    if result.variance_qty > tolerance:
        result.outcome = SHORTFALL
        result.reasons.append(
            f"Accepted {received} {po.base_uom} against {po.ordered_qty_base} {po.base_uom} "
            f"expected after the deadline passed"
        )
    elif received > po.ordered_qty_base:
        result.outcome = OVER_RECEIVED
        result.reasons.append("Accepted quantity exceeds the ordered quantity")
    else:
        result.outcome = NO_VARIANCE
        result.reasons.append("Accepted quantity matches the ordered quantity")
    return result
