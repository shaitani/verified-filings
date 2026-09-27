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
from app.db.web import AccessToken, Invite, Job, User, WebBase
from app.schemas.job import JOB_STATUSES, JobStage

MIGRATION = next(Path("app/db/migrations/versions").glob("*_add_web_schema.py"))


def _check_values(name: str) -> set[str]:
    """The values a CHECK in the migration allows, e.g. status IN ('queued', ...)."""
    match = re.search(rf"{name} IN \(([^)]*)\)", MIGRATION.read_text(encoding="utf-8"))
    assert match, f"no CHECK on {name} in {MIGRATION.name}"
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


def test_an_invite_kind_is_one_of_two() -> None:
    assert _check_values("kind") == {"email", "github"}
    with pytest.raises(ValueError, match="unknown invite kind"):
        Invite(kind="gitlab")


# --------------------------------------------------------------------------- #
# What the web role's grants depend on (roles.WEB, api DESIGN §11)
# --------------------------------------------------------------------------- #


def test_every_granted_table_exists_in_web() -> None:
    tables = {table.name for table in WebBase.metadata.tables.values()}
    granted = set(roles.WEB.tables) | {grant.table for grant in roles.WEB.writes}
    assert granted == tables  # nothing granted that does not exist, nothing left ungranted


def test_every_narrowed_grant_names_real_columns() -> None:
    by_name = {table.name: table for table in WebBase.metadata.tables.values()}
    for grant in roles.WEB.writes:
        missing = set(grant.columns) - set(by_name[grant.table].columns.keys())
        assert not missing, f"{grant.privilege} on {grant.table}: no column {sorted(missing)}"


def test_is_superuser_is_left_out_of_an_insert() -> None:
    """vf_web_role may not write is_superuser, so the ORM must not send it:
    no Python default, a database default instead."""
    column = User.__table__.c.is_superuser
    assert column.default is None
    assert column.server_default is not None


def test_every_user_column_the_orm_inserts_is_granted() -> None:
    insert = next(g for g in roles.WEB.writes if g.table == "user" and g.privilege == "INSERT")
    sent = {c.name for c in User.__table__.columns if c.default is not None or not c.server_default}
    assert sent <= set(insert.columns)


def test_the_session_table_has_the_granted_name() -> None:
    assert AccessToken.__tablename__ == "access_token"  # the library's default is "accesstoken"


def test_every_table_lives_in_web() -> None:
    assert {table.schema for table in WebBase.metadata.tables.values()} == {"web"}
    for table in WebBase.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.schema == "web", f"{table.name}.{fk.parent.name}"
