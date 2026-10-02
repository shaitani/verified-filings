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
from collections import defaultdict
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
from app.semantic.metric_aliases import alias_index


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
#: resolves to plain revenue, walking around the curated entry for the phrase
#: and the caveat it attaches. For a term the file declines or asks about,
#: the same omission walks around the refusal or the question.
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

#: Time words a colon-list heading can carry that belong to the period, not to
#: the metric: "yearly revenue: gross, net" asks for gross revenue and net
#: revenue, for the year. A small closed class of words, unlike the open-ended
#: vocabulary of metrics, so a list is the honest tool here. Measured
#: 2026-09-26: "gross yearly revenue" misses the curated alias and embedding
#: search puts GrossProfit on top at 0.751, while "gross revenue" hits the
#: curated entry it should.
_TIME_WORDS = frozenset(
    {"yearly", "annual", "annually", "quarterly", "monthly", "weekly", "daily",
     "year-to-date", "ytd"}
)

#: A possessive, which is where a colon-list heading starts: "Apple's cash
#: flow: ...", "the companies' margins: ...". Matched on normalized text, so
#: the apostrophe is already ASCII.
_POSSESSIVE = re.compile(r"(?:'s|s')(?=\s|$)")

#: What separates list items after the colon. "and" / "or" split too, so
#: "operating, investing and financing" is three items -- at the cost that
#: "research and development" is two, which only means that item has no
#: reading and must be copied as written.
_LIST_SEPARATORS = re.compile(r",|;|&|\band\b|\bor\b")


class _SharedHead:
    """A colon list whose items share the heading before the colon.

    "What is Apple's cash flow: operating, investing, financing" asks for
    three figures, and none of their names appears whole in the question:
    each item names one only together with the heading. The substring check
    cannot see that, so this recovers it from the grammar -- the colon, the
    heading back to the possessive, the items after it -- and offers exactly
    one reading per item, ``"<item> <heading>"``.

    Never from vocabulary: no word decides whether the structure applies.
    What makes a composed span faithful is that the question itself
    distributes the heading over the items; composing freely from words that
    merely *occur* would accept "net income" from "net revenue and operating
    income", a real and different figure. See ``app/parser/DESIGN.md`` §2a.
    """

    def __init__(self, heading: str, items: list[str]) -> None:
        self.heading = heading
        self.items = items
        #: reading -> the item it spells. Empty when there is no heading.
        self.readings = {f"{item} {heading}": item for item in items} if heading else {}

    @classmethod
    def find(cls, normalized_question: str) -> _SharedHead | None:
        if ":" not in normalized_question:
            return None
        before, after = normalized_question.split(":", 1)
        possessives = list(_POSSESSIVE.finditer(before))
        heading_words = before[possessives[-1].end():].split() if possessives else []
        heading = " ".join(w for w in heading_words if w not in _TIME_WORDS).strip(_EDGE)
        items = [
            item
            for item in (part.strip(_EDGE) for part in _LIST_SEPARATORS.split(after))
            if item
        ]
        return cls(heading, items) if items else None

    def item_of(self, span: str) -> str | None:
        """The list item a metric span was taken from, if any."""
        if span in self.items:
            return span
        return self.readings.get(span)


#: A four-digit year. Matched against the **question**: a ``fiscal_year`` the
#: reader never mentioned is invented, because the model is told nothing about
#: today's date. Measured on the first live run, "last year" came back as
#: ``fiscal_year: 2023`` -- a well-formed plan answering about the wrong year,
#: which nothing downstream would question. Relative spans belong in
#: ``last_n_years``, which the mapper anchors on the newest year it can
#: actually resolve.
_YEAR = re.compile(r"\b\d{4}\b")

#: Words that ask for figures by quarter. A small closed class, like
#: ``_TIME_WORDS``, matched on normalized text. Not "quarter" alone: "which
#: quarter is strongest" and "the last quarter" ask for no quarterly series.
_QUARTERLY = re.compile(r"\b(?:quarterly|(?:by|each|every|per) quarter)\b")

#: Which optional wire fields each kind may carry. Anything outside its set
#: is a confusion, not a spare field -- see gate 3.
_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "metric": frozenset({"clarify_as", "over_time", "rank", "top_n"}),
    "company": frozenset(),
    "period": frozenset(
        {
            "fiscal_year",
            "fiscal_years",
            "from_fiscal_year",
            "to_fiscal_year",
            "fiscal_period",
            "last_n_years",
            "last_n_quarters",
        }
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
    haystack = _haystack(normalized_question, answers)
    # A colon list's readings are faithful sources for a metric and nothing
    # else: a company or a period is never spelled by a list item plus a
    # heading. Joined with the same seam as a clarification answer, so no span
    # can straddle a reading and the question.
    shared = _SharedHead.find(normalized_question) if element.kind == "metric" else None
    readings = list(shared.readings) if shared else []
    if normalized_span not in " ~ ".join([haystack, *readings]):
        asked = _curated_question_text(element)
        if asked is not None:
            return asked
        hint = (
            f" The list after the colon is written one metric per item, each "
            f"with its heading: {', '.join(repr(r) for r in readings)}."
            if readings
            else ""
        )
        raise UnfaithfulSpan(
            f"element {element.id!r} has text {element.text!r}, which does not "
            f"appear in the question: {question!r}. Copy the words from the "
            "question exactly -- do not reword, expand or correct them." + hint
        )

    if element.kind == "metric":
        # Against the question alone -- plus its colon-list readings, where
        # "revenue: gross, net" says "gross revenue" as plainly as the words
        # side by side do. The rule exists to stop the model dropping a
        # qualifier the *reader typed*; an answer they chose from a curated
        # list is already exact, and policing it would refuse the obvious
        # reply -- the label "Total revenue" would forbid the span "revenue"
        # that it names.
        _refuse_dropped_modifier(
            element, normalized_span, " ~ ".join([normalized_question, *readings])
        )
    return span


def _curated_question_text(element: WireElement) -> str | None:
    """The text a vague metric is given when its own words are not the
    question's, but it names a curated question -- or ``None``.

    Measured 2026-10-02 on "how much did google make in 2025?" and "how much
    money did google make in 2025?": the model set ``clarify_as: money_made``
    -- exactly right -- and wrote the metric as "how much money was made" or
    "money made", words neither question contains. The faithfulness gate
    refused both, and the retry lost ``clarify_as`` while keeping the wording.

    Accepting it is safe because the element can then only ever ask. The gate
    exists to stop a reworded phrase from *binding the wrong figure*; this
    replaces the model's text with one that the curated lookup resolves to
    that same ``clarify`` entry, so the mapper asks its question and nothing
    else. A paraphrase with no ``clarify_as`` -- or one naming something that
    is not a curated question -- is still refused, and "revenue" carrying a
    ``clarify_as`` never reaches the mapper as "revenue".
    """
    if element.kind != "metric" or not element.clarify_as:
        return None
    index = alias_index()
    entry = index.clarify_entry(element.clarify_as)
    if entry is None:
        return None
    for text in (entry.label, entry.metric):
        hit = index.lookup(text)
        if hit is not None and hit.metric == entry.metric:
            return text
    return None


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
    # Compared without time words, the way a colon list's readings are built:
    # "yearly revenue" for "yearly revenue: gross, net" drops the list as
    # surely as "revenue" does, and "gross yearly revenue" is nowhere to be
    # found. Measured 2026-09-26: accepted, it was refused downstream and the
    # two figures asked for were lost.
    core = " ".join(word for word in span.split() if word not in _TIME_WORDS) or span
    for modifier in _METRIC_MODIFIERS:
        if core.startswith(f"{modifier} "):
            continue  # already carries it
        if f"{modifier} {core}" in question:
            raise UnfaithfulSpan(
                f"element {element.id!r} has text {element.text!r}, but the "
                f"question says {modifier + ' ' + core!r}. Copy the whole "
                f"phrase: {modifier!r} changes which figure is meant."
            )


def _refuse_a_list_item_used_twice(elements: list[ElementIn], question: str) -> None:
    """One metric per item of a colon list.

    The readings give each item exactly one spelling, but the item on its own
    is in the question too, so "gross" and "gross revenue" could both pass
    the span check and ask for one thing twice.
    """
    shared = _SharedHead.find(normalize(question))
    if shared is None:
        return
    used: dict[str, str] = {}
    for element in elements:
        if element.kind != "metric":
            continue
        item = shared.item_of(normalize(element.text))
        if item is None:
            continue
        if item in used:
            raise MalformedProposal(
                f"elements {used[item]!r} and {element.id!r} both come from the list "
                f"item {item!r}. Each item after the colon is one metric -- keep one "
                "element for it."
            )
        used[item] = element.id


def _refuse_one_span_at_two_granularities(elements: list[WireElement]) -> None:
    """Period elements copied from one phrase are all annual or all quarterly.

    One phrase names one granularity. "Quarterly from 2020 until today" came
    back cold as the open annual range *plus* the quarters (2 of 2 runs, then
    with Q1 replaced by the annual one), and "revenue from 2023 to 2025 by
    quarter" once as three years plus four bare quarters. The mapper resolves
    both happily into a result mixing 363-day and 90-day figures -- more rows
    than were asked for, and an annual figure passed off as a point in a
    quarterly series.

    Structural, like ``_SharedHead``: it reads which elements share a span, not
    what the words say. Two granularities stated in two phrases ("2024 and
    each quarter of it") keep separate spans and pass.
    """
    by_span: dict[str, list[WireElement]] = defaultdict(list)
    for element in elements:
        if element.kind == "period":
            by_span[normalize(element.text)].append(element)
    for group in by_span.values():
        annual = [e.id for e in group if e.fiscal_period in (None, "FY")]
        quarterly = [e.id for e in group if e.fiscal_period not in (None, "FY")]
        if annual and quarterly:
            raise MalformedProposal(
                f"period elements {annual} and {quarterly} were all copied from "
                f"{group[0].text!r}, but {annual} are annual and {quarterly} are "
                "quarters. One phrase is one or the other: asked quarterly, it is "
                'fiscal_period: "quarterly" on the element with the years, and no '
                "annual element beside it."
            )


def _refuse_quarterly_asked_as_annual(elements: list[ElementIn], question: str) -> None:
    """A question that says "quarterly" gets at least one quarter.

    Measured 2026-09-30: "Compare Microsoft, Apple, and Nvidia quarterly profits
    across 2023, 2024 and 2025" came back as the three years alone, and so did
    "Nvidia's quarterly net income in 2023, 2024 and 2025". The word sits on the
    metric, outside the span the model copies for the period; where it sits
    inside ("each quarter in 2021, 2022 and 2023") it was set every time. On
    repair both came back quarterly -- one field to add, not elements to
    rebuild. Structural: it compares the reply to the question, whatever the
    wording in between.
    """
    asked = _QUARTERLY.search(normalize(question))
    if asked is None:
        return
    if any(
        element.kind == "period"
        and (element.fiscal_period not in (None, "FY") or element.last_n_quarters)
        for element in elements
    ):
        return
    raise MalformedProposal(
        f"the question says {asked.group(0)!r}, but no period element has "
        'fiscal_period: "quarterly", so every figure would be annual. Set '
        'fiscal_period: "quarterly" on the period elements, keeping their years.'
    )


def _refuse_a_named_year_left_out(elements: list[ElementIn], question: str) -> None:
    """Every year the question names is selected by some period element.

    Measured 2026-09-30, before ``fiscal_years`` existed: "in 2023, 2024 and
    2025" came back as ``fiscal_year: 2023`` alone, and "for 2022 and 2024" as
    2022 alone -- a clean answer about fewer years than were asked, which
    nothing downstream would notice. Kept since as the net under any way of
    dropping a year.

    A year inside another element's span is not a period ("more than 2000
    employees"), and a period naming no year at all -- every year on file, a
    relative span -- selects years this cannot know, so the check stands down.
    """
    periods = [e for e in elements if e.kind == "period"]
    if any(
        (e.fiscal_year, e.fiscal_years, e.from_fiscal_year) == (None,) * 3 for e in periods
    ):
        return
    covered: set[int] = set()
    open_from: int | None = None
    for e in periods:
        if e.fiscal_year is not None:
            covered.add(e.fiscal_year)
        covered.update(e.fiscal_years or ())
        if e.from_fiscal_year is not None:
            if e.to_fiscal_year is None:
                open_from = min(e.from_fiscal_year, open_from or e.from_fiscal_year)
            else:
                covered.update(range(e.from_fiscal_year, e.to_fiscal_year + 1))
    elsewhere = " ".join(normalize(e.text) for e in elements if e.kind != "period")
    named = {int(y) for y in _YEAR.findall(question)} - {
        int(y) for y in _YEAR.findall(elsewhere)
    }
    missing = sorted(
        y for y in named if y not in covered and (open_from is None or y < open_from)
    )
    if missing:
        raise MalformedProposal(
            f"the question names {', '.join(map(str, missing))}, but no period element "
            "selects it, so the answer would leave it out. A list of years is "
            "fiscal_years with every year listed; a range is from_fiscal_year and "
            "to_fiscal_year."
        )


def _check_clarify_as(element: WireElement) -> None:
    """``clarify_as`` names a curated question, or is absent.

    The grammar already confines it to the curated names; this is the same
    check for a reply that reached here some other way, with the names spelled
    out so a repair has something to choose from.
    """
    if element.clarify_as is None:
        return
    entries = alias_index().clarify_entries()
    if element.clarify_as not in {hit.metric for hit in entries}:
        raise MalformedProposal(
            f"metric {element.id!r} has clarify_as {element.clarify_as!r}, which is "
            f"not one of the curated questions: "
            f"{', '.join(hit.metric for hit in entries)}. Use one of those, or leave "
            "clarify_as out."
        )


_NUMBER_WORDS = {
    word: number
    for number, word in enumerate(
        "one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(),
        start=1,
    )
} | {"thirty": 30, "forty": 40, "fifty": 50, "hundred": 100}


def _check_top_n(element: WireElement, question: str) -> None:
    """``top_n`` is a count the question states, in digits or in words.

    Same reason as a period's year: a count the reader never wrote is invented,
    and it would silently cut the answer short. "the top 3" and "the five
    lowest" pass; "which company is highest" carries no count and gets none.
    """
    if element.top_n is None:
        return
    stated = {
        int(token) if token.isdigit() else _NUMBER_WORDS.get(token)
        for token in re.findall(r"[a-z]+|\d+", normalize(question))
    }
    if element.top_n not in stated:
        raise MalformedProposal(
            f"metric {element.id!r} has top_n {element.top_n}, which the question does "
            "not state. A count is only what the question says (\"top 3\", \"the five "
            "lowest\"); a question that names none has no top_n: leave it out."
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

    Open-ended forward is allowed -- a ``fiscal_year`` inside a range the
    question states is a year it never typed. Backwards is allowed by exactly
    one year, and for one reason: a growth or change question needs the period
    *before* the one it names, and "revenue growth in 2024" is meaningless
    without 2023. Two years back is not a comparison, so that is where the line
    sits.

    A range's **end** is held tighter: ``to_fiscal_year`` must be a year the
    question names. "From 2020 until today" has no end the model can know --
    it is not told what year it is -- and a guessed one resolves cleanly,
    cutting the answer short or reaching for a year nobody asked about. An
    open range leaves the end to the mapper, which reads it from the data.
    """
    if (
        element.fiscal_year,
        element.fiscal_years,
        element.from_fiscal_year,
        element.to_fiscal_year,
        element.last_n_years,
        element.last_n_quarters,
        element.fiscal_period,
    ) == (None,) * 7:
        raise MalformedProposal(
            f"period element {element.id!r} ({span!r}) carries no fiscal_year, "
            "fiscal_years, from_fiscal_year, last_n_years, last_n_quarters or "
            "fiscal_period, so it names no time at all. Set fiscal_year for a "
            "stated year, fiscal_years for a list of years, from_fiscal_year (and "
            "to_fiscal_year) for a range of years, last_n_years for a relative "
            "span of years, or last_n_quarters for the most recent quarters."
        )

    years = [
        (name, getattr(element, name))
        for name in ("fiscal_year", "from_fiscal_year", "to_fiscal_year")
        if getattr(element, name) is not None
    ] + [("fiscal_years", year) for year in element.fiscal_years or ()]
    if not years:
        return

    stated = [int(year) for year in _YEAR.findall(question)]
    if not stated:
        name, year = years[0]
        raise MalformedProposal(
            f"period element {element.id!r} has {name} {year}, but the question "
            "names no year at all. You are not told what year it is now, so do not "
            'guess one: use last_n_years for a relative span ("last year" is '
            "last_n_years: 1)."
        )
    for name, year in years:
        if year < min(stated) - 1:
            raise MalformedProposal(
                f"period element {element.id!r} has {name} {year}, which is more "
                f"than a year earlier than anything the question names (the "
                f"earliest is {min(stated)})."
            )
    if element.to_fiscal_year is not None and element.to_fiscal_year not in stated:
        raise MalformedProposal(
            f"period element {element.id!r} has to_fiscal_year "
            f"{element.to_fiscal_year}, which the question does not name. A range "
            "that runs to today or to the latest year has no to_fiscal_year: set "
            "from_fiscal_year alone."
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
            _check_clarify_as(element)
            _check_top_n(element, question)
            return MetricElementIn(
                id=element.id,
                text=span,
                clarify_as=element.clarify_as,
                over_time=element.over_time,
                rank=element.rank,
                top_n=element.top_n,
            )
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
                fiscal_years=element.fiscal_years,
                from_fiscal_year=element.from_fiscal_year,
                to_fiscal_year=element.to_fiscal_year,
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

    _refuse_a_list_item_used_twice(elements, question)

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

    _refuse_one_span_at_two_granularities(elements)
    _refuse_quarterly_asked_as_annual(elements, question)
    _refuse_a_named_year_left_out(elements, question)

    ranked = [e.id for e in elements if e.kind == "metric" and e.rank is not None]
    if wire.intent == "rank" and not ranked:
        # Without it the ordering is a guess, and a list in the wrong order is a
        # plausible wrong answer ("which is highest" answered with the lowest).
        raise MalformedProposal(
            'intent is "rank" but no metric says which end comes first. Set '
            '"rank": "highest" or "rank": "lowest" on the metric being ranked.'
        )
    if ranked and wire.intent != "rank":
        raise MalformedProposal(
            f"metric(s) {ranked} carry `rank` but intent is {wire.intent!r}. Either "
            'the question orders by that metric -- intent "rank" -- or drop `rank`.'
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
