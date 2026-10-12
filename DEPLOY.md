# Deploying AlphaTradePro

Three layers, in order of preference: Docker Compose (fastest), systemd on a small VPS
(no Docker), or any managed container host.

---

## The three things that make it "always running"

| Layer | Mechanism | What it protects against |
| --- | --- | --- |
| Process | `restart: unless-stopped` / `Restart=always` | Crashes, OOM kills, reboots |
| Session | `backend/broker/neo_auth.py` keepalive every 5 min | Expired Kotak tokens, dropped sessions |
| Host | External cron on `/api/v1/system/health` | The machine itself going quiet |

Without the middle layer a long-running process eventually serves *nothing* while appearing
healthy, because the Neo trade token expired. That was a real bug in the predecessor of this
codebase; `neo_auth.py` exists to make it impossible to repeat.

---

> Running a single always-on cloud VM? See [CLOUD.md](CLOUD.md) — it wires the
> same three services into a reboot-surviving stack with a watchdog and backups.

---

## Static IP and redirect URL (required before live orders)

Two deployment facts must be registered with a broker before it will accept live orders. Both are
surfaced in **Settings → Deployment**, with copy buttons and a pass/fail checklist, so nothing has
to be hunted down in a shell.

### 1. A static public IP

SEBI requires the broker to see a **fixed source IP** on order APIs. A plain VM address does not
survive a stop/start or a redeploy, so attach a real one first:

| Provider | Product | Notes |
| --- | --- | --- |
| AWS | **Elastic IP** | Free while attached to a running instance |
| DigitalOcean | **Reserved IP** | Attach to the droplet; survives rebuilds |
| GCP | **Static external IP** | Reserve it, then assign to the instance |
| Azure | **Static public IP** | Set the allocation method to *Static* |
| Hetzner | **Primary IP** | Static by default |

Then record it so the platform can watch for drift:

```sh
STATIC_IP=203.0.113.10          # the address registered on the broker portal
STATIC_IP_RESERVED=1            # it is a real Elastic/Reserved IP
```

On a host that cannot reach a public "what is my IP" service, state the address directly and skip
the lookup:

```sh
PUBLIC_IP_OVERRIDE=203.0.113.10
PUBLIC_IP_CHECK=0
```

**Why record it at all?** Because the failure mode is silent otherwise. If the instance is rebuilt
onto a dynamic address, the broker starts rejecting every order with an IP error while the platform
looks perfectly healthy. With `STATIC_IP` set, **Settings → Deployment** reports `static IP mismatch`
and names both addresses the moment they diverge.

Verify from the API:

```sh
curl -s -H "Authorization: Bearer $TOKEN" \
     https://trade.example.com/api/v1/system/deployment | jq '.static_ip'
```

```json
{
  "registered_ip": "203.0.113.10",
  "detected_ip": "203.0.113.10",
  "status": "verified",
  "reserved": true
}
```

`status` is one of `verified`, `mismatch`, `unregistered` or `unknown`. A private address is never
reported as public — if detection cannot reach the internet, `detected_ip` is `null` and
`detection_error` says why.

### 2. A redirect URL

Broker OAuth / API authorisation flows send the user back to a URL that must be **pre-registered**
and **publicly resolvable**. The platform serves one at `/callback`:

| Environment | URL |
| --- | --- |
| Production | `https://mywebsite.com/callback` |
| Development | `http://127.0.0.1:8000/callback` |

Configure it either by letting it derive from the public URL:

```sh
PUBLIC_URL=https://trade.example.com
REDIRECT_PATH=/callback            # default
```

…or by pinning it explicitly, which is what to do when the broker needs one exact string:

```sh
REDIRECT_URL=https://trade.example.com/callback
REQUIRE_HTTPS=1                    # reject a redirect that arrives over plain HTTP
```

If a broker also accepts a loopback callback for local testing, list it so it is not refused:

```sh
REDIRECT_ALLOWLIST=https://trade.example.com/callback,http://127.0.0.1:8000/callback
```

The endpoint answers a bare `GET /callback` with a plain 200 (so you can prove it resolves), and
forwards a broker's `?code=…&state=…` into the terminal's `/connect` page.

> **Security.** `/callback` is unauthenticated by necessity — the broker is the caller. It
> therefore validates any `next` target against `REDIRECT_ALLOWLIST` (or the site's own origin) and
> returns **400** for anything else, so it cannot be used as an open redirect.

### Checklist

**Settings → Deployment** shows the same list, live:

- [ ] `STATIC_IP` set to the address registered on the broker portal
- [ ] `STATIC_IP_RESERVED=1` (the address survives a stop/start)
- [ ] The detected IP matches the registered one
- [ ] `PUBLIC_URL` or `REDIRECT_URL` set
- [ ] The redirect URL is HTTPS
- [ ] `REQUIRE_HTTPS=1`

`ready_for_live` in `GET /api/v1/system/deployment` is true only when all of them pass.

---

## Option A — Docker Compose

```sh
cp backend/.env.example .env      # fill in real values
docker compose up -d --build
docker compose ps                 # all three services healthy
```

What comes up:

| Service | Role | Exposed |
| --- | --- | --- |
| `api` | FastAPI, port 8000 | Internal only |
| `web` | Next.js standalone, port 3000 | Internal only |
| `proxy` | nginx, the only public surface | 80 / 443 |

The API is deliberately **not** published. Everything the browser needs is reachable on one
origin, so CORS is a non-issue and the WebSocket upgrade works.

```sh
docker compose logs -f api
curl -s https://trade.example.com/api/v1/system/health | jq
```

### TLS

```sh
mkdir -p nginx/certs
certbot certonly --webroot -w ./nginx/certbot -d trade.example.com
cp /etc/letsencrypt/live/trade.example.com/fullchain.pem nginx/certs/
cp /etc/letsencrypt/live/trade.example.com/privkey.pem  nginx/certs/
docker compose restart proxy
```

---

## Option B — systemd, no Docker

```sh
sudo useradd -m -s /bin/bash alphatrade
sudo rsync -a --exclude var --exclude web/node_modules --exclude .git ./ /opt/alphatrade/
sudo chown -R alphatrade:alphatrade /opt/alphatrade

# --- backend ---
sudo -u alphatrade python3 -m venv /opt/alphatrade/.venv
sudo -u alphatrade /opt/alphatrade/.venv/bin/pip install -r /opt/alphatrade/backend/requirements.txt
sudo -u alphatrade /opt/alphatrade/.venv/bin/pip install \
  "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"

# --- frontend ---
cd /opt/alphatrade/web
sudo -u alphatrade npm ci && sudo -u alphatrade npm run build
sudo -u alphatrade cp -r .next/static .next/standalone/.next/static
sudo -u alphatrade cp -r public .next/standalone/public

# --- services ---
sudo cp deploy/alphatrade-api.service deploy/alphatrade-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alphatrade-api alphatrade-web
sudo cp deploy/keepalive.cron /etc/cron.d/alphatrade-keepalive
sudo chmod 644 /etc/cron.d/alphatrade-keepalive
```

```sh
systemctl status alphatrade-api
journalctl -u alphatrade-api -f
curl -s localhost:8000/api/v1/system/health | jq
```

nginx on the host, serving both upstreams on one domain:

```nginx
upstream alphatrade_web { server 127.0.0.1:3000; }
upstream alphatrade_api { server 127.0.0.1:8000; }

server {
    listen 443 ssl;
    server_name trade.example.com;

    ssl_certificate     /etc/letsencrypt/live/trade.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/trade.example.com/privkey.pem;

    location /api/ {
        proxy_pass http://alphatrade_api;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
    location /ws/ {
        proxy_pass http://alphatrade_api;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;   # a market-data socket is long-lived
    }
    location / {
        proxy_pass http://alphatrade_web;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## Option C — managed container host

Any host that runs OCI containers works: Fly.io, Railway, Render, AWS ECS, Google Cloud Run.

The requirements are the same:

* both services on the **same origin**, so the terminal can reach the API without CORS,
* `var/` on a **persistent volume**, or recorded ticks are lost on every deploy,
* health checks on `/api/v1/system/health` and `/login`.

Fly.io sketch:

```toml
# fly.toml
[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "suspend"

[[vm]]
  memory = "1gb"
```

`app.py` already accepts proxy headers and the WebSocket route upgrades correctly, so no code
changes are needed behind a load balancer.

---

## First boot checklist

1. **Verify the consumer key.** A wrong one produces
   `totp_login rejected: Consumer key … does not exist` and *everything* fails. Test it before
   anything else:

   ```sh
   curl -s https://trade.example.com/api/v1/session | jq
   ```

2. **Load the symbol master.** Without it, search, breadth and the option chain are all empty.

   ```sh
   curl -X POST -H "Authorization: Bearer $TOKEN" \
        https://trade.example.com/api/v1/market/sync-master?force=true
   ```

3. **Check the feed.**

   ```sh
   curl -s https://trade.example.com/api/v1/market/status | jq '.feed, .session'
   ```

   `status: connected` and a rising `frames_received` means the WebSocket is streaming.

4. **Create the admin.** `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` seed the first
   account only when the user table is empty. After that, self-registration is closed and users
   are created from **Settings → Users**.

5. **Set the risk limits you actually want.** The defaults are conservative and *enabled*.

6. **Only then, enable live trading.** `ENABLE_LIVE_TRADING=1` and `ENABLE_ALGO_EXECUTION=1`,
   then restart. Consider keeping the kill switch within reach until you have watched a few paper
   runs.

---

## Turning on optional features

```sh
# Telegram alerts
TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...

# WhatsApp Cloud API
WHATSAPP_API_KEY=...     WHATSAPP_SENDER=...   WHATSAPP_RECIPIENT=...

# Email
SMTP_HOST=...  SMTP_USER=...  SMTP_PASSWORD=...

# AI coach narration (deterministic analysis works without this)
OPENAI_API_KEY=...
```

After changing anything: `docker compose restart api` or `systemctl restart alphatrade-api`.

Use **Alerts → Test** to verify a channel before relying on it. A channel that is not configured
is reported as `skipped`, never as `sent`.

---

## Before you go live

* [ ] A **valid** Kotak consumer key, verified via `/api/v1/session`
* [ ] A **static IP** attached and registered with the broker (see above)
* [ ] The **redirect URL** registered, over HTTPS
* [ ] Authentication in front of nginx (Cloudflare Access, Tailscale, or basic auth)
* [ ] TLS, with HSTS
* [ ] Risk limits reviewed and set deliberately
* [ ] The kill switch tested: engage it, confirm orders are refused, release it
* [ ] A paper run watched end to end
* [ ] Persistent storage for `var/` so the recorded tape survives a deploy
* [ ] `ENABLE_LIVE_TRADING` left **off** until the above are all true

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Consumer key … does not exist` | Wrong or revoked consumer key | Generate a new one in the Kotak Neo app |
| `Complete the 2fa process` | TOTP/MPIN/UCC mismatch | Re-register TOTP; check the values are current |
| `No instruments in the symbol master` | Master never synced | `POST /api/v1/market/sync-master?force=true` |
| `No recorded candles` | Feed not running, or the instrument is new | Check `/api/v1/market/status`; history starts when recording starts |
| Feed `status: reconnecting` | Kotak WebSocket dropped | It self-heals with backoff; check whether `reconnects` is climbing |
| Options show no Greeks | Spot or IV unavailable | Needs a live index quote and a traded premium |
| `VaultLocked` when saving an account | `CREDENTIAL_ENCRYPTION_KEY` not set | Set it; the vault never falls back to plaintext |
| Live order returns 403 `DISABLED` | Feature gate off | `ENABLE_LIVE_TRADING=1`, then restart |
| Live order returns `POSITION_SIZE` | Risk limit | Real — the engine blocked it. Review the limit, not the code |
| Broker rejects orders with an IP error | Egress IP no longer matches the registered one | Check **Settings → Deployment**; re-attach the Elastic/Reserved IP or update `STATIC_IP` |
| Deployment panel shows `static IP unregistered` | `STATIC_IP` never set | Register the detected IP with the broker, then set `STATIC_IP` to it |
| `/callback` returns 400 | `next` not in `REDIRECT_ALLOWLIST` | Add the target explicitly, or drop the `next` parameter |
