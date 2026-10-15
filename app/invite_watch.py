"""Say when an invite code is spent in production: a Windows notification that
stays on screen until dismissed, and a push to the phone through ntfy.sh.

    uv run python -m app.invite_watch --setup         # once: app name and phone topic
    uv run python -m app.invite_watch                 # one check (Task Scheduler runs this)
    uv run python -m app.invite_watch --test windows  # a test notification: windows|phone|both

One check per run; Task Scheduler supplies the every-15-minutes. Nothing is
long-running and nothing here holds a connection open.

Reading production
------------------
``psql`` inside the production database container, as ``vf_invite_watch_role``
-- three columns of ``web.invite``, read-only, every write refused by its
grants (``app/db/roles.py``). No port is opened: production's database
publishes none, and inside the container ``psql`` connects over its local
socket, which needs no password.

An invite counts as spent when it names its account (``used_by``). One whose
``used_at`` is set without an account is either mid sign-up -- the API claims
the code first and releases it if the account is not made -- or spent by an
account since deleted. Waiting ``_CLAIM_GRACE`` tells the two apart without a
false alarm.

What is remembered
------------------
``data/invite_watch/`` (git-ignored): ``seen.json``, the invites already
reported; ``ntfy_topic``, the phone's topic, which is the only secret ntfy.sh
has; ``log.txt``. The first check records what is already spent without
alerting. An invite is marked seen once either channel delivered, so if both
fail the next check tries again.

If the database cannot be read, that is said once -- by the same two channels
-- not every 15 minutes; a later successful read clears it. Silence must mean
nothing was spent, not that the watcher stopped seeing.

What leaves this PC
-------------------
Only the push to ntfy.sh, and it is generic: how many codes were used and
when. Never a code, an email or an id.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
STATE_DIR = ROOT / "data" / "invite_watch"
SEEN_FILE = STATE_DIR / "seen.json"
TOPIC_FILE = STATE_DIR / "ntfy_topic"
LOG_FILE = STATE_DIR / "log.txt"

ROLE = "vf_invite_watch_role"  # app/db/roles.py INVITE_WATCH
DATABASE = "verified_filings"

#: Longer than any sign-up takes between claiming a code and making the account.
_CLAIM_GRACE = "10 minutes"

SPENT_QUERY = (
    "SELECT id, used_at FROM invite "
    f"WHERE used_by IS NOT NULL OR used_at < now() - interval '{_CLAIM_GRACE}' "
    "ORDER BY used_at"
)

#: The Windows identity notifications are sent under. Its own, so Focus Assist's
#: priority list can let this one app through and nothing else.
APP_ID = "VerifiedFilings.InviteWatch"
APP_NAME = "Invite Watch"
APP_KEY = rf"Software\Classes\AppUserModelId\{APP_ID}"

NTFY_URL = "https://ntfy.sh"

#: No console window flashing up every 15 minutes for docker or PowerShell.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ReadFailed(Exception):
    """Production's invites could not be read."""


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def read_spent() -> dict[str, str]:
    """Every spent invite in production: id -> used_at."""
    command = [
        "docker", "compose", "-f", str(PROD_COMPOSE), "exec", "-T", "db",
        "psql", "-X", "-q", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1",
        "-U", ROLE, "-d", DATABASE, "-c", SPENT_QUERY,
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReadFailed(str(error)) from error
    if done.returncode != 0:
        raise ReadFailed((done.stderr or done.stdout).strip() or f"exit {done.returncode}")
    return parse_rows(done.stdout)


def parse_rows(output: str) -> dict[str, str]:
    rows = {}
    for line in output.splitlines():
        if line.strip():
            invite_id, _, used_at = line.partition("|")
            rows[invite_id.strip()] = used_at.strip()
    return rows


# --------------------------------------------------------------------------- #
# What to say
# --------------------------------------------------------------------------- #


def spent_message(new: dict[str, str]) -> tuple[str, str]:
    """Title and body for newly spent invites. Generic: it goes to ntfy.sh."""
    count = len(new)
    title = "Invite code used" if count == 1 else f"{count} invite codes used"
    latest = max(new.values())
    return title, f"Someone signed up to verified-filings. Last used: {_local(latest)}."


def _local(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%a %d %b %H:%M")
    except ValueError:
        return timestamp


UNREACHABLE = (
    "Invite watch is blind",
    "It cannot read the production database, so a used invite would go unnoticed. "
    "Said once; a later successful check clears it.",
)


# --------------------------------------------------------------------------- #
# Telling
# --------------------------------------------------------------------------- #


def notify_windows(title: str, body: str) -> bool:
    """A reminder-style notification under APP_ID: it stays on screen until
    dismissed, rather than sliding into the Action Center after a few seconds.
    Windows only shows a reminder as one when it has a button, hence Dismiss."""
    toast = (
        '<toast scenario="reminder">'
        '<visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        "</binding></visual>"
        '<actions><action content="Dismiss" arguments="dismiss" activationType="system"/>'
        "</actions></toast>"
    )
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] > $null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml({_ps_quote(toast)})\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier({_ps_quote(APP_ID)}).Show($toast)\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"windows notification failed: {error}")
        return False
    if done.returncode != 0:
        log(f"windows notification failed: {done.stderr.strip()}")
        return False
    return True


def _ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal: nothing in it is expanded."""
    return "'" + value.replace("'", "''") + "'"


def notify_phone(title: str, body: str) -> bool:
    if not TOPIC_FILE.exists():
        log("phone push skipped: no topic -- run with --setup")
        return False
    topic = TOPIC_FILE.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"{NTFY_URL}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": "urgent", "Tags": "key"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return 200 <= response.status < 300
    except OSError as error:  # URLError and HTTPError are OSErrors
        log(f"phone push failed: {error}")
        return False


def notify(title: str, body: str) -> bool:
    """Both channels, each whatever the other did. True if either delivered."""
    on_windows = notify_windows(title, body)
    on_phone = notify_phone(title, body)
    return on_windows or on_phone


# --------------------------------------------------------------------------- #
# Remembering
# --------------------------------------------------------------------------- #


def load_state() -> dict | None:
    """None before the first check."""
    if not SEEN_FILE.exists():
        return None
    return json.loads(SEEN_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")


def log(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8", newline="\n") as file:
        file.write(f"{stamp}  {line}\n")
    print(line)


# --------------------------------------------------------------------------- #
# One check
# --------------------------------------------------------------------------- #


def check() -> int:
    state = load_state()
    try:
        spent = read_spent()
    except ReadFailed as error:
        log(f"cannot read production: {error}")
        if state is not None and not state.get("unreachable"):
            if notify(*UNREACHABLE):
                state["unreachable"] = True
                save_state(state)
        return 1

    if state is None:  # the first check: what is spent already is not news
        save_state({"seen": sorted(spent), "unreachable": False})
        log(f"first check: {len(spent)} invite(s) already spent, recorded without alerting")
        return 0

    if state.get("unreachable"):
        log("reading production again")
        state["unreachable"] = False

    seen = set(state["seen"])
    new = {invite: used for invite, used in spent.items() if invite not in seen}
    if new:
        if notify(*spent_message(new)):
            seen |= set(new)
            log(f"alerted: {len(new)} newly spent invite(s)")
        else:
            log(f"{len(new)} newly spent invite(s), but no channel delivered; will retry")
    else:
        log(f"no change: {len(spent)} spent")
    state["seen"] = sorted(seen)
    save_state(state)
    return 0


# --------------------------------------------------------------------------- #
# Setup and testing
# --------------------------------------------------------------------------- #


def setup() -> int:
    """Register APP_ID's display name for this Windows user, and make the phone
    topic. Re-running keeps an existing topic: the phone is subscribed to it."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
    print(rf"registered {APP_NAME!r} as {APP_ID} (HKCU\{APP_KEY})")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if TOPIC_FILE.exists():
        print(f"phone topic kept: {TOPIC_FILE}")
    else:
        # The topic's name is the only thing keeping others from reading or
        # posting to it on ntfy.sh, so it is long and random.
        topic = "vf-invites-" + secrets.token_urlsafe(24)
        TOPIC_FILE.write_text(topic + "\n", encoding="utf-8", newline="\n")
        print(f"phone topic made: {TOPIC_FILE}")
    return 0


def test(channel: str) -> int:
    title, body = "Invite watch test", "If you can read this, this channel works."
    results = {}
    if channel in ("windows", "both"):
        results["windows"] = notify_windows(title, body)
    if channel in ("phone", "both"):
        results["phone"] = notify_phone(title, body)
    for name, delivered in results.items():
        print(f"{name}: {'sent' if delivered else 'FAILED (see log)'}")
    return 0 if all(results.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alert when a production invite code is spent.")
    parser.add_argument("--setup", action="store_true", help="register the app, make the topic")
    parser.add_argument("--test", choices=("windows", "phone", "both"), help="send a test alert")
    args = parser.parse_args(argv)
    if args.setup:
        return setup()
    if args.test:
        return test(args.test)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
