# `evals/` — what real questions actually require

`questions.yaml` is a set of questions someone might ask this system, tagged by
what answering them would take. It exists to replace intuition with a
distribution: the open decision it was built to settle is **how much of the
work is plain retrieval, and how much is aggregation, ranking or arithmetic
that a `QueryPlan` does not describe** — which in turn decides whether the SQL
layer is a deterministic emitter, a language model, or both.

Nothing here is a fixture. Expected *answers* are deliberately not recorded:
the point is the shape of the question, not the number, and pinning numbers
would turn this into a brittle regression suite instead of a design instrument.

Run `uv run python evals/summarize.py` for the current distribution.

## Running the set

```
uv run python evals/run.py                  # all 56 through [C] -> [D] -> [E]
uv run python evals/run.py --stage map      # stop at the plan; no Qwen
uv run python evals/run.py --stage parse    # stop at the QueryIn; no database
uv run python evals/run.py --id q001 q026   # just these
```

`run.py` is block [B] with the browser, the conversation state and the
presenter taken out. A full `--stage answer` run takes about fifteen minutes
and writes `data/eval_runs/<stamp>-answer.json`, which is gitignored: a run
measures one checkout against one database at one moment.

**It grades the decision, not the number** -- whether the chain answered, and
whether answering was the right call. Consistent with the rule above: expected
answers are not recorded here, and adding them would make this a regression
suite instead of a design instrument.

| grade | meaning |
|---|---|
| `pass` | the chain did what the `expect` tag called for |
| `fail` | it refused something it should have answered. Costly, but safe |
| `unsafe` | it **answered** something tagged `refuse` or `clarify` |
| `untriaged` | `expect` is `unknown`; left out of the tally |

`unsafe` is counted apart from `fail` and printed last, because the two are
not comparable. Only a full `--stage answer` run is graded: [C] deliberately
does not judge whether a question is answerable at all, so scoring a
parse-only run would measure the wrong component.

Questions written with a `<Company>` placeholder are substituted with Apple,
recorded on every run.

## Adding a question

Only `id` and `question` are required. Write the question the way you would
actually ask it — including ones the system should *refuse*, which are as
useful as the ones it should answer. Do not soften a question to fit what the
code can currently do; that defeats the purpose.

```yaml
  - id: q043
    question: How much did Meta spend on capex last year?
    expect: answerable
    needs: [retrieval]
```

`tests/test_evals.py` validates ids are unique and tags come from the
vocabulary below, so a typo fails the suite rather than quietly skewing a
count.

## `expect`

| value | meaning |
|---|---|
| `answerable` | the system should produce a complete answer |
| `partial` | some of it resolves, the rest must be reported, not hidden |
| `clarify` | the term is genuinely several things; **asking** is the correct behaviour |
| `refuse` | the data cannot support it; saying so is the correct behaviour |
| `unknown` | written down but not yet triaged — also what omitting the field means |

`clarify` was added 2026-09-22, after the first full run scored four questions
as failures for doing exactly what they were written to do. q046 is the case
it exists for: its own note says *"Deliberately ASKS rather than resolving."*
A curated `clarify` entry in `metric_aliases.yaml` is a designed outcome, not
a half-answer, and the vocabulary had no way to say so.

A `clarify` question that comes back **answered** is scored `unsafe`, not
`fail`. Reading a bare "profit margin" as net rather than gross is twenty
points on the same company, which is a wrong number, not a near miss.

`unknown` entries are listed separately by `summarize.py` and left out of the
tally, so an untriaged question never quietly skews a count. Write the question
first and classify later; requiring classification up front is how a set like
this stops growing.

A `refuse` is a success when the system declines clearly. The failure mode
worth catching is a confident answer built on the wrong facts.

## `shape` — what the answer has to be

Optional, and it changes *how much data* is needed rather than how it is drawn.
A question asking for a chart needs a point per period per company; the same
question asking for a figure needs one row. Mirrors `QueryIn.shape`.

| value | meaning |
|---|---|
| `scalar` | one number |
| `series` | one metric over an ordered axis, per entity — charts live here |
| `table` | several metrics side by side |
| `ranking` | entities ordered by one metric |

Deliberately separate from `needs`: mixing *what to compute* with *what to
return* is how the dimension got missed in the first draft of this file.

## `template`

Set `true` when the question uses a `<Company>` placeholder rather than naming
a filer. These measure coverage of a common ask rather than one concrete query,
and a runner has to substitute before executing them.

## `needs` — what answering takes, beyond fetching facts

| tag | meaning |
|---|---|
| `retrieval` | fetch values for known coordinates; every answerable question has this |
| `aggregation` | sum, average or count across rows |
| `ranking` | order and pick top / bottom |
| `growth` | period-over-period change or rate |
| `ratio` | one metric divided by another |
| `cross_company_total` | a figure spanning companies, which no single binding describes |
| `multi_step` | a filter that depends on a value computed first |

## `blocked_by` — why a question cannot be answered

| tag | meaning |
|---|---|
| `segment_data` | needs dimensional facts; companyfacts has none (PITFALLS 3.2) |
| `filing_text` | needs narrative from the filing, not XBRL facts |
| `out_of_scope_period` | outside the loaded fiscal years |
| `not_in_dataset` | the figure is not in the store at all |
| `causal` | asks *why*, which facts alone cannot answer |
| `company_not_loaded` | names a filer that was never ingested |
| `restatement_metadata` | needs the fact that a value changed (PITFALLS 2.2) |
| `sector_classification` | needs SIC or sector; `QueryIn` can express it, but the columns are unpopulated |

## `exercises` — which known hazard a question probes

`q4_residual`, `concept_drift`, `fiscal_calendar`, `derived_metric`,
`instant_fact`, `alias_gap`, `clarification`, `mixed_granularity`. Each maps to
a section of the root [`PITFALLS.md`](../PITFALLS.md).

`alias_gap` is the most actionable: it marks a question that *should* work but
does not, because `metric_aliases.yaml` is missing an entry. Those are the
queue for the next round of curation.
