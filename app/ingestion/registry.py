"""Dataset registry: the one place that knows each export's shape.

Adding another retailer export means adding a row model and a ``DatasetSpec``
here (plus a projection step in ``app.mapping.projector``). The detector only
reads canonical tables and does not change.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from app.ingestion.schemas import (
    EmployeeRow,
    GoodsReceiptLineRow,
    LocationAliasRow,
    ProductAliasRow,
    PurchaseOrderLineRow,
    WorkforceStatusRow,
    canonical_value,
)


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source_system: str
    row_model: type[BaseModel]
    # Builds the source identity. Exports without a native record ID get a
    # documented derived key built from their natural key.
    record_key: Callable[[BaseModel], str]
    key_is_derived: bool = False

    @property
    def required_columns(self) -> list[str]:
        return [name for name, field in self.row_model.model_fields.items() if field.is_required()]

    @property
    def all_columns(self) -> list[str]:
        return list(self.row_model.model_fields)


DATASETS: dict[str, DatasetSpec] = {
    spec.name: spec
    for spec in [
        DatasetSpec(
            "purchase_order_lines", "ERP", PurchaseOrderLineRow, lambda r: r.source_record_id
        ),
        DatasetSpec(
            "goods_receipt_lines", "WMS", GoodsReceiptLineRow, lambda r: r.source_record_id
        ),
        DatasetSpec(
            "product_aliases",
            "MDM",
            ProductAliasRow,
            lambda r: f"{r.source_system}:{r.external_item_id}",
            key_is_derived=True,
        ),
        DatasetSpec(
            "location_aliases",
            "MDM",
            LocationAliasRow,
            lambda r: f"{r.source_system}:{r.external_location_id}",
            key_is_derived=True,
        ),
        DatasetSpec("employees", "HRMS", EmployeeRow, lambda r: r.source_employee_id),
        DatasetSpec(
            "workforce_status",
            "WFM",
            WorkforceStatusRow,
            lambda r: f"{r.source_employee_id}@{canonical_value(r.observed_at)}",
            key_is_derived=True,
        ),
    ]
}

# Load order used by the CLI so mappings exist before the facts that use them.
# The projection is rebuilt after every import, so any order gives the same end
# state; this order just produces fewer transient data-quality warnings.
LOAD_ORDER = [
    "product_aliases",
    "location_aliases",
    "employees",
    "workforce_status",
    "purchase_order_lines",
    "goods_receipt_lines",
]
