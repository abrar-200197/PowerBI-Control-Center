"""
Control Center Catalog Service
==============================
Precomputed Power BI metadata from SharePoint latest/ only (source of truth).
In-memory TTL cache; no local-disk catalog serving.

Public API:
    from catalog_service import catalog_service
    catalog_service.get_workspace_catalog()
    catalog_service.get_workspace_reports(workspace_id, allowed_ids)
    catalog_service.lookup_table("FactSales")
"""
from catalog_service.service import CatalogService, catalog_service

__all__ = ["CatalogService", "catalog_service"]
