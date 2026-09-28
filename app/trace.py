"""What one job did, collected while it runs, for its ``web.job_trace`` row.

The parser, the SQL generator and the executor already see the raw material --
prompts, replies, statements. They append it here **when a trace is active**
and do nothing otherwise, so the evals, the CLI and the tests behave exactly as
before. Held in a context variable, so it follows a job through every ``await``
without being passed to anything. ``app/api/DESIGN.md`` §8.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_current: ContextVar[Trace | None] = ContextVar("trace", default=None)


@dataclass
class Trace:
    job_id: str | None = None
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    statements: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def collecting(job_id: str | None = None) -> Iterator[Trace]:
    """Collect for the code run inside this block -- one job."""
    trace = Trace(job_id=job_id)
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


def job_id() -> str | None:
    trace = _current.get()
    return trace.job_id if trace else None


def model_call(
    stage: str, model: str, prompt: str, reply: str | None, seconds: float, error: str | None
) -> None:
    """One model call, whole: the prompt as sent, the reply as it came back --
    including a truncated one, which is the one most worth reading."""
    trace = _current.get()
    if trace is not None:
        trace.model_calls.append(
            {
                "stage": stage,
                "model": model,
                "prompt": prompt,
                "reply": reply,
                "seconds": round(seconds, 3),
                "error": error,
            }
        )


def statement(sql: str, *, verdict: dict | None = None, error: str | None = None) -> None:
    """One SQL statement: what ``validate`` or the database made of it."""
    trace = _current.get()
    if trace is not None:
        trace.statements.append({"sql": sql, "verdict": verdict, "error": error})
