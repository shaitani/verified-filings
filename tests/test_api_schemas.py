"""The Web Server's request and response models (app/api/schemas.py)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from app.api.schemas import (
    MAX_QUESTION,
    AnswersIn,
    Ask,
    DoneEvent,
    JobEvent,
    JobView,
    NewConversationIn,
    Option,
    OptionAnswerIn,
    Part,
    Refusal,
    Reply,
    StageEvent,
)
from app.schemas.answer_view import AnswerRow, AnswerView, CitationView
from app.schemas.query import ConceptRef

CONVERSATION = uuid.uuid4()
JOB = uuid.uuid4()


def _ask(ask_id: str = "a1", kind: str = "clarification") -> Ask:
    return Ask(
        ask_id=ask_id,
        kind=kind,
        question="Which profit margin?",
        options=[
            Option(option_id="o1", label="Net margin", description="Net income / revenue"),
            Option(option_id="o2", label="Gross margin", description="Gross profit / revenue"),
        ],
    )


def _answered(part_id: str = "e1") -> Part:
    return Part(part_id=part_id, text="revenue", outcome="answered")


def _answer(element_id: str = "e1") -> AnswerView:
    row = AnswerRow(
        element_id=element_id, metric="revenue", company_cik=320193, company="AAPL",
        fiscal_year=2024, fiscal_period="FY", period_label="FY2024", granularity="annual",
        period_start=date(2023, 10, 1), period_end=date(2024, 9, 28), is_instant=False,
        value=Decimal("391035000000"), display="$391.04B", unit="USD", unit_kind="money",
        citations=["b0"],
    )
    citation = CitationView(
        key="b0",
        concepts=[ConceptRef(concept_id=1, taxonomy="us-gaap", name="Revenues")],
        expression="c0", unit="USD", resolved_by="alias",
    )
    return AnswerView(shape="scalar", rows=[row], citations={"b0": citation})


def _reply(**fields) -> Reply:
    return Reply(conversation_id=CONVERSATION, job_id=JOB, **fields)


# --------------------------------------------------------------------------- #
# Parts
# --------------------------------------------------------------------------- #


def test_a_refused_part_carries_its_reason_and_nothing_else() -> None:
    assert Part(part_id="e2", text="goodwill", outcome="refused", reason="Not filed.").reason
    with pytest.raises(ValidationError, match="reason is set exactly when refused"):
        Part(part_id="e2", text="goodwill", outcome="refused")
    with pytest.raises(ValidationError, match="reason is set exactly when refused"):
        Part(part_id="e1", text="revenue", outcome="answered", reason="why")


def test_an_asked_part_carries_its_ask() -> None:
    assert Part(part_id="e1", text="margin", outcome="asked", ask=_ask()).ask
    with pytest.raises(ValidationError, match="ask is set exactly when asked"):
        Part(part_id="e1", text="margin", outcome="asked")


def test_an_ask_repeating_an_option_id_is_refused() -> None:
    option = Option(option_id="o1", label="x", description="y")
    with pytest.raises(ValidationError, match="repeats an option_id"):
        Ask(ask_id="a1", kind="ambiguity", question="q", options=[option, option])


def test_an_ambiguity_may_offer_a_single_candidate() -> None:
    option = Option(
        option_id="o1", label="Payments of Dividends", description="us-gaap:PaymentsOfDividends"
    )
    ask = Ask(ask_id="a1", kind="ambiguity", question="Did you mean?", options=[option])
    assert ask.kind == "ambiguity"


# --------------------------------------------------------------------------- #
# The reply mixes parts
# --------------------------------------------------------------------------- #


def test_figures_a_refusal_and_a_question_travel_together() -> None:
    reply = _reply(
        parts=[
            _answered("e1"),
            Part(part_id="e2", text="goodwill", outcome="refused", reason="No goodwill filed."),
            Part(part_id="e3", text="margin", outcome="asked", ask=_ask()),
        ],
        answer=_answer("e1"),
    )
    assert reply.status == "partial"
    assert reply.model_dump(mode="json")["status"] == "partial"


@pytest.mark.parametrize(
    ("outcomes", "status"),
    [
        (["answered"], "answered"),
        (["answered", "refused"], "partial"),
        (["asked", "refused"], "asked"),
        (["refused"], "refused"),
    ],
)
def test_status_is_derived_from_the_parts(outcomes: list[str], status: str) -> None:
    parts = []
    for index, outcome in enumerate(outcomes):
        extra = {"reason": "r"} if outcome == "refused" else {}
        extra |= {"ask": _ask(f"a{index}")} if outcome == "asked" else {}
        parts.append(Part(part_id=f"e{index}", text="t", outcome=outcome, **extra))
    answer = _answer("e0") if "answered" in outcomes else None
    assert _reply(parts=parts, answer=answer).status == status


def test_a_parser_refusal_has_no_parts() -> None:
    reply = _reply(blocking=Refusal(stage="parse", reason="I could not understand the question."))
    assert reply.status == "refused"
    with pytest.raises(ValidationError, match="no parts must carry a blocking refusal"):
        _reply()


def test_a_blocking_refusal_leaves_nothing_answered() -> None:
    with pytest.raises(ValidationError, match="leaves no part answered"):
        _reply(
            blocking=Refusal(stage="map", reason="No data for Samsung."),
            parts=[_answered()],
            answer=_answer(),
        )


def test_figures_travel_only_with_answered_parts() -> None:
    with pytest.raises(ValidationError, match="answer is set exactly"):
        _reply(parts=[_answered()])
    refused = Part(part_id="e1", text="revenue", outcome="refused", reason="r")
    with pytest.raises(ValidationError, match="answer is set exactly"):
        _reply(parts=[refused], answer=_answer())


def test_figures_for_a_part_not_marked_answered_are_refused() -> None:
    with pytest.raises(ValidationError, match="not marked answered"):
        _reply(parts=[_answered("e1")], answer=_answer("e9"))


def test_ask_ids_are_unique_across_parts() -> None:
    with pytest.raises(ValidationError, match="must each be unique"):
        _reply(
            parts=[
                Part(part_id="e1", text="a", outcome="asked", ask=_ask("a1")),
                Part(part_id="e2", text="b", outcome="asked", ask=_ask("a1")),
            ]
        )


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


def test_the_question_limit_is_the_parsers() -> None:
    from app.parser import MAX_QUESTION as PARSER_LIMIT

    assert MAX_QUESTION == PARSER_LIMIT
    with pytest.raises(ValidationError):
        NewConversationIn(question="x" * (MAX_QUESTION + 1))


def test_an_answer_names_its_kind_on_the_wire() -> None:
    dumped = OptionAnswerIn(ask_id="a1", option_id="o1").model_dump(mode="json")
    assert dumped == {"kind": "option", "ask_id": "a1", "option_id": "o1"}
    with pytest.raises(ValidationError):
        OptionAnswerIn.model_validate(dumped | {"kind": "text"})


def test_each_ask_is_answered_at_most_once() -> None:
    answer = OptionAnswerIn(ask_id="a1", option_id="o1")
    with pytest.raises(ValidationError, match="at most once"):
        AnswersIn(answers=[answer, answer])
    with pytest.raises(ValidationError):
        AnswersIn(answers=[])


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


def test_events_are_told_apart_by_kind() -> None:
    adapter = TypeAdapter(JobEvent)
    stage = adapter.validate_python({"kind": "stage", "stage": "parsing", "seconds": 0.4})
    assert isinstance(stage, StageEvent)
    reply = _reply(blocking=Refusal(stage="parse", reason="r"))
    done = adapter.validate_json(DoneEvent(reply=reply).model_dump_json())
    assert isinstance(done, DoneEvent) and done.reply.status == "refused"


def test_a_job_carries_its_reply_exactly_when_done() -> None:
    reply = _reply(blocking=Refusal(stage="parse", reason="r"))
    assert JobView(job_id=JOB, conversation_id=CONVERSATION, status="done", reply=reply).reply
    with pytest.raises(ValidationError, match="exactly when status is 'done'"):
        JobView(job_id=JOB, conversation_id=CONVERSATION, status="done")
    with pytest.raises(ValidationError, match="exactly when status is 'done'"):
        JobView(job_id=JOB, conversation_id=CONVERSATION, status="parsing", reply=reply)


def test_the_reply_schema_publishes_its_status() -> None:
    """The Angular types come from this schema (§9); a derived field must be in it."""
    schema = Reply.model_json_schema(mode="serialization")
    assert "status" in schema["properties"]


def test_a_stored_reply_reads_back_and_its_status_is_rechecked() -> None:
    reply = _reply(parts=[_answered()], answer=_answer())
    stored = reply.model_dump_json()
    assert Reply.model_validate_json(stored) == reply
    with pytest.raises(ValidationError, match="disagrees with the parts"):
        Reply.model_validate_json(stored.replace('"status":"answered"', '"status":"refused"'))
