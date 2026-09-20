"""``build_prompt(plan)`` -- the plan, rendered as text for Qwen. No database.

NOT BUILT. The signature is settled and the statement shape it has to teach is
worked out and verified by hand (``app/retrieval/DESIGN.md`` §4); what is
missing is the rendering.

What it owes, when it is written:

* **The plan's coordinates as a ``VALUES`` list**, not as prose for the model
  to turn into predicates. That is what makes per-company concept divergence
  data rather than SQL cleverness: Apple binds one concept id and NVIDIA
  another, and the difference is two rows of a literal table the statement
  joins against.
* **The projection, named.** Exactly ``RESULT_COLUMNS``
  (``app/schemas/result.py``), because ``validate()`` refuses anything else.
* **``derivation``**, and when to set it. A computed value that comes back
  unmarked is read as the metric it came from, which is the one failure the
  contract exists to prevent (DESIGN §1).
* **The view is the only relation.** Naming ``fact`` or ``filing`` is not a
  style mistake, it is a statement the role cannot run.
"""

from __future__ import annotations

from app.schemas.query import QueryPlan


def build_prompt(plan: QueryPlan) -> str:
    """Render ``plan`` as the text Qwen is asked to write SQL from."""
    raise NotImplementedError(
        "build_prompt is not written yet; see app/retrieval/DESIGN.md §4 for the "
        "statement shape it has to teach"
    )
