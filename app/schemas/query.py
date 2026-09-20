"""Pydantic v2 schemas for the two ends of the query mapper
(``app/semantic/query_mapper.py``).

``QueryIn``   -- a user's question, already parsed into elements by whatever
                produced it (an external LLM today; the producer is
                deliberately not this project's concern). Inbound, validated.

``QueryPlan`` -- what the mapper resolves that question into: concrete
                ``Concept`` ids, ciks and fiscal years, plus the fact-level
                coordinates (``unit``, ``is_instant``) needed to write correct
                SQL. Outbound, constructed by us, handed to the SQL-generating
                model.

Full rationale for every choice here: ``app/schemas/DESIGN.md`` §8. In brief:

* ``QueryIn`` speaks the *user's* language -- "revenue", never
  ``us-gaap:Revenues``. Translating one into the other is the mapper's whole
  job, so an XBRL identifier appearing on this side means the boundary leaked.
* elements are a **discriminated union on ``kind``**, because a company name,
  a fiscal period and a metric each need a different resolver -- embedding
  search over "Apple" returns noise.
* a binding is keyed on ``(element_id, company_cik)``, not ``element_id``:
  filers tag the same business concept differently, so one element legitimately
  resolves to different concepts for different companies.
* ``Binding.concepts`` is a list with an ``expression`` over it, because some
  metrics ("gross margin") are arithmetic over several concepts and exist as no
  single ``Concept`` row.
* ``coverage`` carries the evidence that a binding is real -- facts actually
  exist for that ``(cik, concept)``. Similarity is a prior; coverage is proof.
* unresolved / ambiguous elements are first-class fields, not exceptions: a
  half-resolved plan is a normal outcome the caller must be able to inspect.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.xbrl import FilingForm, Taxonomy

#: How the question wants its data shaped. A *hint* from the producer, not a
#: contract: ``question`` stays the authority and the SQL step may ignore this.
#: Kept as a Literal anyway so a typo fails loudly instead of silently meaning
#: nothing downstream.
Intent = Literal["lookup", "compare", "trend", "rank", "derive"]

#: Which cascade step produced a binding. Logged per binding so the alias
#: hit-rate is measurable over time -- as the curated layer absorbs cases, the
#: share resolved by "embedding" should fall.
ResolvedBy = Literal["alias", "embedding"]

#: What a question may *ask* for. Deliberately wider than the storage-level
#: ``FiscalPeriod``: no US filer files a Q4 (the 10-K covers it, so the store
#: holds zero Q4 filings), but "compare their Q4s" is a perfectly ordinary
#: question. Translating an askable period into storable ones is the mapper's
#: job -- rejecting Q4 at the boundary would just make it unanswerable.
QueryFiscalPeriod = Literal["FY", "Q1", "Q2", "Q3", "Q4"]

#: How a binding gets its value out of the facts.
#:   "direct"   -- read the fact at the window; what every stored period uses.
#:   "residual" -- subtract one window from another (Q4 = annual - 9-month YTD).
#: Only ever describes the arithmetic; the SQL step performs it.
PeriodRule = Literal["direct", "residual"]

#: What the answer has to *be*, which decides how much data has to come back.
#: A chart needs a point per period per company; a single figure needs one row.
#: Nothing here describes drawing -- only cardinality.
#:   "scalar"  -- one number.
#:   "series"  -- one metric over an ordered axis, per entity. Charts live here.
#:   "table"   -- several metrics side by side.
#:   "ranking" -- entities ordered by one metric.
ResultShape = Literal["scalar", "series", "table", "ranking"]

#: Annual and quarterly figures are not interchangeable points. A 363-day
#: value and a 90-day value on one axis reads as a fourfold spike, so the two
#: are tracked apart rather than merged into an undifferentiated "period".
PeriodGranularity = Literal["annual", "quarterly"]

#: A dimension the result varies along. The retrieval step must not collapse
#: these: "revenue by quarter for three companies" varies along both, and
#: returning one row per company would silently answer a different question.
ResultAxis = Literal["company", "period", "metric"]

#: Caveats a binding can carry. The answer is computable, but something about
#: it should reach the reader rather than being smoothed over. See PITFALLS.md.
#:   "concept_switch"     -- the filer changed tags mid-range and the two agree
#:                           where they overlap, so the series was stitched.
#:   "unverified_switch"  -- same, but there is no overlapping period to check
#:                           the seam against, or the overlap disagrees.
#:   "partial_coverage"   -- some requested periods have no facts and are absent
#:                           from this binding.
#:   "period_misalignment" -- companies being compared put very different dates
#:                           under the same fiscal label. Plan-level.
#:   "mixed_granularity"  -- annual and quarterly figures in one result.
#:                           Plan-level.
#:   "narrower_than_asked" -- the bound concept measures a *subset* of what the
#:                           phrase names, because no filed concept covers the
#:                           whole of it. Curated, not inferred: a person wrote
#:                           down what is missing. "Total debt" resolving to
#:                           long-term debt is the case it exists for -- Apple
#:                           carries $8.0B of commercial paper outside that
#:                           figure, so the number is 8% light and nothing else
#:                           in the plan would say so.
#:   "incomplete_result"  -- rows the plan promised did not come back. Made
#:                           *after* execution, unlike every other kind here:
#:                           the plan proved the facts exist, so a shortfall is
#:                           a fault in the query or the run, not in the data.
#:                           Distinct from "partial_coverage", which is the
#:                           plan saying up front that some periods have no
#:                           facts at all.
NoteKind = Literal[
    "concept_switch",
    "unverified_switch",
    "partial_coverage",
    "period_misalignment",
    "mixed_granularity",
    "narrower_than_asked",
    "incomplete_result",
]

#: Matches a concept reference inside ``Binding.expression`` -- "c0", "c1", ...
_CONCEPT_REF = re.compile(r"c(\d+)")


class _Base(BaseModel):
    """Shared config, same rules as ``xbrl.py``'s ``_Base``.

    ``extra="forbid"`` matters more here than it does there: the inbound
    producer is a language model, and a silently-dropped key it believed it was
    sending is far worse to debug than a loud ``ValidationError`` it can be
    shown and asked to correct.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Inbound: the query object
# --------------------------------------------------------------------------- #


class _ElementBase(_Base):
    #: Stable handle ("e1"). ``QueryPlan`` references elements by this, so it
    #: has to survive the round trip and be unique within one ``QueryIn``.
    id: str = Field(min_length=1, max_length=32)

    #: The span as the user actually said it -- "revenue", "Apple", "last
    #: 5 years". Not normalized, not translated: the mapper wants the raw
    #: phrasing to embed and the human wants it to debug against.
    text: str = Field(min_length=1, max_length=256)


class MetricElementIn(_ElementBase):
    """Something measurable: "revenue", "gross margin", "headcount".

    Carries no payload -- ``text`` *is* the input to the resolver. This is the
    only kind that reaches the alias/embedding cascade.
    """

    kind: Literal["metric"] = "metric"


class CompanyElementIn(_ElementBase):
    """A filer. Resolved by deterministic lookup, never by embedding.

    Both hints are optional because the producer may only have the surface form
    ("the iPhone maker"); the mapper falls back to ``text`` when neither is set.
    """

    kind: Literal["company"] = "company"
    ticker: str | None = Field(default=None, max_length=10)  # mirrors Company.ticker
    name: str | None = Field(default=None, max_length=150)  # mirrors Company.entity_name


class PeriodElementIn(_ElementBase):
    """A time span. Either absolute (``fiscal_year`` / ``fiscal_period``) or
    relative (``last_n_years``); the mapper turns both into concrete years.

    ``fiscal_period`` uses ``QueryFiscalPeriod``, which accepts "Q4" even
    though no Q4 filing exists -- see that alias. The mapper turns it into the
    windows a Q4 can be computed from.
    """

    kind: Literal["period"] = "period"
    fiscal_year: int | None = Field(default=None, ge=2000, le=2100)
    fiscal_period: QueryFiscalPeriod | None = None
    last_n_years: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def _absolute_or_relative_not_both(self) -> PeriodElementIn:
        if self.last_n_years is not None and self.fiscal_year is not None:
            raise ValueError("set fiscal_year or last_n_years, not both")
        return self


class CompanyGroupElementIn(_ElementBase):
    """A *set* of filers chosen by attribute rather than named one by one --
    "every company in semiconductors", "all the ones in the same SIC office".

    Separate from ``CompanyElementIn`` on purpose: naming one filer and
    selecting a population are different operations with different failure
    modes, and collapsing them would make "which company" and "which companies"
    the same request.

    All three selectors are optional and at least one must be set. The columns
    behind them (``Company.sic_code`` / ``sic_description`` / ``sic_office``)
    exist but are **not populated yet**, so this currently resolves to nothing
    and the mapper says so rather than returning an empty set silently.
    """

    kind: Literal["company_group"] = "company_group"

    #: Exact 4-digit SIC code.
    sic_code: str | None = Field(default=None, min_length=1, max_length=4)

    #: Substring match against the SEC's ``sicDescription`` -- "semiconductor"
    #: rather than "3674", since that is how people ask.
    sic_description: str | None = Field(default=None, min_length=1, max_length=120)

    #: SEC review office. No source populates this today; see Company.sic_office.
    sic_office: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def _at_least_one_selector(self) -> CompanyGroupElementIn:
        if not any((self.sic_code, self.sic_description, self.sic_office)):
            raise ValueError(
                "a company group needs at least one of sic_code, sic_description "
                "or sic_office; an unconstrained group is every loaded filer, "
                "which is what omitting the element already means"
            )
        return self


ElementIn = Annotated[
    MetricElementIn | CompanyElementIn | CompanyGroupElementIn | PeriodElementIn,
    Field(discriminator="kind"),
]


class QueryIn(_Base):
    """One parsed user question, on the way into the mapper."""

    version: Literal["1"] = "1"
    question: str = Field(min_length=1, max_length=2000)
    intent: Intent
    elements: list[ElementIn] = Field(min_length=1)

    #: What the asker wants back, when the question says so -- "show me
    #: visually" or "chart this" means a series, and a series needs a point per
    #: period rather than one summary figure. Left unset when the question does
    #: not indicate; the mapper then infers it from what actually resolved.
    shape: ResultShape | None = None

    @model_validator(mode="after")
    def _element_ids_unique(self) -> QueryIn:
        seen = set()
        for element in self.elements:
            if element.id in seen:
                raise ValueError(f"duplicate element id {element.id!r}")
            seen.add(element.id)
        return self


# --------------------------------------------------------------------------- #
# Outbound: the query plan
# --------------------------------------------------------------------------- #


class ConceptRef(_Base):
    """One resolved ``Concept``, denormalized enough that the SQL step never
    has to look it up to know what it is."""

    concept_id: int
    taxonomy: Taxonomy
    name: str = Field(min_length=1, max_length=255)

    #: The taxonomy's human label, carried so a refusal can offer "Revenues"
    #: as a choice rather than making someone read
    #: ``RevenueFromContractWithCustomerExcludingAssessedTax``. Null for the
    #: ~200 concepts that have no label.
    label: str | None = Field(default=None, max_length=512)


class ComponentCoverage(_Base):
    """Coverage for one window a *derived* value is computed from."""

    period_start: date
    period_end: date
    fact_count: int = Field(ge=0)


class Coverage(_Base):
    """Proof that a binding points at facts that exist.

    ``fact_count == 0`` means the binding is wrong -- an empty result set from
    a well-formed query reads identically to "the company reported nothing",
    which is the failure mode this field exists to make impossible.

    ``components`` exists because that whole-concept count is *not enough for a
    derived value*. Measured: NVIDIA carries four annual facts under the
    revenue concept Apple and Microsoft use quarterly, and zero nine-month
    ones. A Q4 binding to it passes a concept-level count and then cannot be
    computed -- so a residual binding has to prove each window separately.
    """

    fact_count: int = Field(ge=0)
    period_min: date | None = None
    period_max: date | None = None

    #: One entry per window a residual depends on. Empty for a direct binding.
    components: list[ComponentCoverage] = Field(default_factory=list)


class Note(_Base):
    """A caveat that must survive all the way to the reader.

    Distinct from ``Unresolved``: the binding *works*, but presenting its
    numbers without this sentence would mislead. The user-facing model is
    expected to pass these on rather than summarize them away.
    """

    kind: NoteKind
    message: str = Field(min_length=1, max_length=512)


class PeriodRef(_Base):
    """Points at one ``ResolvedPeriod`` in ``PlanFilters``; the company comes
    from the ``Binding`` that carries it."""

    fiscal_year: int
    fiscal_period: QueryFiscalPeriod


class Binding(_Base):
    """One element, resolved, for one company, over the periods it covers.

    ``company_cik=None`` means the binding holds for every cik in
    ``QueryPlan.filters``; a per-company binding overrides it. That split is
    what lets one "revenue" element resolve to ``Revenues`` for one filer and
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` for another.

    ``periods`` narrows it further, because a filer can change tags *during*
    the range asked about. Alphabet reports revenue under
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` through FY2024 and
    ``Revenues`` in FY2025, so a five-year question yields two bindings for one
    company, each naming the periods it answers. One binding per element per
    company would have had to pick a concept that covers everything, and there
    isn't one -- see PITFALLS.md.
    """

    element_id: str = Field(min_length=1, max_length=32)
    company_cik: int | None = None

    #: The periods this binding answers for, as keys into
    #: ``PlanFilters.periods``. Empty means every period in scope.
    periods: list[PeriodRef] = Field(default_factory=list)

    #: Operands for ``expression``, positionally: ``concepts[0]`` is "c0".
    concepts: list[ConceptRef] = Field(min_length=1)

    #: Arithmetic over the operands -- "c0" for the ordinary single-concept
    #: case, "c0 / c1" for a ratio like gross margin. A string rather than a
    #: parsed tree on purpose: the only consumer today is a language model that
    #: reads it as text, and a real AST can replace this the moment something
    #: needs to evaluate it.
    expression: str = Field(default="c0", min_length=1, max_length=256)

    #: The unit of the binding's **result**. For a single-operand binding that
    #: is the facts' own unit; for a ratio it is ``pure``, because dividing
    #: like by like is dimensionless (PITFALLS §2.1 -- copying the lead
    #: operand's unit through made a 0.46 gross margin report as "USD", which
    #: any formatter renders as 46 cents).
    unit: str = Field(min_length=1, max_length=32)  # mirrors Fact.unit

    #: The unit the **operands are filed in**, when that differs from the
    #: result's. ``None`` means they are the same, which is true of every
    #: single-operand binding.
    #:
    #: Separate from ``unit`` because the two are used for different things
    #: and only coincide by accident. ``unit`` describes the number a reader
    #: sees; this one goes in the *fact join*, and dropping it from that key
    #: is what turns AMD's FY2024 tax rate into two rows that sum to 0.38
    #: (app/retrieval/DESIGN.md §2.4). A ratio binding whose result is
    #: ``pure`` therefore cannot be retrieved from ``unit`` alone -- there are
    #: no ``pure`` facts behind it, only the USD ones it divides.
    operand_unit: str | None = Field(default=None, min_length=1, max_length=32)

    is_instant: bool

    #: Whether the value is read straight off a window or computed by
    #: subtracting one from another. Sits here rather than on
    #: ``ResolvedPeriod`` because the answer depends on the *concept*: at Q4,
    #: revenue is a residual but total assets is a plain instant read.
    period_rule: PeriodRule = "direct"

    coverage: Coverage
    confidence: float = Field(ge=0.0, le=1.0)
    resolved_by: ResolvedBy

    #: Why this binding, in a sentence a non-accountant can check. Doubles as
    #: what the user-facing model cites when it says which concept it used.
    rationale: str = Field(min_length=1, max_length=512)

    #: Caveats that must reach the reader. See ``NoteKind``.
    notes: list[Note] = Field(default_factory=list)

    @property
    def fact_unit(self) -> str:
        """The unit to join facts on -- what retrieval needs, as opposed to
        what a reader is shown."""
        return self.operand_unit or self.unit

    @model_validator(mode="after")
    def _multi_operand_names_its_operand_unit(self) -> Binding:
        """A multi-operand binding must say what its operands are filed in.

        Without it the result unit is the only one available, and for a ratio
        that is ``pure`` -- a unit no fact behind the binding actually has. A
        join on it returns nothing, and "nothing" is indistinguishable from
        "the company reported nothing", which is the failure this schema
        exists to prevent.
        """
        if len(self.concepts) > 1 and self.operand_unit is None:
            raise ValueError(
                f"{self.expression!r} is computed over {len(self.concepts)} concepts, "
                f"so operand_unit must say what they are filed in; the result unit "
                f"({self.unit!r}) describes the answer, not the facts"
            )
        return self

    @model_validator(mode="after")
    def _expression_refs_exist(self) -> Binding:
        for index in _CONCEPT_REF.findall(self.expression):
            if int(index) >= len(self.concepts):
                raise ValueError(
                    f"expression {self.expression!r} references c{index}, "
                    f"but only {len(self.concepts)} concept(s) were bound"
                )
        return self

    @model_validator(mode="after")
    def _residual_is_provable(self) -> Binding:
        """A residual binding must be one that can actually be computed.

        Subtracting a window whose facts are missing does not fail -- it
        quietly yields a different number (drop the 9-month term and "Q4"
        becomes the whole year). Committing to a binding is the moment to
        refuse that, so the checks are here rather than left to the SQL step.
        """
        if self.period_rule != "residual":
            return self
        if self.is_instant:
            raise ValueError(
                "an instant fact needs no residual -- the fiscal-year-end instant "
                "is already the Q4-end instant"
            )
        if not self.coverage.components:
            raise ValueError("a residual binding must carry per-component coverage")
        empty = [c for c in self.coverage.components if c.fact_count == 0]
        if empty:
            raise ValueError(
                f"{len(empty)} of {len(self.coverage.components)} component windows "
                "have no facts; the subtraction would return a plausible wrong number"
            )
        return self


class Candidate(_Base):
    """A concept the mapper considered but did not commit to."""

    concept: ConceptRef
    score: float
    coverage: Coverage


class Ambiguity(_Base):
    """An element the mapper found candidates for but would not commit to.

    Two ways to land here: several candidates survive equally, or a single one
    survives too weakly to trust. Both mean the same thing downstream -- refuse
    the question and offer these back -- so they share a channel, and
    ``candidates`` may hold one.

    Distinct from ``Unresolved``, which means nothing plausible was found at
    all. Here there is something to ask the person about, which is why
    ``element_text`` travels with it: the refusal has to name the phrase it
    could not pin down.
    """

    element_id: str = Field(min_length=1, max_length=32)

    #: The phrase as the asker wrote it, so the refusal can quote it back.
    element_text: str = Field(min_length=1, max_length=256)

    candidates: list[Candidate] = Field(min_length=1)


class ClarifyOption(_Base):
    """One choice to offer back."""

    metric: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=240)


class Clarification(_Base):
    """A term that is genuinely several things, with the choices to offer.

    Distinct from both ``Unresolved`` (nothing matched) and ``Ambiguity`` (the
    *machine* could not decide, and the candidates are raw concepts). This one
    is curated: a person decided the term is ambiguous and wrote the options in
    business language, so the reply can be a real question rather than a
    shrug.

    A plan carrying these is not a failure. It is a request for one more piece
    of information, and answering it should be a round trip, not a dead end.
    """

    element_id: str = Field(min_length=1, max_length=32)
    element_text: str = Field(min_length=1, max_length=256)
    question: str = Field(min_length=1, max_length=240)
    options: list[ClarifyOption] = Field(min_length=2)


class Unresolved(_Base):
    """An element the mapper could not bind at all."""

    element_id: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=512)


class PeriodResidual(_Base):
    """The two windows a period with no filing of its own is computed from.

    Only Q4 needs this today: no 10-Q covers it, so it is the annual duration
    minus the nine-month year-to-date duration. Both components open on the
    same day -- the fiscal year start -- which is exactly what lets the SQL
    step join them without guessing, so the shared start is one field rather
    than two that happen to agree.

    Verified against the store: Apple's FY2024 is 2023-10-01 → 2024-09-28 with
    a nine-month term ending 2024-06-29, giving Q4 revenue of 94.9B, the
    reported figure.
    """

    shared_start: date
    whole_end: date
    subtract_end: date

    @model_validator(mode="after")
    def _subtrahend_is_shorter(self) -> PeriodResidual:
        if not self.shared_start < self.subtract_end < self.whole_end:
            raise ValueError(
                f"expected shared_start < subtract_end < whole_end, got "
                f"{self.shared_start} / {self.subtract_end} / {self.whole_end}"
            )
        return self


class ResolvedPeriod(_Base):
    """One company's concrete date window for one ``(fiscal_year,
    fiscal_period)``.

    The window is **read from the facts, never computed from the label**. A
    fiscal year's name and its dates are independent: J&J's FY2021 ends
    2022-01-02, and several filers run 52/53-week calendars where a quarter is
    83 or 97 days rather than 91. ``fiscal_year`` here is the filing's own
    label; ``period_start`` / ``period_end`` are what the data says it covers.

    Carried per company because fiscal calendars differ -- Microsoft's FY2024
    is Jul 2023 to Jun 2024, Apple's is Oct 2023 to Sep 2024. One "FY2024"
    element therefore resolves to a *different window per cik*, which is why
    this is a list of rows rather than a list of years.
    """

    company_cik: int
    fiscal_year: int
    fiscal_period: QueryFiscalPeriod
    period_start: date
    period_end: date

    @property
    def granularity(self) -> PeriodGranularity:
        return "annual" if self.fiscal_period == "FY" else "quarterly"

    #: Set exactly when this period has no filing of its own (Q4). The windows
    #: are supplied whatever the metric turns out to be; whether they get used
    #: is ``Binding.period_rule``'s call, because an instant concept reads the
    #: year-end value directly and needs no subtraction.
    residual_of: PeriodResidual | None = None

    @model_validator(mode="after")
    def _only_q4_is_derived(self) -> ResolvedPeriod:
        if (self.fiscal_period == "Q4") != (self.residual_of is not None):
            raise ValueError(
                f"{self.fiscal_period} period and residual_of="
                f"{'set' if self.residual_of else 'None'} disagree: Q4 is the only "
                "period the store cannot supply directly"
            )
        return self


class PlanFilters(_Base):
    """The concrete row-selection the SQL step should apply. Empty list means
    "unconstrained on this axis", not "match nothing"."""

    ciks: list[int] = Field(default_factory=list)

    #: Date windows, not fiscal-year integers. See §8.11: filtering facts by
    #: ``Filing.fiscal_year`` selects by *provenance* (which filing a number
    #: appeared in), not by which period it describes, because a 10-K carries
    #: prior-year comparative columns.
    periods: list[ResolvedPeriod] = Field(default_factory=list)

    forms: list[FilingForm] = Field(default_factory=list)


class ResultSpec(_Base):
    """How much data the answer needs, and along which dimensions.

    This is the part of "show me a chart" that the retrieval step actually has
    to honour. Drawing is somebody else's problem; *not collapsing twelve
    quarters into one figure* is this one's. ``row_count`` is the cardinality
    the retrieval must produce -- the product of the axes it varies along.

    Derived from what resolved, not from the question: if only one period came
    back, there is no period axis however the question was phrased.
    """

    shape: ResultShape
    axes: list[ResultAxis] = Field(default_factory=list)

    companies: int = Field(ge=0)
    periods: int = Field(ge=0)
    metrics: int = Field(ge=0)

    #: Which period granularities the result mixes. More than one means annual
    #: and quarterly figures share an axis, which is rarely what was wanted.
    granularities: list[PeriodGranularity] = Field(default_factory=list)

    @property
    def row_count(self) -> int:
        """Rows the retrieval should return. A result with fewer has dropped
        something the question asked for."""
        return max(1, self.companies) * max(1, self.periods) * max(1, self.metrics)


class QueryPlan(_Base):
    """Everything needed to write the SQL, with nothing left to guess."""

    version: Literal["1"] = "1"
    question: str = Field(min_length=1, max_length=2000)
    intent: Intent
    result: ResultSpec
    filters: PlanFilters
    bindings: list[Binding] = Field(default_factory=list)
    ambiguous: list[Ambiguity] = Field(default_factory=list)
    unresolved: list[Unresolved] = Field(default_factory=list)

    #: Curated questions to put back to the asker. See ``Clarification`` --
    #: these are answerable, unlike ``unresolved``.
    clarifications: list[Clarification] = Field(default_factory=list)

    #: Caveats about the result as a whole rather than about one binding --
    #: currently only that the companies' fiscal labels cover different dates.
    #: Kept separate from ``Binding.notes`` because attaching a statement about
    #: the comparison to one of its sides would be arbitrary.
    notes: list[Note] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """True when every element bound cleanly. The caller's signal to go
        ahead and generate SQL rather than escalate back to the user."""
        return not self.ambiguous and not self.unresolved and not self.clarifications

    @property
    def needs_input(self) -> bool:
        """True when the plan is incomplete but *answerable with one more
        reply* -- a curated question, or candidates worth offering.

        Separates "ask them" from "tell them it cannot be done": an
        ``Unresolved`` alone means the data does not support the question,
        while these mean it might once the asker narrows it.
        """
        return bool(self.clarifications or self.ambiguous)

    @staticmethod
    def binding_key(index: int) -> str:
        """The stable handle a result row cites a binding by. Positional,
        because the position is what makes plan and result auditable against
        each other."""
        return f"b{index}"

    def binding_for(
        self,
        element_id: str,
        company_cik: int,
        fiscal_year: int,
        fiscal_period: QueryFiscalPeriod,
    ) -> tuple[int, Binding]:
        """The one binding that answers for this cell, and its index.

        This is what makes citation a *lookup* rather than a guess. A result
        row carries no ``concept_id``; it carries this key, and the concepts
        come from the plan, which is trustworthy. Resolution order mirrors
        §8.3: a company-specific binding beats the ``company_cik=None``
        fallback, and ``Binding.periods`` separates a filer that changed tags
        mid-range.

        Raises ``LookupError`` when nothing answers for the cell -- the row
        was invented -- and ``ValueError`` when two bindings claim it, which
        means the plan itself is malformed and nothing else would notice.
        """
        wanted = PeriodRef(fiscal_year=fiscal_year, fiscal_period=fiscal_period)
        matches = [
            (index, binding)
            for index, binding in enumerate(self.bindings)
            if binding.element_id == element_id
            and binding.company_cik in (None, company_cik)
            and (not binding.periods or wanted in binding.periods)
        ]
        specific = [pair for pair in matches if pair[1].company_cik is not None]
        chosen = specific or matches
        if not chosen:
            raise LookupError(
                f"no binding answers for element {element_id!r}, cik {company_cik}, "
                f"{fiscal_period}{fiscal_year}"
            )
        if len(chosen) > 1:
            raise ValueError(
                f"{len(chosen)} bindings claim element {element_id!r}, cik "
                f"{company_cik}, {fiscal_period}{fiscal_year}: indices "
                f"{[index for index, _ in chosen]}. A cell with two answers has none."
            )
        return chosen[0]

    def bindings_for_company(
        self, element_id: str, company_cik: int
    ) -> list[tuple[int, Binding]]:
        """Every binding of one element for one company, with indices.

        Coarse attribution, for a *derived* row: a growth figure spanning a
        tag change is computed from two bindings, and a row labelled with one
        period cannot name the other end. This says "computed from these",
        which is true, rather than claiming a precision the row lacks.
        """
        matches = [
            (index, binding)
            for index, binding in enumerate(self.bindings)
            if binding.element_id == element_id
            and binding.company_cik in (None, company_cik)
        ]
        specific = [pair for pair in matches if pair[1].company_cik is not None]
        return specific or matches
