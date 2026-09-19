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

1. **Companies** — deterministic lookup: the derived lexicon
   (`company_aliases.json`, §5.6) first, then ticker / entity name. Naming
   no company at all means every loaded filer.
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
curated accounting judgment as **data, not code** — 47 metrics, 182 surface
forms. Each operand slot lists *alternatives in preference order*, and coverage
picks per company. That is how filer divergence resolves without per-company
tables: Apple and Microsoft bind
`RevenueFromContractWithCustomerExcludingAssessedTax`, NVIDIA binds `Revenues`,
from one entry.

An entry does one of three things, and the split matters more than the count:
39 **resolve**, 4 **ask** (`clarify`), 4 **decline** (`unavailable` — share
price, market cap, segment revenue, gross revenue). An entry may also attach a
per-concept `caveat`, which becomes a `narrower_than_asked` note when a
fallback alternative answers less than the phrase asked for. See
`app/semantic/DESIGN.md` §8, §8a, §8b.

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
45 answerable or partial questions
... of those, retrieval only  25
... needing more than that    20   →  44%
```

(It read 56% when I had written all the questions; the user's 13 additions are
more retrieval-heavy and pulled it down, and the two of mine they later deleted
— an all-twenty ranking and a cross-company share-of-total — took a few
points with them, and q049 gave one back when loading the sector data turned
it from a refusal into a ranking. Every reading says the same thing: a large
minority needs more than retrieval.)

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

**Caveat:** I wrote 42 of the 56 questions, so the distribution still leans on
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

### 5.6 The query-object producer — not started, but two of its burdens lifted

Something has to turn a question into a `QueryIn`. Deliberately left outside
this project so far. Note that "how much money was made" is *metric-ambiguous*
(revenue? net income?) — a different ambiguity from the concept-level one the
mapper handles, and it belongs to this layer.

Two things the mapper used to demand of it, and no longer does:

**A ticker for every company.** The mapper now reads
`company_aliases.json`, a lexicon derived from SEC data by
`app/ingest/alias_index.py` and refreshed on every `get-submission`. All 26
probe names now resolve; before it, name-only lookup missed Google, Facebook,
Bank of America, AMD, Johnson and Johnson and United Health. Share classes
work too — "GOOG" is Alphabet although the stored ticker is GOOGL.

The names come from `corpus_companies.json` (the SEC's `company_tickers.json`:
`title`, `input_name`, `all_tickers`) and the submissions endpoint (`name`,
`tickers`, **`formerNames`** with dates — Meta was `Facebook Inc`, Chevron was
`CHEVRONTEXACO CORP`). Short forms and "and"/"&" spellings are derived.

**Historical tickers are not obtainable and the file says so.** Both SEC feeds
give only the *current* symbol, so a question using a retired one (FB rather
than META) will not resolve. The in-house source would be `dei:TradingSymbol`
from each cover page — but the XBRL data endpoint returns only numeric facts
and that is a string, so it is absent from the store entirely (three `dei`
concepts are loaded, all numeric). This never threatens correctness, because
**the cik never changes**: a missing alias costs a refusal, never a wrong
company.

**Enumerating "all companies".** A question with no company element now means
every loaded filer rather than being refused as "no company in scope". The
distinction that makes it safe: an *absent* element widens, a *failed* one
does not — "Apple versus Samsung" stays a half-answer and must never quietly
become the whole corpus. Both are pinned by tests.

### 5.7 Company grouping — DONE for sector, still blocked for office

From q049/q050 ("which companies in a given sector / by SIC office performed
best"). **q049 now answers**: "which companies in semiconductors" resolves to
AMD, INTC, MU and NVDA and comes back complete.

`app/db/loader.py` merges `sic_code` and `sic_description` into the `Company`
upsert from `sic_index.sic_by_cik()`. All 20 filers are populated across 14
SIC codes. Two decisions worth keeping:

- **Keyed on cik, not ticker**, although `sic_numbers.json` carries both. A
  cik is permanent and a ticker is not (see §6.1 below) — matching on ticker
  would silently drop a company the day it re-symboled. The loader does not
  copy the ticker either; the XBRL data file already fills that column.
- **Absent data never overwrites present data.** A company with no row in the
  index keeps its existing sector, so a reload against a missing or half-built
  index cannot null out what is already there. A test pins it.

`sic_office` still has **no source at all**. `sic_numbers.json` carries code
and description only; the SEC assigns review offices by SIC *range*, so it is
derivable given that mapping, which this project does not have. The refusal
now says exactly that rather than "SIC data has not been loaded", which would
send someone after data that does not exist — the three sector columns fail
for two different reasons and the message distinguishes them.

### 5.8 Alias curation — the 13 gaps are closed

`alias_gap` was tagged 13 times and is now **zero**. The file went from 22
metrics / 87 surface forms to 37 / 142. Added: investing and financing cash
flow, interest expense (ordered fallback, resolves per filer), effective tax
rate, share buybacks, dividends per share, dividends paid, PP&E, current
assets and liabilities, operating margin, net margin, free cash flow margin,
current ratio. Capex gained `PaymentsToAcquireProductiveAssets`, taking it from
14 filers to 18 — BAC and JPM report no capex concept at all, which is normal
for banks.

Four entries deliberately **ask** rather than resolve — `profit_margin`,
`profit`, `cash_flow`, `debt` — via the `clarify` mechanism
(app/semantic/DESIGN.md §8). Naming the specific metric resolves straight
through.

Four **decline**, via `unavailable` (§8a): `stock_price`, `market_cap`,
`segment_revenue`, `gross_revenue`. These are questions people actually ask
that this store structurally cannot answer, and the entry exists so the
refusal is a reason rather than a menu — left unlisted, a term falls to the
embedding search, which always returns *something*.

`gross_revenue` is the instructive one. It was listed here as **deliberately
uncurated** on the correct reasoning that US GAAP has no gross-vs-net revenue
pair. That turned out not to be the same as declining it: unlisted, it fell
through to `GrossProfit` at 0.812 — a different line, and a smaller one.
"Nothing honest to map it to" is an argument for `unavailable`, not for
silence.

Still uncurated on purpose:

- **`gross_profit`** — only 9 of 20 filers tag it, and falling back to
  revenue-minus-cost would produce a different number from the filer's own
  subtotal. Unlike gross revenue, the concept genuinely exists; the honest
  answer for the other 11 is "this filer does not report it", which coverage
  already gives.

Adding a filer will reopen gaps: coverage counts in the file are measured
against the current 20 and noted inline.

### 5.9 Nothing checks that the elements express the question — NOT ADDRESSED

The last plausible-wrong-answer in the eval set, and the only one left that
produces a confident number for a question nobody asked.

**q026**, "Did any of these companies restate its revenue?", comes back
`is_complete` with a 100-row revenue series and no caveat. Every element
resolved — "revenue", twenty companies, five years — so by every measure the
mapper has, the plan is perfect. It answers "what was their revenue", which is
a different question.

The mapper cannot catch this on its own and arguably should not: it is handed
elements, not a question, and the elements are all fine. `QueryIn.question`
carries the original text, so *something* could compare the two, but the
natural home is the producer (§5.6) — the layer that decided "restate" needed
no element and dropped it silently, exactly as the removed `qualifier` kind
used to (DESIGN.md §8.19).

Shapes that fail this way: restatement, causality ("why did margins fall"),
counts of filings, anything about the *filing* rather than the figures.

Worth deciding, when the producer is built: should an unrepresented span of the
question raise, warn, or be ignored? Silently ignoring it is what happens today
and is the worst of the three.

### 5.10 Known smaller gaps

- **Metric groups are NOT a thing.** "cash flow: operating, investing,
  financing" is just several `MetricElementIn` along a metric axis, which works
  today. Whether anything knows that "balance sheet totals" means a particular
  list belongs to the *producer*. Briefly designed as a "bundle" concept before
  being recognised as nothing new — see DESIGN.md §8.20. Do not re-invent it.
- Restatements are picked correctly by `is_latest` but never *disclosed*
  (PITFALLS §2.2). The `Note` channel exists and would carry it.
- A derived *and* residual binding reports only the lead operand's components.
  The coverage check is complete; the reported `components` under-describes it.

Two that were here are now fixed, both of which would have made the emitter
return confident wrong numbers:

- `Binding.unit` on a derived metric reported the lead operand's unit, so a
  ratio came back as "USD" (PITFALLS §2.1).
- `Binding.period_rule` was `residual` whenever *any* period in the group was
  a Q4, so a twelve-quarter binding claimed all twelve needed the subtraction.
  Residual periods now get their own binding.

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

Run everything through `uv run`. Tests: `uv run pytest -q` (224 passing).
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
