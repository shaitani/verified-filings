"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Company, Concept
from app.ingest import sic_index, xbrl_store


@pytest.fixture(autouse=True)
def _isolate_sic_index(tmp_path, monkeypatch):
    """Redirect ``sic_numbers.json`` to a temp path for every test, so the
    real project-root file is never read or written by the suite.

    ``sic_index`` resolves ``SIC_INDEX_FILE`` at call time, so patching the
    module attribute is enough.
    """
    monkeypatch.setattr(sic_index, "SIC_INDEX_FILE", tmp_path / "sic_numbers.json")


@pytest.fixture(autouse=True)
def _isolate_xbrl_store(tmp_path, monkeypatch):
    """Redirect the curated XBRL-data store (``data/xbrl/``) to a temp path for
    every test. ``xbrl_store`` resolves ``XBRL_STORE_DIR`` at call time."""
    monkeypatch.setattr(xbrl_store, "XBRL_STORE_DIR", tmp_path / "data" / "xbrl")


# --------------------------------------------------------------------------- #
# Test database (tests/test_loader.py) -- the separate PostgreSQL container
# "db-test" (see docker-compose.dev.yml, docs/ALEMBIC.md), never the real database.
# --------------------------------------------------------------------------- #

#: cik of tests/fixtures/xbrl_fake_company.json -- read from the file itself,
#: not retyped, so it can never drift out of sync with it.
FAKE_CIK = json.loads(
    (Path(__file__).parent / "fixtures" / "xbrl_fake_company.json").read_text("utf-8")
)["cik"]


class _TestDBSettings(BaseSettings):
    """Reads DATABASE_URL_TEST straight from .env. Deliberately separate from
    app.config.Settings, which is real-application config only."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env", extra="ignore"
    )
    database_url_test: str


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """The test database URL as a superuser. Tests that provision roles or
    open their own engine need the string, not a session factory. Only reads
    .env, so one per session serves module-scoped fixtures too."""
    return _TestDBSettings().database_url_test


@pytest_asyncio.fixture
async def test_session_factory():
    """Session factory bound to the test database.

    Fresh engine per test (not session-scoped): each test function gets its
    own asyncio event loop, and an asyncpg connection pool can't be reused
    across event loops.
    """
    engine = create_async_engine(_TestDBSettings().database_url_test)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def clean_fake_company(test_session_factory):
    """Delete the fake company before AND after a test, so tests never see
    leftovers from a previous run (or leave any for the next one).

    Deleting Company cascades to Filing/Fact/LoadRun (ON DELETE CASCADE).
    Concept does not cascade from Company -- it's global -- so its fake rows
    are deleted separately, matched by name prefix.
    """

    async def _delete() -> None:
        async with test_session_factory.begin() as session:
            await session.execute(delete(Company).where(Company.cik == FAKE_CIK))
            await session.execute(delete(Concept).where(Concept.name.startswith("ZzzTest")))

    await _delete()
    yield
    await _delete()


#: The web and admin roles' passwords on the test database only. Provisioned
#: here, never read from .env, so the suite needs no DATABASE_URL_WEB or
#: DATABASE_URL_ADMIN of its own.
TEST_WEB_PASSWORD = "web-pw"
TEST_ADMIN_PASSWORD = "admin-pw"


def role_url_on_test_db(test_db_url: str, role: str, password: str) -> str:
    """The test database's URL, logging in as ``role``."""
    scheme, rest = test_db_url.split("://", 1)
    return f"{scheme}://{role}:{password}@{rest.split('@', 1)[1]}"


@pytest_asyncio.fixture
async def web_factory(test_db_url):
    """``(web_role_factory, owner_factory)`` on the test database, and a clean
    slate: every user whose email starts ``zz-`` is deleted before and after,
    taking their conversations, jobs and traces with them (ON DELETE CASCADE).

    Tests that need users make them as the owner with such an email."""
    from sqlalchemy import text

    from app.db import roles
    from app.db.session import web_sessionmaker

    await roles.provision(
        test_db_url,
        {roles.WEB.name: TEST_WEB_PASSWORD, roles.ADMIN.name: TEST_ADMIN_PASSWORD},
    )
    scheme, rest = test_db_url.split("://", 1)
    web_url = f"{scheme}://{roles.WEB.name}:{TEST_WEB_PASSWORD}@{rest.split('@', 1)[1]}"
    owner_engine = create_async_engine(test_db_url)
    web = web_sessionmaker(web_url)

    async def clean() -> None:
        async with owner_engine.begin() as connection:
            await connection.execute(text("DELETE FROM web.\"user\" WHERE email LIKE 'zz-%'"))
            # An invite names no one, so there is nothing to tell a test's apart by:
            # every invite goes. This is the test database, which holds nothing else.
            await connection.execute(text("DELETE FROM web.invite"))

    await clean()
    try:
        yield web, async_sessionmaker(owner_engine, expire_on_commit=False)
    finally:
        await clean()
        await web.kw["bind"].dispose()
        await owner_engine.dispose()
