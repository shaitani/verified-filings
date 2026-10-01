"""The ``web`` schema's models (app/db/web.py), checked without a database.

What these hold in place: a status spelt one way everywhere, the columns the
web role's grants depend on, and a migration that agrees with the code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from app.db import roles
from app.db.web import AdminAction, AdminActionKind, Job, User, WebBase
from app.schemas.job import JOB_STATUSES, JobStage

VERSIONS = Path("app/db/migrations/versions")
MIGRATION = next(VERSIONS.glob("*_add_web_schema.py"))
ADMIN_MIGRATION = next(VERSIONS.glob("*_add_admin_action_and_invite_revocation.py"))

#: The roles that hold grants in web.
WEB_ROLES = (roles.WEB, roles.ADMIN)


def _check_values(name: str, migration: Path = MIGRATION) -> set[str]:
    """The values a CHECK in a migration allows, e.g. status IN ('queued', ...)."""
    match = re.search(rf"{name} IN \(([^)]*)\)", migration.read_text(encoding="utf-8"))
    assert match, f"no CHECK on {name} in {migration.name}"
    return set(re.findall(r"'([^']*)'", match.group(1)))


# --------------------------------------------------------------------------- #
# A status is one of a fixed list, everywhere
# --------------------------------------------------------------------------- #


def test_the_migration_allows_exactly_the_statuses_the_code_knows() -> None:
    assert _check_values("status") == set(JOB_STATUSES)


def test_every_stage_is_a_status() -> None:
    assert set(get_args(JobStage)) <= set(JOB_STATUSES)


@pytest.mark.parametrize("wrong", ["Done", "complete", "finished", " done"])
def test_a_misspelt_status_fails_where_it_is_written(wrong: str) -> None:
    with pytest.raises(ValueError, match="unknown job status"):
        Job(status=wrong)


def test_a_known_status_is_accepted() -> None:
    assert Job(status="queued").status == "queued"


def test_an_admin_action_is_one_the_migration_allows() -> None:
    assert _check_values("action", ADMIN_MIGRATION) == set(get_args(AdminActionKind))
    with pytest.raises(ValueError, match="unknown admin action"):
        AdminAction(action="promote")


# --------------------------------------------------------------------------- #
# What the web role's grants depend on (roles.WEB, api DESIGN §11)
# --------------------------------------------------------------------------- #


def test_every_granted_table_exists_in_web() -> None:
    tables = {table.name for table in WebBase.metadata.tables.values()}
    granted = {
        table
        for spec in WEB_ROLES
        for table in (*spec.tables, *(grant.table for grant in spec.writes))
    }
    assert granted == tables  # nothing granted that does not exist, nothing left ungranted


@pytest.mark.parametrize("spec", WEB_ROLES, ids=lambda spec: spec.name)
def test_every_narrowed_grant_names_real_columns(spec) -> None:
    by_name = {table.name: table for table in WebBase.metadata.tables.values()}
    for grant in spec.writes:
        missing = set(grant.columns) - set(by_name[grant.table].columns.keys())
        assert not missing, f"{grant.privilege} on {grant.table}: no column {sorted(missing)}"


def test_every_policed_table_exists_and_every_policy_names_a_web_role() -> None:
    tables = {table.name for table in WebBase.metadata.tables.values()}
    assert set(roles.ROW_SECURED) <= tables
    assert {policy.role for policy in roles.ROW_POLICIES} <= {spec.name for spec in WEB_ROLES}


def test_every_role_that_uses_a_policed_table_has_a_policy_on_it() -> None:
    """Row security on, a role with no policy sees no rows: the web role would
    find no users and sign no one in."""
    for spec in WEB_ROLES:
        for table in roles.ROW_SECURED:
            if table in spec.tables:
                assert any(p.role == spec.name and p.table == table for p in roles.ROW_POLICIES), (
                    f"{spec.name} reads {table} but has no policy on it"
                )


def test_the_admin_role_can_never_make_an_administrator() -> None:
    written = {c for g in roles.ADMIN.writes if g.table == "user" for c in g.columns}
    assert "is_superuser" not in written
    assert not any(g.table == "user" and g.privilege == "INSERT" for g in roles.ADMIN.writes)


def test_every_user_column_the_orm_inserts_is_granted() -> None:
    """Including by omission: vf_web_role may not write is_superuser, so it has
    no Python default (which the ORM would send) but a database default."""
    insert = next(g for g in roles.WEB.writes if g.table == "user" and g.privilege == "INSERT")
    sent = {c.name for c in User.__table__.columns if c.default is not None or not c.server_default}
    assert sent <= set(insert.columns)


def test_every_table_lives_in_web() -> None:
    assert {table.schema for table in WebBase.metadata.tables.values()} == {"web"}
    for table in WebBase.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.schema == "web", f"{table.name}.{fk.parent.name}"
