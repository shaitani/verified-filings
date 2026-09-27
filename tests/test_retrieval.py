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


def test_a_multi_operand_metric_never_reaches_the_model_as_operands() -> None:
    """Metric arithmetic is Python's (``figures``): the model is never shown an
    operand, a combining example or the per-element expression -- only the
    computed value. A single-concept plan still gets the ordinary prompt."""
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
        intent="rank",
    )
    plain = _plan([_binding()], [_annual()])
    text = build_prompt(ratio)
    assert "EVERYTHING YOU NEED IS IN ONE CTE: `figures`" in text
    for leak in ("operand", "SOME ROWS COMBINE", "COMBINED ANSWER", "c0 / c1", "NULLIF"):
        assert leak not in text, leak
    assert "operand" not in build_prompt(plain)


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


def test_every_example_begins_at_select() -> None:
    """The CTE is written in Python now, and rule 1 forbids the model a `WITH`
    of its own. This example still opened with `WITH wanted(...) AS (VALUES` --
    written before that move and missed when the other two examples were
    rewritten -- so a multi-operand plan was shown the one thing its own rules
    forbid. Measured on q024: `max(v.value)` with no FILTER at all.
    """
    from app.retrieval.prompt import _EXAMPLE_DERIVED, _EXAMPLE_PLAIN

    for example in (_EXAMPLE_PLAIN, _EXAMPLE_DERIVED):
        assert "WITH " not in example
        assert "VALUES" not in example


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


def test_a_threshold_is_applied_in_the_statement_not_by_the_model() -> None:
    """The comparison is written into `figures`; a ranking over it is told the
    filter is already applied and must not be repeated."""
    from app.retrieval.prompt import emit_figures

    plan = _plan([_binding()], [_annual()], intent="rank").model_copy(
        update={"thresholds": [_threshold()]}
    )
    figures = emit_figures(plan)
    assert "WHERE (w.element_id <> 'e1' OR (v.value) > 100)" in figures
    text = build_prompt(plan)
    assert "Only rows meeting the question's condition are in `figures`" in text
    assert "more than a hundred" in text and "Do not filter again" in text


def test_the_prompt_says_nothing_about_thresholds_when_there_are_none() -> None:
    assert "FEWER rows" not in build_prompt(_plan([_binding()], [_annual()]))


# --------------------------------------------------------------------------- #
# figures -- metric arithmetic and thresholds, written without the model
# --------------------------------------------------------------------------- #


def _margin_binding(cik: int = APPLE, element_id: str = "e1") -> Binding:
    return _binding(
        element_id=element_id,
        company_cik=cik,
        concepts=[_concept(435, "GrossProfit"), _concept(252, "Revenues")],
        expression="c0 / c1",
        unit="pure",
        operand_unit="USD",
    )


def _margin_plan(intent: str = "compare") -> QueryPlan:
    return _plan(
        [_margin_binding(APPLE), _margin_binding(NVIDIA)],
        [_annual(APPLE), _annual(NVIDIA)],
        intent=intent,
        companies=2,
        shape="series",
        axes=["company"],
    )


def test_a_margin_plan_is_answered_from_figures_without_the_model() -> None:
    """q007: the plan says `c0 / c1` in `pure`, so `figures` says exactly that,
    and a plan that computes nothing above its cells reads it as it is."""
    from app.retrieval.prompt import FIGURES_SELECT, emit_figures, uses_figures
    from app.retrieval.validator import validate

    plan = _margin_plan()
    assert uses_figures(plan)
    figures = emit_figures(plan)
    assert "/ NULLIF(max(v.value) FILTER (WHERE w.operand = 1), 0)" in figures
    assert "THEN 'pure'" in figures and "GROUP BY" in figures
    head = prompt_module.emit_cte(plan_cells(plan)) + "," + chr(10) + figures
    sql = head + chr(10) + FIGURES_SELECT
    assert validate(sql) == sql


def test_a_threshold_on_a_margin_is_a_having_on_the_computed_value() -> None:
    from app.retrieval.prompt import emit_figures

    plan = _margin_plan(intent="lookup").model_copy(
        update={"thresholds": [_threshold("0.4")]}
    )
    figures = emit_figures(plan)
    assert "HAVING (w.element_id <> 'e1' OR (max(v.value) FILTER (WHERE w.operand = 0)" in figures
    assert ") > 0.4)" in figures


def test_figures_is_only_for_arithmetic_or_a_threshold() -> None:
    from app.retrieval.prompt import uses_figures

    assert uses_figures(_margin_plan(intent="rank")), "rank reads figures too"
    assert uses_figures(_margin_plan(intent="derive"))
    assert not uses_figures(_plan([_binding()], [_annual()], intent="rank"))


async def test_a_ranking_over_a_margin_is_written_over_figures(monkeypatch) -> None:
    """The model writes only the ordering; the statement it lands in computes
    the margin itself, and validates."""
    from app.retrieval.validator import validate

    class Model:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def generate(self, **kwargs):
            assert "operand" not in kwargs["prompt"]
            return {
                "done_reason": "stop",
                "response": (
                    "SELECT f.element_id, f.company_cik, f.ticker, f.entity_name, "
                    "f.fiscal_year, f.fiscal_period, f.period_start, f.period_end, "
                    "f.is_instant, f.value, f.unit, NULL::text AS derivation "
                    "FROM figures f ORDER BY f.value DESC LIMIT 500"
                ),
            }

    monkeypatch.setattr("app.retrieval.generator.AsyncClient", Model)
    sql = await generate(_margin_plan(intent="rank"))
    assert "figures AS (" in sql and "FROM figures f ORDER BY f.value DESC" in sql
    assert validate(sql) == sql


async def test_generate_does_not_ask_the_model_for_a_margin_plan(monkeypatch) -> None:
    class NoModel:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("the model was asked")

    monkeypatch.setattr("app.retrieval.generator.AsyncClient", NoModel)
    sql = await generate(_margin_plan())
    assert sql.startswith("WITH wanted(") and "AS value" in sql


def test_arithmetic_or_a_threshold_is_never_handed_to_the_model() -> None:
    """If `figures` cannot hold a plan -- one element with two expressions --
    it is refused, never sent to the model with operands to combine."""
    mixed = _plan(
        [
            _margin_binding(APPLE),
            _binding(company_cik=NVIDIA, concepts=[_concept(435), _concept(252)],
                     expression="c0 - c1", unit="USD", operand_unit="USD"),
        ],
        [_annual(APPLE), _annual(NVIDIA)],
        intent="rank",
        companies=2,
    )
    with pytest.raises(UnsupportedPlan, match="never handed to the model"):
        build_prompt(mixed)


# --------------------------------------------------------------------------- #
# over_time -- change, growth and CAGR, computed in Python from two periods
# --------------------------------------------------------------------------- #


def _revenue(years, concept_id: int = 252) -> Binding:
    return _binding(
        element_id="m",
        company_cik=NVIDIA,
        concepts=[_concept(concept_id, "Revenues")],
        periods=[PeriodRef(fiscal_year=y, fiscal_period="FY") for y in years],
    )


def _growth_plan(kind: str = "growth", years=(2021, 2022, 2023), bindings=None, intent="trend"):
    from app.schemas.query import PlanOverTime

    periods = [_annual(NVIDIA, y) for y in years]
    plan = _plan(bindings or [_revenue(years)], periods, intent=intent)
    return plan.model_copy(update={"over_time": [PlanOverTime(element_id="m", kind=kind)]})


def test_over_time_pairs_a_series_step_by_step_and_a_single_period_with_its_support() -> None:
    from app.schemas.query import over_time_pairs

    series = [_annual(NVIDIA, y) for y in (2021, 2022, 2023)]
    pairs = over_time_pairs("growth", series, {})
    assert [(a.fiscal_year, b.fiscal_year) for a, b in pairs] == [(2022, 2021), (2023, 2022)]
    single = [_annual(NVIDIA, 2024)]
    earlier = {(NVIDIA, 2023, "FY"): _annual(NVIDIA, 2023)}
    pairs = over_time_pairs("growth", single, earlier)
    assert [(a.fiscal_year, b.fiscal_year) for a, b in pairs] == [(2024, 2023)]
    assert over_time_pairs("growth", single, {}) == [], "nothing loaded to measure against"
    cagr = over_time_pairs("cagr", series, {})
    assert [(a.fiscal_year, b.fiscal_year) for a, b in cagr] == [(2023, 2021)]


def test_a_growth_cell_reads_the_metric_at_both_ends() -> None:
    cells = plan_cells(_growth_plan())
    assert [(c.fiscal_year, c.derivation, c.result_unit) for c in cells] == [
        (2022, "growth", "pure"), (2023, "growth", "pure"),
    ]
    first = cells[0]
    assert first.concept_ids == (252, 252)
    assert [w[1].year for w in first.operand_windows] == [2022, 2021]
    assert first.expression == "CASE WHEN (c1) > 0 THEN ((c0) - (c1)) / (c1) END"


def test_a_growth_across_a_tag_change_reads_each_end_from_its_own_binding() -> None:
    """NVIDIA's revenue concept changes between FY2022 and FY2023, so FY2023's
    growth reads one concept now and the other a year back."""
    plan = _growth_plan(bindings=[_revenue((2021, 2022), 252), _revenue((2023,), 254)])
    by_year = {c.fiscal_year: c.concept_ids for c in plan_cells(plan)}
    assert by_year == {2022: (252, 252), 2023: (254, 252)}


def test_a_cagr_is_one_cell_over_the_span() -> None:
    (cell,) = plan_cells(_growth_plan("cagr", years=(2021, 2022, 2023, 2024, 2025)))
    assert (cell.fiscal_year, cell.span_years, cell.derivation) == (2025, 4, "cagr")
    assert "power((c0) / (c1), 1.0 / max(w.span_years)) - 1" in cell.expression


def test_an_over_time_statement_validates_and_carries_its_derivation() -> None:
    from app.retrieval.prompt import emit_figures, figures_select
    from app.retrieval.validator import validate

    plan = _growth_plan()
    cte = prompt_module.emit_cte(plan_cells(plan))
    assert "cell_start, cell_end, span_years" in cte
    figures = emit_figures(plan)
    assert "w.derivation AS derivation" in figures and "w.cell_end AS period_end" in figures
    sql = cte + "," + chr(10) + figures + chr(10) + figures_select(plan)
    assert "value, unit, derivation" in figures_select(plan)
    assert validate(sql) == sql


def test_the_model_is_asked_only_to_rank_an_over_time_metric() -> None:
    """A derivation the over-time metric already computed is not derived again."""
    from app.retrieval.prompt import needs_the_model

    assert not needs_the_model(_growth_plan(intent="derive"))
    assert not needs_the_model(_growth_plan(intent="trend"))
    assert needs_the_model(_growth_plan(intent="rank"))
    text = build_prompt(_growth_plan(intent="rank"))
    assert "m is its growth" in text and "f.derivation" in text
    assert "operand" not in text and "c0" not in text


def test_a_comparison_of_plain_figures_is_asked_for_the_figures_only() -> None:
    """q008: "compare" pushed the model to compute, with nothing to follow. The
    figures side by side are the comparison, so no choice is offered."""
    compare = build_prompt(_plan([_binding()], [_annual()], intent="compare"))
    assert "side by side ARE the comparison" in compare
    assert "Decide which of these two" not in compare
    assert "side by side ARE the comparison" not in build_prompt(
        _plan([_binding()], [_annual()], intent="lookup")
    )
    ratio = _plan([_binding(unit="pure")], [_annual()], intent="compare")
    assert "side by side ARE the comparison" not in build_prompt(ratio)


# --------------------------------------------------------------------------- #
# growth beside a plain series -- the figures and the change between them
# --------------------------------------------------------------------------- #


def _series_with_growth(years=(2021, 2022, 2023)) -> QueryPlan:
    from app.schemas.query import PlanOverTime

    plan = _plan([_revenue(years)], [_annual(NVIDIA, y) for y in years], intent="trend")
    return plan.model_copy(
        update={"over_time": [PlanOverTime(element_id="m", kind="growth", replaces=False)]}
    )


def test_a_series_keeps_its_figures_and_gains_the_growth_between_them() -> None:
    cells = plan_cells(_series_with_growth())
    assert [(c.fiscal_year, c.derivation) for c in cells] == [
        (2021, None), (2022, None), (2023, None), (2022, "growth"), (2023, "growth"),
    ]


def test_figures_tells_a_growth_row_from_a_filed_row_of_the_same_metric() -> None:
    from app.retrieval.prompt import emit_figures, figures_select, uses_figures
    from app.retrieval.validator import validate

    plan = _series_with_growth()
    assert uses_figures(plan), "Python writes it; the model is not asked"
    cte = prompt_module.emit_cte(plan_cells(plan))
    assert "'growth')" in cte and ", NULL)" in cte
    figures = emit_figures(plan)
    assert "w.element_id = 'm' AND w.derivation IS NULL THEN" in figures
    assert "w.element_id = 'm' AND w.derivation = 'growth' THEN" in figures
    assert "w.derivation AS derivation" in figures and ", w.derivation" in figures
    sql = cte + "," + chr(10) + figures + chr(10) + figures_select(plan)
    assert validate(sql) == sql


def test_the_verdict_holds_a_series_and_its_growth_to_every_row() -> None:
    """Exact on (element, company, period, derivation): a missing growth row
    is a shortfall, not hidden behind the filed row with the same period."""
    plan = _series_with_growth(years=(2022, 2023))

    def row(year: int, value: str, derivation: str | None) -> AnnotatedRow:
        period = _annual(NVIDIA, year)
        return AnnotatedRow(
            row=ResultRow(
                element_id="m", company_cik=NVIDIA, fiscal_year=year, fiscal_period="FY",
                period_start=period.period_start, period_end=period.period_end,
                is_instant=False, value=Decimal(value),
                unit="pure" if derivation else "USD", derivation=derivation,
            ),
            binding_keys=["b0"],
        )

    full = [row(2022, "10", None), row(2023, "12", None), row(2023, "0.2", "growth")]
    assert executor._verdict(full, plan).status == "complete"
    short = executor._verdict(full[:2], plan)
    assert short.status == "partial" and not short.is_answerable
