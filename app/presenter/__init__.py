"""[F] Presenter -- ``present(result, metrics) -> AnswerView``. Plain Python, no model.

See ``DESIGN.md`` beside this file.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.presenter.format import PresentationError, display, period_label, unit_kind
from app.schemas.answer_view import (
    AnswerRow,
    AnswerView,
    BarView,
    CitationView,
    LineView,
    Series,
    StatView,
)
from app.schemas.result import ResultSet

__all__ = ["BAR_LIMIT", "PresentationError", "present"]

#: Bars drawn for a ranking. The table below the chart holds every row.
BAR_LIMIT = 10


def present(result: ResultSet, metrics: Mapping[str, str]) -> AnswerView:
    """``metrics`` maps each answered element id to the asker's phrase for it."""
    if not result.is_answerable:
        # [B] checks first; this is the second lock on the same door (api DESIGN §1).
        raise PresentationError(f"verdict {result.verdict.status!r} is not answerable")

    rows = _order([_row(annotated, metrics) for annotated in result.rows], result, metrics)
    shape = result.result.shape
    if shape == "scalar":
        views = [StatView(title=_title(rows[0]), row=0)] if len(rows) == 1 else []
    elif shape == "ranking":
        views = _bars(rows, result)
    elif shape == "series":
        views = _lines(rows)
    else:
        views = []  # "table": the table is the answer

    return AnswerView(
        shape=shape,
        rows=rows,
        views=views,
        citations={
            key: CitationView(
                key=key,
                concepts=citation.concepts,
                expression=citation.expression,
                unit=citation.unit,
                resolved_by=citation.resolved_by,
                notes=citation.notes,
            )
            for key, citation in result.citations.items()
        },
        notes=result.notes,  # verbatim: never summarised, never dropped
    )


def _row(annotated, metrics: Mapping[str, str]) -> AnswerRow:
    row = annotated.row
    if row.element_id not in metrics:
        # A figure for something the asker's phrases do not name has no label to wear.
        raise PresentationError(f"no metric phrase for element {row.element_id!r}")
    return AnswerRow(
        element_id=row.element_id,
        metric=metrics[row.element_id],
        company_cik=row.company_cik,
        company=row.ticker or row.entity_name or str(row.company_cik),
        fiscal_year=row.fiscal_year,
        fiscal_period=row.fiscal_period,
        period_label=period_label(row.fiscal_year, row.fiscal_period),
        granularity="annual" if row.fiscal_period == "FY" else "quarterly",
        period_start=row.period_start,
        period_end=row.period_end,
        is_instant=row.is_instant,
        value=row.value,
        display=display(row.value, row.unit, row.derivation),
        unit=row.unit,
        unit_kind=unit_kind(row.unit),
        derivation=row.derivation,
        citations=annotated.binding_keys,
    )


def _order(rows: list[AnswerRow], result: ResultSet, metrics: Mapping[str, str]) -> list[AnswerRow]:
    """Metrics in the asker's order. A ranked metric in rank order; anything
    else by company, then filed before derived, annual before quarterly, date."""
    position = {element_id: i for i, element_id in enumerate(metrics)}
    ordered: list[AnswerRow] = []
    for element_id in sorted({row.element_id for row in rows}, key=position.__getitem__):
        mine = [row for row in rows if row.element_id == element_id]
        direction = result.result.rank.get(element_id)
        if direction is not None:
            # Sorted here, not trusted from the statement (retrieval DESIGN §9).
            # A row with no value cannot be ranked, so it goes last.
            valued = sorted(
                (row for row in mine if row.value is not None),
                key=lambda row: row.value,
                reverse=direction == "highest",
            )
            ordered += valued + [row for row in mine if row.value is None]
        else:
            ordered += sorted(
                mine,
                key=lambda row: (
                    row.company,
                    row.derivation is not None,
                    row.derivation or "",
                    row.granularity != "annual",
                    row.period_end,
                ),
            )
    return ordered


def _bars(rows: list[AnswerRow], result: ResultSet) -> list[BarView]:
    views = []
    for element_id, direction in result.result.rank.items():
        ranked = [i for i, row in enumerate(rows) if row.element_id == element_id]
        valued = [i for i in ranked if rows[i].value is not None]
        if not valued:
            continue
        kinds = {rows[i].unit_kind for i in valued}
        if len(kinds) != 1:
            raise PresentationError(f"ranking {element_id!r} mixes unit kinds {sorted(kinds)}")
        shown = valued[:BAR_LIMIT]
        first = rows[shown[0]]
        title = f"{_name(first)} — {direction} first"
        if len(valued) > len(shown):
            title += f", top {len(shown)} of {len(valued)}"
        views.append(
            BarView(
                title=title,
                unit_kind=kinds.pop(),
                order="descending" if direction == "highest" else "ascending",
                rows=shown,
            )
        )
    return views


def _lines(rows: list[AnswerRow]) -> list[LineView]:
    """One panel per (derivation, unit kind, granularity): a panel never mixes
    a figure with its growth, money with a ratio, or a year with a quarter."""
    panels: dict[tuple, list[int]] = {}
    for i, row in enumerate(rows):
        panels.setdefault((row.derivation, row.unit_kind, row.granularity), []).append(i)

    views = []
    for (_, kind, granularity), indices in sorted(
        panels.items(), key=lambda item: (item[0][0] is not None, item[0][2] != "annual")
    ):
        lines: dict[tuple, list[int]] = {}
        for i in indices:
            lines.setdefault((rows[i].element_id, rows[i].company_cik), []).append(i)
        series = [
            Series(
                label=f"{rows[members[0]].company} — {_name(rows[members[0]])}",
                rows=sorted(members, key=lambda i: rows[i].period_end),
            )
            for members in lines.values()
        ]
        names = dict.fromkeys(_name(rows[i]) for i in indices)  # ordered, unique
        views.append(
            LineView(
                title=f"{', '.join(names)} — {granularity}",
                unit_kind=kind,
                granularity=granularity,
                series=series,
            )
        )
    return views


def _name(row: AnswerRow) -> str:
    return f"{row.metric} {row.derivation}" if row.derivation else row.metric  # "revenue growth"


def _title(row: AnswerRow) -> str:
    return f"{row.company} — {_name(row)}, {row.period_label}"
