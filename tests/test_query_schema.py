"""Tests for app/schemas/query.py -- pure, no database.

Covers the invariants the schemas are there to enforce: that the element union
dispatches on ``kind``, that our own consistency rules (unique ids, expression
operands in range) fail loudly, and that the strictness inherited from
``xbrl.py``'s ``_Base`` actually applies.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.query import (
    Ambiguity,
    Binding,
    Candidate,
    CompanyElementIn,
    ComponentCoverage,
    ConceptRef,
    Coverage,
    MetricElementIn,
    PeriodElementIn,
    PeriodResidual,
    PlanFilters,
    QueryIn,
    QueryPlan,
    ResolvedPeriod,
    Unresolved,
)


def _query(*elements, intent: str = "lookup") -> dict:
    return {"question": "q", "intent": intent, "elements": list(elements)}


def _concept(concept_id: int = 1, name: str = "Revenues") -> ConceptRef:
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


# --------------------------------------------------------------------------- #
# QueryIn
# --------------------------------------------------------------------------- #


def test_elements_dispatch_on_kind() -> None:
    query = QueryIn.model_validate(
        _query(
            {"id": "e1", "text": "revenue", "kind": "metric"},
            {"id": "e2", "text": "Apple", "kind": "company", "ticker": "AAPL"},
            {"id": "e3", "text": "last 5 years", "kind": "period", "last_n_years": 5},
            {"id": "e4", "text": "annual", "kind": "qualifier"},
        )
    )

    assert isinstance(query.elements[0], MetricElementIn)
    assert isinstance(query.elements[1], CompanyElementIn)
    assert isinstance(query.elements[2], PeriodElementIn)
    assert query.version == "1"


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryIn.model_validate(_query({"id": "e1", "text": "x", "kind": "sector"}))


def test_payload_from_the_wrong_kind_is_rejected() -> None:
    """A ticker on a metric element is extra="forbid" territory -- it means the
    producer mislabelled the element, which should fail rather than be ignored."""
    with pytest.raises(ValidationError):
        QueryIn.model_validate(
            _query({"id": "e1", "text": "revenue", "kind": "metric", "ticker": "AAPL"})
        )


def test_duplicate_element_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate element id"):
        QueryIn.model_validate(
            _query(
                {"id": "e1", "text": "revenue", "kind": "metric"},
                {"id": "e1", "text": "profit", "kind": "metric"},
            )
        )


def test_period_rejects_absolute_and_relative_together() -> None:
    with pytest.raises(ValidationError, match="not both"):
        QueryIn.model_validate(
            _query(
                {"id": "e1", "text": "x", "kind": "period", "fiscal_year": 2024, "last_n_years": 3}
            )
        )


def test_elements_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        QueryIn.model_validate(_query())


def test_validated_query_is_frozen() -> None:
    query = QueryIn.model_validate(_query({"id": "e1", "text": "revenue", "kind": "metric"}))
    with pytest.raises(ValidationError):
        query.question = "something else"


# --------------------------------------------------------------------------- #
# Binding
# --------------------------------------------------------------------------- #


def test_binding_defaults_to_the_single_concept_expression() -> None:
    assert _binding().expression == "c0"


def test_binding_allows_an_expression_over_several_concepts() -> None:
    binding = _binding(
        concepts=[_concept(1, "GrossProfit"), _concept(2, "Revenues")],
        expression="c0 / c1",
    )
    assert len(binding.concepts) == 2


def test_binding_rejects_an_operand_it_did_not_bind() -> None:
    """The guard that keeps a derived-metric expression honest: "c0 / c1" with
    only one concept bound would hand the SQL step a dangling reference."""
    with pytest.raises(ValidationError, match="references c1"):
        _binding(expression="c0 / c1")


# --------------------------------------------------------------------------- #
# QueryPlan
# --------------------------------------------------------------------------- #


def test_plan_is_complete_only_when_nothing_is_outstanding() -> None:
    plan = QueryPlan(question="q", intent="lookup", filters=PlanFilters(), bindings=[_binding()])
    assert plan.is_complete

    with_gap = QueryPlan(
        question="q",
        intent="lookup",
        filters=PlanFilters(),
        unresolved=[Unresolved(element_id="e1", reason="nope")],
    )
    assert not with_gap.is_complete


def test_ambiguity_needs_at_least_two_candidates() -> None:
    """One surviving candidate is a binding, not an ambiguity."""
    candidate = Candidate(concept=_concept(), score=0.8, coverage=Coverage(fact_count=3))
    with pytest.raises(ValidationError):
        Ambiguity(element_id="e1", candidates=[candidate])


def test_plan_filters_default_to_unconstrained() -> None:
    filters = PlanFilters()
    assert filters.ciks == []
    assert filters.periods == []
    assert filters.forms == []


def test_resolved_period_allows_a_window_ending_outside_its_fiscal_year() -> None:
    """J&J's FY2021 ends 2022-01-02. The model must not assume period_end's
    calendar year matches fiscal_year -- 52/53-week filers break that, which is
    why the window is read from the facts instead of derived from the label."""
    period = ResolvedPeriod(
        company_cik=200406,
        fiscal_year=2021,
        fiscal_period="FY",
        period_start=date(2021, 1, 4),
        period_end=date(2022, 1, 2),
    )
    assert period.fiscal_year == 2021
    assert period.period_end.year == 2022


# --------------------------------------------------------------------------- #
# Q4 -- the period the store cannot supply directly
# --------------------------------------------------------------------------- #


def _residual() -> PeriodResidual:
    """Apple's FY2024: annual 2023-10-01 -> 2024-09-28, nine-month term ending
    2024-06-29. Real windows, so the shape is checked against real data."""
    return PeriodResidual(
        shared_start=date(2023, 10, 1),
        whole_end=date(2024, 9, 28),
        subtract_end=date(2024, 6, 29),
    )


def test_query_period_accepts_q4_even_though_no_filing_has_one() -> None:
    """The query vocabulary is wider than the storage vocabulary on purpose --
    "compare their Q4s" is askable, and translating it is the mapper's job."""
    query = QueryIn.model_validate(
        _query({"id": "e1", "text": "Q4", "kind": "period", "fiscal_period": "Q4"})
    )
    assert query.elements[0].fiscal_period == "Q4"


def test_residual_requires_the_subtrahend_inside_the_whole() -> None:
    with pytest.raises(ValidationError, match="shared_start < subtract_end < whole_end"):
        PeriodResidual(
            shared_start=date(2023, 10, 1),
            whole_end=date(2024, 9, 28),
            subtract_end=date(2024, 10, 5),  # past the end of the whole window
        )


def test_q4_period_must_carry_its_components() -> None:
    with pytest.raises(ValidationError, match="only period the store cannot supply"):
        ResolvedPeriod(
            company_cik=320193,
            fiscal_year=2024,
            fiscal_period="Q4",
            period_start=date(2024, 6, 30),
            period_end=date(2024, 9, 28),
        )


def test_non_q4_period_must_not_carry_components() -> None:
    with pytest.raises(ValidationError, match="only period the store cannot supply"):
        ResolvedPeriod(
            company_cik=320193,
            fiscal_year=2024,
            fiscal_period="FY",
            period_start=date(2023, 10, 1),
            period_end=date(2024, 9, 28),
            residual_of=_residual(),
        )


def test_residual_binding_rejects_a_missing_component() -> None:
    """The NVIDIA case: the concept has annual facts but no nine-month ones, so
    a concept-level count passes while the subtraction cannot be computed.
    Dropping the missing term would silently return the whole year as "Q4"."""
    with pytest.raises(ValidationError, match="plausible wrong number"):
        _binding(
            period_rule="residual",
            coverage=Coverage(
                fact_count=4,  # nonzero at concept level -- the trap
                components=[
                    ComponentCoverage(
                        period_start=date(2023, 10, 1),
                        period_end=date(2024, 9, 28),
                        fact_count=4,
                    ),
                    ComponentCoverage(
                        period_start=date(2023, 10, 1),
                        period_end=date(2024, 6, 29),
                        fact_count=0,  # nothing to subtract
                    ),
                ],
            ),
        )


def test_residual_binding_rejects_an_instant_concept() -> None:
    """Total assets at Q4 is just the fiscal-year-end instant -- subtracting
    anything from it would be wrong, not merely unnecessary."""
    with pytest.raises(ValidationError, match="instant fact needs no residual"):
        _binding(
            is_instant=True,
            period_rule="residual",
            coverage=Coverage(
                fact_count=5,
                components=[
                    ComponentCoverage(
                        period_start=date(2023, 10, 1),
                        period_end=date(2024, 9, 28),
                        fact_count=5,
                    )
                ],
            ),
        )


def test_residual_binding_accepts_provable_components() -> None:
    binding = _binding(
        period_rule="residual",
        coverage=Coverage(
            fact_count=9,
            components=[
                ComponentCoverage(
                    period_start=date(2023, 10, 1), period_end=date(2024, 9, 28), fact_count=5
                ),
                ComponentCoverage(
                    period_start=date(2023, 10, 1), period_end=date(2024, 6, 29), fact_count=4
                ),
            ],
        ),
    )
    assert binding.period_rule == "residual"


def test_direct_binding_needs_no_components() -> None:
    assert _binding().period_rule == "direct"
    assert _binding().coverage.components == []
