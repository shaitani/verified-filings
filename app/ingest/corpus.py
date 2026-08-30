"""Loader for the closed-corpus company list.

`corpus_companies.json` (project root) is the master list of the 20
companies this project will ever cover — see sec-retriever.md, Section 5.
This module is the single place that reads it and resolves a caller-
supplied identifier (ticker, ticker alias, or CIK) to the matching entry.

Ingest code should always resolve identifiers through `find_company()`
rather than accepting a raw CIK/URL from elsewhere, so a request can never
silently be made for a company outside the 20-company corpus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CORPUS_FILE = Path(__file__).resolve().parents[2] / "corpus_companies.json"


class UnknownCompanyError(ValueError):
    """Raised when an identifier does not match any corpus company."""


def load_corpus(path: Path = CORPUS_FILE) -> list[dict[str, Any]]:
    """Return the `companies` list from corpus_companies.json."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["companies"]


def find_company(identifier: str, *, path: Path = CORPUS_FILE) -> dict[str, Any]:
    """Resolve `identifier` to its corpus_companies.json entry.

    Matches case-insensitively against the primary ticker, any ticker
    alias in `all_tickers`, the raw integer CIK, or the zero-padded CIK.
    Raises `UnknownCompanyError` if nothing in the 20-company corpus
    matches.
    """
    needle = identifier.strip().upper()
    for company in load_corpus(path):
        tickers = {company["ticker"], *company.get("all_tickers", [])}
        if needle in {t.upper() for t in tickers}:
            return company
        if needle == str(company["cik"]):
            return company
        if needle == company["cik_padded"].upper():
            return company

    raise UnknownCompanyError(
        f"{identifier!r} is not one of the 20 corpus companies in {path.name}"
    )
