"""App configuration, loaded once from the environment / ``.env`` file."""

from pathlib import Path

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
    #: How long a sign-in lasts, from signing in (DESIGN §12a). Shorten before deploying.
    auth_session_days: int = 30
    #: Secure cookies: sent over https only -- browsers treat localhost as secure.
    auth_cookie_secure: bool = True


settings = Settings()
