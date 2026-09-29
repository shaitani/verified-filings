# `app/retrieval` — [E] Executor: the result contract, the view, the SQL

The layer between a `QueryPlan` and the rows an answer is made of. Four
functions, each doing one job, and only it doing that job:

```
build_prompt(plan) -> str        text for Qwen, when Qwen is needed      no DB
generate(plan)     -> str        the statement: Python writes it         no DB
validate(sql)      -> str        judges; returns its input unchanged     no DB
execute(sql, plan) -> ResultSet  reads xbrl.reported_fact as vf_retrieval_role
```

`answer(plan)` runs generate → **validate** → execute. Calling `execute()` on
a generator's output directly is the mistake this package exists to prevent.
Qwen never holds a credential: whatever it writes runs as a role that can
reach exactly one relation.

---

## 1. What the contract is for

Three jobs, each closing a way of producing a confident wrong number:

1. **It makes `ResultSpec.row_count` checkable.** A three-company,
   twelve-quarter chart is 36 rows and retrieval must not come in under.
   Nothing could check that until something said what a row was.
2. **It makes citation provable rather than inferred.** With a mid-range tag
   change one company has two bindings for one element, so inferring from the
   plan alone picks wrong.
3. **It stops a derived value being read as the metric it came from.** A row
   of `0.081 / pure` against element `revenue` is revenue *growth*; with no
   marker, a presenter renders "revenue: 0.08".

## 2. The row

Two types, split by **who writes them**. That split is the whole design.

```python
class ResultRow(_Base):
    """The SQL contract: exactly the columns a statement must project."""
    element_id: str
    company_cik: int
    ticker: str | None
    entity_name: str | None
    fiscal_year: int
    fiscal_period: QueryFiscalPeriod
    period_start: date | None       # NULL exactly when is_instant
    period_end: date
    is_instant: bool
    value: Decimal | None           # NULL only on a derived row (a growth with no base)
    unit: str
    derivation: str | None          # NULL = as filed


class AnnotatedRow(_Base):
    """A ResultRow after Python has attributed it."""
    row: ResultRow
    binding_keys: list[str]         # "b0", "b3" -- indices into plan.bindings
    base: PeriodRef | None          # an over-time row's base period (§4.1)
```

Nothing in `AnnotatedRow` is taken from the model.

### 2.1 Citation resolves in Python, not in the SQL

`concept_id` is deliberately **not** a contract column. The tuple
`(element_id, company_cik, fiscal_year, fiscal_period)` already resolves to
exactly one `Binding` — a company-specific binding beats the
`company_cik=None` fallback, and `Binding.periods` separates a tag change. So
attribution is a lookup against the plan, which is trustworthy, not a column
carried through by the model, which is not.

`QueryPlan.binding_for(...)` returns the binding **and its index**, raising
`LookupError` on no match and `ValueError` on more than one — a cheap
integrity check nothing else performs: a cell with two answers has none. The
key (`b0`, `b1`, …) is positional, so plan and result are auditable against
each other. `binding_keys` is a list because a derived row can span bindings:
a growth figure across a tag change is computed from two concepts, and naming
one would be a lie.

### 2.2 Rationale and notes are keyed, not repeated

A binding's rationale and its concept's label run to ~1 KB. They live once in
`ResultSet.citations`, keyed by binding key; each row carries only the key.

### 2.3 `derivation` is a label

Non-NULL whenever the value is not the bound metric as filed: `change`,
`growth`, `cagr` (written in Python), or a label the model gives an aggregate
or derivation (`average`, …). `validate` refuses a computed value in the
column — the label names what was computed, the number belongs in `value`
(q010 once put the growth rate itself there). The Presenter refuses a label it
has no display rule for.

### 2.4 `unit` is part of the key

Among `is_latest` facts, `(company, concept, unit, period_start, period_end,
is_instant)` is unique — **0 duplicate groups across ~174,000 facts**, so a
row count means something. Drop `unit` and there is exactly one collision:
AMD's FY2024 effective tax rate, filed as both `pure` and `Rate`, both 0.19 —
one cell becomes two rows, and anything summing them returns 0.38.

### 2.5 A missing cell is absent, not NULL

Statements use inner joins, so a cell with no fact is simply absent and
Python computes the missing set. A left join would put a NULL where the
Presenter expects a number, and a NULL that leaks reads as zero.
`period_start` is NULL exactly when `is_instant` is true — a duration row that
lost its start is how a 90-day figure passes for a 365-day one, and the
contract cannot express it.

## 3. The view

`xbrl.reported_fact` is the one relation retrieval can read. It bakes in the
`is_latest` filter and the joins to `company` and `concept` — the parts of
every query that are identical and silently wrong when omitted — and exposes
`company_cik`, `ticker`, `entity_name`, `concept_id`, `taxonomy`,
`concept_name`, `concept_label`, `unit`, `is_instant`, `period_start`,
`period_end`, `value` and `is_synthesized`.

It **synthesizes every fourth quarter** (GAPS D1.4): annual minus nine-month,
paired by the gap the subtraction leaves, flows only, the filed row winning
where one exists. A Q4 is an ordinary row to fetch.

### 3.1 It exposes no fiscal year. That is the point.

`Filing.fiscal_year` is provenance — which filing a number appeared in — not
the period it describes (GAPS D1.1). A column of that name on a relation the
model can see invites `WHERE fiscal_year = 2024`, which is that bug. So the
view offers **date windows only**; the contract's `fiscal_year` and
`fiscal_period` come from the plan. The trap is closed by absence. For the same
reason the view omits `filing`, `filed_date`, `frame`, `is_latest`,
`load_run`, and a concept's embedding and description.

### 3.2 It runs with owner rights, and must keep doing so

`security_invoker` is off, so a role with `SELECT` on the view needs nothing
on the tables beneath it. That is what makes §7 work. Turning it on would
silently require base-table grants back.

## 4. How a statement is written: Python writes it

Every value an answer reads is written in Python from the plan's typed
objects, in two CTEs:

- **`wanted`** (`emit_cte`) — one row per plan cell and operand: element,
  company, fiscal labels, the exact window (as the plan says it, never computed
  from a label), concept, unit, instant or not, derivation. `plan_cells(plan)`
  is the cell list, and its length is the row count the verdict checks.
- **`figures`** (`emit_figures`) — one row per cell, its value computed:
  - fetched: `wanted` joined to the view on company, concept, unit, instant and
    window;
  - combined by the metric's expression: each `cN` becomes
    `max(v.value) FILTER (WHERE w.operand = N)`, the operators as written,
    `NULLIF` around a divisor — so a missing operand gives NULL, never a
    partial sum;
  - filtered by a threshold (`WHERE`, or `HAVING` on a computed value);
  - computed over time (§4.1).

`figures_select(plan)` reads `figures` in the fixed projection, with a
ranking's `ORDER BY` per ranked metric in the direction the plan records
(`NULLS LAST`, so a growth with no base sinks) and the `LIMIT`.

**What still reaches Qwen**: only a plan whose answer needs an aggregate or
another derivation above `figures` (`needs_the_model`). Its prompt shows
`figures` alone — computed values, no relation, no join, no operand, no
coordinate — and Qwen writes the layer above. A lookup, comparison, series,
threshold or ranking never reaches the model. A plan `figures` cannot hold
(one element with two expressions) is refused, never handed over.

The prompt no longer grows with the plan (a hundred cells and one produce the
same prompt). `MAX_PROMPT_CHARS`, derived from the generator's 8,192-token
context, is a backstop for what can still grow — a very long question, many
caveats — because Ollama truncates an overlong prompt **silently** and the
model answers from the part it saw.

### 4.1 Change, growth and CAGR

Fixed arithmetic the plan can state, so written in Python like a margin:
growth `(this − before) / before`, change `this − before`, CAGR
`(last / first)^(1 / years) − 1`.

The parser marks the metric `over_time: change | growth | cagr` and keeps its
text the metric ("revenue"); the mapper records it in `QueryPlan.over_time`
and binds the metric over every period needed. **Which periods pair up** is
one rule, `over_time_pairs` in `app/schemas/query.py`, used by the mapper to
fetch and by retrieval to read:

- a series steps from each period to the next in the series — "the last five
  years" is four growths; the first period is a base, not an answer;
- a single period is measured against the one before it, which the mapper
  fetches as a support period ("growth in 2024" reads 2023 too);
- a CAGR is the last fiscal year against the first, one figure per company.

Cells are built per cell, not per binding, because the two ends can be
different concepts: NVIDIA's FY2023 growth reads each end from the binding
covering it, and the row cites both. A growth or CAGR from a zero or negative
base is NULL rather than a number. `prompt.over_time_bases` walks the same
pairs to tell the Presenter each row's base ("vs FY2023") — one loop, so the
label and the arithmetic cannot differ.

**A plain series gets its growth beside it** (`replaces = False`): "show me
revenue over five years" returns the figures and the growth between them; a
metric asked for *as* its movement gets the growth instead. Only a series along
`period`, single-concept, not a ratio, not a ranking or derivation, never
beside a threshold; steps follow each company's own series and granularity, so
annual and quarterly are never compared.

Measured: Tesla FY2024 revenue growth 0.95%; NVIDIA's four growths across its
tag change (61.4%, 0.2%, 125.9%, 114.2%); Apple's five-year revenue CAGR
3.28%; the change in Apple's gross margin +0.70 points.

## 5. Lessons from model-written SQL

Qwen once wrote every statement. Each failure below was fixed for good only
by moving that piece into Python — which is why §4 exists — and the lessons
still govern the prompt for what Qwen writes today.

| failure | cause |
|---|---|
| `is_instant = 'no'` | the prompt rendered a boolean as yes/no; the model copies what it is shown |
| invented every value, no `FROM` | the coordinate table read as *output*, not a filter — with two rows, transcribing it into a `UNION ALL` of literals was the likelier completion |
| `fiscal_period = 'Q12023'` | a combined label needed splitting |
| `WHERE entity_name IN ('Google', …)` | company names lifted from the question; the stored names differ, so nothing matched |
| **Q4 returned as the whole year** | told to subtract nine months from the year, it did not — 36 of 36 rows, verdict `complete` |
| `LIMIT 1` on a ranking | rule 3 said "LIMIT 500 **or less**" — permission, which it took |
| invented a `WHERE` filter | told a CTE existed that it could not see, it narrowed it with a value lifted from elsewhere in the prompt |
| `max(v.value)` where a subtraction belonged | its one worked example showed a division and then argued against itself |
| free cash flow as 12.5B, not 108.8B | shown only a division, it divided |

What they teach:

- **Show the thing it must produce** — the literal the column holds, the name
  the contract wants, the operator the expression uses. Nearly every failure
  was the prompt's, not the model's: the model did something reasonable the
  prompt did not rule out.
- **A question is context for what to compute, not a source of literals.**
- **An example that contradicts the rules beside it wins.** No example may
  show what a rule forbids; a test holds every example to beginning at
  `SELECT`.
- **This model's behaviour is not reachable by reasoning about the prompt.**
  Two wrong edits were made to fix `LIMIT 1` before a six-variant harness,
  run in minutes, showed two words were the whole cause. Reach for the
  harness first.
- **A prompt claim needs a rate over several cold runs**, not one observation
  ([docs/TESTING.md](../../docs/TESTING.md#the-prompt-cache)).
- **Every prompt rule names a candidate for Python.** Two remain: the fixed
  projection and the `LIMIT` ([docs/FUTURE.md](../../docs/FUTURE.md#quality-of-answers)).

## 6. The verdict, and when to refuse

```python
class ResultVerdict(_Base):
    status: Literal["complete", "partial", "over", "empty"]
    expected_rows: int
    returned_rows: int
    missing: list[MissingCell]     # each marked anticipated or not
    unattributable: list[int]      # row indices matching no binding

class ResultSet(_Base):
    question: str
    result: ResultSpec             # copied from the plan, for the Presenter
    rows: list[AnnotatedRow]
    verdict: ResultVerdict
    citations: dict[str, Citation]
    notes: list[Note]
```

**Answer only when the shortfall was already disclosed.**

| status | answer? |
|---|---|
| `complete` | yes |
| `partial`, every missing cell anticipated by the plan | yes, with the note |
| `partial`, any cell not anticipated | **refuse** |
| `over` | **refuse** |
| `empty` | **refuse** |
| any unattributable row | **refuse** |

Coverage was *proved* before a binding was made, so a cell missing afterwards
is a fault in the statement, not the filings — and every other row came out of
the same statement, so partial trust is not on offer. A chart missing one of
36 points looks small; if the mapper did not predict it, the real finding is
"the statement is wrong in a way we do not understand". The converse matters
as much: a gap the plan *did* disclose is not news, and refusing there would
make `partial_coverage` pointless. `over` — more rows than promised — means a
join fanned out: a confident wrong aggregate rather than a visible gap. An
unanticipated shortfall raises an `incomplete_result` note.

**Exact wherever the plan names every row** (`_verdict_exact`): nothing left to
the model, so rows are matched to plan cells on `(element, company, period,
derivation)` — a missing growth row is a shortfall, not hidden behind the
filed row with the same period. For what the model writes, the check is
weaker: the set of `(element, company)` pairs must equal the plan's and no row
may be unattributable (a five-year growth series has four points, not five).

Two more checks mark rows unattributable: **a row in the wrong unit**
(`_wrong_unit` — a binding whose result is `pure` cannot have `USD` rows,
because dividing like by like is dimensionless; its gap is GAPS G3), and **a
row that fails the plan's threshold** (`_threshold_violations` — the plan holds
the number, so the predicate is re-applied to what came back). A threshold is
the one narrowing *meant* to return fewer rows than the grid: eleven of twenty
companies clearing $100B is `complete`.

## 7. The grant narrowing

The other half of the fence. The view alone is a convention; the grant makes
"generated SQL cannot name `fact`" true. `vf_retrieval_role` holds `SELECT` on
`reported_fact` and nothing else (`app/db/roles.py`), so it cannot reach
`fact`, `filing`, `company`, `concept`, `load_run` or anything in `web`. Keep
the view and this together: either alone is a fence with no posts.

## 8. `validate()`

The role stops writes and DDL, but two of the controls around it are not
boundaries at all: `statement_timeout` and `default_transaction_read_only` are
`USERSET`, and PostgreSQL has no per-role row cap. Those can only live here.

**It parses with PostgreSQL's own grammar** (`pglast`, libpg_query), so there
is no gap between how the validator reads a string and how the server will.
Anything not positively understood is refused:

1. It parses, and is exactly one statement.
2. **Every** statement node in the tree is a `SELECT`. Not "starts with
   SELECT": `WITH d AS (DELETE FROM xbrl.fact RETURNING *) SELECT * FROM d`
   starts with `WITH` and deletes rows.
3. No `SELECT … INTO`, no locking clause.
4. No denied function anywhere. **`set_config` is the one that matters** — it
   is `SET` in an expression, reaching `statement_timeout` from inside an
   ordinary select list. The bare name is matched, so a schema-qualified call is
   refused too.
5. Every relation named is the view or a CTE of the same statement, and it
   reads the view at all — a statement of invented literals with no `FROM`
   passed every other check once.
6. The projection is exactly `RESULT_COLUMNS` (derived from `ResultRow`, so the
   two cannot drift), each column named explicitly; `derivation` is a label or
   NULL at every level.
7. A `LIMIT`, a plain integer, at or under the row cap. `FETCH … WITH TIES` is
   refused: its own count is not a cap.

**It returns its input byte-identical.** An earlier version appended a
missing `LIMIT`; a validator that edits its input means what runs is not what
was read. A missing `LIMIT` is a refusal.

Two kinds of rejection: `ContractViolation`, an ordinary mistake (wrong
columns, no `LIMIT`); and `OutOfRole`, the statement reaching outside what it
is for (a `SET`, `set_config`, another relation, a data-modifying CTE, reading
nothing) — **logged at WARNING**, so it surfaces whether or not anyone reads a
return value. Messages are written to be shown to a model; whether a rejected
statement goes back for another try is undecided
([docs/FUTURE.md](../../docs/FUTURE.md#smaller)).

## 9. The SQL is logged, not returned

`ResultSet` carries no `sql`. The statement is appended as JSONL to
`data/retrieval_log.jsonl` with the question and verdict (and the job id, in
the Web Server). It is wanted for debugging, and it is the one thing a
presenter might quote at a reader — keeping it out of the envelope means that
cannot happen by accident.

Open edges: [docs/GAPS.md](../../docs/GAPS.md) G2, G3, G7.
