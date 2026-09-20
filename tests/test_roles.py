"""The two read-only roles, exercised against the real test database.

Not mocked. The whole value of a role is what PostgreSQL does when a query it
should not serve arrives, so these provision the roles for real and then try.

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

PASSWORDS = {roles.QUERY_MAPPER.name: "mapper-pw", roles.RETRIEVAL.name: "retrieval-pw"}

#: 768 floats, matching the embedding column. A shorter literal fails on
#: dimension rather than on privilege, which would make the test lie.
VECTOR = "[" + ",".join(["0.01"] * 768) + "]"


def _role_url(superuser_url: str, role: str) -> str:
    scheme, rest = superuser_url.split("://", 1)
    _, host_and_path = rest.split("@", 1)
    return f"{scheme}://{role}:{PASSWORDS[role]}@{host_and_path}"


@pytest_asyncio.fixture
async def provisioned(test_db_url):
    """Both roles, created against the test database.

    Provisioned here rather than assumed, so these cover ``app/db/roles.py``
    itself and the suite needs no setup step of its own.
    """
    await roles.provision(test_db_url, PASSWORDS)
    return test_db_url


def _engine(url: str, role: str, *, autocommit: bool = False):
    kwargs = {"isolation_level": "AUTOCOMMIT"} if autocommit else {}
    return create_async_engine(_role_url(url, role), **kwargs)


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #


async def test_provisioning_is_idempotent(test_db_url) -> None:
    """Re-running is how a changed grant is applied, so it has to be safe.
    ``CREATE ROLE`` has no ``IF NOT EXISTS``; the DO block is what makes the
    second run a no-op rather than an error."""
    await roles.provision(test_db_url, PASSWORDS)
    await roles.provision(test_db_url, PASSWORDS)

    report = await roles.describe(test_db_url)
    for spec in roles.ROLES:
        state = report[spec.name]
        assert state["exists"], spec.name
        assert state["superuser"] is False
        assert state["createdb"] is False
        assert state["createrole"] is False


async def test_the_spec_is_authoritative(provisioned) -> None:
    """Provisioning revokes before it grants, so narrowing a spec narrows the
    role. Without that, a role could only ever accumulate privileges."""
    report = await roles.describe(provisioned)
    granted = set(report[roles.RETRIEVAL.name]["grants"])
    assert granted == set(roles.RETRIEVAL.tables) | set(roles.RETRIEVAL.columns)


async def test_the_superseded_single_role_is_gone(provisioned) -> None:
    """`verified_filings_ro` did the job of both. Leaving an unused login with
    SELECT on everything lying around is worse than never having made it."""
    report = await roles.describe(provisioned)
    assert report[roles.LEGACY_ROLE]["exists"] is False


# --------------------------------------------------------------------------- #
# What each role can reach
# --------------------------------------------------------------------------- #


async def test_the_mapper_role_can_search_embeddings(provisioned) -> None:
    """Its reason for existing separately. The metric fallback is a pgvector
    similarity search, so this role needs the embedding column *and* `public`
    on its search_path for the `<=>` operator."""
    engine = _engine(provisioned, roles.QUERY_MAPPER.name)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(f"SELECT id FROM concept ORDER BY embedding <=> '{VECTOR}'::vector LIMIT 1")
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("embedding column", "SELECT embedding FROM concept LIMIT 1"),
        ("vector operator", f"SELECT '{VECTOR}'::vector"),
    ],
)
async def test_the_retrieval_role_cannot_reach_embeddings(
    provisioned, label: str, statement: str
) -> None:
    """The narrowing that makes the second role worth having. By the time
    Qwen's SQL runs, the plan already names concrete concept_ids -- so this
    role has no reason to read 1,904 x 768 floats, and cannot."""
    engine = _engine(provisioned, roles.RETRIEVAL.name)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("role", [roles.QUERY_MAPPER.name, roles.RETRIEVAL.name])
async def test_neither_role_can_read_load_run(provisioned, role: str) -> None:
    """An append-only log of every load that ever ran. Nothing that answers a
    question has any business reading it."""
    engine = _engine(provisioned, role)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(text("SELECT count(*) FROM load_run"))
            assert "InsufficientPrivilege" in str(caught.value)
    finally:
        await engine.dispose()


async def test_the_mapper_role_reads_the_base_tables(provisioned) -> None:
    engine = _engine(provisioned, roles.QUERY_MAPPER.name)
    try:
        async with engine.connect() as connection:
            who = (await connection.execute(text("SELECT current_user"))).scalar_one()
            assert who == roles.QUERY_MAPPER.name
            # Unqualified, so this also proves search_path reaches xbrl.
            await connection.execute(text("SELECT count(*) FROM company"))
            await connection.execute(text("SELECT count(*) FROM fact"))
    finally:
        await engine.dispose()


async def test_the_retrieval_role_reads_the_view(provisioned) -> None:
    engine = _engine(provisioned, roles.RETRIEVAL.name)
    try:
        async with engine.connect() as connection:
            who = (await connection.execute(text("SELECT current_user"))).scalar_one()
            assert who == roles.RETRIEVAL.name
            # Unqualified, so this also proves search_path reaches xbrl.
            await connection.execute(text("SELECT count(*) FROM reported_fact"))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("table", ["fact", "filing", "company", "concept"])
async def test_the_retrieval_role_cannot_name_the_base_tables(
    provisioned, table: str
) -> None:
    """The other half of the fence (app/retrieval/DESIGN.md 6). The view bakes
    in the is_latest filter and the three joins; this is what stops generated
    SQL routing around it -- and what keeps every column called `fiscal_year`
    out of reach, since Filing's is provenance, not a period (PITFALLS 1.1)."""
    engine = _engine(provisioned, roles.RETRIEVAL.name)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(text(f"SELECT count(*) FROM {table}"))
            assert "InsufficientPrivilege" in str(caught.value)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# The two layers of protection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [roles.QUERY_MAPPER.name, roles.RETRIEVAL.name])
async def test_an_ordinary_transaction_is_read_only(provisioned, role: str) -> None:
    """The outer layer: a connection opened normally cannot write, because
    every transaction these roles start is read-only."""
    engine = _engine(provisioned, role)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(text("INSERT INTO company (cik) VALUES (999999)"))
            assert "ReadOnlySQLTransaction" in str(caught.value)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("role", [roles.QUERY_MAPPER.name, roles.RETRIEVAL.name])
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
    provisioned, role: str, label: str, statement: str
) -> None:
    """The one that matters. ``default_transaction_read_only`` is switched OFF
    first, because the role can do that and so could a generated statement.
    What is left is the grants, and they have to be enough on their own."""
    engine = _engine(provisioned, role, autocommit=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET default_transaction_read_only = off"))
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(text(statement))
            assert "InsufficientPrivilege" in str(caught.value), f"{role}/{label}"
    finally:
        await engine.dispose()


async def test_the_statement_timeout_is_in_force(provisioned) -> None:
    """A runaway query fails rather than occupying a connection. Also USERSET,
    so this catches the accident, not the hostile case."""
    engine = _engine(provisioned, roles.RETRIEVAL.name, autocommit=True)
    try:
        async with engine.connect() as connection:
            timeout = (await connection.execute(text("SHOW statement_timeout"))).scalar_one()
            assert timeout == roles.RETRIEVAL.statement_timeout
            await connection.execute(text("SET statement_timeout = '150ms'"))
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(text("SELECT pg_sleep(5)"))
            assert "QueryCanceled" in str(caught.value)
    finally:
        await engine.dispose()


async def test_the_retrieval_role_has_a_connection_cap(provisioned) -> None:
    """Bounds how much of the pool one runaway execution can occupy. The
    mapper is our own code and is left uncapped."""
    report = await roles.describe(provisioned)
    assert report[roles.RETRIEVAL.name]["connection_limit"] == roles.RETRIEVAL.connection_limit
    assert report[roles.QUERY_MAPPER.name]["connection_limit"] == -1


def test_password_literal_escapes_a_quote() -> None:
    """``ALTER ROLE ... PASSWORD`` takes no bind parameter, so the literal is
    built by hand and has to survive a quote in the password."""
    assert roles._quote_literal("a'b") == "'a''b'"
    with pytest.raises(ValueError, match="NUL"):
        roles._quote_literal("a\x00b")
