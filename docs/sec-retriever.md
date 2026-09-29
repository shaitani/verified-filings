# SEC retrieval — the corpus, the client, the curated files

Reference for block [H], `app/ingest/`: what is fetched from the SEC, under
which rules, and what is written to disk. The CLI is `sec-retriever`
(`app/cli.py`).

**Terminology.** The endpoint
`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` is called
**"XBRL data"** everywhere — never "companyfacts", though that is SEC's own
path. `https://data.sec.gov/submissions/CIK##########.json` is
"submissions".

---

## The corpus

**Exactly the 20 companies in `corpus_companies.json`** — a master list, never
added to. Each entry holds the CIK (`cik`, `cik_padded`), tickers, the
company's title, and its two SEC URLs. All ingest code resolves identifiers
(ticker, ticker alias or CIK, case-insensitive) through `app/ingest/corpus.py`,
so nothing outside the corpus can be requested.

Three are substitutes, with the reason recorded in the file:

- **QCOM for Samsung** — Samsung is not an SEC registrant.
- **CVX for Exxon Mobil** — XOM's current CIK is a newer holding entity; its
  history sits under another CIK.
- **JPM for HSBC** — HSBC files 20-F under IFRS, not 10-K/10-Q under us-gaap.

Two are there to stress the semantic layer: **JPM and BAC** (banks) have no
revenue / cost-of-goods / gross-margin structure, and **CVX** has large
impairment and asset-retirement lines.

Scope: **10-K and 10-Q only**, **FY2021–FY2025** (below).

## Rules for every SEC request

1. **At most 10 requests per second**, globally across the whole process — not
   per endpoint or per company.
2. **User-Agent exactly** `Amo Spamo amospamo@proton.me`.
3. **Disk cache keyed by URL.** Every response is cached; the cache is checked
   before any request.
4. **httpx**, not requests.

## The client

- **`RateLimiter`** — a sliding one-second window (a fixed window can burst
  2× at its boundary), shared by every `SECClient` in the process. Global
  within one process only; separate CLI runs do not share it.
  `SECClient.reset_shared_rate_limiter()` exists for tests.
- **`DiskCache`** — `.cache/sec_edgar/` (gitignored), one
  `<sha256(url)>.body` (raw bytes) and `.meta.json` (status, headers) per URL.
  **Entries never expire**: a fetched URL is served from disk forever, so
  re-running a command re-derives its output with no network traffic.
- **`SECClient`** — an async context manager around `httpx.AsyncClient`
  carrying the User-Agent. A cache miss takes a rate-limiter slot and hits the
  network. It counts `request_count` and `cache_hit_count` itself, so every
  action gets the CLI's summary line for free:
  `[get-submission] 3 network request(s) made, 2 served from cache`. JSON goes
  to stdout, the summary to stderr.

## Actions

Both resolve **every** identifier against the corpus first; if any fails,
`UnknownCompanyError` names them all and **nothing is fetched**. Requests run
in order, sharing one client, so a duplicate becomes a cache hit. Results are
keyed by canonical ticker ("GOOG" files under "GOOGL").

```
uv run sec-retriever get-submission AAPL MSFT
uv run sec-retriever get-xbrl AAPL
```

**`get-submission`** returns each company's submissions JSON, and refreshes
two derived files from it as its last step (`refresh_sic_index=False` skips
both):

- `sic_numbers.json` — each company's SIC code and description, used by the
  load step for company-group questions.
- `company_aliases.json` — names people write for each company (former names,
  short forms, share classes), used by the mapper's company resolver.

Both are derived caches: deleting one is safe, and the next run rebuilds the
rows for the companies it fetches.

**`get-xbrl`** fetches each company's XBRL data, filters it to scope, and
writes `data/xbrl/<TICKER>.json`. It returns a manifest (path, fiscal years,
counts, size, `latest_complete_fy`), not the facts. XBRL data has no
pagination: one complete document per company.

## The curated files: `data/xbrl/<TICKER>.json`

The raw document carries every fact ever tagged (all forms, ~15 years, 500+
concepts). `filter_facts()` keeps:

- **Forms:** exactly `10-K` and `10-Q`. Amendments (`10-K/A`) and every other
  form are dropped.
- **Fiscal years:** one **fixed window for all 20** — `FISCAL_YEAR_MAX` (2025)
  back `FISCAL_YEARS_KEPT` (5): FY2021–FY2025. Not derived per company:
  cross-company questions need every store on the same grid.
  `FISCAL_YEAR_MAX` is the newest year every company has an annual 10-K for;
  `get-xbrl` prints a note naming any company already past it (rolling
  forward: [FUTURE.md](FUTURE.md#data-and-ingest)). A row's `fy` is the fiscal
  year of the *filing* it appeared in, so a kept 10-K still carries its
  prior-year comparative columns.
- Empty concepts, units and taxonomies are pruned. `srt` and `dei` are kept.
- Fact rows pass through **untouched**, keeping full filing provenance.

The file: pretty-printed, top-level `cik`, `ticker`, `entity_name`,
`source_url`, `retrieved` (UTC), `scope` (`forms`, `fiscal_years`), `counts`
(`taxonomies`, `concepts`, `facts`) and `facts` (the filtered tree). `data/` is
gitignored and regenerable from the cache. The load step validates each file
against `app/schemas/xbrl.py` ([app/schemas/DESIGN.md](../app/schemas/DESIGN.md)).

## `sic_numbers.json` format

A top-level JSON array, one object per corpus company in
`corpus_companies.json` order, each with exactly these string keys:

```json
[
  {
    "cik": "CIK<10-digit zero-padded CIK>",
    "ticker": "<primary ticker>",
    "company_name": "<name as SEC records it>",
    "sic": "<4-digit SIC code>",
    "sic_description": "<SEC's SIC description, verbatim>"
  }
]
```

`cik` carries a `CIK` prefix, unlike `corpus_companies.json`'s `cik_padded`.
`sic` is a string. `company_name` keeps SEC's own inconsistent casing.
