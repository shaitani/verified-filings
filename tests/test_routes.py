"""The question routes (app/api/routes.py) over HTTP: the real app, real sign-in,
the real job runner and chain, as the web role on the test database. Only the
model and XBRL stages are stood in for, by the captures in tests/fixtures/chain/."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import routing
from sqlalchemy import text

import app.chain as chain
from app.api import admin, limits, storage
from app.api.auth import SESSION_COOKIE
from app.api.server import create_app
from app.schemas.query import QueryIn, QueryPlan
from app.schemas.result import ResultSet
from tests.test_auth import CONFIG, PASSWORD, _admin_url, _web_url

CHAIN = Path(__file__).parent / "fixtures" / "chain"

#: Which capture answers which question -- one per kind of reply.
QUESTIONS = {
    "Apple's balances?": "q052",  # figures and a refused goodwill
    "Apple's profit margin?": "q046",  # a curated question back
    "Apple's accounts payable?": "ambiguous",  # an ambiguity
}


def _load(name: str):
    data = json.loads((CHAIN / f"{name}.json").read_text(encoding="utf-8"))
    result = data["result_set"]
    return (
        QueryIn.model_validate(data["query_in"]),
        QueryPlan.model_validate(data["plan"]),
        ResultSet.model_validate(result) if result else None,
    )


@pytest.fixture(autouse=True)
def captured_chain(monkeypatch):
    """parse -> map -> answer from the captures. A round with a curated answer
    re-parses into q046's second round; a round with a pin re-maps into the
    accounts-payable one."""
    captures = {
        name: _load(name) for name in {*QUESTIONS.values(), "q046_round2", "ambiguous_round2"}
    }

    async def parse(question, *, answers=None):
        return captures["q046_round2" if answers else QUESTIONS[question]][0]

    async def map_query(query, *, pins=None):
        if pins:
            return captures["ambiguous_round2"][1]
        return next(plan for q, plan, _ in captures.values() if q is query)

    async def answer(plan):
        return next(result for _, p, result in captures.values() if p is plan)

    monkeypatch.setattr(chain, "parse_question", parse)
    monkeypatch.setattr(chain, "map_query", map_query)
    monkeypatch.setattr(chain, "answer", answer)


@pytest.fixture
async def server(web_factory, test_db_url):
    """The app started as uvicorn would; yields a factory of signed-in clients."""
    _, owner = web_factory
    app = create_app(  # as deployed, whatever .env says
        CONFIG, _web_url(test_db_url), _admin_url(test_db_url), showDocs=False
    )
    clients: list[httpx.AsyncClient] = []

    async def signed_in(name: str) -> httpx.AsyncClient:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(http)
        email = f"zz-routes-{name}@example.com"
        code = await admin.create_email_invite(owner, email)
        registered = await http.post(
            "/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": code}
        )
        assert registered.status_code == 201, registered.text
        login = await http.post("/api/auth/login", data={"username": email, "password": PASSWORD})
        assert login.status_code == 204
        return http

    async with app.router.lifespan_context(app):
        try:
            yield signed_in, app, owner
        finally:
            for http in clients:
                await http.aclose()


def _frames(body: str) -> list[tuple[str, dict]]:
    """A text/event-stream body as (event, data) pairs."""
    frames = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        frames.append((lines["event"], json.loads(lines["data"])))
    return frames


async def _ask(http, question: str) -> tuple[str, str, list]:
    created = await http.post("/api/conversations", json={"question": question})
    assert created.status_code == 201, created.text
    ids = created.json()
    stream = await http.get(f"/api/jobs/{ids['job_id']}/events")
    return ids["conversation_id"], ids["job_id"], _frames(stream.text)


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #


async def test_a_question_streams_its_stages_and_ends_with_the_reply(server) -> None:
    signed_in, _, _ = server
    http = await signed_in("a")
    _, job, frames = await _ask(http, "Apple's balances?")

    assert [f[1]["stage"] for f in frames if f[0] == "stage"] == [
        "queued",
        "parsing",
        "mapping",
        "fetching",
        "presenting",
    ]
    kind, done = frames[-1]
    assert kind == "done" and done["reply"]["status"] == "partial"
    parts = {p["text"]: p["outcome"] for p in done["reply"]["parts"]}
    assert parts["goodwill"] == "refused" and parts["assets"] == "answered"

    again = await http.get(f"/api/jobs/{job}")  # a reload
    assert again.json()["status"] == "done" and again.json()["reply"] == done["reply"]


async def test_the_stream_says_it_is_one(server) -> None:
    signed_in, _, _ = server
    http = await signed_in("a")
    created = (await http.post("/api/conversations", json={"question": "Apple's balances?"})).json()
    stream = await http.get(f"/api/jobs/{created['job_id']}/events")
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.headers["cache-control"] == "no-cache"


async def test_a_question_over_the_parser_s_limit_is_refused(server) -> None:
    signed_in, _, _ = server
    http = await signed_in("a")
    response = await http.post("/api/conversations", json={"question": "x" * 2001})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Answering the questions put back
# --------------------------------------------------------------------------- #


async def test_an_ambiguity_is_answered_by_picking_a_candidate(server) -> None:
    signed_in, _, _ = server
    http = await signed_in("a")
    conversation, _, frames = await _ask(http, "Apple's accounts payable?")
    ask = frames[-1][1]["reply"]["parts"][0]["ask"]
    assert ask["kind"] == "ambiguity"  # the client shows the "ambiguous" tag

    answered = await http.post(
        f"/api/conversations/{conversation}/answers",
        json={"answers": [{"kind": "option", "ask_id": ask["ask_id"], "option_id": "o1"}]},
    )
    stream = await http.get(f"/api/jobs/{answered.json()['job_id']}/events")
    frames = _frames(stream.text)
    assert "parsing" not in [f[1].get("stage") for f in frames]  # a pick does not parse
    assert frames[-1][1]["reply"]["answer"]["rows"][0]["display"] == "$68.96B"


@pytest.mark.parametrize(
    ("question", "ask_id", "option_id", "status", "detail"),
    [
        ("Apple's profit margin?", "a1", "o9", 400, "UNKNOWN_CHOICE"),
        ("Apple's profit margin?", "a9", "o1", 400, "UNKNOWN_CHOICE"),
        ("Apple's balances?", "a1", "o1", 409, "NOTHING_TO_ANSWER"),
    ],
)
async def test_an_answer_that_does_not_fit_is_refused(
    server, question, ask_id, option_id, status, detail
) -> None:
    signed_in, _, _ = server
    http = await signed_in("a")
    conversation, _, _ = await _ask(http, question)
    response = await http.post(
        f"/api/conversations/{conversation}/answers",
        json={"answers": [{"kind": "option", "ask_id": ask_id, "option_id": option_id}]},
    )
    assert (response.status_code, response.json()["detail"]) == (status, detail)


async def test_a_round_still_running_cannot_be_answered(server, monkeypatch) -> None:
    signed_in, app, _ = server
    http = await signed_in("a")

    async def never_runs(job_id):  # queued, and left there
        return None

    monkeypatch.setattr(app.state.runner, "submit", never_runs)
    created = (await http.post("/api/conversations", json={"question": "Apple's balances?"})).json()
    response = await http.post(
        f"/api/conversations/{created['conversation_id']}/answers",
        json={"answers": [{"kind": "option", "ask_id": "a1", "option_id": "o1"}]},
    )
    assert (response.status_code, response.json()["detail"]) == (409, "ROUND_STILL_RUNNING")


# --------------------------------------------------------------------------- #
# History and reporting
# --------------------------------------------------------------------------- #


async def test_a_reader_s_history_is_theirs_newest_first(server) -> None:
    signed_in, _, _ = server
    mine, theirs = await signed_in("a"), await signed_in("b")
    first, _, _ = await _ask(mine, "Apple's balances?")
    second, _, _ = await _ask(mine, "Apple's profit margin?")
    await _ask(theirs, "Apple's balances?")
    listed = (await mine.get("/api/conversations")).json()
    assert [c["conversation_id"] for c in listed] == [second, first]
    assert [c["status"] for c in listed] == ["done", "done"]
    assert [c["reply_status"] for c in listed] == ["asked", "partial"]  # "asked": awaits the reader


async def test_a_curated_question_is_answered_and_the_thread_reopens(server) -> None:
    """Round 1 asks which profit margin; round 2 answers it; the conversation
    then reads back as both rounds, the pick in words."""
    signed_in, _, _ = server
    http = await signed_in("a")
    conversation, first_job, frames = await _ask(http, "Apple's profit margin?")
    (part,) = frames[-1][1]["reply"]["parts"]
    ask = part["ask"]
    assert part["outcome"] == "asked" and ask["kind"] == "clarification"
    gross = next(o for o in ask["options"] if o["label"] == "Gross margin")
    pick = {"kind": "option", "ask_id": ask["ask_id"], "option_id": gross["option_id"]}
    path = f"/api/conversations/{conversation}"
    answered = await http.post(f"{path}/answers", json={"answers": [pick]})
    assert answered.status_code == 201
    second_job = answered.json()["job_id"]
    stream = await http.get(f"/api/jobs/{second_job}/events")  # let it finish
    reply = _frames(stream.text)[-1][1]["reply"]
    assert reply["status"] == "answered" and reply["answer"]["rows"][0]["display"] == "46.9%"

    thread = (await http.get(path)).json()
    assert thread["question"] == "Apple's profit margin?"
    first, second = thread["rounds"]
    assert (first["round"], first["job_id"], first["answers"]) == (1, first_job, [])
    assert first["reply"]["parts"][0]["outcome"] == "asked"
    assert second["job_id"] == second_job
    assert [a["text"] for a in second["answers"]] == ["Gross margin"]  # the pick, in words
    assert second["reply"]["answer"]["rows"][0]["display"] == "46.9%"


async def test_a_reopened_round_still_running_says_so(server, monkeypatch) -> None:
    signed_in, app, _ = server
    http = await signed_in("a")

    async def never_runs(job_id):
        return None

    monkeypatch.setattr(app.state.runner, "submit", never_runs)
    created = (await http.post("/api/conversations", json={"question": "Apple's balances?"})).json()
    thread = f"/api/conversations/{created['conversation_id']}"
    (queued,) = (await http.get(thread)).json()["rounds"]
    assert (queued["status"], queued["reply"], queued["message"]) == ("queued", None, None)

    await storage.fail(app.state.web, uuid.UUID(created["job_id"]))
    (failed,) = (await http.get(thread)).json()["rounds"]
    assert failed["status"] == "failed" and failed["message"] == chain.BUG  # as the stream says it


async def test_reporting_a_problem_flags_the_job_for_the_owner(server) -> None:
    signed_in, _, owner = server
    http = await signed_in("a")
    _, job, _ = await _ask(http, "Apple's balances?")
    response = await http.post(f"/api/jobs/{job}/feedback", json={"note": "goodwill is wrong"})
    assert response.status_code == 204
    async with owner() as session:
        note = await session.scalar(
            text("SELECT note FROM web.job_feedback WHERE job_id = :j"), {"j": job}
        )
    assert note == "goodwill is wrong"


# --------------------------------------------------------------------------- #
# Whose it is
# --------------------------------------------------------------------------- #


async def test_another_reader_s_rounds_do_not_exist(server) -> None:
    signed_in, _, _ = server
    mine, theirs = await signed_in("a"), await signed_in("b")
    conversation, job, _ = await _ask(mine, "Apple's profit margin?")
    answer = {"answers": [{"kind": "option", "ask_id": "a1", "option_id": "o1"}]}
    assert (await theirs.get(f"/api/conversations/{conversation}")).status_code == 404
    assert (await theirs.get(f"/api/jobs/{job}")).status_code == 404
    assert (await theirs.get(f"/api/jobs/{job}/events")).status_code == 404
    assert (await theirs.post(f"/api/jobs/{job}/feedback", json={})).status_code == 404
    refused = await theirs.post(f"/api/conversations/{conversation}/answers", json=answer)
    assert refused.status_code == 404


# --------------------------------------------------------------------------- #
# Limits on questions (limits.py)
# --------------------------------------------------------------------------- #


def _held(monkeypatch, app) -> None:
    """Rounds stay queued: the runner never sees them."""

    async def never_runs(job_id):
        return None

    monkeypatch.setattr(app.state.runner, "submit", never_runs)


def _ask_only(http, question: str = "Apple's balances?"):
    return http.post("/api/conversations", json={"question": question})


async def test_a_reader_may_have_two_questions_waiting_not_three(server, monkeypatch) -> None:
    signed_in, app, _ = server
    mine, theirs = await signed_in("a"), await signed_in("b")
    _held(monkeypatch, app)
    for _ in range(limits.UNFINISHED_PER_READER):
        assert (await _ask_only(mine)).status_code == 201
    refused = await _ask_only(mine)
    assert (refused.status_code, refused.json()["detail"]) == (429, limits.TOO_MANY_QUESTIONS)
    assert "Retry-After" not in refused.headers  # it frees when one finishes, not at a time
    assert (await _ask_only(theirs)).status_code == 201  # their own count


async def test_two_asks_at_once_cannot_share_the_last_place(server, monkeypatch) -> None:
    signed_in, app, _ = server
    http = await signed_in("a")
    _held(monkeypatch, app)
    assert (await _ask_only(http)).status_code == 201
    both = await asyncio.gather(_ask_only(http), _ask_only(http))
    assert sorted(r.status_code for r in both) == [201, 429]


async def test_a_busy_server_turns_new_questions_away(server, monkeypatch) -> None:
    signed_in, app, _ = server
    monkeypatch.setattr(limits, "UNFINISHED_ACROSS_SERVER", 2)
    _held(monkeypatch, app)
    for name in ("a", "b"):
        assert (await _ask_only(await signed_in(name))).status_code == 201
    refused = await _ask_only(await signed_in("c"))
    assert (refused.status_code, refused.json()["detail"]) == (503, limits.SERVER_BUSY)
    assert refused.headers["Retry-After"] == str(limits.BUSY_RETRY_AFTER)


async def test_the_daily_cap_counts_finished_questions_and_spares_administrators(
    server, monkeypatch
) -> None:
    signed_in, _, owner = server
    monkeypatch.setattr(limits, "DAILY_PER_READER", 2)
    http = await signed_in("a")
    for _ in range(2):
        await _ask(http, "Apple's balances?")  # run to the end: finished still counts
    refused = await _ask_only(http)
    assert (refused.status_code, refused.json()["detail"]) == (429, limits.DAILY_LIMIT)
    # When the first of the two leaves the last 24 hours: nearly a day from now.
    assert 86_000 < int(refused.headers["Retry-After"]) <= 86_400

    async with owner() as session:
        await session.execute(
            text('UPDATE web."user" SET is_superuser = true WHERE email = :e'),
            {"e": "zz-routes-a@example.com"},
        )
        await session.commit()
    assert (await _ask_only(http)).status_code == 201


async def test_answering_a_question_back_counts_as_a_question(server, monkeypatch) -> None:
    """Each round is a GPU job, whether it asks or answers."""
    signed_in, _, _ = server
    monkeypatch.setattr(limits, "DAILY_PER_READER", 1)
    http = await signed_in("a")
    conversation, _, frames = await _ask(http, "Apple's accounts payable?")
    ask = frames[-1][1]["reply"]["parts"][0]["ask"]
    refused = await http.post(
        f"/api/conversations/{conversation}/answers",
        json={"answers": [{"kind": "option", "ask_id": ask["ask_id"], "option_id": "o1"}]},
    )
    assert (refused.status_code, refused.json()["detail"]) == (429, limits.DAILY_LIMIT)


# --------------------------------------------------------------------------- #
# Anonymous visitors
# --------------------------------------------------------------------------- #

#: Everything a visitor who is not signed in may reach: signing in, and health.
#: A route added without a sign-in check fails the test below until it is either
#: protected or added here on purpose.
OPEN_TO_ANONYMOUS = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/register"),  # needs an invitation (test_auth.py)
    ("GET", "/api/auth/github/authorize"),  # only builds GitHub's URL
    ("GET", "/api/auth/github/callback"),  # a new account needs a GitHub invitation
    ("GET", "/api/health"),  # "ok", for Docker's health check
}


def _mounted(app) -> set[tuple[str, str]]:
    """Every (method, path) the app answers, read from the app itself -- included
    routers flattened, routes hidden from the schema too."""
    return {
        (method, context.path)
        for context in routing.iter_route_contexts(app.routes)
        for method in getattr(context, "methods", None) or ()
    }


async def test_an_anonymous_visitor_reaches_nothing_but_sign_in(server) -> None:
    """Built from the mounted routes, not a list kept by hand, so it covers routes
    nobody remembered to add to it. No cookie and a forged one alike get 401."""
    _, app, _ = server
    mounted = _mounted(app)
    assert OPEN_TO_ANONYMOUS <= mounted, "an open route no longer exists: drop it from the list"
    protected = sorted(mounted - OPEN_TO_ANONYMOUS)
    assert ("POST", "/api/conversations") in protected  # the sweep found the routers

    zero = "00000000-0000-0000-0000-000000000000"
    refused = []
    for cookies in ({}, {SESSION_COOKIE: "forged"}):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies
        ) as anon:
            for method, path in protected:
                url = re.sub(r"\{[^}]+\}", zero, path)
                response = await anon.request(method, url, json={})
                if response.status_code != 401:
                    refused.append((method, path, bool(cookies), response.status_code))
    assert not refused, f"reachable without signing in: {refused}"
