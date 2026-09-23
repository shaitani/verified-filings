"""Question -> ``QueryIn``. Block [C] of the chain; the head of the pipeline.

Three steps, of which only the middle one talks to a model::

    build_question_prompt(question)  -> str       rules and examples   no model
    propose(prompt)                  -> str       the only model call  no schema
    accept(reply, question)          -> QueryIn   judges, or refuses   no model

``parse_question()`` runs them in order, with exactly one repair attempt. Each
function does one job and only it does that job, the same split
``app/retrieval/`` uses, with its own names so nothing collides on import.

**The model proposes; this package decides.** Nothing the model returns is
trusted: ``accept()`` re-derives a ``QueryIn`` from the reply and refuses
anything that does not fit, and the ``question`` handed to the mapper is the
caller's string, never the model's restatement of it. By the time a
``QueryIn`` leaves here, the fact that a language model was involved is not
observable downstream.

**What keeps this safe with a 7B model** is the faithfulness gate in
``accept()`` -- every element's ``text`` must appear in the question. Of the
ways a parser can be wrong, most either fail loudly (bad structure) or cost
a refusal (an unknown company, which the mapper's deterministic lookup
rejects). The one that costs a *wrong answer* is a paraphrase, because the
mapper resolves the replacement phrase perfectly and everything downstream
agrees. That is checked in code rather than asked for in the prompt.

**What this does not do, on purpose.** It does not decide whether the question
is answerable from filed facts at all. "Did any of these companies restate its
revenue?" parses into perfectly good elements and comes back with a revenue
series -- see HANDOFF §6. Closing that needs the eval set to be runnable, and
the eval set needs this module, so it is deliberately the next thing rather
than part of this one.
"""

from app.parser.acceptor import (
    MalformedProposal,
    UnacceptableProposal,
    UnfaithfulSpan,
    accept,
    check_span,
    extract_json,
    normalize,
)
from app.parser.prompt import build_question_prompt, build_repair_prompt
from app.parser.proposer import (
    CONTEXT_TOKENS,
    PARSER_MODEL,
    TEMPERATURE,
    ProposalError,
    propose,
)
from app.parser.wire import WIRE_SCHEMA, WireElement, WireQuery
from app.schemas.query import QueryIn

__all__ = [
    "CONTEXT_TOKENS",
    "MalformedProposal",
    "PARSER_MODEL",
    "ProposalError",
    "TEMPERATURE",
    "UnacceptableProposal",
    "UnfaithfulSpan",
    "WIRE_SCHEMA",
    "WireElement",
    "WireQuery",
    "accept",
    "build_question_prompt",
    "build_repair_prompt",
    "check_span",
    "extract_json",
    "normalize",
    "parse_question",
    "propose",
]

#: ``QueryIn.question`` caps at 2000 characters. Checked here rather than
#: letting Pydantic raise at the end, because a question that is too long is
#: the *caller's* fault and the model should not be asked to fix it -- nor
#: should the run cost a generation to find out.
MAX_QUESTION = 2000


async def parse_question(
    question: str,
    *,
    answers: list[tuple[str, str]] | None = None,
    model: str = PARSER_MODEL,
) -> QueryIn:
    """One question, parsed into a ``QueryIn`` the mapper can resolve.

    ``answers`` carries the clarification round trip -- ``(question_put,
    choice)`` pairs from a previous ``QueryPlan.clarifications``. The original
    question is re-parsed with them appended rather than patched in place, so
    there is exactly one path from words to elements.

    Raises ``UnacceptableProposal`` when two attempts both fail, and
    ``ProposalError`` when the model returned nothing at all. Both are
    outcomes the caller reports; neither is an internal error.

    One retry, not a loop: see ``build_repair_prompt``.
    """
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    if len(question) > MAX_QUESTION:
        raise ValueError(
            f"question is {len(question)} characters; the limit is {MAX_QUESTION}"
        )

    reply = await propose(build_question_prompt(question, answers), model=model)
    try:
        return accept(reply, question, answers)
    except UnacceptableProposal as first:
        repaired = await propose(
            build_repair_prompt(question, reply, str(first)), model=model
        )
        try:
            return accept(repaired, question, answers)
        except UnacceptableProposal as second:
            # Chained so the first failure survives in the traceback: the two
            # are often different, and "what it did after being corrected" is
            # the more useful half when a prompt rule needs writing.
            raise second from first
