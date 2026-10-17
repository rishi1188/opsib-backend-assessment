"""End-to-end check against a running server, using only the standard library.

    python scripts/verify_running_server.py                 # http://localhost:8000
    python scripts/verify_running_server.py http://host:8000

Start the server on an EMPTY database first (see docs/local-verification.md).
The script imports the fixture pack over HTTP, runs detection, checks the six
required scenario outcomes and routing, then walks one task through the whole
lifecycle, including an invalid transition and a repeated completion.
"""

import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
LOAD_ORDER = ["product_aliases", "location_aliases", "employees", "workforce_status",
              "purchase_order_lines", "goods_receipt_lines"]
failures = 0


def call(method, path, body=None, headers=None, raw=None, content_type="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = Request(BASE + path, data=data, method=method, headers={**(headers or {})})
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def upload(dataset):
    boundary = uuid.uuid4().hex
    content = (FIXTURES / f"{dataset}.csv").read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{dataset}.csv\"\r\n"
            f"Content-Type: text/csv\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return call("POST", f"/imports/{dataset}", raw=body, content_type=f"multipart/form-data; boundary={boundary}")


def check(label, condition, detail=""):
    global failures
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not condition else ""))
    failures += 0 if condition else 1


def num(v):
    return None if v is None else Decimal(v).normalize()


def main():
    status, health = call("GET", "/health")
    if status != 200:
        sys.exit(f"Server not reachable at {BASE}")
    print(f"Server OK, evaluation time {health['evaluation_time']}\n")

    print("1. Imports")
    for d in LOAD_ORDER:
        status, b = upload(d)
        print(f"  {d:22} {b['status']:22} accepted={b['accepted_rows']} duplicate={b['duplicate_rows']} "
              f"rejected={b['rejected_rows']}")
    _, imports = call("GET", "/imports")
    po = next(i for i in imports if i["dataset"] == "purchase_order_lines")
    check("PO file: 6 accepted, 1 identical duplicate skipped", (po["accepted_rows"], po["duplicate_rows"]) == (6, 1),
          f"got {po['accepted_rows']}/{po['duplicate_rows']}; was the database empty?")

    print("\n2. Detection (six required scenarios)")
    _, run = call("POST", "/detection/runs")
    got = {e["po_number"]: e for e in run["evaluations"]}
    expected = {"PO-501": ("SHORTFALL", 8), "PO-502": ("NO_VARIANCE", 0), "PO-503": ("NOT_DUE", None),
                "PO-504": ("CANCELLED", None), "PO-505": ("SHORTFALL", 24), "PO-506": ("SHORTFALL", 10)}
    for po_number, (outcome, variance) in expected.items():
        e = got.get(po_number, {})
        ok = e.get("outcome") == outcome and (variance is None or num(e.get("variance_qty_base")) == variance)
        check(f"{po_number}: {outcome}" + (f", variance {variance} EA" if variance else ""), ok, json.dumps(e))

    print("\n3. Routing")
    _, tasks = call("GET", "/tasks")
    by_loc = {}
    for t in tasks:
        by_loc.setdefault(t["location_id"], set()).add(t["assignee_id"])
    check("STORE-014 tasks go to HR-102", by_loc.get("STORE-014") == {"HR-102"}, str(by_loc))
    check("STORE-015 task falls back to manager HR-401", by_loc.get("STORE-015") == {"HR-401"}, str(by_loc))
    check("HR-101 (off shift) and HR-103 (inactive) never assigned",
          not ({"HR-101", "HR-103"} & {t["assignee_id"] for t in tasks}))

    print("\n4. Task lifecycle")
    task = next(t for t in tasks if t["location_id"] == "STORE-014")
    tid, assoc, mgr = task["id"], {"X-Actor-Id": "HR-102"}, {"X-Actor-Id": "HR-201"}
    done = {"resolution_code": "SHORTAGE_CONFIRMED_CLAIM_RAISED", "evidence_ref": "doc://count-sheet-1"}
    s, b = call("POST", f"/tasks/{tid}/complete", done, assoc)
    check("Completing before starting is refused (409)", s == 409, f"{s} {b}")
    s, _ = call("POST", f"/tasks/{tid}/acknowledge", None, {"X-Actor-Id": "HR-101"})
    check("Someone other than the assignee is refused (403)", s == 403)
    s1, _ = call("POST", f"/tasks/{tid}/acknowledge", None, assoc)
    s2, _ = call("POST", f"/tasks/{tid}/start", None, assoc)
    s3, b3 = call("POST", f"/tasks/{tid}/complete", done, assoc)
    check("Acknowledge, start, complete -> PENDING_VERIFICATION",
          (s1, s2, s3) == (200, 200, 200) and b3["status"] == "PENDING_VERIFICATION", f"{s1},{s2},{s3}")
    s4, b4 = call("POST", f"/tasks/{tid}/complete", done, assoc)
    check("Repeated completion returns 200 as an idempotent replay",
          s4 == 200 and b4.get("idempotent_replay") is True, f"{s4} {b4}")
    s, _ = call("POST", f"/tasks/{tid}/verify", {}, assoc)
    check("The completer cannot verify (403)", s == 403)
    s5, b5 = call("POST", f"/tasks/{tid}/verify", {"note": "checked"}, mgr)
    check("Store manager verifies -> VERIFIED", s5 == 200 and b5["status"] == "VERIFIED", f"{s5} {b5}")
    _, exc = call("GET", f"/exceptions/{task['exception_id']}")
    check("Exception is RESOLVED after verification", exc["status"] == "RESOLVED", exc["status"])
    _, detail = call("GET", f"/tasks/{tid}")
    print("  Audit trail: " + " -> ".join(e["event_type"] for e in detail["events"]))

    print("\n5. Re-running detection changes nothing")
    _, again = call("POST", "/detection/runs")
    check("No new exceptions or tasks", again["summary"]["exceptions_created"] == 0
          and again["summary"]["tasks_created"] == 0, str(again["summary"]))

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
