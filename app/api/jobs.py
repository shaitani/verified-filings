"""The job queue: one job at a time, its stages pushed to whoever is watching.

One worker, because the GPU serves one request at a time and one gate is
simpler than one per model call (DESIGN §12a). Each job runs the chain inside a
trace; its stages are written to ``web.job`` and published as events; it ends
``done`` with its reply, or ``failed`` if the machinery itself broke -- a
refusal is a finished job. Its trace is written whatever happened.

The queue and the watchers live in this process, which is why the Web Server
runs one worker (DESIGN §4); a job cut off by a restart is failed at startup.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.chain as chain
from app import trace
from app.api import storage
from app.api.schemas import DoneEvent, FailedEvent, JobEvent, StageEvent
from app.api.trace import write_trace
from app.chain import BUG, ErrorRecord, Outcome
from app.schemas.job import JobStage

log = logging.getLogger(__name__)

TERMINAL = (DoneEvent, FailedEvent)


class JobRunner:
    """Owns the queue, the worker, and who is watching which job."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sessions = session_factory
        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._started: dict[UUID, float] = {}  # submit time, for StageEvent.seconds
        self._history: dict[UUID, list[JobEvent]] = {}  # replayed to a late watcher
        self._watchers: dict[UUID, set[asyncio.Queue[JobEvent]]] = {}

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> int:
        """Fail what a restart cut off, then start the worker. Returns how many were swept."""
        swept = await storage.sweep_unfinished(self._sessions)
        self._worker = asyncio.create_task(self._work(), name="job-runner")
        return swept

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def submit(self, job_id: UUID) -> None:
        """Queue a job whose row already exists (``storage.create_*``)."""
        self._started[job_id] = time.monotonic()
        self._publish(job_id, StageEvent(stage="queued", seconds=0.0))
        await self._queue.put(job_id)

    async def drained(self) -> None:
        """Wait until every submitted job has run -- for tests and shutdown."""
        await self._queue.join()

    # -- watching --------------------------------------------------------------

    def is_running(self, job_id: UUID) -> bool:
        return job_id in self._history

    async def watch(self, job_id: UUID) -> AsyncIterator[JobEvent]:
        """Every event so far, then each new one, ending with ``done`` or ``failed``.
        Only for a job this process is running; a finished one is read from the
        database instead (``event_stream``)."""
        inbox: asyncio.Queue[JobEvent] = asyncio.Queue()
        seen = list(self._history.get(job_id, []))
        self._watchers.setdefault(job_id, set()).add(inbox)
        try:
            for event in seen:
                yield event
                if isinstance(event, TERMINAL):
                    return
            while True:
                event = await inbox.get()
                yield event
                if isinstance(event, TERMINAL):
                    return
        finally:
            self._watchers.get(job_id, set()).discard(inbox)

    def _publish(self, job_id: UUID, event: JobEvent) -> None:
        self._history.setdefault(job_id, []).append(event)
        for inbox in self._watchers.get(job_id, ()):
            inbox.put_nowait(event)
        if isinstance(event, TERMINAL):
            # Finished: from now on the database answers (event_stream).
            self._history.pop(job_id, None)
            self._started.pop(job_id, None)

    # -- running ----------------------------------------------------------------

    async def _work(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run(job_id)
            except Exception:  # never let one job stop the worker
                log.exception("job runner: unhandled error running %s", job_id)
            finally:
                self._queue.task_done()

    async def _run(self, job_id: UUID) -> None:
        outcome: Outcome | None = None
        with trace.collecting(job_id=str(job_id)) as collected:
            try:
                inputs = await storage.round_inputs(self._sessions, job_id)

                async def on_stage(stage: JobStage) -> None:
                    await storage.set_status(self._sessions, job_id, stage)
                    self._publish(job_id, StageEvent(stage=stage, seconds=self._elapsed(job_id)))

                if inputs.pending is None:
                    outcome = await chain.ask(inputs.question, on_stage=on_stage)
                else:
                    outcome, _, _ = await chain.ask_again(
                        inputs.question,
                        inputs.pending,
                        inputs.choices,
                        answers=inputs.answers,
                        pins=inputs.pins,
                        on_stage=on_stage,
                    )
                reply = outcome.reply(inputs.conversation_id, job_id)
                await storage.finish(self._sessions, job_id, reply, outcome.pending)
                self._publish(job_id, DoneEvent(reply=reply))
            except Exception as exc:
                # The chain turns every expected failure into a reply, so this is
                # the machinery itself: the reader gets the bug sentence.
                log.exception("job %s failed", job_id)
                outcome = outcome or Outcome()
                outcome.errors.append(
                    ErrorRecord(
                        stage="runner",
                        type=type(exc).__name__,
                        message=str(exc),
                        traceback="".join(traceback.format_exception(exc)),
                    )
                )
                await _quietly(storage.fail(self._sessions, job_id))
                self._publish(job_id, FailedEvent(message=BUG))
            finally:
                # Written whatever happened: the failed job is the one to read.
                await _quietly(write_trace(self._sessions, job_id, outcome or Outcome(), collected))

    def _elapsed(self, job_id: UUID) -> float:
        return round(time.monotonic() - self._started.get(job_id, time.monotonic()), 3)


async def _quietly(awaitable) -> None:
    """Bookkeeping after a failure must not raise over the failure it records."""
    try:
        await awaitable
    except Exception:
        log.exception("job runner: bookkeeping failed")


# --------------------------------------------------------------------------- #
# Server-sent events
# --------------------------------------------------------------------------- #


def sse(event: JobEvent) -> str:
    """One event as a text/event-stream frame: its kind, then its JSON."""
    return f"event: {event.kind}\ndata: {event.model_dump_json()}\n\n"


async def event_stream(
    runner: JobRunner, session_factory: async_sessionmaker, user_id: UUID, job_id: UUID
) -> AsyncIterator[str]:
    """What ``GET /api/jobs/{id}/events`` sends: live stages while the job runs
    here, then its end; for a job already finished, just its end, from the
    database -- so a reload or a dropped stream always gets the answer.
    ``storage.NotFound`` for a job that is not this reader's."""
    view = await storage.job_view(session_factory, user_id, job_id)  # ownership first
    if runner.is_running(job_id):
        async for event in runner.watch(job_id):
            yield sse(event)
        return
    if view.status == "done":
        yield sse(DoneEvent(reply=view.reply))
    elif view.status == "failed":
        yield sse(FailedEvent(message=BUG))
    else:
        # Unfinished, yet not queued here: routes submit before returning an id,
        # and startup fails what a restart cut off -- so this job is orphaned.
        # Waiting would hang the stream forever; fail it, as a restart would.
        log.error("job %s is %s but not queued in this process", job_id, view.status)
        await _quietly(storage.fail(session_factory, job_id))
        yield sse(FailedEvent(message=BUG))
