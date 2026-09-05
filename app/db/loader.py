"""Load curated XBRL-data files (data/xbrl/<TICKER>.json) into the database.

    python -m app.db.loader TICKER [TICKER ...]

Standalone entry point -- not part of app/cli.py. Retrieval (SEC -> disk,
app/ingest/) and loading (disk -> DB, here) stay separate steps joined only by
the files in data/xbrl/. See LOADER.md for the full write-up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import ALLOWED_UNITS, Company, Concept, Fact, Filing, LoadRun
from app.db.session import SessionLocal
from app.ingest.corpus import UnknownCompanyError, find_company
from app.ingest.xbrl_store import store_path
from app.schemas.xbrl import CompanyFactsFile

# --------------------------------------------------------------------------- #
# Pure transform: validated file -> everything needed to load it. No I/O, so
# this half is unit-testable without a database.
# --------------------------------------------------------------------------- #


@dataclass
class _FactRow:
    filing_accession: str
    unit: str
    period_start: date | None
    period_end: date
    is_instant: bool
    value: Decimal
    frame: str | None
    filed: date  # only used to pick is_latest; not stored
    is_latest: bool = False


@dataclass
class LoadPlan:
    company: Company
    filings: dict[str, Filing]  # accession_number -> Filing
    concepts: dict[tuple[str, str], dict]  # (taxonomy, name) -> {label, description}
    facts: list[tuple[tuple[str, str], _FactRow]]  # (concept key, row)
    dropped_unit: int


def build_plan(doc: CompanyFactsFile) -> LoadPlan:
    """Turn one validated XBRL-data document into rows ready to insert."""
    filings: dict[str, Filing] = {}
    concepts: dict[tuple[str, str], dict] = {}
    facts: list[tuple[tuple[str, str], _FactRow]] = []
    dropped_unit = 0
    # Groups facts that describe the same period so the newest filing's value
    # can be picked as is_latest -- see the loop below.
    groups: dict[tuple, list[_FactRow]] = defaultdict(list)

    for taxonomy, name, concept, unit, fact in doc.iter_facts():
        if unit not in ALLOWED_UNITS:
            dropped_unit += 1
            continue

        filings.setdefault(
            fact.accn,
            Filing(
                accession_number=fact.accn,
                company_cik=doc.cik,
                form=fact.form,
                fiscal_year=fact.fy,
                fiscal_period=fact.fp,
                filed_date=fact.filed,
            ),
        )

        key = (taxonomy, name)
        meta = concepts.setdefault(key, {"label": None, "description": None})
        meta["label"] = meta["label"] or concept.label
        meta["description"] = meta["description"] or concept.description

        row = _FactRow(
            filing_accession=fact.accn,
            unit=unit,
            period_start=fact.start,
            period_end=fact.end,
            is_instant=fact.start is None,
            value=fact.val,
            frame=fact.frame,
            filed=fact.filed,
        )
        facts.append((key, row))
        groups[(*key, unit, fact.start, fact.end)].append(row)

    for group in groups.values():
        max(group, key=lambda r: r.filed).is_latest = True

    company = Company(
        cik=doc.cik, ticker=doc.ticker, entity_name=doc.entity_name, source_url=doc.source_url
    )
    return LoadPlan(company, filings, concepts, facts, dropped_unit)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


@dataclass
class LoadResult:
    filings: int
    facts: int
    dropped_unit: int


class LoadBatchError(RuntimeError):
    """One company's load failed partway through a batch.

    Companies loaded *before* the failing one stay committed -- each
    company's load is its own independent transaction, so there is nothing
    to roll back for them.
    """

    def __init__(self, failed_ticker: str, completed: dict[str, LoadResult], *, remaining: int = 0):
        super().__init__(f"load failed for {failed_ticker}")
        self.failed_ticker = failed_ticker
        self.completed = completed
        self.remaining = remaining  # companies never attempted because of the abort


async def load_file(
    path: Path, *, session_factory: async_sessionmaker = SessionLocal
) -> LoadResult:
    """Validate and load one curated XBRL-data file. One transaction.

    Plain json.loads is fine here -- no parse_float=Decimal needed. Pydantic's
    Decimal validator converts a float via its *string* form (str(500000.47)
    -> Decimal('500000.47')), not the raw binary value, so precision only
    depends on this going straight into CompanyFactsFile.model_validate()
    before anything else touches `val`.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    doc = CompanyFactsFile.model_validate(raw)
    plan = build_plan(doc)

    async with session_factory.begin() as session:
        # Company: upsert so `id` (the inert identity column) stays stable
        # across reloads, rather than churning on every re-run.
        await session.execute(
            pg_insert(Company)
            .values(
                cik=plan.company.cik,
                ticker=plan.company.ticker,
                entity_name=plan.company.entity_name,
                source_url=plan.company.source_url,
            )
            .on_conflict_do_update(
                index_elements=[Company.cik],
                set_={
                    "ticker": plan.company.ticker,
                    "entity_name": plan.company.entity_name,
                    "source_url": plan.company.source_url,
                },
            )
        )

        # Wipe this company's Filing rows -- Fact cascades away with them
        # (ON DELETE CASCADE). LoadRun is untouched: it's an append-only log
        # of every load that ever happened, not current-state data.
        await session.execute(delete(Filing).where(Filing.company_cik == doc.cik))

        # Concept is global (shared by every company): one bulk upsert, keeping
        # whichever label/description was already there unless it was null.
        # RETURNING hands back the surrogate ids, so no follow-up SELECT.
        concept_id: dict[tuple[str, str], int] = {}
        if plan.concepts:
            stmt = pg_insert(Concept).values(
                [
                    {"taxonomy": taxonomy, "name": name, **meta}
                    for (taxonomy, name), meta in plan.concepts.items()
                ]
            )
            rows = await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Concept.taxonomy, Concept.name],
                    set_={
                        "label": func.coalesce(Concept.label, stmt.excluded.label),
                        "description": func.coalesce(
                            Concept.description, stmt.excluded.description
                        ),
                    },
                ).returning(Concept.id, Concept.taxonomy, Concept.name)
            )
            concept_id = {(r.taxonomy, r.name): r.id for r in rows}

        session.add_all(plan.filings.values())
        session.add_all(
            Fact(
                filing_accession=row.filing_accession,
                company_cik=doc.cik,
                concept_id=concept_id[key],
                unit=row.unit,
                period_start=row.period_start,
                period_end=row.period_end,
                is_instant=row.is_instant,
                value=row.value,
                frame=row.frame,
                is_latest=row.is_latest,
            )
            for key, row in plan.facts
        )
        session.add(
            LoadRun(
                company_cik=doc.cik,
                source_url=doc.source_url,
                retrieved_at=doc.retrieved,
                scope_forms=list(doc.scope.forms),
                scope_fiscal_years=list(doc.scope.fiscal_years),
                taxonomy_count=doc.counts.taxonomies,
                concept_count=doc.counts.concepts,
                fact_count=doc.counts.facts,
                units_allowlist=sorted(ALLOWED_UNITS),
                facts_dropped_unit=plan.dropped_unit,
            )
        )

    return LoadResult(
        filings=len(plan.filings), facts=len(plan.facts), dropped_unit=plan.dropped_unit
    )


async def load_batch(
    identifiers: Sequence[str], *, session_factory: async_sessionmaker = SessionLocal
) -> dict[str, LoadResult]:
    """Load one or more corpus companies' curated files.

    Mirrors get_xbrl_data()'s identifier handling: every identifier is
    resolved against the corpus *before* anything is loaded -- if any of
    them don't match, nothing is loaded and UnknownCompanyError names every
    failure. Companies then load sequentially; if one fails, the batch stops
    there (see LoadBatchError) rather than continuing best-effort.
    """
    tickers: list[str] = []
    unresolved = []
    for identifier in identifiers:
        try:
            ticker = find_company(identifier)["ticker"]
        except UnknownCompanyError:
            unresolved.append(identifier)
            continue
        if ticker not in tickers:  # "GOOG GOOGL" is one company, load it once
            tickers.append(ticker)
    if unresolved:
        raise UnknownCompanyError(
            f"{unresolved!r} did not match any of the 20 corpus companies -- nothing loaded"
        )

    # Check every file up front too, so a batch can't load half its companies
    # and then stop because one was never retrieved.
    missing = [t for t in tickers if not store_path(t).exists()]
    if missing:
        raise FileNotFoundError(
            f"no curated XBRL-data file for {missing!r} -- run "
            f"`sec-retriever get-xbrl {' '.join(missing)}` first. Nothing loaded."
        )

    results: dict[str, LoadResult] = {}
    for ticker in tickers:
        try:
            results[ticker] = await load_file(store_path(ticker), session_factory=session_factory)
        except Exception as exc:
            remaining = len(tickers) - len(results) - 1
            raise LoadBatchError(ticker, results, remaining=remaining) from exc
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _report(results: dict[str, LoadResult]) -> None:
    for ticker, r in results.items():
        note = f", {r.dropped_unit} dropped (unit not allowed)" if r.dropped_unit else ""
        print(f"[load] {ticker}: OK ({r.filings} filings, {r.facts} facts{note})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db.loader")
    parser.add_argument(
        "identifiers",
        nargs="+",
        help="One or more company tickers (e.g. AAPL) or CIKs, from corpus_companies.json.",
    )
    args = parser.parse_args(argv)

    try:
        results = asyncio.run(load_batch(args.identifiers))
    except (UnknownCompanyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except LoadBatchError as exc:
        _report(exc.completed)
        print(f"[load] {exc.failed_ticker}: FAILED - {exc.__cause__}", file=sys.stderr)
        skipped = f", {exc.remaining} not attempted" if exc.remaining else ""
        print(
            f"[load] aborted: {len(exc.completed)} loaded, 1 failed{skipped}",
            file=sys.stderr,
        )
        return 1

    _report(results)
    noun = "company" if len(results) == 1 else "companies"
    print(f"[load] {len(results)}/{len(results)} {noun} loaded", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
