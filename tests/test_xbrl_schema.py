"""Tests for the inbound XBRL-file schemas (``app/schemas/xbrl.py``).

No network. ``VALID`` is a miniature well-formed ``data/xbrl/<TICKER>.json``
document; each test mutates a copy of it to trip one validation rule.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.xbrl import CompanyFactsFile, FactIn

VALID: dict = {
    "cik": 320193,
    "ticker": "AAPL",
    "entity_name": "Apple Inc.",
    "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
    "retrieved": "2026-08-30T02:30:47Z",
    "scope": {"forms": ["10-K", "10-Q"], "fiscal_years": [2021, 2022, 2023, 2024, 2025]},
    "counts": {"taxonomies": 2, "concepts": 2, "facts": 3},
    "facts": {
        "us-gaap": {
            "Assets": {
                "label": "Assets",
                "description": "Sum of the carrying amounts of all assets.",
                "units": {
                    "USD": [
                        {
                            "end": "2023-09-30",
                            "val": 352583000000,
                            "accn": "0000320193-23-000106",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-11-03",
                            "frame": "CY2023Q3I",
                        },
                        {
                            "start": "2022-09-25",
                            "end": "2023-09-30",
                            "val": "0.1234",
                            "accn": "0000320193-23-000106",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-11-03",
                        },
                    ]
                },
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Entity Common Stock, Shares Outstanding",
                "description": None,
                "units": {
                    "shares": [
                        {
                            "end": "2023-10-20",
                            "val": 15552752000,
                            "accn": "0000320193-23-000106",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-11-03",
                        }
                    ]
                },
            },
        },
    },
}


def _mutate(**overrides) -> dict:
    """Deep copy of VALID with top-level keys replaced."""
    doc = copy.deepcopy(VALID)
    doc.update(overrides)
    return doc


def test_valid_fixture_validates() -> None:
    m = CompanyFactsFile.model_validate(VALID)
    assert m.cik == 320193
    assert m.retrieved.tzinfo is not None


def test_iter_facts_walks_every_row() -> None:
    m = CompanyFactsFile.model_validate(VALID)
    rows = list(m.iter_facts())
    assert len(rows) == 3
    taxonomies = {tax for tax, *_ in rows}
    assert taxonomies == {"us-gaap", "dei"}
    # tuple shape: (taxonomy, concept_name, ConceptIn, unit, FactIn)
    tax, name, concept, unit, fact = rows[0]
    assert (tax, name, unit) == ("us-gaap", "Assets", "USD")
    assert concept.label == "Assets"
    assert isinstance(fact, FactIn)


def test_val_parsed_as_exact_decimal() -> None:
    m = CompanyFactsFile.model_validate(VALID)
    _, _, _, _, fact = next(r for r in m.iter_facts() if r[4].val == Decimal("0.1234"))
    assert isinstance(fact.val, Decimal)


def test_frozen_after_validation() -> None:
    m = CompanyFactsFile.model_validate(VALID)
    with pytest.raises(ValidationError):
        m.cik = 1


def test_unknown_top_level_key_rejected() -> None:
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(_mutate(surprise="new field"))


def test_unknown_nested_key_rejected() -> None:
    doc = copy.deepcopy(VALID)
    doc["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["extra"] = 1
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(doc)


def test_bad_fiscal_period_rejected() -> None:
    doc = copy.deepcopy(VALID)
    doc["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["fp"] = "Q4"
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(doc)


def test_bad_form_rejected() -> None:
    doc = copy.deepcopy(VALID)
    doc["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["form"] = "10-K/A"
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(doc)


def test_unknown_taxonomy_rejected() -> None:
    doc = copy.deepcopy(VALID)
    doc["facts"]["ifrs-full"] = doc["facts"].pop("dei")
    doc["counts"]["concepts"] = 2  # unchanged; still 2 concepts
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(doc)


def test_counts_mismatch_raises() -> None:
    doc = _mutate(counts={"taxonomies": 2, "concepts": 2, "facts": 99})
    with pytest.raises(ValidationError, match="counts block disagrees"):
        CompanyFactsFile.model_validate(doc)


def test_start_after_end_raises() -> None:
    doc = copy.deepcopy(VALID)
    doc["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["start"] = "2099-01-01"
    with pytest.raises(ValidationError, match="is after end"):
        CompanyFactsFile.model_validate(doc)


def test_val_too_many_decimal_places_rejected() -> None:
    doc = copy.deepcopy(VALID)
    doc["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["val"] = "1.1234567"
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(doc)


def test_empty_units_rejected() -> None:
    doc = copy.deepcopy(VALID)
    doc["facts"]["us-gaap"]["Assets"]["units"] = {}
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(doc)


def test_naive_retrieved_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        CompanyFactsFile.model_validate(_mutate(retrieved="2026-08-30T02:30:47"))


# --------------------------------------------------------------------------- #
# Real-store smoke test -- validates the actual data/xbrl/*.json if present.
# Bypasses the conftest _isolate_xbrl_store fixture by reading the repo path
# directly.
# --------------------------------------------------------------------------- #
_REAL_STORE = Path(__file__).resolve().parents[1] / "data" / "xbrl"
_REAL_FILES = sorted(_REAL_STORE.glob("*.json")) if _REAL_STORE.is_dir() else []


@pytest.mark.skipif(not _REAL_FILES, reason="no data/xbrl/*.json store present")
@pytest.mark.parametrize("path", _REAL_FILES, ids=lambda p: p.stem)
def test_real_store_file_validates(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    CompanyFactsFile.model_validate(raw)
