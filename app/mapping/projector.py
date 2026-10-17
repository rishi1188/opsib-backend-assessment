"""Rebuild the canonical layer from raw source records.

The projection is a pure function of (source records, alias records), so it is
rebuilt in full inside the import transaction. That keeps load order irrelevant
(a PO imported before its alias resolves once the alias arrives) and means two
imports of the same data always produce the same canonical rows. At pilot
scale a full rebuild is cheap; a live system would rebuild incrementally per
affected key.

Source-system authority (which system owns which field):
- ERP: PO line identity, supplier, ordered quantity/UOM, PO status, deadline.
- WMS: receipt identity, accepted quantity/UOM, posting status, received time.
- MDM alias files: canonical product/location identity and UOM conversion.
- HRMS: employment status, role, home location, manager, region.
- WFM: shift availability, as observed at ``observed_at``.
"""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (
    Employee,
    EmployeeAvailability,
    LocationAlias,
    PoLine,
    ProductAlias,
    ReceiptLine,
    SourceRecord,
)
from app.ingestion.schemas import (
    EmployeeRow,
    GoodsReceiptLineRow,
    LocationAliasRow,
    ProductAliasRow,
    PurchaseOrderLineRow,
    WorkforceStatusRow,
)
from app.mapping.aliases import (
    LOCATION_UNMAPPED,
    REGION_MISMATCH,
    AliasIndex,
    LocationMapping,
    ProductMapping,
)

CANONICAL_TABLES = [PoLine, ReceiptLine, EmployeeAvailability, Employee, ProductAlias, LocationAlias]


def _records(session: Session, tenant_id: str, dataset: str) -> list[SourceRecord]:
    return list(
        session.scalars(
            select(SourceRecord)
            .where(SourceRecord.tenant_id == tenant_id, SourceRecord.dataset == dataset)
            .order_by(SourceRecord.source_record_id)
        )
    )


def load_alias_index(session: Session, tenant_id: str) -> AliasIndex:
    index = AliasIndex()
    for rec in _records(session, tenant_id, "product_aliases"):
        row = ProductAliasRow.model_validate(rec.payload)
        index.products[(row.source_system, row.external_item_id)] = ProductMapping(
            row.canonical_product_id, row.source_uom, row.base_uom, row.to_base_factor
        )
        index.content_hashes.append(rec.content_hash)
    for rec in _records(session, tenant_id, "location_aliases"):
        row = LocationAliasRow.model_validate(rec.payload)
        index.locations[(row.source_system, row.external_location_id)] = LocationMapping(
            row.canonical_location_id, row.region_id
        )
        index.content_hashes.append(rec.content_hash)
    return index


def rebuild_canonical(session: Session, tenant_id: str) -> AliasIndex:
    for model in CANONICAL_TABLES:
        session.execute(delete(model).where(model.tenant_id == tenant_id))
    session.flush()

    index = load_alias_index(session, tenant_id)
    version = index.version

    for rec in _records(session, tenant_id, "product_aliases"):
        row = ProductAliasRow.model_validate(rec.payload)
        session.add(
            ProductAlias(
                tenant_id=tenant_id,
                source_system=row.source_system,
                external_item_id=row.external_item_id,
                canonical_product_id=row.canonical_product_id,
                source_uom=row.source_uom,
                base_uom=row.base_uom,
                to_base_factor=row.to_base_factor,
                source_record_pk=rec.id,
            )
        )
    for rec in _records(session, tenant_id, "location_aliases"):
        row = LocationAliasRow.model_validate(rec.payload)
        session.add(
            LocationAlias(
                tenant_id=tenant_id,
                source_system=row.source_system,
                external_location_id=row.external_location_id,
                canonical_location_id=row.canonical_location_id,
                region_id=row.region_id,
                source_record_pk=rec.id,
            )
        )

    for rec in _records(session, tenant_id, "purchase_order_lines"):
        row = PurchaseOrderLineRow.model_validate(rec.payload)
        issues: list[str] = []
        loc = index.resolve_location(rec.source_system, row.location_code)
        if loc is None:
            issues.append(f"{LOCATION_UNMAPPED}:{rec.source_system}:{row.location_code}")
        product = index.resolve_product(
            rec.source_system, row.item_code, row.ordered_qty, row.ordered_uom
        )
        issues.extend(product.issues)
        session.add(
            PoLine(
                tenant_id=tenant_id,
                source_record_pk=rec.id,
                po_number=row.po_number,
                line_number=row.line_number,
                supplier_code=row.supplier_code,
                source_location_code=row.location_code,
                source_item_code=row.item_code,
                canonical_location_id=loc.canonical_location_id if loc else None,
                canonical_product_id=product.canonical_product_id,
                ordered_qty=row.ordered_qty,
                ordered_uom=row.ordered_uom,
                ordered_qty_base=product.base_qty,
                base_uom=product.base_uom,
                status=row.status,
                expected_receipt_by=row.expected_receipt_by,
                mapping_issues=issues,
                mapping_version=version,
            )
        )

    for rec in _records(session, tenant_id, "goods_receipt_lines"):
        row = GoodsReceiptLineRow.model_validate(rec.payload)
        issues = []
        loc = index.resolve_location(rec.source_system, row.location_code)
        if loc is None:
            issues.append(f"{LOCATION_UNMAPPED}:{rec.source_system}:{row.location_code}")
        product = index.resolve_product(
            rec.source_system, row.product_ref, row.accepted_qty, row.uom
        )
        issues.extend(product.issues)
        session.add(
            ReceiptLine(
                tenant_id=tenant_id,
                source_record_pk=rec.id,
                receipt_number=row.receipt_number,
                receipt_line=row.receipt_line,
                po_number=row.po_number,
                po_line_number=row.po_line_number,
                source_location_code=row.location_code,
                source_product_ref=row.product_ref,
                canonical_location_id=loc.canonical_location_id if loc else None,
                canonical_product_id=product.canonical_product_id,
                accepted_qty=row.accepted_qty,
                uom=row.uom,
                accepted_qty_base=product.base_qty,
                base_uom=product.base_uom,
                received_at=row.received_at,
                posting_status=row.posting_status,
                mapping_issues=issues,
                mapping_version=version,
            )
        )

    for rec in _records(session, tenant_id, "employees"):
        row = EmployeeRow.model_validate(rec.payload)
        issues = []
        canonical_location = None
        region = row.region_id
        if row.location_code:
            loc = index.resolve_location(rec.source_system, row.location_code)
            if loc is None:
                issues.append(f"{LOCATION_UNMAPPED}:{rec.source_system}:{row.location_code}")
            else:
                canonical_location = loc.canonical_location_id
                if region and loc.region_id and region != loc.region_id:
                    issues.append(f"{REGION_MISMATCH}:{region}!={loc.region_id}")
                region = region or loc.region_id
        session.add(
            Employee(
                tenant_id=tenant_id,
                employee_id=row.source_employee_id,
                source_record_pk=rec.id,
                display_name=row.display_name,
                employment_status=row.employment_status,
                role=row.role,
                source_location_code=row.location_code,
                canonical_location_id=canonical_location,
                region_id=region,
                manager_employee_id=row.manager_employee_id,
                mapping_issues=issues,
            )
        )

    for rec in _records(session, tenant_id, "workforce_status"):
        row = WorkforceStatusRow.model_validate(rec.payload)
        session.add(
            EmployeeAvailability(
                tenant_id=tenant_id,
                employee_id=row.source_employee_id,
                source_record_pk=rec.id,
                availability_status=row.availability_status,
                shift_start=row.shift_start,
                shift_end=row.shift_end,
                observed_at=row.observed_at,
            )
        )

    session.flush()
    return index
