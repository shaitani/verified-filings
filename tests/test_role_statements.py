"""Every statement ``roles.provision()`` sends, pinned to a file.

``test_roles.py`` proves what the provisioned roles can and cannot do. This
proves that what provisioning *sends* has not changed. A dropped ``REVOKE``
may not fail any behaviour test, and here it cannot slip by.

A deliberate change regenerates the file and shows up as its diff:

    UPDATE_PINNED=1 uv run pytest tests/test_role_statements.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.db import roles

PINNED = Path(__file__).parent / "fixtures" / "role_statements.sql"

URL = "postgresql+asyncpg://owner:secret@host:5432/verified_filings"
PASSWORDS = {spec.name: f"{spec.name}-pw" for spec in roles.ROLES}  # every role provisioned
SEPARATOR = "\n-- ---------------------------------------------------------------\n"


class _RecordingConnection:
    def __init__(self, sent: list[str]) -> None:
        self._sent = sent

    async def __aenter__(self) -> _RecordingConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: object) -> None:
        self._sent.append(str(statement))  # the SQL text, exactly as it would be sent


class _RecordingEngine:
    def __init__(self, sent: list[str]) -> None:
        self._sent = sent

    def connect(self) -> _RecordingConnection:
        return _RecordingConnection(self._sent)

    async def dispose(self) -> None:
        return None


async def _sent_by_provision(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    # Stands in for the database; provision() itself runs unchanged.
    monkeypatch.setattr(roles, "create_async_engine", lambda *a, **k: _RecordingEngine(sent))
    provisioned = await roles.provision(URL, PASSWORDS)
    assert provisioned == [spec.name for spec in roles.ROLES]  # nothing skipped
    return sent


async def test_provisioning_sends_exactly_the_pinned_statements(monkeypatch) -> None:
    sent = SEPARATOR.join(await _sent_by_provision(monkeypatch)) + "\n"
    if os.environ.get("UPDATE_PINNED"):
        PINNED.write_text(sent, encoding="utf-8", newline="\n")
    assert PINNED.exists(), f"no pinned file; run with UPDATE_PINNED=1 to write {PINNED.name}"
    assert sent == PINNED.read_text(encoding="utf-8"), (
        "roles.provision() sends different SQL than the pinned file. If that is "
        "deliberate, regenerate with UPDATE_PINNED=1 and review the file's diff."
    )

