# Verifying everything on your own machine

There are three levels of checking, from fastest to most hands-on:

1. Run the automated test suite (89 tests).
2. Start the server and run one script that checks every scenario over HTTP.
3. Click through the API yourself in the browser (Swagger UI).

You need **Python 3.12 or newer** and **git**. Docker is only needed for the optional
PostgreSQL step.

## 0. Get the code and install

macOS / Linux:

```bash
git clone https://github.com/rishi1188/opsib-backend-assessment.git
cd opsib-backend-assessment
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Windows (PowerShell):

```powershell
git clone https://github.com/rishi1188/opsib-backend-assessment.git
cd opsib-backend-assessment
py -m venv .venv
.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install -r requirements-dev.txt
```

Every command below is the same on both systems once the virtual environment is active.

## 1. Run the test suite

```bash
python -m pytest
```

Expected: `88 passed, 1 skipped`. The skipped test needs PostgreSQL (see step 4).
`python -m pytest -v` lists every test by name. The table in the README maps each required
test area to its file.

## 2. Run the service and check every scenario in one go

Create the database (a local `opsib.db` SQLite file) and start the server:

```bash
python -m alembic upgrade head
python -m uvicorn app.main:create_app --factory --port 8000
```

Leave that running. In a **second terminal** (activate the venv again), run:

```bash
python scripts/verify_running_server.py
```

It imports the six CSVs over HTTP, runs detection, and checks the following. Every line
should say `PASS`, ending with `ALL CHECKS PASSED`:

* the duplicate rows are skipped,
* the six scenario outcomes PO-501 to PO-506,
* routing: HR-102 at STORE-014, fallback HR-401 at STORE-015, never HR-101 or HR-103,
* a full task lifecycle: invalid transition 409, wrong person 403, repeated completion 200
  replay, completer cannot verify, manager verifies, exception resolved,
* re-running detection creates nothing new.

The script expects an **empty database**. To run it again, stop the server, delete
`opsib.db`, and repeat step 2.

Without the server, `python -m app.cli demo fixtures/` prints the same import and
detection results as JSON (also on an empty database).

## 3. Explore it yourself in the browser

With the server running, open **http://localhost:8000/docs**. For each endpoint, click
it, then **Try it out**, then **Execute**.

| What to check | How |
|---|---|
| Imports and row-level outcomes | `GET /imports`, then `GET /imports/{import_id}` with `outcome=DUPLICATE` |
| Six scenario outcomes | `POST /detection/runs`; each PO line's `outcome` and quantities are in `evaluations` |
| A finding traced to its source rows | `GET /exceptions`, copy an `id`, then `GET /exceptions/{id}`; see `evidence`, `source_records` (file name and row number) and the neutral `explanation` |
| Why a person was chosen | `GET /tasks`, copy a task `id`, then `GET /tasks/{id}`; `assignments[0].candidates` gives each employee's reasons |
| Task lifecycle | `POST /tasks/{id}/acknowledge`, `/start` and `/complete` with header `X-Actor-Id` = the assignee (for example `HR-102`); then `/verify` with `X-Actor-Id: HR-201` |
| Idempotent completion | send the same `/complete` twice; the second returns 200 with `"idempotent_replay": true` |
| Bad input made visible | upload a CSV with a broken row to `POST /imports/goods_receipt_lines`, then `GET /data-quality` |

Example body for `/complete`:

```json
{"resolution_code": "SHORTAGE_CONFIRMED_CLAIM_RAISED", "evidence_ref": "doc://count-sheet-1"}
```

The same calls with real responses are in [api-examples.md](api-examples.md).

## 4. Optional: PostgreSQL

Run everything on PostgreSQL with Docker Desktop:

```bash
docker compose up --build
```

This starts PostgreSQL 16, runs the migrations and serves the API on port 8000. Then run
`python scripts/verify_running_server.py` exactly as in step 2. To reset, run
`docker compose down -v`.

To run the test suite on PostgreSQL, including the concurrency test that SQLite skips, keep
`docker compose up` running and in another terminal run:

```bash
docker compose exec db createdb -U opsib opsib_test
```

Then run the tests. On macOS / Linux:

```bash
OPSIB_TEST_DATABASE_URL=postgresql+psycopg://opsib:opsib-local-only@localhost:5432/opsib_test python -m pytest
```

On Windows (PowerShell):

```powershell
$env:OPSIB_TEST_DATABASE_URL="postgresql+psycopg://opsib:opsib-local-only@localhost:5432/opsib_test"
python -m pytest
```

Expected: `89 passed`.

## Troubleshooting

* **`address already in use`**: another program is using port 8000. Add `--port 8001` to
  the uvicorn command and run `python scripts/verify_running_server.py http://localhost:8001`.
* **The verify script reports duplicates instead of accepted rows**: the database already
  had data. Delete `opsib.db` (or run `docker compose down -v`) and start again.
* **`ModuleNotFoundError`**: the virtual environment is not active in that terminal.
