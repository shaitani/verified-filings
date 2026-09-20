"""``generate(plan)`` -- ask Qwen for SQL. No database.

Ollama serves the model alongside the embedding one (``docker-compose.yml``);
``app/embedding_client.py`` is the pattern this follows.

``GENERATION_MODEL`` below is authoritative. **It is also named in
``docker-compose.yml``'s pull line and health check, and the two have to be
kept in step** -- the ``ollama`` service pulls its models on startup because
``docker compose down -v`` wipes the model volume along with the database
ones, and a model that only ever arrives by someone running ``ollama pull`` by
hand is the step that goes missing after a wipe. See ``BOOTSTRAP.md``.

**No credential, ever.** The separation this layer rests on is that the model
takes text and returns text. It has no driver, no connection string and no
route to PostgreSQL. What it returns is a *proposal*; ``validate()`` decides
whether it runs.

Whether a rejected statement gets handed back to the model with the
``InvalidSQL`` message and one more attempt is still **undecided**. It is
attractive -- those messages are written to be shown to a model -- but a retry
loop is also how a validator's error text becomes a map of what to get around.
Decide it deliberately; nothing here retries today.
"""

from __future__ import annotations

import re

from ollama import AsyncClient

from app.config import settings
from app.retrieval.prompt import build_prompt
from app.schemas.query import QueryPlan

#: The model that writes the SQL. Mirrored in ``docker-compose.yml``'s pull
#: line and health check -- this constant is authoritative, that is the copy.
#:
#: qwen2.5-coder:7b rather than something larger, because the task is narrow:
#: one relation, the plan's coordinates handed over as a literal VALUES list,
#: and a fixed twelve-column projection that ``validate()`` refuses any
#: deviation from. A weaker model therefore degrades into refusals with
#: specific messages, not into plausible wrong numbers -- which is what makes
#: starting small cheap to be wrong about. Measured on a GTX 1080 Ti: loads
#: 100% onto the card at 4.7GB and generates ~48 tok/s.
GENERATION_MODEL = "qwen2.5-coder:7b"

#: Ollama's default context is 4096 tokens, and a prompt carrying a 36-cell
#: VALUES list plus the base query runs to several thousand. Silently
#: truncating it would drop plan rows off the end, and the model would then
#: write a valid query for a subset of the question -- the failure mode
#: hardest to notice downstream, because everything about the result looks
#: right except how much of it there is.
CONTEXT_TOKENS = 8192

#: Deterministic output. This is code generation against a fixed contract, not
#: prose: nothing here is improved by variety, and a reproducible statement is
#: worth a great deal when something has to be debugged.
TEMPERATURE = 0.0

#: ```sql ... ``` or ``` ... ```, which instruction-tuned models emit whatever
#: they are told. Unwrapping is this module's job, not ``validate()``'s: a
#: fenced answer is a correct answer in the wrong wrapper.
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

#: A bare answer with no fence: take from the first statement keyword on.
_LEADING = re.compile(r"\b(WITH|SELECT)\b", re.IGNORECASE)


class GenerationError(RuntimeError):
    """The model returned nothing usable as a statement.

    Distinct from ``InvalidSQL``, which means a statement *was* produced and
    then refused. This one means there was no statement to refuse.
    """


def extract_sql(reply: str) -> str:
    """Pull the statement out of whatever the model wrapped it in.

    Prefers the **last** fenced block that looks like a statement. A model
    that narrates before answering tends to quote fragments first and put its
    answer last, so taking the first block picks up the commentary.
    """
    blocks = [match.group(1).strip() for match in _FENCE.finditer(reply)]
    candidates = [block for block in blocks if _LEADING.search(block)]
    if candidates:
        return candidates[-1]

    start = _LEADING.search(reply)
    if start is None:
        raise GenerationError(f"the model returned no SELECT: {reply.strip()[:200]!r}")
    return reply[start.start() :].strip()


async def generate(plan: QueryPlan, *, model: str = GENERATION_MODEL) -> str:
    """Ask the model for the SQL that answers ``plan``, and return it.

    The statement is **not** validated here. Callers pass the result to
    ``validate()``, which is what decides whether it runs.
    """
    client = AsyncClient(host=settings.embedding_url)
    response = await client.generate(
        model=model,
        prompt=build_prompt(plan),
        stream=False,
        options={"temperature": TEMPERATURE, "num_ctx": CONTEXT_TOKENS},
    )
    reply = response.get("response") or ""
    if not reply.strip():
        raise GenerationError(f"{model} returned an empty response")
    return extract_sql(reply)
