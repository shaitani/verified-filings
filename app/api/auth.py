"""Sign-in: FastAPI Users, invitations, and the guard rails of DESIGN §10.

What the library does is used as it is; what it leaves to us is here:

* **invitations** on both ways an account is made -- registration and a first
  GitHub sign-in -- claimed with one atomic UPDATE, so two sign-ups cannot
  spend one invite, and released if the account then fails to be made;
* **GitHub's scopes narrowed** to ``read:user`` and ``user:email`` whatever the
  caller asks for, and its two convenience flags forced off;
* **sessions** as ``web.access_token`` rows behind an httpOnly cookie, and a
  cleanup of expired ones, which the library leaves to us.
"""

# No `from __future__ import annotations`: FastAPI resolves a dependency's
# annotations at runtime, and these dependencies are closures it could not see.

import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi_users import (
    BaseUserManager,
    FastAPIUsers,
    InvalidPasswordException,
    UUIDIDMixin,
    exceptions as user_errors,
    schemas as user_schemas,
)
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyAccessTokenDatabase
from httpx_oauth.clients.github import GitHubOAuth2
from pydantic import Field
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.web import AccessToken, OAuthAccount, User

SESSION_COOKIE = "vf_session"

#: What GitHub is asked for -- identity and a verified-or-not email, nothing else.
GITHUB_SCOPES = ["read:user", "user:email"]

#: Invite codes: 12 characters in three groups, no look-alikes (0/O, 1/I/L).
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

MIN_PASSWORD = 12

#: A signing secret shorter than this is refused at startup: HMAC-SHA256 wants a
#: key of at least 32 bytes (RFC 7518 §3.2); `secrets.token_urlsafe(32)` gives 43.
MIN_SECRET = 32

#: The error a reader gets for a missing, wrong, spent or expired invitation --
#: one code for all, so it tells a guesser nothing about which.
INVITE_REQUIRED = "INVITE_REQUIRED"


# --------------------------------------------------------------------------- #
# What the browser sees of a user
# --------------------------------------------------------------------------- #


class UserRead(user_schemas.BaseUser[uuid.UUID]):
    pass


class UserCreate(user_schemas.BaseUserCreate):
    invite_code: str = Field(min_length=12, max_length=32)  # "K7QM-3XRD-9TPW"

    # The library copies every field onto the user row; the code is not a column.
    def create_update_dict(self):
        return {k: v for k, v in super().create_update_dict().items() if k != "invite_code"}

    def create_update_dict_superuser(self):
        return {
            k: v for k, v in super().create_update_dict_superuser().items() if k != "invite_code"
        }


# --------------------------------------------------------------------------- #
# Invitations
# --------------------------------------------------------------------------- #


def new_invite_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def hash_invite_code(code: str) -> str:
    """SHA-256 of the code as typed, forgivingly: case, dashes and spaces aside.
    A plain hash suffices -- the code is random, not a password a person chose --
    and makes the lookup one indexed comparison."""
    normalized = "".join(ch for ch in code.upper() if ch.isalnum())
    return hashlib.sha256(normalized.encode()).hexdigest()


async def _claim(session: AsyncSession, where: str, params: dict) -> uuid.UUID:
    """Mark one unused, unexpired invite used, atomically: of two claims racing
    for it, exactly one gets a row back."""
    claimed = await session.execute(
        text(
            "UPDATE invite SET used_at = now() "
            f"WHERE used_at IS NULL AND (expires_at IS NULL OR expires_at > now()) AND {where} "
            "RETURNING id"
        ),
        params,
    )
    invite_id = claimed.scalar_one_or_none()
    await session.commit()  # visible to the next claimer at once
    if invite_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=INVITE_REQUIRED)
    return invite_id


async def _spend(session: AsyncSession, invite_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await session.execute(
        text("UPDATE invite SET used_by = :user WHERE id = :invite"),
        {"user": user_id, "invite": invite_id},
    )
    await session.commit()


async def _release(session: AsyncSession, invite_id: uuid.UUID) -> None:
    """The account was not made, so the invite was not spent."""
    await session.rollback()
    await session.execute(
        text("UPDATE invite SET used_at = NULL WHERE id = :invite AND used_by IS NULL"),
        {"invite": invite_id},
    )
    await session.commit()


# --------------------------------------------------------------------------- #
# The user manager
# --------------------------------------------------------------------------- #


def user_manager_class(reset_secret: str, verify_secret: str) -> type[BaseUserManager]:
    class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
        reset_password_token_secret = reset_secret
        verification_token_secret = verify_secret

        @property
        def _session(self) -> AsyncSession:
            return self.user_db.session

        async def validate_password(self, password: str, user) -> None:
            if len(password) < MIN_PASSWORD:
                raise InvalidPasswordException(
                    reason=f"A password needs at least {MIN_PASSWORD} characters."
                )

        async def create(self, user_create, safe: bool = False, request: Request | None = None):
            """Registration: the invitation first. Bound to one address, so a
            code handed to one person does not open an account for another."""
            invite = await _claim(
                self._session,
                "kind = 'email' AND lower(email) = lower(:email) AND code_hash = :code",
                {"email": user_create.email, "code": hash_invite_code(user_create.invite_code)},
            )
            try:
                user = await super().create(user_create, safe=True, request=request)
            except Exception:
                await _release(self._session, invite)
                raise
            await _spend(self._session, invite, user.id)
            return user

        async def oauth_callback(
            self,
            oauth_name: str,
            access_token: str,
            account_id: str,
            account_email: str,
            expires_at: int | None = None,
            refresh_token: str | None = None,
            request: Request | None = None,
            *,
            associate_by_email: bool = False,
            is_verified_by_default: bool = False,
        ):
            """A GitHub sign-in. A returning account needs nothing; a new one
            needs a GitHub invite for its numeric id -- never its email, which
            GitHub does not guarantee is verified (DESIGN §10)."""
            try:
                await self.get_by_oauth_account(oauth_name, account_id)
                invite = None
            except user_errors.UserNotExists:
                invite = await _claim(
                    self._session,
                    "kind = 'github' AND github_account_id = :account",
                    {"account": str(account_id)},
                )
            try:
                user = await super().oauth_callback(
                    oauth_name, access_token, account_id, account_email, expires_at,
                    refresh_token, request,
                    # Forced off whatever the router was given: an unverified GitHub
                    # email must neither verify an account nor take one over.
                    associate_by_email=False,
                    is_verified_by_default=False,
                )
            except Exception:
                if invite is not None:
                    await _release(self._session, invite)
                raise
            if invite is not None:
                await _spend(self._session, invite, user.id)
            return user

    return UserManager


class NarrowGitHub(GitHubOAuth2):
    """GitHub, asked for our scopes only. The library's /authorize passes on any
    ``scopes`` the caller sends, and GitHub would grant them."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        super().__init__(client_id, client_secret, scopes=GITHUB_SCOPES)

    async def get_authorization_url(self, redirect_uri, state=None, scope=None, **kwargs):
        return await super().get_authorization_url(redirect_uri, state, GITHUB_SCOPES, **kwargs)


# --------------------------------------------------------------------------- #
# Wiring it together
# --------------------------------------------------------------------------- #


@dataclass
class AuthConfig:
    reset_secret: str
    verify_secret: str
    oauth_state_secret: str
    session_days: int = 30
    cookie_secure: bool = True
    github_client_id: str | None = None
    github_client_secret: str | None = None
    github_redirect_url: str | None = None

    @classmethod
    def from_settings(cls, settings) -> "AuthConfig":
        missing = [
            name
            for name in ("auth_reset_secret", "auth_verify_secret", "auth_oauth_state_secret")
            if not getattr(settings, name)
        ]
        if missing:
            raise RuntimeError(
                f"{', '.join(n.upper() for n in missing)} not set: sign-in needs all three "
                "(BOOTSTRAP.md)."
            )
        short = [
            name.upper()
            for name in ("auth_reset_secret", "auth_verify_secret", "auth_oauth_state_secret")
            if len(getattr(settings, name)) < MIN_SECRET
        ]
        if short:
            raise RuntimeError(f"{', '.join(short)} shorter than {MIN_SECRET} characters")
        return cls(
            reset_secret=settings.auth_reset_secret,
            verify_secret=settings.auth_verify_secret,
            oauth_state_secret=settings.auth_oauth_state_secret,
            session_days=settings.auth_session_days,
            cookie_secure=settings.auth_cookie_secure,
            github_client_id=settings.github_oauth_client_id,
            github_client_secret=settings.github_oauth_client_secret,
            github_redirect_url=settings.github_oauth_redirect_url,
        )

    @property
    def lifetime(self) -> int:
        return self.session_days * 24 * 3600


@dataclass
class Auth:
    """Everything the app mounts, and the dependency that says who is asking."""

    users: FastAPIUsers
    backend: AuthenticationBackend
    github: NarrowGitHub | None
    current_user: object


def build_auth(config: AuthConfig) -> Auth:
    manager_class = user_manager_class(config.reset_secret, config.verify_secret)

    async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
        async with request.app.state.web() as session:
            yield session

    Session = Annotated[AsyncSession, Depends(get_session)]

    async def get_user_db(session: Session):
        yield SQLAlchemyUserDatabase(session, User, OAuthAccount)

    async def get_user_manager(user_db: Annotated[SQLAlchemyUserDatabase, Depends(get_user_db)]):
        yield manager_class(user_db)

    async def get_token_db(session: Session):
        yield SQLAlchemyAccessTokenDatabase(session, AccessToken)

    def get_strategy(
        token_db: Annotated[SQLAlchemyAccessTokenDatabase, Depends(get_token_db)],
    ) -> DatabaseStrategy:
        return DatabaseStrategy(token_db, lifetime_seconds=config.lifetime)

    backend = AuthenticationBackend(
        name="cookie",
        transport=CookieTransport(
            cookie_name=SESSION_COOKIE,
            cookie_max_age=config.lifetime,
            cookie_secure=config.cookie_secure,
            cookie_httponly=True,  # no script can read it
            cookie_samesite="lax",
        ),
        get_strategy=get_strategy,
    )
    users = FastAPIUsers[User, uuid.UUID](get_user_manager, [backend])
    github = (
        NarrowGitHub(config.github_client_id, config.github_client_secret)
        if config.github_client_id and config.github_client_secret
        else None
    )
    return Auth(
        users=users, backend=backend, github=github, current_user=users.current_user(active=True)
    )


async def purge_expired_sessions(session_factory: async_sessionmaker, lifetime_seconds: int) -> int:
    """Delete sessions past their lifetime -- they no longer sign anyone in, and
    nothing in the library removes them."""
    cutoff = datetime.now(UTC) - timedelta(seconds=lifetime_seconds)
    async with session_factory() as session:
        purged = await session.execute(delete(AccessToken).where(AccessToken.created_at < cutoff))
        await session.commit()
    return purged.rowcount
