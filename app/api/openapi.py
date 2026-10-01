"""Write the Web Server's contract to ``web/openapi.json`` -- what the Web Client's
TypeScript types are generated from (DESIGN §9).

    uv run python -m app.api.openapi

``tests/test_openapi.py`` fails while the file is out of date, so a change to a
model cannot reach the client unnoticed; ``npm run api:types`` in ``web/`` then
regenerates the types from it.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.api.auth import AuthConfig
from app.api.server import create_app

SPEC_FILE = Path(__file__).resolve().parents[2] / "web" / "openapi.json"

#: Only to describe the routes: the app is built, never started, so these sign
#: nothing. GitHub is on so its routes are always in the contract, whatever this
#: machine's .env says.
_DESCRIBE_ONLY = AuthConfig(
    reset_secret="describe-only",
    verify_secret="describe-only",
    oauth_state_secret="describe-only",
    device_secret="describe-only",
    github_client_id="describe-only",
    github_client_secret="describe-only",
)


def contract() -> str:
    """The contract as the file holds it: indented, ending in one newline."""
    spec = create_app(_DESCRIBE_ONLY).openapi()
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    SPEC_FILE.write_text(contract(), encoding="utf-8", newline="\n")
    print(f"wrote {SPEC_FILE}")


if __name__ == "__main__":
    main()
