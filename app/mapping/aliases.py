"""Identity and unit resolution through the alias crosswalks.

Resolution is exact-match on (source_system, external_id). There is no fuzzy
matching, no name similarity and no guessing: an unknown code yields a
data-quality issue that blocks detection for that record.
"""

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

PRODUCT_UNMAPPED = "PRODUCT_UNMAPPED"
LOCATION_UNMAPPED = "LOCATION_UNMAPPED"
UOM_NOT_CONVERTIBLE = "UOM_NOT_CONVERTIBLE"
REGION_MISMATCH = "REGION_MISMATCH"


@dataclass(frozen=True)
class ProductMapping:
    canonical_product_id: str
    source_uom: str
    base_uom: str
    to_base_factor: Decimal


@dataclass(frozen=True)
class LocationMapping:
    canonical_location_id: str
    region_id: str | None


@dataclass(frozen=True)
class ResolvedProduct:
    canonical_product_id: str | None
    base_qty: Decimal | None
    base_uom: str | None
    issues: list[str] = field(default_factory=list)


@dataclass
class AliasIndex:
    products: dict[tuple[str, str], ProductMapping] = field(default_factory=dict)
    locations: dict[tuple[str, str], LocationMapping] = field(default_factory=dict)
    content_hashes: list[str] = field(default_factory=list)

    @property
    def version(self) -> str:
        """Fingerprint of the alias set in force; stored on every derived fact."""
        digest = hashlib.sha256("|".join(sorted(self.content_hashes)).encode()).hexdigest()
        return f"map-{digest[:12]}"

    def resolve_location(self, source_system: str, code: str) -> LocationMapping | None:
        return self.locations.get((source_system, code))

    def resolve_product(
        self, source_system: str, code: str, qty: Decimal, uom: str
    ) -> ResolvedProduct:
        mapping = self.products.get((source_system, code))
        if mapping is None:
            return ResolvedProduct(None, None, None, [f"{PRODUCT_UNMAPPED}:{source_system}:{code}"])
        if uom == mapping.source_uom:
            factor = mapping.to_base_factor
        elif uom == mapping.base_uom:
            factor = Decimal(1)
        else:
            return ResolvedProduct(
                mapping.canonical_product_id,
                None,
                mapping.base_uom,
                [
                    f"{UOM_NOT_CONVERTIBLE}:{source_system}:{code}:{uom}"
                    f"->{mapping.base_uom}"
                ],
            )
        return ResolvedProduct(mapping.canonical_product_id, qty * factor, mapping.base_uom)
