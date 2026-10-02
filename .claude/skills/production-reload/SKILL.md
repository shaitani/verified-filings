---
name: production-reload
description: Update the production web app to the current code - rebuild the api and web containers of the production project from the working tree. Use when the user asks to update, reload or restart production.
---

# Reload production

Run from the repository root. Don't ask anything, don't commit.

1. **Rebuild the api and web containers** from the working tree (they hold a
   copy of the code and the built Web Client, so they serve stale code until
   rebuilt). `CODE_VERSION` is what job traces record; `+dirty` when there are
   uncommitted changes. `--wait` returns once they are healthy.

   ```bash
   V=$(git rev-parse --short=12 HEAD); [ -n "$(git status --porcelain)" ] && V="$V+dirty"
   CODE_VERSION="$V" docker compose -f docker-compose.prod.yml up -d --build --wait api web
   ```

   If it fails, show `docker compose -f docker-compose.prod.yml logs --tail 60 api web` and stop.

2. **Check** the page is up and `/api` reaches the container through it:

   ```powershell
   for ($i = 0; $i -lt 60; $i++) { try { if ((Invoke-WebRequest http://localhost:8080/api/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { 'up'; break } } catch { Start-Sleep 1 } }
   ```

   Not `up`: show `docker compose -f docker-compose.prod.yml logs --tail 60 api web`.

Report in one or two lines: the `CODE_VERSION` now running, and that
http://localhost:8080 is up.
