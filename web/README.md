# [A] Web Client

The browser end of Verified Filings: Angular 22 (standalone, zoneless,
strict), Angular Material, `@ngrx/signals`. It renders what the Web Server
sends and decides nothing.

- Starting it: [docs/STARTUP.md](../docs/STARTUP.md)
- Design: [app/api/DESIGN.md](../app/api/DESIGN.md)
- Libraries and why: [docs/TOOLS.md](../docs/TOOLS.md#web-client-web)
- Tests: [docs/TESTING.md](../docs/TESTING.md#the-web-clients-tests)

Run from this folder:

| command             | what it does                                                                                 |
| ------------------- | -------------------------------------------------------------------------------------------- |
| `npm install`       | install the exact versions in `package-lock.json`                                            |
| `npm start`         | dev server on http://localhost:4200, forwarding `/api` to the Web Server on `127.0.0.1:8000` |
| `npm test`          | types current, then the unit tests, once                                                     |
| `npm run lint`      | ESLint                                                                                       |
| `npm run format`    | Prettier over everything; also turns CRLF into LF                                            |
| `npm run build`     | production build into `dist/web/`                                                            |
| `npm run api:types` | regenerate `src/app/api/openapi.d.ts` from `openapi.json`                                    |

## The contract

The server's models are the source of truth; the client's types are
generated from them, never written by hand:

```
Python models  --uv run python -m app.api.openapi-->  openapi.json             pytest: current?
openapi.json   --npm run api:types-->                 src/app/api/openapi.d.ts  api:check: current?
openapi.d.ts   --the compiler-->                      every use in the client
```

Both generated files are committed, ignored by Prettier and ESLint, and never
edited by hand. `src/app/api/types.ts` gives the generated shapes short names,
and `apiPath()` accepts only routes the contract names, so a renamed route
stops compiling here rather than failing in the browser.

## Layout rules

- **One origin.** `proxy.dev.json` (named in `angular.json`) forwards `/api`,
  so the sign-in cookie and the event stream need no cross-origin setup.
- **Only `src/app/answer/chart/` imports ApexCharts**; ESLint fails any other
  file that does.
- **Test-only helpers end in `.testing.ts`**, kept out of the app build.
