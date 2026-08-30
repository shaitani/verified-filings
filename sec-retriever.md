# SEC Retriever — Project Profile & Spec

Status: **Implementation started.** Section 7 tracks what's actually been
built. Everything else in this document remains the living spec — update it
as new information/limitations come in.

**Terminology:** the SEC endpoint
`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (stored per
company as `companyfacts_url` in `corpus_companies.json`) is referred to
throughout this project as **"XBRL data"** — never "companyfacts" or "company
facts", even though that is SEC's own path segment. The separate endpoint
`https://data.sec.gov/submissions/CIK##########.json` is "submissions" /
"submissions JSON". New code, CLI subcommands, and docs should follow this.

## 1. Goal (first milestone)

A **CLI tool** that:

- Takes a natural-language question as input.
- Answers it using SEC filing data.
- Prints every number in the answer **color-coded by verification status**
  (i.e., whether the figure was directly sourced from a filing/XBRL fact vs.
  derived/computed vs. unverifiable).
- Handles **cross-sector questions intelligently** — i.e., is aware that
  different sectors (e.g., banks vs. energy vs. tech) report different line
  items and structures, and adapts accordingly.
- **No web anything** — this is a local CLI, not a web app/UI.

## 2. Tech stack

- **Language:** Python, targeting **3.13+** (`requires-python = ">=3.13"`
  in `pyproject.toml`; `ruff` `target-version = "py313"`). Developed and
  run against a real CPython **3.13.15** interpreter (installed at
  `C:\Python313`; `.python-version` pins `3.13`, so `uv` selects it
  automatically). History: the project originally targeted 3.9 to match
  the user's then-installed 3.9.2 and was verified against it; it moved
  to 3.13 on 2026-08-29 once 3.9 was end-of-life. The one code change the
  bump required was `typing.Callable` -> `collections.abc.Callable` in
  `cli.py`; the `from __future__ import annotations` headers are now
  unnecessary but were left in place (harmless).
- **Dependency management:** `uv`
- **Lint/format:** `ruff`
- **Testing:** `pytest`
- **Database:** SQLAlchemy 2.0 + `pgvector-python` (Postgres w/ pgvector)
- **Migrations:** Alembic
- **HTTP client:** `httpx` (explicitly **not** `requests`) — chosen for native
  async support (needed to respect the global rate limit while still being
  able to issue many SEC requests efficiently), HTTP/2 support, and modern
  timeout/connection-pooling controls.

## 3. Project structure

Per project-level instructions, the repo should match this layout where
possible:

```
verified-filings/
  app/
    schemas/       # Pydantic models — the single source of truth
    ingest/        # SEC client, XBRL, chunking
    semantic/      # intent map, sector detection, metric resolution
    retrieval/     # hybrid search
    verify/        # the claim verifier
    api/           # FastAPI routes (Sprint 2)
    db/            # SQLAlchemy models + Alembic migrations
  web/             # Angular (Sprint 3)
  evals/           # eval sets + result history (commit these)
  docker-compose.yml
  pyproject.toml
```

The CLI milestone described in Section 1 corresponds primarily to
`app/ingest/`, `app/semantic/`, `app/verify/`, and `app/schemas/`.

## 4. SEC client — hard limitations

These apply to **all** network access to the SEC site:

1. **Global rate limit:** never exceed **10 requests per second**, enforced
   globally across the whole client (not per-endpoint or per-company).
2. **User-Agent header:** must be exactly
   `"Amo Spamo amospamo@proton.me"` on every request.
3. **Disk cache keyed by URL:** every response from any URL on the SEC site
   must be cached to disk, keyed by the URL. Before making a request, the
   client checks the cache first; if that URL has already been fetched, the
   cached response is used instead of hitting the site again.
4. **HTTP library:** `httpx`, not `requests` (see Section 2).

## 5. Data scope — closed corpus

- **Company list:** exactly the 20 companies defined in
  `corpus_companies.json` (top-level of `verified-filings/`). This is a
  **master list — never to be added to or expanded**.
  - Each entry has a resolved CIK (`cik`, `cik_padded`), ticker(s), the
    company title, and pre-built SEC URLs:
    - `submissions_url`: `https://data.sec.gov/submissions/CIK{cik_padded}.json`
    - `companyfacts_url`: `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json`
  - Three companies are substitutions for entities that aren't viable SEC
    data sources, with reasons recorded in the file:
    - Samsung → **QCOM** (Samsung is Korea Exchange-listed, not an SEC
      registrant — no CIK/EDGAR/XBRL).
    - Exxon Mobil → **CVX** (XOM's current CIK is a newer holding-company
      entity; historical filings live under a different, absent CIK; CVX is
      a stable supermajor with a clean 10-K history).
    - HSBC Holdings → **JPM** (HSBC is a foreign private issuer filing 20-F
      under IFRS tagging, not 10-K/10-Q under us-gaap).
  - The corpus file also flags sector-specific reporting quirks intended as
    stress tests for the semantic layer's sector/concept mapping:
    - **JPM, BAC (banks):** no revenue/COGS/gross-margin structure — expect
      net interest income and noninterest revenue instead. Requires
      financial-sector entries in the concept map.
    - **CVX (energy):** large impairment and asset-retirement-obligation
      line items; revenue split across sales vs. equity-affiliate income.
      Stress test for concept-map fallback ordering.
- **Fiscal years:** 5 fiscal years only.
- **Filing types:** **10-K and 10-Q only** (no 8-K, DEF 14A, S-1, etc.).

### 5.1 Sector classification — `sic_numbers.json`

A second top-level file, `sic_numbers.json`, supplements the corpus with
each company's **SIC code** (Standard Industrial Classification) and
description. This is the intended raw input for the semantic layer's
**sector detection** (`app/semantic/`).

Per user instruction, the actual data values from this file (SIC codes,
descriptions, and which companies share a code) are **not** retained in
this spec or anywhere else — only the file's shape/schema is recorded below,
so it can be regenerated later on request. The file will be deleted from
the project folder; when recreated, it should be re-derived from SEC data
(e.g. each company's `submissions_url` in `corpus_companies.json`, which
includes an SIC field) rather than from anything stored here.

**As built (2026-08-26), this file is now maintained automatically** — see
§7.1. `get_submissions()` refreshes each fetched company's row from that
company's live submissions JSON (`sic` / `sicDescription` / `name`) as its
last step, so `sic_numbers.json` is a derived cache of the corpus, not a
hand-maintained input. It currently holds all 20 companies. Deleting it is
safe: the next `get-submission` run rebuilds the rows for whatever
companies it fetches.

**Exact file format (for recreation), shape only — no real values:**

A **top-level JSON array** (not an object with a wrapper key) of 20
objects, one per corpus company, each with exactly these five
string-valued keys, in this order:

```json
[
  {
    "cik": "CIK<10-digit zero-padded CIK>",
    "ticker": "<primary ticker>",
    "company_name": "<company name as SEC records it>",
    "sic": "<4-digit SIC code>",
    "sic_description": "<SEC's SIC description text>"
  }
]
```

Field notes:
- `cik`: the **"CIK" prefix + zero-padded 10-digit CIK** as a string (e.g.
  `"CIK0001652044"` in shape) — note this differs from `corpus_companies.json`,
  which stores the padded CIK without the `CIK` prefix in `cik_padded` and
  the raw integer in `cik`.
- `ticker`: primary ticker string (matches `ticker` in
  `corpus_companies.json`).
- `company_name`: company name as it appears in SEC records — casing
  follows SEC's own inconsistent style per company (mixed case for some,
  all-caps for others) — same casing pattern as `title` in
  `corpus_companies.json`.
- `sic`: 4-digit SIC code as a **string** (not a number).
- `sic_description`: SEC's SIC description text, verbatim.

Row order in the original file followed `corpus_companies.json`'s company
order — all 20 corpus companies, one row each, no extras and no omissions.

## 6. Open items (to be specified later)

- Exact definition/taxonomy of "verification status" categories and their
  color coding.
- Precise semantics of "cross-sector questions" handling (e.g., comparison
  across sectors, sector-aware metric normalization, or both).
- Details of the semantic layer (intent map, sector detection, metric
  resolution) beyond what's implied by the corpus file's notes.
- CLI UX (command structure, input/output format details beyond
  color-coding).

## 7. Implementation status

### 7.1 SEC client + `get-submission` action (built)

Files, all under `verified-filings/`:

```
pyproject.toml            # uv project, httpx dep, ruff/pytest dev extras,
                           # `sec-retriever` console script -> app.cli:main
.gitignore
app/
  __init__.py
  cli.py                   # argparse CLI; subcommand "get-submission"
  ingest/
    __init__.py
    rate_limiter.py         # RateLimiter — global sliding-window limiter
    disk_cache.py            # DiskCache — URL-keyed on-disk response cache
    sec_client.py             # SECClient — async httpx client wiring
                              #   rate limit + UA + cache together;
                              #   submissions_url() URL builder
    corpus.py                 # loads corpus_companies.json, resolves a
                              #   ticker/alias/CIK to its entry
    sic_index.py               # update_sic_index() — refreshes
                              #   sic_numbers.json from submissions JSON
    actions.py                 # get_submission() — the "get-submission"
                              #   action
tests/
  __init__.py
  conftest.py                  # autouse: redirects sic_numbers.json to tmp
  test_rate_limiter.py
  test_disk_cache.py
  test_sec_client.py           # uses httpx.MockTransport — no real network
  test_corpus.py
  test_actions.py              # get_submission end-to-end, mocked
  test_sic_index.py            # sic_numbers.json upsert + auto-refresh
  test_cli.py                  # CLI wiring + reporting, mocked
```

Design notes:

- **`RateLimiter`** (`app/ingest/rate_limiter.py`): sliding 1-second
  window, not a fixed-window counter, so it can't burst 2x at a window
  boundary. One instance is shared **process-wide** across every
  `SECClient` (class-level `_shared_rate_limiter`), so the 10 req/s cap is
  global as specified, not per-client-instance. `SECClient.reset_shared_rate_limiter()`
  exists only so tests can isolate timing between test cases.
- **`DiskCache`** (`app/ingest/disk_cache.py`): content-addressed by
  `sha256(url)`; each URL gets a `{hash}.body` (raw response bytes,
  untouched) and `{hash}.meta.json` (status code + headers) file. Default
  location: `.cache/sec_edgar/` (relative, gitignored). Entries never
  expire — once fetched, a URL is served from disk indefinitely.
- **`SECClient`** (`app/ingest/sec_client.py`): async context manager
  wrapping `httpx.AsyncClient`. Every request always carries
  `User-Agent: "Amo Spamo amospamo@proton.me"`. `get_bytes()`/`get_json()`
  check the disk cache first; only a cache miss acquires a rate-limiter
  slot and hits the network. Accepts an optional `transport=` override
  (used only by tests, via `httpx.MockTransport`) so request-handling
  logic can be verified without any real HTTP calls.
- **`corpus.py`**: single place that reads `corpus_companies.json` and
  resolves an identifier (ticker, ticker alias, or CIK, case-insensitive)
  to its corpus entry; raises `UnknownCompanyError` otherwise. All ingest
  code is expected to resolve identifiers through this rather than
  accepting a raw CIK/URL, so nothing outside the 20-company corpus can be
  requested.
- **`get_submissions(identifiers)`** (`app/ingest/actions.py`): the
  "get-submission" action, batch form — takes a list of 1 or more
  identifiers (as few or as many as requested, up to all 20 corpus
  companies). All identifiers are resolved against the corpus *up front*;
  if any one of them doesn't match, `UnknownCompanyError` is raised naming
  every identifier that failed, and **nothing is fetched at all** — a typo
  in a 5-company batch never wastes the other 4 requests. Returns a dict
  keyed by each company's canonical ticker (e.g. an alias like "GOOG"
  files its result under "GOOGL"), mapping to that company's submissions
  JSON. Requests run sequentially, in the order given, sharing whatever
  client is passed in — so duplicate identifiers in the same batch just
  become a cache hit on the repeat, not a second fetch.
  `get_submission(identifier)` (singular) still exists as a thin
  convenience wrapper around `get_submissions([identifier])` for
  single-company use.
- **`sic_index.py` — auto-maintained `sic_numbers.json`:**
  `get_submissions()`, as its last step, calls
  `update_sic_index(results)`, which upserts one row per just-fetched
  company (`cik` / `ticker` / `company_name` / `sic` / `sic_description`,
  the §5.1 format) pulled from that company's submissions JSON, rewriting
  the file in corpus order. Rows for companies not in the current batch
  are left untouched, so the file grows to cover exactly the companies
  fetched so far (currently all 20). The path is
  `<repo root>/sic_numbers.json`, resolved via `corpus.CORPUS_FILE.parent`
  (CWD-independent, unlike the response cache). Pass
  `refresh_sic_index=False` to `get_submissions()` to skip it. The CLI
  prints a second stderr line, e.g.
  `[get-submission] sic_numbers.json now indexes 20 companies`. Tests
  never touch the real file — `tests/conftest.py` has an autouse fixture
  redirecting `sic_index.SIC_INDEX_FILE` to a temp path.
- **Request-count reporting (built into the client, not per-action):**
  `SECClient` tracks `request_count` (real network requests) and
  `cache_hit_count` (calls served from disk instead) as it runs —
  incremented inside `get_bytes()` itself, so this holds for any endpoint,
  not just submissions, and accumulates across an entire batch since every
  fetch in `get_submissions()` shares the one client. The CLI (`app/cli.py`)
  always constructs its own `SECClient`, hands it to whichever action is
  running, and after the action returns prints a summary line to stderr,
  e.g. for a 5-company request with 2 already cached:
  `[get-submission] 3 network request(s) made, 2 served from cache`
  (JSON result goes to stdout, kept separate so it's still pipeable/parseable).
  Actions follow the convention `async def action(identifiers: list[str], *,
  client: SECClient | None = None) -> Any` (documented in
  `actions.py`'s module docstring) — any action written to that
  convention and registered in `cli.py`'s `ACTIONS` dict automatically
  gets this reporting for free, with no per-action wiring needed. Counts
  are per-invocation only — a fresh `SECClient` (and fresh counters) is
  built each time the CLI runs, so nothing accumulates across separate
  command invocations; likewise the "global" 10 req/s rate limiter is only
  global within one running process, not persisted across separate CLI
  invocations either.
- **CLI**: `sec-retriever get-submission AAPL` for one company, or
  `sec-retriever get-submission AAPL MSFT GOOGL TSLA AMZN` for several in
  one call (or, without installing the entry point,
  `uv run python -m app.cli get-submission AAPL MSFT`).

**Live `get-submission` run for all 20 corpus companies (2026-08-26).**
Every company returned HTTP 200 with valid submissions JSON; all 20
responses are cached under `.cache/sec_edgar/` (~9 MB total, one
`<sha256(url)>.body` + `.meta.json` pair each). Re-runs serve entirely
from cache (0 network requests).

**Known limitation surfaced by that run — `filings.recent` pagination:**
SEC's `filings.recent` block holds "the most recent 1,000 filings *or*
one year, whichever is more," with older filings pushed into
`filings.files[]` overflow files that `get-submission` does **not**
currently fetch. For 18 of the 20 companies this is a non-issue — all 5
fiscal years of 10-K/10-Q are in `recent`. But **JPM and BAC** file
thousands of structured-note prospectuses per year, so their `recent`
window is ~1 year (25,879 and 11,512 entries) and contains only **1
10-K + 3 10-Q each**; the remaining 4 years sit in overflow files (69
for JPM, 20 for BAC). Any 5-year analysis of the two bank substitutions
will need a follow-on step that fetches and merges those overflow files.

Before the live runs, everything was verified offline: `httpx.MockTransport` in `tests/` and in throwaway smoke-test
scripts (not delivered) stands in for the network to confirm caching,
User-Agent, rate-limiter timing, corpus resolution, the action's URL
selection, request-count/cache-hit accounting (including the exact
"batch of 5, 2 cached + 3 fetched" scenario), and `app.cli.main()`'s
summary output — all behave correctly.

**Local toolchain set up (2026-08-26), Python bumped to 3.13 (2026-08-29).**
`uv` project environment; `.python-version` pins `3.13`, `uv.lock`
committed. `uv sync --extra dev` installs `httpx` + the dev tools
(`ruff`, `pytest`, `pytest-asyncio`) — 15 packages on 3.13, down from 19
on 3.9 (the `<3.11` backfills `exceptiongroup` / `tomli` /
`typing-extensions` / `backports-asyncio-runner` are no longer pulled
in).

- `uv run pytest` — **33 passed** on CPython 3.13.15, fully offline.
- `uv run ruff check .` and `uv run ruff format --check .` — clean.
- The ruff rule set is pinned explicitly in `pyproject.toml`
  (`[tool.ruff.lint] select = ["E", "F", "I", "UP", "B"]`,
  `target-version = "py313"`) rather than relying on ruff's shifting
  defaults.

Commands to reproduce from a clean checkout:

```
uv sync --extra dev
uv run pytest
uv run ruff check .
```

### 7.2 XBRL-data fetch + `get-xbrl` action (built)

New files under `verified-filings/`:

```
app/ingest/
  xbrl_store.py            # scope filter + curated per-company JSON store
data/xbrl/<TICKER>.json    # the curated store (gitignored)
tests/
  test_xbrl_store.py
  test_xbrl_actions.py
```

Plus additions to existing files: `xbrl_data_url()` builder in `sec_client.py`;
`get_xbrl_data()` in `actions.py`; `get-xbrl` subcommand + `ACTIONS` entry in
`cli.py`; an autouse `_isolate_xbrl_store` fixture in `tests/conftest.py`;
`/data/` in `.gitignore`.

Design notes:

- **Endpoint.** `get-xbrl` fetches each company's
  `companyfacts_url` from `corpus_companies.json`
  (`https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`). Per the
  project terminology note at the top of this doc, this is called **"XBRL
  data"** everywhere, never "companyfacts". Unlike `submissions`, this
  endpoint has **no pagination / overflow** — one complete document per
  company — so the JPM/BAC `filings.recent` problem from §7.1 does not recur.
- **`get_xbrl_data(identifiers, *, client=None, write_store=True)`**
  (`app/ingest/actions.py`): same shape and identifier-resolution contract as
  `get_submissions()` — every identifier resolved against the closed corpus
  up front, `UnknownCompanyError` naming *all* failures and fetching nothing
  if any fails; sequential fetches sharing one client; duplicates collapse to
  one fetch + a cache hit. **Returns a manifest** (canonical ticker → small
  summary: store path, fiscal years kept, taxonomy/concept/fact counts, byte
  size, and `latest_complete_fy`), *not* the filtered facts — those are large
  and go to the store files. The CLI prints that manifest to stdout; the
  standard `[get-xbrl] N network request(s) made, M served from cache` line
  still applies. The `sic_numbers.json` summary line is `get-submission`-only.
- **`xbrl_store.py` — scope filter (applied on ingest).** The raw SEC
  document carries every fact ever tagged (all forms, ~15 years, 500+
  concepts). `filter_facts()` reduces it to project scope (§5):
  - **Forms:** only exact `10-K` and `10-Q` rows. Amendments (`10-K/A` …) and
    all other forms are dropped.
  - **Fiscal years:** one **fixed window**, the same for all 20 companies —
    `FISCAL_YEAR_MAX` (2025) back `FISCAL_YEARS_KEPT` (5) years, i.e.
    **FY2021–FY2025**. Not derived per company: the corpus is closed and
    cross-company questions require every store on an identical fiscal-year
    grid. A row's `fy` is the fiscal year of the *filing* it appeared in (not
    the period it covers), so a kept 10-K still carries its prior-year
    comparative columns (FY2020 figures therefore still appear as comparatives
    inside FY2021 filings).
    - `FISCAL_YEAR_MAX` is the newest year for which **every** corpus company
      has an annual 10-K. As of 2026-08, MSFT / NVDA / ORCL have already filed
      a FY2026 10-K (their fiscal years end mid-calendar); those extra years
      are deliberately dropped to keep the grid aligned. `get-xbrl` prints a
      stderr note naming any such company — the signal to bump
      `FISCAL_YEAR_MAX` and re-run (re-filters from cache, no network) once
      **all** 20 have rolled forward.
  - Empty concepts / units / taxonomies are pruned. `srt` and `dei` facts are
    kept (filtered the same way) — the filter is about forms and years, not
    taxonomies.
  - Fact rows pass through **untouched** (`start`/`end`/`val`/`accn`/`fy`/
    `fp`/`form`/`filed`/`frame`) — full filing-level provenance for the
    verifier.
  - `latest_complete_fiscal_year(raw_facts)` — helper: the highest `fy` with a
    `10-K` in the raw document. Drives the roll-forward note; not part of the
    filter itself.
- **Store format.** `data/xbrl/<TICKER>.json`, pretty-printed (`indent=2`),
  one file per company: top-level `cik` / `ticker` / `entity_name` /
  `source_url` / `retrieved` (UTC ISO-8601) / `scope` (`forms`,
  `fiscal_years`) / `counts` (`taxonomies`, `concepts`, `facts`) / `facts`
  (the filtered tree). `data/` is gitignored (derived, regenerable). No
  cache-busting / `--refresh` flag yet (deliberate) — a URL that's cached is
  served from disk forever, so re-running `get-xbrl` re-writes the store from
  the cached document without new network traffic.

**Live `get-xbrl` run for all 20 corpus companies (2026-08-29).** All 20
returned HTTP 200. Raw responses cached under `.cache/sec_edgar/` (~84 MB for
the 20 XBRL documents, on top of the ~9 MB of submissions). Curated store:
**~54 MB total** across the 20 pretty-printed files (~1.9–4.7 MB each; JPM
largest), all on the **FY2021–FY2025** grid, **174,390 fact rows** kept. The
roll-forward note fired for MSFT, NVDA, ORCL (FY2026 10-K already filed).
Re-runs are 0 network requests (all served from cache).

Verified offline first (`tests/test_xbrl_store.py`,
`tests/test_xbrl_actions.py`, `tests/test_cli.py` — `httpx.MockTransport`
throughout): URL selection, the forms + fixed fiscal-year window (including
that newer years are clamped out and the window is overridable),
concept/unit/taxonomy pruning, `latest_complete_fiscal_year()`, manifest
shape and ticker canonicalisation, batch + duplicate + unknown-identifier
behaviour, cache-hit accounting, pretty-printed output, the roll-forward
stderr note, and `write_store=False`.
`uv run pytest` — **55 passed**; `ruff check` / `ruff format --check` clean.

### 7.3 Not yet built

- Anything in `app/schemas/`, `app/semantic/`, `app/retrieval/`,
  `app/verify/`, `app/api/`, `app/db/`.
- ~~Recreating `sic_numbers.json`~~ **Done (2026-08-26).** Rebuilt from
  live SEC data and now maintained automatically by `get_submissions()`
  via `sic_index.update_sic_index()` — see §5.1 and the `sic_index.py`
  design note in §7.1. Currently holds all 20 companies.
- ~~`companyfacts_url` / XBRL-data fetching~~ **Done (2026-08-29)** — see §7.2.
- The `submissions` overflow-file merge for JPM & BAC (§7.1) — still not done;
  note this only affects `submissions`, not the XBRL-data store, which has no
  overflow.
- The question-answering CLI itself (Section 1) — `get-submission` /
  `get-xbrl` are raw data retrieval, not yet wired to answering a question or
  color-coding verification status.
