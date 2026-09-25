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

import httpx
from ollama import AsyncClient

from app.config import settings
from app.retrieval.prompt import build_prompt, emit_cte, plan_cells
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

#: Hard ceiling on the statement. Nothing here stopped before this: q015 of the
#: eval set, "show me every company whose gross margin fell three years
#: running", ran for **42 minutes** and was still going when it was killed --
#: the same unbounded-generation defect ``app/parser/proposer.py`` carries a
#: fix for, one function behind the same interface.
#:
#: 4096 rather than something tighter, because real SQL here is long. Measured
#: 2026-09-22: q014, a 40-cell growth query that passes 40/40, generates
#: **2,513 output tokens**; q009 at 16 cells generates 2,242. A cap near either
#: number would refuse working questions, so this sits at roughly 1.6x the
#: largest legitimate statement seen.
MAX_OUTPUT_TOKENS = 4096

#: Wall clock for one request. The token ceiling is the real control -- it
#: bounds a model that will not stop -- and this is the backstop for a server
#: that never answers at all, which no token limit reaches.
#:
#: **Not one minute.** That was the ask, and it was measured wrong: q014 spends
#: 73.2 seconds generating, all of it output (prompt evaluation is ~0s once the
#: context is cached), and it passes. A 60s cap would trade a hang for a false
#: failure on a question that works. 150s clears the measured maximum with room
#: for a cold model load, and the token ceiling binds well before it.
REQUEST_TIMEOUT = 150.0

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

    **The model writes only the SELECT.** The coordinate CTE in front of it is
    written by ``emit_cte`` from the plan's own typed objects, and this function
    joins the two. That is the division ``app/retrieval/__init__.py`` describes:
    fetching the values a ``Binding`` names is deterministic work, and the model
    writes the layer above it.

    The join is done here rather than in ``execute`` so that what ``validate()``
    judges is exactly what runs -- the whole statement, CTE included. Nothing
    downstream sees a half-statement.

    The statement is **not** validated here. Callers pass the result to
    ``validate()``, which is what decides whether it runs.

    Raises ``GenerationError`` when the model produced no usable statement --
    empty, truncated at ``MAX_OUTPUT_TOKENS``, or never answered. A truncated
    statement is **not** handed on to ``validate()``: half a SELECT can still
    parse and still run, and a query that lost its last join returns rows that
    look like an answer. "The model did not finish" is not a contract
    violation, so it travels on the channel that already means the machinery
    failed rather than the one that means the model wrote something wrong.
    """
    client = AsyncClient(host=settings.embedding_url, timeout=REQUEST_TIMEOUT)
    try:
        response = await client.generate(
            model=model,
            prompt=build_prompt(plan),
            stream=False,
            options={
                "temperature": TEMPERATURE,
                "num_ctx": CONTEXT_TOKENS,
                "num_predict": MAX_OUTPUT_TOKENS,
            },
        )
    except httpx.TimeoutException as exc:
        raise GenerationError(
            f"{model} did not finish the SQL within {REQUEST_TIMEOUT:.0f}s"
        ) from exc

    if response.get("done_reason") == "length":
        raise GenerationError(
            f"{model} hit the {MAX_OUTPUT_TOKENS}-token ceiling without finishing the "
            f"statement, so what it wrote is truncated and will not be run"
        )

    reply = response.get("response") or ""
    if not reply.strip():
        raise GenerationError(f"{model} returned an empty response")
    return _splice(plan, extract_sql(reply))


def _splice(plan: QueryPlan, select: str) -> str:
    """Put the generated SELECT behind the CTE this package wrote.

    The model is told to begin at ``SELECT``, and mostly does. When it opens
    with its own ``WITH`` anyway the two cannot be concatenated -- the result
    would be ``WITH ... WITH ...``, which does not parse -- so that is a
    generation failure, reported as one rather than handed to ``validate()``
    as a mystery syntax error.
    """
    body = select.strip().rstrip(";").strip()
    if body.upper().startswith("WITH"):
        raise GenerationError(
            "the model opened with its own WITH clause. The coordinate CTE is "
            "written for it and a second one replaces it, so the reply has to "
            "begin at SELECT"
        )
    if not body.upper().startswith("SELECT"):
        raise GenerationError(
            f"the reply does not begin at SELECT: {body[:80]!r}"
        )
    return emit_cte(plan_cells(plan)) + chr(10) + body
