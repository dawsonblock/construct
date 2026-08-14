"""ERP integration: supplier resolution and (later) evidence snapshots."""
from __future__ import annotations

from construction_ai.erp.supplier_resolution import SupplierResolution, normalize_name, resolve_supplier_to_company

__all__ = ["SupplierResolution", "normalize_name", "resolve_supplier_to_company"]
