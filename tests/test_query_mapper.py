"""Tests for app/semantic/query_mapper.py.

Runs against the real "db-test" PostgreSQL container (tests/conftest.py), with
the same fake-company fixture the loader tests use -- so the company and period
resolvers are checked against actually-loaded rows rather than mocks.

Expected windows are derived from the fixture (`_fixture_windows`), never
retyped as literals, so editing the fixture can't silently desync them.

Metric tests patch in tests/fixtures/metric_aliases_fake.yaml so the resolver
cascade runs against the fixture's own concepts rather than the real corpus.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import update

from app.db import Company
from app.db.loader import load_file
from app.schemas.query import (
    Binding,
    Clarification,
    ClarifyOption,
    ConceptRef,
    Coverage,
    MetricElementIn,
    Note,
    PeriodRef,
    PlanFilters,
    QueryIn,
    QueryPlan,
    ResolvedPeriod,
    ResultSpec,
    Unresolved,
)
from app.semantic import query_mapper
from app.semantic.metric_aliases import load_aliases
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


async def test_plan_shape_end_to_end(test_session_factory, clean_fake_company) -> None:
    """The whole shape, end to end. "revenue" hits the real curated alias file,
    whose concepts are not in this test database, so it reports rather than
    binds -- which is the right answer here and keeps the assertion about plan
    *shape* rather than about the fixture's accounting.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "e1", "text": "revenue", "kind": "metric"},
            {"id": "e2", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "e3", "text": "last 2 years", "kind": "period", "last_n_years": 2},
        ),
        session_factory=test_session_factory,
    )

    assert plan.question == "test question"
    assert plan.filters.ciks == [FAKE_CIK]

    # The metric element is the only thing outstanding.
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


# --------------------------------------------------------------------------- #
# Metrics -- the alias cascade, against the fixture's own concepts
# --------------------------------------------------------------------------- #

FAKE_ALIASES = Path(__file__).parent / "fixtures" / "metric_aliases_fake.yaml"


@pytest.fixture
def fake_aliases(monkeypatch):
    """Point the resolver at the fixture alias file.

    Patched on ``query_mapper`` rather than on ``app.semantic.metric_aliases`` because
    the name is bound at import; ``alias_index`` is also lru_cached, and
    replacing it here keeps the real file's cache untouched for other tests.
    """
    index = load_aliases(FAKE_ALIASES)
    monkeypatch.setattr(query_mapper, "alias_index", lambda: index)
    return index


async def test_alias_falls_through_to_a_loaded_alternative(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """The first listed concept isn't in this database at all, so the second
    one is bound -- preference order, with unloaded refs skipped."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget sales", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.is_complete
    (binding,) = plan.bindings
    assert [c.name for c in binding.concepts] == ["ZzzTestRevenues"]
    assert binding.resolved_by == "alias"
    assert binding.is_instant is False


async def test_coverage_rejects_a_loaded_concept_without_the_right_facts(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """Both alternatives exist in the database. The first one's only fact is an
    instant dated partway through the year, so it cannot answer a fiscal-year
    request and the second is chosen -- coverage overriding preference order.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget assets", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    (binding,) = plan.bindings
    assert [c.name for c in binding.concepts] == ["ZzzTestAssets"]
    assert binding.is_instant is True


async def test_no_candidate_with_coverage_is_reported_not_guessed(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget nothing", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.bindings == []
    assert not plan.is_complete
    assert "no concept with facts covering" in plan.unresolved[0].reason


async def test_multi_slot_alias_binds_every_operand(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """A derived metric binds one concept per operand slot and carries the
    expression through for the SQL step to apply."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget margin", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    (binding,) = plan.bindings
    assert binding.expression == "c0 / c1"
    assert [c.name for c in binding.concepts] == ["ZzzTestNetLoss", "ZzzTestRevenues"]


async def test_naming_no_company_means_every_loaded_filer(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """"Rank all twenty companies" carries no company element, because the
    population is the answer rather than something to name. That used to
    resolve to an empty cik list and be refused as "no company in scope"."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget sales", "kind": "metric"},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.filters.ciks == [FAKE_CIK]
    assert [b.company_cik for b in plan.bindings] == [FAKE_CIK]
    assert plan.is_complete


async def test_a_company_that_failed_to_resolve_does_not_widen_the_scope(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """The distinction that makes the expansion safe. "Apple versus Samsung"
    names two companies and resolves one; it must stay a half-answer, and must
    never quietly become every filer in the store."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget sales", "kind": "metric"},
            {"id": "c", "text": "Nonexistent Corp", "kind": "company"},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.filters.ciks == []
    assert plan.bindings == []
    reasons = " ".join(u.reason for u in plan.unresolved)
    assert "no loaded company" in reasons
    assert "no company in scope" in reasons


async def test_unaliased_term_makes_no_embedding_call_without_a_corpus(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """The test database has no concept embeddings. The resolver must notice
    and report, not reach out to the embedding service."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "blorptastic synergy index", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.bindings == []
    assert "nothing in the concept corpus" in plan.unresolved[0].reason


# --------------------------------------------------------------------------- #
# Concept drift -- a filer changing tags partway through the range asked about
# --------------------------------------------------------------------------- #


async def test_one_binding_per_company_when_nothing_drifts(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """The common case must not get noisier: a metric whose concept holds for
    the whole range still produces exactly one binding, with no notes."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget sales", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    (binding,) = plan.bindings
    assert binding.notes == []
    assert [p.fiscal_year for p in binding.periods] == [WINDOW_YEAR]


async def test_periods_with_no_covering_concept_are_noted_not_dropped(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """Coverage is decided per period now, so a range where only some periods
    resolve binds the ones that do -- but the gap has to be disclosed, never
    silently omitted from a series someone will read as continuous."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    missing_year = min(y for y in (WINDOW_YEAR - 1, WINDOW_YEAR) if y not in WINDOWS)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget assets", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p1", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
            {"id": "p2", "text": "prior", "kind": "period", "fiscal_year": missing_year},
        ),
        session_factory=test_session_factory,
    )

    # The missing year has no reporting window at all, so it never reaches the
    # metric resolver -- the period resolver reports it.
    assert any("no FY reporting window" in u.reason for u in plan.unresolved)
    assert [p.fiscal_year for p in plan.bindings[0].periods] == [WINDOW_YEAR]


def test_binding_periods_default_to_every_period_in_scope() -> None:
    """An empty list means "all", so a plan built by hand stays valid."""
    binding = Binding(
        element_id="e1",
        concepts=[ConceptRef(concept_id=1, taxonomy="us-gaap", name="Revenues")],
        unit="USD",
        is_instant=False,
        coverage=Coverage(fact_count=1),
        confidence=1.0,
        resolved_by="alias",
        rationale="x",
    )
    assert binding.periods == []
    assert binding.notes == []


def test_note_survives_on_a_binding() -> None:
    binding = Binding(
        element_id="e1",
        periods=[PeriodRef(fiscal_year=2025, fiscal_period="FY")],
        concepts=[ConceptRef(concept_id=1, taxonomy="us-gaap", name="Revenues")],
        unit="USD",
        is_instant=False,
        coverage=Coverage(fact_count=1),
        confidence=1.0,
        resolved_by="alias",
        rationale="x",
        notes=[Note(kind="concept_switch", message="tags changed in FY2025")],
    )
    assert binding.notes[0].kind == "concept_switch"


# --------------------------------------------------------------------------- #
# Sign conventions -- an operand whose direction the expression supplies
# --------------------------------------------------------------------------- #


async def test_negative_magnitude_operand_refuses_the_binding(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """The fixture's second operand is negative and declared a magnitude, so
    `c0 - c1` would add where it means to subtract. That is a wrong number
    rather than a caveated one, so it is refused, not noted."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget burn", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.bindings == []
    assert not plan.is_complete
    assert "declared a magnitude" in plan.unresolved[0].reason


async def test_negative_signed_operand_still_binds(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """Same negative operand, left unannotated. A negative operating cash flow
    or gross profit is real data, so the default must not over-refuse."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget signed burn", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    (binding,) = plan.bindings
    assert binding.expression == "c0 - c1"
    assert [c.name for c in binding.concepts] == ["ZzzTestRevenues", "ZzzTestNetLoss"]


# --------------------------------------------------------------------------- #
# Cross-company period alignment -- pure, no database
# --------------------------------------------------------------------------- #


def _period(cik: int, end: date, *, year: int = 2025, fp: str = "Q4") -> ResolvedPeriod:
    return ResolvedPeriod(
        company_cik=cik,
        fiscal_year=year,
        fiscal_period=fp,
        period_start=end - timedelta(days=90),
        period_end=end,
    )


def _series_spec(companies: int) -> ResultSpec:
    axes = ["company", "period"] if companies > 1 else ["period"]
    return ResultSpec(shape="series", axes=axes, companies=companies, periods=4, metrics=1)


def test_misaligned_fiscal_calendars_are_flagged() -> None:
    """Measured across the store, one fiscal label can cover period_ends 339
    days apart. A shared time axis reads those as contemporaneous."""
    notes = query_mapper._alignment_notes(
        [_period(1, date(2025, 1, 26)), _period(2, date(2025, 12, 31))],
        _series_spec(2),
    )
    assert [n.kind for n in notes] == ["period_misalignment"]
    assert "339 days" in notes[0].message


def test_aligned_calendars_are_not_flagged() -> None:
    """Two December filers genuinely are comparable; warning anyway would make
    the note noise that gets ignored when it matters."""
    notes = query_mapper._alignment_notes(
        [_period(1, date(2025, 12, 31)), _period(2, date(2025, 12, 28))],
        _series_spec(2),
    )
    assert notes == []


def test_single_company_is_never_flagged() -> None:
    """Within one filer the labels are self-consistent, whatever its calendar."""
    notes = query_mapper._alignment_notes(
        [_period(1, date(2025, 1, 26)), _period(1, date(2025, 12, 31))],
        _series_spec(1),
    )
    assert notes == []


# --------------------------------------------------------------------------- #
# Company groups -- selecting filers by attribute
# --------------------------------------------------------------------------- #


async def test_company_group_reports_that_sic_data_is_missing(
    test_session_factory, clean_fake_company
) -> None:
    """An empty result would read as "no company is in that sector", which is a
    different and wrong answer, so the absence of the data has to be stated.

    The fixture company has no SIC values, which is the case this exercises.
    The reason names the column and says what to do about it, because the
    three sector columns fail for different reasons: `sic_code` and
    `sic_description` are simply not loaded yet, while `sic_office` has no
    source at all (see `test_sic_office_says_it_has_no_source`)."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {
                "id": "g",
                "text": "semiconductor companies",
                "kind": "company_group",
                "sic_description": "semiconductor",
            }
        ),
        session_factory=test_session_factory,
    )

    assert plan.filters.ciks == []
    reason = plan.unresolved[0].reason
    assert "sic_description" in reason
    assert "not populated for any filer" in reason
    assert "Run the load step" in reason


async def test_sic_office_says_it_has_no_source(
    test_session_factory, clean_fake_company
) -> None:
    """Not the same failure as an unloaded column, and the message must not
    imply it is. `sic_office` has no source anywhere in this project -- the
    SEC assigns review offices by SIC *range* and nothing here carries that
    mapping -- so "run the load step" would send someone after data that does
    not exist."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {
                "id": "g",
                "text": "office of technology",
                "kind": "company_group",
                "sic_office": "Office of Technology",
            }
        ),
        session_factory=test_session_factory,
    )

    reason = plan.unresolved[0].reason
    assert "No source for this exists" in reason
    assert "Run the load step" not in reason


async def test_company_group_resolves_once_sic_data_exists(
    test_session_factory, clean_fake_company
) -> None:
    """The point of the whole exercise: the query path is complete, so loading
    the data is the only remaining step. Populating one column is enough to
    make a group resolve, with no code change."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    async with test_session_factory.begin() as session:
        await session.execute(
            update(Company)
            .where(Company.cik == FAKE_CIK)
            .values(sic_code="3674", sic_description="Semiconductors & Related Devices")
        )

    plan = await map_query(
        _query(
            {
                "id": "g",
                "text": "semiconductor companies",
                "kind": "company_group",
                "sic_description": "semiconductor",
            }
        ),
        session_factory=test_session_factory,
    )

    assert plan.filters.ciks == [FAKE_CIK]
    assert plan.unresolved == []


async def test_company_group_matches_an_exact_sic_code(
    test_session_factory, clean_fake_company
) -> None:
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    async with test_session_factory.begin() as session:
        await session.execute(
            update(Company).where(Company.cik == FAKE_CIK).values(sic_code="3674")
        )

    plan = await map_query(
        _query({"id": "g", "text": "SIC 3674", "kind": "company_group", "sic_code": "3674"}),
        session_factory=test_session_factory,
    )
    assert plan.filters.ciks == [FAKE_CIK]

    miss = await map_query(
        _query({"id": "g", "text": "SIC 7372", "kind": "company_group", "sic_code": "7372"}),
        session_factory=test_session_factory,
    )
    assert miss.filters.ciks == []
    assert "no loaded company matches" in miss.unresolved[0].reason


async def test_named_company_and_group_merge_without_duplicates(
    test_session_factory, clean_fake_company
) -> None:
    """A question can name a filer AND a group it belongs to; the same company
    must not be counted twice."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    async with test_session_factory.begin() as session:
        await session.execute(
            update(Company).where(Company.cik == FAKE_CIK).values(sic_code="3674")
        )

    plan = await map_query(
        _query(
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "g", "text": "SIC 3674", "kind": "company_group", "sic_code": "3674"},
        ),
        session_factory=test_session_factory,
    )

    assert plan.filters.ciks == [FAKE_CIK]


async def test_ambiguous_term_asks_instead_of_refusing(
    test_session_factory, clean_fake_company
) -> None:
    """A curated clarify entry must come back as a question with named choices,
    not as a dead end. Uses the real alias file -- "profit margin" is exactly
    the case it exists for."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "profit margin", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert not plan.is_complete
    assert plan.needs_input, "a curated question is answerable, not a failure"
    (clarification,) = plan.clarifications
    assert clarification.element_text == "profit margin"
    assert "?" in clarification.question
    assert {o.metric for o in clarification.options} == {
        "gross_margin",
        "operating_margin",
        "net_margin",
    }
    # Options read in business terms, not file keys or concept names.
    assert all(o.label and o.description for o in clarification.options)


async def test_specific_margin_resolves_without_asking(
    test_session_factory, clean_fake_company
) -> None:
    """Only the bare term asks. Naming the margin has to go straight through,
    or the clarification becomes a toll gate."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "net profit margin", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.clarifications == []


async def test_unavailable_term_declines_with_a_reason(
    test_session_factory, clean_fake_company
) -> None:
    """A curated `unavailable` entry must refuse outright, with the reason, and
    must never reach the embedding search.

    The distinction matters: left uncurated, "share price" bound
    dei:EntityListingParValuePerShare and reported par value -- $0.000006 --
    as a verified answer. Offering it back as an `Ambiguity` would be no better,
    because there is nothing the asker could narrow.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "share price", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert not plan.is_complete
    assert not plan.needs_input, "there is no choice to offer; this is a refusal"
    assert plan.bindings == []
    assert plan.ambiguous == []
    (problem,) = plan.unresolved
    assert problem.element_id == "m"
    assert "par value" in problem.reason


async def _fixture_concept(session_factory, name: str):
    """One loaded concept from the fixture, as an embedding candidate would
    arrive -- so a stubbed search points at something coverage can verify."""
    from sqlalchemy import select

    from app.db import Concept

    async with session_factory() as session:
        row = await session.execute(
            select(Concept.id, Concept.taxonomy, Concept.name, Concept.label).where(
                Concept.name == name
            )
        )
        return row.one()


async def test_a_distant_match_is_refused_rather_than_offered(
    test_session_factory, clean_fake_company, monkeypatch
) -> None:
    """Below the plausibility floor there is nothing to choose between, so the
    element is `unresolved`, not `ambiguous`.

    Measured over 221 labelled cases: candidate lists this weak never contained
    the right concept. Offering them is how "competition risk disclosure" came
    back as a choice between AssetsFairValueDisclosure and
    LiabilitiesFairValueDisclosure.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    cid, taxonomy, name, label = await _fixture_concept(
        test_session_factory, "ZzzTestRevenues"
    )

    async def _distant(session, text):
        return [
            query_mapper._Candidate(
                concept_id=cid, taxonomy=taxonomy, name=name, score=0.60, label=label
            )
        ]

    monkeypatch.setattr(query_mapper, "_nearest_concepts", _distant)

    plan = await map_query(
        _query(
            {"id": "m", "text": "something nobody curated", "kind": "metric"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.ambiguous == [], "a 0.60 match is not a choice worth offering"
    assert plan.bindings == []
    assert not plan.needs_input
    (problem,) = plan.unresolved
    assert problem.element_id == "m"
    assert "plausibly measures" in problem.reason
    assert "0.60" in problem.reason


def _fy2024(cik: int, fiscal_period: str) -> ResolvedPeriod:
    """One period of a calendar-year filer. Q4 is ordinary now: the view
    computes it, so the mapper carries no windows to subtract."""
    if fiscal_period == "Q4":
        return ResolvedPeriod(
            company_cik=cik,
            fiscal_year=2024,
            fiscal_period="Q4",
            period_start=date(2024, 10, 1),
            period_end=date(2024, 12, 31),
        )
    return ResolvedPeriod(
        company_cik=cik,
        fiscal_year=2024,
        fiscal_period=fiscal_period,
        period_start=date(2024, 7, 1),
        period_end=date(2024, 9, 30),
    )


def test_a_q4_is_bound_in_the_same_binding_as_its_neighbours() -> None:
    """Q4 used to force its own binding, because it was computed by
    subtraction and the quarters beside it were not -- one `period_rule` had
    to be true of every period in the group. `xbrl.reported_fact` supplies the
    fourth quarter as an ordinary row, so a series no longer splits on it and
    only a genuine tag change does.
    """
    concept = query_mapper._Candidate(
        concept_id=1, taxonomy="us-gaap", name="Revenues", score=1.0
    )
    periods = [_fy2024(11, "Q3"), _fy2024(11, "Q4")]
    evidence = {
        (11, 1): query_mapper._Evidence(
            is_instant=False,
            unit="USD",
            values={
                (period.period_start, period.period_end): Decimal("25")
                for period in periods
            },
        )
    }
    bindings: list = []
    query_mapper._bind_per_company(
        MetricElementIn(id="e1", text="revenue"),
        slots=[[concept]],
        signs=["signed"],
        caveats={},
        expression="c0",
        resolved_by="alias",
        source="curated alias",
        ciks=[11],
        periods=periods,
        evidence=evidence,
        bindings=bindings,
        ambiguous=[],
        problems=[],
        uncovered_ciks=[],
    )
    assert len(bindings) == 1
    assert {(p.fiscal_year, p.fiscal_period) for p in bindings[0].periods} == {
        (2024, "Q3"),
        (2024, "Q4"),
    }


def test_a_ratio_is_dimensionless_not_the_numerator_s_unit() -> None:
    """`gross_margin` is USD over USD and the answer is a ratio. Reporting the
    lead operand's unit said 0.46 was "USD", which anything formatting values
    would render as 46 cents (PITFALLS 2.1)."""
    numerator = query_mapper._Candidate(
        concept_id=1, taxonomy="us-gaap", name="GrossProfit", score=1.0
    )
    denominator = query_mapper._Candidate(
        concept_id=2, taxonomy="us-gaap", name="Revenues", score=1.0
    )
    window = (date(2024, 7, 1), date(2024, 9, 30))
    usd = query_mapper._Evidence(is_instant=False, unit="USD", values={window: Decimal("1")})
    evidence = {(11, 1): usd, (11, 2): usd}

    for expression, expected in (("c0 / c1", "pure"), ("c0 - c1", "USD")):
        bindings: list[Binding] = []
        query_mapper._bind_per_company(
            MetricElementIn(id="m", text="gross margin"),
            slots=[[numerator], [denominator]],
            signs=["signed", "signed"],
            expression=expression,
            resolved_by="alias",
            source="curated alias",
            ciks=[11],
            periods=[_fy2024(11, "Q3")],
            evidence=evidence,
            bindings=bindings,
            ambiguous=[],
            problems=[],
            uncovered_ciks=[],
        )
        (binding,) = bindings
        assert binding.unit == expected, expression


def test_operands_in_different_units_are_refused() -> None:
    """No shipped entry does this. The guard is so that one written later --
    dividing USD by a share count, say -- fails loudly rather than inheriting
    a unit that describes only its numerator."""
    dollars = query_mapper._Candidate(
        concept_id=1, taxonomy="us-gaap", name="NetIncomeLoss", score=1.0
    )
    shares = query_mapper._Candidate(
        concept_id=2, taxonomy="us-gaap", name="CommonStockSharesOutstanding", score=1.0
    )
    window = (date(2024, 7, 1), date(2024, 9, 30))
    evidence = {
        (11, 1): query_mapper._Evidence(
            is_instant=False, unit="USD", values={window: Decimal("1")}
        ),
        (11, 2): query_mapper._Evidence(
            is_instant=False, unit="shares", values={window: Decimal("1")}
        ),
    }

    bindings: list[Binding] = []
    problems: list[Unresolved] = []
    query_mapper._bind_per_company(
        MetricElementIn(id="m", text="earnings per share"),
        slots=[[dollars], [shares]],
        signs=["signed", "signed"],
        expression="c0 / c1",
        resolved_by="alias",
        source="curated alias",
        ciks=[11],
        periods=[_fy2024(11, "Q3")],
        evidence=evidence,
        bindings=bindings,
        ambiguous=[],
        problems=problems,
        uncovered_ciks=[],
    )

    assert bindings == []
    (problem,) = problems
    assert "different units" in problem.reason
    assert "USD" in problem.reason and "shares" in problem.reason


def test_a_narrower_fallback_discloses_what_it_leaves_out() -> None:
    """When a metric's preferred concept is missing and a narrower alternative
    binds instead, the binding has to say so.

    "Total debt" is the case this exists for: most filers tag no combined debt
    line, so the answer is long-term debt, which omits commercial paper --
    Apple's is $8.0B against $90.7B, so the figure runs 8% light. Nothing else
    in the plan would tell a reader that, and an 8% understatement labelled
    "total debt" is a wrong number rather than a caveated one.

    The filers that *do* tag the exact line must carry no such note, which is
    why caveats are keyed per concept rather than per metric.
    """
    window = (date(2024, 1, 1), date(2024, 12, 31))
    exact = query_mapper._Candidate(
        concept_id=1, taxonomy="us-gaap", name="DebtLongtermAndShorttermCombinedAmount", score=1.0
    )
    narrower = query_mapper._Candidate(
        concept_id=2, taxonomy="us-gaap", name="LongTermDebt", score=1.0
    )
    periods = [
        ResolvedPeriod(
            company_cik=cik,
            fiscal_year=2024,
            fiscal_period="FY",
            period_start=window[0],
            period_end=window[1],
        )
        for cik in (11, 22)
    ]
    fact = query_mapper._Evidence(
        is_instant=False, unit="USD", values={window: Decimal("100")}
    )
    # cik 11 tags both and must take the exact one; cik 22 only the fallback.
    evidence = {(11, 1): fact, (11, 2): fact, (22, 2): fact}

    bindings: list[Binding] = []
    query_mapper._bind_per_company(
        MetricElementIn(id="m", text="total debt"),
        slots=[[exact, narrower]],
        signs=["signed"],
        caveats={("us-gaap", "LongTermDebt"): "Short-term borrowings are not included."},
        expression="c0",
        resolved_by="alias",
        source="curated alias 'total_debt'",
        ciks=[11, 22],
        periods=periods,
        evidence=evidence,
        bindings=bindings,
        ambiguous=[],
        problems=[],
        uncovered_ciks=[],
    )

    by_cik = {b.company_cik: b for b in bindings}
    assert by_cik[11].concepts[0].name == "DebtLongtermAndShorttermCombinedAmount"
    assert by_cik[11].notes == [], "the exact line needs no caveat"

    assert by_cik[22].concepts[0].name == "LongTermDebt"
    (note,) = by_cik[22].notes
    assert note.kind == "narrower_than_asked"
    assert "Short-term borrowings" in note.message


def test_ambiguity_is_reported_once_per_element_not_once_per_company() -> None:
    """Twenty companies tying on one phrase is one question, not twenty.

    Measured before this merged: "total debt" across the corpus produced
    nineteen separate `Ambiguity` records for a single element, each with a
    slightly different candidate list. Unusable as something to put to a
    person. The merge keeps the best sighting of each concept.
    """
    element = MetricElementIn(id="m", text="total debt")
    window = (date(2024, 1, 1), date(2024, 12, 31))

    def _candidate(concept_id: int, name: str, score: float):
        return query_mapper._Candidate(
            concept_id=concept_id, taxonomy="us-gaap", name=name, score=score
        )

    # Two filers, overlapping but not identical candidate sets, and the shared
    # concept scores differently for each.
    shared = ("LongTermDebt", 0.69)
    slot = [
        _candidate(1, "DebtInstrumentCarryingAmount", 0.71),
        _candidate(2, *shared),
        _candidate(3, "ShortTermBorrowings", 0.66),
    ]
    periods = [
        ResolvedPeriod(
            company_cik=cik,
            fiscal_year=2024,
            fiscal_period="FY",
            period_start=window[0],
            period_end=window[1],
        )
        for cik in (11, 22)
    ]
    evidence = {
        (cik, candidate.concept_id): query_mapper._Evidence(
            is_instant=False, unit="USD", values={window: Decimal("1")}
        )
        for cik in (11, 22)
        for candidate in slot
    }

    ambiguous: list = []
    query_mapper._bind_per_company(
        element,
        slots=[slot],
        signs=["signed"],
        expression="c0",
        resolved_by="embedding",
        source="embedding search",
        ciks=[11, 22],
        periods=periods,
        evidence=evidence,
        bindings=[],
        ambiguous=ambiguous,
        problems=[],
        uncovered_ciks=[],
    )

    (report,) = ambiguous
    assert report.element_id == "m"
    names = [c.concept.name for c in report.candidates]
    assert len(names) == len(set(names)), "one concept, one entry"
    assert names == sorted(names, key=lambda n: -dict(
        DebtInstrumentCarryingAmount=0.71, LongTermDebt=0.69, ShortTermBorrowings=0.66
    )[n]), "best match first"


def test_needs_input_separates_asking_from_impossible() -> None:
    """`unresolved` means the data cannot support the question; clarifications
    and ambiguities mean it might, once the asker narrows it."""
    spec = ResultSpec(shape="scalar", companies=1, periods=1, metrics=1)
    impossible = QueryPlan(
        question="q",
        intent="lookup",
        result=spec,
        filters=PlanFilters(),
        unresolved=[Unresolved(element_id="e1", reason="no such data")],
    )
    assert not impossible.is_complete
    assert not impossible.needs_input

    askable = QueryPlan(
        question="q",
        intent="lookup",
        result=spec,
        filters=PlanFilters(),
        clarifications=[
            Clarification(
                element_id="e1",
                element_text="margin",
                question="Which margin?",
                options=[
                    ClarifyOption(metric="gross_margin", label="Gross margin", description="a"),
                    ClarifyOption(metric="net_margin", label="Net margin", description="b"),
                ],
            )
        ],
    )
    assert not askable.is_complete
    assert askable.needs_input


# --------------------------------------------------------------------------- #
# The partial path
# --------------------------------------------------------------------------- #
#
# A company that reports nothing for a metric is ordinary, not an error:
# JPMorgan tags no OperatingIncomeLoss, correctly for a bank. Before this, one
# such filer in twenty refused the whole question -- measured on the eval set,
# five of sixteen remaining failures were exactly that. What must NOT happen is
# the answer quietly narrowing, so every drop leaves a `partial_coverage` note.


def test_an_uncovered_company_is_collected_not_refused() -> None:
    """The core change. One filer covers, one does not; the covered one still
    binds and the other is handed back for the caller to disclose."""
    element = MetricElementIn(id="m", text="operating margin")
    window = (date(2024, 1, 1), date(2024, 12, 31))
    candidate = query_mapper._Candidate(
        concept_id=1, taxonomy="us-gaap", name="OperatingIncomeLoss", score=1.0
    )
    periods = [
        ResolvedPeriod(
            company_cik=cik,
            fiscal_year=2024,
            fiscal_period="FY",
            period_start=window[0],
            period_end=window[1],
        )
        for cik in (11, 22)
    ]
    # Only cik 11 has facts. cik 22 is the bank.
    evidence = {
        (11, 1): query_mapper._Evidence(
            is_instant=False, unit="USD", values={window: Decimal("1")}
        )
    }

    bindings: list = []
    problems: list = []
    uncovered: list = []
    query_mapper._bind_per_company(
        element,
        slots=[[candidate]],
        signs=["signed"],
        expression="c0",
        resolved_by="alias",
        source="curated alias",
        ciks=[11, 22],
        periods=periods,
        evidence=evidence,
        bindings=bindings,
        ambiguous=[],
        problems=problems,
        uncovered_ciks=uncovered,
    )

    assert [b.company_cik for b in bindings] == [11]
    assert uncovered == [22]
    assert problems == [], "an uncovered company is not a refusal on its own"


def test_a_ranking_says_it_is_over_a_subset() -> None:
    """Dropping a company from a lookup costs a row. Dropping one from a
    ranking can change the answer, and no row count downstream would show it."""
    assert query_mapper._subset_warning("rank", kept=19)
    assert "19" in query_mapper._subset_warning("rank", kept=19)
    assert query_mapper._subset_warning("lookup", kept=19) == ""
    assert query_mapper._subset_warning("trend", kept=19) == ""


def test_result_spec_counts_only_the_companies_that_bound() -> None:
    """`row_count` is a promise about how many rows come back. Counting a
    company with no binding promises a row nothing will produce."""
    def _binding(cik: int | None) -> Binding:
        return Binding(
            element_id="m",
            company_cik=cik,
            concepts=[ConceptRef(concept_id=1, taxonomy="us-gaap", name="X")],
            unit="USD",
            is_instant=False,
            coverage=Coverage(fact_count=1),
            confidence=1.0,
            resolved_by="alias",
            rationale="test",
        )

    assert query_mapper._answering_ciks([11, 22], [_binding(11)]) == [11]
    assert query_mapper._answering_ciks([11, 22], [_binding(11), _binding(22)]) == [11, 22]
    # A binding for every company in scope, and one that resolved nothing.
    assert query_mapper._answering_ciks([11, 22], [_binding(None)]) == [11, 22]
    assert query_mapper._answering_ciks([11, 22], []) == [11, 22]


async def test_an_unknown_company_alongside_a_known_one_is_noted_not_refused(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """"How does Apple compare to Samsung" is a real question about Apple.

    The answer has to say Samsung is missing, but half an answer plus that
    sentence tells the asker more than a flat refusal does.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget sales", "kind": "metric"},
            {"id": "c1", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "c2", "text": "Samsung", "kind": "company"},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert plan.is_complete, "the half that resolved is answerable"
    assert plan.filters.ciks == [FAKE_CIK]
    (note,) = [n for n in plan.notes if n.kind == "partial_coverage"]
    assert "Samsung" in note.message
    assert FIXTURE_TICKER in note.message or FIXTURE_NAME_FRAGMENT in note.message


async def test_when_no_named_company_resolves_the_scope_does_not_widen(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """The guard that keeps a dropped company from becoming every company.

    `map_query` widens to every loaded filer only when the question names no
    company *element* at all. A question that named one and failed to resolve
    it has to refuse -- answering about twenty filers instead of the one that
    was asked for is a different question, not a partial answer to this one.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget sales", "kind": "metric"},
            {"id": "c", "text": "Samsung", "kind": "company"},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert not plan.is_complete
    assert plan.filters.ciks == []
    assert any("Samsung" in u.reason for u in plan.unresolved)
    assert not [n for n in plan.notes if n.kind == "partial_coverage"]


# --------------------------------------------------------------------------- #
# metric_qualifier
# --------------------------------------------------------------------------- #


async def test_an_unsatisfiable_qualifier_takes_its_metric_with_it(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """The safety property the element exists for.

    "How much revenue did Apple make from iPhones?" must not come back as
    Apple's total revenue. Measured 2026-09-23 before this existed: dropping
    the qualifier left the metric bound and returned $391,035,000,000 for a
    question about one product -- verdict `complete`, fully attributable, and
    answering something nobody asked.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m", "text": "widget sales", "kind": "metric"},
            {"id": "q", "text": "from left-handed widgets", "kind": "metric_qualifier",
             "qualifies": "m"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    assert not plan.is_complete
    assert plan.bindings == [], "the metric must not answer on its own"
    (problem,) = [u for u in plan.unresolved if u.element_id == "m"]
    assert "left-handed widgets" in problem.reason
    assert "company totals only" in problem.reason


async def test_a_qualifier_only_drops_the_metric_it_names(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """A second, unqualified metric still answers.

    Otherwise one narrow phrase would refuse a whole multi-metric question,
    which is the over-correction the partial path exists to avoid.
    """
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query(
            {"id": "m1", "text": "widget sales", "kind": "metric"},
            {"id": "m2", "text": "widget sales", "kind": "metric"},
            {"id": "q", "text": "in Europe", "kind": "metric_qualifier", "qualifies": "m1"},
            {"id": "c", "text": FIXTURE_TICKER, "kind": "company", "ticker": FIXTURE_TICKER},
            {"id": "p", "text": "fy", "kind": "period", "fiscal_year": WINDOW_YEAR},
        ),
        session_factory=test_session_factory,
    )

    bound = {binding.element_id for binding in plan.bindings}
    assert bound == {"m2"}, "only the qualified metric is dropped"
    assert [u.element_id for u in plan.unresolved] == ["m1"]


# --------------------------------------------------------------------------- #
# Uneven freshness
# --------------------------------------------------------------------------- #
#
# Nothing else in this file has two companies loaded to different points,
# because the corpus never is. Both helpers below exist for the day it is.


def _window(cik: int, year: int, period: str, end: date) -> ResolvedPeriod:
    return ResolvedPeriod(
        company_cik=cik,
        fiscal_year=year,
        fiscal_period=period,
        period_start=end - timedelta(days=89),
        period_end=end,
    )


def _windows(*periods: ResolvedPeriod) -> dict[tuple[int, int, str], ResolvedPeriod]:
    return {(p.company_cik, p.fiscal_year, p.fiscal_period): p for p in periods}


def test_each_company_gets_its_own_newest_year() -> None:
    """One filer brought forward must not move everyone else's "last year".

    This was a single global `max(fiscal_year)`. Load FY2026 for cik 11 and
    every question asking for the last year would ask cik 22 for FY2026 too,
    which it has no window for -- so it resolves to nothing and a question
    about cik 22 fails because of an ingest that touched cik 11.
    """
    windows = _windows(
        _window(11, 2026, "FY", date(2026, 12, 31)),
        _window(11, 2025, "FY", date(2025, 12, 31)),
        _window(22, 2025, "FY", date(2025, 6, 30)),
        _window(22, 2024, "FY", date(2024, 6, 30)),
    )
    assert query_mapper._newest_by_company(windows) == {11: 2026, 22: 2025}


def test_the_newest_quarter_is_found_by_date_not_by_label() -> None:
    """`last_n_quarters` must not assume the newest quarter is a Q4.

    A corpus loaded mid-year ends on Q2, and picking by label would either
    name a quarter that does not exist or silently return an older one.
    """
    windows = _windows(
        _window(11, 2026, "Q2", date(2026, 6, 30)),
        _window(11, 2026, "Q1", date(2026, 3, 31)),
        _window(11, 2025, "Q4", date(2025, 12, 31)),
    )
    picked = query_mapper._recent_quarters(windows, 1, ciks=[11])
    assert [p.fiscal_period for p in picked.values()] == ["Q2"]
    assert [p.fiscal_year for p in picked.values()] == [2026]


def test_recent_quarters_are_taken_per_company() -> None:
    """Fiscal calendars here sit up to three months apart, so one global
    "newest quarter" would hand a June filer a December filer's window."""
    windows = _windows(
        _window(11, 2025, "Q4", date(2025, 12, 31)),
        _window(11, 2025, "Q3", date(2025, 9, 30)),
        _window(22, 2025, "Q4", date(2025, 6, 30)),
        _window(22, 2025, "Q3", date(2025, 3, 31)),
    )
    picked = query_mapper._recent_quarters(windows, 1, ciks=[11, 22])
    ends = {p.company_cik: p.period_end for p in picked.values()}
    assert ends == {11: date(2025, 12, 31), 22: date(2025, 6, 30)}


def test_a_company_out_of_scope_contributes_no_quarters() -> None:
    windows = _windows(
        _window(11, 2025, "Q4", date(2025, 12, 31)),
        _window(99, 2025, "Q4", date(2025, 12, 31)),
    )
    picked = query_mapper._recent_quarters(windows, 4, ciks=[11])
    assert {p.company_cik for p in picked.values()} == {11}


def test_the_annual_window_is_never_offered_as_a_quarter() -> None:
    """A full year ends on the same date as its own Q4, so sorting by
    `period_end` alone would let the annual figure in."""
    windows = _windows(
        _window(11, 2025, "FY", date(2025, 12, 31)),
        _window(11, 2025, "Q4", date(2025, 12, 31)),
    )
    picked = query_mapper._recent_quarters(windows, 2, ciks=[11])
    assert [p.fiscal_period for p in picked.values()] == ["Q4"]
