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
from decimal import Decimal

import httpx
import pytest

from app.retrieval import (
    GenerationError,
    UnsupportedPlan,
    build_prompt,
    executor,
    generate,
    plan_cells,
    prompt as prompt_module,
)
from app.retrieval.generator import MAX_OUTPUT_TOKENS, REQUEST_TIMEOUT, extract_sql
from app.schemas.query import (
    Binding,
    ConceptRef,
    Coverage,
    Note,
    PeriodRef,
    PlanFilters,
    PlanThreshold,
    QueryPlan,
    ResolvedPeriod,
    ResultSpec,
)
from app.schemas.result import AnnotatedRow, Citation, ResultRow, ResultSet

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


def _cell(**overrides) -> prompt_module.PlanCell:
    kwargs = {
        "element_id": "e1",
        "company_cik": APPLE,
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_start": date(2023, 10, 1),
        "period_end": date(2024, 9, 28),
        "concept_ids": (77,),
        "expression": "c0",
        "unit": "USD",
        "result_unit": "USD",
        "is_instant": False,
        "binding_index": 0,
    }
    return prompt_module.PlanCell(**(kwargs | overrides))


def test_the_emitted_cte_has_one_value_per_declared_column() -> None:
    """The bug this catches cost a live failure, before the CTE was emitted here.

    Shown a nine-column CTE declaration alongside a ten-column operand table,
    the model wrote ten values per row against nine names -- `unit` received a
    concept id and `is_instant` received 'USD', failing as `boolean = text`.
    Emitting both sides from one constant makes that unrepresentable, and this
    pins it for the operand shape as well as the plain one.
    """
    for cells in (
        [_cell(concept_ids=(77,))],
        [_cell(concept_ids=(77, 88), expression="c0 / c1")],
    ):
        cte = prompt_module.emit_cte(cells)
        declared = cte[cte.index("(") + 1 : cte.index(") AS (")].split(", ")
        for line in cte.splitlines():
            line = line.strip().rstrip(",")
            if not line.startswith("("):
                continue
            values = line[1:-1].split(", ")
            assert len(values) == len(declared), (len(values), len(declared), line)


def test_the_emitted_cte_copies_the_plan_s_dates_verbatim() -> None:
    """The reason this function exists.

    Measured 2026-09-24: asked to transcribe 378 coordinate rows, the model
    wrote 40 and computed their windows from the fiscal-year label -- Oracle's
    FY2021 Q1 came back as 2021-06-01 where the plan says 2020-06-01, because
    Oracle's year ends in May. Emitted here, a window cannot be anything but
    what the plan says.
    """
    cells = [_cell(fiscal_year=2021, period_start=date(2020, 6, 1),
                   period_end=date(2020, 8, 31))]
    cte = prompt_module.emit_cte(cells)
    assert "DATE '2020-06-01'" in cte and "DATE '2020-08-31'" in cte
    assert "2021-06-01" not in cte


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


# --------------------------------------------------------------------------- #
# The generator's output ceiling
# --------------------------------------------------------------------------- #
#
# Added after q015 of the eval set ran for 42 minutes and was killed. These
# stub the client, not the model: what is under test is the branch `generate`
# takes on what came back.


class _FakeGenClient:
    def __init__(self, response=None, raises=None):
        self.response = response or {}
        self.raises = raises
        self.options: dict | None = None
        self.init_kwargs: dict = {}

    def __call__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        return self

    async def generate(self, **kwargs):
        self.options = kwargs.get("options")
        if self.raises:
            raise self.raises
        return dict(self.response)


def _one_cell_plan() -> QueryPlan:
    return _plan([_binding(company_cik=APPLE)], [_annual(APPLE, 2024)])


async def test_the_sql_ceiling_is_actually_sent(monkeypatch):
    """The regression that would be invisible: a cap nobody passes.

    Dropping `num_predict` leaves every other test green and restores the
    42-minute question.
    """
    client = _FakeGenClient({"response": "SELECT 1", "done_reason": "stop"})
    monkeypatch.setattr("app.retrieval.generator.AsyncClient", client)
    await generate(_one_cell_plan())
    assert client.options["num_predict"] == MAX_OUTPUT_TOKENS
    assert client.init_kwargs["timeout"] == REQUEST_TIMEOUT


async def test_a_truncated_statement_never_reaches_the_validator(monkeypatch):
    """Half a SELECT can parse and can run.

    A statement that lost its last join returns rows that look like an answer,
    which is the failure this project exists to prevent -- so truncation is
    refused here rather than handed on to be judged on its merits.
    """
    client = _FakeGenClient(
        {"response": "SELECT a, b FROM xbrl.reported_fact WHERE", "done_reason": "length"}
    )
    monkeypatch.setattr("app.retrieval.generator.AsyncClient", client)
    with pytest.raises(GenerationError, match="ceiling"):
        await generate(_one_cell_plan())


async def test_a_slow_model_is_reported_not_left_hanging(monkeypatch):
    """`GenerationError`, the channel that already means the machinery failed.

    `answer()` lets it propagate, so [B] refuses and the eval runner records
    it as `error` with this message -- no new channel needed.
    """
    client = _FakeGenClient(raises=httpx.ReadTimeout("too slow"))
    monkeypatch.setattr("app.retrieval.generator.AsyncClient", client)
    with pytest.raises(GenerationError, match="did not finish"):
        await generate(_one_cell_plan())


def test_a_prompt_too_long_for_the_context_window_is_refused() -> None:
    """A backstop, not a live limit any more.

    It was added when the model had to transcribe the coordinates: 378 cells
    rendered to 53,114 characters against an 8,192-token window, Ollama
    truncated it silently, and the model answered from the part it saw --
    computing the windows it could not read from the fiscal-year label. Oracle's
    FY ends in May, so every one was twelve months out, attributable, plausible
    and in the right unit.

    ``emit_cte`` removed the cause, so `build_prompt` no longer grows with the
    plan and cannot reach this on coordinate count alone -- see the test below.
    The check stays for whatever else can grow without bound: a very long
    question, or a plan carrying many caveats.
    """
    with pytest.raises(UnsupportedPlan, match="truncate it silently"):
        prompt_module._refuse_if_too_long("x" * (prompt_module.MAX_PROMPT_CHARS + 1), [])


def test_the_prompt_no_longer_grows_with_the_plan() -> None:
    """The measurable half of moving the CTE out of the model's hands.

    One cell and a hundred cells now produce the same prompt, because the
    coordinates are not in it. Before, 378 cells was 53,114 characters and
    three times the context window.
    """
    one = build_prompt(_plan([_binding()], [_annual()]))
    many = [_annual(APPLE, year) for year in range(2000, 2100)]
    hundred = build_prompt(_plan([_binding()], many))
    assert len(hundred) - len(one) < 200, (len(one), len(hundred))
    assert "DATE '" not in hundred, "no coordinate should reach the prompt"


def test_the_char_budget_tracks_the_generator_s_context_window() -> None:
    """Two copies of 8,192, so a test rather than a shared constants module --
    `generator` imports this module, so importing back would be a cycle."""
    from app.retrieval import generator

    assert prompt_module._CONTEXT_TOKENS == generator.CONTEXT_TOKENS
    assert prompt_module.MAX_PROMPT_CHARS == int(
        generator.CONTEXT_TOKENS * prompt_module.CHARS_PER_TOKEN
    )


# --------------------------------------------------------------------------- #
# Thresholds -- checked, not trusted
# --------------------------------------------------------------------------- #


def _citation() -> Citation:
    return Citation(
        binding_key="b0",
        element_id="e1",
        concepts=[_concept()],
        expression="c0",
        unit="USD",
        resolved_by="alias",
        confidence=1.0,
        rationale="because",
    )


def _threshold(value: str = "100", comparison: str = "gt") -> PlanThreshold:
    return PlanThreshold(
        element_id="e1",
        element_text="more than a hundred",
        comparison=comparison,
        value=Decimal(value),
    )


def _annotated(value: str | None, element_id: str = "e1") -> AnnotatedRow:
    return AnnotatedRow(
        row=ResultRow(
            element_id=element_id,
            company_cik=APPLE,
            fiscal_year=2024,
            fiscal_period="FY",
            period_start=date(2023, 10, 1),
            period_end=date(2024, 9, 28),
            is_instant=False,
            value=None if value is None else Decimal(value),
            unit="USD",
        ),
        binding_keys=["b0"],
    )


def test_a_row_below_the_threshold_is_unattributable() -> None:
    """The failure this exists for.

    A model that drops the comparison returns every row -- attributable, right
    unit, plausible -- and the reader who asked for companies above $100B is
    handed all twenty with nothing saying the question was widened. Because the
    plan holds the number, the predicate is simply re-applied to what came back.
    """
    plan = _plan([_binding()], [_annual()]).model_copy(
        update={"thresholds": [_threshold()]}
    )
    rows = [_annotated("101"), _annotated("99")]
    assert executor._threshold_violations(rows, plan) == [1]


def test_a_threshold_only_judges_its_own_metric() -> None:
    """A question with two metrics and a bar on one leaves the other alone."""
    plan = _plan([_binding()], [_annual()]).model_copy(
        update={"thresholds": [_threshold()]}
    )
    rows = [_annotated("1", element_id="e2")]
    assert executor._threshold_violations(rows, plan) == []


def test_a_plan_with_no_threshold_checks_nothing() -> None:
    plan = _plan([_binding()], [_annual()])
    assert executor._threshold_violations([_annotated("1")], plan) == []


def test_a_threshold_makes_fewer_rows_correct_rather_than_a_shortfall() -> None:
    """The one narrowing that is *meant* to return less than the grid holds.

    Twenty companies resolve and eleven clear the bar; the other nine are
    correctly absent, so neither the row-count equality nor the per-company
    coverage check applies. Measured on q039, which returns 11 of 20 `complete`.
    """
    plan = _plan(
        [_binding(company_cik=APPLE), _binding(company_cik=NVIDIA)],
        [_annual(APPLE, 2024), _annual(NVIDIA, 2024)],
        companies=2,
    ).model_copy(update={"thresholds": [_threshold()]})

    verdict = executor._verdict([_annotated("101")], plan)
    assert verdict.status == "complete"
    assert verdict.returned_rows == 1 and verdict.expected_rows == 2
    assert verdict.missing == []
    assert ResultSet(
        question="q", rows=[_annotated("101")], verdict=verdict,
        citations={"b0": _citation()},
    ).is_answerable


def test_the_prompt_states_the_comparison_when_the_plan_carries_one() -> None:
    """Stated apart from the row-count promise, because the two would otherwise
    contradict each other -- which is the shape of §4.3c."""
    plan = _plan([_binding()], [_annual()]).model_copy(
        update={"thresholds": [_threshold()]}
    )
    text = build_prompt(plan)
    assert "value > 100" in text
    assert "FEWER rows than the count above is correct" in text
    assert "more than a hundred" in text


def test_the_prompt_says_nothing_about_thresholds_when_there_are_none() -> None:
    assert "FEWER rows" not in build_prompt(_plan([_binding()], [_annual()]))
