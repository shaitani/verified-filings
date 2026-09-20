# Bootstrap — bringing everything up from nothing

For a fresh machine, a fresh checkout, or after `docker compose down -v`.

The pieces that make this project work live in four different places, and only
one of them is inside the database volume. Wiping the volume therefore loses
more than the data, and rebuilding it is not a single command. This file is the
order.

---

## What a volume wipe actually destroys

`docker compose down -v` removes `pgdata`, `pgdata-test` **and**
`ollama-models`.

| | survives | why |
|---|---|---|
| schema, tables, the `reported_fact` view | ✅ rebuilt | they are Alembic migrations |
| the `vector` extension | ✅ rebuilt | `CREATE EXTENSION` is in migration `00b08d07eff8` |
| `vf_query_mapper_role` / `vf_retrieval_role` | ❌ gone | cluster objects, deliberately not migrations |
| the ~174,000 facts | ❌ gone | table data |
| concept embeddings | ❌ gone | table data |
| `nomic-embed-text` | ❌ gone | `ollama-models` is a volume too |
| `data/xbrl/*.json` | ✅ untouched | files on disk, not in any volume |
| `.env` | ✅ untouched | a file on disk (but git-ignored — see §3) |

The last two matter more than they look. Because the 20 curated JSON files
survive, **restoring the data is a reload, not a re-fetch**: nothing has to go
back to the SEC, and the corpus cannot drift underneath you while you rebuild.
And because `.env` survives, the role passwords do too.

---

## 1. The order, and why it is not negotiable

```bash
docker compose up -d
```

```bash
uv run alembic upgrade head
```

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/verified_filings_test uv run alembic upgrade head
```

```bash
uv run python -m app.db.roles
```

```bash
uv run python -m app.db.loader AAPL AMD AMZN BAC COST CVX GOOGL INTC JNJ JPM META MSFT MU NTGR NVDA ORCL QCOM TMUS TSLA UNH
```

```bash
uv run python -m app.db.embedder
```

(PowerShell, for step 3: `$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/verified_filings_test"; uv run alembic upgrade head`, then clear it again.)

**Roles cannot come before migrations.** Provisioning grants `USAGE ON SCHEMA
xbrl` and `SELECT` on named relations, so running step 4 first fails:

```
sqlalchemy.exc.DBAPIError: schema "xbrl" does not exist
[SQL: GRANT USAGE ON SCHEMA xbrl TO vf_query_mapper_role]
```

**Embedding cannot come before loading.** `app/db/embedder.py` embeds
`Concept` rows, and the load is what creates them.

Steps 2 and 3 are separate because the test database is a **separate container
on a separate volume** (`db-test`, port 5433). Nothing migrates it
automatically — see `ALEMBIC.md` step 5. Skipping it means `tests/` fails
against a schema that does not exist, which reads as a broken test suite
rather than a missing step.

There is no `ollama pull` step: `docker compose up -d` runs the `ollama-pull`
service, which fetches the model into the `ollama` service's volume and exits.

---

## 2. Confirming it landed

Each check fails loudly rather than returning something plausible-looking.

```bash
docker compose ps --format "table {{.Service}}\t{{.Status}}"
```

`db`, `db-test` and `ollama` must all say `(healthy)`. `ollama-pull` is a
one-shot and is absent once it has exited — that is success, not a failure.

```bash
uv run alembic check
```

Expected: `No new upgrade operations detected.`

```bash
uv run python -m app.db.roles --check
```

`vf_retrieval_role` must list **`reported_fact` and nothing else**. If it also
lists `fact` or `filing`, the narrowing in `app/db/roles.py` did not take, and
generated SQL can reach around the view (`app/retrieval/DESIGN.md` §6).

```bash
docker compose exec ollama ollama list
```

`nomic-embed-text` must be present.

```bash
uv run pytest -q
```

324 passing. This is the real end-to-end check: the suite provisions roles
against the test database, asserts the view's exact column list, and runs a
validated statement through it.

---

## 3. A fresh checkout also needs `.env`

`.env` is git-ignored, so a clone has none and `app/config.py` will refuse to
start. Five variables, all local:

```
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/verified_filings
DATABASE_URL_TEST=postgresql+asyncpg://postgres:postgres@localhost:5433/verified_filings_test
DATABASE_URL_QUERY_MAPPER=postgresql+asyncpg://vf_query_mapper_role:<password>@localhost:5432/verified_filings
DATABASE_URL_RETRIEVAL=postgresql+asyncpg://vf_retrieval_role:<password>@localhost:5432/verified_filings
EMBEDDING_URL=http://localhost:11434
```

The two role passwords are yours to choose — **`app/db/roles.py` reads them
back out of these URLs** and provisions exactly those, rather than taking a
separate variable, so the two cannot drift. A password provisioned that nothing
connects with is a failure that looks like success.

If `data/xbrl/` is also empty (a genuinely fresh clone — it is git-ignored),
the corpus has to be fetched before step 5:

```bash
uv run sec-retriever get-xbrl AAPL AMD AMZN BAC COST CVX GOOGL INTC JNJ JPM META MSFT MU NTGR NVDA ORCL QCOM TMUS TSLA UNH
```

That one does talk to the SEC, and is rate-limited accordingly.

---

## 4. What is deliberately *not* automated

**The roles are not an Alembic migration.** A role is a cluster object that
outlives any one database, autogenerate cannot see it, and its password has no
business in version control. `app/db/roles.py` says so at the top.

**None of this is a Postgres init script.** `/docker-entrypoint-initdb.d/` is
the obvious-looking home for it and it does not work, for two independent
reasons:

- Init scripts run **before** anything else connects, so the schema Alembic
  creates does not exist yet — the grants in step 4 have nothing to grant on.
  This is the same failure quoted in §1, and no ordering in `docker-compose.yml`
  can fix it, because the init hook is by definition the earliest thing.
- They run **only on the first initialisation of an empty data directory**. A
  script there is silently skipped on every existing volume, so any drift
  between it and the migrations would go unnoticed for as long as you happen
  not to wipe.

The schema and the view stay in migrations, where they are idempotent,
re-runnable and versioned. What `docker-compose.yml` *does* carry is the part
that genuinely belongs to the container lifecycle: health checks, and the model
pull.
