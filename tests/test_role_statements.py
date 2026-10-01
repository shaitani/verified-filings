"""Every statement ``roles.provision()`` sends, pinned to a file.

``test_roles.py`` proves what the provisioned roles can and cannot do. This
proves that what provisioning *sends* has not changed. A dropped ``REVOKE``
may not fail any behaviour test, and here it cannot slip by.

Three files, each role added later pinned apart from those before it, so
adding it provably took nothing from them: the readers', then everything else
about the ``web`` schema, then everything naming the admin role -- including
the row-security statements, which exist because of it. A deliberate change
regenerates a file and shows up as its diff:

    UPDATE_PINNED_READERS=1 uv run pytest tests/test_role_statements.py  # readers
    UPDATE_PINNED=1 uv run pytest tests/test_role_statements.py          # web
    UPDATE_PINNED_ADMIN=1 uv run pytest tests/test_role_statements.py    # admin
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.db import roles

FIXTURES = Path(__file__).parent / "fixtures"
PINNED_READERS = FIXTURES / "role_statements.sql"  # the readers, as before web existed
PINNED_WEB = FIXTURES / "role_statements_web.sql"
PINNED_ADMIN = FIXTURES / "role_statements_admin.sql"

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


def _about_admin(statement: str) -> bool:
    return roles.ADMIN.name in statement


def _about_web(statement: str) -> bool:
    if _about_admin(statement):
        return False
    return roles.WEB.name in statement or re.search(r"\bweb\b", statement) is not None


def _check(statements: list[str], pinned: Path, update_flag: str) -> None:
    sent = SEPARATOR.join(statements) + "\n"
    if os.environ.get(update_flag):
        pinned.write_text(sent, encoding="utf-8", newline="\n")
    assert pinned.exists(), f"no pinned file; run with {update_flag}=1 to write {pinned.name}"
    assert sent == pinned.read_text(encoding="utf-8"), (
        f"roles.provision() sends different SQL than {pinned.name}. If that is "
        f"deliberate, regenerate with {update_flag}=1 and review the file's diff."
    )


async def test_the_readers_statements_are_unchanged(monkeypatch) -> None:
    # Everything about web removed, what is left must be byte-identical to the
    # file pinned before web existed: the readers lost nothing.
    sent = await _sent_by_provision(monkeypatch)
    readers = [s for s in sent if not _about_web(s) and not _about_admin(s)]
    _check(readers, PINNED_READERS, "UPDATE_PINNED_READERS")


async def test_the_web_statements_are_pinned(monkeypatch) -> None:
    sent = await _sent_by_provision(monkeypatch)
    _check([s for s in sent if _about_web(s)], PINNED_WEB, "UPDATE_PINNED")


async def test_the_admin_statements_are_pinned(monkeypatch) -> None:
    sent = await _sent_by_provision(monkeypatch)
    _check([s for s in sent if _about_admin(s)], PINNED_ADMIN, "UPDATE_PINNED_ADMIN")
