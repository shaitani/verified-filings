# The web end — [A] Web Client, [B] Web Server, [F] Presenter

```
[A] Web Client   web/            Angular + ngrx/signals. Renders; decides nothing.
      ↕  REST + server-sent events, same origin, cookie session (§10)
[B] Web Server   app/api/        FastAPI, in its own container (§12). Runs the
                                 chain, owns users, conversations and jobs; the
                                 only thing that talks to [A].
      ↓  app/chain.py: parse_question → map_query → answer → present
[C] Query Parser → [D] Query Mapper → [E] Executor → [F] Presenter
```

The chain lives in `app/chain.py`, outside `app/api/`, so it has no web
dependency: `ask()` runs one round, `ask_again()` a round that answers
questions put back. Request and response models live in `app/api/`, not
`app/schemas/`; `AnswerView` is the exception, in `app/schemas/answer_view.py`,
because it is [F]'s contract and [F] must not import the Web Server. [F]'s own
decisions: [`app/presenter/DESIGN.md`](../presenter/DESIGN.md).

---

## 1. A reply has parts

A question is answered **per part** (semantic DESIGN §8e). There is no
three-way branch — answer / clarify / refuse — to route on. There is one
reply, and every part says what became of it.

Parts come from three stages:

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
not on offer (retrieval DESIGN §6). It does **not** touch the parts the mapper
already refused or asked about. And a `ResultSet` exists in that case; the
chain checks `is_answerable` before any row leaves the server.

```
Reply
  conversation_id, job_id
  blocking     Refusal | null     stage + reason. Set → no part carries figures.
  parts[]      one per thing asked (metric and narrative elements)
                 text       the asker's phrase, verbatim
                 outcome    answered | none | asked | refused
                 reason     when refused, or none (why nothing is shown)
                 ask        when asked (§3)
  answer       AnswerView | null  the figures for the answered parts, with notes
                                  and citations (§5, §6)
  status       derived from the parts, never decided separately
```

**`none` is an answer.** The question ran and nothing met the condition it set —
"net loss" in a year nobody had one, "revenue over $10T". It carries no figures;
`reason` states the condition and any caveat the binding has, and the client
shows it with the answered tick, not the error colour. The reply's `status`
counts it as answered (a reply of only `none` parts is `answered` with
`answer: null`). It arises only where a threshold covers every bound metric
(retrieval DESIGN §6); an empty result with none is still a refusal.

Notes travel on the answer, not the reply: with no figures there is nothing
for them to qualify. A blocking refusal is shown alone — a knock-on refusal it
caused is not listed beside it.

**Whose words reach the reader.** Mapper reasons are curated sentences written
for a person (`Unresolved.reason`, `unavailable` entries,
`Clarification.question`) and go through as written. Parser and executor
exception messages are written for a model (`validate`'s invite a retry) or
for debugging; the reader gets one fixed sentence per stage, and the message
goes to the trace (§8). The sentences, as the user worded them:

| stage | sentence |
|---|---|
| parse | "I couldn't work out what that question is asking for. Try naming the figure, the company and the period explicitly." |
| execute | "The figures for this query came back in a form that mismatches what I was expecting, so I haven't shown them." |
| present | "Something went wrong attempting to display the results." |
| anything else (a bug) | "Something went terribly wrong, likely a backend bug. Please contact your database administrator, jk, time to debug." |

## 2. [F] Presenter: deterministic, not prose

`ResultSet → AnswerView`, a typed structure the client renders, written in
Python with no model.

**Why not a model writing prose.** It is a new place for a plausible wrong
number — a model retyping 94,930,000,000 — and "carry notes through rather
than summarising them away" is the one rule a summariser reliably breaks. The
lesson of retrieval DESIGN §4 applies directly: take the job away from the
model rather than check its work.

**Why not hand Angular the raw `ResultSet`.** The domain decisions would move
into TypeScript, where nothing tests them against the data:

- **units** — `pure` is a percentage, `USD` wants scaling, `USD/shares` is
  per-share. A ratio rendered as money is GAPS D1.17, moved to the client;
- **`derivation`** — a growth row and a figure row are different series;
- **mixed granularity** — annual and quarterly on one axis reads as a fourfold
  spike;
- **numbers** — Pydantic serialises `Decimal` as a JSON string, so the client
  would be parsing and formatting money itself.

So each cell carries the exact value (a string), a display string and its unit
kind, and [A] shows what it is given. [F] never sees a plan: what it needs —
the `ResultSpec` — travels on the `ResultSet`.

## 3. Questions back to the asker

**An ambiguity is a real question back, and is marked as one.**
`Clarification` (a person curated the choices) and `Ambiguity` (the machine
could not choose between raw concepts) share one wire shape; the ask carries
its `kind` so [A] shows an **"ambiguous"** tag on the second. The tag is for
debugging as much as for the reader: an ambiguity means the alias file has a
gap, and the tag is how one gets noticed. The evals score them apart (`asked`
against `confused`).

```
Ask        ask_id, kind: clarification | ambiguity, question, options[]
Option     option_id, label, description
AnswerIn   ask_id, kind: option, option_id          ← the only kind for now
```

An ambiguity's options are its candidates: `label` is the concept's label
("Payments of Dividends"), `description` its `taxonomy:name`.

**How each binds on the next round:**

| ask kind | an option names | next round |
|---|---|---|
| clarification | a curated metric | `(question, option label)` into `parse_question(answers=)` (parser DESIGN §10) |
| ambiguity | a concept (`ConceptRef`) | a **pin**: `map_query(query, pins=...)` |

The pin cannot ride through the parser: the label would go to embedding search
again with no guarantee of landing on the concept picked, and `QueryIn` never
carries an XBRL identifier. A round whose answers are all pins **skips the
parser**: it re-maps the stored `QueryIn`, so no model call and no chance of
the phrase moving. A round with a new curated answer re-parses with every
answer so far and matches each pin to its element **by phrase**, because a
re-parse may number elements differently; a pin whose phrase does not
reappear finds nothing, so the phrase is ambiguous again and is asked again,
never dropped.

The mapper looks a pinned concept up by `(taxonomy, name)`, never by the id it
arrived with — the pick comes back from `web.job`, which the web role can
write — and binds it through the same per-company coverage check as any other
(`resolved_by = "pinned"`, no similarity bar). `resolve_choices` accepts an
`option_id` only from the options **that round** offered and raises
`UnknownChoice` for anything else.

**In the client** (`web/src/app/conversation/`): a blocking refusal first, as
an alert; then each part — answered, none (answered, with the condition nothing met), refused with its reason, or asked. An ask
is a radio list of its options. The latest round's asks are answered
together: **"Continue" waits for a pick on every one**, because the server
would take some and ask the rest again. An earlier round's asks stay on the
page, closed, showing the pick. A refused answer's code (`UNKNOWN_CHOICE`,
`ROUND_CONFLICT`, …) is shown in the reader's words.

**Options only, not free text** — for now. An option is known to resolve
(every clarify option is validated at load); free text reopens the whole
chain, changes the question's scope through the answer channel, cannot pin an
ambiguity, and is unmeasured. How it stays addable without a refactor:
[docs/FUTURE.md](../../docs/FUTURE.md#quality-of-answers).

## 4. Conversations and jobs

A question takes ~10 s on average and ~60 s at worst, on one GPU where Ollama
serves one request at a time. So a question is a **job**, and the browser
follows it rather than waiting on a request.

```
POST /api/conversations                    {question}         → {conversation_id, job_id}
POST /api/conversations/{id}/answers       {answers[]}        → {job_id}
GET  /api/jobs/{job_id}/events             text/event-stream
GET  /api/jobs/{job_id}                    the Reply, once done (reconnects, reloads)
GET  /api/conversations                    the signed-in reader's history; each line's latest
                                           round status and, once done, its reply_status
GET  /api/conversations/{id}               one reopened: every round, its picks, its reply
POST /api/jobs/{job_id}/feedback           {note}             "report a problem" (§8)
```

Events: `queued` → `parsing` → `mapping` → `fetching` → `presenting` → `done`
(carrying the `Reply`) or `failed`. `queued` is real: jobs run one at a time,
and the asker should see they are waiting rather than that it is slow.

**The history carries each conversation's standing.** `reply_status` is the
latest round's `Reply.status` once it is done, so the list can say "waiting for
your answer" (`asked`) as well as answered, partly answered or refused. It is
read from the stored reply's JSON (`reply ->> 'status'`), never the whole
reply, and it is set exactly when the round is done.

**The client's side** — `web/src/app/history/` and the thread page. The past
questions sit in a sidebar, beside the thread on a wide screen and behind a
menu button on a narrow one; each opens its thread, and a question waiting for
the reader stands out. The list is re-read on signing in, on opening a
conversation, when a round ends and after an answer, and emptied on signing
out. It loads once the page is idle (`@defer`), off the first screen's
download. Every finished round offers **"Report a problem"** with an optional
note; the round then reads "Reported" for the rest of the visit — the web role
may write a report but never read one (§11), so a reload cannot know. The
stage line's clock counts from the round's queueing and ticks each second.

**The server holds the conversation, in Postgres** (§11). A conversation is
the original question; each round is a job, storing the picks that started it
and what it asked. Every conversation and job belongs to a user, and a user
reads only their own.

**Routes** (`app/api/routes.py`). Every route needs a signed-in reader. Open to
anyone are only signing in (§10) and `GET /api/health`; a test builds the list
of mounted routes from the app and requires 401 from every other one, without
a cookie and with a forged one, so a route added without the check fails it.
FastAPI's own `/docs`, `/redoc` and `/openapi.json` are unmounted unless
`API_DOCS` is on, which only a development `.env` sets.
Another reader's conversation reads as **404, never 403**, so an id tells a
guesser nothing. Refusals on the wire: an option never offered is 400
`UNKNOWN_CHOICE`; answering a round still running, one that asked nothing, or
losing a race for the round number is 409 `ROUND_STILL_RUNNING` /
`NOTHING_TO_ANSWER` / `ROUND_CONFLICT`. The events route checks ownership
**before** it opens the stream — once streaming starts, a 404 can no longer be
sent. A job is queued before its id is returned, so no stream misses its
start. CSRF: the cookie is `SameSite=Lax` and every write takes JSON, which a
cross-site page can neither send with the cookie nor post without a CORS
preflight this server never grants.

**Storage** (`app/api/storage.py`), as `vf_web_role` through
`web_sessionmaker()`, which refuses a URL that logs in as anyone else. Round
*n* stores the picks made against round *n−1*'s questions, and round *n−1*
stores what it asked (a `chain.Pending`); `round_inputs` replays every earlier
round's picks, so the answers and pins so far are rebuilt from the rows, never
kept twice. A new round is refused while the last is still running, when it
asked nothing, and for a pick never offered (before anything is written); two
answers racing for one round number get one round and a `Conflict`. `done` and
`failed` are final.

**The queue** (`app/api/jobs.py`). `JobRunner`, one worker: one job at a time,
in the order submitted — one gate is simpler than one per model call, and the
GPU is the bottleneck either way. Each job rebuilds its round, runs
`chain.ask` or `chain.ask_again` inside `trace.collecting`, saves and publishes
every stage, and ends `done` with its reply and pending questions — or
`failed` with the bug sentence if the machinery broke. Its trace is written in
a `finally`, so a failed job is never the one without a record. A late watcher
is sent the stages it missed, then live ones; a finished job's stream is its
end, read from the database; a job unfinished yet unknown to this process is
orphaned and failed rather than waited on forever. The gate and the streams
live in the process, so **the Web Server runs one worker**, and a job found
unfinished at startup — cut off by a restart — is marked `failed`.

**How many may queue** (`limits.py`, checked in `routes._admit`). Every round
is a GPU job, asking or answering, so every round is counted:

| limit | refusal |
|---|---|
| 2 unfinished rounds per reader | `429 TOO_MANY_QUESTIONS`, no `Retry-After`: it frees when one finishes |
| 200 rounds per reader in any 24 hours; administrators exempt | `429 DAILY_LIMIT`, `Retry-After` when the oldest leaves the 24 hours |
| 25 unfinished rounds across the server | `503 SERVER_BUSY`, `Retry-After: 60` |

Counted from the rows in one statement (`storage.admission`), so a restart
resets nothing and a round counts the moment it is written. Counting and
writing share one lock, so two requests at once cannot both take the last
place.

**Reopening.** `GET /api/conversations/{id}` returns a `ConversationView`: the
question, then each round in order with its `job_id`, status, the picks that
started it (`GivenAnswer`, in words — the option's label) and its reply. A
failed round carries the same fixed sentence the event stream sends. The
stored and the shown answer are one class.

**In the client.** Asking creates the conversation and opens `/c/<id>`; the
thread page reads it with the route above and, if its last round is still
running, follows that round over `EventSource`. **The database is the
record**: when a round ends or the stream drops, the page re-reads the
conversation rather than piece the round together from events, then
re-attaches if it is still running (three drops in a row and the reader is
told). The browser's own silent reconnect is switched off. So a reload
mid-round resumes by construction.

**A known property, not a bug.** Ollama's prompt cache changes replies
([docs/TESTING.md](../../docs/TESTING.md#the-prompt-cache)), so with several
askers an answer can depend on what the previous one asked. Unloading between
jobs would remove it at a cost in latency.

## 5. What to draw

`ResultSpec.shape` is set by what resolved, and a question asking to *see* it
drawn becomes `series` (parser DESIGN §5). [F] turns that into views:

| `shape` | view |
|---|---|
| `scalar` | a stat card |
| `series` | a line chart over **real dates**, one line per company or metric |
| `ranking` | a sorted bar chart |
| `table` | a table (with comparison bars along companies) |

**A series is plotted on a time axis, not on fiscal labels.** Each point sits
at its `period_end`, with its fiscal label and window in the tooltip. "FY2024"
is a different span per filer, and on a real time scale that — and the 83–97
day spread of quarter lengths — is visible rather than flattened into evenly
spaced labels.

- **The table always ships**, beside any chart. A chart hides the exact figure
  and has nowhere to hang a citation.
- **Notes sit above the view**, not in a tooltip. A `narrower_than_asked` or
  `partial_coverage` note is part of the answer.
- **A ranking is drawn in order or not at all.** [F] sorts by
  `ResultSpec.rank` and says which end is first; `BarView` refuses bars that
  are not monotonic in its stated direction.
- **Mixed granularity splits** into separate panels.
- **Derived rows are their own series**, labelled by `derivation`.

**One component draws charts** (`web/src/app/answer/chart/`), and nothing else
in the client imports the chart library — ESLint enforces it. It takes one
view and the rows it points into; everything around it is plain Angular
reading the contract, and every display string arrives made by [F], so the
component formats only axis ticks. Replacing the library means rewriting that
folder. The library and why: [docs/TOOLS.md](../../docs/TOOLS.md#web-client-web).

`AnswerPanel` lays out, in order: notes (verbatim) and conditions ("Only
where: revenue over $100.00B"), the views, the table, and the sources.
Tooltips show [F]'s strings, escaped — they are data, never markup.

## 6. Citations with every answer

Every answer shows what it was answered *as*: per binding, the concept's label
and name, the expression over its operands, the companies, and each figure's
actual date window. This is the cheap half of GAPS G1 — nothing checks that
the answer matches the question, and a chart makes a wrong answer look more
finished. Showing the interpretation does not close that gap; it lets the
reader see it.

In the client, "Answered as" numbers each source in the order the table first
cites it and shows: the label and `taxonomy:name`, the arithmetic in operand
names for a computed figure ("Assets, Current ÷ Liabilities, Current"), the
companies it answered for, its notes, and how it was chosen — **curated**
(alias), **your pick** (pinned), or **unreviewed match** (embedding, in the
error colour). Open by default up to three sources.

## 7. Never sent to the client

The SQL, the `QueryPlan` and the `QueryIn`. The statement is the one thing a
reader could be quoted by accident; the plan carries rationales and raw
candidates written for machines. What a reader needs of either reaches them
through §1's parts and §6's citations.

## 8. Kept, so a run can be debugged afterwards

What is never sent is always kept, so "why did this question go wrong" can be
handed to someone — or to Claude — without a replay. One `web.job_trace` row
per job: it joins to its conversation and user, and "every job where an
ambiguity was asked" is a query rather than a grep.

| field | why |
|---|---|
| `code_version` | git revision and a dirty flag (`CODE_VERSION` in the container, which has no `.git`). A trace from last week's prompts is a different experiment |
| `models` | parser, generator and embedding model tags |
| `query_in`, `plan`, `result` | the three contract objects, each null past the stage that stopped the job |
| `model_calls` | every model call in order: stage, model, the **full prompt**, the **raw reply**, seconds. The parser's rejected first reply and its repair are both here |
| `statements` | every SQL statement: text, `validate` verdict, execution error |
| `timings` | per stage |
| `errors` | every exception: stage, type, message, traceback |

Prompts are stored whole, not rebuilt from the revision: a dirty tree cannot
be rebuilt. A job is ~30–50 KB before Postgres compresses it (q040, an average
over 14 filers: ~53 KB).

**Collected without changing the chain.** `app/trace.py` holds the current
job's trace in a context variable, so concurrent jobs cannot see each other's.
The places that already see the raw material append to it **when one is
set**: `propose()` in the parser and `_ask()` in the generator (prompt and
raw reply on every exit — a truncated reply is the one worth reading),
`execute()`'s log, and `answer()` for a statement `validate()` rejected.
When none is set — the evals, the CLI, every test — they behave as before, and
`data/retrieval_log.jsonl` keeps being written. [B] writes the row once, in a
`finally`: `app/api/trace.write_trace`, one `INSERT`, never read back.

**Who can read it: not the Web Server.** `vf_web_role` has `INSERT` on
`web.job_trace` and no `SELECT`, so no bug in the reply path and no future
endpoint can hand a trace to a browser. Reading is done on the host with the
owner's credential ([docs/TESTING.md](../../docs/TESTING.md#reading-a-readers-question)).

**Reporting a problem.** A reply carries a "report a problem" control with an
optional note, written to `web.job_feedback`. It is how a wrong-looking answer
becomes a `--flagged` job rather than something remembered later.

## 9. Types across the boundary

The client's TypeScript types are generated from FastAPI's OpenAPI schema,
never written by hand: two hand-kept copies of one contract drift. The chain
of checks — pytest keeps `web/openapi.json` current, `npm test` keeps the
generated types current, the compiler checks every use — is in
[web/README.md](../../web/README.md#the-contract); the generator and its
settings in [docs/TOOLS.md](../../docs/TOOLS.md#web-client-web). The event
stream's client is hand-written, since OpenAPI cannot describe a stream as a
sequence of typed messages.

Two things the published schema needed:

- **The stream's events are published explicitly.** FastAPI cannot see a
  streamed body's type, so `server._publish_stream_events` adds `JobEvent` (a
  union on `kind`) and points the events route at it. A model FastAPI already
  published must mean the same both ways, or startup fails.
- **Always-sent fields are required.** A field with a default — every `kind`,
  `Reply.blocking` — was optional in the output schema, so the generated types
  said `kind?:` and TypeScript could not narrow a union on it. `_Base` sets
  `json_schema_serialization_defaults_required`, which changes output schemas
  only: a request may still leave `kind` out.

The contract always includes the GitHub routes: the writer builds the app with
a describe-only config rather than this machine's `.env`.

What the browser sends is named with an `In` suffix (`NewConversationIn`,
`AnswersIn`), the repo's mark for inbound, validated input; what the server
sends is not. Not a `User` prefix: FastAPI Users already names its account
models `UserRead` / `UserCreate`.

## 10. Users and sign-in

User accounts, signing in with email and password or with GitHub, through
**FastAPI Users** (its maintenance status and why it was accepted:
[docs/TOOLS.md](../../docs/TOOLS.md#web-server)). The rest of the app depends
only on a `current_user` dependency.

### What the library does, and what it leaves to us

Read from its source:

- **Reset and verification tokens are stateless** signed JWTs, each with its
  own secret. A reset token carries a fingerprint of the password hash, so it
  stops working once the password changes. No table.
- **The OAuth flow** sets its own CSRF cookie and signs its `state`. No table.
- **Sessions** are `web.access_token` rows checked against a maximum age.
  Nothing in the library deletes expired rows — ours are purged at startup and
  every six hours.
- **Email lookup is case-insensitive, the uniqueness is not**, so two
  concurrent registrations as `Bob@x.com` and `bob@x.com` could both land. Our
  migration adds a unique index on `lower(email)`.
- **No brute-force protection** on login — ours is below.
- **The password check runs on the event loop.** Argon2 at 64 MB and ~50 ms a
  hash, synchronously, on the one worker that streams every reader's stages:
  a login flood would freeze the site. Ours runs it in a thread (Argon2 frees
  the GIL; measured, the longest stall fell from 162 ms to the timer's 19).
- **No first administrator.** The owner's CLI makes one.

### Limits on signing in

`SignInGuard` (`auth.py`), every number in `limits.py`, counted in memory over
a sliding window (the server is one process; a restart only forgets).

| limit | counts | stops |
|---|---|---|
| 10 per 15 min per **address and IP** | password attempts | guessing from one machine — a stranger spends their own budget, not the owner's |
| 10 per 15 min per **address**, from browsers without `vf_device` | password attempts | guessing one address from many IPs |
| 30 per 15 min per **IP** | logins, registrations, GitHub | one machine trying many addresses or codes |
| 5 a second across the server | password attempts | a flood from any number of IPs taking the CPU |

**Attempts are counted, not failures**, so every check runs before the hash
and needs no outcome; a session lasts weeks, so a real reader never comes near.
A refusal is `429 TOO_MANY_ATTEMPTS` with `Retry-After`, charged to no limit —
hammering while refused does not push the wait out.

**No stranger can lock an owner out.** A plain lockout per address is a switch
anyone who knows the address can throw. So a successful sign-in, by password
or GitHub, leaves `vf_device`: a signed JWT (`AUTH_DEVICE_SECRET`) naming the
SHA-256 of the address, httpOnly, `SameSite=Strict`, sent only to
`/api/auth`, a year long. A browser carrying it for the address tried is
exempt from the per-address limit. It signs no one in, so a stolen one only
exempts its holder from one limit. The cost: when strangers have spent an
address's budget, a browser it has never signed in on waits 15 minutes —
existing sessions and GitHub sign-in still work.

**Who is asking** is `limits.client_ip`, the connecting address. Behind a
proxy that is the proxy, so the deployment's front door must be trusted for
the real one (docs/FUTURE.md). Two-factor sign-in and a CAPTCHA after
failures are later options in the same file.

### Guard rails on GitHub sign-in

- **Scopes are narrowed.** `httpx-oauth` asks for `user` (read *and write*) by
  default, and the library's `/authorize` passes on any `scopes` the caller
  sends — `?scopes=repo` would ask for write access to a person's
  repositories. `NarrowGitHub` asks for `read:user` and `user:email` whatever
  is sent.
- **The email GitHub returns is not checked as verified**, so
  `is_verified_by_default` and `associate_by_email` are forced off inside the
  user manager, whatever the router is configured with. A GitHub email
  matching an existing account is refused rather than joined to it — the
  takeover the library's own documentation describes.
- **GitHub's access token is stored** in plain text, because the library's
  column requires it. Nothing calls GitHub after sign-in, and the narrowed
  scopes keep the token's reach small.

### Sessions

An httpOnly cookie, `vf_session`, `SameSite=Lax`, backed by a database token —
not a bearer JWT. The event stream decides it: `EventSource` cannot set an
`Authorization` header, and a cookie goes with it automatically. A database
token can also be revoked, which a JWT cannot before it expires; sign-out
deletes the row, and an old cookie replayed afterwards gets 401. **30 days**,
a setting (`AUTH_SESSION_DAYS`) expected to shorten before deployment. [A] is
served from the same origin as the API, so there is no CORS to configure.

Configuration, in `.env`, never in git: four signing secrets (each at least
32 characters, checked at startup), the GitHub client id and secret, the
redirect URL, the session length, the cookie's `Secure` flag. Passwords need
12 characters.

### Sign-up is by invitation

Every question costs GPU time on one machine, so no one registers uninvited.
An allowlist of addresses is not enough while email is unverified: whoever
registers an allowed address first would own it. So an invitation is a
**single-use code** — 12 characters in three groups, `K7QM-3XRD-9TPW`, no
look-alike letters, 14 days by default — shown once and stored hashed, that
you hand to the person yourself. Nothing is emailed. It is made from the
owner's CLI (`invite`), which alone makes the very first, or by an admin.

**A code is bound to no address.** The code is the secret; the address is
unverified either way, so binding one would prove nothing. Whoever holds it
registers once, with the address they type. The costs, accepted: a leaked code
works for whoever has it until it is spent, revoked or expired; and someone
could register an address that is not theirs, which an admin sees in the
users list and can delete. A code can be **revoked** while unspent.

**Signing up through GitHub takes a code too.** The register page sends it to
`POST /api/auth/github/invite` first, which checks it — so a wrong one is said
before the trip to GitHub — without spending it, and holds it in `vf_invite`:
httpOnly, `SameSite=Lax`, ten minutes, sent only to `/api/auth/github`. The
callback claims it for a new account and the cookie is cleared. It is not
signed: the claim checks the code against the database, so the cookie proves
nothing on its own. A returning GitHub account needs no code.

The check sits on **both** ways an account is created — registration and the
first GitHub sign-in — because the library creates OAuth users by a different
path. An invitation is claimed with one atomic `UPDATE ... RETURNING`, so two
sign-ups racing for one code make one account, and a claim is released if the
account then fails (a short password, an address already registered). A
missing, wrong, spent, revoked or expired code all read the same,
`INVITE_REQUIRED`. Codes are hashed with SHA-256, not a password hash: they are
random, not chosen, and the hash is unique, the one way an invite is found.
Case, dashes and spaces are forgiven. Registration ignores `is_superuser` and
`is_verified` in the request.

**Email is deferred.** Until a sender is chosen, accounts work unverified, the
verify and forgot-password routes are **not mounted**, and a password is reset
from the CLI (generated and printed once). The invitation code stands in for
proof of the address.

**Mounted:** login, logout, register, GitHub authorize, callback and invite,
`GET /api/me`. **Administration** is the admin routes and the owner's CLI
(§13).

### The client's side

`web/src/app/auth/`. The client holds no credential: the session is the
httpOnly cookie, and `AuthStore` knows only what `/api/me` last said. Pages:
login (email and password, or GitHub), register (the invite code, then email
and password or GitHub), and `/auth/github/callback` — the page `GITHUB_OAUTH_REDIRECT_URL`
names, which passes GitHub's `code` and `state` to the API's callback.

- **`returnTo` is only ever a path on this site** (`safeReturnTo`), so a
  crafted `/login?returnTo=https://elsewhere` cannot bounce a reader off it.
- **The server's rules stay the server's.** The register page checks only that
  fields are filled; the server refuses and its sentence is shown.
- **A 401 from a question route means the session ended**: forget the user,
  sign in again, back to the same page. `/api/me` and `/api/auth/*` handle
  their own 401s, or the check would loop.
- The browser must use the host the redirect URL names (`localhost`, not
  `127.0.0.1`): the OAuth CSRF cookie belongs to the host sign-in began on.

## 11. The `web` schema, `vf_web_role` and `vf_admin_role`

A second PostgreSQL schema, **`web`**, beside `xbrl` in the same database.
Every primary key is a UUID made in Python, so there are no sequences to
grant.

| table | columns | notes |
|---|---|---|
| `web.user` | `id`, `email`, `hashed_password`, `is_active`, `is_superuser`, `is_verified`, `created_at` | the library's columns plus `created_at`; a unique index on `lower(email)`. `user` is reserved, so hand-written SQL says `web."user"` |
| `web.oauth_account` | `id`, `user_id`, `oauth_name`, `account_id`, `account_email`, `access_token`, `refresh_token`, `expires_at` | the library's |
| `web.access_token` | `token`, `user_id`, `created_at` | one row per signed-in session |
| `web.invite` | `id`, `code_hash`, `created_at`, `expires_at`, `used_at`, `used_by`, `revoked_at`, `created_by` | a code bound to no address (§10); `code_hash` unique; made from the CLI or by an admin (`created_by`, null from the CLI); spent once; a revoked one cannot be spent, a spent one cannot be revoked |
| `web.conversation` | `id`, `user_id`, `question`, `created_at` | the original question, verbatim |
| `web.job` | `id`, `conversation_id`, `round`, `status`, `asks`, `answers`, `reply`, `created_at`, `finished_at` | one per round. `asks` keeps the options offered, so an answer is checked against them and a pin read from them; `answers` keeps each answer's words |
| `web.job_trace` | `job_id`, `created_at`, `code_version`, `models`, `query_in`, `plan`, `result`, `model_calls`, `statements`, `timings`, `errors` | §8 |
| `web.job_feedback` | `id`, `job_id`, `user_id`, `note`, `created_at` | "report a problem" |
| `web.admin_action` | `id`, `created_at`, `admin_id`, `admin_email`, `action`, `target_id`, `target_email`, `detail` | the audit log: one row per thing an admin did, opening a trace included. The target is plain values, no foreign key, so the record outlives a deleted user |

Deleting a user deletes their conversations and everything under them
(`ON DELETE CASCADE`), and empties `used_by` on the invite they spent. The
owner's credential can delete anyone; `vf_admin_role` only a non-administrator.
Statuses and admin actions are explicit, named CHECKs that the
code's lists are tested against, so each is spelt one way everywhere.

### What `vf_web_role` may do

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
| `admin_action` | **no** | **no** | no | no |
| anything in `xbrl` | **no** | no | no | no |

Three are guard rails in their own right. **The Web Server cannot make anyone
an administrator**: `is_superuser` is outside its column grants and defaults to
false in the database, so only the owner's credential can set it, whatever a
bug in a route does. **It cannot invite anyone**: it can spend an invite, never
create one. **It cannot read a trace.** Because the two write-only tables
grant no `SELECT`, their models turn `eager_defaults` off: the ORM's
`INSERT ... RETURNING created_at` would need it.

**The web role never falls back to the owner.** The read-only roles' session
factories fall back to `DATABASE_URL` with a warning when their URL is unset;
the Web Server refuses to start without `DATABASE_URL_WEB`, because the owner
can do everything the grid was designed to prevent.

### What `vf_admin_role` may do

The admin routes' own login (`/api/admin/*`, through `admin_sessionmaker()`),
so what administration needs never reaches `vf_web_role`: a bug in an
ordinary route still holds only the grid above. Both logins live in one
process, so this guards against a route's mistake, not against the process
being taken.

| table | `SELECT` | `INSERT` | `UPDATE` | `DELETE` |
|---|---|---|---|---|
| `user` | yes | **no** | `is_active`, `hashed_password` only — a non-administrator's row | a non-administrator's row |
| `access_token` | yes | **no** | no | a non-administrator's sessions |
| `invite` | yes | yes, except `used_*` and `revoked_at` | `revoked_at` only | no |
| `admin_action` | yes | yes | **no** | **no** |
| `oauth_account`, `conversation`, `job`, `job_trace`, `job_feedback` | yes | no | no | no |
| anything in `xbrl` | **no** | no | no | no |

It cannot make an administrator, make an account, start a session as anyone,
spend an invite, or change the audit log. **Deleting a reader takes their
whole tree** although it holds no `DELETE` below `user`: PostgreSQL runs a
foreign key's cascade with the table owner's rights.

**An admin cannot act on an admin**, and the database says so, not only the
code: row-level security on `web.user` and `web.access_token`
(`roles.ROW_POLICIES`) lets `vf_admin_role` change or delete only rows where
`is_superuser` is false. Such a statement is not an error — it changes no
rows — so the admin routes check the count. `vf_web_role` has an
allow-everything policy, so sign-in is untouched; the owner bypasses row
security, which is how the CLI makes and unmakes administrators. The policies
are provisioned with the roles, not by a migration, since they name roles, and
each table's are replaced in one statement, so the running Web Server never
sees security on with its policy missing.

The Web Server refuses to start without `DATABASE_URL_ADMIN`, as it does
without its own login, rather than fail on an admin's first click.

### The rules every role keeps (`app/db/roles.py`)

- `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`, all four roles.
- Every table privilege is revoked before the spec is granted, in **every**
  managed schema — `web` from the two readers, `xbrl` from the web role — so
  the spec is authoritative and narrowing it narrows the role.
- No default privileges on future tables for the retrieval and web roles: each
  new table is a decision.
- `default_transaction_read_only = on` for both readers. The web role is the
  one without it; its narrowing is the grid above. A `RoleSpec` that is
  read-only and names a write is refused when it is built.
- `CREATE` on `public` revoked, and `USAGE` on `public` revoked from `PUBLIC`
  database-wide — otherwise the per-role revoke does not take (the retrieval
  role could cast to `public.vector`). The mapper is granted it by name for
  pgvector. **A login added later that needs `public` must be granted it by
  name.**
- `search_path` pinned per role (`web` for the web role); `statement_timeout`,
  `idle_in_transaction_session_timeout` and connection limits per role.
- Each password is read from the URL the application connects with.
- `--check` reports every privilege type in every managed schema.

Every statement provisioning sends is pinned in a file, the readers' apart
from the web role's, so adding a role provably takes nothing from the others
([docs/TESTING.md](../../docs/TESTING.md#fixtures-testsfixtures)). The grid
itself is exercised live on the test database, allowed cell by allowed cell
and refused cell by refused cell (`tests/test_roles.py`).

## 12. Containers

The Web Server runs in its own container: `api.Dockerfile`, its paired
`api.Dockerfile.dockerignore` (Docker matches it to the Dockerfile by name),
and the `api` service in `docker-compose.dev.yml`.

- **Python 3.13-slim with `uv`**, a frozen install from `uv.lock`, **one
  uvicorn worker** (§4), a non-root user, port 8000 on this machine's
  loopbacks only. It waits for `db` and `ollama` to be healthy; its own health
  check calls `GET /api/health`, which runs `SELECT 1` as the web role.
- **Behind the `web` profile**, so a plain `docker compose -f docker-compose.dev.yml up -d` does not
  start it before the migration and roles it needs exist.
- **Only the credentials it needs, by name** — the four role URLs, the
  secrets, GitHub — never `.env` whole. The owner's login and the test
  database's never enter the container. `DATABASE_URL`, which the code
  requires, is the **web role's** there, so even an accidental owner session
  has only `vf_web_role`'s grants.
- **`DATABASE_HOST=db:5432`** points every database URL at the compose service
  while keeping its login (`app/config.py`), so `.env` stays written for
  Python on the host and no password is written twice.
- **Nothing secret in the image** — the ignore file keeps `.env`, `data/`,
  `.venv` and `.git` out. `CODE_VERSION` is a build argument. `./data` is
  mounted, so `retrieval_log.jsonl` survives the container.
- **[A] in development**: `ng serve` with a proxy to the server, one origin.
  **[A] deployed**: a small web-server container serving the built bundle and
  proxying `/api`, with response buffering off for the event stream.

**Production is a second compose file**, `docker-compose.prod.yml` beside
`docker-compose.dev.yml`, standalone rather than an override (an override cannot
remove a service cleanly, and one file should say plainly what production is).
Each names its project (`verified-filings`, `verified-filings-prod`), so the two
share no container, network or database; they are never run together — one
GPU. The Ollama model volume is the one thing shared, to save 5 GB.

- **Nothing is published but the web container, on the PC's loopback**
  (`localhost:8080`, to check production in a browser here); the network and
  the internet reach nothing. The front door reaches the web container over the
  project's network.
- **The web container** (`web.Dockerfile`, `web.Caddyfile`): the Angular app
  built in one stage and served by Caddy in the next, as a user of its own,
  with `/api/*` passed to the api — one origin. Plain HTTP inside; the front
  door holds the certificate. It sets the security headers: a Content Security
  Policy allowing scripts from the site alone (no inline script, no eval —
  which is why Angular's critical-CSS inlining, which adds an inline script, is
  off in `angular.json`), styles and fonts also from Google Fonts, nothing that
  frames it; HSTS, `nosniff`, a referrer policy, a permissions policy. Hashed
  bundles are cached for good, the page never. Request bodies over 1 MB are
  refused (413). The event stream passes each event on as it comes
  (`flush_interval -1`; measured: stages arrive live). Any path outside
  `/api` is the app, so `/docs` there is the app's page, not the API's.
- **Who the visitor is.** The rate limits key on `limits.client_ip`. Caddy
  believes `X-Forwarded-For` only from the Tailscale container and otherwise
  takes the connecting address; it hands the api one address, the visitor's,
  and uvicorn believes that header from the web container alone
  (`--forwarded-allow-ips`). Both trust a fixed address, so the project's
  network has a fixed range (`10.42.42.0/24`). Measured: a forged
  `X-Forwarded-For` from outside is ignored.
- **Secrets are files**, made by `app/prod_secrets.py` into `secrets/`
  (git-ignored, and in the image's ignore file), each mounted only into the
  containers that need it at `/run/secrets/<setting>`, which `app/config.py`
  reads (`secrets_dir`). `docker inspect` shows none of them — checked value by
  value. Postgres takes its superuser password the same way
  (`POSTGRES_PASSWORD_FILE`), a new one, not dev's `postgres`.
- **The owner's login lives in no running container.** The `ops` service — the
  api image with Python as the entry point, behind the `ops` profile — is run by
  hand for migrations, provisioning and administration, and removed when done.
  The api gets the four role logins and the signing keys only.
- **Images pinned** (`ollama/ollama:0.34.0`), and every long-running service
  restarts on its own.
- Inside the database container, the official image trusts local connections
  without a password; over the network the password is required (checked: the
  old `postgres` is refused). Only someone already able to run Docker on the PC
  can reach the former.

Commands: [docs/STARTUP.md](../../docs/STARTUP.md#production). What the
deployment still needs: [docs/FUTURE.md](../../docs/FUTURE.md#deployment--the-users-pc-through-tailscale-funnel).

## 13. Administration

Two places, split by what only the owner may do.

| | the owner's CLI (`python -m app.api.admin`) | the admin routes (`/api/admin/*`) |
|---|---|---|
| credential | the database owner | `vf_admin_role` (§11) |
| for | `make-admin`, `unmake-admin`, the very first `invite`; also `reset-password` | everything else, for any administrator |

`unmake-admin` clears `is_superuser` and nothing else; the account, its
sessions and its questions stay. The role is read from the database on every
request, so it takes effect on the next click.

**The routes are invisible to everyone else.** One dependency guards the whole
router: no session is 401, as everywhere; a signed-in reader who is not an
administrator gets **404 with FastAPI's own body for a missing route**, so the
routes do not reveal themselves. A test sweeps every mounted admin route as an
ordinary reader, so a route added later is covered.

| route | does | refusals |
|---|---|---|
| `GET users` | every account: active, admin, how it signs in, sessions now, rounds in the last 24 hours | |
| `POST users/{id}/deactivate` | inactive, every session ended: out at once | 404 `NOT_FOUND`, 409 `ADMIN_PROTECTED` |
| `POST users/{id}/reactivate` | active again | as above |
| `POST users/{id}/end-sessions` | signed out everywhere; may sign in again | as above |
| `POST users/{id}/reset-password` | a new random password, shown once; sessions ended, so none outlives the old password | as above |
| `DELETE users/{id}` | the account and its whole tree, for good | as above |
| `GET invites` | newest first: `open`, `used` (and by whom), `revoked`, `expired`, and who made it | |
| `POST invites` `{days}` | a code, 1–90 days (14 by default), shown once | |
| `POST invites/{id}/revoke` | an unspent code can no longer be spent — atomic against a sign-up racing for it | 404, 409 `INVITE_NOT_OPEN` |
| `GET jobs` | recent rounds, newest first: who, the question, status, reply status, reports, whether a trace exists; `?reported`, `?failed`, `?user_id` | |
| `GET jobs/{id}/trace` | everything §8 keeps, and the reports on it | 404 |
| `GET reports` | every "report a problem", newest first | |
| `GET actions` | the audit log, newest first | |

**An admin cannot act on an admin**, themselves included, and both layers say
so: `admin_storage` looks the target up first (409 `ADMIN_PROTECTED`), and row
security (§11) changes no rows if a target became an administrator in between —
which is why every change checks the count it made.

**Every change is logged** in `web.admin_action`, in the same transaction as
the change, so neither lands without the other. **Opening a trace is logged
too** (`read_trace`): it shows a reader's question and every prompt it made.

**A trace is for a person debugging.** It carries prompts, raw model replies
and SQL — what §7 never sends to a reader — so it reaches only administrators,
through `vf_admin_role`; the web role still cannot read one.

**The client's side** — `web/src/app/admin/`, at `/admin`, behind `adminGuard`
and an **Admin** button beside "New question", both shown only when `/api/me`
says `is_superuser` (a convenience: the server refuses anyone else). Its own
lazily loaded bundle, so no other reader downloads it. Two tabs, each titled
with its count: **Users** and **Invites**, the latter split into **Open**,
**Used**, **Revoked** and **Expired**, newest first. A user's actions are
**Reset password** (a new one shown once) and **Delete** (the email typed to
confirm); deactivate, reactivate and end sessions sit apart in a small, italic
"debug" menu, kept for debugging and removable later. An administrator's row
stays visible with its actions greyed out. A new invite's code is shown once,
with a copy button. After every action the lists are re-read from the server.

Two more tabs. **Rounds**: the newest 100, filtered **All**, **Reported** or
**Failed** by the server (`GET jobs`), each with its reports' notes under the
question — so problem reports need no tab of their own (`GET reports` feeds the
notes). **Audit log**: the newest 100 entries, the debug actions set apart as
in Users, a trace opening linked to its round. A list that holds all the
server sent is counted "100+". A round opens on its own page,
`/admin/rounds/:jobId`, linkable: the question and its facts (reader, status,
time, code version — marked when written from uncommitted code — and models),
the problem reports, errors with their tracebacks, timings, every model call's
prompt and reply, every statement with the validator's verdict, and QueryIn,
QueryPlan and Result as JSON — each with a copy button, and **Copy the whole
trace as JSON**, so one paste hands a round over for debugging. The admin
calls live in `admin/admin-api.ts`, not `ApiService`, so they ship in the admin
bundle alone.
