# OPSiB Receipt Exception Service

A small backend service that imports retail source exports (ERP purchase orders, WMS goods
receipts, product/location crosswalks, HRMS roster, workforce shifts), reconciles them into a
canonical model, detects **receipt shortfalls** against purchase orders at a frozen evaluation
time, routes an investigation task to an eligible employee, and records the task lifecycle
with a full audit trail.

Stack: Python 3.12+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL (SQLite for quick local runs).

* **[How to verify everything on your machine](docs/local-verification.md)**
* [Architecture and data model](docs/architecture.md), which also answers both design questions
* [API examples](docs/api-examples.md), recorded from a real run
* [OpenAPI export](docs/openapi.json); the live docs are at `/docs` when the server runs

## Results on the supplied fixtures

Evaluated at `2026-10-16T15:00:00+05:30`:

| PO line | Outcome | Expected | Accepted | Variance | Task |
|---|---|---|---|---|---|
| PO-501/1 | SHORTFALL | 100 EA | 92 EA (60 + 32) | 8 EA | HR-102 (receiving associate, STORE-014) |
| PO-502/1 | NO_VARIANCE | 40 EA | 40 EA | 0 | — |
| PO-503/1 | NOT_DUE | 50 EA | 20 EA | (deadline 2026-10-17 18:00) | — |
| PO-504/1 | CANCELLED | — | — | — | — |
| PO-505/1 | SHORTFALL | 120 EA (10 BOX × 12) | 96 EA | 24 EA | HR-401 (fallback store manager, STORE-015) |
| PO-506/1 | SHORTFALL | 20 EA | 10 EA (repeated row counted once) | 10 EA | HR-102 |

HR-101 (ACTIVE but OFF_SHIFT) and HR-103 (INACTIVE but ON_SHIFT) are never selected. HR-501
(regional operations) keeps a region scope with no store location.

None of these values is hard-coded. They come from the rule running over the imported files,
and the integration tests assert them.

## Quick start (SQLite, no Docker)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

export OPSIB_DATABASE_URL=sqlite:///./opsib.db
alembic upgrade head

# Load the fixture pack and run detection from the CLI
python -m app.cli demo fixtures/

# Or run the API
uvicorn app.main:create_app --factory --reload
# then open http://localhost:8000/docs
```

Driving it over HTTP:

```bash
for d in product_aliases location_aliases employees workforce_status purchase_order_lines goods_receipt_lines; do
  curl -s -F "file=@fixtures/$d.csv" localhost:8000/imports/$d | jq '{dataset,status,accepted_rows,duplicate_rows,rejected_rows}'
done
curl -s -X POST localhost:8000/detection/runs | jq '.evaluations[] | {po_number,outcome,variance_qty_base}'
curl -s localhost:8000/exceptions | jq
curl -s "localhost:8000/tasks?assignee_id=HR-102" | jq
curl -s -X POST -H "X-Actor-Id: HR-102" localhost:8000/tasks/<task_id>/acknowledge
```

## Running with PostgreSQL (Docker)

```bash
docker compose up --build        # runs migrations, then serves on :8000
```

The API container runs `alembic upgrade head` on start. The compose password is a local
placeholder only.

## Tests

```bash
pytest                                            # SQLite, one fresh database per test
OPSIB_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/opsib_test pytest   # PostgreSQL
```

On PostgreSQL the suite drops and recreates tables in the target database, so point it at a
throwaway database.

Last run:

```
SQLite:      88 passed, 1 skipped in 21.24s   (the skip is the PostgreSQL-only concurrency test)
PostgreSQL:  89 passed in 40.79s              (PostgreSQL 16)
```

| Required test area | Where |
|---|---|
| UOM conversion, partial receipts, deadline boundaries, cancelled lines | `tests/test_units_and_mapping.py`, `tests/test_shortfall_rule.py` |
| Fixture pack → six scenario outcomes, traceability, neutral wording | `tests/test_fixture_scenarios.py` |
| Same PO and receipt data imported twice, detection re-run, late receipts | `tests/test_idempotency.py` |
| Location mapping, off-shift, inactive, fallback manager, manual queue | `tests/test_routing.py` |
| Invalid transitions, repeated completion, Idempotency-Key, audit history, reassignment | `tests/test_workflow.py` |
| Failure paths: rejected rows, missing columns, conflicting duplicate ID, unknown product mapping, no eligible assignee | `tests/test_ingestion_failures.py`, `tests/test_routing.py` |
| AI boundary (invalid output, invented record IDs, timeout) | `tests/test_ai_boundary.py` |
| Migrations match models; concurrent workers produce one result (PostgreSQL) | `tests/test_migrations_and_concurrency.py` |

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `OPSIB_DATABASE_URL` | `sqlite:///./opsib.db` | SQLAlchemy URL; use `postgresql+psycopg://...` for PostgreSQL |
| `OPSIB_EVALUATION_TIME` | `2026-10-16T15:00:00+05:30` | Frozen "now" for deadlines, shifts and timestamps; a UTC offset is required |
| `OPSIB_DEFAULT_TENANT_ID` | `default` | Tenant used when no `X-Tenant-Id` header is sent |
| `OPSIB_SHORTFALL_TOLERANCE_BASE_UNITS` | `0` | Variance at or below this is not a shortfall |
| `OPSIB_AVAILABILITY_MAX_AGE_MINUTES` | `60` | Older shift samples are treated as stale |
| `OPSIB_TASK_SLA_HOURS` | `24` | Task due time after creation |
| `OPSIB_AI_PROVIDER` | `disabled` | No AI calls are made |
| `OPSIB_LOG_LEVEL` | `INFO` | Structured JSON logs to stdout |

The machine clock is never used for scenario logic. Every service receives a `Clock` pinned to
`OPSIB_EVALUATION_TIME`, so import, detection and audit timestamps all show that instant.

## API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/imports/{dataset}` | Upload one CSV (`multipart/form-data`, field `file`). Returns counts and a result for every row. `201` if processed, `422` if the file itself is unusable |
| GET | `/imports`, `/imports/{id}?outcome=` | Import history, counts, row errors |
| GET | `/datasets` | Supported datasets, required columns, load order |
| GET | `/data-quality` | Failed imports, rejected rows, open conflicts, unmapped records, not-evaluable PO lines, orphan receipts |
| POST | `/detection/runs` | Evaluate every PO line at the frozen time; creates or updates exceptions and tasks |
| GET | `/detection/runs/{id}` | Outcome for every PO line in that run |
| GET | `/exceptions?status=&location_id=` | List exceptions |
| GET | `/exceptions/{id}` | Exception with evidence, explanation, linked raw source rows, tasks and events |
| GET | `/exceptions/{id}/investigation-summary` | Advisory summary (deterministic unless an AI provider is configured) |
| GET | `/tasks?assignee_id=&location_id=&status=&queue=` | List tasks with status and due time |
| GET | `/tasks/{id}` | Task with assignment history and audit events |
| POST | `/tasks/{id}/acknowledge` · `start` · `block` · `unblock` · `complete` · `verify` · `reject` · `reassign` | Validated commands (header `X-Actor-Id`, optional `Idempotency-Key`) |

Errors always use `{"error": {"code", "message", "details"}}`: `404 not_found`,
`409 invalid_transition` / `conflict` / `concurrent_modification`, `403 forbidden`,
`422 validation_failed` / `request_invalid`.

## Repository layout

```
app/
  api/          routes, request/response schemas, error envelope, dependencies
  ingestion/    dataset registry, row schemas, import pipeline, data-quality report
  mapping/      alias crosswalks + UOM conversion, canonical projection
  detection/    pure shortfall rule, neutral explanation, run/exception persistence
  routing/      pure eligibility policy, DB loading
  workflows/    state machine, task commands, verification, idempotency keys
  ai/           optional advisory summary interface + validation
  db/           models, UTC datetime type, sessions, tenant lock
  cli.py        load / detect / demo
migrations/     Alembic (0001 initial schema)
fixtures/       supplied files, unchanged (SHA256SUMS verified by a test)
tests/
scripts/generate_api_docs.py       regenerates docs/openapi.json and docs/api-examples.md
scripts/verify_running_server.py   end-to-end check against a running server
```

## Assumptions and trade-offs

* **A finding is a receipt variance, not a cause.** There is no dispatch/ASN, carrier or
  proof-of-delivery data, so a finding says only that WMS accepted fewer units than the PO line
  ordered. It lists what is unknown and what evidence would establish the cause. Resolution codes
  are neutral, and none of them asserts supplier fault.
* **Deadline semantics.** A line is actionable only if the evaluation time is strictly after
  `expected_receipt_by`. A deadline exactly equal to the evaluation time is still `NOT_DUE`.
* **Late receipts count.** Posted receipts received after the deadline but at or before the
  evaluation time reduce the shortfall. Receipts timestamped after the evaluation time are
  ignored. Only `POSTED` receipts count; `PENDING` and `REVERSED` ones are listed as excluded
  evidence.
* **Unmapped or conflicting data never becomes a business exception.** It shows up in
  `/data-quality` and as `NOT_EVALUABLE` in the run. Unknown codes are accepted as raw facts
  (not rejected), because a later alias import can resolve them. The canonical layer is rebuilt
  after every import, so load order does not matter.
* **Conflict policy.** The first version of a source ID wins until someone reviews the
  conflict. An open conflict blocks evaluation of anything that depends on it. A review/accept
  endpoint is not implemented; today it would be a manual database update. That is the
  natural next step.
* **Unknown status values are rejected** (for example a PO status outside
  `OPEN/PARTIALLY_RECEIVED/CLOSED/CANCELLED`) instead of being guessed. `CLOSED` lines are
  evaluated like open ones, because a short-closed PO is exactly the case to look at.
* **Hashing ignores formatting.** `100` and `100.0`, or `+05:30` and the equivalent `Z` time,
  hash identically, so a re-export with different formatting is a duplicate, not a conflict.
* **Routing freshness.** Availability samples older than 60 minutes, or observed after the
  evaluation time, are not trusted. The shift window must contain the evaluation time and the
  status must be `ON_SHIFT`.
* **One task per exception.** The model allows several (`task_key` is per task type), but the
  rule creates one investigation task. Reassignment never creates a new exception.
* **Full projection rebuild.** At fixture scale, rebuilding the canonical layer in the import
  transaction is simple and obviously deterministic. At real volume it would be incremental per
  affected key.
* **SQLite** is supported for convenience. It has no advisory locks and serialises writers
  globally, and timezone handling is emulated by a UTC column type. PostgreSQL is the target,
  and the suite passes on both.
* **Auth stand-ins.** `X-Tenant-Id` and `X-Actor-Id` are trusted headers for this exercise, and
  every query is scoped by tenant. Real authentication is not implemented.

## What changes for production (documented, not implemented)

* **Tenant-level authorization:** derive tenant and actor from a verified token (OIDC), enforce
  tenant scoping in one place (for example PostgreSQL row-level security), and map HR roles to
  permissions instead of hard-coded role names.
* **Secrets:** database and integration credentials come from a secret manager (Vault, AWS
  Secrets Manager), never from env files in the repo. Rotate them, and use per-integration
  service accounts.
* **Integrations:** pull from ERP/WMS/HRMS through authenticated connectors on a schedule,
  replacing manual uploads. Import jobs go on a background worker with retries.
* **Migrations:** run Alembic as a separate release step with backward-compatible
  (expand/contract) changes.
* **Monitoring:** metrics for import outcomes, rejected/conflict rates, detection run duration,
  open exceptions by age, overdue tasks and manual-queue depth. Alert on failed imports and on
  tasks stuck in `MANUAL_ASSIGNMENT`. Ship structured logs centrally with request IDs.
* **Retention and privacy:** set retention for raw payloads and import row results, and
  minimise employee personal data (names are stored but never logged). Keep audit tables
  append-only.
* **Conflict review workflow:** endpoints to accept or reject a pending source version, which
  triggers re-projection and re-detection.
* **Optional AI:** a Claude-backed `SummaryProvider` behind the existing interface, with
  timeouts, schema validation and citation checks already enforced by `app/ai/provider.py`.

## AI boundary

`app/ai/provider.py` defines the interface. The deterministic detector produces the finding
and the evidence IDs. An optional provider receives only that case's evidence package. Its
output is schema-validated (extra fields rejected), every cited record ID must be in the
package, and timeouts, errors and invalid output fall back to the deterministic explanation.
The model never computes quantities, picks assignees, verifies outcomes or writes to source
systems. No API key is needed and no calls are made.

## Derived test data

The supplied files in `fixtures/` are never modified, and a test checks their SHA-256 sums.
Scenario variants are built in memory by `tests/conftest.py::csv_with` from the originals:

* a conflicting re-send of `WMS-GRN501-A-L1` with `accepted_qty=100`,
* PO-501 pointing at an unknown item `ERP-SKU-404`; HR-102 with an unmapped location `ST-999`,
* HR-401 set to `OFF_SHIFT` (no eligible fallback at STORE-015 → manual queue),
* late receipts `GRN-501-C` of 5 EA and 8 EA posted on 2026-10-16 13:00 +05:30,
* reformatted but identical PO rows (`100.00`, `Z` timestamps),
* rows with a negative quantity, no UTC offset, an unknown status and a missing source ID.

## AI tooling disclosure

> **Candidate to complete before submission.**
>
> * Tools used: Claude (Anthropic), through Claude Code, for code generation, tests and
>   documentation drafting.
> * Parts independently reviewed or changed: _list the modules you reviewed line by line,
>   anything you rewrote, and any decisions you changed_.
