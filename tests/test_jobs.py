"""The job queue (app/api/jobs.py): the real chain, run by the real runner, as the
web role on the test database. Only the model and XBRL stages are stood in for,
by the live captures in tests/fixtures/chain/."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

import app.chain as chain
from app.api import storage
from app.api.jobs import JobRunner, event_stream, sse
from app.api.schemas import DoneEvent, FailedEvent, StageEvent
from app.api.trace import show_job
from app.chain import BUG
from app.schemas.query import QueryIn, QueryPlan
from app.schemas.result import ResultSet

CHAIN = Path(__file__).parent / "fixtures" / "chain"


def _fixture(name: str):
    data = json.loads((CHAIN / f"{name}.json").read_text(encoding="utf-8"))
    return (
        QueryIn.model_validate(data["query_in"]),
        QueryPlan.model_validate(data["plan"]),
        ResultSet.model_validate(data["result_set"]) if data["result_set"] else None,
    )


def _stages(monkeypatch, name: str, *, round2: str | None = None, parse=None):
    """The chain's three outside stages, from a capture. With ``round2``, a
    mapping given pins answers from that capture instead -- the second round."""
    query, plan, result = _fixture(name)
    later = _fixture(round2) if round2 else None
    parsed: list[str] = []

    async def fake_parse(question, *, answers=None):
        parsed.append(question)
        if parse:
            await parse()
        return query

    async def fake_map(q, *, pins=None):
        return later[1] if (later and pins) else plan

    async def fake_answer(p):
        return later[2] if (later and p is later[1]) else result

    monkeypatch.setattr(chain, "parse_question", fake_parse)
    monkeypatch.setattr(chain, "map_query", fake_map)
    monkeypatch.setattr(chain, "answer", fake_answer)
    return parsed


async def _user(owner) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with owner() as session:
        await session.execute(
            text(
                'INSERT INTO web."user" (id, email, hashed_password, is_active, is_verified) '
                "VALUES (:id, :email, 'x', true, false)"
            ),
            {"id": user_id, "email": f"zz-jobs-{user_id}@example.test"},
        )
        await session.commit()
    return user_id


async def _collect(runner: JobRunner, job_id: uuid.UUID) -> list:
    return [event async for event in runner.watch(job_id)]


@pytest.fixture
async def running(web_factory):
    """A started runner over the web role, stopped afterwards."""
    web, owner = web_factory
    runner = JobRunner(web)
    await runner.start()
    try:
        yield runner, web, owner
    finally:
        await runner.stop()


# --------------------------------------------------------------------------- #
# A job, start to finish
# --------------------------------------------------------------------------- #


async def test_a_job_runs_every_stage_and_ends_stored_with_its_trace(running, monkeypatch) -> None:
    runner, web, owner = running
    _stages(monkeypatch, "q052")  # five figures and a refused goodwill
    user = await _user(owner)
    conversation, job = await storage.create_conversation(web, user, "Apple's balances?")

    await runner.submit(job)
    events = await _collect(runner, job)

    stages = [e.stage for e in events if isinstance(e, StageEvent)]
    assert stages == ["queued", "parsing", "mapping", "fetching", "presenting"]
    seconds = [e.seconds for e in events if isinstance(e, StageEvent)]
    assert seconds == sorted(seconds)  # time since submitted, never going back
    done = events[-1]
    assert isinstance(done, DoneEvent) and done.reply.status == "partial"

    view = await storage.job_view(web, user, job)
    assert view.status == "done" and view.reply == done.reply  # what was sent is what was kept
    kept = await show_job(owner, job)  # the owner reads the trace the web role wrote
    assert kept["trace"]["timings"].keys() >= {"parse", "map", "answer", "present"}
    assert kept["trace"]["result"]["verdict"]["status"] == "complete"


async def test_jobs_run_one_at_a_time_in_the_order_given(running, monkeypatch) -> None:
    runner, web, owner = running
    active, peak = 0, 0

    async def slow_parse():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)  # long enough for a second job to start, if it could
        active -= 1

    parsed = _stages(monkeypatch, "q001", parse=slow_parse)
    user = await _user(owner)
    jobs = []
    for question in ("first?", "second?", "third?"):
        _, job = await storage.create_conversation(web, user, question)
        jobs.append(job)
        await runner.submit(job)
    await runner.drained()

    assert peak == 1
    assert parsed == ["first?", "second?", "third?"]
    assert [(await storage.job_view(web, user, j)).status for j in jobs] == ["done"] * 3


async def test_a_broken_runner_fails_the_job_and_still_writes_its_trace(
    running, monkeypatch
) -> None:
    runner, web, owner = running
    _stages(monkeypatch, "q001")

    async def broken_finish(*_):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(storage, "finish", broken_finish)
    user = await _user(owner)
    _, job = await storage.create_conversation(web, user, "q?")
    await runner.submit(job)
    events = await _collect(runner, job)

    assert events[-1] == FailedEvent(message=BUG)
    assert (await storage.job_view(web, user, job)).status == "failed"
    kept = (await show_job(owner, job))["trace"]
    assert kept["errors"][-1]["stage"] == "runner"
    assert "the database went away" in kept["errors"][-1]["message"]


async def test_a_second_round_of_picks_alone_runs_without_parsing(running, monkeypatch) -> None:
    """Round 1 asks which "accounts payable"; round 2 picks one. The runner must
    replay the round's inputs and re-map the stored parse (chain.ask_again)."""
    runner, web, owner = running
    parsed = _stages(monkeypatch, "ambiguous", round2="ambiguous_round2")
    user = await _user(owner)
    conversation, first = await storage.create_conversation(web, user, "Apple's accounts payable?")
    await runner.submit(first)
    asked = (await _collect(runner, first))[-1]
    assert asked.reply.status == "asked" and asked.reply.parts[0].ask.kind == "ambiguity"

    parsed.clear()
    second = await storage.create_round(web, user, conversation, [("a1", "o1")])
    await runner.submit(second)
    events = await _collect(runner, second)

    assert parsed == []  # a round of picks alone does not parse
    assert "parsing" not in [e.stage for e in events if isinstance(e, StageEvent)]
    reply = events[-1].reply
    assert reply.status == "answered" and reply.answer.rows[0].display == "$68.96B"


# --------------------------------------------------------------------------- #
# Watching: live, late, reloaded, orphaned
# --------------------------------------------------------------------------- #


async def _frames(runner, web, user, job) -> list[str]:
    return [frame async for frame in event_stream(runner, web, user, job)]


async def test_a_finished_job_streams_its_answer_from_the_database(running, monkeypatch) -> None:
    runner, web, owner = running
    _stages(monkeypatch, "q001")
    user = await _user(owner)
    _, job = await storage.create_conversation(web, user, "q?")
    await runner.submit(job)
    await runner.drained()

    (frame,) = await _frames(runner, web, user, job)  # a reload, after the fact
    assert frame.startswith("event: done\n")
    assert json.loads(frame.split("data: ", 1)[1])["reply"]["status"] == "answered"


async def test_a_late_watcher_is_told_what_it_missed(running, monkeypatch) -> None:
    runner, web, owner = running
    gate = asyncio.Event()

    async def held():
        await gate.wait()  # the job stays in "parsing" until released

    _stages(monkeypatch, "q001", parse=held)
    user = await _user(owner)
    _, job = await storage.create_conversation(web, user, "q?")
    await runner.submit(job)
    await asyncio.sleep(0.05)  # it has reached "parsing" before anyone watches

    stream = asyncio.create_task(_frames(runner, web, user, job))
    await asyncio.sleep(0.05)
    gate.set()
    frames = await stream
    kinds = [f.split("\n", 1)[0] for f in frames]
    assert kinds[:2] == ["event: stage", "event: stage"]  # queued, parsing: replayed
    assert kinds[-1] == "event: done"


async def test_another_reader_cannot_watch_a_job(running, monkeypatch) -> None:
    runner, web, owner = running
    mine, theirs = await _user(owner), await _user(owner)
    _, job = await storage.create_conversation(web, mine, "q?")
    with pytest.raises(storage.NotFound):
        await _frames(runner, web, theirs, job)


async def test_an_orphaned_job_is_failed_rather_than_waited_on(running) -> None:
    """Unfinished yet never queued in this process: waiting would hang forever."""
    runner, web, owner = running
    user = await _user(owner)
    _, job = await storage.create_conversation(web, user, "q?")  # never submitted
    (frame,) = await _frames(runner, web, user, job)
    assert frame.startswith("event: failed\n")
    assert (await storage.job_view(web, user, job)).status == "failed"


async def test_starting_fails_what_a_restart_cut_off(web_factory) -> None:
    web, owner = web_factory
    user = await _user(owner)
    _, job = await storage.create_conversation(web, user, "q?")
    await storage.set_status(web, job, "fetching")  # mid-run when the process died
    runner = JobRunner(web)
    try:
        assert await runner.start() >= 1
    finally:
        await runner.stop()
    assert (await storage.job_view(web, user, job)).status == "failed"


def test_an_event_is_one_text_event_stream_frame() -> None:
    frame = sse(StageEvent(stage="mapping", seconds=1.25))
    assert frame == 'event: stage\ndata: {"kind":"stage","stage":"mapping","seconds":1.25}\n\n'
