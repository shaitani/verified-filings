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

The agreed order, one piece at a time, each planned with the user first:

- **Admin CLI to take access back** — list users and invites, revoke an
  invite, deactivate a user and delete their sessions.
- **Choose the host** — a walk-through with the user. The GPU decides it;
  options on the table: all at home behind Cloudflare Tunnel (+ Access), a
  droplet with Ollama at home over Tailscale, a rented GPU VM, Tailscale-only.
- **The production compose override** — the gate above.
- **The web container** — the Angular bundle and `/api` behind one proxy,
  security headers, the real client IP trusted from the proxy (every limit
  keys on `limits.client_ip`; untrusted, all visitors share the proxy's
  budget), per-IP limits at the proxy.
- **Production settings, data and backups, an outside-in security check.**

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
