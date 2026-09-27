"""Alembic environment.

Wired to this project:
* connection URL comes from ``app.config.settings.database_url`` (i.e. ``.env``),
  not from ``alembic.ini``;
* ``target_metadata`` is both ORM metadatas -- ``app/db/models.py`` (the ``xbrl``
  schema) and ``app/db/web.py`` (the ``web`` schema) -- so ``--autogenerate``
  diffs against both;
* autogenerate is restricted to those two schemas; Alembic's own
  ``alembic_version`` table is kept in ``public``.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import settings
from app.db import Base
from app.db.web import WebBase

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL from .env, not alembic.ini.
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = [Base.metadata, WebBase.metadata]


def include_name(name, type_, parent_names):
    """Filter for --autogenerate: which reflected DB objects to compare against
    the models. Only the ``xbrl`` and ``web`` schemas are ours; Alembic's own
    ``alembic_version`` bookkeeping table (in ``public``) must be ignored, or
    every autogenerate run proposes dropping it."""
    if type_ == "schema":
        return name in (None, "xbrl", "web")
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def _common_config() -> dict:
    return {
        "target_metadata": target_metadata,
        "include_schemas": True,
        "include_name": include_name,
        "version_table_schema": "public",
        "compare_type": True,
    }


def run_migrations_offline() -> None:
    """'offline' mode -- emit SQL to stdout, no live database connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_common_config(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_common_config())
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """'online' mode -- create an async Engine and run migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
