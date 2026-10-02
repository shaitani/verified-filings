"""Make the production secret files that docker-compose.prod.yml mounts.

    uv run python -m app.prod_secrets

One file per secret in ``secrets/`` at the repository root -- git-ignored and
kept out of every image. Each container sees only the files compose gives it,
at ``/run/secrets/<setting>`` (``app/config.py`` reads them there). Files, not
environment variables, so ``docker inspect`` shows none of them.

**Never overwrites.** A file that exists is kept, so running this again only
fills in what is missing -- the database's passwords are fixed once it holds
data. A new password goes in by deleting its file and running this again, then
re-provisioning (docs/STARTUP.md). Values are printed nowhere.

The GitHub pair is written empty: GitHub sign-in stays off until the
production OAuth app exists and its id and secret are pasted in.
"""

from __future__ import annotations

import secrets
from pathlib import Path

SECRETS = Path(__file__).resolve().parent.parent / "secrets"

#: The production database, as the containers reach it on the compose network.
HOST, DATABASE = "db:5432", "verified_filings"

#: Each login and the file its URL goes in. The owner's is for the ops container
#: alone (migrations, provisioning, administration); the Web Server never sees it.
LOGINS = {
    "database_url_owner": "postgres",
    "database_url_web": "vf_web_role",
    "database_url_admin": "vf_admin_role",
    "database_url_query_mapper": "vf_query_mapper_role",
    "database_url_retrieval": "vf_retrieval_role",
}

#: Signing secrets, each its own (app/api/auth.py SECRETS).
SIGNING = (
    "auth_reset_secret",
    "auth_verify_secret",
    "auth_oauth_state_secret",
    "auth_device_secret",
)

#: Filled in by hand once the production GitHub OAuth app exists.
BY_HAND = ("github_oauth_client_id", "github_oauth_client_secret")


def _url(user: str, password: str) -> str:
    return f"postgresql+asyncpg://{user}:{password}@{HOST}/{DATABASE}"


def make(folder: Path = SECRETS) -> list[str]:
    """Write every missing file; return the names written."""
    folder.mkdir(exist_ok=True)
    wanted: dict[str, str] = {}
    owner_password = secrets.token_urlsafe(24)
    wanted["postgres_password"] = owner_password  # the server's own superuser
    for name, user in LOGINS.items():
        password = owner_password if user == "postgres" else secrets.token_urlsafe(24)
        wanted[name] = _url(user, password)
    wanted |= {name: secrets.token_urlsafe(32) for name in SIGNING}
    wanted |= {name: "" for name in BY_HAND}

    if (folder / "postgres_password").exists() != (folder / "database_url_owner").exists():
        raise SystemExit(
            "secrets/postgres_password and secrets/database_url_owner must be made "
            "together: delete both, or neither."
        )
    written = []
    for name, value in wanted.items():
        path = folder / name
        if path.exists():
            continue
        path.write_text(value + "\n", encoding="utf-8", newline="\n")
        written.append(name)
    return written


def main() -> None:
    written = make()
    if written:
        print(f"wrote {len(written)} file(s) in {SECRETS}: {', '.join(written)}")
    else:
        print(f"nothing to do: every file in {SECRETS} already exists")


if __name__ == "__main__":
    main()
