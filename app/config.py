"""App configuration, loaded once from the environment / ``.env`` file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"  # repo root, CWD-independent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")
    database_url: str
    embedding_url: str

    #: Read-only login, used by everything that only ever SELECTs -- the query
    #: mapper today, the SQL emitter's execution step once it exists. Optional
    #: so a checkout without the role provisioned still runs: `app/db/session.py`
    #: falls back to `database_url` and says so. Create the role with
    #: `uv run python -m app.db.roles`.
    database_url_readonly: str | None = None


settings = Settings()
