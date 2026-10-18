# AlphaTradePro
Institutional algorithmic trading for the Indian markets, built on **two brokers with two jobs**:

| Role | Broker | Why |
| --- | --- | --- |
| **Market data** — instrument master, live quotes, historical candles | **Angel One SmartAPI** | Kotak Neo publishes **no historical candle API**, so candles and the instrument master come from Angel |
| **Execution** — place / modify / cancel orders | **Kotak Neo** *and/or* **Angel One** | A signal can be routed to either broker, or fanned out to both |

No simulated prices, no demo data, no placeholder option chains. Every figure on every screen
comes from Angel One SmartAPI. When the data session is down, the interface says so — it never
fills the gap with invented numbers.

---

## Data flow
```
                 ┌──────────────────────────────┐
   Angel One     │  instrument master · live quotes · candles    │
   SmartAPI  ───►│  (1m 3m 5m 15m 30m 60m 1d)                    │───►  Website / charts /
   (data only)   │  NO order flow goes here for data purposes    │      backtests / algo engine
                 └──────────────────────────────┘
                                        │
                                        ▼
                                 Strategy / order signal
                                        │
                     ┌──────────────────┴──────────────────┐
                     ▼                                     ▼
            Kotak Neo Trade API                   Angel One SmartAPI
            (order execution)                     (order execution)
```

* Angel One is the **only** market-data source. Kotak is never asked for candles or quotes.
* Kotak Neo is the **default** execution venue; Angel One execution is opt-in per order.
* `broker: "both"` fans one signal out to every enabled broker, each resolved to *its own*
  account — an Angel order cannot be sent to a Kotak client code or vice versa.

---

## Why the split exists
Kotak Neo's quote endpoints are live-only and its scrip master is static reference data, not
price history. A backtester over candles this platform invented for itself would be fiction;
a backtester over Angel One's real historical candles is not. So:

* **Candles** come from Angel's `/historical/v1/getCandleData`, converted from IST and cached.
* **The instrument master** comes from Angel's public `OpenAPIScripMaster.json`, normalised into
  the shared `instruments` table (`source = 'angel'`).
* **Live quotes** come from Angel's `/market/v1/quote` (LTP / OHLC / FULL), normalised into the
  platform's `Quote` and recorded as ticks so coverage keeps growing.
* **Every order** still clears the same gates it always did before reaching Kotak or Angel.

> **SEBI note.** Kotak Neo requires a **registered static IP** for its order APIs. Register this
> host's public IP with Kotak before enabling live orders.

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
                              ├──► Angel One SmartAPI   (market data: master, quotes, candles)
                              ├──► Kotak Neo Trade API  (order execution: TOTP + MPIN session)
                              └──► Angel One SmartAPI   (order execution: TOTP session)
```

```
backend/
  core/        config, security (PBKDF2 + JWT), vault (Fernet, both brokers), database (SQLite)
  angel/       auth (SmartAPI sessions), instruments (master sync), candles, quotes, orders, feed
  broker/      neo_auth (Kotak session manager), accounts (multi-broker credential registry)
  marketdata/  quotes (cache), ticks (recorder), market_hours, master (shared instrument table)
  strategies/  indicators, dsl (no-code engine), templates
  options/     chain (Black-Scholes Greeks, PCR, max pain)
  backtesting/ engine
  papertrading/engine
  execution/   brokers (Kotak + Angel adapters), order_service (routing + risk), algo (runner), charges
  risk/        engine (limits, kill switch)
  analytics/   metrics (Sharpe, Sortino, CAGR, drawdown)
  alerts/      service (Telegram, WhatsApp, email)
  ai/          coach (deterministic patterns + optional LLM)
  api/         routes, deps (auth/RBAC, angel_call), ws
  deployment.py  static IP detection + redirect URL resolution (SEBI prerequisites)
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

Then connect credentials **in the app** (Settings → Connect broker), which encrypts them in the
vault. Environment variables are only a single-account fallback:

| Variable | Broker | Purpose |
| --- | --- | --- |
| `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_KEY` | Angel One | **Market data** (create the key at SmartAPI → My Apps) |
| `KOTAK_CONSUMER_KEY`, `KOTAK_TOTP_KEY`, `KOTAK_MOBILE_NUMBER`, `KOTAK_MPIN`, `KOTAK_UCC` | Kotak Neo | **Order execution** |

**Two independent authentication flows.** Angel and Kotak each have their own TOTP session; the
platform runs a keepalive for each and a login for one never touches the other.

**Get the Kotak consumer key right.** Kotak rejects an unknown key with
`Consumer key … does not exist`, and every order call then fails. Generate a fresh one from the
Kotak Neo app rather than reusing a sample from a tutorial.

### 4. Run
```sh
# API
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload

# Terminal
cd web && npm install && npm run dev
```

Open <http://localhost:3000>, sign in with the bootstrap admin, then connect an **Angel One**
account (which powers all market data) and a **Kotak Neo** account (for live orders). The
instrument master syncs from Angel automatically on first boot and can be re-run from
**Settings**, or via `POST /api/v1/market/sync-master`.

---

## Execution routing
`POST /api/v1/orders/live` accepts a `broker` field:

| `broker` | Behaviour |
| --- | --- |
| `"kotak"` (default) | Order goes to Kotak Neo |
| `"angel"` | Order goes to Angel One SmartAPI |
| `"both"` | The same signal is sent to every enabled broker; each leg reports its own result |

The default is `DEFAULT_EXECUTION_BROKER`; the set of brokers allowed at all is `EXECUTION_BROKERS`.
The order ticket in the UI exposes the same choice, and the algo engine carries it on the run.

Every live order — hand-placed or from a strategy — passes through `execution/order_service.py`, so
the feature gate, typed confirmation, validation and risk check apply identically no matter which
broker receives it. Modify and cancel look the broker up from the local order record, so the UI
needs no broker argument.

---

## Deployment

See **[DEPLOY.md](DEPLOY.md)** for Docker Compose, systemd, nginx and CI/CD.
Running a single always-on VM? **[CLOUD.md](CLOUD.md)** wires the stack with a watchdog and
backups, and **[HOSTINGER.md](HOSTINGER.md)** adapts it step by step to a Hostinger VPS
(hPanel firewall, DNS, dedicated static IP for the SEBI requirement, certbot).

Short version, the things that make it "always running":

1. **Auto-restart** — `restart: unless-stopped` in Compose, `Restart=always` in systemd.
2. **Two self-healing sessions** — `backend/broker/neo_auth.py` keeps Kotak logged in, and
   `backend/angel/auth.py` keeps Angel fresh (its refresh token mints new JWTs without a TOTP).
3. **External liveness check** — point a free cron service at `/api/v1/system/health`, which reports
   the data feed and both broker sessions separately.

---

## Security

* Passwords hashed with PBKDF2-HMAC-SHA256 (240k rounds).
* JWT access tokens, HS256, verified in constant time.
* API keys stored hashed; the plaintext is shown exactly once.
* **Both** brokers' secrets encrypted at rest with Fernet, tagged with their broker so a rotation
  never mixes them up. Without `CREDENTIAL_ENCRYPTION_KEY` the vault refuses to persist — it never
  falls back to plaintext.
* Roles: `viewer` (read), `trader` (orders, API keys, paper), `admin` (users, risk limits, master sync).
* Live orders need the feature gate **and** the typed phrase `PLACE LIVE ORDER` **and** a passing
  risk check — on every broker.
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
PYTHONPATH=. python backend/_smoke_api.py          # 58 checks

cd web && npx tsc --noEmit && npm run build
```

The engine and indicator tests use synthetic candles **on purpose and only in tests**, to prove the
maths and the wiring. Nothing in `backend/` generates market data at runtime.

---

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `production` enables stricter behaviour |
| `DATA_DIR` | `./var` | Where the tick, state and session databases live |
| **Deployment (static IP + redirect, SEBI)** | | |
| `STATIC_IP` | — | The public IP registered with the broker; drives drift detection |
| `STATIC_IP_RESERVED` | `0` | `1` once a real Elastic/Reserved IP is attached |
| `PUBLIC_IP_OVERRIDE` | — | State the egress IP directly, skipping detection |
| `PUBLIC_IP_CHECK` | `1` | Turn the public-IP lookup off entirely |
| `PUBLIC_URL` | — | Canonical origin; the redirect URL derives from it |
| `REDIRECT_PATH` | `/callback` | The broker callback path |
| `REDIRECT_URL` | — | Pin the exact redirect string a broker was given |
| `REDIRECT_ALLOWLIST` | — | Extra allowed callback targets (comma-separated) |
| `REQUIRE_HTTPS` | `0` | Reject a redirect that arrives over plain HTTP |
| **Angel One (data + optional execution)** | | |
| `ANGEL_ENVIRONMENT` | `prod` | `prod` or `uat` |
| `ANGEL_SESSION_TTL_SECONDS` | `28800` | Force a fresh login after this age |
| `ANGEL_RATE_LIMIT_PER_SECOND` | `3` | SmartAPI allows ~3 requests/second |
| `ANGEL_MAX_TOKENS_PER_CALL` | `50` | Symbols per quote call |
| `ANGEL_HISTORY_DAYS_DEFAULT` / `_MAX` | `30` / `1800` | Candle lookback window |
| **Kotak Neo (execution)** | | |
| `KOTAK_ENVIRONMENT` | `prod` | `prod` or `uat` |
| `KOTAK_KEEPALIVE`, `KOTAK_KEEPALIVE_SECONDS` | `1`, `300` | Session refresh loop |
| `KOTAK_SESSION_TTL_SECONDS` | `2700` | Force re-login after this age |
| **Execution routing** | | |
| `DEFAULT_EXECUTION_BROKER` | `kotak` | `kotak`, `angel` or `both` |
| `EXECUTION_BROKERS` | `kotak,angel` | Brokers this deployment may route to |
| `DUAL_EXECUTION_SIZE` | `1.0` | Size multiplier for a fanned-out signal |
| **Market data** | | |
| `RECORD_TICKS`, `TICK_RETENTION_DAYS` | `1`, `30` | Tape recording |
| `BREADTH_UNIVERSE_SIZE` | `200` | Instruments scanned for breadth |
| `CANDLE_INTERVALS` | `1m,3m,5m,15m,30m,60m,1d` | Intervals offered to the UI |
| **Feature gates** | | |
| `ENABLE_LIVE_TRADING` | `0` | Master switch for real orders |
| `ENABLE_PAPER_TRADING` | `1` | Paper engine |
| `ENABLE_BACKTEST` | `1` | Backtester |
| `ENABLE_ALGO_EXECUTION` | `0` | Deploying strategies to trade |
| **Alerts / AI (optional)** | | |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Telegram alerts |
| `WHATSAPP_API_KEY`, `WHATSAPP_SENDER`, `WHATSAPP_RECIPIENT` | — | WhatsApp alerts |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` | — | Email alerts |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | — | Optional LLM layer of the AI coach |

---

## Scope and honesty

* **Market data is Angel One only. Execution is Kotak Neo and/or Angel One.** There is no third
  fallback broker and no fallback data source.
* **A static IP and a registered redirect URL are deployment prerequisites**, not optional extras.
  SEBI requires the broker to see a fixed source address on order APIs, and OAuth flows need a
  pre-registered callback. Both are reported in **Settings → Deployment**, and the platform warns
  the moment the egress IP stops matching the one that was registered.
* **Candles are Angel's real history**, not a tape this platform invented. The historic feed is
  chunked, IST-corrected and cached; coverage grows as ticks are recorded too.
* **Greeks are computed**, using exchange-reported IV or an IV backed out of the traded premium by
  bisection. They are never assumed.
* **The AI coach reviews process, not markets.** It detects patterns in your own trade history
  and never proposes a trade. The optional LLM sees only numbers already computed locally and has
  no broker access.
* **Risk limits are on by default** with conservative values, so the engine blocks reckless orders
  from the first trade rather than after a loss.
* **This is not investment advice.** It is trading infrastructure. You are responsible for every
  order it sends.
