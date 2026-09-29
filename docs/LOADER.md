# The load step — curated XBRL files → PostgreSQL

`app/db/loader.py` reads `data/xbrl/<TICKER>.json` (written by retrieval,
`app/ingest/`) and writes it into the `xbrl` tables. It is a separate step
from retrieval: the two are joined only by the files on disk.

```
uv run python -m app.db.loader AAPL MSFT
```

- Identifiers work like `get-xbrl`: ticker, ticker alias or CIK, resolved
  against `corpus_companies.json`.
- **All or nothing up front.** If any identifier does not resolve, or any
  company's file is missing, nothing loads, and the error names every one
  (with the `get-xbrl` to run).
- Duplicates collapse — `AAPL AAPL` (or `GOOG GOOGL`) loads once.
- Companies then load one at a time, each in its own transaction. If one fails,
  the batch stops; the ones before it stay loaded.
- Re-running is safe (below).

Output, one line per company; a dropped-unit count appears only when it is
non-zero, since it is the one thing worth a human's attention:

```
[load] AAPL: OK (20 filings, 6120 facts, 11 dropped (unit not allowed))
[load] 2/2 companies loaded
```

## How it works

1. **`build_plan(doc)`** — pure, no I/O. Takes a validated `CompanyFactsFile`
   and works out every row: dedupes filings by accession number and concepts
   by `(taxonomy, name)`, drops facts whose unit is not in `ALLOWED_UNITS`
   (`app/db/DESIGN.md` §3.6), and decides `is_latest` — the newest `filed`
   date wins, per period.
2. **`load_file(path)`** — validates, plans, and writes in **one
   transaction**:
   - `company` — upsert, so the inert `id` stays stable across reloads.
   - `filing` — delete, then insert, for this company. The delete cascades to
     `fact`.
   - `concept` — one bulk upsert (shared by every company). A label or
     description already on file is kept; a missing one is filled in.
     `RETURNING` hands back the ids, so there is no follow-up `SELECT`.
   - `load_run` — always inserted, never deleted: an append-only record of
     every load, with its dropped-unit count.
3. **`load_batch(identifiers)`** — the resolve-first, stop-on-failure
   orchestration above.

The sector columns (`sic_code`, `sic_description`) come from
`sic_numbers.json`, keyed on cik. A company missing from that file keeps what
it had: absent data never overwrites present data.

**Re-running a company is safe.** Filings and their facts are rewritten from
scratch, companies and concepts are upserted, and `load_run` gains a row.

Tests: [TESTING.md](TESTING.md). Open items: [FUTURE.md](FUTURE.md#data-and-ingest).
