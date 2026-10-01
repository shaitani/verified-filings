"""What the admin routes read and change, as ``vf_admin_role`` (DESIGN §11, §13).

Every function takes the admin session factory (``admin_sessionmaker()``), never
the web role's. Every change writes its audit row (``web.admin_action``) in the
same transaction, so an action and its record land together or not at all.

**An admin cannot act on an admin.** Row-level security says so in the database:
an UPDATE or DELETE on an administrator's row changes no rows, without an error.
So each change looks its target up first -- ``AdminProtected`` for an
administrator, ``NotFound`` for nobody -- and checks the count it changed, which
also catches a target made an administrator in between.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi_users.password import PasswordHelper
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.api.auth import hash_invite_code, new_invite_code
from app.api.schemas import (
    AdminActionView,
    AdminInvite,
    AdminJob,
    AdminReport,
    AdminTrace,
    AdminUser,
    InviteStatus,
)
from app.db.web import (
    AccessToken,
    AdminAction,
    Conversation,
    Invite,
    Job,
    JobFeedback,
    JobTrace,
    OAuthAccount,
    User,
)


class NotFound(Exception):
    """No such user, invite or job."""


class AdminProtected(Exception):
    """The target is an administrator: only the owner's CLI acts on one."""


class InviteNotOpen(Exception):
    """Already spent or revoked."""


_DAY = text("interval '24 hours'")
_NO_SYNC = {"synchronize_session": False}  # plain statements: no ORM objects to keep in step


def _audit(
    session: AsyncSession,
    admin: User,
    action: str,
    *,
    target_id: UUID | None = None,
    target_email: str | None = None,
    detail: Any = None,
) -> None:
    session.add(
        AdminAction(
            id=uuid.uuid4(),
            admin_id=admin.id,
            admin_email=admin.email,
            action=action,
            target_id=target_id,
            target_email=target_email,
            detail=detail,
        )
    )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


async def users(session_factory: async_sessionmaker) -> list[AdminUser]:
    """Every account, oldest first, with what an admin needs to judge it."""
    providers = (
        select(func.array_agg(func.distinct(OAuthAccount.oauth_name)))  # null with none
        .where(OAuthAccount.user_id == User.id)
        .scalar_subquery()
    )
    sessions = select(func.count()).where(AccessToken.user_id == User.id).scalar_subquery()
    today = (
        select(func.count())
        .select_from(Job)
        .join(Conversation, Conversation.id == Job.conversation_id)
        .where(Conversation.user_id == User.id, Job.created_at > func.now() - _DAY)
        .scalar_subquery()
    )
    statement = select(
        User.id,
        User.email,
        User.created_at,
        User.is_active,
        User.is_superuser,
        providers,
        sessions,
        today,
    ).order_by(User.created_at)
    async with session_factory() as session:
        rows = (await session.execute(statement)).all()
    return [
        AdminUser(
            id=row[0],
            email=row[1],
            created_at=row[2],
            is_active=row[3],
            is_superuser=row[4],
            sign_in_providers=sorted(row[5] or []),
            sessions=row[6],
            questions_today=row[7],
        )
        for row in rows
    ]


async def _reader(session: AsyncSession, user_id: UUID) -> str:
    """The target's email -- if it exists and is not an administrator."""
    row = (
        await session.execute(select(User.email, User.is_superuser).where(User.id == user_id))
    ).first()
    if row is None:
        raise NotFound
    if row.is_superuser:
        raise AdminProtected
    return row.email


async def _changed_one(session: AsyncSession, statement) -> None:
    """Run an UPDATE or DELETE on one user's row; row security changing nothing
    means the target became an administrator since it was looked up."""
    result = await session.execute(statement.execution_options(**_NO_SYNC))
    if result.rowcount != 1:
        await session.rollback()
        raise AdminProtected


async def _end_sessions(session: AsyncSession, user_id: UUID) -> int:
    ended = await session.execute(
        delete(AccessToken).where(AccessToken.user_id == user_id).execution_options(**_NO_SYNC)
    )
    return ended.rowcount


async def deactivate(session_factory: async_sessionmaker, admin: User, user_id: UUID) -> None:
    """Out at once: inactive, and every session ended."""
    async with session_factory() as session:
        email = await _reader(session, user_id)
        await _changed_one(session, update(User).where(User.id == user_id).values(is_active=False))
        await _end_sessions(session, user_id)
        _audit(session, admin, "deactivate", target_id=user_id, target_email=email)
        await session.commit()


async def reactivate(session_factory: async_sessionmaker, admin: User, user_id: UUID) -> None:
    async with session_factory() as session:
        email = await _reader(session, user_id)
        await _changed_one(session, update(User).where(User.id == user_id).values(is_active=True))
        _audit(session, admin, "reactivate", target_id=user_id, target_email=email)
        await session.commit()


async def end_sessions(session_factory: async_sessionmaker, admin: User, user_id: UUID) -> int:
    """Sign them out everywhere. Returns how many sessions ended."""
    async with session_factory() as session:
        email = await _reader(session, user_id)
        ended = await _end_sessions(session, user_id)
        _audit(
            session,
            admin,
            "end_sessions",
            target_id=user_id,
            target_email=email,
            detail={"ended": ended},
        )
        await session.commit()
    return ended


async def reset_password(session_factory: async_sessionmaker, admin: User, user_id: UUID) -> str:
    """A new random password, returned once, and the account's sessions ended --
    so no session outlives the password it was opened with."""
    password = secrets.token_urlsafe(18)  # 24 characters, well over the 12 minimum
    hashed = await asyncio.to_thread(PasswordHelper().hash, password)  # off the event loop
    async with session_factory() as session:
        email = await _reader(session, user_id)
        await _changed_one(
            session, update(User).where(User.id == user_id).values(hashed_password=hashed)
        )
        await _end_sessions(session, user_id)
        _audit(session, admin, "reset_password", target_id=user_id, target_email=email)
        await session.commit()
    return password


async def delete_user(session_factory: async_sessionmaker, admin: User, user_id: UUID) -> None:
    """The account and everything of theirs -- conversations, rounds, traces,
    reports -- by the foreign keys' cascades. The audit row keeps their email."""
    async with session_factory() as session:
        email = await _reader(session, user_id)
        await _changed_one(session, delete(User).where(User.id == user_id))
        _audit(session, admin, "delete_user", target_id=user_id, target_email=email)
        await session.commit()


# --------------------------------------------------------------------------- #
# Invites
# --------------------------------------------------------------------------- #


def _invite_status(
    used_at: datetime | None, revoked_at: datetime | None, expires_at: datetime
) -> InviteStatus:
    if used_at is not None:
        return "used"
    if revoked_at is not None:
        return "revoked"
    return "expired" if expires_at <= datetime.now(UTC) else "open"


def _invites_statement():
    used_by, created_by = aliased(User), aliased(User)
    return (
        select(Invite, used_by.email, created_by.email)
        .outerjoin(used_by, used_by.id == Invite.used_by)
        .outerjoin(created_by, created_by.id == Invite.created_by)
    )


def _invite_view(invite: Invite, used_by: str | None, created_by: str | None) -> AdminInvite:
    return AdminInvite(
        id=invite.id,
        status=_invite_status(invite.used_at, invite.revoked_at, invite.expires_at),
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        used_at=invite.used_at,
        used_by_email=used_by,
        revoked_at=invite.revoked_at,
        created_by_email=created_by,
    )


async def invites(session_factory: async_sessionmaker, limit: int) -> list[AdminInvite]:
    """The newest first."""
    statement = _invites_statement().order_by(Invite.created_at.desc()).limit(limit)
    async with session_factory() as session:
        rows = (await session.execute(statement)).all()
    return [_invite_view(*row) for row in rows]


async def create_invite(
    session_factory: async_sessionmaker, admin: User, days: int
) -> tuple[AdminInvite, str]:
    """A single-use code, valid ``days``. The code is returned here, once, and
    only its hash is stored."""
    code = new_invite_code()
    invite_id = uuid.uuid4()
    async with session_factory() as session:
        # A plain INSERT of exactly the granted columns: the ORM would also send
        # used_at, used_by and revoked_at as NULL, which vf_admin_role may not write.
        await session.execute(
            insert(Invite).values(
                id=invite_id,
                code_hash=hash_invite_code(code),
                expires_at=datetime.now(UTC) + timedelta(days=days),
                created_by=admin.id,
            )
        )
        _audit(session, admin, "create_invite", target_id=invite_id, detail={"days": days})
        await session.commit()
        row = (await session.execute(_invites_statement().where(Invite.id == invite_id))).one()
    return _invite_view(*row), code


async def revoke_invite(session_factory: async_sessionmaker, admin: User, invite_id: UUID) -> None:
    """An unspent invite can no longer be spent. Atomic against a sign-up racing
    for it: one of the two UPDATEs finds it open, never both."""
    async with session_factory() as session:
        revoked = await session.execute(
            update(Invite)
            .where(Invite.id == invite_id, Invite.used_at.is_(None), Invite.revoked_at.is_(None))
            .values(revoked_at=func.now())
            .execution_options(**_NO_SYNC)
        )
        if revoked.rowcount != 1:
            exists = await session.scalar(select(func.count()).where(Invite.id == invite_id))
            raise InviteNotOpen if exists else NotFound
        _audit(session, admin, "revoke_invite", target_id=invite_id)
        await session.commit()


# --------------------------------------------------------------------------- #
# Rounds, traces and reports
# --------------------------------------------------------------------------- #


def _jobs_statement():
    reports = select(func.count()).where(JobFeedback.job_id == Job.id).scalar_subquery()
    traced = select(JobTrace.job_id).where(JobTrace.job_id == Job.id).exists()
    return (
        select(
            Job.id,
            Job.conversation_id,
            User.email,
            Conversation.question,
            Job.round,
            Job.status,
            Job.created_at,
            Job.finished_at,
            Job.reply["status"].astext,
            reports,
            traced,
        )
        .join(Conversation, Conversation.id == Job.conversation_id)
        .join(User, User.id == Conversation.user_id)
    )


def _job_view(row) -> AdminJob:
    return AdminJob(
        job_id=row[0],
        conversation_id=row[1],
        user_email=row[2],
        question=row[3],
        round=row[4],
        status=row[5],
        created_at=row[6],
        finished_at=row[7],
        reply_status=row[8],
        reports=row[9],
        has_trace=row[10],
    )


async def jobs(
    session_factory: async_sessionmaker,
    limit: int,
    *,
    reported: bool = False,
    failed: bool = False,
    user_id: UUID | None = None,
) -> list[AdminJob]:
    """Recent rounds, newest first: all of them, or only the reported, the
    failed, or one reader's."""
    statement = _jobs_statement()
    if reported:
        statement = statement.where(
            select(JobFeedback.id).where(JobFeedback.job_id == Job.id).exists()
        )
    if failed:
        statement = statement.where(Job.status == "failed")
    if user_id is not None:
        statement = statement.where(Conversation.user_id == user_id)
    statement = statement.order_by(Job.created_at.desc()).limit(limit)
    async with session_factory() as session:
        rows = (await session.execute(statement)).all()
    return [_job_view(row) for row in rows]


def _reports_statement():
    return (
        select(
            JobFeedback.id,
            JobFeedback.job_id,
            User.email,
            Conversation.question,
            JobFeedback.note,
            JobFeedback.created_at,
        )
        .join(Job, Job.id == JobFeedback.job_id)
        .join(Conversation, Conversation.id == Job.conversation_id)
        .join(User, User.id == JobFeedback.user_id)
    )


def _report_view(row) -> AdminReport:
    return AdminReport(
        id=row[0], job_id=row[1], user_email=row[2], question=row[3], note=row[4], created_at=row[5]
    )


async def reports(session_factory: async_sessionmaker, limit: int) -> list[AdminReport]:
    """Every "report a problem", newest first."""
    statement = _reports_statement().order_by(JobFeedback.created_at.desc()).limit(limit)
    async with session_factory() as session:
        rows = (await session.execute(statement)).all()
    return [_report_view(row) for row in rows]


async def trace(session_factory: async_sessionmaker, admin: User, job_id: UUID) -> AdminTrace:
    """One round's whole record. Opening it shows a reader's question and every
    prompt it produced, so the opening is logged."""
    async with session_factory() as session:
        row = (await session.execute(_jobs_statement().where(Job.id == job_id))).first()
        if row is None:
            raise NotFound
        job = _job_view(row)
        kept = await session.get(JobTrace, job_id)
        reported = (
            await session.execute(
                _reports_statement()
                .where(JobFeedback.job_id == job_id)
                .order_by(JobFeedback.created_at)
            )
        ).all()
        _audit(
            session,
            admin,
            "read_trace",
            target_id=job_id,
            target_email=job.user_email,
            detail={"conversation_id": str(job.conversation_id), "round": job.round},
        )
        await session.commit()
    fields = (
        "code_version",
        "models",
        "query_in",
        "plan",
        "result",
        "model_calls",
        "statements",
        "timings",
        "errors",
    )
    stored = {name: getattr(kept, name) for name in fields} if kept is not None else {}
    return AdminTrace(job=job, reports=[_report_view(r) for r in reported], **stored)


# --------------------------------------------------------------------------- #
# The audit log
# --------------------------------------------------------------------------- #


async def actions(session_factory: async_sessionmaker, limit: int) -> list[AdminActionView]:
    """What admins did, newest first."""
    statement = select(AdminAction).order_by(AdminAction.created_at.desc()).limit(limit)
    async with session_factory() as session:
        rows = (await session.scalars(statement)).all()
    return [
        AdminActionView(
            id=row.id,
            created_at=row.created_at,
            admin_email=row.admin_email,
            action=row.action,
            target_id=row.target_id,
            target_email=row.target_email,
            detail=row.detail,
        )
        for row in rows
    ]
