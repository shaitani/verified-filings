"""The ``web`` schema: users, sign-in, conversations and jobs for the Web Server.

Beside ``models.py``'s ``xbrl`` tables, under a declarative base of its own so
the two schemas never share a ``MetaData``. Design: ``app/api/DESIGN.md`` §11.
Grants: ``app/db/roles.py`` ``WEB``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, get_args

from fastapi_users_db_sqlalchemy import (
    SQLAlchemyBaseOAuthAccountTableUUID,
    SQLAlchemyBaseUserTableUUID,
)
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyBaseAccessTokenTableUUID
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from app.db.models import NAMING_CONVENTION
from app.schemas.job import JOB_STATUSES

SCHEMA = "web"

InviteKind = Literal["email", "github"]


def _choices(name: str, values: tuple[str, ...]) -> SAEnum:
    # Text plus a CHECK in the database, not a PostgreSQL enum (db DESIGN §3.11),
    # and validate_strings so a misspelt value fails in Python before it is sent.
    return SAEnum(
        *values, name=name, native_enum=False, create_constraint=True, validate_strings=True
    )


def _now() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WebBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------- #
# Sign-in: the library's tables, with our changes marked
# --------------------------------------------------------------------------- #


class User(SQLAlchemyBaseUserTableUUID, WebBase):
    __table_args__ = (
        # The library looks emails up by lower(email) but its UNIQUE is exact,
        # so two sign-ups differing only in case could both land.
        Index("uq_user_email_lower", func.lower(text("email")), unique=True),
    )

    # Overridden: a database default rather than the library's Python one, so an
    # INSERT leaves the column out -- vf_web_role cannot write it (DESIGN §11).
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    created_at: Mapped[datetime] = _now()


class OAuthAccount(SQLAlchemyBaseOAuthAccountTableUUID, WebBase):
    pass  # a linked GitHub login; deleted with its user


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, WebBase):
    __tablename__ = "access_token"  # the library's default is "accesstoken"


class Invite(WebBase):
    """One way in: an email with a single-use code, or a GitHub account id (§10)."""

    __tablename__ = "invite"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'email' AND email IS NOT NULL AND code_hash IS NOT NULL"
            " AND github_account_id IS NULL)"
            " OR (kind = 'github' AND github_account_id IS NOT NULL"
            " AND email IS NULL AND code_hash IS NULL)",
            name="one_way_in",
        ),
        # used_by is cleared if that user is deleted; used_at stays as the record.
        CheckConstraint("used_by IS NULL OR used_at IS NOT NULL", name="used_by_needs_used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(_choices("invite_kind", get_args(InviteKind)))
    email: Mapped[str | None] = mapped_column(String(320))
    code_hash: Mapped[str | None] = mapped_column(String(1024))  # the code itself is never stored
    github_account_id: Mapped[str | None] = mapped_column(String(320))  # as GitHub reports it
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set by the CLI
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("user.id", ondelete="SET NULL")
    )

    @validates("kind")
    def _known_kind(self, _key: str, value: str) -> str:
        if value not in get_args(InviteKind):
            raise ValueError(f"unknown invite kind {value!r}")
        return value


# --------------------------------------------------------------------------- #
# Conversations and jobs
# --------------------------------------------------------------------------- #


class Conversation(WebBase):
    __tablename__ = "conversation"
    __table_args__ = (Index("ix_conversation_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)  # verbatim
    created_at: Mapped[datetime] = _now()


class Job(WebBase):
    """One round of a conversation: the question, or a set of answers to it."""

    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("conversation_id", "round", name="uq_job_conversation_round"),
        CheckConstraint("round >= 1", name="round_positive"),
        # The same rules JobView enforces, held by the database too.
        CheckConstraint("(reply IS NOT NULL) = (status = 'done')", name="reply_when_done"),
        CheckConstraint(
            "(finished_at IS NOT NULL) = (status IN ('done', 'failed'))",
            name="finished_when_finished",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = the question
    status: Mapped[str] = mapped_column(_choices("job_status", JOB_STATUSES), nullable=False)
    asks: Mapped[Any | None] = mapped_column(JSONB)  # the options offered, checked on answer
    answers: Mapped[Any | None] = mapped_column(JSONB)  # this round's answers: text + option_id
    reply: Mapped[Any | None] = mapped_column(JSONB)  # the Reply, once done
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("status")
    def _known_status(self, _key: str, value: str) -> str:
        # At assignment, not at flush: "Done" or "complete" fails where it is written.
        if value not in JOB_STATUSES:
            raise ValueError(f"unknown job status {value!r}; expected one of {JOB_STATUSES}")
        return value


class JobTrace(WebBase):
    """Everything never sent to a browser, kept for debugging (§8). Write-only
    for vf_web_role, so it is written with a plain INSERT and never read back."""

    __tablename__ = "job_trace"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("job.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _now()
    code_version: Mapped[str] = mapped_column(String(64), nullable=False)  # git rev, "+dirty"
    models: Mapped[Any] = mapped_column(JSONB, nullable=False)  # parser/generator/embedding tags
    query_in: Mapped[Any | None] = mapped_column(JSONB)  # each null past the stage that stopped
    plan: Mapped[Any | None] = mapped_column(JSONB)
    result: Mapped[Any | None] = mapped_column(JSONB)
    model_calls: Mapped[Any] = mapped_column(JSONB, nullable=False)  # full prompts, raw replies
    statements: Mapped[Any] = mapped_column(JSONB, nullable=False)  # SQL, verdict, error
    timings: Mapped[Any] = mapped_column(JSONB, nullable=False)
    errors: Mapped[Any] = mapped_column(JSONB, nullable=False)  # stage, type, message, traceback


class JobFeedback(WebBase):
    """Report a problem -- how a wrong-looking answer becomes a --flagged job."""

    __tablename__ = "job_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("job.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
