"""Flask Blueprints extracted from app.py, grouped by feature domain.

Each module here owns its own routes and feature-local helpers. Shared
app-level helpers (auth decorators, token getters, etc.) are imported from
`app` at call time to avoid circular imports, since `app.py` registers these
blueprints after defining them.
"""
