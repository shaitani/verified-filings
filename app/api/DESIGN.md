# The web end — [A] Web Client, [B] Web Server, [F] Presenter

**Nothing here is built.** This is the agreed shape, written down before any
schema is drafted. Decisions the user has made are stated as decisions; the
rest are marked *proposed* or listed in §13 as open.

Agreed 2026-09-27. Replaces the earlier [A] Ask UI / [B] Web Display sketch,
and changes what [F] is (§2).

```
[A] Web Client   web/            Angular + ngrx/signals. Renders; decides nothing.
      ↕  REST + server-sent events, same origin, cookie session (§10)
[B] Web Server   app/api/        FastAPI, in its own container (§12). Runs the
                                 chain, owns users, conversations and jobs; the
                                 only thing that talks to [A].
      ↓  parse_question → map_query → answer
[C] Query Parser → [D] Query Mapper → [E] Executor        (built)
      ↓  ResultSet
[F] Presenter    app/presenter/  ResultSet → AnswerView. Plain Python, no model.
```

`app/api/` and a top-level `web/` are where `sec-retriever.md` §3 put them
from the start; `app/db/DESIGN.md` §5 puts request/response models in
`app/api/`, not `app/schemas/`. [F]'s section moves to
`app/presenter/DESIGN.md` when that package exists.

---

## 1. The rule that shapes everything: a reply has parts

A question is answered **per part** (semantic DESIGN §8e). "Apple's assets,
liabilities, goodwill" is two figures and a refusal; "Apple's margins and
revenue" can be a question back *and* a figure. So there is no three-way
branch — answer / clarify / refuse — to route on. There is one reply, and
every part of it says what became of it.

Parts come from **three stages**, and the reply has to carry all of them:

| stage | what it can produce | scope |
|---|---|---|
| [C] parser | `UnacceptableProposal`, `ProposalError`, `ValueError` (empty, too long) | the whole question — there are no parts yet |
| [D] mapper | `Unresolved` with `blocks_question` (a company, a period) | the whole question |
| [D] mapper | `Unresolved` on a metric or narrative element | that part |
| [D] mapper | `Clarification`, `Ambiguity` | that part — a question back |
| [D] mapper | bindings | the parts to answer |
| [E] executor | `GenerationError`, `InvalidSQL`, `UnsupportedPlan` | every part being answered |
| [E] executor | a `ResultSet` whose verdict is not answerable | every part being answered |

The executor rows are easy to get wrong. The answered parts come out of **one
statement**, so an unanswerable verdict refuses all of them — partial trust is
not on offer (retrieval DESIGN §5). But it does **not** touch the parts the
mapper already refused or asked about: those reasons and questions still go
back. And a `ResultSet` exists in that case; [B] must check
`result.is_answerable` before any row leaves the server.

### The envelope (proposed; the schema is still to be drafted)

```
Reply
  conversation_id, job_id
  blocking     Refusal | null     stage + reason. Set → no part carries figures.
  parts[]      one per thing asked (metric and narrative elements)
                 text       the asker's phrase, verbatim
                 outcome    answered | asked | refused
                 reason     when refused
                 ask        when asked — see §3
  answer       AnswerView | null  the figures for the answered parts, with their
                                  notes and citations (§5, §6)
```

Drafted 2026-09-27: `Reply` and the request and event models in
`app/api/schemas.py`; `AnswerView` in `app/schemas/answer_view.py`, because
it is [F]'s contract and [F] must not import the Web Server. Notes travel on the
answer, not the reply: with no figures there is nothing for them to qualify.

A summary status for a header line is **derived** from the parts, never
decided separately — a second source of truth is how the two come to disagree.

**Whose words reach the reader.** Mapper reasons are curated sentences written
for a person (`Unresolved.reason`, `unavailable` entries, `Clarification.
question`) and go through as written. Parser and executor exception messages
are written **for a model** (`validate`'s messages invite a retry) or for
debugging; the reader gets a fixed sentence per stage, and the message goes to
the trace (§8).

## 2. [F] Presenter: deterministic, not prose

[F] was planned as `ResultSet → prose`. It becomes `ResultSet → AnswerView`, a
typed structure the client renders, written in Python with no model.

**Why not a model writing prose.** It is a new place for a plausible wrong
number — a model retyping 94,930,000,000 — and "carry `Note`s through rather
than summarising them away" is the one rule a summariser reliably breaks. The
lesson of retrieval DESIGN §4.6 applies directly: take the job away from the
model rather than check its work.

**Why not hand Angular the raw `ResultSet`.** The domain decisions would move
into TypeScript, where nothing tests them against the data:

- **units** — `pure` is a percentage, `USD` wants scaling, `USD/shares` is
  per-share. A ratio rendered as money is PITFALLS §2.1, moved to the client;
- **`derivation`** — a growth row and a figure row are different series, never
  one (retrieval DESIGN §1, job 3);
- **`mixed_granularity`** — annual and quarterly on one axis reads as a
  fourfold spike (schemas DESIGN §8.18);
- **numbers** — Pydantic serialises `Decimal` as a JSON *string*
  (`"94930000000.000000"`, verified), so the client would be parsing and
  formatting money itself.

So each cell in an `AnswerView` carries the exact value (string), a display
string, and its unit kind, and [A] shows what it is given.

[F] still **never sees a plan**. What it needs from one — the `ResultSpec` —
travels on the `ResultSet` (retrieval DESIGN §5; built 2026-09-27).

## 3. Questions back to the asker

**Decided: an ambiguity is a real question back, and is marked as one.**
`Clarification` (a person curated the choices) and `Ambiguity` (the machine
could not choose between raw concepts) share one wire shape, and the ask
carries its `kind` so [A] can show a small **"ambiguous"** tag on the second.
The tag is for debugging as much as for the reader: an ambiguity means the
alias file has a gap (semantic DESIGN §3), and the tag is how one gets
noticed. Evals keep scoring them apart (`asked` vs `confused`).

```
Ask        ask_id, kind: clarification | ambiguity, question, options[]
Option     option_id, label, description
AnswerIn   ask_id, kind: option, option_id          ← the only kind for now
```

An ambiguity's options are its candidates: `label` is the concept's label
("Payments of Dividends"), `description` its `taxonomy:name`.

**How each binds on the next round:**

| ask kind | option names | next round |
|---|---|---|
| clarification | a curated metric | `(question, option label)` into `parse_question(answers=)` — built, measured (parser DESIGN §10) |
| ambiguity | a concept (`ConceptRef`) | a **pin**: `map_query(query, pins=...)` — to build |

The pin cannot ride through the parser: the label would go to embedding search
again with no guarantee of landing on the concept picked, and it cannot go in
`QueryIn`, which never carries an XBRL identifier (schemas DESIGN §8.1). A
pinned concept still goes through the coverage check.

*Proposed* for the pin: a round whose answers are all pins skips the parser —
it re-maps the conversation's stored `QueryIn`, so no model call and no chance
of the phrase moving. A round mixing pins with clarification answers re-parses
and matches each pin to its element by phrase; a pin whose phrase does not
reappear is asked again, never dropped.

The server resolves `option_id` against the options **it** offered (§4) and
refuses anything else.

### Free-text answers — not now, and not boxed out

**Decided: options only for now.** The case for free text is real: the reader
wants a figure the options do not list, and the parser already accepts any
answer text as a source of spans (parser DESIGN §10). The case against, for
now:

- an option is **known to resolve** — every clarify option is validated at
  load (semantic DESIGN §8b); free text reopens the whole chain, and can end in
  embedding search, another ambiguity, or another question;
- an answer is a span source, so "and Microsoft too" changes the question's
  scope through the answer channel;
- free text to an **ambiguity** cannot pin — it goes back through the parser
  like any other text;
- nothing measures it: the parser's round trip was measured with option labels.

Three things keep adding it later additive rather than a refactor:

1. **`AnswerIn` is a union on `kind`** from the start. Free text is a second
   variant, `kind: text` — the OpenAPI schema grows, nothing changes shape.
2. **Every stored answer keeps its text** — for an option, its label — with
   `option_id` nullable (§11). No migration.
3. **The chain already takes text.** An option becomes `(question, label)` at
   the server's edge, so `parse_question` never learns which kind it was.

What adding it costs then: the input on [A], the `text` branch in [B], and a
measurement of the round trip with typed answers.

## 4. Conversations and jobs

**Decided: job ids with streamed stage events.** A question takes ~10 s on
average and ~60 s at worst, on one GPU where Ollama serves one request at a
time. A POST that blocks for a minute is fragile, and a second asker simply
waits.

```
POST /api/conversations                    {question}         → {conversation_id, job_id}
POST /api/conversations/{id}/answers       {answers[]}        → {job_id}
GET  /api/jobs/{job_id}/events             text/event-stream
GET  /api/jobs/{job_id}                    the Reply, once done (reconnects, reloads)
GET  /api/conversations                    the signed-in user's history
```

Events: `queued` → `parsing` → `mapping` → `fetching` → `presenting` → `done`
(carrying the `Reply`) or `failed`. `queued` is real, not decoration: model
calls go through a one-at-a-time gate, and the asker should see they are
waiting rather than that it is slow. The parse / map / answer split is the one
`evals/walkthrough.py` already times.

**Decided: the server holds the conversation, in Postgres** (§11). A
conversation is the original question, the asks put back, and the answers
given; each round is a job. Every conversation and job belongs to a user, and
a user reads only their own.

The gate and the event streams live in the process, so **the Web Server runs
one worker**. A job found unfinished at startup was cut off by a restart and is
marked `failed`, not left `queued` forever.

**A known property, not a bug.** Ollama's prompt cache changes replies
(HANDOFF §6), so with several askers an answer can depend on what the previous
one asked. Unloading between jobs would remove it at a cost in latency.

## 5. What to draw

The plan already says it. `ResultSpec.shape` is set by what resolved, and a
question asking to *see* it drawn becomes `series` (parser DESIGN §5). [F]
turns that into a view choice; [A] picks the library.

| `shape` | view |
|---|---|
| `scalar` | a stat card |
| `series` | a line chart over **real dates**, one line per company or metric |
| `ranking` | a sorted bar chart |
| `table` | a table |

**Decided: a series is plotted on a time axis, not on fiscal labels.** Each
point sits at its `period_end`, with its fiscal label and full window in the
tooltip. "FY2024" is a different span of months per filer (schemas DESIGN
§8.11), and on a real time scale that disparity, and the 83–97-day spread of
quarter lengths, is visible rather than flattened into evenly spaced labels.
The `period_misalignment` note still shows.

- **The table always ships**, beside any chart. A chart hides the exact figure
  and has nowhere to hang a citation.
- **Notes sit above the view**, not in a tooltip. A `narrower_than_asked` or
  `partial_coverage` note is part of the answer.
- **A ranking is drawn in order or not at all.** The direction is
  `ResultSpec.rank` on the `ResultSet` (parser DESIGN §10c), so [F] sorts by it
  and says which end is first ("highest first"); `BarView` refuses bars that
  are not monotonic in its stated direction.
- **`mixed_granularity` splits** into separate series or panels.
- **Derived rows are their own series**, labelled by `derivation`.

## 6. Citations with every answer

*Decided.* Every answer shows what it was answered *as*: per binding, the
concept's label and name, the expression over its operands, the companies,
and each figure's actual date window. `Citation` and the rows already carry
all of it.

This is the cheap half of HANDOFF §6's top gap — nothing checks that the
answer matches the question, and a chart makes a wrong answer look more
finished. Showing the interpretation does not close that gap; it lets the
reader see it. A binding with `resolved_by = embedding` says so: it was
matched by similarity, and nobody reviewed it.

## 7. Never sent to the client

The SQL, the `QueryPlan` and the `QueryIn`. The statement is the one thing a
reader could be quoted by accident (retrieval DESIGN §8); the plan carries
rationales and raw candidates written for machines. What a reader needs of
either reaches them through §1's parts and §6's citations.

## 8. Kept, so a run can be debugged afterwards

*Decided:* what is never sent is always kept, so "why did this question go
wrong" can be handed to someone — or to Claude in the background — without a
replay.

*Proposed:* one `web.job_trace` row per job (§11) rather than a JSONL file,
now that the jobs are in Postgres anyway: it joins to its conversation and
user, and "every job where an ambiguity was asked" is a query rather than a
grep.

### What a trace holds

| field | why |
|---|---|
| `code_version` | git revision and a dirty flag, read at startup. Prompts change weekly; a trace from last Tuesday's code is a different experiment |
| `models` | parser, generator and embedding model tags |
| `query_in`, `plan`, `result` | the three contract objects, each null past the stage that stopped the job |
| `model_calls` | every model call in order: stage, model, the **full prompt**, the **raw reply**, seconds. The parser's rejected first reply and its repair are both here — "what it did after being corrected" is the useful half (parser `__init__.py`) |
| `statements` | every SQL statement: text, `validate` verdict, execution error |
| `timings` | per stage, the split `walkthrough.py` already uses |
| `errors` | every exception: stage, type, message, traceback |

Prompts are stored whole, not rebuilt from the revision: a dirty tree cannot
be rebuilt. Measured 2026-09-27, the parser prompt is ~20 KB and the SQL prompt
~6 KB, so a job with a retry is ~30–50 KB before Postgres compresses it.

### How it is collected — without changing the chain

A small collector, `app/trace.py`, holds the current job's trace in a context
variable. The three places that already see the raw material append to it
**when one is set**: `propose()` in the parser and `generate()` in retrieval
(prompt and reply), and `execute()`'s `_log` (statement, verdict, error). When
none is set — the evals, the CLI, every test — they do exactly what they do
today. No signature changes, and `data/retrieval_log.jsonl` keeps being
written, gaining the job id when there is one.

[B] opens the trace when a job starts and writes the row once, in a `finally`,
whether the job answered, refused or crashed. A job cut off by a restart has
no trace; its `web.job` row is marked `failed` at startup (§4).

### Who can read it

**The Web Server cannot.** `vf_web_role` gets `INSERT` on `web.job_trace` and
no `SELECT` (§11), and writes it with a plain `INSERT` — so no bug in the
reply path, and no future endpoint, can hand a trace to a browser. Reading is
done on the host with the owner's credential, the one migrations already use:

```
uv run python -m app.api.trace <job_id>          one job, as JSON
uv run python -m app.api.trace --flagged         jobs a user reported
uv run python -m app.api.trace --failed --since 2026-10-01
```

That command is what a background Claude session is pointed at.

### Reporting a problem

*Proposed:* each reply carries a **"report a problem"** control with an
optional note, written to `web.job_feedback`. It is how a wrong-looking answer
becomes a `--flagged` job rather than something remembered later — the
refusals and wrong numbers worth debugging are the ones a person noticed.

## 9. Types across the boundary

*Decided.* The Angular types are generated from FastAPI's OpenAPI schema, not
written by hand. `app/schemas` is the single source of truth (`sec-retriever.md`
§3), and two hand-kept copies of one contract drift.

What the browser sends is named with an `In` suffix (`NewConversationIn`,
`AnswersIn`), the repo's mark for inbound, validated input (schemas DESIGN
§4.13); what the server sends is not. Not a `User` prefix: FastAPI Users
already names its account models `UserRead` / `UserCreate`, and in the
generated types the two would read as one family.

## 10. Users and sign-in

**Decided: user accounts, signing in with email and password or with GitHub.**

**Decided: FastAPI Users** — email/password with registration, verification
and password reset, GitHub through its OAuth flow, and an async SQLAlchemy
adapter that fits the `asyncpg` stack (`app/db/DESIGN.md` §5). **It is in
maintenance mode** (checked 2026-09-27): security fixes continue, no new
features, and a successor is announced but unnamed. Accepted because the rest
of the app depends only on a `current_user` dependency, so replacing it later
touches the auth module and its tables, nothing else.

### What it does, and what it leaves to us

Read from its source, 2026-09-27:

- **Reset and verification tokens are stateless** signed JWTs, one hour by
  default, each with its own secret. They need no table. A reset token carries
  a fingerprint of the password hash, so it stops working once the password
  changes — single use without storage.
- **The OAuth flow** sets its own CSRF cookie and signs its `state` with a
  secret. No table.
- **Sessions** are `web.access_token` rows, checked against a maximum age.
  Expired rows are not deleted by anything; a periodic cleanup is ours.
- **Email lookup is case-insensitive, the uniqueness is not.** The library
  finds users by `lower(email)` but its column is a plain `UNIQUE`, so two
  concurrent registrations as `Bob@x.com` and `bob@x.com` can both land. Our
  migration adds a unique index on `lower(email)`.
- **No brute-force protection** on login. It belongs with rate limiting, which
  is deferred (§13) — acceptable while the site is not public, and not after.
- **No first administrator.** `is_superuser` exists; nothing sets it. A CLI
  command creates the first one.

### Guard rails on GitHub sign-in

- **Scopes are narrowed.** The GitHub client (`httpx-oauth`) asks by default
  for `user` — read *and write* access to the profile. We ask for `read:user`
  and `user:email` only.
- **The email GitHub returns is not checked as verified.** The client takes the
  public profile email, or else the primary address, without looking at
  GitHub's `verified` flag. So both of the library's convenience flags stay at
  their safe default, `False`: `is_verified_by_default` (a GitHub sign-up is
  not treated as a verified email) and `associate_by_email` (a GitHub account
  is never attached to an existing account because the addresses match — the
  library's own documentation describes the takeover that allows).
- **GitHub's access token is stored**, in plain text, because the library's
  column requires it. Nothing here calls GitHub after sign-in, and the narrowed
  scopes keep what that token can do small.

### Configuration it needs

In `.env`, never in git: the reset-token secret, the verification-token
secret, the OAuth state secret, the GitHub client id and secret, the
session lifetime, and the cookie's `Secure` flag (off only for `http://`
development).

*Proposed session: an httpOnly cookie backed by a database token, not a bearer
JWT.* The event stream decides it: the browser's `EventSource` cannot set an
`Authorization` header, and a cookie goes with it automatically. A database
token can also be revoked, which a JWT cannot before it expires. The cookie is
`SameSite=Lax`, and [A] is served from the same origin as the API (§12), so
there is no CORS to configure.

Two things the user has to supply, neither of which code can: a **GitHub OAuth
app** (client id and secret, into `.env`), and an **email sender** for
verification and reset mail.

### Sign-up is by invitation

**Decided (2026-09-27).** Every question costs GPU time on one machine, so no
one registers uninvited. Open sign-up can be switched on later.

An allowlist of email addresses is not enough while email verification is
deferred (below): with nothing proving the address belongs to the person
typing it, whoever registers an allowed address first owns the account. So an
invitation is one of two things, created only from the CLI with the owner's
credential:

- **An email invite** — the address plus a **single-use code**, stored hashed,
  that you hand to the person yourself. Registering needs both; the code is
  spent on use.
- **A GitHub invite** — the GitHub account's numeric id, looked up from the
  username when the invite is made. Not an email: the id is permanent and
  cannot be claimed by someone else, where the email GitHub hands back is not
  checked as verified (above).

The check sits on **both** ways an account is created — the registration route
and the first GitHub sign-in — and is tested on both, because the library
creates OAuth users by a different path from registered ones. The Web Server
can spend an invite but cannot create one (§11).

### Email is deferred

**Decided (2026-09-27).** Until a sender is chosen, accounts work unverified,
the verify and forgot-password routes are **not mounted**, and a password is
reset from the CLI. The invitation code is what stands in for proof of the
address. The hooks the library calls to send mail (`on_after_request_verify`,
`on_after_forgot_password`) are where a sender plugs in later.

## 11. What Postgres gains — proposed, not built

**Shown to the user before any migration is written.** Approved in outline
2026-09-27 (the `web` schema and `vf_web_role`); this section is the detail.

A second PostgreSQL schema, **`web`**, in the same `verified_filings` database,
next to `xbrl`. Every primary key is a UUID made in Python, so there are no
sequences to grant.

### Sign-in (§10)

| table | columns | notes |
|---|---|---|
| `web.user` | `id`, `email` (320), `hashed_password` (1024), `is_active`, `is_superuser`, `is_verified`, **`created_at`** | the library's columns plus `created_at`; **a unique index on `lower(email)`** beside its plain one (§10) |
| `web.oauth_account` | `id`, `user_id`, `oauth_name` (`github`), `account_id`, `account_email`, `access_token`, `refresh_token`, `expires_at` | the library's columns; deleted with its user |
| `web.access_token` | `token` (43), `user_id`, `created_at` | one row per signed-in session; deleted on sign-out, and by a periodic cleanup once past the session lifetime |

| table | columns | notes |
|---|---|---|
| `web.invite` | `id`, `kind` (`email` or `github`), `email`, `code_hash`, `github_account_id`, `created_at`, `used_at`, `used_by` | §10. Made only from the CLI; spent once |

Nothing else is needed for sign-in to work end to end: reset and verification
tokens and the OAuth state are signed, not stored (§10). `user` is a reserved
word in PostgreSQL, so hand-written SQL says `web."user"`; the name stays
because the library's foreign keys point at it. That those foreign keys resolve
into `web` rather than the default schema is to be checked when the models are
written, not assumed.

### Conversations and debugging

| table | columns | notes |
|---|---|---|
| `web.conversation` | `id`, `user_id`, `question`, `created_at` | the original question, verbatim |
| `web.job` | `id`, `conversation_id`, `round`, `status`, `asks`, `answers`, `reply`, `created_at`, `finished_at` | one per round (§4). `asks` keeps the options offered — for an ambiguity, the candidate concepts — so an answer is checked against them and a pin read from them. `answers` keeps each answer's text with `option_id` nullable (§3) |
| `web.job_trace` | `job_id`, `created_at`, `code_version`, `models`, `query_in`, `plan`, `result`, `model_calls`, `statements`, `timings`, `errors` | §8. Write-only for the Web Server |
| `web.job_feedback` | `id`, `job_id`, `user_id`, `note`, `created_at` | "report a problem" (§8) |

Deleting a user deletes their conversations and everything under them
(`ON DELETE CASCADE`). Only the owner's credential can delete a user; the Web
Server cannot (below).

### `vf_web_role`, and what the role code must not lose

The Web Server's own credential. What it may do, table by table:

| table | `SELECT` | `INSERT` | `UPDATE` | `DELETE` |
|---|---|---|---|---|
| `user` | yes | yes, **except `is_superuser`** | yes, **except `is_superuser`** | no |
| `oauth_account` | yes | yes | yes | no |
| `access_token` | yes | yes | no | yes |
| `invite` | yes | **no** | `used_at`, `used_by` only | no |
| `conversation` | yes | yes | no | no |
| `job` | yes | yes | yes | no |
| `job_trace` | **no** | yes | no | no |
| `job_feedback` | no | yes | no | no |
| anything in `xbrl` | **no** | no | no | no |

Three of those are guard rails in their own right. **The Web Server cannot
make anyone an administrator**: `is_superuser` is outside its column grants and
defaults to false in the database, so only the owner's credential — the CLI
command of §10 — can set it, whatever a bug in a route does. **It cannot
invite anyone**: it can mark an invite spent, never create one (§10). **It
cannot read a trace** (§8).

`app/db/roles.py` was written for read-only roles, and every guard rail in it
stays exactly as it is for the two that exist:

| guard rail in `roles.py` today | after |
|---|---|
| `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS` | unchanged, all three roles |
| revoke every table privilege before granting, so the spec is authoritative | unchanged, and applied in **every** managed schema: `web` is revoked from the two readers, `xbrl` from the web role |
| no default privileges on future tables (`inherit_future_tables=False`) | unchanged; the web role gets it too, so each new table is a decision |
| `default_transaction_read_only = on` | unchanged for both readers. The web role is the one role without it, and its narrowing is the grants above |
| `REVOKE CREATE ON SCHEMA public`, and `public` usage only where pgvector needs it | unchanged; the web role gets neither |
| `search_path` pinned per role | unchanged; the web role's is `web` |
| `statement_timeout`, `idle_in_transaction_session_timeout`, connection limit | unchanged; the web role gets its own values |
| legacy role dropped, `REVOKE ALL ON DATABASE ... FROM PUBLIC` | unchanged |
| password taken from the URL the application connects with | unchanged; `DATABASE_URL_WEB` added |

How the code changes without loosening anything:

- **`RoleSpec` gains fields whose defaults are today's behaviour** — a schema
  defaulting to `xbrl`, a per-table write map defaulting to empty, and
  `read_only` defaulting to true. The two existing specs are not edited. A spec
  that is read-only and names a write is refused when it is built, so
  "read-only" cannot quietly stop meaning it.
- **Pinned before touched.** A test is written first, against today's code,
  recording every statement it emits for the two existing roles. After the
  change the same test must pass, with the only difference being the added
  revokes on `web`. The change is checked against that, not against a reading
  of the diff.
- **Checked live on the test database**, with `has_table_privilege` and
  `has_column_privilege`: the retrieval role reaches `xbrl.reported_fact` and
  nothing in `web`; the web role reaches nothing in `xbrl`, cannot read
  `job_trace`, and cannot write `is_superuser`.
- **`--check` reports every privilege type in every managed schema** — it
  reports only column privileges in `xbrl` today, which would not show a stray
  `INSERT` or a grant in `web`.
- **Migrate before provisioning.** The web role's grants name `web` tables, so
  `BOOTSTRAP.md` gains that ordering.

### Alembic

One history for the database (`ALEMBIC.md`). The web models get their own
`MetaData(schema="web")` beside `xbrl`'s, `env.py`'s `include_name` widens to
both schemas, and the first migration creates `web` explicitly, as the first
one did `xbrl`. The test database is migrated by hand, as ever (`ALEMBIC.md`
step 5).

## 12. Containers

*Decided:* the Web Server runs in its own container in `docker-compose.yml`.

*Proposed:*

- **`api`** — Python 3.13 with `uv`, one `uvicorn` worker (§4), port 8000.
  `depends_on` `db` and `ollama` with `condition: service_healthy`, both of
  which already have honest health checks. Its database and Ollama URLs name
  the compose services (`db`, `ollama`) in `environment:` — `.env`'s
  `localhost` values are for Python running on Windows and stay as they are.
  `./data` is mounted so `retrieval_log.jsonl` survives the container; the
  lexicon files (`company_aliases.json`, `sic_numbers.json`,
  `corpus_companies.json`) are tracked in git and go into the image.
- **[A] in development** — `ng serve` with a proxy to `api`, so the browser
  sees one origin.
- **[A] deployed** — later, a small web-server container (nginx) serving the
  built bundle and proxying `/api`, with response buffering off for the event
  stream. One origin is what keeps the cookie and the stream simple (§10).

## 13. Open

- **Email sender** for verification and reset. Deferred by the user; until
  then accounts are unverified and invitations stand in for it (§10).
- **One orchestrator for [B] and the evals.** `evals/run.py::run_one` is [B]
  without the browser; if the route writes its own copy of the chain, the evals
  measure a surrogate (HANDOFF §3). Deferred by the user to the testing update.
- **Rate limiting** — every request costs GPU seconds, and login has no
  protection against password guessing (§10). Deferred by the user; needed
  before the site is reachable from outside.
