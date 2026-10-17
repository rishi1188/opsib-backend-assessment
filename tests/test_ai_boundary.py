"""The optional AI summary is advisory, validated and never load-bearing."""

import time

from sqlalchemy import select

from app.ai.provider import DisabledProvider, investigation_summary
from app.db.models import ExceptionCase


class Fake:
    name = "fake"

    def __init__(self, fn):
        self.fn = fn

    def summarize(self, package):
        return self.fn(package)


def _exc(env):
    with env.session() as s:
        return s.scalar(select(ExceptionCase).where(ExceptionCase.po_number == "PO-501"))


def test_disabled_provider_uses_deterministic_summary(loaded):
    out = investigation_summary(_exc(loaded), DisabledProvider(), 1)
    assert out["source"] == "deterministic" and out["advisory_only"] is True
    assert "ERP-PO501-L1" in out["cited_record_ids"]


def test_valid_provider_output_is_used(loaded):
    good = Fake(lambda p: {"summary": "Receipts total 92 of 100.", "missing_context": ["ASN"],
                           "suggested_steps": ["Check dock"], "cited_record_ids": ["WMS-GRN501-A-L1"]})
    out = investigation_summary(_exc(loaded), good, 1)
    assert out["source"] == "fake" and out["fallback_reason"] is None


def test_invented_record_id_is_rejected(loaded):
    bad = Fake(lambda p: {"summary": "x", "cited_record_ids": ["WMS-INVENTED-1"]})
    out = investigation_summary(_exc(loaded), bad, 1)
    assert out["source"] == "deterministic" and "not in the evidence package" in out["fallback_reason"]


def test_malformed_output_is_rejected(loaded):
    bad = Fake(lambda p: {"summary": "x", "verified": True})  # extra field not allowed
    out = investigation_summary(_exc(loaded), bad, 1)
    assert out["source"] == "deterministic" and out["fallback_reason"].startswith("provider output rejected")


def test_timeout_and_errors_fall_back(loaded):
    slow = Fake(lambda p: time.sleep(0.5))
    assert investigation_summary(_exc(loaded), slow, 0.05)["fallback_reason"] == "provider timed out"
    boom = Fake(lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    assert investigation_summary(_exc(loaded), boom, 1)["fallback_reason"] == "provider error: RuntimeError"


def test_summary_endpoint(loaded, client):
    exc_id = client.get("/exceptions").json()[0]["id"]
    r = client.get(f"/exceptions/{exc_id}/investigation-summary")
    assert r.status_code == 200 and r.json()["source"] == "deterministic"
