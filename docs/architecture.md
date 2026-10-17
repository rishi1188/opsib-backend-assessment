# Architecture and data model

## Flow

```
CSV export ──► ingestion ──► raw layer ──► mapping/projector ──► canonical layer ──► detection ──► exceptions ──► workflows/routing ──► tasks
             (validate,     (source_record,  (alias crosswalks,   (po_line,           (pure rule,    (stable case    (eligibility policy,
              dedupe,        import results,  UOM conversion)      receipt_line,       frozen time)   key, evidence)  state machine, audit)
              hash)          conflicts)                            employee, ...)
```

| Module | Responsibility | Knows about |
|---|---|---|
| `app/ingestion/` | CSV parsing, typed row validation, idempotent persistence of source records, import reports, data-quality report | Source export shapes (`registry.py`) |
| `app/mapping/` | Alias crosswalks, UOM conversion, rebuilding the canonical layer | Source systems and alias files |
| `app/detection/` | The receipt-shortfall rule (`shortfall.py`, pure), neutral explanation, persistence of runs/exceptions | Canonical facts only |
| `app/routing/` | Eligibility policy (`policy.py`, pure) and DB loading | Canonical employees and availability |
| `app/workflows/` | Task state machine, commands, verification, idempotency keys, audit events | Exceptions, tasks, routing |
| `app/ai/` | Optional advisory summary behind a validated interface | One exception's evidence package |
| `app/api/` | HTTP routes, request/response models, error envelope | Services |
| `app/db/` | Models, UTC datetime type, sessions, tenant lock | — |

The detector and the routing policy are pure functions over small dataclasses. They have no
database access and are unit-tested directly; the services around them only load facts and
persist results.

## Data model

### Raw input layer (never overwritten)

| Table | Grain | Key / constraint | Notes |
|---|---|---|---|
| `import_batch` | one uploaded file | `id` | dataset, source system, file SHA-256, status (`COMPLETED`, `COMPLETED_WITH_ISSUES`, `FAILED`), row counts |
| `import_row_result` | one input row of one import | unique `(import_id, row_number)` | outcome `ACCEPTED` / `DUPLICATE` / `CONFLICT` / `REJECTED`, reasons, raw row |
| `source_record` | one source fact | **unique `(tenant_id, dataset, source_record_id)`** | normalised payload, SHA-256 content hash, first import + row, last import that re-sent it |
| `source_record_conflict` | one differing re-send | unique `(source_record_pk, incoming_hash)` | incoming payload and changed fields; `OPEN` until reviewed |

Exports without a native record ID get a documented derived ID:
`product_aliases` → `SOURCE:external_item_id`, `location_aliases` → `SOURCE:external_location_id`,
`workforce_status` → `employee_id@observed_at`, `employees` → `source_employee_id`.

### Canonical layer (deterministic projection, rebuilt in the import transaction)

| Table | Grain | Notes |
|---|---|---|
| `product_alias`, `location_alias` | one crosswalk row | unique per `(tenant, source_system, external_id)` |
| `po_line` | one ERP PO line source record | canonical product/location (nullable when unmapped), ordered qty in source and base UOM, status, deadline, `mapping_issues`, `mapping_version` |
| `receipt_line` | one WMS receipt line source record | canonical product/location, accepted qty in source and base UOM, posting status, received time |
| `employee` | one HRMS employee | canonical location **optional** (region-scoped roles), region, manager, mapping issues |
| `employee_availability` | one WFM sample | unique `(tenant, employee, observed_at)` |

### Business layer

| Table | Grain | Key / constraint |
|---|---|---|
| `detection_run` | one evaluation at a frozen instant | rule version, mapping version, summary |
| `line_evaluation` | one PO line in one run | every outcome is recorded, not only shortfalls |
| `exception_case` | one business case | **unique `(tenant_id, case_key)`**; `id = uuid5(tenant, case_key)` |
| `exception_event` | audit | `DETECTED`, `REEVALUATED`, `CLEARED`, `REOPENED`, `RESOLVED` |
| `task` | one unit of work | **unique `(tenant_id, task_key)`**, status check constraint, optimistic `version` column |
| `task_assignment` | one assignment decision | policy, reason, every candidate checked with reasons, who assigned |
| `task_event` | audit | every transition with from/to status, actor, data |
| `idempotency_record` | one client command | unique `(tenant_id, idempotency_key)`, request hash, stored response |

`case_key = receipt_shortfall:<po source system>:<po_number>:<line_number>`;
`task_key = <case_key>:investigate`.

## Identity rules

* **Field mapping vs entity identity.** A PO row's `item_code` is mapped through
  `(source_system="ERP", external_item_id)` only. The same string from WMS is a different
  key. There is no join on names or text similarity.
* **Canonical product/location** come only from the alias files. An unknown code leaves the
  canonical ID `NULL` and records `PRODUCT_UNMAPPED:ERP:<code>` / `LOCATION_UNMAPPED:...`.
* **UOM.** A quantity is converted only if its unit equals the alias's `source_uom` (multiply by
  `to_base_factor`) or already equals `base_uom` (factor 1). Anything else is
  `UOM_NOT_CONVERTIBLE`, never guessed. `10 BOX` becomes `120 EA` before any comparison.
* **Employees.** HRMS `location_code` maps through the HRMS alias. An employee with only a
  `region_id` is region-scoped and is never forced into a store.

## Which system is authoritative

| Field(s) | Authority |
|---|---|
| PO identity, supplier, ordered qty/UOM, PO status, expected-receipt deadline | ERP |
| Receipt identity, accepted qty/UOM, posting status, received time | WMS |
| Canonical product/location identity, UOM conversion | Alias (MDM) files |
| Employment status, role, home location, manager, region | HRMS |
| Shift availability as of `observed_at` | WFM |

If two sources disagree (for example, a receipt mapped to a different product than its PO
line), the rule reports `NOT_EVALUABLE` with the reason rather than picking a winner.

## Detection rule (`receipt_shortfall/v1`)

For each canonical PO line, in this order:

1. Open conflict on the PO source record → `NOT_EVALUABLE`.
2. More than one source record claims the same PO number and line → `NOT_EVALUABLE`.
3. PO status `CANCELLED` → `CANCELLED`.
4. PO product, location or unit not mapped → `NOT_EVALUABLE`.
5. Gather receipt lines linked by `(po_number, po_line_number)`. Count each source record once.
   Exclude receipts that are not `POSTED` or were received after the evaluation time (both are
   listed as excluded evidence). Receipts with open conflicts, unmapped identity, a different
   canonical product/location or base UOM, or the same receipt number/line under two source
   IDs block the evaluation.
6. `as_of <= expected_receipt_by` → `NOT_DUE`. The deadline must be strictly in the past.
7. Blocking receipt issues → `NOT_EVALUABLE`.
8. `expected - accepted > tolerance` (default 0) → `SHORTFALL`; `accepted > expected` →
   `OVER_RECEIVED`; otherwise `NO_VARIANCE`.

Receipts that arrive after the deadline but before the evaluation time count, because they
reduce what is actually missing. A re-run that sees new receipts updates the same exception
(`REEVALUATED`) or clears it (`CLEARED`, open tasks `CANCELLED`). Resolved exceptions are not
rewritten.

## Routing policy (`store_receiving_routing/v1`)

A candidate is eligible only if **all** hold at the frozen evaluation time:

* `employment_status == ACTIVE` (HRMS wins over any shift data),
* role is in the tier list: `receiving_associate` first, then `store_manager`,
* canonical location equals the task's location (unmapped or region-only never matches),
* the latest availability sample at or before `as_of` is `ON_SHIFT`, no older than
  `OPSIB_AVAILABILITY_MAX_AGE_MINUTES` (60), and its shift window contains `as_of`.

The first eligible employee of the first tier with any eligible member is chosen, with ties
broken by employee ID. If none qualifies, the task stays `CREATED` in the `MANUAL_ASSIGNMENT`
queue. Every decision stores all candidates and their reasons. Manual reassignment by a
supervisor must name a reason and passes the same eligibility check.

## Task lifecycle

```
CREATED ─assign─► ASSIGNED ─acknowledge─► ACKNOWLEDGED ─start─► IN_PROGRESS ─complete─► COMPLETED ─auto─► PENDING_VERIFICATION ─verify─► VERIFIED
                     ▲                                             │  ▲                                          │
                     └──────────── reassign (any open state) ──────┘  └────────────── reject (reopen) ───────────┘
                                                    IN_PROGRESS ⇄ BLOCKED (block / unblock)
            any open state ─cancel (system, when the exception clears)─► CANCELLED
```

* Only the assignee can acknowledge, start, block, unblock or complete.
* Completion needs a resolution code from a neutral list and an evidence reference.
* Verification needs an ACTIVE `store_manager` at the task's store or `regional_operations`
  in its region, who is not the completer. When all non-cancelled tasks of an exception are
  `VERIFIED`, the exception becomes `RESOLVED`.

## Design questions

### How a second import or a concurrent worker cannot produce a different result

1. `source_record` is unique on `(tenant_id, dataset, source_record_id)` and carries a content
   hash. A row that already exists with the same hash is a `DUPLICATE` and changes nothing.
   One with a different hash is a `CONFLICT`: the original stays and the new version waits for
   review.
2. One import is one transaction: row results, new source records, conflicts and the canonical
   rebuild commit together or not at all. A crash leaves no half-imported file, and the retry
   behaves like a first import.
3. On PostgreSQL the import and detection transactions take
   `pg_advisory_xact_lock(hashtext('<tenant>:pipeline'))`, so workers for the same tenant run one
   at a time. The second worker sees the first worker's committed rows and reports them as
   duplicates. SQLite allows only one writer at a time, which gives the same effect.
4. If something raced past the lock anyway, the unique constraints on `source_record`,
   `exception_case.case_key` and `task.task_key` make the second insert fail and roll back its
   transaction. The retry then finds the existing rows.

`tests/test_migrations_and_concurrency.py::test_concurrent_imports_and_detection_create_one_result`
runs four workers at once against PostgreSQL and checks that there are 36 source records,
3 exceptions and 3 tasks.

### Task completion committed, client timed out, client retries

* With an `Idempotency-Key` header, the response is stored in `idempotency_record` in the
  **same transaction** as the state change. The retry returns the stored response (`200`,
  `Idempotent-Replay: true`) without executing again. Reusing the key with a different body
  returns `409`.
* Without a key, completion is still naturally idempotent. If the task is already
  completed by the same actor with the same resolution code and evidence reference, the server
  returns `200` with the current task and `idempotent_replay: true`, and writes no events.
  Different details get `409`, never a silent overwrite.
* Concurrent edits to the same task are detected by the `version` column (optimistic locking)
  and return `409 concurrent_modification`.
