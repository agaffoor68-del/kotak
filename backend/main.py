"""AlphaTradePro FastAPI application.

Market data is served by **Angel One SmartAPI** (instrument master, quotes and
historical candles). Order execution is dual-broker: a signal can go to **Kotak
Neo**, to **Angel One**, or to both. Nothing in this service invents prices:
when a broker is unreachable the API returns an error the UI can show, not a
placeholder.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import app_bootstrap
from backend.angel.auth import angel_session_manager
from backend.angel.breadth import market_breadth_worker
from backend.angel.feed import angel_feed
from backend.api.routes import (
    admin_users,
    analytics,
    auth,
    broker,
    deployment,
    execution,
    market,
    options,
    share,
    strategies,
    user_settings,
)
from backend.api.ws import router as ws_router
from backend.broker import accounts
from backend.broker.neo_auth import session_manager
from backend.core.config import settings
from backend.marketdata import master
from backend.marketdata.feed import market_feed

log = logging.getLogger("alphatrade")

DESCRIPTION = """
Institutional trading platform for the Indian markets.

* Market data from **Angel One SmartAPI** — instrument master, live quotes and
  historical candles (1m to 1d)
* Dual-broker execution: orders route to **Kotak Neo**, **Angel One**, or both
* Option chain analytics with Black-Scholes Greeks, PCR and max pain
* A no-code strategy builder, backtester and algo execution engine
* Paper trading, a risk engine with a kill switch, and an AI trading coach

**Data note.** Kotak Neo publishes no historical candles, which is exactly why
this platform reads its market data from Angel One. Every figure is real; when a
broker is unreachable the API says so rather than inventing one.
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app_bootstrap.startup()
    except Exception:
        # Uvicorn prints "Application startup failed. Exiting." with no cause
        # unless we log the traceback here. Log it, then re-raise so the
        # process still exits non-zero and Render reports the deploy as failed.
        log.exception("Startup failed — refusing to serve with bad configuration")
        raise
    log.info("Environment: %s", settings.environment)

    # Background services. The Kotak keepalive keeps Neo logged in for order
    # execution; the Angel keepalive keeps the market-data session fresh; the
    # Angel feed records the tape and fans quotes out to the dashboard; the
    # Kotak feed streams HSM ticks (websocket) into the same quote cache.
    # None may kill the boot: without credentials they log and idle, and
    # Render's health check must still pass so the cause is visible in logs.
    try:
        session_manager.start_keepalive(_credential_resolver)
    except Exception:  # noqa: BLE001
        log.exception("Kotak keepalive failed to start; continuing without it")
    try:
        angel_session_manager.start_keepalive(_angel_credential_resolver)
    except Exception:  # noqa: BLE001
        log.exception("Angel keepalive failed to start; continuing without it")
    try:
        angel_feed.start(_angel_feed_resolver)
    except Exception:  # noqa: BLE001
        log.exception("Angel market feed failed to start; continuing without it")
    try:
        market_breadth_worker.start(_angel_feed_resolver)
    except Exception:  # noqa: BLE001
        log.exception("Angel market breadth worker failed to start; continuing without it")
    try:
        market_feed.start()
    except Exception:  # noqa: BLE001
        log.exception("Kotak Neo websocket feed failed to start; continuing without it")

    yield

    log.info("Shutting down AlphaTradePro")
    try:
        market_feed.stop()
    except Exception:  # noqa: BLE001
        log.exception("Error stopping the Kotak Neo websocket feed")
    try:
        market_breadth_worker.stop()
    except Exception:  # noqa: BLE001
        log.exception("Error stopping the Angel market breadth worker")
    try:
        angel_feed.stop()
    except Exception:  # noqa: BLE001
        log.exception("Error stopping the Angel market feed")


def _credential_resolver() -> dict[str, Any]:
    try:
        return accounts.resolve_kotak()
    except Exception as error:  # noqa: BLE001
        log.warning("Could not resolve Kotak credentials: %s", error)
        return {}


def _angel_credential_resolver() -> dict[str, Any]:
    try:
        return accounts.resolve_angel()
    except Exception as error:  # noqa: BLE001
        log.warning("Could not resolve Angel credentials: %s", error)
        return {}


def _angel_feed_resolver():
    """The (account_id, smartapi bridge) pair the market-data feed polls with."""
    from backend.api.deps import angel_call, resolve_angel_account_id

    account_id = resolve_angel_account_id()
    if not account_id:
        return None, None
    try:
        return account_id, angel_call(account_id=account_id)
    except Exception as error:  # noqa: BLE001
        log.warning("Angel feed could not build a session bridge: %s", error)
        return account_id, None


app = FastAPI(
    title="AlphaTradePro",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS: the deployed Vercel frontend must be explicitly allowed. In production
# the origin list is fixed (no regex), so a missing origin shows up as a browser
# "Failed to fetch" / CORS error with no backend traceback.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://kotak-jade.vercel.app",  # deployed Vercel frontend
        "http://localhost:3000",          # local dev
        "http://127.0.0.1:3000",          # local dev
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-QR-Payload"],  # required by api/routes/share.py QR download
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening headers on every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, error: Exception) -> JSONResponse:
    """Return a readable error rather than an empty 500."""
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error. The backend log has the full traceback.",
            "error_type": type(error).__name__,
            "path": request.url.path,
        },
    )


# ----------------------------------------------------------------- routes

app.include_router(auth.router)
app.include_router(broker.router)
app.include_router(market.router)
app.include_router(options.router)
app.include_router(strategies.router)
app.include_router(execution.router)
app.include_router(analytics.router)
app.include_router(share.router)
app.include_router(deployment.router)
app.include_router(deployment.callback_router)
app.include_router(ws_router)
app.include_router(admin_users.router)
app.include_router(user_settings.router)


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    from backend.execution import brokers as broker_registry

    return {
        "service": "AlphaTradePro",
        "version": "1.0.0",
        "data_source": "angel_one",
        "execution_brokers": broker_registry.enabled_brokers(),
        "default_execution_broker": settings.default_execution_broker,
        "static_ip": settings.registered_static_ip or None,
        "redirect_url": settings.redirect_url or None,
        "redirect_path": settings.redirect_path,
        "docs": "/docs",
        "health": "/api/v1/system/health",
        "deployment": "/api/v1/system/deployment",
        "callback": settings.redirect_path,
        "share": "/api/v1/share",
        "share_qr": "/api/v1/share/qr.png",
        "auth": "/api/v1/auth/login",
    }


@app.get("/api/v1/system/health", tags=["meta"])
def health() -> dict[str, Any]:
    """Liveness and dependency health.

    Reports the real state of each dependency, so a monitor can distinguish
    "process up" from "Angel data session live" and "Kotak order session live".
    """
    from backend.angel import instruments as angel_instruments
    from backend.api.deps import angel_session_health
    from backend.execution import brokers as broker_registry
    from backend.marketdata.quotes import quote_engine

    return {
        "status": "ok",
        "time": time.time(),
        "environment": settings.environment,
        "data_source": "angel_one",
        "execution_brokers": broker_registry.enabled_brokers(),
        "feed": angel_feed.health(),
        "kotak_websocket_feed": market_feed.health(),
        "scrip_master": master.status(),
        "angel_instruments": angel_instruments.status(),
        "quotes": quote_engine.health(),
        "angel_session": angel_session_health(),
        "deployment": {
            "static_ip": settings.registered_static_ip or None,
            "redirect_url": settings.redirect_url or None,
            "redirect_path": settings.redirect_path,
            "report": "/api/v1/system/deployment",
        },
        "sessions": {
            "registry": "ready",
            "kotak": "configured" if accounts.list_accounts("kotak") else "unconfigured",
            "angel": "configured" if accounts.list_accounts("angel") else "unconfigured",
        },
        "features": {
            "live_trading": settings.enable_live_trading,
            "paper_trading": settings.enable_paper_trading,
            "backtest": settings.enable_backtest,
            "algo_execution": settings.enable_algo_execution,
            "tick_recording": settings.record_ticks,
            "ai_coach_llm": bool(settings.openai_api_key),
        },
    }
