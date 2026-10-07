# Testing

Three instruments, measuring different things:

| | what it proves | needs |
|---|---|---|
| **pytest** (`tests/`) | the code does what it says, against a real test database | `db-test` migrated to head |
| **Web Client tests** (`web/`) | the client renders real replies and its types match the server | nothing running |
| **The eval set** (`evals/`) | the chain makes the right *decision* on real questions — answer, ask or refuse | `db` loaded, Ollama up |

None of them checks that a figure is *right*. The eval set grades whether
answering was the right call; a `pass` says nothing about the number.
[Verifying figures yourself](#verifying-figures-yourself) is how that is done.

---

## Commands

```
uv run pytest -q                     all Python tests (~1 minute)
uv run ruff check .                  lint
uv run python evals/run.py           the eval set, one line per question
uv run python evals/walkthrough.py   the eval set, written out in full
```

From `web/`:

```
npm test                             types current, then the unit tests
npm run lint
```

In Claude Code, `/walkthrough` runs the whole walkthrough in its own window
and reports the tally.

---

## The Python suite

### What it needs

`db-test` up and **migrated to head** — it is a separate server that nothing
migrates automatically ([STARTUP.md](STARTUP.md) step 4). A missing migration
reads as a broken test suite, not as a missing step. The suite provisions all
four database roles on the test database itself; it never touches `db`.

### How it isolates itself

- `tests/conftest.py` redirects `sic_numbers.json` and the `data/xbrl/` store
  to temporary paths for every test, so the real files are never read or
  written.
- The fake company (cik `9999999`, ticker `ZZZZ`, concepts named `ZzzTest…`)
  is deleted before and after each test that loads it.
- Web tests create users whose email starts `zz-` and delete them, with
  everything under them, before and after.
- No test calls a model or the SEC: the SEC client runs on
  `httpx.MockTransport`, and model clients are stubbed. A test that needs a
  language model to agree with it is not a test.

### Fixtures (`tests/fixtures/`)

| fixture | what it is |
|---|---|
| `xbrl_fake_company.json` | one fake filer built to exercise the load step: an instant and a duration fact, a restatement (same period, two filings — proves `is_latest`), a unit outside the allow-list, a concept with no label, two taxonomies, a negative value, a `frame`. 10-Ks only, so it has no Q3 and the view synthesizes no Q4 for it |
| `metric_aliases_fake.yaml` | an alias file over the fake company's concepts, so the mapper's resolver runs against loaded rows |
| `chain/*.json` | live runs of the chain — the `QueryIn`, `QueryPlan` and `ResultSet` each produced — replayed by stubs, so chain, route and job tests need no model |
| `presenter/*.json` | captured `ResultSet`s, one per eval question shape, for the Presenter |
| `role_statements.sql`, `role_statements_web.sql` | every statement `roles.provision()` sends, pinned. A dropped `REVOKE` may not fail a behaviour test; it fails here |

A deliberate change to what provisioning sends regenerates a pinned file, and
the file's diff is the review:

```
UPDATE_PINNED=1 uv run pytest tests/test_role_statements.py          # web role
UPDATE_PINNED_READERS=1 uv run pytest tests/test_role_statements.py  # the two readers
```

(PowerShell: `$env:UPDATE_PINNED=1; uv run pytest tests/test_role_statements.py; Remove-Item Env:UPDATE_PINNED`.)

### Generated files pytest keeps current

Committed, never edited by hand; a test fails while either is out of date.

| file | regenerate with |
|---|---|
| `web/openapi.json` — the server's contract | `uv run python -m app.api.openapi` |
| `web/src/app/conversation/fixtures/replies.json` — real replies for the client's tests | `uv run python -m tests.web_replies` |

---

## The Web Client's tests

`npm test` runs `api:check` first — fails while `src/app/api/openapi.d.ts`
differs from what `openapi.json` generates (`npm run api:types` to
regenerate) — then the Vitest unit tests once. So a model changed on the
server fails pytest, then the client's tests, then the client's build at
every line that no longer fits.

The client is tested against **real replies** (`replies.json`, above), not
hand-written ones. Test-only helpers end in `.testing.ts`, which
`tsconfig.app.json` keeps out of the build.

Regenerating the types also triggers the TypeScript-override check in
[TOOLS.md](TOOLS.md#the-typescript-override).

**Checking by hand on the running site.** The real `web` tables hold the
owner's own account, so a live check signs up a `zz-…@example.com` account
and deletes it afterwards.

---

## The eval set

`evals/questions.yaml` holds questions a person might actually ask, tagged by
what answering them takes. It was built to replace intuition with a
distribution — how much of the work is plain retrieval and how much is
arithmetic or ranking — and it is the regression measure for the chain.

**Expected answers are deliberately not recorded.** The point is the shape of
a question, not its number; pinning numbers would make this a brittle
regression suite rather than a design instrument. Write questions the way you
would actually ask them, including ones the system should refuse. Do not
soften a question to fit what the code can do.

### Running it

```
uv run python evals/run.py                        every question, answered and graded
uv run python evals/run.py --id q007 q009         just these
uv run python evals/run.py --stage map            stop at the plan: no Qwen
uv run python evals/run.py --stage parse          stop at the parse: no database
uv run python evals/run.py --include-known-gaps   also the questions marked known_gap
uv run python evals/walkthrough.py                every question, written out
uv run python evals/walkthrough.py q020 q029      a range
uv run python evals/summarize.py                  the tag distribution
```

`run.py` prints a tally and one line per question, and writes
`data/eval_runs/<stamp>-answer.json` (gitignored). `walkthrough.py` writes
`data/question-walkthrough.md` (gitignored): per question, every element the
parser produced, expected against got item by item, the rows with their units
and windows, the provenance, every note, any exception verbatim, and the time
split parse / map / answer. It rewrites the file after each question, so it is
readable mid-run. The whole set takes about five minutes. Both share one
scorer, so they never disagree on a grade.

`run.py` calls the parser, mapper and executor itself rather than
`app/chain.ask`, so it measures its own copy of the chain; moving it onto
`ask` is in [FUTURE.md](FUTURE.md).

### Grades

| grade | meaning |
|---|---|
| `pass` | every item did what its `expect` entry says |
| `fail` | some item did not — usually a refusal of something answerable. Costly, but safe |
| **`unsafe`** | an item marked `refused` or `asked` came back **answered** |
| `ungraded` | a `--stage parse` or `--stage map` run, or a question with no `expect` |

**`unsafe` is the grade to watch.** Every other failure costs a refusal, which
this project prefers; `unsafe` puts a figure in front of someone where the
honest reply was a decline or a question. It is counted apart from `fail` and
printed last.

Scoring rules worth knowing:

- **One entry per item.** `expect` is a list, one entry per metric the question
  names: "assets, liabilities, equity, cash, goodwill, inventory" is six asks,
  and five can succeed while the sixth fails. One bad item fails the question.
- **A blocking refusal spreads; a metric's does not.** A refusal on a company
  or a period refuses every item (q036, "Apple vs Samsung"). A refusal on one
  metric refuses only that item (q052's goodwill).
- **`confused` never passes.** It means the term was not understood — the
  parser failed, or the mapper offered raw XBRL candidates. `confused` and
  `error` are observed, never written as an expectation.
- **A length mismatch cannot pass.** An element the question never named is
  padded in as "(not expected)".
- **A ranking read backwards fails** whatever its items say.

### The question file's vocabulary

Only `id`, `question` and `expect` are needed. `tests/test_evals.py` checks
every tag against this list, so a typo fails the suite.

| field | values | meaning |
|---|---|---|
| `expect` | list of `answered`, `asked`, `refused` | one per item, in order |
| `expect_by_company` | `{Company: [...]}` | on a template, replaces `expect` for that filer item by item — always with a comment saying why, checked against the data |
| `template` | `true` | the question uses `<Company>`, substituted with Apple (`TEMPLATE_COMPANY`) |
| `rank` | `highest`, `lowest` | a ranking's direction; "largest decline" is `lowest` |
| `shape` | `scalar`, `series`, `table`, `ranking` | how much data the answer needs — a chart needs a point per period per company |
| `known_gap` | a sentence (10+ words) | skipped by default and listed apart; the sentence is what someone reads to decide whether to re-open it |
| `needs` | `retrieval`, `aggregation`, `ranking`, `growth`, `ratio`, `cross_company_total`, `multi_step` | what answering takes. Every question expecting an answer needs `retrieval` |
| `blocked_by` | `segment_data`, `filing_text`, `out_of_scope_period`, `not_in_dataset`, `causal`, `company_not_loaded`, `sector_classification` | why it cannot be answered. Required on a question expecting only refusals |
| `exercises` | `q4_residual`, `concept_drift`, `fiscal_calendar`, `derived_metric`, `instant_fact`, `alias_gap`, `clarification`, `mixed_granularity` | which known hazard it probes ([GAPS.md](GAPS.md#data-hazards)). `alias_gap` marks a question that should work and does not for want of an alias entry — the curation queue |

### The prompt cache

**A question's grade depends on Ollama's prompt cache. Do not re-run a
question N times to measure non-determinism — repeats measure the cache, not
the model.** Measured 2026-09-26:

- Temperature is 0 and Qwen is deterministic **for a given cache state**: 41
  calls of one parser prompt were byte-identical.
- The cache state changes the reply. Once Ollama has cached a longer request
  that *begins with* a prompt, that prompt gets a different (equally stable)
  reply. The parser's repair prompt is exactly that, so **a question behaves
  differently after its own repair has run once.**
- So re-running a question you just ran is optimistic: q048 fails every time
  from a cold model and passes every time once its repair is cached.
- A full run asks each question once, so it is the closer measure — but a
  question can still inherit cache state from the ones before it.

To see a question as a first-time asker would, unload the model, then run it
once. To compare before and after a change, do that for both:

```
docker exec verified-filings-ollama-1 ollama stop qwen2.5-coder:7b
```

A prompt claim needs a rate over several cold runs, not one observation.

---

## Verifying figures yourself

Ad-hoc SQL as the owner (`DATABASE_URL`, or pgAdmin): `xbrl.fact` joined to
`xbrl.concept` / `xbrl.filing` for everything, `xbrl.reported_fact` for what
retrieval sees. Patterns worth reusing:

- **A filing's own window:** `DISTINCT ON (company, fiscal_year,
  fiscal_period)` ordered by `period_end DESC, (period_end - period_start)
  ASC` — the shortest span breaks ties, so a discrete quarter beats the
  year-to-date.
- **Restatements:** group by `(company, concept, unit, period_start,
  period_end)` and count distinct values. Leaving out `period_start` is a
  trap — it compares 3-month and 9-month windows that share an end date.
- **Concept drift:** per company, per alias slot, the fiscal years each
  alternative covers. A slot where no single alternative covers them all is a
  drift case.
- **Signs and units:** group by concept, count companies with positive versus
  negative values, or count distinct units.

Figures to check arithmetic against, each computed independently from the
database: Apple FY2024 revenue 391,035,000,000; Q4 FY2024 94,930,000,000;
gross margin 0.462063; free cash flow 108,807,000,000.

A live check that exercises chart cardinality, Q4, concept drift and calendar
misalignment at once:

> Show me visually how much money was made from 2023 to 2025 by quarter, for
> Google, Apple and Nvidia

Expected plan: `shape=series`, `axes=['company','period']`, `row_count=36`,
four bindings (Alphabet changes revenue tags mid-range), a
`period_misalignment` note, and `complete` 36 of 36. Apple's Q4s come back
89.5B / 94.9B / 102.5B. "How much money was made" is a curated question, so
the reply first asks which figure is meant.

### Reading a reader's question

Every job the Web Server runs leaves a trace — full prompts, raw replies,
every SQL statement, timings, errors — readable only with the owner's
credential:

```
uv run python -m app.api.trace <job_id>
uv run python -m app.api.trace --flagged     jobs a reader reported
uv run python -m app.api.trace --failed
```
