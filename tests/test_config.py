"""DATABASE_HOST: every database URL pointed at the container's host, its login kept."""

from __future__ import annotations

from app.config import Settings, _on_host


def test_the_host_changes_and_the_login_does_not() -> None:
    url = "postgresql+asyncpg://vf_web_role:p@ss:w0rd@localhost:5432/verified_filings"
    assert _on_host(url, "db:5432") == (
        "postgresql+asyncpg://vf_web_role:p@ss:w0rd@db:5432/verified_filings"
    )


def test_a_url_without_a_login_keeps_none() -> None:
    assert _on_host("http://localhost:11434", "ollama:11434") == "http://ollama:11434"


def test_every_database_url_is_pointed_at_the_container_host() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://vf_web_role:a@localhost:5432/v",
        database_url_web="postgresql+asyncpg://vf_web_role:a@localhost:5432/v",
        database_url_query_mapper="postgresql+asyncpg://vf_query_mapper_role:b@localhost:5432/v",
        database_url_retrieval=None,  # unset stays unset
        embedding_url="http://ollama:11434",
        database_host="db:5432",
    )
    assert (
        settings.database_url_query_mapper
        == "postgresql+asyncpg://vf_query_mapper_role:b@db:5432/v"
    )
    assert settings.database_url_web.endswith("@db:5432/v")
    assert settings.database_url_retrieval is None


def test_without_a_host_nothing_changes() -> None:
    url = "postgresql+asyncpg://u:p@localhost:5432/v"
    settings = Settings(_env_file=None, database_url=url, embedding_url="http://x")
    assert settings.database_url == url
