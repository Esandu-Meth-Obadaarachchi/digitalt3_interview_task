"""Data access, one module per entity.

Every SQL statement in the application lives under this package. Routers stay
thin, services hold the business rules, and repositories own the queries, so
there is exactly one place to look when a column changes.
"""
