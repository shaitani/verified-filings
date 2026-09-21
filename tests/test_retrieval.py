"""app/retrieval/prompt.py and generator.py.

All pure. Nothing here calls Qwen: a test that needs a language model to agree
with it is not a test.

`build_prompt` writes no SQL -- it assembles the coordinates the model needs
and the model writes the statement -- so what is checked here is that the
coordinates are right and that the prompt says what the data actually holds.
The literal `is_instant` renders as `true`/`false` rather than `yes`/`no`
because the model copies what it is shown, and `yes` produced
`is_instant = 'no'` against a boolean column.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.retrieval import (
    GenerationError,
    UnsupportedPlan,
    build_prompt,
    plan_cells,
)
from app.retrieval.generator import extract_sql
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
    """An ordinary quarter. No filer reports a Q4, but the view synthesizes
    one, so nothing here treats it specially any more."""
    return ResolvedPeriod(
        company_cik=cik,
        fiscal_year=year,
        fiscal_period="Q4",
        period_start=date(year, 6, 30),
        period_end=date(year, 9, 28),
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
    assert {(c.company_cik, c.concept_ids) for c in cells} == {
        (APPLE, (252,)),
        (NVIDIA, (254,)),
    }


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
    assert {(c.fiscal_year, c.concept_ids) for c in cells} == {
        (2023, (252,)),
        (2024, (254,)),
    }


def test_a_q4_cell_is_an_ordinary_cell() -> None:
    """The view computes the fourth quarter (migration a8b5b820cf1a), so the
    plan names one window like any other. It used to carry the two windows to
    subtract, and the SQL-writing model would not subtract them."""
    plan = _plan([_binding()], [_q4()])
    (cell,) = plan_cells(plan)
    assert cell.fiscal_period == "Q4"
    assert cell.period_start == date(2024, 6, 30)
    assert cell.period_end == date(2024, 9, 28)


def test_a_ratio_is_one_cell_with_two_operands() -> None:
    """One row of the answer, not two. `gross_margin` reads two facts and
    reports one number, so the row count the verdict checks stays at the
    answer's grain while the filter table renders a line per operand."""
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
    (cell,) = plan_cells(plan)
    assert cell.operands == 2
    assert cell.concept_ids == (1, 2)
    assert cell.expression == "c0 / c1"
    assert cell.unit == "USD", "the join keys on the operands' unit"
    assert cell.result_unit == "pure", "the answer is dimensionless"


def test_the_filter_table_gains_an_operand_column_only_when_needed() -> None:
    """41 of 47 curated metrics are a single concept, and every sentence in
    the prompt is one the model can act on when it should not."""
    ratio = _plan(
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
    plain = _plan([_binding()], [_annual()])
    assert "operand" in build_prompt(ratio)
    assert "SOME ROWS COMBINE" in build_prompt(ratio)
    assert "operand" not in build_prompt(plain)
    assert "SOME ROWS COMBINE" not in build_prompt(plain)


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


def test_each_worked_example_declares_as_many_columns_as_its_table() -> None:
    """The bug this catches cost a live failure. The plain example's CTE names
    nine columns; handed to a model alongside an operand filter table of ten,
    it produced ten values per row against those nine names -- so `unit`
    received a concept id and `is_instant` received 'USD', failing as
    `boolean = text`. Each example now matches the table it is shown with.
    """
    from app.retrieval.prompt import (
        _EXAMPLE_COMBINING,
        _EXAMPLE_PLAIN,
        _TABLE_HEADER,
        _TABLE_HEADER_OPERANDS,
    )

    for example, header in (
        (_EXAMPLE_PLAIN, _TABLE_HEADER),
        (_EXAMPLE_COMBINING, _TABLE_HEADER_OPERANDS),
    ):
        table_columns = len(header.split("|"))
        cte = example[example.index("WITH wanted(") : example.index(") AS (")]
        assert len(cte.split(",")) == table_columns, cte


def test_the_combining_example_shows_both_operators() -> None:
    """It used to show only a division, and a `c0 - c1` metric came back
    divided: Apple's FY2024 free cash flow as 12.5 rather than 108.8 billion,
    in the right unit, attributable, verdict `complete`. Same-unit arithmetic
    has no structural check behind it, so the prompt is the whole defence."""
    from app.retrieval.prompt import _EXAMPLE_COMBINING

    assert "Had the expression been `c0 - c1`" in _EXAMPLE_COMBINING
    assert "do not copy the operator from this example" in _EXAMPLE_COMBINING


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
