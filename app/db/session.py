"""Async SQLAlchemy engines and session factories.

Everything that talks to PostgreSQL imports from here instead of making its own
engine. For a unit of work that commits on success and rolls back on error::

    from app.db.session import SessionLocal

    async with SessionLocal.begin() as session:
        session.add(obj)

Four factories, one per role, because four different things connect:

===========================  =========================  ========================
factory                      role                       used by
===========================  =========================  ========================
``SessionLocal``             ``postgres`` (superuser)   loader, embedder, Alembic
``QueryMapperSessionLocal``  ``vf_query_mapper_role``   app/semantic/query_mapper
``RetrievalSessionLocal``    ``vf_retrieval_role``      app/retrieval
``web_sessionmaker()``       ``vf_web_role``            app/api (the Web Server)
===========================  =========================  ========================

The owner and the web role write; the other two read. See ``app/db/roles.py``
for what each role can reach and why they differ.
"""

import warnings

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.roles import ADMIN, WEB

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


class WebRoleMissing(RuntimeError):
    """The Web Server has no credential of its own, and will not borrow one."""


def web_sessionmaker(url: str | None = None) -> async_sessionmaker:
    """The Web Server's factory: ``vf_web_role`` and nothing else.

    A function rather than an import-time engine, so the evals and the CLI --
    which never need it -- do not need ``DATABASE_URL_WEB`` either. And no
    fallback, unlike the readers above: the owner can do everything this
    role's grants exist to prevent (make an administrator, read a trace).
    """
    url = url if url is not None else settings.database_url_web
    if not url:
        raise WebRoleMissing(
            "DATABASE_URL_WEB is not set. The Web Server runs only as vf_web_role -- "
            "see docs/STARTUP.md for the .env line and `uv run python -m app.db.roles`."
        )
    user = make_url(url).username
    if user != WEB.name:
        # Pointing it at the owner by mistake would quietly undo every grant.
        raise WebRoleMissing(f"DATABASE_URL_WEB logs in as {user!r}, not {WEB.name!r}")
    return async_sessionmaker(create_async_engine(url), expire_on_commit=False)


class AdminRoleMissing(RuntimeError):
    """The admin routes have no credential of their own, and will not borrow one."""


def admin_sessionmaker(url: str | None = None) -> async_sessionmaker:
    """The admin routes' factory: ``vf_admin_role`` and nothing else. No fallback,
    for the reason ``web_sessionmaker`` has none -- and not the web role either,
    which was built unable to do what these routes do."""
    url = url if url is not None else settings.database_url_admin
    if not url:
        raise AdminRoleMissing(
            "DATABASE_URL_ADMIN is not set. The admin routes run only as vf_admin_role -- "
            "see docs/STARTUP.md for the .env line and `uv run python -m app.db.roles`."
        )
    user = make_url(url).username
    if user != ADMIN.name:
        raise AdminRoleMissing(f"DATABASE_URL_ADMIN logs in as {user!r}, not {ADMIN.name!r}")
    return async_sessionmaker(create_async_engine(url), expire_on_commit=False)
