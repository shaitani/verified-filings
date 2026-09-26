"""``add_period_changes(result, plan)`` -- the change from each period to the next.

Computed here, in Python, rather than asked of the model. The model is good at
fetching the figures -- the as-filed worked example, unchanged -- and has
failed at the arithmetic above them in three different ways (q010 put the
growth rate in ``derivation``; see ``app/retrieval/DESIGN.md`` §4.7). The
change between two adjacent points is fixed arithmetic over rows that are
already here, so nothing about it needs a model.

Runs after ``execute()`` has judged the filed rows, so the row-count promise is
checked against exactly what the plan named; the change rows are added on top
and counted into both sides of the verdict.
"""

from __future__ import annotations

from app.retrieval.prompt import PlanCell, plan_cells, wants_period_changes
from app.schemas.query import Note, QueryPlan
from app.schemas.result import AnnotatedRow, ResultRow, ResultSet, ResultVerdict

#: What a change row says it is. Read by the presenter as text.
DERIVATION = "change_from_prior"


def _granularity(cell: PlanCell) -> str:
    return "annual" if cell.fiscal_period == "FY" else "quarterly"


def _steps(plan: QueryPlan) -> list[tuple[PlanCell, PlanCell]]:
    """Adjacent ``(prior, current)`` cells: same element, company and
    granularity, consecutive in the plan's own period order.

    Adjacency comes from the *plan*, not from whichever rows came back, so a
    missing year is a gap nothing is computed across rather than a silent
    two-year step.
    """
    series: dict[tuple[str, int, str], list[PlanCell]] = {}
    for cell in plan_cells(plan):
        series.setdefault((cell.element_id, cell.company_cik, _granularity(cell)), []).append(cell)
    steps = []
    for cells in series.values():
        cells.sort(key=lambda cell: cell.period_end)
        steps += list(zip(cells, cells[1:], strict=False))
    return steps


def add_period_changes(result: ResultSet, plan: QueryPlan) -> ResultSet:
    """``result`` with a change row after each adjacent pair of filed figures.

    Returned unchanged when the plan does not qualify, when the result is not
    answerable (nothing is built on a result the verdict refused), or when any
    row is already derived.
    """
    if (
        not wants_period_changes(plan)
        or not result.is_answerable
        or any(annotated.row.derivation is not None for annotated in result.rows)
    ):
        return result

    filed = {
        (a.row.element_id, a.row.company_cik, a.row.fiscal_year, a.row.fiscal_period): a
        for a in result.rows
    }
    added: list[AnnotatedRow] = []
    skipped: list[str] = []
    for prior_cell, cell in _steps(plan):
        prior = filed.get(
            (prior_cell.element_id, prior_cell.company_cik, prior_cell.fiscal_year,
             prior_cell.fiscal_period)
        )
        current = filed.get(
            (cell.element_id, cell.company_cik, cell.fiscal_year, cell.fiscal_period)
        )
        if prior is None or current is None:
            continue
        base = prior.row.value
        if base is None or current.row.value is None:
            continue
        if base <= 0:
            skipped.append(
                f"{current.row.ticker or current.row.company_cik} "
                f"FY{prior.row.fiscal_year} {prior.row.fiscal_period}"
            )
            continue
        row = current.row
        added.append(
            AnnotatedRow(
                row=ResultRow(
                    element_id=row.element_id,
                    company_cik=row.company_cik,
                    ticker=row.ticker,
                    entity_name=row.entity_name,
                    fiscal_year=row.fiscal_year,
                    fiscal_period=row.fiscal_period,
                    period_start=row.period_start,
                    period_end=row.period_end,
                    is_instant=row.is_instant,
                    value=(row.value - base) / base,
                    unit="pure",
                    derivation=DERIVATION,
                ),
                # Both ends: across a tag change the step spans two bindings.
                binding_keys=sorted(set(prior.binding_keys) | set(current.binding_keys)),
            )
        )

    if not added and not skipped:
        return result

    notes = list(result.notes)
    if skipped:
        notes.append(
            Note(
                kind="narrower_than_asked",
                message=(
                    "No percentage change is given from a zero or negative figure "
                    f"({', '.join(skipped)}): a change from a loss has no meaningful "
                    "percentage"
                )[:512],
            )
        )
    verdict = result.verdict
    # Built, not copied: ResultSet's own consistency checks run again.
    return ResultSet(
        question=result.question,
        rows=[*result.rows, *added],
        verdict=ResultVerdict(
            status=verdict.status,
            expected_rows=verdict.expected_rows + len(added),
            returned_rows=verdict.returned_rows + len(added),
            missing=verdict.missing,
            unattributable=verdict.unattributable,
        ),
        citations=result.citations,
        notes=notes,
    )
