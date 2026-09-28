# Handoff — read this first

**Status, 2026-09-27.** Steps 1–3 are done: the question-to-answer chain, the
Presenter, and the Web Server (FastAPI, sign-in, job queue, its own
container). **Next is step 4, the Angular Web Client** — its plan and open
decisions are in §11, and the user wants the plan and questions put to them
*before* any building starts, as step 3's were. The web end at a glance: §10.

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
   ↓  app/parser/       [C] Query Parser
       build_question_prompt / propose(Qwen) / accept  → QueryIn
   ↓  app/schemas/query.py
QueryIn                  question + typed elements + optional shape
   ↓  app/semantic/query_mapper.py  [D]  reads as vf_query_mapper_role
QueryPlan                concrete coordinates, caveats, cardinality
   ↓  app/retrieval/     [E] Executor
       build_prompt(plan) → text for Qwen (rank/derive)  no DB, writes no SQL
       generate(plan)     → the SQL: Python writes it;   no DB
                            Qwen only a ranking/derivation over `figures`
       validate(sql)      → verdict only                 no DB, no edits
       execute(sql, plan) → ResultSet   reads xbrl.reported_fact as
                                        vf_retrieval_role
   ↓  app/schemas/result.py
ResultSet                rows + citations + verdict + notes + ResultSpec
   ↓  app/presenter/     [F] Presenter        ResultSet → AnswerView   built
   ↓  app/api/           [B] Web Server       FastAPI                  built
   ↓  web/               [A] Web Client       Angular                  NOT BUILT
```

Everything from a question string to a reply is built and works end to end:
`app/chain.py`'s `ask()` runs `parse_question` → `map_query` → `answer` →
`present`, and the Web Server runs it once per job (§10). `evals/run.py`
still calls the first three itself — moving it onto `ask()` is deferred to
the user's testing update (§11). 815 tests pass.

### Block names

The user named the components of the browser-to-answer chain so they can be
referred to unambiguously. Package names and block names differ in two places
and that is known, not an oversight.

| # | Name | Status | Where |
|---|---|---|---|
| [A] | Web Client | not built | `web/` — Angular + ngrx/signals; renders, decides nothing |
| [B] | Web Server | built | `app/api/` — FastAPI; runs the chain, the only thing that talks to [A] |
| [C] | Query Parser | **built** | `app/parser/` |
| [D] | Query Mapper | built | `app/semantic/query_mapper.py` |
| [E] | Executor | built | `app/retrieval/` |
| [F] | Presenter | built | `app/presenter/` — `ResultSet → AnswerView`, no model |
| [G] | Store | built | `app/db/` |
| [H] | Ingest | built | `app/ingest/` |

[B] is built (2026-09-27, step 3: eight slices, `app/chain.py` and
`app/api/`) and runs in its own container; [A] is designed, not built. Read
[`app/api/DESIGN.md`](app/api/DESIGN.md). [F] is built (2026-09-27):
[`app/presenter/DESIGN.md`](app/presenter/DESIGN.md). [A] and [B] were renamed on
2026-09-27 from Ask UI and Web Display.

There is **no three-way branch** — answer / clarify / refuse. A question is
answered per part (semantic DESIGN §8e), so one reply can hold figures,
refusals and questions back together, and they can come from the parser, the
mapper or the executor. [B] is still the only block that talks to the user,
and refusals and questions still never pass through a model on the way out: a
curated clarification is already plain business language, and an LLM asked to
soften a refusal writes something that reads like an answer.

Two Docker Postgres containers, `db` and `db-test`; Ollama serves the
embedding model and Qwen. See [`BOOTSTRAP.md`](BOOTSTRAP.md) to bring it all
up — the roles and the models do **not** come back with the schema.

## 3. The two ends of the chain

Both ends used to be out of scope, on the grounds that they belonged to a
user-facing LLM this project did not own. That changed when the decision was
made to put a web front end on it (§2's block table). Both are now built on
the server side: [C] the parser, and [B]/[F] the Web Server and Presenter.
Only [A], the browser client, remains (§11).

**[C] Query Parser is built** — `app/parser/`, 2026-09-20. Read
[`app/parser/DESIGN.md`](app/parser/DESIGN.md) before touching it. The
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

**[F] Presenter is built, and is not prose.** `ResultSet →
AnswerView`, a typed structure the Web Client renders as a chart, a table or a
figure, written in Python with no model — a model retyping figures is a new
place for a plausible wrong number. It never sees a plan (the `ResultSpec` it
needs travels on the `ResultSet`), and it must carry `Note`s through verbatim.
[`app/api/DESIGN.md`](app/api/DESIGN.md) §2,
[`app/presenter/DESIGN.md`](app/presenter/DESIGN.md).

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

A filer that covers nothing does not refuse the question. It is dropped, and a
plan-level `partial_coverage` note names it — JPMorgan tags no
`OperatingIncomeLoss`, correctly for a bank, and refusing "highest operating
margin" because one filer of twenty cannot take part is worse than answering
over nineteen and saying so. A *ranking* gets an extra sentence, because
dropping a company can change the answer rather than just shorten it. Full
rules, including what this deliberately does not fix:
[`app/semantic/DESIGN.md`](app/semantic/DESIGN.md) §8c.

The exception: a `compare` or `rank` over **named** companies that is left
with fewer than two refuses, naming what is missing — "Apple vs Samsung" has
no Samsung data, and half a comparison is a different answer. Five named with
one missing still ranks the other four. §8d.

Coverage is the load-bearing idea: a candidate with no facts for the requested
windows is never bound, whatever its similarity score. Zero rows from a
well-formed query is indistinguishable from "the company reported nothing", so
a binding has to be *proven* before it is made. It is proven against
`xbrl.reported_fact` — the same relation retrieval reads — so the mapper
cannot bind something retrieval cannot fetch.

### The alias layer

[`app/semantic/metric_aliases.yaml`](app/semantic/metric_aliases.yaml) is
curated accounting judgment as **data, not code**: 51 metrics, 235 synonyms.
Each operand slot lists *alternatives in preference order* and coverage picks
per company, which is how filer divergence resolves without per-company
tables: Apple binds `RevenueFromContractWithCustomerExcludingAssessedTax`,
NVIDIA binds `Revenues`, from one entry.

An entry does exactly one of three things: **41 resolve**, **6 ask**
(`clarify`), **4 decline** (`unavailable`). An entry may also attach a
per-concept `caveat`, which becomes a `narrower_than_asked` note.

**A vague term reaches a curated question by one of two routes** — use them
when a question should come back `asked` and instead comes back `refused` or
`confused`:

1. **Its phrase is listed** as a synonym of a `clarify` entry. Exact phrase
   after normalization, so list the wordings people actually use ("money made",
   "money was made", "money is made", …). Checked first.
2. **The parser names the entry**, in the metric element's `clarify_as` field,
   for a vague phrase the file does not list ("How did Apple *fare*" →
   `performance`). Used only when route 1 finds nothing, and before the
   embedding search. It can only ever produce a question, never a figure: the
   reader's answer comes back through `parse_question(answers=...)` and binds
   by alias.

So the fix for a vague phrase is: pick (or add) the `clarify` entry whose
question fits, add the common wordings as synonyms, and let `clarify_as` catch
the rest. A new `clarify` entry needs no code — the grammar's enum and the
mapper both read it from the file. The model learns the field from three
worked examples; a rule listing the questions was tried and broke colon-list
questions, so do not add one without measuring them cold. Do **not** route
vague phrases by similarity: measured, phrases that deserve a question
(0.48–0.68) and phrases that deserve a refusal (0.49–0.61) overlap completely.
Full write-up: `app/parser/DESIGN.md` §10a.

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

**Python writes the SQL; Qwen writes only a ranking or a derivation.** Since
2026-09-26 every value the answer reads is written in Python from the plan:
the coordinates (`wanted`) and each cell's figure — fetched, combined, filtered
by a threshold, or computed over time — in a second CTE, `figures`. A plan
that computes nothing above its cells never reaches Qwen. A `rank` or `derive`
plan gets a short prompt showing only `figures`, and Qwen writes the layer
above it (retrieval DESIGN §4.6). `validate` raises `OutOfRole`
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

- **Nothing checks that the answer matches the question.** "Did any of these
  companies restate its revenue?" comes back `is_complete` with a revenue
  series and no caveat: every element resolved, so by every measure the mapper
  has, the plan is perfect. It answers a different question. Seen again live —
  a ranking question returned the underlying figures, verdict `complete`,
  `is_answerable` true. The verdict checks cardinality and attribution, not
  meaning. Shapes that fail this way: restatement, causality, counts of
  filings, anything about the *filing* rather than the figures. Natural home
  is [C], which deliberately does not do it yet: it needs the eval runner
  first, so the gate can be measured rather than guessed at
  (`app/parser/DESIGN.md` §7).
- **Metric arithmetic and thresholds are no longer the model's to write.**
  `free_cash_flow` is `c0 - c1` over two USD concepts, so a wrong operator is
  a wrong number in the right unit, and it happened live three times: free
  cash flow as 12.5 rather than 108.8 billion, NVIDIA's FY2025 free cash flow
  as 64.1 (operating cash flow, the subtraction dropped) rather than 60.9, and
  q009 ranking operating income minus revenue as "operating margin" — all
  attributable, verdict `complete`. Since 2026-09-26 every multi-operand
  metric, every threshold, and every change / growth / CAGR the parser marks
  (`over_time`) is computed in a Python-written CTE, `figures` (retrieval
  DESIGN §4.6, §4.8); the model sees only computed values, and only for a
  ranking or derivation above them. What the model still writes — the
  ordering, a derivation — has no structural check behind it.
- **The eval set has a runner but no full-run number yet.**
  `evals/run.py` is [B] with the browser, the state and [F] taken out: it
  calls `parse_question` → `map_query` → `answer` over the 56 questions (its
  own copy of the chain — `app/chain.ask` is the one readers get; moving the
  evals onto it is deferred, §11) and
  scores the *decision* — did it answer, and was answering the right call. It
  does not check the figure.

  **Two ways to run it.**

  ```
  uv run python evals/run.py                     # one line per question
  uv run python evals/walkthrough.py             # a markdown breakdown
  ```

  `run.py` gives a tally and a terse line each — use it for a number. Both
  take an id range and share the same scorer, so they never disagree on a
  grade:

  ```
  uv run python evals/run.py --id q007 q009      # run.py takes ids
  uv run python evals/walkthrough.py q020 q029   # walkthrough takes a range
  uv run python evals/run.py --stage map         # no Qwen; plans only
  uv run python evals/run.py --include-known-gaps
  ```

  `walkthrough.py` writes `data/question-walkthrough.md` (gitignored) and is
  what to reach for when a count moves and you need to know why: per question
  it records every element the parser produced, expected against observed item
  by item, the rows with their units and windows, the provenance, every caveat
  raised, and any exception verbatim. It writes after each question, so the
  file is readable while the run is still going. Budget about 10 minutes for
  the whole set; a handful of questions take a minute each.

  Both need the database and Ollama up (`BOOTSTRAP.md`). `--stage parse` needs
  neither.

  **No current number is recorded here, on purpose.** Earlier runs were
  deleted along with their artefacts: they were taken across a week of prompt
  changes, several were stale in both directions, and a stale pass rate is
  worse than none — it gets quoted. Run it and see.

  Two things to know before reading the output. `expect` is a **list**, one
  entry per metric the question names, because "assets, liabilities, equity,
  cash, goodwill, inventory" is six asks and five can succeed while the sixth
  fails. And a question carrying `known_gap` is skipped by default and listed
  separately; `--include-known-gaps` runs it anyway. Two carry one today.

  The grade to watch is **`unsafe`** — the system answering an item marked
  `refused` or `asked`. Every other failure costs a refusal, which is the
  outcome this project prefers; that one is the failure it exists to prevent,
  so it is counted apart from `fail` and printed last.

  **A question's grade depends on Ollama's prompt cache. Do not re-run a
  question N times to measure "non-determinism" — it has been done, and
  repeats measure the cache, not the model.** Measured 2026-09-26:

  - Temperature is 0 and Qwen is deterministic **for a given cache state**:
    41 calls of q048's parser prompt were byte-identical — cold, back to back,
    and after another question's prompt, an SQL call or an embedding call.
  - The cache state changes the reply. Once Ollama has cached a longer request
    that *begins with* a prompt, that prompt gets a different (equally stable)
    reply. The parser's retry prompt is exactly that — the first prompt plus
    the rejected reply and the error — so **a question's first attempt behaves
    differently after its own retry has run once.** Unloading the model
    (`keep_alive=0`) resets it. The retry's content does not leak; only the
    arithmetic path changes.
  - So re-running a question you just ran is optimistic: q048 **fails every
    time from a cold model** (invents `fiscal_year 2024`) and passes every time
    once its retry is cached — 9/10 "passes" in a 10× loop were all warm.
  - A full walkthrough runs each question once, so it is the closer measure,
    but a question can still inherit cache state from the ones before it
    (q034 failed in a full run and passes cold and warm in isolation;
    unexplained).

  To see a question as a first-time asker would: unload Qwen, then run it
  once. To compare before/after a change, do that for both.

  ```
  docker exec verified-filings-ollama-1 ollama stop qwen2.5-coder:7b
  ```
 Not yet done: the
  runner does not unload between questions, so full-run grades are not fully
  reproducible. Scripts from the measurement are not kept.

- **The unit guard can be switched off by the model, and has been.**
  `_wrong_unit` is the one structural check on derived arithmetic: a binding
  whose result unit is `pure` cannot have rows in `USD`, because dividing like
  by like is dimensionless. It skips any row whose `derivation` is set — and
  `derivation` is a string the *model* writes. A ratio question that came back
  as a subtraction, labelled `USD`, with a plausible `derivation` name, was
  graded `complete` and answerable for three days before anything noticed.
  **Do not trust a model-supplied flag to gate a check on that model's own
  output.** TODO in `app/retrieval/executor.py`; not fixed.

  Worth pairing with the limit above it: the runner grades the *decision*, not
  the figure. It cannot catch this class on its own, by design
  (`evals/README.md`). When a run says `pass`, that means the chain made the
  right call about whether to answer — not that the number is right.

- **What Qwen still writes has no structural check.** A derivation over
  `figures` is the model's, and the statement is checked for shape, not for
  meaning. Measured failure of that layer: q040 leaving `unit` out of an
  average (refused, now a prompt rule). Rankings are no longer the model's
  (below).
  The old `_JOB_EITHER` choice between "copy" and "compute", which improvised
  arithmetic when its example was missing, is gone with the path that used it.
- **TODO — retire Qwen as a SQL emitter entirely.** The user's stated
  direction (2026-09-27). **Rankings are done** (parser DESIGN §10c): the
  parser marks `rank: highest | lowest`, Python writes the `ORDER BY`, and q009,
  q014, q038 and q039 no longer reach Qwen; eval entries pin the direction
  (`rank:`). What Qwen still writes is an aggregate (q040's average) or another
  derivation. Move that reading into the parser as a closed list too, the
  pattern that worked for `clarify_as`, `over_time` and `rank`:
  `aggregate: average | sum | min | max`, `share_of_total`, `difference`,
  grammar-constrained and checked in `accept()`. Python then writes every
  statement from a fixed template over `figures`; an operation not on the list
  is refused with a reason (q015's "fell three years running" until added).
  Costs to weigh: a wrong direction becomes a plausible wrong answer (state the
  operation in the answer, pin it in eval expectations); long-tail operations
  Qwen improvises today stop working until listed; and it is another parser
  prompt change, so re-measure the colon-list questions cold.
- **A company the parser drops widens the question silently.** Naming no
  company means every filer, so an omitted company element is not a refusal —
  it is an answer about all twenty. Measured 2026-09-27: a prompt change made
  the q018 round trip drop "Costco" and rank every filer's quarters, verdict
  `complete` (parser DESIGN §10c; fixed by example order, not by a check). The
  structural fix would mirror the faithfulness gate: refuse a parse when the
  question contains a known company alias no company element covers. Not
  built; it needs measuring for short aliases that occur as ordinary words.
- **A relationship between two metrics has nowhere to live.** "How much of
  Alphabet's revenue goes to R&D?" is a ratio of two filed figures, and the
  chain cannot say so: the parser emits two independent metric elements, the
  mapper builds two single-concept bindings, and the division survives only as
  English in `question`. `QueryIn` is a flat list with no field relating one
  element to another, so even a perfect parser would have nowhere to put it.
  The only working route today is a curated alias per *phrase* — "R&D
  intensity" binds, "how much of revenue goes to R&D" does not — which does
  not scale, because a file cannot enumerate how English relates two
  quantities. **Probably the highest-value thing not built.** Full write-up,
  including what it would take and why the mapper half is nearly free:
  [`app/retrieval/DESIGN.md`](app/retrieval/DESIGN.md) §4.3d.
- **The optimisation lever, when there is time for one.** Writing the
  coordinate CTE in Python instead of asking the model to transcribe it took
  q038 from 40 of 378 rows to 378 of 378, made mislabelled dates
  unrepresentable rather than discouraged, and shrank the prompt from 53,114
  characters — growing with the plan — to about 6,200, constant. The same move
  is available for the join, the fixed projection, the `LIMIT` and the operand
  expression, all of which are prose in the prompt today. Every prompt rule
  exists because the model can get something wrong, so every rule names a
  candidate. Listed with measurements in
  [`app/retrieval/DESIGN.md`](app/retrieval/DESIGN.md) §4.6.

- **Restatements** are picked correctly by `is_latest` but never *disclosed*
  (PITFALLS §2.2). The `Note` channel would carry it.
- **Segment and geography questions refuse, by design.** "Revenue from
  iPhones" resolves its qualifier to a refusal naming what is missing, because
  the XBRL data endpoint carries no dimensional facts (PITFALLS §3.2). The
  mechanism is `MetricQualifierElementIn` plus `_apply_qualifiers`, and the
  load-bearing rule is that an unsatisfiable qualifier **takes its metric with
  it** — without that, the metric binds alone and a question about one product
  is answered with a company total. `app/parser/DESIGN.md` §6a.
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

### Questions that come back wrong — collected, to fix later

Every question known to get a wrong, odd or badly worded reply, in one place.
The user's call (2026-09-27): collect them here and address them together.
Most came from running every eval question through the chain and the
Presenter; `pass` in the eval runner means the *decision* was right, not the
figure (above), so several of these grade `pass`.

| question | what comes back | whose | status |
|---|---|---|---|
| q057 "highest operating income at Costco **in 2024**" | Costco's **FY2025** quarters: the parser drops "in 2024" and emits `last_n_years: 1` | [C] | a plausible wrong answer; task chip raised 2026-09-27 |
| "revenue from 2023 to 2025 **by quarter**" (HANDOFF §8's smoke test, reworded) | 128 rows, not 36: three years plus four bare quarters, which the mapper reads across every year | [C] | task chip raised 2026-09-27; kept as the Presenter's `smoke_mixed` fixture |
| q042 "has anyone's total debt more than **doubled** since 2021?" | year-over-year dollar changes for 15 filers, not growth from 2021 with a >100% filter | [C]/[D] | the answer-matches-question gap, first bullet of this section |
| q040 "**average** R&D spend across these companies" | the average on 14 rows, one per company | [E] | the Presenter collapses it to one figure naming the 14 (presenter DESIGN §4a); the real fix is the `aggregate` operation in the TODO above |
| q018's round trip | dropped "Costco" and ranked every filer's quarters | [C] | fixed by example order, not a check — "A company the parser drops", above |
| q023 "how much of Alphabet's revenue goes to R&D?" | one of the two operands, not the ratio | schema | `known_gap`; "A relationship between two metrics", above |
| q015 "gross margin fell three years running" | refused: the 7B model runs out of tokens | [E] | `known_gap` |
| q050 "by sic office … quarterly and yearly" | refused before the mapper: the parser invents a year | [C] | `known_gap` |
| q048 "quarterly revenues: gross, net" | fails from a cold model, passes warm | [C] | the prompt-cache note, above |
| q055 "Apple's **interest expense**" | refused: Apple stops tagging it after FY2023 | data | eval expects `answered`; decide whether the expectation or the answer changes |
| q028 / q029 "Apple's revenue in 2015 / 2019" | the real refusal plus a knock-on one ("no reporting period in scope to verify coverage against") | [B] | [B] shows only the blocking refusal when one exists — step 3 |
| q049 "companies in **a given sector**" | "no loaded company matches sic_description='a given sector'" | [D] | reason text written for a developer; reword in the mapper |

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

Run everything through `uv run`. Tests: `uv run pytest -q` (815 passing,
2026-09-27) — it needs `db-test` up, migrated to head (the `web` schema
included: `ALEMBIC.md` step 5), and provisions all three roles itself.
Lint: `uv run ruff check app/ tests/ evals/`.

- **Tooling on this Windows machine.** Escapes are lost through layers: a
  script sent through the Bash tool that writes Python source through a string
  literal turned `\b` and `\n` into control characters in a test file
  (2026-09-27), and `$'\r'` did not expand. Write such scripts to a scratch
  file with the Write tool, or edit with the Edit tool; check line endings
  with `file`, not `grep -c $'\r'`.
- **The user prefers explicit names to conventions** — `api.Dockerfile`
  named in `docker-compose.yml`, not a default `Dockerfile` found by
  magic. Where a tool cannot be told a name, say so in a comment.
- **Explain infrastructure plainly.** The user is not steeped in Docker or
  Linux; say what a thing is and why before how.

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
| [`app/api/DESIGN.md`](app/api/DESIGN.md) | [A] [B], the web end — [B] built (step 3's eight slices, §12a), [A] designed: the reply's parts, questions back, jobs, what to draw, the trace, sign-in, the `web` schema and its role, the container |
| [`app/presenter/DESIGN.md`](app/presenter/DESIGN.md) | [F] the Presenter — display strings, views by shape, what it refuses |
| [`app/retrieval/DESIGN.md`](app/retrieval/DESIGN.md) | the result contract, the view, the validator, and §4.3's catalogue of prompt failures |
| [`app/parser/DESIGN.md`](app/parser/DESIGN.md) | [C] the Query Parser — the faithfulness gate, its measured failures, and what is deliberately not done |
| [`app/schemas/DESIGN.md`](app/schemas/DESIGN.md) | §8 = the query schemas, decision by decision |
| [`app/semantic/DESIGN.md`](app/semantic/DESIGN.md) | the curated alias layer |
| [`app/db/DESIGN.md`](app/db/DESIGN.md) | ORM models, layout |
| [`app/db/roles.py`](app/db/roles.py) | the two read-only roles, and which half of them is a real boundary |
| [`LOADER.md`](LOADER.md) | the load step |
| [`ALEMBIC.md`](ALEMBIC.md) | migrations — note step 5, the test database is NOT migrated automatically |
| [`evals/README.md`](evals/README.md) | eval tag vocabulary, how to add questions, how to run the set |
| [`evals/run.py`](evals/run.py) | the runner — a tally and one line per question |
| [`evals/walkthrough.py`](evals/walkthrough.py) | the same run, written out per question in full |
| [`sec-retriever.md`](sec-retriever.md) | the original project brief |

## 10. The web end (step 3), at a glance

Built 2026-09-27 in eight slices, each committed on its own; every decision is
in [`app/api/DESIGN.md`](app/api/DESIGN.md) (§12a lists the user's answers).

| piece | what it is |
|---|---|
| `app/chain.py` | `ask()` / `ask_again()`: one round, parse → map → answer → present, every outcome from any stage a reply part; the reader's four fixed failure sentences; `Pending`, what the next round needs |
| `app/presenter/` | [F] `ResultSet → AnswerView`, no model; its own `DESIGN.md` |
| `app/api/storage.py` | conversations and jobs as `vf_web_role`; rounds chain through their stored picks |
| `app/api/jobs.py` | `JobRunner`: one job at a time, stages saved and streamed (SSE), trace always written |
| `app/trace.py`, `app/api/trace.py` | the per-job trace — collected while it runs, written write-only, read with the owner's CLI |
| `app/api/auth.py`, `server.py`, `routes.py`, `admin.py` | sign-in by invitation (FastAPI Users), `create_app()`, the question routes, the owner's CLI |
| `app/api/schemas.py`, `app/schemas/answer_view.py` | the wire contract the Angular types will be generated from |
| `app/db/web.py`, migration `70e7ca6e7347` | the `web` schema, applied to both databases |
| `app/db/roles.py` `WEB`, `session.web_sessionmaker()` | `vf_web_role` — writes `web` only, never falls back to the owner |
| `api.Dockerfile`, `docker-compose.yml` `api` | the container, behind the `web` profile |

**Running it.** On the host (reloads on edits):

```bash
uv run uvicorn app.api.server:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

Or as it will be deployed:

```bash
CODE_VERSION=$(git rev-parse --short=12 HEAD) docker compose --profile web up -d --build api
```

Then `http://localhost:8000/docs`. Sign-up is by invitation:
`uv run python -m app.api.admin invite --email <address>` prints a code once.

**Debugging a reader's question.** Every job leaves a trace — full prompts, raw
replies, every SQL statement, timings, errors — that only the owner can read:
`uv run python -m app.api.trace <job_id>`, `--flagged` (a reader pressed
"report a problem"), `--failed`. The real `web` tables were empty at handoff:
every live check used the test database or a user deleted afterwards.

## 11. Where to pick up — remaining steps

### Step 4: the Angular Web Client ([A]) — next

Put the plan and these questions to the user before building:

- **Chart library — decided: ApexCharts 7.6.1, pinned exactly, behind one
  chart component** that nothing else in the client bypasses, so it can be
  swapped by rewriting that component (DESIGN §5). Tables: Angular Material.
- **Types from `/openapi.json`**, generated with `openapi-typescript`
  (decided), never hand-written (DESIGN §9).
- **Slice 0 done** (server, 2026-09-28): `GET /api/conversations/{id}`
  reopens a past conversation as its thread (DESIGN §4), and `/openapi.json`
  publishes the event stream's types (DESIGN §9). Next: slice 1, `web/`.
- **Development**: `ng serve` proxying `/api` to `:8000`, one origin, so the
  cookie and the event stream need nothing extra.
- **GitHub's redirect**: today it returns to the API, which sets the cookie
  and shows a blank page. With a client it should land on a client page that
  calls the API's callback — change `GITHUB_OAUTH_REDIRECT_URL` and add that
  URL to the GitHub OAuth app (up to 10 are allowed); no code change.
- **What the client must render faithfully**: parts (answered / asked /
  refused) and a blocking refusal; asks as questions with option buttons and
  an **"ambiguous" tag** on `kind: ambiguity`; `notes` above the views and
  `conditions` stated; citations with every answer (flag `resolved_by:
  embedding` as unreviewed); `display` strings for every figure (the client
  formats only axis ticks, by `unit_kind`); live stages from
  `EventSource('/api/jobs/{id}/events')`; history; "report a problem".
- **Sign-in pages**: login, register with an invite code, GitHub.

### The deployment step — after step 4, host not decided

DigitalOcean is likely but **not decided**, and the user is not ready to be
walked through deployment. Until then, keep it in mind as a constraint:
flag anything built now that would not run on a rented Linux VM with Docker
(a DigitalOcean Droplet or similar). Recorded in DESIGN §12 and §13; none of
it is done:

- a **production compose override**: publish only the reverse proxy's 443
  (and 80 to redirect) — `docker-compose.yml` publishes db and Ollama on
  loopback, which a server must not; drop `db-test` and `pgadmin`; replace
  the committed `postgres`/`postgres` and pgAdmin's `admin1234`; secrets from a
  secret store rather than readable with `docker inspect`;
- a **reverse proxy** (nginx or Caddy, with TLS) serving the Angular bundle
  and proxying `/api`, response buffering off for the event stream;
- a **second GitHub OAuth app** for production, with the `https` callback;
- **rate limiting, including login brute-force protection — required before
  the site is reachable from outside** (deferred until now by the user);
- a shorter `AUTH_SESSION_DAYS` (the user expects to change 30);
- a **GPU** for Ollama on the server — Qwen on a CPU is an order of
  magnitude slower (BOOTSTRAP §2);
- optionally an **email sender**, which turns on verification and reset.

### Deferred by the user, to pick up when they ask

- **Evals onto `app/chain.ask`**, in their testing update — until then the
  evals measure their own copy of the chain.
- **Retire Qwen as a SQL writer** (§6 TODO): averages and other derivations
  are still Qwen's; the Presenter guards the average's display meanwhile.
- **The questions that come back wrong** (§6 table). Two had task chips in the
  previous chat, which will not carry over — their details are in that table:
  q057's dropped year, and "by quarter" with a year range widening to every
  year.
- **The 10-K/A gap** (below): not to be reopened unprompted.

### Small and stale

- `BOOTSTRAP.md` §2 says "324 passing"; it is 815.
- Mapper refusal wording written for a developer reaches readers verbatim
  (q028, q049 in the §6 table).
