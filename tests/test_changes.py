"""app/retrieval/changes.py -- the change from each period to the next.

All pure. The rows are built the way ``execute()`` builds them -- attributed
through the plan and judged by the same verdict -- so what is tested is the
step that runs after it, not a stand-in for the executor.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.retrieval import add_period_changes, build_prompt, executor
from app.retrieval.changes import DERIVATION
from app.schemas.query import (
    Binding,
    ConceptRef,
    Coverage,
    Note,
    PeriodRef,
    PlanFilters,
    QueryPlan,
    ResolvedPeriod,
    ResultSpec,
)
from app.schemas.result import AnnotatedRow, ResultRow, ResultSet

NVIDIA = 1045810

#: NVIDIA's filed revenue, FY2021-FY2025, as the view returns it.
REVENUE = {
    2021: Decimal("16675000000"),
    2022: Decimal("26914000000"),
    2023: Decimal("26974000000"),
    2024: Decimal("60922000000"),
    2025: Decimal("130497000000"),
}


def _period(year: int, fiscal_period: str = "FY") -> ResolvedPeriod:
    if fiscal_period == "FY":
        return ResolvedPeriod(
            company_cik=NVIDIA, fiscal_year=year, fiscal_period="FY",
            period_start=date(year - 1, 2, 1), period_end=date(year, 1, 30),
        )
    quarter = int(fiscal_period[1])
    start = date(year - 1, 2 + 3 * (quarter - 1), 1)
    end = date(year - 1, 4 + 3 * (quarter - 1), 28)
    return ResolvedPeriod(
        company_cik=NVIDIA, fiscal_year=year, fiscal_period=fiscal_period,
        period_start=start, period_end=end,
    )


def _binding(years=(), concept_id: int = 254, **overrides) -> Binding:
    kwargs = {
        "element_id": "e2",
        "company_cik": NVIDIA,
        "periods": [PeriodRef(fiscal_year=y, fiscal_period="FY") for y in years],
        "concepts": [ConceptRef(concept_id=concept_id, taxonomy="us-gaap", name="Revenues")],
        "unit": "USD",
        "is_instant": False,
        "coverage": Coverage(fact_count=5),
        "confidence": 1.0,
        "resolved_by": "alias",
        "rationale": "because",
    }
    return Binding(**(kwargs | overrides))


def _plan(bindings, periods, *, intent="trend", axes=("period",)) -> QueryPlan:
    return QueryPlan(
        question="How fast has NVIDIA's revenue grown over the past five years?",
        intent=intent,
        result=ResultSpec(
            shape="series" if axes else "scalar", axes=list(axes),
            companies=1, periods=len(periods), metrics=1,
        ),
        filters=PlanFilters(ciks=[NVIDIA], periods=periods),
        bindings=bindings,
    )


def _result(plan: QueryPlan, values: dict[tuple[int, str], Decimal]) -> ResultSet:
    """What ``execute()`` would return for these filed values."""
    rows = []
    for period in plan.filters.periods:
        key = (period.fiscal_year, period.fiscal_period)
        if key not in values:
            continue
        row = ResultRow(
            element_id="e2", company_cik=NVIDIA, ticker="NVDA", entity_name="NVIDIA CORP",
            fiscal_year=period.fiscal_year, fiscal_period=period.fiscal_period,
            period_start=period.period_start, period_end=period.period_end,
            is_instant=False, value=values[key], unit="USD",
        )
        rows.append(AnnotatedRow(row=row, binding_keys=executor._attribute(row, plan)))
    verdict = executor._verdict(rows, plan)
    return ResultSet(
        question=plan.question, rows=rows, verdict=verdict,
        citations=executor._citations(plan), notes=executor._notes(plan, verdict),
    )


def _five_years() -> tuple[QueryPlan, ResultSet]:
    plan = _plan([_binding()], [_period(y) for y in REVENUE])
    return plan, _result(plan, {(y, "FY"): v for y, v in REVENUE.items()})


def _changes(result: ResultSet) -> list[AnnotatedRow]:
    return [a for a in result.rows if a.row.derivation == DERIVATION]


def test_every_adjacent_pair_gets_its_change() -> None:
    """q010's answer: the five figures, and the change into each of the last four."""
    plan, filed = _five_years()
    result = add_period_changes(filed, plan)

    assert result.rows[:5] == filed.rows, "the filed figures are kept, unchanged"
    changes = _changes(result)
    assert [a.row.fiscal_year for a in changes] == [2022, 2023, 2024, 2025]
    assert [round(a.row.value, 4) for a in changes] == [
        Decimal("0.6140"), Decimal("0.0022"), Decimal("1.2585"), Decimal("1.1420"),
    ]
    assert {a.row.unit for a in changes} == {"pure"}
    assert result.is_answerable
    assert result.verdict.status == "complete"
    assert result.verdict.expected_rows == result.verdict.returned_rows == 9


def test_a_step_across_a_tag_change_cites_both_bindings() -> None:
    """NVIDIA moved revenue tags between FY2022 and FY2023. The step between
    them is computed from two concepts, and says so."""
    plan = _plan(
        [_binding((2021, 2022), concept_id=252), _binding((2023, 2024, 2025))],
        [_period(y) for y in REVENUE],
    )
    result = add_period_changes(_result(plan, {(y, "FY"): v for y, v in REVENUE.items()}), plan)

    by_year = {a.row.fiscal_year: a.binding_keys for a in _changes(result)}
    assert by_year[2022] == ["b0"]
    assert by_year[2023] == ["b0", "b1"]
    assert by_year[2024] == ["b1"]


def test_nothing_is_computed_across_a_missing_figure() -> None:
    """A disclosed gap leaves both of its steps empty -- never a two-year step
    presented as one."""
    note = Note(kind="partial_coverage", message="FY2022 is absent")
    plan = _plan([_binding(notes=[note])], [_period(y) for y in (2021, 2022, 2023)])
    filed = _result(plan, {(2021, "FY"): REVENUE[2021], (2023, "FY"): REVENUE[2023]})
    assert filed.is_answerable, "the gap was disclosed"

    assert _changes(add_period_changes(filed, plan)) == []


def test_no_percentage_from_a_zero_or_negative_figure() -> None:
    plan = _plan([_binding()], [_period(y) for y in (2021, 2022, 2023)])
    filed = _result(
        plan,
        {(2021, "FY"): Decimal("-5"), (2022, "FY"): Decimal("10"), (2023, "FY"): Decimal("15")},
    )
    result = add_period_changes(filed, plan)

    assert [a.row.fiscal_year for a in _changes(result)] == [2023]
    assert any("zero or negative" in n.message for n in result.notes)


def test_annual_and_quarterly_are_never_compared() -> None:
    periods = [_period(2024), _period(2025, "Q1"), _period(2025, "Q2"), _period(2025)]
    plan = _plan([_binding()], periods)
    values = {(p.fiscal_year, p.fiscal_period): Decimal(100 + i) for i, p in enumerate(periods)}
    result = add_period_changes(_result(plan, values), plan)

    steps = {(a.row.fiscal_year, a.row.fiscal_period) for a in _changes(result)}
    assert steps == {(2025, "FY"), (2025, "Q2")}


def test_a_refused_result_is_left_alone() -> None:
    """Nothing is built on rows the verdict would not stand behind."""
    plan = _plan([_binding()], [_period(y) for y in (2021, 2022, 2023)])
    filed = _result(plan, {(2021, "FY"): REVENUE[2021], (2023, "FY"): REVENUE[2023]})
    assert not filed.is_answerable, "an undisclosed gap"
    assert add_period_changes(filed, plan) is filed


def test_only_a_period_series_of_plain_figures_qualifies() -> None:
    _, filed = _five_years()
    periods = [_period(y) for y in REVENUE]
    for plan in (
        _plan([_binding()], periods, axes=()),
        _plan([_binding()], periods, intent="rank"),
        _plan([_binding()], periods, intent="derive"),
        _plan([_binding(unit="pure")], periods),
    ):
        assert add_period_changes(filed, plan) is filed


def test_the_model_is_asked_for_the_figures_only() -> None:
    """q010: offered a choice, the model computed the growth itself and put it
    in `derivation`. With the change computed afterwards there is no choice."""
    plan, _ = _five_years()
    text = build_prompt(plan)
    assert "computed AFTER your statement runs" in text
    assert "Decide which of these two" not in text
    assert "computed AFTER" not in build_prompt(_plan([_binding()], [_period(2025)], axes=()))
