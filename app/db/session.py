"""Async SQLAlchemy engine and session factory, built from ``settings.database_url``.

Everything that talks to PostgreSQL imports from here instead of making its own
engine. For a unit of work that commits on success and rolls back on error::

    from app.db.session import SessionLocal

    async with SessionLocal.begin() as session:
        session.add(obj)
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

#: One per process: a managed pool of connections to PostgreSQL.
engine = create_async_engine(settings.database_url)

#: Factory for short-lived ``AsyncSession`` objects (one unit of work each).
#: ``expire_on_commit=False`` keeps loaded objects usable after ``commit()``.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
