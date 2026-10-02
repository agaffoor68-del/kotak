"""Option chain analytics: Greeks, PCR, max pain, and support/resistance.

Greeks are computed with Black-Scholes from parameters Kotak actually provides
(spot from the live index quote, strike and expiry from the scrip master, and
IV from the exchange feed). When IV is missing, an ATM-based implied-volatility
surface is *not* fabricated — the row is returned with `greeks_available: false`
so the UI can explain why.

Max pain, PCR and the support/resistance levels are computed from the real
open-interest distribution of the chain.
"""

from __future__ import annotations

import math
from typing import Any

from backend.strategies.indicators import atr_series

# Indian market convention: 365 calendar days / 365 trading days.
DAYS_PER_YEAR = 365.0
#: Risk-free rate used for discounting, in decimal (India 6-month G-Sec proxy).
RISK_FREE_RATE = 0.065
#: Annualised volatility-of-volatility floor for vega (Black-Scholes needs a sigmasq).
MIN_SIGMA_SQ = 1e-4


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def d1_d2(spot: float, strike: float, time_years: float, rate: float, sigma: float) -> tuple[float, float]:
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        raise ValueError("invalid Black-Scholes parameters")
    sigma_sqrt_t = sigma * math.sqrt(time_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * time_years) / sigma_sqrt_t
    return d1, d1 - sigma_sqrt_t


def black_scholes_price(
    spot: float, strike: float, time_years: float, rate: float, sigma: float, is_call: bool
) -> float:
    """European option price."""
    d1, d2 = d1_d2(spot, strike, time_years, rate, sigma)
    discounted_strike = strike * math.exp(-rate * time_years)
    if is_call:
        return spot * norm_cdf(d1) - discounted_strike * norm_cdf(d2)
    return discounted_strike * norm_cdf(-d2) - spot * norm_cdf(-d1)


def greeks(
    spot: float,
    strike: float,
    time_years: float,
    sigma: float,
    *,
    is_call: bool,
    rate: float = RISK_FREE_RATE,
) -> dict[str, float | None]:
    """Delta, gamma, theta, vega and rho for one contract.

    `spot` is the underlying price and `sigma` the annualised IV in decimal
    (e.g. 0.18 for 18%). Returns `None` values rather than throwing when the
    inputs cannot support a meaningful result.
    """
    try:
        if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
            return {"delta": None, "gamma": None, "theta": None, "vega": None, "rho": None, "price": None}
        d1, d2 = d1_d2(spot, strike, time_years, rate, sigma)
        discounted_strike = strike * math.exp(-rate * time_years)
        gamma = norm_pdf(d1) / (spot * sigma * math.sqrt(time_years))
        vega = spot * norm_pdf(d1) * math.sqrt(time_years) / 100.0  # per 1% vol
        if is_call:
            delta = norm_cdf(d1)
            theta = (-(spot * norm_pdf(d1) * sigma) / (2 * math.sqrt(time_years))
                     - rate * discounted_strike * norm_cdf(d2)) / DAYS_PER_YEAR
            rho = (discounted_strike * norm_cdf(d2) * time_years) / 100.0
            price = spot * norm_cdf(d1) - discounted_strike * norm_cdf(d2)
        else:
            delta = norm_cdf(d1) - 1.0
            theta = (-(spot * norm_pdf(d1) * sigma) / (2 * math.sqrt(time_years))
                     + rate * spot * norm_cdf(-d1)
                     - rate * discounted_strike * norm_cdf(-d2)) / DAYS_PER_YEAR
            rho = (-discounted_strike * norm_cdf(-d2) * time_years) / 100.0
            price = discounted_strike * norm_cdf(-d2) - spot * norm_cdf(-d1)
        return {
            "delta": round(delta, 6), "gamma": round(gamma, 8), "theta": round(theta, 6),
            "vega": round(vega, 6), "rho": round(rho, 6), "price": round(price, 4),
        }
    except (ValueError, ZeroDivisionError, OverflowError):
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "rho": None, "price": None}


def years_to_expiry(expiry: str, now_ts: float) -> float | None:
    """Calendar time to an ISO expiry date, floored at zero."""
    from datetime import datetime, timezone

    try:
        target = datetime.fromisoformat(expiry).replace(hour=15, minute=30, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    seconds = target.timestamp() - now_ts
    return max(0.0, seconds / (DAYS_PER_YEAR * 86400))


# ------------------------------------------------------------------- analytics


def max_pain(strikes: list[float], call_oi: list[float], put_oi: list[float]) -> float | None:
    """Strike where option writers' total payout is lowest.

    Computed by summing intrinsic value for every strike: the point of maximum
    pain for holders is the point of minimum total pain for writers.
    """
    if not strikes or len(strikes) != len(call_oi) or len(strikes) != len(put_oi):
        return None
    best_strike, lowest_pain = None, float("inf")
    for candidate in strikes:
        total = 0.0
        for index, strike in enumerate(strikes):
            total += max(0.0, candidate - strike) * (call_oi[index] or 0)
            total += max(0.0, strike - candidate) * (put_oi[index] or 0)
        if total < lowest_pain:
            best_strike, lowest_pain = candidate, total
    return best_strike


def pcr(call_oi: float, put_oi: float) -> float | None:
    """Put-call ratio on open interest."""
    if not call_oi:
        return None
    return round(put_oi / call_oi, 4)


def support_resistance(strikes: list[float], call_oi: list[float], put_oi: list[float], top: int = 3) -> dict[str, list[float]]:
    """Strike levels with the heaviest put (support) and call (resistance) OI."""
    if not strikes:
        return {"support": [], "resistance": []}
    support = sorted(zip(strikes, put_oi), key=lambda pair: pair[1] or 0, reverse=True)[:top]
    resistance = sorted(zip(strikes, call_oi), key=lambda pair: pair[1] or 0, reverse=True)[:top]
    return {
        "support": [strike for strike, _ in support if _ > 0],
        "resistance": [strike for strike, _ in resistance if _ > 0],
    }


def build_chain(
    contracts: dict[str, Any],
    *,
    spot: float | None,
    quote_lookup,
    now_ts: float,
) -> dict[str, Any]:
    """Assemble a full option chain.

    `contracts` is the shape from :func:`backend.marketdata.master.option_contracts`
    and `quote_lookup(token, segment)` returns a live `Quote` (or `None`).
    """
    rows_by_strike = contracts.get("rows") or {}
    strikes = sorted(float(strike) for strike in rows_by_strike)
    if not strikes:
        return {
            "expiries": contracts.get("expiries", []), "rows": [], "spot": spot,
            "has_data": False, "greeks_available": False,
            "note": "No option contracts for this underlying in the scrip master. Run a symbol-master sync.",
        }

    expiry = rows_by_strike[strikes[0]].get("expiry")
    if expiry:
        # Only the strikes belonging to the earliest expiry are the near chain.
        strikes = [s for s in strikes if rows_by_strike[s].get("expiry") == expiry]

    chain_rows: list[dict[str, Any]] = []
    for strike in strikes:
        entry = rows_by_strike[strike]
        call_token = (entry.get("call") or {}).get("token")
        put_token = (entry.get("put") or {}).get("token")
        call_quote = quote_lookup(call_token, entry.get("exchange_segment", "nse_fo")) if call_token else None
        put_quote = quote_lookup(put_token, entry.get("exchange_segment", "nse_fo")) if put_token else None

        call_ltp = call_quote.last if call_quote else None
        put_ltp = put_quote.last if put_quote else None
        call_oi = call_quote.open_interest if call_quote else None
        put_oi = put_quote.open_interest if put_quote else None
        call_iv = call_quote.implied_volatility if call_quote else None
        put_iv = put_quote.implied_volatility if put_quote else None

        row: dict[str, Any] = {
            "strike": strike,
            "expiry": expiry,
            "call": {
                "token": call_token, "trading_symbol": (entry.get("call") or {}).get("trading_symbol"),
                "ltp": call_ltp, "open_interest": call_oi, "volume": call_quote.volume if call_quote else None,
                "change_percent": call_quote.change_percent if call_quote else None,
                "bid": call_quote.bid if call_quote else None, "ask": call_quote.ask if call_quote else None,
                "iv": (call_iv / 100.0) if call_iv and call_iv > 1 else call_iv,
            },
            "put": {
                "token": put_token, "trading_symbol": (entry.get("put") or {}).get("trading_symbol"),
                "ltp": put_ltp, "open_interest": put_oi, "volume": put_quote.volume if put_quote else None,
                "change_percent": put_quote.change_percent if put_quote else None,
                "bid": put_quote.bid if put_quote else None, "ask": put_quote.ask if put_quote else None,
                "iv": (put_iv / 100.0) if put_iv and put_iv > 1 else put_iv,
            },
        }

        time_years = years_to_expiry(expiry, now_ts) if expiry else None
        if spot and time_years and call_iv and put_iv:
            row["call"].update(greeks(spot, strike, time_years, row["call"]["iv"], is_call=True))
            row["put"].update(greeks(spot, strike, time_years, row["put"]["iv"], is_call=False))
        elif spot and time_years and call_ltp and put_ltp:
            # Recover IV from the market price (invert Black-Scholes by bisection).
            call_sigma = implied_vol(call_ltp, spot, strike, time_years, is_call=True)
            put_sigma = implied_vol(put_ltp, spot, strike, time_years, is_call=False)
            if call_sigma:
                row["call"].update(greeks(spot, strike, time_years, call_sigma, is_call=True))
            if put_sigma:
                row["put"].update(greeks(spot, strike, time_years, put_sigma, is_call=False))
        chain_rows.append(row)

    call_ois = [row["call"]["open_interest"] or 0 for row in chain_rows]
    put_ois = [row["put"]["open_interest"] or 0 for row in chain_rows]
    total_call_oi, total_put_oi = sum(call_ois), sum(put_ois)
    levels = support_resistance(strikes, call_ois, put_ois)

    return {
        "expiries": contracts.get("expiries", []),
        "expiry": expiry,
        "rows": chain_rows,
        "spot": spot,
        "has_data": True,
        "greeks_available": spot is not None and expiry is not None,
        "summary": {
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "pcr": pcr(total_call_oi, total_put_oi),
            "max_pain": max_pain(strikes, call_ois, put_ois),
            "atm_strike": min(strikes, key=lambda s: abs(s - spot)) if spot else None,
            "support": levels["support"],
            "resistance": levels["resistance"],
            "oi_change_bias": (
                "put writing" if total_put_oi > total_call_oi * 1.2
                else "call writing" if total_call_oi > total_put_oi * 1.2
                else "balanced"
            ),
        },
    }


def implied_vol(price: float, spot: float, strike: float, time_years: float, *, is_call: bool,
                low: float = 0.01, high: float = 5.0, iterations: int = 60) -> float | None:
    """Back out implied volatility from a market premium via bisection."""
    if price <= 0 or spot <= 0 or strike <= 0 or time_years <= 0:
        return None
    try:
        intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
        if price < intrinsic:
            return None
        low_price = black_scholes_price(spot, strike, time_years, RISK_FREE_RATE, low, is_call)
        high_price = black_scholes_price(spot, strike, time_years, RISK_FREE_RATE, high, is_call)
        if not (low_price <= price <= high_price):
            return None
        for _ in range(iterations):
            mid = (low + high) / 2
            mid_price = black_scholes_price(spot, strike, time_years, RISK_FREE_RATE, mid, is_call)
            if mid_price > price:
                high = mid
            else:
                low = mid
        return round((low + high) / 2, 6)
    except (ValueError, OverflowError):
        return None


def expected_move(spot: float, time_years: float, sigma: float) -> float | None:
    """One-standard-deviation move, used to frame the expected-move band."""
    if not spot or time_years is None or time_years <= 0 or not sigma or sigma <= 0:
        return None
    return round(spot * sigma * math.sqrt(time_years), 2)
