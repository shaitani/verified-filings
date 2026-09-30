"""Sign-in (app/api/auth.py, server.py, admin.py): the real app, as the web role,
on the test database. GitHub itself is not called: its callback is exercised
through the user manager, and its /authorize only builds a URL."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from fastapi_users import exceptions as user_errors
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy import select, text

from app.api import admin
from app.api.auth import (
    INVITE_REQUIRED,
    SESSION_COOKIE,
    AuthConfig,
    UserRead,
    hash_invite_code,
    purge_expired_sessions,
    user_manager_class,
)
from app.api.server import create_app
from app.db import roles
from app.db.web import Invite, OAuthAccount, User
from tests.conftest import TEST_WEB_PASSWORD

CONFIG = AuthConfig(
    reset_secret="test-reset-" + "r" * 32,
    verify_secret="test-verify-" + "v" * 32,
    oauth_state_secret="test-state-" + "s" * 32,
    session_days=30,
    cookie_secure=False,  # the test client speaks http; browsers treat localhost as secure
    github_client_id="test-client",
    github_client_secret="test-secret",
    github_redirect_url="http://test/api/auth/github/callback",
)
PASSWORD = "correct horse battery"


@pytest.fixture
async def client(web_factory, test_db_url):
    """The app, started and stopped as uvicorn would, and an http client for it."""
    scheme, rest = test_db_url.split("://", 1)
    web_url = f"{scheme}://{roles.WEB.name}:{TEST_WEB_PASSWORD}@{rest.split('@', 1)[1]}"
    app = create_app(CONFIG, web_url)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _invite(owner, email: str, *, days: int = 14) -> str:
    return await admin.create_email_invite(owner, email, days)


def _register(http, email: str, code: str, password: str = PASSWORD, **extra):
    return http.post(
        "/api/auth/register",
        json={"email": email, "password": password, "invite_code": code, **extra},
    )


async def _invite_row(owner, email: str) -> Invite:
    async with owner() as session:
        return await session.scalar(select(Invite).where(Invite.email == email))


# --------------------------------------------------------------------------- #
# Registration is by invitation
# --------------------------------------------------------------------------- #


async def test_an_invited_address_registers_and_spends_the_invite(client, web_factory) -> None:
    _, owner = web_factory
    code = await _invite(owner, "zz-auth-a@example.com")
    response = await _register(client, "zz-auth-a@example.com", code)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (
        body["is_superuser"] is False and body["is_verified"] is False
    )  # unverified: no email yet
    invite = await _invite_row(owner, "zz-auth-a@example.com")
    assert invite.used_at is not None and str(invite.used_by) == body["id"]


@pytest.mark.parametrize("case", ["no such code", "another address", "expired", "spent"])
async def test_every_other_way_in_is_refused_alike(client, web_factory, case) -> None:
    """One error for all: it tells a guesser nothing about why."""
    _, owner = web_factory
    email, code = "zz-auth-b@example.com", None
    if case == "no such code":
        code = "AAAA-BBBB-CCCC"
    elif case == "another address":
        code = await _invite(owner, "zz-auth-someone-else@example.com")
    elif case == "expired":
        code = await _invite(owner, email, days=-1)
    else:
        code = await _invite(owner, email)
        assert (await _register(client, email, code)).status_code == 201
        email = "zz-auth-c@example.com"  # the same code, again
    response = await _register(client, email, code)
    assert (response.status_code, response.json()["detail"]) == (400, INVITE_REQUIRED)


async def test_a_code_is_forgiving_about_case_and_dashes(client, web_factory) -> None:
    _, owner = web_factory
    code = await _invite(owner, "zz-auth-d@example.com")
    typed = code.replace("-", " ").lower()
    assert (await _register(client, "zz-auth-d@example.com", typed)).status_code == 201


async def test_registration_cannot_make_an_administrator(client, web_factory) -> None:
    _, owner = web_factory
    code = await _invite(owner, "zz-auth-e@example.com")
    response = await _register(
        client, "zz-auth-e@example.com", code, is_superuser=True, is_verified=True
    )
    assert response.status_code == 201
    assert (response.json()["is_superuser"], response.json()["is_verified"]) == (False, False)


async def test_a_refused_account_does_not_spend_the_invite(client, web_factory) -> None:
    _, owner = web_factory
    code = await _invite(owner, "zz-auth-f@example.com")
    short = await _register(client, "zz-auth-f@example.com", code, password="short")
    assert (
        short.status_code == 400 and short.json()["detail"]["code"] == "REGISTER_INVALID_PASSWORD"
    )
    assert (await _invite_row(owner, "zz-auth-f@example.com")).used_at is None  # released
    assert (await _register(client, "zz-auth-f@example.com", code)).status_code == 201


async def test_two_sign_ups_racing_for_one_code_make_one_account(client, web_factory) -> None:
    _, owner = web_factory
    code = await _invite(owner, "zz-auth-g@example.com")
    first, second = await asyncio.gather(
        _register(client, "zz-auth-g@example.com", code),
        _register(client, "zz-auth-g@example.com", code),
    )
    assert sorted([first.status_code, second.status_code]) == [201, 400]


# --------------------------------------------------------------------------- #
# Signing in and out
# --------------------------------------------------------------------------- #


async def _registered(client, owner, email: str) -> None:
    code = await _invite(owner, email)
    assert (await _register(client, email, code)).status_code == 201


def _login(http, email: str, password: str = PASSWORD):
    return http.post("/api/auth/login", data={"username": email, "password": password})


async def test_a_session_is_an_http_only_cookie_for_thirty_days(client, web_factory) -> None:
    _, owner = web_factory
    await _registered(client, owner, "zz-auth-h@example.com")
    response = await _login(client, "zz-auth-h@example.com")
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=2592000" in cookie  # 30 days (app/api/DESIGN.md §10)

    me = await client.get("/api/me")
    assert me.status_code == 200 and me.json()["email"] == "zz-auth-h@example.com"


async def test_signing_out_ends_the_session_in_the_database(client, web_factory) -> None:
    _, owner = web_factory
    await _registered(client, owner, "zz-auth-i@example.com")
    await _login(client, "zz-auth-i@example.com")
    token = client.cookies[SESSION_COOKIE]
    assert (await client.post("/api/auth/logout")).status_code == 204
    async with owner() as session:
        left = await session.scalar(
            text("SELECT count(*) FROM web.access_token WHERE token = :t"), {"t": token}
        )
    assert left == 0
    client.cookies.set(SESSION_COOKIE, token)  # replaying the old cookie gets nothing
    assert (await client.get("/api/me")).status_code == 401


async def test_a_wrong_password_is_refused(client, web_factory) -> None:
    _, owner = web_factory
    await _registered(client, owner, "zz-auth-j@example.com")
    response = await _login(client, "zz-auth-j@example.com", "not the password")
    assert (response.status_code, response.json()["detail"]) == (400, "LOGIN_BAD_CREDENTIALS")
    assert (await client.get("/api/me")).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/request-verify-token",
        "/api/auth/verify",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
        "/api/users/me",
    ],
)
async def test_what_waits_for_an_email_sender_is_not_mounted(client, path) -> None:
    assert (await client.post(path, json={})).status_code == 404


async def test_expired_sessions_are_purged(web_factory) -> None:
    web, owner = web_factory
    user = uuid.uuid4()
    async with owner() as session:
        await session.execute(
            text(
                'INSERT INTO web."user" (id, email, hashed_password, is_active, is_verified) '
                "VALUES (:u, 'zz-auth-k@example.com', 'x', true, false)"
            ),
            {"u": user},
        )
        for token, age in (("old", 31), ("new", 1)):
            await session.execute(
                text(
                    "INSERT INTO web.access_token (token, user_id, created_at) VALUES (:t, :u, :at)"
                ),
                {"t": f"zz-{token}", "u": user, "at": datetime.now(UTC) - timedelta(days=age)},
            )
        await session.commit()
    assert await purge_expired_sessions(web, CONFIG.lifetime) == 1
    async with owner() as session:
        left = (
            (
                await session.execute(
                    text("SELECT token FROM web.access_token WHERE user_id = :u"), {"u": user}
                )
            )
            .scalars()
            .all()
        )
    assert left == ["zz-new"]


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #


async def test_github_is_asked_for_our_scopes_whatever_the_caller_asks(client) -> None:
    response = await client.get(
        "/api/auth/github/authorize", params={"scopes": ["repo", "admin:org"]}
    )
    url = response.json()["authorization_url"]
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == ["read:user user:email"]
    assert query["redirect_uri"] == [CONFIG.github_redirect_url]


async def _github(web, account_id: str, email: str):
    manager = user_manager_class(CONFIG.reset_secret, CONFIG.verify_secret)
    async with web() as session:
        return await manager(SQLAlchemyUserDatabase(session, User, OAuthAccount)).oauth_callback(
            "github",
            "gho_token",
            account_id,
            email,
            # What a careless router might pass; the manager forces both off.
            associate_by_email=True,
            is_verified_by_default=True,
        )


async def test_a_new_github_account_needs_a_github_invite(web_factory) -> None:
    web, owner = web_factory
    with pytest.raises(HTTPException) as refused:
        await _github(web, "zz-gh-1", "zz-auth-gh1@example.com")
    assert refused.value.detail == INVITE_REQUIRED

    await admin.create_github_invite(owner, "zz-gh-1")
    user = await _github(web, "zz-gh-1", "zz-auth-gh1@example.com")
    assert user.is_verified is False  # GitHub's email is not proof of the address
    async with owner() as session:
        invite = await session.scalar(select(Invite).where(Invite.github_account_id == "zz-gh-1"))
    assert invite.used_by == user.id

    again = await _github(web, "zz-gh-1", "zz-auth-gh1@example.com")  # a returning account
    assert again.id == user.id


async def test_a_github_email_never_takes_over_an_existing_account(client, web_factory) -> None:
    web, owner = web_factory
    await _registered(client, owner, "zz-auth-victim@example.com")
    await admin.create_github_invite(owner, "zz-gh-2")
    with pytest.raises(user_errors.UserAlreadyExists):
        await _github(web, "zz-gh-2", "zz-auth-victim@example.com")
    async with owner() as session:
        invite = await session.scalar(select(Invite).where(Invite.github_account_id == "zz-gh-2"))
    assert invite.used_at is None  # not spent on an account that was not made


# --------------------------------------------------------------------------- #
# Administration (the owner's CLI)
# --------------------------------------------------------------------------- #


async def test_an_invite_code_is_shown_once_and_stored_hashed(web_factory) -> None:
    _, owner = web_factory
    code = await _invite(owner, "ZZ-auth-L@Example.com")
    assert re.fullmatch(r"[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}", code)
    row = await _invite_row(owner, "zz-auth-l@example.com")  # stored lower-cased
    assert row.code_hash == hash_invite_code(code) and code not in row.code_hash
    assert row.expires_at > datetime.now(UTC) + timedelta(days=13)


async def test_the_owner_makes_an_administrator_and_resets_a_password(client, web_factory) -> None:
    _, owner = web_factory
    await _registered(client, owner, "zz-auth-m@example.com")
    assert await admin.make_admin(owner, "zz-auth-m@example.com")
    new_password = await admin.reset_password(owner, "zz-auth-m@example.com")
    assert (await _login(client, "zz-auth-m@example.com")).status_code == 400  # the old one
    assert (await _login(client, "zz-auth-m@example.com", new_password)).status_code == 204
    assert (await client.get("/api/me")).json()["is_superuser"] is True
    assert await admin.reset_password(owner, "zz-nobody@example.com") is None


async def test_a_github_username_is_looked_up_to_its_numeric_id(monkeypatch) -> None:
    def fake_github(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/octocat":
            return httpx.Response(200, json={"login": "octocat", "id": 583231})
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        admin.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(fake_github), **kw),
    )
    assert await admin.github_account_id("octocat") == "583231"
    with pytest.raises(LookupError):
        await admin.github_account_id("nobody-at-all")


def test_the_server_will_not_start_without_its_secrets() -> None:
    class Incomplete:
        auth_reset_secret, auth_verify_secret, auth_oauth_state_secret = "a", None, ""

    with pytest.raises(RuntimeError, match="AUTH_VERIFY_SECRET, AUTH_OAUTH_STATE_SECRET"):
        AuthConfig.from_settings(Incomplete)

    class Weak:
        auth_reset_secret = auth_verify_secret = "x" * 43
        auth_oauth_state_secret = "too-short"

    with pytest.raises(RuntimeError, match="AUTH_OAUTH_STATE_SECRET shorter than 32"):
        AuthConfig.from_settings(Weak)


async def test_me_names_the_providers_an_account_signs_in_with(client, web_factory) -> None:
    web, owner = web_factory
    await _registered(client, owner, "zz-auth-pw@example.com")
    await _login(client, "zz-auth-pw@example.com")
    assert (await client.get("/api/me")).json()["sign_in_providers"] == []

    await admin.create_github_invite(owner, "zz-gh-9")
    await _github(web, "zz-gh-9", "zz-auth-gh9@example.com")
    async with web() as session:
        row = await SQLAlchemyUserDatabase(session, User, OAuthAccount).get_by_email(
            "zz-auth-gh9@example.com"
        )
        shown = UserRead.model_validate(row).model_dump()
    assert shown["sign_in_providers"] == ["github"]
    assert "oauth_accounts" not in shown  # no account id or token reaches the browser
