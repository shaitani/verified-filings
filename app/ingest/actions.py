"""High-level ingest actions — the named operations the CLI exposes.

Each action composes the corpus loader (`corpus.py`) with `SECClient` into
one operation. Actions don't do any network I/O of their own beyond what
`SECClient` does.

Convention for a CLI-exposed action: signature `async def action_name(identifiers: list[str], *,
client: SECClient | None = None) -> Any`, accepting one or more company
identifiers. The CLI (`app/cli.py`) always constructs its own `SECClient`,
passes it in as `client`, and after the action returns reports that
client's `request_count`/`cache_hit_count` — which, because every fetch in
the action shares that one client, naturally covers the whole batch (e.g.
"2 cache hits, 3 network requests" for a 5-company call), not just one
identifier. Following this convention is what gets a new action that
reporting for free, with no extra wiring per action.

Side effect: `get_submissions()` refreshes `sic_numbers.json` (via
`sic_index.update_sic_index()`) as its last step, so that derived index
always covers exactly the companies fetched so far. Pass
`refresh_sic_index=False` to suppress it. Likewise `get_xbrl_data()` writes
the curated per-company store under `data/xbrl/` (via `xbrl_store.write_store()`)
as it goes; pass `write_store=False` to compute the manifest without writing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.ingest import xbrl_store
from app.ingest.corpus import UnknownCompanyError, find_company
from app.ingest.sec_client import SECClient
from app.ingest.sic_index import update_sic_index


async def get_submissions(
    identifiers: Sequence[str],
    *,
    client: SECClient | None = None,
    refresh_sic_index: bool = True,
) -> dict[str, Any]:
    """The "get-submission" action. Fetches the SEC submissions JSON for
    any number of corpus companies — as few as one, as many as the full
    20-company corpus — in a single call.

    Each entry in `identifiers` may be a ticker, a ticker alias, or a CIK;
    every one is resolved against the closed 20-company corpus in
    `corpus_companies.json` via `find_company()` *before* any request is
    made — if any identifier doesn't match a corpus company, nothing is
    fetched and `UnknownCompanyError` is raised naming every identifier
    that failed to resolve, not just the first, so a typo never wastes a
    partial batch of network requests.

    Returns a dict keyed by each resolved company's canonical ticker
    (not the identifier as given, so an alias like "GOOG" ends up filed
    under "GOOGL"), mapping to that company's submissions JSON. Duplicate
    identifiers collapse to one entry and one fetch (the repeat is simply
    a cache hit).

    Requests are made sequentially, in identifier order. If `client` is
    not provided, a short-lived `SECClient` is opened and closed for just
    this call; when it is provided, every fetch in the batch shares its
    rate limiter, cache, and request/cache-hit counters.

    As a final step (unless `refresh_sic_index=False`), the just-fetched
    companies' rows in `sic_numbers.json` are refreshed from their
    submissions JSON -- see `sic_index.update_sic_index()`.
    """
    companies = []
    unresolved = []
    for identifier in identifiers:
        try:
            companies.append(find_company(identifier))
        except UnknownCompanyError:
            unresolved.append(identifier)

    if unresolved:
        raise UnknownCompanyError(
            f"{unresolved!r} did not match any of the 20 corpus companies "
            "-- no requests were made for this batch"
        )

    async def _fetch_all(active_client: SECClient) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for company in companies:
            results[company["ticker"]] = await active_client.get_json(company["submissions_url"])
        return results

    if client is not None:
        results = await _fetch_all(client)
    else:
        async with SECClient() as owned_client:
            results = await _fetch_all(owned_client)

    if refresh_sic_index:
        update_sic_index(results)
    return results


async def get_submission(identifier: str, *, client: SECClient | None = None) -> dict[str, Any]:
    """Single-company convenience wrapper around `get_submissions()` —
    fetches the SEC submissions JSON for exactly one corpus company.
    """
    results = await get_submissions([identifier], client=client)
    return next(iter(results.values()))


async def get_xbrl_data(
    identifiers: Sequence[str],
    *,
    client: SECClient | None = None,
    write_store: bool = True,
) -> dict[str, Any]:
    """The "get-xbrl" action. Fetches SEC **XBRL data** (the
    `companyfacts` endpoint) for any number of corpus companies — one or the
    full 20 — filters each document down to this project's scope (10-K/10-Q
    facts, most recent 5 fiscal years), and writes the curated local store
    `data/xbrl/<TICKER>.json` for each.

    Identifier resolution matches `get_submissions()` exactly: every entry in
    `identifiers` may be a ticker, ticker alias, or CIK, and all are resolved
    against the closed 20-company corpus *before* any request is made — if any
    identifier doesn't resolve, nothing is fetched and `UnknownCompanyError`
    is raised naming every failure.

    Returns a **manifest** dict keyed by canonical ticker: one small summary
    per company (store path, fiscal years kept, taxonomy/concept/fact counts,
    byte size, and `latest_complete_fy` — the newest fiscal year that company
    has a 10-K for, which the CLI uses to flag when the fixed store window may
    be due to roll forward) — *not* the filtered facts, which are large and go
    to the store files. Pass `write_store=False` to build the manifest (counts
    only, no `path`/`bytes`) without writing anything.

    Requests run sequentially in identifier order. Filtering the SEC's XBRL
    documents (which range into the tens of MB) happens in memory per company;
    the raw response is still written to the URL-keyed disk cache by
    `SECClient`, as for any endpoint.
    """
    companies = []
    unresolved = []
    for identifier in identifiers:
        try:
            companies.append(find_company(identifier))
        except UnknownCompanyError:
            unresolved.append(identifier)

    if unresolved:
        raise UnknownCompanyError(
            f"{unresolved!r} did not match any of the 20 corpus companies "
            "-- no requests were made for this batch"
        )

    async def _fetch_all(active_client: SECClient) -> dict[str, Any]:
        manifest: dict[str, Any] = {}
        for company in companies:
            raw = await active_client.get_json(company["companyfacts_url"])
            if write_store:
                entry = xbrl_store.write_store(company, raw)
            else:
                document = xbrl_store.build_document(company, raw)
                entry = {
                    "ticker": company["ticker"],
                    "fiscal_years": document["scope"]["fiscal_years"],
                    **document["counts"],
                }
            entry["latest_complete_fy"] = xbrl_store.latest_complete_fiscal_year(
                raw.get("facts", {})
            )
            manifest[company["ticker"]] = entry
        return manifest

    if client is not None:
        return await _fetch_all(client)
    async with SECClient() as owned_client:
        return await _fetch_all(owned_client)
