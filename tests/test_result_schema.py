"""Tests for app/schemas/result.py and QueryPlan's binding lookup -- pure, no
database.

The contract exists to make three things impossible (see
``app/retrieval/DESIGN.md`` §1), so the tests are organised around those:
a row count that cannot be checked, a citation that was guessed rather than
looked up, and a derived value that reads as the metric it came from.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.query import (
    Binding,
    ConceptRef,
    Coverage,
    Note,
    PeriodRef,
    PlanFilters,
    QueryPlan,
    ResultSpec,
)
from app.schemas.result import (
    RESULT_COLUMNS,
    AnnotatedRow,
    Citation,
    MissingCell,
    ResultRow,
    ResultSet,
    ResultVerdict,
)

APPLE = 320193
NVIDIA = 1045810


def _concept(concept_id: int = 252, name: str = "Revenues") -> ConceptRef:
    return ConceptRef(concept_id=concept_id, taxonomy="us-gaap", name=name)


def _binding(**overrides) -> Binding:
    kwargs = {
        "element_id": "e1",
        "concepts": [_concept()],
        "unit": "USD",
        "is_instant": False,
        "coverage": Coverage(fact_count=5),
        "confidence": 0.9,
        "resolved_by": "alias",
        "rationale": "because",
    }
    return Binding(**(kwargs | overrides))


def _plan(bindings: list[Binding]) -> QueryPlan:
    return QueryPlan(
        question="q",
        intent="lookup",
        result=ResultSpec(shape="scalar", companies=1, periods=1, metrics=1),
        filters=PlanFilters(),
        bindings=bindings,
    )


def _row(**overrides) -> ResultRow:
    kwargs = {
        "element_id": "e1",
        "company_cik": APPLE,
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_start": date(2023, 10, 1),
        "period_end": date(2024, 9, 28),
        "is_instant": False,
        "value": Decimal("391035000000"),
        "unit": "USD",
    }
    return ResultRow(**(kwargs | overrides))


def _citation(binding_key: str = "b0", **overrides) -> Citation:
    kwargs = {
        "binding_key": binding_key,
        "element_id": "e1",
        "company_cik": APPLE,
        "concepts": [_concept()],
        "expression": "c0",
        "unit": "USD",
        "resolved_by": "alias",
        "confidence": 0.9,
        "rationale": "because",
    }
    return Citation(**(kwargs | overrides))


# --------------------------------------------------------------------------- #
# ResultRow -- the SQL contract
# --------------------------------------------------------------------------- #


def test_result_columns_is_the_projection_validate_checks() -> None:
    """The column list is the contract. If this changes, every prompt and the
    projection check in validate() change with it."""
    assert RESULT_COLUMNS == (
        "element_id",
        "company_cik",
        "ticker",
        "entity_name",
        "fiscal_year",
        "fiscal_period",
        "period_start",
        "period_end",
        "is_instant",
        "value",
        "unit",
        "derivation",
    )


def test_a_duration_row_needs_its_start() -> None:
    """A missing period_start is how a 90-day figure passes for a 365-day one."""
    with pytest.raises(ValidationError, match="needs period_start"):
        _row(period_start=None)


def test_an_instant_row_carries_no_span() -> None:
    with pytest.raises(ValidationError, match="covers no span"):
        _row(is_instant=True)


def test_an_instant_row_is_valid_without_a_start() -> None:
    row = _row(is_instant=True, period_start=None)
    assert row.period_start is None
    assert row.period_end == date(2024, 9, 28)


def test_a_backwards_window_is_rejected() -> None:
    with pytest.raises(ValidationError, match="period_start < period_end"):
        _row(period_start=date(2025, 1, 1))


def test_derivation_defaults_to_none_meaning_as_reported() -> None:
    assert _row().derivation is None
    assert _row(derivation="yoy_growth").derivation == "yoy_growth"


def test_an_unknown_column_is_rejected() -> None:
    """extra='forbid': a key the generator believed it was sending must fail
    loudly rather than be dropped."""
    with pytest.raises(ValidationError):
        _row(concept_id=252)


# --------------------------------------------------------------------------- #
# QueryPlan.binding_for -- citation as a lookup, not a guess
# --------------------------------------------------------------------------- #


def test_binding_for_returns_the_index_the_key_is_built_from() -> None:
    plan = _plan([_binding(company_cik=APPLE)])
    index, binding = plan.binding_for("e1", APPLE, 2024, "FY")
    assert index == 0
    assert QueryPlan.binding_key(index) == "b0"
    assert binding.company_cik == APPLE


def test_a_company_specific_binding_beats_the_fallback() -> None:
    """§8.3: company_cik=None holds for every filer, and a per-company binding
    overrides it. Both match the cell; only one is the answer."""
    plan = _plan([_binding(), _binding(company_cik=APPLE, concepts=[_concept(254)])])
    index, binding = plan.binding_for("e1", APPLE, 2024, "FY")
    assert index == 1
    assert binding.concepts[0].concept_id == 254


def test_concept_drift_is_separated_by_period() -> None:
    """Alphabet reports revenue under one tag through FY2024 and another in
    FY2025, so one company has two bindings and the period picks between
    them. Citing the wrong one is exactly what this prevents."""
    plan = _plan(
        [
            _binding(
                company_cik=NVIDIA,
                concepts=[_concept(252, "RevenueFromContractWithCustomer")],
                periods=[PeriodRef(fiscal_year=2022, fiscal_period="FY")],
            ),
            _binding(
                company_cik=NVIDIA,
                concepts=[_concept(254, "Revenues")],
                periods=[PeriodRef(fiscal_year=2025, fiscal_period="FY")],
            ),
        ]
    )
    assert plan.binding_for("e1", NVIDIA, 2022, "FY")[0] == 0
    assert plan.binding_for("e1", NVIDIA, 2025, "FY")[0] == 1


def test_an_unanswered_cell_raises_rather_than_returning_none() -> None:
    plan = _plan([_binding(company_cik=APPLE)])
    with pytest.raises(LookupError, match="no binding answers"):
        plan.binding_for("e1", NVIDIA, 2024, "FY")


def test_two_bindings_claiming_one_cell_is_a_malformed_plan() -> None:
    """Nothing else in the system would notice this."""
    plan = _plan([_binding(company_cik=APPLE), _binding(company_cik=APPLE)])
    with pytest.raises(ValueError, match="A cell with two answers has none"):
        plan.binding_for("e1", APPLE, 2024, "FY")


def test_bindings_for_company_is_the_coarse_derived_row_attribution() -> None:
    """A growth figure spanning a tag change comes from both bindings, and the
    row's single period label cannot name the other end."""
    plan = _plan(
        [
            _binding(
                company_cik=NVIDIA, periods=[PeriodRef(fiscal_year=2022, fiscal_period="FY")]
            ),
            _binding(
                company_cik=NVIDIA, periods=[PeriodRef(fiscal_year=2025, fiscal_period="FY")]
            ),
            _binding(company_cik=APPLE),
        ]
    )
    assert [index for index, _ in plan.bindings_for_company("e1", NVIDIA)] == [0, 1]


# --------------------------------------------------------------------------- #
# ResultVerdict -- answer only when the shortfall was already disclosed
# --------------------------------------------------------------------------- #


def _missing(anticipated: bool) -> MissingCell:
    return MissingCell(
        element_id="e1",
        company_cik=APPLE,
        fiscal_year=2024,
        fiscal_period="FY",
        anticipated=anticipated,
    )


def test_a_complete_run_is_answerable() -> None:
    verdict = ResultVerdict(status="complete", expected_rows=36, returned_rows=36)
    assert verdict.is_answerable


def test_a_disclosed_gap_is_answerable() -> None:
    """partial_coverage exists so an answer can go out with a caveat; refusing
    here would make it pointless."""
    verdict = ResultVerdict(
        status="partial", expected_rows=36, returned_rows=35, missing=[_missing(True)]
    )
    assert verdict.is_answerable


def test_an_undisclosed_gap_refuses() -> None:
    """Coverage was proved before the binding was made, so a cell missing
    after that proof means the statement is wrong -- and 35 plausible dots is
    the shape this project exists to refuse."""
    verdict = ResultVerdict(
        status="partial", expected_rows=36, returned_rows=35, missing=[_missing(False)]
    )
    assert not verdict.is_answerable


def test_one_undisclosed_gap_among_disclosed_ones_still_refuses() -> None:
    verdict = ResultVerdict(
        status="partial",
        expected_rows=36,
        returned_rows=34,
        missing=[_missing(True), _missing(False)],
    )
    assert not verdict.is_answerable


def test_extra_rows_refuse() -> None:
    """A fan-out produces a confident wrong aggregate rather than a visible
    gap -- the AMD pure/Rate case."""
    verdict = ResultVerdict(status="over", expected_rows=1, returned_rows=2)
    assert not verdict.is_answerable


def test_an_empty_result_refuses() -> None:
    verdict = ResultVerdict(status="empty", expected_rows=36, returned_rows=0)
    assert not verdict.is_answerable


def test_an_unattributable_row_refuses_even_when_complete() -> None:
    """A row matching no binding was invented by the model."""
    verdict = ResultVerdict(
        status="complete", expected_rows=1, returned_rows=1, unattributable=[0]
    )
    assert not verdict.is_answerable


def test_status_and_counts_must_agree() -> None:
    with pytest.raises(ValidationError, match="no rows is 'empty'"):
        ResultVerdict(status="partial", expected_rows=1, returned_rows=0)
    with pytest.raises(ValidationError, match="needs returned_rows > expected_rows"):
        ResultVerdict(status="over", expected_rows=5, returned_rows=5)
    with pytest.raises(ValidationError, match="'complete' with 1 missing"):
        ResultVerdict(
            status="complete", expected_rows=1, returned_rows=1, missing=[_missing(True)]
        )


# --------------------------------------------------------------------------- #
# ResultSet -- internal consistency
# --------------------------------------------------------------------------- #


def _set(**overrides) -> ResultSet:
    kwargs = {
        "question": "q",
        "rows": [AnnotatedRow(row=_row(), binding_keys=["b0"])],
        "verdict": ResultVerdict(status="complete", expected_rows=1, returned_rows=1),
        "citations": {"b0": _citation()},
    }
    return ResultSet(**(kwargs | overrides))


def test_a_wellformed_result_set_is_answerable() -> None:
    result = _set()
    assert result.is_answerable
    assert result.rows[0].binding_keys == ["b0"]
    assert result.citations["b0"].concepts[0].name == "Revenues"


def test_a_row_citing_a_missing_citation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no citation"):
        _set(rows=[AnnotatedRow(row=_row(), binding_keys=["b7"])])


def test_the_row_count_must_match_the_verdict() -> None:
    with pytest.raises(ValidationError, match="returned_rows=1 but 2 row"):
        _set(rows=[AnnotatedRow(row=_row(), binding_keys=["b0"])] * 2)


def test_unattributable_must_agree_with_the_rows() -> None:
    """The verdict is what the presenter reads, so it cannot quietly disagree
    with the rows it describes."""
    with pytest.raises(ValidationError, match="disagrees with the rows"):
        _set(rows=[AnnotatedRow(row=_row(), binding_keys=[])])


def test_an_orphan_row_is_recorded_and_refused() -> None:
    result = _set(
        rows=[AnnotatedRow(row=_row(), binding_keys=[])],
        verdict=ResultVerdict(
            status="complete", expected_rows=1, returned_rows=1, unattributable=[0]
        ),
    )
    assert not result.is_answerable


def test_a_derived_row_can_span_two_bindings() -> None:
    result = _set(
        rows=[AnnotatedRow(row=_row(derivation="yoy_growth", unit="pure",
                                    value=Decimal("0.081")), binding_keys=["b0", "b1"])],
        citations={"b0": _citation("b0"), "b1": _citation("b1")},
    )
    assert result.rows[0].row.derivation == "yoy_growth"
    assert len(result.rows[0].binding_keys) == 2


def test_notes_survive_onto_the_envelope() -> None:
    note = Note(kind="incomplete_result", message="5 of 36 rows did not come back")
    result = _set(notes=[note])
    assert result.notes[0].kind == "incomplete_result"
