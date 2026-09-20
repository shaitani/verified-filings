"""Plan -> SQL -> rows. The layer between a ``QueryPlan`` and a rendered answer.

Four steps, of which only the last touches the database::

    build_prompt(plan)   -> str        the plan, rendered for Qwen      no DB
    generate(plan)       -> str        calls Qwen, returns SQL          no DB
    validate(sql)        -> str        raises, or returns the statement no DB
    execute(sql, plan)   -> ResultSet  reads as vf_retrieval_role

Qwen never holds a credential. It is a language model behind an HTTP endpoint:
it takes text and returns text, and ``execute()`` runs what it wrote as a role
that can reach exactly one relation -- the ``xbrl.reported_fact`` view.

Qwen also does not write all of the SQL. Retrieval -- getting the values a
``Binding`` names -- is bounded to four shapes (direct/residual x
instant/duration) and should be deterministic code. Qwen writes the layer
*above* it: ranking, growth, ratios across companies, filters on computed
values. Measured on the eval set, 20 of 45 answerable questions (44%) need
that layer.

``answer()`` is the three of them in the one order that is load-bearing:
generate, **validate**, execute. Calling ``execute()`` on a generator's output
directly is the mistake this package exists to prevent.

See ``app/retrieval/DESIGN.md``.
"""

from app.retrieval.executor import execute
from app.retrieval.generator import GENERATION_MODEL, GenerationError, generate
from app.retrieval.prompt import UnsupportedPlan, base_query, build_prompt, plan_cells
from app.retrieval.validator import MAX_ROWS, InvalidSQL, validate
from app.schemas.query import QueryPlan
from app.schemas.result import ResultSet

__all__ = [
    "GENERATION_MODEL",
    "MAX_ROWS",
    "GenerationError",
    "InvalidSQL",
    "UnsupportedPlan",
    "answer",
    "base_query",
    "build_prompt",
    "execute",
    "generate",
    "plan_cells",
    "validate",
]


async def answer(plan: QueryPlan, *, model: str = GENERATION_MODEL) -> ResultSet:
    """Plan in, rows out: generate, validate, execute.

    Raises rather than returning a half-answer. ``GenerationError`` means the
    model produced no statement; ``InvalidSQL`` means it produced one this
    layer will not run; ``UnsupportedPlan`` means the plan itself cannot be
    rendered yet. A ``ResultSet`` that comes back may still be unanswerable --
    check ``is_answerable`` -- because that is a judgement about the *data*,
    not about whether the machinery worked.
    """
    return await execute(validate(await generate(plan, model=model)), plan)
