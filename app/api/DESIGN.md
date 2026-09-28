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
`app/api/`, not `app/schemas/`. [F] is built; its own
decisions are in `app/presenter/DESIGN.md`.

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
| ambiguity | a concept (`ConceptRef`) | a **pin**: `map_query(query, pins=...)` — built 2026-09-27 |

The pin cannot ride through the parser: the label would go to embedding search
again with no guarantee of landing on the concept picked, and it cannot go in
`QueryIn`, which never carries an XBRL identifier (schemas DESIGN §8.1). A
pinned concept still goes through the coverage check.

**Built** (2026-09-27, `app/chain.py`): a round whose answers are all pins
skips the parser — it re-maps the stored `QueryIn` (`Pending.query_in`), so no
model call and no chance of the phrase moving. A round with a new curated
answer re-parses with every answer so far and matches each pin to its element
**by phrase**, because a re-parse may number elements differently; a pin whose
phrase does not reappear finds nothing, so the phrase is ambiguous again and is
asked again, never dropped. `ask_again(question, pending, choices, answers=,
pins=)` holds that rule; it returns every answer and pin so far for the Web
Server to keep. Measured live: "accounts payable" picked → bound `pinned`, no
parser call, $68.96B for Apple FY2024; a round mixing "Gross margin" with that
pick → one re-parse, both parts answered.

The mapper looks a pinned concept up by `(taxonomy, name)`, never by the id it
arrived with — the pick comes back from `web.job`, which the web role can
write — and binds it through the same per-company coverage check as any other
(`resolved_by = "pinned"`, no similarity bar, no tie to report).

`resolve_choices` accepts an `option_id` only from the options **this round**
offered (`Pending.asks`) and raises `UnknownChoice` for anything else.

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
GET  /api/conversations/{id}               one reopened: every round, its picks, its reply
POST /api/jobs/{job_id}/feedback           {note}             "report a problem" (§8)
```

**Reopening (step 4, slice 0, 2026-09-28).** The history list carries no job
ids, so without `GET /api/conversations/{id}` the client could list a past
question but never open it. It returns a `ConversationView`: the question, then
each round in order with its `job_id`, status, the picks that started it
(`GivenAnswer`, in words — the option's label) and its reply. A failed round
carries the same fixed sentence the event stream sends, so the client never
writes one; a round not yet finished is watched on its events route as usual.
`GivenAnswer` is also what `Job.answers` stores, so the stored and the shown
answer are one class.

**Built (slice 7, 2026-09-27)**, `app/api/routes.py`. Every route needs a
signed-in reader and touches only their conversations; another reader's reads
as **404, never 403**, so an id tells a guesser nothing. Refusals on the wire:
an option never offered 400 `UNKNOWN_CHOICE`; answering a round still running,
one that asked nothing, or losing a race for the round number 409
`ROUND_STILL_RUNNING` / `NOTHING_TO_ANSWER` / `ROUND_CONFLICT`. The events route
checks ownership **before** it opens the stream — once streaming starts, a 404
can no longer be sent (a test fails without the check). A job is queued before
its id is returned, so no stream can miss its start. CSRF: the cookie is
`SameSite=Lax` and every write takes JSON, which a cross-site page can neither
send with the cookie nor post without a CORS preflight this server never
grants. Measured live over HTTP with the real models: register with a CLI
invite, sign in, ask "Apple's accounts payable in 2024?", stream to an
ambiguity, pick one, stream to $68.96B in 0.3 s, and see it in the history.

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

**Storage built (slice 4, 2026-09-27)**, `app/api/storage.py`, as `vf_web_role`
through `web_sessionmaker()` — which refuses to start without
`DATABASE_URL_WEB`, and refuses a URL that logs in as anyone else (pointing it
at the owner would undo every grant). How rounds chain: round *n* stores the
picks made against round *n-1*'s questions, round *n-1* stores what it asked
(a `chain.Pending`), and `round_inputs` replays every earlier round's picks —
so the answers and pins so far are rebuilt from the rows, never kept twice. A
new round is refused while the last is still running (`NotReady`), when it
asked nothing (`NothingToAnswer`), and for a pick it never offered
(`UnknownChoice`, before anything is written); two answers racing for the same
round number get one round and a `Conflict`. A job moves only while
unfinished: `done` and `failed` are final.

**The queue built (slice 5)**, `app/api/jobs.py`. `JobRunner`: one worker, so one
job at a time (a test fails the moment two overlap), in the order submitted.
Each job rebuilds its round (`storage.round_inputs`), runs `chain.ask` or
`chain.ask_again` inside `trace.collecting`, saves and publishes every stage,
and ends `done` with its reply and pending questions — or `failed` with the bug
sentence if the machinery itself broke. Its trace is written in a `finally`,
so the failed job is never the one without a record. A late watcher is sent
the events it missed, then live ones; a finished job's stream is its end, read
from the database, so a reload always gets the answer; a job unfinished yet
unknown to this process is orphaned and failed rather than waited on forever.
`event_stream()` yields the text/event-stream frames the route will send.
Measured with the real models and data: a first round that asks, then a round
of one pick answered in under half a second with no model call ($68.96B).

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

### The chart library, and swapping it

**Decided (2026-09-28): ApexCharts, via `ng-apexcharts`, pinned to an exact
version** — `"apexcharts": "7.6.1"`, never `^7.6.1` — so an update is always a
deliberate edit, never a side effect of an install. Chosen by eye from five
libraries drawing the same three real answers (ECharts, AG Charts, Chart.js,
ApexCharts, Highcharts). Tables are Angular Material.

**One component draws charts, and nothing else in the client imports the
library.** It takes one `StatView` / `LineView` / `BarView` and the
`AnswerView` rows it points into, and turns them into the library's options;
everything around it — the table, notes, citations, the reply's parts — is
plain Angular reading the contract. So replacing the library means rewriting
that one component: `AnswerView` already names nothing library-specific, and
every display string arrives made by [F], so the component formats only axis
ticks, by `unit_kind`. ECharts is the nearest alternative (Apache-2.0, a native
time axis); the comparison showed it draws the same three answers with the
same inputs.

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
Measured on a real job the same day: q040 (an average over 14 filers, so a
large plan and result) came to ~53 KB — parser prompt 20 KB, SQL prompt 3.6 KB.

### How it is collected — without changing the chain

**Built 2026-09-27.** A small collector, `app/trace.py`, holds the current
job's trace in a context variable, so concurrent jobs cannot see each other's
(tested). The places that already see the raw material append to it **when one
is set**: `propose()` in the parser and `_ask()` in the generator (prompt and
raw reply on every exit — a truncated or empty reply is the one worth reading),
`execute()`'s `_log` (statement, verdict, error), and `answer()` for a
statement `validate()` **rejected**, which never reached the executor's log
and so was written down nowhere before. When
none is set — the evals, the CLI, every test — they do exactly what they do
today. No signature changes, and `data/retrieval_log.jsonl` keeps being
written, gaining the job id when there is one.

[B] opens the trace when a job starts (`trace.collecting(job_id)` around
`chain.ask`) and writes the row once, in a `finally`, whether the job
answered, refused or crashed: `app/api/trace.write_trace` — one INSERT, never
read back. `code_version` is `CODE_VERSION` when set (the container has no
`.git`), else the git revision with `+dirty`. A job cut off by a restart has
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

**The generator is `openapi-typescript`** (decided 2026-09-28): types only, no
generated runtime code; a small hand-written service makes the calls with
Angular's `HttpClient`. Chosen over ng-openapi-gen (generated services, method
names taken from FastAPI Users' long operation ids) and `@hey-api/openapi-ts`
(not yet 1.0). The event stream is hand-written whatever the generator, since
OpenAPI cannot describe a stream as a sequence of typed messages.

**Built (step 4, slice 2, 2026-09-28).** `uv run python -m app.api.openapi`
writes the contract to `web/openapi.json`, committed, and a pytest fails while
that file differs from what the server would publish. `npm run api:types`
generates `web/src/app/api/openapi.d.ts` from it, committed too, and
`npm run api:check` — part of `npm test` — fails while the two disagree. So a
model changed on the server fails pytest, then the client's tests, then its
build at every line that no longer fits. Two generator settings matter:
`--immutable` (every server type read-only: the client renders, it does not
edit) and `--default-non-nullable false` (a request field with a default stays
optional — without it `UserCreate` demanded `is_superuser` from the browser).
The GitHub routes are always in the contract: the writer builds the app with a
describe-only config rather than this machine's `.env`.

**The generator runs on TypeScript 6 by override, and that is re-checked.**
`openapi-typescript` 7.13 declares TypeScript 5; Angular 22 needs 6.0. The
user's call (2026-09-28): keep it, via `package.json` `overrides`, but
**every time the types are regenerated or either package is upgraded,
regenerate under TypeScript 5.9.3 and require a byte-identical file** — the
two commands are in `web/README.md`. Alternatives considered and set aside:
running the generator in its own package on TypeScript 5, or switching to
`@hey-api/openapi-ts` (declares 6, not yet 1.0).

**Two things `/openapi.json` needed first** (2026-09-28, `tests/test_openapi.py`):

- **The stream's events were unpublished.** FastAPI cannot see a streamed
  body's type, so `StageEvent`, `DoneEvent` and `FailedEvent` never reached the
  schema. `server._publish_stream_events` adds `JobEvent` (a union on `kind`)
  and the events route points at it. A model FastAPI already published must
  mean the same both ways, or startup fails.
- **Always-sent fields read as optional.** A field with a default — every
  `kind`, `Reply.blocking` — was optional in the output schema, so the
  generated types would have said `kind?:` and TypeScript could not narrow a
  union on it. `_Base` sets `json_schema_serialization_defaults_required`,
  which changes output schemas only: a request may still leave `kind` out.

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

### Built (slice 6, 2026-09-27)

`app/api/auth.py`, `server.py`, `admin.py`; proved against the test database
and live on `.env`.

- **Invitations are claimed with one atomic `UPDATE ... RETURNING`** on both
  registration and a new GitHub account, so two sign-ups racing for one
  code make one account (tested); a claim is released if the account then
  fails to be made — a short password, an address already registered. A
  missing, wrong, spent, expired or other-address code all read the same,
  `INVITE_REQUIRED`, telling a guesser nothing. Codes are hashed with
  SHA-256, not a password hash: they are random, not chosen, and the lookup
  is one comparison. Case, dashes and spaces are forgiven.
- **Found in the library: `/authorize` passes any `scopes` the caller sends
  on to GitHub**, so `?scopes=repo` would have asked for write access to a
  person's repositories. `NarrowGitHub` asks for `read:user` and `user:email`
  whatever is sent (tested, and checked live).
- **The GitHub callback forces `associate_by_email` and
  `is_verified_by_default` off** inside the user manager, whatever the router
  is configured with. A GitHub email matching an existing account is refused
  rather than joined to it (tested).
- **Registration ignores `is_superuser` and `is_verified`** in the request
  (tested); the database would refuse the first anyway (§11).
- **Passwords need 12 characters.** Signing secrets need 32, checked at
  startup (RFC 7518 §3.2), as are all three being present.
- **Sessions**: an httpOnly `SameSite=Lax` cookie, `vf_session`, for 30 days,
  backed by a `web.access_token` row that sign-out deletes (tested: an old
  cookie replayed afterwards gets 401). Expired rows are purged at startup
  and every six hours — the library never removes them.
- **Mounted**: login, logout, register, GitHub authorize and callback,
  `GET /api/me`. **Not**: verification, forgot/reset password (no sender),
  and the library's `/users/{id}` admin routes — administration is the
  owner's CLI (`python -m app.api.admin invite | make-admin | reset-password`).
  A reset password is generated and printed once, never typed.
- **For step 4:** GitHub's callback currently returns to the API itself,
  which sets the cookie and shows an empty page. With a browser client the
  redirect should land on a client page that calls the API's callback — a
  change of `GITHUB_OAUTH_REDIRECT_URL` and one added callback URL on the
  GitHub app, not of code.

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

**Built 2026-09-27**, and proved live on the test database once the `web`
migration ran there (`tests/test_roles.py`, "The web role"): every allowed cell
of the grid above, every refused one, FastAPI Users creating a user as this
role, and a trace written without reading it back — which found that the ORM's
`INSERT ... RETURNING created_at` needs `SELECT`, so the two write-only models
turn `eager_defaults` off. Built: `RoleSpec` (`schema`, `writes`, `read_only`), `WEB`, `--check`
across both schemas, `DATABASE_URL_WEB`. The readers' statements are pinned in
`tests/fixtures/role_statements.sql` and must stay byte-identical once every
statement about `web` is removed; the web role's are pinned apart, and only
the web file is rewritten by `UPDATE_PINNED=1`.

**Found by the new `--check`, not caused by it, and closed:** `public` granted
`USAGE` to `PUBLIC`, so the per-role `REVOKE ALL ON SCHEMA public` did not
take — the retrieval role could cast to `public.vector`, though it could read
no table there. `provision()` now ends with `REVOKE USAGE ON SCHEMA public FROM
PUBLIC` (one line added to the readers' pinned file, on the user's call); the
mapper keeps its explicit grant. It is database-wide: a login added later that
needs `public` must be granted it by name.

### Alembic

One history for the database (`ALEMBIC.md`). The web models get their own
`MetaData(schema="web")` beside `xbrl`'s, `env.py`'s `include_name` widens to
both schemas, and the first migration creates `web` explicitly, as the first
one did `xbrl`. The test database is migrated by hand, as ever (`ALEMBIC.md`
step 5).

## 12. Containers

*Decided:* the Web Server runs in its own container in `docker-compose.yml`.

**Built (slice 8, 2026-09-27)** — `api.Dockerfile`, `api.Dockerfile.dockerignore`, the `api`
service:

- **`api`** — Python 3.13-slim with `uv`, a frozen install from `uv.lock`,
  one `uvicorn` worker (§4), a non-root user, port 8000 on this machine's
  loopbacks only. `depends_on` `db` and `ollama` healthy; its own health
  check calls `GET /api/health`, which runs `SELECT 1` as the web role.
  Behind the `web` **profile**, so BOOTSTRAP's first `docker compose up -d`
  does not start it before the migration and roles it needs exist:
  `docker compose --profile web up -d`.
- **Only the credentials it needs, by name** — the three role URLs, the
  secrets, GitHub — never `.env` whole. The owner's login and the test
  database's never enter the container (checked: none in its environment).
  `DATABASE_URL`, which the code requires, is the **web role's** there, so
  even an accidental owner session has only `vf_web_role`'s grants.
- **`DATABASE_HOST=db:5432`** points every database URL at the compose
  service while keeping its login (`app/config.py`), so `.env` stays written
  for Python on the host and no password is written twice.
- **Nothing secret in the image** — `api.Dockerfile.dockerignore` (Docker pairs
  it with the Dockerfile by name) keeps `.env`, `data/`,
  `.venv` and `.git` out (checked in the built image). `CODE_VERSION` is a
  build argument, the git revision, since there is no `.git` inside to ask.
  `./data` is mounted, so `retrieval_log.jsonl` survives the container.
- Measured end to end through the container against the real database: an
  invite, registration, a `Secure` 30-day session, Apple's FY2024 revenue
  streamed to $391.04B, the trace carrying the container's `CODE_VERSION`;
  the temporary user was deleted afterwards.
- **Not for a public server as it stands.** `docker-compose.yml` still
  commits `postgres`/`postgres` and pgAdmin's password, publishes database
  and Ollama ports (loopback only, but a server should publish none), and
  the container's secrets are readable with `docker inspect`. A production
  override — no published ports but the proxy's, no `db-test` or
  `pgadmin`, secrets from a secret store — is the deployment step's.
- **[A] in development** — `ng serve` with a proxy to `api`, so the browser
  sees one origin.
- **[A] deployed** — later, a small web-server container (nginx) serving the
  built bundle and proxying `/api`, with response buffering off for the event
  stream. One origin is what keeps the cookie and the stream simple (§10).

## 12a. Building it — decisions for step 3 (2026-09-27)

Eight slices, one at a time: (1) the chain as one function, (2) the ambiguity
pin, (3) the trace collector, (4) storage, (5) the job queue and its events,
(6) sign-in, (7) the routes, (8) the container. The user's answers:

- **The chain lives in `app/chain.py`**, outside `app/api/`, as
  `ask(question, answers) -> Reply`. The route calls it; `evals/run.py` moves
  onto it in the user's testing update, not in step 3.
- **One whole job at a time**, parse through present — the GPU is the
  bottleneck either way, and one gate is simpler than one per model call.
- **A session lasts 30 days** from sign-in, a setting; expected to shorten
  before deployment.
- **The fixed sentences** a reader gets when no curated reason exists (§1):

  | stage | sentence |
  |---|---|
  | parse | "I couldn't work out what that question is asking for. Try naming the figure, the company and the period explicitly." |
  | execute | "The figures for this query came back in a form that mismatches what I was expecting, so I haven't shown them." |
  | present | "Something went wrong attempting to display the results." |
  | anything else (a bug) | "Something went terribly wrong, likely a backend bug. Please contact your database administrator, jk, time to debug." |

- **Run on the host during slices 1–7** (uvicorn with reload); the container
  arrives in slice 8.
- **Conversation history is in**: a "my past questions" list.
- **Administration is CLI-only** in step 3 — invites, the first administrator,
  password resets. An admin page may come later.
- **Invite codes**: 12 characters in three groups (`K7QM-3XRD-9TPW`) from an
  alphabet without look-alikes, 14 days by default, shown once, stored hashed.
- **The web role never falls back to the owner.** The read-only roles' session
  factories fall back to `DATABASE_URL` with a warning when their URL is unset;
  the Web Server refuses to start without `DATABASE_URL_WEB` instead, because
  the owner can do everything its grants were designed to prevent.

## 13. Open

- **Email sender** for verification and reset. Deferred by the user; until
  then accounts are unverified and invitations stand in for it (§10).
- **Evals onto the one orchestrator.** `evals/run.py::run_one` is [B] without
  the browser; once `app/chain.py` exists it should call that, or the evals
  measure a surrogate (HANDOFF §3). Deferred by the user to the testing update.
- **Rate limiting** — every request costs GPU seconds, and login has no
  protection against password guessing (§10). Deferred by the user; needed
  before the site is reachable from outside.
