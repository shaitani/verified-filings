"""Conversations and jobs in ``web`` (app/api/storage.py), as the web role, on the test database."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.api import storage
from app.api.schemas import Refusal, Reply
from app.chain import AskRecord, OptionRecord, Pending, UnknownChoice
from app.db.session import WebRoleMissing, web_sessionmaker
from app.db.web import Job
from app.schemas.query import QueryIn, QueryPlan

CHAIN = Path(__file__).parent / "fixtures" / "chain"


def _query(name: str) -> QueryIn:
    data = json.loads((CHAIN / f"{name}.json").read_text(encoding="utf-8"))
    return QueryIn.model_validate(data["query_in"])


def _ambiguity() -> Pending:
    """What round 1 of "accounts payable" asked (the live run, tests/fixtures/chain)."""
    plan = QueryPlan.model_validate(
        json.loads((CHAIN / "ambiguous_round2.json").read_text(encoding="utf-8"))["plan"]
    )
    concept = plan.bindings[0].concepts[0]
    return Pending(
        query_in=_query("ambiguous"),
        asks=[
            AskRecord(
                ask_id="a1", kind="ambiguity", element_id="e2", element_text="accounts payable",
                question="Which of these did you mean by 'accounts payable'?",
                options=[OptionRecord(option_id="o1", label="Accounts Payable, Current",
                                      concept=concept)],
            )
        ],
    )


def _clarification() -> Pending:
    return Pending(
        query_in=_query("q046"),
        asks=[
            AskRecord(
                ask_id="a1", kind="clarification", element_id="e2", element_text="profit margin",
                question="Which profit margin do you mean?",
                options=[
                    OptionRecord(option_id="o1", label="Gross margin", metric="gross_margin"),
                    OptionRecord(option_id="o2", label="Net profit margin", metric="net_margin"),
                ],
            )
        ],
    )


def _reply(conversation_id, job_id) -> Reply:
    return Reply(
        conversation_id=conversation_id,
        job_id=job_id,
        blocking=Refusal(stage="parse", reason="I couldn't work out what that question asks."),
    )


async def _users(owner, *names: str) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in names]
    async with owner() as session:
        for user_id, name in zip(ids, names, strict=True):
            await session.execute(
                text(
                    'INSERT INTO web."user" (id, email, hashed_password, is_active, is_verified) '
                    "VALUES (:id, :email, 'x', true, false)"
                ),
                {"id": user_id, "email": f"zz-store-{name}@example.test"},
            )
        await session.commit()
    return ids


async def _asked_round(web, user, pending: Pending) -> tuple[uuid.UUID, uuid.UUID]:
    """A conversation whose first round finished having asked ``pending``."""
    conversation, job = await storage.create_conversation(web, user, "q?")
    await storage.finish(web, job, _reply(conversation, job), pending)
    return conversation, job


# --------------------------------------------------------------------------- #
# The web role, and only the web role
# --------------------------------------------------------------------------- #


def test_the_web_server_will_not_start_without_its_own_role(test_db_url) -> None:
    with pytest.raises(WebRoleMissing, match="DATABASE_URL_WEB is not set"):
        web_sessionmaker("")
    with pytest.raises(WebRoleMissing, match="not 'vf_web_role'"):
        web_sessionmaker(test_db_url)  # the owner: every grant would be undone


# --------------------------------------------------------------------------- #
# A job's life
# --------------------------------------------------------------------------- #


async def test_a_new_question_is_a_queued_first_round(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, job = await storage.create_conversation(web, user, "What was Apple's revenue?")
    view = await storage.job_view(web, user, job)
    assert (view.conversation_id, view.status, view.reply) == (conversation, "queued", None)


async def test_a_job_moves_through_its_stages_and_then_never_again(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, job = await storage.create_conversation(web, user, "q?")
    await storage.set_status(web, job, "parsing")
    assert (await storage.job_view(web, user, job)).status == "parsing"

    await storage.finish(web, job, _reply(conversation, job), None)
    view = await storage.job_view(web, user, job)
    assert view.status == "done" and view.reply.status == "refused"  # read back, re-checked

    with pytest.raises(storage.NotFound, match="no unfinished job"):
        await storage.set_status(web, job, "mapping")  # done is final
    with pytest.raises(ValueError, match="is final"):
        await storage.set_status(web, job, "done")  # only finish() sets it


async def test_a_failed_job_has_no_reply(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    _, job = await storage.create_conversation(web, user, "q?")
    await storage.fail(web, job)
    view = await storage.job_view(web, user, job)
    assert (view.status, view.reply) == ("failed", None)


async def test_another_reader_s_job_does_not_exist(web_factory) -> None:
    web, owner = web_factory
    mine, theirs = await _users(owner, "a", "b")
    conversation, job = await storage.create_conversation(web, mine, "q?")
    with pytest.raises(storage.NotFound):
        await storage.job_view(web, theirs, job)
    await storage.finish(web, job, _reply(conversation, job), _ambiguity())
    with pytest.raises(storage.NotFound):
        await storage.create_round(web, theirs, conversation, [("a1", "o1")])


async def test_a_restart_fails_the_jobs_it_cut_off(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    _, running = await storage.create_conversation(web, user, "q1?")
    await storage.set_status(web, running, "fetching")
    _, queued = await storage.create_conversation(web, user, "q2?")
    conversation, done = await storage.create_conversation(web, user, "q3?")
    await storage.finish(web, done, _reply(conversation, done), None)

    assert await storage.sweep_unfinished(web) >= 2
    statuses = {j: (await storage.job_view(web, user, j)).status for j in (running, queued, done)}
    assert statuses == {running: "failed", queued: "failed", done: "done"}


# --------------------------------------------------------------------------- #
# Answering: the next round
# --------------------------------------------------------------------------- #


async def test_a_round_that_is_still_running_cannot_be_answered(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, _ = await storage.create_conversation(web, user, "q?")
    with pytest.raises(storage.NotReady):
        await storage.create_round(web, user, conversation, [("a1", "o1")])


async def test_a_round_that_asked_nothing_cannot_be_answered(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, job = await storage.create_conversation(web, user, "q?")
    await storage.finish(web, job, _reply(conversation, job), None)
    with pytest.raises(storage.NothingToAnswer):
        await storage.create_round(web, user, conversation, [("a1", "o1")])


async def test_a_pick_never_offered_is_refused_and_nothing_is_stored(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, _ = await _asked_round(web, user, _ambiguity())
    with pytest.raises(UnknownChoice):
        await storage.create_round(web, user, conversation, [("a1", "o9")])
    async with owner() as session:
        rounds = await session.scalar(
            select(func.count()).select_from(Job).where(Job.conversation_id == conversation)
        )
    assert rounds == 1


async def test_an_answer_is_stored_with_its_text(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, _ = await _asked_round(web, user, _ambiguity())
    job = await storage.create_round(web, user, conversation, [("a1", "o1")])
    async with owner() as session:
        stored = await session.get(Job, job)
    assert (stored.round, stored.status) == (2, "queued")
    assert stored.answers == [
        {"ask_id": "a1", "kind": "option", "option_id": "o1", "text": "Accounts Payable, Current"}
    ]


async def test_two_answers_at_once_start_one_round(web_factory) -> None:
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, _ = await _asked_round(web, user, _ambiguity())
    results = await asyncio.gather(
        storage.create_round(web, user, conversation, [("a1", "o1")]),
        storage.create_round(web, user, conversation, [("a1", "o1")]),
        return_exceptions=True,
    )
    assert sum(isinstance(r, uuid.UUID) for r in results) == 1
    # The loser either read round 1 too and hit the unique round number (Conflict),
    # or read the winner's queued round 2 (NotReady): a refusal either way.
    (refused,) = [r for r in results if not isinstance(r, uuid.UUID)]
    assert isinstance(refused, storage.Conflict | storage.NotReady), refused


async def test_each_round_is_given_every_earlier_round_s_answers(web_factory) -> None:
    """Round 2 picked a concept for "accounts payable"; round 3 answers a curated
    question. Round 3 must carry round 2's pin and add its own choice."""
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, first = await _asked_round(web, user, _ambiguity())

    one = await storage.round_inputs(web, first)
    assert (one.round, one.pending, one.choices, one.answers, one.pins) == (1, None, [], [], {})

    second = await storage.create_round(web, user, conversation, [("a1", "o1")])
    two = await storage.round_inputs(web, second)
    assert two.pending == _ambiguity() and two.choices == [("a1", "o1")]
    assert (two.answers, two.pins) == ([], {})  # nothing earlier than round 1's own questions

    await storage.finish(web, second, _reply(conversation, second), _clarification())
    third = await storage.create_round(web, user, conversation, [("a1", "o2")])
    three = await storage.round_inputs(web, third)
    assert three.pending == _clarification() and three.choices == [("a1", "o2")]
    assert set(three.pins) == {"accounts payable"}  # round 2's pick, carried forward
    assert three.question == "q?"


async def test_a_reader_s_conversations_newest_first(web_factory) -> None:
    web, owner = web_factory
    mine, theirs = await _users(owner, "a", "b")
    older, _ = await _asked_round(web, mine, _ambiguity())
    await storage.create_round(web, mine, older, [("a1", "o1")])
    newer, _ = await storage.create_conversation(web, mine, "newer?")
    await storage.create_conversation(web, theirs, "not mine")

    listed = await storage.conversations(web, mine)
    assert [c.conversation_id for c in listed] == [newer, older]
    assert [(c.rounds, c.status) for c in listed] == [(1, "queued"), (2, "queued")]


async def test_a_round_number_taken_meanwhile_is_a_conflict(web_factory, monkeypatch) -> None:
    """Both answers read round 1 as the latest; the second insert of round 2 hits
    the unique (conversation, round) and is refused -- forced here by handing
    create_round a stale "latest round" after round 2 already exists."""
    web, owner = web_factory
    (user,) = await _users(owner, "a")
    conversation, first = await _asked_round(web, user, _ambiguity())
    await storage.create_round(web, user, conversation, [("a1", "o1")])

    async with web() as session:
        stale = await session.get(Job, first)

    async def still_round_one(session, conversation_id):
        return stale

    monkeypatch.setattr(storage, "_latest_round", still_round_one)
    with pytest.raises(storage.Conflict, match="started first"):
        await storage.create_round(web, user, conversation, [("a1", "o1")])
