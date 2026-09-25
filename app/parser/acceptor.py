"""``accept(reply, question)`` -- turn a model's JSON into a ``QueryIn``, or
refuse it. The only thing here that decides whether a proposal is usable.

Sits where ``app/retrieval/validator.py`` sits: the model writes, this judges.
It runs three gates, cheapest first, and each one is a class of mistake the
next could not catch.

1. **Shape** -- the reply parses as JSON and fits ``WireQuery``. The grammar
   should have guaranteed this already; the repair path has no grammar behind
   it in the same way, and a gate that is usually redundant costs nothing.

2. **Faithfulness** -- every element's ``text`` appears in the question. This
   is the gate the whole design leans on, and it is the reason a 7B model is
   defensible here. Without it the parser can quietly substitute a phrase --
   "gross revenue" becoming "revenue" -- and the substitution is invisible
   downstream: the mapper resolves the replacement cleanly, coverage proves
   it, the verdict says ``complete``, and a real number comes back under a
   label it does not fit. Every other parser mistake either fails loudly or
   costs a refusal. This one costs a wrong answer, so it is checked in code
   rather than asked for in a prompt.

3. **Meaning** -- a field belongs to the kind that carries it, and
   ``QueryIn`` accepts the result. A ``fiscal_year`` on a company element is
   rejected rather than dropped: it means the model was confused about that
   element, and discarding the evidence hides a fault in the one place where
   silence is most expensive.

What comes out is an ordinary ``QueryIn``. The mapper never learns that a
language model was involved.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

from pydantic import ValidationError

from app.parser.wire import WireElement, WireQuery
from app.schemas.query import (
    CompanyElementIn,
    CompanyGroupElementIn,
    ElementIn,
    MetricElementIn,
    MetricQualifierElementIn,
    MetricThresholdElementIn,
    NarrativeElementIn,
    PeriodElementIn,
    QueryIn,
)


class UnacceptableProposal(Exception):
    """Base for everything ``accept()`` refuses.

    Caught by ``parse_question`` to drive its single repair attempt, so every
    message below is written to be shown to a model: it names the element,
    quotes what was wrong, and says what to do instead. A message that only
    makes sense to a person here is a wasted retry.
    """


class MalformedProposal(UnacceptableProposal):
    """The reply is not a well-formed query -- bad JSON, a field on the wrong
    kind of element, or something ``QueryIn`` itself rejects."""


class UnfaithfulSpan(UnacceptableProposal):
    """An element's ``text`` is not in the question.

    Separate from ``MalformedProposal`` because it is a different kind of
    wrong: the reply is structurally perfect and describes a question nobody
    asked.
    """


#: ```json ... ``` -- constrained decoding returns bare JSON, but the repair
#: path re-prompts a chat-tuned model, which fences things out of habit.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

#: Typographic characters a model reproduces from a question that contains
#: them -- or introduces into a question that does not. Folded to ASCII on
#: both sides of the span check so a smart quote is never the reason a
#: faithful span is called unfaithful.
_LOOKALIKES = str.maketrans(
    {
        "‘": "'", "’": "'", "‛": "'",
        "“": '"', "”": '"',
        "‐": "-", "‑": "-", "‒": "-",
        "–": "-", "—": "-", "−": "-",
        " ": " ",
    }
)

#: Trimmed off a span's ends before the check. A model that includes the
#: comma after "Apple," in "Apple, Microsoft and Nvidia" has transcribed
#: faithfully and punctuated carelessly, which is not the failure this gate
#: is for.
_EDGE = " \t\n\r\"'`.,;:!?()[]{}"

#: Words that change which line of the accounts a metric names, and that a
#: careless parser drops. Curated rather than inferred, and narrow on
#: purpose: this is the one place where plain substring faithfulness is not
#: enough, because a *shortened* span is genuinely present in the question.
#: "What was Apple's gross revenue" with a metric span of "revenue" passes
#: every other check here -- the word really is in the question -- and then
#: resolves to a real revenue figure under a label it does not fit, walking
#: around the curated `unavailable` entry that exists to refuse the phrase.
#:
#: Substitution is caught by the substring check. Omission is caught by this.
#: Accounting judgment as data, the same argument as `metric_aliases.yaml`:
#: add a word here when a filer's numbers differ across it.
_METRIC_MODIFIERS = (
    "gross",
    "net",
    "total",
    "operating",
    "free",
    "adjusted",
    "diluted",
    "basic",
    "deferred",
    "current",
    "long-term",
    "short-term",
    "non-operating",
)

#: A four-digit year. Matched against the **question**: a ``fiscal_year`` the
#: reader never mentioned is invented, because the model is told nothing about
#: today's date. Measured on the first live run, "last year" came back as
#: ``fiscal_year: 2023`` -- a well-formed plan answering about the wrong year,
#: which nothing downstream would question. Relative spans belong in
#: ``last_n_years``, which the mapper anchors on the newest year it can
#: actually resolve.
_YEAR = re.compile(r"\b\d{4}\b")

#: Which optional wire fields each kind may carry. Anything outside its set
#: is a confusion, not a spare field -- see gate 3.
_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "metric": frozenset(),
    "company": frozenset(),
    "period": frozenset(
        {"fiscal_year", "fiscal_period", "last_n_years", "last_n_quarters"}
    ),
    "company_group": frozenset({"sic_code", "sic_description"}),
    "metric_qualifier": frozenset({"qualifies"}),
    "metric_threshold": frozenset({"qualifies", "comparison", "threshold"}),
    "narrative": frozenset(),
}

def _kind_owning(stray: set[str]) -> str:
    """Which kind those fields belong to, for the message the model is shown.

    Looked up rather than guessed: with several field-owning kinds, a two-way
    guess names the wrong one and sends the model off correcting something
    that was already right.
    """
    owners = sorted(kind for kind, fields in _FIELDS_BY_KIND.items() if stray & fields)
    return " or ".join(owners) if owners else "another kind of"


#: Every optional field on ``WireElement``, so a new one cannot be added
#: without appearing in ``_FIELDS_BY_KIND`` above and failing the test that
#: checks the two agree.
_OPTIONAL_FIELDS = frozenset(
    name for names in _FIELDS_BY_KIND.values() for name in names
)


def normalize(text: str) -> str:
    """Casefold, fold look-alike punctuation, collapse whitespace.

    Applied to both sides of the span check. Deliberately *not* a stemmer or
    a synonym pass: the point of the check is that the words survived, and
    anything that lets two different words compare equal is the hole this
    exists to close.
    """
    return " ".join(text.translate(_LOOKALIKES).casefold().split())


def extract_json(reply: str) -> str:
    """Pull the object out of whatever the model wrapped it in.

    Prefers the **last** fenced block, matching ``extract_sql``'s reasoning:
    a model that narrates before answering quotes fragments first and puts
    its answer last.
    """
    blocks = [match.group(1).strip() for match in _FENCE.finditer(reply)]
    if blocks:
        return blocks[-1]

    start, end = reply.find("{"), reply.rfind("}")
    if start == -1 or end <= start:
        raise MalformedProposal(
            f"no JSON object in the reply: {reply.strip()[:200]!r}. "
            "Reply with a single JSON object and nothing else."
        )
    return reply[start : end + 1]


def check_span(
    element: WireElement, question: str, answers: list[tuple[str, str]] | None = None
) -> str:
    """The element's ``text``, trimmed, having proved it came from ``question``.

    Returns the trimmed span rather than the original so the ``QueryIn`` the
    mapper sees carries the phrase and not the punctuation around it.

    **Period elements are exempt from faithfulness.** Their meaning is carried
    entirely by ``fiscal_year`` / ``fiscal_period`` / ``last_n_years``; the
    mapper reads a period's ``text`` in exactly two places, both of them
    refusal messages (``query_mapper.py:492`` and ``:515``). Holding them to
    the substring rule refused correct work: measured over the 56-question
    eval set, 8 answerable questions failed because the model *composed* a
    period span out of words from different parts of the question -- "Q4 last
    year", "year-over-year in 2024" -- and q025 expanded "from 2021 through
    2025" into individual years and was refused for writing "2022", which is
    exactly the right thing to emit and simply is not a literal substring.
    The gate that matters for a period is on ``fiscal_year``, in
    ``_check_period``.
    """
    span = element.text.strip(_EDGE)
    if not span:
        raise UnfaithfulSpan(
            f"element {element.id!r} has an empty `text`. Copy the words from "
            "the question that name this element."
        )
    if element.kind == "period":
        return span

    normalized_span, normalized_question = normalize(span), normalize(question)
    if normalized_span not in _haystack(normalized_question, answers):
        raise UnfaithfulSpan(
            f"element {element.id!r} has text {element.text!r}, which does not "
            f"appear in the question: {question!r}. Copy the words from the "
            "question exactly -- do not reword, expand or correct them."
        )

    if element.kind == "metric":
        # Against the question alone. The rule exists to stop the model
        # dropping a qualifier the *reader typed*; an answer they chose from a
        # curated list is already exact, and policing it would refuse the
        # obvious reply -- the label "Total revenue" would forbid the span
        # "revenue" that it names.
        _refuse_dropped_modifier(element, normalized_span, normalized_question)
    return span


def _haystack(normalized_question: str, answers: list[tuple[str, str]] | None) -> str:
    """What a span may faithfully have come from.

    The question, plus anything the reader said in answer to a clarification.
    Their answer is as authoritative as their question and rather more
    specific -- it was given in reply to a direct one.

    Without this the round trip cannot close, which is how the omission was
    found: asked "measured by what?" about Costco's strongest quarter, the
    reader picks "Total revenue", the parser rightly emits a metric of
    "revenue", and the gate refuses it because that word is nowhere in
    "Which quarter is Costco's strongest?".

    The parts are joined by a separator that survives ``normalize`` so no span
    can straddle the seam and appear faithful by accident.
    """
    if not answers:
        return normalized_question
    replies = [normalize(reply) for _, reply in answers]
    return " ~ ".join([normalized_question, *replies])


def _refuse_dropped_modifier(
    element: WireElement, span: str, question: str
) -> None:
    """Refuse a metric span the question qualifies and the model did not.

    Only metrics: "Apple" preceded by "net" is a coincidence, "revenue"
    preceded by "net" is a different line of the income statement. See
    ``_METRIC_MODIFIERS``.
    """
    for modifier in _METRIC_MODIFIERS:
        if span.startswith(f"{modifier} "):
            continue  # already carries it
        if f"{modifier} {span}" in question:
            raise UnfaithfulSpan(
                f"element {element.id!r} has text {element.text!r}, but the "
                f"question says {modifier + ' ' + span!r}. Copy the whole "
                f"phrase: {modifier!r} changes which figure is meant."
            )


def _check_period(element: WireElement, span: str, question: str) -> None:
    """The period rules a schema cannot state.

    Both turn a silent downstream outcome into a repairable one. A period with
    no selector reaches the mapper and comes back ``Unresolved``, which is a
    refusal the reader sees; caught here it costs one more generation instead.
    An invented ``fiscal_year`` is worse -- it resolves, and answers about a
    year nobody asked for.

    The year rule is checked against the **question**, not the span. It used
    to read the span, which blocked range expansion: "from 2021 through 2025"
    has to become five elements, and only one of them can carry a span with
    its own year in it. What actually matters is that the year came from the
    reader rather than from the model, and the question is where to look.

    Open-ended forward is allowed -- "since 2021" legitimately reaches years
    the question never names. Backwards is allowed by exactly one year, and
    for one reason: a growth or change question needs the period *before* the
    one it names, and "revenue growth in 2024" is meaningless without 2023.
    Two years back is not a comparison, so that is where the line sits.
    """
    if (
        element.fiscal_year,
        element.last_n_years,
        element.last_n_quarters,
        element.fiscal_period,
    ) == (None,) * 4:
        raise MalformedProposal(
            f"period element {element.id!r} ({span!r}) carries no fiscal_year, "
            "last_n_years, last_n_quarters or fiscal_period, so it names no time "
            "at all. Set fiscal_year for a stated year, last_n_years for a "
            "relative span of years, or last_n_quarters for the most recent "
            "quarters."
        )

    if element.fiscal_year is None:
        return

    stated = [int(year) for year in _YEAR.findall(question)]
    if not stated:
        raise MalformedProposal(
            f"period element {element.id!r} has fiscal_year "
            f"{element.fiscal_year}, but the question names no year at all. "
            "You are not told what year it is now, so do not guess one: use "
            'last_n_years for a relative span ("last year" is last_n_years: 1).'
        )
    if element.fiscal_year < min(stated) - 1:
        raise MalformedProposal(
            f"period element {element.id!r} has fiscal_year "
            f"{element.fiscal_year}, which is more than a year earlier than "
            f"anything the question names (the earliest is {min(stated)})."
        )


def _build_element(element: WireElement, span: str, question: str) -> ElementIn:
    """One ``WireElement`` as its real discriminated-union member.

    The field check happens here because this is where the kind is finally
    known: the grammar has one element type, so nothing before this point can
    tell a stray ``fiscal_year`` from a legitimate one.
    """
    allowed = _FIELDS_BY_KIND[element.kind]
    supplied = {
        name for name in _OPTIONAL_FIELDS if getattr(element, name) is not None
    }
    if stray := supplied - allowed:
        raise MalformedProposal(
            f"element {element.id!r} is a {element.kind} but carries "
            f"{sorted(stray)}, which only a "
            f"{_kind_owning(stray)} "
            f"element can have. Either change `kind` or drop those fields."
        )

    if element.kind == "period":
        _check_period(element, span, question)

    try:
        if element.kind == "metric":
            return MetricElementIn(id=element.id, text=span)
        if element.kind == "company":
            # No ticker, no name: see wire.py. The span alone reaches the
            # mapper's lexicon, and a hint the model invented would outrank
            # it there.
            return CompanyElementIn(id=element.id, text=span)
        if element.kind == "period":
            return PeriodElementIn(
                id=element.id,
                text=span,
                fiscal_year=element.fiscal_year,
                fiscal_period=element.fiscal_period,
                last_n_years=element.last_n_years,
                last_n_quarters=element.last_n_quarters,
            )
        if element.kind == "narrative":
            return NarrativeElementIn(id=element.id, text=span)
        if element.kind == "metric_threshold":
            if element.qualifies is None or element.comparison is None:
                raise MalformedProposal(
                    f"metric_threshold {element.id!r} needs `qualifies` set to the id "
                    f"of the metric it tests and `comparison` set to one of gt, gte, "
                    f"lt, lte, eq"
                )
            if element.threshold is None:
                raise MalformedProposal(
                    f"metric_threshold {element.id!r} needs `threshold` set to the "
                    f"number being compared against, in the metric's own unit -- "
                    f"dollars for a dollar figure, a fraction for a percentage"
                )
            return MetricThresholdElementIn(
                id=element.id,
                text=span,
                qualifies=element.qualifies,
                comparison=element.comparison,
                # str() first: Decimal(float) carries the float's binary error
                # into a value that gets compared against stored Decimals.
                value=Decimal(str(element.threshold)),
            )
        if element.kind == "metric_qualifier":
            if element.qualifies is None:
                raise MalformedProposal(
                    f"metric_qualifier {element.id!r} needs `qualifies` set to the "
                    f"id of the metric it narrows"
                )
            return MetricQualifierElementIn(
                id=element.id, text=span, qualifies=element.qualifies
            )
        return CompanyGroupElementIn(
            id=element.id,
            text=span,
            sic_code=element.sic_code,
            sic_description=element.sic_description,
        )
    except ValidationError as exc:
        # The per-kind rules the flat wire shape cannot express: a period that
        # is both absolute and relative, a company group with no selector.
        raise MalformedProposal(
            f"element {element.id!r} is not a valid {element.kind}: {exc}"
        ) from exc


def accept(
    reply: str, question: str, answers: list[tuple[str, str]] | None = None
) -> QueryIn:
    """The model's reply as a ``QueryIn``, or an exception saying why not."""
    payload = extract_json(reply)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MalformedProposal(
            f"the reply is not valid JSON ({exc}). Reply with a single JSON "
            "object and nothing else."
        ) from exc

    try:
        wire = WireQuery.model_validate(parsed)
    except ValidationError as exc:
        raise MalformedProposal(f"the reply does not fit the schema: {exc}") from exc

    if not wire.elements:
        raise MalformedProposal(
            "no elements. Every question names at least a metric; find the "
            "words in it that do."
        )

    elements = [
        _build_element(element, check_span(element, question, answers), question)
        for element in wire.elements
    ]

    if not any(element.kind == "metric" for element in elements):
        # Measured: "How did Apple do last year?" came back with a company and
        # a period and no metric at all, and the mapper called that plan
        # ``complete`` -- there was nothing left unresolved because nothing
        # had been asked for. A query naming no figure is not a small query,
        # it is an empty one, and it must not be able to look finished.
        #
        # The vague words are not a reason to omit the metric: "strongest",
        # "best" and "performed" are curated in ``metric_aliases.yaml`` and
        # resolve to a question with named options, which is a far better
        # outcome than silence.
        raise MalformedProposal(
            "no metric element, so the query names no figure to fetch. Every "
            "question asks for something measurable -- when the question is "
            "vague about which figure it wants (\"strongest\", \"best\", "
            '"how did they do"), that word is the metric; copy it as one.'
        )

    if not any(element.kind == "period" for element in elements):
        # No period element means ``PlanFilters.periods`` is empty, which the
        # mapper reads as unconstrained -- every year on file. Measured over
        # the eval set, 17 of 56 questions came back this way, so "what is
        # Apple's current ratio" asked for five years of rows. Almost always
        # the reader meant the most recent year, and where they did not, the
        # question says so and the model has something to copy.
        raise MalformedProposal(
            "no period element, so the query places no bound on time and would "
            "return every year on file. Every question needs at least one: when "
            "the question names no time, use last_n_years: 1 for the most recent "
            "year."
        )

    try:
        return QueryIn(
            question=question,
            intent=wire.intent,
            # False leaves ``shape`` unset so ``_describe_result`` infers it
            # from what resolved, which it does better than the model would.
            # True is the one thing cardinality cannot say. See ``wire.py``.
            shape="series" if wire.wants_chart else None,
            elements=elements,
        )
    except ValidationError as exc:
        # Reached by the rules a grammar cannot express -- duplicate element
        # ids, a period carrying both fiscal_year and last_n_years, a span
        # over 256 characters.
        raise MalformedProposal(f"the query is not valid: {exc}") from exc
