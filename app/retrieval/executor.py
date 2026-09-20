"""``execute(sql, plan)`` -- run a validated statement, return a ``ResultSet``.

NOT BUILT. The only step here that touches the database, and it connects as
``vf_retrieval_role``, which holds ``SELECT`` on ``xbrl.reported_fact`` and
nothing else (``app/db/roles.py``).

What it owes, when it is written:

* **Rows in, ``ResultRow`` out.** Validation already proved the projection
  matches ``RESULT_COLUMNS``, so this is a construction, not a rescue.
* **Attribution.** Each row resolves through
  ``QueryPlan.binding_for(element_id, cik, fiscal_year, fiscal_period)`` to
  exactly one binding; a derived row (``derivation`` set) resolves coarsely
  through ``bindings_for_company``. A row that resolves to nothing is
  unattributable -- the model invented it -- and the verdict refuses.
* **The verdict.** Difference the plan's expected cells against what came
  back, mark each missing cell ``anticipated`` when the binding already
  carried a ``partial_coverage`` note, and raise ``incomplete_result`` when
  any is not. The row-count equality applies only when every row is
  as-reported (DESIGN §5.1).
* **The log.** The statement that ran is appended to
  ``data/retrieval_log.jsonl``, not carried on the ``ResultSet``: it is the one
  thing in reach that a presenter might quote at a user.

``execute()`` runs what Qwen wrote, so it must be handed a statement that came
back from ``validate()`` -- never the generator's output directly.
"""

from __future__ import annotations

from app.schemas.query import QueryPlan
from app.schemas.result import ResultSet


async def execute(sql: str, plan: QueryPlan) -> ResultSet:
    """Run a **validated** statement and assemble the ``ResultSet``."""
    raise NotImplementedError(
        "execute is not written yet; see app/retrieval/DESIGN.md §5 for the "
        "verdict it has to compute"
    )
