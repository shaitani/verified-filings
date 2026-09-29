# [A] Web Client

The browser end of Verified Filings: Angular 22 (standalone, zoneless, strict),
Angular Material, `@ngrx/signals`. It renders what the Web Server sends and
decides nothing. Design: [`../app/api/DESIGN.md`](../app/api/DESIGN.md);
plan: [`../HANDOFF.md`](../HANDOFF.md) §11.

Built with Node 24 and npm 11. Everything below runs from this folder.

| command             | what it does                                                             |
| ------------------- | ------------------------------------------------------------------------ |
| `npm install`       | install the exact versions in `package-lock.json`                        |
| `npm start`         | dev server on http://localhost:4200, forwarding `/api` to the Web Server |
| `npm test`          | `api:check`, then the unit tests (Vitest), once                          |
| `npm run lint`      | ESLint, with the `vf-` selector prefix enforced                          |
| `npm run format`    | Prettier over everything; also turns CRLF into LF                        |
| `npm run build`     | production build into `dist/web/`                                        |
| `npm run api:types` | regenerate `src/app/api/openapi.d.ts` from `openapi.json`                |
| `npm run api:check` | fail if `openapi.d.ts` no longer matches `openapi.json`                  |

**The contract.** The server's models are the source of truth; the client's
types are generated from them, never written by hand (DESIGN §9):

```
Python models  --uv run python -m app.api.openapi-->  openapi.json      pytest: current?
openapi.json   --npm run api:types-->          src/app/api/openapi.d.ts  api:check: current?
openapi.d.ts   --the compiler-->               every use in the client
```

Change a model on the server and pytest fails until `openapi.json` is
rewritten; rewrite it and `npm test` fails until the types are regenerated;
regenerate them and the build fails at every line that no longer fits. Both
generated files are committed, ignored by Prettier and ESLint, and never edited
by hand. `src/app/api/types.ts` gives the generated shapes short names, and
`apiPath()` accepts only routes the contract names, so a renamed route stops
compiling here rather than failing in the browser.

**Test replies are real.** `src/app/conversation/fixtures/replies.json` is
written by `uv run python -m tests.web_replies` (from the repository root): the
chain and the Presenter over captured runs, no model or database needed. A
pytest fails while it is out of date. Test-only helpers end in `.testing.ts`,
which `tsconfig.app.json` keeps out of the app build.

**The TypeScript override.** `openapi-typescript` 7.13 declares TypeScript 5;
Angular 22 is on TypeScript 6. `package.json`'s `overrides` lets the generator
use the workspace's TypeScript 6 — checked 2026-09-28: its output is
byte-identical to a run under TypeScript 5.9.3. Remove the override once a
release declares TypeScript 6.

**Re-check the override whenever the types change** — after `npm run api:types`
produces a new `openapi.d.ts`, or when `openapi-typescript` or `typescript` is
upgraded. Generate the same file under the TypeScript the generator declares,
and compare; any difference means the override is no longer safe:

```
npx -y -p openapi-typescript@7.13.0 -p typescript@5.9.3 openapi-typescript openapi.json --immutable --default-non-nullable false -o ../.cache/api-types-ts5.d.ts
git diff --no-index --exit-code src/app/api/openapi.d.ts ../.cache/api-types-ts5.d.ts
```

No output from the second command means identical. Keep the flags in step
with `api:types` in `package.json`.

**The dev proxy.** `npm start` needs the Web Server running on
`127.0.0.1:8000` (HANDOFF §10). `angular.json` names `proxy.dev.json` as the
development proxy: the browser sees one site on port 4200, so the sign-in
cookie and the event stream need no cross-origin setup.

**Pinned exactly.** `apexcharts` and `ng-apexcharts` carry no `^`, so an update
is always a deliberate edit. Only `src/app/answer/chart/` may import them — ESLint
fails any other file that does (DESIGN §5).

**Line endings.** The Angular CLI writes CRLF on Windows; the repository is LF.
`.gitattributes` makes git commit LF regardless, and `npm run format` fixes the
working copy after `ng generate`.
