"""The query mapper: ``QueryIn`` in, ``QueryPlan`` out.

    from app.semantic.query_mapper import map_query
    plan = await map_query(query)

**Skeleton.** The two deterministic resolvers (company, period) are real; the
metric resolver -- the one that needs the curated alias layer and the embedding
cascade -- is a documented stub that reports every metric element as
unresolved. The shape of what goes in and comes out is therefore exercisable
end to end today, and only ``_resolve_metrics`` has to change to make it
answer anything.

Why the split is drawn there: companies and periods resolve by exact lookup and
arithmetic, and getting them wrong is a bug. Metrics resolve by judgment about
how filers tag things, and getting them wrong is a *research problem* -- so it
is deliberately the one piece left empty rather than filled with a plausible
guess that would be hard to tell apart from a working implementation.

Dependency direction: ``semantic -> (schemas, db)``, never the reverse.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Company, Concept, Fact, Filing
from app.db.session import SessionLocal
from app.embedding_client import embed_query
from app.schemas.query import (
    Ambiguity,
    Binding,
    Candidate,
    CompanyElementIn,
    ComponentCoverage,
    ConceptRef,
    Coverage,
    MetricElementIn,
    PeriodElementIn,
    PeriodResidual,
    PlanFilters,
    QueryIn,
    QueryPlan,
    ResolvedPeriod,
    Unresolved,
)
from app.semantic.aliases import AliasHit, alias_index

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


async def map_query(
    query: QueryIn, *, session_factory: async_sessionmaker = SessionLocal
) -> QueryPlan:
    """Resolve one parsed question into a plan the SQL step can execute.

    Never raises on an unresolvable element -- a partial plan with populated
    ``unresolved`` / ``ambiguous`` is a normal outcome, and the caller decides
    whether to proceed (``plan.is_complete``) or go back to the user.
    """
    companies = [e for e in query.elements if isinstance(e, CompanyElementIn)]
    periods = [e for e in query.elements if isinstance(e, PeriodElementIn)]
    metrics = [e for e in query.elements if isinstance(e, MetricElementIn)]

    async with session_factory() as session:
        ciks, company_problems = await _resolve_companies(companies, session)
        resolved_periods, period_problems = await _resolve_periods(periods, session, ciks=ciks)
        bindings, ambiguous, metric_problems = await _resolve_metrics(
            metrics, session, ciks=ciks, periods=resolved_periods
        )

    return QueryPlan(
        question=query.question,
        intent=query.intent,
        filters=PlanFilters(ciks=ciks, periods=resolved_periods),
        bindings=bindings,
        ambiguous=ambiguous,
        unresolved=company_problems + period_problems + metric_problems,
    )


# --------------------------------------------------------------------------- #
# Companies -- deterministic lookup
# --------------------------------------------------------------------------- #


async def _resolve_companies(
    elements: list[CompanyElementIn], session: AsyncSession
) -> tuple[list[int], list[Unresolved]]:
    ciks: list[int] = []
    problems: list[Unresolved] = []

    for element in elements:
        matches = await _lookup_company(element, session)
        if not matches:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"no loaded company matches {element.text!r}",
                )
            )
        elif len(matches) > 1:
            # Company ambiguity has no home in ``Ambiguity`` -- that model is
            # concept-shaped. Reported here instead, naming the collisions so
            # the caller can re-ask. See DESIGN.md §8.5.
            named = ", ".join(str(cik) for cik in matches)
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"{element.text!r} matches more than one company (ciks: {named})",
                )
            )
        else:
            ciks.append(matches[0])

    return ciks, problems


async def _lookup_company(element: CompanyElementIn, session: AsyncSession) -> list[int]:
    """Ciks matching one company element, most specific hint first.

    Capped at three: the caller only needs to tell "none" from "one" from
    "several", and an unhinted substring like "Inc" would otherwise drag back
    the whole table.
    """
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

    newest = max(fiscal_year for _, fiscal_year, _ in windows)
    resolved: dict[tuple[int, int, str], ResolvedPeriod] = {}
    problems: list[Unresolved] = []

    for element in elements:
        if (element.fiscal_year, element.last_n_years, element.fiscal_period) == (None,) * 3:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"period {element.text!r} carries no fiscal_year, "
                    "last_n_years or fiscal_period",
                )
            )
            continue

        wanted_period = element.fiscal_period or "FY"
        if element.fiscal_year is not None:
            wanted_years = {element.fiscal_year}
        elif element.last_n_years is not None:
            wanted_years = set(range(newest - element.last_n_years + 1, newest + 1))
        else:
            wanted_years = set()  # every year we have a window for

        matched = {
            key: period
            for key, period in windows.items()
            if key[2] == wanted_period and (not wanted_years or key[1] in wanted_years)
        }
        if not matched:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"no {wanted_period} reporting window found for {element.text!r}",
                )
            )
        resolved.update(matched)

    return [resolved[key] for key in sorted(resolved)], problems


def _with_derived_q4(
    windows: dict[tuple[int, int, str], tuple[date, date]],
) -> dict[tuple[int, int, str], ResolvedPeriod]:
    """Every stored window, plus a synthesized Q4 wherever a company has both
    an annual and a Q3 window for the same fiscal year.

    No US filer files a fourth quarter -- the 10-K absorbs it -- so Q4 is the
    only period that has to be constructed. It needs no extra query: it runs
    from the day after Q3 closes to the fiscal year end, and the nine-month
    term the SQL step subtracts is the annual window truncated at Q3's close.
    Q3's discrete window already ends on exactly that day.

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
            residual_of=PeriodResidual(
                shared_start=annual_start,
                whole_end=annual_end,
                subtract_end=nine_month_end,
            ),
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


@dataclass(frozen=True)
class _Candidate:
    """One concept under consideration for one operand slot."""

    concept_id: int
    taxonomy: str
    name: str
    score: float


@dataclass(frozen=True)
class _Evidence:
    """What the facts say about one ``(company, concept)`` pair."""

    is_instant: bool
    unit: str
    windows: frozenset[tuple[date | None, date]]


async def _resolve_metrics(
    elements: list[MetricElementIn],
    session: AsyncSession,
    *,
    ciks: list[int],
    periods: list[ResolvedPeriod],
) -> tuple[list[Binding], list[Ambiguity], list[Unresolved]]:
    """Bind every metric element to concepts that provably have the facts.

    The cascade, in order:

    1. curated alias lookup (``aliases.yaml``). A hit **skips step 2** -- a
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
        return [], [], []

    blockers = []
    if not ciks:
        blockers.append("no company in scope to verify coverage against")
    if not periods:
        blockers.append("no reporting period in scope to verify coverage against")
    if blockers:
        reason = "; ".join(blockers)
        return [], [], [Unresolved(element_id=e.id, reason=reason) for e in elements]

    index = alias_index()
    bindings: list[Binding] = []
    ambiguous: list[Ambiguity] = []
    problems: list[Unresolved] = []

    for element in elements:
        hit = index.lookup(element.text)
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
            resolved_by = "embedding"
            source = "embedding search"

        evidence = await _gather_evidence(
            session,
            ciks=ciks,
            concept_ids={c.concept_id for slot in slots for c in slot},
            periods=periods,
        )
        _bind_per_company(
            element,
            slots=slots,
            expression=expression,
            resolved_by=resolved_by,
            source=source,
            ciks=ciks,
            periods=periods,
            evidence=evidence,
            bindings=bindings,
            ambiguous=ambiguous,
            problems=problems,
        )

    return bindings, ambiguous, problems


def _bind_per_company(
    element: MetricElementIn,
    *,
    slots: list[list[_Candidate]],
    expression: str,
    resolved_by: str,
    source: str,
    ciks: list[int],
    periods: list[ResolvedPeriod],
    evidence: dict[tuple[int, int], _Evidence],
    bindings: list[Binding],
    ambiguous: list[Ambiguity],
    problems: list[Unresolved],
) -> None:
    """Commit one binding per company, or say why there isn't one."""
    for cik in ciks:
        in_scope = [p for p in periods if p.company_cik == cik]
        if not in_scope:
            continue

        chosen: list[_Candidate] = []
        tied: list[_Candidate] = []
        for slot in slots:
            survivors = [c for c in slot if _covers(evidence.get((cik, c.concept_id)), in_scope)]
            if not survivors:
                chosen = []
                break
            if resolved_by == "embedding" and len(survivors) > 1:
                tied = survivors
                chosen = []
                break
            chosen.append(survivors[0])

        if tied:
            ambiguous.append(
                Ambiguity(
                    element_id=element.id,
                    candidates=[
                        Candidate(
                            concept=ConceptRef(
                                concept_id=c.concept_id, taxonomy=c.taxonomy, name=c.name
                            ),
                            score=c.score,
                            coverage=_coverage_for(evidence[(cik, c.concept_id)], in_scope),
                        )
                        for c in tied[:4]
                    ],
                )
            )
            continue

        if not chosen:
            problems.append(
                Unresolved(
                    element_id=element.id,
                    reason=f"{element.text!r} has no concept with facts covering every "
                    f"requested period for cik {cik}",
                )
            )
            continue

        lead = evidence[(cik, chosen[0].concept_id)]
        residual = not lead.is_instant and any(p.residual_of for p in in_scope)

        bindings.append(
            Binding(
                element_id=element.id,
                company_cik=cik,
                concepts=[
                    ConceptRef(concept_id=c.concept_id, taxonomy=c.taxonomy, name=c.name)
                    for c in chosen
                ],
                expression=expression,
                unit=lead.unit,
                is_instant=lead.is_instant,
                period_rule="residual" if residual else "direct",
                coverage=_coverage_for(lead, in_scope, residual=residual),
                confidence=1.0 if resolved_by == "alias" else chosen[0].score,
                resolved_by=resolved_by,
                rationale=(
                    f"{element.text!r} -> "
                    + ", ".join(f"{c.taxonomy}:{c.name}" for c in chosen)
                    + f" via {source}; verified against {len(in_scope)} requested period(s)"
                    + (" as a Q4 residual" if residual else "")
                ),
            )
        )


def _required_windows(
    period: ResolvedPeriod, *, is_instant: bool
) -> list[tuple[date | None, date]]:
    """The fact windows a period needs before it can be answered.

    An instant only ever needs its closing date -- including at Q4, where the
    fiscal-year-end balance already *is* the Q4-end balance. A duration at Q4
    needs both residual components, because subtracting a window that is not
    there does not fail; it quietly returns the whole year.
    """
    if is_instant:
        return [(None, period.period_end)]
    if period.residual_of is not None:
        residual = period.residual_of
        return [
            (residual.shared_start, residual.whole_end),
            (residual.shared_start, residual.subtract_end),
        ]
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


def _coverage_for(
    evidence: _Evidence, periods: list[ResolvedPeriod], *, residual: bool = False
) -> Coverage:
    required = [
        window
        for period in periods
        for window in _required_windows(period, is_instant=evidence.is_instant)
    ]
    present = [window for window in required if window in evidence.windows]
    ends = [end for _, end in present]

    components: list[ComponentCoverage] = []
    if residual:
        seen: set[tuple[date, date]] = set()
        for period in periods:
            if period.residual_of is None:
                continue
            for start, end in _required_windows(period, is_instant=False):
                if (start, end) in seen:
                    continue
                seen.add((start, end))
                components.append(
                    ComponentCoverage(
                        period_start=start,
                        period_end=end,
                        fact_count=int((start, end) in evidence.windows),
                    )
                )

    return Coverage(
        fact_count=len(present),
        period_min=min(ends) if ends else None,
        period_max=max(ends) if ends else None,
        components=components,
    )


async def _slots_from_alias(session: AsyncSession, hit: AliasHit) -> list[list[_Candidate]]:
    """An alias hit's ``(taxonomy, name)`` refs as loaded concepts.

    Keeps the file's preference order and silently drops refs this database
    has never seen -- a slot left empty by that is reported by the caller.
    """
    refs = {ref for slot in hit.terms for ref in slot}
    rows = await session.execute(
        select(Concept.id, Concept.taxonomy, Concept.name).where(
            tuple_(Concept.taxonomy, Concept.name).in_(refs)
        )
    )
    loaded = {(taxonomy, name): concept_id for concept_id, taxonomy, name in rows}
    return [
        [
            _Candidate(concept_id=loaded[ref], taxonomy=ref[0], name=ref[1], score=1.0)
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
        select(Concept.id, Concept.taxonomy, Concept.name, distance.label("distance"))
        .where(Concept.embedding.is_not(None), distance < _MAX_EMBEDDING_DISTANCE)
        .order_by(distance)
        .limit(_EMBEDDING_CANDIDATES)
    )
    return [
        _Candidate(concept_id=cid, taxonomy=taxonomy, name=name, score=1.0 - float(dist))
        for cid, taxonomy, name, dist in rows
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
    dates and duration spans, residual components included -- in one query,
    and leaves it to the caller to decide which were actually required.
    """
    instant_dates = {period.period_end for period in periods}
    duration_windows: set[tuple[date, date]] = set()
    for period in periods:
        if period.residual_of is not None:
            residual = period.residual_of
            duration_windows.add((residual.shared_start, residual.whole_end))
            duration_windows.add((residual.shared_start, residual.subtract_end))
        else:
            duration_windows.add((period.period_start, period.period_end))

    rows = await session.execute(
        select(
            Fact.company_cik,
            Fact.concept_id,
            Fact.unit,
            Fact.is_instant,
            Fact.period_start,
            Fact.period_end,
        ).where(
            Fact.company_cik.in_(ciks),
            Fact.concept_id.in_(concept_ids),
            Fact.is_latest.is_(True),
            or_(
                and_(Fact.is_instant.is_(True), Fact.period_end.in_(instant_dates)),
                and_(
                    Fact.is_instant.is_(False),
                    tuple_(Fact.period_start, Fact.period_end).in_(duration_windows),
                ),
            ),
        )
    )

    collected: dict[tuple[int, int], list[tuple[str, bool, date | None, date]]] = defaultdict(list)
    for cik, concept_id, unit, is_instant, start, end in rows:
        collected[(cik, concept_id)].append((unit, is_instant, None if is_instant else start, end))

    evidence: dict[tuple[int, int], _Evidence] = {}
    for key, found in collected.items():
        # A concept is either a flow or a balance. If a filer has tagged both,
        # go with whichever it did more often rather than guessing.
        is_instant = Counter(instant for _, instant, _, _ in found).most_common(1)[0][0]
        same_kind = [row for row in found if row[1] == is_instant]
        evidence[key] = _Evidence(
            is_instant=is_instant,
            unit=Counter(unit for unit, _, _, _ in same_kind).most_common(1)[0][0],
            windows=frozenset((start, end) for _, _, start, end in same_kind),
        )
    return evidence
