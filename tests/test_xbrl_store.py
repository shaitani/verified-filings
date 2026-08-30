"""Tests for the curated XBRL-data store and its scope filter
(``app/ingest/xbrl_store.py``). No network -- ``RAW`` stands in for a SEC
companyfacts payload."""

import json

from app.ingest.corpus import find_company
from app.ingest.xbrl_store import (
    FISCAL_YEAR_MAX,
    FISCAL_YEARS_KEPT,
    build_document,
    filter_facts,
    latest_complete_fiscal_year,
    load_store,
    store_path,
    write_store,
)

# A miniature companyfacts payload. The fixed default window is
# FY(FISCAL_YEAR_MAX - 4)..FY(FISCAL_YEAR_MAX) == FY2021..FY2025; RAW's in-scope
# rows sit at fy 2024 and 2025.
RAW = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "label": "Entity Common Stock, Shares Outstanding",
                "description": "Number of shares outstanding.",
                "units": {
                    "shares": [
                        # fy 2020 -- before the window, dropped
                        {
                            "end": "2020-10-16",
                            "val": 17001802000,
                            "accn": "a-20",
                            "fy": 2020,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2020-10-30",
                        },
                        {
                            "end": "2025-10-17",
                            "val": 15000000000,
                            "accn": "a-25",
                            "fy": 2025,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-11-01",
                        },
                    ]
                },
            }
        },
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "description": "Amount of revenue recognized from goods sold.",
                "units": {
                    "USD": [
                        # fy 2019 -- before the window
                        {
                            "start": "2018-10-01",
                            "end": "2019-09-28",
                            "val": 100,
                            "accn": "a-19",
                            "fy": 2019,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2019-10-30",
                        },
                        # recent but wrong form -- dropped
                        {
                            "start": "2024-01-01",
                            "end": "2024-03-31",
                            "val": 200,
                            "accn": "a-8k",
                            "fy": 2025,
                            "fp": "Q2",
                            "form": "8-K",
                            "filed": "2025-05-01",
                        },
                        {
                            "start": "2023-10-01",
                            "end": "2024-09-28",
                            "val": 391035,
                            "accn": "a-24",
                            "fy": 2024,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-11-01",
                        },
                        {
                            "start": "2024-10-01",
                            "end": "2025-09-27",
                            "val": 400000,
                            "accn": "a-25",
                            "fy": 2025,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2025-11-01",
                        },
                        {
                            "start": "2025-06-29",
                            "end": "2025-09-27",
                            "val": 90000,
                            "accn": "a-25q3",
                            "fy": 2025,
                            "fp": "Q3",
                            "form": "10-Q",
                            "filed": "2025-08-01",
                        },
                    ]
                },
            },
            "OnlyOld": {
                "label": "Only old",
                "units": {
                    "USD": [
                        {
                            "start": "2018-10-01",
                            "end": "2019-09-28",
                            "val": 1,
                            "accn": "a-19",
                            "fy": 2019,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2019-10-30",
                        }
                    ]
                },
            },
        },
    },
}


def test_filter_keeps_only_10k_10q_within_the_5_year_window():
    filtered, years = filter_facts(RAW["facts"])

    assert years == [2024, 2025]
    assert set(filtered) == {"us-gaap", "dei"}
    # "OnlyOld" had nothing in the window -> pruned entirely.
    assert set(filtered["us-gaap"]) == {"Revenues"}

    rows = filtered["us-gaap"]["Revenues"]["units"]["USD"]
    assert [r["fy"] for r in rows] == [2024, 2025, 2025]
    assert [r["accn"] for r in rows] == ["a-24", "a-25", "a-25q3"]
    assert all(r["form"] in ("10-K", "10-Q") for r in rows)

    shares = filtered["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
    assert [r["fy"] for r in shares] == [2025]


def test_filter_passes_fact_rows_through_untouched():
    filtered, _ = filter_facts(RAW["facts"])
    assert filtered["us-gaap"]["Revenues"]["units"]["USD"][0] == {
        "start": "2023-10-01",
        "end": "2024-09-28",
        "val": 391035,
        "accn": "a-24",
        "fy": 2024,
        "fp": "FY",
        "form": "10-K",
        "filed": "2024-11-01",
    }


def test_filter_keeps_concept_label_and_description():
    filtered, _ = filter_facts(RAW["facts"])
    concept = filtered["us-gaap"]["Revenues"]
    assert concept["label"] == "Revenues"
    assert concept["description"].startswith("Amount of revenue")
    assert set(concept) == {"label", "description", "units"}


def test_filter_returns_empty_when_nothing_is_in_scope():
    raw = {
        "us-gaap": {
            "X": {
                "units": {
                    "USD": [
                        {
                            "end": "2024-01-01",
                            "val": 1,
                            "fy": 2024,
                            "fp": "FY",
                            "form": "8-K",
                            "filed": "2024-02-01",
                        }
                    ]
                }
            }
        }
    }
    assert filter_facts(raw) == ({}, [])


def _annual_rows(*years: int) -> dict:
    return {
        "us-gaap": {
            "X": {
                "units": {
                    "USD": [
                        {
                            "end": f"{y}-12-31",
                            "val": y,
                            "accn": f"a-{y}",
                            "fy": y,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": f"{y + 1}-02-01",
                        }
                        for y in years
                    ]
                }
            }
        }
    }


def test_default_window_is_fixed_and_ignores_newer_fiscal_years():
    # A company that has already filed well past FISCAL_YEAR_MAX is still
    # clamped to the shared FY2021..FY2025 grid.
    raw = _annual_rows(*range(2019, 2031))
    _, years = filter_facts(raw)
    assert years == [2021, 2022, 2023, 2024, 2025]
    assert FISCAL_YEAR_MAX == 2025 and FISCAL_YEARS_KEPT == 5


def test_filter_window_can_be_overridden():
    _, years = filter_facts(_annual_rows(*range(2008, 2015)), fiscal_year_max=2014)
    assert years == [2010, 2011, 2012, 2013, 2014]


def test_filter_window_size_is_configurable():
    _, years = filter_facts(RAW["facts"], fiscal_years_kept=1)
    assert years == [2025]


def test_latest_complete_fiscal_year_uses_10k_only():
    raw = {
        "us-gaap": {
            "X": {
                "units": {
                    "USD": [
                        {"fy": 2025, "fp": "FY", "form": "10-K"},
                        {"fy": 2026, "fp": "Q1", "form": "10-Q"},  # newer, but a 10-Q
                    ]
                }
            }
        }
    }
    assert latest_complete_fiscal_year(raw) == 2025
    assert latest_complete_fiscal_year({}) is None


def test_build_document_shape():
    company = find_company("AAPL")
    doc = build_document(company, RAW, retrieved="2026-08-29T00:00:00Z")

    assert doc["cik"] == 320193
    assert doc["ticker"] == "AAPL"
    assert doc["entity_name"] == "Apple Inc."
    assert doc["source_url"] == company["companyfacts_url"]
    assert doc["retrieved"] == "2026-08-29T00:00:00Z"
    assert doc["scope"] == {"forms": ["10-K", "10-Q"], "fiscal_years": [2024, 2025]}
    assert doc["counts"] == {"taxonomies": 2, "concepts": 2, "facts": 4}
    assert set(doc["facts"]) == {"us-gaap", "dei"}


def test_write_store_writes_pretty_json_and_returns_a_manifest_entry(tmp_path):
    company = find_company("AAPL")
    entry = write_store(company, RAW, directory=tmp_path, retrieved="2026-08-29T00:00:00Z")

    target = store_path("AAPL", directory=tmp_path)
    assert target == tmp_path / "AAPL.json"
    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "\n  " in text  # indent=2, i.e. pretty-printed
    assert json.loads(text)["ticker"] == "AAPL"

    assert entry == {
        "ticker": "AAPL",
        "path": str(target),
        "fiscal_years": [2024, 2025],
        "taxonomies": 2,
        "concepts": 2,
        "facts": 4,
        "bytes": len(text.encode("utf-8")),
    }


def test_load_store_roundtrips_and_returns_none_when_absent(tmp_path):
    company = find_company("AAPL")
    assert load_store("AAPL", directory=tmp_path) is None

    write_store(company, RAW, directory=tmp_path, retrieved="2026-08-29T00:00:00Z")
    loaded = load_store("AAPL", directory=tmp_path)

    assert loaded["ticker"] == "AAPL"
    assert loaded["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["accn"] == "a-24"
