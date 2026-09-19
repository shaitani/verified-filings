"""Auto-maintained company name/ticker lexicon -- ``company_aliases.json`` at
the project root.

Maintained exactly like ``sic_numbers.json`` (see ``sic_index.py``): every
``get-submission`` refreshes the fetched companies' rows, so there is no
separate build step. It is a *derived cache*, safe to delete.

Why this exists
---------------
A question says "Google", "Facebook" or "Bank of America". The store holds
``Alphabet Inc.``, ``Meta Platforms, Inc.`` and ``BofA Finance LLC``, so the
mapper's company lookup -- ticker equality, then a substring match on
``entity_name`` -- found none of them. Measured against the corpus, name-only
lookup missed Google, Facebook, Bank of America, AMD, Johnson and Johnson and
United Health.

Where the names come from, and what is *not* here
-------------------------------------------------
Two SEC sources, both already fetched:

``corpus_companies.json``
    Built from the SEC's ``company_tickers.json``. Carries ``title`` (the
    SEC's own name), ``input_name`` (what a person called the company when
    the corpus was assembled -- this is where "Google" and "Bank Of America"
    come from) and ``all_tickers``, which includes every share class.

the submissions endpoint
    Carries ``name``, the current ``tickers``, and **``formerNames``** with
    from/to dates. This is the history: Meta was ``Facebook Inc``, Chevron was
    ``CHEVRONTEXACO CORP``, UnitedHealth was ``UNITED HEALTHCARE CORP``.

**Historical tickers are not obtainable here, and this file does not pretend
otherwise.** ``company_tickers.json`` and the submissions endpoint both give
only *current* tickers; neither carries a ticker history, so a question using
a retired symbol (FB rather than META) will not resolve. The obvious in-house
source would be ``dei:TradingSymbol`` from each filing's cover page, which
would give the symbol as reported at the time -- but the SEC's XBRL data
endpoint returns only numeric facts, and ``TradingSymbol`` is a string, so it
is absent from the store entirely (the store holds three ``dei`` concepts, all
numeric). Historical tickers would need a different data source.

None of that threatens correctness, because **the CIK never changes**. Every
alias here resolves to a cik, and a renamed or re-symboled company keeps its
own. A missing alias costs a refusal, never a wrong company.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.ingest.corpus import CORPUS_FILE, load_corpus

ALIAS_INDEX_FILE = CORPUS_FILE.parent / "company_aliases.json"

_KEY_ORDER = ("cik", "ticker", "names", "tickers")

#: Trailing words that make a legal name rather than the name people say.
#: Stripped from the end, repeatedly, to derive a short form: "UNITEDHEALTH
#: GROUP INC" -> "unitedhealth", "BANK OF AMERICA CORP /DE/" -> "bank of
#: america". Only ever *adds* an alias; the full name stays indexed too.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "llc",
        "lp",
        "plc",
        "holdings",
        "holding",
        "group",
        "sa",
        "nv",
        "ag",
    }
)

#: EDGAR's state-of-incorporation marker, e.g. "BANK OF AMERICA CORP /DE/".
_STATE_MARKER = re.compile(r"/[A-Z]{2}/?", re.IGNORECASE)

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def _index_path(path: Path | None) -> Path:
    # Resolved at call time so tests can redirect ALIAS_INDEX_FILE.
    return path if path is not None else ALIAS_INDEX_FILE


def load_alias_index(path: Path | None = None) -> list[dict[str, Any]]:
    """The current file contents, or ``[]`` if it does not exist yet."""
    target = _index_path(path)
    if not target.exists():
        return []
    return json.loads(target.read_text(encoding="utf-8"))


def name_variants(name: str) -> list[str]:
    """Every spelling of one company name worth indexing, longest first.

    Three things, because each fixes a real miss measured against the corpus:

    * the name as recorded -- ``JOHNSON & JOHNSON``
    * an "and" spelling, since a person types "Johnson and Johnson" while
      EDGAR writes the ampersand, and dropping punctuation folds those two to
      different strings
    * a short form with the state marker and legal suffixes stripped, so
      ``Facebook Inc`` answers to "Facebook" and ``BANK OF AMERICA CORP /DE/``
      to "Bank of America"

    Returns raw spellings; normalizing them is the reader's job.
    """
    cleaned = _STATE_MARKER.sub(" ", name)
    variants = [cleaned]
    if "&" in cleaned:
        variants.append(cleaned.replace("&", " and "))

    for variant in list(variants):
        words = _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", variant)).strip().split()
        while len(words) > 1 and words[-1].lower() in _LEGAL_SUFFIXES:
            words.pop()
        short = " ".join(words)
        if short and short.lower() != variant.strip().lower():
            variants.append(short)

    seen: list[str] = []
    for variant in variants:
        collapsed = _WHITESPACE.sub(" ", variant).strip()
        if collapsed and collapsed not in seen:
            seen.append(collapsed)
    return seen


def _row(company: dict[str, Any], submissions: dict[str, Any]) -> dict[str, Any]:
    names: list[str] = []
    for source in (
        submissions.get("name"),
        company.get("title"),
        company.get("input_name"),
        *(former.get("name") for former in submissions.get("formerNames") or ()),
    ):
        if not source:
            continue
        for variant in name_variants(source):
            if variant not in names:
                names.append(variant)

    tickers: list[str] = []
    sources = (company["ticker"], *company.get("all_tickers", ()), *submissions.get("tickers", ()))
    for ticker in sources:
        if ticker and ticker not in tickers:
            tickers.append(ticker)

    return {
        "cik": f"CIK{company['cik_padded']}",
        "ticker": company["ticker"],
        "names": names,
        "tickers": tickers,
    }


def update_alias_index(
    submissions_by_ticker: dict[str, Any], *, path: Path | None = None
) -> list[dict[str, Any]]:
    """Upsert one row per just-fetched company, same contract as
    ``sic_index.update_sic_index()``.

    Rows for companies not in this batch are kept; the file is rewritten in
    ``corpus_companies.json`` order. An entry whose submissions JSON carries no
    ``name`` is skipped, so a stubbed test payload cannot half-write a row.
    """
    target = _index_path(path)
    corpus = load_corpus()
    corpus_order = {company["ticker"]: i for i, company in enumerate(corpus)}
    by_ticker = {company["ticker"]: company for company in corpus}

    rows: dict[str, dict[str, Any]] = {}
    for existing in load_alias_index(target):
        rows[existing["ticker"]] = {key: existing[key] for key in _KEY_ORDER}

    for ticker, submissions in submissions_by_ticker.items():
        if ticker not in by_ticker or not submissions.get("name"):
            continue
        rows[ticker] = _row(by_ticker[ticker], submissions)

    if not rows and not target.exists():
        return []

    ordered = sorted(
        rows.values(), key=lambda row: corpus_order.get(row["ticker"], len(corpus_order))
    )
    target.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    return ordered
