"""The query mapper: ``QueryIn`` in, ``QueryPlan`` out.

    from app.semantic.query_mapper import map_query
    plan = await map_query(query)

Three resolvers run in order, because each needs the last one's answer:
companies by exact lookup, periods into concrete date windows per company, then
metrics -- curated alias, embedding fallback, and a coverage check against the
facts that decides between them.

Nothing here raises on bad input. An element that cannot be resolved comes back
in ``unresolved`` or ``ambiguous``, and one that resolves with a caveat carries
a ``Note``; the caller reads ``plan.is_complete`` and decides.

The data hazards this navigates -- comparative columns, 52/53-week calendars,
Q4 never being filed, filers changing tags mid-range -- are catalogued with
their evidence in ``PITFALLS.md``.

Dependency direction: ``semantic -> (schemas, db)``, never the reverse.

Connects as ``vf_query_mapper_role``: read-only, but with access to
``concept.embedding``, which the metric fallback searches and which the
retrieval role deliberately lacks. See ``app/db/roles.py``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Company, Concept, Fact, Filing
from app.db.session import QueryMapperSessionLocal
from app.db.views import reported_fact
from app.embedding_client import embed_query
from app.schemas.query import (
    Ambiguity,
    Binding,
    Candidate,
    Clarification,
    ClarifyOption,
    CompanyElementIn,
    CompanyGroupElementIn,
    ConceptRef,
    Coverage,
    Intent,
    MetricElementIn,
    MetricQualifierElementIn,
    NarrativeElementIn,
    Note,
    PeriodElementIn,
    PeriodRef,
    PlanFilters,
    QueryIn,
    QueryPlan,
    ResolvedPeriod,
    ResultAxis,
    ResultSpec,
    Unresolved,
)
from app.semantic.company_aliases import company_alias_index
from app.semantic.metric_aliases import AliasHit, alias_index

#: Day spans that identify a filing's *own* reporting window, separating it
#: from the year-to-date and comparative durations filed alongside it.
#: Verified against the whole store: all 100 10-K filings resolve inside the
#: annual range and all 298 10-Q filings inside the quarterly one (measured
#: 83-97 days -- 52/53-week filers like JNJ and NVDA make a "quarter" neither
#: 90 nor 91). Both ranges are deliberately wider than the observed extremes so
#: an unusual filer fails loudly as "no window" rather than matching the wrong
#: duration.
_ANNUAL_SPAN_DAYS = (355, 375)
_QUARTER_SPAN_DAYS = (80, 100)

#: How far apart two companies' dates may sit under the same fiscal label
#: before a cross-company comparison is worth warning about. A month shifts a
#: quarter by a third and changes which conditions a year captures. The bar is
#: low because the real spreads are not: measured across the store, one fiscal
#: label covers period_ends up to 343 days apart -- "FY2025" runs from
#: 2025-01-26 to 2025-12-31 depending on the filer.
_ALIGNMENT_TOLERANCE_DAYS = 30


async def map_query(
    query: QueryIn, *, session_factory: async_sessionmaker = QueryMapperSessionLocal
) -> QueryPlan:
    """Resolve one parsed question into a plan the SQL step can execute.

    Never raises on an unresolvable element -- a partial plan with populated
    ``unresolved`` / ``ambiguous`` is a normal outcome, and the caller decides
    whether to proceed (``plan.is_complete``) or go back to the user.
    """
    companies = [e for e in query.elements if isinstance(e, CompanyElementIn)]
    groups = [e for e in query.elements if isinstance(e, CompanyGroupElementIn)]
    periods = [e for e in query.elements if isinstance(e, PeriodElementIn)]
    metrics = [e for e in query.elements if isinstance(e, MetricElementIn)]

    async with session_factory() as session:
        ciks, company_problems, missing_companies = await _resolve_companies(
            companies, session
        )
        group_ciks, group_problems = await _resolve_company_groups(groups, session)
        ciks = _merge_ciks(ciks, group_ciks)
        company_problems += group_problems
        if not companies and not groups:
            ciks = await _all_loaded_ciks(session)
        company_notes: list[Note] = []
        if missing_companies:
            named = ", ".join(repr(name) for name in missing_companies)
            if ciks:
                # Something else resolved, so there is an answer to give. Note
                # it rather than refuse: "how does Apple compare to Samsung"
                # is a real question about Apple, and a flat refusal tells the
                # asker less than half an answer plus this sentence does.
                company_notes.append(
                    Note(
                        kind="partial_coverage",
                        message=(
                            f"{named} is not a company loaded in this dataset and is "
                            f"absent from the result; the answer covers "
                            f"{await _describe_scope(ciks, session)} only"
                        ),
                    )
                )
            else:
                # Nothing resolved. Refuse -- and note that this is the one
                # path that must NOT widen to every loaded filer, which is why
                # the `_all_loaded_ciks` fallback above is guarded on there
                # being no company element at all rather than on `ciks` being
                # empty.
                company_problems += [
                    Unresolved(
                        element_id=element.id,
                        reason=f"no loaded company matches {element.text!r}",
                    )
                    for element in companies
                    if element.text in missing_companies
                ]
        resolved_periods, period_problems = await _resolve_periods(periods, session, ciks=ciks)
        (
            bindings,
            ambiguous,
            metric_problems,
            clarifications,
            coverage_notes,
        ) = await _resolve_metrics(
            metrics, session, ciks=ciks, periods=resolved_periods, intent=query.intent
        )

    qualifiers = [e for e in query.elements if isinstance(e, MetricQualifierElementIn)]
    if qualifiers:
        bindings, qualifier_problems = _apply_qualifiers(qualifiers, bindings, metrics)
        metric_problems += qualifier_problems

    # Deliberately does NOT drop the metric's bindings or its clarification,
    # unlike an unsatisfiable qualifier. The refusal already makes the plan
    # incomplete, so nothing is answered -- and leaving the rest standing is
    # what lets one reply say both halves: "I cannot tell you why, and which
    # margin did you mean?" A reader who has to re-type the question to get the
    # second half has been told less than the system knew.
    narrative_problems = [
        Unresolved(element_id=element.id, reason=_narrative_refusal(element))
        for element in query.elements
        if isinstance(element, NarrativeElementIn)
    ]

    result = _describe_result(
        query,
        ciks=_answering_ciks(ciks, bindings),
        periods=resolved_periods,
        metrics=len(metrics),
    )
    return QueryPlan(
        question=query.question,
        intent=query.intent,
        result=result,
        filters=PlanFilters(ciks=ciks, periods=resolved_periods),
        bindings=bindings,
        ambiguous=ambiguous,
        unresolved=company_problems
        + period_problems
        + metric_problems
        + narrative_problems,
        clarifications=clarifications,
        notes=company_notes
        + coverage_notes
        + _alignment_notes(resolved_periods, result)
        + _granularity_notes(result),
    )


def _qualifier_is_satisfiable(qualifier: MetricQualifierElementIn) -> str | None:
    """``None`` if the qualifier can be honoured, else why it cannot.

    Always a reason today, and the reason is a property of the corpus rather
    than of the phrase: the SEC's XBRL data endpoint returns company totals
    only, with no dimensional breakdown at all (PITFALLS §3.2). There is no
    product axis, no geography axis and no segment axis to filter on, for any
    filer.

    Written as a satisfiability question rather than a flat refusal so the day
    segment facts are ingested this is the one function that changes. Nothing
    above it assumes the answer.
    """
    return (
        "this dataset holds company totals only, with no breakdown by product, "
        "region or segment"
    )


#: Span fragments that say which *kind* of non-figure was asked for. Matched
#: against the narrative element's own text, lowercased. Order matters only in
#: that the first hit wins, and the two sets do not overlap in practice.
_CAUSAL_MARKERS = ("why", "how come", "what caused", "reason for", "reason why",
                   "driven by", "because")
_FILING_TEXT_MARKERS = ("say about", "says about", "said about", "say", "says",
                        "describe", "discuss", "disclose", "mention", "explain",
                        "guidance", "outlook", "risk factor")


def _narrative_refusal(element: NarrativeElementIn) -> str:
    """Why this span cannot be honoured, said specifically.

    A generic "not in the dataset" would be true and useless. The reader asked
    for one of two different things and each deserves its own sentence: a cause,
    which no quantity implies, or the filing's own words, which are not
    ingested at all. Measured 2026-09-24: q032 already refused, but with
    "'competition risk' has no concept with facts covering any requested
    period" -- accurate about the machinery and silent about the actual reason.

    Framed as a lookup over the span rather than a flat string so a later
    ingest of narrative sections changes this function and nothing above it.
    """
    span = element.text.lower()
    if any(marker in span for marker in _CAUSAL_MARKERS):
        return (
            f"{element.text!r} asks why something happened. The store holds filed "
            f"figures, which can show that a number changed but never why it "
            f"changed; a cause is not a quantity"
        )
    if any(marker in span for marker in _FILING_TEXT_MARKERS):
        return (
            f"{element.text!r} asks what the filing says. Only numeric XBRL facts "
            f"are ingested -- no risk factors, no management discussion, none of "
            f"the narrative sections"
        )
    return (
        f"{element.text!r} asks for something other than a filed figure, and this "
        f"store holds nothing else"
    )


def _apply_qualifiers(
    qualifiers: list[MetricQualifierElementIn],
    bindings: list[Binding],
    metrics: list[MetricElementIn],
) -> tuple[list[Binding], list[Unresolved]]:
    """Narrow each qualified metric, or refuse it and drop its bindings.

    **An unsatisfiable qualifier takes its metric with it.** That is the whole
    safety argument for this element existing: "how much revenue did Apple make
    from iPhones" must not be answered with Apple's total revenue, and leaving
    the metric bound is exactly how that happened when the qualifier was simply
    dropped (measured 2026-09-23 -- $391,035,000,000 returned for a question
    about iPhones, verdict `complete`, attributable, and wrong).

    The refusal names the *metric*, not the qualifier, because the metric is
    what the reader asked for and what they will not be getting.
    """
    problems: list[Unresolved] = []
    refused: set[str] = set()
    labels = {metric.id: metric.text for metric in metrics}

    for qualifier in qualifiers:
        reason = _qualifier_is_satisfiable(qualifier)
        if reason is None:
            continue
        refused.add(qualifier.qualifies)
        problems.append(
            Unresolved(
                element_id=qualifier.qualifies,
                reason=f"{labels.get(qualifier.qualifies, qualifier.qualifies)!r} was "
                f"asked for only {qualifier.text!r}, and {reason}",
            )
        )

    kept = [binding for binding in bindings if binding.element_id not in refused]
    return kept, problems


def _answering_ciks(ciks: list[int], bindings: list[Binding]) -> list[int]:
    """The companies the result will actually carry rows for.

    ``PlanFilters.ciks`` stays the full resolved scope -- it records what the
    question reached, and narrowing it would erase the evidence that a dropped
    filer was ever in it. ``ResultSpec`` is the other half of that pair and has
    to be honest the other way: it states the cardinality retrieval must
    produce, and promising a row for a company with no binding makes the
    verdict report a shortfall for something the plan already disclosed.

    Union, not intersection, when several metrics cover different companies.
    That over-counts a plan whose metrics disagree, which is why the verdict
    counts ``plan_cells`` rather than this -- ``row_count`` is what the reader
    is promised, and the cells are what is checked.
    """
    if not bindings or any(binding.company_cik is None for binding in bindings):
        return ciks
    bound = {binding.company_cik for binding in bindings}
    return [cik for cik in ciks if cik in bound]


def _describe_result(
    query: QueryIn, *, ciks: list[int], periods: list[ResolvedPeriod], metrics: int
) -> ResultSpec:
    """What the answer has to contain, from what actually resolved.

    The axes are measured rather than requested: a question phrased as a chart
    whose periods all failed to resolve has no period axis, and saying so is
    more useful than promising a series that cannot be filled. ``shape`` does
    take the asker's word when they gave one, because "show me visually"
    carries intent the cardinality alone cannot -- one company over twelve
    quarters and one company over twelve quarters *as a chart* need the same
    rows but not the same answer.
    """
    distinct_periods = {(p.fiscal_year, p.fiscal_period) for p in periods}
    axes: list[ResultAxis] = []
    if len(ciks) > 1:
        axes.append("company")
    if len(distinct_periods) > 1:
        axes.append("period")
    if metrics > 1:
        axes.append("metric")

    if query.shape is not None:
        shape = query.shape
    elif not axes:
        shape = "scalar"
    elif query.intent == "rank":
        shape = "ranking"
    elif axes == ["period"] or axes == ["company", "period"]:
        shape = "series"
    else:
        shape = "table"

    return ResultSpec(
        shape=shape,
        axes=axes,
        companies=len(ciks),
        periods=len(distinct_periods),
        metrics=metrics,
        granularities=sorted({p.granularity for p in periods}),
    )


def _alignment_notes(periods: list[ResolvedPeriod], result: ResultSpec) -> list[Note]:
    """Warn when one fiscal label covers very different dates per company.

    Only fires on a cross-company comparison, because within one filer the
    labels are self-consistent. It matters most for the shape that hides it
    best: a line chart puts "Q1 2025" at one x position, and the viewer reads
    points as contemporaneous when one company's quarter can end ten months
    after another's.

    Reports the worst label rather than all of them -- once the reader knows
    the axis is approximate, an enumeration adds nothing.
    """
    if "company" not in result.axes:
        return []

    by_label: dict[tuple[int, str], list[ResolvedPeriod]] = defaultdict(list)
    for period in periods:
        by_label[(period.fiscal_year, period.fiscal_period)].append(period)

    worst: tuple[int, tuple[int, str], date, date] | None = None
    for label, group in by_label.items():
        if len({p.company_cik for p in group}) < 2:
            continue
        earliest = min(p.period_end for p in group)
        latest = max(p.period_end for p in group)
        spread = (latest - earliest).days
        if spread > _ALIGNMENT_TOLERANCE_DAYS and (worst is None or spread > worst[0]):
            worst = (spread, label, earliest, latest)

    if worst is None:
        return []

    spread, (year, fiscal_period), earliest, latest = worst
    return [
        Note(
            kind="period_misalignment",
            message=f"These companies do not share a fiscal calendar: {fiscal_period} "
            f"{year} ends anywhere from {earliest} to {latest}, a spread of {spread} "
            "days. Points sharing a period label are not contemporaneous, so treat a "
            "shared time axis as approximate.",
        )
    ]


def _granularity_notes(result: ResultSpec) -> list[Note]:
    """Warn when annual and quarterly figures share a result.

    A 363-day value next to four 90-day values is not five comparable points;
    on a shared axis the annual reads as a fourfold spike. The question is
    legitimate -- "2024, both quarterly and yearly" is a normal ask -- so this
    is a note rather than a refusal, but the two series want plotting
    separately.
    """
    if len(result.granularities) < 2:
        return []
    return [
        Note(
            kind="mixed_granularity",
            message="This result mixes "
            + " and ".join(result.granularities)
            + " periods. They measure different spans, so they are not "
            "comparable points on one axis and should be shown separately.",
        )
    ]


def _merge_ciks(named: list[int], from_groups: list[int]) -> list[int]:
    """Named companies first, then group members, without duplicates."""
    merged = list(named)
    for cik in from_groups:
        if cik not in merged:
            merged.append(cik)
    return merged


# --------------------------------------------------------------------------- #
# Companies -- deterministic lookup
# --------------------------------------------------------------------------- #


#: Why a sector column has no values, per column. The three are not the same
#: problem and a reader acts on them differently: ``sic_code`` and
#: ``sic_description`` are loaded from ``sic_numbers.json`` and being empty
#: means the load step has not run, while ``sic_office`` has no source at all
#: and being empty is the permanent state until one is found.
_WHY_UNPOPULATED = {
    "sic_code": "Run the load step -- sic_numbers.json carries this and app/db/loader.py "
    "writes it.",
    "sic_description": "Run the load step -- sic_numbers.json carries this and "
    "app/db/loader.py writes it.",
    "sic_office": "No source for this exists: sic_numbers.json carries the SIC code and "
    "description only. The SEC assigns review offices by SIC range, so it is derivable "
    "given that mapping, which this project does not have. Selecting by sector "
    "(sic_description) works today.",
}


async def _resolve_company_groups(
    elements: list[CompanyGroupElementIn], session: AsyncSession
) -> tuple[list[int], list[Unresolved]]:
    """Filers matching an attribute, rather than named one at a time.

    The SIC columns exist but nothing populates them yet, so this reports
    "not loaded" rather than returning an empty set -- an empty set would read
    as "no company is in that sector", which is a different and wrong answer.
    The distinction is made by asking whether *any* filer has the column set,
    which also means this starts working the moment the data lands, with no
    code change.
    """
    if not elements:
        return [], []

    ciks: list[int] = []
    problems: list[Unresolved] = []

    for element in elements:
        column, value = _group_selector(element)
        populated = await session.execute(
            select(Company.cik).where(column.is_not(None)).limit(1)
        )
        if populated.first() is None:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"{element.text!r} selects companies by {column.key}, "
                    f"which is not populated for any filer. {_WHY_UNPOPULATED[column.key]}",
                )
            )
            continue

        where = column.ilike(f"%{value}%") if column is Company.sic_description else column == value
        matched = await session.execute(select(Company.cik).where(where))
        found = list(matched.scalars().all())
        if not found:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"no loaded company matches {column.key}={value!r}",
                )
            )
        ciks.extend(found)

    return ciks, problems


def _group_selector(element: CompanyGroupElementIn):
    """The column and value one group element selects on, most specific first."""
    if element.sic_code:
        return Company.sic_code, element.sic_code
    if element.sic_office:
        return Company.sic_office, element.sic_office
    return Company.sic_description, element.sic_description


async def _resolve_companies(
    elements: list[CompanyElementIn], session: AsyncSession
) -> tuple[list[int], list[Unresolved], list[str]]:
    """``(ciks, refusals, names that matched nothing)``.

    The third return is separated from the second because the two failures are
    not the same failure. A name that matches **nothing** is droppable: "how
    does Apple compare to Samsung" is answerable about Apple, as long as the
    reader is told Samsung is missing, and the caller makes that call because
    only it knows whether anything else resolved.

    A name that matches **several** loaded companies is not droppable. Picking
    one would be a guess between real alternatives, and dropping it would
    answer a narrower question than the one asked without anybody choosing to.
    That stays a refusal here.
    """
    ciks: list[int] = []
    problems: list[Unresolved] = []
    missing: list[str] = []

    for element in elements:
        matches = await _lookup_company(element, session)
        if not matches:
            missing.append(element.text)
        elif len(matches) > 1:
            # Company ambiguity has no home in ``Ambiguity`` -- that model is
            # concept-shaped. Reported here instead, naming the collisions so
            # the caller can re-ask. See DESIGN.md §8.5.
            #
            # Named, not numbered. This reason is what a user-facing model
            # quotes back, and "matches more than one company (ciks: 1652044,
            # 320193, 1326801)" asks someone to choose between three integers.
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"{element.text!r} matches more than one company: "
                    f"{await _describe_companies(matches, session)}. Name one of them.",
                )
            )
        else:
            ciks.append(matches[0])

    return ciks, problems, missing


async def _describe_scope(ciks: list[int], session: AsyncSession) -> str:
    """The surviving companies, named while there are few enough to read.

    A two-company comparison that lost one side has to say which side is left;
    a twenty-company question that lost one does not need the other nineteen
    listed back.
    """
    if len(ciks) > 4:
        return f"the {len(ciks)} companies that resolved"
    return await _describe_companies(ciks, session)


async def _describe_companies(ciks: list[int], session: AsyncSession) -> str:
    """``"Alphabet Inc. (GOOGL), Apple Inc. (AAPL)"`` -- the companies behind a
    list of ciks, for a message a person has to act on.

    Falls back to the bare cik for a row carrying neither name nor ticker,
    which is better than omitting a collision from the list of things to
    choose between.
    """
    rows = await session.execute(
        select(Company.cik, Company.ticker, Company.entity_name).where(Company.cik.in_(ciks))
    )
    labels = {
        cik: f"{name} ({ticker})" if name and ticker else (name or ticker or f"cik {cik}")
        for cik, ticker, name in rows
    }
    return ", ".join(labels.get(cik, f"cik {cik}") for cik in ciks)


async def _all_loaded_ciks(session: AsyncSession) -> list[int]:
    """Every filer in the database, for a question that names none.

    "Rank all twenty companies by revenue" and "which company grew fastest"
    carry no company element, because there is no company *to* name -- the
    population is the answer. That used to resolve to an empty cik list, which
    the metric resolver read as "no company in scope to verify coverage
    against" and refused outright.

    The distinction that makes this safe is between an absent element and a
    *failed* one, so the expansion happens in ``map_query`` where both are
    visible. "Apple versus Samsung" names two companies and resolves one; that
    must stay a half-answer with Samsung reported unresolved, and must never
    widen into every filer in the store.

    Enumerated rather than left empty. ``PlanFilters.ciks`` treats an empty
    list as unconstrained, which the SQL step could honour with no WHERE
    clause -- but a plan whose bindings, coverage proofs and
    ``ResultSpec.companies`` all name concrete ciks is the point of the plan,
    and "however many rows come back" is not a cardinality anything can check.
    """
    rows = await session.execute(select(Company.cik).order_by(Company.cik))
    return list(rows.scalars().all())


async def _lookup_company(element: CompanyElementIn, session: AsyncSession) -> list[int]:
    """Ciks matching one company element, most specific hint first.

    The derived lexicon goes first, then the database's own columns.

    The lexicon exists because the columns are not what people say. Measured
    against the corpus, name-only lookup missed Google (stored as "Alphabet
    Inc."), Facebook (now Meta), Bank of America (whose ``entity_name`` is
    "BofA Finance LLC"), Johnson and Johnson (EDGAR spells the ampersand) and
    United Health. It also covers share classes -- "GOOG" is Alphabet even
    though the stored ticker is GOOGL -- and former names, since a cik
    outlives both the ticker and the name. See
    ``app/semantic/company_aliases.py``.

    A lexicon hit is intersected with what is actually loaded, so a corpus
    company absent from this database falls through to "no loaded company
    matches" rather than resolving to a cik with no rows behind it.

    Capped at three: the caller only needs to tell "none" from "one" from
    "several", and an unhinted substring like "Inc" would otherwise drag back
    the whole table.
    """
    for hint in (element.ticker, element.name, element.text):
        if not hint:
            continue
        known = company_alias_index().lookup(hint)
        if not known:
            continue
        rows = await session.execute(select(Company.cik).where(Company.cik.in_(known)).limit(3))
        found = list(rows.scalars().all())
        if found:
            return found

    if element.ticker:
        where = Company.ticker == element.ticker.upper()
    elif element.name:
        where = Company.entity_name.ilike(f"%{element.name}%")
    else:
        where = or_(
            Company.ticker == element.text.upper(),
            Company.entity_name.ilike(f"%{element.text}%"),
        )

    rows = await session.execute(select(Company.cik).where(where).limit(3))
    return list(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Periods -- arithmetic against what is actually loaded
# --------------------------------------------------------------------------- #


async def _resolve_periods(
    elements: list[PeriodElementIn], session: AsyncSession, *, ciks: list[int]
) -> tuple[list[ResolvedPeriod], list[Unresolved]]:
    """Concrete date windows for every period element, per company.

    Two decisions worth knowing about:

    A period element with no ``fiscal_period`` defaults to ``"FY"`` -- "revenue
    in 2024" means the fiscal year, not every quarter in it.

    ``last_n_years`` counts back from the newest fiscal year that actually
    *resolved to a window*, not from today's date and not from the newest
    ``Filing`` row. "The last 5 years" should mean five years this database can
    answer for; a filing whose own reporting window can't be identified is not
    one of them.

    That count is taken **per company**. It used to be one global
    ``max(fiscal_year)`` across everything in scope, which is the same value
    only while the corpus is evenly fresh. Load FY2026 for one filer and every
    other filer's "last year" starts asking for a year it does not have, so a
    question about Microsoft begins failing because of an Apple ingest.

    ``last_n_quarters`` is resolved **per company, by date**, which the year
    path is not -- see ``_recent_quarters``.
    """
    if not elements:
        return [], []

    windows = _with_derived_q4(await _load_windows(session, ciks))
    if not windows:
        return [], [
            Unresolved(
                element_id=element.id,
                reason="no filing reporting windows could be resolved for the companies in scope",
            )
            for element in elements
        ]

    newest_by_company = _newest_by_company(windows)
    resolved: dict[tuple[int, int, str], ResolvedPeriod] = {}
    problems: list[Unresolved] = []

    for element in elements:
        if element.last_n_quarters is not None:
            matched = _recent_quarters(windows, element.last_n_quarters, ciks=ciks)
            if not matched:
                problems.append(
                    Unresolved(
                        element_id=element.id,
                        reason=f"no quarterly reporting window found for {element.text!r}",
                    )
                )
            resolved.update(matched)
            continue

        if (
            element.fiscal_year,
            element.last_n_years,
            element.fiscal_period,
        ) == (None,) * 3:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"period {element.text!r} carries no fiscal_year, "
                    "last_n_years or fiscal_period",
                )
            )
            continue

        wanted_period = element.fiscal_period or "FY"

        def _wanted(company_cik: int, element: PeriodElementIn = element) -> set[int] | None:
            """The years this element selects *for this company*.

            ``None`` means unconstrained -- every year there is a window for.
            A year range is computed from that company's own newest year, so
            filers loaded to different points each get their own last N.
            """
            if element.fiscal_year is not None:
                return {element.fiscal_year}
            if element.last_n_years is None:
                return None
            newest = newest_by_company.get(company_cik)
            if newest is None:
                return set()
            return set(range(newest - element.last_n_years + 1, newest + 1))

        matched = {}
        for key, period in windows.items():
            if key[2] != wanted_period:
                continue
            years = _wanted(key[0])
            if years is None or key[1] in years:
                matched[key] = period
        if not matched:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"no {wanted_period} reporting window found for {element.text!r}",
                )
            )
        resolved.update(matched)

    return [resolved[key] for key in sorted(resolved)], problems


def _newest_by_company(
    windows: dict[tuple[int, int, str], ResolvedPeriod],
) -> dict[int, int]:
    """The newest fiscal year each company has a window for.

    Per company rather than one global maximum, which is what this replaced.
    The two are the same number only while every filer is loaded to the same
    point; the day one is brought forward, a single maximum makes every other
    filer's "last year" name a year they do not have, and they resolve to
    nothing. Nothing about that failure points at the ingest that caused it.
    """
    newest: dict[int, int] = {}
    for company_cik, fiscal_year, _ in windows:
        current = newest.get(company_cik)
        if current is None or fiscal_year > current:
            newest[company_cik] = fiscal_year
    return newest


def _recent_quarters(
    windows: dict[tuple[int, int, str], ResolvedPeriod],
    count: int,
    *,
    ciks: list[int],
) -> dict[tuple[int, int, str], ResolvedPeriod]:
    """The ``count`` newest quarter windows for each company, by ``period_end``.

    **Per company, and by date rather than by label.** Both matter.

    By date, because the label cannot be trusted to be the newest: "the last
    quarter" was previously written as ``fiscal_period="Q4"`` with
    ``last_n_years=1``, which is right only while every filer's data happens to
    end at a fiscal year end. Load Q1 and Q2 of a new year and the newest
    fiscal year has no Q4 at all, so that element resolves to nothing and a
    working question starts refusing. Sorting the windows this company actually
    has and taking the last few cannot go stale that way.

    Per company, because fiscal calendars in this corpus are up to three months
    apart. A single global "newest quarter" would hand Microsoft, whose year
    ends in June, the window belonging to a December filer.
    """
    by_company: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for key, period in windows.items():
        if period.fiscal_period != "FY" and period.company_cik in set(ciks):
            by_company[period.company_cik].append(key)

    picked: dict[tuple[int, int, str], ResolvedPeriod] = {}
    for company_keys in by_company.values():
        newest_first = sorted(company_keys, key=lambda k: windows[k].period_end, reverse=True)
        for key in newest_first[:count]:
            picked[key] = windows[key]
    return picked


def _with_derived_q4(
    windows: dict[tuple[int, int, str], tuple[date, date]],
) -> dict[tuple[int, int, str], ResolvedPeriod]:
    """Every stored window, plus a Q4 *label* wherever a company has both an
    annual and a Q3 window for the same fiscal year.

    No US filer files a fourth quarter -- the 10-K absorbs it -- so Q4 is the
    only period whose window has to be constructed: the day after Q3 closes to
    the fiscal year end. It needs no extra query.

    What this no longer constructs is the *arithmetic*. It used to attach the
    two windows the SQL step had to subtract, because the store held no Q4
    value; ``xbrl.reported_fact`` now synthesizes one at exactly this window
    (migration ``a8b5b820cf1a``), so a Q4 is an ordinary row to bind and read.
    The two derivations were cross-checked over 20 companies, 4 metrics and 5
    years before this one was removed: 275 of 275 agreed.

    A company missing either component simply gets no Q4 key, so asking for one
    reports "no Q4 reporting window" instead of quietly producing a year.
    """
    resolved = {
        key: ResolvedPeriod(
            company_cik=key[0],
            fiscal_year=key[1],
            fiscal_period=key[2],
            period_start=start,
            period_end=end,
        )
        for key, (start, end) in windows.items()
    }

    for (cik, year, period), (annual_start, annual_end) in windows.items():
        if period != "FY":
            continue
        third_quarter = windows.get((cik, year, "Q3"))
        if third_quarter is None:
            continue
        nine_month_end = third_quarter[1]
        if not annual_start < nine_month_end < annual_end:
            continue
        resolved[(cik, year, "Q4")] = ResolvedPeriod(
            company_cik=cik,
            fiscal_year=year,
            fiscal_period="Q4",
            period_start=nine_month_end + timedelta(days=1),
            period_end=annual_end,
        )

    return resolved


async def _load_windows(
    session: AsyncSession, ciks: list[int]
) -> dict[tuple[int, int, str], tuple[date, date]]:
    """Every ``(cik, fiscal_year, fiscal_period) -> (start, end)`` window in scope.

    A filing's own window is the duration fact with the **latest**
    ``period_end`` in it -- comparative columns describe earlier periods, so the
    maximum is the period the filing is actually about. Ties on ``period_end``
    break toward the *shortest* span, which is what separates a 10-Q's discrete
    quarter from the year-to-date duration filed beside it (both end on the same
    day).

    One query for the whole plan; at twenty companies and five years this is a
    hundred-odd rows, so it is not worth resolving lazily per element.
    """
    span = Fact.period_end - Fact.period_start
    key = (Filing.company_cik, Filing.fiscal_year, Filing.fiscal_period)

    stmt = (
        select(*key, Fact.period_start, Fact.period_end)
        .join(Fact, Fact.filing_accession == Filing.accession_number)
        .where(
            Fact.is_instant.is_(False),
            or_(
                and_(Filing.fiscal_period == "FY", span.between(*_ANNUAL_SPAN_DAYS)),
                and_(Filing.fiscal_period != "FY", span.between(*_QUARTER_SPAN_DAYS)),
            ),
        )
        .distinct(*key)
        .order_by(*key, Fact.period_end.desc(), span.asc())
    )
    if ciks:
        stmt = stmt.where(Filing.company_cik.in_(ciks))

    rows = await session.execute(stmt)
    return {(cik, year, period): (start, end) for cik, year, period, start, end in rows}


# --------------------------------------------------------------------------- #
# Metrics -- curated aliases first, embeddings as the recall net, coverage as
# the arbiter of both
# --------------------------------------------------------------------------- #

#: How many nearest concepts the embedding search returns when no curated alias
#: matches. Generous on purpose: this step is a *recall* net whose output is a
#: candidate set, and the coverage filter -- not the distance -- decides.
_EMBEDDING_CANDIDATES = 25

#: Cosine distance past which a candidate isn't worth considering at all. Loose
#: (measured good matches sit near 0.15-0.25) so near-misses still reach the
#: coverage filter, which is the step that can actually disprove them.
_MAX_EMBEDDING_DISTANCE = 0.45

#: Similarity a lone embedding candidate must clear to be bound outright.
#: Below it the match is a guess, and a guess presented as an answer is the
#: failure this project keeps trying to avoid -- so it is offered back as a
#: choice instead. Curated aliases bypass this; they are judgment, not
#: distance.
#:
#: Was 0.70. Measured 2026-09-19 over 221 labelled (phrase, company) cases, by
#: where the *top covered candidate* landed:
#:
#:     band        right   wrong   not in the store
#:     0.70-0.75       2      19        22
#:     0.75-0.80      32       7         0
#:     0.80+          12       2         0
#:
#: The band just above the old bar is 5% precise; above 0.75 it is 83%. That
#: band is where the wrong bindings lived -- "interest income" resolving to
#: pre-tax income at 0.718, "treasury stock" to a share count at 0.701.
#:
#: Raising it is worth doing and does not fix the path. Right answers span
#: 0.675-0.851 and wrong ones 0.556-0.810, and most of both sits in the
#: overlap, so no threshold makes an unreviewed binding safe. The fix for a
#: term people keep asking is a curated entry. See DESIGN.md §3b.
_MIN_BINDING_SIMILARITY = 0.75

#: Similarity below which the nearest concept is not worth *offering* either.
#:
#: Measured 2026-09-19 over 221 (phrase, company) cases with hand-labelled
#: truth: a floor here at 0.65 turns 104 of 129 questions the store cannot
#: answer from "here are four concepts, pick one" into a refusal, and of the 15
#: answerable cases it also refuses, **none** had the right concept anywhere in
#: the list they were offering. The cost is zero because a candidate list this
#: weak was never going to help -- "competition risk disclosure" was coming
#: back as a choice between AssetsFairValueDisclosure and
#: LiabilitiesFairValueDisclosure. Above 0.68 the cost stops being zero, which
#: is why the floor sits here and not higher.
#:
#: The distinction this draws is the one in DESIGN.md §8: `ambiguous` means the
#: machine could not choose between real possibilities, and `unresolved` means
#: there is nothing to choose between.
_MIN_PLAUSIBLE_SIMILARITY = 0.65


@dataclass(frozen=True)
class _Candidate:
    """One concept under consideration for one operand slot."""

    concept_id: int
    taxonomy: str
    name: str
    score: float
    label: str | None = None

    def as_ref(self) -> ConceptRef:
        return ConceptRef(
            concept_id=self.concept_id,
            taxonomy=self.taxonomy,
            name=self.name,
            label=self.label,
        )


@dataclass(frozen=True)
class _Evidence:
    """What the facts say about one ``(company, concept)`` pair.

    Values are kept, not just the window keys, so that a concept switch can be
    checked at the seam: where two concepts both report a period, agreeing
    values mean the filer dual-tagged one quantity and the series can be
    stitched.
    """

    is_instant: bool
    unit: str
    values: dict[tuple[date | None, date], Decimal]

    @property
    def windows(self) -> set[tuple[date | None, date]]:
        return set(self.values)


async def _resolve_metrics(
    elements: list[MetricElementIn],
    session: AsyncSession,
    *,
    ciks: list[int],
    periods: list[ResolvedPeriod],
    intent: Intent,
) -> tuple[list[Binding], list[Ambiguity], list[Unresolved], list[Clarification], list[Note]]:
    """Bind every metric element to concepts that provably have the facts.

    The cascade, in order:

    1. curated alias lookup (``metric_aliases.yaml``). A hit **skips step 2** -- a
       curated entry is accounting judgment, and re-checking it against cosine
       distance could only add noise.
    2. embedding search, as a recall net for whatever the file does not cover
       yet. Produces candidates, never a decision.
    3. coverage: drop any candidate without facts for every window the plan
       asks for, per company. This is what separates a verified binding from a
       plausible one, and it outranks similarity outright.
    4. commit one ``Binding`` per ``(element, company)``, or report.

    Curated alternatives are ordered, so the first survivor of step 3 wins
    outright -- that ordering *is* the disambiguation, which is why the alias
    path never yields an ``Ambiguity``. The embedding path has no such
    ordering, so several survivors there is a real tie and gets reported.

    This is also where filer divergence dissolves without per-company
    curation: "revenue" lists several concepts, and each company keeps
    whichever one its own facts support.
    """
    if not elements:
        return [], [], [], [], []

    blockers = []
    if not ciks:
        blockers.append("no company in scope to verify coverage against")
    if not periods:
        blockers.append("no reporting period in scope to verify coverage against")
    if blockers:
        reason = "; ".join(blockers)
        return [], [], [Unresolved(element_id=e.id, reason=reason) for e in elements], [], []

    index = alias_index()
    bindings: list[Binding] = []
    ambiguous: list[Ambiguity] = []
    problems: list[Unresolved] = []
    clarifications: list[Clarification] = []
    notes: list[Note] = []

    for element in elements:
        hit = index.lookup(element.text)
        if hit is not None and hit.unavailable is not None:
            # Curated: the term is understood and the dataset has no answer for
            # it. Reaching the resolver ahead of the embedding net is the whole
            # point -- left to similarity, "share price" bound par value and
            # reported $0.000006. An `Unresolved` says so and does not ask,
            # because there is nothing the asker could narrow.
            problems.append(Unresolved(element_id=element.id, reason=hit.unavailable))
            continue
        if hit is not None and hit.clarify is not None:
            # Curated: the term really is several things, and someone wrote the
            # choices. Ask rather than pick a convention and be quietly wrong.
            clarifications.append(
                Clarification(
                    element_id=element.id,
                    element_text=element.text,
                    question=hit.clarify.question,
                    options=[
                        ClarifyOption(
                            metric=option.metric, label=label, description=option.description
                        )
                        for option, label in zip(
                            hit.clarify.options, hit.option_labels, strict=True
                        )
                    ],
                )
            )
            continue
        if hit is not None:
            slots = await _slots_from_alias(session, hit)
            if any(not slot for slot in slots):
                problems.append(
                    Unresolved(
                        element_id=element.id,
                        reason=f"curated alias {hit.metric!r} maps to concepts that are "
                        "not loaded in this database",
                    )
                )
                continue
            expression = hit.expression
            signs = list(hit.signs)
            caveats = hit.caveats
            resolved_by = "alias"
            source = f"curated alias {hit.metric!r}"
        else:
            nearest = await _nearest_concepts(session, element.text)
            if not nearest:
                problems.append(
                    Unresolved(
                        element_id=element.id,
                        reason=f"no curated alias for {element.text!r}, and nothing in the "
                        "concept corpus is close enough",
                    )
                )
                continue
            slots = [nearest]
            expression = "c0"
            # An embedding hit is a single lookup, never arithmetic, so there
            # is no operator for a magnitude assumption to attach to.
            signs = ["signed"]
            # Caveats are curated judgment; a distance match has none behind it.
            caveats = {}
            resolved_by = "embedding"
            source = "embedding search"

        evidence = await _gather_evidence(
            session,
            ciks=ciks,
            concept_ids={c.concept_id for slot in slots for c in slot},
            periods=periods,
        )
        bound_before = len(bindings)
        uncovered: list[int] = []
        _bind_per_company(
            element,
            slots=slots,
            signs=signs,
            caveats=caveats,
            expression=expression,
            resolved_by=resolved_by,
            source=source,
            ciks=ciks,
            periods=periods,
            evidence=evidence,
            bindings=bindings,
            ambiguous=ambiguous,
            problems=problems,
            uncovered_ciks=uncovered,
        )
        if uncovered:
            dropped = await _describe_companies(uncovered, session)
            if len(bindings) > bound_before:
                notes.append(
                    Note(
                        kind="partial_coverage",
                        message=(
                            f"{dropped} report no {element.text!r} for the periods asked "
                            f"about, so they are absent from the result"
                            + _subset_warning(intent, kept=len(ciks) - len(uncovered))
                        ),
                    )
                )
            else:
                # Nothing bound anywhere. One refusal naming every company,
                # not one per company: "total debt" over twenty filers used to
                # produce twenty near-identical reasons for one phrase.
                problems.append(
                    Unresolved(
                        element_id=element.id,
                        reason=f"{element.text!r} has no concept with facts covering any "
                        f"requested period for {dropped}",
                    )
                )

    return bindings, ambiguous, problems, clarifications, notes


def _subset_warning(intent: Intent, *, kept: int) -> str:
    """The extra sentence a ranking needs.

    Dropping a company from a lookup costs a row. Dropping one from a *ranking*
    can change the answer outright -- the excluded filer might have been first
    -- and no row count downstream would show it.
    """
    if intent != "rank":
        return ""
    return (
        f". The ordering below is over the remaining {kept} company(s) only, "
        f"and is not necessarily the ordering over all of them"
    )


def _bind_per_company(
    element: MetricElementIn,
    *,
    slots: list[list[_Candidate]],
    signs: list[str],
    expression: str,
    resolved_by: str,
    source: str,
    caveats: dict[tuple[str, str], str] | None = None,
    ciks: list[int],
    periods: list[ResolvedPeriod],
    evidence: dict[tuple[int, int], _Evidence],
    bindings: list[Binding],
    ambiguous: list[Ambiguity],
    problems: list[Unresolved],
    uncovered_ciks: list[int],
) -> None:
    """Commit bindings per company, splitting where the filer changed tags.

    Coverage is decided **per period**, not once for the whole range, because a
    filer can switch concepts partway through it -- Alphabet moves from
    ``RevenueFromContractWithCustomerExcludingAssessedTax`` to ``Revenues``
    between FY2024 and FY2025, so no single concept answers all five years.
    Periods that resolve to the same concepts are grouped into one binding, so
    the common case (nothing changed) still produces exactly one.

    Preference order does the grouping for free: for each period the first
    covering alternative wins, so two periods agree iff the same alternative
    covers both.

    Ambiguity is reported **once per element**, not once per company. The
    candidates differ per filer -- each keeps whichever concepts its own facts
    cover -- but the question they raise is the same one question, and asking
    it twenty times is not twenty questions. Measured before this merged:
    "total debt" over twenty companies produced nineteen separate records for
    one phrase.
    """
    # Merged across companies: concept_id -> the best sighting of it anywhere.
    tied_candidates: dict[int, _Candidate] = {}
    tied_coverage: dict[int, Coverage] = {}
    weak_ciks: list[int] = []
    weak_best: _Candidate | None = None

    for cik in ciks:
        in_scope = [p for p in periods if p.company_cik == cik]
        if not in_scope:
            continue

        # Keyed on concepts alone. It used to be keyed on (concepts, needs the
        # Q4 subtraction) as well, because a Q4 was computed differently from
        # the quarters beside it; the view supplies Q4 as an ordinary row now,
        # so there is nothing to split a series on but a genuine tag change.
        groups: dict[tuple[int, ...], list[ResolvedPeriod]] = {}
        concept_groups: dict[tuple[int, ...], list[ResolvedPeriod]] = {}
        chosen_by_key: dict[tuple[int, ...], list[_Candidate]] = {}
        uncovered: list[ResolvedPeriod] = []
        tied: list[_Candidate] = []
        weak: _Candidate | None = None

        for period in in_scope:
            chosen: list[_Candidate] = []
            for slot in slots:
                survivors = [
                    c for c in slot if _covers(evidence.get((cik, c.concept_id)), [period])
                ]
                if not survivors:
                    chosen = []
                    break
                if resolved_by == "embedding":
                    if survivors[0].score < _MIN_PLAUSIBLE_SIMILARITY:
                        # Not close enough to be worth offering as a choice.
                        # Offering it anyway is how "competition risk
                        # disclosure" came back as a choice between two
                        # fair-value concepts.
                        weak = survivors[0]
                        chosen = []
                        break
                    if len(survivors) > 1 or survivors[0].score < _MIN_BINDING_SIMILARITY:
                        # Several equally plausible, or one that is only a
                        # guess. Both refuse; the caller gets the candidates.
                        tied = survivors
                        chosen = []
                        break
                chosen.append(survivors[0])

            if weak is not None or tied:
                break
            if not chosen:
                uncovered.append(period)
                continue

            concepts_key = tuple(c.concept_id for c in chosen)
            groups.setdefault(concepts_key, []).append(period)
            concept_groups.setdefault(concepts_key, []).append(period)
            chosen_by_key[concepts_key] = chosen

        if weak is not None:
            weak_ciks.append(cik)
            if weak_best is None or weak.score > weak_best.score:
                weak_best = weak
            continue

        if tied:
            for candidate in tied:
                seen = tied_candidates.get(candidate.concept_id)
                if seen is None or candidate.score > seen.score:
                    tied_candidates[candidate.concept_id] = candidate
                    tied_coverage[candidate.concept_id] = _coverage_for(
                        evidence[(cik, candidate.concept_id)], in_scope
                    )
            continue

        if not groups:
            # Not a refusal on its own. A filer that reports nothing for this
            # metric is ordinary -- JPMorgan tags no OperatingIncomeLoss,
            # correctly for a bank -- and refusing the whole question because
            # one company of twenty cannot answer it is how "highest operating
            # margin" came back as nothing at all. The caller decides: every
            # company uncovered is still a refusal, some of them is a
            # `partial_coverage` note naming who dropped out.
            uncovered_ciks.append(cik)
            continue

        shared_notes = _switch_notes(concept_groups, chosen_by_key, cik=cik, evidence=evidence)
        if uncovered:
            missing = ", ".join(f"FY{p.fiscal_year} {p.fiscal_period}" for p in uncovered)
            shared_notes.append(
                Note(
                    kind="partial_coverage",
                    message=f"No concept reports {element.text!r} for {missing}; "
                    "those periods are absent from the result.",
                )
            )

        for concepts_key, covered in groups.items():
            chosen = chosen_by_key[concepts_key]
            violation = _sign_violation(chosen, signs, cik=cik, periods=covered, evidence=evidence)
            if violation is not None:
                problems.append(Unresolved(element_id=element.id, reason=violation))
                continue
            unit, operand_unit, mismatch = _binding_unit(
                chosen, expression, cik=cik, evidence=evidence
            )
            if unit is None:
                problems.append(Unresolved(element_id=element.id, reason=mismatch))
                continue
            lead = evidence[(cik, chosen[0].concept_id)]
            years = [p.fiscal_year for p in covered]
            span = f"FY{min(years)}-FY{max(years)}"

            # Which alternative won decides what has to be disclosed: a filer's
            # own combined debt line needs no caveat, the long-term fallback
            # omits commercial paper, and the noncurrent one omits more still.
            narrower = [
                Note(kind="narrower_than_asked", message=(caveats or {})[(c.taxonomy, c.name)])
                for c in chosen
                if (c.taxonomy, c.name) in (caveats or {})
            ]

            bindings.append(
                Binding(
                    element_id=element.id,
                    company_cik=cik,
                    periods=[
                        PeriodRef(fiscal_year=p.fiscal_year, fiscal_period=p.fiscal_period)
                        for p in covered
                    ],
                    concepts=[c.as_ref() for c in chosen],
                    expression=expression,
                    unit=unit,
                    operand_unit=operand_unit,
                    is_instant=lead.is_instant,
                    coverage=_coverage_for(lead, covered),
                    confidence=1.0 if resolved_by == "alias" else chosen[0].score,
                    resolved_by=resolved_by,
                    rationale=(
                        f"{element.text!r} -> "
                        + ", ".join(f"{c.taxonomy}:{c.name}" for c in chosen)
                        + f" via {source}; verified over {len(covered)} period(s) ({span})"
                    ),
                    notes=shared_notes + narrower,
                )
            )

    if weak_best is not None:
        problems.append(
            Unresolved(
                element_id=element.id,
                reason=f"nothing in the concept corpus plausibly measures "
                f"{element.text!r} for {_named_ciks(weak_ciks)}; the closest match, "
                f"{weak_best.taxonomy}:{weak_best.name}, scores "
                f"{weak_best.score:.2f} and is not near enough to offer as a choice",
            )
        )

    if tied_candidates:
        ranked = sorted(tied_candidates.values(), key=lambda c: -c.score)[:4]
        ambiguous.append(
            Ambiguity(
                element_id=element.id,
                element_text=element.text,
                candidates=[
                    Candidate(
                        concept=c.as_ref(),
                        score=c.score,
                        coverage=tied_coverage[c.concept_id],
                    )
                    for c in ranked
                ],
            )
        )


def _binding_unit(
    chosen: list[_Candidate],
    expression: str,
    *,
    cik: int,
    evidence: dict[tuple[int, int], _Evidence],
) -> tuple[str | None, str | None, str]:
    """``(result_unit, operand_unit, reason)`` -- or ``(None, None, reason)``.

    Two units, because they are used for different things. The *result* unit
    describes the number a reader sees. The *operand* unit is what the facts
    are filed in, and it is what the retrieval join keys on -- for a ratio
    those differ, and only this function ever sees both. It used to return
    the first and drop the second, which left ``app/retrieval/`` unable to
    render any ratio at all.


    A single-operand binding reports its fact's unit, which is what every
    stored value already is. Arithmetic is where this used to go wrong:
    ``gross_margin`` is ``c0 / c1`` over two USD concepts and the result is
    dimensionless, but the lead operand's unit was copied through, so the plan
    said a ratio of 0.46 was "USD" (PITFALLS §2.1). Anything formatting that
    would render 46 cents.

    Deliberately not a unit algebra -- one rule, matching what the alias file
    actually contains: every expression here divides like-for-like or adds
    like-for-like. A division of equal units gives ``"pure"``, which is XBRL's
    own name for a dimensionless quantity and already appears in the store on
    concepts like ``EffectiveIncomeTaxRateContinuingOperations``.

    Operands in *different* units are refused rather than guessed at. No entry
    does that today; the guard is here so that one written later (a per-share
    figure, say, dividing USD by shares) fails loudly instead of inheriting a
    unit that describes only its numerator.
    """
    units = [evidence[(cik, candidate.concept_id)].unit for candidate in chosen]
    if len(units) == 1:
        # One operand: the result *is* the fact, so there is nothing to carry.
        return units[0], None, ""

    distinct = set(units)
    if len(distinct) > 1:
        return None, None, (
            f"{expression!r} combines operands filed in different units "
            f"({', '.join(sorted(distinct))}) for cik {cik}; the result's unit is not "
            "one of them, so no binding was made"
        )
    operand_unit = units[0]
    return ("pure" if "/" in expression else operand_unit), operand_unit, ""


def _named_ciks(ciks: list[int], limit: int = 4) -> str:
    """Ciks for a refusal message, abbreviated once there are too many to read."""
    if len(ciks) > limit:
        return f"{len(ciks)} companies"
    return "cik " + ", ".join(str(cik) for cik in ciks)


def _switch_notes(
    groups: dict[tuple[int, ...], list[ResolvedPeriod]],
    chosen_by_key: dict[tuple[int, ...], list[_Candidate]],
    *,
    cik: int,
    evidence: dict[tuple[int, int], _Evidence],
) -> list[Note]:
    """Disclose a mid-range concept change, and say whether it was verifiable.

    Where two concepts both report a period, their values are compared. Equal
    values mean the filer dual-tagged one quantity and stitching the series is
    safe -- measured on Alphabet, the two revenue concepts agree exactly in all
    three overlapping years. Unequal values, or no overlap at all, mean the
    switch could be a change of *definition* rather than of tag, and the reader
    has to be told rather than handed a smooth-looking line.
    """
    if len(groups) < 2:
        return []

    ordered = sorted(groups.items(), key=lambda kv: min(p.fiscal_year for p in kv[1]))
    described = "; ".join(
        f"FY{min(p.fiscal_year for p in periods)}-FY{max(p.fiscal_year for p in periods)}: "
        + ", ".join(c.name for c in chosen_by_key[key])
        for key, periods in ordered
    )

    agreed, compared = _seam_agrees(ordered, chosen_by_key, cik=cik, evidence=evidence)
    if compared and agreed:
        return [
            Note(
                kind="concept_switch",
                message=f"Filer changed tags mid-range ({described}). The tags report "
                f"identical values in {compared} overlapping period(s), so the series "
                "was stitched across the change.",
            )
        ]
    if compared:
        return [
            Note(
                kind="unverified_switch",
                message=f"Filer changed tags mid-range ({described}), and the tags "
                f"DISAGREE in {compared} overlapping period(s). The series may not be "
                "continuous; treat comparisons across the change with care.",
            )
        ]
    return [
        Note(
            kind="unverified_switch",
            message=f"Filer changed tags mid-range ({described}), with no overlapping "
            "period to check the two against. The series may not be continuous.",
        )
    ]


def _seam_agrees(
    ordered: list[tuple[tuple[int, ...], list[ResolvedPeriod]]],
    chosen_by_key: dict[tuple[int, ...], list[_Candidate]],
    *,
    cik: int,
    evidence: dict[tuple[int, int], _Evidence],
) -> tuple[bool, int]:
    """``(values agree, how many periods could be compared)`` across the change.

    The overlap to inspect sits in the **earlier** group's periods: preference
    order means the later concept never won a period the earlier one covered,
    so any period where both have facts is one the earlier concept was chosen
    for. Alphabet is the worked example -- ``Revenues`` also reports FY2021,
    FY2023 and FY2024, which ``RevenueFromContractWithCustomerExcludingAssessedTax``
    won on order, and the two agree to the dollar in all three.

    Only the lead operand is compared. A derived metric whose *numerator*
    survives the switch is the case worth catching; comparing every operand
    would report a disagreement for ratios whose denominator legitimately
    moved.
    """
    compared = 0
    for i in range(len(ordered) - 1):
        left_key, left_periods = ordered[i]
        right_key, _ = ordered[i + 1]
        left = evidence.get((cik, chosen_by_key[left_key][0].concept_id))
        right = evidence.get((cik, chosen_by_key[right_key][0].concept_id))
        if left is None or right is None:
            continue
        for period in left_periods:
            for window in _required_windows(period, is_instant=left.is_instant):
                if window in left.values and window in right.values:
                    compared += 1
                    if left.values[window] != right.values[window]:
                        return False, compared
    return True, compared


def _sign_violation(
    chosen: list[_Candidate],
    signs: list[str],
    *,
    cik: int,
    periods: list[ResolvedPeriod],
    evidence: dict[tuple[int, int], _Evidence],
) -> str | None:
    """Reason to refuse, when an operand declared ``magnitude`` arrives negative.

    A magnitude operand is a size whose direction the expression supplies:
    capex is tagged positive and subtracted. A filer tagging it negative turns
    ``c0 - c1`` into an addition and inflates the answer, and nothing
    downstream can tell. That is a wrong number rather than a caveated one, so
    it is refused here for the same reason a missing window is --
    see PITFALLS.md section 1.15.

    Operands declared ``signed`` are left alone: operating cash flow really
    does go negative, and so does gross profit.
    """
    for candidate, sign in zip(chosen, signs, strict=False):
        if sign != "magnitude":
            continue
        found = evidence.get((cik, candidate.concept_id))
        if found is None:
            continue
        wanted = {
            window
            for period in periods
            for window in _required_windows(period, is_instant=found.is_instant)
        }
        negative = [w for w in wanted if w in found.values and found.values[w] < 0]
        if negative:
            when = ", ".join(str(end) for _, end in sorted(negative, key=lambda w: w[1]))
            return (
                f"{candidate.taxonomy}:{candidate.name} is declared a magnitude but cik "
                f"{cik} reports it negative for {when}; the expression would compute the "
                "wrong sign, so no binding was made"
            )
    return None


def _required_windows(
    period: ResolvedPeriod, *, is_instant: bool
) -> list[tuple[date | None, date]]:
    """The window a period needs before it can be answered.

    One window, always. An instant needs only its closing date; a duration
    needs its own span. A Q4 used to need two -- the annual and the nine-month
    it was subtracted from -- but ``xbrl.reported_fact`` supplies the fourth
    quarter as an ordinary row, so there is nothing left here that a Q4 does
    differently from a Q2.
    """
    if is_instant:
        return [(None, period.period_end)]
    return [(period.period_start, period.period_end)]


def _covers(evidence: _Evidence | None, periods: list[ResolvedPeriod]) -> bool:
    if evidence is None:
        return False
    required = {
        window
        for period in periods
        for window in _required_windows(period, is_instant=evidence.is_instant)
    }
    return required <= evidence.windows


def _coverage_for(evidence: _Evidence, periods: list[ResolvedPeriod]) -> Coverage:
    required = [
        window
        for period in periods
        for window in _required_windows(period, is_instant=evidence.is_instant)
    ]
    present = [window for window in required if window in evidence.windows]
    ends = [end for _, end in present]

    return Coverage(
        fact_count=len(present),
        period_min=min(ends) if ends else None,
        period_max=max(ends) if ends else None,
    )


async def _slots_from_alias(session: AsyncSession, hit: AliasHit) -> list[list[_Candidate]]:
    """An alias hit's ``(taxonomy, name)`` refs as loaded concepts.

    Keeps the file's preference order and silently drops refs this database
    has never seen -- a slot left empty by that is reported by the caller.
    """
    refs = {ref for slot in hit.terms for ref in slot}
    rows = await session.execute(
        select(Concept.id, Concept.taxonomy, Concept.name, Concept.label).where(
            tuple_(Concept.taxonomy, Concept.name).in_(refs)
        )
    )
    loaded = {(taxonomy, name): (cid, label) for cid, taxonomy, name, label in rows}
    return [
        [
            _Candidate(
                concept_id=loaded[ref][0],
                taxonomy=ref[0],
                name=ref[1],
                score=1.0,
                label=loaded[ref][1],
            )
            for ref in slot
            if ref in loaded
        ]
        for slot in hit.terms
    ]


async def _nearest_concepts(session: AsyncSession, text: str) -> list[_Candidate]:
    """Concepts nearest ``text`` in embedding space, closest first.

    Returns nothing when the corpus has no embeddings, so a database that has
    not had ``app.db.embedder`` run against it degrades to "no candidates"
    instead of making a pointless call to the embedding service.
    """
    any_embedded = await session.execute(
        select(Concept.id).where(Concept.embedding.is_not(None)).limit(1)
    )
    if any_embedded.first() is None:
        return []

    distance = Concept.embedding.cosine_distance(await embed_query(text))
    rows = await session.execute(
        select(
            Concept.id,
            Concept.taxonomy,
            Concept.name,
            Concept.label,
            distance.label("distance"),
        )
        .where(Concept.embedding.is_not(None), distance < _MAX_EMBEDDING_DISTANCE)
        .order_by(distance)
        .limit(_EMBEDDING_CANDIDATES)
    )
    return [
        _Candidate(
            concept_id=cid,
            taxonomy=taxonomy,
            name=name,
            score=1.0 - float(dist),
            label=label,
        )
        for cid, taxonomy, name, label, dist in rows
    ]


async def _gather_evidence(
    session: AsyncSession,
    *,
    ciks: list[int],
    concept_ids: set[int],
    periods: list[ResolvedPeriod],
) -> dict[tuple[int, int], _Evidence]:
    """What facts exist for each ``(company, candidate concept)`` pair.

    Fetches every window any interpretation might need -- instant closing
    dates and duration spans -- in one query,
    and leaves it to the caller to decide which were actually required.
    """
    instant_dates = {period.period_end for period in periods}
    duration_windows = {(period.period_start, period.period_end) for period in periods}

    rows = await session.execute(
        select(
            reported_fact.c.company_cik,
            reported_fact.c.concept_id,
            reported_fact.c.unit,
            reported_fact.c.is_instant,
            reported_fact.c.period_start,
            reported_fact.c.period_end,
            reported_fact.c.value,
        ).where(
            # The VIEW, not `fact`. Coverage has to be proved against the
            # relation `app/retrieval/` will read, or the mapper can bind a
            # value retrieval cannot fetch. The view also already applies
            # `is_latest`, so that predicate is gone rather than missing.
            reported_fact.c.company_cik.in_(ciks),
            reported_fact.c.concept_id.in_(concept_ids),
            or_(
                and_(
                    reported_fact.c.is_instant.is_(True),
                    reported_fact.c.period_end.in_(instant_dates),
                ),
                and_(
                    reported_fact.c.is_instant.is_(False),
                    tuple_(
                        reported_fact.c.period_start, reported_fact.c.period_end
                    ).in_(duration_windows),
                ),
            ),
        )
    )

    collected: dict[tuple[int, int], list[tuple[str, bool, date | None, date, Decimal]]] = (
        defaultdict(list)
    )
    for cik, concept_id, unit, is_instant, start, end, value in rows:
        collected[(cik, concept_id)].append(
            (unit, is_instant, None if is_instant else start, end, value)
        )

    evidence: dict[tuple[int, int], _Evidence] = {}
    for key, found in collected.items():
        # A concept is either a flow or a balance. If a filer has tagged both,
        # go with whichever it did more often rather than guessing.
        is_instant = Counter(instant for _, instant, _, _, _ in found).most_common(1)[0][0]
        same_kind = [row for row in found if row[1] == is_instant]
        # One unit per binding, and the covered windows must be the windows of
        # THAT unit -- a concept filed in both EUR and USD would otherwise pass
        # coverage on one and be reported as the other. See PITFALLS.md.
        unit = Counter(unit for unit, _, _, _, _ in same_kind).most_common(1)[0][0]
        evidence[key] = _Evidence(
            is_instant=is_instant,
            unit=unit,
            values={(start, end): value for u, _, start, end, value in same_kind if u == unit},
        )
    return evidence
