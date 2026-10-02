# Handoff — work in progress

Context for picking up the current work in a new session. Everything
permanent lives elsewhere: the system in [docs/DESIGN.md](docs/DESIGN.md),
what is wrong in [docs/GAPS.md](docs/GAPS.md), what comes later in
[docs/FUTURE.md](docs/FUTURE.md), how to start it all in
[docs/STARTUP.md](docs/STARTUP.md).

---

## Now: preparing to deploy

> **Gate — do not deploy until the production compose override exists and is
> in use.** That means a new Postgres password (not the committed
> `postgres`/`postgres`), no `db-test` or `pgadmin`, nothing published but the
> entry point, and secrets not readable with `docker inspect`
> ([docs/FUTURE.md](docs/FUTURE.md#deployment--host-not-decided)). The user
> asked for this to be enforced: refuse to help put the site on a reachable
> host before it is done.

What is left, in order, each planned with the user before it is built. The
user names these steps, not numbers:

- **Production Docker setup** — in progress; the gate above. The host is
  decided: the user's PC, reached through Tailscale Funnel run as a container
  (docs/FUTURE.md). `docker-compose.prod.yml`, its secrets, the ops
  container and the web container are in and tried (app/api/DESIGN.md §12;
  production at http://localhost:8080 on this PC). The user stops production
  while developing, never runs both. Next: the Tailscale container and
  Funnel.
- **Production settings** — a shorter session, a second GitHub OAuth app
  with the `https` callback, a way to run the owner's CLI on the server.
- **Data and backups** — the database onto the server; the `web` schema
  backed up.
- **Outside-in security check** — the anonymous probe from outside the
  network, a port scan, headers, both invite flows, a dependency audit.

Decided with the user for administration (the rest is in DESIGN §10–§13):
only the CLI sets or clears `is_superuser` (`make-admin`, `unmake-admin`) and
makes the very first invite; everything else is the admin tab; no
re-authentication for admin actions for now; deleting readers is allowed.

An anonymous probe of the live API (2026-09-30) found every question route,
`/api/me` and a forged cookie refused with 401, the unmounted library routes
404, and registration without an invite refused; open were login, GitHub
authorize, and health. The doc routes it also found open are now off unless
`API_DOCS` is set, and sign-in and questions are rate limited
(app/api/DESIGN.md §4, §10).

## The Web Client is complete

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
