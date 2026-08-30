# verified-filings

A local CLI for retrieving SEC filing data and verifying numeric claims against it.
Every figure in an answer is tagged by verification status — sourced directly from a
filing/XBRL fact, derived, or unverifiable. No web UI; local only.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for dependency management

Key libraries (see `uv.lock` for the full pinned set):

- httpx 0.28 — async SEC HTTP client
- SQLAlchemy 2.0.52 — ORM for the curated XBRL store
- pytest 9.1 / pytest-asyncio 1.4 — tests
- ruff 0.16 — lint + format

## Setup

```bash
uv sync --extra dev
```

## Usage

```bash
# SEC submissions JSON for one or more corpus companies
uv run python -m app.cli get-submission AAPL MSFT

# Curated XBRL data -> data/xbrl/<TICKER>.json (10-K/10-Q facts, last 5 fiscal years)
uv run python -m app.cli get-xbrl AAPL
```

Companies are resolved by ticker or CIK from `corpus_companies.json`. SEC responses
are cached on disk; each run reports how many calls hit the network vs. the cache.

## Development

```bash
uv run pytest        # tests
uv run ruff check    # lint
uv run ruff format   # format
```

## Layout

| Path | Contents |
| --- | --- |
| `app/ingest/` | SEC HTTP client, rate limiter, disk cache, XBRL store |
| `app/db/` | SQLAlchemy 2.0 models (PostgreSQL `xbrl` schema) |
| `data/xbrl/` | Curated per-company XBRL JSON (git-ignored) |
| `sec-retriever.md` | Full project spec |

## License

Private project — all rights reserved.
