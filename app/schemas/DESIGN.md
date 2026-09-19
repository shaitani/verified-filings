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

`query.py` (section 8) and `aliases.py` (section 9) serve the query mapper.

`xbrl.py` validates **one curated XBRL-data file** (`data/xbrl/<TICKER>.json`,
written by the retrieval step in `app/ingest/xbrl_store.py`) on the way IN,
before the **load step** (`app/db/loader.py`, see `LOADER.md`) turns it into
`app.db` ORM rows.

- **No read DTOs yet.** Nothing here mirrors ORM rows on the way out — those
  come if/when there's an HTTP API or structured CLI output, as separate
  classes with `from_attributes=True`. Never reuse the `*In` models for output.
  (`query.py`'s `QueryPlan` *is* outbound, but it is computed, not a projection
  of ORM rows, so `from_attributes` doesn't apply — see §8.)
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

- Read DTOs projecting ORM rows — deferred until there's an API or structured
  CLI output. (`query.py`'s outbound models are computed, not projections.)
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

---

## 8. `query.py` — the query mapper's two ends

Added 2026-09-18 alongside `app/semantic/query_mapper.py`. `QueryIn` is what a user's
question looks like once some producer (an external LLM today) has parsed it;
`QueryPlan` is what the mapper resolves that into, and what the SQL-generating
model reads. Both live in one module because they are two ends of one contract
— changing one almost always means changing the other.

### 8.1 `QueryIn` speaks the user's language, never XBRL

`text` is `"revenue"`, never `us-gaap:Revenues`. Translating between the two is
the mapper's entire job, so an XBRL identifier appearing on the inbound side
means the boundary has leaked and the producer has taken on work it has no
business doing (it cannot see the database, and filer-specific tagging is not
knowable from the question text).

### 8.2 Elements are a discriminated union on `kind`

`metric` / `company` / `period` / `qualifier`, dispatched by Pydantic on
`kind`. A flat "list of things being asked about" was the original sketch and
it does not survive contact: each kind needs a *different* resolver, and
running embedding search over `"Apple"` returns noise. The discriminator is
what routes each element correctly, and `extra="forbid"` then makes a
mislabelled element (a `ticker` on a `metric`) fail instead of being silently
ignored.

### 8.3 A binding is keyed on `(element_id, company_cik)`

Filers tag the same business concept differently — Apple reports revenue as
`RevenueFromContractWithCustomerExcludingAssessedTax`, others as `Revenues`.
So one `"revenue"` element legitimately resolves to *different* concepts for
different companies, and a mapping shaped `element -> concept_id` structurally
cannot say that. `Binding.company_cik = None` means "holds for every cik in
`filters`"; a per-company binding is the override.

### 8.4 `concepts` is a list, with an `expression` over it

Some metrics exist as no single `Concept` row: gross margin is
`GrossProfit / Revenues`, free cash flow is operating cash flow minus capex. A
1:1 `element -> concept` assumption would have to be torn out the first time
one of those is asked for, so the list is there from the start. The ordinary
case is one operand and the default `expression` of `"c0"`.

`expression` is a **string**, not a parsed tree. Its only consumer today is a
language model that reads it as text, and a real AST can replace it the moment
something needs to *evaluate* it. A `model_validator` keeps it honest: every
`cN` it references must be an operand that was actually bound.

### 8.5 Company ambiguity has no home in `Ambiguity`

`Ambiguity.candidates` is `ConceptRef`-shaped, because concept ambiguity is the
dominant case and the one the curated alias layer exists to settle. A company
element matching several filers therefore reports through `Unresolved` with a
reason naming the colliding ciks, which reads worse than it should.

Accepted deliberately rather than generalising `Ambiguity` over element kinds:
the generalisation costs a layer of indirection on every binding to serve a
case that has not been hit yet. Revisit if company collisions turn out to be
common in practice.

### 8.6 `coverage` is evidence, and it outranks similarity

`Coverage.fact_count == 0` means a binding is wrong. This matters more than it
looks: a well-formed query over a wrongly-bound concept returns zero rows, and
zero rows is indistinguishable from "the company reported nothing" — so the
user-facing model cheerfully reports a false negative. Carrying the count makes
that impossible to miss, and makes a verified binding tellable from a guess.

Consequence for the resolver (documented on `_resolve_metrics`): coverage
filters *before* ranking, and a candidate at 0.91 similarity with no facts
loses to one at 0.78 with twenty.

### 8.7 `unresolved` / `ambiguous` are fields, not exceptions

A half-resolved plan is a normal outcome — one element of five failing should
not discard the four that worked. `map_query` never raises on an unresolvable
element; `QueryPlan.is_complete` is the caller's go/no-go signal.

### 8.8 `resolved_by` is there to be measured

Every binding records whether the alias layer or the embedding search produced
it. As the curated YAML absorbs cases, the embedding share should fall — that
ratio is the honest maturity metric for the mapping layer, and it costs one
`Literal` field to have.

### 8.9 `FilingForm` / `Taxonomy` are imported from `xbrl.py`

Not redefined. They mirror the same DB enums, and two copies would drift.

**Superseded in part (see §8.12):** this section originally also imported
`FiscalPeriod` for `PeriodElementIn.fiscal_period`, and called the resulting
rejection of `fiscal_period="Q4"` a useful side effect. It wasn't — it made
every Q4 question unanswerable. Period *labels* a question may use are now
`QueryFiscalPeriod`, defined in `query.py`; only the storage-level fields still
borrow `xbrl.py`'s four-value Literal.

### 8.10 `extra="forbid"` matters more here than in `xbrl.py`

§4.1's reasoning was "we generate these files". Here the producer is a language
model, which is *less* controlled — but the conclusion is the same and
stronger: a silently-dropped key the model believed it was sending is far
harder to debug than a loud `ValidationError` it can be shown and asked to
correct.

### 8.11 Periods are date windows, not fiscal-year integers

`PlanFilters` carries `periods: list[ResolvedPeriod]` — `(company_cik,
fiscal_year, fiscal_period, period_start, period_end)` — and deliberately has
no `fiscal_years: list[int]`.

**Why the integer version was wrong.** `Filing.fiscal_year` is the fiscal year
*of the filing*, and a 10-K carries two years of comparative columns. Combined
with `Fact.is_latest` (which keeps the most recently *filed* copy of a period),
filtering facts through `filing.fiscal_year` selects by **provenance**, not by
which period a number describes. Measured: Apple's FY2024-filed 10-K holds the
`2021-09-26 → 2022-09-24` duration — FY2022's window — as its surviving
`is_latest` row, because the FY2025 10-K later superseded Apple's copies of
FY2023 and FY2024. A query filtered on `fiscal_year = 2024` would have returned
FY2022 revenue and said nothing was wrong.

**Why the window can't be computed from the label.** Fiscal years are named
independently of the dates they cover. J&J's FY2021 runs `2021-01-04 →
2022-01-02`; two of the store's 100 annual filings end in a different calendar
year than their `fiscal_year`. Several filers (JNJ, NVDA, AAPL, QCOM) use
52/53-week calendars, so quarters measure 83–97 days rather than 91. Any rule
deriving dates from the year number is wrong for those.

**How the window is resolved.** `query_mapper._load_windows` takes the label
from `Filing.fiscal_year` and the dates from the facts: a filing's own window
is its duration fact with the **latest `period_end`** (comparatives describe
earlier periods), breaking `period_end` ties toward the **shortest span** (what
separates a 10-Q's discrete quarter from the year-to-date duration filed beside
it). Verified across the whole store: 100/100 annual and 298/298 quarterly
filings resolve to exactly one unambiguous window.

**Consequences.** Windows are per-company, because one "FY2024" is Jul→Jun for
Microsoft and Oct→Sep for Apple — so cross-company period comparison is
comparing different calendar spans, which the plan now makes visible rather
than hiding behind a shared integer. A filing whose own window can't be
identified (no duration fact of the expected length) is reported as
`Unresolved` rather than guessed at, and `last_n_years` counts back from the
newest year that actually *resolved* — not the newest `Filing` row — so
"the last 5 years" means five years the database can answer for.

This is also the groundwork for Q4: a derived fourth quarter is the annual
window minus the 9-month year-to-date window sharing its `period_start`, which
is only expressible once periods carry dates.

### 8.12 Q4 is derived, and the schema makes the derivation provable

No US filer files a fourth quarter — the store holds 100 `FY`, 99 `Q1`, 99
`Q2`, 100 `Q3` and **zero** `Q4` filings. Q4 is therefore not an edge case to
special-case per company; it is a derivation that must happen every time
anyone asks for one.

**Query vocabulary is wider than storage vocabulary.** `PeriodElementIn` uses
`QueryFiscalPeriod` (`FY`/`Q1`/`Q2`/`Q3`/`Q4`) while `Filing.fiscal_period`
stays at four values. An earlier version of §8.9 called rejecting `Q4` at the
boundary "correct" — that was wrong. It conflated *no Q4 rows exist* with *Q4
is not askable*, and translating between those is the mapper's entire purpose.

**The derivation.** For duration (flow) facts,
`Q4 = annual window − nine-month year-to-date window sharing its start`. Both
components open on the fiscal year start, which is what lets the SQL step join
them without guessing — hence `PeriodResidual.shared_start` as one field rather
than two that happen to agree. For **instant** facts there is no arithmetic at
all: the fiscal-year-end balance *is* the Q4-end balance.

**Why the rule lives on `Binding`, not `ResolvedPeriod`.** Whether Q4 needs
subtracting depends on the *concept*, not the period: at the same Q4, revenue
is a residual and total assets is a plain instant read. So `ResolvedPeriod`
always supplies the component windows for a Q4 and `Binding.period_rule`
decides whether to use them.

**The mapper needs no extra query.** Q4 runs from the day after Q3 closes to
the fiscal year end, and Q3's discrete window already ends exactly where the
nine-month term does — so `_with_derived_q4` builds it from windows §8.11
already resolved. A company missing either component gets no Q4 key at all.

**Why `Coverage.components` exists.** Verified against the store: NVIDIA
carries four *annual* facts under the revenue concept Apple and Microsoft use
quarterly, and zero nine-month ones. A Q4 binding to that concept passes a
concept-level `fact_count > 0` check and then cannot be computed. Worse, a
subtraction with a missing term does not error — drop the nine-month value and
"Q4" silently becomes the whole year. `Binding` therefore *refuses* to validate
a `residual` binding whose components aren't each proven non-empty, which makes
that bug unrepresentable rather than merely discouraged.

**Verified end to end.** The mapper's residual windows drive SQL that joins on
exact plan-supplied dates — no day-span ranges — returning FY2025 Q4 revenue of
102.5B (AAPL), 76.4B (MSFT) and 39.3B (NVDA), all matching reported figures.
Note those are three different calendar quarters (Jun–Sep, Apr–Jun, Oct–Jan)
carrying the same label, which is why §8.11's per-company windows had to land
first.

---

## 9. `aliases.py` — the curated alias layer

Added 2026-09-18 with the metric resolver. `app/semantic/aliases.yaml` holds the
accounting judgment; `app/schemas/aliases.py` validates it; `app/semantic/
aliases.py` indexes it. It is **data, not code**, so extending it is an edit
rather than a deploy — and it is the artifact a non-accountant and a model can
sensibly co-author, which is the whole reason it exists.

### 9.1 `terms` are operand slots, each holding ordered alternatives

`terms[i]` is operand `c{i}` of `expression`; each slot lists *alternative*
concepts in preference order, not concepts to combine. Both axes from §8.4 are
therefore expressible in one shape: `revenue` is one slot with three
alternatives, `free_cash_flow` is two slots of one alternative each with
`expression: c0 - c1`.

### 9.2 Filer divergence resolves from data, not per-company curation

This is the payoff. `revenue` lists `RevenueFromContractWithCustomerExcluding
AssessedTax` then `Revenues`; the resolver keeps, per company, the first one
whose facts actually cover the requested windows. Measured live: Apple and
Microsoft bind the first, NVIDIA binds the second, from a single entry with no
per-company table to maintain. §8.3's `(element, company)` binding key is what
makes that representable.

The alternative — a curated per-filer mapping — would have needed 20 rows per
metric, gone stale the moment a filer re-tagged, and had no way to notice.

### 9.3 Preference order *is* the disambiguation, so aliases never go ambiguous

A curated list is already ranked by someone who thought about it. So on the
alias path the first survivor of the coverage filter wins outright and no
`Ambiguity` is ever emitted. The embedding path has no such ranking, so several
survivors there is a real tie and gets reported — deciding it on a hair of
cosine distance is exactly the noise §8.6 argues against.

Observed: "dividends paid" (no alias) returns `PaymentsOfDividends` at 0.757
and `CommonStockDividendsPerShareDeclared` at 0.686, both covered. One is USD
and one is per-share — genuinely different questions, correctly refused rather
than guessed. That pair is also a good candidate for the next alias entry;
that is the intended feedback loop.

### 9.4 Surface forms must be unique, at two levels

The schema rejects duplicate raw strings; `AliasIndex` additionally rejects
forms that only collide once normalized (lowercased, punctuation dropped,
underscores folded to spaces). Both exist because lookup resolving by dict
order is precisely the quiet wrongness this layer is meant to remove.

Normalizing underscores means a metric key like `free_cash_flow` answers to
"free cash flow" without anyone remembering to add it as a synonym. Punctuation
is dropped rather than replaced, so "R&D" folds to `rd` and not to `r d` — the
file lists the spelled-out forms separately.

### 9.5 Coverage still outranks the file

An alias entry is a hypothesis, not an answer. Every candidate it proposes goes
through the same coverage filter as an embedding candidate, and a curated
concept with no facts for the requested windows loses to a later alternative
that has them. Two entries in the file exist purely to make that testable
against the fixture (`tests/fixtures/aliases_fake.yaml`).

### 9.6 Known rough edge: `unit` on a derived binding

`Binding.unit` is read from the first operand's facts. For `expression: "c0"`
that is exactly right; for `gross_margin` (`c0 / c1`) the result is
dimensionless and "USD" describes the operands, not the answer. Left as is
rather than inventing a unit algebra: `expression` is right there for a
consumer to notice, and a real unit system should wait until something needs
one. Worth fixing before any display layer formats these values.

### 9.7 What the resolver deliberately does not do

No fallback arithmetic. `gross_profit` maps only to `us-gaap:GrossProfit` even
though just 9 of 20 filers tag it, rather than quietly computing revenue minus
cost — a computed subtotal is a different number from the filer's own, and
silently substituting one for the other is the class of error this whole layer
exists to prevent. When a filer does not report it, "unresolved" is the honest
answer.
