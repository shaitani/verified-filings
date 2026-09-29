"""App configuration, loaded once from the environment / ``.env`` file."""

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"  # repo root, CWD-independent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")
    database_url: str
    embedding_url: str

    #: Read-only logins, one per read-only consumer. Optional so a checkout
    #: without the roles provisioned still runs: `app/db/session.py` falls back
    #: to `database_url` and warns. Create them with
    #: `uv run python -m app.db.roles`.
    database_url_query_mapper: str | None = None
    database_url_retrieval: str | None = None

    #: The Web Server's login, the one role that writes (in `web` only). Set it
    #: after the `web` migration: its grants name those tables.
    database_url_web: str | None = None

    #: Sign-in (app/api/DESIGN.md §10). The three secrets sign tokens -- reset,
    #: verification, the GitHub sign-in's state -- and the server will not start
    #: without them. GitHub is optional: without both values it is not offered.
    auth_reset_secret: str | None = None
    auth_verify_secret: str | None = None
    auth_oauth_state_secret: str | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    #: Where GitHub sends the browser back. One of the app's registered callbacks.
    github_oauth_redirect_url: str = "http://localhost:8000/api/auth/github/callback"
    #: How long a sign-in lasts, from signing in (app/api/DESIGN.md §10). Shorten before deploying.
    auth_session_days: int = 30
    #: Secure cookies: sent over https only -- browsers treat localhost as secure.
    auth_cookie_secure: bool = True

    #: In a container: the database's ``host:port`` there ("db:5432"). Every
    #: database URL is pointed at it, keeping its login -- so the URLs, and the
    #: passwords in them, are written once in .env, not again in docker-compose.
    database_host: str | None = None

    @model_validator(mode="after")
    def _point_at_database_host(self) -> "Settings":
        if self.database_host:
            for name in (
                "database_url",
                "database_url_query_mapper",
                "database_url_retrieval",
                "database_url_web",
            ):
                url = getattr(self, name)
                if url:
                    setattr(self, name, _on_host(url, self.database_host))
        return self


def _on_host(url: str, host: str) -> str:
    """``url`` with its host and port replaced, its login untouched."""
    parts = urlsplit(url)
    login = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=f"{login}@{host}" if login else host))


settings = Settings()
