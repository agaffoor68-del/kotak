# Running AlphaTradePro on a Hostinger VPS

Hostinger VPS fits this stack well: every plan ships a **dedicated static IPv4** — which is
exactly what Kotak Neo's SEBI static-IP rule requires — and the machine never auto-suspends.
This guide adapts [CLOUD.md](CLOUD.md) to Hostinger's hPanel specifics. Target shape:
**KVM 2 or larger** (≥2 vCPU / 4 GB RAM). Smaller works only with swap (step 2).

---

## 1. Create the VPS

1. hPanel → **VPS → Add a VPS** → pick a plan, location **as close to India as available**
   (latency to the brokers' APIs matters).
2. OS template: **Ubuntu 24.04 or 22.04, 64-bit**. Pick plain Ubuntu — *not* the
   CyberPanel/LiteSpeed templates; those pre-install a web server that fights nginx for ports
   80/443.
3. Note the **dedicated IPv4** shown in hPanel → VPS → **Overview**. This is a real static IP:
   it survives reboots and OS reinstalls, and you will register it with Kotak in step 8.
4. Set a root password from hPanel (VPS → Settings → **Root password**) and SSH in:

   ```sh
   ssh root@YOUR_VPS_IP
   ```

---

## 2. Baseline the machine

```sh
apt update && apt upgrade -y
apt install -y docker.io docker-compose-v2 git ufw ca-certificates curl
systemctl enable --now docker
usermod -aG docker "$USER"    # then log out and back in
timedatectl set-ntp true && timedatectl   # expect "System clock synchronized: yes"
```

The TZ inside the containers is already forced to `Asia/Kolkata` by the compose files; the OS
clock just has to be NTP-correct.

**Swap (recommended on 4 GB plans).** The Next.js image build is the only memory-hungry moment
in this deployment:

```sh
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

**Firewall.** Ubuntu's ufw ships inactive, and Hostinger's hPanel firewall (VPS → **Security →
Firewall**) is separate — if you enable either, all of these must be open:

| Port | Why |
| --- | --- |
| 22 (or your SSH port) | admin access |
| 80 | nginx + ACME certificate validation |
| 443 | nginx over TLS |

The API and database bind to container-internal addresses only; nothing else is public.

---

## 3. DNS

hPanel → **Domains → DNS Zone** (or point the domain's nameservers at Hostinger first), then
add one record:

```
Type  Name  Value              TTL
A     @     YOUR_VPS_IP        600
A     www   YOUR_VPS_IP        600
```

Wait for it to resolve (`nslookup trade.example.com`) before step 6 — certbot and the broker
redirect URL both require a resolvable name. You *can* boot the stack on the bare IP over
plain HTTP to test, but live trading needs an HTTPS redirect URL, so a domain is effectively
mandatory.

---

## 4. Get the code there

```sh
mkdir -p /opt/alphatrade && chown "$USER" /opt/alphatrade
git clone https://github.com/agaffoor68-del/kotak.git /opt/alphatrade
cd /opt/alphatrade
```

If the repo is private, authenticate the clone with a GitHub personal access token
(`git clone https://<TOKEN>@github.com/agaffoor68-del/kotak.git /opt/alphatrade`) or push the
code to a repo the VPS can read.

---

## 5. Configure `.env`

```sh
cp backend/.env.example .env
python3 - <<'PY'
import secrets
for name in ("SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY", "JWT_SECRET"):
    print(name, secrets.token_urlsafe(48))
PY
```

Paste the three generated values into `.env` (never reuse the example ones — they are public),
set the first admin, and adjust these for Hostinger (`trade.example.com` = your domain,
`YOUR_VPS_IP` = the dedicated IP from hPanel):

```dotenv
ENVIRONMENT=production
DEBUG=0

BOOTSTRAP_ADMIN_EMAIL=you@example.com
BOOTSTRAP_ADMIN_PASSWORD=<a strong password you will remember>

DATA_DIR=/app/var
PUBLIC_URL=https://trade.example.com
ALLOWED_ORIGINS=https://trade.example.com
REQUIRE_HTTPS=1

# SEBI: register this exact IP in the Kotak Neo portal (step 8), then record it here.
STATIC_IP=YOUR_VPS_IP
STATIC_IP_RESERVED=1

# Brokers — fill now or connect accounts later from Settings → Connect broker.
KOTAK_CONSUMER_KEY=
KOTAK_TOTP_KEY=
KOTAK_MOBILE_NUMBER=
KOTAK_MPIN=
KOTAK_UCC=
ANGEL_API_KEY=
ANGEL_CLIENT_CODE=
ANGEL_PIN=
ANGEL_TOTP_KEY=

KOTAK_KEEPALIVE=1
KOTAK_KEEPALIVE_SECONDS=300

RECORD_TICKS=1
TICK_RETENTION_DAYS=30
ENABLE_PAPER_TRADING=1
ENABLE_BACKTEST=1
ENABLE_LIVE_TRADING=0
ENABLE_ALGO_EXECUTION=0
```

Two traps to avoid:

* `CREDENTIAL_ENCRYPTION_KEY` is **not optional** — without it the vault refuses to persist any
  broker account (`VaultLocked`). Generate it, don't skip it.
* The example ships `ENABLE_LIVE_TRADING=1`; leave it **0** until the checklist in step 9 passes.

Then lock it down — this file becomes a live credential store:

```sh
chmod 600 .env
```

---

## 6. Up

```sh
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.cloud.yml ps
```

Four services should reach a healthy state: `api`, `web`, `proxy`, `watchdog`. Only nginx is
published, on port 80; the API is never exposed directly. If the build dies on the web step with
an OOM error, the fix is the swap file from step 2, then rerun the same command.

```sh
curl -s localhost/api/v1/system/health | jq
curl -s localhost/api/v1/session | jq      # the real proof the Kotak consumer key works
```

Sign in at `http://YOUR_VPS_IP` with the bootstrap admin and connect your **Angel One** account
(market data — quotes, candles and the instrument master all come from it) and the **Kotak Neo**
account (order execution). Then trigger the master sync from Settings, or:

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" \
     https://trade.example.com/api/v1/market/sync-master?force=true
```

---

## 7. TLS

The cloud overlay boots with `nginx/alphatrade-http.conf` (no SSL block) so the stack comes up
before a certificate exists. Issue the certificate with the **webroot** method — the running
nginx already serves ACME challenges from `./nginx/certbot`, so nothing has to stop:

```sh
apt install -y certbot
mkdir -p nginx/certs nginx/certbot
certbot certonly --webroot -w /opt/alphatrade/nginx/certbot \
  -d trade.example.com --agree-tos -m you@example.com --non-interactive

cp /etc/letsencrypt/live/trade.example.com/fullchain.pem nginx/certs/
cp /etc/letsencrypt/live/trade.example.com/privkey.pem  nginx/certs/
```

Now switch nginx to the SSL config — edit `docker-compose.cloud.yml` and replace the mount

```yaml
- ./nginx/alphatrade-http.conf:/etc/nginx/conf.d/default.conf:ro
```

with

```yaml
- ./nginx/alphatrade.conf:/etc/nginx/conf.d/default.conf:ro
```

then recreate the proxy:

```sh
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d proxy
```

HTTP now 301-redirects to HTTPS and HSTS is on. Renewals need the cert copied into the mount
and the proxy reloaded — wire it once:

```sh
sudo tee /etc/cron.d/alphatrade-certbot >/dev/null <<'CRON'
17 3,15 * * * root certbot renew --quiet && cp /etc/letsencrypt/live/trade.example.com/fullchain.pem /opt/alphatrade/nginx/certs/ && cp /etc/letsencrypt/live/trade.example.com/privkey.pem /opt/alphatrade/nginx/certs/ && docker exec alphatrade-proxy nginx -s reload
CRON
sudo chmod 644 /etc/cron.d/alphatrade-certbot
```

Alternative: terminate TLS at Cloudflare (orange-cloud the record, keep the HTTP config). Only
do that with Cloudflare Access or another auth layer in front — this app's order endpoints are
protected by application auth only.

---

## 8. Register with the brokers (SEBI prerequisites)

1. **Static IP.** Kotak Neo rejects order calls whose source IP is not registered. In the Kotak
   Neo portal, register the VPS's dedicated IP from step 1. Hostinger's IP survives reboots and
   reinstalls, so `STATIC_IP_RESERVED=1` is correct. The platform warns in
   **Settings → Deployment** the moment egress stops matching.
2. **Redirect URL.** Register `https://trade.example.com/callback` on the broker portal; it is
   served by the app out of the box. `ready_for_live` in **Settings → Deployment** turns true
   only when the IP, redirect and HTTPS checks all pass.

---

## 9. Turn on live trading — last

Only after [DEPLOY.md](DEPLOY.md)'s "Before you go live" checklist is fully ticked:

1. Verify sessions: `curl -s https://trade.example.com/api/v1/session | jq`
2. Watch a paper run end to end for a full market session.
3. Test the kill switch: engage it, confirm orders are refused, release it.
4. Set your risk limits deliberately (defaults are conservative and active from day one).
5. Flip `ENABLE_LIVE_TRADING=1` and `ENABLE_ALGO_EXECUTION=1`, then
   `docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d --force-recreate api`.

---

## 10. Keep it alive

All three liveness layers from the README are already wired by the cloud overlay:

| Layer | Mechanism | Verify |
| --- | --- | --- |
| Process | `restart: always` on all four containers | `systemctl is-enabled docker` → `enabled` |
| Session | Kotak/Angel keepalive loops inside the API | `docker logs alphatrade-watchdog` |
| Host | watchdog polls `/api/v1/system/health` every 60 s and restarts a wedged API | same |

Beyond that:

```sh
# Nightly tape backup — the recorded ticks are the one thing that cannot be rebuilt.
sudo tee /etc/cron.d/alphatrade-backup >/dev/null <<'CRON'
40 2 * * * root docker run --rm -v alphatrade_market_data:/data -v /opt/alphatrade/backups:/out alpine tar czf /out/tape-$(date +\%F).tgz -C /data .
CRON
sudo chmod 644 /etc/cron.d/alphatrade-backup
```

* Point an external monitor (UptimeRobot etc.) at `https://trade.example.com/healthz` on a
  5-minute interval — the box going quiet must be visible from outside the box.
* Watch the disk: `docker exec alphatrade-api df -h /app/var`. `TICK_RETENTION_DAYS=30` is the
  control that stops the tick DB filling the volume; a full disk is the most common way a
  long-running instance actually dies.
* hPanel → VPS → **Backups** gives you host-level snapshots; the cron above additionally keeps
  the irreplaceable tape in `/opt/alphatrade/backups`.

---

## Cheat sheet

| Job | Command |
| --- | --- |
| Status | `docker compose -f docker-compose.yml -f docker-compose.cloud.yml ps` |
| Logs | `... logs -f api` (add `watchdog` for session health) |
| Restart one service | `... restart api` |
| Apply an `.env` change | `... up -d --force-recreate api` |
| Health | `curl -s localhost/api/v1/system/health \| jq` |
| Session (Kotak key proof) | `curl -s localhost/api/v1/session \| jq` |
| Disk | `docker exec alphatrade-api df -h /app/var` |
| Ports blocked from outside | check hPanel → Security → Firewall **and** `ufw status` |
| After a bad deploy | `... down` — never `down -v`, that deletes the tape |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `http://IP` unreachable | hPanel firewall or ufw closed 80/443 | Open them in both places (step 2) |
| certbot `Unauthorized` | DNS not propagated, or port 80 blocked | `nslookup`; fix firewall; rerun |
| certbot fails while stack is up | `--standalone` tried to bind port 80 | Use the `--webroot` command from step 7 |
| web image build killed | OOM during `next build` | Add the swap file, rebuild |
| `static IP mismatch` in Settings | VPS rebuilt onto a different IP | Re-register with the broker, update `STATIC_IP` |
| Login works locally, orders fail from VPS | Broker session/IP binding | Register the VPS IP (step 8) before rotating credentials |
| Anything else | See [DEPLOY.md](DEPLOY.md) troubleshooting table | |
