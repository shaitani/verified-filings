"""The admin routes (app/api/admin_routes.py) over HTTP: the real app, real
sign-in, as vf_admin_role on the test database. The chain is stood in for by the
captures test_routes.py uses, so a reader's question leaves a real job and trace."""

from __future__ import annotations

import asyncio
import re
import uuid

import httpx
import pytest
from fastapi import routing
from sqlalchemy import select, text

from app.api import admin
from app.api.server import create_app
from app.db.web import AdminAction, User
from tests.test_auth import CONFIG, PASSWORD, _admin_url, _web_url
from tests.test_routes import _ask, captured_chain  # noqa: F401 -- captured_chain is autouse

ZERO = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def site(web_factory, test_db_url):
    """The app started as uvicorn would; yields ``(signed_in, app, owner)``, where
    ``signed_in(name, admin=False)`` registers ``zz-admin-<name>@example.com`` with an
    invite, signs in, and -- with ``admin`` -- is made an administrator by the owner."""
    _, owner = web_factory
    app = create_app(CONFIG, _web_url(test_db_url), _admin_url(test_db_url), showDocs=False)
    clients: list[httpx.AsyncClient] = []

    async def signed_in(name: str, *, as_admin: bool = False) -> httpx.AsyncClient:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(http)
        email = _email(name)
        code = await admin.create_invite(owner)
        registered = await http.post(
            "/api/auth/register", json={"email": email, "password": PASSWORD, "invite_code": code}
        )
        assert registered.status_code == 201, registered.text
        if as_admin:
            assert await admin.make_admin(owner, email)
        assert (await _login(http, email)).status_code == 204
        return http

    async with app.router.lifespan_context(app):
        try:
            yield signed_in, app, owner
        finally:
            for http in clients:
                await http.aclose()


def _email(name: str) -> str:
    return f"zz-admin-{name}@example.com"


def _login(http, email: str, password: str = PASSWORD):
    return http.post("/api/auth/login", data={"username": email, "password": password})


async def _user_id(boss: httpx.AsyncClient, name: str) -> str:
    listed = (await boss.get("/api/admin/users")).json()
    return next(u["id"] for u in listed if u["email"] == _email(name))


async def _audit(owner, action: str) -> list[AdminAction]:
    async with owner() as session:
        rows = await session.scalars(select(AdminAction).where(AdminAction.action == action))
        return [r for r in rows if (r.admin_email or "").startswith("zz-admin-")]


@pytest.fixture(autouse=True)
async def _clean_audit(web_factory):
    """The audit log outlives users by design, so a test's rows are removed by hand."""
    _, owner = web_factory

    async def clean() -> None:
        async with owner() as session:
            await session.execute(
                text("DELETE FROM web.admin_action WHERE admin_email LIKE 'zz-admin-%'")
            )
            await session.commit()

    await clean()
    yield
    await clean()


# --------------------------------------------------------------------------- #
# Only for administrators
# --------------------------------------------------------------------------- #


def _admin_routes(app) -> list[tuple[str, str]]:
    return sorted(
        (method, context.path)
        for context in routing.iter_route_contexts(app.routes)
        if context.path.startswith("/api/admin")
        for method in getattr(context, "methods", None) or ()
    )


async def test_to_a_reader_every_admin_route_does_not_exist(site) -> None:
    """404, with FastAPI's own body for a missing route: nothing to tell apart.
    Built from the mounted routes, so a route added later is covered."""
    signed_in, app, _ = site
    reader = await signed_in("reader")
    routes = _admin_routes(app)
    assert len(routes) >= 13
    seen = []
    for method, path in routes:
        response = await reader.request(method, re.sub(r"\{[^}]+\}", ZERO, path), json={})
        if (response.status_code, response.json()) != (404, {"detail": "Not Found"}):
            seen.append((method, path, response.status_code))
    assert not seen, f"an ordinary reader reached: {seen}"
    missing = await reader.get("/api/admin/no-such-route")
    assert missing.json() == {"detail": "Not Found"}  # the same body, for comparison


async def test_unmaking_an_admin_takes_effect_on_their_next_request(site) -> None:
    signed_in, _, owner = site
    boss = await signed_in("boss", as_admin=True)
    assert (await boss.get("/api/admin/users")).status_code == 200
    assert await admin.unmake_admin(owner, _email("boss"))
    assert (await boss.get("/api/admin/users")).status_code == 404
    assert (await boss.get("/api/me")).status_code == 200  # the account itself is untouched


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


async def test_the_users_list_says_what_an_admin_needs(site) -> None:
    signed_in, _, _ = site
    boss = await signed_in("boss", as_admin=True)
    reader = await signed_in("reader")
    await _ask(reader, "Apple's balances?")
    listed = {u["email"]: u for u in (await boss.get("/api/admin/users")).json()}
    mine, theirs = listed[_email("boss")], listed[_email("reader")]
    assert mine["is_superuser"] is True and theirs["is_superuser"] is False
    assert theirs["sessions"] == 1 and theirs["questions_today"] == 1
    assert theirs["sign_in_providers"] == [] and theirs["is_active"] is True


async def test_a_deactivated_reader_is_out_at_once_until_reactivated(site) -> None:
    signed_in, _, owner = site
    boss = await signed_in("boss", as_admin=True)
    reader = await signed_in("reader")
    target = await _user_id(boss, "reader")

    assert (await boss.post(f"/api/admin/users/{target}/deactivate")).status_code == 204
    assert (await reader.get("/api/conversations")).status_code == 401  # session ended
    assert (await _login(reader, _email("reader"))).status_code == 400  # and cannot sign in

    assert (await boss.post(f"/api/admin/users/{target}/reactivate")).status_code == 204
    assert (await _login(reader, _email("reader"))).status_code == 204
    (row,) = await _audit(owner, "deactivate")
    assert (row.admin_email, row.target_email, str(row.target_id)) == (
        _email("boss"),
        _email("reader"),
        target,
    )
    assert len(await _audit(owner, "reactivate")) == 1


async def test_ending_sessions_signs_a_reader_out_but_not_for_good(site) -> None:
    signed_in, _, owner = site
    boss = await signed_in("boss", as_admin=True)
    reader = await signed_in("reader")
    target = await _user_id(boss, "reader")
    assert (await boss.post(f"/api/admin/users/{target}/end-sessions")).status_code == 204
    assert (await reader.get("/api/conversations")).status_code == 401
    assert (await _login(reader, _email("reader"))).status_code == 204
    (row,) = await _audit(owner, "end_sessions")
    assert row.detail == {"ended": 1}


async def test_a_reset_password_is_shown_once_and_ends_their_sessions(site) -> None:
    signed_in, _, _ = site
    boss = await signed_in("boss", as_admin=True)
    reader = await signed_in("reader")
    target = await _user_id(boss, "reader")
    reset = await boss.post(f"/api/admin/users/{target}/reset-password")
    assert reset.status_code == 200
    password = reset.json()["password"]
    assert len(password) >= 12
    assert (await reader.get("/api/conversations")).status_code == 401
    assert (await _login(reader, _email("reader"))).status_code == 400  # the old one
    assert (await _login(reader, _email("reader"), password)).status_code == 204


async def test_deleting_a_reader_takes_everything_and_the_record_stays(site) -> None:
    signed_in, _, owner = site
    boss = await signed_in("boss", as_admin=True)
    reader = await signed_in("reader")
    _, job, _ = await _ask(reader, "Apple's balances?")
    target = await _user_id(boss, "reader")

    assert (await boss.delete(f"/api/admin/users/{target}")).status_code == 204
    assert (await reader.get("/api/conversations")).status_code == 401
    async with owner() as session:
        assert await session.get(User, uuid.UUID(target)) is None
        left = await session.execute(
            text("SELECT count(*) FROM web.job_trace WHERE job_id = :j"), {"j": job}
        )
        assert left.scalar_one() == 0
    (row,) = await _audit(owner, "delete_user")
    assert row.target_email == _email("reader")  # outlives the account


@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("POST", "deactivate"),
        ("POST", "reactivate"),
        ("POST", "end-sessions"),
        ("POST", "reset-password"),
        ("DELETE", ""),
    ],
)
async def test_an_admin_cannot_act_on_an_admin(site, method: str, action: str) -> None:
    """Not another, not themselves: only the owner's CLI touches an administrator."""
    signed_in, _, owner = site
    boss = await signed_in("boss", as_admin=True)
    await signed_in("other", as_admin=True)
    for name in ("other", "boss"):
        target = await _user_id(boss, name)
        path = f"/api/admin/users/{target}" + (f"/{action}" if action else "")
        refused = await boss.request(method, path)
        assert (refused.status_code, refused.json()["detail"]) == (409, "ADMIN_PROTECTED"), name
    async with owner() as session:
        others = await session.scalar(select(User).where(User.email == _email("other")))
        assert others.is_active and others.is_superuser
    assert await _audit(owner, action.replace("-", "_") or "delete_user") == []


async def test_a_user_who_does_not_exist_is_not_found(site) -> None:
    signed_in, _, _ = site
    boss = await signed_in("boss", as_admin=True)
    refused = await boss.post(f"/api/admin/users/{ZERO}/deactivate")
    assert (refused.status_code, refused.json()["detail"]) == (404, "NOT_FOUND")


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #


async def test_an_admin_s_invite_registers_one_account_and_shows_who_used_it(site) -> None:
    signed_in, app, owner = site
    boss = await signed_in("boss", as_admin=True)
    created = await boss.post("/api/admin/invites", json={"days": 3})
    assert created.status_code == 201
    code, invite = created.json()["code"], created.json()["invite"]
    assert invite["status"] == "open" and invite["created_by_email"] == _email("boss")

    newcomer = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with newcomer:
        registered = await newcomer.post(
            "/api/auth/register",
            json={"email": _email("newcomer"), "password": PASSWORD, "invite_code": code},
        )
    assert registered.status_code == 201
    (listed,) = [
        i for i in (await boss.get("/api/admin/invites")).json() if i["id"] == invite["id"]
    ]
    assert (listed["status"], listed["used_by_email"]) == ("used", _email("newcomer"))
    (row,) = await _audit(owner, "create_invite")
    assert row.detail == {"days": 3} and str(row.target_id) == invite["id"]


async def test_a_revoked_invite_no_longer_registers(site) -> None:
    signed_in, app, owner = site
    boss = await signed_in("boss", as_admin=True)
    created = (await boss.post("/api/admin/invites", json={})).json()
    invite_id = created["invite"]["id"]
    assert (await boss.post(f"/api/admin/invites/{invite_id}/revoke")).status_code == 204
    again = await boss.post(f"/api/admin/invites/{invite_id}/revoke")
    assert (again.status_code, again.json()["detail"]) == (409, "INVITE_NOT_OPEN")
    unknown = await boss.post(f"/api/admin/invites/{ZERO}/revoke")
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "NOT_FOUND")

    newcomer = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with newcomer:
        refused = await newcomer.post(
            "/api/auth/register",
            json={"email": _email("late"), "password": PASSWORD, "invite_code": created["code"]},
        )
    assert refused.json()["detail"] == "INVITE_REQUIRED"
    (listed,) = [i for i in (await boss.get("/api/admin/invites")).json() if i["id"] == invite_id]
    assert listed["status"] == "revoked"
    assert len(await _audit(owner, "revoke_invite")) == 1


@pytest.mark.parametrize("days", [0, 91])
async def test_an_invite_lasts_between_a_day_and_ninety(site, days: int) -> None:
    signed_in, _, _ = site
    boss = await signed_in("boss", as_admin=True)
    assert (await boss.post("/api/admin/invites", json={"days": days})).status_code == 422


# --------------------------------------------------------------------------- #
# Rounds, traces, reports, and the audit log
# --------------------------------------------------------------------------- #


async def test_an_admin_reads_a_round_s_trace_and_the_reading_is_logged(site) -> None:
    signed_in, _, owner = site
    boss = await signed_in("boss", as_admin=True)
    reader = await signed_in("reader")
    _, job, _ = await _ask(reader, "Apple's balances?")
    reported = await reader.post(f"/api/jobs/{job}/feedback", json={"note": "goodwill?"})
    assert reported.status_code == 204

    (listed,) = [j for j in (await boss.get("/api/admin/jobs")).json() if j["job_id"] == job]
    assert listed["user_email"] == _email("reader") and listed["question"] == "Apple's balances?"
    assert listed["status"] == "done" and listed["has_trace"] and listed["reports"] == 1
    flagged = (await boss.get("/api/admin/jobs", params={"reported": "true"})).json()
    assert [j["job_id"] for j in flagged] == [job]

    opened = await boss.get(f"/api/admin/jobs/{job}/trace")
    assert opened.status_code == 200
    body = opened.json()
    assert body["job"]["job_id"] == job and body["code_version"]
    assert isinstance(body["model_calls"], list) and isinstance(body["errors"], list)
    assert [r["note"] for r in body["reports"]] == ["goodwill?"]
    (row,) = await _audit(owner, "read_trace")
    assert (str(row.target_id), row.target_email) == (job, _email("reader"))

    (report,) = [r for r in (await boss.get("/api/admin/reports")).json() if r["job_id"] == job]
    assert report["note"] == "goodwill?" and report["user_email"] == _email("reader")


async def test_a_round_that_does_not_exist_has_no_trace(site) -> None:
    signed_in, _, _ = site
    boss = await signed_in("boss", as_admin=True)
    refused = await boss.get(f"/api/admin/jobs/{ZERO}/trace")
    assert (refused.status_code, refused.json()["detail"]) == (404, "NOT_FOUND")


async def test_the_audit_log_reads_newest_first(site) -> None:
    signed_in, _, _ = site
    boss = await signed_in("boss", as_admin=True)
    await boss.post("/api/admin/invites", json={})
    reader = await signed_in("reader")
    await boss.post(f"/api/admin/users/{await _user_id(boss, 'reader')}/end-sessions")
    assert reader is not None
    logged = [a["action"] for a in (await boss.get("/api/admin/actions")).json()]
    assert logged[:2] == ["end_sessions", "create_invite"]


async def test_a_burst_of_admin_requests_waits_for_connections_rather_than_failing(site) -> None:
    """The admin page loads five lists at once, and vf_admin_role may hold only
    four connections: the pool must queue the rest, not ask for a fifth (which
    the database refuses -- a 500 on the page)."""
    signed_in, _, _ = site
    boss = await signed_in("boss", as_admin=True)
    paths = ["/api/admin/users", "/api/admin/invites", "/api/admin/jobs", "/api/admin/reports"]
    answers = await asyncio.gather(*(boss.get(path) for path in paths * 3))
    assert [a.status_code for a in answers] == [200] * len(answers)
