"""Tests for app/semantic/query_mapper.py.

Runs against the real "db-test" PostgreSQL container (tests/conftest.py), with
the same fake-company fixture the loader tests use -- so the company and period
resolvers are checked against actually-loaded rows rather than mocks.

Expected windows are derived from the fixture (`_fixture_windows`), never
retyped as literals, so editing the fixture can't silently desync them.

The metric resolver is a stub today; the test below pins the stub's *contract*
(every metric element comes back unresolved, nothing raises) so that filling it
in has to update a test rather than silently change behaviour.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.db.loader import load_file
from app.schemas.query import QueryIn
from app.semantic.query_mapper import map_query
from tests.conftest import FAKE_CIK

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "xbrl_fake_company.json"

#: The fixture's own ticker / name, so these can't drift.
FIXTURE_TICKER = "ZZZZ"
FIXTURE_NAME_FRAGMENT = "Zzz Test Holdings"


def _fixture_windows() -> dict[int, tuple[date, date]]:
    """``fiscal_year -> (start, end)`` for every duration fact in the fixture,
    resolved the same way the mapper does it: latest ``period_end`` wins.

    The fixture's FY2023 filing carries only instant facts, so it has no
    window -- which is deliberately exercised below.
    """
    raw = json.loads(FIXTURE_PATH.read_text("utf-8"))
    best: dict[int, tuple[date, date]] = {}
    for concepts in raw["facts"].values():
        for concept in concepts.values():
            for facts in concept["units"].values():
                for fact in facts:
                    if not fact.get("start"):
                        continue
                    window = (date.fromisoformat(fact["start"]), date.fromisoformat(fact["end"]))
                    current = best.get(fact["fy"])
                    if current is None or window[1] > current[1]:
                        best[fact["fy"]] = window
    return best


WINDOWS = _fixture_windows()
WINDOW_YEAR = max(WINDOWS)
WINDOW_START, WINDOW_END = WINDOWS[WINDOW_YEAR]


def _query(*elements, intent: str = "lookup") -> QueryIn:
    return QueryIn.model_validate(
        {"question": "test question", "intent": intent, "elements": list(elements)}
    )


async def test_resolves_company_and_period_and_stubs_the_metric(
    test_session_factory, clean_fake_company
) -> None:
    """The whole shape, end to end: in a QueryIn, out a QueryPlan whose
    deterministic halves are filled and whose metric half is reported missing.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "e1", "text": "revenue", "kind": "metric"},
            {"id": "e2", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "e3", "text": "last 2 years", "kind": "period", "last_n_years": 2},
            {"id": "e4", "text": "annual", "kind": "qualifier"},
        ),
        session_factory=test_session_factory,
    )

    assert plan.question == "test question"
    assert plan.filters.ciks == [FAKE_CIK]

    # The stub's contract: the metric element is the only thing outstanding.
    assert [u.element_id for u in plan.unresolved] == ["e1"]
    assert plan.bindings == []
    assert not plan.is_complete


# --------------------------------------------------------------------------- #
# Companies
# --------------------------------------------------------------------------- #


async def test_resolves_company_by_name_fragment(test_session_factory, clean_fake_company) -> None:
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query({"id": "e1", "text": "x", "kind": "company", "name": FIXTURE_NAME_FRAGMENT}),
        session_factory=test_session_factory,
    )

    assert plan.filters.ciks == [FAKE_CIK]
    assert plan.unresolved == []


async def test_unknown_company_is_reported_not_raised(
    test_session_factory, clean_fake_company
) -> None:
    plan = await map_query(
        _query({"id": "e1", "text": "Nonexistent Corp", "kind": "company"}),
        session_factory=test_session_factory,
    )

    assert plan.filters.ciks == []
    assert len(plan.unresolved) == 1
    assert "no loaded company" in plan.unresolved[0].reason


# --------------------------------------------------------------------------- #
# Periods -- date windows, not fiscal-year integers
# --------------------------------------------------------------------------- #


async def test_period_resolves_to_a_window_read_from_the_facts(
    test_session_factory, clean_fake_company
) -> None:
    """The core of the fix: a fiscal year comes back as concrete dates taken
    from the filing's own duration fact, not as a bare year integer that the
    SQL step would have to match against Filing.fiscal_year."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query({"id": "e1", "text": "fiscal 2024", "kind": "period", "fiscal_year": WINDOW_YEAR}),
        session_factory=test_session_factory,
    )

    assert len(plan.filters.periods) == 1
    period = plan.filters.periods[0]
    assert period.company_cik == FAKE_CIK
    assert period.fiscal_year == WINDOW_YEAR
    assert period.fiscal_period == "FY"
    assert (period.period_start, period.period_end) == (WINDOW_START, WINDOW_END)


async def test_relative_period_counts_back_from_the_newest_resolvable_window(
    test_session_factory, clean_fake_company
) -> None:
    """ "Last 1 year" anchors to the newest year that actually has a window --
    not to the newest Filing row, and not to today's date."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query({"id": "e1", "text": "last year", "kind": "period", "last_n_years": 1}),
        session_factory=test_session_factory,
    )

    assert [p.fiscal_year for p in plan.filters.periods] == [WINDOW_YEAR]


async def test_fiscal_year_without_a_duration_fact_is_unresolved(
    test_session_factory, clean_fake_company
) -> None:
    """The fixture's other fiscal year has a filing but only instant facts, so
    its reporting window can't be identified. That must be reported, not
    guessed at from the label."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    year_without_window = min(y for y in (WINDOW_YEAR - 1, WINDOW_YEAR) if y not in WINDOWS)

    plan = await map_query(
        _query(
            {"id": "e1", "text": "that year", "kind": "period", "fiscal_year": year_without_window}
        ),
        session_factory=test_session_factory,
    )

    assert plan.filters.periods == []
    assert len(plan.unresolved) == 1
    assert "no FY reporting window" in plan.unresolved[0].reason


async def test_quarter_with_no_matching_filing_is_unresolved(
    test_session_factory, clean_fake_company
) -> None:
    """The fixture files 10-Ks only, so a Q3 request has nothing to resolve to
    and must say so rather than silently falling back to the annual window."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query({"id": "e1", "text": "Q3", "kind": "period", "fiscal_period": "Q3"}),
        session_factory=test_session_factory,
    )

    assert plan.filters.periods == []
    assert "no Q3 reporting window" in plan.unresolved[0].reason


async def test_period_element_carrying_nothing_is_unresolved(
    test_session_factory, clean_fake_company
) -> None:
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query({"id": "e1", "text": "recently", "kind": "period"}),
        session_factory=test_session_factory,
    )

    assert plan.filters.periods == []
    assert "carries no fiscal_year" in plan.unresolved[0].reason


# --------------------------------------------------------------------------- #
# Q4 -- synthesized, since no filer files one
# --------------------------------------------------------------------------- #


async def test_q4_is_unresolved_without_a_q3_window(
    test_session_factory, clean_fake_company
) -> None:
    """The fixture files 10-Ks only. Q4 needs a Q3 window to know where the
    nine-month term ends, so with no Q3 it must report rather than guess --
    the dangerous failure would be quietly returning the whole year."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query({"id": "e1", "text": "Q4", "kind": "period", "fiscal_period": "Q4"}),
        session_factory=test_session_factory,
    )

    assert plan.filters.periods == []
    assert "no Q4 reporting window" in plan.unresolved[0].reason
