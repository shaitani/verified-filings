# [F] Presenter — `ResultSet` → `AnswerView`

```
present(result: ResultSet, metrics: {element_id: asker's phrase}) -> AnswerView
```

Plain Python, no model, no database, no plan. Why it is deterministic rather
than prose, and what the client does with the output: `app/api/DESIGN.md` §2,
§5, §6. The output contract is `app/schemas/answer_view.py`, whose validators
run on everything this builds.

Built 2026-09-27 against twelve real `ResultSet`s captured from the chain
(`tests/fixtures/presenter/`), so its tests run with no database and no model.

## 1. Inputs, and what it refuses

- **`result`** — the Executor's `ResultSet`. An unanswerable verdict is
  refused with `PresentationError`. [B] checks first; this is the second lock.
- **`metrics`** — each answered metric element's phrase, from `QueryIn`. The
  plan is not needed: the one thing the Presenter needs from it, the
  `ResultSpec` (shape, axes, ranking direction), rides on the `ResultSet`. A
  row for an element with no phrase is refused — it has no label to wear.
- **An unknown unit is refused.** `format.UNIT_KINDS` covers exactly the load
  step's `ALLOWED_UNITS`; guessing a format is how a ratio gets shown as money.

`PresentationError` is always our fault, never the asker's: [B] reports it as
a failed job, with the detail in the trace.

## 2. The table

Every row of the `ResultSet` becomes a row of the table, which always ships —
charts point into it and are never a second copy of the figures.

Order: metrics in the order the asker named them. A ranked metric in rank
order, **sorted here from the values**, not trusted from the statement
(retrieval DESIGN §9); a row with no value cannot be ranked and goes last.
Anything else by company, then filed before derived, annual before quarterly,
then date.

## 3. Display strings

| unit | kind | shown as |
|---|---|---|
| `USD`, `EUR` | money | `$391.04B`, `$1.25T`, `$950,000` |
| `pure`, `Rate` | ratio | `46.2%`; a *change* in a ratio is points, `+0.7 pp` |
| `USD/shares` | per_share | `$1.02` — never scaled like money |
| `shares` | count | `15.12B shares` |

A change, growth or CAGR carries its sign (`+0.9%`, `-$32.13B`); a filed
figure does not. A derived row with no value (growth from a zero base) is `—`.
Display strings round (1 decimal for percentages, 2 for scaled money); the
exact value travels beside every one of them.

## 4. Views, by shape

| `ResultSpec.shape` | views |
|---|---|
| `scalar` | one stat card, when there is exactly one row |
| `ranking` | one bar chart per ranked metric, in the direction the parser recorded (`highest` → descending), capped at `BAR_LIMIT` (10) bars with "top 10 of 359" in the title |
| `series` | line panels, one per (derivation, unit kind, granularity), one line per (metric, company), each in date order |
| `table` | none: the table is the answer |

A line panel never holds a figure beside its own growth, money beside a
ratio, or a year beside a quarter — `AnswerView` refuses all three, so a
mistake here fails loudly rather than drawing a misleading chart. Two metrics
in the same unit share a panel (revenue and net income, q041).

## 5. Found while building it

- **The reworded HANDOFF §8 smoke test comes back 128 rows, not 36.** "Revenue
  from 2023 to 2025 by quarter" parses into three years plus four bare quarters,
  which the mapper reads across every year on file. A parser problem, kept as
  the `smoke_mixed` fixture because the output is honest and exercises mixed
  granularity and a tag change; flagged for a separate fix.
- **q038's "largest decline" is a Q4 → Q1 seasonal drop** (Amazon, -$32.13B).
  Correct for the words asked; worth knowing before reading it as news.

## 6. Not done

- **No bar chart for a `table` comparison.** "Compare Q4 revenue across Apple,
  Microsoft and NVIDIA" (q017) is a table only. Unranked bars would be an easy
  addition if wanted.
- **No headline sentence.** If one is ever wanted it is templated here from a
  row, never written by a model.
