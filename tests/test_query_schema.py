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
    CompanyGroupElementIn,
    ConceptRef,
    Coverage,
    MetricElementIn,
    PeriodElementIn,
    PlanFilters,
    QueryIn,
    QueryPlan,
    ResolvedPeriod,
    ResultSpec,
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
            {
                "id": "e4",
                "text": "semiconductors",
                "kind": "company_group",
                "sic_description": "semiconductor",
            },
        )
    )

    assert isinstance(query.elements[0], MetricElementIn)
    assert isinstance(query.elements[1], CompanyElementIn)
    assert isinstance(query.elements[2], PeriodElementIn)
    assert isinstance(query.elements[3], CompanyGroupElementIn)
    assert query.version == "1"


def test_retired_qualifier_kind_is_rejected() -> None:
    """`qualifier` was inert -- nothing ever resolved it -- so a parser
    emitting one had its intent silently dropped. Removing the kind turns that
    into a loud error."""
    with pytest.raises(ValidationError):
        QueryIn.model_validate(_query({"id": "e1", "text": "annual", "kind": "qualifier"}))


def test_company_group_needs_a_selector() -> None:
    """An unconstrained group is every loaded filer, which is already what
    omitting the element means -- so an empty one is a mistake, not a wildcard."""
    with pytest.raises(ValidationError, match="at least one of sic_code"):
        QueryIn.model_validate(_query({"id": "e1", "text": "companies", "kind": "company_group"}))


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        QueryIn.model_validate(_query({"id": "e1", "text": "x", "kind": "sector"}))


def test_payload_from_the_wrong_kind_is_rejected() -> None:
    """A ticker on a metric element is extra="forbid" territory -- it means the
    parser mislabelled the element, which should fail rather than be ignored."""
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
        unit="pure",
        operand_unit="USD",
    )
    assert len(binding.concepts) == 2
    assert binding.fact_unit == "USD"


def test_a_ratio_must_say_what_its_operands_are_filed_in() -> None:
    """The result of `c0 / c1` is `pure`, and no `pure` fact exists behind it.
    Joining on the result unit returns nothing -- and nothing is
    indistinguishable from "the company reported nothing"."""
    with pytest.raises(ValidationError, match="operand_unit must say"):
        _binding(
            concepts=[_concept(1, "GrossProfit"), _concept(2, "Revenues")],
            expression="c0 / c1",
            unit="pure",
        )


def test_a_single_operand_binding_joins_on_its_own_unit() -> None:
    """Nothing to carry: the result *is* the fact."""
    binding = _binding(unit="USD")
    assert binding.operand_unit is None
    assert binding.fact_unit == "USD"


def test_binding_rejects_an_operand_it_did_not_bind() -> None:
    """The guard that keeps a derived-metric expression honest: "c0 / c1" with
    only one concept bound would hand the SQL step a dangling reference."""
    with pytest.raises(ValidationError, match="references c1"):
        _binding(expression="c0 / c1")


# --------------------------------------------------------------------------- #
# QueryPlan
# --------------------------------------------------------------------------- #


def _scalar() -> ResultSpec:
    return ResultSpec(shape="scalar", companies=1, periods=1, metrics=1)


def test_plan_is_complete_only_when_nothing_is_outstanding() -> None:
    plan = QueryPlan(
        question="q",
        intent="lookup",
        result=_scalar(),
        filters=PlanFilters(),
        bindings=[_binding()],
    )
    assert plan.is_complete

    with_gap = QueryPlan(
        question="q",
        intent="lookup",
        result=_scalar(),
        filters=PlanFilters(),
        unresolved=[Unresolved(element_id="e1", reason="nope")],
    )
    assert not with_gap.is_complete


def test_row_count_is_the_product_of_the_axes() -> None:
    """Twelve quarters for three companies is 36 points, not 3 or 12 -- this is
    the number a chart request actually asks retrieval for."""
    spec = ResultSpec(
        shape="series", axes=["company", "period"], companies=3, periods=12, metrics=1
    )
    assert spec.row_count == 36
    assert _scalar().row_count == 1


def test_ambiguity_accepts_a_lone_weak_candidate() -> None:
    """A single match too weak to trust is still something to offer back, so it
    shares the channel with a genuine tie -- but zero candidates is not an
    ambiguity, it is an Unresolved."""
    candidate = Candidate(concept=_concept(), score=0.8, coverage=Coverage(fact_count=3))
    lone = Ambiguity(element_id="e1", element_text="dividends", candidates=[candidate])
    assert len(lone.candidates) == 1

    with pytest.raises(ValidationError):
        Ambiguity(element_id="e1", element_text="dividends", candidates=[])


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


def test_query_period_accepts_q4_even_though_no_filing_has_one() -> None:
    """The query vocabulary is wider than the storage vocabulary on purpose --
    "compare their Q4s" is askable, and translating it is the mapper's job."""
    query = QueryIn.model_validate(
        _query({"id": "e1", "text": "Q4", "kind": "period", "fiscal_period": "Q4"})
    )
    assert query.elements[0].fiscal_period == "Q4"


def test_a_q4_resolved_period_is_an_ordinary_period() -> None:
    """It used to carry `residual_of` -- the two windows the SQL step had to
    subtract -- with a validator insisting Q4 was the only period allowed one,
    and `Binding` carried a `period_rule` saying which arithmetic applied.
    `xbrl.reported_fact` computes the fourth quarter now (migration
    a8b5b820cf1a), so a Q4 is a window like any other and the schema has
    nothing left to special-case."""
    period = ResolvedPeriod(
        company_cik=320193,
        fiscal_year=2024,
        fiscal_period="Q4",
        period_start=date(2024, 6, 30),
        period_end=date(2024, 9, 28),
    )
    assert period.granularity == "quarterly"
    assert "residual_of" not in ResolvedPeriod.model_fields
    assert "period_rule" not in Binding.model_fields
    assert "components" not in Coverage.model_fields
