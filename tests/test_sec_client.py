"""SECClient tests use httpx.MockTransport throughout — no real requests
are made to data.sec.gov here."""

from pathlib import Path

import httpx
import pytest

from app.ingest.sec_client import USER_AGENT, SECClient, submissions_url, xbrl_data_url


@pytest.fixture(autouse=True)
def _isolated_rate_limiter():
    # Each test gets its own shared rate limiter instance so timing/limits
    # set by one test don't leak into another.
    SECClient.reset_shared_rate_limiter()
    yield
    SECClient.reset_shared_rate_limiter()


def test_submissions_url_builder():
    assert submissions_url("0001652044") == "https://data.sec.gov/submissions/CIK0001652044.json"


def test_xbrl_data_url_builder():
    assert (
        xbrl_data_url("0000320193")
        == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    )


@pytest.mark.asyncio
async def test_sends_fixed_user_agent_and_caches(tmp_path: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"hello": "world"})

    transport = httpx.MockTransport(handler)
    url = "https://data.sec.gov/submissions/CIK0000320193.json"

    async with SECClient(cache_dir=tmp_path, transport=transport) as client:
        first = await client.get_json(url)
        second = await client.get_json(url)  # should be served from cache

    assert first == {"hello": "world"}
    assert second == {"hello": "world"}
    # The mock transport should only have been hit once -- the second
    # get_json() call must be a cache hit, not a second network call.
    assert len(calls) == 1
    assert calls[0].headers["user-agent"] == USER_AGENT
    # request_count/cache_hit_count must agree with the above.
    assert client.request_count == 1
    assert client.cache_hit_count == 1


@pytest.mark.asyncio
async def test_cache_persists_across_client_instances(tmp_path: Path):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"n": call_count})

    url = "https://data.sec.gov/submissions/CIK0000789019.json"

    async with SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler)) as client:
        first = await client.get_json(url)
    assert client.request_count == 1
    assert client.cache_hit_count == 0

    # A brand-new SECClient instance pointed at the same cache_dir should
    # still find the cached entry and never call the transport.
    async with SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler)) as client:
        second = await client.get_json(url)
    assert client.request_count == 0
    assert client.cache_hit_count == 1

    assert first == second == {"n": 1}
    assert call_count == 1


@pytest.mark.asyncio
async def test_request_and_cache_hit_counters(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with SECClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler)) as client:
        assert client.request_count == 0
        assert client.cache_hit_count == 0

        await client.get_json("https://data.sec.gov/submissions/CIK0000320193.json")
        assert client.request_count == 1
        assert client.cache_hit_count == 0

        await client.get_json("https://data.sec.gov/submissions/CIK0000789019.json")
        assert client.request_count == 2
        assert client.cache_hit_count == 0

        # Re-fetching an already-cached URL bumps cache_hit_count, not
        # request_count.
        await client.get_json("https://data.sec.gov/submissions/CIK0000320193.json")
        assert client.request_count == 2
        assert client.cache_hit_count == 1
