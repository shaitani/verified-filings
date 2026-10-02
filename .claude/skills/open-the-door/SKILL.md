---
name: open-the-door
description: Open the site to the internet - start the Tailscale container with Funnel on, so https://vf.zubron-ratio.ts.net serves production publicly. Use when the user asks to open the door, put the site online, or turn Funnel on.
---

# Open the door

Run from the repository root in Git Bash. Don't ask anything, don't commit.
Opening publishes the production site to the internet at
https://vf.zubron-ratio.ts.net. It stays open, across restarts, until
`/close-the-door`.

1. **Production must already be running** (never alongside dev: one GPU). If
   this does not print `healthy`, stop and tell the user to start production
   first (docs/STARTUP.md, *Production*):

   ```bash
   docker inspect -f '{{.State.Health.Status}}' verified-filings-prod-web-1
   ```

2. **Funnel on** in the serve file. git shows the file modified while the door
   is open; never commit it open (`/close-the-door` puts it back):

   ```bash
   sed -i 's/"${TS_CERT_DOMAIN}:443": false/"${TS_CERT_DOMAIN}:443": true/' tailscale.serve.json
   grep -A1 '"AllowFunnel"' tailscale.serve.json
   ```

3. **Start the Tailscale container** (it reads the file as it starts, and
   re-reads it if it was already running):

   ```bash
   docker compose -f docker-compose.prod.yml up -d tailscale
   ```

4. **Check** Funnel is on and the public address answers (up to a minute):

   ```bash
   docker compose -f docker-compose.prod.yml exec -T tailscale tailscale funnel status
   for i in $(seq 30); do c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://vf.zubron-ratio.ts.net/api/health); [ "$c" = 200 ] && { echo "OK  public address answers"; break; }; sleep 2; done
   ```

Report **"Door open: https://vf.zubron-ratio.ts.net"** only if the status shows
Funnel on and the OK line printed. Otherwise show both outputs and run
`/close-the-door`'s steps so the door is not left half-open.
