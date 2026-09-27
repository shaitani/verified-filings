"""[F] Presenter over real ``ResultSet``s captured from the chain (tests/fixtures/presenter/).

Each fixture is one eval question's Executor output, captured 2026-09-27, so
these run with no database and no model.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.presenter import BAR_LIMIT, PresentationError, present
from app.presenter.format import display
from app.schemas.answer_view import AnswerView
from app.schemas.result import ResultSet

FIXTURES = Path(__file__).parent / "fixtures" / "presenter"


def _load(name: str) -> tuple[ResultSet, dict[str, str]]:
    fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return ResultSet.model_validate(fixture["result_set"]), fixture["metrics"]


def _present(name: str) -> AnswerView:
    return present(*_load(name))


@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.json")))
def test_every_captured_result_presents(name: str) -> None:
    result, _ = _load(name)
    view = _present(name)  # AnswerView's own validators run here
    assert len(view.rows) == len(result.rows)  # the table drops nothing
    assert view.notes == result.notes  # verbatim


# --------------------------------------------------------------------------- #
# One view per shape
# --------------------------------------------------------------------------- #


def test_a_single_figure_is_a_stat() -> None:
    view = _present("q001")  # Apple FY2024 revenue, HANDOFF §8's check figure
    (stat,) = view.views
    assert stat.kind == "stat"
    assert view.rows[stat.row].value == Decimal("391035000000")
    assert view.rows[stat.row].display == "$391.04B"
    assert view.rows[stat.row].period_label == "FY2024"


def test_a_growth_asked_for_alone_is_a_signed_percentage() -> None:
    view = _present("q011")  # Tesla FY2024 revenue growth
    assert view.rows[0].derivation == "growth"
    assert view.rows[0].display == "+0.9%"
    assert "revenue growth" in view.views[0].title


def test_a_ranking_is_drawn_in_the_direction_asked() -> None:
    view = _present("q009")  # highest operating margin, 16 filers
    (bar,) = view.views
    assert bar.order == "descending" and len(bar.rows) == BAR_LIMIT
    assert view.rows[bar.rows[0]].company == "NVDA"
    assert "top 10 of 16" in bar.title
    assert [n.kind for n in view.notes] == ["partial_coverage", "period_misalignment"]


def test_a_largest_decline_ranks_lowest_first() -> None:
    view = _present("q038")  # 359 quarter-on-quarter changes
    (bar,) = view.views
    assert bar.order == "ascending" and bar.unit_kind == "money"
    first = view.rows[bar.rows[0]]
    assert (first.company, first.period_label, first.display) == ("AMZN", "Q1 FY2025", "-$32.13B")
    assert len(view.rows) == 359  # the chart is capped; the table is not


def test_a_ranking_sorts_itself_rather_than_trusting_row_order() -> None:
    result, metrics = _load("q014")
    shuffled = result.model_copy(update={"rows": list(reversed(result.rows))})
    view = present(shuffled, metrics)
    assert view.rows[view.views[0].rows[0]].company == "NVDA"  # +125.9% growth


def test_a_comparison_table_has_no_chart() -> None:
    view = _present("q017")
    assert view.shape == "table" and view.views == []


# --------------------------------------------------------------------------- #
# Series: panels never mix what cannot share an axis
# --------------------------------------------------------------------------- #


def test_a_trend_draws_figures_and_growth_on_separate_panels() -> None:
    view = _present("q013")
    money, growth = view.views
    assert (money.unit_kind, growth.unit_kind) == ("money", "ratio")
    assert len(money.series[0].rows) == 5 and len(growth.series[0].rows) == 4


def test_two_metrics_share_a_panel_when_they_share_a_unit() -> None:
    view = _present("q041")  # revenue and net income side by side
    money = view.views[0]
    assert [s.label for s in money.series] == ["AAPL — revenue", "AAPL — net income"]


def test_one_line_per_company() -> None:
    view = _present("q007")  # Intel and AMD gross margins
    (panel,) = view.views
    assert panel.unit_kind == "ratio"
    assert sorted(s.label for s in panel.series) == ["AMD — gross margin", "INTC — gross margin"]


def test_annual_and_quarterly_never_share_a_panel() -> None:
    view = _present("smoke_mixed")  # 128 rows, mixed_granularity, a tag change
    panels = {(v.unit_kind, v.granularity) for v in view.views}
    assert panels == {
        ("money", "annual"), ("money", "quarterly"), ("ratio", "annual"), ("ratio", "quarterly"),
    }
    assert "mixed_granularity" in [n.kind for n in view.notes]


# --------------------------------------------------------------------------- #
# Tables, units and citations
# --------------------------------------------------------------------------- #


def test_a_list_of_balances_is_a_table_of_instants() -> None:
    view = _present("q052")  # goodwill was refused upstream; five figures remain
    assert view.views == [] and len(view.rows) == 5
    assert all(row.is_instant and row.period_start is None for row in view.rows)
    assert [row.metric for row in view.rows][:2] == ["assets", "liabilities"]  # asker's order


def test_a_binding_caveat_travels_on_its_citation() -> None:
    view = _present("q052")
    cash = next(row for row in view.rows if row.metric == "cash")
    (key,) = cash.citations
    assert [n.kind for n in view.citations[key].notes] == ["narrower_than_asked"]


def test_per_share_is_not_scaled_like_money() -> None:
    view = _present("q053")
    shown = {row.metric: row.display for row in view.rows}
    assert shown == {"share buyback spend": "$90.71B", "dividends per share": "$1.02"}


def test_a_concept_switch_is_cited_on_both_bindings() -> None:
    view = _present("smoke_mixed")  # Alphabet changes revenue tags mid-range
    switched = [
        c for c in view.citations.values() if any(n.kind == "concept_switch" for n in c.notes)
    ]
    assert len(switched) == 2


# --------------------------------------------------------------------------- #
# What the Presenter refuses
# --------------------------------------------------------------------------- #


def test_an_unanswerable_result_is_refused() -> None:
    result, metrics = _load("q001")
    verdict = result.verdict.model_copy(update={"status": "over", "expected_rows": 0})
    with pytest.raises(PresentationError, match="not answerable"):
        present(result.model_copy(update={"verdict": verdict}), metrics)


def test_a_figure_with_no_phrase_is_refused() -> None:
    result, _ = _load("q001")
    with pytest.raises(PresentationError, match="no metric phrase"):
        present(result, {})


def test_an_unknown_unit_is_refused_rather_than_guessed() -> None:
    with pytest.raises(PresentationError, match="no display rule"):
        display(Decimal("3"), "warehouse", None)


# --------------------------------------------------------------------------- #
# Display strings
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "unit", "derivation", "shown"),
    [
        ("391035000000", "USD", None, "$391.04B"),
        ("1250000000000", "USD", None, "$1.25T"),
        ("950000", "USD", None, "$950,000"),
        ("-32125000000", "USD", "change", "-$32.13B"),
        ("4200000000", "USD", "change", "+$4.20B"),
        ("0.462063", "pure", None, "46.2%"),
        ("0.0070", "pure", "change", "+0.7 pp"),  # a ratio's change is points
        ("-0.028", "pure", "growth", "-2.8%"),
        ("0.19", "Rate", None, "19.0%"),
        ("1.02", "USD/shares", None, "$1.02"),
        ("15116786000", "shares", None, "15.12B shares"),
        ("8.5", "EUR", None, "€9"),
    ],
)
def test_display(value: str, unit: str, derivation: str | None, shown: str) -> None:
    assert display(Decimal(value), unit, derivation) == shown


def test_a_missing_derived_value_is_a_dash() -> None:
    assert display(None, "pure", "growth") == "—"
