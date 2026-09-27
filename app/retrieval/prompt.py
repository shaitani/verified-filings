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

import re
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
    """One ``(binding, period)`` pair: **one row of the answer**.

    The unit of the row-count promise -- ``len(plan_cells(plan))`` is how many
    rows a plain retrieval should return -- which is why a cell stays whole
    when its metric is arithmetic. ``gross_margin`` reads two facts and
    answers with one number, so it is one cell holding two ``concept_ids``,
    not two cells. The filter table renders a line per operand; the count the
    verdict checks does not.
    """

    element_id: str
    company_cik: int
    fiscal_year: int
    fiscal_period: str
    period_start: date
    period_end: date

    #: Positional: ``concept_ids[0]`` is ``c0`` in ``expression``.
    concept_ids: tuple[int, ...]

    #: Arithmetic over the operands -- "c0" for the ordinary case, "c0 / c1"
    #: for a ratio. Straight from ``Binding.expression``.
    expression: str

    #: The unit the **facts** are filed in, which is what the join keys on.
    #: For a ratio this is not ``result_unit``: there are no ``pure`` facts
    #: behind a gross margin, only the USD ones it divides.
    unit: str

    #: The unit of the **answer** -- ``pure`` for a ratio. What the row must
    #: be projected with, and what ``execute()`` checks it came back as.
    result_unit: str

    is_instant: bool
    binding_index: int

    @property
    def operands(self) -> int:
        return len(self.concept_ids)


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
    A multi-operand binding -- ``gross_margin`` is ``c0 / c1`` over two USD
    concepts -- produces one cell carrying both ``concept_ids`` and the
    expression. It is still one row of the answer.
    """
    cells: list[PlanCell] = []
    for index, binding in enumerate(plan.bindings):
        concept_ids = tuple(concept.concept_id for concept in binding.concepts)
        for period in _periods_for(binding, plan):
            cells.append(
                PlanCell(
                    element_id=binding.element_id,
                    company_cik=period.company_cik,
                    fiscal_year=period.fiscal_year,
                    fiscal_period=period.fiscal_period,
                    period_start=period.period_start,
                    period_end=period.period_end,
                    concept_ids=concept_ids,
                    expression=binding.expression,
                    unit=binding.fact_unit,
                    result_unit=binding.unit,
                    is_instant=binding.is_instant,
                    binding_index=index,
                )
            )
    if not cells:
        raise UnsupportedPlan("the plan binds nothing, so there is no query to write")
    return cells


NEWLINE = chr(10)

#: The name of the CTE this module writes and the model selects from.
CTE_NAME = "wanted"

#: How much rendered CTE to inline before falling back to describing it.
#:
#: Moving the CTE out of the prompt (2026-09-24) shrank it to a constant ~6,200
#: characters and fixed q038. It also broke q001, q004, q005, q007 and q008,
#: and the mechanism took a day to find: **a model that cannot see the CTE
#: invents a filter to narrow it.** Told only "1 row(s) of coordinates", it
#: wrote ``WHERE w.fiscal_year = '2024' AND w.fiscal_period = 'Q2'`` -- lifting
#: `Q2` from the OUTPUT section, the one place in the whole prompt where a
#: fiscal_period value appears. The join then matched nothing and a question
#: that had worked for four days returned `empty`.
#:
#: Eight wordings were measured against it and only one held: show the rows.
#: Telling it "add no other filter", or "your statement has NO WHERE clause at
#: all", or making that a numbered RULE, each fixed some questions and broke
#: others -- and q005 answered every prohibition with a *different* invented
#: filter (`v.concept_id`, then `v.concept_name`). Showing it the two bad
#: clauses as things not to write taught it to write them, which is
#: DESIGN.md §4.3's recurring lesson arriving again.
#:
#: So the CTE is inlined while it is small, and described when it is not. The
#: gate is characters rather than rows because a multi-operand plan carries two
#: rows per cell: what has to stay bounded is the prompt. At 4,000 the whole
#: eval set inlines except q038 (378 rows, 34,664 characters, which would put
#: the prompt at 43,542 -- roughly 11k tokens against an 8,192 context, and it
#: would truncate). q038 is also the question the optimisation was made for,
#: and it passes without seeing the CTE.
MAX_INLINE_CTE_CHARS = 4000

#: Column names for the coordinate CTE. One constant so the worked example, the
#: CTE's own declaration and the prose describing it can never drift apart -- a
#: model shown two different column lists has been given a reason to invent a
#: third.
#:
#: Every column is named either exactly as the contract wants it projected, or
#: after the view column it joins to. Nothing needs renaming on the way
#: through. Two measured failures produced that: short names `fy`/`fp` came
#: back projected as `fy` and `fp`, which the contract refused, and before that
#: a single combined "Q12023" label came back as `fiscal_period = 'Q12023'`.
_CTE_COLUMNS = (
    "element_id", "company_cik", "fiscal_year", "fiscal_period",
    "concept_id", "unit", "is_instant", "window_start", "window_end",
)

#: The same, plus `operand`, used only when some metric is arithmetic over more
#: than one concept. Two shapes rather than one column that is always 0,
#: because the single-operand path is the one that took five measured failures
#: to get right and is not worth disturbing for the 13% of curated metrics that
#: are ratios.
_CTE_COLUMNS_OPERANDS = (
    "element_id", "company_cik", "fiscal_year", "fiscal_period", "operand",
    "concept_id", "unit", "is_instant", "window_start", "window_end",
)


def _cte_columns(cells: list[PlanCell]) -> tuple[str, ...]:
    combining = any(cell.operands > 1 for cell in cells)
    return _CTE_COLUMNS_OPERANDS if combining else _CTE_COLUMNS


def emit_cte(cells: list[PlanCell]) -> str:
    """The coordinate CTE, written by **this module** rather than by the model.

    The single most important line in this package, for one reason: every date,
    cik and concept_id in the statement now comes from a typed Python object,
    so the model cannot get one wrong. It is not persuaded not to -- it is never
    asked.

    Measured 2026-09-24 on q038, "Which company had the largest single-quarter
    revenue decline?". Asked to transcribe 378 coordinate rows into a VALUES
    list, qwen2.5-coder:7b wrote 40 of them and **computed** the windows for
    those from the fiscal-year label: Oracle's FY2021 Q1 came out as 2021-06-01
    where the plan says 2020-06-01, because Oracle's year ends in May. A fiscal
    year's name and its dates are independent (PITFALLS §1.1). Every window was
    twelve months wrong, and the rows were attributable, plausible and in the
    right unit -- nothing downstream would have caught it.

    Two limits caused that and this removes both. The 378-row prompt was ~25,700
    tokens against an 8,192-token window, which Ollama truncates silently; and
    even given the whole thing at ``num_ctx`` 32,768 the model still wrote only
    139 of 378 rows, because transcription fidelity is its own ceiling. Emitting
    the CTE here took the prompt from 53,114 characters to 1,701 and the answer
    from 40 rows to 378.

    This is the division ``app/retrieval/__init__.py`` always described: fetching
    the values a ``Binding`` names is bounded, deterministic work, and the model
    writes the layer above it.
    """
    columns = _cte_columns(cells)
    combining = "operand" in columns
    rows = []
    for cell in cells:
        for operand, concept_id in enumerate(cell.concept_ids):
            values = [
                f"'{cell.element_id}'",
                str(cell.company_cik),
                str(cell.fiscal_year),
                f"'{cell.fiscal_period}'",
                *([str(operand)] if combining else []),
                str(concept_id),
                f"'{cell.unit}'",
                "true" if cell.is_instant else "false",
                f"DATE '{cell.period_start}'",
                f"DATE '{cell.period_end}'",
            ]
            rows.append("    (" + ", ".join(values) + ")")
    declared = ", ".join(columns)
    opener = f"WITH {CTE_NAME}({declared}) AS (" + NEWLINE + "  VALUES" + NEWLINE
    return opener + ("," + NEWLINE).join(rows) + NEWLINE + ")"


#: ``QueryIn.intent`` is the parser saying what kind of answer is wanted, and
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

def wants_period_changes(plan: QueryPlan) -> bool:
    """Whether the change from each period to the next is computed in Python.

    A series along ``period``, single-concept and not ``pure``: a percentage
    change of a margin reads as a change in points and is not one. Not for
    ``rank`` / ``derive``, where the model is asked to compute and its rows
    are already derived. See ``app/retrieval/changes.py``.
    """
    if "period" not in plan.result.axes or plan.intent in DERIVING_INTENTS:
        return False
    return _plain_figures(plan)


def _plain_figures(plan: QueryPlan) -> bool:
    """Every binding a single concept, as filed, in a unit that is not ``pure``."""
    return all(len(b.concepts) == 1 and b.unit != "pure" for b in plan.bindings)


def wants_side_by_side(plan: QueryPlan) -> bool:
    """A comparison whose answer is the figures themselves, side by side.

    q008 ("How does Tesla's R&D spending compare to Meta's?") and q036 were
    both offered `_JOB_EITHER`; the word "compare" pushed the model toward
    branch (b), where there is no DERIVATION example to follow. q008 put a
    LAG in `derivation`; q036 renamed `value` to `apple_revenue`. Two
    companies' filed figures next to each other already are the comparison.
    """
    return plan.intent == "compare" and _plain_figures(plan)


#: The job for a plan whose period-to-period change is computed afterwards.
#: One option, not two: q010 ("how fast has NVIDIA's revenue grown") was
#: offered the choice, took (b) with no DERIVATION example in the prompt, and
#: put the growth rate in `derivation`. Nothing is left for it to compute.
_JOB_FIGURES_ONLY = """Reply with the worked example above, unchanged.

The change from one period to the next is computed AFTER your statement runs,
from the rows it returns. Do not compute it, do not add LAG or any window
function, and leave `derivation` NULL on every row."""

#: The job for `wants_side_by_side`. Same reasoning as `_JOB_FIGURES_ONLY`.
_JOB_SIDE_BY_SIDE = """Reply with the worked example above, unchanged.

The figures side by side ARE the comparison: the reader sees each company's
value next to the others. Do not compute a difference, a ratio, a rank or
anything else, and leave `derivation` NULL on every row."""

_JOB_EITHER = """Decide which of these two the question needs. Read the question again before
choosing -- returning the figures when the question asked for a comparison
between them answers a different question.

(a) The question asks for the figures themselves ("what was X's revenue").
    Reply with the worked example above, unchanged.

(b) The question asks for something COMPUTED from them -- a growth rate, a
    ranking, a share of a total, a difference, a filter on a computed value.
    Then follow the DERIVATION example below instead of the as-filed one.
    Set `derivation` to a short name for what you computed. Leave it NULL only
    on rows whose `value` is a figure exactly as filed -- a computed value with
    a NULL derivation is reported to the reader as the metric itself, which is
    wrong."""

#: The derivation the model is shown, on an invented derivation name.
#:
#: Added 2026-09-24, and it is the difference between q038 failing and passing.
#: Told in prose to compute, qwen2.5-coder:7b wrote
#: ``WHERE w.fiscal_period = 'Q4' ORDER BY v.value DESC LIMIT 1`` -- a filter
#: the plan never asked for, an ordering by *revenue* rather than by change, and
#: one row, labelled ``derivation = 'revenue_decline'`` having computed no
#: decline. One row of twenty companies also fails the verdict, correctly.
#:
#: **One level, not two.** A first attempt showed the computation in a subquery
#: with the outer level filtering the NULL leading row. The model flattened it,
#: aliased the computed column ``AS change``, and the projection was refused --
#: the same refusal to restructure measured earlier the same day on q011's
#: growth query. It does not need two levels: PostgreSQL takes an output alias
#: in ORDER BY, and a NULL ``value`` is legal on a derived row now
#: (``ResultRow._null_value_needs_a_derivation``), so there is nothing to filter.
_EXAMPLE_DERIVED = f"""WORKED EXAMPLE OF A DERIVATION (invented name -- copy the FORM)

A question asking which company's figure fell most from one period to the next.
ONE level: the computed column is aliased `value`, and the window function goes
straight in the projection.

  SELECT w.element_id, v.company_cik, v.ticker, v.entity_name,
         w.fiscal_year, w.fiscal_period,
         v.period_start, v.period_end, v.is_instant,
         v.value - LAG(v.value) OVER (
           PARTITION BY v.company_cik ORDER BY v.period_end) AS value,
         v.unit, 'qoq_change' AS derivation
  FROM {CTE_NAME} w
  JOIN {VIEW} v
    ON  v.company_cik = w.company_cik
    AND v.concept_id  = w.concept_id
    AND v.unit        = w.unit
    AND v.is_instant  = w.is_instant
    AND v.period_end  = w.window_end
    AND (w.is_instant OR v.period_start = w.window_start)
  ORDER BY value ASC
  LIMIT {MAX_ROWS}

Three things that gets right and are easy to get wrong:

  - The computed column is aliased `value`, never its own name. The projection
    is fixed and `change` is not one of its columns.
  - `PARTITION BY v.company_cik`, so each company is compared against ITSELF.
  - No `LIMIT 1` and no WHERE of your own. `LIMIT 1` answers "what is the
    biggest fall" but not "which company", and a ranking the reader cannot see
    is not a ranking. The first period of each company has nothing before it,
    so its computed value is NULL -- that is expected, and it is kept.
"""

_JOB_MUST_DERIVE = """This question asks for a value that must be COMPUTED from those rows. The
as-filed example is NOT the answer -- returning it unchanged, or with only an
ORDER BY added, answers a different question.

Follow the DERIVATION example instead. In every row you compute, set
`derivation` to a short name for what it is, and set `unit` to what the computed
number actually is ('pure' for a ratio or a growth rate). Leave `derivation`
NULL only on rows whose `value` is a figure exactly as filed."""


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
_EXAMPLE_PLAIN = f"""WORKED EXAMPLE -- this is the whole shape of an as-filed answer:

  SELECT w.element_id, v.company_cik, v.ticker, v.entity_name,
         w.fiscal_year, w.fiscal_period,
         v.period_start, v.period_end, v.is_instant,
         v.value, v.unit, NULL::text AS derivation
  FROM {CTE_NAME} w
  JOIN {VIEW} v
    ON  v.company_cik = w.company_cik
    AND v.concept_id  = w.concept_id
    AND v.unit        = w.unit
    AND v.is_instant  = w.is_instant
    AND v.period_end  = w.window_end
    AND (w.is_instant OR v.period_start = w.window_start)
  LIMIT {MAX_ROWS}

Note that value, ticker and entity_name appear ONLY as v.<column>. They are
never written as literals -- they are what you are querying FOR. Every
coordinate is w.<column>, because those are already in `{CTE_NAME}`.
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


#: The same example for a plan whose metrics combine operands.
#:
#: Two of them, not one adaptive one, because the difference is not cosmetic:
#: the column list gains `operand` and the select gains a pivot and a GROUP
#: BY. Handing over the plain example beside an operand filter table produced
#: exactly the failure that would predict -- ten values per row against nine
#: declared column names, so `unit` received a concept id and `is_instant`
#: received 'USD', failing as `boolean = text`.
def _operand_terms(expression: str) -> str:
    """``c0 - c1`` -> the two aggregate terms, with a NULLIF around any divisor.

    The example used to hard-code ``c0 / c1`` and then spend two paragraphs
    correcting itself -- "Had the expression been `c0 - c1` instead..." and
    "do not copy the operator from this example". Rendering the plan's own
    operator removes the mismatch instead of apologising for it. The ids in the
    example stay invented; an operator is structure, not data.
    """
    term = "max(v.value) FILTER (WHERE w.operand = {n})"
    rendered = re.sub(r"/\s*c(\d+)", lambda m: "/ NULLIF(" + term.format(n=m.group(1)) + ", 0)",
                      expression)
    return re.sub(r"c(\d+)", lambda m: term.format(n=m.group(1)), rendered)


#: The second CTE, written by ``emit_figures``: one row per answer cell with
#: every metric's value already computed and any threshold already applied.
FIGURES_NAME = "figures"


def uses_figures(plan: QueryPlan) -> bool:
    """Whether Python computes the values, so the model never sees an operand.

    True when the plan has a metric that is arithmetic over several concepts --
    a margin, a ratio, free cash flow -- or a threshold. Both are fully written
    down in the plan (``Binding.expression`` / ``unit``, ``PlanThreshold``), and
    both failed when the model was asked to write them: q007 turned ``c0 / c1``
    into ``c0 - c1``, q024 dropped the subtraction in free cash flow and was
    graded ``pass`` at 64.1 billion, q009 ranked operating income *minus*
    revenue. Written here, the operator cannot change.

    Needs one expression and one unit per element across its companies, which
    one curated entry always gives; otherwise the old path stands.
    """
    if not plan.bindings:
        return False
    cells = plan_cells(plan)
    if not (plan.thresholds or any(cell.operands > 1 for cell in cells)):
        return False
    shape_of: dict[str, tuple[str, str]] = {}
    for cell in cells:
        shape = (cell.expression, cell.result_unit)
        if shape_of.setdefault(cell.element_id, shape) != shape:
            return False
    return True


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def emit_figures(plan: QueryPlan) -> str:
    """``figures AS (...)``: the value of every cell, computed from ``wanted``.

    Multi-operand: the combining form -- one ``max(v.value) FILTER (WHERE
    w.operand = N)`` per ``cN`` via ``_operand_terms`` (NULLIF around a
    divisor), grouped to one row per cell. Single-operand: the plain join.

    A threshold is applied here, in the statement, so a ranking or derivation
    written above it only ever sees the rows that qualify -- "rank the
    companies with a margin above 40%" filters first, then ranks. The executor
    still re-checks every row against it (``_threshold_violations``).
    """
    cells = plan_cells(plan)
    combining = any(cell.operands > 1 for cell in cells)
    shape_of = {cell.element_id: (cell.expression, cell.result_unit) for cell in cells}

    def per_element(pick) -> str:
        whens = "".join(
            f" WHEN {_sql_literal(element)} THEN {pick(element)}" for element in shape_of
        )
        return f"CASE w.element_id{whens} END"

    if combining:
        term = {element: _operand_terms(shape_of[element][0]) for element in shape_of}
        value = per_element(lambda element: term[element])
        unit = per_element(lambda element: _sql_literal(shape_of[element][1]))
    else:
        term = {element: "v.value" for element in shape_of}
        value, unit = "v.value", "v.unit"
    conditions = [
        f"(w.element_id <> {_sql_literal(t.element_id)} OR ({term[t.element_id]}) "
        f"{t.operator} {t.value:f})"
        for t in plan.thresholds
        if t.element_id in term
    ]
    lines = [
        f"{FIGURES_NAME} AS (",
        "  SELECT w.element_id, v.company_cik, v.ticker, v.entity_name,",
        "         w.fiscal_year, w.fiscal_period,",
        "         v.period_start, v.period_end, v.is_instant,",
        f"         {value} AS value,",
        f"         {unit} AS unit",
        f"  FROM {CTE_NAME} w",
        f"  JOIN {VIEW} v",
        "    ON  v.company_cik = w.company_cik",
        "    AND v.concept_id  = w.concept_id",
        "    AND v.unit        = w.unit",
        "    AND v.is_instant  = w.is_instant",
        "    AND v.period_end  = w.window_end",
        "    AND (w.is_instant OR v.period_start = w.window_start)",
    ]
    if combining:
        lines += [
            "  GROUP BY w.element_id, v.company_cik, v.ticker, v.entity_name,",
            "           w.fiscal_year, w.fiscal_period,",
            "           v.period_start, v.period_end, v.is_instant",
        ]
    if conditions:
        keyword = "HAVING" if combining else "WHERE"
        lines.append(f"  {keyword} " + (chr(10) + "    AND ").join(conditions))
    lines.append(")")
    return chr(10).join(lines)


#: The whole SELECT for a plan that computes nothing above its cells: the
#: figures, as they are. The model is not asked.
FIGURES_SELECT = f"""SELECT element_id, company_cik, ticker, entity_name,
       fiscal_year, fiscal_period, period_start, period_end, is_instant,
       value, unit, NULL::text AS derivation
FROM {FIGURES_NAME}
LIMIT {MAX_ROWS}"""


_FIGURES_JOB_RANK = """This question asks for an ORDERING of the figures -- which is highest,
lowest, largest, smallest. Return EVERY row of `figures` with `value` and
`unit` unchanged and `derivation` NULL, ordered the way the question asks:
DESC for highest / largest / most, ASC for lowest / smallest / least.
No LIMIT 1 and no WHERE: the reader has to see the whole ordering, and every
row of `figures` is part of the answer."""

_FIGURES_EXAMPLE_RANK = f"""WORKED EXAMPLE (copy the FORM)

  SELECT f.element_id, f.company_cik, f.ticker, f.entity_name,
         f.fiscal_year, f.fiscal_period, f.period_start, f.period_end, f.is_instant,
         f.value, f.unit, NULL::text AS derivation
  FROM {FIGURES_NAME} f
  ORDER BY f.value DESC
  LIMIT {MAX_ROWS}"""

_FIGURES_JOB_DERIVE = """This question asks for a value COMPUTED from the figures -- a change, a
share of a total, a growth rate. Compute it from `f.value` with a window
function over `figures`, alias the result `value`, set `unit` to what the
result is ('pure' for a ratio or a growth rate, the figures' own unit for a
difference) and `derivation` to a short name for what you computed.
Keep every row: the first period of a series has nothing before it, so its
result is NULL, which is expected. No LIMIT 1."""

_FIGURES_EXAMPLE_DERIVE = f"""WORKED EXAMPLE (invented name -- copy the FORM)

  SELECT f.element_id, f.company_cik, f.ticker, f.entity_name,
         f.fiscal_year, f.fiscal_period, f.period_start, f.period_end, f.is_instant,
         f.value - LAG(f.value) OVER (
           PARTITION BY f.element_id, f.company_cik ORDER BY f.period_end) AS value,
         f.unit, 'change_from_prior' AS derivation
  FROM {FIGURES_NAME} f
  ORDER BY value ASC
  LIMIT {MAX_ROWS}"""


def _figures_prompt(plan: QueryPlan) -> str:
    """The prompt for a ranking or derivation over ``figures``.

    Short on purpose: no relation, no coordinates, no join and no operands --
    ``figures`` already holds every value, computed. What is left is the layer
    the model is for.
    """
    spec = plan.result
    cells = plan_cells(plan)
    notes = _plan_notes(plan)
    units = sorted({(cell.element_id, cell.result_unit) for cell in cells})
    unit_lines = chr(10).join(f"      {element}: unit '{unit}'" for element, unit in units)
    threshold_line = ""
    if plan.thresholds:
        tests = "; ".join(
            f"{t.element_id} {t.operator} {t.value:f} (from {t.element_text!r})"
            for t in plan.thresholds
        )
        threshold_line = (
            f"{chr(10)}  - Only rows meeting the question's condition are in `figures`: "
            f"{tests}.{chr(10)}    It is already applied. Do not filter again."
        )
    ranking = plan.intent == "rank"
    job = _FIGURES_JOB_RANK if ranking else _FIGURES_JOB_DERIVE
    example = _FIGURES_EXAMPLE_RANK if ranking else _FIGURES_EXAMPLE_DERIVE
    return f"""You write the SELECT half of one PostgreSQL statement. SQL only, nothing else.

QUESTION
{plan.question}

WHAT THE ANSWER MUST CONTAIN
shape={spec.shape}, varies along {spec.axes or ["nothing"]}, {spec.row_count} row(s) of
underlying data ({spec.companies} compan(ies) x {spec.periods} period(s) x
{spec.metrics} metric(s)). Do not collapse an axis the answer varies along.

EVERYTHING YOU NEED IS IN ONE CTE: `{FIGURES_NAME}`
It is already written above whatever you write. Do NOT write `WITH`. Begin
your reply at `SELECT`, and read FROM `{FIGURES_NAME}`.

  {FIGURES_NAME}(element_id, company_cik, ticker, entity_name, fiscal_year,
          fiscal_period, period_start, period_end, is_instant, value, unit)

  - One row per company, period and metric the question needs.
  - `value` is each metric's figure ALREADY COMPUTED -- a margin is already
    divided, free cash flow already subtracted -- in `unit`:
{unit_lines}
    Never recompute a metric from other figures, and never join anything to
    get one. Read `value`.{threshold_line}
  - `ticker` and `entity_name` are for display only. Never filter on them.

{example}

YOUR JOB
{job}

OUTPUT
Project exactly these column names, in any order:
  {", ".join(RESULT_COLUMNS)}

RULES (the statement is rejected if it breaks one)
1. Begin at SELECT and read FROM `{FIGURES_NAME}`. No `WITH`, no `VALUES`.
   SELECT only: no SET, no set_config(), no SELECT INTO, no locking clause.
2. End with `LIMIT {MAX_ROWS}`.
3. Give every projected column an explicit alias unless it is a bare column
   reference. `NULL::text` without an alias is a column named "text".
4. Never write a number, a date or a company name into the SQL as a literal.

CAVEATS ALREADY ATTACHED TO THIS PLAN (do not drop rows because of them)
{chr(10).join(notes) if notes else "- none"}

SQL:"""


def _plan_notes(plan: QueryPlan) -> list[str]:
    notes = [f"- {note.kind}: {note.message}" for note in plan.notes]
    for index, binding in enumerate(plan.bindings):
        for note in binding.notes:
            notes.append(f"- binding {index} ({binding.element_id}) {note.kind}: {note.message}")
    return notes


#: Characters of *this* prompt per token, measured 2026-09-24: the 378-cell
#: q038 prompt is 53,114 characters and Ollama reported ``prompt_eval_count``
#: 25,729 for it. 2.06, nowhere near prose's usual 4 -- a filter table is
#: digits, dashes and pipes, which tokenise badly. Rounded down, so the
#: estimate errs towards refusing.
CHARS_PER_TOKEN = 2.0

#: Mirrors ``generator.CONTEXT_TOKENS``; a test keeps the two in step. Copied
#: rather than imported because ``generator`` imports *this* module, and the
#: cycle is not worth a shared constants file.
_CONTEXT_TOKENS = 8192

#: The longest prompt worth sending. Beyond the window the model reads through,
#: **Ollama truncates silently** -- no error, no warning, and the model answers
#: from the part it saw.
MAX_PROMPT_CHARS = int(_CONTEXT_TOKENS * CHARS_PER_TOKEN)


def build_prompt(plan: QueryPlan) -> str:
    """Render ``plan`` as the text Qwen is asked to write SQL from.

    Raises ``UnsupportedPlan`` when the rendered prompt cannot fit the model's
    context window. See ``_refuse_if_too_long``.
    """
    if uses_figures(plan):
        return _figures_prompt(plan)
    cells = plan_cells(plan)
    if plan.thresholds or any(cell.operands > 1 for cell in cells):
        # `uses_figures` declined a plan it should hold: one element with two
        # expressions or units across its companies, which one curated entry
        # cannot produce. Refused rather than handed to the model -- metric
        # arithmetic and threshold filters are never the model's to write.
        raise UnsupportedPlan(
            "a metric's arithmetic or threshold could not be written in Python "
            "(one element with more than one expression or unit), and it is "
            "never handed to the model"
        )
    spec = plan.result
    notes = _plan_notes(plan)
    columns = ", ".join(RESULT_COLUMNS)
    if plan.intent in DERIVING_INTENTS:
        job = _JOB_MUST_DERIVE
    elif wants_period_changes(plan):
        job = _JOB_FIGURES_ONLY
    elif wants_side_by_side(plan):
        job = _JOB_SIDE_BY_SIDE
    else:
        job = _JOB_EITHER
    worked_example = _EXAMPLE_PLAIN
    # Shown for a deriving plan. Measured 2026-09-24: shown alongside the
    # combining example on q009 ("highest operating margin"), the model took the
    # example's `-` where its own expression said `/`. Metric arithmetic is now
    # written in Python (`figures`), so no plan reaching this prompt combines
    # operands and the two are never shown together.
    if plan.intent in DERIVING_INTENTS:
        worked_example = worked_example + chr(10) + _EXAMPLE_DERIVED
    declared = ", ".join(_cte_columns(cells))
    rendered = emit_cte(cells)
    if len(rendered) <= MAX_INLINE_CTE_CHARS:
        indented = chr(10).join("  " + line for line in rendered.splitlines())
        cte_body = (
            "This is it in full -- it is already written, do not repeat it:"
            + chr(10) * 2
            + indented
        )
    else:
        cte_body = f"  {CTE_NAME}({declared})"

    prompt = f"""You write the SELECT half of one PostgreSQL statement. SQL only, nothing else.

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

A COORDINATE CTE IS ALREADY WRITTEN FOR YOU
Do NOT write `WITH`. Do NOT write a `VALUES` list. Begin your reply at
`SELECT`. A CTE named `{CTE_NAME}` is already defined above whatever you write,
holding {len(cells)} row(s) of coordinates across {spec.companies} compan(ies):

{cte_body}

Every coordinate you need is in it, already correct. **Never write a date, a
cik or a concept_id as a literal** -- there is nothing to copy and nothing to
work out. `concept_id` differs per company on purpose, because filers tag the
same business concept differently, and `{CTE_NAME}` already knows which is
which.

`element_id`, `fiscal_year` and `fiscal_period` exist ONLY in `{CTE_NAME}` --
the relation has no such columns -- so project them as `w.<column>`.

`{CTE_NAME}` is the COMPLETE row selection. Add no other filter. In particular,
do not filter on `ticker` or `entity_name` using names from the question: the
question says "Apple", the database says "Apple Inc.", and a filter on the
one finds none of the other. `company_cik` in the CTE already identifies the
company exactly; `ticker` and `entity_name` are for display only.

A Q4 row is no different from any other: no filer reports a fourth quarter,
but the relation supplies one anyway, already computed. Fetch it like the rest.

Match each row with an EXACT equality on all of:
  company_cik, concept_id, unit, is_instant, period_end = window_end
and, only when is_instant is false, period_start = window_start.
Do not use BETWEEN or a date range: two different periods can end in the same
year, and a range returns both.

Joining `{CTE_NAME}` to the relation also keeps `element_id` and the period
labels attached to the right rows.
{worked_example}

YOUR JOB
{job}

OUTPUT
Project exactly these column names, in any order:
  {columns}

  - `element_id`, `fiscal_year` and `fiscal_period` come from `{CTE_NAME}`
    and are projected under exactly those names -- the relation has no such
    columns. `fiscal_period` is one of 'FY', 'Q1', 'Q2', 'Q3', 'Q4' and
    nothing else.
  - `period_start`, `period_end`, `is_instant`, `value`, `unit`, `ticker`,
    `entity_name`, `company_cik` come from the relation.

RULES (the statement is rejected if it breaks one)
0. EVERY value must be read from the relation. Never write a number, a
   ticker, a date or a company name into the SQL as a literal -- a statement
   that does not read `{VIEW}` is rejected outright. `{CTE_NAME}` says WHICH
   rows to fetch; it holds no values, and the values are not yours to supply.
1. Begin at SELECT. No `WITH` and no `VALUES` -- the CTE is already written,
   and a second one replaces it. SELECT only: no SET, no set_config(), no
   data-modifying CTE, no SELECT INTO, no locking clause.
2. `{VIEW}` is the only readable relation. `fact`, `filing`, `company` and
   `concept` will raise a permission error.
3. End with `LIMIT {MAX_ROWS}`. A statement with no LIMIT is rejected;
   nothing adds one for you, and a smaller limit is not an improvement --
   it drops rows the plan asked for.
4. Give every projected column an explicit alias unless it is a bare column
   reference. `NULL::text` without an alias is a column named "text".
5. Never mix rows of different `unit` in one arithmetic expression.

CAVEATS ALREADY ATTACHED TO THIS PLAN (do not drop rows because of them)
{chr(10).join(notes) if notes else "- none"}

SQL:"""
    _refuse_if_too_long(prompt, cells)
    return prompt


def _refuse_if_too_long(prompt: str, cells: list[PlanCell]) -> None:
    """Refuse a prompt the model cannot read all of.

    **Ollama truncates an over-long prompt without saying so.** Measured
    2026-09-24 on q038, "Which company had the largest single-quarter revenue
    decline?": 378 cells render to 53,114 characters, roughly 25,700 tokens,
    against a window of 8,192. The model ingested 4,098 of them -- it never saw
    five sixths of the filter table -- and wrote a syntactically valid statement
    covering 40 cells.

    What makes that the worst failure shape in this project is the second half.
    Working from a table it could only partly see, the model stopped copying
    windows and started *computing* them from the fiscal-year label: Oracle's
    FY2021 Q1 came out as 2021-06-01 when the plan says 2020-06-01, because
    Oracle's year ends in May. A fiscal year's name and its dates are
    independent (PITFALLS §1.1), so every one of those windows was twelve months
    wrong -- and the rows were attributable, plausible and in the right unit.
    Nothing downstream would have caught it.

    Raising the window is not the fix and was measured too: at ``num_ctx``
    32,768 the same prompt got all 25,729 tokens in and the model still wrote
    only 139 of 378 rows. Transcription fidelity is its own limit. This check
    exists so the case *fails loudly* in the meantime, which is the whole
    premise of the project -- the alternative is invented dates reaching a
    reader.
    """
    if len(prompt) <= MAX_PROMPT_CHARS:
        return
    raise UnsupportedPlan(
        f"the plan renders to {len(prompt):,} characters (~"
        f"{int(len(prompt) / CHARS_PER_TOKEN):,} tokens) for {len(cells)} cell(s), "
        f"against a {_CONTEXT_TOKENS:,}-token context window. Ollama would "
        f"truncate it silently and the model would answer from the part it saw, "
        f"inventing the windows it could not read"
    )
