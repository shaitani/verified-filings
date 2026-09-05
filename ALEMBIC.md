# Alembic (database migrations)

Alembic turns the SQLAlchemy models in `app/db/models.py` into real PostgreSQL
tables, and keeps the database in step as those models change over time.

Run every `alembic` command **from the repository root**.

---

## Part 1 — How it was set up (reference only, already done)

### Pieces and where they live

| Path | What it is |
|---|---|
| `alembic.ini` (repo root) | Alembic's config file. Points at the migrations folder; the database URL is **not** here. |
| `app/db/migrations/env.py` | Run at the start of every Alembic command. Wires Alembic to this project. |
| `app/db/migrations/versions/` | One Python file per migration. `d1ec76ee2152_initial_xbrl_schema.py` is the first. |
| `app/config.py` | Reads `DATABASE_URL` from `.env` into `settings.database_url`. |
| `.env` (repo root, git-ignored) | Holds `DATABASE_URL` (the real database) and `DATABASE_URL_TEST` (used only by `tests/`). |
| `docker-compose.yml` | Two Postgres containers: `db` (port 5432, real data) and `db-test` (port 5433, used only by `tests/`) — separate server, separate volume, fully isolated. |

### What was done, in order

1. Added the dependency: `uv add alembic` (the database driver `asyncpg` and
   `pydantic-settings` were already present).
2. Scaffolded with the **async** template:
   `uv run alembic init -t async app/db/migrations`.
3. Edited `alembic.ini`:
   - `prepend_sys_path = %(here)s` — so `import app` works from any directory.
   - removed the `sqlalchemy.url` line — the URL comes from `.env` instead.
4. Edited `app/db/migrations/env.py`:
   - reads the URL from `app.config.settings.database_url`;
   - `target_metadata = Base.metadata` — the models it compares against;
   - only looks at the `xbrl` schema when comparing, and ignores Alembic's own
     `alembic_version` table.
5. Told `ruff` to skip generated migration code
   (`extend-exclude = ["app/db/migrations"]` in `pyproject.toml`).
6. Generated the first migration:
   `uv run alembic revision --autogenerate -m "initial xbrl schema"`.
7. Hand-corrected that migration (see the known gaps in Part 2), then applied it:
   `uv run alembic upgrade head`.

### The database URL

`app/config.py` reads `DATABASE_URL` from `.env`; the app and Alembic both
connect with it. Anatomy:

```
postgresql+asyncpg://postgres:postgres@localhost:5432/verified_filings
└───────┬────────┘   └──┬───┘ └──┬───┘ └───┬───┘ └┬─┘ └──────┬───────┘
   dialect+driver      user    password    host  port     database
```

- **`postgresql+asyncpg`** — use SQLAlchemy's PostgreSQL support, driven by the
  `asyncpg` library.
- **user / password / database** come from `docker-compose.yml`:
  `postgres` is the Postgres image's fixed default superuser; the password is
  `POSTGRES_PASSWORD`; the database is `POSTGRES_DB`.
- **host / port** come from the `ports: ["5432:5432"]` line in
  `docker-compose.yml`.

To change any of these: edit `docker-compose.yml`, recreate the container
(`docker compose down && docker compose up -d`), then update `.env` to match.

### Other facts worth knowing

- Every table and enum type lives in a dedicated PostgreSQL schema called `xbrl`.
- Alembic's own bookkeeping table, `alembic_version`, lives in `public` and
  holds one row: the ID of the migration currently applied.
- The database runs in Docker (`docker compose up -d`), reachable at
  `localhost:5432`.
- It connects as the `postgres` superuser, which is why migrations can
  `CREATE SCHEMA` / `CREATE TYPE` without permission errors.

---

## Part 2 — Running Alembic again (the normal workflow)

### When you need a migration

Any change to `app/db/models.py` that changes the **database structure**:

- adding or removing a table
- adding, removing, or renaming a column
- changing a column's type, nullability, or default
- adding, changing, or removing an index or constraint
- adding a value to an enum (`taxonomy`, `filing_form`, `fiscal_period`)

### When you do **not** need one

- editing comments or docstrings in `models.py`
- changes to `app/schemas/`, the loader, or any non-`models.py` code
- `relationship()` changes that don't change a foreign key

### The steps

**1. Make sure the database is running.**

```
docker compose up -d
```

**2. Generate the migration file.** This only writes a file — it does not touch
the database.

```
uv run alembic revision --autogenerate -m "short description of the change"
```

A new file appears in `app/db/migrations/versions/`.

**3. Open that file and read it.** Autogenerate is not fully reliable for this
schema. Check and fix by hand:

| Thing | What to do |
|---|---|
| **Expression indexes** (e.g. `uq_fact_natural`, which uses `coalesce(...)`) | Autogenerate may show a spurious drop-and-recreate. If the index did not actually change, delete those lines. |
| **New enum types** | Make sure `downgrade()` removes them: `op.execute("DROP TYPE IF EXISTS xbrl.<name>")`. |
| **New enum values** | Autogenerate cannot add them. Add by hand: `op.execute("ALTER TYPE xbrl.<name> ADD VALUE '<new value>'")`. Note this statement cannot run inside a transaction. |
| **The `xbrl` schema** | Only the first migration created it. Later migrations assume it already exists. |

Make sure both `upgrade()` and `downgrade()` are correct.

**4. Apply it to the database.**

```
uv run alembic upgrade head
```

**5. Apply the same migration to the test database.** The test suite
(`tests/`) runs against a **separate PostgreSQL container** (`db-test`, port
5433), which is not touched automatically — only do this when the schema
actually changed:

```
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/verified_filings_test uv run alembic upgrade head
```

(PowerShell: `$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/verified_filings_test"; uv run alembic upgrade head`)

This works because `app/config.py` reads whichever `DATABASE_URL` is in the
environment at the time — a real environment variable always wins over the one
in `.env`, so this one command temporarily points Alembic at the test database
without changing any file. (It has to be the literal URL, not `$DATABASE_URL_TEST`
— that variable only exists inside `.env`, which Python reads directly; your
shell has never seen it.)

**6. Confirm the (real) database matches the models.**

```
uv run alembic check
```

Expected output: `No new upgrade operations detected.`

**7. Commit** the new migration file together with the `models.py` change, in
the same commit.

### Handy commands

| Command | Purpose |
|---|---|
| `uv run alembic current` | Which migration is currently applied |
| `uv run alembic history` | List all migrations, oldest to newest |
| `uv run alembic upgrade head --sql` | Print the SQL a migration would run, without running it |
| `uv run alembic downgrade -1` | Undo the most recent migration |
| `uv run alembic downgrade base` | Undo every migration (empties the `xbrl` schema) |

### Rules

- Run every `alembic` command from the repository root.
- One migration per logical change.
- **Never edit a migration that has already been applied to a shared database.**
  Write a new migration instead.
