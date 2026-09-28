# The Web Server ([B]). docker-compose builds and runs it as the "api" service.
#
#   docker compose build --build-arg CODE_VERSION=$(git rev-parse --short=12 HEAD) api
#
# Nothing secret is baked in: every credential arrives as an environment variable
# at run time (docker-compose.yml), and .dockerignore keeps .env out of the image.

FROM python:3.13-slim

# uv, pinned to the version this repo uses, for a frozen install from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv

WORKDIR /srv

# Dependencies first, as their own layer: they change far less often than the code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The code, and the lexicon files it reads beside it (paths are relative to /srv,
# as they are to the repository root).
COPY app/ ./app/
COPY alembic.ini company_aliases.json corpus_companies.json sic_numbers.json README.md ./
RUN uv sync --frozen --no-dev

# What ran, for every job's trace: there is no .git in here to ask (app/api/trace.py).
ARG CODE_VERSION=unknown
ENV CODE_VERSION=${CODE_VERSION}

# Not root. data/ is a mounted volume, written for retrieval_log.jsonl.
RUN useradd --create-home --uid 10001 app && mkdir -p /srv/data && chown app /srv/data
USER app

EXPOSE 8000

# One worker: the job queue and its watchers live in the process (app/api/DESIGN.md §4).
CMD ["/srv/.venv/bin/uvicorn", "app.api.server:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
