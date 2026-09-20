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

### 4.2 The model is handed the retrieval SQL, not asked for it

`build_prompt` does not describe the schema and hope. It **writes the
retrieval query itself** — `base_query(plan)` is complete, runnable, and
satisfies the contract on its own — and asks the model either to return it
unchanged or to wrap it.

That is the §2 division of labour made literal. Everything easy to get quietly
wrong (the `is_latest` filter, instant-versus-duration matching, the residual
subtraction, keeping `unit` in the join key) is never asked of the model. What
is asked is arithmetic over rows that are already right.

`base_query` also collapses the `UNION ALL` §4 expected for mixing direct and
residual bindings. A `LEFT JOIN` to the subtrahend, keyed on a `subtract_end`
column that is NULL for direct cells, handles both in one block:

```sql
       v.value - COALESCE(s.value, 0) AS value
...
WHERE p.subtract_end IS NULL OR s.value IS NOT NULL
```

That `WHERE` is the load-bearing half. Without it a *missing* subtrahend makes
`COALESCE` return the whole year as if it were Q4 — a textbook plausible wrong
number. Dropping the row instead turns it into a missing cell, which the
verdict reports.

### 4.3 Two findings from building it

**`validate()` parses; it does not type-check.** A `VALUES` column that is
NULL in every row — which `subtract_end` is for any plan without a Q4 — is
typed `text` by PostgreSQL, and `date = text` then fails at *execution* time.
The statement is syntactically perfect and libpg_query has no complaint. The
fix is `NULL::date`; the lesson is that validation is not a substitute for
running the thing.

**A 7B model handed a finished answer returns the finished answer.** Given
`base_query` in the prompt *and* permission to reply with it unchanged,
qwen2.5-coder:7b returned it unchanged for "rank these companies by
year-over-year revenue growth" — it copied the query and appended an
`ORDER BY`. Adding a worked example did not move it. What worked was removing
the option: when `QueryPlan.intent` is `rank` or `derive`, the prompt does not
offer the "reply unchanged" branch at all. It then produced the correct
`lag()`-over-partition growth query, marked `derivation='yoy_growth'` and
`unit='pure'`.

Worth keeping as a general shape: with a small model, **remove the wrong path
rather than argue against it**.

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
- **Multi-operand bindings are not renderable**, and the reason is a gap in
  `Binding` rather than a shortcut here. `Binding.unit` is the unit of the
  *result* — `pure` for `c0 / c1` — and the operands' own unit is carried
  nowhere. The fact join needs the operands' unit (§2.4), so recovering it
  means a database lookup `build_prompt` deliberately does not do.
  `plan_cells` raises `UnsupportedPlan` saying exactly that. Six of the 47
  curated metrics are affected (`gross_margin` and the other ratios). The fix
  is an operand unit on `Binding`, set by the mapper, which already knows it.
- **Nothing checks that the answer matches the question.** The ranking case in
  §4.3 came back `complete` and `is_answerable` while answering a different
  question, because the verdict checks cardinality and attribution, not
  meaning. That is HANDOFF §4.6 reappearing one layer down, and it belongs in
  the same place — with the producer.
- **No retry.** Whether a rejected statement goes back to the model with the
  `InvalidSQL` message is still undecided; see `generator.py`.
