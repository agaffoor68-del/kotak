# Running AlphaTradePro 24/7 in the cloud

Target: one Linux VM that never sleeps, one public IP, one domain. The stack is
the existing three containers plus a watchdog, so a crash, a reboot, or a wedged
process all resolve without you.

---

## 1. The VM

Any 2 vCPU / 4 GB VPS works. The shape of the machine matters less than these
three properties:

| Property | Why |
| --- | --- |
| **Never auto-suspends** | A stopped machine is not a running bot. Hetzner/DO/OVH do not idle by default; AWS/Al GCP do. |
| **A static public IPv4** | The recorded tape and the session must not move between hosts. |
| **IST-friendly clock** | `TZ=Asia/Kolkata` is set in the compose files; the OS clock must be NTP-synced. |

```sh
timedatectl set-ntp true
timedatectl          # expect "System clock synchronized: yes"
```

Ubuntu/Debian baseline:

```sh
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" && newgrp docker
```

---

## 2. Get the code and the secrets there

```sh
sudo mkdir -p /opt/alphatrade && sudo chown "$USER" /opt/alphatrade
git clone <your-repo> /opt/alphatrade
cd /opt/alphatrade
```

`.env` is gitignored and must be created on the host — never baked into an image:

```sh
cp backend/.env.example .env
python3 - <<'PY'
import secrets
for name in ("SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY", "JWT_SECRET"):
    print(name, secrets.token_urlsafe(48))
PY
```

Paste the three generated values into `.env`, then add the rest:

```dotenv
ENVIRONMENT=production
DEBUG=0

DATA_DIR=/app/var
PUBLIC_URL=https://trade.example.com
ALLOWED_ORIGINS=https://trade.example.com

KOTAK_CONSUMER_KEY=...
KOTAK_TOTP_KEY=...
KOTAK_MOBILE_NUMBER=...
KOTAK_MPIN=...
KOTAK_UCC=...
KOTAK_KEEPALIVE=1
KOTAK_KEEPALIVE_SECONDS=300

RECORD_TICKS=1
ENABLE_PAPER_TRADING=1
ENABLE_BACKTEST=1
ENABLE_LIVE_TRADING=0
ENABLE_ALGO_EXECUTION=0
```

`CREDENTIAL_ENCRYPTION_KEY` is not optional: without it the vault raises
`VaultLocked` and no broker account can be saved. Set it before first boot.

Lock the file down — it is a live trading credential store:

```sh
chmod 600 .env
```

---

## 3. Up

```sh
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.cloud.yml ps
```

Four services should be up: `api`, `web`, `proxy`, `watchdog`. The API is not
published — only nginx is, on port 80.

```sh
curl -s localhost/api/v1/system/health | jq
curl -s localhost/api/v1/session   | jq     # the real proof the Kotak key works
```

---

## 4. TLS

The cloud overlay ships `nginx/alphatrade-http.conf`, which has no SSL block, so
the stack boots before a certificate exists.

```sh
sudo apt install -y certbot
sudo certbot certonly --standalone -d trade.example.com \
  --agree-tos -m you@example.com --non-interactive

sudo mkdir -p nginx/certs
sudo cp /etc/letsencrypt/live/trade.example.com/fullchain.pem nginx/certs/
sudo cp /etc/letsencrypt/live/trade.example.com/privkey.pem  nginx/certs/
sudo chown "$USER" nginx/certs/*
```

Now mount the real config instead of the HTTP one, so the HTTP block starts
redirecting to HTTPS:

```sh
# in docker-compose.cloud.yml, swap:
#   ./nginx/alphatrade-http.conf:/etc/nginx/conf.d/default.conf:ro
# for:
#   ./nginx/alphatrade.conf:/etc/nginx/conf.d/default.conf:ro
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d proxy
```

Renewal needs the cert copied into the mount and the proxy reloaded, twice a day:

```sh
sudo tee /etc/cron.d/alphatrade-certbot >/dev/null <<'CRON'
17 3,15 * * * root certbot renew --quiet && cp /etc/letsencrypt/live/trade.example.com/fullchain.pem /opt/alphatrade/nginx/certs/ && cp /etc/letsencrypt/live/trade.example.com/privkey.pem /opt/alphatrade/nginx/certs/ && docker exec alphatrade-proxy nginx -s reload
CRON
```

Alternative: terminate TLS at Cloudflare and leave this file alone. Only do that
with Cloudflare Access or another auth layer in front — this app's order
endpoints are protected by application auth only.

---

## 5. Survive reboots

`restart: always` on all four services means Docker brings the stack back after
a host restart. Confirm the daemon itself is enabled:

```sh
systemctl is-enabled docker   # expect: enabled
```

That is the whole of layer 1. Layers 2 and 3 are already wired:

* **Layer 2 — session.** `KOTAK_KEEPALIVE=1` re-authenticates to Kotak every 5
  minutes. A Neo trade token expires in ~45 minutes, so a process left alone
  eventually serves empty quotes while still looking healthy.
* **Layer 3 — host.** The `watchdog` container polls `/api/v1/system/health`
  every 60s and `docker restart`s a wedged API, and logs `/api/v1/session` so
  an expired credential is visible in `docker logs alphatrade-watchdog`.

```sh
docker logs -f alphatrade-watchdog
```

---

## 6. Persistence

The recorded tape is the reason to do this at all — it is the only real history
the platform has. It lives on the `market_data` named volume, so it survives
`up -d --build` and reboots, but **not** `docker compose down -v`.

```sh
docker volume ls | grep market_data
```

Nightly backup, because a live tape is not reproducible:

```sh
sudo tee /etc/cron.d/alphatrade-backup >/dev/null <<'CRON'
40 2 * * * root docker run --rm -v alphatrade_market_data:/data -v /opt/alphatrade/backups:/out alpine tar czf /out/tape-$(date +\%F).tgz -C /data .
CRON
sudo mkdir -p /opt/alphatrade/backups
```

---

## 7. Watching it

```sh
docker compose -f docker-compose.yml -f docker-compose.cloud.yml logs -f api
docker exec alphatrade-api df -h /app/var      # tick DB is the thing that fills up
```

Ticks are high frequency. `TICK_RETENTION_DAYS=30` in `.env` is the control
that stops the disk filling; a full disk is the most common way a long-running
instance actually dies.

External uptime check, so the machine itself going quiet is visible:

```sh
curl -s https://trade.example.com/healthz     # proxies to the API health route
```

Point UptimeRobot or similar at `/healthz` on a 5-minute interval. Alert if the
tape stops growing during market hours, not just if the endpoint 500s.

---

## 8. Turning on live trading

Last, and only after the checklist in `DEPLOY.md`:

1. `ENABLE_LIVE_TRADING=1` and `ENABLE_ALGO_EXECUTION=1` in `.env`.
2. `docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d api`
3. Watch paper runs for a full session first.
4. Keep the kill switch one click away.

Broker sessions are tied to the host's IP for some Kotak flows. If logins fail
from the VPS but worked from your laptop, that is why — check before rotating
credentials.

---

## Cheat sheet

| Job | Command |
| --- | --- |
| Status | `docker compose -f docker-compose.yml -f docker-compose.cloud.yml ps` |
| Logs | `... logs -f api` |
| Restart one service | `... restart api` |
| Apply an `.env` change | `... up -d --force-recreate api` |
| Health | `curl -s localhost/api/v1/system/health \| jq` |
| Session | `curl -s localhost/api/v1/session \| jq` |
| Disk | `docker exec alphatrade-api df -h /app/var` |
| Watchdog | `docker logs -f alphatrade-watchdog` |
| Disk after a bad deploy | `docker compose -f docker-compose.yml -f docker-compose.cloud.yml down` (never `-v`) |
