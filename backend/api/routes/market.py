"""Market data routes: status, instruments, quotes, candles and the feed.

Market data is served by **Angel One SmartAPI** — the instrument master, live
quotes and historical candles all come from here. Kotak Neo is never asked for
market data; it only receives orders.

Every route below resolves an Angel session through
:func:`backend.api.deps.angel_call`, which keeps the credentials and the
session pool out of the route bodies.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.angel import breadth as angel_breadth
from backend.angel import candles as angel_candles
from backend.angel import instruments as angel_instruments
from backend.angel.feed import angel_feed
from backend.angel.quotes import QuoteError as AngelQuoteError
from backend.angel.quotes import fetch as angel_fetch
from backend.api.deps import CurrentUser, angel_call, current_user, require_admin
from backend.core.config import settings
from backend.core.database import app_cursor, now
from backend.marketdata import market_hours, master, search_index, ticks
from backend.marketdata.quotes import quote_engine

router = APIRouter(prefix="/api/v1/market", tags=["market"])

log = logging.getLogger("alphatrade.api.market")

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
        "search_index": search_index.status(),
        "quotes_cache": quote_engine.health(),
        "record_ticks": settings.record_ticks,
        "intervals": SUPPORTED_INTERVALS,
        "server_time": time.time(),
    }


@router.get("/search")
def search_instruments(
    q: str = Query(min_length=1, max_length=40),
    limit: int = Query(default=10, ge=1, le=50),
    exchange_segment: str | None = None,
    scope: str = Query(
        default="equity",
        description="'equity' searches the in-memory index of NSE/BSE cash instruments; "
        "'all' searches every segment, derivatives included",
    ),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Search the local instrument master by symbol or company name.

    The master is Angel One's scrip master, loaded into a local index: this
    never calls the broker and never scans the table, so the dropdown stays
    instant while the operator types. Every result carries the `token` the
    rest of the platform quotes by.
    """
    _require_master()
    if scope.strip().lower() == "all" and not exchange_segment:
        results = search_index.search_all(q, limit=limit)
    else:
        results = search_index.search(q, limit=limit, exchange_segment=exchange_segment)
    return {"query": q, "count": len(results), "results": results}


def _require_master() -> None:
    """Refuse to answer from an empty master instead of saying "no matches"."""
    if not master.status()["instruments"]:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The instrument master is empty. Sync the Angel One instrument master first.",
        )


@router.get("/resolve")
def resolve_symbol(
    symbol: str = Query(min_length=1, max_length=64),
    exchange_segment: str | None = None,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Turn a symbol the user typed into the instrument that carries the token.

    This is the conversion the platform depends on: "SBIN" becomes the Angel
    One symbol token and the exchange segment it must be quoted with.
    """
    _require_master()
    match = search_index.resolve(symbol, exchange_segment=exchange_segment)
    if match is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No instrument matches '{symbol}'. Search for it first.",
        )
    return match


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


@router.get("/quote/{token}")
def get_quote(
    token: str,
    exchange_segment: str | None = Query(default=None),
    refresh: bool = Query(default=False, description="Bypass the quote cache and call Angel One"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """The live quote for one instrument, by Angel One symbol token.

    The token is looked up in the local master to work out the exchange
    (NSE or BSE) it must be requested with, then Angel One's market-data
    endpoint answers with the LTP, the day's OHLC and the volume.

    A quote that is already in the cache and still fresh is returned as-is —
    the feed keeps it current, and SmartAPI only allows one request a second.
    Pass ``refresh=true`` to force a broker round trip.
    """
    instrument = search_index.find_instrument(token, exchange_segment)
    if instrument is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No instrument '{token}' in the master. Search for the symbol first.",
        )
    segment = str(instrument.get("exchange_segment") or "nse_cm")
    exchange = angel_instruments.SEGMENT_EXCHANGE.get(segment, "NSE")

    cached = quote_engine.cached(token, segment)
    if cached is not None and not refresh and not cached.is_stale:
        return {
            "token": str(token),
            "exchange_segment": segment,
            "exchange": exchange,
            "symbol": instrument.get("symbol"),
            "name": instrument.get("name"),
            "source": "cache",
            "cached": True,
            "quote": cached.to_dict(),
        }

    call = angel_call(user)
    try:
        quotes = angel_fetch(call, [(str(token), segment)], "all")
    except AngelQuoteError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    except Exception as error:  # noqa: BLE001 - the UI must get one clear message
        log.warning("Quote fetch failed for %s: %s", token, error)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not fetch the quote for {token}.") from error

    if not quotes:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Angel One returned no quote for {token} on {exchange}.",
        )
    quote = quotes[0]
    quote_engine.store(quote)
    ticks.record_quote(quote)
    ticks.flush()
    return {
        "token": str(token),
        "exchange_segment": segment,
        "exchange": exchange,
        "symbol": instrument.get("symbol"),
        "name": instrument.get("name"),
        "source": "angel_one",
        "cached": False,
        "quote": quote.to_dict(),
    }


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
) -> list[str]:
    return master.futures(exchange_segment)


# ------------------------------------------------------------------ watchlist


class WatchlistItem(BaseModel):
    """One instrument on the user's watchlist."""

    token: str = Field(min_length=1, max_length=32)
    symbol: str = Field(default="", max_length=64)
    exchange_segment: str | None = Field(default=None, max_length=16)


class WatchlistRequest(BaseModel):
    """Add one or more instruments to the watchlist."""

    items: list[WatchlistItem] = Field(default_factory=list, max_length=50)


def _watchlist_rows(user_id: str) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT token, exchange_segment, symbol, name, position, created_at
            FROM watchlist WHERE user_id = ?
            ORDER BY position ASC, created_at ASC
            """,
            (user_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


@router.get("/watchlist")
def get_watchlist(user: CurrentUser = Depends(current_user)) -> dict[str, Any]:
    """The user's watchlist, as instrument tokens the backend can quote."""
    rows = _watchlist_rows(user.id)
    return {"items": rows, "count": len(rows)}


@router.post("/watchlist")
def add_to_watchlist(
    payload: WatchlistRequest,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Add instruments to the watchlist.

    The token is resolved against the local master, so the symbol and the
    exchange segment are always the ones the broker expects. Adding the same
    instrument twice is a no-op rather than an error.
    """
    if not payload.items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide at least one instrument")

    resolved: list[dict[str, Any]] = []
    for item in payload.items:
        instrument = search_index.find_instrument(item.token, item.exchange_segment)
        if instrument is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Unknown instrument token '{item.token}'. Search for the symbol first.",
            )
        resolved.append({
            "token": str(instrument["token"]),
            "exchange_segment": str(instrument.get("exchange_segment") or "nse_cm"),
            "symbol": str(instrument.get("symbol") or item.symbol or instrument["token"]),
            "name": instrument.get("name"),
        })

    with app_cursor() as cursor:
        for entry in resolved:
            cursor.execute(
                """
                INSERT INTO watchlist (id, user_id, token, exchange_segment, symbol, name, position, created_at)
                VALUES (?,?,?,?,?,?,(SELECT COALESCE(MAX(position), -1) + 1 FROM watchlist WHERE user_id = ?),?)
                ON CONFLICT(user_id, token, exchange_segment) DO UPDATE SET
                    symbol = excluded.symbol, name = excluded.name
                """,
                (
                    uuid.uuid4().hex, user.id, entry["token"], entry["exchange_segment"],
                    entry["symbol"], entry["name"], user.id, now(),
                ),
            )

    rows = _watchlist_rows(user.id)
    return {"status": "ok", "count": len(rows), "items": rows}


@router.delete("/watchlist/{token}")
def remove_from_watchlist(
    token: str,
    exchange_segment: str | None = Query(default=None),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Remove an instrument from the watchlist."""
    with app_cursor() as cursor:
        if exchange_segment:
            cursor.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND token = ? AND exchange_segment = ?",
                (user.id, str(token), exchange_segment),
            )
        else:
            cursor.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND token = ?",
                (user.id, str(token)),
            )
        removed = cursor.rowcount

    rows = _watchlist_rows(user.id)
    return {"status": "ok", "removed": removed, "count": len(rows), "items": rows}



class HistoryDownloadRequest(BaseModel):
    symbol_token: str = Field(min_length=1, max_length=32)
    exchange: str = Field(default="NSE")
    interval: str = Field(default="FIVE_MINUTE")
    days: int = Field(default=5, ge=1, le=1800)


class PrefetchDataRequest(BaseModel):
    symbol_token: str = Field(min_length=1, max_length=32)
    exchange: str = Field(default="NSE")
    interval: str = Field(default="5m")
    start_date: datetime
    end_date: datetime


@router.post("/prefetch-data", status_code=status.HTTP_202_ACCEPTED)
def prefetch_data(
    payload: PrefetchDataRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Queue a bounded historical download; backtests never call SmartAPI."""
    if payload.interval not in angel_candles.INTERVAL_MAP and payload.interval not in angel_candles.INTERVAL_MAP.values():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported interval")
    if payload.start_date >= payload.end_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start_date must be before end_date")

    call = angel_call(user)
    background_tasks.add_task(
        angel_candles.download_historical_data_sync,
        call, payload.symbol_token, payload.exchange, payload.interval,
        payload.start_date, payload.end_date,
    )
    return {
        "status": "accepted", "symbol_token": payload.symbol_token,
        "exchange": payload.exchange, "interval": payload.interval,
        "start_date": payload.start_date.isoformat(), "end_date": payload.end_date.isoformat(),
        "message": "Historical download queued. Backtest after it completes.",
    }


@router.post("/download-history")
def download_history(
    payload: HistoryDownloadRequest,
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Download and persist Angel candles for manual backtest preparation."""
    try:
        candles = angel_candles.fetch_and_save_historical_candles(
            angel_call(user), payload.symbol_token, payload.exchange,
            payload.interval, payload.days,
        )
    except angel_candles.CandleError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    return {
        "status": "ok", "symbol_token": payload.symbol_token,
        "exchange": payload.exchange, "interval": payload.interval,
        "days": payload.days, "count": len(candles),
        "candles": angel_candles.format_candles(candles),
    }


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
