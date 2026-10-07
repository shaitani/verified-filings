# verified-filings

Answers natural-language questions about SEC financial data with figures that
are actually correct, and refuses plainly when it cannot. Every figure in an
answer is attributable to a filed XBRL fact. A web client asks; a FastAPI
server runs the question through a parser, a mapper, an executor and a
presenter over US filers' 10-K and 10-Q data in PostgreSQL.

| start here | |
|---|---|
| [docs/STARTUP.md](docs/STARTUP.md) | bring everything up |
| [docs/DESIGN.md](docs/DESIGN.md) | how it works, and the doc map |

## Layout

| path | contents |
|---|---|
| `app/ingest/` | [H] SEC client, rate limiter, disk cache, curated XBRL store |
| `app/db/` | [G] models, migrations, the `reported_fact` view, roles, loader, embedder |
| `app/schemas/` | the typed contracts: `xbrl.py`, `query.py`, `result.py`, `answer_view.py` |
| `app/parser/` | [C] Query Parser — question → `QueryIn` |
| `app/semantic/` | [D] Query Mapper — `QueryIn` → `QueryPlan`, and the curated metric aliases |
| `app/retrieval/` | [E] Executor — `QueryPlan` → SQL → `ResultSet` |
| `app/presenter/` | [F] Presenter — `ResultSet` → `AnswerView` |
| `app/chain.py`, `app/api/` | [B] Web Server — the chain as one call, jobs, sign-in, routes |
| `web/` | [A] Web Client — Angular |
| `evals/` | the eval questions and their runners |
| `tests/` | the Python test suite |
| `docs/` | everything else written down |
| `data/` | curated XBRL files, eval runs, logs (gitignored) |

## License

Private project — all rights reserved.
