"""Typed row models for each supported source export.

Validation here is purely syntactic and per row: required fields, types,
explicit UTC offsets on timestamps, known status vocabularies and sane
quantities. Mapping to canonical identity happens later, in ``app.mapping``,
because an unknown product code is a data-quality issue that may be fixed by a
later alias import, not a reason to throw the source row away.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Code = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
UpperCode = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32, to_upper=True)
]
OptionalCode = Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=128)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeQty = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
PositiveQty = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data):
        # CSV has no null; an empty cell means "not supplied".
        if isinstance(data, dict):
            return {k: (None if isinstance(v, str) and v.strip() == "" else v) for k, v in data.items()}
        return data


class PurchaseOrderLineRow(_Row):
    source_record_id: Code
    po_number: Code
    line_number: PositiveInt
    supplier_code: Code
    location_code: Code
    item_code: Code
    ordered_qty: PositiveQty
    ordered_uom: UpperCode
    status: Literal["OPEN", "PARTIALLY_RECEIVED", "CLOSED", "CANCELLED"]
    expected_receipt_by: AwareDatetime


class GoodsReceiptLineRow(_Row):
    source_record_id: Code
    receipt_number: Code
    receipt_line: PositiveInt
    po_number: Code
    po_line_number: PositiveInt
    location_code: Code
    product_ref: Code
    accepted_qty: NonNegativeQty
    uom: UpperCode
    received_at: AwareDatetime
    posting_status: Literal["POSTED", "PENDING", "REVERSED"]


class ProductAliasRow(_Row):
    source_system: UpperCode
    external_item_id: Code
    canonical_product_id: Code
    source_uom: UpperCode
    base_uom: UpperCode
    to_base_factor: PositiveQty

    @model_validator(mode="after")
    def _identity_factor(self):
        if self.source_uom == self.base_uom and self.to_base_factor != 1:
            raise ValueError("to_base_factor must be 1 when source_uom equals base_uom")
        return self


class LocationAliasRow(_Row):
    source_system: UpperCode
    external_location_id: Code
    canonical_location_id: Code
    region_id: OptionalCode = None


class EmployeeRow(_Row):
    source_employee_id: Code
    display_name: Code
    employment_status: Literal["ACTIVE", "INACTIVE", "ON_LEAVE", "TERMINATED"]
    role: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, to_lower=True)]
    location_code: OptionalCode = None
    manager_employee_id: OptionalCode = None
    region_id: OptionalCode = None

    @model_validator(mode="after")
    def _needs_scope(self):
        # Region-scoped roles may omit a store, but every employee needs some
        # organisational scope or routing cannot reason about them.
        if not self.location_code and not self.region_id:
            raise ValueError("employee needs a location_code or a region_id")
        return self


class WorkforceStatusRow(_Row):
    source_employee_id: Code
    availability_status: Literal["ON_SHIFT", "OFF_SHIFT", "ON_BREAK", "ON_LEAVE"]
    shift_start: AwareDatetime
    shift_end: AwareDatetime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _shift_order(self):
        if self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        return self


def canonical_value(value):
    """JSON-safe, formatting-insensitive representation used for hashing.

    ``100`` and ``100.0`` hash the same; timestamps are compared as UTC
    instants, so ``+05:30`` and the equivalent ``Z`` time are identical.
    """
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(normalized, "f") if normalized != 0 else "0"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value
