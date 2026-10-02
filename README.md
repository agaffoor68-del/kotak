# AlphaTradePro

Institutional algorithmic trading for the Indian markets, connected **exclusively** to
**Kotak Neo Trade API** and the **Kotak Neo WebSocket feed**.

No simulated prices, no demo data, no placeholder option chains. Every figure on every screen
comes from Kotak Neo. When the broker is unreachable, the interface says so — it never fills
the gap with invented numbers.

---

## Read this first: the historical-data limitation

**Kotak Neo publishes no historical candle API.** Its quote endpoints are live-only, and the
scrip master is static reference data, not price history.

That has one honest consequence: a backtester can only be honest if the platform records the
tape itself. So AlphaTradePro does exactly that:

* every incoming quote is written to a `ticks` table (`backend/marketdata/ticks.py`),
* candles are aggregated from those ticks on read, at 1m/3m/5m/15m/30m/60m/1d,
* charts, indicators, backtests and the algo engine all read that recorded data,
* and the API reports coverage explicitly, so you always know how much real history exists.

This is a genuine architectural constraint, not an omission. A backtest over synthetic candles
would be fiction; a backtest over recorded ticks is real. History accumulates from the moment you
start recording, and gets better every session.

---

## Architecture

```
browser (Next.js 15, dark desk, PWA)
   │  HTTPS / WSS  (one origin via nginx)
   ▼
nginx ── /            → Next.js standalone        :3000
       ├─ /api/        → FastAPI                  :8000
       └─ /ws/market   → FastAPI WebSocket feed
                              │
                              ▼
                       Kotak Neo Trade API  (TOTP + MPIN session)
                       Kotak Neo WebSocket  (tick feed)
```

```
backend/
  core/        config, security (PBKDF2 + JWT), vault (Fernet), database (SQLite)
  broker/      neo_auth (session manager), accounts (credential registry)
  marketdata/  master (scrip sync), quotes (cache), ticks (recorder), feed (socket), market_hours
  strategies/  indicators, dsl (no-code engine), templates
  options/     chain (Black-Scholes Greeks, PCR, max pain)
  backtesting/ engine
  papertrading/engine
  execution/   live (Kotak routing), algo (runner), charges (Indian slabs)
  risk/        engine (limits, kill switch)
  analytics/   metrics (Sharpe, Sortino, CAGR, drawdown)
  alerts/      service (Telegram, WhatsApp, email)
  ai/          coach (deterministic patterns + optional LLM)
  api/         routes, deps (auth/RBAC), ws
```

---

## Quick start

### 1. Install

```sh
git clone <your-repo> alphatrade && cd alphatrade

python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"
```

### 2. Generate secrets

```sh
python - <<'PY'
import secrets
for name in ("SECRET_KEY", "CREDENTIAL_ENCRYPTION_KEY", "JWT_SECRET"):
    print(f"{name}={secrets.token_urlsafe(48)}")
PY
```

### 3. Configure

```sh
cp backend/.env.example .env
```

Set at minimum:

| Variable | Meaning |
| --- | --- |
| `SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, `JWT_SECRET` | From the command above |
| `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD` | The first admin, created on first boot |
| `KOTAK_CONSUMER_KEY` | Kotak Neo → invest → trade API card |
| `KOTAK_TOTP_KEY`, `KOTAK_MOBILE_NUMBER`, `KOTAK_MPIN`, `KOTAK_UCC` | From your Kotak Neo account |

**Get the consumer key right.** Kotak rejects an unknown key with
`Consumer key … does not exist`, and every market-data and order call then fails. Generate a
fresh one from the Kotak Neo app rather than reusing a sample from a tutorial.

### 4. Run

```sh
# API
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal
cd web && npm install && npm run dev
```

Open <http://localhost:3000>, sign in with the bootstrap admin, then visit **Settings →
Sync symbol master** (or call `POST /api/v1/market/sync-master`) to load the instrument master
from Kotak.

---

## Deployment

See **[DEPLOY.md](DEPLOY.md)** for Docker Compose, systemd, nginx and CI/CD.

Short version, the three things that make it "always running":

1. **Auto-restart** — `restart: unless-stopped` in Compose, `Restart=always` in systemd.
2. **Self-healing broker session** — `backend/broker/neo_auth.py` caches the Neo session,
   re-authenticates when it expires, and a keepalive thread refreshes it every 5 minutes.
3. **External liveness check** — point a free cron service at `/api/v1/system/health`.

---

## Security

* Passwords hashed with PBKDF2-HMAC-SHA256 (240k rounds).
* JWT access tokens, HS256, verified in constant time.
* API keys stored hashed; the plaintext is shown exactly once.
* Kotak secrets encrypted at rest with Fernet. Without `CREDENTIAL_ENCRYPTION_KEY` the vault
  refuses to persist — it never falls back to plaintext.
* Roles: `viewer` (read), `trader` (orders, API keys, paper), `admin` (users, risk limits).
* Live orders need the feature gate **and** the typed phrase `PLACE LIVE ORDER` **and** a passing
  risk check.
* The kill switch blocks every new order and halts running strategies until released.

> **The API has no user-facing login of its own beyond the app's JWT session.** Anything that can
> reach the app can reach the order endpoints. Put authentication in front of it (Cloudflare
> Access, Tailscale, basic auth) before exposing it to the internet.

---

## Testing

```sh
PYTHONPATH=. python backend/_smoke_indicators.py   # 41 checks
PYTHONPATH=. python backend/_smoke_options.py      # 38 checks
PYTHONPATH=. python backend/_smoke_engine.py       # 91 checks
PYTHONPATH=. python backend/_smoke_api.py          # 57 checks

cd web && npx tsc --noEmit && npm run build
```

The engine tests use synthetic candles **on purpose and only in tests**, to prove the maths and
the wiring. Nothing in `backend/` generates market data at runtime.

---

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `production` enables stricter behaviour |
| `POSTGRES_DSN`, `REDIS_URL` | — | Optional; SQLite needs no server |
| `DATA_DIR` | `./var` | Where tick and state databases live |
| `KOTAK_ENVIRONMENT` | `prod` | `prod` or `uat` |
| `KOTAK_KEEPALIVE`, `KOTAK_KEEPALIVE_SECONDS` | `1`, `300` | Session refresh loop |
| `KOTAK_SESSION_TTL_SECONDS` | `2700` | Force re-login after this age |
| `RECORD_TICKS`, `TICK_RETENTION_DAYS` | `1`, `30` | Tape recording |
| `BREADTH_UNIVERSE_SIZE` | `200` | Instruments scanned for breadth |
| `ENABLE_LIVE_TRADING` | `0` | Master switch for real orders |
| `ENABLE_PAPER_TRADING` | `1` | Paper engine |
| `ENABLE_BACKTEST` | `1` | Backtester |
| `ENABLE_ALGO_EXECUTION` | `0` | Deploying strategies to trade |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Telegram alerts |
| `WHATSAPP_API_KEY`, `WHATSAPP_SENDER`, `WHATSAPP_RECIPIENT` | — | WhatsApp alerts |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` | — | Email alerts |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | — | Optional LLM layer of the AI coach |

---

## Scope and honesty

* **Market data and execution are Kotak Neo only.** There is no fallback broker and no fallback
  data source.
* **Greeks are computed**, using exchange-reported IV or an IV backed out of the traded premium
  by bisection. They are never assumed.
* **The AI coach reviews process, not markets.** It detects patterns in your own trade history
  and never proposes a trade. The optional LLM sees only numbers already computed locally and has
  no broker access.
* **Risk limits are on by default** with conservative values, so the engine blocks reckless orders
  from the first trade rather than after a loss.
* **This is not investment advice.** It is trading infrastructure. You are responsible for every
  order it sends.
