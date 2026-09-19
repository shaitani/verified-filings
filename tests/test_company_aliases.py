"""Tests for the derived company lexicon.

``app/ingest/alias_index.py`` writes ``company_aliases.json`` from SEC data;
``app/semantic/company_aliases.py`` reads it. The two halves never import each
other -- the file path is the contract -- so they are tested together here,
through it.
"""

from __future__ import annotations

import json

import pytest

from app.ingest.alias_index import name_variants, update_alias_index
from app.semantic.company_aliases import ALIAS_FILE, load_company_aliases


@pytest.mark.parametrize(
    ("recorded", "typed"),
    [
        ("Facebook Inc", "Facebook"),
        ("BANK OF AMERICA CORP /DE/", "Bank of America"),
        ("UNITEDHEALTH GROUP INC", "UnitedHealth"),
        ("JOHNSON & JOHNSON", "Johnson and Johnson"),
        ("JOHNSON & JOHNSON", "Johnson & Johnson"),
        ("Alphabet Inc.", "Alphabet"),
    ],
)
def test_name_variants_cover_how_people_actually_write_it(recorded: str, typed: str) -> None:
    """Each pair is a miss measured against the real corpus: EDGAR's spelling
    on the left, what someone types on the right."""
    from app.semantic.metric_aliases import normalize

    folded = {normalize(v) for v in name_variants(recorded)}
    assert normalize(typed) in folded


def test_name_variants_keep_the_full_name_too() -> None:
    """The short form is an addition, never a replacement -- someone who types
    the legal name has to find it."""
    assert "BANK OF AMERICA CORP" in " ".join(name_variants("BANK OF AMERICA CORP /DE/"))


def test_index_round_trips_through_the_file(tmp_path) -> None:
    """The two halves are wired together only by the file, so the test goes
    through it as well."""
    path = tmp_path / "company_aliases.json"
    update_alias_index(
        {
            "META": {
                "name": "Meta Platforms, Inc.",
                "tickers": ["META"],
                "formerNames": [{"name": "Facebook Inc"}],
            }
        },
        path=path,
    )

    index = load_company_aliases(path)
    assert index.lookup("Facebook") == [1326801]
    assert index.lookup("Meta Platforms") == [1326801]
    assert index.lookup("META") == [1326801]
    assert index.lookup("Nothing In Particular") == []


def test_a_stub_payload_does_not_half_write_a_row(tmp_path) -> None:
    """Same guard as the SIC index: a submissions payload without a name is
    skipped rather than producing a row with nothing in it."""
    path = tmp_path / "company_aliases.json"
    assert update_alias_index({"META": {"tickers": ["META"]}}, path=path) == []
    assert not path.exists()


def test_a_missing_file_is_an_empty_index_not_an_error(tmp_path) -> None:
    """It is a derived cache. Deleting it must degrade the resolver to its own
    SQL lookup, not break it."""
    index = load_company_aliases(tmp_path / "absent.json")
    assert len(index) == 0
    assert index.lookup("Apple") == []


def test_shipped_file_resolves_the_names_that_used_to_miss() -> None:
    """The file in the repo, not a fixture. These six are exactly the lookups
    that failed before it existed."""
    index = load_company_aliases()
    rows = json.loads(ALIAS_FILE.read_text("utf-8"))
    assert len(rows) == 20

    for typed, ticker in [
        ("Google", "GOOGL"),
        ("Facebook", "META"),
        ("Bank of America", "BAC"),
        ("AMD", "AMD"),
        ("Johnson and Johnson", "JNJ"),
        ("United Health", "UNH"),
    ]:
        expected = next(int(r["cik"].removeprefix("CIK")) for r in rows if r["ticker"] == ticker)
        assert index.lookup(typed) == [expected], typed


def test_share_classes_reach_the_same_company() -> None:
    """"GOOG" is Alphabet even though the stored ticker is GOOGL. The corpus
    carries every class, so the lexicon does too."""
    index = load_company_aliases()
    assert index.lookup("GOOG") == index.lookup("GOOGL") != []
