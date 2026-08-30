"""Database layer: SQLAlchemy 2.0 ORM models for the curated XBRL data.

The models mirror the per-company JSON written by ``app/ingest/xbrl_store.py``
(``data/xbrl/<TICKER>.json``), which is itself a scope-filtered slice of the
SEC ``xbrl/companyfacts`` endpoint ("XBRL data") -- 10-K/10-Q filings, five
most recent fiscal years.

Everything lives in a dedicated PostgreSQL schema (namespace) called ``xbrl``
rather than ``public`` -- see ``app/db/models.py``. Engine/session wiring and
Alembic migrations are intentionally not here yet.

Re-exports the declarative ``Base`` and the five model classes for convenience::

    from app.db import Base, Company, Filing, Concept, Fact, IngestRun
"""

from __future__ import annotations

from app.db.models import (
    ALLOWED_UNITS,
    Base,
    Company,
    Concept,
    Fact,
    Filing,
    IngestRun,
    filing_form_enum,
    fiscal_period_enum,
    taxonomy_enum,
)

__all__ = [
    "ALLOWED_UNITS",
    "Base",
    "Company",
    "Concept",
    "Fact",
    "Filing",
    "IngestRun",
    "filing_form_enum",
    "fiscal_period_enum",
    "taxonomy_enum",
]
