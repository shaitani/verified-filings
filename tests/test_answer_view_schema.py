"""``AnswerView`` -- the Presenter's output contract (app/schemas/answer_view.py)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.answer_view import (
    AnswerRow,
    AnswerView,
    BarView,
    CitationView,
    LineView,
    Series,
    StatView,
)
from app.schemas.query import ConceptRef


def _row(**overrides) -> AnswerRow:
    fields = {
        "element_id": "e1",
        "metric": "revenue",
        "company_cik": 320193,
        "company": "AAPL",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_label": "FY2024",
        "granularity": "annual",
        "period_start": date(2023, 10, 1),
        "period_end": date(2024, 9, 28),
        "is_instant": False,
        "value": Decimal("391035000000"),
        "display": "$391.04B",
        "unit": "USD",
        "unit_kind": "money",
        "citations": ["b0"],
    }
    return AnswerRow(**(fields | overrides))


def _citation(key: str = "b0") -> CitationView:
    return CitationView(
        key=key,
        concepts=[ConceptRef(concept_id=1, taxonomy="us-gaap", name="Revenues", label="Revenues")],
        expression="c0",
        unit="USD",
        resolved_by="alias",
    )


def _view(rows: list[AnswerRow], views: list | None = None, **overrides) -> AnswerView:
    fields = {
        "shape": "scalar",
        "rows": rows,
        "views": views or [],
        "citations": {"b0": _citation()},
    }
    return AnswerView(**(fields | overrides))


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def test_a_row_serialises_its_value_exactly_as_a_string() -> None:
    assert _row().model_dump(mode="json")["value"] == "391035000000"


def test_an_as_filed_row_needs_a_value() -> None:
    with pytest.raises(ValidationError, match="as-filed row must carry a value"):
        _row(value=None, display="—")


def test_a_derived_row_may_lack_a_value() -> None:
    row = _row(value=None, display="—", derivation="growth", unit="pure", unit_kind="ratio")
    assert row.value is None


def test_period_start_is_missing_exactly_for_an_instant() -> None:
    with pytest.raises(ValidationError, match="exactly when is_instant"):
        _row(period_start=None)
    with pytest.raises(ValidationError, match="exactly when is_instant"):
        _row(is_instant=True)
    assert _row(period_start=None, is_instant=True).is_instant


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #


def test_a_row_citing_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="unknown key"):
        _view([_row(citations=["b9"])])


def test_a_citation_filed_under_the_wrong_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="filed under"):
        _view([_row()], citations={"b0": _citation("b1")})


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #


def _quarter(month: int, value: str, **overrides) -> AnswerRow:
    end = date(2024, month, 28)
    return _row(
        fiscal_period="Q1",
        period_label="Q1 FY2024",
        granularity="quarterly",
        period_start=date(2024, month - 2, 1),
        period_end=end,
        value=Decimal(value),
        **overrides,
    )


def test_a_stat_view_points_at_a_real_row() -> None:
    assert _view([_row()], [StatView(title="Revenue", row=0)]).views[0].kind == "stat"
    with pytest.raises(ValidationError, match="points at row 3"):
        _view([_row()], [StatView(title="Revenue", row=3)])


def test_a_line_series_must_be_in_date_order() -> None:
    rows = [_quarter(3, "1"), _quarter(6, "2")]
    series = LineView(
        title="Revenue", unit_kind="money", granularity="quarterly",
        series=[Series(label="Apple", rows=[1, 0])],
    )
    with pytest.raises(ValidationError, match="not in date order"):
        _view(rows, [series], shape="series")


def test_a_line_view_never_mixes_annual_and_quarterly() -> None:
    rows = [_quarter(3, "1"), _row()]
    series = LineView(
        title="Revenue", unit_kind="money", granularity="quarterly",
        series=[Series(label="Apple", rows=[0, 1])],
    )
    with pytest.raises(ValidationError, match="mixes annual and quarterly"):
        _view(rows, [series], shape="series")


def test_a_ratio_never_shares_a_money_axis() -> None:
    margin = _row(value=Decimal("0.462063"), display="46.2%", unit="pure", unit_kind="ratio")
    bar = BarView(title="Apple", unit_kind="money", order="descending", rows=[0, 1])
    with pytest.raises(ValidationError, match="mixes unit kinds"):
        _view([_row(), margin], [bar], shape="ranking")


def test_a_bar_view_must_be_drawn_in_its_stated_order() -> None:
    rows = [_row(value=Decimal("1")), _row(value=Decimal("3"), company_cik=1, company="X")]
    with pytest.raises(ValidationError, match="not in descending order"):
        _view(rows, [BarView(title="t", unit_kind="money", order="descending", rows=[0, 1])])
    ok = _view(rows, [BarView(title="t", unit_kind="money", order="descending", rows=[1, 0])])
    assert ok.views[0].rows == [1, 0]


def test_a_bar_view_cannot_rank_a_missing_value() -> None:
    growth = _row(value=None, display="—", derivation="growth", unit="pure", unit_kind="ratio")
    bar = BarView(title="growth", unit_kind="ratio", order="descending", rows=[0])
    with pytest.raises(ValidationError, match="no value to rank"):
        _view([growth], [bar], shape="ranking")


def test_the_view_kind_travels_on_the_wire() -> None:
    dumped = _view([_row()], [StatView(title="Revenue", row=0)]).model_dump(mode="json")
    assert dumped["views"][0]["kind"] == "stat"
    assert AnswerView.model_validate(dumped).views[0].kind == "stat"
