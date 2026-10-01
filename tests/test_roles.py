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

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import roles

PASSWORDS = {
    roles.QUERY_MAPPER.name: "mapper-pw",
    roles.RETRIEVAL.name: "retrieval-pw",
    roles.WEB.name: "web-pw",  # needs the web migration on the test database (docs/STARTUP.md)
    roles.ADMIN.name: "admin-pw",
}

#: 768 floats, matching the embedding column. A shorter literal fails on
#: dimension rather than on privilege, which would make the test lie.
VECTOR = "[" + ",".join(["0.01"] * 768) + "]"


def _role_url(superuser_url: str, role: str) -> str:
    scheme, rest = superuser_url.split("://", 1)
    _, host_and_path = rest.split("@", 1)
    return f"{scheme}://{role}:{PASSWORDS[role]}@{host_and_path}"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def provisioned(test_db_url):
    """Every role, created against the test database.

    Provisioned here rather than assumed, so these cover ``app/db/roles.py``
    itself and the suite needs no setup step of its own.

    Once per module: no test here alters a role -- session settings end with
    their connection and the web role's statements are rolled back -- so every
    test sees the grants exactly as provisioned. Re-running provision is
    tested on its own, by `test_provisioning_is_idempotent`.
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
    grants = report[roles.RETRIEVAL.name]["grants"]
    expected = {*roles.RETRIEVAL.tables, *roles.RETRIEVAL.columns}
    assert set(grants) == {f"{roles.RETRIEVAL.schema}.{table}" for table in expected}
    assert all(privileges == ["SELECT"] for privileges in grants.values())  # nothing else, anywhere


async def test_row_security_is_on_with_every_policy(provisioned) -> None:
    report = (await roles.describe(provisioned))[roles.ROW_SECURITY]
    for table in roles.ROW_SECURED:
        state = report[f"web.{table}"]
        assert state["enabled"] is True, table
        named = {line.split(":", 1)[0] for line in state["policies"]}
        assert named == {p.name for p in roles.ROW_POLICIES if p.table == table}


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
        # Named in full, so search_path is no defence: only the schema grant is.
        ("qualified vector type", f"SELECT '{VECTOR}'::public.vector"),
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
    out of reach, since Filing's is provenance, not a period (docs/GAPS.md D1.1)."""
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


# --------------------------------------------------------------------------- #
# The web role -- the one that writes (app/api/DESIGN.md §11)
# --------------------------------------------------------------------------- #

WEB = roles.WEB.name
USER_ID = "00000000-0000-0000-0000-00000000000a"
JOB_ID = "00000000-0000-0000-0000-00000000000b"

#: Rows the statements under test need, inserted as the web role itself --
#: which is also the proof it may insert them. Every check is rolled back.
SETUP = [
    'INSERT INTO "user" (id, email, hashed_password, is_active, is_verified) '
    f"VALUES ('{USER_ID}', 'zz-roles@example.test', 'x', true, false)",
    f"INSERT INTO conversation (id, user_id, question) VALUES ('{USER_ID}', '{USER_ID}', 'q')",
    "INSERT INTO job (id, conversation_id, round, status) "
    f"VALUES ('{JOB_ID}', '{USER_ID}', 1, 'queued')",
]

TRACE = (
    "INSERT INTO job_trace (job_id, code_version, models, model_calls, statements, timings, "
    f"errors) VALUES ('{JOB_ID}', 'abc', '{{}}', '[]', '[]', '{{}}', '[]')"
)


async def _as_web(url: str, statement: str) -> None:
    """Run the setup and one statement as the web role, inside a transaction
    that is rolled back whatever happens."""
    engine = _engine(url, WEB)
    try:
        async with engine.connect() as connection:
            try:
                for setup in SETUP:
                    await connection.execute(text(setup))
                await connection.execute(text(statement))
            finally:
                await connection.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("read a user", 'SELECT email, is_superuser FROM "user"'),
        ("change a password", "UPDATE \"user\" SET hashed_password = 'y'"),
        ("verify a user", 'UPDATE "user" SET is_verified = true'),
        (
            "start a session",
            "INSERT INTO access_token (token, user_id, created_at) "
            f"VALUES ('t', '{USER_ID}', now())",
        ),
        ("sign out", "DELETE FROM access_token"),
        ("spend an invite", f"UPDATE invite SET used_at = now(), used_by = '{USER_ID}'"),
        ("move a job", "UPDATE job SET status = 'parsing'"),
        ("write a trace", TRACE),
        (
            "report a problem",
            "INSERT INTO job_feedback (id, job_id, user_id, note) "
            f"VALUES ('{USER_ID}', '{JOB_ID}', '{USER_ID}', 'wrong year')",
        ),
    ],
)
async def test_the_web_role_can_do_its_job(provisioned, label: str, statement: str) -> None:
    """The grid's allowed cells (DESIGN §11), exercised as the web role."""
    await _as_web(provisioned, statement)


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        # Nothing in xbrl, by any route.
        ("read xbrl facts", "SELECT count(*) FROM xbrl.fact"),
        ("read the retrieval view", "SELECT count(*) FROM xbrl.reported_fact"),
        # Its three guard rails of its own (DESIGN §11).
        ("make an administrator", 'UPDATE "user" SET is_superuser = true'),
        (
            "insert an administrator",
            'INSERT INTO "user" (id, email, hashed_password, is_active, is_verified, '
            "is_superuser) VALUES (gen_random_uuid(), 'zz-admin@example.test', 'x', true, true, "
            "true)",
        ),
        (
            "create an invite",
            "INSERT INTO invite (id, kind, github_account_id) "
            "VALUES (gen_random_uuid(), 'github', '1')",
        ),
        ("read a trace", "SELECT count(*) FROM job_trace"),
        # Everything else outside the grid.
        ("rewrite an invite's address", "UPDATE invite SET email = 'x@example.test'"),
        ("delete a user", 'DELETE FROM "user"'),
        ("delete a conversation", "DELETE FROM conversation"),
        ("rewrite a question", "UPDATE conversation SET question = 'other'"),
        ("delete a job", "DELETE FROM job"),
        ("rewrite a trace", "UPDATE job_trace SET code_version = 'x'"),
        ("read feedback", "SELECT count(*) FROM job_feedback"),
        ("read the audit log", "SELECT count(*) FROM admin_action"),
        (
            "write the audit log",
            "INSERT INTO admin_action (id, admin_email, action) "
            "VALUES (gen_random_uuid(), 'x', 'deactivate')",
        ),
        ("revoke an invite", "UPDATE invite SET revoked_at = now()"),
        ("create in web", "CREATE TABLE web.should_not_exist (i int)"),
        ("create in public", "CREATE TABLE public.should_not_exist (i int)"),
    ],
)
async def test_the_web_role_cannot_leave_its_grid(provisioned, label: str, statement: str) -> None:
    with pytest.raises(DBAPIError) as caught:
        await _as_web(provisioned, statement)
    assert "InsufficientPrivilege" in str(caught.value), label


@pytest.mark.parametrize("role", [roles.QUERY_MAPPER.name, roles.RETRIEVAL.name])
async def test_neither_reader_can_see_web(provisioned, role: str) -> None:
    engine = _engine(provisioned, role)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(text('SELECT count(*) FROM web."user"'))
            assert "InsufficientPrivilege" in str(caught.value)
    finally:
        await engine.dispose()


async def test_the_web_role_is_the_one_that_writes(provisioned) -> None:
    engine = _engine(provisioned, WEB)
    try:
        async with engine.connect() as connection:
            read_only = await connection.execute(text("SHOW default_transaction_read_only"))
            path = await connection.execute(text("SHOW search_path"))
            assert (read_only.scalar_one(), path.scalar_one()) == ("off", "web")
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# The web role through the real code paths, not hand-written SQL
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def web_session(provisioned):
    """An ORM session as the web role; rows it commits are deleted as the owner."""
    owner = create_async_engine(provisioned)

    async def _clean() -> None:
        async with owner.begin() as connection:
            await connection.execute(text("DELETE FROM web.\"user\" WHERE email LIKE 'zz-orm-%'"))

    await _clean()
    engine = _engine(provisioned, WEB)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await _clean()
        await owner.dispose()


async def test_the_sign_in_library_creates_a_user_as_the_web_role(web_session) -> None:
    """FastAPI Users' own create path, which the web role must support without
    being able to write is_superuser -- the reason it is a database default."""
    from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase

    from app.db.web import User

    async with web_session() as session:
        user = await SQLAlchemyUserDatabase(session, User).create(
            {"email": "zz-orm-user@example.test", "hashed_password": "x"}
        )
        assert user.is_superuser is False and user.created_at is not None


async def test_a_trace_and_a_report_are_written_without_reading_back(web_session) -> None:
    """job_trace and job_feedback grant INSERT and no SELECT. An ORM insert that
    fetched a server default back (RETURNING created_at) would need SELECT."""
    from app.db.web import Conversation, Job, JobFeedback, JobTrace, User

    user_id, conversation_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with web_session() as session:
        session.add(
            User(
                id=user_id, email="zz-orm-trace@example.test", hashed_password="x",
                is_active=True, is_verified=False,
            )
        )
        await session.flush()  # no relationships declared, so parents are flushed first
        session.add(Conversation(id=conversation_id, user_id=user_id, question="q"))
        await session.flush()
        session.add(Job(id=job_id, conversation_id=conversation_id, round=1, status="queued"))
        await session.flush()
        session.add(
            JobTrace(
                job_id=job_id, code_version="abc", models={}, model_calls=[],
                statements=[], timings={}, errors=[],
            )
        )
        session.add(JobFeedback(job_id=job_id, user_id=user_id, note="wrong year"))
        await session.commit()


# --------------------------------------------------------------------------- #
# The spec's own guard rails -- no database needed
# --------------------------------------------------------------------------- #


def test_a_read_only_spec_cannot_name_a_write() -> None:
    write = roles.WriteGrant("job", "INSERT")
    with pytest.raises(ValueError, match="read-only and names a write"):
        roles.RoleSpec(name="r", used_by="t", tables=(), schema="web", writes=(write,))


def test_a_writer_never_inherits_future_tables() -> None:
    write = roles.WriteGrant("job", "INSERT")
    with pytest.raises(ValueError, match="never inherits"):
        roles.RoleSpec(
            name="r", used_by="t", tables=(), schema="web", writes=(write,),
            read_only=False, inherit_future_tables=True,
        )


def test_a_delete_cannot_be_narrowed_to_columns() -> None:
    with pytest.raises(ValueError, match="cannot be narrowed"):
        roles.WriteGrant("job", "DELETE", ("status",))


def test_a_role_lives_in_a_managed_schema() -> None:
    with pytest.raises(ValueError, match="not managed"):
        roles.RoleSpec(name="r", used_by="t", tables=(), schema="public")


def test_password_literal_escapes_a_quote() -> None:
    """``ALTER ROLE ... PASSWORD`` takes no bind parameter, so the literal is
    built by hand and has to survive a quote in the password."""
    assert roles._quote_literal("a'b") == "'a''b'"
    with pytest.raises(ValueError, match="NUL"):
        roles._quote_literal("a\x00b")


# --------------------------------------------------------------------------- #
# The admin role: its grid, and the row security that keeps it off admins
# --------------------------------------------------------------------------- #

ADMIN = roles.ADMIN.name
READER_ID = "00000000-0000-0000-0000-0000000000a1"
ADMIN_ID = "00000000-0000-0000-0000-0000000000a2"
READER_JOB = "00000000-0000-0000-0000-0000000000a3"
INVITE_ID = "00000000-0000-0000-0000-0000000000a4"
READER = f"id = '{READER_ID}'"
AN_ADMIN = f"id = '{ADMIN_ID}'"


@pytest_asyncio.fixture
async def admin_rows(provisioned):
    """A reader with a question, a trace, a report and a session; an
    administrator with a session; an invite -- committed as the owner, since the
    admin role may create none of them, and deleted afterwards."""
    owner = create_async_engine(provisioned)

    async def clean() -> None:
        async with owner.begin() as connection:
            await connection.execute(text(f"DELETE FROM web.invite WHERE id = '{INVITE_ID}'"))
            await connection.execute(
                text(f"DELETE FROM web.\"user\" WHERE id IN ('{READER_ID}', '{ADMIN_ID}')")
            )

    await clean()
    async with owner.begin() as connection:
        for statement in (
            'INSERT INTO web."user" (id, email, hashed_password, is_active, is_verified, '
            f"is_superuser) VALUES ('{READER_ID}', 'zz-roles-reader@example.test', 'x', true, "
            f"false, false), ('{ADMIN_ID}', 'zz-roles-admin@example.test', 'x', true, false, "
            "true)",
            "INSERT INTO web.conversation (id, user_id, question) "
            f"VALUES ('{READER_ID}', '{READER_ID}', 'q')",
            "INSERT INTO web.job (id, conversation_id, round, status) "
            f"VALUES ('{READER_JOB}', '{READER_ID}', 1, 'queued')",
            TRACE.replace("INTO job_trace", "INTO web.job_trace").replace(JOB_ID, READER_JOB),
            "INSERT INTO web.job_feedback (id, job_id, user_id) "
            f"VALUES ('{READER_ID}', '{READER_JOB}', '{READER_ID}')",
            "INSERT INTO web.access_token (token, user_id, created_at) "
            f"VALUES ('zz-reader-token', '{READER_ID}', now()), "
            f"('zz-admin-token', '{ADMIN_ID}', now())",
            "INSERT INTO web.invite (id, kind, email, code_hash, expires_at) "
            f"VALUES ('{INVITE_ID}', 'email', 'zz-roles-invite@example.test', 'h', "
            "now() + interval '1 day')",
        ):
            await connection.execute(text(statement))
    try:
        yield provisioned
    finally:
        await clean()
        await owner.dispose()


async def _as_admin(url: str, *statements: str) -> list:
    """Run statements as the admin role in one transaction, rolled back whatever
    happens. Each result: the rows a SELECT returned, else the rows changed."""
    engine = _engine(url, ADMIN)
    results = []
    try:
        async with engine.connect() as connection:
            try:
                for statement in statements:
                    result = await connection.execute(text(statement))
                    results.append(result.all() if result.returns_rows else result.rowcount)
            finally:
                await connection.rollback()
    finally:
        await engine.dispose()
    return results


@pytest.mark.parametrize("table", roles.ADMIN.tables)
async def test_the_admin_role_reads_all_of_web(admin_rows, table: str) -> None:
    (rows,) = await _as_admin(admin_rows, f'SELECT count(*) FROM "{table}"')
    assert rows[0][0] >= 0


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("deactivate a reader", f'UPDATE "user" SET is_active = false WHERE {READER}'),
        ("reset a reader's password", f"UPDATE \"user\" SET hashed_password = 'y' WHERE {READER}"),
        ("end a reader's sessions", f"DELETE FROM access_token WHERE user_{READER}"),
        ("revoke an invite", f"UPDATE invite SET revoked_at = now() WHERE id = '{INVITE_ID}'"),
        (
            "create an invite",
            "INSERT INTO invite (id, kind, email, code_hash, expires_at, created_by) VALUES "
            f"(gen_random_uuid(), 'email', 'zz-x@example.test', 'h2', now(), '{ADMIN_ID}')",
        ),
        (
            "write the audit log",
            "INSERT INTO admin_action (id, admin_id, admin_email, action, target_email) VALUES "
            f"(gen_random_uuid(), '{ADMIN_ID}', 'zz-roles-admin@example.test', 'deactivate', 'x')",
        ),
    ],
)
async def test_the_admin_role_can_do_its_job(admin_rows, label: str, statement: str) -> None:
    (changed,) = await _as_admin(admin_rows, statement)
    assert changed == 1, label


async def test_deleting_a_reader_takes_everything_of_theirs(admin_rows) -> None:
    """The admin role has no DELETE on conversations, jobs, traces or reports;
    the foreign keys' cascades remove them anyway, as the table owner would."""
    deleted, *left = await _as_admin(
        admin_rows,
        f'DELETE FROM "user" WHERE {READER}',
        f"SELECT count(*) FROM conversation WHERE user_{READER}",
        f"SELECT count(*) FROM job WHERE id = '{READER_JOB}'",
        f"SELECT count(*) FROM job_trace WHERE job_id = '{READER_JOB}'",
        f"SELECT count(*) FROM job_feedback WHERE user_{READER}",
        f"SELECT count(*) FROM access_token WHERE user_{READER}",
    )
    assert deleted == 1
    assert [rows[0][0] for rows in left] == [0, 0, 0, 0, 0]


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("deactivate an admin", f'UPDATE "user" SET is_active = false WHERE {AN_ADMIN}'),
        (
            "reset an admin's password",
            f"UPDATE \"user\" SET hashed_password = 'y' WHERE {AN_ADMIN}",
        ),
        ("delete an admin", f'DELETE FROM "user" WHERE {AN_ADMIN}'),
        ("end an admin's sessions", f"DELETE FROM access_token WHERE user_{AN_ADMIN}"),
    ],
)
async def test_the_admin_role_cannot_touch_an_admin(admin_rows, label: str, statement: str) -> None:
    """Row security, not a grant: the statement runs and changes nothing -- which
    is why the admin routes must check how many rows changed."""
    (changed,) = await _as_admin(admin_rows, statement)
    assert changed == 0, label


async def test_the_admin_role_still_sees_an_admin(admin_rows) -> None:
    (rows,) = await _as_admin(admin_rows, f'SELECT is_superuser FROM "user" WHERE {AN_ADMIN}')
    assert rows == [(True,)]


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        # Its guard rails: no administrators, no accounts, no sessions of its own making.
        ("make an administrator", f'UPDATE "user" SET is_superuser = true WHERE {READER}'),
        (
            "create a user",
            'INSERT INTO "user" (id, email, hashed_password, is_active, is_verified) '
            "VALUES (gen_random_uuid(), 'zz-new@example.test', 'x', true, false)",
        ),
        ("change an email", "UPDATE \"user\" SET email = 'zz-other@example.test'"),
        ("verify a user", 'UPDATE "user" SET is_verified = true'),
        (
            "start a session as someone",
            "INSERT INTO access_token (token, user_id, created_at) "
            f"VALUES ('zz-forged', '{READER_ID}', now())",
        ),
        ("relink a GitHub account", "UPDATE oauth_account SET account_id = 'x'"),
        # An invite: made and revoked, never spent or rewritten.
        ("spend an invite", "UPDATE invite SET used_at = now()"),
        ("rewrite an invite", "UPDATE invite SET code_hash = 'x'"),
        ("delete an invite", "DELETE FROM invite"),
        # The audit log: added to, never changed.
        ("rewrite the audit log", "UPDATE admin_action SET action = 'reactivate'"),
        ("delete from the audit log", "DELETE FROM admin_action"),
        # Questions and their records: read, never written.
        ("rewrite a question", "UPDATE conversation SET question = 'other'"),
        ("delete a conversation", "DELETE FROM conversation"),
        ("move a job", "UPDATE job SET status = 'done'"),
        ("delete a job", "DELETE FROM job"),
        ("write a trace", TRACE),
        ("rewrite a trace", "UPDATE job_trace SET code_version = 'x'"),
        ("delete a report", "DELETE FROM job_feedback"),
        # Nothing outside web, nothing structural.
        ("read xbrl facts", "SELECT count(*) FROM xbrl.fact"),
        ("create in web", "CREATE TABLE web.should_not_exist (i int)"),
        ("create in public", "CREATE TABLE public.should_not_exist (i int)"),
    ],
)
async def test_the_admin_role_cannot_leave_its_grid(admin_rows, label: str, statement: str) -> None:
    with pytest.raises(DBAPIError) as caught:
        await _as_admin(admin_rows, statement)
    assert "InsufficientPrivilege" in str(caught.value), label


async def test_the_admin_role_writes_and_lives_in_web(provisioned) -> None:
    engine = _engine(provisioned, ADMIN)
    try:
        async with engine.connect() as connection:
            read_only = await connection.execute(text("SHOW default_transaction_read_only"))
            path = await connection.execute(text("SHOW search_path"))
            assert (read_only.scalar_one(), path.scalar_one()) == ("off", "web")
    finally:
        await engine.dispose()
