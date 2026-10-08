# Tools and libraries

Everything the project runs on, where it is used, why it was chosen, and what
else could do the job. Exact Python versions are in `uv.lock`, JavaScript
versions in `web/package-lock.json`. How to start each piece: [STARTUP.md](STARTUP.md).

---

## Runtime and tooling

| tool | where | notes | alternatives |
|---|---|---|---|
| **Python 3.13+** | everything under `app/`, `evals/`, `tests/` | `.python-version` pins 3.13. Moved up from 3.9 when 3.9 reached end of life. The `from __future__ import annotations` headers are now unnecessary and harmless | Go, Node/TypeScript, Java/Kotlin |
| **uv** | dependency management, every Python command | Run everything as `uv run …`; the global `python` lacks the project's packages. Pinned in `api.Dockerfile` for a frozen install from `uv.lock` | Poetry, pip + pip-tools, PDM, Pipenv |
| **hatchling** | `pyproject.toml` build backend | Builds the `app` package and the `sec-retriever` command | setuptools, Poetry-core, flit, PDM-backend |
| **ruff** | lint and import sorting | Rule set pinned in `pyproject.toml` (`E F I UP B`) so it does not drift with ruff's defaults. `app/db/migrations/` is excluded (Alembic owns that style). `app/parser/prompt.py` ignores `E501`: its line breaks are prompt content, and reflowing one example changed the model's output for an unrelated question | flake8 + isort + pyupgrade, pylint, Black (formatting only) |
| **Node 24, npm 11** | `web/` | | pnpm, Yarn, Bun |
| **git** | version control; `code_version` in every job trace | The container gets `CODE_VERSION` instead, having no `.git` | Mercurial, Fossil |

## Containers and infrastructure

| tool | where | notes | alternatives |
|---|---|---|---|
| **Docker Desktop + Compose** | `docker-compose.dev.yml`, `docker-compose.prod.yml` | Two projects, never run together (app/api/DESIGN.md §12). Dev — five services: `db`, `db-test`, `pgadmin`, `ollama`, and `api` (behind the `web` profile). Every port is published on this machine's loopbacks only (`127.0.0.1` and `[::1]` — Windows resolves `localhost` to `::1` first, and binding only `127.0.0.1` made every connection wait ~2 s). `gpus: all` gives Ollama the card; Docker Desktop supplies the NVIDIA runtime | Podman + podman-compose, Rancher Desktop, installing each service directly |
| **Base images** `python:3.13-slim`, `node:24.18.0-slim` | `api.Dockerfile`; `web.Dockerfile`'s build stage | | Alpine, full Debian, distroless, Chainguard |
| **Caddy** | `web.Dockerfile`, `web.Caddyfile` | The production web container: serves the built Web Client, passes `/api` to the api, sets the security headers. Chosen over nginx for its short config and its client-IP handling (`trusted_proxies`). Its core has no per-IP rate limiting — that needs a plugin build, not done; the api's own limits stand | nginx, Traefik, HAProxy, Apache |
| **Tailscale** (`tailscale/tailscale` image) | `docker-compose.prod.yml` `tailscale`, `tailscale.serve.json` | The front door: Serve and Funnel publish the web container at `https://vf.zubron-ratio.ts.net` without opening a router port; TLS ends on the PC. Chosen over Cloudflare Tunnel (which reads the traffic) and a router port-forward (which exposes the home IP). Free Personal plan: a `*.ts.net` address only, and unpublished Funnel bandwidth limits. Opened and closed by the `/open-the-door` and `/close-the-door` skills | Cloudflare Tunnel, ngrok, router port-forward, a rented reverse proxy |

## Data and database

| tool | where | notes | alternatives |
|---|---|---|---|
| **PostgreSQL 17 + pgvector** | image `pgvector/pgvector:pg17`: `db` (port 5432, real data) and `db-test` (5433, used only by `tests/`) | Two servers on two volumes, fully isolated. The `vector` extension is created by a migration | Postgres: MySQL/MariaDB, SQLite, CockroachDB. Vector search: Qdrant, Weaviate, Chroma, FAISS, Milvus |
| **SQLAlchemy 2.0, async** | `app/db/` | Everything touching Postgres is async | SQLModel, Tortoise ORM, Piccolo, raw asyncpg |
| **asyncpg** | the database driver | Chosen over psycopg (async throughout). Never imported: SQLAlchemy loads it from the `postgresql+asyncpg://` URL scheme. `greenlet` is SQLAlchemy's async bridge | psycopg 3 (async), aiopg |
| **pgvector (Python)** | `app/db/models.py`, the embedding migration | The `Vector` column type for concept embeddings | a vector store's own client (see above) |
| **Alembic** | `alembic.ini`, `app/db/migrations/` | Async template. The URL comes from `.env`, not `alembic.ini`. How to write a migration: [ALEMBIC.md](ALEMBIC.md) | Atlas, yoyo-migrations, Flyway, Sqitch |
| **pgAdmin** | `docker-compose.dev.yml`, http://localhost:5050 | Browsing the databases by hand. Local only | DBeaver, DataGrip, TablePlus, psql |
| **PyYAML** | `app/semantic/metric_aliases.py`, `evals/` | The curated alias file and the eval questions | ruamel.yaml, TOML (`tomllib`), JSON |

## SEC access

| tool | where | notes | alternatives |
|---|---|---|---|
| **httpx** | `app/ingest/sec_client.py`, the admin CLI's GitHub lookup | Chosen over `requests` for native async: the SEC's global rate limit is enforced across concurrent requests. Tests replace the network with `httpx.MockTransport` | aiohttp, requests (sync only), urllib3 |
| SEC endpoints | `app/ingest/` | Hard rules for every request — 10/s, a fixed User-Agent, a disk cache: [sec-retriever.md](sec-retriever.md) | |

## Models

| tool | where | notes | alternatives |
|---|---|---|---|
| **Ollama** | `ollama` service, port 11434 | Serves both models locally; nothing leaves the machine. The service pulls both models itself on startup, and its health check passes only once both are present. Serves one request at a time | llama.cpp server, vLLM, LM Studio, TGI, hosted APIs |
| **ollama (Python client)** | `app/parser/proposer.py`, `app/retrieval/generator.py`, `app/embedding_client.py` | | plain httpx against Ollama's API |
| **`qwen2.5-coder:7b`** | [C] Query Parser (question → elements); [E] Executor (only a derivation above Python's figures) | Temperature 0, 8,192-token context. Loads fully onto a GTX 1080 Ti (11 GB) at ~48 tok/s; `ollama ps` must say `100% GPU` or generation is an order of magnitude slower. Its replies depend on Ollama's prompt cache: [TESTING.md](TESTING.md#the-prompt-cache) | Llama 3.x 8B, Mistral 7B, DeepSeek-Coder, Phi, Claude/GPT over an API |
| **`nomic-embed-text` (v1.5)** | concept embeddings (`app/db/embedder.py`), question embeddings (mapper fallback) | Trained with task prefixes the Ollama model does not add: `search_document:` is baked into each concept's embedded text and `search_query:` is added to questions. Unprefixed, "net income" ranked `NetIncomeLoss` fifth | bge-small/base, all-MiniLM, mxbai-embed-large, OpenAI/Voyage embeddings |
| **LangGraph** | `app/prime_new_companies.py` (dev extra) | The new-company priming graph: a state machine that adds companies to the corpus and primes them | plain asyncio code, Prefect, LlamaIndex workflows, Temporal |

Both model tags are named in code, which is authoritative; `docker-compose.dev.yml`
repeats them for the pull and must be kept in step.

## SQL safety

| tool | where | notes | alternatives |
|---|---|---|---|
| **pglast** | `app/retrieval/validator.py` | Parses a statement with PostgreSQL's own grammar (libpg_query), so there is no gap between how the validator reads SQL and how the server will. A reimplemented parser would have that gap, and the gap is the attack | sqlglot, sqlparse (a tokenizer, not a real parser), a hand-written allow-list |

## Web Server

| tool | where | notes | alternatives |
|---|---|---|---|
| **FastAPI** | `app/api/` | Declared directly, not only through FastAPI Users | Litestar, Starlette, Django REST, Flask/Quart |
| **uvicorn** | `api.Dockerfile`, local runs | One worker, always: the job queue and its event streams live in the process. Started with `--ws none`: no route is a WebSocket | Hypercorn, Granian, gunicorn + uvicorn workers |
| **FastAPI Users** (with its SQLAlchemy adapter) | `app/api/auth.py` | Email/password and GitHub sign-in, sessions as database tokens. **In maintenance mode** (checked 2026-09-27: security fixes only, successor unnamed). Accepted because the rest of the app depends only on a `current_user` dependency, so replacing it touches the auth module and its tables alone. What it leaves undone, and the guard rails added: [app/api/DESIGN.md](../app/api/DESIGN.md) §10 | Authlib, a hand-rolled auth module, Auth0/Clerk/Keycloak |
| **httpx-oauth** | GitHub sign-in | Asks for `user` scope by default; narrowed to `read:user user:email` | Authlib, requests-oauthlib |
| **PyJWT** (`jwt`) | `app/api/auth.py` | Installed through FastAPI Users | python-jose, joserfc, `authlib.jose` |
| *rate limiting: our own* | `app/api/limits.py` | A sliding window in memory, about forty lines. **Not slowapi**: it decorates routes we write, not the ones FastAPI Users mounts, and is barely maintained. **Not `limits`**: its storage backends are for many processes, and this server is one. What is limited: [app/api/DESIGN.md](../app/api/DESIGN.md) §4, §10 | slowapi, `limits`, a Caddy rate-limit plugin |
| **Pydantic v2, pydantic-settings** | every contract (`app/schemas/`, `app/api/schemas.py`); `app/config.py` reads `.env` | | Pydantic: attrs + cattrs, msgspec, marshmallow. Settings: python-dotenv, dynaconf, environs |

## Web Client (`web/`)

| tool | where | notes | alternatives |
|---|---|---|---|
| **Angular 22** | all of `web/` | Standalone components, zoneless, strict. The CLI writes CRLF on Windows; `.gitattributes` and `npm run format` keep the repository LF | React, Vue, Svelte, SolidJS |
| **Angular Material** (+ CDK) | tables and controls | | PrimeNG, Taiga UI, Spartan, Tailwind + headless components |
| **Material Symbols as SVG** (`@material-symbols/svg-400`, Apache-2.0) | `web/src/app/icons.ts` | Every icon, bundled as an SVG and registered with Angular Material (`<mat-icon svgIcon="…">`), not Google's icon font: a browser that blocks web fonts (Firefox Focus, iPhone's Lockdown Mode) showed the ligature words — "menu", "edit_square" — instead. No request, no font | Lucide, Heroicons, Font Awesome, Tabler Icons |
| **Google Fonts (Roboto)** | `web.Dockerfile` build | Inlined at build time, with the device's sans-serif behind it | self-hosted fonts, the system font stack |
| **@ngrx/signals** | `AuthStore` and other state | | NgRx Store, Elf, plain Angular signals in services |
| **ApexCharts + ng-apexcharts** | `web/src/app/answer/chart/` only | Chosen by eye from five libraries (ECharts, AG Charts, Chart.js, ApexCharts, Highcharts) drawing the same three real answers. **Pinned exactly** (`7.6.1`, no `^`), so an update is always a deliberate edit. **One folder draws charts**: ESLint's `no-restricted-imports` fails any other file that imports the library, and `AnswerView` names nothing library-specific, so swapping it means rewriting that folder. ECharts is the nearest alternative (Apache-2.0, native time axis). Loaded only when a chart first appears (~228 kB compressed) | ECharts, Chart.js, AG Charts, Highcharts |
| **openapi-typescript** | `npm run api:types` | Generates the client's types from `web/openapi.json`. Types only, no generated runtime code. Chosen over ng-openapi-gen (generated services with FastAPI Users' long method names) and `@hey-api/openapi-ts` (not yet 1.0). Flags that matter: `--immutable` (the client never edits server data) and `--default-non-nullable false` (a request field with a default stays optional) | ng-openapi-gen, `@hey-api/openapi-ts`, Orval |
| **TypeScript 6** | | Angular 22 needs 6. `openapi-typescript` 7.13 declares 5, so `package.json`'s `overrides` runs it on 6 — see below | none for Angular |
| **ESLint** (+ angular-eslint, typescript-eslint) | `npm run lint` | Enforces the `vf-` selector prefix and the chart-folder rule | Biome, oxlint |
| **Prettier** | `npm run format` | | Biome, dprint |
| **Vitest + jsdom** | `npm test` | Unit tests without a browser | Jest, Karma + Jasmine, Playwright component tests |
| rxjs, tslib | Angular runtime | `tslib` is used through `importHelpers` | |

### The TypeScript override

Kept on the user's call, on one condition: **every time the types are
regenerated or either package is upgraded**, regenerate under the TypeScript
the generator declares and require a byte-identical file. From `web/`:

```
npx -y -p openapi-typescript@7.13.0 -p typescript@5.9.3 openapi-typescript openapi.json --immutable --default-non-nullable false -o ../.cache/api-types-ts5.d.ts
git diff --no-index --exit-code src/app/api/openapi.d.ts ../.cache/api-types-ts5.d.ts
```

No output from the second command means identical. Keep the flags in step
with `api:types` in `package.json`. Remove the override once a release of
`openapi-typescript` declares TypeScript 6. Alternatives set aside: running the
generator in its own package on TypeScript 5, or switching generators.

## Testing

| tool | where | notes | alternatives |
|---|---|---|---|
| **pytest, pytest-asyncio** | the Python test suite: [TESTING.md](TESTING.md) | Runs against `db-test` | unittest, ward |
| *eval runner: our own* | `evals/run.py` | Grades each question pass, fail, gap or unsafe | promptfoo, DeepEval, Ragas, OpenAI Evals |
