"""Auto-maintained SIC index -- ``sic_numbers.json`` at the project root.

Every time ``get-submission`` fetches a company's submissions JSON, that
company's row in ``sic_numbers.json`` is refreshed from it: ``get_submissions()``
in ``actions.py`` calls ``update_sic_index()`` as its final step. The file
therefore always reflects exactly the corpus companies that have been fetched
at least once -- there is no separate build step to run.

The format is fixed by sec-retriever.md section 5.1: a top-level JSON array,
one object per fetched company, in ``corpus_companies.json`` order, each with
exactly these string keys, in this order:

===============  ============================================================
``cik``          ``"CIK"`` + zero-padded 10-digit CIK
``ticker``       primary ticker
``company_name`` SEC's own ``name`` for the entity (casing as SEC records it)
``sic``          4-digit SIC code, as a string
``sic_description`` SEC's ``sicDescription`` text, verbatim
===============  ============================================================
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.ingest.corpus import CORPUS_FILE, load_corpus

SIC_INDEX_FILE = CORPUS_FILE.parent / "sic_numbers.json"

_KEY_ORDER = ("cik", "ticker", "company_name", "sic", "sic_description")
_REQUIRED_SUBMISSION_FIELDS = ("name", "sic", "sicDescription")


def _index_path(path: Path | None) -> Path:
    # Resolved at call time (not as a default argument) so tests can
    # redirect the module-level SIC_INDEX_FILE.
    return path if path is not None else SIC_INDEX_FILE


def load_sic_index(path: Path | None = None) -> list[dict[str, str]]:
    """Return the current ``sic_numbers.json`` contents, or ``[]`` if the
    file does not exist yet."""
    target = _index_path(path)
    if not target.exists():
        return []
    return json.loads(target.read_text(encoding="utf-8"))


def _row(cik_padded: str, ticker: str, submissions: dict[str, Any]) -> dict[str, str]:
    return {
        "cik": f"CIK{cik_padded}",
        "ticker": ticker,
        "company_name": submissions["name"],
        "sic": str(submissions["sic"]),
        "sic_description": submissions["sicDescription"],
    }


def update_sic_index(
    submissions_by_ticker: dict[str, Any], *, path: Path | None = None
) -> list[dict[str, str]]:
    """Upsert one row per just-fetched company into ``sic_numbers.json``.

    ``submissions_by_ticker`` is exactly what ``get_submissions()`` returns:
    canonical ticker -> that company's submissions JSON. Each listed
    company's row is rebuilt from its submissions JSON; rows already in the
    file for companies *not* in this batch are kept as-is. The file is then
    rewritten in ``corpus_companies.json`` order and contains only companies
    fetched at least once.

    An entry whose submissions JSON lacks ``name``/``sic``/``sicDescription``
    (e.g. a stubbed test payload) is skipped. If that leaves nothing to
    write and no file exists yet, nothing is written.

    Returns the rows as written (or as they would be), in file order.
    """
    target = _index_path(path)
    corpus = load_corpus()
    corpus_order = {company["ticker"]: i for i, company in enumerate(corpus)}
    cik_by_ticker = {company["ticker"]: company["cik_padded"] for company in corpus}

    rows: dict[str, dict[str, str]] = {}
    for existing in load_sic_index(target):
        rows[existing["ticker"]] = {key: existing[key] for key in _KEY_ORDER}

    for ticker, submissions in submissions_by_ticker.items():
        if ticker not in cik_by_ticker:
            continue
        if not all(field in submissions for field in _REQUIRED_SUBMISSION_FIELDS):
            continue
        rows[ticker] = _row(cik_by_ticker[ticker], ticker, submissions)

    if not rows and not target.exists():
        return []

    ordered = sorted(
        rows.values(),
        key=lambda row: corpus_order.get(row["ticker"], len(corpus_order)),
    )
    target.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    return ordered
