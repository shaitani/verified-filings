"""The job trace: collected while a job runs, written as the web role, read as the owner.

The hooks are exercised with a fake Ollama client, so no model is needed; the
row is written and read against the test database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import trace
from app.api.trace import code_version, failed, flagged, show_job, write_trace
from app.chain import ErrorRecord, Outcome
from app.db import roles
from app.parser import proposer
from app.retrieval import InvalidSQL, executor, generator
from app.schemas.query import QueryIn, QueryPlan
from app.schemas.result import ResultSet

CHAIN = Path(__file__).parent / "fixtures" / "chain"


def _fixture(name: str) -> tuple[QueryIn, QueryPlan, ResultSet | None]:
    data = json.loads((CHAIN / f"{name}.json").read_text(encoding="utf-8"))
    result = data["result_set"]
    return (
        QueryIn.model_validate(data["query_in"]),
        QueryPlan.model_validate(data["plan"]),
        ResultSet.model_validate(result) if result else None,
    )


class _FakeOllama:
    """Stands in for ollama.AsyncClient: returns one canned response."""

    response: dict = {}

    def __init__(self, **_):
        pass

    async def generate(self, **_):
        return self.response


def _ollama(monkeypatch, module, response: dict) -> None:
    fake = type("Fake", (_FakeOllama,), {"response": response})
    monkeypatch.setattr(module, "AsyncClient", fake)


# --------------------------------------------------------------------------- #
# The collector
# --------------------------------------------------------------------------- #


def test_nothing_is_collected_outside_a_job() -> None:
    trace.model_call("parse", "m", "prompt", "reply", 0.1, None)  # must not raise, must not keep
    trace.statement("SELECT 1")
    assert trace.job_id() is None


async def test_two_jobs_at_once_keep_their_own_traces() -> None:
    async def job(name: str) -> trace.Trace:
        with trace.collecting(job_id=name) as collected:
            await asyncio.sleep(0)  # let the other job run in between
            trace.statement(f"SELECT '{name}'")
            await asyncio.sleep(0)
            assert trace.job_id() == name
        return collected

    a, b = await asyncio.gather(job("a"), job("b"))
    assert [s["sql"] for s in a.statements] == ["SELECT 'a'"]
    assert [s["sql"] for s in b.statements] == ["SELECT 'b'"]


# --------------------------------------------------------------------------- #
# The hooks
# --------------------------------------------------------------------------- #


async def test_the_parser_s_call_is_kept_whole(monkeypatch) -> None:
    _ollama(monkeypatch, proposer, {"response": '{"elements": []}', "done_reason": "stop"})
    with trace.collecting() as collected:
        await proposer.propose("THE PROMPT")
    (call,) = collected.model_calls
    assert (call["stage"], call["prompt"], call["reply"], call["error"]) == (
        "parse", "THE PROMPT", '{"elements": []}', None,
    )


async def test_a_truncated_reply_is_kept_with_its_error(monkeypatch) -> None:
    """The reply most worth reading is the one that failed."""
    _ollama(monkeypatch, proposer, {"response": '{"elements": [{"id"', "done_reason": "length"})
    with trace.collecting() as collected:
        with pytest.raises(proposer.ProposalError):
            await proposer.propose("THE PROMPT")
    (call,) = collected.model_calls
    assert call["reply"] == '{"elements": [{"id"' and "token ceiling" in call["error"]


async def test_the_generator_s_call_is_kept(monkeypatch) -> None:
    _, plan, _ = _fixture("q040")  # an average: the model writes the layer above `figures`
    _ollama(monkeypatch, generator, {"response": "SELECT 1", "done_reason": "stop"})
    with trace.collecting() as collected:
        await generator._ask(plan, "qwen")
    (call,) = collected.model_calls
    assert call["stage"] == "generate" and call["reply"] == "SELECT 1"
    assert "figures" in call["prompt"]


async def test_a_rejected_statement_is_kept(monkeypatch) -> None:
    """validate() refuses it before it runs, so the executor's log never sees it."""
    import app.retrieval as retrieval

    async def writes_a_delete(plan, model):
        return "DELETE FROM xbrl.fact"

    monkeypatch.setattr(retrieval, "generate", writes_a_delete)
    _, plan, _ = _fixture("q001")
    with trace.collecting() as collected:
        with pytest.raises(InvalidSQL):
            await retrieval.answer(plan)
    (statement,) = collected.statements
    assert statement["sql"] == "DELETE FROM xbrl.fact" and statement["error"]


def test_the_retrieval_log_line_carries_the_job_id(monkeypatch, tmp_path) -> None:
    log = tmp_path / "retrieval_log.jsonl"
    monkeypatch.setattr(executor, "LOG_PATH", log)
    _, plan, _ = _fixture("q001")
    with trace.collecting(job_id="job-7") as collected:
        executor._log("SELECT 1", plan, None, error="boom")
    assert json.loads(log.read_text(encoding="utf-8"))["job_id"] == "job-7"
    assert collected.statements == [{"sql": "SELECT 1", "verdict": None, "error": "boom"}]


def test_the_code_version_names_what_ran(monkeypatch) -> None:
    code_version.cache_clear()
    monkeypatch.setenv("CODE_VERSION", "abc123")  # the container has no .git
    assert code_version() == "abc123"
    code_version.cache_clear()
    monkeypatch.delenv("CODE_VERSION")
    rev = code_version()
    assert len(rev.removesuffix("+dirty")) == 12
    code_version.cache_clear()


# --------------------------------------------------------------------------- #
# Written as the web role, read as the owner (test database)
# --------------------------------------------------------------------------- #

WEB_PASSWORD = "web-pw"


@pytest_asyncio.fixture
async def owner_and_web(test_db_url):
    """An owner session factory, a web-role one, and a job row to trace."""
    await roles.provision(test_db_url, {roles.WEB.name: WEB_PASSWORD})
    scheme, rest = test_db_url.split("://", 1)
    web_url = f"{scheme}://{roles.WEB.name}:{WEB_PASSWORD}@{rest.split('@', 1)[1]}"
    owner_engine, web_engine = create_async_engine(test_db_url), create_async_engine(web_url)

    async def clean() -> None:
        async with owner_engine.begin() as c:
            await c.execute(text("DELETE FROM web.\"user\" WHERE email LIKE 'zz-trace-%'"))

    await clean()
    user, conversation, job = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO web.\"user\" (id, email, hashed_password, is_active, is_verified) "
                "VALUES (:u, 'zz-trace-user@example.test', 'x', true, false)"
            ),
            {"u": user},
        )
        await c.execute(
            text("INSERT INTO web.conversation (id, user_id, question) VALUES (:c, :u, 'q?')"),
            {"c": conversation, "u": user},
        )
        await c.execute(
            text(
                "INSERT INTO web.job (id, conversation_id, round, status) "
                "VALUES (:j, :c, 1, 'fetching')"
            ),
            {"j": job, "c": conversation},
        )
    try:
        yield (
            async_sessionmaker(owner_engine, expire_on_commit=False),
            async_sessionmaker(web_engine, expire_on_commit=False),
            job,
            user,
        )
    finally:
        await clean()
        await owner_engine.dispose()
        await web_engine.dispose()


def _outcome() -> Outcome:
    query, plan, result = _fixture("q052")
    return Outcome(
        query_in=query,
        plan=plan,
        result=result,
        errors=[ErrorRecord(stage="execute", type="InvalidSQL", message="m", traceback="t")],
        timings={"parse": 1.0, "map": 0.2},
    )


async def test_the_web_role_writes_a_trace_the_owner_reads(owner_and_web) -> None:
    owner, web, job, _ = owner_and_web
    with trace.collecting(job_id=str(job)) as collected:
        trace.model_call("parse", "qwen", "PROMPT", "REPLY", 3.2, None)
        trace.statement("SELECT 1", verdict={"status": "complete"})
    await write_trace(web, job, _outcome(), collected)

    shown = await show_job(owner, job)
    kept = shown["trace"]
    assert kept["model_calls"][0]["prompt"] == "PROMPT"
    assert kept["statements"][0]["verdict"] == {"status": "complete"}
    assert kept["plan"]["question"] and kept["result"]["verdict"]["status"] == "complete"
    assert kept["models"]["parser"] == "qwen2.5-coder:7b"
    assert kept["code_version"] and kept["created_at"] is not None  # filled in by the database
    assert shown["question"] == "q?" and shown["rounds"][0]["round"] == 1


async def test_a_job_cut_off_before_its_trace_shows_none(owner_and_web) -> None:
    owner, _, job, _ = owner_and_web
    assert (await show_job(owner, job))["trace"] is None


async def test_an_error_on_the_way_makes_a_finished_job_findable(owner_and_web) -> None:
    """A refusal finishes the job, so its error lives only in the trace."""
    owner, web, job, _ = owner_and_web
    with trace.collecting() as collected:
        pass
    await write_trace(web, job, _outcome(), collected)
    found = [row for row in await failed(owner) if row["job_id"] == job]
    assert found and found[0]["first_error"]["type"] == "InvalidSQL"


async def test_a_reported_job_is_flagged(owner_and_web) -> None:
    owner, _, job, user = owner_and_web
    async with owner() as session:
        await session.execute(
            text(
                "INSERT INTO web.job_feedback (id, job_id, user_id, note) "
                "VALUES (:i, :j, :u, :n)"
            ),
            {"i": uuid.uuid4(), "j": job, "u": user, "n": "wrong year"},
        )
        await session.commit()
    reports = [row for row in await flagged(owner) if row["job_id"] == job]
    (report,) = reports
    assert (report["note"], report["question"]) == ("wrong year", "q?")
