# `app/db` — XBRL data model: design decisions

This document is the context handoff for the SQLAlchemy models in
[`models.py`](models.py). It records **what was decided and why**, so a future
session can extend the layer (e.g. write the Pydantic schemas, wire up a
session, add a table) or explain any part of it without re-deriving the
reasoning. Read it alongside `models.py` — the code has per-line comments, this
has the rationale.

Related memory: `db-layer-layout.md`, `xbrl-data-ingest-plan.md`,
`xbrl-data-terminology.md`.

### Vocabulary — two steps, don't say "ingest"

| step | direction | code | timestamp |
|---|---|---|---|
| **retrieval** | SEC cloud → `data/xbrl/<TICKER>.json` on disk | `app/ingest/` (built) | the file's own `retrieved` field |
| **load** | `data/xbrl/*.json` → rows in the `xbrl.*` tables | `app/db/loader.py` (not written) | `LoadRun.loaded_at` |

"Ingest" was previously used for both and is avoided from here on. `sec-retriever.md`
still names the retrieval layer `app/ingest/` — that folder was kept (renaming it
collided with §3's reserved `app/retrieval/` for hybrid search).

---

## 1. What this models

One row per **fact** in the curated per-company JSON at `data/xbrl/<TICKER>.json`,
written by [`app/ingest/xbrl_store.py`](../ingest/xbrl_store.py). Each of those
files is a scope-filtered copy of the SEC `xbrl/companyfacts` endpoint payload
("**XBRL data**" — never call it "companyfacts" in prose), restricted to:

- forms **10-K and 10-Q** only
- the **5 most recent fiscal years**

Source JSON shape:

```jsonc
{
  "cik": 320193,
  "ticker": "AAPL",
  "entity_name": "Apple Inc.",
  "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
  "retrieved": "2026-08-30T02:30:47Z",
  "scope":  { "forms": ["10-K", "10-Q"], "fiscal_years": [2021, 2022, 2023, 2024, 2025] },
  "counts": { "taxonomies": 2, "concepts": 252, "facts": 6131 },
  "facts": {
    "<taxonomy>": {                         // "dei" | "us-gaap" | "srt"
      "<ConceptName>": {                    // e.g. "Assets"
        "label": "Assets",
        "description": "Sum of the carrying amounts ...",
        "units": {
          "<unit>": [                       // e.g. "USD"
            { "end": "2020-09-26", "val": 323888000000, "accn": "0000320193-21-000010",
              "fy": 2021, "fp": "Q1", "form": "10-Q", "filed": "2021-01-28",
              "start": "2019-09-29",        // present only for duration facts
              "frame": "CY2020Q4I" }        // present ~43% of the time
          ]
        }
      }
    }
  }
}
```

---

## 2. The five tables

| model | grain | PK | notes |
|---|---|---|---|
| `Company` | one issuer | `cik` | 20 rows for the current corpus |
| `Filing` | one XBRL accession | `accession_number` | a 10-K or 10-Q |
| `Concept` | one taxonomy element, **global** | `id` (surrogate) | ~6.8k rows, shared by all companies |
| `Fact` | one observed value | `id` (surrogate) | the big table — ~174k rows for 20 companies |
| `LoadRun` | one load of one company file | `id` (surrogate) | provenance + audit |

Relationships:

```
Company ─1:∞─ Filing ─1:∞─ Fact ─∞:1─ Concept
   │                          │
   └─1:∞─ Fact  (denormalized shortcut past Filing)
   └─1:∞─ LoadRun
```

---

## 3. Decisions & rationale

### 3.1 PostgreSQL namespace: `xbrl`, not `public`

All tables and the three enum types live in a dedicated PG schema `xbrl`
(`Base.metadata = MetaData(schema="xbrl")`). A `before_create` event emits
`CREATE SCHEMA IF NOT EXISTS xbrl`; an `after_drop` event emits
`DROP SCHEMA IF EXISTS xbrl CASCADE`. Both are guarded with
`.execute_if(dialect="postgresql")` so a non-PG backend (e.g. a SQLite test DB)
doesn't choke.

Why: isolation from anything else in the database; `DROP SCHEMA xbrl CASCADE`
is a one-line clean-slate for a full reload.

Note the three words for "schema" that came up:
- **relational schema** = the whole table blueprint (what `models.py` expresses)
- **PostgreSQL schema** = this namespace
- **Pydantic "schema"** = a DTO / request-response shape (FastAPI's naming); the
  framework-agnostic term is **DTO**. Not built yet — see §5.

### 3.2 `Company.cik` is the primary key; `Company.id` is inert

`cik` (SEC Central Index Key) is a genuine natural key: SEC-assigned, integer,
globally unique, never reused or reassigned, and every company in scope has one
(it's how the data is fetched). So it is the PK, and every FK targets it
(`company_cik`).

`Company.id` **is still created and populated** — it's a PostgreSQL `IDENTITY`
column, gets a value on every insert, and carries a `UNIQUE` constraint — but
**nothing references it and the application never keys on it**. It exists so a
synthetic surrogate can be promoted to the FK target later (e.g. if we ever need
to store an entity with no CIK — a private subsidiary, a test fixture) without a
dedupe migration. The user explicitly wanted it present-but-unused, not omitted.

### 3.3 `Filing.accession_number` is the primary key

The SEC accession number (e.g. `0000320193-23-000106`) is globally unique — it
embeds the filer's CIK and a sequence. No surrogate needed. `fy`, `fp`, `form`,
`filed` repeat on every fact in the source JSON; they are hoisted onto `Filing`
so `Fact` doesn't carry four redundant columns.

### 3.4 `Concept` is one global table

Verified against all 20 files: for every concept name shared across companies,
the `label` and `description` are **byte-identical** — 0 conflicts across 1,914
shared concepts. `us-gaap:Assets` means the same thing everywhere (it's a
published FASB taxonomy; companies can't redefine it). So label/description are
stored **once** here, not per-company. ~6,834 concept rows cover all 20
companies and the table barely grows as companies are added.

- **Surrogate `id` PK**, not the natural `(taxonomy, name)` pair: `Fact`
  references `Concept` on every row, and a single-column int FK is cheaper to
  carry and index than a two-column composite. The natural key is enforced by
  `UniqueConstraint("taxonomy", "name")`.
- `label` / `description` are nullable — ~206 concepts (mostly `dei` / `srt`)
  have neither.
- On load: upsert by `(taxonomy, name)`; if a later company supplies a
  non-null label where we had null, fill it in. No conflict handling beyond that.

### 3.5 `Fact` details

**`value` is `Numeric(30, 6)` (DECIMAL), never `BigInteger`.**
Observed value range across all files: −3.1e11 … 6.3e13, with ~8,700 non-integer
values and up to **4 decimal places**. `BigInteger` would not merely lose
precision — it would **destroy** every sub-1 value: EPS `2.97 → 2`, tax rate
`0.159 → 0`, par value `0.00001 → 0`, dividend-per-share `0.1925 → 0`. `Numeric`
stores big integers and small decimals exactly; maps to Python `Decimal`.

**Instant vs duration: `period_start` nullable + `is_instant` bool.**
Balance-sheet facts (instant) have no `start` in the source; flow facts
(duration) do. `period_start IS NULL` ⇔ instant. `is_instant` is a stored
boolean mirror of that, kept honest by a check constraint
`(period_start IS NULL) = is_instant`. A second check enforces
`period_start <= period_end`.

**Natural / dedupe key — a functional unique index:**
```
UNIQUE (filing_accession, concept_id, unit, coalesce(period_start, period_end), period_end)
```
`COALESCE` because PostgreSQL treats `NULL` as distinct in a plain `UNIQUE`
constraint, so instants (null start) would not dedupe. This collapses instants
onto `period_end`. Verified: this tuple is unique across every file; **without**
`accn`/`filing` in the key there are up to 10 duplicates per period (the same
period re-reported by later filings).

**`is_latest` boolean.** The same period is re-reported by every subsequent
filing, and sometimes the value changes (a true restatement). Measured: 46,378
period-slots are reported by more than one filing; 1,575 of those have a changed
value. Example — Apple `ContractWithCustomerLiabilityRevenueRecognized`, FY2023:
`$8.20B` (filed 2023-11-03) → `$8.20B` (2024-11-01, carried as comparative) →
`$8.169B` (2025-10-31, restated). **All rows are kept.** `is_latest = True`
marks the most-recently-filed value for a
`(company_cik, concept_id, unit, period_start, period_end)` group; the load step
flips older rows to `False` as newer filings load. Analytical queries filter
`WHERE is_latest`. The partial index `ix_fact_latest_lookup` backs this.

**`company_cik` is denormalized onto `Fact`** (also reachable via `filing`).
Nearly every query slices by company; this avoids a join and lets the partial
index exist. Costs 4 bytes/row.

**`unit` is a plain `String(32)`, no lookup table.** After the ALLOWED_UNITS
filter (§3.6) only ~6 real values remain; a table would be ceremony. FK
`ondelete`: `filing`/`company` are `CASCADE`, `concept` is `RESTRICT` (reference
data — must not vanish under facts).

**`frame`** is the SEC "frame" tag (e.g. `CY2021Q2I`) for cross-company
comparison; often null; `String(16)`. Backed by `ix_fact_concept_frame`.

### 3.6 `ALLOWED_UNITS` — a load-time policy, not a schema constraint

```python
ALLOWED_UNITS = frozenset({"USD", "shares", "pure", "USD/shares", "Rate", "EUR"})
```

`USD` covers ~6,048 of ~6,900 concept/unit pairs. Real secondary units:
`shares`, `pure`, `USD/shares`, `Rate`, `EUR`. Then a tail of ~22 junk units
(`warehouse`, `Plaintiff`, `judicialCase`, `MWh`, `reportable_segment`, …), each
in exactly one company — these tag disclosure **counts** (number of lawsuits,
warehouses, segments), not financial data, with inconsistent `val` semantics.

**The load step drops any fact whose unit ∉ `ALLOWED_UNITS`.** The `Fact.unit`
column itself is an unconstrained string — nothing in the DB enforces the
allow-list. The constant lives in `models.py` only so the policy is documented
next to the table and the `LoadRun` audit columns. **The enforcement code does
not exist yet** — when `app/db/loader.py` is built, it will:
`if unit not in ALLOWED_UNITS: dropped += len(facts); continue`, then write
`LoadRun.units_allowlist = sorted(ALLOWED_UNITS)` and
`LoadRun.facts_dropped_unit = dropped`.

To widen coverage later: add to `ALLOWED_UNITS`, reload. The audit trail in
`LoadRun` explains any historical gap.

### 3.7 `LoadRun` — provenance, idempotency, drop audit

Captures the source file's `source_url` / `retrieved` (→ `retrieved_at` — when
*retrieval* fetched the file, not when this load ran), the `scope` block
(`scope_forms`, `scope_fiscal_years` as `JSONB`), the `counts` block
(`taxonomy_count` / `concept_count` / `fact_count` — checksum the load against
these), plus `units_allowlist` and `facts_dropped_unit` for the §3.6 audit.
`loaded_at` (when this load ran) defaults to `now()`.

> ⚠️ The three `JSONB` list columns are typed `Mapped[list[...]]`. In-place
> mutation (`run.scope_forms.append(...)`) is **not** change-tracked without
> `MutableList.as_mutable(JSONB)`. Fine for an append-only row built once; don't
> rely on `.append()` persisting.

### 3.8 Auto keys: `Identity()` everywhere, no `SERIAL`

Every surrogate PK and `Company.id` uses `mapped_column(..., Identity())` →
`GENERATED BY DEFAULT AS IDENTITY` (SQL-standard, PG 10+). The three surrogate
PKs originally emitted legacy `SERIAL`/`BIGSERIAL` (SQLAlchemy's default for an
int PK); changed to `Identity()` for consistency and because there is no reason
to use the legacy sequence mechanism on a fresh PG-only schema.

### 3.9 Nullability declared twice, on purpose

The user chose **explicitness over DRY**. Every non-PK column carries an
explicit `nullable=True` / `nullable=False` *in addition to* what its
`Mapped[T]` vs `Mapped[T | None]` annotation already implies. PK columns state
non-null once via `primary_key=True` (no redundant `nullable=False`). Keep this
pattern when adding columns.

### 3.10 String lengths bounded from observed data

Measured across all 20 files:

| column | observed max | p99 | choice | reasoning |
|---|---|---|---|---|
| `Company.source_url` | 61 | — | `String(255)` | fixed `…/companyfacts/CIK{10}.json` |
| `LoadRun.source_url` | 61 | — | `String(255)` | same |
| `Company.entity_name` | 31 | — | `String(150)` | EDGAR conformed-name cap is 150 |
| `Concept.name` | 165 | — | `String(255)` | XBRL element names; 255 safe |
| `Concept.label` | 180 | 131 | `String(512)` | bounded, ~3× headroom |
| `Concept.description` | ~1,668 | — | **`Text`** | prose paragraphs, no natural bound |
| `Filing.accession_number` | 20 | — | `String(20)` | fixed format `NNNNNNNNNN-NN-NNNNNN` |
| `Fact.unit` | 10 (`USD/shares`) | — | `String(32)` | headroom for a widened allow-list |
| `Fact.frame` | 9 (`CY2021Q2I`) | — | `String(16)` | |
| `Company.ticker` | ≤5 | — | `String(10)` | |

Rule going forward: bound a string when the data has a natural/observed ceiling;
use `Text` only for genuinely open-ended prose (currently just
`Concept.description`).

### 3.11 Native PG enums

`taxonomy` (`dei` / `us-gaap` / `srt`), `filing_form` (`10-K` / `10-Q`),
`fiscal_period` (`FY` / `Q1` / `Q2` / `Q3`) are `sqlalchemy.Enum` → native PG
`CREATE TYPE`, in the `xbrl` schema. Trade-off: adding a value later (e.g. a
`10-K/A` amendment form) needs `ALTER TYPE ... ADD VALUE`, which Alembic can do
but not inside a transaction. If that churn becomes real, switch the affected
one to `String` + `CheckConstraint`.

### 3.12 Delete behavior

- `Company.filings` / `Company.facts` / `Company.load_runs` / `Filing.facts`:
  `cascade="all, delete-orphan"` + `passive_deletes=True`, paired with FK
  `ondelete="CASCADE"`. The DB does the cascade; the ORM doesn't pre-load
  children.
- `Concept.facts`: `passive_deletes="all"` and **no** cascade, paired with FK
  `ondelete="RESTRICT"`. Without `passive_deletes="all"`, deleting a `Concept`
  would make SQLAlchemy try to `NULL` out `fact.concept_id` (a `NOT NULL`
  column) and raise a confusing ORM error before the DB's `RESTRICT` fires.
  With it, the `RESTRICT` is the enforcer and you get a clean FK violation —
  which is correct: concepts are shared reference data and must not be deleted
  while facts point at them.

### 3.13 Constraint / index naming

`Base.metadata` carries a `naming_convention` so SQLAlchemy-auto-named objects
(chiefly FKs) get deterministic names → stable Alembic autogenerate diffs.
Explicitly-named constraints keep their given name. Because the `ck` template is
`ck_%(table_name)s_%(constraint_name)s`, **every `CheckConstraint` must be given
a `name`** (the two on `Fact` are `instant_flag` / `period_order` →
`ck_fact_instant_flag` / `ck_fact_period_order`). A nameless `CheckConstraint`
will fail at DDL-compile.

### 3.14 Index style (known inconsistency, deliberately deferred)

Single-column indexes are declared inline (`index=True`); composite and partial
indexes are `Index(...)` in `__table_args__`. `Fact` uses both styles. The user
chose to leave this as-is for now. `concept_id` has **no** standalone index —
`ix_fact_concept_frame (concept_id, frame)` covers the prefix and the FK check.

Full index list:

| index | table | columns | kind |
|---|---|---|---|
| `uq_fact_natural` | fact | `(filing_accession, concept_id, unit, coalesce(period_start,period_end), period_end)` | UNIQUE, functional |
| `ix_fact_latest_lookup` | fact | `(company_cik, concept_id, period_end)` `WHERE is_latest` | partial |
| `ix_fact_concept_frame` | fact | `(concept_id, frame)` | |
| `ix_filing_company_period` | filing | `(company_cik, fiscal_year, fiscal_period)` | |
| `ix_filing_filed_date` | filing | `(filed_date)` | |
| inline `index=True` | | `company.ticker`, `filing.company_cik`, `fact.filing_accession`, `fact.company_cik`, `load_run.company_cik` | |

---

## 4. Reference: source field → column

### Company (from the file header)
| source | column |
|---|---|
| `cik` | `Company.cik` (PK) |
| `ticker` | `Company.ticker` |
| `entity_name` | `Company.entity_name` |
| `source_url` | `Company.source_url` |
| — (DB-generated) | `Company.id` |

### Filing (from each fact's repeated fields, deduped by `accn`)
| source | column |
|---|---|
| `accn` | `Filing.accession_number` (PK) |
| `cik` | `Filing.company_cik` (FK) |
| `form` | `Filing.form` |
| `fy` | `Filing.fiscal_year` |
| `fp` | `Filing.fiscal_period` |
| `filed` | `Filing.filed_date` |

### Concept (from `facts.<taxonomy>.<name>`)
| source | column |
|---|---|
| `<taxonomy>` key | `Concept.taxonomy` |
| `<ConceptName>` key | `Concept.name` |
| `.label` | `Concept.label` |
| `.description` | `Concept.description` |
| — (DB-generated) | `Concept.id` (PK) |

### Fact (from each element of `...units.<unit>[]`)
| source | column |
|---|---|
| `accn` | `Fact.filing_accession` (FK) |
| `cik` (file-level) | `Fact.company_cik` (FK) |
| resolved concept | `Fact.concept_id` (FK) |
| `<unit>` key | `Fact.unit` |
| `start` (may be absent) | `Fact.period_start` |
| `end` | `Fact.period_end` |
| derived: `start` absent | `Fact.is_instant` |
| `val` | `Fact.value` |
| `frame` (may be absent) | `Fact.frame` |
| derived by the load step | `Fact.is_latest` |
| — (DB-generated) | `Fact.id` (PK) |

### LoadRun (from the file header)
| source | column |
|---|---|
| `source_url` | `LoadRun.source_url` |
| `retrieved` | `LoadRun.retrieved_at` |
| `scope.forms` | `LoadRun.scope_forms` (JSONB) |
| `scope.fiscal_years` | `LoadRun.scope_fiscal_years` (JSONB) |
| `counts.taxonomies` | `LoadRun.taxonomy_count` |
| `counts.concepts` | `LoadRun.concept_count` |
| `counts.facts` | `LoadRun.fact_count` |
| `sorted(ALLOWED_UNITS)` | `LoadRun.units_allowlist` (JSONB) |
| count of filtered facts | `LoadRun.facts_dropped_unit` |
| — (DB `now()` at load) | `LoadRun.loaded_at` |

---

## 5. Where things go — layout follows `sec-retriever.md` §3

| thing | location | status |
|---|---|---|
| **Pydantic schemas** | `app/schemas/xbrl.py` (+ `DESIGN.md`, `tests/test_xbrl_schema.py`) | **built 2026-08-30.** Inbound validation of one `data/xbrl/*.json` file. See §6 and `app/schemas/DESIGN.md`. |
| **The load step** | `app/db/loader.py` | not written. Reads a file (`json.loads(..., parse_float=Decimal)`), validates via `CompanyFactsFile`, walks `iter_facts()`, applies `ALLOWED_UNITS` (from `app.db`), upserts `Concept`, inserts `Filing` / `Fact`, maintains `is_latest`, writes a `LoadRun`. |
| **Alembic** | `app/db/migrations/` (scripts) + `alembic.ini` at repo root | not written. `alembic.ini` at root is just the default lookup location; `script_location = app/db/migrations`. `env.py` imports `app.db.Base` for `target_metadata`. User will ask for help when ready. |
| **Engine + `sessionmaker`** | `app/db/session.py` | not written. Do alongside the Alembic work. |
| **HTTP API DTOs** | `app/api/` — **only if** a real HTTP API is added | §3 reserves `app/api/` for FastAPI routes (Sprint 2). Keep request/response DTOs there, not in `app/schemas/`. Currently a CLI. |

Dependency direction is always **schemas → nothing in the app** and **loader →
(schemas, models)**, never the reverse. The nested-JSON walk is
`CompanyFactsFile.iter_facts()` — a method on the schema, not a separate module
(see `app/schemas/DESIGN.md` §4.14).

---

## 6. Pydantic schemas — built

`app/schemas/xbrl.py` (2026-08-30). Inbound validation of one
`data/xbrl/<TICKER>.json` file before the load step. Classes: `CompanyFactsFile`
(top), `ScopeIn`, `CountsIn`, `ConceptIn`, `FactIn`. Every rationale — the seven
signed-off decisions plus the micro-choices — is in
**[`app/schemas/DESIGN.md`](../schemas/DESIGN.md)**. Tests:
`tests/test_xbrl_schema.py` (all 20 real store files validate).

What the load step (`app/db/loader.py`, TBD) must know:

- Read files with **`json.loads(text, parse_float=Decimal)`** so `FactIn.val`
  arrives as an exact `Decimal`, never a binary `float`.
- Walk facts via **`CompanyFactsFile.iter_facts()`** — yields
  `(taxonomy, concept_name, ConceptIn, unit, FactIn)`. Don't re-implement the loop.
- Schema field names mirror the **source JSON** (`start`/`end`/`val`); the loader
  renames to the ORM names (`period_start`/`period_end`/`value`).
- Apply `ALLOWED_UNITS` in the loader — the schema accepts any unit key.
- `LoadRun.taxonomy_count` / `concept_count` / `fact_count` can be taken straight
  from `CountsIn` (the schema already checksums them against the facts tree).

Outbound / read DTOs are still deferred — separate classes with
`from_attributes=True`, never reuse the `*In` models.

---

## 7. Data facts (measured across all 20 files, 2026-08-30)

- 20 companies. Taxonomies present: `dei` (20), `us-gaap` (20), `srt` (7).
- ~174,390 total facts; ~6,834 concept rows; 206 concepts with null
  label+description.
- Fact object keys: `end`, `val`, `accn`, `fy`, `fp`, `form`, `filed` always
  present; `start` on ~59%; `frame` on ~43%.
- `form` ∈ {`10-K`, `10-Q`}; `fp` ∈ {`FY`, `Q1`, `Q2`, `Q3`}.
- `val`: int or float, range −3.1e11 … 6.3e13, ≤4 decimal places, ~8,700
  non-integer, ~19,000 negative.
- Concept `label`/`description`: **identical across all companies** (0 conflicts
  / 1,914 shared concepts).
- Restatements: 46,378 multi-filing period-slots; 1,575 with a changed value.
- Natural key `(filing, concept, unit, start, end)` is unique; drop `filing` and
  up to 10 duplicates appear.
