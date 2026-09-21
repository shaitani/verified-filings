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

from ollama import AsyncClient

from app.config import settings
from app.producer.wire import WIRE_SCHEMA

#: The model that proposes the query object. Shared with
#: ``app/retrieval/generator.py``'s ``GENERATION_MODEL`` by value, not by
#: import: they are free to diverge, and a single constant would make that
#: change look accidental.
PRODUCER_MODEL = "qwen2.5-coder:7b"

#: Deterministic. The producer sits at the head of a pipeline whose whole
#: claim is auditability, and a question that parses differently on Tuesday
#: cannot be regression-tested. It is also what makes a cached ``QueryIn``
#: per eval question meaningful.
TEMPERATURE = 0.0

#: The prompt runs to roughly 2,500 characters of rules and examples before
#: the question is appended, and the repair prompt carries a rejected reply on
#: top of that. Ollama's 4096 default would truncate the far end -- which is
#: where the question itself lives.
CONTEXT_TOKENS = 8192


class ProposalError(RuntimeError):
    """The model returned nothing to judge.

    Distinct from ``UnacceptableProposal``, which means something *was*
    returned and then refused.
    """


async def propose(prompt: str, *, model: str = PRODUCER_MODEL) -> str:
    """The model's reply, raw.

    ``format=WIRE_SCHEMA`` constrains decoding to the flat wire shape, so the
    common structural mistakes cannot be made rather than being caught. It
    guarantees well-formedness only -- see ``wire.py`` for why ``QueryIn``
    itself cannot be used as the grammar, and ``acceptor.py`` for what the
    grammar leaves undecided.
    """
    client = AsyncClient(host=settings.embedding_url)
    response = await client.generate(
        model=model,
        prompt=prompt,
        stream=False,
        format=WIRE_SCHEMA,
        options={"temperature": TEMPERATURE, "num_ctx": CONTEXT_TOKENS},
    )
    reply = response.get("response") or ""
    if not reply.strip():
        raise ProposalError(f"{model} returned an empty response")
    return reply
