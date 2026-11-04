"""
Vendored metadata library from PowerBI-Reports-MetaData.

Provides Scanner batch extraction, M/SQL parsing, impact indexes,
and SharePoint artifact helpers — used by Control Center CatalogService.
"""

from .expression_parser import (
    SourceRef,
    parse_m_expression,
    extract_expression,
    extract_tables_from_sql_text,
    classify_source_display,
)
from .impact_builder import (
    build_impact_index,
    build_sources_index,
    lookup_table_impact,
)

__all__ = [
    "SourceRef",
    "parse_m_expression",
    "extract_expression",
    "extract_tables_from_sql_text",
    "classify_source_display",
    "build_impact_index",
    "build_sources_index",
    "lookup_table_impact",
]
