# [F] Presenter — `ResultSet` → `AnswerView`

```
present(result: ResultSet, metrics: {element_id: asker's phrase}) -> AnswerView
```

Plain Python, no model, no database, no plan. Why it is deterministic rather
than prose, and what the client does with the output: `app/api/DESIGN.md` §2,
§5, §6. The output contract is `app/schemas/answer_view.py`, whose validators
run on everything this builds.

Tested against real `ResultSet`s captured from the chain
(`tests/fixtures/presenter/`), so its tests need no database and no model.

## 1. Inputs, and what it refuses

- **`result`** — the Executor's `ResultSet`. An unanswerable verdict is
  refused with `PresentationError`. [B] checks first; this is the second lock.
- **`metrics`** — each answered metric element's phrase, from `QueryIn`. The
  plan is not needed: the one thing the Presenter needs from it, the
  `ResultSpec` (shape, axes, ranking direction), rides on the `ResultSet`. A
  row for an element with no phrase is refused — it has no label to wear.
- **An unknown unit is refused.** `format.UNIT_KINDS` covers exactly the load
  step's `ALLOWED_UNITS`; guessing a format is how a ratio gets shown as money.
- **An unknown computation is refused.** A row's `derivation` must be filed
  (`None`), over time (`change`, `growth`, `cagr`, written in Python), or an
  aggregate (§4a). Anything else the model wrote has no known reading.

What it reads from the `ResultSet` besides the rows, all put there so it never
needs the plan: `ResultSpec` (shape, axes, ranking direction, **thresholds**),
each citation's **`display_as`** (from the curated alias), and each over-time
row's **`base`** — the period it was measured from, found by the same pairing
the statement was written with (`prompt.over_time_bases`).

`PresentationError` is always our fault, never the asker's: [B] reports it as
a failed job, with the detail in the trace.

## 2. The table

Every row of the `ResultSet` becomes a row of the table, which always ships —
charts point into it and are never a second copy of the figures.

Order: metrics in the order the asker named them. A ranked metric in rank
order, **sorted here from the values**, not trusted from the statement
(retrieval DESIGN §6); a row with no value cannot be ranked and goes last.
Anything else by company, then filed before derived, annual before quarterly,
then date.

A count the question put on a ranking (`ResultSpec.top_n`) is applied **after**
that sort, so the rows kept are the top because everything else was ranked
below them; retrieval still returns and proves the whole ranking. The count is
kept exactly and goes unremarked: the reader asked for it, and a sentence about
the cut would only tell them how many companies the dataset holds. A row with
no value sorts last, so it is kept only if the count reaches it.

**A question that named no companies** — every filer, or a group like
"semiconductor companies" (`ResultSpec.companies_named` false) — and stated no
count shows `SHOWN_LIMIT` (10) of what it asked for. Retrieval still fetches
and proves everything; only what is shown is cut, after ordering:

- a ranked metric keeps its first ten rows, so "worst quarters in 2023" shows
  ten quarters, one company more than once if it ranks so;
- anything else keeps ten companies, the same ones for every metric: the
  highest by the first metric asked for at its latest period. A company is kept
  or dropped whole, so no series is cut partway; an across-companies figure (an
  average) is one figure and is never cut.

A condition says why, never how many there were: "Limiting results to the 10
companies with the highest revenue", "Limiting results to the 10 lowest by
revenue change". It stays out of chart titles. Named companies are never cut:
twelve typed out are twelve shown.

## 3. Display strings

| unit | kind | shown as |
|---|---|---|
| `USD`, `EUR` | money | `$391.04B`, `$1.25T`, `$950,000` |
| `pure`, `Rate` | ratio | `46.2%`; a *change* in a ratio is points, `+0.7 pp` |
| `pure`, curated `display_as: multiple` | multiple | `0.89×` — a current ratio covers 0.89 times, it is not 89%; its growth is still `+12.0%` |
| `USD/shares` | per_share | `$1.02` — never scaled like money |
| `shares` | count | `15.12B shares` |

A change, growth or CAGR carries its sign (`+0.9%`, `-$32.13B`) and says what
it is measured from (`compared_with`: "vs Q3 FY2025"); a filed figure does not.
A balance is labelled by its date — "end of FY2025", and "as of 2025-06-30" on
a stat card — because it is a position at a moment, not a flow over a year. A derived row with no value (growth from a zero base) is `—`.
Display strings round (1 decimal for percentages, 2 for scaled money); the
exact value travels beside every one of them.

## 4. Views, by shape

| `ResultSpec.shape` | views |
|---|---|
| any, with exactly one row | one stat card |
| `ranking` | one bar chart per ranked metric, in the direction the parser recorded (`highest` → descending), capped at `BAR_LIMIT` (10) bars with "top 10 of 359" in the title |
| `series` | line panels, one per (derivation, unit kind, granularity), one line per (metric, company), each in date order |
| `table` along companies only | comparison bars — one figure per company, largest first, titled by what is compared ("revenue, FY2024"), never "highest first" |
| any other `table` | none: the table is the answer |

**A filtered list states its filter.** Each threshold becomes a sentence in
`AnswerView.conditions` ("revenue over $100.00B") and joins a ranking's title.
Without it, q039's eleven companies read as a ranking of everyone.

A line panel never holds a figure beside its own growth, money beside a
ratio, or a year beside a quarter — `AnswerView` refuses all three, so a
mistake here fails loudly rather than drawing a misleading chart. Two metrics
in the same unit share a panel (revenue and net income, q041).

## 4a. An aggregate across companies is one figure

q040 ("average R&D spend across these companies") comes back as the average
on **14 rows, one per company** — the model writes aggregates, and the
statement attaches the one figure to each company it spans. As a table that
reads as "Apple spent $15.27B, AMD spent $15.27B, …": a wrong number made by
the display, not the data.

So an aggregate (`average`, `sum`, `min`, `max`, `median`, …) over more than
one company becomes **one row**: `company_cik` null, "14 companies", and the
`companies` it spans listed. Only when that is provable — the same value on
every row, for every company the metric bound; otherwise it is refused. One
company's own aggregate stays that company's row.

The real fix is upstream: the parser's closed list of operations
([docs/FUTURE.md](../../docs/FUTURE.md#retire-qwen-as-a-sql-writer)) lets Python
write the aggregate as one row with no company at all.

## 5. What the fixtures show

Every eval question was run through the chain and here; six displayed
something a reader could misread or not see, each now covered above and pinned
by a fixture: the average on every company (`q040`), a current ratio as 89%
(`q022`), a filtered list that never said its filter (`q039`), a change with
no "vs" (`q020`, `q011`, `q013`), a two-company comparison with no chart
(`q006`), a balance labelled like a flow (`q002`).

Two worth knowing before reading a chart: the `smoke_mixed` fixture is a
misparsed question kept because its output is honest; q038's "largest
decline" is a Q4 → Q1 seasonal drop, correct for the words asked. Answers that
are wrong rather than badly shown: [docs/GAPS.md](../../docs/GAPS.md#questions-that-come-back-wrong).
Not done: [docs/FUTURE.md](../../docs/FUTURE.md#quality-of-answers), GAPS G9.
