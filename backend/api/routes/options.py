"""Option chain routes.

Market data comes from **Angel One SmartAPI**, exactly like the rest of the
market-data layer. The chain resolves each contract's live price through
:func:`backend.angel.quotes.fetch`, never through the Kotak quote engine — Kotak
tokens and Angel tokens are different namespaces, so asking Kotak for an Angel
contract returns nothing and the chain renders empty.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.angel.quotes import QuoteError as AngelQuoteError
from backend.angel.quotes import fetch as angel_fetch
from backend.api.deps import CurrentUser, angel_call, broker_call, current_user, resolve_account_id
from backend.core.database import app_cursor
from backend.marketdata import master
from backend.marketdata.quotes import quote_engine
from backend.options.chain import build_chain, calculate_strike, strike_interval

log = logging.getLogger("alphatrade.api.options")

router = APIRouter(prefix="/api/v1/options", tags=["options"])

#: SmartAPI accepts at most 50 exchange tokens in one quote request.
_QUOTE_BATCH = 50

#: The pause between two quote requests. SmartAPI allows three requests a
#: second; 0.4s keeps the chain inside that budget with room to spare.
_QUOTE_BATCH_DELAY = 0.4

#: The master source whose tokens the SmartAPI quote endpoint understands.
_ANGEL_SOURCE = "angel"

#: The short names the desk uses and the index rows that carry their spot.
_SPOT_ALIASES = {
    "NIFTY": ("NIFTY 50",),
    "BANKNIFTY": ("NIFTY BANK", "BANK NIFTY"),
    "FINNIFTY": ("FIN NIFTY", "NIFTY FINANCIAL SERVICES"),
    "MIDCPNIFTY": ("MIDCAP NIFTY", "NIFTY MIDCAP"),
    "SENSEX": ("SENSEX",),
}


def _expiry_dates(underlying: str, exchange_segment: str) -> list[str]:
    """The expiry dates of an underlying, Angel One's master first.

    The chain quotes with SmartAPI, so the expiries of the master whose tokens
    SmartAPI understands are the ones that can actually be served.
    """
    for source in (_ANGEL_SOURCE, None):
        sql = """
            SELECT DISTINCT expiry FROM instruments
            WHERE (UPPER(underlying) = UPPER(?) OR UPPER(symbol) = UPPER(?))
              AND exchange_segment = ?
              AND option_type IN ('CE', 'PE')
              AND expiry IS NOT NULL AND expiry != ''
        """
        params: list[Any] = [underlying, underlying, exchange_segment]
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY expiry ASC"
        with app_cursor() as cursor:
            cursor.execute(sql, tuple(params))
            expiries = [row["expiry"] for row in cursor.fetchall() if row["expiry"]]
        if expiries:
            return expiries
    return master.option_expiries(underlying, exchange_segment)


def get_nearest_future_expiry(underlying: str, exchange_segment: str = "nse_fo") -> str | None:
    """Fetch distinct expiry dates from Angel One master, filter >= today in IST, and pick nearest future expiry."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(tz=ist).strftime("%Y-%m-%d")

    expiries = _expiry_dates(underlying, exchange_segment)

    future = [d for d in expiries if d >= today_ist]
    return min(future) if future else (expiries[0] if expiries else None)


@router.get("/expiries")
def expiry_dates(
    underlying: str = Query(description="Underlying symbol, e.g. NIFTY"),
    exchange_segment: str = Query(default="nse_fo"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Available expiry dates for an underlying, ascending (`getExpiryDate`)."""
    expiries = _expiry_dates(underlying, exchange_segment)

    near = get_nearest_future_expiry(underlying, exchange_segment)
    return {
        "underlying": underlying,
        "exchange_segment": exchange_segment,
        "expiries": expiries,
        "near": near,
        "count": len(expiries),
        "source": "angel_one",
        "as_of": time.time(),
        "note": (
            "No option expiries in the instrument master for this underlying. Run a scrip-master sync."
            if not expiries else None
        ),
    }


@router.get("/chain")
async def option_chain(
    underlying: str = Query(description="Underlying symbol, e.g. NIFTY"),
    exchange_segment: str = Query(default="nse_fo"),
    expiry: str | None = Query(default=None, description="ISO expiry date YYYY-MM-DD. If omitted, nearest future expiry is dynamically selected"),
    strike_type: str = Query(default="ATM", pattern="^(ITM|ATM|OTM)$", description="Strategy strike selection mode"),
    offset: int = Query(default=15, ge=0, le=50, description="Number of strike intervals from ATM"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Full option chain with OI, IV, Greeks, PCR, max pain and support/resistance.

    1. Expiry date is dynamically resolved to the nearest future expiry if not explicitly passed.
    2. Exact Angel One symbol tokens are fetched directly from the instrument master for each strike.
    3. Quotes are fetched with ``getMarketData("FULL", ...)`` in batches of 50 tokens,
       0.35s apart, so SmartAPI's request budget is never exceeded.
    """
    # 1. Dynamically select nearest future expiry if not provided
    selected_expiry = expiry or get_nearest_future_expiry(underlying, exchange_segment)

    # 2. Query option contracts directly from the instruments table
    raw_contracts = _load_contracts(underlying, exchange_segment, selected_expiry)

    # If empty, try fallback with master
    if not raw_contracts:
        contracts_envelope = master.option_contracts(
            underlying, exchange_segment, expiry=selected_expiry, source=_ANGEL_SOURCE,
        )
        if not contracts_envelope["rows"]:
            contracts_envelope = master.option_contracts(
                underlying, exchange_segment, expiry=selected_expiry,
            )
        by_expiry = contracts_envelope.get("rows", {})
        all_expiries = contracts_envelope.get("expiries", [])
    else:
        all_expiries = sorted({c["expiry"] for c in raw_contracts if c["expiry"]})
        by_expiry = {}
        for c in raw_contracts:
            exp_key = c["expiry"] or ""
            strike_val = float(c["strike"] or 0)
            bucket = by_expiry.setdefault(exp_key, {})
            strike_entry = bucket.setdefault(strike_val, {
                "strike": strike_val,
                "expiry": c["expiry"],
                "lot_size": c["lot_size"],
                "tick_size": c["tick_size"],
                "exchange_segment": c["exchange_segment"],
            })
            strike_entry[c["option_type"].lower()] = {
                "token": str(c["token"]),
                "trading_symbol": c["trading_symbol"],
                "exchange_segment": c["exchange_segment"],
            }
        contracts_envelope = {"expiries": all_expiries, "rows": by_expiry, "count": len(raw_contracts)}

    if not contracts_envelope["rows"]:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No option contracts for '{underlying}' in the symbol master. Please run an instrument sync.",
        )

    # If no expiry was selected yet, pick the nearest one from the envelope
    if not selected_expiry:
        selected_expiry = get_nearest_future_expiry(underlying, exchange_segment)
        if not selected_expiry and all_expiries:
            selected_expiry = all_expiries[0]

    target_rows = by_expiry.get(selected_expiry or "") or (by_expiry[all_expiries[0]] if all_expiries else {})

    # Quote only a bounded window around ATM. The full master remains available,
    # while missing legs are safely ignored by build_chain.
    underlying_record = _find_index_for(underlying)
    spot_quote = quote_engine.cached(underlying_record["token"], underlying_record["exchange_segment"]) if underlying_record else None
    spot = spot_quote.last if spot_quote and spot_quote.last else None
    interval = strike_interval(underlying)

    # The live spot decides where the strike window sits, so it is fetched
    # before the window is computed rather than after. One extra quote
    # request buys a chain that is actually centred on the money.
    spot_token = str(underlying_record["token"]) if underlying_record else None
    spot_segment = str(underlying_record["exchange_segment"]) if underlying_record else "nse_cm"
    if spot is None and spot_token:
        spot = await _fetch_spot(user, spot_token, spot_segment, underlying)

    available_strikes = sorted(float(value) for value in target_rows)
    selected_strikes = _strike_window(available_strikes, spot, interval, offset)
    if selected_strikes is not None:
        target_rows = {key: value for key, value in target_rows.items() if float(key) in selected_strikes}

    # 3. Collect exact symbol tokens for all strikes (Call and Put)
    token_keys_to_fetch: list[tuple[str, str]] = []

    for strike, entry in target_rows.items():
        for leg in ("call", "put"):
            leg_info = entry.get(leg) or {}
            tok = leg_info.get("token")
            seg = leg_info.get("exchange_segment") or exchange_segment
            if tok:
                token_keys_to_fetch.append((str(tok), seg))

    # De-duplicate token keys and log the exact NFO mapping before SmartAPI.
    unique_keys = list(dict.fromkeys(token_keys_to_fetch))
    for token, segment in unique_keys:
        log.info("Option chain mapped contract: underlying=%s segment=%s exchange=%s token=%s", underlying, segment, segment.upper(), token)

    # 4. Fetch live quotes via Angel One SmartAPI in batches of <= 50 tokens.
    token_to_quote_map, fetch_errors = await _fetch_quotes(user, unique_keys, underlying)

    def quote_lookup(token: str, segment: str):
        if not token:
            return None
        return token_to_quote_map.get(str(token))

    chain = build_chain(
        contracts_envelope,
        spot=spot,
        quote_lookup=quote_lookup,
        now_ts=time.time(),
        target_expiry=selected_expiry,
        strike_filter=selected_strikes,
    )
    chain["underlying"] = underlying
    chain["fetch_error"] = fetch_errors[0] if fetch_errors else None
    chain["fetch_errors"] = fetch_errors
    chain["quotes_fetched"] = len(token_to_quote_map)
    chain["contracts_requested"] = len(unique_keys)
    chain["spot_source"] = underlying_record["symbol"] if underlying_record else None
    chain["as_of"] = time.time()
    if not token_to_quote_map and not fetch_errors and not chain.get("note"):
        chain["note"] = (
            "The instrument master has the contracts but SmartAPI returned no quotes for them. "
            "Check the Angel One session and the exchange token mapping."
        )
    return chain


def _load_contracts(
    underlying: str,
    exchange_segment: str,
    expiry: str | None,
) -> list[dict[str, Any]]:
    """Every CE/PE contract of the underlying, in the broker's own token space.

    Angel One is the quote provider, so its rows are preferred: a Kotak token
    sent to SmartAPI is meaningless to it and makes it reject the whole
    request, which is what left the chain without a single price.
    """
    where = """
        (UPPER(underlying) = UPPER(?) OR UPPER(symbol) = UPPER(?))
        AND exchange_segment = ?
        AND option_type IN ('CE', 'PE')
    """
    params: list[Any] = [underlying, underlying, exchange_segment]
    if expiry:
        where += " AND expiry = ?"
        params.append(expiry)

    for source in (_ANGEL_SOURCE, None):
        clause = where
        source_params = list(params)
        if source:
            clause += " AND source = ?"
            source_params.append(source)
        with app_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT token, exchange_segment, trading_symbol, symbol, expiry, option_type,
                       strike, lot_size, tick_size, precision, source
                FROM instruments
                WHERE {clause}
                ORDER BY expiry ASC, strike ASC
                """,
                tuple(source_params),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        if rows:
            if source is None:
                log.warning(
                    "Option contracts for %s are not from the Angel One master; the tokens may not be quotable",
                    underlying,
                )
            return rows
    return []


def _strike_window(
    available: list[float],
    spot: float | None,
    interval: float,
    offset: int,
) -> set[float] | None:
    """The strikes to quote: a window around ATM, clipped to the master.

    A window that does not intersect the available strikes would render an
    empty chain, so it is narrowed to the closest strikes that do exist. When
    the spot price is unknown the middle of the chain is used as the anchor.
    """
    if not available:
        return None
    if spot:
        anchor = calculate_strike(spot, "ATM", "CE", interval=interval)
    else:
        anchor = available[len(available) // 2]

    radius = max(1, offset) * interval
    window = {value for value in available if abs(value - anchor) <= radius}
    if window:
        return window
    # The master uses a different strike step than the exchange default: fall
    # back to the closest strikes that do exist so the chain is never empty.
    closest = sorted(available, key=lambda value: abs(value - anchor))
    return set(closest[: max(1, 2 * max(1, offset) + 1)])


async def _fetch_spot(
    user: CurrentUser,
    token: str,
    segment: str,
    underlying: str,
) -> float | None:
    """The underlying's live last traded price, straight from SmartAPI."""
    try:
        call = await asyncio.to_thread(angel_call, user)
    except Exception:  # noqa: BLE001 - the chain still works without a spot
        log.exception("Option chain could not open an Angel One session for the %s spot", underlying)
        return None
    try:
        quotes = await asyncio.to_thread(angel_fetch, call, [(token, segment)], "all")
    except Exception:  # noqa: BLE001 - the chain still works without a spot
        log.exception("Option chain could not fetch the %s spot price", underlying)
        return None
    for quote in quotes:
        if quote.last:
            return float(quote.last)
    return None


async def _fetch_quotes(
    user: CurrentUser,
    keys: list[tuple[str, str]],
    underlying: str,
) -> tuple[dict[str, Any], list[str]]:
    """Fetch a FULL quote for every contract token, in batches of 50.

    SmartAPI accepts at most 50 exchange tokens per request and rate-limits
    the rest, so the batches are spaced by 0.35s. The broker call is blocking,
    so it runs in a worker thread and the event loop keeps serving the UI.
    """
    token_to_quote: dict[str, Any] = {}
    errors: list[str] = []
    if not keys:
        return token_to_quote, errors

    try:
        call = await asyncio.to_thread(angel_call, user)
    except Exception as error:  # noqa: BLE001 - the chain must report, not crash
        log.exception("Option chain could not open an Angel One session for %s", underlying)
        return token_to_quote, [f"session: {error}"]

    for index in range(0, len(keys), _QUOTE_BATCH):
        batch = keys[index:index + _QUOTE_BATCH]
        if index:
            await asyncio.sleep(_QUOTE_BATCH_DELAY)
        quotes = []
        for attempt in (1, 2):
            try:
                quotes = await asyncio.to_thread(angel_fetch, call, batch, "all")
            except Exception as error:  # noqa: BLE001 - one bad batch must not blank the chain
                log.error("Option chain quote batch %s attempt %s failed for %s: %s", index // _QUOTE_BATCH + 1, attempt, underlying, error)
                errors.append(f"batch {index // _QUOTE_BATCH + 1}: {error}")
                quotes = []
                if attempt == 1:
                    await asyncio.sleep(_QUOTE_BATCH_DELAY)
                    continue
                break
            if not quotes:
                log.error("Option chain quote batch %s attempt %s returned empty data for %s", index // _QUOTE_BATCH + 1, attempt, underlying)
                if attempt == 1:
                    await asyncio.sleep(_QUOTE_BATCH_DELAY)
                    continue
            break
        for quote in quotes:
            # The cache is what the paper-trading engine prices against, so the
            # chain's quotes are stored: an order placed from this chain must
            # fill at the price the operator is looking at.
            quote_engine.store(quote)
            token_to_quote[str(quote.token)] = quote
    return token_to_quote, errors


def _find_index_for(underlying: str) -> dict[str, Any] | None:
    """The index row that carries the underlying's spot price.

    The platform calls the index "NIFTY" while the instrument master lists it
    as "NIFTY 50", so an exact match is tried first, then the known short
    names, then a prefix match.
    """
    wanted = underlying.strip().upper()
    indices = master.indices("nse_cm") + master.indices("bse_cm")

    def fields(index: dict[str, Any]) -> list[str]:
        return [
            str(index.get(key) or "").strip().upper()
            for key in ("symbol", "trading_symbol", "name")
        ]

    for index in indices:
        if wanted in fields(index):
            return index
    for alias in _SPOT_ALIASES.get(wanted, ()):
        for index in indices:
            if alias in fields(index):
                return index
    for index in indices:
        for value in fields(index):
            if value and (value.startswith(wanted) or wanted.startswith(value)):
                return index
    return None


@router.get("/greeks")
def greeks(
    spot: float = Query(gt=0),
    strike: float = Query(gt=0),
    time_years: float = Query(gt=0),
    iv: float = Query(gt=0, description="Annualised IV as a decimal, e.g. 0.18"),
    option_type: str = Query(default="CE", pattern="^(CE|PE)$"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Black-Scholes Greeks for a single leg — used by the payoff builder."""
    from backend.options.chain import greeks as compute_greeks

    return compute_greeks(spot, strike, time_years, iv, is_call=option_type == "CE")
