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


settings = Settings()
