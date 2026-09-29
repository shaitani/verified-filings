# Future work

Planned and deferred tasks. Where a task closes a gap, the gap's evidence is
in [GAPS.md](GAPS.md) and is not repeated here. The work currently in
progress is in [HANDOFF.md](../HANDOFF.md).

---

## Next

### Deployment — host not decided

DigitalOcean is likely, not decided, and the user is not ready to be walked
through it. Until then it is a constraint: flag anything built now that would
not run on a rented Linux VM with Docker. None of this is done:

- **A production compose override** — publish only the reverse proxy's 443
  (and 80, to redirect); drop `db-test` and `pgadmin`; replace the committed
  `postgres`/`postgres` and pgAdmin password; secrets from a secret store, not
  readable with `docker inspect`.
- **A reverse proxy** (nginx or Caddy, with TLS) serving the Angular bundle
  and proxying `/api`, with response buffering off for the event stream. One
  origin keeps the cookie and the stream simple.
- **A second GitHub OAuth app** for production, with the `https` callback.
- **Rate limiting, including login brute-force protection** — required before
  the site is reachable from outside. Every request costs GPU seconds.
- **A shorter session** — `AUTH_SESSION_DAYS` is 30; the user expects to
  change it.
- **A GPU** for Ollama on the server; on a CPU generation is an order of
  magnitude slower.
- **Optionally an email sender**, which turns on email verification and
  password reset (below).

## Correctness

### Retire Qwen as a SQL writer

The user's stated direction. Rankings are done: the parser marks
`rank: highest | lowest` and Python writes the `ORDER BY`. What Qwen still
writes is an aggregate (q040's average) or another derivation (G2). Move that
reading into the parser as a closed list, the pattern that worked for
`clarify_as`, `over_time` and `rank`: `aggregate: average | sum | min | max`,
`share_of_total`, `difference`, grammar-constrained and checked in `accept()`.
Python then writes every statement from a fixed template over `figures`; an
operation not on the list is refused with a reason (q015's "fell three years
running" until added).

Costs to weigh: a wrong operation becomes a plausible wrong answer (state the
operation in the answer, pin it in eval expectations); long-tail operations
Qwen improvises today stop working until listed; and it is another parser
prompt change, so re-measure the colon-list questions (q044, q045, q048,
q051) cold.

### Relationships between metrics (G5)

In order: a way for `QueryIn` to carry a relation between element ids
(`{"op": "ratio", "of": "e3", "to": "e2"}`); a mapper step turning it into one
`Binding` with a multi-operand `expression`; a parser prompt that emits it.
The middle step is nearly free — `Binding`, `operand_unit`, the unit check and
`figures` were built for exactly this shape. The ends are the work. The prize:
"share of", "per", "divided by", "as a percentage of" and "difference between"
stop needing an alias each, over any two metrics the corpus holds.

### The answerability gate (G1)

Refuse, in the parser, a question that is about the filing rather than its
figures. Needs measuring against the eval set rather than guessing.

### A dropped-company check (G4)

Mirror the faithfulness gate: refuse a parse when the question contains a
known company alias that no company element covers. Measure first for short
aliases that occur as ordinary words.

### The unit guard (G3)

Stop `_wrong_unit` skipping rows because the model labelled them derived.

### Smaller

- **Pin the retrieval rules text** with a test, so "or less" cannot return
  (G7).
- **Restatement disclosure** (D2.1): a `Note` when a value was restated; needs
  the superseded rows, which the view hides — a second view or a `filed_date`
  column.
- **Detect mixed units** in a result set, probably as a verdict check (G7).
- **Reword developer-facing refusal text** in the mapper (G8).
- **Answer out-of-range years from comparatives** — an open decision, not yet a
  task (G9).
- **Whether a rejected SQL statement goes back to the model** once, with the
  validator's message. Undecided: the messages invite it, but a retry loop is
  also how a validator's error text becomes a map of what to get around.

## Quality of answers

- **Alias curation for recall** — the terms listed in G9. Collaborative: the
  user is not an accountant, and this file is the main lever on answer
  quality.
- **Move more prompt rules into Python.** Every rule exists because the model
  can get something wrong, so every rule names a candidate. Left: the fixed
  twelve-column projection (would retire the unnamed-column trap) and the
  `LIMIT` (would retire G7's wording hazard for good).
- **A headline sentence**, if ever wanted: templated from a row in the
  Presenter, never written by a model.
- **Free-text answers to a question back.** Options only for now. Kept open
  without a refactor: `AnswerIn` is a union on `kind` (free text would be
  `kind: text`), every stored answer keeps its text, and the chain already
  takes text. Adding it costs the input in the client, the `text` branch in the
  server, and a measurement of the round trip with typed answers.
- **An admin page**, perhaps. Administration is the owner's CLI today.

## Testing

- **Evals onto `app/chain.ask`** — `evals/run.py` still runs its own copy of
  the chain, so it measures a surrogate. Deferred by the user to their testing
  update.
- **Unload the model between eval questions**, so a full run's grades are
  reproducible (G6).

## Data and ingest

- **Roll the fiscal-year window forward.** The store keeps FY2021–FY2025 for
  every filer. `get-xbrl` prints a note for any company that has already filed
  a later 10-K (MSFT, NVDA, ORCL had filed FY2026 as of 2026-08). Once **all
  20** have, bump `FISCAL_YEAR_MAX` and re-run `get-xbrl` (it re-filters from
  the cache, no network) and the load.
- **Recover NVIDIA's FY2021 Q1 and Q2** (D3.8), dropped because the filings
  carry `fy: 2020`. Decide how ingest scopes a filing whose `fy` disagrees with
  its period dates before changing the filter — it is the same field D1.1
  warns about.
- **Submissions overflow** for JPM and BAC (G9), if a five-year analysis of
  their submissions is ever needed.
- **Loader:** cross-check a file's `cik` against its ticker; model the
  submissions JSON (SIC, filing history) in the database; a Core bulk insert if
  load time ever matters (AAPL ~1.4 s, JPM ~2.4 s today).
- **Schemas:** assert `fy ∈ scope.fiscal_years` (true today; left loose in
  case comparative columns make it noisy); a format check on `accn` and
  `frame` (every current value matches); a tighter `fy` bound than
  2000–2100.
- **A `sic_office` source**, if one is found (G9).

## Web

- **Email sender** — verification and password reset. Deferred by the user;
  until then accounts are unverified and invitations stand in for proof of the
  address. The library's `on_after_request_verify` and
  `on_after_forgot_password` hooks are where it plugs in.
- **Deleting a past conversation** — left for later by the user. The web role
  has no `DELETE` on `conversation` (app/api/DESIGN.md §11), deliberately; a
  delete would need that grant decided first.
- **More than 50 past questions** — the history returns the newest 50 and has
  no paging.

## Deferred by the user — do not reopen unprompted

- **Amended filings** (D3.4).
