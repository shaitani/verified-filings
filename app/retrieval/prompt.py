"""``build_prompt(plan)`` -- the prompt. Nothing else, and nobody else.

This module's only output is text for Qwen. It writes **no SQL**: it assembles
the input the model needs -- what the one readable relation contains, what the
plan resolved to, and what the answer has to look like -- and the model writes
the statement.

That division is the point. ``build_prompt`` prompts, ``generate`` asks,
``validate`` judges, ``execute`` runs. Each of those is the only thing that
does its job, and a function that quietly does a second one makes the whole
chain impossible to reason about.

The plan reaches the model as a **table of coordinates**, one row per value
the answer needs. Every hazard the data holds is spelled out beside it:
per-company concept ids (filers tag the same business concept differently),
date windows rather than fiscal-year labels (a 10-K carries prior-year
comparatives), ``unit`` as part of the key, instants having no start date, and
the two windows a Q4 has to be computed from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.retrieval.validator import MAX_ROWS
from app.schemas.query import Binding, QueryPlan, ResolvedPeriod
from app.schemas.result import RESULT_COLUMNS

#: The one relation the retrieval role can read.
VIEW = "xbrl.reported_fact"


class UnsupportedPlan(ValueError):
    """A plan this module will not render SQL for.

    Separate from a *refusal to answer*: the plan may be perfectly good and
    the question answerable. It means this layer cannot express it yet, which
    is a gap to close rather than a caveat to pass on to a reader.
    """


@dataclass(frozen=True)
class PlanCell:
    """One ``(binding, period)`` pair, flattened to the coordinates the SQL
    joins on. The unit of the row-count promise: ``len(plan_cells(plan))`` is
    how many rows a plain retrieval should return."""

    element_id: str
    company_cik: int
    fiscal_year: int
    fiscal_period: str
    period_start: date
    period_end: date
    concept_id: int
    unit: str
    is_instant: bool
    binding_index: int


def _periods_for(binding: Binding, plan: QueryPlan) -> list[ResolvedPeriod]:
    """The resolved periods this binding answers for, for its company.

    ``Binding.periods`` is a list of labels and may be empty, which means
    "every period in scope" -- so the windows come from ``PlanFilters``, which
    is the only place the concrete dates live.
    """
    wanted = {(ref.fiscal_year, ref.fiscal_period) for ref in binding.periods}
    return [
        period
        for period in plan.filters.periods
        if (binding.company_cik is None or period.company_cik == binding.company_cik)
        and (not wanted or (period.fiscal_year, period.fiscal_period) in wanted)
    ]


def plan_cells(plan: QueryPlan) -> list[PlanCell]:
    """Flatten the plan into the cells retrieval must return, one row each.

    Raises ``UnsupportedPlan`` for a multi-operand binding. The *unit* half of
    that is now solved -- ``Binding.operand_unit`` carries what the facts are
    filed in, and ``fact_unit`` is what the join keys on -- but rendering the
    arithmetic is not built: one cell becomes one row per operand, and the
    expression has to be evaluated over them. See DESIGN §9.
    """
    cells: list[PlanCell] = []
    for index, binding in enumerate(plan.bindings):
        if len(binding.concepts) > 1:
            raise UnsupportedPlan(
                f"binding {index} ({binding.element_id!r}) computes "
                f"{binding.expression!r} over {len(binding.concepts)} concepts. Its "
                f"operands are filed in {binding.fact_unit!r} and the result is "
                f"{binding.unit!r}, so the coordinates are all here -- what is missing "
                f"is rendering the arithmetic over one row per operand"
            )
        concept_id = binding.concepts[0].concept_id
        for period in _periods_for(binding, plan):
            cells.append(
                PlanCell(
                    element_id=binding.element_id,
                    company_cik=period.company_cik,
                    fiscal_year=period.fiscal_year,
                    fiscal_period=period.fiscal_period,
                    period_start=period.period_start,
                    period_end=period.period_end,
                    concept_id=concept_id,
                    # The *facts'* unit, not the result's. For a single-operand
                    # binding they are the same; for a ratio the result is
                    # `pure` and no `pure` fact exists behind it.
                    unit=binding.fact_unit,
                    is_instant=binding.is_instant,
                    binding_index=index,
                )
            )
    if not cells:
        raise UnsupportedPlan("the plan binds nothing, so there is no query to write")
    return cells


#: Column names for the filter table. One constant so the worked example below
#: and the real table can never drift apart -- a model shown two different
#: headers has been given a reason to invent a third.
#: Every column here is named either exactly as the contract wants it
#: projected, or after the view column it joins to. Nothing needs renaming on
#: the way through, which is the point: a model copies what it is shown.
#:
#: Two measured failures produced this. Short names `fy`/`fp` came back
#: projected as `fy` and `fp`, and the contract refused them. Before that, a
#: single combined "Q12023" label -- which had to be split -- came back as
#: `fiscal_period = 'Q12023'`. Handing over values already in the shape the
#: answer needs removes the step instead of explaining it.
_TABLE_HEADER = (
    "  element_id | company_cik | fiscal_year | fiscal_period | concept_id | "
    "unit  | is_instant | window_start | window_end"
)


def _table_row(
    element_id: str,
    company_cik: int,
    fiscal_year: int,
    fiscal_period: str,
    concept_id: int,
    unit: str,
    is_instant: bool,
    window_start: str,
    window_end: str,
) -> str:
    """One line of the filter table, for the real one and the worked example
    alike. Shared so the two cannot drift: a model shown two different shapes
    has been given a reason to invent a third.

    ``is_instant`` renders as the literal the column actually holds. Rendering
    it "yes"/"no" once produced ``is_instant = 'no'`` -- boolean compared with
    text, failing at execution.
    """
    return (
        "  {:<10} | {:<11} | {:<11} | {:<13} | {:<10} | {:<5} | {:<10} | "
        "{:<12} | {}".format(
            element_id,
            company_cik,
            fiscal_year,
            fiscal_period,
            concept_id,
            unit,
            "true" if is_instant else "false",
            window_start,
            window_end,
        )
    )


def _coordinate_table(cells: list[PlanCell]) -> str:
    """The plan as data: one row per value the answer needs.

    Deliberately not a ``VALUES`` list. That would be SQL, and writing SQL is
    the model's job, not this one's -- handing it a half-written statement is
    how the line between the two blurs.
    """
    rule = "  " + "-" * (len(_TABLE_HEADER) - 2)
    rows = [
        _table_row(
            cell.element_id,
            cell.company_cik,
            cell.fiscal_year,
            cell.fiscal_period,
            cell.concept_id,
            cell.unit,
            cell.is_instant,
            cell.period_start.isoformat(),
            cell.period_end.isoformat(),
        )
        for cell in cells
    ]
    return chr(10).join([_TABLE_HEADER, rule, *rows])


#: ``QueryIn.intent`` is the producer saying what kind of answer is wanted, and
#: it is the only signal available here that separates "give me the figures"
#: from "compute something from them".
#:
#: It is used to remove an option rather than to add one. Measured with
#: qwen2.5-coder:7b: given a complete, correct query in the prompt *and*
#: permission to return it unchanged, it returns it unchanged even for a
#: ranking question -- it copied the base query and appended an ORDER BY. A
#: 7B model handed a finished answer does not go looking for a better one.
#: So when the intent says the answer must be computed, the "reply unchanged"
#: branch is not offered at all.
DERIVING_INTENTS = frozenset({"rank", "derive"})

_JOB_EITHER = """Decide which of these two the question needs. Read the question again before
choosing -- returning the figures when the question asked for a comparison
between them answers a different question.

(a) The question asks for the figures themselves ("what was X's revenue").
    Reply with the query above, unchanged.

(b) The question asks for something COMPUTED from them -- a growth rate, a
    ranking, a share of a total, a difference, a filter on a computed value.
    Then wrap it, keeping the query above as a CTE, and set `derivation` to a
    short name for what you computed. Leave `derivation` NULL only on rows
    whose `value` is a figure exactly as filed -- a computed value with a NULL
    derivation is reported to the reader as the metric itself, which is
    wrong."""

_JOB_MUST_DERIVE = """This question asks for a value that must be COMPUTED from those rows. The
query above is NOT the answer -- returning it unchanged, or with only an
ORDER BY added, answers a different question.

Keep it as a CTE and compute the answer from it. In every row you compute,
set `derivation` to a short name for what it is, and set `unit` to what the
computed number actually is ('pure' for a ratio or a growth rate). Leave
`derivation` NULL only on rows whose `value` is a figure exactly as filed."""


#: One worked example, on **invented data**.
#:
#: Not a retreat from "the model writes the SQL": the cik, concept ids and
#: dates here belong to no company in the store, so this teaches the *form*
#: and answers no part of the plan it is attached to.
#:
#: It exists because of a measured failure. Without it, qwen2.5-coder:7b
#: transcribed the filter table into a ``UNION ALL`` of literal rows -- column
#: for column, in the table's own order -- and invented a value to go with
#: them. One filter row still produced a correct query; two did not, because
#: with two rows "transcribe the table" becomes the more obvious completion
#: than "join against it". The example makes the join the obvious one instead.
#:
#: The closing sentence does most of the work: it names the three columns the
#: model was inventing and says where they come from.
_WORKED_EXAMPLE = f"""WORKED EXAMPLE (different company and dates -- copy the FORM, not the values)

If the filter table were:

{_TABLE_HEADER}
{_table_row("e9", 11111, 2019, "FY", 77, "USD", False, "2018-01-01", "2018-12-31")}
{_table_row("e9", 22222, 2019, "FY", 88, "USD", False, "2018-07-01", "2019-06-30")}

the statement would be:

  WITH wanted(element_id, company_cik, fiscal_year, fiscal_period,
              concept_id, unit, is_instant, window_start, window_end) AS (
    VALUES ('e9', 11111, 2019, 'FY', 77, 'USD', false, DATE '2018-01-01', DATE '2018-12-31'),
           ('e9', 22222, 2019, 'FY', 88, 'USD', false, DATE '2018-07-01', DATE '2019-06-30')
  )
  SELECT w.element_id, v.company_cik, v.ticker, v.entity_name,
         w.fiscal_year, w.fiscal_period,
         v.period_start, v.period_end, v.is_instant,
         v.value, v.unit, NULL::text AS derivation
  FROM wanted w
  JOIN xbrl.reported_fact v
    ON  v.company_cik = w.company_cik
    AND v.concept_id  = w.concept_id
    AND v.unit        = w.unit
    AND v.is_instant  = w.is_instant
    AND v.period_end  = w.window_end
    AND (w.is_instant OR v.period_start = w.window_start)
  LIMIT 500

Note that value, ticker and entity_name appear ONLY as v.<column>. They are
never written as literals -- they are what you are querying FOR.
"""


#: Shown only when the filter table has a `minus_window_ending`.
#:
#: Prose alone was not enough. Told in words to subtract, qwen2.5-coder:7b
#: returned Apple's FY2024 annual revenue -- 391,035,000,000 -- as its Q4,
#: against a real Q4 of 94,930,000,000. Thirty-six of thirty-six rows came
#: back, every one attributed, verdict `complete`: nothing downstream can see
#: that a quarter is really a year. It is the most dangerous single thing in
#: this prompt, so it gets its own worked example rather than a sentence.
_RESIDUAL_HELP = """
{count} row(s) in the filter table have a date in `minus_window_ending`. Those
values are NOT filed directly. Reading `window_start`..`window_end` for them
returns a FULL YEAR, and reporting that as a quarter is the worst mistake you
can make here.

Each one is a subtraction of two rows of the relation that share a start date:

  (value for window_start..window_end) MINUS (value for window_start..minus_window_ending)

which means joining the relation to itself:

  JOIN xbrl.reported_fact v
    ON v.company_cik = w.company_cik AND v.concept_id = w.concept_id
   AND v.unit = w.unit AND v.period_start = w.window_start
   AND v.period_end = w.window_end
  JOIN xbrl.reported_fact sub
    ON sub.company_cik = w.company_cik AND sub.concept_id = w.concept_id
   AND sub.unit = w.unit AND sub.period_start = w.window_start
   AND sub.period_end = w.minus_window_ending
  ...
  v.value - sub.value AS value

Use an inner join for the second one: if the row to subtract is missing, the
answer must be left out entirely, never returned unsubtracted.
"""


def _plan_notes(plan: QueryPlan) -> list[str]:
    notes = [f"- {note.kind}: {note.message}" for note in plan.notes]
    for index, binding in enumerate(plan.bindings):
        for note in binding.notes:
            notes.append(f"- binding {index} ({binding.element_id}) {note.kind}: {note.message}")
    return notes


def build_prompt(plan: QueryPlan) -> str:
    """Render ``plan`` as the text Qwen is asked to write SQL from."""
    cells = plan_cells(plan)
    spec = plan.result
    notes = _plan_notes(plan)
    columns = ", ".join(RESULT_COLUMNS)
    job = _JOB_MUST_DERIVE if plan.intent in DERIVING_INTENTS else _JOB_EITHER

    return f"""You write one PostgreSQL SELECT statement. Reply with the SQL and nothing else.

QUESTION
{plan.question}

WHAT THE ANSWER MUST CONTAIN
shape={spec.shape}, varies along {spec.axes or ["nothing"]}, {spec.row_count} row(s) of
underlying data ({spec.companies} compan(ies) x {spec.periods} period(s) x
{spec.metrics} metric(s)). Do not collapse an axis the answer varies along.

THE ONLY RELATION YOU CAN READ
{VIEW}(company_cik, ticker, entity_name, concept_id, taxonomy, concept_name,
                   concept_label, unit, is_instant, period_start, period_end, value)

  - One row per reported value. Superseded restatements are already filtered
    out; you do not need to think about that.
  - `period_end` is the date the value is as of (is_instant = true) or ends on
    (is_instant = false). `period_start` is NULL for every instant.
  - It has NO fiscal_year or fiscal_period column, on purpose. Match on the
    dates given below, never on a year.
  - `unit` MUST be part of every join or filter that selects a value. The same
    company, concept and period can be filed under two units, and ignoring it
    returns each value twice.

FILTER TABLE -- WHICH ROWS TO FETCH
This is a filter, not data to output. Each line says how to FIND one row in
the relation. The values themselves are in the database and are the whole
point of the query; they are not here, and they are not yours to supply.
`concept_id` differs per company on purpose: filers tag the same business
concept differently.

{_coordinate_table(cells)}

Every value in that table is a literal you write into the SQL. `element_id`,
`fiscal_year` and `fiscal_period` exist ONLY there -- the relation has no such
columns, so they have to be selected as constants per row.

A Q4 row is no different from any other: no filer reports a fourth quarter,
but the relation supplies one anyway, already computed. Fetch it like the rest.

Match each row with an EXACT equality on all of:
  company_cik, concept_id, unit, is_instant, period_end = window_end
and, only when is_instant is false, period_start = window_start.
Do not use BETWEEN or a date range: two different periods can end in the same
year, and a range returns both.

The straightforward way is a VALUES list of the table above joined to the
relation, which also keeps `element_id` and the period labels attached to the
right rows.
{_WORKED_EXAMPLE}
YOUR JOB
{job}

OUTPUT
Project exactly these column names, in any order:
  {columns}

  - `element_id`, `fiscal_year` and `fiscal_period` come from the filter
    table and are projected under exactly those names -- the relation has no
    such columns, so they are constants per row. `fiscal_period` is one of
    'FY', 'Q1', 'Q2', 'Q3', 'Q4' and nothing else.
  - `period_start`, `period_end`, `is_instant`, `value`, `unit`, `ticker`,
    `entity_name`, `company_cik` come from the relation.

RULES (the statement is rejected if it breaks one)
0. EVERY value must be read from the relation. Never write a number, a ticker
   or a company name into the SQL as a literal -- a statement that does not
   read `{VIEW}` is rejected outright. The table above tells you WHICH rows to
   fetch; it does not contain the values, and the values are not yours to
   supply.
1. One statement. SELECT only -- no SET, no set_config(), no data-modifying
   CTE, no SELECT INTO, no locking clause.
2. `{VIEW}` is the only readable relation. `fact`, `filing`, `company` and
   `concept` will raise a permission error.
3. End with `LIMIT {MAX_ROWS}` or less. A statement with no LIMIT is rejected;
   nothing adds one for you.
4. Give every projected column an explicit alias unless it is a bare column
   reference. `NULL::text` without an alias is a column named "text".
5. Never mix rows of different `unit` in one arithmetic expression.

CAVEATS ALREADY ATTACHED TO THIS PLAN (do not drop rows because of them)
{chr(10).join(notes) if notes else "- none"}

SQL:"""
