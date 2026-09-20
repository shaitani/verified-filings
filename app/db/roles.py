"""Provision the read-only database role.

    uv run python -m app.db.roles          # create/refresh against DATABASE_URL
    uv run python -m app.db.roles --check  # report, change nothing

Idempotent: safe to re-run, and re-running is how you apply a changed grant or
timeout. Run it once per database (the test container has its own).

Why this is not an Alembic migration
------------------------------------
A role is a *cluster* object, not a schema object -- it outlives any one
database and Alembic's autogenerate cannot see it. Putting one in a migration
would also commit its password to version control. So it is a script, and
``ALEMBIC.md`` step 5 already establishes the convention that a fresh database
needs a couple of deliberate setup commands.

What this protects against, and what it does not
------------------------------------------------
The only login role in this cluster is ``postgres``, a superuser. Every
connection the project makes -- loader, embedder, query mapper -- uses it.
That is fine while all the SQL is ours. It stops being fine the moment a
language model writes a query, which is the whole design of the SQL emitter
(HANDOFF §5.5).

This role is the floor under that: no writes, no DDL, a statement timeout, and
``default_transaction_read_only`` so a mistakenly widened grant still cannot
turn into a write. It is **not** yet a boundary between our SQL and the
model's -- both would use this same role, and it can read every table in the
``xbrl`` schema. The boundary arrives with the narrow query view (HANDOFF
§5.1): at that point the emitter gets its own role holding SELECT on the view
and nothing else, while the mapper keeps this one. Creating that second role
now would grant it exactly what this one has, which buys no safety and invites
the belief that the emitter is sandboxed when it is not.

Which half of this is a real boundary
-------------------------------------
Worth being exact, because the two halves fail differently.

**The grants are a hard boundary.** A role cannot grant itself privileges.
Verified against the live database with ``default_transaction_read_only``
deliberately turned *off*: INSERT, UPDATE, DELETE, CREATE TABLE in ``xbrl``,
CREATE TABLE in ``public`` and reading ``pg_authid`` all fail with
``InsufficientPrivilegeError``.

**The session settings are not.** ``default_transaction_read_only``,
``statement_timeout`` and ``search_path`` are all ``USERSET`` -- the role can
change them in its own session. They stop an *accident*: a mistakenly widened
grant, a runaway join, an unqualified name resolving somewhere unexpected. A
generated statement that says ``SET statement_timeout = 0`` before its SELECT
would shrug them off.

Closing that is the emitter's job, not this role's: one statement per
execution, parsed and checked to be a SELECT, with no leading ``SET``. Until
that validation exists, treat the timeout as a safety net rather than a
control.

Row caps are deliberately absent here for the same reason -- PostgreSQL has no
per-role row limit, so capping belongs to the emitter as a ``LIMIT`` it appends
and verifies.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

#: The login role the read-only consumers use. Named for what it is rather
#: than for who uses it, because the mapper and (for now) the emitter share it.
READONLY_ROLE = "verified_filings_ro"

#: Schema the role may read. Nothing grants it anything in ``public``.
SCHEMA = "xbrl"

#: Longest a query from this role may run. Generous for the mapper's own
#: lookups, which are indexed and small, and short enough that a model-written
#: cross join fails rather than occupying a connection.
STATEMENT_TIMEOUT = "10s"

#: A transaction left open by a crashed caller is rolled back rather than
#: holding locks and pinning the vacuum horizon.
IDLE_TX_TIMEOUT = "30s"


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _quote_literal(value: str) -> str:
    """A string literal for a statement that cannot take a bind parameter.

    ``ALTER ROLE ... PASSWORD`` is a utility statement, and PostgreSQL will not
    accept ``$1`` there -- it fails with a syntax error rather than quietly
    setting the password to the placeholder, which is the good kind of failure.
    So the literal is built here.

    Doubling single quotes is the whole of the escaping, because
    ``standard_conforming_strings`` has been on by default since PostgreSQL 9.1
    and a backslash is therefore an ordinary character. A NUL cannot appear in
    a literal at all, so it is rejected rather than escaped.
    """
    if "\x00" in value:
        raise ValueError("a password may not contain a NUL byte")
    return "'" + value.replace("'", "''") + "'"


def _statements(database: str, password: str) -> list[str]:
    """Everything the role needs, in dependency order.

    ``CREATE ROLE`` has no ``IF NOT EXISTS`` in PostgreSQL, hence the DO block;
    the password is set unconditionally afterwards so a re-run also rotates it
    to whatever the environment now says.
    """
    role = READONLY_ROLE
    return [
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN;
            END IF;
        END
        $$
        """,
        # NOSUPERUSER/NOCREATEDB/NOCREATEROLE are the defaults, but stated so a
        # reader does not have to know that, and so a role that somehow
        # acquired them is corrected by a re-run.
        f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
        # Inlined, not bound -- see _quote_literal. It is not logged:
        # log_statement is off by default, and this runs once by hand.
        f"ALTER ROLE {role} PASSWORD {_quote_literal(password)}",
        f'GRANT CONNECT ON DATABASE "{database}" TO {role}',
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {role}",
        # A table added later is readable without re-running this -- but the
        # re-run is still the documented way to apply a change.
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {role}",
        # Belt and braces: even if a grant is widened by mistake, every
        # transaction this role opens starts read-only.
        f"ALTER ROLE {role} SET default_transaction_read_only = on",
        f"ALTER ROLE {role} SET statement_timeout = '{STATEMENT_TIMEOUT}'",
        f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = '{IDLE_TX_TIMEOUT}'",
        # `xbrl` first, so an unqualified table name resolves there. `public`
        # has to stay on the path: the pgvector extension is installed into it
        # and operators are resolved through search_path, so dropping it made
        # `embedding <=> $1` fail with "operator does not exist" and took the
        # concept search down with it. Caught by running the eval harness after
        # this role landed, not by the unit tests.
        f"ALTER ROLE {role} SET search_path = {SCHEMA}, public",
        # USAGE is what makes those operators visible. It grants no access to
        # anything *in* public -- reading a table there still needs SELECT on
        # it, and creating one needs CREATE, which is revoked next.
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"REVOKE CREATE ON SCHEMA public FROM {role}",
        # PUBLIC (every role) can create objects in `public` by default before
        # PostgreSQL 15 and connect to any database; neither is wanted here.
        f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC',
    ]


async def provision(url: str, password: str) -> str:
    """Create or refresh the role against one database. Returns its name."""
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in _statements(_database_name(url), password):
                await connection.execute(text(statement))
    finally:
        await engine.dispose()
    return READONLY_ROLE


async def describe(url: str) -> dict[str, object]:
    """What the cluster currently says about the role, for ``--check``."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolconfig "
                        "FROM pg_roles WHERE rolname = :role"
                    ),
                    {"role": READONLY_ROLE},
                )
            ).first()
            if row is None:
                return {"exists": False}
            tables = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.table_privileges "
                        "WHERE grantee = :role AND table_schema = :schema"
                    ),
                    {"role": READONLY_ROLE, "schema": SCHEMA},
                )
            ).scalar_one()
            return {
                "exists": True,
                "superuser": row[0],
                "createdb": row[1],
                "createrole": row[2],
                "settings": list(row[3] or []),
                "granted_tables": tables,
            }
    finally:
        await engine.dispose()


def _readonly_password() -> str | None:
    """The password to set, from ``DATABASE_URL_READONLY``.

    Taken from the URL the application will actually connect with, rather than
    from a separate variable, so the two cannot drift apart -- provisioning a
    password nothing connects with is a failure that looks like success.
    """
    if settings.database_url_readonly is None:
        return None
    return urlsplit(settings.database_url_readonly).password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        default=settings.database_url,
        help="superuser URL to provision against (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--check", action="store_true", help="report the role's state and change nothing"
    )
    args = parser.parse_args(argv)

    if args.check:
        state = asyncio.run(describe(args.url))
        if not state["exists"]:
            print(f"{READONLY_ROLE}: NOT PRESENT -- run `uv run python -m app.db.roles`")
            return 1
        print(f"{READONLY_ROLE}: present")
        print(f"  superuser={state['superuser']} createdb={state['createdb']} "
              f"createrole={state['createrole']}")
        print(f"  tables granted in {SCHEMA}: {state['granted_tables']}")
        for setting in state["settings"]:
            print(f"  {setting}")
        return 0

    password = _readonly_password()
    if not password:
        print(
            "DATABASE_URL_READONLY is not set (or carries no password), so there is "
            "nothing to provision.\nAdd it to .env -- see .env.example -- then re-run.",
            file=sys.stderr,
        )
        return 2

    asyncio.run(provision(args.url, password))
    print(f"{READONLY_ROLE}: provisioned against {_database_name(args.url)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
