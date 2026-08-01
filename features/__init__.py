"""
Power BI Control Center - Modular Features Package

This package contains all feature modules extracted from the monolithic app.py.
Each module handles a specific feature or group of related endpoints.

Structure:
- auth_*.py          - Authentication & SSO
- workspace_*.py     - Workspace management
- reports_*.py       - Report listing and metadata
- usage_*.py         - Usage analytics
- crash_test.py      - Health diagnostics
- lineage_*.py       - Dataset, query, visual lineage
- similarity_*.py    - Similarity analysis
- search_*.py        - Deep search features
- orphaned_*.py      - Orphaned reports
- export_*.py        - Export functionality
- documentation_*.py - Documentation generation
- cache_*.py         - Cache management
- page_*.py          - HTML page routes
- debug_*.py         - Debug endpoints
- utils.py           - Shared utilities

NOTE: This folder is for FUTURE refactoring (currently in planning phase).
      The main app.py is still the active application.
      DO NOT import from this package in app.py until refactoring is complete.
"""

__version__ = "2.0.0-planning"
__author__ = "Power BI Control Center Team"

# DISABLED - Feature modules not created yet
# This will be enabled when refactoring is implemented

# def register_all_blueprints(app):
#     """
#     Register all feature blueprints with the Flask app.
#
#     This function should be called from the main app.py after app initialization.
#
#     Args:
#         app: Flask application instance
#     """
#     # Will be implemented during refactoring
#     pass

__all__ = [
    'semantic_model_lineage',
    'report_usage_tracker',
    'utils',
    'auth_decorators'
]
