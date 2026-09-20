"""The result contract: what comes back from ``app/retrieval/``.

Two halves, split by **who writes them**, and that split is the whole design.

``ResultRow``
    The SQL contract. Exactly the columns a generated statement must project,
    with these names. Produced by SQL -- by Qwen's, or by the deterministic
    retrieval that handles the bounded shapes -- and by nothing else.

``ResultSet``
    What the presenter reads: annotated rows, a verdict on whether the run
    delivered what the plan promised, citations, and the notes that must reach
    the reader. Assembled in Python. Nothing in it is taken from the model.

Three things the contract exists to make impossible, each of them a way of
producing a confident wrong number:

* **An uncheckable row count.** ``ResultSpec.row_count`` says a three-company,
  twelve-quarter chart is 36 rows. Nothing could check that before this module,
  because nothing said what a row was.
* **An inferred citation.** The presenter has to name the concept behind each
  figure, and with concept drift one company has two bindings for one element.
  ``binding_keys`` is resolved from the plan, so the attribution is looked up
  rather than guessed.
* **A derived value read as the metric it came from.** ``0.081 / pure`` against
  element ``revenue`` is revenue *growth*; with no marker a presenter renders
  "revenue: 0.08". ``derivation`` is that marker.

Full rationale: ``app/retrieval/DESIGN.md``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.query import (
    ConceptRef,
    Note,
    PeriodRule,
    QueryFiscalPeriod,
    ResolvedBy,
    _Base,
)

#: How the run compares with what the plan promised.
#:   "complete" -- every cell the plan named came back.
#:   "partial"  -- some did not. Whether that is answerable depends on whether
#:                 the plan had already disclosed the gap; see
#:                 ``ResultVerdict.is_answerable``.
#:   "over"     -- more rows than the plan named. A join fanned out, and every
#:                 aggregate over the result is suspect. As dangerous as
#:                 "partial" and much easier to miss.
#:   "empty"    -- nothing came back at all.
VerdictStatus = Literal["complete", "partial", "over", "empty"]


class ResultRow(_Base):
    """One value, as a generated statement must project it.

    Deliberately *not* carrying ``concept_id``: the tuple ``(element_id,
    company_cik, fiscal_year, fiscal_period)`` already resolves to exactly one
    ``Binding`` via ``QueryPlan.binding_for``, so provenance comes from the
    plan -- which is trustworthy -- rather than from a column carried through
    by the model, which is not. It also keeps the contract narrow, which is the
    surface Qwen can get wrong.

    ``fiscal_year`` / ``fiscal_period`` are projected from the plan rows
    injected into the statement, never read from the database: the
    ``xbrl.reported_fact`` view has no such column, because ``Filing``'s is
    provenance rather than the period a fact describes (PITFALLS §1.1).
    """

    #: Which ``QueryIn`` element this answers. Several elements share one
    #: result set, which is what makes one statement per question workable.
    element_id: str = Field(min_length=1, max_length=32)

    #: Identity. ``ticker`` and ``entity_name`` are for display only.
    company_cik: int
    ticker: str | None = Field(default=None, max_length=10)
    entity_name: str | None = Field(default=None, max_length=150)

    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_period: QueryFiscalPeriod

    #: The window the value actually covers. ``None`` exactly when the value is
    #: an instant -- the label and the dates are independent, and the dates are
    #: the ones a reader comparing two filers needs to see.
    period_start: date | None = None
    period_end: date
    is_instant: bool

    value: Decimal
    #: Part of the *key*, not decoration. Measured: dropping it from the join
    #: makes AMD's FY2024 effective tax rate two rows (filed as both ``pure``
    #: and ``Rate``), and summing them gives 0.38 for a 0.19 figure.
    unit: str = Field(min_length=1, max_length=32)

    #: ``None`` when the value is the bound metric exactly as filed. Otherwise
    #: a short name for what was computed -- "yoy_growth", "rank",
    #: "share_of_total". A free string rather than an enum for the same reason
    #: as ``Binding.expression``: the only reader is a language model reading
    #: it as text. What matters is that it is set *whenever the value is not
    #: the metric*, because a presenter with no marker cites it as one.
    derivation: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _instant_has_no_start(self) -> ResultRow:
        if self.is_instant and self.period_start is not None:
            raise ValueError(
                f"an instant value covers no span, but period_start="
                f"{self.period_start} was set"
            )
        if not self.is_instant and self.period_start is None:
            raise ValueError(
                "a duration value needs period_start; a missing one is how a "
                "90-day figure passes for a 365-day one"
            )
        if self.period_start is not None and not self.period_start < self.period_end:
            raise ValueError(
                f"expected period_start < period_end, got {self.period_start} / "
                f"{self.period_end}"
            )
        return self


#: The contract, as names. ``validate()`` compares a statement's projection
#: against this: a query that returns the right numbers under the wrong labels
#: is unusable, and finding that out in ``execute()`` is late.
RESULT_COLUMNS: tuple[str, ...] = tuple(ResultRow.model_fields)


class Citation(_Base):
    """What one binding contributed, said once.

    Keyed rather than repeated per row on purpose: ``rationale`` and
    ``ConceptRef.label`` are 512 characters each, and copying them onto 36 rows
    is ~18KB of duplicated prose into the presenter's context.
    """

    #: "b0", "b3" -- ``QueryPlan.binding_key`` of the binding's index.
    binding_key: str = Field(min_length=1, max_length=32)

    element_id: str = Field(min_length=1, max_length=32)
    company_cik: int | None = None

    #: What to cite, and the arithmetic over it.
    concepts: list[ConceptRef] = Field(min_length=1)
    expression: str = Field(min_length=1, max_length=256)
    period_rule: PeriodRule
    unit: str = Field(min_length=1, max_length=32)

    resolved_by: ResolvedBy
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=512)

    #: The binding's caveats. Carried here so a presenter reading a row can
    #: reach them through one key rather than being handed the whole plan.
    notes: list[Note] = Field(default_factory=list)


class AnnotatedRow(_Base):
    """A ``ResultRow`` after Python has attributed it.

    The only thing added is provenance, and Python adds it because the model
    cannot be trusted to.

    ``binding_keys`` is a **list** because a derived row can span bindings:
    Alphabet changes revenue tags mid-range, so a 2024→2025 growth figure is
    computed across two concepts and naming one of them would be a lie. An
    as-reported row has exactly one. An *empty* list means the row matched no
    binding at all -- the model invented it -- which the verdict refuses.
    """

    row: ResultRow
    binding_keys: list[str] = Field(default_factory=list)


class MissingCell(_Base):
    """A cell the plan named that did not come back.

    ``anticipated`` is the whole difference between a caveat and a refusal: a
    gap the plan already disclosed (a ``partial_coverage`` note) is not news,
    while one it did not means the statement is wrong in a way nobody
    understands.
    """

    element_id: str = Field(min_length=1, max_length=32)
    company_cik: int
    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_period: QueryFiscalPeriod
    anticipated: bool


class ResultVerdict(_Base):
    """Whether the run delivered what the plan promised."""

    status: VerdictStatus
    expected_rows: int = Field(ge=0)
    returned_rows: int = Field(ge=0)

    missing: list[MissingCell] = Field(default_factory=list)

    #: Indices into ``ResultSet.rows`` that matched no binding.
    unattributable: list[int] = Field(default_factory=list)

    @property
    def is_answerable(self) -> bool:
        """Answer only when the shortfall was already disclosed.

        Coverage is *proved* before a binding is made, so a cell missing after
        that proof is a fault in the query or the run, not in the filings --
        and every other row came out of the same statement, so partial trust
        is not on offer. A chart missing one of 36 points looks small, but if
        the mapper did not predict it the finding is "the statement is wrong in
        a way we do not understand", and 35 plausible dots is the shape this
        project exists to refuse.

        The converse matters as much: refusing on a gap the plan *did* disclose
        would make ``partial_coverage`` pointless -- it exists so the answer
        can go out with a caveat attached.
        """
        if self.unattributable:
            return False
        if self.status == "complete":
            return True
        if self.status == "partial":
            return all(cell.anticipated for cell in self.missing)
        return False

    @model_validator(mode="after")
    def _status_matches_counts(self) -> ResultVerdict:
        if (self.returned_rows == 0) != (self.status == "empty"):
            raise ValueError(
                f"status={self.status!r} disagrees with returned_rows="
                f"{self.returned_rows}: no rows is 'empty' and 'empty' is no rows"
            )
        if self.status == "over" and self.returned_rows <= self.expected_rows:
            raise ValueError(
                f"status='over' needs returned_rows > expected_rows, got "
                f"{self.returned_rows} <= {self.expected_rows}"
            )
        if self.status == "complete" and self.missing:
            raise ValueError(
                f"status='complete' with {len(self.missing)} missing cell(s)"
            )
        return self


class ResultSet(_Base):
    """Rows, provenance and a verdict -- everything the presenter reads."""

    version: Literal["1"] = "1"
    question: str = Field(min_length=1, max_length=2000)

    rows: list[AnnotatedRow] = Field(default_factory=list)
    verdict: ResultVerdict

    #: Keyed by ``binding_key``. See ``Citation``.
    citations: dict[str, Citation] = Field(default_factory=dict)

    #: Plan-level notes, plus the ``incomplete_result`` note an unanticipated
    #: shortfall raises. A binding's own notes travel on its ``Citation``.
    notes: list[Note] = Field(default_factory=list)

    #: No ``sql`` field. The statement that ran is appended to
    #: ``data/retrieval_log.jsonl`` instead: it is wanted for debugging, and it
    #: is the one thing in reach that a presenter might quote at a user.

    @property
    def is_answerable(self) -> bool:
        return self.verdict.is_answerable

    @model_validator(mode="after")
    def _internally_consistent(self) -> ResultSet:
        if self.verdict.returned_rows != len(self.rows):
            raise ValueError(
                f"verdict.returned_rows={self.verdict.returned_rows} but "
                f"{len(self.rows)} row(s) were carried"
            )
        unknown = {
            key
            for annotated in self.rows
            for key in annotated.binding_keys
            if key not in self.citations
        }
        if unknown:
            raise ValueError(
                f"row(s) cite binding key(s) with no citation: {sorted(unknown)}"
            )
        orphans = [
            index for index, annotated in enumerate(self.rows) if not annotated.binding_keys
        ]
        if orphans != sorted(self.verdict.unattributable):
            raise ValueError(
                f"verdict.unattributable={sorted(self.verdict.unattributable)} "
                f"disagrees with the rows carrying no binding: {orphans}"
            )
        return self
