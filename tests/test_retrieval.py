"""app/retrieval/prompt.py, generator.py and executor.py.

Everything here is pure except the last section, which runs a generated
statement against the test database. Nothing calls Qwen: a test that needs a
language model to agree with it is not a test.

The invariant worth stating plainly: **`base_query(plan)` must always survive
`validate(base_query(plan))`**. The prompt hands that query to the model as
the thing to build on, so if it could not run, every generated statement would
inherit the fault.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.retrieval import (
    GenerationError,
    UnsupportedPlan,
    base_query,
    build_prompt,
    plan_cells,
    validate,
)
from app.retrieval.generator import extract_sql
from app.schemas.query import (
    Binding,
    ComponentCoverage,
    ConceptRef,
    Coverage,
    Note,
    PeriodRef,
    PeriodResidual,
    PlanFilters,
    QueryPlan,
    ResolvedPeriod,
    ResultSpec,
)
from app.schemas.result import RESULT_COLUMNS

APPLE = 320193
NVIDIA = 1045810


def _concept(concept_id: int = 252, name: str = "Revenues") -> ConceptRef:
    return ConceptRef(concept_id=concept_id, taxonomy="us-gaap", name=name)


def _binding(**overrides) -> Binding:
    kwargs = {
        "element_id": "e1",
        "company_cik": APPLE,
        "concepts": [_concept()],
        "unit": "USD",
        "is_instant": False,
        "coverage": Coverage(fact_count=5),
        "confidence": 0.9,
        "resolved_by": "alias",
        "rationale": "because",
    }
    return Binding(**(kwargs | overrides))


def _annual(cik: int = APPLE, year: int = 2024) -> ResolvedPeriod:
    return ResolvedPeriod(
        company_cik=cik,
        fiscal_year=year,
        fiscal_period="FY",
        period_start=date(year - 1, 10, 1),
        period_end=date(year, 9, 28),
    )


def _q4(cik: int = APPLE, year: int = 2024) -> ResolvedPeriod:
    return ResolvedPeriod(
        company_cik=cik,
        fiscal_year=year,
        fiscal_period="Q4",
        period_start=date(year, 6, 30),
        period_end=date(year, 9, 28),
        residual_of=PeriodResidual(
            shared_start=date(year - 1, 10, 1),
            whole_end=date(year, 9, 28),
            subtract_end=date(year, 6, 29),
        ),
    )


def _plan(bindings, periods, *, intent: str = "lookup", **spec) -> QueryPlan:
    counts = {"companies": 1, "periods": 1, "metrics": 1, "shape": "scalar"} | spec
    return QueryPlan(
        question="q",
        intent=intent,
        result=ResultSpec(**counts),
        filters=PlanFilters(periods=periods),
        bindings=bindings,
    )


# --------------------------------------------------------------------------- #
# plan_cells -- the row-count promise
# --------------------------------------------------------------------------- #


def test_cells_are_the_cross_product_of_bindings_and_their_periods() -> None:
    plan = _plan(
        [_binding(company_cik=APPLE), _binding(company_cik=NVIDIA, concepts=[_concept(254)])],
        [_annual(APPLE, 2023), _annual(APPLE, 2024), _annual(NVIDIA, 2023), _annual(NVIDIA, 2024)],
    )
    cells = plan_cells(plan)
    assert len(cells) == 4
    assert {(c.company_cik, c.concept_id) for c in cells} == {(APPLE, 252), (NVIDIA, 254)}


def test_a_binding_narrowed_to_periods_only_claims_those() -> None:
    """Concept drift: one company, two bindings, each answering for its own
    slice of the range."""
    plan = _plan(
        [
            _binding(periods=[PeriodRef(fiscal_year=2023, fiscal_period="FY")]),
            _binding(
                concepts=[_concept(254)],
                periods=[PeriodRef(fiscal_year=2024, fiscal_period="FY")],
            ),
        ],
        [_annual(APPLE, 2023), _annual(APPLE, 2024)],
    )
    cells = plan_cells(plan)
    assert len(cells) == 2
    assert {(c.fiscal_year, c.concept_id) for c in cells} == {(2023, 252), (2024, 254)}


def test_a_residual_cell_carries_the_windows_to_subtract() -> None:
    plan = _plan(
        [
            _binding(
                period_rule="residual",
                coverage=Coverage(
                    fact_count=2,
                    components=[
                        ComponentCoverage(
                            period_start=date(2023, 10, 1),
                            period_end=date(2024, 9, 28),
                            fact_count=1,
                        ),
                        ComponentCoverage(
                            period_start=date(2023, 10, 1),
                            period_end=date(2024, 6, 29),
                            fact_count=1,
                        ),
                    ],
                ),
            )
        ],
        [_q4()],
    )
    (cell,) = plan_cells(plan)
    assert cell.period_start == date(2023, 10, 1)  # the shared start, not the Q4 start
    assert cell.period_end == date(2024, 9, 28)
    assert cell.subtract_end == date(2024, 6, 29)


def test_a_multi_operand_binding_is_refused_for_the_arithmetic_not_the_unit() -> None:
    """`operand_unit` closed the unit half of this. What is still missing is
    rendering the expression over one row per operand."""
    plan = _plan(
        [
            _binding(
                concepts=[_concept(1), _concept(2)],
                expression="c0 / c1",
                unit="pure",
                operand_unit="USD",
            )
        ],
        [_annual()],
    )
    with pytest.raises(UnsupportedPlan, match="rendering the arithmetic"):
        plan_cells(plan)


def test_a_cell_joins_on_the_operand_unit_not_the_result_unit() -> None:
    """A ratio's result is `pure`; its facts are USD. Keying the join on
    `pure` would find nothing at all."""
    plan = _plan([_binding(unit="USD")], [_annual()])
    (cell,) = plan_cells(plan)
    assert cell.unit == "USD"


def test_a_plan_binding_nothing_is_refused() -> None:
    with pytest.raises(UnsupportedPlan, match="binds nothing"):
        plan_cells(_plan([], [_annual()]))


# --------------------------------------------------------------------------- #
# base_query -- must always be runnable
# --------------------------------------------------------------------------- #


def test_the_base_query_always_passes_validation() -> None:
    """The prompt hands this to the model as the thing to build on. If it did
    not validate, every generated statement would inherit the fault."""
    plan = _plan(
        [_binding(company_cik=APPLE), _binding(company_cik=NVIDIA, concepts=[_concept(254)])],
        [_annual(APPLE, 2024), _annual(NVIDIA, 2024)],
    )
    assert validate(base_query(plan))


def test_a_residual_base_query_also_validates() -> None:
    plan = _plan(
        [
            _binding(
                period_rule="residual",
                coverage=Coverage(
                    fact_count=2,
                    components=[
                        ComponentCoverage(
                            period_start=date(2023, 10, 1),
                            period_end=date(2024, 9, 28),
                            fact_count=1,
                        ),
                        ComponentCoverage(
                            period_start=date(2023, 10, 1),
                            period_end=date(2024, 6, 29),
                            fact_count=1,
                        ),
                    ],
                ),
            )
        ],
        [_q4()],
    )
    assert validate(base_query(plan))


def test_a_null_subtract_end_is_cast_to_date() -> None:
    """An all-NULL VALUES column is typed `text`, and `date = text` fails at
    *execution* time -- libpg_query parses, it does not type-check, so
    validate() cannot catch this."""
    sql = base_query(_plan([_binding()], [_annual()]))
    assert "NULL::date" in sql
    assert ", NULL," not in sql


def test_the_base_query_projects_exactly_the_contract() -> None:
    sql = base_query(_plan([_binding()], [_annual()]))
    for column in RESULT_COLUMNS:
        assert column in sql


def test_period_start_comes_from_the_view_not_the_plan() -> None:
    """An instant fact has no start. Projecting the plan's date would give a
    balance-sheet figure a span it does not have."""
    sql = base_query(_plan([_binding()], [_annual()]))
    assert "v.period_start," in sql
    assert "p.period_start," not in sql


def test_a_missing_subtrahend_drops_the_row_rather_than_returning_the_year() -> None:
    sql = base_query(
        _plan(
            [
                _binding(
                    period_rule="residual",
                    coverage=Coverage(
                        fact_count=2,
                        components=[
                            ComponentCoverage(
                                period_start=date(2023, 10, 1),
                                period_end=date(2024, 9, 28),
                                fact_count=1,
                            ),
                            ComponentCoverage(
                                period_start=date(2023, 10, 1),
                                period_end=date(2024, 6, 29),
                                fact_count=1,
                            ),
                        ],
                    ),
                )
            ],
            [_q4()],
        )
    )
    assert "WHERE p.subtract_end IS NULL OR s.value IS NOT NULL" in sql


# --------------------------------------------------------------------------- #
# build_prompt
# --------------------------------------------------------------------------- #


def test_a_deriving_intent_is_not_offered_the_easy_option() -> None:
    """Measured with qwen2.5-coder:7b: given a complete correct query *and*
    permission to return it unchanged, it returns it unchanged even for a
    ranking question. So the branch is removed rather than argued with."""
    bindings, periods = [_binding()], [_annual()]
    assert "unchanged" in build_prompt(_plan(bindings, periods, intent="lookup"))
    ranked = build_prompt(_plan(bindings, periods, intent="rank"))
    assert "is NOT the answer" in ranked
    assert "Reply with the query above, unchanged" not in ranked


def test_the_prompt_states_the_row_count_the_answer_needs() -> None:
    plan = _plan(
        [_binding(company_cik=APPLE), _binding(company_cik=NVIDIA)],
        [_annual(APPLE, 2024), _annual(NVIDIA, 2024)],
        companies=2,
        shape="table",
    )
    assert "2 row(s) of" in build_prompt(plan)


def test_plan_notes_reach_the_prompt() -> None:
    note = Note(kind="concept_switch", message="tag changed")
    plan = _plan([_binding(notes=[note])], [_annual()])
    assert "tag changed" in build_prompt(plan)


# --------------------------------------------------------------------------- #
# extract_sql -- unwrapping the model's answer
# --------------------------------------------------------------------------- #


def test_a_fenced_answer_is_unwrapped() -> None:
    assert extract_sql("Here you go:\n```sql\nSELECT 1 AS a\n```\n") == "SELECT 1 AS a"


def test_an_unlabelled_fence_works_too() -> None:
    assert extract_sql("```\nSELECT 1 AS a\n```") == "SELECT 1 AS a"


def test_the_last_statement_block_wins() -> None:
    """A model that narrates quotes the question's fragments first and puts
    its answer last."""
    reply = "As shown:\n```sql\nSELECT 0 AS old\n```\nBetter:\n```sql\nSELECT 1 AS new\n```"
    assert extract_sql(reply) == "SELECT 1 AS new"


def test_a_non_sql_fence_is_skipped() -> None:
    reply = "```\njust some notes\n```\n```sql\nWITH x AS (SELECT 1) SELECT * FROM x\n```"
    assert extract_sql(reply).startswith("WITH x")


def test_a_bare_answer_is_taken_from_the_first_keyword() -> None:
    assert extract_sql("Sure. SELECT 1 AS a").strip() == "SELECT 1 AS a"


def test_a_reply_with_no_statement_raises() -> None:
    with pytest.raises(GenerationError, match="no SELECT"):
        extract_sql("I cannot answer that.")


# --------------------------------------------------------------------------- #
# Against the real database
# --------------------------------------------------------------------------- #


async def test_a_base_query_runs_and_returns_the_contract(test_session_factory) -> None:
    """Empty result, but the column names and types are the point -- this is
    what catches a VALUES column typed `text` that validate() cannot see."""
    from sqlalchemy import text

    plan = _plan(
        [_binding(company_cik=APPLE), _binding(company_cik=NVIDIA, concepts=[_concept(254)])],
        [_annual(APPLE, 2024), _annual(NVIDIA, 2024)],
    )
    async with test_session_factory() as session:
        result = await session.execute(text(validate(base_query(plan))))
        assert tuple(result.keys()) == RESULT_COLUMNS


async def test_a_residual_base_query_runs(test_session_factory) -> None:
    from sqlalchemy import text

    plan = _plan(
        [
            _binding(
                period_rule="residual",
                coverage=Coverage(
                    fact_count=2,
                    components=[
                        ComponentCoverage(
                            period_start=date(2023, 10, 1),
                            period_end=date(2024, 9, 28),
                            fact_count=1,
                        ),
                        ComponentCoverage(
                            period_start=date(2023, 10, 1),
                            period_end=date(2024, 6, 29),
                            fact_count=1,
                        ),
                    ],
                ),
            )
        ],
        [_q4()],
    )
    async with test_session_factory() as session:
        result = await session.execute(text(validate(base_query(plan))))
        assert tuple(result.keys()) == RESULT_COLUMNS
