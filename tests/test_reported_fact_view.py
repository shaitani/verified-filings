"""The ``xbrl.reported_fact`` view, against the real test database.

The view is half of a fence (``app/retrieval/DESIGN.md`` §3 and §6): it is the
only relation ``vf_retrieval_role`` may read, so what it exposes is exactly
what generated SQL can reach. Widening it by accident is a silent change to
that boundary, which is why the column list is asserted rather than described.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.db.loader import load_file
from tests.conftest import FAKE_CIK

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "xbrl_fake_company.json"

#: Every column the view offers. The result contract is built from these, and
#: ``vf_retrieval_role`` can see nothing else in the database.
EXPECTED_COLUMNS = {
    "company_cik",
    "ticker",
    "entity_name",
    "concept_id",
    "taxonomy",
    "concept_name",
    "concept_label",
    "unit",
    "is_instant",
    "period_start",
    "period_end",
    "value",
}

#: Columns whose *absence* is the design. `fiscal_year` / `fiscal_period` sit
#: on `filing` and describe which filing a number appeared in, not the period
#: it covers (PITFALLS §1.1) -- a 10-K carries prior-year comparatives, so
#: `WHERE fiscal_year = 2024` returns the wrong years. The labels reach the
#: result from the query plan instead. The rest are simply out of scope for
#: anything answering a question.
FORBIDDEN_COLUMNS = {
    "fiscal_year",
    "fiscal_period",
    "is_latest",
    "embedding",
    "description",
    "filing_accession",
    "accession_number",
    "filed_date",
    "form",
    "frame",
}


async def _view_columns(session) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'xbrl' AND table_name = 'reported_fact'"
        )
    )
    return {name for (name,) in rows}


async def test_the_view_exposes_exactly_these_columns(test_session_factory) -> None:
    async with test_session_factory() as session:
        assert await _view_columns(session) == EXPECTED_COLUMNS


async def test_the_view_hides_the_columns_it_is_meant_to(test_session_factory) -> None:
    """Especially `fiscal_year`: the trap is closed by absence, because a
    column of that name on a relation Qwen can see is an invitation to use
    it."""
    async with test_session_factory() as session:
        assert await _view_columns(session) & FORBIDDEN_COLUMNS == set()


async def test_the_view_runs_with_owner_rights(test_session_factory) -> None:
    """`security_invoker` off is what lets a role hold SELECT on the view while
    holding nothing on fact / filing / company / concept. Turning it on would
    silently require the base-table grants back."""
    async with test_session_factory() as session:
        options = (
            await session.execute(
                text("SELECT reloptions FROM pg_class WHERE oid = 'xbrl.reported_fact'::regclass")
            )
        ).scalar_one()
        assert not [option for option in (options or []) if "security_invoker" in option]


async def test_the_view_drops_superseded_facts(
    test_session_factory, clean_fake_company
) -> None:
    """The fixture restates a period, so the base table holds more rows than
    the view. Every count is read from the database, not retyped."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    async with test_session_factory() as session:
        all_facts = (
            await session.execute(
                text("SELECT count(*) FROM xbrl.fact WHERE company_cik = :cik"),
                {"cik": FAKE_CIK},
            )
        ).scalar_one()
        latest_facts = (
            await session.execute(
                text("SELECT count(*) FROM xbrl.fact WHERE company_cik = :cik AND is_latest"),
                {"cik": FAKE_CIK},
            )
        ).scalar_one()
        in_view = (
            await session.execute(
                text("SELECT count(*) FROM xbrl.reported_fact WHERE company_cik = :cik"),
                {"cik": FAKE_CIK},
            )
        ).scalar_one()

        assert latest_facts < all_facts, "fixture should restate a period"
        assert in_view == latest_facts


async def test_the_view_is_unique_at_the_binding_grain(
    test_session_factory, clean_fake_company
) -> None:
    """The grain the result contract counts rows at. `unit` belongs in it:
    without it AMD's FY2024 effective tax rate is two rows (filed as both
    `pure` and `Rate`) and anything that sums them doubles a 0.19 figure."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    async with test_session_factory() as session:
        duplicates = (
            await session.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT 1 FROM xbrl.reported_fact"
                    "  GROUP BY company_cik, concept_id, unit, period_start,"
                    "           period_end, is_instant"
                    "  HAVING count(*) > 1"
                    ") t"
                )
            )
        ).scalar_one()
        assert duplicates == 0
