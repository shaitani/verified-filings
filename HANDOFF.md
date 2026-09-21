# Handoff — read this first

Orientation for a fresh session. Not a permanent doc: everything here is
either a pointer to a `DESIGN.md` that sits next to the code, or forward plan
that lives nowhere else yet. Where this file and a `DESIGN.md` disagree, the
`DESIGN.md` is right.

---

## 1. What this is

A system that answers natural-language questions about SEC financial data with
figures that are actually correct, and that refuses — loudly — when it cannot.
The corpus is 20 US filers, 5 fiscal years, ~174,000 facts from the SEC's XBRL
data endpoint, loaded into PostgreSQL.

The animating concern: **a plausible wrong number is worse than a refusal.**
Nearly every design decision traces back to that. XBRL is far less uniform than
it looks, and most of the hard problems are data problems, not code problems.
They are catalogued with measurements in [`PITFALLS.md`](PITFALLS.md) — read it
before trusting any figure.

## 2. Data flow, end to end

```
SEC XBRL data endpoint
   ↓  app/ingest/           fetch, rate-limit, scope to 10-K/10-Q + 5 FYs
data/xbrl/<TICKER>.json     curated per-company files (gitignored)
   ↓  app/schemas/xbrl.py   validate the file shape
   ↓  app/db/loader.py      upsert Concept, insert Filing/Fact, maintain
                            is_latest; merge sic_* from sic_numbers.json
PostgreSQL (xbrl schema)    company / filing / concept / fact / load_run
   ↓  app/db/embedder.py    embed every Concept (nomic-embed-text via Ollama)
══════════════════════════ everything above is BUILT and stable ══════════════
   ↓
[producer LLM]              question → QueryIn                    ← NOT BUILT
   ↓  app/schemas/query.py
QueryIn                     question + typed elements + optional shape
   ↓  app/semantic/query_mapper.py    ── reads DB as vf_query_mapper_role
QueryPlan                   concrete coordinates, caveats, cardinality
   ↓  app/retrieval/                                              ← NOT BUILT
       build_prompt(plan) → text to send Qwen              no DB
       generate(plan)     → calls Qwen, returns SQL        no DB
       validate(sql)      → raises, or returns             no DB  ← LOAD-BEARING
       execute(sql, plan) → ResultSet
                ── reads xbrl.reported_fact as vf_retrieval_role
   ↓  app/schemas/result.py           ── BUILT
ResultSet                   rows + citations + verdict + notes
   ↓
[presenter LLM]             renders, cites, discloses caveats     ← NOT BUILT
```

Two Docker Postgres containers: `db` (real) and `db-test` (tests). Ollama
serves both the embedding model and Qwen. See `docker-compose.yml`, and
[`BOOTSTRAP.md`](BOOTSTRAP.md) for bringing it all up from nothing — the roles
and the model pull do **not** come back with the schema.

**Qwen never touches the database.** It is a language model behind an HTTP
endpoint: it takes text and returns text. It has no driver, no credentials and
no route to PostgreSQL. `execute()` runs what it wrote. That separation is the
main protection in the design, and `validate()` is what makes it real — one
statement per execution, parsed, must be a `SELECT`, no leading `SET`, with a
`LIMIT` appended and verified.

**Qwen does not write all of the SQL.** Retrieval — getting the values a
`Binding` names — is bounded: four shapes (`direct`/`residual` ×
`instant`/`duration`, plus operand arithmetic). That half should be
deterministic code. Qwen writes the layer *above* it: ranking, growth, ratios
across companies, filters on computed values. Measured on the eval set, 20 of
45 answerable questions (44%) need that layer.

## 3. The core idea: QueryIn → QueryPlan

**`QueryIn`** speaks the *user's* language — `"revenue"`, never
`us-gaap:Revenues`. Elements are a discriminated union on `kind` — `metric` /
`company` / `company_group` / `period` — because each needs a different
resolver. Embedding search over "Apple" returns noise. (A `qualifier` kind
existed and was removed: nothing resolved it, so a producer emitting one had
its intent silently dropped. Emitting one now raises.)

**`QueryPlan`** is what the mapper resolves that into, and it is the real
artifact of this project. It carries concrete `concept_id`s, per-company date
windows, units, instant-vs-duration, whether a value needs subtracting, proof
that the facts exist, and caveats that must reach the reader. The design intent
is that **the SQL step has nothing left to guess**.

Full rationale: [`app/schemas/DESIGN.md`](app/schemas/DESIGN.md) §8 and
[`app/semantic/DESIGN.md`](app/semantic/DESIGN.md).

### The three resolvers, in order

1. **Companies** — deterministic. The derived lexicon
   (`company_aliases.json`, §4.5) first, then ticker / entity name. Naming no
   company at all means every loaded filer; a company element that *fails* does
   not widen the scope.
2. **Periods** — into concrete date windows *per company*. Never fiscal-year
   integers; `Filing.fiscal_year` is provenance, not the period a number
   describes (PITFALLS §1.1 — this silently returned FY2022 revenue for an
   FY2024 query before it was fixed). Q4 is synthesized, because no US filer
   files one.
3. **Metrics** — curated alias → embedding fallback → **coverage check against
   the facts**, which is the arbiter of both.

Coverage is the load-bearing idea: a candidate with no facts for the requested
windows is never bound, whatever its similarity score. Zero rows from a
well-formed query is indistinguishable from "the company reported nothing", so
the binding has to be *proven* before it is made.

### The alias layer

[`app/semantic/metric_aliases.yaml`](app/semantic/metric_aliases.yaml) is
curated accounting judgment as **data, not code** — currently 47 metrics and
182 surface forms. Each operand slot lists *alternatives in preference order*
and coverage picks per company, which is how filer divergence resolves without
per-company tables: Apple binds
`RevenueFromContractWithCustomerExcludingAssessedTax`, NVIDIA binds `Revenues`,
from one entry.

An entry does exactly one of three things, and the split matters more than the
count: **39 resolve**, **4 ask** (`clarify` — `profit_margin`, `profit`,
`cash_flow`, `debt`), **4 decline** (`unavailable` — `stock_price`,
`market_cap`, `segment_revenue`, `gross_revenue`). An entry may also attach a
per-concept `caveat`, which becomes a `narrower_than_asked` note when a
fallback answers less than the phrase asked for; `total_debt` is the one that
does.

Two lessons from curating it, both in `app/semantic/DESIGN.md` §8a–§8b:

- **"Nothing honest to map it to" argues for `unavailable`, not for silence.**
  An unlisted term does not fail safely — it falls to the embedding search,
  which always returns *something*. `gross_revenue` sat uncurated for that
  reason and came back as `GrossProfit` at 0.812.
- **Similarity cannot be trusted to separate right from wrong.** Measured over
  221 labelled cases, the band just above the old binding bar was 5% precise.
  The bar is now 0.75 and there is a 0.65 floor below which an element is
  `unresolved` rather than `ambiguous`. That makes the path less bad, not safe.
  The fix for a term people keep asking is a curated entry.

**The user is not an accountant.** Curating this file is collaborative, and it
is the main lever for answer quality. `alias_gap` is tagged zero times in the
eval set today; adding a filer will reopen gaps, since the coverage counts
noted inline are measured against the current 20.

### Two things the plan tracks that are easy to miss

`ResultSpec` says how much data the answer needs — `shape`, the `axes` it
varies along, and `row_count`, which retrieval must not come in under. A chart
of 3 companies over 12 quarters is 36 rows, and nothing else in the plan says
so (schemas DESIGN §8.14).

`granularities` tracks annual vs quarterly separately, because a 363-day value
and a 90-day value are not comparable points — mixing them raises a plan-level
`mixed_granularity` note (§8.18).

## 4. What is NOT built — the forward plan

In the order I would do it.

### 4.1 Result contract — BUILT

`app/schemas/result.py`. `ResultRow` is the column list generated SQL must
project; `ResultSet` is what the presenter reads — annotated rows, citations
keyed by binding, a verdict, and the notes. `RESULT_COLUMNS` is the projection
`validate()` will check against.

Citation is a **lookup, not a column**: a row carries no `concept_id`, and
`QueryPlan.binding_for()` resolves `(element_id, cik, fiscal_year,
fiscal_period)` to exactly one binding, raising when two claim a cell. The
refuse rule lives on `ResultVerdict.is_answerable` — answer only when the
shortfall was already disclosed by the plan.

### 4.2 A narrow query surface (view) — BUILT

`xbrl.reported_fact` (migration `3fcc714d6050`), applied to both databases. It
bakes in `is_latest` and the three joins, and deliberately exposes **no**
`fiscal_year` / `fiscal_period`: that column on `filing` is provenance, not the
period a fact describes, and having one in reach is an invitation to PITFALLS
§1.1. Period labels reach the result from the plan instead.

The grant half is done with it: `vf_retrieval_role` now holds `SELECT` on the
view and nothing else. Verified — it is refused on `fact`, `filing`, `company`,
`concept` and `load_run`.

Both halves, and every measurement behind them, are in
[`app/retrieval/DESIGN.md`](app/retrieval/DESIGN.md).

### 4.3 The retrieval layer — `app/retrieval/` — BUILT

All four functions, plus `answer(plan)` which runs generate → validate →
execute. 
**Qwen writes the SQL, all of it.** Each function does one job and only it
does that job: `build_prompt` assembles text and writes no SQL, `generate` is
the only thing that talks to Qwen, `validate` judges without running or
*modifying* (it returns its input byte-identical), `execute` is the only thing
that touches the database.

Measured with `qwen2.5-coder:7b`, the layer currently **refuses more than it
answers** — see `app/retrieval/DESIGN.md` §4.3 for the three failure modes and
what each one cost. Everything failed safe; nothing returned a wrong number.
The open question is the model, not the structure.

`app/retrieval/` is the slot the original brief
reserves ([`sec-retriever.md`](sec-retriever.md) §3); the name is a leftover
from an earlier design that meant BM25-plus-vectors over document chunks, but
it is the right home and inventing a new directory should be a deliberate
decision, not a drive-by one.

`validate()` is the part that cannot be skipped, and it is built. It parses
with `pglast` — libpg_query, PostgreSQL's own grammar — so there is no gap
between how it reads a statement and how the server will. It refuses anything
whose tree holds a non-`SELECT` statement (a `DELETE` inside a CTE passes a
top-of-tree check), any `set_config` call (`SET` in expression form, the real
USERSET escape), any relation but the view, any projection that is not exactly
`RESULT_COLUMNS`, and any missing or over-large `LIMIT`. Full list in
`app/retrieval/DESIGN.md` §7.

### 4.4 Composition convention — decided

**One statement per question.** It falls out of the contract: because a row
carries `element_id`, several bindings coexist in one result set, and the
plan's coordinates are injected as a `VALUES` list the statement joins against
— so per-company concept divergence is data rather than SQL cleverness. The
open edge is that mixing `direct` and `residual` bindings needs a `UNION ALL`
of two blocks. See `app/retrieval/DESIGN.md` §4.

### 4.5 The query-object producer — not started

Something has to turn a question into a `QueryIn`. Deliberately outside this
project so far. What it still owes:

- **Pinning metric-level ambiguity.** "How much money was made" is revenue or
  net income — a different ambiguity from the concept-level one the mapper
  handles.
- **§4.6 below.**

Two burdens it *no longer* carries. Company names resolve through
`company_aliases.json`, derived from SEC data by `app/ingest/alias_index.py`
and refreshed on every `get-submission`: Google, Facebook, Bank of America,
AMD, Johnson and Johnson and United Health all missed before it, and share
classes work ("GOOG" is Alphabet). And a question naming no company now means
every loaded filer.

**Historical tickers are not obtainable**, and `alias_index.py` says so rather
than implying otherwise. Both SEC feeds give only the current symbol; the
in-house source would be `dei:TradingSymbol` from each cover page, but the XBRL
data endpoint returns only numeric facts and that is a string. A question using
a retired symbol (FB rather than META) will not resolve. This never threatens
correctness because **the cik never changes** — a missing alias costs a
refusal, never a wrong company.

### 4.6 Nothing checks that the elements express the question

The last plausible-wrong-answer in the eval set, and the only one that produces
a confident number for a question nobody asked.

**q026**, "Did any of these companies restate its revenue?", comes back
`is_complete` with a 100-row revenue series and no caveat. Every element
resolved — "revenue", twenty companies, five years — so by every measure the
mapper has, the plan is perfect. It answers a different question.

The mapper cannot catch this and arguably should not: it is handed elements,
not a question, and the elements are fine. `QueryIn.question` carries the
original text so *something* could compare the two, but the natural home is the
producer — the layer that dropped "restate" silently, exactly as the removed
`qualifier` kind used to.

Shapes that fail this way: restatement, causality ("why did margins fall"),
counts of filings, anything about the *filing* rather than the figures.

Open decision for when the producer is built: should an unrepresented span of
the question raise, warn, or be ignored? Ignoring it is today's behaviour and
the worst of the three.

### 4.7 Database roles — built, and what is left

`uv run python -m app.db.roles` creates two read-only logins; `--check` reports
them. `app/db/roles.py` explains every grant.

| role | used by | difference |
|---|---|---|
| `vf_query_mapper_role` | `app/semantic/query_mapper.py` | needs `concept.embedding` for the pgvector fallback, so it also gets `public` on its `search_path` |
| `vf_retrieval_role` | `app/retrieval/` | one relation only: `SELECT` on the `xbrl.reported_fact` view, nothing on any base table; no vector operators; 4 connections |

Neither can reach `load_run`. The **grants** are a real boundary — verified
with `default_transaction_read_only` deliberately off. The **session settings**
are not; see §4.3.

Two traps. The `search_path` decides whether pgvector is reachable: it installs
into `public` and operators resolve through the path, so dropping `public` made
`embedding <=> $1` fail with "operator does not exist" and took the concept
search down with it. And provisioning **revokes before it grants**, so the
`RoleSpec`s are authoritative — remove a table from one and the next run
removes the privilege.

### 4.8 Sector grouping — done, except the office

`sic_code` and `sic_description` are loaded for all 20 filers across 14 codes,
keyed on cik rather than ticker. "Which companies in semiconductors" resolves
to AMD, INTC, MU, NVDA.

`sic_office` has **no source at all**: `sic_numbers.json` carries code and
description only, and the SEC assigns review offices by SIC *range*, a mapping
this project does not have. Its refusal says exactly that. Populating it is a
separate decision.

### 4.9 Known smaller gaps

- **Metric groups are NOT a thing.** "cash flow: operating, investing,
  financing" is several `MetricElementIn` along a metric axis, which works
  today. Whether anything knows "balance sheet totals" means a particular list
  belongs to the *producer*. Briefly designed as a "bundle" before being
  recognised as nothing new — schemas DESIGN §8.20. Do not re-invent it.
- Restatements are picked correctly by `is_latest` but never *disclosed*
  (PITFALLS §2.2). The `Note` channel would carry it, and
  `narrower_than_asked` proved that channel extends cleanly.
- A derived *and* residual binding reports only the lead operand's components.
  The coverage check is complete; the reported `components` under-describes it.
- Alias curation for recall: interest income, treasury stock, deferred revenue,
  operating expenses, depreciation, accounts receivable/payable, retained
  earnings.
- **Q4 is no longer special anywhere but the view.** `PeriodResidual`,
  `ResolvedPeriod.residual_of`, `Binding.period_rule`, `Coverage.components`
  and the prompt's residual instructions are all gone;
  `xbrl.reported_fact` synthesizes the fourth quarter and the mapper proves
  coverage against that same view. Cross-checked before removal: 275 of 275
  residuals agreed across 20 companies, 4 metrics, 5 years.
- **Multi-operand bindings still cannot be rendered**, though no longer for
  the unit reason: `Binding.operand_unit` now carries what the facts are filed
  in (`USD` behind a `pure` gross margin) and `fact_unit` is what the join
  keys on. What is left is rendering the *arithmetic* — one cell becomes one
  row per operand and the expression has to be evaluated over them. Six of 47
  curated metrics.
- **No eval runner still.** 56 questions with expected outcomes and no way to
  run them. Now that `answer()` exists, the only missing piece is a `QueryIn`
  per question — which is the producer's job (§4.5). Until then "is this model
  good enough?" cannot be answered.

### Explicitly deferred by the user

**The 10-K/A gap** (PITFALLS §3.4). Ingest fetches only `10-K` and `10-Q`, so
amended filings — the vehicle for material restatements — are never seen. The
user has decided not to address this now. Do not reopen it unprompted.

## 5. Working conventions

The user's memory file carries these; they are repeated because violating them
has caused real friction.

- **Never `git commit` unless asked in that turn.** One approval is not
  standing permission.
- **Keep commit messages to a couple of sentences.** Rationale belongs in the
  relevant `DESIGN.md`, not in git history where nobody can edit it.
- **One step per turn.** Do the thing, report, stop. Offer the next step as a
  question rather than proceeding.
- **Match repo line endings — LF everywhere** (except `LOADER.md`, already
  CRLF). Python text-mode writes on Windows silently convert to CRLF; pass
  `newline="\n"` and check `git diff --stat` against `--ignore-all-space`
  after any scripted edit.
- **Verify empirically before asserting.** Every number in `PITFALLS.md` came
  from a probe against the real database. The user notices unfounded
  confidence and will call it out — correctly.
- Terse output. No long explanations unless asked.

Run everything through `uv run`. Tests: `uv run pytest -q` (338 passing).
Lint: `uv run ruff check app/ tests/ evals/`.

## 6. Verifying things yourself

Ad-hoc SQL against `xbrl.fact` joined to `concept` / `filing`. Patterns worth
reusing, from PITFALLS §5:

- **A filing's own window:** `DISTINCT ON (company, fiscal_year,
  fiscal_period)` ordered by `period_end DESC, (period_end - period_start)
  ASC` — shortest span breaks ties so a discrete quarter beats the
  year-to-date.
- **Restatements:** group by `(company, concept, unit, period_start,
  period_end)` and count distinct values. Omitting `period_start` is a trap —
  it compares 3-month against 9-month windows sharing an end date.
- **Concept drift:** per company per alias slot, the fiscal years each
  alternative covers. Any slot where no single alternative covers the union is
  a drift case.

Good live smoke test — exercises chart cardinality, Q4 residuals, concept drift
and calendar misalignment at once:

> "Show me visually how much money was made from 2023 to 2025 by quarter, for
> Google, Apple and Nvidia"

Expected: `shape=series`, `axes=['company','period']`, `row_count=36`, **four
bindings** (three filers, and Alphabet changes revenue tags mid-range) and a
plan-level `period_misalignment`.

It was eight bindings until the view took over the Q4 subtraction: a series
used to split into direct and residual halves, because one `period_rule` had
to be true of every period in a binding. Only a genuine tag change splits one
now.

For a broader check, `evals/questions.yaml` holds 56 questions with their
expected outcome, and `uv run python evals/summarize.py` prints the
distribution. There is no *runner* — executing the set means writing a
`QueryIn` per question by hand, which is the producer's job (§4.5). Doing
that in a scratch file is how several of the bugs fixed this session were
found, including two the unit tests did not catch.

## 7. Doc map

| file | what |
|---|---|
| [`BOOTSTRAP.md`](BOOTSTRAP.md) | bringing everything up from nothing, and what a volume wipe destroys |
| [`PITFALLS.md`](PITFALLS.md) | every known data hazard, measured, and whether it is handled |
| [`app/schemas/DESIGN.md`](app/schemas/DESIGN.md) | §8 = the query schemas, decision by decision |
| [`app/semantic/DESIGN.md`](app/semantic/DESIGN.md) | the curated alias layer |
| [`app/db/DESIGN.md`](app/db/DESIGN.md) | ORM models, layout |
| [`app/retrieval/DESIGN.md`](app/retrieval/DESIGN.md) | the result contract, the view, and the refuse rule |
| [`app/db/roles.py`](app/db/roles.py) | the two read-only roles, and which half of them is a real boundary |
| [`LOADER.md`](LOADER.md) | the load step |
| [`ALEMBIC.md`](ALEMBIC.md) | migrations — note step 5, the test database is NOT migrated automatically |
| [`evals/README.md`](evals/README.md) | eval tag vocabulary, how to add questions |
| [`sec-retriever.md`](sec-retriever.md) | the original project brief; §3 is the reserved layout |
