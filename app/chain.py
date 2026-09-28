"""The question-to-reply chain, as one function: parse -> map -> answer -> present.

``ask()`` is what the Web Server runs for each job, and what the evals should
call too, so they measure the path a reader gets (``app/api/DESIGN.md`` §12a).
It never raises for a question it cannot answer: every outcome, from any
stage, becomes part of the ``Outcome``, and the reader's words for a failure
are fixed sentences -- the detail goes to ``errors``, for the trace.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from pydantic import Field

from app.api.schemas import Ask, Option, Part, Refusal, Reply
from app.parser import ProposalError, UnacceptableProposal, parse_question
from app.presenter import PresentationError, present
from app.retrieval import GenerationError, InvalidSQL, UnsupportedPlan, answer
from app.schemas.answer_view import AnswerView
from app.schemas.job import JobStage
from app.schemas.query import ConceptRef, QueryIn, QueryPlan, _Base
from app.schemas.result import ResultSet
from app.semantic.query_mapper import map_query

log = logging.getLogger(__name__)

#: What a reader is told when no curated reason exists (DESIGN §12a).
PARSE_FAILED = (
    "I couldn't work out what that question is asking for. Try naming the figure, "
    "the company and the period explicitly."
)
EXECUTE_FAILED = (
    "The figures for this query came back in a form that mismatches what I was "
    "expecting, so I haven't shown them."
)
PRESENT_FAILED = "Something went wrong attempting to display the results."
BUG = (
    "Something went terribly wrong, likely a backend bug. Please contact your database "
    "administrator, jk, time to debug."
)

#: Element kinds that are "a thing the question asked for" -- the evals' ITEM_KINDS.
PART_KINDS = ("metric", "narrative")

StageHook = Callable[[JobStage], Awaitable[None] | None]


# --------------------------------------------------------------------------- #
# What a question back remembers, for the next round (stored in web.job.asks)
# --------------------------------------------------------------------------- #


class OptionRecord(_Base):
    option_id: str
    label: str  # a clarification's answer text: what parse_question(answers=) is given
    metric: str | None = None  # clarification: the curated metric it leads to
    concept: ConceptRef | None = None  # ambiguity: the concept to pin (slice 2)


class AskRecord(_Base):
    ask_id: str
    kind: str  # "clarification" | "ambiguity", as Ask.kind
    element_id: str
    element_text: str
    question: str
    options: list[OptionRecord] = Field(min_length=1)


class ErrorRecord(_Base):
    stage: str
    type: str
    message: str
    traceback: str


# --------------------------------------------------------------------------- #
# The outcome
# --------------------------------------------------------------------------- #


@dataclass
class Outcome:
    """A reply without its ids, plus everything the trace keeps."""

    blocking: Refusal | None = None
    parts: list[Part] = field(default_factory=list)
    answer: AnswerView | None = None
    asks: list[AskRecord] = field(default_factory=list)

    query_in: QueryIn | None = None
    plan: QueryPlan | None = None
    result: ResultSet | None = None
    errors: list[ErrorRecord] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def reply(self, conversation_id: UUID, job_id: UUID) -> Reply:
        return Reply(
            conversation_id=conversation_id,
            job_id=job_id,
            blocking=self.blocking,
            parts=self.parts,
            answer=self.answer,
        )


async def ask(
    question: str,
    *,
    answers: list[tuple[str, str]] | None = None,
    on_stage: StageHook | None = None,
) -> Outcome:
    """One round of a conversation. ``answers`` are ``(question put, label chosen)``
    pairs from earlier rounds, handed to the parser as it already expects."""
    outcome = Outcome()

    async def stage(name: JobStage) -> None:
        if on_stage is not None:
            maybe = on_stage(name)
            if maybe is not None:
                await maybe

    # -- parse -----------------------------------------------------------------
    await stage("parsing")
    with _timed(outcome, "parse"):
        try:
            outcome.query_in = await parse_question(question, answers=answers)
        except (UnacceptableProposal, ProposalError, ValueError) as exc:
            _record(outcome, "parse", exc)
            outcome.blocking = Refusal(stage="parse", reason=PARSE_FAILED)
            return outcome
        except Exception as exc:
            _record(outcome, "parse", exc, bug=True)
            outcome.blocking = Refusal(stage="parse", reason=BUG)
            return outcome

    # -- map -------------------------------------------------------------------
    await stage("mapping")
    with _timed(outcome, "map"):
        try:
            outcome.plan = await map_query(outcome.query_in)
        except Exception as exc:  # the mapper does not raise by design; if it does, a bug
            _record(outcome, "map", exc, bug=True)
            outcome.blocking = Refusal(stage="map", reason=BUG)
            return outcome

    query, plan = outcome.query_in, outcome.plan
    items = [e for e in query.elements if e.kind in PART_KINDS]
    item_ids = {e.id for e in items}

    # A refusal of something that is not a part -- a company, a period -- sinks
    # the whole question. Only it is shown: the parts' own refusals are knock-on
    # ("no reporting period in scope", q028), and a question back is moot.
    sinking = [u for u in plan.unresolved if u.blocks_question and u.element_id not in item_ids]
    if sinking:
        outcome.blocking = Refusal(stage="map", reason=_joined(u.reason for u in sinking))
        return outcome

    # -- each part's outcome, in the evals' precedence (evals/run.py observe) ---
    clarifications = {c.element_id: c for c in plan.clarifications}
    ambiguities = {a.element_id: a for a in plan.ambiguous}
    refusals: dict[str, list[str]] = {}
    for u in plan.unresolved:
        refusals.setdefault(u.element_id, []).append(u.reason)
    bound = {b.element_id for b in plan.bindings}

    decided: dict[str, Part] = {}  # element id -> a non-answered part
    to_answer: list = []  # elements whose figures are fetched
    for element in items:
        if element.id in clarifications:
            decided[element.id] = _clarification_part(element, clarifications[element.id], outcome)
        elif element.id in ambiguities:
            decided[element.id] = _ambiguity_part(element, ambiguities[element.id], outcome)
        elif element.id in refusals:
            decided[element.id] = _refused(element, _joined(refusals[element.id]))
        elif element.id in bound:
            to_answer.append(element)
        else:
            # Neither bound nor refused: nothing to show, and nothing said why.
            decided[element.id] = _refused(element, BUG)
            _record(outcome, "map", RuntimeError(f"element {element.id} has no outcome"), bug=True)

    answered: dict[str, str] = {}  # element id -> phrase, for the Presenter
    if to_answer:
        answered, outcome.answer = await _answer(outcome, plan, to_answer, decided, stage)

    outcome.parts = [
        Part(part_id=e.id, text=e.text, outcome="answered") if e.id in answered else decided[e.id]
        for e in items
    ]
    return outcome


async def _answer(
    outcome: Outcome, plan: QueryPlan, elements: list, decided: dict[str, Part], stage
) -> tuple[dict[str, str], AnswerView | None]:
    """Fetch and present the bound parts. Any failure refuses every one of them
    -- they came out of one statement (retrieval DESIGN §5) -- and leaves the
    mapper's refusals and questions standing."""

    def refuse_all(reason: str) -> tuple[dict[str, str], None]:
        for element in elements:
            decided[element.id] = _refused(element, reason)
        return {}, None

    await stage("fetching")
    with _timed(outcome, "answer"):
        try:
            outcome.result = await answer(plan)
        except (GenerationError, InvalidSQL, UnsupportedPlan) as exc:
            _record(outcome, "execute", exc)
            return refuse_all(EXECUTE_FAILED)
        except Exception as exc:
            _record(outcome, "execute", exc, bug=True)
            return refuse_all(BUG)
    result = outcome.result
    if not result.is_answerable:
        _record(outcome, "execute", RuntimeError(f"verdict {result.verdict.status!r}"))
        return refuse_all(EXECUTE_FAILED)

    # Rows of an element refused or asked about are not shown: a part is
    # answered with figures, or it is not answered at all.
    wanted = {e.id: e.text for e in elements}
    with_rows = {row.row.element_id for row in result.rows} & wanted.keys()
    for element in elements:
        if element.id not in with_rows:
            decided[element.id] = _refused(element, _no_figures(element, plan))
    shown = {eid: text for eid, text in wanted.items() if eid in with_rows}
    if not shown:
        return {}, None
    rows = [row for row in result.rows if row.row.element_id in shown]

    await stage("presenting")
    with _timed(outcome, "present"):
        try:
            view = present(result.model_copy(update={"rows": rows}), shown)
        except PresentationError as exc:
            _record(outcome, "present", exc)
            return refuse_all(PRESENT_FAILED)
        except Exception as exc:
            _record(outcome, "present", exc, bug=True)
            return refuse_all(BUG)
    return shown, view


# --------------------------------------------------------------------------- #
# Parts
# --------------------------------------------------------------------------- #


def _refused(element, reason: str) -> Part:
    return Part(part_id=element.id, text=element.text, outcome="refused", reason=reason)


def _clarification_part(element, clarification, outcome: Outcome) -> Part:
    ask_id = f"a{len(outcome.asks) + 1}"
    options = [
        OptionRecord(option_id=f"o{i}", label=o.label, metric=o.metric)
        for i, o in enumerate(clarification.options, start=1)
    ]
    descriptions = [o.description for o in clarification.options]
    return _asked(
        element, ask_id, "clarification", clarification.question, options, descriptions, outcome
    )


def _ambiguity_part(element, ambiguity, outcome: Outcome) -> Part:
    ask_id = f"a{len(outcome.asks) + 1}"
    concepts = [candidate.concept for candidate in ambiguity.candidates]
    options = [
        OptionRecord(option_id=f"o{i}", label=c.label or c.name, concept=c)
        for i, c in enumerate(concepts, start=1)
    ]
    descriptions = [f"{c.taxonomy}:{c.name}" for c in concepts]  # the concept, named exactly
    question = f"Which of these did you mean by '{ambiguity.element_text}'?"
    return _asked(element, ask_id, "ambiguity", question, options, descriptions, outcome)


def _asked(element, ask_id, kind, question, options, descriptions, outcome: Outcome) -> Part:
    outcome.asks.append(
        AskRecord(
            ask_id=ask_id, kind=kind, element_id=element.id, element_text=element.text,
            question=question, options=options,
        )
    )
    return Part(
        part_id=element.id,
        text=element.text,
        outcome="asked",
        ask=Ask(
            ask_id=ask_id,
            kind=kind,
            question=question,
            options=[
                Option(option_id=o.option_id, label=o.label, description=d)
                for o, d in zip(options, descriptions, strict=True)
            ],
        ),
    )


def _no_figures(element, plan: QueryPlan) -> str:
    if any(threshold.element_id == element.id for threshold in plan.thresholds):
        return f"None of the figures for '{element.text}' met the condition asked for."
    return f"No figures came back for '{element.text}' for the periods asked about."


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #


def _joined(reasons, limit: int = 512) -> str:
    """Distinct reasons, whole, as many as fit -- a curated sentence is never cut."""
    kept: list[str] = []
    for reason in dict.fromkeys(reasons):
        if len(" ".join([*kept, reason])) > limit and kept:
            break
        kept.append(reason)
    return " ".join(kept)


def _record(outcome: Outcome, stage: str, exc: BaseException, *, bug: bool = False) -> None:
    if bug:
        log.exception("chain: unexpected %s in %s", type(exc).__name__, stage)
    outcome.errors.append(
        ErrorRecord(
            stage=stage,
            type=type(exc).__name__,
            message=str(exc),
            traceback="".join(traceback.format_exception(exc)),
        )
    )


class _timed:
    def __init__(self, outcome: Outcome, stage: str) -> None:
        self.outcome, self.stage = outcome, stage

    def __enter__(self) -> None:
        self.started = time.monotonic()

    def __exit__(self, *exc: object) -> None:
        self.outcome.timings[self.stage] = round(time.monotonic() - self.started, 3)
