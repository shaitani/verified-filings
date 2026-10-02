---
name: close-the-door
description: Close the site to the internet - turn Tailscale Funnel off, stop the Tailscale container, and prove nothing of this project is reachable from outside this PC. Use when the user asks to close the door, take the site offline, or stop Funnel.
---

# Close the door

Run from the repository root in Git Bash. Don't ask anything, don't commit.
Closed means: Funnel off **and** the Tailscale container stopped, so this PC is
not even connected to the tailnet. Both dev and production publish only on this
PC's loopback, so with Tailscale stopped nothing of this project is reachable
from the network or the internet.

1. **Funnel off** in the serve file (the committed state; Tailscale reads it
   whenever its container starts):

   ```bash
   sed -i 's/"${TS_CERT_DOMAIN}:443": true/"${TS_CERT_DOMAIN}:443": false/' tailscale.serve.json
   grep -A1 '"AllowFunnel"' tailscale.serve.json
   ```

2. **Stop the Tailscale container** (harmless if production is not running):

   ```bash
   docker compose -f docker-compose.prod.yml stop tailscale
   ```

3. **Prove it.** Each check must print its OK line:

   ```bash
   grep -q '"${TS_CERT_DOMAIN}:443": false' tailscale.serve.json && echo "OK  Funnel is off in tailscale.serve.json"
   [ -z "$(docker ps -q --filter name=verified-filings-prod-tailscale)" ] && echo "OK  the Tailscale container is not running"
   [ -z "$(docker ps --format '{{.Names}} {{.Ports}}' | grep -E 'verified-filings' | grep -E '0\.0\.0\.0:|\[::\]:')" ] && echo "OK  no container of this project listens beyond this PC's loopback"
   [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://vf.zubron-ratio.ts.net/)" = "000" ] && echo "OK  https://vf.zubron-ratio.ts.net does not answer"
   ```

Report **"Door closed"** only if all four OK lines printed. Otherwise say which
check failed, show its output, and say plainly that the door may still be open.
