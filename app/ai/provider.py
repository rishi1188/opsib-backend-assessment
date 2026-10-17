"""Optional AI investigation summary behind a narrow, validated interface.

Boundary:
* The deterministic detector owns quantities, eligibility and verification.
* An AI provider receives only a structured evidence package for one case and
  may return an explanation, missing context and suggested investigation steps.
* Output is schema-validated, and every cited record ID must exist in the
  package. Invalid output, timeouts and provider errors fall back to the
  deterministic explanation; the API never depends on the model.

No live provider is wired in: ``DisabledProvider`` is the default and no API
calls are made. A Claude-backed provider would implement ``summarize`` by
sending the package as JSON with a tool/JSON schema matching
``InvestigationSummary``.
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db.models import ExceptionCase
from app.logging_config import get_logger

log = get_logger("ai")


class EvidencePackage(BaseModel):
    exception_id: str
    exception_type: str
    po_number: str
    po_line_number: int
    canonical_product_id: str
    canonical_location_id: str
    base_uom: str
    expected_qty: str
    received_qty: str
    variance_qty: str
    evidence_record_ids: list[str]
    known_facts: list[str]
    unknowns: list[str]


class InvestigationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    missing_context: list[str] = Field(default_factory=list, max_length=10)
    suggested_steps: list[str] = Field(default_factory=list, max_length=10)
    cited_record_ids: list[str] = Field(default_factory=list)


class SummaryProvider(Protocol):
    name: str

    def summarize(self, package: EvidencePackage) -> dict | None: ...


class DisabledProvider:
    name = "disabled"

    def summarize(self, package: EvidencePackage) -> dict | None:
        return None


def build_package(exc: ExceptionCase) -> EvidencePackage:
    ev = exc.evidence
    ids = [ev["po_line"]["source_record_id"]] + [r["source_record_id"] for r in ev["receipts_counted"]]
    ids += [r["source_record_id"] for r in ev["receipts_excluded"]]
    return EvidencePackage(
        exception_id=exc.id,
        exception_type=exc.exception_type,
        po_number=exc.po_number,
        po_line_number=exc.po_line_number,
        canonical_product_id=exc.canonical_product_id,
        canonical_location_id=exc.canonical_location_id,
        base_uom=exc.base_uom,
        expected_qty=str(exc.expected_qty),
        received_qty=str(exc.received_qty),
        variance_qty=str(exc.variance_qty),
        evidence_record_ids=ids,
        known_facts=exc.explanation["known"],
        unknowns=exc.explanation["unknown"],
    )


def validate_summary(raw: object, package: EvidencePackage) -> InvestigationSummary:
    summary = InvestigationSummary.model_validate(raw)
    unknown = sorted(set(summary.cited_record_ids) - set(package.evidence_record_ids))
    if unknown:
        raise ValueError(f"Summary cites records not in the evidence package: {unknown}")
    return summary


def deterministic_summary(exc: ExceptionCase, package: EvidencePackage) -> InvestigationSummary:
    return InvestigationSummary(
        summary=exc.explanation["summary"],
        missing_context=exc.explanation["unknown"],
        suggested_steps=exc.explanation["evidence_needed"],
        cited_record_ids=package.evidence_record_ids,
    )


def investigation_summary(exc: ExceptionCase, provider: SummaryProvider, timeout_s: float) -> dict:
    package = build_package(exc)
    source, error = "deterministic", None
    result: InvestigationSummary | None = None
    if not isinstance(provider, DisabledProvider):
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            raw = pool.submit(provider.summarize, package).result(timeout=timeout_s)
            if raw is not None:
                result = validate_summary(raw, package)
                source = provider.name
        except FutureTimeout:
            error = "provider timed out"
        except (ValidationError, ValueError) as exc_:
            error = f"provider output rejected: {exc_}"
        except Exception as exc_:  # noqa: BLE001 - any provider failure falls back
            error = f"provider error: {type(exc_).__name__}"
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if error:
            log.warning("ai_summary_fallback", extra={"exception_id": exc.id, "error": error})
    if result is None:
        result = deterministic_summary(exc, package)
    return {
        "exception_id": exc.id,
        "source": source,
        "fallback_reason": error,
        "advisory_only": True,
        **result.model_dump(),
    }
