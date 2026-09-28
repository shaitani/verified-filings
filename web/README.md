# [A] Web Client

The browser end of Verified Filings: Angular 22 (standalone, zoneless, strict),
Angular Material, `@ngrx/signals`. It renders what the Web Server sends and
decides nothing. Design: [`../app/api/DESIGN.md`](../app/api/DESIGN.md);
plan: [`../HANDOFF.md`](../HANDOFF.md) §11.

Built with Node 24 and npm 11. Everything below runs from this folder.

| command          | what it does                                                             |
| ---------------- | ------------------------------------------------------------------------ |
| `npm install`    | install the exact versions in `package-lock.json`                        |
| `npm start`      | dev server on http://localhost:4200, forwarding `/api` to the Web Server |
| `npm test`       | unit tests (Vitest), once                                                |
| `npm run lint`   | ESLint, with the `vf-` selector prefix enforced                          |
| `npm run format` | Prettier over everything; also turns CRLF into LF                        |
| `npm run build`  | production build into `dist/web/`                                        |

**The dev proxy.** `npm start` needs the Web Server running on
`127.0.0.1:8000` (HANDOFF §10). `angular.json` names `proxy.dev.json` as the
development proxy: the browser sees one site on port 4200, so the sign-in
cookie and the event stream need no cross-origin setup.

**Pinned exactly.** `apexcharts` and `ng-apexcharts` carry no `^`, so an update
is always a deliberate edit. Only the chart component imports them (DESIGN §5).

**Line endings.** The Angular CLI writes CRLF on Windows; the repository is LF.
`.gitattributes` makes git commit LF regardless, and `npm run format` fixes the
working copy after `ng generate`.
