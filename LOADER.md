# The data loader (curated XBRL data -> PostgreSQL)

`app/db/loader.py` reads the curated files in `data/xbrl/<TICKER>.json`
(written by retrieval, `app/ingest/`) and writes their contents into the
`xbrl` PostgreSQL tables (`app/db/models.py`). It is a **separate, standalone
step** from retrieval — the two are joined only by the files on disk, and are
run independently.

Run every command **from the repository root**.

---

## Running it

```
python -m app.db.loader AAPL MSFT
```
(or `uv run python -m app.db.loader AAPL MSFT` without installing the entry point)

- Identifiers work exactly like `get-xbrl` / `get-submission`: ticker, ticker
  alias, or CIK, one or many, resolved against `corpus_companies.json` first.
- If **any** identifier doesn't resolve, **nothing is loaded** —
  `UnknownCompanyError` names every one that failed.
- Likewise if any company's curated file is missing: checked up front, nothing
  is loaded, and the error tells you which `get-xbrl` to run first.
- Duplicate identifiers collapse — `AAPL AAPL` (or `GOOG GOOGL`) loads once.
- Companies then load one at a time. If one fails partway through a batch,
  the batch **stops there** — earlier companies stay loaded (each company's
  load is its own database transaction; there is nothing to undo for them).
- Re-running for a company that's already loaded is safe (see below).

Example output:
```
[load] AAPL: OK (20 filings, 6120 facts, 11 dropped (unit not allowed))
[load] MSFT: OK (20 filings, 5980 facts)
[load] 2/2 companies loaded
```
A dropped-unit count is only shown when it's non-zero — it's the one thing
worth a human's attention on an otherwise successful load.

---

## How it works

Two halves, on purpose:

1. **`build_plan(doc)`** — pure, no I/O. Takes a validated
   `CompanyFactsFile` (`app/schemas/xbrl.py`) and works out every row to
   insert: dedupes `Filing`s by accession number, dedupes `Concept`s by
   `(taxonomy, name)`, drops any fact whose unit isn't in `ALLOWED_UNITS`, and
   decides `is_latest` (the fact with the newest `filed` date wins, per
   period). Fully unit-testable without a database.
2. **`load_file(path)`** — validates the file, calls `build_plan`, and writes
   everything in **one transaction**:
   - `Company` — **upsert** (`ON CONFLICT ... DO UPDATE`), so the inert `id`
     column stays stable across reloads.
   - `Filing` — **delete then insert** for this company. Deleting `Filing`
     cascades to `Fact` automatically (`ON DELETE CASCADE`), so wiping
     `Filing` is enough to clear both.
   - `Concept` — a single bulk **upsert**, since it's shared across every
     company. A non-null `label`/`description` already on file is kept; a null
     one gets filled in from the new file. `RETURNING` hands back the surrogate
     ids for the facts to reference, so there's no follow-up `SELECT`.
   - `LoadRun` — always **inserted**, never touched by the delete. It's an
     append-only log of every load that ever happened, including the ones
     dropped-for-unit count — reloading a company doesn't erase its history.
3. **`load_batch(identifiers)`** — the resolve-everything-first, then
   sequential-with-abort-on-failure orchestration described above.

### Why re-running a company is safe

Loading the same file twice does **not** duplicate rows or error. `Filing`
(and the `Fact`s that cascade with it) are wiped and rewritten from scratch
each time; `Company` and `Concept` are upserted; `LoadRun` simply gets a new
row recording the second load. Verified by `tests/test_loader.py`.

---

## Testing it

Runs against a **separate PostgreSQL container**, `db-test` (see
`docker-compose.yml`, `ALEMBIC.md`) — never the real database.

- `tests/fixtures/xbrl_fake_company.json` — a fake company (cik `9999999`,
  ticker `ZZZZ`) built to exercise: an instant fact, a duration fact, a
  **restatement** (same period, two filings, different values — proves
  `is_latest`), a unit outside `ALLOWED_UNITS` (proves it's dropped and
  counted), a concept with a null label/description, and two taxonomies.
  Concept *names* are obviously fake (`ZzzTest...`); the taxonomy values
  themselves have to be real (`us-gaap` / `dei`) since that's a fixed enum.
- `tests/conftest.py` — `test_session_factory` (a fresh engine per test,
  pointed at `DATABASE_URL_TEST`) and `clean_fake_company` (deletes the fake
  company + its concepts before and after a test).
- `tests/test_loader.py` — `build_plan` tested directly (no DB), plus two
  tests against the real test database: one loads the fixture and reads every
  table back, the other loads it twice to prove re-running is safe.

If you change `app/db/models.py`, remember to migrate the test database too
— see `ALEMBIC.md` Part 2, step 5.

---

## A correction worth knowing

Earlier design notes (now fixed) assumed the loader had to read files with
`json.loads(text, parse_float=Decimal)` to avoid a value like `0.047` becoming
an imprecise binary float. **Not needed.** Pydantic's `Decimal` field
validator converts a Python `float` via its *string* form
(`str(500000.47) -> Decimal('500000.47')`), not the raw binary value, so
plain `json.loads(text)` is already exact — as long as the raw dict goes
straight into `CompanyFactsFile.model_validate()` before anything else
touches `val`. Verified in `tests/test_loader.py` (the fixture's revenue
fact is a bare JSON float with cents) and empirically:

```python
>>> Decimal(500000.47)                 # raw float -> Decimal: wrong
Decimal('500000.46999999997206032276153564453125')
>>> FactIn.model_validate({"val": 500000.47, ...}).val   # via Pydantic: exact
Decimal('500000.47')
```

---

## Not done yet

- Loading is XBRL-data only. The **submissions** JSON (SIC codes, filing
  history) is not modeled in the database at all.
- No logging beyond the one summary line per company.
- `Fact` rows are inserted as ORM objects. Measured ~1.4s for AAPL (6.1k facts)
  and ~2.4s for JPM (15.5k facts, the largest) — fine as-is; a Core bulk insert
  would be the next lever if that ever matters.
- No cross-check that a file's internal `cik` matches the ticker used to look
  it up.
- Outbound read APIs / queries over the loaded data — not built; this is
  write-only so far.
