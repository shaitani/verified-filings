"""Tests for app/db/loader.py.

test_build_plan_transform is pure -- no database. The rest run against the
real "db-test" PostgreSQL container (tests/conftest.py); see LOADER.md.

Expected values are derived from the fixture itself (via `doc` and
`doc.iter_facts()`), never retyped as literals -- so editing the fixture can't
silently desync the assertions from what it actually contains.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import ALLOWED_UNITS, Company, Concept, Fact, Filing, LoadRun
from app.db.loader import build_plan, load_batch, load_file
from app.ingest.corpus import UnknownCompanyError
from app.schemas.xbrl import CompanyFactsFile
from tests.conftest import FAKE_CIK

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "xbrl_fake_company.json"


def _load_doc() -> CompanyFactsFile:
    raw = json.loads(FIXTURE_PATH.read_text("utf-8"))
    return CompanyFactsFile.model_validate(raw)


def _kept_and_dropped(doc: CompanyFactsFile):
    """Split every fact in doc the way build_plan does, independently of it."""
    kept, dropped = [], []
    for taxonomy, name, concept, unit, fact in doc.iter_facts():
        (kept if unit in ALLOWED_UNITS else dropped).append((taxonomy, name, concept, unit, fact))
    return kept, dropped


def _null_concept_key(doc: CompanyFactsFile) -> tuple[str, str]:
    """The (taxonomy, name) the fixture itself gives no label/description to."""
    return next(
        (tax, name)
        for tax, concepts in doc.facts.items()
        for name, c in concepts.items()
        if c.label is None and c.description is None
    )


def test_build_plan_transform() -> None:
    doc = _load_doc()
    plan = build_plan(doc)
    kept, dropped = _kept_and_dropped(doc)

    assert plan.company.cik == doc.cik
    assert plan.company.ticker == doc.ticker
    assert len(plan.filings) == len({fact.accn for *_, fact in doc.iter_facts()})
    assert len(plan.concepts) == len({(tax, name) for tax, name, *_ in kept})
    assert plan.dropped_unit == len(dropped)
    assert len(plan.facts) == len(kept)

    # Group plan.facts the way build_plan groups for is_latest, and check every
    # group independently: whichever row has the newest `filed` date (read
    # from the fixture, not hardcoded) is the one flagged is_latest.
    groups: dict[tuple, list] = defaultdict(list)
    for key, row in plan.facts:
        groups[(*key, row.unit, row.period_start, row.period_end)].append(row)
    assert any(len(rows) > 1 for rows in groups.values()), "fixture should restate a period"
    for rows in groups.values():
        winner = max(rows, key=lambda r: r.filed)
        for row in rows:
            assert row.is_latest == (row is winner)

    # Negative value and frame both round-trip -- read from the fixture, not retyped.
    assert sorted(row.value for _, row in plan.facts if row.value < 0) == sorted(
        fact.val for *_, fact in kept if fact.val < 0
    )
    assert sorted(row.frame for _, row in plan.facts if row.frame) == sorted(
        fact.frame for *_, fact in kept if fact.frame
    )

    # Null label/description pass through untouched.
    assert plan.concepts[_null_concept_key(doc)] == {"label": None, "description": None}


async def test_load_file_inserts_expected_rows(test_session_factory, clean_fake_company) -> None:
    doc = _load_doc()
    kept, dropped = _kept_and_dropped(doc)

    result = await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    assert result.filings == len({fact.accn for *_, fact in doc.iter_facts()})
    assert result.facts == len(kept)
    assert result.dropped_unit == len(dropped)

    async with test_session_factory() as session:
        company = await session.get(Company, doc.cik)
        assert company is not None
        assert company.ticker == doc.ticker

        filings = (
            (await session.execute(select(Filing).where(Filing.company_cik == doc.cik)))
            .scalars()
            .all()
        )
        assert len(filings) == len({fact.accn for *_, fact in doc.iter_facts()})
        filed_date = {f.accession_number: f.filed_date for f in filings}

        facts = (
            (await session.execute(select(Fact).where(Fact.company_cik == doc.cik))).scalars().all()
        )
        assert len(facts) == len(kept)

        # Same grouping/verification as the pure test, against the persisted
        # rows -- the filed date comes from the joined Filing, not a literal.
        groups: dict[tuple, list] = defaultdict(list)
        for f in facts:
            groups[(f.concept_id, f.unit, f.period_start, f.period_end)].append(f)
        assert any(len(g) > 1 for g in groups.values())
        for group in groups.values():
            winner = max(group, key=lambda f: filed_date[f.filing_accession])
            for f in group:
                assert f.is_latest == (f is winner)

        # Negative value and frame, compared against the fixture's own values.
        assert sorted(f.value for f in facts if f.value < 0) == sorted(
            fact.val for *_, fact in kept if fact.val < 0
        )
        assert sorted(f.frame for f in facts if f.frame) == sorted(
            fact.frame for *_, fact in kept if fact.frame
        )

        load_run = (
            (await session.execute(select(LoadRun).where(LoadRun.company_cik == doc.cik)))
            .scalars()
            .one()
        )
        assert load_run.fact_count == doc.counts.facts
        assert load_run.facts_dropped_unit == len(dropped)

        concepts = (
            (await session.execute(select(Concept).where(Concept.name.startswith("ZzzTest"))))
            .scalars()
            .all()
        )
        assert len(concepts) == len({(tax, name) for tax, name, *_ in kept})

        null_concept = next(c for c in concepts if (c.taxonomy, c.name) == _null_concept_key(doc))
        assert null_concept.label is None
        assert null_concept.description is None


async def test_load_file_is_idempotent(test_session_factory, clean_fake_company) -> None:
    """Re-loading the same file must not raise -- proves the delete + reinsert
    (Filing/Fact) and upsert (Company/Concept) logic actually works."""
    kept, _dropped = _kept_and_dropped(_load_doc())

    first = await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    second = await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    assert first == second

    async with test_session_factory() as session:
        facts = (
            (await session.execute(select(Fact).where(Fact.company_cik == FAKE_CIK)))
            .scalars()
            .all()
        )
        assert len(facts) == len(kept)  # not doubled

        # "2", not fixture data -- this test calls load_file twice itself.
        load_runs = (
            (await session.execute(select(LoadRun).where(LoadRun.company_cik == FAKE_CIK)))
            .scalars()
            .all()
        )
        assert len(load_runs) == 2  # append-only audit log -- both loads recorded


# --------------------------------------------------------------------------- #
# Batch guards -- both raise before any database work, so no DB fixture needed.
# (conftest's autouse _isolate_xbrl_store points store_path() at a temp dir, so
# no real data/xbrl/ file exists during tests.)
# --------------------------------------------------------------------------- #


async def test_load_batch_rejects_unknown_identifier() -> None:
    with pytest.raises(UnknownCompanyError, match="nothing loaded"):
        await load_batch(["AAPL", "NOPE"])


async def test_load_batch_requires_the_store_file() -> None:
    with pytest.raises(FileNotFoundError, match="get-xbrl"):
        await load_batch(["AAPL"])
