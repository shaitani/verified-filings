"""``build_prompt(plan)`` -- the plan, rendered as text for Qwen. No database.

The division of labour, from ``app/retrieval/DESIGN.md``: retrieval is bounded
-- four shapes over one view -- and should be deterministic code, while the
model writes the layer above it (ranking, growth, ratios across companies).

So this module **writes the retrieval SQL itself** and hands it to the model
already correct. ``base_query(plan)`` is complete, runnable, and satisfies the
contract on its own. The prompt asks for it back unchanged when the question
needs nothing more, and for it to be wrapped when the question needs a
computed answer.

That is a deliberate choice about what a 7B model is being asked to do. The
parts that are easy to get quietly wrong -- the ``is_latest`` filter, the
instant-versus-duration period match, the residual subtraction, keeping
``unit`` in the join key -- are not asked of it at all. What is asked of it is
arithmetic over rows that are already correct, and ``validate()`` refuses
anything that deviates from the contract.

The plan reaches the SQL as a literal ``VALUES`` list. That is what makes
per-company concept divergence *data*: Apple binds one concept id and NVIDIA
another, and the difference is two rows of a table, not a branch the model has
to invent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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
    #: For a direct cell, the fact's own window. For a residual, the *whole*
    #: window -- the minuend -- whose start both terms share.
    period_start: date
    period_end: date
    #: Set only for a residual: the end of the window to subtract. ``None``
    #: means read the value straight off ``period_end``.
    subtract_end: date | None
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
            residual = binding.period_rule == "residual"
            if residual and period.residual_of is None:
                raise UnsupportedPlan(
                    f"binding {index} is a residual over "
                    f"{period.fiscal_period}{period.fiscal_year}, which carries no "
                    f"residual windows"
                )
            cells.append(
                PlanCell(
                    element_id=binding.element_id,
                    company_cik=period.company_cik,
                    fiscal_year=period.fiscal_year,
                    fiscal_period=period.fiscal_period,
                    period_start=(
                        period.residual_of.shared_start if residual else period.period_start
                    ),
                    period_end=(
                        period.residual_of.whole_end if residual else period.period_end
                    ),
                    subtract_end=period.residual_of.subtract_end if residual else None,
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


def _sql_date(value: date | None) -> str:
    """A date literal, or a **typed** NULL.

    The cast is not decoration. PostgreSQL infers a ``VALUES`` column's type
    from its literals, and a column that is NULL in every row -- which
    ``subtract_end`` is for any plan with no Q4 in it -- comes out as ``text``.
    Comparing it to a date then fails with "operator does not exist: date =
    text" at *execution* time. ``validate()`` cannot catch this: libpg_query
    parses, it does not type-check.
    """
    return "NULL::date" if value is None else f"DATE '{value.isoformat()}'"


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _values_rows(cells: list[PlanCell]) -> str:
    rows = [
        "    ("
        + ", ".join(
            [
                _sql_text(cell.element_id),
                str(cell.company_cik),
                str(cell.fiscal_year),
                _sql_text(cell.fiscal_period),
                _sql_date(cell.period_start),
                _sql_date(cell.period_end),
                _sql_date(cell.subtract_end),
                str(cell.concept_id),
                _sql_text(cell.unit),
                "true" if cell.is_instant else "false",
            ]
        )
        + ")"
        for cell in cells
    ]
    return ",\n".join(rows)


def base_query(plan: QueryPlan) -> str:
    """The retrieval SQL for this plan: complete, correct, contract-shaped.

    Every hazard the view does not already close is closed here:

    * the period match is ``period_end`` plus ``period_start`` *only when the
      value is a duration* -- an instant fact has no start;
    * ``unit`` is in the join key, because without it one cell can be two rows;
    * a residual subtracts the shorter window from the whole one, sharing a
      start, and the ``WHERE`` drops the cell entirely if the subtrahend is
      missing. That last clause is the important one: a missing subtrahend
      would otherwise make ``LIMIT``-shaped nonsense of a Q4 by returning the
      whole year, which is the exact plausible-wrong-number this project
      exists to refuse. Dropping the row makes it a missing cell instead, and
      the verdict says so.
    """
    cells = plan_cells(plan)
    return f"""WITH plan(element_id, company_cik, fiscal_year, fiscal_period,
          period_start, period_end, subtract_end, concept_id, unit, is_instant) AS (
  VALUES
{_values_rows(cells)}
)
SELECT p.element_id,
       v.company_cik,
       v.ticker,
       v.entity_name,
       p.fiscal_year,
       p.fiscal_period,
       v.period_start,
       v.period_end,
       v.is_instant,
       v.value - COALESCE(s.value, 0) AS value,
       v.unit,
       NULL::text AS derivation
FROM plan p
JOIN {VIEW} v
  ON  v.company_cik = p.company_cik
  AND v.concept_id  = p.concept_id
  AND v.unit        = p.unit
  AND v.is_instant  = p.is_instant
  AND v.period_end  = p.period_end
  AND (p.is_instant OR v.period_start = p.period_start)
LEFT JOIN {VIEW} s
  ON  p.subtract_end IS NOT NULL
  AND s.company_cik  = p.company_cik
  AND s.concept_id   = p.concept_id
  AND s.unit         = p.unit
  AND s.is_instant   = false
  AND s.period_start = p.period_start
  AND s.period_end   = p.subtract_end
WHERE p.subtract_end IS NULL OR s.value IS NOT NULL"""


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
shape={spec.shape}, varies along {spec.axes or ['nothing']}, {spec.row_count} row(s) of
underlying data ({spec.companies} compan(ies) x {spec.periods} period(s) x
{spec.metrics} metric(s)). Do not collapse an axis the answer varies along.

THE RETRIEVAL QUERY IS ALREADY WRITTEN
It returns exactly {len(cells)} row(s), one per company/period/metric, with the
right concept per company and the Q4 subtractions already done:

{base_query(plan)}

YOUR JOB
{job}

Worked example of (b), for "rank these companies by year-over-year revenue
growth in FY2024":

  WITH plan(...) AS (VALUES ...),        -- copied unchanged from above
  base AS (
      SELECT ... FROM plan p JOIN ...    -- the rest of the query above
  ),
  growth AS (
      SELECT element_id, company_cik, ticker, entity_name,
             fiscal_year, fiscal_period, period_start, period_end, is_instant,
             value / NULLIF(lag(value) OVER (PARTITION BY company_cik
                                             ORDER BY fiscal_year), 0) - 1 AS value
      FROM base
  )
  SELECT element_id, company_cik, ticker, entity_name,
         fiscal_year, fiscal_period, period_start, period_end, is_instant,
         value, 'pure' AS unit, 'yoy_growth' AS derivation
  FROM growth
  WHERE value IS NOT NULL AND fiscal_year = 2024
  ORDER BY value DESC

Note what that example does: the computed rows say `unit = 'pure'` because a
growth rate is dimensionless, and `derivation = 'yoy_growth'` because the
number is no longer revenue.

RULES (the statement is rejected if it breaks one)
1. Project exactly these column names, in any order:
   {columns}
2. `{VIEW}` is the only readable relation. `fact`, `filing`, `company` and
   `concept` will raise a permission error.
3. One statement. SELECT only -- no SET, no set_config(), no data-modifying
   CTE, no SELECT INTO, no locking clause.
4. Give every projected column an explicit alias unless it is a bare column
   reference. `NULL::text` without an alias is a column named "text".
5. No LIMIT above 500.
6. Never mix rows of different `unit` in one arithmetic expression.

CAVEATS ALREADY ATTACHED TO THIS PLAN (do not drop rows because of them)
{chr(10).join(notes) if notes else "- none"}

SQL:"""
