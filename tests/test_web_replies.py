"""The Web Client's test replies are what the chain sends today (tests/web_replies.py)."""

from __future__ import annotations

from tests.web_replies import REPLIES_FILE, replies


def test_the_clients_test_replies_are_current() -> None:
    assert REPLIES_FILE.read_text(encoding="utf-8") == replies(), (
        "web/src/app/conversation/fixtures/replies.json is out of date: "
        "run `uv run python -m tests.web_replies`"
    )
