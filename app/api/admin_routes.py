"""The admin routes, ``/api/admin/*`` (DESIGN §13): users, invites, rounds and
their traces, problem reports, and the audit log.

**Only for administrators, and invisible to everyone else.** The whole router
depends on ``admin_reader``: no session is 401, as everywhere; a signed-in
reader who is not an administrator gets **404**, the same body as a route that
does not exist, so the routes do not reveal themselves. ``is_superuser`` is read
fresh on every request, so ``unmake-admin`` takes effect on the next click.

Everything here runs as ``vf_admin_role`` (``request.app.state.admin``), never as
the web role, and every change is written to the audit log (``admin_storage``).
"""

# No `from __future__ import annotations`: FastAPI resolves a dependency's
# annotations at runtime, and these dependencies are closures it could not see.

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.api import admin_storage
from app.api.auth import Auth
from app.api.schemas import (
    AdminActionView,
    AdminInvite,
    AdminJob,
    AdminReport,
    AdminTrace,
    AdminUser,
    InviteCreated,
    NewInviteIn,
    PasswordReset,
)
from app.db.web import User

#: What each refusal is on the wire.
REFUSALS = {
    admin_storage.NotFound: (status.HTTP_404_NOT_FOUND, "NOT_FOUND"),
    admin_storage.AdminProtected: (status.HTTP_409_CONFLICT, "ADMIN_PROTECTED"),
    admin_storage.InviteNotOpen: (status.HTTP_409_CONFLICT, "INVITE_NOT_OPEN"),
}

#: How many rows a list returns unless asked for fewer.
Limit = Annotated[int, Query(ge=1, le=500)]


def _refused(exc: Exception) -> HTTPException:
    code, detail = REFUSALS[type(exc)]
    return HTTPException(code, detail=detail)


def build_admin_router(auth: Auth) -> APIRouter:
    async def admin_reader(reader: Annotated[User, Depends(auth.current_user)]) -> User:
        if not reader.is_superuser:
            # FastAPI's own body for a route that does not exist: nothing to tell apart.
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")
        return reader

    Admin = Annotated[User, Depends(admin_reader)]
    # On the router as well as each route, so a route that forgets the parameter
    # is guarded all the same (FastAPI runs a dependency once per request).
    router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(admin_reader)])

    # -- users -----------------------------------------------------------------

    @router.get("/users")
    async def users(request: Request, admin: Admin) -> list[AdminUser]:
        """Every account, oldest first."""
        return await admin_storage.users(request.app.state.admin)

    async def _act(change, request: Request, admin: User, user_id: uuid.UUID):
        try:
            return await change(request.app.state.admin, admin, user_id)
        except tuple(REFUSALS) as exc:
            raise _refused(exc) from exc

    @router.post("/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
    async def deactivate(user_id: uuid.UUID, request: Request, admin: Admin) -> Response:
        """Out at once: inactive, every session ended. Not for an administrator."""
        await _act(admin_storage.deactivate, request, admin, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/users/{user_id}/reactivate", status_code=status.HTTP_204_NO_CONTENT)
    async def reactivate(user_id: uuid.UUID, request: Request, admin: Admin) -> Response:
        await _act(admin_storage.reactivate, request, admin, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/users/{user_id}/end-sessions", status_code=status.HTTP_204_NO_CONTENT)
    async def end_sessions(user_id: uuid.UUID, request: Request, admin: Admin) -> Response:
        """Signed out everywhere; they can sign in again."""
        await _act(admin_storage.end_sessions, request, admin, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/users/{user_id}/reset-password")
    async def reset_password(user_id: uuid.UUID, request: Request, admin: Admin) -> PasswordReset:
        """A new random password, shown once; their sessions are ended."""
        password = await _act(admin_storage.reset_password, request, admin, user_id)
        return PasswordReset(password=password)

    @router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_user(user_id: uuid.UUID, request: Request, admin: Admin) -> Response:
        """The account and everything of theirs, for good. Not for an administrator."""
        await _act(admin_storage.delete_user, request, admin, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # -- invites -----------------------------------------------------------------

    @router.get("/invites")
    async def invites(request: Request, admin: Admin, limit: Limit = 100) -> list[AdminInvite]:
        """The newest first."""
        return await admin_storage.invites(request.app.state.admin, limit)

    @router.post("/invites", status_code=status.HTTP_201_CREATED)
    async def create_invite(body: NewInviteIn, request: Request, admin: Admin) -> InviteCreated:
        """A single-use code; the only time it is shown."""
        invite, code = await admin_storage.create_invite(request.app.state.admin, admin, body.days)
        return InviteCreated(invite=invite, code=code)

    @router.post("/invites/{invite_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
    async def revoke_invite(invite_id: uuid.UUID, request: Request, admin: Admin) -> Response:
        """An unspent code can no longer be spent."""
        await _act(admin_storage.revoke_invite, request, admin, invite_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # -- rounds, traces, reports ---------------------------------------------------

    @router.get("/jobs")
    async def jobs(
        request: Request,
        admin: Admin,
        limit: Limit = 100,
        reported: bool = False,
        failed: bool = False,
        user_id: uuid.UUID | None = None,
    ) -> list[AdminJob]:
        """Recent rounds, newest first; ``reported``, ``failed`` or one reader's."""
        return await admin_storage.jobs(
            request.app.state.admin, limit, reported=reported, failed=failed, user_id=user_id
        )

    @router.get("/jobs/{job_id}/trace")
    async def trace(job_id: uuid.UUID, request: Request, admin: Admin) -> AdminTrace:
        """One round's whole record: prompts, raw replies, SQL, errors, reports.
        Opening it is logged."""
        return await _act(admin_storage.trace, request, admin, job_id)

    @router.get("/reports")
    async def reports(request: Request, admin: Admin, limit: Limit = 100) -> list[AdminReport]:
        """Every "report a problem", newest first."""
        return await admin_storage.reports(request.app.state.admin, limit)

    # -- the audit log -------------------------------------------------------------

    @router.get("/actions")
    async def actions(request: Request, admin: Admin, limit: Limit = 100) -> list[AdminActionView]:
        """What admins did, newest first."""
        return await admin_storage.actions(request.app.state.admin, limit)

    return router
