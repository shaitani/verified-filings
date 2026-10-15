"""The invite watcher's decisions: what is news, what is remembered, what is
said once. The database read and both channels are stood in for; the real
read is the role's, tested in test_roles.py."""

from __future__ import annotations

import pytest

from app import invite_watch

A = "00000000-0000-0000-0000-0000000000f1"
B = "00000000-0000-0000-0000-0000000000f2"
USED = "2026-10-14 09:30:00.123+00"


@pytest.fixture
def watch(tmp_path, monkeypatch):
    """The watcher with its state in tmp_path, production as a dict the test
    sets, and every notification recorded instead of sent."""
    monkeypatch.setattr(invite_watch, "STATE_DIR", tmp_path)
    monkeypatch.setattr(invite_watch, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(invite_watch, "LOG_FILE", tmp_path / "log.txt")

    class World:
        spent: dict[str, str] | None = {}  # None: production unreadable
        delivers = True
        sent: list[tuple[str, str]] = []

    world = World()
    world.sent = []

    def read_spent():
        if world.spent is None:
            raise invite_watch.ReadFailed("container not running")
        return dict(world.spent)

    def notify(title, body):
        world.sent.append((title, body))
        return world.delivers

    monkeypatch.setattr(invite_watch, "read_spent", read_spent)
    monkeypatch.setattr(invite_watch, "notify", notify)
    return world


def test_rows_parse_from_psql_output() -> None:
    assert invite_watch.parse_rows(f"{A}|{USED}\n\n{B}|{USED}\n") == {A: USED, B: USED}


def test_the_first_check_records_without_alerting(watch) -> None:
    watch.spent = {A: USED}
    assert invite_watch.check() == 0
    assert watch.sent == []
    assert invite_watch.load_state()["seen"] == [A]


def test_a_newly_spent_invite_alerts_once(watch) -> None:
    invite_watch.check()
    watch.spent = {A: USED}
    invite_watch.check()
    invite_watch.check()
    assert [title for title, _ in watch.sent] == ["Invite code used"]


def test_several_at_once_are_one_alert(watch) -> None:
    invite_watch.check()
    watch.spent = {A: USED, B: USED}
    invite_watch.check()
    assert [title for title, _ in watch.sent] == ["2 invite codes used"]


def test_undelivered_is_retried(watch) -> None:
    invite_watch.check()
    watch.spent, watch.delivers = {A: USED}, False
    invite_watch.check()
    watch.delivers = True
    invite_watch.check()
    assert len(watch.sent) == 2
    assert invite_watch.load_state()["seen"] == [A]


def test_unreachable_is_said_once_and_cleared(watch) -> None:
    invite_watch.check()
    watch.spent = None
    assert invite_watch.check() == 1
    assert invite_watch.check() == 1
    assert watch.sent == [invite_watch.UNREACHABLE]
    watch.spent = {}
    assert invite_watch.check() == 0
    assert invite_watch.load_state()["unreachable"] is False
    watch.spent = None
    invite_watch.check()
    assert len(watch.sent) == 2  # a new outage is news again


def test_the_message_carries_no_identifier(watch) -> None:
    title, body = invite_watch.spent_message({A: USED})
    assert A not in title + body


def test_powershell_quoting_expands_nothing() -> None:
    assert invite_watch._ps_quote("it's $x") == "'it''s $x'"
