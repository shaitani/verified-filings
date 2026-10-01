"""Conversations and jobs in ``web``, as ``vf_web_role`` (``app/api/DESIGN.md`` §4, §11).

Every function takes the session factory -- ``web_sessionmaker()`` in the
server, a test database's in the tests -- and every read or write on behalf of
a reader takes that reader's ``user_id``: a conversation that is not theirs is
``NotFound``, never someone else's.

How rounds chain: round *n*'s job stores the picks made against round *n-1*'s
questions (``Job.answers``); round *n-1* stores what it asked (``Job.asks``, a
``chain.Pending``). ``round_inputs`` replays every earlier round's picks, so
nothing but those rows has to be kept.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.schemas import (
    ConversationSummary,
    ConversationView,
    GivenAnswer,
    JobView,
    Reply,
    RoundView,
)
from app.chain import BUG, Pending, resolve_choices
from app.db.web import Conversation, Job, JobFeedback
from app.schemas.job import FINISHED, JobStatus
from app.schemas.query import ConceptRef


class NotFound(LookupError):
    """No such conversation or job -- or not this reader's, which reads the same."""


class NotReady(RuntimeError):
    """The last round is still running; its questions are not settled yet."""


class NothingToAnswer(RuntimeError):
    """The last round asked nothing, so there is no question to answer."""


class Conflict(RuntimeError):
    """Another round was started at the same moment (the unique round number)."""


@dataclass
class RoundInputs:
    """What running one round needs; ``chain.ask`` / ``chain.ask_again`` take it from here."""

    question: str
    conversation_id: UUID
    round: int
    pending: Pending | None = None  # the previous round's questions; None for round 1
    choices: list[tuple[str, str]] = field(default_factory=list)  # this round's picks
    answers: list[tuple[str, str]] = field(default_factory=list)  # every earlier round's
    pins: dict[str, ConceptRef] = field(default_factory=dict)  # every earlier round's


# --------------------------------------------------------------------------- #
# Starting rounds
# --------------------------------------------------------------------------- #


async def create_conversation(
    session_factory: async_sessionmaker, user_id: UUID, question: str
) -> tuple[UUID, UUID]:
    """A conversation and its first round, queued. ``(conversation_id, job_id)``."""
    conversation = Conversation(id=uuid.uuid4(), user_id=user_id, question=question)
    job = Job(id=uuid.uuid4(), conversation_id=conversation.id, round=1, status="queued")
    async with session_factory() as session:
        session.add(conversation)
        await session.flush()  # no relationships declared, so the parent goes first
        session.add(job)
        await session.commit()
    return conversation.id, job.id


async def create_round(
    session_factory: async_sessionmaker,
    user_id: UUID,
    conversation_id: UUID,
    choices: list[tuple[str, str]],
) -> UUID:
    """The next round, answering the last one's questions. The picks are checked
    against what was offered **before** anything is written (``UnknownChoice``)."""
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise NotFound(f"no conversation {conversation_id}")
        latest = await _latest_round(session, conversation_id)
        if latest.status not in FINISHED:
            raise NotReady(f"round {latest.round} is still {latest.status}")
        if latest.status != "done" or not latest.asks:
            raise NothingToAnswer(f"round {latest.round} asked nothing to answer")

        pending = Pending.model_validate(latest.asks)
        resolve_choices(pending, choices)  # raises UnknownChoice: nothing stored
        labels = {
            (record.ask_id, option.option_id): option.label
            for record in pending.asks
            for option in record.options
        }
        stored = [
            GivenAnswer(ask_id=a, option_id=o, text=labels[(a, o)]).model_dump(mode="json")
            for a, o in choices
        ]
        job = Job(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            round=latest.round + 1,
            status="queued",
            answers=stored,
        )
        session.add(job)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise Conflict("another round of this conversation was started first") from exc
    return job.id


async def round_inputs(session_factory: async_sessionmaker, job_id: UUID) -> RoundInputs:
    """Everything one round needs to run, rebuilt from the conversation's rows."""
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise NotFound(f"no job {job_id}")
        conversation = await session.get(Conversation, job.conversation_id)
        rounds = list(
            (
                await session.scalars(
                    select(Job)
                    .where(Job.conversation_id == job.conversation_id, Job.round <= job.round)
                    .order_by(Job.round)
                )
            ).all()
        )
    inputs = RoundInputs(
        question=conversation.question, conversation_id=conversation.id, round=job.round
    )
    for earlier, later in zip(rounds, rounds[1:], strict=False):
        pending = Pending.model_validate(earlier.asks)
        choices = [(a["ask_id"], a["option_id"]) for a in later.answers or []]
        if later.id == job.id:
            inputs.pending, inputs.choices = pending, choices  # this round's own
        else:
            answers, pins = resolve_choices(pending, choices)
            inputs.answers += answers
            inputs.pins |= pins
    return inputs


# --------------------------------------------------------------------------- #
# Running and finishing -- each a no-op on a job already finished
# --------------------------------------------------------------------------- #


async def set_status(session_factory: async_sessionmaker, job_id: UUID, status: JobStatus) -> None:
    """A stage change. Never moves a finished job: done and failed are final."""
    if status in FINISHED:
        raise ValueError(f"{status!r} is final: use finish() or fail()")
    await _update(session_factory, job_id, status=status)


async def finish(
    session_factory: async_sessionmaker, job_id: UUID, reply: Reply, pending: Pending | None
) -> None:
    """Done: the reply, and what the next round will need if anything was asked."""
    await _update(
        session_factory,
        job_id,
        status="done",
        reply=reply.model_dump(mode="json"),
        asks=pending.model_dump(mode="json") if pending else None,
        finished_at=func.now(),
    )


async def fail(session_factory: async_sessionmaker, job_id: UUID) -> None:
    """Failed: the chain itself broke. A refusal is not this -- it finishes ``done``."""
    await _update(session_factory, job_id, status="failed", finished_at=func.now())


async def sweep_unfinished(session_factory: async_sessionmaker) -> int:
    """At startup: a job still mid-run was cut off by a restart, so it failed
    rather than stay "queued" forever (§4). Returns how many."""
    async with session_factory() as session:
        swept = await session.execute(
            update(Job)
            .where(Job.status.not_in(FINISHED))
            .values(status="failed", finished_at=func.now())
        )
        await session.commit()
    return swept.rowcount


@dataclass(frozen=True)
class Admission:
    """What a new round would join (limits.py): counted in one statement."""

    unfinished_mine: int
    unfinished_all: int
    today_mine: int
    #: Seconds until the reader's oldest round in the last 24 hours drops out of
    #: them -- when the daily cap next frees one. ``None`` with none.
    first_frees_in: float | None


async def admission(session_factory: async_sessionmaker, user_id: UUID) -> Admission:
    """Counted from the rows, so a restart resets nothing -- and a round counts the
    moment it is written, before the runner sees it."""
    unfinished = Job.status.not_in(FINISHED)
    mine = Conversation.user_id == user_id
    today = Job.created_at > func.now() - text("interval '24 hours'")
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    func.count().filter(unfinished & mine),
                    func.count().filter(unfinished),
                    func.count().filter(mine & today),
                    func.extract(
                        "epoch",
                        func.min(Job.created_at).filter(mine & today)
                        + text("interval '24 hours'")
                        - func.now(),
                    ),
                )
                .select_from(Job)
                .join(Conversation, Conversation.id == Job.conversation_id)
                .where(unfinished | (mine & today))
            )
        ).one()
    frees_in = None if row[3] is None else max(0.0, float(row[3]))
    return Admission(row[0], row[1], row[2], frees_in)


# --------------------------------------------------------------------------- #
# Reading, for the reader it belongs to
# --------------------------------------------------------------------------- #


async def job_view(session_factory: async_sessionmaker, user_id: UUID, job_id: UUID) -> JobView:
    async with session_factory() as session:
        found = (
            await session.execute(
                select(Job).join(Conversation).where(
                    Job.id == job_id, Conversation.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    if found is None:
        raise NotFound(f"no job {job_id}")
    return JobView(
        job_id=found.id,
        conversation_id=found.conversation_id,
        status=found.status,
        reply=Reply.model_validate(found.reply) if found.reply else None,  # re-checked on read
    )


async def conversations(
    session_factory: async_sessionmaker, user_id: UUID, limit: int = 50
) -> list[ConversationSummary]:
    """The reader's conversations, newest first, each with its latest round's status
    and, once done, its reply's -- read from the stored reply, not the whole of it."""
    latest = (
        select(Job.conversation_id, func.max(Job.round).label("rounds"))
        .group_by(Job.conversation_id)
        .subquery()
    )
    async with session_factory() as session:
        rows = await session.execute(
            select(Conversation, latest.c.rounds, Job.status, Job.reply["status"].astext)
            .join(latest, latest.c.conversation_id == Conversation.id)
            .join(Job, (Job.conversation_id == Conversation.id) & (Job.round == latest.c.rounds))
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.created_at.desc())
            .limit(limit)
        )
        return [
            ConversationSummary(
                conversation_id=conversation.id,
                question=conversation.question,
                created_at=conversation.created_at,
                rounds=rounds,
                status=status,
                reply_status=reply_status,
            )
            for conversation, rounds, status, reply_status in rows
        ]


async def conversation_view(
    session_factory: async_sessionmaker, user_id: UUID, conversation_id: UUID
) -> ConversationView:
    """A past conversation reopened: every round in order, each with what started
    it and how it ended. ``NotFound`` unless it is this reader's."""
    async with session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise NotFound(f"no conversation {conversation_id}")
        jobs = (
            await session.scalars(
                select(Job).where(Job.conversation_id == conversation_id).order_by(Job.round)
            )
        ).all()
    return ConversationView(
        conversation_id=conversation.id,
        question=conversation.question,
        created_at=conversation.created_at,
        rounds=[
            RoundView(
                round=job.round,
                job_id=job.id,
                status=job.status,
                answers=[GivenAnswer.model_validate(a) for a in job.answers or []],
                reply=Reply.model_validate(job.reply) if job.reply else None,  # re-checked
                message=BUG if job.status == "failed" else None,  # as the event stream says it
            )
            for job in jobs
        ],
    )


async def _latest_round(session, conversation_id: UUID) -> Job:
    return (
        await session.scalars(
            select(Job).where(Job.conversation_id == conversation_id).order_by(Job.round.desc())
        )
    ).first()


async def _update(session_factory: async_sessionmaker, job_id: UUID, **values) -> None:
    async with session_factory() as session:
        moved = await session.execute(
            update(Job).where(Job.id == job_id, Job.status.not_in(FINISHED)).values(**values)
        )
        await session.commit()
    if moved.rowcount == 0:
        raise NotFound(f"no unfinished job {job_id}")


async def add_feedback(
    session_factory: async_sessionmaker, user_id: UUID, job_id: UUID, note: str | None
) -> None:
    """Report a problem on one of this reader's jobs (DESIGN §8). Written, never read
    back by the web role -- it makes the job a ``--flagged`` one for the owner's CLI."""
    await job_view(session_factory, user_id, job_id)  # NotFound unless it is theirs
    async with session_factory() as session:
        session.add(JobFeedback(id=uuid.uuid4(), job_id=job_id, user_id=user_id, note=note))
        await session.commit()
