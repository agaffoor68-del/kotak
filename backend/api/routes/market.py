"""Market data routes: status, instruments, quotes, candles and the feed."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.api.deps import CurrentUser, broker_call, current_user, resolve_account_id, session_health
from backend.core.config import settings
from backend.marketdata import market_hours, master, ticks
from backend.marketdata.feed import market_feed
from backend.marketdata.quotes import QuoteError, quote_engine

router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/status")
def status_route(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """Market session, feed health and master-sync state."""
    account_id = resolve_account_id(user)
    return {
        "session": market_hours.session_state(),
        "seconds_until_open": market_hours.seconds_until_open(),
        "feed": market_feed.health(),
        "scrip_master": master.status(),
        "broker_session": session_health(account_id),
        "record_ticks": settings.record_ticks,
        "intervals": ticks.supported_intervals(),
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
            "The symbol master is empty. Run a scrip-master sync so real instruments are available.",
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
    """Fetch a live snapshot for a list of `{instrument_token, exchange_segment}` pairs."""
    account_id = resolve_account_id(user)
    tokens = payload.tokens
    if not tokens:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide at least one instrument")
    if len(tokens) > settings.max_tokens_per_quote_call:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"At most {settings.max_tokens_per_quote_call} instruments per request.",
        )

    call = broker_call(account_id)
    try:
        quotes = quote_engine.fetch(call, tokens, payload.quote_type)
    except QuoteError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error

    for quote in quotes:
        ticks.record_quote(quote)
    ticks.flush()
    return {
        "as_of": time.time(),
        "count": len(quotes),
        "quotes": [quote.to_dict() for quote in quotes],
    }


@router.get("/candles/{token}")
def get_candles(
    token: str,
    exchange_segment: str = Query(default="nse_cm"),
    interval: str = Query(default="5m"),
    limit: int = Query(default=300, ge=1, le=5000),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Recorded OHLCV candles.

    These are built from ticks this platform has recorded, because Kotak Neo
    publishes no historical candles. `coverage` makes the gap explicit.
    """
    if interval not in ticks.INTERVAL_SECONDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unsupported interval '{interval}'. Available: {', '.join(ticks.supported_intervals())}",
        )
    candles = ticks.history(token, exchange_segment, interval, limit)
    coverage = ticks.coverage(token, exchange_segment)
    return {
        "token": token, "exchange_segment": exchange_segment, "interval": interval,
        "candles": candles, "count": len(candles), "coverage": coverage,
    }


@router.post("/sync-master")
def sync_master(
    force: bool = Query(default=False),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Download the scrip master from Kotak (admin only through the broker gate)."""
    account_id = resolve_account_id(user)
    if not master.is_due(force):
        return {"status": "skipped", "reason": "A sync already ran recently.", **master.status()}
    call = broker_call(account_id)
    return master.sync(lambda method, *a, **k: call(method, *a, **k))


@router.get("/breadth")
def market_breadth(
    limit: int = Query(default=settings.breadth_universe_size, ge=10, le=1000),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Advance/decline breadth over a real universe of instruments."""
    account_id = resolve_account_id(user)
    universe = master.universe("nse_cm", limit)
    if not universe:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No instruments in the symbol master. Run a scrip-master sync first.",
        )
    tokens = [{"instrument_token": item["token"], "exchange_segment": item["exchange_segment"]} for item in universe]
    call = broker_call(account_id)
    quotes = quote_engine.fetch_batched(call, tokens, quote_type="ltp")

    return {
        "as_of": time.time(),
        "universe_size": len(universe),
        "priced": len([q for q in quotes if q.last is not None]),
        "breadth": market_hours.breadth(quotes),
        "top_gainers": market_hours.rank(quotes, limit=10, reverse=True),
        "top_losers": market_hours.rank(quotes, limit=10, reverse=False),
        "most_active": market_hours.most_active(quotes, limit=10),
    }


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
    added = market_feed.subscribe([
        {"instrument_token": token, "exchange_segment": exchange_segment} for token in parsed
    ])
    return {"subscribed": added, "total": len(market_feed.subscription_tokens()), "feed": market_feed.health()}


@router.get("/feed/unsubscribe")
def unsubscribe_feed(
    tokens: str = Query(description="Comma-separated instrument tokens"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    removed = market_feed.unsubscribe([token.strip() for token in tokens.split(",") if token.strip()])
    return {"unsubscribed": removed, "total": len(market_feed.subscription_tokens())}
