# `app/retrieval` — the result contract, and the view it reads

The layer between a `QueryPlan` and a rendered answer. Four functions, of which
only the last touches the database:

```
build_prompt(plan) -> str     the plan, rendered as text for Qwen      no DB
generate(plan)     -> str     calls Qwen, returns SQL                  no DB
validate(sql)      -> str     raises, or returns the statement to run  no DB
execute(sql, plan) -> ResultSet                reads as vf_retrieval_role
```

Qwen never holds a credential. `execute()` runs what it wrote, as a role that
can reach exactly one relation.

This document covers the **result contract** and the **view**, which were
designed together: the view's columns and the contract's columns are the same
list seen from two ends, and neither is coherent alone.

**All four are built**, along with the view (`xbrl.reported_fact`, migration
`3fcc714d6050`), the grant narrowing (§6), the contract
(`app/schemas/result.py`) and the binding lookup (`QueryPlan.binding_for`).
`answer(plan)` runs the three steps in the one order that matters:
generate → **validate** → execute.

Verified end to end against the real database with `qwen2.5-coder:7b`. The
HANDOFF smoke test — three filers, twelve quarters, Q4 residuals and a
mid-range tag change — comes back `complete`, 36 of 36 rows, with Apple's
FY2024 Q4 revenue at 94,930,000,000, the reported figure.

---

## 1. What the contract is for

Three jobs. Each of them closes a way of producing a confident wrong number.

1. **It makes `ResultSpec.row_count` checkable.** Schemas DESIGN §8.14 says a
   three-company, twelve-quarter chart is 36 rows and retrieval must not come
   in under. Nothing could check that, because nothing said what a row was.
2. **It makes citation provable rather than inferred.** The presenter has to
   name the concept behind each figure. With concept drift one company has two
   bindings for one element, so inferring from the plan alone picks wrong.
3. **It stops a derived value being read as the metric it came from.** A row of
   `0.081 / pure` against element `revenue` is revenue growth; a presenter with
   no marker on it renders "revenue: 0.08". This is the §4.6 failure one layer
   further down — the elements are right and the answer is still to a different
   question.

---

## 2. The row

Two types, split by **who writes them**. That split is the whole design.

```python
class ResultRow(_Base):
    """The SQL contract: exactly the columns a generated statement must
    project, in any order, with these names."""
    element_id: str
    company_cik: int
    ticker: str | None
    entity_name: str | None
    fiscal_year: int
    fiscal_period: QueryFiscalPeriod
    period_start: date | None       # NULL for an instant
    period_end: date
    is_instant: bool
    value: Decimal
    unit: str
    derivation: str | None          # NULL = as reported


class AnnotatedRow(_Base):
    """A ResultRow after Python has attributed it. The only thing added is
    provenance, and Python adds it because the model cannot be trusted to."""
    row: ResultRow
    binding_keys: list[str]         # "b0", "b3" -- indices into plan.bindings
```

`ResultRow` is what Qwen (or the deterministic half) produces. `AnnotatedRow`
is what the presenter reads. Nothing in the second is taken from the model.

### 2.1 Citation resolves in Python, not in the SQL

`concept_id` is deliberately **not** a contract column. The tuple
`(element_id, company_cik, fiscal_year, fiscal_period)` already resolves to
exactly one `Binding` — company-specific beats the `company_cik=None` fallback,
and `Binding.periods` separates the drift case. So attribution is a lookup
against the plan, which is trustworthy, instead of a column carried through by
the model, which is not.

`QueryPlan.binding_for(element_id, cik, fiscal_year, fiscal_period)` returns
the binding **and its index**, raising `LookupError` on no match and
`ValueError` on more than one. The second is a cheap integrity check on the
mapper that nothing else performs: a plan where two bindings claim one cell is
malformed, and a cell with two answers has none.

`QueryPlan.binding_key(index)` formats the key — positional, because the
position is what makes plan and result auditable against each other.
`QueryPlan.bindings_for_company(element_id, cik)` is the coarse lookup derived
rows use.

`binding_keys` is a **list** because a derived row can span bindings. Alphabet
changes revenue tags mid-range, so a 2024→2025 growth figure is computed across
two concepts; naming one of them would be a lie. For an as-reported row the
list has exactly one entry.

For derived rows attribution is necessarily coarser: it resolves on
`(element_id, company_cik)` and returns every binding of that element for that
company whose periods overlap the row. That says "computed from these", which
is true, rather than pretending to a precision the row does not have.

### 2.2 Rationale and notes are keyed, not repeated

`Binding.rationale` is up to 512 characters and `ConceptRef.label` up to 512
more. Repeating those on each of 36 rows is ~18KB of duplicated prose into the
presenter's context. They live once in `ResultSet.citations`, keyed by binding
key; the row carries only the key.

### 2.3 `derivation` is a free string, not an enum

Same reasoning as `Binding.expression` (schemas DESIGN §8.4): the only reader is
a language model reading it as text, and an enum would need widening every time
Qwen computes something new. What matters is that it is **non-NULL whenever the
value is not the bound metric as filed**; its content is for the reader.

### 2.4 `unit` is part of the key, not decoration

Measured, not assumed. Among `is_latest` facts, `(company_cik, concept_id,
unit, period_start, period_end, is_instant)` is unique — **0 duplicate groups
across ~174,000 facts**, so the grain is clean and a row count means something.

Drop `unit` from that key and there is exactly one collision in the corpus:
AMD's `EffectiveIncomeTaxRateContinuingOperations` for FY2024 is filed twice,
once as `pure` and once as `Rate`, both `0.19`. One cell becomes two rows, and
anything that sums them returns `0.38`. Verified live:

| join | rows | `sum(value)` |
|---|---|---|
| with `unit` | 1 | 0.19 |
| without `unit` | 2 | 0.38 |

Separately, 6 facts are `EUR` (T-Mobile and Oracle debt instruments), so
cross-currency aggregation is a live hazard in this corpus rather than a
theoretical one.

### 2.5 `value` is not nullable

An emitted statement uses an INNER join, so a cell with no fact is simply
absent. A LEFT join would make shortfalls visible as `value IS NULL` rows and
save `execute()` a set difference — but it puts a NULL where the presenter
expects a number, and a NULL that leaks reads as zero. Python computes the
missing set instead; it is reliable and it keeps `value: Decimal` total.

`period_start`, by contrast, is nullable and **exactly** as nullable as
`is_instant` is true — `ResultRow` rejects either half without the other. A
duration row that lost its start is how a 90-day figure passes for a 365-day
one, and that is not a shape the contract should be able to express.

---

## 3. The view

```sql
CREATE VIEW xbrl.reported_fact AS
SELECT f.company_cik,
       co.ticker,
       co.entity_name,
       f.concept_id,
       c.taxonomy,
       c.name        AS concept_name,
       c.label       AS concept_label,
       f.unit,
       f.is_instant,
       f.period_start,
       f.period_end,
       f.value
FROM xbrl.fact f
JOIN xbrl.company co ON co.cik = f.company_cik
JOIN xbrl.concept  c ON c.id   = f.concept_id
WHERE f.is_latest;
```

It bakes in the `is_latest` filter and the three joins — the parts of every
query that are identical and that are silently wrong when omitted.

### 3.1 It exposes no fiscal year or fiscal period. That is the point.

`Filing.fiscal_year` is *provenance* — which filing a number appeared in — not
the period the number describes, because a 10-K carries prior-year comparative
columns (PITFALLS §1.1; this returned FY2022 revenue for an FY2024 question
before it was fixed). Any column named `fiscal_year` on a relation Qwen can see
invites `WHERE fiscal_year = 2024`, which is that bug.

So the view offers **date windows only**, and the labels come from the plan.
The contract's `fiscal_year` / `fiscal_period` are projected from the injected
plan rows (§4), never read from the database. The trap is closed by absence.

For the same reason the view omits `filing`, `accession_number`, `filed_date`,
`frame`, `fact.id`, `is_latest` (always true here), `load_run`, and
`concept.embedding` / `concept.description`.

### 3.2 The view runs with owner rights, and must keep doing so

`security_invoker` is off (the default), so a role with `SELECT` on the view
needs no privilege on `fact`, `filing`, `company` or `concept`. That is exactly
what makes §6 work. Setting `security_invoker = true` would silently require
base-table grants back, so it is not a knob to turn casually.

---

## 4. How a statement is shaped: the plan as a `VALUES` list

The plan's resolved coordinates go into the statement as data, not as SQL the
model has to compose:

```sql
WITH plan(element_id, company_cik, fiscal_year, fiscal_period,
          period_start, period_end, concept_id, unit, is_instant) AS (
  VALUES
    ('e1', 320193,  2024, 'FY', DATE '2023-10-01', DATE '2024-09-28', 252, 'USD', false),
    ('e1', 1045810, 2025, 'FY', DATE '2024-01-29', DATE '2025-01-26', 254, 'USD', false)
)
SELECT p.element_id, v.company_cik, v.ticker, v.entity_name,
       p.fiscal_year, p.fiscal_period,
       v.period_start, v.period_end, v.is_instant,
       v.value, v.unit, NULL::text AS derivation
FROM plan p
JOIN xbrl.reported_fact v
  ON  v.company_cik = p.company_cik
  AND v.concept_id  = p.concept_id
  AND v.unit        = p.unit
  AND v.is_instant  = p.is_instant
  AND v.period_end  = p.period_end
  AND (p.is_instant OR v.period_start = p.period_start)
LIMIT 500;
```

Two things fall out of this that were open questions:

**Per-company concept divergence stops being SQL cleverness.** Apple binds
concept 252, NVIDIA binds 254, and the difference is two rows of a `VALUES`
list. Verified live: five plan cells in, four rows out, and the deliberately
bogus cik produced no row rather than an error.

**HANDOFF §4.4's composition question resolves to one statement per question.**
Because the row carries `element_id`, several bindings coexist in one result set
and a table of revenue and net income is just rows with different `element_id`.
The caveat is that mixing `direct` and `residual` bindings needs a `UNION ALL`
of two such blocks — fine for the deterministic half, which writes it itself;
more surface when Qwen has to.

The instant case is why the period join is `(p.is_instant OR v.period_start =
p.period_start)`: an instant fact has `period_start IS NULL` and its
`period_end` is the instant date, which equals the resolved period's end.

### 4.1 Residuals

`PeriodResidual` gives `shared_start`, `whole_end`, `subtract_end`, and the
subtraction is a self-join on the shared start:

```sql
SELECT w.value - s.value AS value
FROM xbrl.reported_fact w
JOIN xbrl.reported_fact s
  ON  s.company_cik  = w.company_cik
  AND s.concept_id   = w.concept_id
  AND s.unit         = w.unit
  AND s.period_start = w.period_start
WHERE w.period_start = :shared_start
  AND w.period_end   = :whole_end
  AND s.period_end   = :subtract_end
```

Verified: Apple FY2024 Q4 revenue comes back **94,930,000,000**, the reported
figure. `Binding._residual_is_provable` has already refused any residual whose
component windows lack facts, so this join cannot quietly return the whole year
because a term went missing.

### 4.2 Division of responsibility

Each function does one job, and only it does that job:

| function | does | does not |
|---|---|---|
| `build_prompt` | assembles the text | write SQL |
| `generate` | talks to Qwen | judge or run what comes back |
| `validate` | judges the statement | run it, or **modify** it |
| `execute` | runs it against the database | anything else |

**Qwen writes the SQL. All of it.** `build_prompt` hands over what the model
needs — the view's columns and their traps, a table of coordinates (one row
per value the answer needs), the required projection, the rules — and the
model composes the statement. The coordinate table is deliberately *not* a
`VALUES` list: that would be SQL, and a half-written statement blurs the line
this table exists to keep.

`validate` returns its input **byte-identical**. An earlier version appended a
missing `LIMIT`; it no longer does, because a validator that edits its input
means what runs is not what was read, and a wrong answer then traces back to a
statement nobody wrote. A missing `LIMIT` is a refusal instead.

It reports two kinds of rejection. `ContractViolation` is an ordinary mistake
— wrong columns, no `LIMIT`. `OutOfRole` is the model reaching outside what it
is for — a `SET`, a `set_config`, another relation, a data-modifying CTE — and
that one is **logged at WARNING** naming what was attempted, so it surfaces
whether or not anyone inspects a return value.

### 4.3 What the model actually does, measured

`qwen2.5-coder:7b`, writing the statement from the filter table. Six distinct
failures. **Five were the prompt's fault, not the model's** — worth stating,
because "the model is too small" is where diagnosis stops rather than starts.

| # | failure | cause | fix |
|---|---|---|---|
| 1 | `is_instant = 'no'` | the table rendered a boolean as `yes`/`no` | render `true`/`false` |
| 2 | invented every value, no `FROM` | the table read as *output*, not *filter* | reframe + worked example |
| 3 | `fiscal_period = 'Q12023'` | a combined `Q12023` label needed splitting | give `fy` and `fp` as separate columns |
| 4 | paraphrased dates | downstream of #2 | went away with #2 |
| 5 | **skipped the Q4 subtraction** | — | not fixed; see below |
| 6 | `LIMIT 1` on a ranking | rule 3 said `LIMIT 500` **"or less"** | drop "or less"; see §4.3c |

The instructive one is **#2**. The filter table was introduced with "one row
here = one row the answer needs", which says the rows *are* the output. With
one filter row the model still wrote a correct query; with two, transcribing
the table into a `UNION ALL` of literals became the more obvious completion
than joining against it — column for column, in the table's own order, with an
invented `1000000000 AS value`.

Three things fixed it, and it was the third that mattered: calling the table a
filter, saying the values are in the database and not here, and a worked
example on **invented data** (cik `11111`, dates in 2019) whose closing line
names the three columns the model kept inventing. The example teaches form and
answers no part of the plan it is attached to.

Isolation tests ruled out the obvious suspects first: the model copies six
dates verbatim at `repeat_penalty` 1.1 and 1.0 alike, and no prompt came near
the context limit (1,176–2,086 tokens against 8,192).

### 4.3a The question is context, not a filter

Found with the same ladder. At 36 cells the smoke test returned `empty` while
a 1-cell version of the same query was correct, which looked like a scale
problem. It was not: every rung from 1 to 36 cells passed, coordinates
reproduced exactly, `complete` each time.

What differed was the **question text**. Same plan, same 36 filter rows, same
correct join — and the failing statement added one clause:

```sql
WHERE v.entity_name IN ('Google', 'Apple', 'Nvidia')
```

The stored names are `Alphabet Inc.`, `Apple Inc.` and `NVIDIA CORP`, so that
matches nothing. The model had lifted company names out of the question and
added a belt-and-braces filter with them; the terse phrasing names no
companies, so it added none.

The prompt now says the filter table is the complete row selection, that no
other filter belongs, and that `ticker` and `entity_name` are display-only
because the question says "Apple" where the database says "Apple Inc.".

Worth generalising: **a question is context for what to compute, not a source
of literals.** `company_cik` in the filter table already identifies a filer
exactly, and any second way of naming one is a second way to get it wrong.

### 4.3b Metrics that combine several facts

`gross_margin` is `c0 / c1` over two USD concepts. Six of the 47 curated
metrics are arithmetic like this, in three shapes: `c0 / c1`, `c0 - c1` and
`(c0 - c1) / c2`.

Unlike the Q4 subtraction this **cannot** move into the view: which concepts
to divide is decided per question, not by the data. So the model does it, and
the work is in leaving nothing to invent.

- **A cell stays one row of the answer.** `PlanCell` holds `concept_ids` and
  the `expression`, so `len(plan_cells(plan))` is still the number the verdict
  checks. The filter table renders a line per operand; the promise does not.
- **`unit` and `result_unit` are separate on the cell.** The join keys on what
  the facts are filed in (`USD`); the row is projected with what the answer is
  (`pure`). There are no `pure` facts behind a gross margin.
- **The operand column and its instructions appear only when needed.** 41 of
  47 metrics are a single concept, and the single-operand prompt is the one
  that took five measured failures to get right.
- **Two worked examples, not one adaptive one**, because the difference is
  structural: the CTE gains a column and the select gains a pivot and a
  `GROUP BY`. A test asserts each example declares as many CTE columns as the
  table it is shown beside — the plain example against an operand table
  produced ten values per row against nine names, so `is_instant` received
  `'USD'` and it failed as `boolean = text`.
- **The expression is substituted, not pattern-matched.** Replace each `cN`
  with `max(v.value) FILTER (WHERE w.operand = N)` and keep the operators as
  written. Saying that, and showing the subtraction beside the division, is
  what fixed the free-cash-flow case in §9.

Verified end to end against figures computed independently from the database:

| metric | expression | result |
|---|---|---|
| gross margin | `c0 / c1` | 0.462063 |
| operating margin | `c0 / c1` | 0.315102 |
| net margin | `c0 / c1` | 0.239713 |
| free cash flow | `c0 - c1` | 108,807,000,000 |
| free cash flow margin | `(c0 - c1) / c2` | 0.278254 |
| current ratio | `c0 / c1` | 0.867313 |

### 4.3c "or less" — two words that cost fifteen rows

Measured 2026-09-23. "Which of these companies has the highest operating
margin?" produced a plan for 16 companies and a statement ending:

```sql
ORDER BY value DESC
LIMIT 1;
```

Everything above that line was right — the 32-row `VALUES` table, the join on
all five key columns, the ratio, the `pure` unit. One row came back against
16 expected, the verdict refused it, and nothing reached a reader.

**The cause was rule 3**, which read:

> `3. End with LIMIT 500 or less.`

"or less" is permission. Asked for the *highest* of something and told a
smaller limit was acceptable, the model took it.

Six single-variable runs against the same plan, changing one thing each:

| variation | `LIMIT` | `ORDER BY` |
|---|---|---|
| baseline | **1** | yes |
| **rule 3 drops "or less"** | **500** | yes |
| `YOUR JOB` preamble removed | 1 | yes |
| **whole `YOUR JOB` section removed** | 1 | yes |
| question reworded without "highest" | 500 | **no** |
| rule 3 fixed *and* preamble removed | 500 | yes |

And, on top of the rule-3 fix, applying both reverted edits together:

| variation | `LIMIT` | `ORDER BY` |
|---|---|---|
| rule 3 fixed only (**shipped**) | **500** | yes |
| + `rank` out of `DERIVING_INTENTS` | 500 | yes |
| + *also* "a ranking" out of option (b) | **1** | yes |

Three things those tables settle.

**The job text was irrelevant.** Removing the entire `YOUR JOB` section left
`LIMIT 1` intact. Three separate hypotheses about `rank` belonging in
`DERIVING_INTENTS`, or about option (b) naming rankings as computations, were
all wrong, and two of them were implemented and reverted before this was run.

**Removing "or less" is the whole fix.** Nothing else in the prompt needed to
change, and nothing else did.

**A conditional warning, for future work only.** `intent="rank"` is in
`DERIVING_INTENTS`, so a ranking's prompt renders `_JOB_MUST_DERIVE` and
`_JOB_EITHER` — the "(a)/(b)" text, and the words "a ranking" inside it — is
never shown for this question at all. The second table's last row is
therefore hypothetical: it applies only if `rank` is ever moved out of
`DERIVING_INTENTS`, at which point `_JOB_EITHER` starts being used and
stripping "a ranking" from option (b) brings `LIMIT 1` back. As things stand
that phrase has no effect on a ranking question.

The recurring lesson of this section held again, and the cost of ignoring it
was two wrong edits: **this model's behaviour is not reachable by reasoning
about the prompt.** A six-variant harness took minutes and answered it
outright. Reach for that first.

**Nothing pins this wording.** All 58 retrieval and validator tests pass with
"or less" present or absent, so a tidy-up could restore it and every ranking
question would silently return one row again. A test asserting on the rules
text would close that, and has not been written.

### 4.3d A relationship between two metrics has nowhere to live

**This is the most valuable thing in this document that is not built.**

"How much of Alphabet's revenue goes to R&D?" is a ratio of two filed figures.
The chain has no way to say so. Measured 2026-09-23:

```
parser  -> metrics ['revenue', 'R&D']          two independent elements
mapper  -> b0 expr='c0'  Revenues
           b1 expr='c0'  ResearchAndDevelopmentExpense
```

Two bindings, no arithmetic. The fact that one is meant to be divided by the
other exists **nowhere in the plan** -- it survives only as English in
`QueryPlan.question`, which the SQL model reads as prose.

#### The two mechanisms that do exist, and why neither covers this

**Curated alias.** `metric_aliases.yaml` carries an `expression` per metric,
so `gross_margin` is one element resolving to one binding with `c0 / c1` and a
checked `operand_unit`. Deterministic, and it is why q007, q021, q022 and q024
work. But it only fires when the question names the ratio as ONE phrase: "R&D
intensity" parses to a single element and binds; "how much of revenue goes to
R&D" parses to two and never reaches the file, whatever synonyms are in it.

This is the ceiling: **an alias per phrasing**. `rd_intensity` was added for
the named form, and the phrased form still fails. A file cannot enumerate the
ways English relates two quantities.

**The SQL model computes it.** `YOUR JOB` invites the model to wrap the query
and derive. This is how growth and rankings work, and it is the path the
generic case would take. It is not reliable: §4.3c is one measured failure in
it, and q023 is another -- the model reads "one number wanted", then fetches
one of the two inputs instead of computing. Five prompt variants were tried
and none survived repetition (see the note on sampling below).

**A third, pattern-based route does not exist.** `QueryIn` is a flat list --
`version`, `question`, `intent`, `elements`, `shape`. There is no field
relating one element to another, no operator, no `relations`. So even a parser
that recognised "X as a share of Y" perfectly would have nowhere to put the
result. **Closing this is a schema change before it is a prompt change.**

#### What it would take

Roughly, in order: a way for `QueryIn` to carry a relation between element ids
(`{"op": "ratio", "of": "e3", "to": "e2"}`); a mapper that turns that into one
`Binding` with a multi-operand `expression`, reusing the machinery the alias
path already has; and a parser prompt that emits the relation. The middle step
is nearly free -- `Binding`, `operand_unit`, the unit check and the "SOME ROWS
COMBINE SEVERAL FACTS" prompt section were all built for exactly this shape.
The ends are the work.

The prize is that "share of", "per", "divided by", "as a percentage of" and
"difference between" stop needing an alias each, over any two metrics the
corpus holds.

#### A methodological note, because it cost a day

Ollama at `temperature=0` is **not** bit-deterministic across calls. The same
byte-identical prompt produced a 1-row and a 2-row statement on different runs
of q023, which made a single-sample variant sweep read as a clean result when
it was noise. A variant that "fixes" something on one run has not been
measured. **Prompt claims in this file need a rate over n runs, not an
observation.** §4.3c's `LIMIT` finding stands because it reproduced across the
full eval set afterwards; the q023 variants did not and were reverted.

### 4.4 The Q4 subtraction -- fixed, by moving it

The fourth quarter is the annual figure minus the year-to-date one. Told in
prose and then shown a worked self-join, the model **returned Apple's FY2024
annual revenue, 391,035,000,000, as its Q4** -- against a real Q4 of
94,930,000,000. Thirty-six of thirty-six rows, every one attributed, verdict
`complete`. Nothing downstream could tell a quarter was really a year.

It is not asked of the model any more. `xbrl.reported_fact` synthesizes the
fourth quarter (migration `a8b5b820cf1a`), so a Q4 is an ordinary row to fetch
and Apple's Q4 FY2024 now comes back as 94,930,000,000 through the same
pipeline that returned the year.

That removed a great deal besides: `PeriodResidual`,
`ResolvedPeriod.residual_of`, `Binding.period_rule`, `Coverage.components`,
this prompt's residual section, and a `min_view_reads` check in `validate()`
that existed only to catch the subtraction being skipped. The two derivations
were cross-checked before either was deleted -- 275 of 275 residuals agreed
across 20 companies, 4 metrics and 5 years.

### 4.5 Superseded: what the subtraction used to cost

A residual value is the whole window minus the shorter one. Told in prose and
then shown a worked self-join, the model **returned Apple's FY2024 annual
revenue, 391,035,000,000, as its Q4** — against a real Q4 of 94,930,000,000.

Thirty-six of thirty-six rows came back. Every one attributed. Verdict
`complete`, `is_answerable` true. **Nothing downstream could tell that a
quarter was really a year**, because the verdict checks cardinality and
attribution, not arithmetic — and a year-sized number in a revenue series
looks like revenue.

It is caught now, by the cheapest thing that works: **two windows subtracted
cannot come from one read of the relation.** `validate(sql, min_view_reads=2)`
refuses a single-read statement for a plan that has a residual cell. The
arithmetic is invisible to a parser; the shape it requires is not.

`answer()` derives `min_view_reads` from the plan and passes it in, so the
validator still knows nothing about plans — it is told a property the
statement must have, and checks it.

The consequence is honest rather than good: **Q4 questions are now refused**
rather than answered with a year. Closing that properly means either a model
that will write the self-join, or moving the residual back into deterministic
code — which is a real decision, not a bug fix, because it puts SQL-writing
back on this side of the line.

---

## 5. The verdict, and when to refuse

```python
class MissingCell(_Base):
    element_id: str
    company_cik: int
    fiscal_year: int
    fiscal_period: QueryFiscalPeriod
    anticipated: bool          # the plan already disclosed this gap

class ResultVerdict(_Base):
    status: Literal["complete", "partial", "over", "empty"]
    expected_rows: int
    returned_rows: int
    missing: list[MissingCell]
    unattributable: list[int]  # row indices matching no binding

class ResultSet(_Base):
    version: Literal["1"] = "1"
    question: str
    rows: list[AnnotatedRow]
    verdict: ResultVerdict
    citations: dict[str, Citation]
    notes: list[Note]
```

**The rule: answer only when the shortfall was already disclosed.**

| status | answer? |
|---|---|
| `complete` | yes |
| `partial`, every missing cell `anticipated` | yes, with the note |
| `partial`, any cell not anticipated | **refuse** |
| `over` | **refuse** |
| `empty` | **refuse** |
| any `unattributable` row | **refuse** |

The reasoning is the project's own premise. Coverage was *proved* before a
binding was made, so a cell missing after that proof is a fault in the query or
the run, not in the filings — and every other row in that result set came out
of the same faulty statement, so partial trust is not on offer. A chart missing
one of 36 points looks like a small thing, but if the mapper did not predict
it, the real finding is "the statement is wrong in a way we do not understand",
and 35 plausible dots is precisely the shape this project exists to refuse.

The converse matters as much: a gap the plan *did* disclose is not news, and
refusing there would make `partial_coverage` pointless — it exists so the
answer can go out with a caveat attached.

`over` is as important as `partial` and easier to miss. More rows than the plan
promised means a join fanned out; that is the AMD unit case, and it produces a
confident wrong aggregate rather than a visible gap.

An unanticipated shortfall raises `incomplete_result` (schemas DESIGN §8.15) —
the one `NoteKind` made after execution rather than by the mapper.

### 5.1 The row-count equality only applies to as-reported results

`ResultSpec.row_count` is companies × periods × metrics, which describes the
grid of *filed* values. A five-year growth series has four points, not five, so
equality would fail a correct answer.

So: when every returned row has `derivation IS NULL`, `returned_rows` must
equal `expected_rows`. When any row is derived, the check becomes weaker but
still catches the thing §8.14 was written for — the set of
`(element_id, company_cik)` pairs present must equal the plan's, and no row may
be unattributable. That is what stops three companies being collapsed into one.

---

## 6. The grant narrowing

Half of the fence. The view alone is a convention; the grant is what makes
"Qwen's SQL cannot name `fact`" true.

`app/db/roles.py` already revokes before it grants, and PostgreSQL's
`REVOKE ALL ON ALL TABLES IN SCHEMA` covers views as well as tables — so this
is a change to `RETRIEVAL`'s spec, not to the provisioning code:

```python
RETRIEVAL = RoleSpec(
    name="vf_retrieval_role",
    tables=("reported_fact",),   # was ("company", "filing", "fact")
    columns={},                  # the concept column grant moves into the view
    ...
)
```

Verified on a throwaway role: after revoke-then-grant, `has_table_privilege`
is true for `reported_fact` and false for `company`, `concept`, `fact`,
`filing` and `load_run`.

Do the view and this together. Either alone is a fence with no posts.

---

## 7. `validate()` — built

The role stops writes and DDL, but two of the guards around it are not
boundaries at all. `statement_timeout` and `default_transaction_read_only` are
`USERSET`, and PostgreSQL has no per-role row cap. Those two controls can only
live here, which is what makes this module load-bearing rather than
defence-in-depth decoration.

**It parses with PostgreSQL's own grammar.** `pglast` wraps libpg_query, so
there is no gap between how the validator reads a string and how the server
that executes it will. A validator built on a reimplemented parser has such a
gap, and the gap is the whole attack.

What it enforces, conservatively — anything not positively understood is
refused:

1. The text parses, and is exactly one statement.
2. **Every** statement node in the tree is a `SelectStmt`. Not "starts with
   SELECT": `WITH d AS (DELETE FROM xbrl.fact RETURNING *) SELECT * FROM d`
   starts with `WITH` and deletes rows, and a check on the top of the tree
   passes it.
3. No `SELECT ... INTO`, no locking clause.
4. No denied function anywhere. **`set_config` is the one that matters** — it
   is `SET` in an expression, so it reaches `statement_timeout` from inside an
   otherwise ordinary select list and needs no `SET` statement. The bare name
   is what is matched, so `pg_catalog.set_config(...)` is refused too.
5. Every relation named is the view or a CTE of the same statement. Redundant
   with the grant on purpose: the error names the problem instead of arriving
   as `permission denied` from a live connection.
6. The projection is exactly `RESULT_COLUMNS` (`app/schemas/result.py`,
   derived from `ResultRow` so the two cannot drift), in any order. Every
   column needs an explicit alias or to be a bare column reference — an
   unnamed `NULL::text` is a column called `text`, and guessing that name is
   how a projection goes silently wrong.
7. A `LIMIT` at or under `max_rows`, appended on its own line when absent and
   **re-parsed to confirm it took**. `FETCH ... WITH TIES` is refused: it
   returns however many rows tie at the cut-off, so its own count is not a cap.

The returned string is not always the one passed in — a missing `LIMIT` is
added, because the cap cannot live anywhere else. Nothing else is rewritten: a
statement that would need changing to be safe is refused instead, so what runs
is what was read.

Every message is written to be shown to a model. Whether a rejected statement
is handed back for another attempt is **undecided** (see `generator.py`): the
messages invite it, but a retry loop is also how a validator's error text
becomes a map of what to get around.

---

## 8. The SQL is logged, not returned

`ResultSet` carries no `sql` field. The statement that ran is appended as JSONL
to `data/retrieval_log.jsonl` (`/data/` is already gitignored) alongside the
question and the verdict. It is wanted for debugging, and it is the one thing
in reach that a presenter might quote at a user — keeping it out of the
envelope means that cannot happen by accident. Revisit when there is a reason
to surface it.

---

### 4.6 Moving work out of the model is the lever that keeps working

Two changes on 2026-09-24 did more for correctness than any prompt wording has,
and they are the same move twice: **take a thing the model was being asked to
produce, produce it in Python, and hand it over already correct.**

| | before | after |
|---|---|---|
| q038 coordinate rows in the statement | 40 of 378 | **378 of 378** |
| q038 invented windows | 16 | **0 — unrepresentable** |
| prompt size | 53,114 chars, **growing with the plan** | ~6,200 chars, **constant** |

`emit_cte` writes the coordinate CTE from the plan's typed objects. The model
now writes only the `SELECT`, and the failure that mattered most is gone not
because the model was persuaded but because it is never asked: a window it does
not write cannot be a window it computes from the fiscal-year label.

The second lever is the same in spirit — a worked example of the *form* the
answer takes, so the shape is shown rather than described. §4.3's whole
catalogue says this, and the derivation example in §4.3 is the fourth instance.

**Where to look next, when optimising.** Every one of these is currently prose
in the prompt and could be structure instead:

- **The join itself.** Five equality conditions, identical in every statement.
  A second emitted CTE — `figures AS (SELECT ... FROM wanted JOIN view ON ...)`
  — would leave the model only the analytical layer. It also removes the
  `unit`-in-the-join rule, `period_start`-only-when-duration, and "do not use
  BETWEEN", which are three of the measured failures in §4.3.
- **The fixed twelve-column projection.** `RESULT_COLUMNS` never varies, and
  the contract refuses any deviation. Emitting the projection would retire
  rule 4 and the `NULL::text` alias trap with it.
- **`LIMIT`.** Rule 3 exists only because nothing adds one; §4.3c is two words
  in that rule costing fifteen rows.
- **The operand expression.** `max(v.value) FILTER (WHERE operand = N)` per
  `cN` is mechanical substitution into `Binding.expression`. Doing it here
  would close §9's "same-unit arithmetic has no structural check", which is
  currently defended by a prompt line alone.

The pattern to keep in mind: a prompt rule exists because the model can get
something wrong, so every rule names a candidate for deterministic emission.
The model is good at the analytical layer — ranking, growth, a filter over a
computed value — and reliably bad at transcription. The less it transcribes,
the less there is to check.

None of this is required for correctness today; it is where to spend effort
when effort is available, and it shrinks the prompt every time, which is its
own reward on a 1080 Ti.

## 9. Open

- **`ResultShape` is not enforced.** The verdict checks cardinality; nothing
  checks that a `ranking` came back ordered.
- **Restatement disclosure** (HANDOFF §4.9) needs the superseded rows, which
  `is_latest` hides and the view therefore cannot see. A second view, or a
  `filed_date` column, when that is built.
- **A second view for residuals** was considered and left out: one `UNION ALL`
  branch is not enough surface to justify it.
- **Cross-unit aggregation** is prevented by `unit` being in the join key, but
  nothing *detects* a result set that mixes units and would be meaningless
  summed. Probably a verdict check.
- **Same-unit arithmetic has no structural check.** A ratio that comes back
  `USD` is caught, because the plan says the answer is `pure` and
  `execute()` marks a row in the wrong unit unattributable. `free_cash_flow`
  is `c0 - c1` over two USD concepts, so a wrong operator gives a wrong
  number in the *right* unit and nothing downstream can tell. Measured: shown
  only a division in the worked example, the model divided, returning Apple's
  FY2024 free cash flow as 12.5 rather than 108.8 billion — attributable,
  verdict `complete`. The example now shows both operators and says to read
  the expression rather than copy it, and that prompt is the whole defence.
- **Nothing checks that the answer matches the question.** The ranking case in
  §4.3 came back `complete` and `is_answerable` while answering a different
  question, because the verdict checks cardinality and attribution, not
  meaning. That is HANDOFF §4.6 reappearing one layer down, and it belongs in
  the same place — with the parser.
- **No retry.** Whether a rejected statement goes back to the model with the
  `InvalidSQL` message is still undecided; see `generator.py`.
