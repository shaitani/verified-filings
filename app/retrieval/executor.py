"""``execute(sql, plan)`` -- run a validated statement, return a ``ResultSet``.

The only step that touches the database, and it connects as
``vf_retrieval_role``, which holds ``SELECT`` on ``xbrl.reported_fact`` and
nothing else (``app/db/roles.py``).

Three things happen here, in order, and only the first involves the database:

1. **Run it.** ``validate()`` has already proved the projection matches
   ``RESULT_COLUMNS``, so building rows is a construction, not a rescue.
2. **Attribute.** Each row resolves through the *plan* to the binding that
   answers for it. Nothing in the attribution comes from the model, because
   the model is the thing being checked.
3. **Judge.** Difference what the plan promised against what came back, and
   decide whether the result is answerable at all.

``execute()`` must be handed a statement that came back from ``validate()``,
never the generator's output directly. That is the one ordering in this
package that is load-bearing rather than tidy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.db.session import RetrievalSessionLocal
from app.retrieval.prompt import plan_cells
from app.schemas.query import Note, QueryPlan
from app.schemas.result import (
    AnnotatedRow,
    Citation,
    MissingCell,
    ResultRow,
    ResultSet,
    ResultVerdict,
)

#: Where the statement that ran is recorded. Deliberately *not* on the
#: ``ResultSet``: it is wanted for debugging, and it is the one thing in reach
#: that a presenter might quote at a user. ``data/`` is already gitignored.
LOG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "retrieval_log.jsonl"


def _log(sql: str, plan: QueryPlan, verdict: ResultVerdict) -> None:
    """Append one line. Logging must never be the reason an answer fails, so
    every error here is swallowed -- a missing log line costs a debugging
    session, a raised one costs the answer."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "question": plan.question,
            "sql": sql,
            "verdict": verdict.model_dump(mode="json"),
        }
        with LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _citations(plan: QueryPlan) -> dict[str, Citation]:
    return {
        QueryPlan.binding_key(index): Citation(
            binding_key=QueryPlan.binding_key(index),
            element_id=binding.element_id,
            company_cik=binding.company_cik,
            concepts=list(binding.concepts),
            expression=binding.expression,
            unit=binding.unit,
            resolved_by=binding.resolved_by,
            confidence=binding.confidence,
            rationale=binding.rationale,
            notes=list(binding.notes),
        )
        for index, binding in enumerate(plan.bindings)
    }


def _attribute(row: ResultRow, plan: QueryPlan) -> list[str]:
    """Which binding(s) a row's value came from.

    An as-reported row resolves to exactly one binding on the full cell key.
    A *derived* row cannot: a growth figure spanning a tag change is computed
    across two concepts, and the row's single period label names only one end
    of it. So it resolves on ``(element_id, company_cik)`` and reports every
    binding that could have contributed -- "computed from these", which is
    true, rather than a precision the row does not have.

    An empty list means the row matched nothing in the plan. The model
    invented it, and the verdict refuses on that.
    """
    if row.derivation is None:
        try:
            index, _ = plan.binding_for(
                row.element_id, row.company_cik, row.fiscal_year, row.fiscal_period
            )
        except (LookupError, ValueError):
            return []
        return [QueryPlan.binding_key(index)]

    return [
        QueryPlan.binding_key(index)
        for index, _ in plan.bindings_for_company(row.element_id, row.company_cik)
    ]


def _anticipated(plan: QueryPlan, element_id: str, cik: int) -> bool:
    """Whether the plan already disclosed that this cell might be absent.

    ``partial_coverage`` is the mapper saying up front that some requested
    periods have no facts. A gap it predicted is a caveat; one it did not is a
    fault in the query or the run.
    """
    for binding in plan.bindings:
        if binding.element_id != element_id:
            continue
        if binding.company_cik not in (None, cik):
            continue
        if any(note.kind == "partial_coverage" for note in binding.notes):
            return True
    return False


def _wrong_unit(rows: list[AnnotatedRow], plan: QueryPlan) -> list[int]:
    """Rows whose unit is not the unit the plan says the metric has.

    The check exists for arithmetic the model might skip. ``gross_margin`` is
    ``c0 / c1`` over two USD concepts and its answer is ``pure``; a row that
    comes back ``USD`` is a gross *profit* wearing a margin's label, and no
    count or attribution would notice -- both operands exist, one row came
    back, the binding is right.

    Only as-reported rows are checked. A derived row's unit is whatever the
    model computed and the plan has no opinion on it.
    """
    wrong = []
    for index, annotated in enumerate(rows):
        if annotated.row.derivation is not None or not annotated.binding_keys:
            continue
        binding = plan.bindings[int(annotated.binding_keys[0][1:])]
        if annotated.row.unit != binding.unit:
            wrong.append(index)
    return wrong


def _verdict(rows: list[AnnotatedRow], plan: QueryPlan) -> ResultVerdict:
    expected = plan_cells(plan)
    expected_keys = {
        (cell.element_id, cell.company_cik, cell.fiscal_year, cell.fiscal_period)
        for cell in expected
    }
    derived = any(annotated.row.derivation is not None for annotated in rows)

    returned_keys = {
        (r.row.element_id, r.row.company_cik, r.row.fiscal_year, r.row.fiscal_period)
        for r in rows
        if r.row.derivation is None
    }
    missing = [
        MissingCell(
            element_id=element_id,
            company_cik=cik,
            fiscal_year=year,
            fiscal_period=period,
            anticipated=_anticipated(plan, element_id, cik),
        )
        for element_id, cik, year, period in sorted(expected_keys - returned_keys)
    ]

    unattributable = [index for index, r in enumerate(rows) if not r.binding_keys]
    # A row in the wrong unit is not attributable to its binding in any useful
    # sense: the binding says `pure` and the row says `USD`, so whatever it
    # holds is not what the binding promised.
    unattributable = sorted(set(unattributable) | set(_wrong_unit(rows, plan)))

    if not rows:
        status = "empty"
    elif derived:
        # The row-count equality describes the grid of *filed* values, and a
        # five-year growth series has four points. What still has to hold is
        # that no company or metric was dropped (DESIGN §5.1).
        expected_pairs = {(cell.element_id, cell.company_cik) for cell in expected}
        returned_pairs = {(r.row.element_id, r.row.company_cik) for r in rows}
        status = "complete" if returned_pairs >= expected_pairs else "partial"
        missing = [] if status == "complete" else missing
    elif len(rows) > len(expected):
        status = "over"
    elif missing:
        status = "partial"
    else:
        status = "complete"

    return ResultVerdict(
        status=status,
        expected_rows=len(expected),
        returned_rows=len(rows),
        missing=missing,
        unattributable=unattributable,
    )


def _notes(plan: QueryPlan, verdict: ResultVerdict) -> list[Note]:
    notes = list(plan.notes)
    undisclosed = [cell for cell in verdict.missing if not cell.anticipated]
    if undisclosed:
        notes.append(
            Note(
                kind="incomplete_result",
                message=(
                    f"{len(undisclosed)} of {verdict.expected_rows} value(s) the plan "
                    f"proved were present did not come back. The plan checked coverage "
                    f"before binding, so this is a fault in the query or the run, not "
                    f"in the filings."
                ),
            )
        )
    if verdict.status == "over":
        notes.append(
            Note(
                kind="incomplete_result",
                message=(
                    f"{verdict.returned_rows} rows came back where the plan named "
                    f"{verdict.expected_rows}. A join has fanned out, so any total or "
                    f"average over this result double-counts."
                ),
            )
        )
    return notes


async def execute(sql: str, plan: QueryPlan) -> ResultSet:
    """Run a **validated** statement and assemble the ``ResultSet``."""
    async with RetrievalSessionLocal() as session:
        result = await session.execute(text(sql))
        records = result.mappings().all()

    rows = [
        AnnotatedRow(row=row, binding_keys=_attribute(row, plan))
        for row in (ResultRow(**dict(record)) for record in records)
    ]
    verdict = _verdict(rows, plan)
    _log(sql, plan, verdict)

    return ResultSet(
        question=plan.question,
        rows=rows,
        verdict=verdict,
        citations=_citations(plan),
        notes=_notes(plan, verdict),
    )
