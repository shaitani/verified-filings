"""A job's status: one list, read by the API models, the ORM column and its CHECK.

Text in the database, not a PostgreSQL enum (``app/db/DESIGN.md`` §3.11), but
never a free string in code: every writer goes through these values.
"""

from __future__ import annotations

from typing import Literal, get_args

#: In order. "queued" is real: model calls go through a one-at-a-time gate.
JobStage = Literal["queued", "parsing", "mapping", "fetching", "presenting"]
JobStatus = Literal["queued", "parsing", "mapping", "fetching", "presenting", "done", "failed"]

JOB_STATUSES: tuple[str, ...] = get_args(JobStatus)
FINISHED: frozenset[str] = frozenset({"done", "failed"})  # a job in one of these never moves again
