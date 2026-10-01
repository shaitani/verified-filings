"""The question routes (DESIGN §4): ask, answer, watch, read back, list, reopen, report.

Every route needs a signed-in reader and touches only that reader's
conversations -- another reader's reads as 404, never 403, so an id tells a
guesser nothing. Cookies are ``SameSite=Lax`` and every write takes JSON, which
a cross-site page can neither send with the cookie nor post without a CORS
preflight this server never grants: that is the CSRF defence.
"""

# No `from __future__ import annotations`: FastAPI resolves a dependency's
# annotations at runtime, and these dependencies are closures it could not see.

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api import limits, storage
from app.api.auth import Auth
from app.api.jobs import event_stream
from app.api.schemas import (
    JOB_EVENT_REF,
    AnswersIn,
    ConversationSummary,
    ConversationView,
    FeedbackIn,
    JobCreated,
    JobView,
    NewConversationIn,
)
from app.chain import UnknownChoice
from app.db.web import User

#: What each storage refusal is on the wire. 404s carry no code: "not yours" and
#: "does not exist" must read the same.
REFUSALS = {
    UnknownChoice: (status.HTTP_400_BAD_REQUEST, "UNKNOWN_CHOICE"),
    storage.NotReady: (status.HTTP_409_CONFLICT, "ROUND_STILL_RUNNING"),
    storage.NothingToAnswer: (status.HTTP_409_CONFLICT, "NOTHING_TO_ANSWER"),
    storage.Conflict: (status.HTTP_409_CONFLICT, "ROUND_CONFLICT"),
    storage.NotFound: (status.HTTP_404_NOT_FOUND, "NOT_FOUND"),
}


def _refused(exc: Exception) -> HTTPException:
    code, detail = REFUSALS[type(exc)]
    return HTTPException(code, detail=detail)


async def _admit(web, reader: User) -> None:
    """Whether a new round may join the queue (limits.py). Every round is a GPU
    job, whether it asks or answers a question back."""
    seen = await storage.admission(web, reader.id)
    if seen.unfinished_all >= limits.UNFINISHED_ACROSS_SERVER:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=limits.SERVER_BUSY,
            headers=limits.retry_after(limits.BUSY_RETRY_AFTER),
        )
    if seen.unfinished_mine >= limits.UNFINISHED_PER_READER:
        # No Retry-After: it frees when one of theirs finishes, not at a time.
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=limits.TOO_MANY_QUESTIONS)
    if not reader.is_superuser and seen.today_mine >= limits.DAILY_PER_READER:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=limits.DAILY_LIMIT,
            headers=limits.retry_after(seen.first_frees_in or limits.DAY),
        )


def build_router(auth: Auth) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["questions"])
    Reader = Annotated[User, Depends(auth.current_user)]
    # Counting and writing a round happen under one lock, so two requests at once
    # cannot both pass a cap with one place left. One process (DESIGN §4), so one
    # lock is all of them; it is held for two short statements.
    admission = asyncio.Lock()

    @router.post("/conversations", status_code=status.HTTP_201_CREATED)
    async def ask(body: NewConversationIn, request: Request, reader: Reader) -> JobCreated:
        """A new question: its conversation and first round, queued."""
        web, runner = request.app.state.web, request.app.state.runner
        async with admission:
            await _admit(web, reader)
            conversation_id, job_id = await storage.create_conversation(
                web, reader.id, body.question
            )
        await runner.submit(job_id)  # before the id is returned, so no stream can miss it
        return JobCreated(conversation_id=conversation_id, job_id=job_id)

    @router.post("/conversations/{conversation_id}/answers", status_code=status.HTTP_201_CREATED)
    async def answer(
        conversation_id: uuid.UUID, body: AnswersIn, request: Request, reader: Reader
    ) -> JobCreated:
        """Answers to the last round's questions: the next round, queued."""
        web, runner = request.app.state.web, request.app.state.runner
        choices = [(a.ask_id, a.option_id) for a in body.answers]
        async with admission:
            await _admit(web, reader)
            try:
                job_id = await storage.create_round(web, reader.id, conversation_id, choices)
            except tuple(REFUSALS) as exc:
                raise _refused(exc) from exc
        await runner.submit(job_id)
        return JobCreated(conversation_id=conversation_id, job_id=job_id)

    @router.get("/conversations")
    async def history(request: Request, reader: Reader) -> list[ConversationSummary]:
        """The reader's past questions, newest first."""
        return await storage.conversations(request.app.state.web, reader.id)

    @router.get("/conversations/{conversation_id}")
    async def conversation(
        conversation_id: uuid.UUID, request: Request, reader: Reader
    ) -> ConversationView:
        """A past conversation reopened: every round, in order, as the thread shows it."""
        try:
            return await storage.conversation_view(
                request.app.state.web, reader.id, conversation_id
            )
        except storage.NotFound as exc:
            raise _refused(exc) from exc

    @router.get("/jobs/{job_id}")
    async def job(job_id: uuid.UUID, request: Request, reader: Reader) -> JobView:
        """One round as it stands -- for a reload or a dropped stream."""
        try:
            return await storage.job_view(request.app.state.web, reader.id, job_id)
        except storage.NotFound as exc:
            raise _refused(exc) from exc

    @router.get(
        "/jobs/{job_id}/events",
        response_class=StreamingResponse,
        # Each frame is "event: <kind>" then "data: <one JobEvent as JSON>"; the
        # schema is published by server._publish_stream_events.
        responses={
            200: {"content": {"text/event-stream": {"schema": {"$ref": JOB_EVENT_REF}}}}
        },
    )
    async def events(job_id: uuid.UUID, request: Request, reader: Reader) -> StreamingResponse:
        """Server-sent events: each stage, then ``done`` (with the reply) or ``failed``."""
        web, runner = request.app.state.web, request.app.state.runner
        try:
            # Checked here, not inside the stream: once streaming starts, 404 is too late.
            await storage.job_view(web, reader.id, job_id)
        except storage.NotFound as exc:
            raise _refused(exc) from exc
        return StreamingResponse(
            event_stream(runner, web, reader.id, job_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},  # no proxy buffering
        )

    @router.post("/jobs/{job_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
    async def feedback(
        job_id: uuid.UUID, body: FeedbackIn, request: Request, reader: Reader
    ) -> Response:
        """Report a problem: the job becomes a ``--flagged`` one in the owner's trace CLI."""
        try:
            await storage.add_feedback(request.app.state.web, reader.id, job_id, body.note)
        except storage.NotFound as exc:
            raise _refused(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
