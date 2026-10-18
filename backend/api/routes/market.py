"""Market data routes: status, instruments, quotes, candles and the feed.

Market data is served by **Angel One SmartAPI** — the instrument master, live
quotes and historical candles all come from here. Kotak Neo is never asked for
market data; it only receives orders.

Every route below resolves an Angel session through
:func:`backend.api.deps.angel_call`, which keeps the credentials and the
session pool out of the route bodies.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.angel import breadth as angel_breadth
from backend.angel import candles as angel_candles
from backend.angel import instruments as angel_instruments
from backend.angel.feed import angel_feed
from backend.angel.quotes import QuoteError as AngelQuoteError
from backend.angel.quotes import fetch as angel_fetch
from backend.api.deps import CurrentUser, angel_call, current_user, require_admin
from backend.core.config import settings
from backend.marketdata import market_hours, master, ticks
from backend.marketdata.quotes import quote_engine

router = APIRouter(prefix="/api/v1/market", tags=["market"])

#: Intervals the candle endpoint accepts (Angel's historical vocabulary).
SUPPORTED_INTERVALS = list(ticks.INTERVAL_SECONDS)


@router.get("/status")
def status_route(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Market session, feed health and instrument-master state.

    `scrip_master` reports the shared table (populated from Angel);
    `angel_instruments` reports Angel's own sync bookkeeping.
    """
    return {
        "session": market_hours.session_state(),
        "seconds_until_open": market_hours.seconds_until_open(),
        "data_source": "angel_one",
        "feed": angel_feed.health(),
        "scrip_master": master.status(),
        "angel_instruments": angel_instruments.status(),
        "quotes_cache": quote_engine.health(),
        "record_ticks": settings.record_ticks,
        "intervals": SUPPORTED_INTERVALS,
        "server_time": time.time(),
    }


@router.get("/search")
def search_instruments(
    q: str = Query(min_length=1, max_length=40),
    limit: int = Query(default=25, ge=1, le=100),
    exchange_segment: str | None = None,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Search any NSE/BSE stock, index, future or option contract."""
    if not master.status()["instruments"]:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The instrument master is empty. Sync the Angel One instrument master first.",
        )
    results = master.search(q, limit=limit, exchange_segment=exchange_segment)
    return {"query": q, "count": len(results), "results": results}


@router.get("/instruments/{token}")
def get_instrument(
    token: str,
    exchange_segment: str = Query(default="nse_cm"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    instrument = master.get_instrument(token, exchange_segment)
    if instrument is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instrument {token} in {exchange_segment}")
    quote = quote_engine.cached(token, exchange_segment)
    return {"instrument": instrument, "quote": quote.to_dict() if quote else None}


@router.get("/indices")
def list_indices(user: CurrentUser = Depends(current_user)) -> list[dict[str, Any]]:
    """Nifty 50, Bank Nifty, FinNifty, Midcap Nifty and Sensex, with live prices."""
    indices = master.indices("nse_cm") + master.indices("bse_cm")
    result = []
    for index in indices:
        quote = quote_engine.cached(index["token"], index["exchange_segment"])
        result.append({**index, "quote": quote.to_dict() if quote else None})
    return result


@router.get("/underlyings")
def list_underlyings(
    exchange_segment: str = Query(default="nse_fo"),
    user: CurrentUser = Depends(current_user),
) -> list[str]:
    return master.underlyings(exchange_segment)


@router.get("/futures")
def list_futures(
    exchange_segment: str = Query(default="nse_fo"),
    user: CurrentUser = Depends(current_user),
) -> list[dict[str, Any]]:
    return master.futures(exchange_segment)


class QuoteRequestModel(BaseModel):
    """A batch quote request."""

    tokens: list[dict[str, str]] = Field(default_factory=list, max_length=500)
    quote_type: str = "all"


@router.post("/quotes")
def fetch_quotes(
    payload: QuoteRequestModel,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Fetch a live snapshot from Angel One for `{instrument_token, exchange_segment}` pairs."""
    tokens = payload.tokens
    if not tokens:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide at least one instrument")

    call = angel_call(user)
    keys = [
        (str(item.get("instrument_token") or item.get("token") or ""), item.get("exchange_segment", "nse_cm"))
        for item in tokens
    ]
    keys = [(token, segment) for token, segment in keys if token]
    try:
        quotes = angel_fetch(call, keys, payload.quote_type)
    except AngelQuoteError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    for quote in quotes:
        quote_engine.store(quote)
        ticks.record_quote(quote)
    ticks.flush()
    return {
        "as_of": time.time(),
        "source": "angel_one",
        "count": len(quotes),
        "quotes": [quote.to_dict() for quote in quotes],
    }


@router.get("/candles/{token}")
def get_candles(
    token: str,
    exchange_segment: str = Query(default="nse_cm"),
    interval: str = Query(default="5m"),
    limit: int = Query(default=300, ge=1, le=5000),
    days: int | None = Query(default=None, ge=1, le=1800),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Historical OHLCV candles from Angel One SmartAPI.

    This is the endpoint that makes the dual-broker split worthwhile: Kotak Neo
    publishes no historical candles at all, so charts, indicators and backtests
    read Angel's candle API. The candles are also persisted, so coverage grows
    and a repeated request costs nothing.
    """
    if interval not in angel_candles.INTERVAL_MAP:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unsupported interval '{interval}'. Available: {', '.join(angel_candles.supported_intervals())}",
        )
    call = angel_call(user)
    try:
        candles, from_cache = angel_candles.history(
            call, token, exchange_segment, interval, limit, days=days,
        )
    except angel_candles.CandleError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    # Map to the exact contract the chart/indicator layer expects:
    # [{time, open, high, low, close, volume}, ...].
    candles = angel_candles.format_candles(candles)
    coverage = ticks.coverage(token, exchange_segment)
    return {
        "token": token,
        "exchange_segment": exchange_segment,
        "interval": interval,
        "source": "angel_one",
        "candles": candles,
        "count": len(candles),
        "cached": from_cache,
        "coverage": coverage,
        "note": (
            "No recorded candles for this instrument yet. The store fills on the first "
            "fetch and on every feed reconnect; check the Angel session if this persists."
            if not candles else None
        ),
    }


@router.post("/sync-master")
def sync_master(
    force: bool = Query(default=False),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """Download the instrument master from Angel One SmartAPI.

    The file is public (it needs no session), so this endpoint does not touch
    the broker credentials — it is an admin action because it rewrites the
    shared `instruments` table.
    """
    if not force and not angel_instruments.is_due():
        return {"status": "skipped", "reason": "A sync already ran recently.", **angel_instruments.status()}
    result = angel_instruments.sync()
    angel_candles.clear_cache()
    return {**result, "master": master.status()}


@router.get("/breadth")
def market_breadth(
    limit: int = Query(default=settings.breadth_universe_size, ge=10, le=1000),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Return the latest background-computed Advance/Decline snapshot."""
    snapshot = angel_breadth.market_breadth_worker.snapshot()
    if snapshot:
        return snapshot
    # On a cold start, populate synchronously once so the dashboard does not
    # display a misleading 0.00 while the worker obtains its first snapshot.
    snapshot = angel_breadth.market_breadth_worker.refresh_once()
    if not snapshot:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Market breadth is not ready. Ensure the Angel session and instrument master are available.",
        )
    return snapshot


@router.get("/feed/subscribe")
def subscribe_feed(
    tokens: str = Query(description="Comma-separated instrument tokens"),
    exchange_segment: str = Query(default="nse_cm"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Start streaming the given instruments over the WebSocket."""
    parsed = [token.strip() for token in tokens.split(",") if token.strip()]
    if not parsed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No tokens supplied")
    added = angel_feed.subscribe([
        {"instrument_token": token, "exchange_segment": exchange_segment} for token in parsed
    ])
    return {
        "subscribed": added,
        "total": len(angel_feed.subscription_tokens()),
        "source": "angel_one",
        "feed": angel_feed.health(),
    }


@router.get("/feed/unsubscribe")
def unsubscribe_feed(
    tokens: str = Query(description="Comma-separated instrument tokens"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    removed = angel_feed.unsubscribe([token.strip() for token in tokens.split(",") if token.strip()])
    return {"unsubscribed": removed, "total": len(angel_feed.subscription_tokens())}
