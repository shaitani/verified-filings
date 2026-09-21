"""Lightweight handles for the ``xbrl`` schema's views.

A ``sqlalchemy.table()`` construct rather than an ORM model or a
``MetaData``-bound ``Table``, deliberately: a view is created and dropped by
migrations (``3fcc714d6050``, ``a8b5b820cf1a``), and anything attached to
``Base.metadata`` would be seen by Alembic's autogenerate, which would then
propose creating it as a table. A detached ``table()`` is invisible to that
and still composes into ``select()`` normally.

Only the columns callers actually read are listed. Adding one here does not
change the view; it just makes it addressable.
"""

from __future__ import annotations

from sqlalchemy import column, table

#: ``xbrl.reported_fact`` -- one row per currently-reported value, plus a
#: synthesized fourth quarter wherever the filings support one.
#:
#: This is the relation ``app/retrieval/`` reads, and it is the relation the
#: mapper proves coverage against. That is the point: if the mapper bound a
#: value, retrieval can fetch it, because both asked the same relation. Proving
#: coverage against ``fact`` instead would be proving something about a
#: different set of rows -- 9,425 of the view's rows do not exist in ``fact``.
reported_fact = table(
    "reported_fact",
    column("company_cik"),
    column("concept_id"),
    column("unit"),
    column("is_instant"),
    column("period_start"),
    column("period_end"),
    column("value"),
    #: False for a filed value, True for a fourth quarter the view computed.
    #: A value nobody filed must never pass for one that was.
    column("is_synthesized"),
    schema="xbrl",
)
