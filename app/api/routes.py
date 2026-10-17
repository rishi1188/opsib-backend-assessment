"""HTTP routes. Thin: parse the request, call a service, shape the response."""

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.provider import SummaryProvider, investigation_summary
from app.api import schemas as s
from app.api.deps import (
    get_actor,
    get_clock,
    get_factory,
    get_provider,
    get_session,
    get_settings,
    get_tenant,
)
from app.clock import Clock
from app.config import Settings
from app.db.models import DetectionRun, ExceptionCase, ImportBatch
from app.db.session import transaction
from app.detection.service import evidence_source_records, run_detection
from app.errors import NotFound, ValidationFailed
from app.ingestion.quality import data_quality_report
from app.ingestion.registry import DATASETS, LOAD_ORDER
from app.ingestion.service import get_import, get_spec, import_dataset
from app.workflows import service as wf
from app.workflows.idempotency import run_idempotent

router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.get("/health", tags=["meta"])
def health(clock: Clock = Depends(get_clock)) -> dict:
    return {"status": "ok", "evaluation_time": clock.now().isoformat()}


# ----------------------------------------------------------------- imports


@router.post("/imports/{dataset}", response_model=s.ImportDetailOut, tags=["imports"],
             summary="Validate and load one CSV export")
async def create_import(
    dataset: str,
    file: UploadFile = File(...),
    tenant: str = Depends(get_tenant),
    factory: sessionmaker[Session] = Depends(get_factory),
    clock: Clock = Depends(get_clock),
):
    get_spec(dataset)
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValidationFailed("File exceeds the 10 MB upload limit")
    import_id = import_dataset(factory, clock, tenant, dataset, content, file.filename)
    with transaction(factory) as session:
        batch = get_import(session, tenant, import_id)
        body = s.ImportDetailOut.model_validate(batch).model_dump(mode="json")
    # 201 for a processed import (inspect counts for partial success), 422 when
    # the file itself could not be processed.
    return JSONResponse(status_code=422 if body["status"] == "FAILED" else 201, content=body)


@router.get("/imports", response_model=list[s.ImportOut], tags=["imports"])
def list_imports(tenant: str = Depends(get_tenant), session: Session = Depends(get_session)):
    return session.scalars(
        select(ImportBatch).where(ImportBatch.tenant_id == tenant).order_by(ImportBatch.created_at, ImportBatch.id)
    ).all()


@router.get("/imports/{import_id}", response_model=s.ImportDetailOut, tags=["imports"])
def read_import(
    import_id: str,
    outcome: str | None = Query(default=None, description="Filter rows by outcome"),
    tenant: str = Depends(get_tenant),
    session: Session = Depends(get_session),
):
    batch = get_import(session, tenant, import_id)
    out = s.ImportDetailOut.model_validate(batch)
    if outcome:
        out.rows = [r for r in out.rows if r.outcome == outcome.upper()]
    return out


@router.get("/datasets", tags=["imports"])
def list_datasets() -> dict:
    return {
        "load_order": LOAD_ORDER,
        "datasets": {
            name: {"source_system": spec.source_system, "required_columns": spec.required_columns,
                   "derived_source_record_id": spec.key_is_derived}
            for name, spec in DATASETS.items()
        },
    }


@router.get("/data-quality", tags=["imports"])
def data_quality(tenant: str = Depends(get_tenant), session: Session = Depends(get_session)) -> dict:
    return data_quality_report(session, tenant)


# --------------------------------------------------------------- detection


@router.post("/detection/runs", response_model=s.DetectionRunDetailOut, status_code=201, tags=["detection"],
             summary="Evaluate every PO line at the frozen evaluation time")
def create_detection_run(
    tenant: str = Depends(get_tenant),
    factory: sessionmaker[Session] = Depends(get_factory),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings),
):
    run_id = run_detection(factory, clock, settings, tenant)
    with transaction(factory) as session:
        return s.DetectionRunDetailOut.model_validate(session.get(DetectionRun, run_id))


@router.get("/detection/runs/{run_id}", response_model=s.DetectionRunDetailOut, tags=["detection"])
def read_detection_run(run_id: str, tenant: str = Depends(get_tenant), session: Session = Depends(get_session)):
    run = session.get(DetectionRun, run_id)
    if run is None or run.tenant_id != tenant:
        raise NotFound(f"Detection run '{run_id}' not found")
    return run


# -------------------------------------------------------------- exceptions


@router.get("/exceptions", response_model=list[s.ExceptionOut], tags=["exceptions"])
def list_exceptions(
    status: str | None = None,
    location_id: str | None = None,
    tenant: str = Depends(get_tenant),
    session: Session = Depends(get_session),
):
    q = select(ExceptionCase).where(ExceptionCase.tenant_id == tenant)
    if status:
        q = q.where(ExceptionCase.status == status.upper())
    if location_id:
        q = q.where(ExceptionCase.canonical_location_id == location_id)
    return session.scalars(q.order_by(ExceptionCase.po_number, ExceptionCase.po_line_number)).all()


def _get_exception(session: Session, tenant: str, exception_id: str) -> ExceptionCase:
    exc = session.get(ExceptionCase, exception_id)
    if exc is None or exc.tenant_id != tenant:
        raise NotFound(f"Exception '{exception_id}' not found")
    return exc


@router.get("/exceptions/{exception_id}", response_model=s.ExceptionDetailOut, tags=["exceptions"])
def read_exception(exception_id: str, tenant: str = Depends(get_tenant), session: Session = Depends(get_session)):
    exc = _get_exception(session, tenant, exception_id)
    out = s.ExceptionDetailOut.model_validate(
        {**s.ExceptionOut.model_validate(exc).model_dump(), "evidence": exc.evidence,
         "explanation": exc.explanation, "source_records": evidence_source_records(session, exc),
         "tasks": exc.tasks, "events": exc.events}
    )
    return out


@router.get("/exceptions/{exception_id}/investigation-summary", tags=["exceptions"],
            summary="Advisory summary (optional AI, deterministic fallback)")
def read_investigation_summary(
    exception_id: str,
    tenant: str = Depends(get_tenant),
    session: Session = Depends(get_session),
    provider: SummaryProvider = Depends(get_provider),
    settings: Settings = Depends(get_settings),
) -> dict:
    exc = _get_exception(session, tenant, exception_id)
    return investigation_summary(exc, provider, settings.ai_timeout_seconds)


# ------------------------------------------------------------------- tasks


@router.get("/tasks", response_model=list[s.TaskOut], tags=["tasks"])
def list_tasks(
    assignee_id: str | None = None,
    location_id: str | None = None,
    status: str | None = None,
    queue: str | None = None,
    tenant: str = Depends(get_tenant),
    session: Session = Depends(get_session),
):
    return wf.list_tasks(session, tenant, assignee_id, location_id, status.upper() if status else None, queue)


@router.get("/tasks/{task_id}", response_model=s.TaskDetailOut, tags=["tasks"])
def read_task(task_id: str, tenant: str = Depends(get_tenant), session: Session = Depends(get_session)):
    return wf.get_task(session, tenant, task_id)


def _command(factory, clock, tenant, task_id, operation, body, idem_key, action):
    with transaction(factory) as session:
        def execute():
            result = action(session)
            session.flush()
            payload = s.TaskOut.model_validate(result.task).model_dump(mode="json")
            payload["idempotent_replay"] = result.replayed
            return 200, payload

        status, payload, replayed = run_idempotent(
            session, clock, tenant, idem_key, f"{operation}:{task_id}", body, execute
        )
    if replayed:
        payload = {**payload, "idempotent_replay": True}
    return JSONResponse(status_code=status, content=payload,
                        headers={"Idempotent-Replay": "true"} if payload.get("idempotent_replay") else None)


class _Ctx:
    def __init__(self, tenant: str = Depends(get_tenant), actor: str = Depends(get_actor),
                 factory: sessionmaker[Session] = Depends(get_factory), clock: Clock = Depends(get_clock),
                 settings: Settings = Depends(get_settings),
                 idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        self.tenant, self.actor, self.factory = tenant, actor, factory
        self.clock, self.settings, self.key = clock, settings, idempotency_key


@router.post("/tasks/{task_id}/acknowledge", response_model=s.TaskOut, tags=["tasks"])
def acknowledge(task_id: str, ctx: _Ctx = Depends()):
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "acknowledge", {"actor": ctx.actor}, ctx.key,
                    lambda ses: wf.acknowledge(ses, ctx.clock, ctx.tenant, task_id, ctx.actor))


@router.post("/tasks/{task_id}/start", response_model=s.TaskOut, tags=["tasks"])
def start(task_id: str, ctx: _Ctx = Depends()):
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "start", {"actor": ctx.actor}, ctx.key,
                    lambda ses: wf.start(ses, ctx.clock, ctx.tenant, task_id, ctx.actor))


@router.post("/tasks/{task_id}/block", response_model=s.TaskOut, tags=["tasks"])
def block(task_id: str, req: s.ReasonRequest, ctx: _Ctx = Depends()):
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "block",
                    {"actor": ctx.actor, **req.model_dump()}, ctx.key,
                    lambda ses: wf.block(ses, ctx.clock, ctx.tenant, task_id, ctx.actor, req.reason))


@router.post("/tasks/{task_id}/unblock", response_model=s.TaskOut, tags=["tasks"])
def unblock(task_id: str, ctx: _Ctx = Depends()):
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "unblock", {"actor": ctx.actor}, ctx.key,
                    lambda ses: wf.unblock(ses, ctx.clock, ctx.tenant, task_id, ctx.actor))


@router.post("/tasks/{task_id}/complete", response_model=s.TaskOut, tags=["tasks"])
def complete(task_id: str, req: s.CompleteRequest, ctx: _Ctx = Depends()):
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "complete",
                    {"actor": ctx.actor, **req.model_dump()}, ctx.key,
                    lambda ses: wf.complete(ses, ctx.clock, ctx.tenant, task_id, ctx.actor,
                                            req.resolution_code, req.evidence_ref, req.note))


@router.post("/tasks/{task_id}/verify", response_model=s.TaskOut, tags=["tasks"])
def verify(task_id: str, req: s.VerifyRequest | None = None, ctx: _Ctx = Depends()):
    note = req.note if req else None
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "verify", {"actor": ctx.actor, "note": note},
                    ctx.key, lambda ses: wf.verify(ses, ctx.clock, ctx.tenant, task_id, ctx.actor, note))


@router.post("/tasks/{task_id}/reject", response_model=s.TaskOut, tags=["tasks"])
def reject(task_id: str, req: s.ReasonRequest, ctx: _Ctx = Depends()):
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "reject",
                    {"actor": ctx.actor, **req.model_dump()}, ctx.key,
                    lambda ses: wf.reject(ses, ctx.clock, ctx.tenant, task_id, ctx.actor, req.reason))


@router.post("/tasks/{task_id}/reassign", response_model=s.TaskOut, tags=["tasks"])
def reassign(task_id: str, req: s.ReassignRequest, ctx: _Ctx = Depends()):
    return _command(ctx.factory, ctx.clock, ctx.tenant, task_id, "reassign",
                    {"actor": ctx.actor, **req.model_dump()}, ctx.key,
                    lambda ses: wf.reassign(ses, ctx.clock, ctx.settings, ctx.tenant, task_id, ctx.actor,
                                            req.assignee_id, req.reason))
