# Startup

Run everything from the repository root unless a step says otherwise.
Commands are for PowerShell; where Git Bash differs, both are given.

---

## After a restart

Data, roles and models survive a restart. Only the processes need starting.

1. Start **Docker Desktop**. Wait until it says it is running.

2. Containers:

   ```
   docker compose -f docker-compose.dev.yml up -d
   ```

3. Wait until `db`, `db-test` and `ollama` show `(healthy)`:

   ```
   docker compose -f docker-compose.dev.yml ps
   ```

4. Web Server — in its own terminal, leave it running:

   ```
   uv run uvicorn app.api.server:create_app --factory --host 127.0.0.1 --port 8000 --reload
   ```

5. Web Client — in its own terminal, leave it running:

   ```
   cd web
   npm start
   ```

6. Open http://localhost:4200

Stop: `Ctrl+C` in both terminals, then `docker compose -f docker-compose.dev.yml stop`.

---

## From nothing

A new machine, a fresh clone, or after `docker compose -f docker-compose.dev.yml down -v`.

### Install once

- Docker Desktop (with GPU support: NVIDIA driver installed)
- uv — https://docs.astral.sh/uv/
- Node 24 and npm 11

### 1. Dependencies

```
uv sync --extra dev
```

```
cd web
npm install
cd ..
```

### 2. `.env`

Copy `.env.example` to `.env`. Choose four role passwords and put them in
`DATABASE_URL_QUERY_MAPPER`, `DATABASE_URL_RETRIEVAL`, `DATABASE_URL_WEB` and
`DATABASE_URL_ADMIN` (uncomment the last two). Generate the four signing secrets and paste them in:

```
uv run python -c "import secrets; [print(f'{n}={secrets.token_urlsafe(32)}') for n in ('AUTH_RESET_SECRET', 'AUTH_VERIFY_SECRET', 'AUTH_OAUTH_STATE_SECRET', 'AUTH_DEVICE_SECRET')]"
```

GitHub sign-in is optional: see the comments in `.env.example`.

### 3. Containers

```
docker compose -f docker-compose.dev.yml up -d
```

Wait for `(healthy)` on `db`, `db-test`, `ollama` (`docker compose -f docker-compose.dev.yml ps`). A
cold start downloads ~5 GB of models first.

### 4. Database schema — both databases

```
uv run alembic upgrade head
```

PowerShell:

```
$env:DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/verified_filings_test"; uv run alembic upgrade head; Remove-Item Env:DATABASE_URL
```

Git Bash:

```
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/verified_filings_test uv run alembic upgrade head
```

### 5. Roles

```
uv run python -m app.db.roles
```

### 6. Data

Only if `data/xbrl/` is empty (it is on a fresh clone). Downloads from the SEC:

```
uv run sec-retriever get-xbrl AAPL AMD AMZN BAC COST CVX GOOGL INTC JNJ JPM META MSFT MU NTGR NVDA ORCL QCOM TMUS TSLA UNH
```

Load it:

```
uv run python -m app.db.loader AAPL AMD AMZN BAC COST CVX GOOGL INTC JNJ JPM META MSFT MU NTGR NVDA ORCL QCOM TMUS TSLA UNH
```

Embed it:

```
uv run python -m app.db.embedder
```

### 7. Check

```
uv run pytest -q
```

All must pass.

### 8. Start

Steps 4–6 of [After a restart](#after-a-restart).

### 9. Your account

```
uv run python -m app.api.admin invite
```

Register at http://localhost:4200 with the printed code. Then:

```
uv run python -m app.api.admin make-admin you@example.com
```

---

## Optional

Web Server in its container instead of step 4 (needs steps 4–5 of *From
nothing* done first):

PowerShell:

```
$env:CODE_VERSION = git rev-parse --short=12 HEAD; docker compose -f docker-compose.dev.yml --profile web up -d --build api
```

Git Bash:

```
CODE_VERSION=$(git rev-parse --short=12 HEAD) docker compose -f docker-compose.dev.yml --profile web up -d --build api
```

pgAdmin: http://localhost:5050 (login in `docker-compose.dev.yml`).

API docs: http://localhost:8000/docs

When something does not come up: [BOOTSTRAP.md](BOOTSTRAP.md) — why this
order, and a check for each piece.

---

## Production

`docker-compose.prod.yml`: its own containers and database, on this PC. Never
run beside dev — stop one before starting the other. Commands from the
repository root, in Git Bash.

### Once

1. Secrets (in `secrets/`, never in git; run again only fills in what is
   missing):

   ```
   uv run python -m app.prod_secrets
   ```

2. Copy the dev database, with dev running:

   ```
   docker compose -f docker-compose.dev.yml exec -T db pg_dump -U postgres -d verified_filings -Fc --no-owner --no-privileges > seed.dump
   ```

3. Stop dev, start the production database, restore into it. Seven errors
   about missing roles are expected: step 4 makes the roles.

   ```
   docker compose -f docker-compose.dev.yml stop
   docker compose -f docker-compose.prod.yml up -d --wait db
   docker compose -f docker-compose.prod.yml exec -T db pg_restore -U postgres -d verified_filings --no-owner --no-privileges < seed.dump
   ```

4. Build, provision the roles, delete the dump:

   ```
   CODE_VERSION=$(git rev-parse --short=12 HEAD) docker compose -f docker-compose.prod.yml build api
   docker compose -f docker-compose.prod.yml run --rm ops -m app.db.roles
   rm seed.dump
   ```

### Start and stop

```
docker compose -f docker-compose.dev.yml stop
CODE_VERSION=$(git rev-parse --short=12 HEAD) docker compose -f docker-compose.prod.yml up -d --build --wait
```

```
docker compose -f docker-compose.prod.yml stop
```

Production in a browser on this PC: http://localhost:8080

After a change to the production file's network, recreate the containers (the
data stays; never add `-v`):

```
docker compose -f docker-compose.prod.yml down
```

### The owner's tools

```
docker compose -f docker-compose.prod.yml run --rm ops -m alembic upgrade head
docker compose -f docker-compose.prod.yml run --rm ops -m app.db.roles --check
docker compose -f docker-compose.prod.yml run --rm ops -m app.api.admin invite
```
