"""Assignment policy (pure, deterministic, no database access).

Safety invariants:
* Employment status wins over any shift data: only ``ACTIVE`` employees can be
  assigned, even if the shift export says ``ON_SHIFT``.
* Availability must be ``ON_SHIFT`` in a sample that is not in the future, is
  no older than ``max_age`` and whose shift window contains ``as_of``.
* Location must match by canonical ID; a raw HRMS code that did not map is
  never compared to anything.
* If nobody qualifies, the decision is "manual assignment queue". The policy
  never falls through to an ineligible person.
* Ties inside a tier are broken by employee ID so the result is reproducible.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

POLICY_VERSION = "store_receiving_routing/v1"
MANUAL_QUEUE = "MANUAL_ASSIGNMENT"

# Ordered role tiers for receipt-shortfall work at a store.
SHORTFALL_ROLE_TIERS: tuple[str, ...] = ("receiving_associate", "store_manager")


@dataclass(frozen=True)
class EmployeeFact:
    employee_id: str
    employment_status: str
    role: str
    canonical_location_id: str | None
    region_id: str | None
    mapping_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class AvailabilityFact:
    availability_status: str
    shift_start: datetime
    shift_end: datetime
    observed_at: datetime


@dataclass
class CandidateCheck:
    employee_id: str
    role: str
    tier: int | None
    eligible: bool
    reasons: list[str]

    def as_dict(self) -> dict:
        return {
            "employee_id": self.employee_id,
            "role": self.role,
            "tier": self.tier,
            "eligible": self.eligible,
            "reasons": self.reasons,
        }


@dataclass
class RoutingDecision:
    assignee_id: str | None
    queue: str | None
    reason: str
    policy: str = POLICY_VERSION
    candidates: list[CandidateCheck] = field(default_factory=list)


def latest_sample(samples: list[AvailabilityFact], as_of: datetime) -> AvailabilityFact | None:
    """Most recent sample observed at or before ``as_of``; later samples are ignored."""
    usable = [s for s in samples if s.observed_at <= as_of]
    return max(usable, key=lambda s: s.observed_at) if usable else None


def check_employee(
    emp: EmployeeFact,
    samples: list[AvailabilityFact],
    location_id: str,
    as_of: datetime,
    max_age: timedelta,
    role_tiers: tuple[str, ...] = SHORTFALL_ROLE_TIERS,
) -> CandidateCheck:
    reasons: list[str] = []
    tier = role_tiers.index(emp.role) if emp.role in role_tiers else None
    if tier is None:
        reasons.append(f"role {emp.role} is not approved for this task")
    if emp.employment_status != "ACTIVE":
        reasons.append(f"employment_status is {emp.employment_status}")
    if emp.mapping_issues:
        reasons.append("HRMS location could not be mapped: " + "; ".join(emp.mapping_issues))
    elif emp.canonical_location_id is None:
        reasons.append("region-scoped employee without a store location")
    elif emp.canonical_location_id != location_id:
        reasons.append(f"assigned to {emp.canonical_location_id}, task is at {location_id}")

    sample = latest_sample(samples, as_of)
    if sample is None:
        reasons.append("no availability sample at or before the evaluation time")
    else:
        if as_of - sample.observed_at > max_age:
            reasons.append(f"availability sample observed {sample.observed_at.isoformat()} is stale")
        if sample.availability_status != "ON_SHIFT":
            reasons.append(f"availability_status is {sample.availability_status}")
        if not (sample.shift_start <= as_of < sample.shift_end):
            reasons.append("evaluation time is outside the reported shift window")

    return CandidateCheck(emp.employee_id, emp.role, tier, not reasons, reasons or ["eligible"])


def choose_assignee(
    employees: list[EmployeeFact],
    availability: dict[str, list[AvailabilityFact]],
    location_id: str,
    as_of: datetime,
    max_age: timedelta,
    role_tiers: tuple[str, ...] = SHORTFALL_ROLE_TIERS,
) -> RoutingDecision:
    checks = [
        check_employee(e, availability.get(e.employee_id, []), location_id, as_of, max_age, role_tiers)
        for e in sorted(employees, key=lambda e: e.employee_id)
    ]
    for tier, role in enumerate(role_tiers):
        eligible = [c for c in checks if c.eligible and c.tier == tier]
        if eligible:
            chosen = eligible[0]
            kind = "primary" if tier == 0 else "fallback"
            return RoutingDecision(
                assignee_id=chosen.employee_id,
                queue=None,
                reason=(
                    f"{kind} role {role}: {chosen.employee_id} is ACTIVE, ON_SHIFT with a fresh "
                    f"availability sample, and located at {location_id}"
                ),
                candidates=checks,
            )
    return RoutingDecision(
        assignee_id=None,
        queue=MANUAL_QUEUE,
        reason=(
            f"No ACTIVE, ON_SHIFT employee in roles {', '.join(role_tiers)} at {location_id}; "
            "task left for manual assignment"
        ),
        candidates=checks,
    )
