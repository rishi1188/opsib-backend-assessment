"""Run the shortfall rule over canonical facts and persist findings and tasks.

Idempotency: an exception's identity is its business key
``receipt_shortfall:<source system>:<po_number>:<line>`` (unique per tenant),
and its ID is a UUIDv5 of (tenant, key), so re-running detection updates the
same row instead of creating a new one. The investigation task is keyed the
same way. The run executes in one transaction under the tenant pipeline lock.
"""

import uuid
from collections import Counter, defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.config import Settings
from app.db.models import (
    DetectionRun,
    ExceptionCase,
    ImportBatch,
    LineEvaluation,
    PoLine,
    ReceiptLine,
    SourceRecord,
    SourceRecordConflict,
)
from app.db.session import acquire_tenant_lock, transaction
from app.detection.explain import build_explanation
from app.detection.shortfall import (
    RULE_ID,
    CANCELLED,
    NO_VARIANCE,
    OVER_RECEIVED,
    RULE_VERSION,
    SHORTFALL,
    Evaluation,
    PoLineFact,
    ReceiptFact,
    SourceRef,
    evaluate_po_line,
)
from app.logging_config import get_logger
from app.mapping.projector import load_alias_index
from app.workflows.service import cancel_open_tasks, ensure_investigation_task, exception_event

log = get_logger("detection")

CLEARING_OUTCOMES = frozenset({NO_VARIANCE, OVER_RECEIVED, CANCELLED})

EXCEPTION_NAMESPACE = uuid.UUID("6f1c2a1e-5b0e-4f7e-9c7a-0d5e3b8a2f10")


def case_key(po: PoLineFact) -> str:
    return f"{RULE_ID}:{po.ref.source_system}:{po.po_number}:{po.line_number}"


def exception_id_for(tenant_id: str, key: str) -> str:
    return str(uuid.uuid5(EXCEPTION_NAMESPACE, f"{tenant_id}|{key}"))


def _ref(rec: SourceRecord) -> SourceRef:
    return SourceRef(rec.source_system, rec.source_record_id, rec.first_import_id, rec.content_hash)


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(Decimal(value).normalize(), "f")


def load_facts(session: Session, tenant_id: str) -> tuple[list[PoLineFact], list[tuple[str, int, ReceiptFact]]]:
    conflicted = set(
        session.scalars(
            select(SourceRecordConflict.source_record_pk).where(
                SourceRecordConflict.tenant_id == tenant_id, SourceRecordConflict.status == "OPEN"
            )
        )
    )
    pos = [
        PoLineFact(
            ref=_ref(p.source_record),
            po_number=p.po_number,
            line_number=p.line_number,
            supplier_code=p.supplier_code,
            canonical_location_id=p.canonical_location_id,
            canonical_product_id=p.canonical_product_id,
            ordered_qty=p.ordered_qty,
            ordered_uom=p.ordered_uom,
            ordered_qty_base=p.ordered_qty_base,
            base_uom=p.base_uom,
            status=p.status,
            expected_receipt_by=p.expected_receipt_by,
            mapping_issues=tuple(p.mapping_issues or ()),
            has_open_conflict=p.source_record_pk in conflicted,
        )
        for p in session.scalars(select(PoLine).where(PoLine.tenant_id == tenant_id))
    ]
    receipts = [
        (
            r.po_number,
            r.po_line_number,
            ReceiptFact(
                ref=_ref(r.source_record),
                receipt_number=r.receipt_number,
                receipt_line=r.receipt_line,
                canonical_location_id=r.canonical_location_id,
                canonical_product_id=r.canonical_product_id,
                accepted_qty=r.accepted_qty,
                uom=r.uom,
                accepted_qty_base=r.accepted_qty_base,
                base_uom=r.base_uom,
                received_at=r.received_at,
                posting_status=r.posting_status,
                mapping_issues=tuple(r.mapping_issues or ()),
                has_open_conflict=r.source_record_pk in conflicted,
            ),
        )
        for r in session.scalars(select(ReceiptLine).where(ReceiptLine.tenant_id == tenant_id))
    ]
    return pos, receipts


def _receipt_evidence(r: ReceiptFact, excluded_reason: str | None = None) -> dict:
    item = {
        "source_system": r.ref.source_system,
        "source_record_id": r.ref.source_record_id,
        "import_id": r.ref.import_id,
        "content_hash": r.ref.content_hash,
        "receipt_number": r.receipt_number,
        "receipt_line": r.receipt_line,
        "accepted_qty": _dec(r.accepted_qty),
        "uom": r.uom,
        "accepted_qty_base": _dec(r.accepted_qty_base),
        "received_at": r.received_at.isoformat(),
        "posting_status": r.posting_status,
    }
    if excluded_reason:
        item["excluded_reason"] = excluded_reason
    return item


def build_evidence(po: PoLineFact, ev: Evaluation, settings: Settings, mapping_version: str) -> dict:
    return {
        "po_line": {
            "source_system": po.ref.source_system,
            "source_record_id": po.ref.source_record_id,
            "import_id": po.ref.import_id,
            "content_hash": po.ref.content_hash,
            "ordered_qty": _dec(po.ordered_qty),
            "ordered_uom": po.ordered_uom,
            "ordered_qty_base": _dec(po.ordered_qty_base),
            "status": po.status,
            "expected_receipt_by": po.expected_receipt_by.isoformat(),
        },
        "receipts_counted": [_receipt_evidence(r) for r in ev.counted],
        "receipts_excluded": [_receipt_evidence(r, why) for r, why in ev.excluded],
        "rule": {"rule_version": RULE_VERSION,
                 "tolerance_base_units": _dec(settings.shortfall_tolerance_base_units)},
        "mapping_version": mapping_version,
    }


def run_detection(factory: sessionmaker[Session], clock: Clock, settings: Settings, tenant_id: str) -> str:
    with transaction(factory) as session:
        acquire_tenant_lock(session, tenant_id, "pipeline")
        run_id = _run(session, clock, settings, tenant_id)
    return run_id


def _run(session: Session, clock: Clock, settings: Settings, tenant_id: str) -> str:
    as_of = clock.now()
    index = load_alias_index(session, tenant_id)
    mapping_version = index.version
    pos, receipts = load_facts(session, tenant_id)
    region_by_location = {m.canonical_location_id: m.region_id for m in index.locations.values()}

    by_line: dict[tuple[str, int], list[ReceiptFact]] = defaultdict(list)
    for po_number, line, fact in receipts:
        by_line[(po_number, line)].append(fact)
    key_counts = Counter((p.po_number, p.line_number) for p in pos)
    po_keys = set(key_counts)

    last_number = session.scalar(
        select(func.max(DetectionRun.run_number)).where(DetectionRun.tenant_id == tenant_id)
    ) or 0
    run = DetectionRun(tenant_id=tenant_id, run_number=last_number + 1, rule_version=RULE_VERSION,
                       mapping_version=mapping_version, evaluated_at=as_of, summary={})
    session.add(run)
    session.flush()

    outcomes: Counter[str] = Counter()
    created = updated = cleared = tasks_created = 0
    shortfall_keys: set[str] = set()
    outcome_by_key: dict[str, Evaluation] = {}

    for po in sorted(pos, key=lambda p: (p.po_number, p.line_number, p.ref.source_record_id)):
        ev = evaluate_po_line(po, by_line.get((po.po_number, po.line_number), []), as_of,
                              settings.shortfall_tolerance_base_units,
                              duplicate_po_keys=key_counts[(po.po_number, po.line_number)] > 1)
        outcomes[ev.outcome] += 1
        outcome_by_key[case_key(po)] = ev
        exception_id = None

        if ev.outcome == SHORTFALL:
            key = case_key(po)
            shortfall_keys.add(key)
            exc, was_created, was_updated = _upsert_exception(
                session, clock, settings, tenant_id, run, po, ev, key, mapping_version,
                region_by_location.get(po.canonical_location_id),
            )
            created += was_created
            updated += was_updated
            _, task_created = ensure_investigation_task(session, clock, settings, exc) if exc.status == "OPEN" else (None, False)
            tasks_created += task_created
            exception_id = exc.id

        session.add(
            LineEvaluation(
                run_id=run.id,
                po_number=po.po_number,
                po_line_number=po.line_number,
                outcome=ev.outcome,
                reasons=ev.reasons,
                expected_qty_base=ev.expected_qty,
                received_qty_base=ev.received_qty,
                variance_qty_base=ev.variance_qty,
                base_uom=ev.base_uom,
                exception_id=exception_id,
            )
        )

    # Previously open shortfalls. Clear one only when the rule now gives a
    # definitive "no shortfall" answer (e.g. a late receipt was posted). If the
    # evidence became unmapped, conflicting or missing, the case stays OPEN:
    # "cannot evaluate" is not the same as "resolved".
    for exc in session.scalars(
        select(ExceptionCase).where(ExceptionCase.tenant_id == tenant_id, ExceptionCase.status == "OPEN",
                                    ExceptionCase.exception_type == RULE_ID)
    ):
        if exc.case_key in shortfall_keys:
            continue
        ev = outcome_by_key.get(exc.case_key)
        if ev is None or ev.outcome not in CLEARING_OUTCOMES:
            reasons = ev.reasons if ev else ["PO line no longer present in the source data"]
            last = exc.events[-1] if exc.events else None
            if not (last and last.event_type == "EVALUATION_BLOCKED" and last.data.get("reasons") == reasons):
                exception_event(session, exc, clock, "EVALUATION_BLOCKED", run_id=run.id, reasons=reasons)
            exc.last_run_id = run.id
            continue
        exc.status = "CLEARED"
        exc.last_evaluated_at = as_of
        exc.last_run_id = run.id
        exception_event(session, exc, clock, "CLEARED", run_id=run.id, outcome=ev.outcome,
                        reason="Shortfall no longer detected at re-evaluation")
        cancel_open_tasks(session, clock, exc, "Exception cleared on re-evaluation")
        cleared += 1

    orphans = sorted(f"{k[0]}/{k[1]}" for k in by_line if k not in po_keys)
    run.summary = {
        "outcomes": dict(sorted(outcomes.items())),
        "exceptions_created": created,
        "exceptions_updated": updated,
        "exceptions_cleared": cleared,
        "tasks_created": tasks_created,
        "receipts_without_po_line": orphans,
    }
    log.info("detection_completed", extra={"tenant_id": tenant_id, "run_id": run.id, **run.summary})
    return run.id


def _upsert_exception(session: Session, clock: Clock, settings: Settings, tenant_id: str, run: DetectionRun,
                      po: PoLineFact, ev: Evaluation, key: str, mapping_version: str,
                      region_id: str | None) -> tuple[ExceptionCase, bool, bool]:
    as_of = clock.now()
    evidence = build_evidence(po, ev, settings, mapping_version)
    explanation = build_explanation(po, ev, as_of)
    exc = session.scalar(select(ExceptionCase).where(ExceptionCase.tenant_id == tenant_id,
                                                     ExceptionCase.case_key == key))
    if exc is None:
        exc = ExceptionCase(
            id=exception_id_for(tenant_id, key),
            tenant_id=tenant_id,
            case_key=key,
            exception_type=RULE_ID,
            status="OPEN",
            rule_version=RULE_VERSION,
            mapping_version=mapping_version,
            detected_at=as_of,
            last_evaluated_at=as_of,
            last_run_id=run.id,
            po_number=po.po_number,
            po_line_number=po.line_number,
            supplier_code=po.supplier_code,
            canonical_product_id=po.canonical_product_id,
            canonical_location_id=po.canonical_location_id,
            region_id=region_id,
            base_uom=po.base_uom,
            expected_qty=ev.expected_qty,
            received_qty=ev.received_qty,
            variance_qty=ev.variance_qty,
            expected_receipt_by=po.expected_receipt_by,
            evidence=evidence,
            explanation=explanation,
        )
        session.add(exc)
        session.flush()
        exception_event(session, exc, clock, "DETECTED", run_id=run.id,
                        variance_qty=_dec(ev.variance_qty), base_uom=po.base_uom)
        return exc, True, False

    exc.last_run_id = run.id
    exc.last_evaluated_at = as_of
    changed = (
        Decimal(exc.variance_qty) != ev.variance_qty
        or Decimal(exc.received_qty) != ev.received_qty
        or exc.evidence != evidence
    )
    if not changed or exc.status == "RESOLVED":
        # Resolved cases are not silently rewritten; the run's line evaluation
        # still records what the rule saw.
        return exc, False, False

    previous = {"expected_qty": _dec(exc.expected_qty), "received_qty": _dec(exc.received_qty),
                "variance_qty": _dec(exc.variance_qty)}
    exc.expected_qty = ev.expected_qty
    exc.received_qty = ev.received_qty
    exc.variance_qty = ev.variance_qty
    exc.evidence = evidence
    exc.explanation = explanation
    exc.mapping_version = mapping_version
    exc.rule_version = RULE_VERSION
    if exc.status == "CLEARED":
        exc.status = "OPEN"
        exception_event(session, exc, clock, "REOPENED", run_id=run.id)
    exception_event(session, exc, clock, "REEVALUATED", run_id=run.id, previous=previous,
                    current={"expected_qty": _dec(ev.expected_qty), "received_qty": _dec(ev.received_qty),
                             "variance_qty": _dec(ev.variance_qty)})
    return exc, False, True


def evidence_source_records(session: Session, exc: ExceptionCase) -> list[dict]:
    """Resolve evidence references back to the stored raw source rows."""
    refs = [exc.evidence["po_line"], *exc.evidence["receipts_counted"], *exc.evidence["receipts_excluded"]]
    out = []
    for ref in refs:
        dataset = "purchase_order_lines" if ref is exc.evidence["po_line"] else "goods_receipt_lines"
        rec = session.scalar(
            select(SourceRecord).where(
                SourceRecord.tenant_id == exc.tenant_id,
                SourceRecord.dataset == dataset,
                SourceRecord.source_record_id == ref["source_record_id"],
            )
        )
        if rec is None:
            continue
        batch = session.get(ImportBatch, rec.first_import_id)
        out.append(
            {
                "dataset": dataset,
                "source_system": rec.source_system,
                "source_record_id": rec.source_record_id,
                "import_id": rec.first_import_id,
                "import_filename": batch.filename if batch else None,
                "row_number": rec.first_row_number,
                "content_hash": rec.content_hash,
                "payload": rec.payload,
            }
        )
    return out
