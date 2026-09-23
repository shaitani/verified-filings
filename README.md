# verified-filings

Answers natural-language questions about SEC financial data with figures that are
actually correct, and refuses — loudly — when it cannot. Every figure in an answer is
attributable to a filed XBRL fact. A CLI ingests the data; the question-answering
chain runs in-process today, with a web front end planned but not built.

Start with [`HANDOFF.md`](HANDOFF.md) — the chain, what is built, and what is not.

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
| `app/ingest/` | [H] SEC HTTP client, rate limiter, disk cache, XBRL store |
| `app/db/` | [G] SQLAlchemy 2.0 models, migrations, the `reported_fact` view, read-only roles |
| `app/schemas/` | The typed contracts: `xbrl.py`, `query.py` (`QueryIn` / `QueryPlan`), `result.py` |
| `app/parser/` | [C] Query Parser — question → `QueryIn` |
| `app/semantic/` | [D] Query Mapper — `QueryIn` → `QueryPlan`, plus the curated metric aliases |
| `app/retrieval/` | [E] Executor — `QueryPlan` → SQL → `ResultSet` |
| `data/xbrl/` | Curated per-company XBRL JSON (git-ignored) |
| `evals/` | 56 tagged questions, `run.py` (the chain, scored) and `summarize.py` (tag distribution) |
| `sec-retriever.md` | Full project spec |

Block letters ([C], [D], …) are the chain handles defined in `HANDOFF.md` §2.

## License

Private project — all rights reserved.
