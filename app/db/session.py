"""Async SQLAlchemy engines and session factories.

Everything that talks to PostgreSQL imports from here instead of making its own
engine. For a unit of work that commits on success and rolls back on error::

    from app.db.session import SessionLocal

    async with SessionLocal.begin() as session:
        session.add(obj)

Two factories, because two different things connect. ``SessionLocal`` writes --
the loader and the embedder use it. ``ReadOnlySessionLocal`` cannot, and is what
the query mapper uses and what the SQL emitter will execute through. See
``app/db/roles.py`` for what the role can and cannot do, and why it is one role
rather than two.
"""

import warnings

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

#: One per process: a managed pool of connections to PostgreSQL.
engine = create_async_engine(settings.database_url)

#: Factory for short-lived ``AsyncSession`` objects (one unit of work each).
#: ``expire_on_commit=False`` keeps loaded objects usable after ``commit()``.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

#: True when the read-only factory is genuinely read-only. False means the role
#: has not been provisioned and the fallback below is in force -- worth
#: asserting in any test that claims to exercise the restriction.
READONLY_AVAILABLE = settings.database_url_readonly is not None

if READONLY_AVAILABLE:
    readonly_engine = create_async_engine(settings.database_url_readonly)
else:
    # A checkout without the role provisioned still has to run. Falling back
    # keeps the mapper working; the warning keeps the fallback from being
    # mistaken for the protection, which is the failure that would matter --
    # believing model-generated SQL is sandboxed when it is running as
    # superuser is worse than knowing it is not.
    warnings.warn(
        "DATABASE_URL_READONLY is not set, so read-only sessions fall back to the "
        "writable connection. Provision the role with "
        "`uv run python -m app.db.roles` before executing generated SQL.",
        RuntimeWarning,
        stacklevel=2,
    )
    readonly_engine = engine

ReadOnlySessionLocal = async_sessionmaker(readonly_engine, expire_on_commit=False)
