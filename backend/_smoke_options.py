"""Option chain checks: Black-Scholes, Greeks, max pain and implied volatility."""

import math

from backend.options.chain import (
    RISK_FREE_RATE, black_scholes_price, build_chain, greeks, implied_vol, max_pain, pcr, years_to_expiry,
)

CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED: {label} {detail}")
    print(f"  ok  {label}")


S, K, T, SIGMA, R = 100.0, 100.0, 1.0, 0.2, RISK_FREE_RATE
print(f"Using the module's risk-free rate r = {R}")

print("Black-Scholes: put-call parity")
call = black_scholes_price(S, K, T, R, SIGMA, True)
put = black_scholes_price(S, K, T, R, SIGMA, False)
lhs = call - put
rhs = S - K * math.exp(-R * T)
check("C - P == S - K*e^(-rT)", math.isclose(lhs, rhs, abs_tol=1e-9), f"{lhs} vs {rhs}")
check("ATM call is worth ~11.26 at r=6.5%", math.isclose(call, 11.2639, abs_tol=0.01), str(round(call, 4)))
check("ATM put follows from parity", math.isclose(put, call - (S - K * math.exp(-R * T)), abs_tol=1e-9),
      str(round(put, 4)))
# The prices are only correct if parity holds, which is the real invariant.
check("Call equals put + forward intrinsic", math.isclose(call - put, S - K * math.exp(-R * T), abs_tol=1e-9))

print("Greeks")
c = greeks(S, K, T, SIGMA, is_call=True, rate=R)
p = greeks(S, K, T, SIGMA, is_call=False, rate=R)
d1 = (math.log(S / K) + (R + 0.5 * SIGMA * SIGMA) * T) / (SIGMA * math.sqrt(T))
expected_delta = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
check("ATM call delta matches N(d1)", math.isclose(c["delta"], round(expected_delta, 6), abs_tol=1e-6),
      f"got {c['delta']} expected {expected_delta}")
check("ATM put delta is call delta - 1", math.isclose(p["delta"], round(c["delta"] - 1, 6), abs_tol=1e-6))
check("Call delta + |put delta| = 1", math.isclose(c["delta"] - p["delta"], 1.0, abs_tol=1e-9))
check("Call and put gamma are equal", math.isclose(c["gamma"], p["gamma"], rel_tol=1e-12))
check("Call and put vega are equal", math.isclose(c["vega"], p["vega"], rel_tol=1e-12))
check("Theta is negative for a long option", c["theta"] < 0 and p["theta"] < 0)
check("Delta is bounded 0..1 for a call", 0 <= c["delta"] <= 1)
deep_itm = greeks(200.0, 100.0, 1.0, 0.2, is_call=True, rate=R)
check("Deep ITM call delta -> 1", deep_itm["delta"] > 0.99, str(deep_itm["delta"]))
deep_otm = greeks(100.0, 200.0, 1.0, 0.2, is_call=True, rate=R)
check("Deep OTM call delta -> 0", deep_otm["delta"] < 0.01, str(deep_otm["delta"]))

print("Degenerate inputs are handled, not crashed")
bad = greeks(100.0, 100.0, 0.0, 0.2, is_call=True, rate=R)
check("Zero time to expiry yields None Greeks", bad["delta"] is None)
check("Zero vol yields None Greeks", greeks(100.0, 100.0, 1.0, 0.0, is_call=True, rate=R)["gamma"] is None)
check("Negative sigma yields None Greeks", greeks(100.0, 100.0, 1.0, -0.2, is_call=True, rate=R)["delta"] is None)

print("Implied volatility round-trip")
recovered = implied_vol(call, S, K, T, is_call=True)
check("IV round-trips to 0.20", math.isclose(recovered, SIGMA, abs_tol=1e-4), str(recovered))
recovered_put = implied_vol(put, S, K, T, is_call=False)
check("Put IV round-trips to 0.20", math.isclose(recovered_put, SIGMA, abs_tol=1e-4), str(recovered_put))
check("IV below intrinsic returns None", implied_vol(0.01, 200.0, 100.0, 1.0, is_call=True) is None)
check("Non-positive price returns None", implied_vol(0.0, 100.0, 100.0, 1.0, is_call=True) is None)

print("Max pain")
# A textbook chain: puts stacked at 100 and calls at 110 -> max pain at 105.
strikes = [100.0, 105.0, 110.0]
call_oi = [0.0, 0.0, 1000.0]
put_oi = [1000.0, 0.0, 0.0]
mp = max_pain(strikes, call_oi, put_oi)
check("Max pain is 100 for calls above / puts below", mp == 100.0, str(mp))
check("Max pain returns None on mismatched lengths", max_pain([1.0], [1.0, 2.0], [1.0]) is None)
check("Max pain returns None on empty input", max_pain([], [], []) is None)

print("PCR")
check("PCR of 1000 puts / 500 calls = 2.0", pcr(500, 1000) == 2.0)
check("PCR with no call OI is None", pcr(0, 1000) is None)

print("Expiry maths")
check("Past expiry floors at zero", years_to_expiry("2020-01-01", 1_700_000_000) == 0.0)
check("Bad expiry returns None", years_to_expiry("not-a-date", 1_700_000_000) is None)
check("Future expiry is positive", years_to_expiry("2030-01-01", 1_700_000_000) > 0)


class FakeQuote:
    def __init__(self, last, oi, iv):
        self.last, self.open_interest, self.implied_volatility = last, oi, iv
        self.volume, self.change_percent = 0.0, 0.0
        self.bid, self.ask = None, None


def lookup(token, segment):
    oi = 1000 if str(token).endswith("C") else 2000
    iv = 18.0 if str(token).endswith("C") else 20.0
    price = 100.0 if str(token).endswith("C") else 90.0
    return FakeQuote(price, oi, iv)


contracts = {
    "expiries": ["2030-01-31"],
    "rows": {
        90.0: {"strike": 90.0, "expiry": "2030-01-31", "exchange_segment": "nse_fo",
               "call": {"token": "1C", "trading_symbol": "X90CE"}, "put": {"token": "1P", "trading_symbol": "X90PE"}},
        100.0: {"strike": 100.0, "expiry": "2030-01-31", "exchange_segment": "nse_fo",
                "call": {"token": "2C", "trading_symbol": "X100CE"}, "put": {"token": "2P", "trading_symbol": "X100PE"}},
        110.0: {"strike": 110.0, "expiry": "2030-01-31", "exchange_segment": "nse_fo",
                "call": {"token": "3C", "trading_symbol": "X110CE"}, "put": {"token": "3P", "trading_symbol": "X110PE"}},
    },
}
chain = build_chain(contracts, spot=100.0, quote_lookup=lookup, now_ts=1_700_000_000)
print("Chain assembly")
check("Chain reports data available", chain["has_data"])
check("Chain has 3 strikes", len(chain["rows"]) == 3)
check("Chain computes total call OI", chain["summary"]["total_call_oi"] == 3000, str(chain["summary"]["total_call_oi"]))
check("Chain computes total put OI", chain["summary"]["total_put_oi"] == 6000)
check("Chain PCR is put-heavy", chain["summary"]["pcr"] > 1.9, str(chain["summary"]["pcr"]))
check("ATM strike is 100", chain["summary"]["atm_strike"] == 100.0)
check("Greeks are attached", chain["rows"][1]["call"]["gamma"] is not None)
check("Support/resistance identified", len(chain["summary"]["support"]) > 0)
check("Bias reads as put writing", chain["summary"]["oi_change_bias"] == "put writing")

empty = build_chain({"expiries": [], "rows": {}}, spot=None, quote_lookup=lambda *_: None, now_ts=0)
check("Empty chain reports no data honestly", empty["has_data"] is False and "sync" in empty["note"])

print(f"\nAll {CHECKS} option chain checks passed.")
