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

from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Company, Fact, Filing
from app.db.session import SessionLocal
from app.schemas.query import (
    Ambiguity,
    Binding,
    CompanyElementIn,
    MetricElementIn,
    PeriodElementIn,
    PeriodResidual,
    PlanFilters,
    QueryIn,
    QueryPlan,
    ResolvedPeriod,
    Unresolved,
)

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
# Metrics -- the part that is not written yet
# --------------------------------------------------------------------------- #


async def _resolve_metrics(
    elements: list[MetricElementIn],
    session: AsyncSession,
    *,
    ciks: list[int],
    periods: list[ResolvedPeriod],
) -> tuple[list[Binding], list[Ambiguity], list[Unresolved]]:
    """NOT IMPLEMENTED. Reports every metric element as unresolved.

    The cascade this becomes, in order:

    1. normalize ``element.text``
    2. curated YAML alias lookup -> ordered candidates. A hit **skips step 3**:
       a curated entry is accounting judgment and re-checking it against cosine
       distance can only add noise.
    3. ``embed_query(text)`` -> top-K nearest ``Concept.embedding``, tuned for
       recall (generous K, loose threshold). A recall net for whatever step 2
       does not cover yet, never a decision.
    4. coverage filter: drop any candidate with no ``Fact`` rows for the
       ``(cik, concept_id)`` pair inside the resolved ``periods``. This is what
       turns a guess into a verified binding, and it outranks similarity
       outright -- a candidate scoring 0.91 with no facts loses to one scoring
       0.78 with twenty.
    5. commit one ``Binding`` per ``(element, cik)``, carrying the ``unit`` and
       ``is_instant`` read off the surviving facts; emit an ``Ambiguity`` when
       more than one candidate survives step 4.

    ``ciks`` and ``periods`` are already resolved when this is called -- step 4
    needs them, which is why the metric pass runs last. Note that coverage has
    to be checked against the *concept that carries the requested granularity*:
    NVDA has annual-only facts under one revenue concept and quarterly facts
    under another, so a concept-level count alone would pass a binding that
    cannot answer a quarterly question.
    """
    return (
        [],
        [],
        [
            Unresolved(
                element_id=element.id,
                reason=f"metric resolver not implemented; {element.text!r} left unbound",
            )
            for element in elements
        ],
    )
