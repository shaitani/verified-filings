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
from app.schemas.query import (
    Binding,
    PeriodRef,
    QueryPlan,
    ResolvedPeriod,
    over_time_pairs,
)
from app.schemas.result import RESULT_COLUMNS

#: The one relation the retrieval role can read.
VIEW = "xbrl.reported_fact"


class UnsupportedPlan(ValueError):
    """A plan this module will not render SQL for.

    Separate from a *refusal to answer*: the plan may be perfectly good and
    the question answerable. It means this layer cannot express it yet, which
    is a gap to close rather than a caveat to pass on to a reader.
    """


class TooManyFigures(ValueError):
    """A plan needing more rows than one answer may hold (``MAX_ROWS``).

    Unlike ``UnsupportedPlan``, a refusal to answer: the reader can fix it by
    asking about fewer companies or a shorter period, and is told so.
    """

    def __init__(self, figures: int) -> None:
        super().__init__(f"the plan needs {figures:,} rows; the ceiling is {MAX_ROWS:,}")
        self.figures = figures


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

    #: For a metric asked for over time only: the window each operand is read
    #: from, positionally. A growth cell reads the metric at its own period
    #: *and* the one before, so its operands do not share the cell's window.
    #: Empty means every operand is read at the cell's own window.
    operand_windows: tuple[tuple[date, date], ...] = ()

    #: Fiscal years between the two ends of a CAGR cell; 0 otherwise.
    span_years: int = 0

    #: What an over-time cell reports -- "change", "growth", "cagr" -- as the
    #: row's ``derivation``; ``None`` for a figure as filed or combined.
    derivation: str | None = None

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
    replaced = {entry.element_id for entry in plan.over_time if entry.replaces}
    cells: list[PlanCell] = []
    for index, binding in enumerate(plan.bindings):
        if binding.element_id in replaced:
            continue  # answered as its movement; see _over_time_cells
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
    cells += _over_time_cells(plan)
    if not cells:
        raise UnsupportedPlan("the plan binds nothing, so there is no query to write")
    return cells


def statement_limit(plan: QueryPlan) -> int:
    """The ``LIMIT`` every statement for ``plan`` carries: one more row than the
    plan names. An answer is never cut short, and a statement that fans out
    returns that one extra row, which the verdict refuses as ``over`` -- without
    fetching however many more it would have produced."""
    return len(plan_cells(plan)) + 1


#: Each over-time cell's arithmetic, in operand terms: ``cur`` is the metric
#: at the cell's own period, ``prev`` at the earlier one (``over_time_pairs``).
#: Guarded rather than divided blindly: a growth from a zero or negative base
#: has no meaning, and comes back NULL on a derived row -- which ``ResultRow``
#: allows -- rather than as a number.
_OVER_TIME_EXPRESSION = {
    "change": "({cur}) - ({prev})",
    "growth": "CASE WHEN ({prev}) > 0 THEN (({cur}) - ({prev})) / ({prev}) END",
    "cagr": (
        "CASE WHEN ({prev}) > 0 AND ({cur}) > 0 "
        "THEN power(({cur}) / ({prev}), 1.0 / max(w.span_years)) - 1 END"
    ),
}


def _shift(expression: str, by: int) -> str:
    """``c0 / c1`` shifted by 2 is ``c2 / c3`` -- the same metric's operands,
    read a second time at another period."""
    return re.sub(r"c(\d+)", lambda m: f"c{int(m.group(1)) + by}", expression)


def _over_time_cells(plan: QueryPlan) -> list[PlanCell]:
    """The answer cells of every metric asked for as change, growth or CAGR.

    Built per cell, not per binding, because the two ends can be bound to
    different concepts: NVIDIA tags revenue one way through FY2022 and another
    from FY2023, so its FY2023 growth reads one concept now and the other a
    year back. Each end takes its operands from whichever binding covers *its*
    period (``QueryPlan.binding_for``), and a cell is made only where both ends
    are bound. Which periods pair up is ``over_time_pairs``, shared with the
    mapper.
    """
    return [
        _over_time_cell(element_id, kind, current, base, now, then)
        for element_id, kind, current, base, now, then in _over_time_pairs_bound(plan)
    ]


def over_time_bases(plan: QueryPlan) -> dict[tuple[str, int, int, str, str], PeriodRef]:
    """``(element, cik, fiscal_year, fiscal_period, kind) -> base period`` for
    every over-time cell -- the same pairs the statement is written from, so the
    period a reader is told a change is "vs" is the one it was computed against."""
    return {
        (element_id, current.company_cik, current.fiscal_year, current.fiscal_period, kind): (
            PeriodRef(fiscal_year=base.fiscal_year, fiscal_period=base.fiscal_period)
        )
        for element_id, kind, current, base, _, _ in _over_time_pairs_bound(plan)
    }


def _over_time_pairs_bound(plan: QueryPlan):
    """``(element, kind, current, base, binding now, binding then)`` for each pair
    ``over_time_pairs`` makes where the metric is bound at both ends."""
    if not plan.over_time:
        return
    earlier = {
        (p.company_cik, p.fiscal_year, p.fiscal_period): p for p in plan.filters.support_periods
    }

    def bound(element_id: str, period: ResolvedPeriod) -> tuple[int, Binding] | None:
        try:
            return plan.binding_for(
                element_id, period.company_cik, period.fiscal_year, period.fiscal_period
            )
        except (LookupError, ValueError):
            return None

    for entry in plan.over_time:
        element_id, kind = entry.element_id, entry.kind
        for current, base in over_time_pairs(kind, plan.filters.periods, earlier):
            now, then = bound(element_id, current), bound(element_id, base)
            if now is not None and then is not None:
                yield element_id, kind, current, base, now, then


def _over_time_cell(
    element_id: str,
    kind: str,
    current: ResolvedPeriod,
    earlier: ResolvedPeriod,
    now: tuple[int, Binding],
    then: tuple[int, Binding],
) -> PlanCell:
    (index, binding_now), (_, binding_then) = now, then
    width = len(binding_now.concepts)
    expression = _OVER_TIME_EXPRESSION[kind].format(
        cur=binding_now.expression, prev=_shift(binding_then.expression, width)
    )
    return PlanCell(
        element_id=element_id,
        company_cik=current.company_cik,
        fiscal_year=current.fiscal_year,
        fiscal_period=current.fiscal_period,
        period_start=current.period_start,
        period_end=current.period_end,
        concept_ids=tuple(
            c.concept_id for c in [*binding_now.concepts, *binding_then.concepts]
        ),
        expression=expression,
        unit=binding_now.fact_unit,
        result_unit=binding_now.unit if kind == "change" else "pure",
        is_instant=binding_now.is_instant,
        binding_index=index,
        operand_windows=(
            *[(current.period_start, current.period_end)] * width,
            *[(earlier.period_start, earlier.period_end)] * len(binding_then.concepts),
        ),
        span_years=current.fiscal_year - earlier.fiscal_year if kind == "cagr" else 0,
        derivation=kind,
    )


NEWLINE = chr(10)

#: The name of the CTE this module writes and the model selects from.
CTE_NAME = "wanted"

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


#: The same, plus each answer cell's own window and a CAGR's span, used only
#: when some metric is asked for over time: its operands are read at windows
#: other than the cell's, so the cell has to be named separately.
#: `derivation` tells a metric's growth rows from its filed rows when both are
#: shown (a plain series and the growth beside it), so `figures` can compute
#: and group each separately.
_CTE_COLUMNS_OVER_TIME = (
    *_CTE_COLUMNS_OPERANDS, "cell_start", "cell_end", "span_years", "derivation",
)


def _cte_columns(cells: list[PlanCell]) -> tuple[str, ...]:
    if any(cell.operand_windows for cell in cells):
        return _CTE_COLUMNS_OVER_TIME
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
    year's name and its dates are independent (docs/GAPS.md D1.1). Every window was
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
    over_time = "cell_start" in columns
    rows = []
    for cell in cells:
        for operand, concept_id in enumerate(cell.concept_ids):
            start, end = (
                cell.operand_windows[operand]
                if cell.operand_windows
                else (cell.period_start, cell.period_end)
            )
            values = [
                f"'{cell.element_id}'",
                str(cell.company_cik),
                str(cell.fiscal_year),
                f"'{cell.fiscal_period}'",
                *([str(operand)] if combining else []),
                str(concept_id),
                f"'{cell.unit}'",
                "true" if cell.is_instant else "false",
                f"DATE '{start}'",
                f"DATE '{end}'",
                *(
                    [f"DATE '{cell.period_start}'", f"DATE '{cell.period_end}'",
                     str(cell.span_years),
                     f"'{cell.derivation}'" if cell.derivation else "NULL"]
                    if over_time
                    else []
                ),
            ]
            rows.append("    (" + ", ".join(values) + ")")
    declared = ", ".join(columns)
    opener = f"WITH {CTE_NAME}({declared}) AS (" + NEWLINE + "  VALUES" + NEWLINE
    return opener + ("," + NEWLINE).join(rows) + NEWLINE + ")"


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
    """Whether Python writes every value this plan reads -- which is always,
    for a plan whose every element has one expression and one unit per
    derivation, as one curated entry always gives.

    Python writes the fetch, the metric arithmetic, the threshold and any
    change, growth or CAGR, in `figures`, and a ranking's ORDER BY; the model
    is asked for nothing but a derivation above it (``needs_the_model``). It got here one
    failure at a time -- q007 turned ``c0 / c1`` into ``c0 - c1``, q024 dropped
    a subtraction and was graded ``pass`` at 64.1 billion, q009 ranked
    operating income *minus* revenue, q010 put a growth rate in `derivation`,
    q036 and q008 improvised when offered a choice -- and now holds for every
    plan: whatever the plan fully specifies is Python's.
    """
    if not plan.bindings:
        return False
    shape_of: dict[tuple[str, str | None], tuple[str, str]] = {}
    for cell in plan_cells(plan):
        shape = (cell.expression, cell.result_unit)
        if shape_of.setdefault((cell.element_id, cell.derivation), shape) != shape:
            return False
    return True


def _carries_derivation(cells: list[PlanCell]) -> bool:
    """Whether ``figures`` has a ``derivation`` column: some cell is over time."""
    return any(cell.derivation for cell in cells)


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def emit_figures(plan: QueryPlan) -> str:
    """``figures AS (...)``: the value of every cell, computed from ``wanted``.

    Multi-operand: the combining form -- one ``max(v.value) FILTER (WHERE
    w.operand = N)`` per ``cN`` via ``_operand_terms`` (NULLIF around a
    divisor), grouped to one row per cell. Single-operand: the plain join.

    A threshold is applied here, in the statement, so a ranking or derivation
    above it only ever sees the rows that qualify -- "rank the
    companies with a margin above 40%" filters first, then ranks. The executor
    still re-checks every row against it (``_threshold_violations``).
    """
    cells = plan_cells(plan)
    combining = any(cell.operands > 1 for cell in cells)
    over_time = _carries_derivation(cells)
    if over_time:
        return _emit_figures_over_time(plan, cells)
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


def _emit_figures_over_time(plan: QueryPlan, cells: list[PlanCell]) -> str:
    """``figures`` when some metric is asked for, or shown, over time.

    Each row is one answer cell, keyed by ``(element_id, derivation)``: a plain
    series and the growth beside it are the same element, so the derivation
    is what tells a filed row (``NULL``) from a growth row. Operands are read
    at other periods than the cell's, so the cell's own window comes from
    ``wanted`` and the row is grouped by it.
    """
    shape_of = {
        (cell.element_id, cell.derivation): (cell.expression, cell.result_unit)
        for cell in cells
    }

    def which(element: str, derivation: str | None) -> str:
        label = f"= {_sql_literal(derivation)}" if derivation else "IS NULL"
        return f"w.element_id = {_sql_literal(element)} AND w.derivation {label}"

    def per_shape(pick) -> str:
        whens = "".join(
            f" WHEN {which(element, derivation)} THEN {pick(element, derivation)}"
            for element, derivation in shape_of
        )
        return f"CASE{whens} END"

    value = per_shape(lambda e, d: _operand_terms(shape_of[(e, d)][0]))
    unit = per_shape(lambda e, d: _sql_literal(shape_of[(e, d)][1]))
    # A threshold tests its element's rows; a threshold plan has one shape per
    # element (the mapper adds no growth beside one), so that shape is it.
    term = {element: _operand_terms(shape_of[(element, d)][0]) for element, d in shape_of}
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
        "         CASE WHEN w.is_instant THEN NULL ELSE w.cell_start END AS period_start,",
        "         w.cell_end AS period_end, w.is_instant,",
        f"         {value} AS value,",
        f"         {unit} AS unit,",
        "         w.derivation AS derivation",
        f"  FROM {CTE_NAME} w",
        f"  JOIN {VIEW} v",
        "    ON  v.company_cik = w.company_cik",
        "    AND v.concept_id  = w.concept_id",
        "    AND v.unit        = w.unit",
        "    AND v.is_instant  = w.is_instant",
        "    AND v.period_end  = w.window_end",
        "    AND (w.is_instant OR v.period_start = w.window_start)",
        "  GROUP BY w.element_id, v.company_cik, v.ticker, v.entity_name,",
        "           w.fiscal_year, w.fiscal_period,",
        "           w.cell_start, w.cell_end, w.is_instant, w.derivation",
    ]
    if conditions:
        lines.append("  HAVING " + (chr(10) + "    AND ").join(conditions))
    lines.append(")")
    return chr(10).join(lines)


def _select(derivation: str, limit: int, order_by: str = "") -> str:
    """The whole SELECT over ``figures``, for a plan the model is not asked about."""
    return (
        f"""SELECT element_id, company_cik, ticker, entity_name,
       fiscal_year, fiscal_period, period_start, period_end, is_instant,
       value, unit, {derivation}
FROM {FIGURES_NAME}
"""
        + (order_by + chr(10) if order_by else "")
        + f"LIMIT {limit}"
    )


def _order_by(plan: QueryPlan) -> str:
    """A ranking's ORDER BY, from the direction the plan carries -- never from English."""
    rank = plan.result.rank
    if not rank:
        return ""
    keys = ["element_id"]  # each metric ranked on its own
    for direction, sql in (("highest", "DESC"), ("lowest", "ASC")):
        ids = sorted(e for e, d in rank.items() if d == direction)
        if ids:
            names = ", ".join(_sql_literal(e) for e in ids)
            # NULLS LAST: a growth from a zero base has no value and no place.
            keys.append(f"CASE WHEN element_id IN ({names}) THEN value END {sql} NULLS LAST")
    keys += ["company_cik", "period_end"]  # ties, and unranked metrics, in a stable order
    return "ORDER BY " + ", ".join(keys)


def figures_select(plan: QueryPlan) -> str:
    """The SELECT over ``figures`` for a plan the model is not asked about. When
    some metric is asked for over time, `figures` carries each row's
    `derivation` ("growth", ...) and it is passed through."""
    over_time = _carries_derivation(plan_cells(plan))
    derivation = "derivation" if over_time else "NULL::text AS derivation"
    return _select(derivation, statement_limit(plan), _order_by(plan))


def needs_the_model(plan: QueryPlan) -> bool:
    """Whether anything is left for the model once ``figures`` is written.

    A ranking is not: its direction is in the plan (``ResultSpec.rank``), so
    Python writes the ORDER BY. A derivation is, unless it is exactly what an
    over-time metric already computed -- "Tesla's year-over-year revenue
    growth" is `derive`, and `figures` holds the growth itself; asking the
    model to derive again would compute a growth of the growth.
    """
    return plan.intent == "derive" and not plan.over_time


_FIGURES_JOB_DERIVE = """This question asks for a value COMPUTED from the figures -- a change, a
share of a total, a growth rate. Compute it from `f.value` with a window
function over `figures`, alias the result `value`, set `unit` to what the
result is ('pure' for a ratio or a growth rate, the figures' own unit for a
difference) and `derivation` to a short name for what you computed.
Keep every row: the first period of a series has nothing before it, so its
result is NULL, which is expected. No LIMIT 1."""

#: Filled in per plan: ``{limit}`` is ``statement_limit``.
_FIGURES_EXAMPLE_DERIVE = f"""WORKED EXAMPLE (invented name -- copy the FORM)

  SELECT f.element_id, f.company_cik, f.ticker, f.entity_name,
         f.fiscal_year, f.fiscal_period, f.period_start, f.period_end, f.is_instant,
         f.value - LAG(f.value) OVER (
           PARTITION BY f.element_id, f.company_cik ORDER BY f.period_end) AS value,
         f.unit, 'change_from_prior' AS derivation
  FROM {FIGURES_NAME} f
  ORDER BY value ASC
  LIMIT {{limit}}"""


def _figures_prompt(plan: QueryPlan) -> str:
    """The prompt for a derivation over ``figures``.

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
    # Only a derivation reaches here, and never over an over-time metric
    # (`needs_the_model`), so `figures` carries no `derivation` column.
    job = _FIGURES_JOB_DERIVE
    limit = statement_limit(plan)
    example = _FIGURES_EXAMPLE_DERIVE.format(limit=limit)
    signature = "unit)"
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
          fiscal_period, period_start, period_end, is_instant, value, {signature}

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
2. End with `LIMIT {limit}`.
3. Give every projected column an explicit alias unless it is a bare column
   reference. `NULL::text` without an alias is a column named "text".
4. Never write a number, a date or a company name into the SQL as a literal.
5. Always project `unit`: `f.unit` for figures, or for a sum, average or
   difference of figures in one unit; 'pure' for a ratio or a growth rate.
   Measured: an average left it out and the statement was refused.
6. `{FIGURES_NAME}` is already the complete row selection. Add no WHERE of your
   own -- no year, no company -- unless the question's own condition needs one.

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
    """The prompt for the one thing left to the model: a derivation over
    ``figures``.

    Every value the answer reads is written in Python (``emit_cte``,
    ``emit_figures``), so the model sees no relation, no coordinate and no
    operand -- only computed values. A plan `figures` cannot hold is refused,
    never handed to the model to fetch or combine itself.

    Raises ``UnsupportedPlan`` for that, and when the prompt would not fit the
    model's context window (``_refuse_if_too_long``).
    """
    if not uses_figures(plan):
        raise UnsupportedPlan(
            "a metric's values could not be written in Python (one element with "
            "more than one expression or unit), and they are never handed to the model"
        )
    prompt = _figures_prompt(plan)
    _refuse_if_too_long(prompt, plan_cells(plan))
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
    independent (docs/GAPS.md D1.1), so every one of those windows was twelve months
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
