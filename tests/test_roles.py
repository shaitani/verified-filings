"""The read-only role, exercised against the real test database.

Not mocked. The whole value of this role is what PostgreSQL does when a write
arrives, so these provision it for real and then try to write.

Two layers, tested separately because they are not equally strong:

* the **grants**, which a role cannot change and which therefore are a
  boundary
* the **session settings** (``default_transaction_read_only``,
  ``statement_timeout``), which are USERSET and which a hostile statement
  could switch off -- a guard against accident, not a control

See the module docstring in ``app/db/roles.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import roles

PASSWORD = "test-readonly-password"


def _readonly_url(superuser_url: str) -> str:
    """The same database, as the read-only role."""
    scheme, rest = superuser_url.split("://", 1)
    _, host_and_path = rest.split("@", 1)
    return f"{scheme}://{roles.READONLY_ROLE}:{PASSWORD}@{host_and_path}"


@pytest_asyncio.fixture
async def readonly_url(test_db_url):
    """Provision the role against the test database and return its URL.

    Provisioned here rather than assumed, so these cover ``app/db/roles.py``
    itself and the suite needs no setup step of its own.
    """
    await roles.provision(test_db_url, PASSWORD)
    return _readonly_url(test_db_url)


@pytest_asyncio.fixture
async def readonly_engine(readonly_url):
    engine = create_async_engine(readonly_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def readonly_autocommit_engine(readonly_url):
    """AUTOCOMMIT, so a test can turn ``default_transaction_read_only`` off and
    have it apply.

    That setting takes effect when a transaction *starts*, so inside an
    already-open transaction the ``SET`` changes nothing -- which is why the
    grants have to be probed outside one.
    """
    engine = create_async_engine(readonly_url, isolation_level="AUTOCOMMIT")
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_provisioning_is_idempotent(test_db_url) -> None:
    """Re-running is how a changed grant or timeout gets applied, so it has to
    be safe. ``CREATE ROLE`` has no ``IF NOT EXISTS``; the DO block is what
    makes the second run a no-op rather than an error."""
    await roles.provision(test_db_url, PASSWORD)
    await roles.provision(test_db_url, PASSWORD)

    state = await roles.describe(test_db_url)
    assert state["exists"]
    assert state["superuser"] is False
    assert state["createdb"] is False
    assert state["createrole"] is False
    assert state["granted_tables"] > 0


async def test_the_role_can_read(readonly_engine) -> None:
    async with readonly_engine.connect() as connection:
        who = (await connection.execute(text("SELECT current_user"))).scalar_one()
        assert who == roles.READONLY_ROLE
        # Unqualified, so this also proves search_path points at xbrl.
        await connection.execute(text("SELECT count(*) FROM company"))


async def test_an_ordinary_transaction_is_read_only(readonly_engine) -> None:
    """The outer layer: a connection opened normally cannot write, because
    every transaction this role starts is read-only."""
    async with readonly_engine.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(text("INSERT INTO company (cik) VALUES (999999)"))
        assert "ReadOnlySQLTransaction" in str(caught.value)


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("insert", "INSERT INTO company (cik) VALUES (999999)"),
        ("update", "UPDATE company SET entity_name = 'x'"),
        ("delete", "DELETE FROM fact"),
        ("create in xbrl", "CREATE TABLE xbrl.should_not_exist (i int)"),
        ("create in public", "CREATE TABLE public.should_not_exist (i int)"),
        ("read pg_authid", "SELECT count(*) FROM pg_authid"),
    ],
)
async def test_grants_hold_with_read_only_turned_off(
    readonly_autocommit_engine, label: str, statement: str
) -> None:
    """The one that matters. ``default_transaction_read_only`` is switched OFF
    first, because the role can do that and so could a generated statement.
    What is left is the grants, and they have to be enough on their own."""
    async with readonly_autocommit_engine.connect() as connection:
        await connection.execute(text("SET default_transaction_read_only = off"))
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(text(statement))
        assert "InsufficientPrivilege" in str(caught.value), label


async def test_the_statement_timeout_is_in_force(readonly_autocommit_engine) -> None:
    """A runaway query fails rather than occupying a connection. Also USERSET,
    so this catches the accident, not the hostile case."""
    async with readonly_autocommit_engine.connect() as connection:
        assert (await connection.execute(text("SHOW statement_timeout"))).scalar_one() == "10s"
        await connection.execute(text("SET statement_timeout = '150ms'"))
        with pytest.raises(DBAPIError) as caught:
            await connection.execute(text("SELECT pg_sleep(5)"))
        assert "QueryCanceled" in str(caught.value)


def test_password_literal_escapes_a_quote() -> None:
    """``ALTER ROLE ... PASSWORD`` takes no bind parameter, so the literal is
    built by hand and has to survive a quote in the password."""
    assert roles._quote_literal("a'b") == "'a''b'"
    with pytest.raises(ValueError, match="NUL"):
        roles._quote_literal("a\x00b")


async def test_the_vector_operator_is_reachable(readonly_engine) -> None:
    """Regression. The role's search_path originally omitted `public`, on the
    reasoning that a bare table name should not pick up something an extension
    installed there. But pgvector lives in `public`, and operators resolve
    through search_path too -- so `embedding <=> $1` stopped existing and the
    whole concept search went down with it.

    `public` is back on the path behind `xbrl`, with USAGE granted and CREATE
    revoked. The harness caught this; nothing else did, which is why it is
    pinned here.
    """
    async with readonly_engine.connect() as connection:
        distance = (
            await connection.execute(
                text("SELECT '[1,0]'::vector <=> '[0,1]'::vector")
            )
        ).scalar_one()
        assert distance == pytest.approx(1.0)
