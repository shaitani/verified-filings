"""Async SQLAlchemy engines and session factories.

Everything that talks to PostgreSQL imports from here instead of making its own
engine. For a unit of work that commits on success and rolls back on error::

    from app.db.session import SessionLocal

    async with SessionLocal.begin() as session:
        session.add(obj)

Three factories, one per role, because three different things connect:

=========================  =========================  ==========================
factory                    role                       used by
=========================  =========================  ==========================
``SessionLocal``           ``postgres`` (superuser)   loader, embedder, Alembic
``QueryMapperSessionLocal``  ``vf_query_mapper_role``   app/semantic/query_mapper
``RetrievalSessionLocal``  ``vf_retrieval_role``      app/retrieval (not built)
=========================  =========================  ==========================

Only the first can write. See ``app/db/roles.py`` for what each read-only role
can reach and why they differ.
"""

import warnings

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

#: One per process: a managed pool of connections to PostgreSQL.
engine = create_async_engine(settings.database_url)

#: Factory for short-lived ``AsyncSession`` objects (one unit of work each).
#: ``expire_on_commit=False`` keeps loaded objects usable after ``commit()``.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _readonly_engine(url: str | None, *, missing: list[str], variable: str):
    """An engine for a read-only role, or the writable one if it is not
    configured.

    A checkout without the roles provisioned still has to run, so this falls
    back rather than failing at import. The caller warns once about everything
    that fell back, because a silent fallback is the dangerous case: believing
    Qwen's SQL is sandboxed while it runs as superuser is worse than knowing it
    is not.
    """
    if url is None:
        missing.append(variable)
        return engine
    return create_async_engine(url)


_missing: list[str] = []

query_mapper_engine = _readonly_engine(
    settings.database_url_query_mapper,
    missing=_missing,
    variable="DATABASE_URL_QUERY_MAPPER",
)
retrieval_engine = _readonly_engine(
    settings.database_url_retrieval,
    missing=_missing,
    variable="DATABASE_URL_RETRIEVAL",
)

#: True when *both* read-only factories are genuinely read-only. Worth
#: asserting in anything that claims to rely on the restriction.
READONLY_AVAILABLE = not _missing

if _missing:
    warnings.warn(
        f"{', '.join(_missing)} not set, so those sessions fall back to the writable "
        "connection. Provision the roles with `uv run python -m app.db.roles`.",
        RuntimeWarning,
        stacklevel=2,
    )

#: Reads our own SQL. Needs ``concept.embedding`` for the metric fallback.
QueryMapperSessionLocal = async_sessionmaker(query_mapper_engine, expire_on_commit=False)

#: Executes SQL written by a language model. Narrower on purpose: no
#: ``concept.embedding``, no ``load_run``, capped connections.
RetrievalSessionLocal = async_sessionmaker(retrieval_engine, expire_on_commit=False)
