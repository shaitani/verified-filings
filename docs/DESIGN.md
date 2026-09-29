# Verified Filings — design

A system that answers natural-language questions about SEC financial data
with figures that are actually correct, and refuses — plainly — when it
cannot. Every figure in an answer is a filed XBRL fact, or arithmetic over
filed facts that the answer shows.

**The premise: a plausible wrong number is worse than a refusal.** Nearly
every decision below traces back to it. XBRL is far less uniform than it
looks, and most of the hard problems are data problems, not code problems —
catalogued with measurements in [GAPS.md](GAPS.md#data-hazards).

This file is the system. Each package's own decisions are in its `DESIGN.md`
([doc map](#doc-map)).

---

## 1. Scope

- **20 US filers**, fixed in `corpus_companies.json` — a closed list, never
  expanded. Three are substitutes for companies that are not SEC filers or
  file under IFRS (QCOM for Samsung, CVX for Exxon, JPM for HSBC); two banks
  (JPM, BAC) deliberately stress metrics like gross profit that banks do not
  have. Details: [sec-retriever.md](sec-retriever.md).
- **Fiscal years FY2021–FY2025**, one fixed window for every filer so
  cross-company questions share a grid.
- **10-K and 10-Q only.** ~174,000 facts, `us-gaap`, `dei` and `srt`
  taxonomies.

**Vocabulary.** The SEC endpoint `api/xbrl/companyfacts/CIK….json` is called
**"XBRL data"**, never "companyfacts". **Retrieval** is SEC → files on disk
(`app/ingest/`); **load** is files → database (`app/db/loader.py`). ("Ingest"
names the package only.)

## 2. The chain

The components have letter names so they can be referred to unambiguously.
Package and block names differ in two places, knowingly.

| | block | package | what it is |
|---|---|---|---|
| [A] | Web Client | `web/` | Angular. Renders what [B] sends; decides nothing |
| [B] | Web Server | `app/api/` | FastAPI. Runs the chain, owns users, conversations and jobs; the only thing that talks to [A] |
| [C] | Query Parser | `app/parser/` | question → `QueryIn`, with Qwen |
| [D] | Query Mapper | `app/semantic/` | `QueryIn` → `QueryPlan` |
| [E] | Executor | `app/retrieval/` | `QueryPlan` → SQL → `ResultSet` |
| [F] | Presenter | `app/presenter/` | `ResultSet` → `AnswerView`, no model |
| [G] | Store | `app/db/` | PostgreSQL: the `xbrl` and `web` schemas, roles, migrations |
| [H] | Ingest | `app/ingest/` | SEC client, rate limit, disk cache, the curated files |

```
SEC XBRL data endpoint
   ↓  [H] app/ingest/        fetch (rate-limited, cached), scope to 10-K/10-Q + 5 FYs
data/xbrl/<TICKER>.json      curated per-company files (gitignored)
   ↓  app/db/loader.py       validate (app/schemas/xbrl.py), load
PostgreSQL, schema xbrl      company / filing / concept / fact / load_run
   ↓  app/db/embedder.py     embed every concept (nomic-embed-text)

browser
   ↕  [A] web/               REST + server-sent events, same origin, cookie session
   ↕  [B] app/api/           one job at a time; app/chain.py runs one round:
   ↓  [C] app/parser/        question → QueryIn           Qwen proposes, code decides
   ↓  [D] app/semantic/      QueryIn → QueryPlan          reads as vf_query_mapper_role
   ↓  [E] app/retrieval/     QueryPlan → ResultSet        Python writes the SQL;
                                                          reads xbrl.reported_fact
                                                          as vf_retrieval_role
   ↓  [F] app/presenter/     ResultSet → AnswerView       plain Python
```

`app/chain.py`'s `ask()` runs parse → map → answer → present for one round;
`ask_again()` runs a round that answers a question put back.

## 3. A reply has parts

A question is answered **per part**. "Apple's assets, liabilities, goodwill"
is two figures and a refusal; "Apple's margins and revenue" can be a question
back *and* a figure. So there is no three-way branch — answer / clarify /
refuse. There is one reply, and every part of it says what became of it:
`answered`, `asked` or `refused`.

- A refusal of a **metric** refuses only that part. A refusal of **scope** —
  a company, a company group, a period — refuses the whole question, because
  every figure depends on it.
- The answered parts come out of one SQL statement, so an unanswerable
  verdict refuses all of them — partial trust is not on offer — but leaves the
  mapper's refusals and questions standing.
- **Refusals and questions never pass through a model on the way out.** A
  mapper reason is a curated sentence written for a person and goes through
  verbatim. A parser or executor error message is written for a model or for
  debugging; the reader gets a fixed sentence per stage, and the message goes
  to the job's trace.

Details: [app/api/DESIGN.md](../app/api/DESIGN.md) §1,
[app/semantic/DESIGN.md](../app/semantic/DESIGN.md) §8e.

## 4. `QueryIn` → `QueryPlan`

**`QueryIn`** speaks the *asker's* language — `"revenue"`, never
`us-gaap:Revenues`. Its elements are a discriminated union on `kind` (metric,
company, company group, period, metric qualifier, metric threshold,
narrative), because each needs a different resolver: embedding search over
"Apple" returns noise.

**`QueryPlan`** is the real artifact of this project: concrete concept ids,
per-company date windows, units, instant or duration, **proof the facts
exist**, and the caveats that must reach the reader. The design intent is that
the SQL step has nothing left to guess.

The mapper's three resolvers, in order:

1. **Companies** — deterministic: the derived name lexicon
   (`company_aliases.json`, "GOOG" is Alphabet, "Facebook" is Meta), then
   ticker and name. A question naming no company means every loaded filer; a
   company element that *fails* never widens the scope.
2. **Periods** — concrete date windows *per company*, never fiscal-year
   integers (D1.1).
3. **Metrics** — curated alias → embedding fallback → **coverage against the
   view**, which is the arbiter of both.

**Coverage is the load-bearing idea.** Zero rows from a well-formed query is
indistinguishable from "the company reported nothing", so a binding has to be
*proven* before it is made. It is proven against `xbrl.reported_fact` — the
same relation retrieval reads — so the mapper cannot bind something retrieval
cannot fetch. A candidate with no facts for the requested windows is never
bound, whatever its similarity.

**A filer that covers nothing does not refuse the question.** JPMorgan tags no
`OperatingIncomeLoss`, correctly for a bank; refusing "highest operating
margin" because one filer of twenty cannot take part is worse than answering
over nineteen and saying so, in a `partial_coverage` note. A ranking gets an
extra sentence, because dropping a company can change the answer. The
exception: a comparison or ranking over **named** companies left with fewer
than two refuses — half a comparison is a different answer.

Detail: [app/schemas/DESIGN.md](../app/schemas/DESIGN.md) §8 (the models),
[app/semantic/DESIGN.md](../app/semantic/DESIGN.md) (the mapper).

### The alias layer

[`app/semantic/metric_aliases.yaml`](../app/semantic/metric_aliases.yaml) is
curated accounting judgment as **data, not code**. Each operand slot lists
alternatives in preference order and coverage picks per company, so filer
divergence resolves without per-company tables: Apple binds
`RevenueFromContractWithCustomerExcludingAssessedTax` and NVIDIA `Revenues`,
from one entry. An entry does exactly one of three things: **resolves**
(`terms`), **asks** (`clarify` — a curated question with named choices), or
**declines** (`unavailable` — a curated reason). It may attach a per-concept
`caveat`, which becomes a `narrower_than_asked` note.

Similarity cannot separate right from wrong: over 221 labelled cases, right
answers scored 0.675–0.851 and wrong ones 0.556–0.810. The embedding path is
made less bad (a 0.75 bar, a 0.65 floor below which nothing is offered), not
safe. **The fix for a term people keep asking is a curated entry.** The user
is not an accountant; curating this file is collaborative, and it is the main
lever on answer quality.

**When a question should come back `asked` and does not**, it reaches a
curated question by one of two routes:

1. Its phrase is listed as a synonym of a `clarify` entry — exact phrase after
   normalization, so list the wordings people use. Checked first.
2. The parser names the entry in `clarify_as`, for a vague phrase the file
   does not list ("How did Apple *fare*" → `performance`). It can only ever
   produce a question, never a figure.

So: pick or add the `clarify` entry whose question fits, add the common
wordings as synonyms, and let `clarify_as` catch the rest. A new entry needs
no code. Do **not** route vague phrases by similarity: phrases that deserve a
question (0.48–0.68) and phrases that deserve a refusal (0.49–0.61) overlap
completely. Detail: [app/parser/DESIGN.md](../app/parser/DESIGN.md) §10.

### Two things the plan tracks that are easy to miss

`ResultSpec` says how much data the answer needs — `shape`, the `axes` it
varies along, and `row_count`, which retrieval must not come in under. A chart
of 3 companies over 12 quarters is 36 rows, and nothing else says so.
`granularities` tracks annual against quarterly: a 363-day value and a 90-day
one are not comparable points.

## 5. The parser: the model proposes, the code decides

Nothing Qwen returns is trusted. `accept()` re-derives a `QueryIn` from the
reply and refuses anything that does not fit, and the question handed on is
the caller's string, never the model's restatement.

**The faithfulness gate** is the safety argument for running a 7B model here:
every metric, company and company-group element's `text` must appear verbatim
in the question (or in the asker's answer to a question put back), checked in
code rather than asked for in the prompt. Every dangerous parser mistake is a
**paraphrase** — the mapper resolves the replacement perfectly, coverage
proves it, the verdict says `complete`, and a real figure comes back under a
label it does not fit. So the words are not allowed to change. Periods are
exempt: their meaning lives in typed fields.

## 6. The Executor: Python writes the SQL

| function | does | does not |
|---|---|---|
| `build_prompt` | assembles text for Qwen | write SQL |
| `generate` | the only thing that talks to Qwen | judge or run the reply |
| `validate` | judges, returning its input byte-identical | run it, or edit it |
| `execute` | the only thing that touches the database | anything else |

Every value an answer reads is written in Python from the plan: the
coordinates (`wanted`) and each cell's figure — fetched, combined by the
metric's expression, filtered by a threshold, computed over time, ordered for
a ranking — in a second CTE, `figures`. A plan that computes nothing above its
cells never reaches Qwen. Only an aggregate or another derivation gets a short
prompt showing `figures`, and Qwen writes the layer above it.

**Taking work away from the model is the lever that keeps working.** Every
measured failure of the model-written SQL was fixed for good only by moving
that piece into Python; every prompt rule names a candidate.

The fence around what Qwen writes has two halves, and either alone is a fence
with no posts:

- **`xbrl.reported_fact`**, the one relation retrieval can read. It bakes in
  `is_latest` and the joins, exposes **no** `fiscal_year` (the D1.1 trap,
  closed by absence), and synthesizes the fourth quarter (D1.4).
- **`vf_retrieval_role`**, which can read that view and nothing else.

`validate` parses with PostgreSQL's own grammar and enforces what the role
cannot: one `SELECT`, no `SET` or `set_config` (both are `USERSET`), only the
view, the exact projection, a `LIMIT` within the row cap.

**The verdict: answer only when the shortfall was already disclosed.** Coverage
was proven before binding, so a cell missing afterwards is a fault in the
statement, not the filings — refuse. More rows than promised means a join
fanned out — refuse. A gap the plan disclosed up front is not news — answer,
with the note. Detail: [app/retrieval/DESIGN.md](../app/retrieval/DESIGN.md).

## 7. The Presenter: deterministic, not prose

`ResultSet → AnswerView`, a typed structure [A] renders as a stat card, chart
and table, written in Python with no model. A model retyping 94,930,000,000 is
a new place for a plausible wrong number, and "carry every note through" is
the one rule a summarizer reliably breaks. Unit, derivation and granularity
decisions stay in Python, where they are tested against the data: each cell
carries the exact value (as a string), a display string and its unit kind, and
[A] shows what it is given. The Presenter never sees a plan; what it needs
travels on the `ResultSet`. Detail:
[app/presenter/DESIGN.md](../app/presenter/DESIGN.md).

## 8. The web end

| piece | what it is |
|---|---|
| `app/chain.py` | one round: parse → map → answer → present, every outcome a reply part; the four fixed sentences a reader gets when a stage fails; `Pending`, what the next round needs |
| `app/api/jobs.py` | `JobRunner`: one job at a time (one GPU), stages saved and streamed as server-sent events; the trace is always written |
| `app/api/storage.py` | conversations and jobs, as `vf_web_role`; each round chains through the picks stored with it |
| `app/trace.py`, `app/api/trace.py` | the per-job trace — full prompts, raw replies, every statement, timings, errors — written write-only, read with the owner's CLI |
| `app/api/auth.py`, `admin.py` | sign-in by invitation (FastAPI Users, email/password or GitHub); the owner's CLI |
| `app/api/schemas.py`, `app/schemas/answer_view.py` | the wire contract; the client's TypeScript types are generated from it |
| `app/db/web.py` | the `web` schema |
| `api.Dockerfile`, `docker-compose.yml` `api` | the server's container, behind the `web` profile |
| `web/` | the Angular client |

A question takes ~10 s on average and ~60 s at worst on one GPU, so a question
is a **job**: the browser gets an id at once and follows its stages
(`queued` → `parsing` → `mapping` → `fetching` → `presenting` → `done` or
`failed`) over an event stream. The database is the record: a reload re-reads
the conversation. Detail: [app/api/DESIGN.md](../app/api/DESIGN.md).

## 9. Who can do what in the database

Three login roles besides the owner, provisioned by `app/db/roles.py` — not by
migrations, because a role is a cluster object whose password has no place in
version control.

| role | used by | may |
|---|---|---|
| `vf_query_mapper_role` | [D] | read the `xbrl` base tables and embeddings |
| `vf_retrieval_role` | [E] | read `xbrl.reported_fact` only |
| `vf_web_role` | [B] | write in `web` only, by a per-table grid; never set `is_superuser`, create an invite, or read a trace |

The grants are the boundary. The readers' session settings
(`default_transaction_read_only`, `statement_timeout`) are `USERSET`, so they
guard against accident, not attack; `validate` covers what they cannot.

## 10. Rules that hold everywhere

- **Take the job away from the model rather than check its work.** A value
  Python writes cannot be mistranscribed.
- **Refuse when the number would be wrong; note when it is right but needs
  context.**
- **Show the thing it must produce.** Every prompt failure measured shared one
  shape: the model did something reasonable the prompt did not rule out. A
  worked example of the exact form beats a rule; a contrasting pair of
  examples holds a line that prose does not.
- **Do not build a stand-in for a real component.** A surrogate gets measured
  and tuned, and then the real thing behaves differently.
- **Verify empirically before asserting.** Every number in these docs came
  from a probe against the real database or a measured run.

---

## Doc map

| doc | what |
|---|---|
| [STARTUP.md](STARTUP.md) | bring everything up, in order |
| [GAPS.md](GAPS.md) | known gaps, wrong answers, and every data hazard |
| [FUTURE.md](FUTURE.md) | planned and deferred work |
| [TESTING.md](TESTING.md) | test suite, eval set, walkthrough, checking figures |
| [TOOLS.md](TOOLS.md) | every tool and library, and why |
| [BOOTSTRAP.md](BOOTSTRAP.md) | why startup is in that order, what a wipe destroys, and checks |
| [ALEMBIC.md](ALEMBIC.md) | writing and applying a migration |
| [LOADER.md](LOADER.md) | the load step |
| [sec-retriever.md](sec-retriever.md) | the SEC client's rules, the corpus, the curated files |
| [../HANDOFF.md](../HANDOFF.md) | work in progress |
| [app/api/DESIGN.md](../app/api/DESIGN.md) | [A] [B]: replies, questions back, jobs, drawing, citations, trace, sign-in, the `web` schema, containers |
| [app/parser/DESIGN.md](../app/parser/DESIGN.md) | [C]: the faithfulness gate and the parser's measured failures |
| [app/semantic/DESIGN.md](../app/semantic/DESIGN.md) | [D]: the alias layer and how the mapper decides |
| [app/schemas/DESIGN.md](../app/schemas/DESIGN.md) | the typed contracts, decision by decision |
| [app/retrieval/DESIGN.md](../app/retrieval/DESIGN.md) | [E]: the result contract, the view, the validator, lessons from model-written SQL |
| [app/presenter/DESIGN.md](../app/presenter/DESIGN.md) | [F]: display strings and views |
| [app/db/DESIGN.md](../app/db/DESIGN.md), [SCHEMA-MAP.md](../app/db/SCHEMA-MAP.md) | [G]: the `xbrl` tables |
