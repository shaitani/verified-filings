"""Request and response models for the Web Server. ``app/api/DESIGN.md`` §1, §3, §4."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, ModelWrapValidatorHandler, computed_field, model_validator

from app.schemas.answer_view import AnswerView
from app.schemas.query import _Base

# --------------------------------------------------------------------------- #
# The reply
# --------------------------------------------------------------------------- #

#: The block a refusal came from. The reader sees a fixed sentence for
#: "parse" and "execute"; the model-facing message goes to the trace (§1, §8).
Stage = Literal["parse", "map", "execute"]

PartOutcome = Literal["answered", "asked", "refused"]


class Refusal(_Base):
    """A refusal that sinks the whole question: no part is answered."""

    stage: Stage
    reason: str = Field(min_length=1, max_length=512)


class Option(_Base):
    option_id: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=512)  # curated text, or a concept's label
    description: str = Field(min_length=1, max_length=512)  # ambiguity: "us-gaap:Revenues"


class Ask(_Base):
    """A question back to the asker (§3)."""

    ask_id: str = Field(min_length=1, max_length=32)
    kind: Literal["clarification", "ambiguity"]  # "ambiguity" gets a visible tag
    question: str = Field(min_length=1, max_length=512)
    options: list[Option] = Field(min_length=1)  # an ambiguity may offer a single candidate

    @model_validator(mode="after")
    def _option_ids_unique(self) -> Ask:
        ids = [option.option_id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError(f"ask {self.ask_id!r} repeats an option_id")
        return self


class Part(_Base):
    """One thing the question asked for, and what became of it (§1)."""

    part_id: str = Field(min_length=1, max_length=32)  # the element id, as AnswerRow.element_id
    text: str = Field(min_length=1, max_length=256)  # the asker's phrase, verbatim
    outcome: PartOutcome
    reason: str | None = Field(default=None, max_length=512)  # set exactly when refused
    ask: Ask | None = None  # set exactly when asked

    @model_validator(mode="after")
    def _fields_match_outcome(self) -> Part:
        if (self.reason is not None) != (self.outcome == "refused"):
            raise ValueError(f"part {self.part_id!r}: reason is set exactly when refused")
        if (self.ask is not None) != (self.outcome == "asked"):
            raise ValueError(f"part {self.part_id!r}: ask is set exactly when asked")
        return self


class Reply(_Base):
    """Everything one job sends back. Parts may mix answered, asked and refused."""

    conversation_id: UUID
    job_id: UUID
    blocking: Refusal | None = None
    parts: list[Part] = Field(default_factory=list)  # empty when the parser refused
    answer: AnswerView | None = None  # the figures for the answered parts

    @computed_field  # derived for a header line, never set: one source of truth
    @property
    def status(self) -> Literal["answered", "partial", "asked", "refused"]:
        outcomes = {part.outcome for part in self.parts}
        if self.blocking is not None or not outcomes:
            return "refused"
        if outcomes == {"answered"}:
            return "answered"
        if "answered" in outcomes:
            return "partial"
        return "asked" if "asked" in outcomes else "refused"

    @model_validator(mode="wrap")
    @classmethod
    def _status_read_back(cls, data: Any, handler: ModelWrapValidatorHandler[Reply]) -> Reply:
        # A stored reply carries the status it was sent with; accept it only if
        # re-deriving agrees, so a reply read back from web.job is re-checked.
        claimed = None
        if isinstance(data, dict) and "status" in data:
            data = dict(data)
            claimed = data.pop("status")
        reply = handler(data)
        if claimed is not None and claimed != reply.status:
            raise ValueError(f"status {claimed!r} disagrees with the parts ({reply.status!r})")
        return reply

    @model_validator(mode="after")
    def _consistent(self) -> Reply:
        answered = {part.part_id for part in self.parts if part.outcome == "answered"}
        if self.blocking is not None and answered:
            raise ValueError("a blocking refusal leaves no part answered")
        if (self.answer is not None) != bool(answered):
            # An executor refusal turns every answered part into a refused one,
            # so figures and "answered" parts always travel together.
            raise ValueError("answer is set exactly when some part is answered")
        if self.answer is not None:
            stray = {row.element_id for row in self.answer.rows} - answered
            if stray:
                raise ValueError(f"figures for parts not marked answered: {sorted(stray)}")
        if not self.parts and self.blocking is None:
            raise ValueError("a reply with no parts must carry a blocking refusal")

        part_ids = [part.part_id for part in self.parts]
        ask_ids = [part.ask.ask_id for part in self.parts if part.ask is not None]
        if len(part_ids) != len(set(part_ids)) or len(ask_ids) != len(set(ask_ids)):
            raise ValueError("part_ids and ask_ids must each be unique")
        return self


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


#: The parser's limit. Not imported: that would load the model client with the
#: schemas. A test holds the two equal.
MAX_QUESTION = 2000


class NewConversation(_Base):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)


class OptionAnswer(_Base):
    kind: Literal["option"] = "option"  # required on the wire; "text" joins later (§3)
    ask_id: str = Field(min_length=1, max_length=32)
    option_id: str = Field(min_length=1, max_length=32)  # checked against the options offered


#: One member today. Free text becomes ``OptionAnswer | TextAnswer`` behind a
#: ``kind`` discriminator; clients already send ``kind``, so nothing they send changes.
Answer = OptionAnswer


class AnswerRequest(_Base):
    answers: list[Answer] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_answer_per_ask(self) -> AnswerRequest:
        ids = [answer.ask_id for answer in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("each ask is answered at most once")
        return self


class FeedbackRequest(_Base):
    """Report a problem on one job (§8)."""

    note: str | None = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------- #
# Jobs and their events
# --------------------------------------------------------------------------- #

#: In order. "queued" is real: model calls go through a one-at-a-time gate (§4).
JobStage = Literal["queued", "parsing", "mapping", "fetching", "presenting"]
JobStatus = Literal["queued", "parsing", "mapping", "fetching", "presenting", "done", "failed"]


class JobCreated(_Base):
    conversation_id: UUID
    job_id: UUID


class StageEvent(_Base):
    kind: Literal["stage"] = "stage"
    stage: JobStage
    seconds: float = Field(ge=0)  # since the job started


class DoneEvent(_Base):
    kind: Literal["done"] = "done"
    reply: Reply  # refusals arrive here too: a refusal is a finished job


class FailedEvent(_Base):
    kind: Literal["failed"] = "failed"
    message: str = Field(min_length=1, max_length=512)  # a fixed sentence; detail in the trace


JobEvent = Annotated[StageEvent | DoneEvent | FailedEvent, Field(discriminator="kind")]


class JobView(_Base):
    """``GET /api/jobs/{id}`` -- for a reload or a dropped stream."""

    job_id: UUID
    conversation_id: UUID
    status: JobStatus
    reply: Reply | None = None  # set exactly when done

    @model_validator(mode="after")
    def _reply_when_done(self) -> JobView:
        if (self.reply is not None) != (self.status == "done"):
            raise ValueError("reply is set exactly when status is 'done'")
        return self
