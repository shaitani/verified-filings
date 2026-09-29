# Bootstrap — why startup is in that order, and how to check it

The commands are in [STARTUP.md](STARTUP.md). This is the reasoning behind
them, what a wipe destroys, and a check for each piece when something does
not come up.

---

## What a volume wipe destroys

`docker compose down -v` removes `pgdata`, `pgdata-test` **and**
`ollama-models`.

| | survives | why |
|---|---|---|
| schemas, tables, the `reported_fact` view | ✅ rebuilt | they are Alembic migrations |
| the `vector` extension | ✅ rebuilt | `CREATE EXTENSION` is in migration `00b08d07eff8` |
| the three login roles | ❌ gone | cluster objects, deliberately not migrations |
| the ~174,000 facts, concept embeddings, users, conversations | ❌ gone | table data |
| `nomic-embed-text`, `qwen2.5-coder:7b` | ❌ gone | `ollama-models` is a volume too; pulled again on startup |
| `data/xbrl/*.json` | ✅ untouched | files on disk, not in any volume |
| `.env` | ✅ untouched | a file on disk (git-ignored) |

Because the 20 curated JSON files survive, **restoring the data is a reload,
not a re-fetch**: nothing goes back to the SEC, and the corpus cannot drift
while you rebuild. Because `.env` survives, so do the role passwords.

## Why the order is not negotiable

**Migrations before roles.** Provisioning grants on named schemas and
relations, so it fails on an empty database:

```
sqlalchemy.exc.DBAPIError: schema "xbrl" does not exist
[SQL: GRANT USAGE ON SCHEMA xbrl TO vf_query_mapper_role]
```

The same holds for `web`: `vf_web_role`'s grants name its tables.

**Both databases.** `db-test` is a separate server on a separate volume, and
nothing migrates it automatically. Skip it and the test suite fails against a
schema that does not exist — which reads as a broken suite, not a missing
step. The suite provisions its own roles there.

**Loading before embedding.** The embedder embeds `concept` rows, and the load
creates them.

**No `ollama pull` step.** The `ollama` service pulls both models itself on
startup, in the background, while the server takes over. On a cold start it
reports `starting` for as long as ~5 GB takes, and only then `healthy`.

**The role passwords come from `.env`.** `app/db/roles.py` reads each role's
password out of the URL the application connects with
(`DATABASE_URL_QUERY_MAPPER`, `_RETRIEVAL`, `_WEB`) rather than from a
separate variable, so the two cannot drift. A role with no URL is skipped.

**The Web Server refuses to start** without `DATABASE_URL_WEB` (or with it
naming any login but `vf_web_role` — the owner would undo every grant),
without the three signing secrets, or with a secret under 32 characters.

## Confirming it landed

Each check fails loudly rather than returning something plausible.

```
docker compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Ports}}"
```

`db`, `db-test` and `ollama` must say `(healthy)`. Every published port must
show `127.0.0.1:` and `[::1]:` only — reachable from this machine and nowhere
else. A bare `0.0.0.0:` means the network can reach it. Ollama's health check
asserts both **models are present**, not merely that the server listens.

```
uv run alembic check
```

Expected: `No new upgrade operations detected.`

```
uv run python -m app.db.roles --check
```

- `vf_retrieval_role` lists **`reported_fact` and nothing else**, and `schema
  public: none`. If it also lists `fact` or `filing`, generated SQL can reach
  around the view.
- `vf_web_role` shows `schema xbrl: none`, **no `SELECT` on `web.job_trace` or
  `web.job_feedback`**, no `INSERT` on `web.invite`, and `user` grants naming
  columns without `is_superuser`.

```
docker compose exec ollama ollama list
```

Both models present — for when `ollama` is *not* healthy and you want to see
which one is missing.

```
docker compose exec ollama ollama ps
```

With a model loaded this must say **`100% GPU`**. Less means layers spilled to
the CPU: roughly ten times slower, and otherwise silent.

```
docker compose logs ollama | grep "inference compute"
```

Must name the card (`library=CUDA`). If it says nothing, the container cannot
see the GPU and Ollama has fallen back to the CPU — it starts, answers, and
passes every other check here, slowly. `gpus: all` in `docker-compose.yml` is
what grants it; Docker Desktop supplies the runtime.

```
uv run pytest -q
```

All passing. The real end-to-end check: the suite provisions roles on the test
database, asserts the view's exact columns, and runs a validated statement
through it.

## What is deliberately not automated

**The roles are not a migration.** A role is a cluster object that outlives
any one database, autogenerate cannot see it, and its password has no place in
version control.

**None of this is a Postgres init script.** `/docker-entrypoint-initdb.d/`
looks like the home for it and does not work, for two reasons:

- Init scripts run **before** anything else connects, so the schemas Alembic
  creates do not exist yet and the grants have nothing to grant on. No ordering
  in `docker-compose.yml` can fix that: the init hook is the earliest thing by
  definition.
- They run **only on the first initialisation of an empty data directory**,
  and are silently skipped on every existing volume — so drift between them and
  the migrations would go unnoticed until the next wipe.

The schemas and the view stay in migrations: idempotent, re-runnable,
versioned. `docker-compose.yml` carries what belongs to the container
lifecycle: health checks and the model pull. The pull is folded into the
`ollama` service rather than a one-shot "puller" service, which would leave an
exited container behind after every `up` — and a row of dead containers is how
a real failure stops being noticed.
