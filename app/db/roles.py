"""Provision the database roles: two read-only readers of ``xbrl``, and the Web
Server's writer of ``web``.

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
    ``app/retrieval/``. Executes SQL *written by Qwen*, so it holds SELECT on
    exactly one relation: the ``xbrl.reported_fact`` view. Generated SQL
    therefore cannot name ``fact``, ``filing``, ``concept`` or ``company`` at
    all, which is what makes the view's baked-in ``is_latest`` filter and
    joins unskippable rather than merely conventional. The view also withholds
    ``concept.embedding`` -- by the time this role runs, the QueryPlan already
    names concrete ``concept_id``s, so it never needs 1,904 x 768 floats.
    Capped connections, and no automatic grant on tables added later.
    See ``app/retrieval/DESIGN.md`` 3 and 6.

Neither gets ``load_run``, an append-only log of every load that ever ran.
Nothing that answers a question has any business reading it.

``vf_web_role``
    ``app/api/``. The one role that writes, and only in ``web``: users,
    sessions, conversations, jobs. Nothing in ``xbrl``, table by table grants
    in ``web`` -- see ``app/api/DESIGN.md`` §11 for the grid.

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
version control. See ``docs/ALEMBIC.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

#: Schema holding the data. The two readers read from here and nowhere else.
SCHEMA = "xbrl"

#: Every schema a role is kept out of unless its spec names it. A role is
#: revoked from each one it does not own, so no schema is reachable by accident.
MANAGED_SCHEMAS = ("xbrl", "web")

# Quotes only where PostgreSQL needs it: "user" is reserved, "company" is not.
_quote = postgresql.dialect().identifier_preparer.quote

#: Superseded by the two roles below. Dropped on provision so an unused login
#: with SELECT on everything does not sit around.
LEGACY_ROLE = "verified_filings_ro"


@dataclass(frozen=True)
class WriteGrant:
    """One write privilege on one table, optionally narrowed to columns."""

    table: str
    privilege: Literal["INSERT", "UPDATE", "DELETE"]
    columns: tuple[str, ...] = ()  # empty = the whole table

    def __post_init__(self) -> None:
        if self.privilege == "DELETE" and self.columns:
            raise ValueError(f"DELETE on {self.table} cannot be narrowed to columns")


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

    #: The one schema this role is granted anything in. The defaults below are
    #: the readers' behaviour, so a spec that names none of them is read-only.
    schema: str = SCHEMA
    writes: tuple[WriteGrant, ...] = ()
    read_only: bool = True  # every transaction starts read-only

    def __post_init__(self) -> None:
        if self.schema not in MANAGED_SCHEMAS:
            raise ValueError(f"{self.name}: schema {self.schema!r} is not managed")
        if self.read_only and self.writes:
            # "read-only" must keep meaning it: a write needs read_only=False said out loud.
            raise ValueError(f"{self.name} is read-only and names a write")
        if self.inherit_future_tables and self.writes:
            raise ValueError(f"{self.name}: a writer never inherits future tables")


QUERY_MAPPER = RoleSpec(
    name="vf_query_mapper_role",
    used_by="app/semantic/query_mapper.py",
    # Five named relations, split by what they are for.
    #
    # `reported_fact` is where the mapper proves **coverage**, because it is
    # the relation `app/retrieval/` reads. Proving a value exists against
    # `fact` while retrieval reads the view would be proving something about a
    # different set of rows -- the view holds 9,425 synthesized fourth quarters
    # that `fact` does not -- and a plan the retrieval step cannot fulfil comes
    # back as a data problem when it is really a disagreement between roles.
    #
    # The base tables stay for what the view deliberately withholds, all of it
    # lexicon rather than values: `company.sic_*` for group selection,
    # `concept.embedding` for the pgvector fallback, and `filing`'s fiscal
    # labels for discovering each filer's calendar. Those are how the mapper
    # decides *what to ask for*; the view is how it checks the answer is there.
    tables=("company", "filing", "fact", "concept", "reported_fact"),
    # False, not a convenience. `ALTER DEFAULT PRIVILEGES ... ON TABLES` covers
    # views too, so with this on, every view added later was silently granted
    # to this role and not to the retrieval one -- verified. Which relations a
    # role can read should be a decision, not a side effect of creation order.
    inherit_future_tables=False,
    needs_vector_operators=True,
)

RETRIEVAL = RoleSpec(
    name="vf_retrieval_role",
    used_by="app/retrieval/ (executes Qwen-written SQL)",
    # One relation, and it is a view. Generated SQL cannot *name* `fact`,
    # `filing` or `concept`, so the `is_latest` filter and the three joins
    # cannot be got wrong by omitting them -- and no column called
    # `fiscal_year` is in reach to be mistaken for a period (docs/GAPS.md D1.1).
    # `REVOKE ALL ON ALL TABLES IN SCHEMA` covers views, and it runs before
    # these grants, so this narrowing takes effect on the next provision.
    # The view's own SELECT withholds `concept.embedding` and
    # `concept.description`, which is why no column grant is needed here.
    tables=("reported_fact",),
    inherit_future_tables=False,
    needs_vector_operators=False,
    connection_limit=4,
)

#: Every column of web.user but is_superuser: only the owner's credential can
#: make an administrator, whatever a bug in a route does.
_USER_INSERTABLE = ("id", "email", "hashed_password", "is_active", "is_verified", "created_at")
_USER_UPDATABLE = ("email", "hashed_password", "is_active", "is_verified")

WEB = RoleSpec(
    name="vf_web_role",
    used_by="app/api/ (the Web Server)",
    schema="web",
    # SELECT. Absent on purpose: job_trace (write-only, so no route can leak a
    # trace) and job_feedback (written, never read back).
    tables=("user", "oauth_account", "access_token", "invite", "conversation", "job"),
    writes=(
        WriteGrant("user", "INSERT", _USER_INSERTABLE),
        WriteGrant("user", "UPDATE", _USER_UPDATABLE),
        WriteGrant("oauth_account", "INSERT"),
        WriteGrant("oauth_account", "UPDATE"),  # GitHub tokens refresh on each sign-in
        WriteGrant("access_token", "INSERT"),
        WriteGrant("access_token", "DELETE"),  # sign-out, and expired-session cleanup
        WriteGrant("invite", "UPDATE", ("used_at", "used_by")),  # spend one, never make one
        WriteGrant("conversation", "INSERT"),
        WriteGrant("job", "INSERT"),
        WriteGrant("job", "UPDATE"),  # status, reply, finished_at as the job runs
        WriteGrant("job_trace", "INSERT"),
        WriteGrant("job_feedback", "INSERT"),
    ),
    read_only=False,
    inherit_future_tables=False,
    needs_vector_operators=False,
    connection_limit=10,
)

ROLES: tuple[RoleSpec, ...] = (QUERY_MAPPER, RETRIEVAL, WEB)


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
    schema = spec.schema
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
        f"GRANT USAGE ON SCHEMA {schema} TO {role}",
        # Revoke first, so the spec is authoritative: dropping a table from
        # `tables` actually removes the privilege on the next run. This also
        # clears any column grants from a previous spec.
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}",
        # A reader was only ever given future SELECT; a writer could have been
        # given more, so its revoke covers everything.
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE "
        f"{'SELECT' if spec.read_only else 'ALL'} ON TABLES FROM {role}",
    ]
    statements += [_revoke_schema(other, role) for other in MANAGED_SCHEMAS if other != schema]

    for table in spec.tables:
        statements.append(f"GRANT SELECT ON {schema}.{_quote(table)} TO {role}")
    for table, columns in spec.columns.items():
        statements.append(
            f"GRANT SELECT ({', '.join(map(_quote, columns))}) ON {schema}.{_quote(table)} "
            f"TO {role}"
        )
    for grant in spec.writes:
        narrowed = f" ({', '.join(map(_quote, grant.columns))})" if grant.columns else ""
        statements.append(
            f"GRANT {grant.privilege}{narrowed} ON {schema}.{_quote(grant.table)} TO {role}"
        )
    if spec.inherit_future_tables:
        statements.append(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role}"
        )

    if spec.needs_vector_operators:
        statements += [
            f"GRANT USAGE ON SCHEMA public TO {role}",
            f"ALTER ROLE {role} SET search_path = {schema}, public",
        ]
    else:
        statements += [
            f"REVOKE ALL ON SCHEMA public FROM {role}",
            f"ALTER ROLE {role} SET search_path = {schema}",
        ]

    statements.append(f"REVOKE CREATE ON SCHEMA public FROM {role}")
    if not spec.read_only:
        # A writer may change rows, never the schema holding them.
        statements.append(f"REVOKE CREATE ON SCHEMA {schema} FROM {role}")
    # Belt and braces for a reader: even a mistakenly widened grant cannot
    # become a write, because every transaction it opens starts read-only. A
    # writer is narrowed by its grants alone, so it says so explicitly.
    statements += [
        f"ALTER ROLE {role} SET default_transaction_read_only = "
        f"{'on' if spec.read_only else 'off'}",
        f"ALTER ROLE {role} SET statement_timeout = '{spec.statement_timeout}'",
        f"ALTER ROLE {role} SET idle_in_transaction_session_timeout = "
        f"'{spec.idle_transaction_timeout}'",
    ]
    return statements


def _revoke_schema(schema: str, role: str) -> str:
    """Everything in a schema this role does not own, taken away.

    Guarded, so provisioning still runs on a database whose ``web`` schema has
    not been migrated in yet.
    """
    return f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '{schema}') THEN
                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role}';
                EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} '
                     || 'REVOKE ALL ON TABLES FROM {role}';
                EXECUTE 'REVOKE ALL ON SCHEMA {schema} FROM {role}';
            END IF;
        END
        $$
        """


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
            # PostgreSQL grants every role USAGE on `public` through PUBLIC, so the
            # per-role `REVOKE ALL ON SCHEMA public` above cannot take on its own.
            # A role that needs `public` (the mapper, for pgvector) is granted it
            # by name.
            await connection.execute(text("REVOKE USAGE ON SCHEMA public FROM PUBLIC"))
    finally:
        await engine.dispose()
    return provisioned


async def describe(url: str) -> dict[str, dict[str, object]]:
    """What the cluster currently says about each role, for ``--check``."""
    engine = create_async_engine(url)
    report: dict[str, dict[str, object]] = {}
    schemas = [*MANAGED_SCHEMAS, "public"]
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
                report[spec.name] = {
                    "exists": True,
                    "superuser": row[0],
                    "createdb": row[1],
                    "createrole": row[2],
                    "connection_limit": row[3],
                    "settings": list(row[4] or []),
                    "grants": await _grants(connection, spec.name),
                    "schemas": await _schema_privileges(connection, spec.name, schemas),
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


async def _grants(connection, role: str) -> dict[str, list[str]]:
    """Every table privilege in every managed schema: ``{"web.job": ["INSERT",
    "SELECT", "UPDATE(status)"]}``. Not just SELECT, and not just ``xbrl`` -- a
    stray write, or anything in a schema the role should not see, must show."""
    params = {"role": role, "schemas": list(MANAGED_SCHEMAS)}
    whole = (
        await connection.execute(
            text(
                "SELECT table_schema, table_name, privilege_type "
                "FROM information_schema.table_privileges "
                "WHERE grantee = :role AND table_schema = ANY(:schemas)"
            ),
            params,
        )
    ).all()
    # column_privileges also lists every column of a whole-table grant, so
    # only what is not already covered by one is a column grant.
    narrowed = (
        await connection.execute(
            text(
                "SELECT table_schema, table_name, privilege_type, "
                "string_agg(column_name, ', ' ORDER BY column_name) "
                "FROM information_schema.column_privileges "
                "WHERE grantee = :role AND table_schema = ANY(:schemas) "
                "GROUP BY table_schema, table_name, privilege_type"
            ),
            params,
        )
    ).all()
    covered = {(schema, table, privilege) for schema, table, privilege in whole}
    grants: dict[str, list[str]] = {}
    for schema, table, privilege in whole:
        grants.setdefault(f"{schema}.{table}", []).append(privilege)
    for schema, table, privilege, columns in narrowed:
        if (schema, table, privilege) not in covered:
            grants.setdefault(f"{schema}.{table}", []).append(f"{privilege}({columns})")
    return {table: sorted(privileges) for table, privileges in sorted(grants.items())}


async def _schema_privileges(connection, role: str, schemas: list[str]) -> dict[str, list[str]]:
    """USAGE and CREATE per schema that exists. A reader holding USAGE on
    ``web``, or anyone holding CREATE, is a finding."""
    rows = (
        await connection.execute(
            text(
                "SELECT nspname, has_schema_privilege(:role, nspname, 'USAGE'), "
                "has_schema_privilege(:role, nspname, 'CREATE') "
                "FROM pg_namespace WHERE nspname = ANY(:schemas) ORDER BY nspname"
            ),
            {"role": role, "schemas": schemas},
        )
    ).all()
    return {
        name: [p for p, held in (("USAGE", usage), ("CREATE", create)) if held]
        for name, usage, create in rows
    }


#: The setting each role's password is read from, and the variable it names.
_URL_SETTINGS = {
    QUERY_MAPPER.name: "database_url_query_mapper",
    RETRIEVAL.name: "database_url_retrieval",
    WEB.name: "database_url_web",
}


def passwords_from_settings() -> dict[str, str]:
    """Each role's password, taken from the URL the application connects with.

    Read from the connection URL rather than a separate variable so the two
    cannot drift -- provisioning a password nothing connects with is a failure
    that looks like success.
    """
    urls = {role: getattr(settings, name) for role, name in _URL_SETTINGS.items()}
    return {
        role: urlsplit(url).password
        for role, url in urls.items()
        if url and urlsplit(url).password
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the database roles.")
    parser.add_argument(
        "--url",
        default=settings.database_url,
        help="superuser URL to provision against (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--check", action="store_true", help="report each role's state and change nothing"
    )
    args = parser.parse_args(argv)
    configured = passwords_from_settings()

    if args.check:
        report = asyncio.run(describe(args.url))
        missing = False
        for spec in ROLES:
            state = report[spec.name]
            if not state["exists"]:
                if spec.name not in configured:
                    # Not set up here yet (the web role, before its migration): not a fault.
                    print(f"{spec.name}: not configured ({_URL_SETTINGS[spec.name].upper()})")
                    continue
                print(f"{spec.name}: NOT PRESENT -- run `uv run python -m app.db.roles`")
                missing = True
                continue
            print(f"{spec.name}: present  ({spec.used_by})")
            print(f"  superuser={state['superuser']} createdb={state['createdb']} "
                  f"createrole={state['createrole']} connections={state['connection_limit']}")
            for schema, privileges in state["schemas"].items():
                print(f"  schema {schema}: {', '.join(privileges) or 'none'}")
            for table, privileges in state["grants"].items():
                print(f"  {table}: {', '.join(privileges)}")
            for setting in state["settings"]:
                print(f"  {setting}")
        if report[LEGACY_ROLE]["exists"]:
            print(f"{LEGACY_ROLE}: still present -- re-run to drop it")
            missing = True
        return 1 if missing else 0

    if not configured:
        names = ", ".join(name.upper() for name in _URL_SETTINGS.values())
        print(
            f"None of {names} is set with a password, so there is nothing to "
            "provision. Add them to .env -- see .env.example -- then re-run.",
            file=sys.stderr,
        )
        return 2

    provisioned = asyncio.run(provision(args.url, configured))
    for role in provisioned:
        print(f"{role}: provisioned against {_database_name(args.url)}")
    for spec in ROLES:
        if spec.name not in provisioned:
            print(f"{spec.name}: skipped, no password in the environment", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
