"""[F] Presenter -- ``present(result, metrics) -> AnswerView``. Plain Python, no model.

See ``DESIGN.md`` beside this file.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.presenter.format import (
    PresentationError,
    condition,
    display,
    period_label,
    unit_kind,
)
from app.schemas.answer_view import (
    AnswerRow,
    AnswerView,
    BarView,
    CitationView,
    LineView,
    Series,
    StatView,
)
from app.schemas.result import AnnotatedRow, ResultSet

__all__ = ["BAR_LIMIT", "PresentationError", "present"]

#: Bars drawn for a ranking. The table below the chart holds every row.
BAR_LIMIT = 10

#: Arithmetic over time, written in Python (retrieval DESIGN §4.1).
OVER_TIME = frozenset({"change", "growth", "cagr"})

#: One figure over several companies, still written by the model (moving these
#: to Python: docs/FUTURE.md). The statement attaches it to every company it
#: spans, which a table would read as each company's own figure (DESIGN §4a).
AGGREGATES = frozenset(
    {"average", "mean", "median", "sum", "total", "min", "minimum", "max", "maximum"}
)


def present(result: ResultSet, metrics: Mapping[str, str]) -> AnswerView:
    """``metrics`` maps each answered element id to the asker's phrase for it."""
    if not result.is_answerable:
        # [B] checks first; this is the second lock on the same door (api DESIGN §1).
        raise PresentationError(f"verdict {result.verdict.status!r} is not answerable")

    rows = [_row(annotated, result, metrics) for annotated in result.rows]
    rows = _order(_collapse_aggregates(rows, result), result, metrics)
    conditions = _conditions(rows, result, metrics)
    rows, shown = _top_n(rows, result, metrics)
    conditions += shown

    shape = result.result.shape
    if len(rows) == 1:
        views = [StatView(title=_title(rows[0]), row=0)]  # one figure, whatever was asked
    elif shape == "ranking":
        views = _bars(rows, result, conditions)
    elif shape == "series":
        views = _lines(rows)
    elif shape == "table" and result.result.axes == ["company"]:
        views = _comparison(rows)
    else:
        views = []  # the table is the answer

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
        conditions=conditions,
    )


def _row(annotated: AnnotatedRow, result: ResultSet, metrics: Mapping[str, str]) -> AnswerRow:
    row = annotated.row
    if row.element_id not in metrics:
        # A figure for something the asker's phrases do not name has no label to wear.
        raise PresentationError(f"no metric phrase for element {row.element_id!r}")
    if row.derivation is not None and row.derivation not in OVER_TIME | AGGREGATES:
        # Refused rather than guessed: an unknown computation has no known reading.
        raise PresentationError(f"no display rule for a {row.derivation!r} row")

    multiple = any(result.citations[key].display_as == "multiple" for key in annotated.binding_keys)
    kind = unit_kind(row.unit)
    if kind == "ratio" and multiple and row.derivation not in ("growth", "cagr"):
        kind = "multiple"  # a growth in a current ratio is still a percentage
    base = annotated.base
    return AnswerRow(
        element_id=row.element_id,
        metric=metrics[row.element_id],
        company_cik=row.company_cik,
        company=row.ticker or row.entity_name or str(row.company_cik),
        fiscal_year=row.fiscal_year,
        fiscal_period=row.fiscal_period,
        period_label=period_label(row.fiscal_year, row.fiscal_period, instant=row.is_instant),
        granularity="annual" if row.fiscal_period == "FY" else "quarterly",
        period_start=row.period_start,
        period_end=row.period_end,
        is_instant=row.is_instant,
        value=row.value,
        display=display(row.value, row.unit, row.derivation, as_multiple=kind == "multiple"),
        unit=row.unit,
        unit_kind=kind,
        derivation=row.derivation,
        compared_with=period_label(base.fiscal_year, base.fiscal_period) if base else None,
        citations=annotated.binding_keys,
    )


#: What an across-companies row replaces rather than copies from its first member.
_SPAN_FIELDS = {"company_cik", "company", "companies", "period_start", "period_end", "citations"}


def _collapse_aggregates(rows: list[AnswerRow], result: ResultSet) -> list[AnswerRow]:
    """One row per aggregate figure, naming every company it spans.

    Only when it is provably one figure: the same value on every row, for every
    company the metric bound. Anything less is refused -- an "average" that
    differs by company is not a figure this can label honestly.
    """
    kept: list[AnswerRow] = []
    groups: dict[tuple, list[AnswerRow]] = {}
    for row in rows:
        if row.derivation in AGGREGATES:
            key = (row.element_id, row.derivation, row.fiscal_year, row.fiscal_period)
            groups.setdefault(key, []).append(row)
        else:
            kept.append(row)

    for (element_id, derivation, *_), members in groups.items():
        ciks = sorted({row.company_cik for row in members})
        if len(ciks) == 1:
            kept += members  # one company's own aggregate is that company's figure
            continue
        bound = {c.company_cik for c in result.citations.values() if c.element_id == element_id}
        if None not in bound and set(ciks) != bound:
            raise PresentationError(
                f"{derivation!r} rows cover {len(ciks)} of {len(bound)} companies"
            )
        if len({row.value for row in members}) != 1 or len(members) != len(ciks):
            raise PresentationError(f"{derivation!r} differs across the companies it spans")
        first = members[0]
        starts = [row.period_start for row in members if row.period_start is not None]
        kept.append(
            AnswerRow(
                **first.model_dump(exclude=_SPAN_FIELDS),
                company_cik=None,
                company=f"{len(ciks)} companies",
                companies=sorted(row.company for row in members),
                # The widest window: the companies' fiscal years differ (period_misalignment).
                period_start=min(starts) if starts else None,
                period_end=max(row.period_end for row in members),
                citations=sorted({key for row in members for key in row.citations}, key=_key_order),
            )
        )
    return kept


def _order(rows: list[AnswerRow], result: ResultSet, metrics: Mapping[str, str]) -> list[AnswerRow]:
    """Metrics in the asker's order. A ranked metric in rank order; anything
    else by company, then filed before derived, annual before quarterly, date."""
    position = {element_id: i for i, element_id in enumerate(metrics)}
    ordered: list[AnswerRow] = []
    for element_id in sorted({row.element_id for row in rows}, key=position.__getitem__):
        mine = [row for row in rows if row.element_id == element_id]
        direction = result.result.rank.get(element_id)
        if direction is not None:
            # Sorted here, not trusted from the statement (docs/GAPS.md G7).
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


def _top_n(
    rows: list[AnswerRow], result: ResultSet, metrics: Mapping[str, str]
) -> tuple[list[AnswerRow], list[str]]:
    """Only the top N of a ranking the question put a count on, and a sentence
    saying how many there were.

    Cut here, after the whole ranking has been ordered, so the rows kept are the
    top because everything else was ranked below them. A row with no value sorts
    last (``_order``), so it is only kept when the count reaches it.
    """
    kept: list[AnswerRow] = []
    stated: list[str] = []
    seen: dict[str, int] = {}
    for row in rows:
        limit = result.result.top_n.get(row.element_id)
        seen[row.element_id] = seen.get(row.element_id, 0) + 1
        if limit is None or seen[row.element_id] <= limit:
            kept.append(row)
    for element_id, limit in result.result.top_n.items():
        total = seen.get(element_id, 0)
        if total > limit:
            stated.append(f"showing the first {limit} of {total} in the ranking")
    return kept, stated


def _conditions(rows: list[AnswerRow], result: ResultSet, metrics: Mapping[str, str]) -> list[str]:
    """Each threshold, stated: a filtered list must say what it was filtered by."""
    stated = []
    for threshold in result.result.thresholds:
        unit = next((row.unit for row in rows if row.element_id == threshold.element_id), None)
        if unit is None:
            continue  # nothing passed the bar: there are no rows for it to describe
        metric = metrics.get(threshold.element_id, threshold.element_text)
        stated.append(condition(metric, threshold.comparison, threshold.value, unit))
    return stated


def _bars(rows: list[AnswerRow], result: ResultSet, conditions: list[str]) -> list[BarView]:
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
        title = f"{_name(rows[shown[0]])} — {direction} first"
        if conditions:
            title += f" ({'; '.join(conditions)})"
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


def _comparison(rows: list[AnswerRow]) -> list[BarView]:
    """Bars for companies side by side on one figure -- "Apple or Microsoft?".

    Sorted largest first for reading, which is not a ranking: the title says
    what is compared, never "highest first".
    """
    if len({(row.element_id, row.derivation, row.period_label) for row in rows}) != 1:
        return []  # several figures per company: the table says it better
    if len({row.unit_kind for row in rows}) != 1 or any(row.value is None for row in rows):
        return []
    order = sorted(range(len(rows)), key=lambda i: rows[i].value, reverse=True)
    first = rows[order[0]]
    return [
        BarView(
            title=f"{_name(first)}, {first.period_label}",
            unit_kind=first.unit_kind,
            order="descending",
            rows=order,
        )
    ]


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
    when = f"as of {row.period_end.isoformat()}" if row.is_instant else row.period_label
    if row.compared_with:
        when += f" vs {row.compared_with}"  # a change says what it is measured from
    if row.companies:
        return f"{_name(row)} across {row.company}, {when}"  # "R&D spend average across 14 …"
    return f"{row.company} — {_name(row)}, {when}"


def _key_order(key: str) -> int:
    return int(key[1:])  # "b10" after "b9"
