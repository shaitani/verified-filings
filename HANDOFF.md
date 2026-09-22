# Handoff — read this first

Orientation for a fresh session. Not a permanent doc: everything here either
points at a `DESIGN.md` next to the code, or is forward plan that lives
nowhere else. Where this file and a `DESIGN.md` disagree, the `DESIGN.md` is
right.

---

## 1. What this is

A system that answers natural-language questions about SEC financial data with
figures that are actually correct, and refuses — loudly — when it cannot. The
corpus is 20 US filers, 5 fiscal years, ~174,000 facts from the SEC's XBRL
data endpoint, in PostgreSQL.

The animating concern: **a plausible wrong number is worse than a refusal.**
Nearly every design decision traces back to that. XBRL is far less uniform
than it looks, and most of the hard problems are data problems, not code
problems. They are catalogued with measurements in
[`PITFALLS.md`](PITFALLS.md) — read it before trusting any figure.

## 2. Data flow, end to end

```
SEC XBRL data endpoint
   ↓  app/ingest/        fetch, rate-limit, scope to 10-K/10-Q + 5 FYs
data/xbrl/<TICKER>.json  curated per-company files (gitignored)
   ↓  app/schemas/xbrl.py + app/db/loader.py
PostgreSQL (xbrl schema) company / filing / concept / fact / load_run
   ↓  app/db/embedder.py embed every Concept (nomic-embed-text via Ollama)

question (a string)
   ↓  app/producer/       [C] Query Parser
       build_question_prompt / propose(Qwen) / accept  → QueryIn
   ↓  app/schemas/query.py
QueryIn                  question + typed elements + optional shape
   ↓  app/semantic/query_mapper.py  [D]  reads as vf_query_mapper_role
QueryPlan                concrete coordinates, caveats, cardinality
   ↓  app/retrieval/     [E] Executor
       build_prompt(plan) → text for Qwen                no DB, writes no SQL
       generate(plan)     → Qwen writes the SQL          no DB
       validate(sql)      → verdict only                 no DB, no edits
       execute(sql, plan) → ResultSet   reads xbrl.reported_fact as
                                        vf_retrieval_role
   ↓  app/schemas/result.py
ResultSet                rows + citations + verdict + notes
   ↓
[F] Presenter            renders, cites, discloses caveats   NOT BUILT
```

Everything from a question string to a `ResultSet` is built and works end to
end: `parse_question(text)` → `map_query(query_in)` → `answer(plan)`. Nothing
yet calls the three in sequence — see §6. 410 tests pass.

### Block names

The user named the components of the browser-to-answer chain so they can be
referred to unambiguously. Package names and block names differ in two places
and that is known, not an oversight.

| # | Name | Status | Where |
|---|---|---|---|
| [A] | Ask UI | not built | browser; no logic, no credentials |
| [B] | Web Display | not built | `app/web/` — the only thing that replies to [A] |
| [C] | Query Parser | **built** | `app/producer/` |
| [D] | Query Mapper | built | `app/semantic/query_mapper.py` |
| [E] | Executor | built | `app/retrieval/` |
| [F] | Presenter | not built | `app/presenter/` |
| [G] | Store | built | `app/db/` |
| [H] | Ingest | built | `app/ingest/` |

[B] owns the three-way branch on the plan — answer / clarify / refuse — and is
the only block that talks to the user. Clarifications and refusals skip [E]
and [F] entirely: a curated clarification is already plain business language,
and an LLM asked to soften a refusal writes something that reads like an
answer.

Two Docker Postgres containers, `db` and `db-test`; Ollama serves the
embedding model and Qwen. See [`BOOTSTRAP.md`](BOOTSTRAP.md) to bring it all
up — the roles and the models do **not** come back with the schema.

## 3. The two ends of the chain

Both ends used to be out of scope, on the grounds that they belonged to a
user-facing LLM this project did not own. That changed when the decision was
made to put a web front end on it (§2's block table). One end is now built.

**[C] Query Parser is built** — `app/producer/`, 2026-09-20. Read
[`app/producer/DESIGN.md`](app/producer/DESIGN.md) before touching it. The
load-bearing idea is the **faithfulness gate**: every metric, company and
company_group element's `text` must appear verbatim in the question, enforced
in `accept()` rather than asked for in the prompt. Of the ways a parser can be
wrong, bad structure fails loudly and an unknown company costs a refusal; the
one that costs a *wrong answer* is a paraphrase, because the mapper then
resolves the replacement phrase perfectly and everything downstream agrees.
Periods are exempt — a period's meaning lives in its typed fields, and holding
"Q4 last year" to a contiguous-span rule refused 8 of 56 correct parses.

The rule that still stands: **do not build a stand-in for either end.** A
surrogate gets measured and tuned, and then the real thing behaves
differently, which is worse than no measurement.

**[F] Presenter is not built.** `ResultSet` → prose, and nothing else: it
never sees a plan, and it must carry `Note`s through rather than summarizing
them away.

Two burdens the parser does *not* carry: company names resolve through
`company_aliases.json` (derived from SEC data, refreshed on every
`get-submission`; share classes work, "GOOG" is Alphabet), and a question
naming no company means every loaded filer.

**Historical tickers are not obtainable.** Both SEC feeds give only the
current symbol, and the in-house source would be `dei:TradingSymbol` from
each cover page, which the XBRL data endpoint does not return. A retired
symbol (FB rather than META) will not resolve. This never threatens
correctness, because **the cik never changes** — a missing alias costs a
refusal, never a wrong company.

## 4. The core idea: QueryIn → QueryPlan

**`QueryIn`** speaks the *user's* language — `"revenue"`, never
`us-gaap:Revenues`. Elements are a discriminated union on `kind` — `metric` /
`company` / `company_group` / `period` — because each needs a different
resolver. Embedding search over "Apple" returns noise.

**`QueryPlan`** is what the mapper resolves that into, and it is the real
artifact of this project: concrete `concept_id`s, per-company date windows,
units, instant-vs-duration, proof the facts exist, and caveats that must reach
the reader. The design intent is that **the SQL step has nothing left to
guess**.

Full rationale: [`app/schemas/DESIGN.md`](app/schemas/DESIGN.md) §8 and
[`app/semantic/DESIGN.md`](app/semantic/DESIGN.md).

### The three resolvers, in order

1. **Companies** — deterministic. The derived lexicon first, then ticker /
   entity name. A company element that *fails* does not widen the scope.
2. **Periods** — into concrete date windows *per company*. Never fiscal-year
   integers; `Filing.fiscal_year` is provenance, not the period a number
   describes (PITFALLS §1.1 — this silently returned FY2022 revenue for an
   FY2024 query before it was fixed).
3. **Metrics** — curated alias → embedding fallback → **coverage check against
   the view**, which is the arbiter of both.

Coverage is the load-bearing idea: a candidate with no facts for the requested
windows is never bound, whatever its similarity score. Zero rows from a
well-formed query is indistinguishable from "the company reported nothing", so
a binding has to be *proven* before it is made. It is proven against
`xbrl.reported_fact` — the same relation retrieval reads — so the mapper
cannot bind something retrieval cannot fetch.

### The alias layer

[`app/semantic/metric_aliases.yaml`](app/semantic/metric_aliases.yaml) is
curated accounting judgment as **data, not code**: 47 metrics, 143 synonyms.
Each operand slot lists *alternatives in preference order* and coverage picks
per company, which is how filer divergence resolves without per-company
tables: Apple binds `RevenueFromContractWithCustomerExcludingAssessedTax`,
NVIDIA binds `Revenues`, from one entry.

An entry does exactly one of three things: **39 resolve**, **4 ask**
(`clarify`), **4 decline** (`unavailable`). An entry may also attach a
per-concept `caveat`, which becomes a `narrower_than_asked` note.

Two lessons, both in `app/semantic/DESIGN.md` §8a–§8b:

- **"Nothing honest to map it to" argues for `unavailable`, not silence.** An
  unlisted term falls to the embedding search, which always returns
  *something* — `gross_revenue` came back as `GrossProfit` at 0.812.
- **Similarity cannot separate right from wrong.** Over 221 labelled cases the
  band just above the old bar was 5% precise. The bar is 0.75 with a 0.65
  floor below which an element is `unresolved` rather than `ambiguous`. That
  makes the path less bad, not safe. The fix for a term people keep asking is
  a curated entry.

**The user is not an accountant.** Curating this file is collaborative, and it
is the main lever on answer quality.

### Two things the plan tracks that are easy to miss

`ResultSpec` says how much data the answer needs — `shape`, the `axes` it
varies along, and `row_count`, which retrieval must not come in under. A chart
of 3 companies over 12 quarters is 36 rows, and nothing else says so.

`granularities` tracks annual vs quarterly separately: a 363-day value and a
90-day value are not comparable points, and mixing them raises a plan-level
`mixed_granularity` note.

## 5. The retrieval layer

Each function does one job, and only it does that job.

| function | does | does not |
|---|---|---|
| `build_prompt` | assembles text | write SQL |
| `generate` | the only thing that talks to Qwen | judge or run the reply |
| `validate` | judges, returning its input byte-identical | run it, or edit it |
| `execute` | the only thing that touches the database | anything else |

**Qwen writes all of the SQL.** The plan reaches it as a table of coordinates
— not a `VALUES` list, which would be SQL. `validate` raises `OutOfRole`
(logged at WARNING) when the statement steps outside its role — a `SET`,
`set_config`, a relation other than the view, a data-modifying CTE, or a
statement that reads nothing at all — and `ContractViolation` for an ordinary
mistake. Model: `qwen2.5-coder:7b`, 100% on the GPU, ~48 tok/s.

**Read [`app/retrieval/DESIGN.md`](app/retrieval/DESIGN.md) §4.3 before
touching the prompt.** It catalogues seven measured failures and what each one
cost. Every one was the prompt's fault, not the model's, and they share a
shape: the model does something reasonable that the prompt did not rule out.
The recurring lesson is **show it the thing it must produce** — the literal
the column holds, the name the contract wants, the operator the expression
uses.

`xbrl.reported_fact` is the one relation retrieval can read. It bakes in
`is_latest` and the joins, exposes **no** `fiscal_year` (that trap is closed
by absence), and **synthesizes the fourth quarter** — no US filer reports one,
so it is the annual figure minus the year-to-date one. 100,841 filed rows plus
9,425 synthesized, marked `is_synthesized`.

## 6. Known gaps and weaknesses

In rough order of how much they matter.

- **Nothing checks that the answer matches the question.** The last
  plausible-wrong-answer in the eval set. **q026**, "Did any of these
  companies restate its revenue?", comes back `is_complete` with a 100-row
  revenue series and no caveat: every element resolved, so by every measure
  the mapper has, the plan is perfect. It answers a different question. Seen
  again live — a ranking question returned the underlying figures, verdict
  `complete`, `is_answerable` true. The verdict checks cardinality and
  attribution, not meaning. Natural home is [C], which deliberately does not
  do it yet: it needs the eval runner first, so the gate can be measured
  rather than guessed at (`app/producer/DESIGN.md` §7). Shapes that
  fail this way: restatement, causality, counts of filings, anything about the
  *filing* rather than the figures.
- **Same-unit arithmetic rests on the prompt alone.** A ratio in the wrong
  unit is caught — `execute()` refuses a row whose unit is not the binding's —
  but `free_cash_flow` is `c0 - c1` over two USD concepts, so a wrong operator
  is a wrong number in the right unit. It happened once, live: free cash flow
  as 12.5 rather than 108.8 billion, attributable, verdict `complete`. Fixed
  in the prompt; there is no structural check behind it.
- **The eval set has a runner but no full-run number yet.**
  `evals/run.py` is [B] with the browser, the state and [F] taken out: it
  calls `parse_question` → `map_query` → `answer` over the 56 questions and
  scores the *decision* — did it answer, and was answering the right call. It
  does not check the figure. Smoke-tested on two questions; a full
  `--stage answer` run takes around fifteen minutes and has not been taken.
  Until it is, "how often is this right?" still rests on a handful of
  hand-written cases.

  The grade to watch is **`unsafe`** — an answerable result for a question
  tagged `refuse`. Every other failure costs a refusal, which is the outcome
  this project prefers; that one is the failure it exists to prevent, so it is
  counted apart from `fail` and printed last.
- **Restatements** are picked correctly by `is_latest` but never *disclosed*
  (PITFALLS §2.2). The `Note` channel would carry it.
- **`sic_office` has no source.** `sic_numbers.json` carries code and
  description only, and the SEC assigns review offices by SIC *range*, a
  mapping this project does not have. Its refusal says exactly that.
  `sic_code` and `sic_description` are loaded for all 20 filers.
- **Alias curation for recall**: interest income, treasury stock, deferred
  revenue, operating expenses, depreciation, accounts receivable/payable,
  retained earnings.
- **Metric groups are NOT a thing.** "cash flow: operating, investing,
  financing" is several `MetricElementIn` along a metric axis, which works.
  Whether anything knows "balance sheet totals" means a particular list
  belongs to [C], the Query Parser. Briefly designed as a "bundle" before being
  recognised as nothing new — schemas DESIGN §8.20. Do not re-invent it.

### Explicitly deferred by the user

**The 10-K/A gap** (PITFALLS §3.4). Ingest fetches only `10-K` and `10-Q`, so
amended filings — the vehicle for material restatements — are never seen. The
user has decided not to address this now. Do not reopen it unprompted.

## 7. Working conventions

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
  `newline="\n"` and check `git diff --stat` against `--ignore-all-space`.
- **Verify empirically before asserting.** Every number in `PITFALLS.md` came
  from a probe against the real database. The user notices unfounded
  confidence and will call it out — correctly.
- Terse output. No long explanations unless asked.

Run everything through `uv run`. Tests: `uv run pytest -q` (417 passing).
Lint: `uv run ruff check app/ tests/ evals/`.

## 8. Verifying things yourself

Ad-hoc SQL against `xbrl.fact` joined to `concept` / `filing`, or against
`xbrl.reported_fact` for what retrieval sees. Patterns worth reusing, from
PITFALLS §5:

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

Good live smoke test — chart cardinality, Q4, concept drift and calendar
misalignment at once:

> "Show me visually how much money was made from 2023 to 2025 by quarter, for
> Google, Apple and Nvidia"

Expected: `shape=series`, `axes=['company','period']`, `row_count=36`, **four
bindings** (Alphabet changes revenue tags mid-range), a plan-level
`period_misalignment`, and `complete` 36/36 through `answer()`. Apple's Q4s
come back 89.5B / 94.9B / 102.5B.

Figures to check arithmetic against, all computed independently from the
database: Apple FY2024 revenue 391,035,000,000; Q4 FY2024 94,930,000,000;
gross margin 0.462063; free cash flow 108,807,000,000.

## 9. Doc map

| file | what |
|---|---|
| [`BOOTSTRAP.md`](BOOTSTRAP.md) | bringing everything up from nothing, and what a volume wipe destroys |
| [`PITFALLS.md`](PITFALLS.md) | every known data hazard, measured, and whether it is handled |
| [`app/retrieval/DESIGN.md`](app/retrieval/DESIGN.md) | the result contract, the view, the validator, and §4.3's catalogue of prompt failures |
| [`app/producer/DESIGN.md`](app/producer/DESIGN.md) | [C] the Query Parser — the faithfulness gate, its measured failures, and what is deliberately not done |
| [`app/schemas/DESIGN.md`](app/schemas/DESIGN.md) | §8 = the query schemas, decision by decision |
| [`app/semantic/DESIGN.md`](app/semantic/DESIGN.md) | the curated alias layer |
| [`app/db/DESIGN.md`](app/db/DESIGN.md) | ORM models, layout |
| [`app/db/roles.py`](app/db/roles.py) | the two read-only roles, and which half of them is a real boundary |
| [`LOADER.md`](LOADER.md) | the load step |
| [`ALEMBIC.md`](ALEMBIC.md) | migrations — note step 5, the test database is NOT migrated automatically |
| [`evals/README.md`](evals/README.md) | eval tag vocabulary, how to add questions, how to run the set |
| [`sec-retriever.md`](sec-retriever.md) | the original project brief |
