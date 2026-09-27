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
from app.presenter.format import condition, display
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
    # The table drops nothing: an across-companies row stands for one row per company.
    assert sum(len(row.companies) or 1 for row in view.rows) == len(result.rows)
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


def test_companies_side_by_side_get_comparison_bars() -> None:
    view = _present("q017")  # Q4 revenue across Apple, Microsoft and NVIDIA
    (bar,) = view.views
    assert view.shape == "table" and bar.kind == "bar"
    assert [view.rows[i].company for i in bar.rows] == ["AAPL", "MSFT", "NVDA"]  # largest first
    assert "first" not in bar.title  # a comparison, not a ranking: "revenue, Q4 FY2025"


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
# What a reader needs besides the figure (2026-09-27 sweep of every eval question)
# --------------------------------------------------------------------------- #


def test_an_average_across_companies_is_one_figure_not_one_per_company() -> None:
    """q040 returns the average on 14 rows, one per company. As a table that
    reads as each company spending $15.27B -- a wrong number made by display."""
    view = _present("q040")
    (row,) = view.rows
    assert row.company_cik is None and row.company == "14 companies"
    assert len(row.companies) == 14 and "AAPL" in row.companies
    assert row.display == "$15.27B" and row.derivation == "average"
    assert view.views[0].title.startswith("R&D spend average across 14 companies")


def test_an_average_that_differs_by_company_is_refused() -> None:
    result, metrics = _load("q040")
    first = result.rows[0]
    changed = first.model_copy(update={"row": first.row.model_copy(update={"value": Decimal("1")})})
    rows = [changed, *result.rows[1:]]
    with pytest.raises(PresentationError, match="differs across the companies"):
        present(result.model_copy(update={"rows": rows}), metrics)


def test_an_average_missing_a_company_is_refused() -> None:
    result, metrics = _load("q040")
    with pytest.raises(PresentationError, match="cover 13 of 14 companies"):
        present(result.model_copy(update={"rows": result.rows[1:]}), metrics)


def test_an_unknown_computation_is_refused_rather_than_guessed() -> None:
    result, metrics = _load("q001")
    first = result.rows[0]
    odd = first.model_copy(update={"row": first.row.model_copy(update={"derivation": "vibes"})})
    with pytest.raises(PresentationError, match="no display rule for a 'vibes' row"):
        present(result.model_copy(update={"rows": [odd]}), metrics)


def test_a_current_ratio_is_a_multiple_not_a_percentage() -> None:
    result, _ = _load("q022")
    assert result.citations["b0"].display_as == "multiple"  # curated, carried from the alias
    row = _present("q022").rows[0]
    assert (row.display, row.unit_kind) == ("0.89×", "multiple")


def test_a_filtered_list_states_its_filter() -> None:
    view = _present("q039")  # more than 100 billion dollars in revenue
    assert view.conditions == ["revenue over $100.00B"]
    assert "revenue over $100.00B" in view.views[0].title


def test_a_change_says_what_it_is_measured_from() -> None:
    view = _present("q020")  # Q3 to Q4
    assert view.rows[0].compared_with == "Q3 FY2025"
    assert view.views[0].title.endswith("Q4 FY2025 vs Q3 FY2025")
    assert _present("q011").rows[0].compared_with == "FY2023"  # growth in 2024


def test_only_derived_rows_carry_a_base() -> None:
    rows = _present("q013").rows  # five figures and four growths
    assert [row.compared_with for row in rows if row.derivation] == [
        "FY2021", "FY2022", "FY2023", "FY2024",
    ]
    assert all(row.compared_with is None for row in rows if not row.derivation)


def test_a_two_company_comparison_gets_bars() -> None:
    view = _present("q006")  # Apple or Microsoft, FY2024 revenue
    (bar,) = view.views
    assert [view.rows[i].company for i in bar.rows] == ["AAPL", "MSFT"]
    assert bar.title == "revenue, FY2024"


def test_a_balance_is_labelled_by_its_date() -> None:
    view = _present("q002")  # Microsoft's cash at the end of its last fiscal year
    row = view.rows[0]
    assert row.period_label == "end of FY2025"
    assert view.views[0].title.endswith("as of 2025-06-30")


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


@pytest.mark.parametrize(
    ("value", "derivation", "shown"),
    [
        ("0.893293", None, "0.89×"),
        ("0.05", "change", "+0.05×"),  # a change in a multiple is a multiple
        ("0.12", "growth", "+12.0%"),  # a growth of one is still a rate
    ],
)
def test_display_as_a_multiple(value: str, derivation: str | None, shown: str) -> None:
    assert display(Decimal(value), "pure", derivation, as_multiple=True) == shown


def test_a_condition_reads_as_a_sentence() -> None:
    assert condition("revenue", "gt", Decimal("100000000000"), "USD") == "revenue over $100.00B"
    assert condition("gross margin", "lte", Decimal("0.3"), "pure") == "gross margin at most 30.0%"


def test_a_missing_derived_value_is_a_dash() -> None:
    assert display(None, "pure", "growth") == "—"
