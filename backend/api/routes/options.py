"""Option chain routes.

Market data comes from **Angel One SmartAPI**, exactly like the rest of the
market-data layer. The chain resolves each contract's live price through
:func:`backend.angel.quotes.fetch`, never through the Kotak quote engine — Kotak
tokens and Angel tokens are different namespaces, so asking Kotak for an Angel
contract returns nothing and the chain renders empty.
"""

from __future__ import annotations

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


def get_nearest_future_expiry(underlying: str, exchange_segment: str = "nse_fo") -> str | None:
    """Fetch distinct expiry dates from Angel One master, filter >= today in IST, and pick nearest future expiry."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(tz=ist).strftime("%Y-%m-%d")

    with app_cursor() as cursor:
        # Check source = 'angel' first, fallback to all sources
        cursor.execute(
            """
            SELECT DISTINCT expiry FROM instruments
            WHERE (UPPER(underlying) = UPPER(?) OR UPPER(symbol) = UPPER(?))
              AND exchange_segment = ?
              AND option_type IN ('CE', 'PE')
              AND expiry IS NOT NULL AND expiry != ''
            ORDER BY expiry ASC
            """,
            (underlying, underlying, exchange_segment),
        )
        expiries = [row["expiry"] for row in cursor.fetchall() if row["expiry"]]

    if not expiries:
        # Also check with master.option_expiries
        expiries = master.option_expiries(underlying, exchange_segment)

    future = [d for d in expiries if d >= today_ist]
    return min(future) if future else (expiries[0] if expiries else None)


@router.get("/expiries")
def expiry_dates(
    underlying: str = Query(description="Underlying symbol, e.g. NIFTY"),
    exchange_segment: str = Query(default="nse_fo"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Available expiry dates for an underlying, ascending (`getExpiryDate`)."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(tz=ist).strftime("%Y-%m-%d")

    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT expiry FROM instruments
            WHERE (UPPER(underlying) = UPPER(?) OR UPPER(symbol) = UPPER(?))
              AND exchange_segment = ?
              AND option_type IN ('CE', 'PE')
              AND expiry IS NOT NULL AND expiry != ''
            ORDER BY expiry ASC
            """,
            (underlying, underlying, exchange_segment),
        )
        expiries = [row["expiry"] for row in cursor.fetchall() if row["expiry"]]

    if not expiries:
        expiries = master.option_expiries(underlying, exchange_segment)

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
def option_chain(
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
    3. Quotes are fetched in rate-limited batches (<= 3 req/sec) to prevent HTTP 429 errors.
    """
    # 1. Dynamically select nearest future expiry if not provided
    selected_expiry = expiry or get_nearest_future_expiry(underlying, exchange_segment)

    # 2. Query option contracts directly from the instruments table
    with app_cursor() as cursor:
        query = """
            SELECT token, exchange_segment, trading_symbol, symbol, expiry, option_type,
                   strike, lot_size, tick_size, precision, source
            FROM instruments
            WHERE (UPPER(underlying) = UPPER(?) OR UPPER(symbol) = UPPER(?))
              AND exchange_segment = ?
              AND option_type IN ('CE', 'PE')
        """
        params: list[Any] = [underlying, underlying, exchange_segment]
        if selected_expiry:
            query += " AND expiry = ?"
            params.append(selected_expiry)
        query += " ORDER BY expiry ASC, strike ASC"
        cursor.execute(query, tuple(params))
        raw_contracts = [dict(row) for row in cursor.fetchall()]

    # If empty, try fallback with master
    if not raw_contracts:
        contracts_envelope = master.option_contracts(underlying, exchange_segment, expiry=selected_expiry)
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
    available_strikes = sorted(float(value) for value in target_rows)
    selected_strikes: set[float] | None = None
    if spot:
        atm = calculate_strike(spot, "ATM", "CE", interval=interval)
        nearest = min(available_strikes, key=lambda value: abs(value - atm), default=atm)
        selected_strikes = {nearest + step * interval for step in range(-offset, offset + 1)}
        target_rows = {key: value for key, value in target_rows.items() if float(key) in selected_strikes}

    # 3. Collect exact symbol tokens for all strikes (Call and Put)
    token_keys_to_fetch: list[tuple[str, str]] = []
    token_to_quote_map: dict[str, Any] = {}

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

    # 4. Fetch live quotes via Angel One SmartAPI in rate-limited batches
    fetch_error = None
    if unique_keys:
        try:
            call = angel_call(user)
            # angel_fetch already chunks into batches of <=40 and enforces 0.35s delay (max 3 req/sec)
            quotes_list = angel_fetch(call, unique_keys, quote_type="all")
            for q in quotes_list:
                token_to_quote_map[str(q.token)] = q
        except Exception as error:
            fetch_error = str(error)
            log.exception("Option chain quotes fetch error for %s", underlying)

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
    chain["fetch_error"] = fetch_error
    chain["spot_source"] = underlying_record["symbol"] if underlying_record else None
    chain["as_of"] = time.time()
    return chain


def _find_index_for(underlying: str) -> dict[str, Any] | None:
    for index in master.indices("nse_cm") + master.indices("bse_cm"):
        if index["symbol"].upper() == underlying.upper():
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
