"""Curated local XBRL-data store -- ``data/xbrl/<TICKER>.json``, one file per company.

The ``get-xbrl`` action (``get_xbrl_data()`` in ``actions.py``) fetches a
company's full SEC **XBRL data** document, filters it down to this project's
scope, and writes the result here as pretty-printed JSON. "XBRL data" is this
project's only name for the SEC endpoint
``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` -- see
sec-retriever.md.

Scope filter applied on ingest (sec-retriever.md section 5):

* **Forms:** only exact ``10-K`` and ``10-Q`` facts are kept -- amendments
  (``10-K/A`` etc.) and every other form are dropped.
* **Fiscal years:** a single fixed window, ``FISCAL_YEAR_MAX`` back
  ``FISCAL_YEARS_KEPT`` years -- currently **FY2021-FY2025** -- applied
  identically to all 20 corpus companies. A fact's ``fy`` is the fiscal year
  of the *filing* it was reported in (not the period it covers), so a kept
  10-K still carries its prior-year comparative figures.

  The window is a hard-coded constant, not derived per company, on purpose:
  the corpus is closed and every company must land on the *same* five fiscal
  years so cross-company questions line up. ``FISCAL_YEAR_MAX`` is the newest
  year for which **every** corpus company has filed a 10-K; a few (MSFT, NVDA,
  ORCL as of 2026-08) have a newer 10-K too, and those extra years are
  deliberately dropped. ``get-xbrl`` prints a stderr note when it sees such a
  company, as a signal that ``FISCAL_YEAR_MAX`` may be due to roll forward.

Individual fact objects are passed through untouched -- ``start`` / ``end`` /
``val`` / ``accn`` / ``fy`` / ``fp`` / ``form`` / ``filed`` / ``frame`` -- so the
claim verifier keeps full filing-level provenance. Concepts, units, and
taxonomies left empty by the filter are pruned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.ingest.corpus import CORPUS_FILE

# Sibling of corpus_companies.json / sic_numbers.json at the project root,
# under data/xbrl/. Resolved via CORPUS_FILE so it is CWD-independent, unlike
# the response cache. Redirected by tests (see tests/conftest.py).
XBRL_STORE_DIR = CORPUS_FILE.parent / "data" / "xbrl"

FORMS_IN_SCOPE: tuple[str, ...] = ("10-K", "10-Q")

# Newest *complete* fiscal year kept, and how many years back from it. All 20
# corpus companies have a 10-K for FY2021..FY2025. Bump FISCAL_YEAR_MAX (and
# re-run `get-xbrl`, which re-filters from cache) once every corpus company has
# filed a 10-K for the next year -- get-xbrl's stderr note flags when that day
# has come for some but not yet all of them.
FISCAL_YEAR_MAX = 2025
FISCAL_YEARS_KEPT = 5

_CONCEPT_META_KEYS = ("label", "description")


def _store_dir(directory: Path | None) -> Path:
    # Resolved at call time (not as a default arg) so tests can redirect the
    # module-level XBRL_STORE_DIR.
    return directory if directory is not None else XBRL_STORE_DIR


def store_path(ticker: str, *, directory: Path | None = None) -> Path:
    """Path to one company's curated store file, ``.../<TICKER>.json``."""
    return _store_dir(directory) / f"{ticker}.json"


def _iter_rows(raw_facts: dict[str, Any]) -> Any:
    for taxonomy in raw_facts.values():
        for concept in taxonomy.values():
            for rows in concept.get("units", {}).values():
                yield from rows


def latest_complete_fiscal_year(raw_facts: dict[str, Any], *, form: str = "10-K") -> int | None:
    """Highest ``fy`` for which ``form`` (default ``10-K``) appears in this
    company's raw facts -- its most recent *complete* annual cycle. ``None`` if
    the company has filed no such form."""
    years = [
        row["fy"]
        for row in _iter_rows(raw_facts)
        if row.get("form") == form and isinstance(row.get("fy"), int)
    ]
    return max(years) if years else None


def filter_facts(
    raw_facts: dict[str, Any],
    *,
    forms: tuple[str, ...] = FORMS_IN_SCOPE,
    fiscal_year_max: int = FISCAL_YEAR_MAX,
    fiscal_years_kept: int = FISCAL_YEARS_KEPT,
) -> tuple[dict[str, Any], list[int]]:
    """Filter a raw XBRL ``facts`` tree down to this project's scope.

    ``raw_facts`` is the ``facts`` object from a companyfacts payload:
    ``{taxonomy: {concept: {"label", "description", "units": {unit: [row, ...]}}}}``.

    Rows are kept when their ``form`` is in ``forms`` and their ``fy`` falls in
    the closed window ``[fiscal_year_max - fiscal_years_kept + 1, fiscal_year_max]``.
    Returns ``(filtered_facts, fiscal_years)`` -- the pruned tree plus the
    sorted list of distinct ``fy`` values that survived (``[]`` if none did).
    """
    forms_set = set(forms)
    min_fy = fiscal_year_max - fiscal_years_kept + 1

    def _in_scope(row: dict[str, Any]) -> bool:
        fy = row.get("fy")
        return (
            row.get("form") in forms_set and isinstance(fy, int) and min_fy <= fy <= fiscal_year_max
        )

    filtered: dict[str, Any] = {}
    kept_years: set[int] = set()
    for tax_name, taxonomy in raw_facts.items():
        out_taxonomy: dict[str, Any] = {}
        for concept_name, concept in taxonomy.items():
            out_units: dict[str, list[dict[str, Any]]] = {}
            for unit_name, rows in concept.get("units", {}).items():
                kept = [row for row in rows if _in_scope(row)]
                if kept:
                    out_units[unit_name] = kept
                    kept_years.update(row["fy"] for row in kept)
            if out_units:
                meta = {k: concept[k] for k in _CONCEPT_META_KEYS if k in concept}
                out_taxonomy[concept_name] = {**meta, "units": out_units}
        if out_taxonomy:
            filtered[tax_name] = out_taxonomy

    return filtered, sorted(kept_years)


def _count_facts(facts: dict[str, Any]) -> tuple[int, int]:
    """Return ``(concept_count, fact_row_count)`` for a filtered facts tree."""
    concepts = 0
    rows = 0
    for taxonomy in facts.values():
        concepts += len(taxonomy)
        for concept in taxonomy.values():
            for unit_rows in concept["units"].values():
                rows += len(unit_rows)
    return concepts, rows


def build_document(
    company: dict[str, Any],
    raw_companyfacts: dict[str, Any],
    *,
    retrieved: str | None = None,
    forms: tuple[str, ...] = FORMS_IN_SCOPE,
    fiscal_year_max: int = FISCAL_YEAR_MAX,
    fiscal_years_kept: int = FISCAL_YEARS_KEPT,
) -> dict[str, Any]:
    """Assemble the curated store document for one company from its raw SEC
    XBRL data payload (``raw_companyfacts``) and its ``corpus_companies.json``
    entry (``company``)."""
    filtered, fiscal_years = filter_facts(
        raw_companyfacts.get("facts", {}),
        forms=forms,
        fiscal_year_max=fiscal_year_max,
        fiscal_years_kept=fiscal_years_kept,
    )
    concepts, fact_rows = _count_facts(filtered)
    return {
        "cik": raw_companyfacts.get("cik", company["cik"]),
        "ticker": company["ticker"],
        "entity_name": raw_companyfacts.get("entityName", company["title"]),
        "source_url": company["companyfacts_url"],
        "retrieved": retrieved or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {
            "forms": list(forms),
            "fiscal_years": fiscal_years,
        },
        "counts": {
            "taxonomies": len(filtered),
            "concepts": concepts,
            "facts": fact_rows,
        },
        "facts": filtered,
    }


def _manifest_entry(document: dict[str, Any], *, path: Path, size: int) -> dict[str, Any]:
    return {
        "ticker": document["ticker"],
        "path": str(path),
        "fiscal_years": document["scope"]["fiscal_years"],
        "taxonomies": document["counts"]["taxonomies"],
        "concepts": document["counts"]["concepts"],
        "facts": document["counts"]["facts"],
        "bytes": size,
    }


def write_store(
    company: dict[str, Any],
    raw_companyfacts: dict[str, Any],
    *,
    directory: Path | None = None,
    retrieved: str | None = None,
    forms: tuple[str, ...] = FORMS_IN_SCOPE,
    fiscal_year_max: int = FISCAL_YEAR_MAX,
    fiscal_years_kept: int = FISCAL_YEARS_KEPT,
) -> dict[str, Any]:
    """Write ``data/xbrl/<TICKER>.json`` for one company (pretty-printed) and
    return a small manifest entry describing what was written."""
    document = build_document(
        company,
        raw_companyfacts,
        retrieved=retrieved,
        forms=forms,
        fiscal_year_max=fiscal_year_max,
        fiscal_years_kept=fiscal_years_kept,
    )
    target = store_path(company["ticker"], directory=directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    return _manifest_entry(document, path=target, size=len(text.encode("utf-8")))


def load_store(ticker: str, *, directory: Path | None = None) -> dict[str, Any] | None:
    """Return one company's curated store document, or ``None`` if not written yet."""
    target = store_path(ticker, directory=directory)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
