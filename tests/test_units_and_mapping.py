"""Unit tests: UOM conversion, alias resolution and content hashing."""

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.clock import Clock
from app.ingestion.schemas import GoodsReceiptLineRow, ProductAliasRow, canonical_value
from app.ingestion.service import content_hash
from app.mapping.aliases import AliasIndex, LocationMapping, ProductMapping


@pytest.fixture
def index() -> AliasIndex:
    idx = AliasIndex()
    idx.products[("ERP", "ERP-BOX-12")] = ProductMapping("SKU-002", "BOX", "EA", Decimal(12))
    idx.products[("WMS", "WMS-MAT-2002")] = ProductMapping("SKU-002", "EA", "EA", Decimal(1))
    idx.locations[("ERP", "MUM-015")] = LocationMapping("STORE-015", "REGION-MH")
    return idx


def test_box_converts_to_base_units(index):
    resolved = index.resolve_product("ERP", "ERP-BOX-12", Decimal(10), "BOX")
    assert resolved.canonical_product_id == "SKU-002"
    assert resolved.base_qty == Decimal(120)
    assert resolved.base_uom == "EA"
    assert resolved.issues == []


def test_quantity_already_in_base_unit_is_not_multiplied(index):
    resolved = index.resolve_product("ERP", "ERP-BOX-12", Decimal(96), "EA")
    assert resolved.base_qty == Decimal(96)


def test_cross_system_ids_resolve_to_one_sku(index):
    erp = index.resolve_product("ERP", "ERP-BOX-12", Decimal(1), "BOX")
    wms = index.resolve_product("WMS", "WMS-MAT-2002", Decimal(1), "EA")
    assert erp.canonical_product_id == wms.canonical_product_id == "SKU-002"


def test_alias_lookup_is_per_source_system(index):
    # The WMS code must not resolve when presented as an ERP code.
    resolved = index.resolve_product("ERP", "WMS-MAT-2002", Decimal(1), "EA")
    assert resolved.canonical_product_id is None
    assert resolved.issues == ["PRODUCT_UNMAPPED:ERP:WMS-MAT-2002"]


def test_unknown_unit_is_not_guessed(index):
    resolved = index.resolve_product("ERP", "ERP-BOX-12", Decimal(1), "PALLET")
    assert resolved.base_qty is None
    assert resolved.issues[0].startswith("UOM_NOT_CONVERTIBLE")


def test_mapping_version_changes_with_alias_content(index):
    index.content_hashes = ["a", "b"]
    v1 = index.version
    index.content_hashes = ["b", "a"]
    assert index.version == v1  # order-insensitive
    index.content_hashes = ["a", "c"]
    assert index.version != v1


def test_identity_conversion_factor_must_be_one():
    with pytest.raises(ValidationError):
        ProductAliasRow.model_validate(
            {"source_system": "ERP", "external_item_id": "X", "canonical_product_id": "SKU-9",
             "source_uom": "EA", "base_uom": "EA", "to_base_factor": "2"}
        )


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValidationError):
        GoodsReceiptLineRow.model_validate(
            {"source_record_id": "R1", "receipt_number": "G1", "receipt_line": "1", "po_number": "P",
             "po_line_number": "1", "location_code": "L", "product_ref": "X", "accepted_qty": "1",
             "uom": "EA", "received_at": "2026-10-15T10:00:00", "posting_status": "POSTED"}
        )


def test_hash_ignores_number_and_offset_formatting():
    a = {"qty": canonical_value(Decimal("100")),
         "at": canonical_value(datetime.fromisoformat("2026-10-15T12:00:00+05:30"))}
    b = {"qty": canonical_value(Decimal("100.00")),
         "at": canonical_value(datetime.fromisoformat("2026-10-15T06:30:00+00:00"))}
    assert content_hash(a) == content_hash(b)


def test_clock_requires_timezone():
    with pytest.raises(ValueError):
        Clock(datetime(2026, 10, 16, 15, 0))
