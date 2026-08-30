"""Tests for the auto-maintained ``sic_numbers.json`` index."""

import json

import httpx
import pytest

from app.ingest import sic_index
from app.ingest.actions import get_submissions
from app.ingest.sec_client import SECClient
from app.ingest.sic_index import load_sic_index, update_sic_index

# Minimal submissions payloads carrying just the SIC-relevant fields.
SUBMISSIONS = {
    "AAPL": {"name": "Apple Inc.", "sic": "3571", "sicDescription": "Electronic Computers"},
    "MSFT": {
        "name": "MICROSOFT CORP",
        "sic": "7372",
        "sicDescription": "Services-Prepackaged Software",
    },
    "GOOGL": {
        "name": "Alphabet Inc.",
        "sic": "7370",
        "sicDescription": "Services-Computer Programming, Data Processing, Etc.",
    },
}


def test_update_writes_one_row_per_company(tmp_path):
    path = tmp_path / "sic_numbers.json"

    rows = update_sic_index({"AAPL": SUBMISSIONS["AAPL"]}, path=path)

    assert rows == [
        {
            "cik": "CIK0000320193",
            "ticker": "AAPL",
            "company_name": "Apple Inc.",
            "sic": "3571",
            "sic_description": "Electronic Computers",
        }
    ]
    assert json.loads(path.read_text()) == rows


def test_update_is_upsert_and_stays_in_corpus_order(tmp_path):
    path = tmp_path / "sic_numbers.json"

    update_sic_index({"MSFT": SUBMISSIONS["MSFT"]}, path=path)
    update_sic_index({"AAPL": SUBMISSIONS["AAPL"], "GOOGL": SUBMISSIONS["GOOGL"]}, path=path)

    # Corpus order is GOOGL, AAPL, ..., MSFT -- not fetch/insertion order.
    assert [row["ticker"] for row in load_sic_index(path)] == ["GOOGL", "AAPL", "MSFT"]


def test_update_refreshes_an_existing_row(tmp_path):
    path = tmp_path / "sic_numbers.json"

    update_sic_index(
        {"AAPL": {"name": "STALE", "sic": "0000", "sicDescription": "stale"}}, path=path
    )
    update_sic_index({"AAPL": SUBMISSIONS["AAPL"]}, path=path)

    rows = load_sic_index(path)
    assert len(rows) == 1
    assert rows[0]["company_name"] == "Apple Inc."
    assert rows[0]["sic"] == "3571"


def test_update_skips_a_company_missing_sic_fields(tmp_path):
    path = tmp_path / "sic_numbers.json"

    rows = update_sic_index({"AAPL": {"cik": "0000320193"}}, path=path)

    assert rows == []
    assert not path.exists()


@pytest.mark.asyncio
async def test_get_submissions_refreshes_the_index(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "0000320193" in str(request.url):
            return httpx.Response(200, json=SUBMISSIONS["AAPL"])
        return httpx.Response(200, json=SUBMISSIONS["MSFT"])

    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler))
    async with client:
        await get_submissions(["AAPL", "MSFT"], client=client)

    # conftest has redirected sic_index.SIC_INDEX_FILE to a temp path.
    assert [row["ticker"] for row in load_sic_index()] == ["AAPL", "MSFT"]
    assert sic_index.SIC_INDEX_FILE.exists()


@pytest.mark.asyncio
async def test_refresh_sic_index_false_leaves_the_file_untouched(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUBMISSIONS["AAPL"])

    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler))
    async with client:
        await get_submissions(["AAPL"], client=client, refresh_sic_index=False)

    assert not sic_index.SIC_INDEX_FILE.exists()
