# `app/schemas` — Pydantic schemas: design decisions

Context handoff for the schemas in [`xbrl.py`](xbrl.py), like
[`app/db/DESIGN.md`](../db/DESIGN.md) is for the ORM models. Records **what was
decided and why**, so a future session can extend or explain the schemas without
re-deriving the reasoning.

Related: `app/db/DESIGN.md`, memory `db-layer-layout.md`, `xbrl-data-terminology.md`.

---

## 1. Purpose & scope

`app/schemas/` is the app-wide home for Pydantic models — `sec-retriever.md` §3
calls it "the single source of truth". One module per domain; consumers import
from the submodule (`from app.schemas.xbrl import CompanyFactsFile`), so
`__init__.py` stays a docstring only.

`xbrl.py` validates **one curated XBRL-data file** (`data/xbrl/<TICKER>.json`,
written by the retrieval step in `app/ingest/xbrl_store.py`) on the way IN,
before the **load step** (`app/db/loader.py`, see `LOADER.md`) turns it into
`app.db` ORM rows.

- **Inbound validation only.** No outbound / read DTOs yet — those come if/when
  there's an HTTP API or structured CLI output, as separate classes with
  `from_attributes=True`. Never reuse the `*In` models for output.
- Dependency direction: **schemas → nothing in the app**; **loader → (schemas,
  models)**. No cycles.

The schemas are a **safety net**: the retrieval step already scope-filters the
data, so most rules here just assert that filter did its job and the file shape
hasn't drifted. A violation should fail the load loudly, not write bad rows.

---

## 2. Why the module is named `xbrl.py`

`xbrl` is the token this repo already uses for this dataset everywhere:
`data/xbrl/`, the `get-xbrl` CLI command, `app/ingest/xbrl_store.py`, "XBRL data"
throughout the docs. A schemas package is conventionally split by domain
(`schemas/user.py`, `schemas/order.py`); the domain here is "xbrl".

Rejected: `companyfacts.py` (the terminology memory says never call it that),
`schemas.py` (that was the name when it was going to be a single flat module
under `app/db/` — it's a package now and needs a domain name), `xbrl_store.py`
(mirrors the producer module but reads oddly as a "store" inside `schemas/`).

---

## 3. The classes

| class | mirrors | notes |
|---|---|---|
| `CompanyFactsFile` | the whole `<TICKER>.json` document | top-level object; carries `iter_facts()` |
| `ScopeIn` | the `scope` block | `forms`, `fiscal_years` |
| `CountsIn` | the `counts` block | `taxonomies`, `concepts`, `facts` |
| `ConceptIn` | one `facts.<taxonomy>.<ConceptName>` | `label`, `description`, `units` |
| `FactIn` | one `...units.<unit>[]` element | the leaf; `start`/`end`/`val`/... |

Module-level type aliases: `Taxonomy`, `FiscalPeriod`, `FilingForm` (`Literal`s).

---

## 4. Decisions

### 4.1 `extra="forbid"` — unknown keys fail the file

Set on `_Base`, inherited by all five classes. If a file has a key we didn't
define — at any nesting level — validation fails. We generate these files
ourselves; a surprise key means a bug in the retrieval step or an un-migrated
format change, and we want to know immediately rather than silently ignore it.

### 4.2 `frozen=True` — validated instances are read-only

Set on `_Base`. After `CompanyFactsFile.model_validate(raw_dict)` returns, the
instance (and every nested `ConceptIn` / `FactIn`) rejects attribute assignment
with a `ValidationError`.

Why: the load step's job is to **read** these objects and build **separate** ORM
objects (`Fact(...)`, `Concept(...)`). It never needs to mutate a `FactIn`.
Freezing catches any code that tries, documents intent, and makes the instances
hashable.

Nuance: `frozen=True` blocks *reassigning* an attribute (`m.facts = {}`), not
mutating a container already inside it (`m.facts["dei"]["X"] = ...` is not
blocked by Pydantic). Good enough — the loader treats them as read-only by
convention and the common accident (rebinding a field) is caught.

### 4.3 `fp` / `form` / taxonomy as `Literal`

`FiscalPeriod = Literal["FY","Q1","Q2","Q3"]`, `FilingForm = Literal["10-K","10-Q"]`,
`Taxonomy = Literal["dei","us-gaap","srt"]`. Any other value fails validation.

Verified 2026-08-30 across all 20 files / 174,390 facts: `form` is only `10-K` /
`10-Q`; `fp` is only `FY` / `Q1` / `Q2` / `Q3`. The DB enums (`filing_form`,
`fiscal_period`, `taxonomy`) would reject anything else anyway — better to fail
at validation with a clear message than at INSERT. If the retrieval filter ever
regresses and lets a `10-K/A` or `Q4` through, the load stops here.

### 4.4 No format regex on `accn` / `frame`

`accn: str = Field(max_length=20)` and `frame: str | None = Field(max_length=16)`
— length caps that **mirror the DB columns** (`Filing.accession_number` is
`String(20)`, `Fact.frame` is `String(16)`), but **no pattern check**. User's
call. (For reference: all `accn` do match `^\d{10}-\d{2}-\d{6}$` and all 72
distinct `frame` values match `^CY\d{4}(Q[1-4])?I?$` in the current data — a
pattern could be added later with low risk, but isn't now.)

### 4.5 `val` precision mirrors `Numeric(30, 6)`

`val: Decimal = Field(max_digits=30, decimal_places=6)`. A value with more than
6 decimal places, or more than 30 total digits, fails validation before the DB
is touched. Observed max in the data is 4 decimal places (rates / EPS); 7+ would
signal a data problem.

**Correction (verified once the loader existed):** an earlier version of this
doc said the loader must read files with `parse_float=Decimal` to avoid `0.047`
becoming an imprecise binary float. Not needed — Pydantic's `Decimal` validator
converts a Python `float` via its *string* form (`str(500000.47) ->
Decimal('500000.47')`), not the raw binary value, so plain `json.loads(text)`
is already exact, as long as the raw dict goes straight into
`CompanyFactsFile.model_validate()` before anything else touches `val`. Verified
empirically: `Decimal(500000.47)` (raw) gives `500000.46999999997206...`, but
`FactIn.model_validate({"val": 500000.47, ...}).val` gives the exact
`Decimal("500000.47")`, with or without `parse_float=Decimal` upstream.

### 4.6 String fields mirror their DB column lengths

`ticker` → 10, `entity_name` → 150, `source_url` → 255, `label` → 512, all with
`min_length=1` where the DB column is `NOT NULL`. Same idea as 4.5: fail in the
schema with a clear message, not at INSERT. `description` has no cap (`Text`
column).

### 4.7 `counts` block re-derived, mismatch raises

`CompanyFactsFile._counts_match_tree` (`model_validator(mode="after")`) recounts
taxonomies / concepts / facts from the `facts` tree and raises `ValueError` on
any mismatch, naming the field and both numbers. The retrieval step writes those
counts in the same pass that writes the facts, so a mismatch is a real bug —
don't load the file. (All 20 real files pass this check.)

### 4.8 `fy` is NOT asserted against `scope.fiscal_years`

`fy: int = Field(ge=2000, le=2100)` is only a loose sanity bound. User's call
not to assert `fy ∈ scope.fiscal_years` yet. Noted: every `fy` across all 20
files is currently 2021–2025 (the real-store smoke test would catch a
regression), so the assertion *would* hold today — it's a candidate to tighten
once we're sure comparative-column edge cases can't make it noisy.

### 4.9 `min_length=1` on `facts` / `units` / `scope` lists

The retrieval step prunes empty taxonomies / concepts / units, so a file with
zero taxonomies, or a concept with zero units, or an empty `scope.forms` would
be a retrieval bug. Same spirit as 4.7 — assert our own invariant.

### 4.10 No silent string coercion

`_Base` deliberately does **not** set `str_strip_whitespace`. Consistent with
4.1: a stray-whitespace value (e.g. `" 10-K"`) should fail, not be quietly
cleaned into validity.

### 4.11 `AwareDatetime` for `retrieved`

The source `retrieved` field always carries a `Z` offset
(`"2026-08-30T02:30:47Z"`). `AwareDatetime` asserts it — a naive datetime fails.

### 4.12 Field names mirror the SOURCE JSON, not the ORM

`FactIn` uses `start` / `end` / `val`; the ORM uses `period_start` /
`period_end` / `value`. The schema is a faithful picture of the file; the
**loader** does the rename when it builds `Fact(...)`. Keeps "does this match the
file?" a simple visual check.

### 4.13 Class naming: `*In` suffix

`ScopeIn` / `CountsIn` / `ConceptIn` / `FactIn` — the `In` marks "inbound,
validation-only" and keeps them visually distinct from the ORM classes
(`Fact`, `Concept` in `app/db/models.py`) and from any future read DTOs
(`FactRead`, …). The top-level class is `CompanyFactsFile` (no suffix) — it
names a concrete artifact (one file on disk), not a direction of flow.

### 4.14 `iter_facts()` is a method on `CompanyFactsFile`

The nested four-deep walk (`taxonomy → concept → unit → fact`) is written once,
as a method that yields `(taxonomy, concept_name, ConceptIn, unit, FactIn)`
tuples. The loader consumes this instead of re-implementing the loop.

Earlier plan (recorded in case it resurfaces): put the walk in `app/ingest/`.
Changed when `app/schemas/` became its own package — a helper there would force
`app/ingest/` to import the schema package for no benefit. As a method it's
colocated with the data it walks and has no external dependency. If a future
loader wants it elsewhere, moving it is trivial.

---

## 5. Relationship to `app/db`

- The load step (`app/db/loader.py`, built — see `LOADER.md`) reads a file with
  plain `json.loads(text)` → `CompanyFactsFile.model_validate(...)`
  → walk `iter_facts()` → apply `ALLOWED_UNITS` (imported from `app.db`) →
  upsert `Concept`, insert `Filing` / `Fact`, maintain `is_latest` → write a
  `LoadRun`.
- `LoadRun.retrieved_at` comes from `CompanyFactsFile.retrieved`;
  `LoadRun.loaded_at` is DB `now()`.
- `LoadRun.taxonomy_count` / `concept_count` / `fact_count` can be taken straight
  from `CountsIn` (already checksum-verified by 4.7).

---

## 6. Not built yet / follow-ups

- Outbound / read DTOs — deferred until there's an API or structured CLI output.
- Possible `fy ∈ scope.fiscal_years` assertion (4.8).
- Possible `accn` / `frame` regex (4.4).
- A tighter `fy` bound than `2000..2100` once the scope-window rule is settled.

---

## 7. Data facts referenced (measured across all 20 files, 2026-08-30)

- `form` values: only `10-K`, `10-Q`.
- `fp` values: only `FY`, `Q1`, `Q2`, `Q3`.
- `accn`: all match `^\d{10}-\d{2}-\d{6}$` (20 chars).
- `frame`: 72 distinct values, all match `^CY\d{4}(Q[1-4])?I?$` (≤ 9 chars).
- `val`: up to 4 decimal places; range −3.1e11 … 6.3e13.
- `fy`: only 2021–2025.
- All 20 files pass `CompanyFactsFile.model_validate` (see
  `tests/test_xbrl_schema.py::test_real_store_file_validates`).
