"""Option chain routes."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.api.deps import CurrentUser, broker_call, current_user, resolve_account_id
from backend.marketdata import master
from backend.marketdata.quotes import quote_engine
from backend.options.chain import build_chain

router = APIRouter(prefix="/api/v1/options", tags=["options"])


@router.get("/chain")
def option_chain(
    underlying: str = Query(description="Underlying symbol, e.g. NIFTY"),
    exchange_segment: str = Query(default="nse_fo"),
    user: CurrentUser = Depends(current_user),
) -> dict[str, Any]:
    """Full option chain with OI, IV, Greeks, PCR, max pain and support/resistance.

    Live prices are pulled from Kotak for every contract, so the chain reflects
    the real market rather than a stored snapshot.
    """
    account_id = resolve_account_id(user)
    contracts = master.option_contracts(underlying, exchange_segment)
    if not contracts["rows"]:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No option contracts for '{underlying}' in the symbol master. Run a scrip-master sync.",
        )

    # Fetch live prices for every leg in batches Kotak accepts.
    tokens = []
    for entry in contracts["rows"].values():
        for leg in ("call", "put"):
            token = (entry.get(leg) or {}).get("token")
            if token:
                tokens.append({"instrument_token": token, "exchange_segment": exchange_segment})

    call = broker_call(account_id)
    try:
        quote_engine.fetch_batched(call, tokens, quote_type="all")
    except Exception as error:  # noqa: BLE001 - the chain is still useful without prices
        fetch_error = str(error)
    else:
        fetch_error = None

    def lookup(token: str, segment: str):
        return quote_engine.cached(str(token), segment)

    # Spot from the underlying index, so the Greeks use a real underlying price.
    underlying_record = _find_index_for(underlying)
    spot_quote = None
    if underlying_record:
        spot_quote = quote_engine.cached(underlying_record["token"], underlying_record["exchange_segment"])
    spot = spot_quote.last if spot_quote and spot_quote.last else None

    chain = build_chain(contracts, spot=spot, quote_lookup=lookup, now_ts=time.time())
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
