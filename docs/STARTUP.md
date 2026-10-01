# Startup

Run everything from the repository root unless a step says otherwise.
Commands are for PowerShell; where Git Bash differs, both are given.

---

## After a restart

Data, roles and models survive a restart. Only the processes need starting.

1. Start **Docker Desktop**. Wait until it says it is running.

2. Containers:

   ```
   docker compose up -d
   ```

3. Wait until `db`, `db-test` and `ollama` show `(healthy)`:

   ```
   docker compose ps
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

Stop: `Ctrl+C` in both terminals, then `docker compose stop`.

---

## From nothing

A new machine, a fresh clone, or after `docker compose down -v`.

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

Copy `.env.example` to `.env`. Choose three role passwords and put them in
`DATABASE_URL_QUERY_MAPPER`, `DATABASE_URL_RETRIEVAL` and `DATABASE_URL_WEB`
(uncomment the last). Generate the four signing secrets and paste them in:

```
uv run python -c "import secrets; [print(f'{n}={secrets.token_urlsafe(32)}') for n in ('AUTH_RESET_SECRET', 'AUTH_VERIFY_SECRET', 'AUTH_OAUTH_STATE_SECRET', 'AUTH_DEVICE_SECRET')]"
```

GitHub sign-in is optional: see the comments in `.env.example`.

### 3. Containers

```
docker compose up -d
```

Wait for `(healthy)` on `db`, `db-test`, `ollama` (`docker compose ps`). A
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
uv run python -m app.api.admin invite --email you@example.com
```

Register at http://localhost:4200 with that email and the printed code. Then:

```
uv run python -m app.api.admin make-admin you@example.com
```

---

## Optional

Web Server in its container instead of step 4 (needs steps 4–5 of *From
nothing* done first):

PowerShell:

```
$env:CODE_VERSION = git rev-parse --short=12 HEAD; docker compose --profile web up -d --build api
```

Git Bash:

```
CODE_VERSION=$(git rev-parse --short=12 HEAD) docker compose --profile web up -d --build api
```

pgAdmin: http://localhost:5050 (login in `docker-compose.yml`).

API docs: http://localhost:8000/docs

When something does not come up: [BOOTSTRAP.md](BOOTSTRAP.md) — why this
order, and a check for each piece.
