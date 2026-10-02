---
name: web-app-reload
description: Update the web app to the current code - rebuild the api container from the working tree and restart the Web Client dev server, hidden. Use when the user asks to update, reload or restart the web app.
---

# Reload the web app

Run from the repository root. Don't ask anything, don't commit.

1. **Rebuild the api container** from the working tree (it holds a copy of the
   code, so it serves stale code until rebuilt). `CODE_VERSION` is what job
   traces record; `+dirty` when there are uncommitted changes. `--wait` returns
   once it is healthy.

   ```bash
   V=$(git rev-parse --short=12 HEAD); [ -n "$(git status --porcelain)" ] && V="$V+dirty"
   CODE_VERSION="$V" docker compose -f docker-compose.dev.yml --profile web up -d --build --wait api
   ```

   If it fails, show `docker compose -f docker-compose.dev.yml logs --tail 60 api` and stop.

2. **Restart the Web Client dev server**, hidden: no window, and no Angular
   prompts (output to a file is not a terminal, and `NG_CLI_ANALYTICS=false`
   turns off the analytics question). Its output goes to `data/web-client.log`.

   ```powershell
   Get-NetTCPConnection -State Listen -LocalPort 4200 -ErrorAction SilentlyContinue | ForEach-Object { taskkill /PID $_.OwningProcess /T /F | Out-Null }
   $env:NG_CLI_ANALYTICS = 'false'
   Start-Process cmd.exe -ArgumentList '/d', '/c', 'npm start' -WorkingDirectory web -WindowStyle Hidden -RedirectStandardOutput data\web-client.log -RedirectStandardError data\web-client.err.log
   ```

3. **Check** the page is up and `/api` reaches the container through it
   (up to a minute while it compiles):

   ```powershell
   for ($i = 0; $i -lt 60; $i++) { try { if ((Invoke-WebRequest http://localhost:4200/api/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { 'up'; break } } catch { Start-Sleep 1 } }
   ```

   Not `up`: show `data/web-client.log` and `data/web-client.err.log`.

Report in one or two lines: the `CODE_VERSION` now running, and that
http://localhost:4200 is up.
