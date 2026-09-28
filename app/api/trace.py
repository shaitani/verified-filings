"""A job's trace: written once per job by the Web Server, read on the host.

Writing is ``write_trace``, as ``vf_web_role``, with a plain INSERT -- that role
cannot read ``web.job_trace`` back (DESIGN §8, §11). Reading is this module as
a command, with the owner's credential:

    uv run python -m app.api.trace <job_id>          one job, as JSON
    uv run python -m app.api.trace --flagged         jobs a reader reported
    uv run python -m app.api.trace --failed [--since 2026-10-01]

That command is what a background Claude session is pointed at.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.chain import Outcome
from app.db.web import Conversation, Job, JobFeedback, JobTrace
from app.embedding_client import EMBEDDING_MODEL
from app.parser.proposer import PARSER_MODEL
from app.retrieval.generator import GENERATION_MODEL
from app.trace import Trace

REPO = Path(__file__).resolve().parent.parent.parent

MODELS = {"parser": PARSER_MODEL, "generator": GENERATION_MODEL, "embedding": EMBEDDING_MODEL}


@lru_cache(maxsize=1)
def code_version() -> str:
    """What ran: ``CODE_VERSION`` when set (the container has no .git), else the
    git revision, ``+dirty`` when tracked files have changed since it."""
    if os.environ.get("CODE_VERSION"):
        return os.environ["CODE_VERSION"][:64]
    try:
        rev = _git("rev-parse", "--short=12", "HEAD")
        dirty = _git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return rev + ("+dirty" if dirty else "")


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True, timeout=10
    )
    return done.stdout.strip()


def trace_row(job_id: UUID, outcome: Outcome, collected: Trace) -> JobTrace:
    def dumped(model):
        return model.model_dump(mode="json") if model is not None else None

    return JobTrace(
        job_id=job_id,
        code_version=code_version(),
        models=MODELS,
        query_in=dumped(outcome.query_in),
        plan=dumped(outcome.plan),
        result=dumped(outcome.result),
        model_calls=collected.model_calls,
        statements=collected.statements,
        timings=outcome.timings,
        errors=[error.model_dump(mode="json") for error in outcome.errors],
    )


async def write_trace(
    session_factory: async_sessionmaker, job_id: UUID, outcome: Outcome, collected: Trace
) -> None:
    """One INSERT, committed, never read back (the model has eager_defaults off)."""
    async with session_factory() as session:
        session.add(trace_row(job_id, outcome, collected))
        await session.commit()


# --------------------------------------------------------------------------- #
# Reading -- the owner's credential only
# --------------------------------------------------------------------------- #


async def show_job(session_factory: async_sessionmaker, job_id: UUID) -> dict | None:
    """Everything kept about one job, with its conversation's other rounds."""
    async with session_factory() as session:
        found = (
            await session.execute(
                select(Job, Conversation).join(Conversation).where(Job.id == job_id)
            )
        ).one_or_none()
        if found is None:
            return None
        job, conversation = found
        trace = await session.get(JobTrace, job_id)
        notes = (
            await session.execute(
                select(JobFeedback.note, JobFeedback.created_at).where(JobFeedback.job_id == job_id)
            )
        ).all()
        rounds = (
            await session.execute(
                select(Job.round, Job.id, Job.status)
                .where(Job.conversation_id == conversation.id)
                .order_by(Job.round)
            )
        ).all()
    return {
        "job_id": job.id,
        "conversation_id": conversation.id,
        "user_id": conversation.user_id,
        "question": conversation.question,
        "round": job.round,
        "status": job.status,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "answers": job.answers,
        "asks": job.asks,
        "reply": job.reply,
        "rounds": [{"round": r, "job_id": i, "status": s} for r, i, s in rounds],
        "feedback": [{"note": n, "at": at} for n, at in notes],
        "trace": _columns(trace) if trace else None,  # None: cut off by a restart
    }


async def flagged(session_factory: async_sessionmaker) -> list[dict]:
    """Jobs a reader reported, newest first."""
    async with session_factory() as session:
        reports = (JobFeedback.job_id, JobFeedback.created_at, JobFeedback.note)
        rows = await session.execute(
            select(*reports, Conversation.question)
            .join(Job, Job.id == JobFeedback.job_id)
            .join(Conversation, Conversation.id == Job.conversation_id)
            .order_by(JobFeedback.created_at.desc())
        )
        return [
            {"job_id": j, "reported_at": at, "note": note, "question": q} for j, at, note, q in rows
        ]


async def failed(session_factory: async_sessionmaker, since: date | None = None) -> list[dict]:
    """Jobs that failed, or finished having recorded an error on the way --
    a refusal is a finished job, so its error is only in the trace."""
    errors = func.coalesce(func.jsonb_array_length(JobTrace.errors), 0)
    query = (
        select(Job.id, Job.created_at, Job.status, Conversation.question, JobTrace.errors)
        .join(Conversation, Conversation.id == Job.conversation_id)
        .outerjoin(JobTrace, JobTrace.job_id == Job.id)
        .where((Job.status == "failed") | (errors > 0))
        .order_by(Job.created_at.desc())
    )
    if since is not None:
        query = query.where(Job.created_at >= datetime.combine(since, time.min))
    async with session_factory() as session:
        rows = await session.execute(query)
        return [
            {
                "job_id": j,
                "created_at": at,
                "status": status,
                "question": q,
                "first_error": (errs or [None])[0],
            }
            for j, at, status, q, errs in rows
        ]


def _columns(row) -> dict:
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the Web Server's job traces.")
    parser.add_argument("job_id", nargs="?", type=UUID, help="one job, in full")
    parser.add_argument("--flagged", action="store_true", help="jobs a reader reported")
    parser.add_argument("--failed", action="store_true", help="jobs that failed or hit an error")
    parser.add_argument("--since", type=date.fromisoformat, help="with --failed: from this date")
    args = parser.parse_args(argv)
    if sum(bool(x) for x in (args.job_id, args.flagged, args.failed)) != 1:
        parser.error("give exactly one of: a job id, --flagged, --failed")

    from app.db.session import SessionLocal  # the owner: vf_web_role cannot read traces

    if args.job_id:
        found = asyncio.run(show_job(SessionLocal, args.job_id))
        if found is None:
            print(f"no job {args.job_id}", file=sys.stderr)
            return 1
        out = found
    elif args.flagged:
        out = asyncio.run(flagged(SessionLocal))
    else:
        out = asyncio.run(failed(SessionLocal, args.since))
    sys.stdout.buffer.write(json.dumps(out, indent=1, default=str, ensure_ascii=False).encode())
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
