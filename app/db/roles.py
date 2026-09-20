"""Provision the two read-only database roles.

    uv run python -m app.db.roles          # create/refresh against DATABASE_URL
    uv run python -m app.db.roles --check  # report, change nothing

Idempotent, and re-running is how a changed grant is applied: each role's
privileges are revoked and re-granted from the spec below, so narrowing a
spec actually narrows the role.

Two roles, because two different things read and they are not equally trusted
-----------------------------------------------------------------------------
``vf_query_mapper_role``
    ``app/semantic/query_mapper.py``. Our own SQL: parameterized, reviewed,
    and the same four queries every time. Needs ``concept.embedding``, because
    the metric fallback is a pgvector similarity search against that column.

``vf_retrieval_role``
    ``app/retrieval/`` (not built yet). Executes SQL *written by Qwen*. Never
    needs the embedding column -- by the time it runs, the QueryPlan already
    names concrete ``concept_id``s -- so it cannot read 1,904 x 768 floats.
    Capped connections, and no automatic grant on tables added later.

Neither gets ``load_run``, an append-only log of every load that ever ran.
Nothing that answers a question has any business reading it.

Which half of this is a real boundary
-------------------------------------
**The grants are.** A role cannot grant itself privileges. Verified with
``default_transaction_read_only`` deliberately off: writes, DDL and
``pg_authid`` all fail with ``InsufficientPrivilegeError``.

**The session settings are not.** ``default_transaction_read_only``,
``statement_timeout`` and ``search_path`` are ``USERSET`` -- a generated
statement beginning ``SET statement_timeout = 0`` would shrug them off. They
stop an accident, not an attack. Closing that is the retrieval layer's job:
one statement per execution, parsed, no leading ``SET``, and a ``LIMIT`` it
appends and verifies. PostgreSQL has no per-role row limit, so the cap can
only live there.

Not an Alembic migration: a role is a cluster object that outlives any one
database, autogenerate cannot see it, and its password has no business in
version control. See ``ALEMBIC.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

#: Schema holding the data. Both roles read from here and nowhere else.
SCHEMA = "xbrl"

#: Superseded by the two roles below. Dropped on provision so an unused login
#: with SELECT on everything does not sit around.
LEGACY_ROLE = "verified_filings_ro"


@dataclass(frozen=True)
class RoleSpec:
    """One role's whole definition. The spec is authoritative -- provisioning
    revokes before it grants, so removing a table here removes the grant."""

    name: str

    #: Whose credential this is, for ``--check`` and for the error a missing
    #: environment variable produces.
    used_by: str

    #: Tables the role may read in full.
    tables: tuple[str, ...]

    #: Tables the role may read *some columns of*. Anything not listed is
    #: unreadable, which is how the embedding column is withheld.
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)

    #: Whether a table added by a later migration becomes readable
    #: automatically. True is a convenience; for the untrusted role it would
    #: be a standing grant on data nobody has looked at yet.
    inherit_future_tables: bool = False

    #: pgvector installs into ``public`` and operators resolve through
    #: ``search_path``, so a role using ``<=>`` needs ``public`` on the path
    #: and USAGE on the schema. A role that does not, does not.
    needs_vector_operators: bool = False

    #: -1 is unlimited. A cap bounds how much of the pool one runaway
    #: component can occupy.
    connection_limit: int = -1

    statement_timeout: str = "10s"
    idle_transaction_timeout: str = "30s"


QUERY_MAPPER = RoleSpec(
    name="vf_query_mapper_role",
    used_by="app/semantic/query_mapper.py",
    tables=("company", "filing", "fact", "concept"),
    inherit_future_tables=True,
    needs_vector_operators=True,
)

RETRIEVAL = RoleSpec(
    name="vf_retrieval_role",
    used_by="app/retrieval/ (executes Qwen-written SQL)",
    tables=("company", "filing", "fact"),
    # Everything a citation needs, and nothing else. `embedding` is withheld
    # because the plan already names concept_ids; `description` because
    # nothing downstream reads it.
    columns={"concept": ("id", "taxonomy", "name", "label")},
    inherit_future_tables=False,
    needs_vector_operators=False,
    connection_limit=4,
)

ROLES: tuple[RoleSpec, ...] = (QUERY_MAPPER, RETRIEVAL)


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _quote_literal(value: str) -> str:
    """A string literal for a statement that takes no bind parameter.

    ``ALTER ROLE ... PASSWORD`` is a utility statement and PostgreSQL rejects
    ``$1`` there. Doubling single quotes is the whole of the escaping, because
    ``standard_conforming_strings`` has been on by default since 9.1.
    """
    if "\x00" in value:
        raise ValueError("a password may not contain a NUL byte")
    return "'" + value.replace("'", "''") + "'"


def _statements(spec: RoleSpec, database: str, password: str) -> list[str]:
    """Everything one role needs, in dependency order."""
    role = spec.name
    statements = [
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN;
            END IF;
        END
        $$
        """,
        f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS",
        f"ALTER ROLE {role} CONNECTION LIMIT {spec.connection_limit}",
        # Inlined, not bound -- see _quote_literal. Not logged: log_statement
        # is off by default and this runs by hand.
        f"ALTER ROLE {role} PASSWORD {_quote_literal(password)}",
        f'GRANT CONNECT ON DATABASE "{database}" TO {role}',
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}",
        # Revoke first, so the spec is authoritative: dropping a table from
        # `tables` actually removes the privilege on the next run. This also
        # clears any column grants from a previous spec.
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {role}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} REVOKE SELECT ON TABLES FROM {role}",
    ]

    for table in spec.tables:
        statements.append(f"GRANT SELECT ON {SCHEMA}.{table} TO {role}")
    for table, columns in spec.columns.items():
        statements.append(
            f"GRANT SELECT ({', '.join(columns)}) ON {SCHEMA}.{table} TO {role}"
        )
    if spec.inherit_future_tables:
        statements.append(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {role}"
        )

    if spec.needs_vector_operators:
        statements += [
            f"GRANT USAGE ON SCHEMA public TO {role}",
            f"ALTER ROLE {role} SET search_path = {SCHEMA}, public",
        ]
    else:
        statements += [
            f"REVOKE ALL ON SCHEMA public FROM {role}",
            f"ALTER ROLE {role} SET search_path = {SCHEMA}",
        ]

    statements += [
        f"REVOKE CREATE ON SCHEMA public FROM {role}",
        # Belt and braces: even a mistakenly widened grant cannot become a
        # write, because every transaction this role opens starts read-only.
        f"ALTER ROLE {role} SET default_transaction_read_only = on",
        f"ALTER ROLE {role} SET statement_timeout = '{spec.statement_timeout}'",
        f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = "
        f"'{spec.idle_transaction_timeout}'",
    ]
    return statements


def _drop_legacy_statements(database: str) -> list[str]:
    """Remove the single ``verified_filings_ro`` role the two above replaced.

    ``DROP OWNED BY`` revokes its privileges in this database (it owns no
    objects); the role then drops cleanly. Guarded so a database that never
    had it is untouched.
    """
    return [
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LEGACY_ROLE}') THEN
                EXECUTE 'DROP OWNED BY {LEGACY_ROLE}';
                EXECUTE 'REVOKE ALL ON DATABASE "{database}" FROM {LEGACY_ROLE}';
                EXECUTE 'DROP ROLE {LEGACY_ROLE}';
            END IF;
        END
        $$
        """
    ]


async def provision(url: str, passwords: dict[str, str]) -> list[str]:
    """Create or refresh every role whose password is supplied.

    ``passwords`` maps role name to password; a role missing from it is left
    alone, so a database can carry one role and not the other.
    """
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    database = _database_name(url)
    provisioned: list[str] = []
    try:
        async with engine.connect() as connection:
            for spec in ROLES:
                password = passwords.get(spec.name)
                if not password:
                    continue
                for statement in _statements(spec, database, password):
                    await connection.execute(text(statement))
                provisioned.append(spec.name)
            for statement in _drop_legacy_statements(database):
                await connection.execute(text(statement))
            await connection.execute(
                text(f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC')
            )
    finally:
        await engine.dispose()
    return provisioned


async def describe(url: str) -> dict[str, dict[str, object]]:
    """What the cluster currently says about each role, for ``--check``."""
    engine = create_async_engine(url)
    report: dict[str, dict[str, object]] = {}
    try:
        async with engine.connect() as connection:
            for spec in ROLES:
                row = (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolconnlimit, "
                            "rolconfig FROM pg_roles WHERE rolname = :role"
                        ),
                        {"role": spec.name},
                    )
                ).first()
                if row is None:
                    report[spec.name] = {"exists": False}
                    continue
                grants = (
                    await connection.execute(
                        text(
                            "SELECT table_name, string_agg(DISTINCT column_name, ', ' "
                            "ORDER BY column_name) FROM information_schema.column_privileges "
                            "WHERE grantee = :role AND table_schema = :schema "
                            "GROUP BY table_name ORDER BY table_name"
                        ),
                        {"role": spec.name, "schema": SCHEMA},
                    )
                ).all()
                report[spec.name] = {
                    "exists": True,
                    "superuser": row[0],
                    "createdb": row[1],
                    "createrole": row[2],
                    "connection_limit": row[3],
                    "settings": list(row[4] or []),
                    "grants": {table: columns for table, columns in grants},
                }
            legacy = (
                await connection.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                    {"role": LEGACY_ROLE},
                )
            ).first()
            report[LEGACY_ROLE] = {"exists": legacy is not None}
    finally:
        await engine.dispose()
    return report


def passwords_from_settings() -> dict[str, str]:
    """Each role's password, taken from the URL the application connects with.

    Read from the connection URL rather than a separate variable so the two
    cannot drift -- provisioning a password nothing connects with is a failure
    that looks like success.
    """
    urls = {
        QUERY_MAPPER.name: settings.database_url_query_mapper,
        RETRIEVAL.name: settings.database_url_retrieval,
    }
    return {
        role: urlsplit(url).password
        for role, url in urls.items()
        if url and urlsplit(url).password
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the read-only database roles.")
    parser.add_argument(
        "--url",
        default=settings.database_url,
        help="superuser URL to provision against (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--check", action="store_true", help="report each role's state and change nothing"
    )
    args = parser.parse_args(argv)

    if args.check:
        report = asyncio.run(describe(args.url))
        missing = False
        for spec in ROLES:
            state = report[spec.name]
            if not state["exists"]:
                print(f"{spec.name}: NOT PRESENT -- run `uv run python -m app.db.roles`")
                missing = True
                continue
            print(f"{spec.name}: present  ({spec.used_by})")
            print(f"  superuser={state['superuser']} createdb={state['createdb']} "
                  f"createrole={state['createrole']} connections={state['connection_limit']}")
            for table, columns in sorted(state["grants"].items()):
                print(f"  {table}: {columns}")
            for setting in state["settings"]:
                print(f"  {setting}")
        if report[LEGACY_ROLE]["exists"]:
            print(f"{LEGACY_ROLE}: still present -- re-run to drop it")
            missing = True
        return 1 if missing else 0

    passwords = passwords_from_settings()
    if not passwords:
        print(
            "Neither DATABASE_URL_QUERY_MAPPER nor DATABASE_URL_RETRIEVAL is set (or "
            "neither carries a password), so there is nothing to provision.\n"
            "Add them to .env -- see .env.example -- then re-run.",
            file=sys.stderr,
        )
        return 2

    provisioned = asyncio.run(provision(args.url, passwords))
    for role in provisioned:
        print(f"{role}: provisioned against {_database_name(args.url)}")
    for spec in ROLES:
        if spec.name not in provisioned:
            print(f"{spec.name}: skipped, no password in the environment", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
