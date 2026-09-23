"""``propose(prompt)`` -- ask the model for a query object. No database, no
judgement, no ``QueryIn``.

The mirror of ``app/retrieval/generator.py``, and the only thing in this
package that talks to a model. What comes back is text; ``accept()`` decides
whether it means anything.

**The same model as retrieval, deliberately.** ``qwen2.5-coder:7b`` is already
pulled, already sized for the card, and Ollama's ``/api/generate`` keeps
nothing between calls -- each request carries its whole prompt and the server
retains none of it afterwards. Two callers of one loaded model are as isolated
from each other as two callers of two models; what is shared is read-only
weights in VRAM. Measured on the GTX 1080 Ti: 11.3 GB total, ~9 GB free, and
this model occupies 4.7 GB. A *second* 7B would not fit alongside it, so the
alternative to sharing is a model swap on every question.

If it proves weak at this, the fix is the constant below and nothing else --
which is the point of putting the model call behind one function.
"""

from __future__ import annotations

import httpx
from ollama import AsyncClient

from app.config import settings
from app.parser.wire import WIRE_SCHEMA

#: The model that proposes the query object. Shared with
#: ``app/retrieval/generator.py``'s ``GENERATION_MODEL`` by value, not by
#: import: they are free to diverge, and a single constant would make that
#: change look accidental.
PARSER_MODEL = "qwen2.5-coder:7b"

#: Deterministic. The parser sits at the head of a pipeline whose whole
#: claim is auditability, and a question that parses differently on Tuesday
#: cannot be regression-tested. It is also what makes a cached ``QueryIn``
#: per eval question meaningful.
TEMPERATURE = 0.0

#: The prompt runs to roughly 2,500 characters of rules and examples before
#: the question is appended, and the repair prompt carries a rejected reply on
#: top of that. Ollama's 4096 default would truncate the far end -- which is
#: where the question itself lives.
CONTEXT_TOKENS = 8192

#: Hard ceiling on the reply. **Without one, a 7B model asked an open question
#: does not stop.** Measured 2026-09-22 on the first full eval run: q050,
#: "Which companies by sic office performed best: highest revenue, profit both
#: quarterly and yearly", produced 195,193 characters -- 2,363 lines of JSON --
#: and then did it again for the repair, taking 4,810 seconds. That one
#: question was 88% of a 91-minute run.
#:
#: 1536 is roughly 2.25x the largest legitimate reply measured on this prompt:
#: the HANDOFF §8 smoke test (three companies, twelve quarters, 16 elements)
#: is 682 tokens, and a seven-company four-metric five-year chart is 439. A
#: question that needs more than this is not a bigger question, it is a model
#: that has stopped tracking the schema.
MAX_OUTPUT_TOKENS = 1536

#: Wall clock for one request, covering a cold model load (~29 s, measured on
#: the first call of an eval run) plus a full ``MAX_OUTPUT_TOKENS`` generation
#: at the ~48 tok/s this card sustains. The cap bounds a *rambling* model; this
#: bounds a *wedged* one, which no token limit reaches.
REQUEST_TIMEOUT = 180.0


class ProposalError(RuntimeError):
    """The model returned nothing to judge.

    Distinct from ``UnacceptableProposal``, which means something *was*
    returned and then refused.
    """


async def propose(prompt: str, *, model: str = PARSER_MODEL) -> str:
    """The model's reply, raw.

    ``format=WIRE_SCHEMA`` constrains decoding to the flat wire shape, so the
    common structural mistakes cannot be made rather than being caught. It
    guarantees well-formedness only -- see ``wire.py`` for why ``QueryIn``
    itself cannot be used as the grammar, and ``acceptor.py`` for what the
    grammar leaves undecided. It does **not** bound length: a schema says what
    a valid reply looks like, not when to stop, and a model can emit elements
    that each satisfy it until something else intervenes. That something is
    ``MAX_OUTPUT_TOKENS``.

    Raises ``ProposalError`` -- never ``UnacceptableProposal`` -- when the
    reply is empty, truncated or times out. All three mean there is nothing to
    judge, and routing them through the repair attempt would spend a second
    full generation re-learning what the first one already showed.
    """
    client = AsyncClient(host=settings.embedding_url, timeout=REQUEST_TIMEOUT)
    try:
        response = await client.generate(
            model=model,
            prompt=prompt,
            stream=False,
            format=WIRE_SCHEMA,
            options={
                "temperature": TEMPERATURE,
                "num_ctx": CONTEXT_TOKENS,
                "num_predict": MAX_OUTPUT_TOKENS,
            },
        )
    except httpx.TimeoutException as exc:
        raise ProposalError(f"{model} did not reply within {REQUEST_TIMEOUT:.0f}s") from exc

    if response.get("done_reason") == "length":
        raise ProposalError(
            f"{model} hit the {MAX_OUTPUT_TOKENS}-token ceiling without finishing "
            f"the object. The reply is truncated, so there is nothing to judge; "
            f"a question needing more than this is one the model has lost track of."
        )

    reply = response.get("response") or ""
    if not reply.strip():
        raise ProposalError(f"{model} returned an empty response")
    return reply
