"""Tests the get-submission action(s) end-to-end against a mocked
transport -- no real requests to data.sec.gov."""

from pathlib import Path

import httpx
import pytest

from app.ingest.actions import get_submission, get_submissions
from app.ingest.corpus import UnknownCompanyError
from app.ingest.sec_client import SECClient


@pytest.fixture(autouse=True)
def _isolated_rate_limiter():
    SECClient.reset_shared_rate_limiter()
    yield
    SECClient.reset_shared_rate_limiter()


@pytest.mark.asyncio
async def test_get_submission_uses_the_companys_submissions_url(tmp_path: Path):
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"cik": "0000320193", "name": "Apple Inc."})

    client = SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    async with client:
        result = await get_submission("AAPL", client=client)

    assert result == {"cik": "0000320193", "name": "Apple Inc."}
    assert seen_urls == ["https://data.sec.gov/submissions/CIK0000320193.json"]
    assert client.request_count == 1
    assert client.cache_hit_count == 0


@pytest.mark.asyncio
async def test_get_submission_rejects_non_corpus_identifier():
    with pytest.raises(UnknownCompanyError):
        await get_submission("NOTACOMPANY")


@pytest.mark.asyncio
async def test_get_submissions_batch_of_several_companies(tmp_path: Path):
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"url": str(request.url)})

    client = SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    async with client:
        result = await get_submissions(["AAPL", "MSFT", "GOOGL"], client=client)

    # Keyed by canonical ticker, one entry per company, in the order requested.
    assert list(result.keys()) == ["AAPL", "MSFT", "GOOGL"]
    assert result["AAPL"] == {"url": "https://data.sec.gov/submissions/CIK0000320193.json"}
    assert len(seen_urls) == 3
    assert client.request_count == 3
    assert client.cache_hit_count == 0


@pytest.mark.asyncio
async def test_get_submissions_reports_mixed_cache_hits_and_requests(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": str(request.url)})

    client = SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    async with client:
        # Pre-warm the cache for 2 of the 5 companies we'll ask for.
        await get_submissions(["AAPL", "MSFT"], client=client)
        assert client.request_count == 2
        assert client.cache_hit_count == 0

        # Now ask for 5, including the 2 already cached -- exactly the
        # "2 cache hits, 3 requests" scenario.
        result = await get_submissions(["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"], client=client)

    assert set(result.keys()) == {"AAPL", "MSFT", "GOOGL", "TSLA", "AMZN"}
    assert client.request_count == 2 + 3  # 2 from the pre-warm, 3 new
    assert client.cache_hit_count == 2  # AAPL + MSFT served from cache this time


@pytest.mark.asyncio
async def test_get_submissions_single_identifier_still_works(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cik": "0000320193"})

    client = SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    async with client:
        result = await get_submissions(["AAPL"], client=client)

    assert result == {"AAPL": {"cik": "0000320193"}}


@pytest.mark.asyncio
async def test_get_submissions_duplicate_identifier_collapses_to_one_fetch(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cik": "0000320193"})

    client = SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    async with client:
        result = await get_submissions(["AAPL", "AAPL"], client=client)

    assert result == {"AAPL": {"cik": "0000320193"}}
    assert client.request_count == 1
    assert client.cache_hit_count == 1  # the 2nd AAPL fetch hits the cache


@pytest.mark.asyncio
async def test_get_submissions_rejects_if_any_identifier_is_unknown(tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    client = SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(UnknownCompanyError):
            # AAPL is valid, but NOTACOMPANY isn't -- nothing should be
            # fetched at all, not even for AAPL.
            await get_submissions(["AAPL", "NOTACOMPANY"], client=client)

    assert calls == []
    assert client.request_count == 0
