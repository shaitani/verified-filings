"""Tests for app/db/loader.py.

test_build_plan_transform is pure -- no database. The rest run against the
real "db-test" PostgreSQL container (tests/conftest.py); see LOADER.md.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import Company, Concept, Fact, Filing, LoadRun
from app.db.loader import build_plan, load_batch, load_file
from app.ingest.corpus import UnknownCompanyError
from app.schemas.xbrl import CompanyFactsFile
from tests.conftest import FAKE_CIK

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "xbrl_fake_company.json"


def _load_doc() -> CompanyFactsFile:
    raw = json.loads(FIXTURE_PATH.read_text("utf-8"))
    return CompanyFactsFile.model_validate(raw)


def test_build_plan_transform() -> None:
    plan = build_plan(_load_doc())

    assert plan.company.cik == FAKE_CIK
    assert plan.company.ticker == "ZZZZ"
    assert len(plan.filings) == 2  # two accession numbers in the fixture
    # 4, not 5: ZzzTestLawsuits' only fact has a disallowed unit and is
    # dropped before the concept is even registered -- it never reaches the
    # concept table since no Fact will ever reference it.
    assert len(plan.concepts) == 4
    assert plan.dropped_unit == 1  # the "lawsuit"-unit fact
    assert len(plan.facts) == 6  # 7 total, minus the dropped one

    # Negative value -- Decimal handles the sign exactly, same as any other.
    net_loss = next(row for key, row in plan.facts if key == ("us-gaap", "ZzzTestNetLoss"))
    assert net_loss.value == Decimal("-150000.75")

    # frame passes through when present, stays None when it's not.
    shares = next(row for key, row in plan.facts if key == ("dei", "ZzzTestSharesOutstanding"))
    assert shares.frame == "CY2024Q4I"
    assert net_loss.frame is None

    # Restatement: same period, two filings -- only the newer filed date wins.
    assets_2023 = [
        row
        for key, row in plan.facts
        if key == ("us-gaap", "ZzzTestAssets") and row.period_end == date(2023, 12, 31)
    ]
    assert len(assets_2023) == 2
    latest = next(r for r in assets_2023 if r.is_latest)
    older = next(r for r in assets_2023 if not r.is_latest)
    assert latest.filing_accession == "0009999999-24-000001"
    assert older.filing_accession == "0009999999-23-000001"

    # A period reported only once is still is_latest.
    assets_2024 = next(
        row
        for key, row in plan.facts
        if key == ("us-gaap", "ZzzTestAssets") and row.period_end == date(2024, 12, 31)
    )
    assert assets_2024.is_latest

    # Null label/description pass through untouched.
    assert plan.concepts[("dei", "ZzzTestSharesOutstanding")] == {
        "label": None,
        "description": None,
    }


async def test_load_file_inserts_expected_rows(test_session_factory, clean_fake_company) -> None:
    result = await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    assert (result.filings, result.facts, result.dropped_unit) == (2, 6, 1)

    async with test_session_factory() as session:
        company = await session.get(Company, FAKE_CIK)
        assert company is not None
        assert company.ticker == "ZZZZ"

        filings = (
            (await session.execute(select(Filing).where(Filing.company_cik == FAKE_CIK)))
            .scalars()
            .all()
        )
        assert len(filings) == 2

        facts = (
            (await session.execute(select(Fact).where(Fact.company_cik == FAKE_CIK)))
            .scalars()
            .all()
        )
        assert len(facts) == 6

        # Negative value round-trips through Numeric(30, 6) exactly.
        net_loss = next(f for f in facts if f.value < 0)
        assert net_loss.value == Decimal("-150000.75")
        assert net_loss.frame is None

        # frame is stored when the source file has one.
        shares = next(f for f in facts if f.unit == "shares")
        assert shares.frame == "CY2024Q4I"

        # Exactly one is_latest among the two restated rows, and it's the newer filing.
        restated = [f for f in facts if f.period_end == date(2023, 12, 31)]
        assert len(restated) == 2
        assert sum(f.is_latest for f in restated) == 1
        winner = next(f for f in restated if f.is_latest)
        winning_filing = await session.get(Filing, winner.filing_accession)
        assert winning_filing.filed_date == date(2024, 11, 1)

        # Exact cents survived: float 500000.47 in the source file -> Decimal.
        revenue = next(f for f in facts if f.period_start is not None)
        assert revenue.value == Decimal("500000.47")

        load_run = (
            (await session.execute(select(LoadRun).where(LoadRun.company_cik == FAKE_CIK)))
            .scalars()
            .one()
        )
        assert load_run.fact_count == 7  # from the file's counts block -- includes the dropped fact
        assert load_run.facts_dropped_unit == 1

        concepts = (
            (await session.execute(select(Concept).where(Concept.name.startswith("ZzzTest"))))
            .scalars()
            .all()
        )
        assert len(concepts) == 4  # not ZzzTestLawsuits -- see test_build_plan_transform
        shares = next(c for c in concepts if c.name == "ZzzTestSharesOutstanding")
        assert shares.label is None
        assert shares.description is None


async def test_load_file_is_idempotent(test_session_factory, clean_fake_company) -> None:
    """Re-loading the same file must not raise -- proves the delete + reinsert
    (Filing/Fact) and upsert (Company/Concept) logic actually works."""
    first = await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    second = await load_file(FIXTURE_PATH, session_factory=test_session_factory)
    assert first == second

    async with test_session_factory() as session:
        facts = (
            (await session.execute(select(Fact).where(Fact.company_cik == FAKE_CIK)))
            .scalars()
            .all()
        )
        assert len(facts) == 6  # not doubled

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
