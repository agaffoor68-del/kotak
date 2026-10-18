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
from backend.options.chain import build_chain

log = logging.getLogger("alphatrade.api.options")

router = APIRouter(prefix="/api/v1/options", tags=["options"])

#: How many contracts are priced per batch before the next SmartAPI call.
_QUOTE_BATCH = 20


@router.get("/expiries")
def expiry_dates(
    underlying: str = Query(description="Underlying symbol, e.g. NIFTY"),
    exchange_segment: str = Query(default="nse_fo"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Available expiry dates for an underlying, ascending (`getExpiryDate`).

    `near` is the first expiry that has not passed — the series the chain shows
    by default. An empty list means the instrument master has no contracts for
    the underlying, which the UI reports as "run a sync" rather than as an error.
    """
    expiries = master.option_expiries(underlying, exchange_segment)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    near = next((date for date in expiries if date >= today), None)
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
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Full option chain with OI, IV, Greeks, PCR, max pain and support/resistance.

    Contract metadata (strikes, expiries, tokens) comes from Kotak's instrument
    master. Live prices (LTP, OI, IV) come from Angel One SmartAPI — Kotak and
    Angel use different token namespaces, so Kotak tokens are mapped to Angel
    tokens by trading_symbol before quoting.
    """
    contracts = master.option_contracts(underlying, exchange_segment)
    if not contracts["rows"]:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No option contracts for '{underlying}' in the symbol master. Run a scrip-master sync.",
        )

    # --- Map Kotak contract tokens -> Angel One tokens via trading_symbol ---
    kotak_to_angel_token: dict[str, str] = {}
    for strikes in contracts["rows"].values():
        for entry in strikes.values():
            for leg in ("call", "put"):
                leg_entry = entry.get(leg) or {}
                token = leg_entry.get("token")
                sym = leg_entry.get("trading_symbol")
                if token and sym:
                    kotak_to_angel_token.setdefault(str(token), sym)

    # Resolve Angel One tokens for each trading_symbol (source='angel' rows).
    angel_token_by_symbol: dict[str, str] = {}
    if kotak_to_angel_token:
        symbols = list(set(kotak_to_angel_token.values()))
        placeholders = ",".join("?" for _ in symbols)
        with app_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT trading_symbol, token FROM instruments
                WHERE trading_symbol IN ({placeholders})
                  AND exchange_segment = ? AND source = 'angel'
                """,
                (*symbols, exchange_segment),
            )
            for row in cursor.fetchall():
                angel_token_by_symbol[row["trading_symbol"]] = str(row["token"])

    kotak_to_angel_token = {
        kt: angel_token_by_symbol[sym]
        for kt, sym in kotak_to_angel_token.items()
        if sym in angel_token_by_symbol
    }

    # --- Fetch live prices from Angel One SmartAPI ---
    fetch_error = None
    quotes_by_angel_token: dict[str, Any] = {}
    if kotak_to_angel_token:
        angel_keys = list({(t, exchange_segment) for t in kotak_to_angel_token.values()})
        call = angel_call(user)
        try:
            angel_quotes_list = angel_fetch(call, angel_keys, quote_type="all")
            for q in angel_quotes_list:
                quotes_by_angel_token[q.token] = q
        except Exception as error:  # noqa: BLE001 - chain still useful without prices
            fetch_error = str(error)

    def quote_lookup(token: str, segment: str):
        angel_token = kotak_to_angel_token.get(str(token))
        if not angel_token:
            return None
        return quotes_by_angel_token.get(angel_token)

    # Spot from the underlying index, so the Greeks use a real underlying price.
    underlying_record = _find_index_for(underlying)
    spot_quote = None
    if underlying_record:
        spot_quote = quote_engine.cached(underlying_record["token"], underlying_record["exchange_segment"])
    spot = spot_quote.last if spot_quote and spot_quote.last else None

    chain = build_chain(contracts, spot=spot, quote_lookup=quote_lookup, now_ts=time.time())
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
