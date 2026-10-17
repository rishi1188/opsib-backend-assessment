"""Command-line entry points.

    python -m app.cli load fixtures/          # import every CSV in load order
    python -m app.cli detect                  # run detection at the frozen time
    python -m app.cli demo fixtures/          # load + detect + print a summary
"""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.clock import Clock
from app.config import get_settings
from app.db.models import DetectionRun, ExceptionCase, ImportBatch, Task
from app.db.session import make_engine, make_session_factory
from app.detection.service import run_detection
from app.ingestion.registry import LOAD_ORDER
from app.ingestion.service import import_dataset
from app.logging_config import configure_logging


def _context():
    settings = get_settings()
    configure_logging("WARNING")
    factory = make_session_factory(make_engine(settings.database_url))
    return settings, Clock(settings.evaluation_time), factory


def load(directory: Path) -> list[dict]:
    settings, clock, factory = _context()
    results = []
    for dataset in LOAD_ORDER:
        path = directory / f"{dataset}.csv"
        if not path.exists():
            results.append({"dataset": dataset, "status": "SKIPPED", "reason": "file not found"})
            continue
        import_id = import_dataset(factory, clock, settings.default_tenant_id, dataset, path.read_bytes(), path.name)
        with factory() as session:
            b = session.get(ImportBatch, import_id)
            results.append({"dataset": dataset, "import_id": b.id, "status": b.status, "total": b.total_rows,
                            "accepted": b.accepted_rows, "duplicate": b.duplicate_rows,
                            "conflict": b.conflict_rows, "rejected": b.rejected_rows, "error": b.error})
    return results


def detect() -> dict:
    settings, clock, factory = _context()
    run_id = run_detection(factory, clock, settings, settings.default_tenant_id)
    with factory() as session:
        run = session.get(DetectionRun, run_id)
        evaluations = [
            {"po": f"{e.po_number}/{e.po_line_number}", "outcome": e.outcome,
             "expected": str(e.expected_qty_base) if e.expected_qty_base is not None else None,
             "received": str(e.received_qty_base) if e.received_qty_base is not None else None,
             "variance": str(e.variance_qty_base) if e.variance_qty_base is not None else None,
             "uom": e.base_uom}
            for e in sorted(run.evaluations, key=lambda e: (e.po_number, e.po_line_number))
        ]
        tasks = [
            {"exception": f"{exc.po_number}/{exc.po_line_number}", "location": t.location_id,
             "status": t.status, "assignee": t.assignee_id, "queue": t.queue}
            for exc in session.scalars(select(ExceptionCase).order_by(ExceptionCase.po_number))
            for t in exc.tasks
        ]
        return {"run_id": run.id, "evaluated_at": run.evaluated_at.isoformat(), "summary": run.summary,
                "evaluations": evaluations, "tasks": tasks,
                "total_tasks": len(session.scalars(select(Task)).all())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsib")
    sub = parser.add_subparsers(dest="command", required=True)
    p_load = sub.add_parser("load")
    p_load.add_argument("directory", type=Path)
    sub.add_parser("detect")
    p_demo = sub.add_parser("demo")
    p_demo.add_argument("directory", type=Path)
    args = parser.parse_args(argv)

    if args.command == "load":
        out = load(args.directory)
    elif args.command == "detect":
        out = detect()
    else:
        out = {"imports": load(args.directory), "detection": detect()}
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
