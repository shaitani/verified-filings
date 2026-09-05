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
# "db-test" (see docker-compose.yml, ALEMBIC.md), never the real database.
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
