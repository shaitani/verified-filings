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


def test_a_production_container_reads_its_secrets_from_files(tmp_path) -> None:
    """docker-compose.prod.yml mounts one file per secret, named after its setting;
    a file written by an editor ends in a newline, which is not part of the value."""
    (tmp_path / "database_url").write_text("postgresql+asyncpg://u:pw@localhost:5432/d\n")
    (tmp_path / "embedding_url").write_text("http://ollama:11434")
    (tmp_path / "auth_device_secret").write_text("d" * 43 + "\n")
    settings = Settings(_env_file=None, _secrets_dir=tmp_path, database_host="db:5432")
    assert settings.auth_device_secret == "d" * 43
    assert settings.database_url == "postgresql+asyncpg://u:pw@db:5432/d"  # still re-hosted


def test_the_production_secrets_are_made_once_and_agree(tmp_path) -> None:
    from app import prod_secrets

    written = prod_secrets.make(tmp_path)
    assert "postgres_password" in written and "auth_device_secret" in written
    password = (tmp_path / "postgres_password").read_text().strip()
    owner = (tmp_path / "database_url_owner").read_text().strip()
    assert owner == f"postgresql+asyncpg://postgres:{password}@db:5432/verified_filings"
    web = (tmp_path / "database_url_web").read_text().strip()
    assert web.startswith("postgresql+asyncpg://vf_web_role:") and password not in web
    assert (tmp_path / "github_oauth_client_id").read_text() == "\n"  # filled in by hand

    before = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert prod_secrets.make(tmp_path) == []  # never overwrites
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == before
