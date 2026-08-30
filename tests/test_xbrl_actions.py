"""End-to-end tests for the ``get-xbrl`` action against a mocked transport --
no real requests to data.sec.gov. The curated store is redirected to a temp
path by the autouse fixture in ``conftest.py``."""

import httpx
import pytest

from app.ingest.actions import get_xbrl_data
from app.ingest.corpus import UnknownCompanyError
from app.ingest.sec_client import SECClient
from app.ingest.xbrl_store import load_store, store_path


@pytest.fixture(autouse=True)
def _isolated_rate_limiter():
    SECClient.reset_shared_rate_limiter()
    yield
    SECClient.reset_shared_rate_limiter()


def _facts_payload(cik: int, entity: str) -> dict:
    """Two Revenues rows in-scope-shaped, plus one stale row and one 8-K row
    that the scope filter must drop. Latest fy is 2024."""
    return {
        "cik": cik,
        "entityName": entity,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "description": "Amount of revenue.",
                    "units": {
                        "USD": [
                            {
                                "start": "2017-01-01",
                                "end": "2017-12-31",
                                "val": 1,
                                "accn": "x-17",
                                "fy": 2017,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2018-02-01",
                            },
                            {
                                "start": "2024-01-01",
                                "end": "2024-03-31",
                                "val": 3,
                                "accn": "x-8k",
                                "fy": 2024,
                                "fp": "Q1",
                                "form": "8-K",
                                "filed": "2024-04-01",
                            },
                            {
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "val": 2,
                                "accn": "x-24",
                                "fy": 2024,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2025-02-01",
                            },
                        ]
                    },
                }
            }
        },
    }


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    assert "/api/xbrl/companyfacts/CIK" in url
    if "0000320193" in url:
        return httpx.Response(200, json=_facts_payload(320193, "Apple Inc."))
    if "0000789019" in url:
        return httpx.Response(200, json=_facts_payload(789019, "MICROSOFT CORP"))
    return httpx.Response(200, json=_facts_payload(1, "Other Co"))


@pytest.mark.asyncio
async def test_hits_the_xbrl_data_url_and_writes_the_curated_store(tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _handler(request)

    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler))
    async with client:
        manifest = await get_xbrl_data(["AAPL"], client=client)

    assert seen == ["https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"]

    entry = manifest["AAPL"]
    assert entry["fiscal_years"] == [2024]  # 2017 out of window, 8-K dropped
    assert entry["facts"] == 1
    assert entry["path"] == str(store_path("AAPL"))

    doc = load_store("AAPL")
    assert doc["entity_name"] == "Apple Inc."
    rows = doc["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    assert [r["accn"] for r in rows] == ["x-24"]


@pytest.mark.asyncio
async def test_batch_manifest_is_keyed_by_canonical_ticker_in_order(tmp_path):
    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(_handler))
    async with client:
        manifest = await get_xbrl_data(["AAPL", "MSFT", "GOOG"], client=client)

    # "GOOG" is an alias -> filed under the canonical "GOOGL".
    assert list(manifest) == ["AAPL", "MSFT", "GOOGL"]
    assert load_store("GOOGL") is not None
    assert client.request_count == 3


@pytest.mark.asyncio
async def test_rejects_a_non_corpus_identifier_and_fetches_nothing(tmp_path):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _handler(request)

    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(UnknownCompanyError):
            await get_xbrl_data(["AAPL", "NOTACOMPANY"], client=client)

    assert calls == []
    assert client.request_count == 0
    assert load_store("AAPL") is None


@pytest.mark.asyncio
async def test_write_store_false_computes_the_manifest_without_writing(tmp_path):
    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(_handler))
    async with client:
        manifest = await get_xbrl_data(["AAPL"], client=client, write_store=False)

    assert load_store("AAPL") is None
    assert "path" not in manifest["AAPL"]
    assert manifest["AAPL"]["facts"] == 1
    assert manifest["AAPL"]["fiscal_years"] == [2024]


@pytest.mark.asyncio
async def test_manifest_reports_latest_complete_fiscal_year(tmp_path):
    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(_handler))
    async with client:
        manifest = await get_xbrl_data(["AAPL"], client=client)

    # _facts_payload's newest 10-K row is fy 2024.
    assert manifest["AAPL"]["latest_complete_fy"] == 2024


@pytest.mark.asyncio
async def test_repeat_identifier_is_served_from_cache(tmp_path):
    client = SECClient(cache_dir=tmp_path / "cache", transport=httpx.MockTransport(_handler))
    async with client:
        await get_xbrl_data(["AAPL"], client=client)
        await get_xbrl_data(["AAPL"], client=client)

    assert client.request_count == 1
    assert client.cache_hit_count == 1
