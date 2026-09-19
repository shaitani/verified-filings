# Handoff — read this first

Temporary orientation document for a fresh session. Not part of the permanent
docs; delete it once its contents have been absorbed or moved.

Everything factual here is cross-referenced to a permanent doc. Where this file
and a `DESIGN.md` disagree, the `DESIGN.md` is right — it sits next to the
code. What is *only* here is the forward plan and the reasoning behind the
Qwen decision, which live nowhere else yet.

---

## 1. What this is

A system that answers natural-language questions about SEC financial data with
figures that are actually correct, and that refuses — loudly — when it cannot.
The corpus is 20 US filers, 5 fiscal years, 174,390 facts from the SEC's XBRL
data endpoint, loaded into PostgreSQL.

The animating concern throughout: **a plausible wrong number is worse than a
refusal.** Nearly every design decision on this project traces back to that.
XBRL is far less uniform than it looks, and most of the hard problems are data
problems, not code problems. They are catalogued with measurements in
[`PITFALLS.md`](PITFALLS.md) — read that before trusting any figure.

## 2. Data flow, end to end

```
SEC XBRL endpoint
   ↓  app/ingest/          fetch, rate-limit, scope-filter to 10-K/10-Q + 5 FYs
data/xbrl/<TICKER>.json    curated per-company files (gitignored)
   ↓  app/schemas/xbrl.py  validate the file shape
   ↓  app/db/loader.py     upsert Concept, insert Filing/Fact, maintain is_latest
PostgreSQL (xbrl schema)   company / filing / concept / fact / load_run
   ↓  app/db/embedder.py   embed every Concept (nomic-embed-text via Ollama)
   ═══════════════════════ everything above is BUILT and stable
   ↓
[external LLM]             parses a question into a QueryIn        ← NOT BUILT
   ↓  app/schemas/query.py
QueryIn                    question + typed elements + optional shape
   ↓  app/semantic/query_mapper.py
QueryPlan                  concrete coordinates, caveats, cardinality
   ↓
[SQL emitter]              plan → SQL                              ← NOT BUILT
   ↓
rows + plan notes
   ↓
[user-facing LLM]          renders, cites, discloses caveats       ← NOT BUILT
```

Two Docker Postgres containers: `db` (real) and `db-test` (tests). Ollama runs
the embedding model. See `docker-compose.yml`.

## 3. The core idea: QueryIn → QueryPlan

**`QueryIn`** speaks the *user's* language — `"revenue"`, never
`us-gaap:Revenues`. Elements are a discriminated union on `kind`
(`metric` / `company` / `period` / `qualifier`), because each needs a different
resolver. Embedding search over "Apple" returns noise.

**`QueryPlan`** is what the mapper resolves that into, and it is the real
artifact of this project. It carries concrete `concept_id`s, per-company date
windows, units, instant-vs-duration, whether a value needs subtracting, proof
that the facts exist, and caveats that must reach the reader. The design intent
is that **the SQL step has nothing left to guess**.

Full rationale: [`app/schemas/DESIGN.md`](app/schemas/DESIGN.md) §8 (fifteen
numbered decisions with their reasoning) and
[`app/semantic/DESIGN.md`](app/semantic/DESIGN.md) for the alias layer.

### The three resolvers, in order

1. **Companies** — deterministic lookup on ticker / entity name.
2. **Periods** — into concrete date windows *per company*. Never fiscal-year
   integers; `Filing.fiscal_year` is provenance, not the period a number
   describes (PITFALLS §1.1 — this one silently returned FY2022 revenue for a
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
curated accounting judgment as **data, not code** — 22 metrics, 87 surface
forms. Each operand slot lists *alternatives in preference order*, and coverage
picks per company. That is how filer divergence resolves without per-company
tables: Apple and Microsoft bind
`RevenueFromContractWithCustomerExcludingAssessedTax`, NVIDIA binds `Revenues`,
from one entry.

**The user is not an accountant.** Curating this file is a collaborative task
and the main lever for improving answer quality.

### Element kinds

`metric` / `company` / `company_group` / `period`. A `qualifier` kind existed
and was **removed** — nothing ever resolved it, so a producer emitting one had
its intent silently dropped (DESIGN.md §8.19). Emitting one now raises.

### Two things the plan tracks that are easy to miss

`ResultSpec` says how much data the answer needs — `shape`, the `axes` it
varies along, and `row_count`, which retrieval must not come in under. A chart
of 3 companies over 12 quarters is 36 rows, and nothing else in the plan says
so (DESIGN.md §8.14).

`granularities` tracks annual vs quarterly separately, because a 363-day value
and a 90-day value are not comparable points — mixing them raises a plan-level
`mixed_granularity` note (DESIGN.md §8.18).

## 4. Qwen's role — decided, with evidence

Originally the plan was "Qwen2.5-Coder writes the SQL". That is still true, but
narrowed, and the narrowing was argued out rather than assumed.

I claimed at one point that the plan was structured enough that a deterministic
emitter might remove the LLM for most questions. **The user pushed back and was
right.** `evals/` was built to settle it:

```
46 answerable or partial questions
... of those, retrieval only  25
... needing more than that    21   →  46%
```

(It read 56% when I had written all the questions; the user's 13 additions are
more retrieval-heavy and pulled it down. Both numbers say the same thing: a
large minority-to-majority needs more than retrieval.)

So **Qwen stays.** The split:

- **Retrieval is bounded and should be deterministic.** A `Binding` can only
  express four shapes (`direct`/`residual` × `instant`/`duration`, plus operand
  arithmetic). Generating that with a model buys nothing but a chance to forget
  `is_latest`. This is a fact about the schema, not a prediction about
  questions.
- **Everything above it stays with the LLM** — ranking, growth, ratios across
  companies, filters on computed values. Open-ended, and the eval set says it
  is the majority.

The difference from the original plan is only *what Qwen writes against*: a
narrow retrieval surface instead of four raw tables.

**Caveat:** I wrote 44 of the 58 questions, so the distribution still leans on
my imagination. More of the user's questions is the cheapest way to sharpen it.

## 5. What is NOT built — the forward plan

In the order I would do it. Items 1–2 were agreed early and never started.

### 5.1 A narrow query surface (view) — agreed, not started

There are **no views** in the `xbrl` schema. Without one, every emitted query
must get `is_latest`, three joins, and instant-vs-duration period matching
right by itself. A view that pre-bakes those collapses most of the surface an
emitter can hallucinate over. Highest value, and it shrinks everything after
it.

### 5.2 Execution safety — not started

The only login role is `postgres`, **superuser**. Before anything
model-generated touches the database: a read-only role, SELECT-only validation,
row caps, statement timeout.

### 5.3 Result contract — not designed

Nothing says what columns come back, so the presentation layer cannot be
written against anything. Needs: company, period, metric, value, unit, and the
concept actually used (for citation). `QueryPlan.result.row_count` already says
how many rows must come back; nothing says their shape.

### 5.4 Composition convention — undecided

Three bindings across two companies → one query or three? Currently the emitter
would have to decide.

### 5.5 The emitter itself

Deterministic for retrieval, Qwen above it. Sequencing 5.1–5.4 first makes this
mostly mechanical.

### 5.6 The query-object producer — not started

Something has to turn a question into a `QueryIn`. Deliberately left outside
this project so far. Note that "how much money was made" is *metric-ambiguous*
(revenue? net income?) — a different ambiguity from the concept-level one the
mapper handles, and it belongs to this layer.

### 5.7 Company grouping — SCHEMA BUILT, DATA MISSING

From q051/q052 ("which companies in a given sector / by SIC office performed
best"). The query path is **complete**; only the data is absent.

Built: `CompanyGroupElementIn` (`sic_code` / `sic_description` / `sic_office`),
`Company.sic_code` / `sic_description` / `sic_office` (migration
`3011d3c40ff8`, applied to both databases), and `_resolve_company_groups`.
Named companies and group members merge without duplicates.

**The one remaining step is loading the data.** `sic_numbers.json` already
carries `sic` and `sic_description` per company (`app/ingest/sic_index.py`);
nothing writes them into the `company` table. Until then the mapper reports
*"SIC data has not been loaded into the database yet"* rather than an empty
set — an empty set would read as "no company is in that sector", a different
and wrong answer. A test populates one column and re-resolves, proving the
path works the moment the data lands with no code change.

`sic_office` has **no source at all**. `sic_numbers.json` carries code and
description only; the SEC assigns review offices by SIC *range*, so it is
derivable given that mapping, which this project does not have. The column
exists so the query path is complete; populating it is a separate decision.

### 5.8 Alias curation queue — now the largest single gap

`alias_gap` is tagged **13 times** across the eval set, more than any other
hazard. All are questions that *should* work, where the concepts exist and are
well covered, and only a curated entry is missing. Verified against the mapper:

| ask | status |
|---|---|
| investing / financing cash flow | refused as ambiguous *against each other*; 20 filers each |
| interest expense | unresolved; `InterestExpense` (17), `InterestExpenseDebt` (5), `InterestIncomeExpenseNet` (4) |
| effective tax rate | refused; 20 filers, but filed under **two units** (`pure` / `Rate`) — live case for PITFALLS §1.14 |
| share buybacks | refused; `PaymentsForRepurchaseOfCommonStock` (19) |
| dividends per share | refused; splits across three concepts (13 / 9 / 6) |
| PP&E | refused between Net (18) and Gross (14) — a real choice, not a tie |
| profit margin | refused between Gross Profit and Operating Income — needs a decision on *which* margin |
| operating margin, current ratio, free cash flow margin | not aliased |
| capex for AMZN, BAC, CVX, JPM, NVDA, QCOM | those six use a different concept |

**The system is behaving correctly here** — it refuses rather than guessing, and
the refusals are right (investing and financing cash flow really do sit
adjacent in embedding space). This is the curation loop working as designed.
Filling these is the highest-leverage, lowest-risk work available, and it is
the collaborative task the user needs help with, since they are not an
accountant.

### 5.9 Known smaller gaps

- **Metric groups are NOT a thing.** "cash flow: operating, investing,
  financing" is just several `MetricElementIn` along a metric axis, which works
  today. Whether anything knows that "balance sheet totals" means a particular
  list belongs to the *producer*. Briefly designed as a "bundle" concept before
  being recognised as nothing new — see DESIGN.md §8.20. Do not re-invent it.
- `Binding.unit` is wrong on a derived metric — `gross_margin` reports "USD"
  when the result is dimensionless (PITFALLS §2.1). Fix before anything renders
  values.
- Restatements are picked correctly by `is_latest` but never *disclosed*
  (PITFALLS §2.2). The `Note` channel exists and would carry it.
- A derived *and* residual binding reports only the lead operand's components.
  The coverage check is complete; the reported `components` under-describes it.

### Explicitly deferred by the user

**The 10-K/A gap** (PITFALLS §3.4). Ingest fetches only `10-K` and `10-Q`, so
amended filings — the vehicle for material restatements — are never seen. The
user has decided not to address this now. Do not reopen it unprompted.

## 6. Working conventions

The user's memory file carries these; they are repeated because violating them
has caused real friction.

- **Never `git commit` unless asked in that turn.** One approval is not
  standing permission.
- **One step per turn.** Do the thing, report, stop. Offer the next step as a
  question rather than proceeding.
- **Match repo line endings — LF everywhere** (except `LOADER.md`, which was
  already CRLF). Python text-mode writes on Windows silently convert to CRLF;
  pass `newline="\n"` and check `git diff --stat` against
  `--ignore-all-space` after any scripted edit.
- **Verify empirically before asserting.** Every number in `PITFALLS.md` came
  from a probe against the real database. The user notices unfounded
  confidence and will call it out — correctly.
- Terse output. No long explanations unless asked.

Run everything through `uv run`. Tests: `uv run pytest -q` (171 passing).
Lint: `uv run ruff check app/ tests/ evals/`.

## 7. Verifying things yourself

Ad-hoc SQL against `xbrl.fact` joined to `concept` / `filing`. Patterns worth
reusing, from PITFALLS §5:

- **A filing's own window:** `DISTINCT ON (company, fiscal_year,
  fiscal_period)` ordered by `period_end DESC, (period_end - period_start)
  ASC` — shortest span breaks ties so a discrete quarter beats the
  year-to-date.
- **Restatements:** group by `(company, concept, unit, period_start,
  period_end)` and count distinct values. Omitting `period_start` is a trap —
  it compares 3-month against 9-month windows sharing an end date. I made that
  mistake.
- **Concept drift:** per company per alias slot, the fiscal years each
  alternative covers. Any slot where no single alternative covers the union is
  a drift case.

Good live smoke test — exercises chart cardinality, Q4 residuals, concept drift
and calendar misalignment at once:

> "Show me visually how much money was made from 2023 to 2025 by quarter, for
> Google, Apple and Nvidia"

Expected: `shape=series`, `axes=['company','period']`, `row_count=36`, four
bindings (Alphabet splits mid-range), nine Q4 residuals, and a
`period_misalignment` note.

## 8. Doc map

| file | what |
|---|---|
| [`PITFALLS.md`](PITFALLS.md) | every known data hazard, measured, and whether it is handled |
| [`app/schemas/DESIGN.md`](app/schemas/DESIGN.md) | §8 = the query schemas, fifteen decisions with reasoning |
| [`app/semantic/DESIGN.md`](app/semantic/DESIGN.md) | the curated alias layer |
| [`app/db/DESIGN.md`](app/db/DESIGN.md) | ORM models, layout |
| [`LOADER.md`](LOADER.md) | the load step |
| [`ALEMBIC.md`](ALEMBIC.md) | migrations — note step 5, the test database is NOT migrated automatically |
| [`evals/README.md`](evals/README.md) | eval tag vocabulary, how to add questions |
| [`sec-retriever.md`](sec-retriever.md) | the original project brief; §3 is the reserved layout |

Recent commits, newest first:

```
<this session>  Company groups, granularity, qualifier removal
b89b9f2  Describe result shape in the plan, and refuse weak metric matches
2585907  Check declared operand signs before binding an expression
d4d03d1  Resolve metric bindings per period, with a notes channel
cdb1cce  Merge the alias modules into app/semantic/metric_aliases.py
f19e462  Implement metric resolver with curated YAML alias layer
068f200  Add query mapper with period-window and Q4 resolution
8b93ca9  Adding prefixes to embeddings
```
