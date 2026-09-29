"""The Web Client's test data is what the server sends today (tests/web_replies.py)."""

from __future__ import annotations

import pytest

from tests.web_replies import ANSWERS_FILE, REPLIES_FILE, answers, replies


@pytest.mark.parametrize(("path", "content"), [(REPLIES_FILE, replies), (ANSWERS_FILE, answers)])
def test_the_clients_test_data_is_current(path, content) -> None:
    assert path.read_text(encoding="utf-8") == content(), (
        f"{path.name} is out of date: run `uv run python -m tests.web_replies`"
    )
