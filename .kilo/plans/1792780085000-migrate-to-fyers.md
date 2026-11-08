# Plan: Migrate to Fyers API v3 (replace Angel One + yfinance, keep Kotak Neo for orders)

## Goal
Replace Angel One for market data (quotes, option chain, historical) and yfinance for backtesting with Fyers API v3. Keep Kotak Neo only for order execution with symbol mapping.

## Scope
- Auth: OAuth2 flow, token storage/refresh
- Symbol master: download NSE_FO/NSE_CM/BSE_CM CSVs, local index
- Option chain: Fyers optionchain API
- Historical: Fyers history API (chunked)
- WebSocket: Fyers Data Socket
- Keep orders/execution with Kotak, add mapping from Fyers symbol to Kotak token

## Current state
- Angel used for quotes/candles/master/feeds
- yfinance used for historical in backtest pipeline
- Kotak used for orders

## 1) Dependencies
Add to requirements: `fyers-apiv3`, `pandas`, `requests`, `websocket-client` (if needed). Also `pyotp` already present.

## 2) Architecture

### 2.1 Config/Env
Add Fyers settings: `FYERS_APP_ID`, `FYERS_SECRET_KEY`, `FYERS_REDIRECT_URI`, `FYERS_ACCESS_TOKEN`, `FYERS_REFRESH_TOKEN`

### 2.2 Auth module `backend/fyers/auth.py`
- Generate auth URL: `fyersModel.generate_authcode()`
- Exchange auth code: `fyersModel.generate_token(data)`
- Store tokens (env/DB)
- Refresh logic (daily expiry)

### 2.3 Routes `backend/api/routes/fyers.py`
- GET `/api/v1/fyers/login` -> auth URL
- GET `/api/v1/fyers/callback` -> exchange code, save tokens

### 2.4 Symbol master `backend/fyers/symbol_master.py`
- Download CSVs from public.fyers.in
- Parse and build SQLite/in-memory index: `fyres_symbol -> fyers_symbol_id` (e.g., `NSE:NIFTY26OCT23100CE`)
- Replace master.search for market data

### 2.5 Option chain `backend/fyers/option_chain.py`
- Use Fyers optionchain API
- Rate limit 5 req/s (sleep 0.2s), handle 429 with retry
- Map to frontend format

### 2.6 Historical `backend/fyers/historical.py`
- Fyers history API, chunk 100 days
- Save to DB in format backtest expects
- Remove yfinance usage

### 2.7 WebSocket `backend/fyers/feed.py`
- FyersDataSocket, subscribe up to 100 symbols
- Parse ticks, update candles/watchlist

### 2.8 Order mapping
In execution, map `fyers_symbol` (NSE:...) to Kotak token via master

## 3) Integration points
- Replace angel calls in market routes where data source is data
- Update backtest to use Fyers historical
- Update feeds to use Fyers
- Keep execution unchanged but add mapping

## 4) Files to create/modify
Create: `backend/fyers/{__init__,auth,symbol_master,option_chain,historical,feed}.py`, `backend/api/routes/fyers.py`
Modify: `backend/main.py` (add router), `backend/api/routes/market.py`, `backend/api/routes/options.py`, `backend/backtest/*` or strategies/backtest, `backend/marketdata/*`

## 5) Risks
- Fyers API differences vs Angel
- Token expiry handling
- Rate limits
- Symbol format differences
- WebSocket limits

## 6) Validation
- Auth flow works
- Symbol search works
- Option chain returns data
- Historical downloads
- WebSocket streams
- Orders still work via Kotak

Ready for implementation.
