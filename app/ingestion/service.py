"""Import pipeline: parse, validate, deduplicate, persist, re-project.

Idempotency and concurrency
---------------------------
* ``source_record`` has a unique constraint on
  (tenant_id, dataset, source_record_id). A source fact can exist once.
* Each row is fingerprinted with a SHA-256 of its normalised business content.
  Same ID + same hash  -> DUPLICATE (no effect).
  Same ID + other hash -> CONFLICT (original kept, incoming parked for review).
* The whole import (row results, new source records, conflicts and the
  canonical rebuild) commits in one transaction under a per-tenant advisory
  lock on PostgreSQL. A second worker importing the same file waits, then sees
  every row as DUPLICATE. If two writers still raced past the lock, the unique
  constraint makes one of them fail and roll back, and its retry is a no-op.
"""

import csv
import hashlib
import io
import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import ImportBatch, ImportRowResult, SourceRecord, SourceRecordConflict
from app.db.session import acquire_tenant_lock, transaction
from app.errors import NotFound
from app.ingestion.registry import DATASETS, DatasetSpec
from app.ingestion.schemas import canonical_value
from app.logging_config import get_logger
from app.mapping.projector import rebuild_canonical

log = get_logger("ingestion")

ACCEPTED, DUPLICATE, CONFLICT, REJECTED = "ACCEPTED", "DUPLICATE", "CONFLICT", "REJECTED"


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _format_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "row"
        out.append(f"{loc}: {err['msg']}")
    return out


def get_spec(dataset: str) -> DatasetSpec:
    spec = DATASETS.get(dataset)
    if spec is None:
        raise NotFound(f"Unknown dataset '{dataset}'", {"supported": sorted(DATASETS)})
    return spec


def import_dataset(
    factory: sessionmaker[Session],
    clock: Clock,
    tenant_id: str,
    dataset: str,
    content: bytes,
    filename: str | None = None,
) -> str:
    """Import one CSV export. Returns the import ID; the batch is always persisted."""
    spec = get_spec(dataset)
    file_sha = hashlib.sha256(content).hexdigest()

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _record_failed(factory, clock, tenant_id, spec, filename, file_sha, "File is not valid UTF-8")

    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in spec.required_columns if c not in header]
    if missing:
        return _record_failed(
            factory, clock, tenant_id, spec, filename, file_sha,
            f"Missing required columns: {', '.join(missing)}",
        )
    reader.fieldnames = header

    try:
        with transaction(factory) as session:
            acquire_tenant_lock(session, tenant_id, "pipeline")
            batch = ImportBatch(
                tenant_id=tenant_id,
                dataset=spec.name,
                source_system=spec.source_system,
                filename=filename,
                file_sha256=file_sha,
                status="PROCESSING",
                created_at=clock.now(),
            )
            session.add(batch)
            session.flush()
            _process_rows(session, clock, tenant_id, spec, batch, reader)
            rebuild_canonical(session, tenant_id)
            batch.status = (
                "COMPLETED_WITH_ISSUES" if batch.rejected_rows or batch.conflict_rows else "COMPLETED"
            )
            import_id = batch.id
            counts = _counts(batch)
    except Exception as exc:  # noqa: BLE001 - recorded and surfaced, never swallowed
        log.exception("import_failed", extra={"tenant_id": tenant_id, "dataset": dataset})
        return _record_failed(factory, clock, tenant_id, spec, filename, file_sha, f"{type(exc).__name__}: {exc}")

    log.info("import_completed", extra={"tenant_id": tenant_id, "dataset": dataset, "import_id": import_id, **counts})
    return import_id


def _counts(batch: ImportBatch) -> dict[str, int]:
    return {
        "total_rows": batch.total_rows,
        "accepted_rows": batch.accepted_rows,
        "duplicate_rows": batch.duplicate_rows,
        "conflict_rows": batch.conflict_rows,
        "rejected_rows": batch.rejected_rows,
    }


def _process_rows(
    session: Session,
    clock: Clock,
    tenant_id: str,
    spec: DatasetSpec,
    batch: ImportBatch,
    reader: csv.DictReader,
) -> None:
    existing: dict[str, SourceRecord] = {
        rec.source_record_id: rec
        for rec in session.scalars(
            select(SourceRecord).where(
                SourceRecord.tenant_id == tenant_id, SourceRecord.dataset == spec.name
            )
        )
    }

    for row_number, raw in enumerate(reader, start=2):  # line 1 is the header
        raw = {k: v for k, v in raw.items() if k is not None}
        batch.total_rows += 1
        result = ImportRowResult(import_id=batch.id, row_number=row_number, raw=raw, reasons=[])

        try:
            row = spec.row_model.model_validate(raw)
        except ValidationError as exc:
            result.outcome = REJECTED
            result.reasons = _format_errors(exc)
            result.source_record_id = (raw.get("source_record_id") or "").strip() or None
            batch.rejected_rows += 1
            session.add(result)
            continue

        payload = {k: canonical_value(v) for k, v in row.model_dump().items()}
        digest = content_hash(payload)
        key = spec.record_key(row)
        result.source_record_id = key
        current = existing.get(key)

        if current is None:
            record = SourceRecord(
                tenant_id=tenant_id,
                dataset=spec.name,
                source_system=spec.source_system,
                source_record_id=key,
                content_hash=digest,
                payload=payload,
                first_import_id=batch.id,
                first_row_number=row_number,
                last_seen_import_id=batch.id,
                created_at=clock.now(),
            )
            session.add(record)
            session.flush()
            existing[key] = record
            result.outcome = ACCEPTED
            batch.accepted_rows += 1
        elif current.content_hash == digest:
            current.last_seen_import_id = batch.id
            result.outcome = DUPLICATE
            result.reasons = [
                f"Identical to source record first seen in import {current.first_import_id} "
                f"row {current.first_row_number}; ignored"
            ]
            batch.duplicate_rows += 1
        else:
            changed = sorted(k for k in payload if payload.get(k) != current.payload.get(k))
            already = session.scalar(
                select(SourceRecordConflict).where(
                    SourceRecordConflict.source_record_pk == current.id,
                    SourceRecordConflict.incoming_hash == digest,
                )
            )
            if already is None:
                session.add(
                    SourceRecordConflict(
                        tenant_id=tenant_id,
                        source_record_pk=current.id,
                        import_id=batch.id,
                        row_number=row_number,
                        incoming_hash=digest,
                        incoming_payload=payload,
                        changed_fields=changed,
                        created_at=clock.now(),
                    )
                )
                session.flush()
            result.outcome = CONFLICT
            result.reasons = [
                f"Source ID '{key}' already exists with different content "
                f"(changed: {', '.join(changed)}); original kept, flagged for review"
            ]
            batch.conflict_rows += 1
        session.add(result)


def _record_failed(
    factory: sessionmaker[Session],
    clock: Clock,
    tenant_id: str,
    spec: DatasetSpec,
    filename: str | None,
    file_sha: str,
    error: str,
) -> str:
    with transaction(factory) as session:
        batch = ImportBatch(
            tenant_id=tenant_id,
            dataset=spec.name,
            source_system=spec.source_system,
            filename=filename,
            file_sha256=file_sha,
            status="FAILED",
            error=error,
            created_at=clock.now(),
        )
        session.add(batch)
        session.flush()
        log.warning("import_rejected", extra={"tenant_id": tenant_id, "dataset": spec.name, "import_id": batch.id, "error": error})
        return batch.id


def get_import(session: Session, tenant_id: str, import_id: str) -> ImportBatch:
    batch = session.get(ImportBatch, import_id)
    if batch is None or batch.tenant_id != tenant_id:
        raise NotFound(f"Import '{import_id}' not found")
    return batch

