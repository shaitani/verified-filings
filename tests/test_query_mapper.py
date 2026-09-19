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
from datetime import date
from pathlib import Path

import pytest

from app.db.loader import load_file
from app.schemas.query import Binding, ConceptRef, Coverage, Note, PeriodRef, QueryIn
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
            {"id": "e4", "text": "annual", "kind": "qualifier"},
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
    assert binding.period_rule == "direct"
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


async def test_metric_without_a_company_cannot_be_verified(
    test_session_factory, clean_fake_company, fake_aliases
) -> None:
    """Coverage is checked per company, so a metric with nothing to check it
    against is reported rather than bound on faith."""
    await load_file(FIXTURE_PATH, session_factory=test_session_factory)

    plan = await map_query(
        _query({"id": "m", "text": "widget sales", "kind": "metric"}),
        session_factory=test_session_factory,
    )

    assert plan.bindings == []
    assert "no company in scope" in plan.unresolved[0].reason


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
