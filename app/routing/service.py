"""Load canonical employee facts from the database and apply the routing policy."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employee, EmployeeAvailability
from app.routing.policy import (
    AvailabilityFact,
    CandidateCheck,
    EmployeeFact,
    RoutingDecision,
    check_employee,
    choose_assignee,
)


def _employee_fact(e: Employee) -> EmployeeFact:
    return EmployeeFact(
        employee_id=e.employee_id,
        employment_status=e.employment_status,
        role=e.role,
        canonical_location_id=e.canonical_location_id,
        region_id=e.region_id,
        mapping_issues=tuple(e.mapping_issues or ()),
    )


def _availability(session: Session, tenant_id: str) -> dict[str, list[AvailabilityFact]]:
    out: dict[str, list[AvailabilityFact]] = {}
    for a in session.scalars(select(EmployeeAvailability).where(EmployeeAvailability.tenant_id == tenant_id)):
        out.setdefault(a.employee_id, []).append(
            AvailabilityFact(a.availability_status, a.shift_start, a.shift_end, a.observed_at)
        )
    return out


def route_task(
    session: Session, tenant_id: str, location_id: str, as_of: datetime, max_age: timedelta
) -> RoutingDecision:
    employees = [
        _employee_fact(e)
        for e in session.scalars(select(Employee).where(Employee.tenant_id == tenant_id))
    ]
    return choose_assignee(employees, _availability(session, tenant_id), location_id, as_of, max_age)


def get_employee(session: Session, tenant_id: str, employee_id: str) -> Employee | None:
    return session.scalar(
        select(Employee).where(Employee.tenant_id == tenant_id, Employee.employee_id == employee_id)
    )


def check_manual_assignee(
    session: Session,
    tenant_id: str,
    employee: Employee,
    location_id: str,
    as_of: datetime,
    max_age: timedelta,
) -> CandidateCheck:
    """Full policy check for a manually chosen assignee, recorded with the assignment."""
    samples = _availability(session, tenant_id).get(employee.employee_id, [])
    return check_employee(_employee_fact(employee), samples, location_id, as_of, max_age)
