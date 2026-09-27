"""``AnswerView`` -- the Presenter's output, and what the Web Client renders.

Built in Python from a ``ResultSet``, never by a model. ``app/api/DESIGN.md``
§2, §5, §6.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.query import (
    ConceptRef,
    Note,
    PeriodGranularity,
    QueryFiscalPeriod,
    ResolvedBy,
    ResultShape,
    _Base,
)

#: How a unit is formatted. A ratio is shown as a percentage, never as money
#: (PITFALLS §2.1).
UnitKind = Literal["money", "ratio", "per_share", "count"]


class AnswerRow(_Base):
    """One figure: a row of the table, and a point or bar in any view."""

    element_id: str = Field(min_length=1, max_length=32)  # the Reply part it answers
    metric: str = Field(min_length=1, max_length=256)  # the asker's phrase, verbatim

    company_cik: int
    company: str = Field(min_length=1, max_length=150)  # ticker, else entity name

    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_period: QueryFiscalPeriod
    period_label: str = Field(min_length=1, max_length=32)  # "FY2024", "Q4 FY2024"
    granularity: PeriodGranularity
    period_start: date | None = None  # None exactly when is_instant
    period_end: date  # a line's x value: real dates, not labels (§5)
    is_instant: bool

    value: Decimal | None = None  # exact; JSON carries it as a string
    display: str = Field(min_length=1, max_length=64)  # "$391.04B", "46.2%", "—"
    unit: str = Field(min_length=1, max_length=32)  # as filed: "USD", "pure", ...
    unit_kind: UnitKind
    derivation: str | None = Field(default=None, max_length=64)  # None = as filed

    citations: list[str] = Field(min_length=1)  # keys into AnswerView.citations

    @model_validator(mode="after")
    def _consistent(self) -> AnswerRow:
        if (self.period_start is None) != self.is_instant:
            raise ValueError("period_start must be None exactly when is_instant")
        if self.value is None and self.derivation is None:
            # Same rule as ResultRow: only a computed row may lack a value.
            raise ValueError("an as-filed row must carry a value")
        return self


class CitationView(_Base):
    """What one binding contributed, for the "answered as" panel (§6)."""

    key: str = Field(min_length=1, max_length=32)  # "b0" -- matches AnswerRow.citations
    concepts: list[ConceptRef] = Field(min_length=1)  # operands c0, c1, ... in order
    expression: str = Field(min_length=1, max_length=256)  # "c0", "c0 / c1"
    unit: str = Field(min_length=1, max_length=32)
    resolved_by: ResolvedBy  # "embedding" is shown as unreviewed
    notes: list[Note] = Field(default_factory=list)  # this binding's caveats, verbatim


class Series(_Base):
    """One line of a line view."""

    label: str = Field(min_length=1, max_length=256)  # "Apple — revenue"
    rows: list[int] = Field(min_length=1)  # indices into AnswerView.rows


class StatView(_Base):
    kind: Literal["stat"] = "stat"
    title: str = Field(min_length=1, max_length=256)
    row: int  # index into AnswerView.rows


class LineView(_Base):
    kind: Literal["line"] = "line"
    title: str = Field(min_length=1, max_length=256)
    unit_kind: UnitKind  # one axis, one kind of unit
    granularity: PeriodGranularity  # annual and quarterly never share a panel
    series: list[Series] = Field(min_length=1)


class BarView(_Base):
    kind: Literal["bar"] = "bar"
    title: str = Field(min_length=1, max_length=256)
    unit_kind: UnitKind
    order: Literal["descending", "ascending"]
    rows: list[int] = Field(min_length=1)  # indices into AnswerView.rows, in bar order


View = Annotated[StatView | LineView | BarView, Field(discriminator="kind")]


class AnswerView(_Base):
    """Everything the Web Client draws for the answered parts of a reply."""

    shape: ResultShape  # as the plan resolved it; the views follow from it
    rows: list[AnswerRow] = Field(min_length=1)  # the table: always rendered (§5)
    views: list[View] = Field(default_factory=list)  # panels above the table
    citations: dict[str, CitationView] = Field(min_length=1)
    notes: list[Note] = Field(default_factory=list)  # shown above the views, verbatim

    @model_validator(mode="after")
    def _consistent(self) -> AnswerView:
        for key, citation in self.citations.items():
            if citation.key != key:
                raise ValueError(f"citation filed under {key!r} says it is {citation.key!r}")
        for index, row in enumerate(self.rows):
            unknown = set(row.citations) - self.citations.keys()
            if unknown:
                raise ValueError(f"row {index} cites unknown key(s) {sorted(unknown)}")
        for view in self.views:
            self._check_view(view)
        return self

    def _row(self, index: int) -> AnswerRow:
        if not 0 <= index < len(self.rows):
            raise ValueError(f"a view points at row {index}; there are {len(self.rows)}")
        return self.rows[index]

    def _check_view(self, view: StatView | LineView | BarView) -> None:
        if isinstance(view, StatView):
            self._row(view.row)
            return

        if isinstance(view, LineView):
            indices = [i for series in view.series for i in series.rows]
        else:
            indices = view.rows
        rows = [self._row(i) for i in indices]
        if any(row.unit_kind != view.unit_kind for row in rows):
            # A ratio drawn on a money axis is the 46-cent gross margin.
            raise ValueError(f"{view.kind} view {view.title!r} mixes unit kinds")

        if isinstance(view, LineView):
            if any(row.granularity != view.granularity for row in rows):
                raise ValueError(f"line view {view.title!r} mixes annual and quarterly")
            for series in view.series:
                ends = [self.rows[i].period_end for i in series.rows]
                if ends != sorted(ends):
                    raise ValueError(f"series {series.label!r} is not in date order")
            return

        values = [row.value for row in rows]
        if any(value is None for value in values):
            raise ValueError(f"bar view {view.title!r} has a row with no value to rank")
        # Nothing upstream checks a ranking's order (retrieval DESIGN §9); here it
        # cannot be drawn out of order.
        if values != sorted(values, reverse=view.order == "descending"):
            raise ValueError(f"bar view {view.title!r} is not in {view.order} order")
