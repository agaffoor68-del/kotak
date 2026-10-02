"""AlphaTradePro FastAPI application.

All market data and all execution flow through Kotak Neo. Nothing in this
service invents prices: when the broker is unreachable the API returns an error
the UI can show, not a placeholder.
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
from backend.api.routes import analytics, auth, execution, market, options, share, strategies
from backend.api.ws import router as ws_router
from backend.broker.neo_auth import session_manager
from backend.core.config import settings
from backend.marketdata import master
from backend.marketdata.feed import market_feed

log = logging.getLogger("alphatrade")

DESCRIPTION = """
Institutional trading platform for the Indian markets, connected exclusively to
**Kotak Neo**.

* Live market data over the Kotak Neo REST quote API and WebSocket feed
* Real order routing, modify and cancel through the Kotak Neo trade API
* Option chain analytics with Black-Scholes Greeks, PCR and max pain
* A no-code strategy builder, backtester and algo execution engine
* Paper trading, a risk engine with a kill switch, and an AI trading coach

**Data note.** Kotak Neo publishes no historical candles. This platform records the
live tape itself, so backtests and charts cover the period it has been running —
and the API says so explicitly when there is no data, rather than inventing any.
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_bootstrap.startup()
    log.info("Environment: %s", settings.environment)

    # Background services. The keepalive keeps Neo logged in; the feed keeps the
    # tape recording so charts and backtests have real history.
    session_manager.start_keepalive(_credential_resolver)
    market_feed.start()

    yield

    log.info("Shutting down AlphaTradePro")
    market_feed.stop()


def _credential_resolver() -> dict[str, Any]:
    from backend.broker import accounts

    try:
        return accounts.resolve_all()
    except Exception as error:  # noqa: BLE001
        log.warning("Could not resolve broker credentials: %s", error)
        return {}


app = FastAPI(
    title="AlphaTradePro",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=settings.allowed_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-QR-Payload"],
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
app.include_router(market.router)
app.include_router(options.router)
app.include_router(strategies.router)
app.include_router(execution.router)
app.include_router(analytics.router)
app.include_router(share.router)
app.include_router(ws_router)


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "AlphaTradePro",
        "version": "1.0.0",
        "broker": "Kotak Neo (exclusive)",
        "docs": "/docs",
        "health": "/api/v1/system/health",
        "share": "/api/v1/share",
        "share_qr": "/api/v1/share/qr.png",
        "auth": "/api/v1/auth/login",
    }


@app.get("/api/v1/system/health", tags=["meta"])
def health() -> dict[str, Any]:
    """Liveness and dependency health.

    Reports the real state of each dependency, so a monitor can distinguish
    "process up" from "broker connected".
    """
    from backend.marketdata.quotes import quote_engine

    return {
        "status": "ok",
        "time": time.time(),
        "environment": settings.environment,
        "broker": "kotak_neo",
        "feed": market_feed.health(),
        "scrip_master": master.status(),
        "quotes": quote_engine.health(),
        "sessions": {
            "registry": "ready",
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
