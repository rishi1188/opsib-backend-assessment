"""Persistence model.

Three layers, kept separate on purpose:

1. Raw input: ``ImportBatch``, ``ImportRowResult``, ``SourceRecord`` and
   ``SourceRecordConflict``. A source record is one row as the source system
   sent it, keyed by (tenant, dataset, source_record_id) and fingerprinted by a
   content hash. It is never overwritten.
2. Canonical facts: alias tables, ``PoLine``, ``ReceiptLine``, ``Employee`` and
   ``EmployeeAvailability``. These are a deterministic projection of the raw
   layer through the alias mappings, rebuilt after every import.
3. Business state: ``DetectionRun``, ``LineEvaluation``, ``ExceptionCase``,
   ``Task`` and their event/assignment history. Only this layer carries
   workflow state, and it references source records by their identity rather
   than by canonical row keys, so a projection rebuild never orphans evidence.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime

QTY = Numeric(18, 4)


def new_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# Raw input layer
# --------------------------------------------------------------------------


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    dataset: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    accepted_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    conflict_rows: Mapped[int] = mapped_column(Integer, default=0)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())

    rows: Mapped[list["ImportRowResult"]] = relationship(
        back_populates="batch", order_by="ImportRowResult.row_number"
    )


class ImportRowResult(Base):
    """Outcome for every input row, including rejected ones."""

    __tablename__ = "import_row_result"
    __table_args__ = (UniqueConstraint("import_id", "row_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    source_record_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(16))  # ACCEPTED/DUPLICATE/CONFLICT/REJECTED
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")


class SourceRecord(Base):
    __tablename__ = "source_record"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dataset", "source_record_id"),
        Index("ix_source_record_tenant_dataset", "tenant_id", "dataset"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64))
    dataset: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    source_record_id: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    first_import_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"))
    first_row_number: Mapped[int] = mapped_column(Integer)
    last_seen_import_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SourceRecordConflict(Base):
    """Same source ID re-sent with different business content.

    The original record is kept as-is; the incoming version waits for review.
    While a conflict is OPEN, anything computed from that record is reported as
    not evaluable instead of being silently recomputed.
    """

    __tablename__ = "source_record_conflict"
    __table_args__ = (UniqueConstraint("source_record_pk", "incoming_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_record_pk: Mapped[str] = mapped_column(ForeignKey("source_record.id"))
    import_id: Mapped[str] = mapped_column(ForeignKey("import_batch.id"))
    row_number: Mapped[int] = mapped_column(Integer)
    incoming_hash: Mapped[str] = mapped_column(String(64))
    incoming_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    changed_fields: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())

    source_record: Mapped[SourceRecord] = relationship()


# --------------------------------------------------------------------------
# Canonical layer (projection of raw records through mappings)
# --------------------------------------------------------------------------


class ProductAlias(Base):
    __tablename__ = "product_alias"
    __table_args__ = (UniqueConstraint("tenant_id", "source_system", "external_item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    external_item_id: Mapped[str] = mapped_column(String(128))
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    source_uom: Mapped[str] = mapped_column(String(16))
    base_uom: Mapped[str] = mapped_column(String(16))
    to_base_factor: Mapped[Decimal] = mapped_column(QTY)
    source_record_pk: Mapped[str] = mapped_column(ForeignKey("source_record.id"))


class LocationAlias(Base):
    __tablename__ = "location_alias"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "external_location_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32))
    external_location_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))
    source_record_pk: Mapped[str] = mapped_column(ForeignKey("source_record.id"))


class PoLine(Base):
    __tablename__ = "po_line"
    __table_args__ = (UniqueConstraint("source_record_pk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_record_pk: Mapped[str] = mapped_column(ForeignKey("source_record.id"))
    po_number: Mapped[str] = mapped_column(String(64))
    line_number: Mapped[int] = mapped_column(Integer)
    supplier_code: Mapped[str] = mapped_column(String(64))
    source_location_code: Mapped[str] = mapped_column(String(64))
    source_item_code: Mapped[str] = mapped_column(String(128))
    canonical_location_id: Mapped[str | None] = mapped_column(String(64))
    canonical_product_id: Mapped[str | None] = mapped_column(String(64))
    ordered_qty: Mapped[Decimal] = mapped_column(QTY)
    ordered_uom: Mapped[str] = mapped_column(String(16))
    ordered_qty_base: Mapped[Decimal | None] = mapped_column(QTY)
    base_uom: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    expected_receipt_by: Mapped[datetime] = mapped_column(UTCDateTime())
    mapping_issues: Mapped[list[str]] = mapped_column(JSON, default=list)
    mapping_version: Mapped[str] = mapped_column(String(32))

    source_record: Mapped[SourceRecord] = relationship()


class ReceiptLine(Base):
    __tablename__ = "receipt_line"
    __table_args__ = (UniqueConstraint("source_record_pk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_record_pk: Mapped[str] = mapped_column(ForeignKey("source_record.id"))
    receipt_number: Mapped[str] = mapped_column(String(64))
    receipt_line: Mapped[int] = mapped_column(Integer)
    po_number: Mapped[str] = mapped_column(String(64))
    po_line_number: Mapped[int] = mapped_column(Integer)
    source_location_code: Mapped[str] = mapped_column(String(64))
    source_product_ref: Mapped[str] = mapped_column(String(128))
    canonical_location_id: Mapped[str | None] = mapped_column(String(64))
    canonical_product_id: Mapped[str | None] = mapped_column(String(64))
    accepted_qty: Mapped[Decimal] = mapped_column(QTY)
    uom: Mapped[str] = mapped_column(String(16))
    accepted_qty_base: Mapped[Decimal | None] = mapped_column(QTY)
    base_uom: Mapped[str | None] = mapped_column(String(16))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    posting_status: Mapped[str] = mapped_column(String(32))
    mapping_issues: Mapped[list[str]] = mapped_column(JSON, default=list)
    mapping_version: Mapped[str] = mapped_column(String(32))

    source_record: Mapped[SourceRecord] = relationship()


class Employee(Base):
    __tablename__ = "employee"
    __table_args__ = (UniqueConstraint("tenant_id", "employee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    employee_id: Mapped[str] = mapped_column(String(64))
    source_record_pk: Mapped[str] = mapped_column(ForeignKey("source_record.id"))
    display_name: Mapped[str] = mapped_column(String(255))
    employment_status: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(64))
    source_location_code: Mapped[str | None] = mapped_column(String(64))
    canonical_location_id: Mapped[str | None] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))
    manager_employee_id: Mapped[str | None] = mapped_column(String(64))
    mapping_issues: Mapped[list[str]] = mapped_column(JSON, default=list)


class EmployeeAvailability(Base):
    __tablename__ = "employee_availability"
    __table_args__ = (UniqueConstraint("tenant_id", "employee_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    employee_id: Mapped[str] = mapped_column(String(64))
    source_record_pk: Mapped[str] = mapped_column(ForeignKey("source_record.id"))
    availability_status: Mapped[str] = mapped_column(String(32))
    shift_start: Mapped[datetime] = mapped_column(UTCDateTime())
    shift_end: Mapped[datetime] = mapped_column(UTCDateTime())
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime())


# --------------------------------------------------------------------------
# Business layer
# --------------------------------------------------------------------------


class DetectionRun(Base):
    __tablename__ = "detection_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    rule_version: Mapped[str] = mapped_column(String(64))
    mapping_version: Mapped[str] = mapped_column(String(32))
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    summary: Mapped[dict[str, Any]] = mapped_column(JSON)

    evaluations: Mapped[list["LineEvaluation"]] = relationship(back_populates="run")


class LineEvaluation(Base):
    """What the rule decided for every PO line in a run, including non-exceptions."""

    __tablename__ = "line_evaluation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("detection_run.id"), index=True)
    po_number: Mapped[str] = mapped_column(String(64))
    po_line_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))
    reasons: Mapped[list[str]] = mapped_column(JSON)
    expected_qty_base: Mapped[Decimal | None] = mapped_column(QTY)
    received_qty_base: Mapped[Decimal | None] = mapped_column(QTY)
    variance_qty_base: Mapped[Decimal | None] = mapped_column(QTY)
    base_uom: Mapped[str | None] = mapped_column(String(16))
    exception_id: Mapped[str | None] = mapped_column(String(36))

    run: Mapped[DetectionRun] = relationship(back_populates="evaluations")


class ExceptionCase(Base):
    __tablename__ = "exception_case"
    __table_args__ = (UniqueConstraint("tenant_id", "case_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    case_key: Mapped[str] = mapped_column(String(255))
    exception_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))  # OPEN / CLEARED / RESOLVED
    rule_version: Mapped[str] = mapped_column(String(64))
    mapping_version: Mapped[str] = mapped_column(String(32))
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_run_id: Mapped[str] = mapped_column(ForeignKey("detection_run.id"))
    po_number: Mapped[str] = mapped_column(String(64))
    po_line_number: Mapped[int] = mapped_column(Integer)
    supplier_code: Mapped[str] = mapped_column(String(64))
    canonical_product_id: Mapped[str] = mapped_column(String(64))
    canonical_location_id: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str | None] = mapped_column(String(64))
    base_uom: Mapped[str] = mapped_column(String(16))
    expected_qty: Mapped[Decimal] = mapped_column(QTY)
    received_qty: Mapped[Decimal] = mapped_column(QTY)
    variance_qty: Mapped[Decimal] = mapped_column(QTY)
    expected_receipt_by: Mapped[datetime] = mapped_column(UTCDateTime())
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON)
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    events: Mapped[list["ExceptionEvent"]] = relationship(
        order_by="ExceptionEvent.id", back_populates="exception"
    )
    tasks: Mapped[list["Task"]] = relationship(back_populates="exception")


class ExceptionEvent(Base):
    __tablename__ = "exception_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exception_id: Mapped[str] = mapped_column(ForeignKey("exception_case.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    exception: Mapped[ExceptionCase] = relationship(back_populates="events")


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_key"),
        CheckConstraint(
            "status IN ('CREATED','ASSIGNED','ACKNOWLEDGED','IN_PROGRESS','BLOCKED',"
            "'COMPLETED','PENDING_VERIFICATION','VERIFIED','CANCELLED')",
            name="status_valid",
        ),
        Index("ix_task_assignee_status", "tenant_id", "assignee_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(64))
    task_key: Mapped[str] = mapped_column(String(255))
    exception_id: Mapped[str] = mapped_column(ForeignKey("exception_case.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    location_id: Mapped[str] = mapped_column(String(64))
    assignee_id: Mapped[str | None] = mapped_column(String(64))
    queue: Mapped[str | None] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_by: Mapped[str | None] = mapped_column(String(64))
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    evidence_ref: Mapped[str | None] = mapped_column(String(512))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    exception: Mapped[ExceptionCase] = relationship(back_populates="tasks")
    events: Mapped[list["TaskEvent"]] = relationship(order_by="TaskEvent.id")
    assignments: Mapped[list["TaskAssignment"]] = relationship(order_by="TaskAssignment.id")

    __mapper_args__ = {"version_id_col": version}


class TaskAssignment(Base):
    """Every assignment decision, with the policy reasoning behind it."""

    __tablename__ = "task_assignment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), index=True)
    assignee_id: Mapped[str | None] = mapped_column(String(64))
    queue: Mapped[str | None] = mapped_column(String(64))
    policy: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    assigned_by: Mapped[str] = mapped_column(String(64))
    assigned_at: Mapped[datetime] = mapped_column(UTCDateTime())


class TaskEvent(Base):
    __tablename__ = "task_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime())
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class IdempotencyRecord(Base):
    """Stored outcome of a client command, keyed by its Idempotency-Key."""

    __tablename__ = "idempotency_record"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    operation: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
