"""The Web Server: ``create_app()``.

    uv run uvicorn app.api.server:create_app --factory --reload

One worker only: the job queue and its watchers live in the process (DESIGN §4).
Routes: sign-in (``auth.py``) and the questions (``routes.py``).
"""

# No `from __future__ import annotations`: FastAPI resolves a dependency's
# annotations at runtime, and these dependencies are closures it could not see.

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response, status
from sqlalchemy import text

from app.api.auth import (
    AuthConfig,
    UserCreate,
    UserRead,
    build_auth,
    purge_expired_sessions,
)
from app.api.jobs import JobRunner
from app.api.routes import build_router
from app.config import settings
from app.db.session import web_sessionmaker
from app.db.web import User

log = logging.getLogger(__name__)

PURGE_EVERY = 6 * 3600  # seconds between sweeps of expired sessions


def create_app(config: AuthConfig | None = None, web_url: str | None = None) -> FastAPI:
    """The app. ``config`` and ``web_url`` default to ``.env``; tests pass their own."""
    config = config or AuthConfig.from_settings(settings)
    auth = build_auth(config)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.web = web_sessionmaker(web_url)  # vf_web_role, or refuse to start
        app.state.runner = JobRunner(app.state.web)
        swept = await app.state.runner.start()
        if swept:
            log.warning("startup: failed %d job(s) a restart cut off", swept)
        purger = asyncio.create_task(_purge_forever(app.state.web, config.lifetime))
        try:
            yield
        finally:
            purger.cancel()
            await app.state.runner.stop()
            await app.state.web.kw["bind"].dispose()

    app = FastAPI(title="Verified Filings", lifespan=lifespan)
    app.state.auth = auth
    users, backend = auth.users, auth.backend

    # Sign-in and sign-out: POST /api/auth/login (form: username, password), /logout.
    app.include_router(users.get_auth_router(backend), prefix="/api/auth", tags=["auth"])
    # Registration, by invitation (UserCreate carries the code).
    app.include_router(
        users.get_register_router(UserRead, UserCreate), prefix="/api/auth", tags=["auth"]
    )
    if auth.github is not None:
        app.include_router(
            users.get_oauth_router(
                auth.github,
                backend,
                config.oauth_state_secret,
                redirect_url=config.github_redirect_url,
                csrf_token_cookie_secure=config.cookie_secure,
            ),
            prefix="/api/auth/github",
            tags=["auth"],
        )
    # Deliberately not mounted: verification and forgot-password wait for an email
    # sender (DESIGN §10), and the library's /users/{id} admin routes -- administration
    # is the CLI's (app/api/admin.py), with the owner's credential.

    app.include_router(build_router(auth))  # ask, answer, watch, read back, list, report

    @app.get("/api/health", tags=["ops"])
    async def health(request: Request, response: Response) -> dict[str, str]:
        """Up, and able to reach the database as vf_web_role. No sign-in: it says
        nothing a reader could use, and Docker's health check calls it."""
        try:
            async with request.app.state.web() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            log.exception("health: the database is unreachable")
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "database unreachable"}
        return {"status": "ok"}

    @app.get("/api/me", response_model=UserRead, tags=["auth"])
    async def me(user: Annotated[User, Depends(auth.current_user)]) -> User:
        return user

    return app


async def _purge_forever(web, lifetime: int) -> None:
    while True:
        try:
            purged = await purge_expired_sessions(web, lifetime)
            if purged:
                log.info("purged %d expired session(s)", purged)
        except Exception:
            log.exception("session purge failed")
        await asyncio.sleep(PURGE_EVERY)
