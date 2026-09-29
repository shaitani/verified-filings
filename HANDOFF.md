# Handoff — work in progress

Context for picking up the current work in a new session. Everything
permanent lives elsewhere: the system in [docs/DESIGN.md](docs/DESIGN.md),
what is wrong in [docs/GAPS.md](docs/GAPS.md), what comes later in
[docs/FUTURE.md](docs/FUTURE.md), how to start it all in
[docs/STARTUP.md](docs/STARTUP.md).

---

## Now: plural metric phrases

q058–q060 ("Nvidia quarterly **gross profits** from 2020 …") parse their
years correctly and fail only because the plural misses the curated
`gross profit` entry ([docs/GAPS.md](docs/GAPS.md#questions-that-come-back-wrong)).
Two fixes to put to the user, neither started: list `gross profits` as a
synonym (the file's convention — 21 plurals are listed by hand), or fall back
to the singular when a phrase misses, with a load-time check that no
singular/plural pair reaches different entries (none does today, over 277
forms).

## Before that

The Web Client ([A], `web/`) is complete: it signs in, asks, follows a
question's stages live, answers questions put back, draws the answer, keeps
the reader's past questions in a sidebar, and reports a problem on any round.
Design: [app/api/DESIGN.md](app/api/DESIGN.md) §3–§6 and §10; orientation:
[web/README.md](web/README.md).

**How the user wants work done:** put the plan and any open questions to them
before building each piece, then build one piece at a time.

## Context that is not written down elsewhere

- **GitHub sign-in through the client** needs `GITHUB_OAUTH_REDIRECT_URL`
  pointed at `http://localhost:4200/auth/github/callback` in `.env`, and that
  URL added to the GitHub OAuth App. The browser must use `localhost`, not
  `127.0.0.1`: the sign-in's CSRF cookie belongs to the host it started on.
- **The `api` container is built from the working tree**, so it must be rebuilt
  after a server change (docs/STARTUP.md) — a stale one serves an older
  contract than the client expects.
- **The real `web` tables** hold the user's own GitHub account; every live test
  used a `zz-…@example.com` account deleted afterwards.

## Next

Deployment — host not decided. See [docs/FUTURE.md](docs/FUTURE.md#deployment--host-not-decided).
