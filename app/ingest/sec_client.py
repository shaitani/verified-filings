"""Async HTTP client for data.sec.gov.

Every request made through `SECClient` honors the project's three hard
requirements on SEC network access:

1. Never exceed a global rate limit (default 10 req/s), enforced across
   the whole process regardless of how many clients or concurrent
   requests exist.
2. Always send a fixed, identifying User-Agent header.
3. Cache every response to disk, keyed by URL — a URL that's already been
   fetched is served from disk on every later request instead of hitting
   the network again.

Nothing in this module makes a network call on import or on
`SECClient()` construction. A request is only issued when
`get_json()`/`get_bytes()` is actually awaited, and only then if the URL
isn't already cached.

Every client also tracks how many real network requests it made and how
many calls were served from cache instead (`request_count` /
`cache_hit_count`), so any caller — the CLI in particular — can report
exactly how much network traffic a run generated.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from app.ingest.disk_cache import CachedResponse, DiskCache
from app.ingest.rate_limiter import RateLimiter

USER_AGENT = "Amo Spamo amospamo@proton.me"
MAX_REQUESTS_PER_SECOND = 10
DEFAULT_CACHE_DIR = Path(".cache") / "sec_edgar"
DEFAULT_BASE_URL = "https://data.sec.gov"


class SECClientError(RuntimeError):
    """Raised for SECClient usage errors (e.g. not used as a context manager)."""


class SECClient:
    """Async client for data.sec.gov endpoints.

    Usage:
        async with SECClient() as client:
            data = await client.get_json(url)

    All instances share one `RateLimiter` (see `_shared_rate_limiter`), so
    the 10 req/s ceiling is global to the process, not per-instance.
    """

    _shared_rate_limiter: RateLimiter | None = None

    def __init__(
        self,
        *,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        max_requests_per_second: int = MAX_REQUESTS_PER_SECOND,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cache = DiskCache(Path(cache_dir))
        self._rate_limiter = self._get_shared_rate_limiter(max_requests_per_second)
        self._timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._request_count = 0
        self._cache_hit_count = 0

    @property
    def request_count(self) -> int:
        """Number of real HTTP requests this client has sent over the
        network so far. Cache hits are never counted here."""
        return self._request_count

    @property
    def cache_hit_count(self) -> int:
        """Number of `get_bytes()`/`get_json()` calls this client has
        served from the disk cache so far, without touching the network."""
        return self._cache_hit_count

    @classmethod
    def _get_shared_rate_limiter(cls, max_requests_per_second: int) -> RateLimiter:
        if cls._shared_rate_limiter is None:
            cls._shared_rate_limiter = RateLimiter(max_requests_per_second)
        return cls._shared_rate_limiter

    @classmethod
    def reset_shared_rate_limiter(cls) -> None:
        """Drop the process-wide shared rate limiter so the next
        `SECClient()` constructed rebuilds it (e.g. with a different
        `max_requests_per_second`). Intended for tests; production code
        should not need this."""
        cls._shared_rate_limiter = None

    async def __aenter__(self) -> SECClient:
        self._http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=self._timeout,
            transport=self._transport,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get_json(self, url: str, *, force_refresh: bool = False) -> Any:
        """Fetch `url` and parse the response body as JSON, using the disk
        cache unless `force_refresh` is set."""
        content = await self.get_bytes(url, force_refresh=force_refresh)
        return json.loads(content)

    async def get_bytes(self, url: str, *, force_refresh: bool = False) -> bytes:
        """Fetch `url` and return the raw response body.

        Cache is checked first (unless `force_refresh=True`); a cache hit
        returns immediately without touching the rate limiter or the
        network. A cache miss acquires a rate-limiter slot, issues the
        request with the fixed User-Agent, then writes the result to the
        cache before returning it.
        """
        if not force_refresh:
            cached = self._cache.get(url)
            if cached is not None:
                self._cache_hit_count += 1
                return cached.content

        if self._http is None:
            raise SECClientError(
                "SECClient must be used as an async context manager: "
                "`async with SECClient() as client: ...`"
            )

        await self._rate_limiter.acquire()
        response = await self._http.get(url)
        response.raise_for_status()
        self._request_count += 1

        self._cache.set(
            url,
            CachedResponse(
                status_code=response.status_code,
                headers=dict(response.headers),
                content=response.content,
            ),
        )
        return response.content


def submissions_url(cik_padded: str) -> str:
    """Build the SEC submissions URL for a zero-padded 10-digit CIK string.

    e.g. "0001652044" -> "https://data.sec.gov/submissions/CIK0001652044.json"
    """
    return f"{DEFAULT_BASE_URL}/submissions/CIK{cik_padded}.json"


def xbrl_data_url(cik_padded: str) -> str:
    """Build the SEC XBRL-data URL for a zero-padded 10-digit CIK string.

    e.g. "0000320193" ->
    "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"

    SEC's own path segment for this endpoint is "companyfacts"; this project
    refers to the data it returns only as "XBRL data" (see sec-retriever.md).
    """
    return f"{DEFAULT_BASE_URL}/api/xbrl/companyfacts/CIK{cik_padded}.json"
