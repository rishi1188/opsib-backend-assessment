"""API request and response models."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ImportRowOut(ORM):
    row_number: int
    source_record_id: str | None
    outcome: str
    reasons: list[Any]
    raw: dict[str, Any]


class ImportOut(ORM):
    id: str
    tenant_id: str
    dataset: str
    source_system: str
    filename: str | None
    file_sha256: str
    status: str
    error: str | None
    total_rows: int
    accepted_rows: int
    duplicate_rows: int
    conflict_rows: int
    rejected_rows: int
    created_at: datetime


class ImportDetailOut(ImportOut):
    rows: list[ImportRowOut]


class DetectionRunOut(ORM):
    id: str
    rule_version: str
    mapping_version: str
    evaluated_at: datetime
    summary: dict[str, Any]


class LineEvaluationOut(ORM):
    po_number: str
    po_line_number: int
    outcome: str
    reasons: list[str]
    expected_qty_base: Decimal | None
    received_qty_base: Decimal | None
    variance_qty_base: Decimal | None
    base_uom: str | None
    exception_id: str | None


class DetectionRunDetailOut(DetectionRunOut):
    evaluations: list[LineEvaluationOut]


class EventOut(ORM):
    event_type: str
    actor: str
    occurred_at: datetime
    data: dict[str, Any]


class TaskEventOut(EventOut):
    from_status: str | None
    to_status: str | None


class AssignmentOut(ORM):
    assignee_id: str | None
    queue: str | None
    policy: str
    reason: str
    candidates: list[Any]
    assigned_by: str
    assigned_at: datetime


class TaskOut(ORM):
    id: str
    task_key: str
    exception_id: str
    task_type: str
    status: str
    location_id: str
    assignee_id: str | None
    queue: str | None
    due_at: datetime
    created_at: datetime
    updated_at: datetime
    completed_by: str | None
    resolution_code: str | None
    evidence_ref: str | None
    verified_by: str | None
    version: int


class TaskDetailOut(TaskOut):
    events: list[TaskEventOut]
    assignments: list[AssignmentOut]


class ExceptionOut(ORM):
    id: str
    case_key: str
    exception_type: str
    status: str
    rule_version: str
    mapping_version: str
    detected_at: datetime
    po_number: str
    po_line_number: int
    supplier_code: str
    canonical_product_id: str
    canonical_location_id: str
    region_id: str | None
    base_uom: str
    expected_qty: Decimal
    received_qty: Decimal
    variance_qty: Decimal
    expected_receipt_by: datetime
    resolution_code: str | None
    resolved_at: datetime | None


class ExceptionDetailOut(ExceptionOut):
    evidence: dict[str, Any]
    explanation: dict[str, Any]
    source_records: list[dict[str, Any]]
    tasks: list[TaskOut]
    events: list[EventOut]


class CompleteRequest(BaseModel):
    resolution_code: str
    evidence_ref: str = Field(min_length=1, max_length=512)
    note: str | None = Field(default=None, max_length=2000)


class ReassignRequest(BaseModel):
    assignee_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class VerifyRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
