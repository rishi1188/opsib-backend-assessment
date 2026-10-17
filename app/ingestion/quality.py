"""Data-quality report: everything that blocks or weakens a business decision."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DetectionRun,
    Employee,
    ImportBatch,
    ImportRowResult,
    LineEvaluation,
    PoLine,
    ReceiptLine,
    SourceRecordConflict,
)
from app.detection.shortfall import NOT_EVALUABLE


def data_quality_report(session: Session, tenant_id: str) -> dict:
    rejected = session.execute(
        select(ImportRowResult, ImportBatch)
        .join(ImportBatch, ImportRowResult.import_id == ImportBatch.id)
        .where(ImportBatch.tenant_id == tenant_id, ImportRowResult.outcome == "REJECTED")
        .order_by(ImportBatch.created_at, ImportRowResult.import_id, ImportRowResult.row_number)
    ).all()
    failed_imports = session.scalars(
        select(ImportBatch).where(ImportBatch.tenant_id == tenant_id, ImportBatch.status == "FAILED")
    ).all()
    conflicts = session.scalars(
        select(SourceRecordConflict).where(
            SourceRecordConflict.tenant_id == tenant_id, SourceRecordConflict.status == "OPEN"
        )
    ).all()

    unmapped = []
    for model, kind, label in (
        (PoLine, "purchase_order_line", lambda r: f"{r.po_number}/{r.line_number}"),
        (ReceiptLine, "goods_receipt_line", lambda r: f"{r.receipt_number}/{r.receipt_line}"),
        (Employee, "employee", lambda r: r.employee_id),
    ):
        for row in session.scalars(select(model).where(model.tenant_id == tenant_id)):
            if row.mapping_issues:
                unmapped.append({"record_type": kind, "record": label(row), "issues": row.mapping_issues})

    latest_run = session.scalar(
        select(DetectionRun).where(DetectionRun.tenant_id == tenant_id).order_by(DetectionRun.evaluated_at.desc(), DetectionRun.id.desc()).limit(1)
    )
    not_evaluable = []
    orphan_receipts: list[str] = []
    if latest_run is not None:
        not_evaluable = [
            {"po_number": e.po_number, "po_line_number": e.po_line_number, "reasons": e.reasons}
            for e in session.scalars(
                select(LineEvaluation).where(
                    LineEvaluation.run_id == latest_run.id, LineEvaluation.outcome == NOT_EVALUABLE
                )
            )
        ]
        orphan_receipts = latest_run.summary.get("receipts_without_po_line", [])

    return {
        "failed_imports": [
            {"import_id": b.id, "dataset": b.dataset, "filename": b.filename, "error": b.error}
            for b in failed_imports
        ],
        "rejected_rows": [
            {
                "import_id": b.id,
                "dataset": b.dataset,
                "row_number": r.row_number,
                "source_record_id": r.source_record_id,
                "reasons": r.reasons,
            }
            for r, b in rejected
        ],
        "open_conflicts": [
            {
                "conflict_id": c.id,
                "dataset": c.source_record.dataset,
                "source_record_id": c.source_record.source_record_id,
                "original_import_id": c.source_record.first_import_id,
                "incoming_import_id": c.import_id,
                "incoming_row_number": c.row_number,
                "changed_fields": c.changed_fields,
            }
            for c in conflicts
        ],
        "unmapped_records": unmapped,
        "not_evaluable_po_lines": not_evaluable,
        "receipts_without_po_line": orphan_receipts,
        "latest_detection_run_id": latest_run.id if latest_run else None,
    }
