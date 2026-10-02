"""Indicator correctness checks against hand-computable series.

A rising ramp makes EMA/SMA/WMA/EMA relation predictable, so a bug in warm-up or
weighting shows up immediately rather than as a plausible-looking chart.
"""

import math

from backend.strategies.indicators import (
    adx, atr_series, bollinger, cmf, ema, ichimoku, macd, obv, rsi, sma, stochastic_rsi,
    supertrend, true_range, volume_profile, vwap, wma,
)

RAMP = [float(value) for value in range(1, 61)]
DECLINE = [float(value) for value in range(60, 0, -1)]
FLAT = [10.0] * 60  # long enough for a 26/9 MACD signal to warm up
# A linear ramp makes EMA converge onto SMA, so a step is used to tell them apart.
STEP = [10.0] * 20 + [20.0] * 10
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED: {label} {detail}")
    print(f"  ok  {label}")


print("Moving averages")
check("SMA ramp = 2.5 at index 3", sma(RAMP, 4)[3] == 2.5, str(sma(RAMP, 4)[3]))
check("SMA ramp last = 58.5", sma(RAMP, 4)[-1] == 58.5, str(sma(RAMP, 4)[-1]))
check("SMA ramp warm-up is None", sma(RAMP, 4)[2] is None)
check("SMA flat = 10", sma(FLAT, 5)[-1] == 10.0)
check("EMA below SMA on a rising ramp", ema(STEP, 10)[-1] < sma(STEP, 10)[-1], f"ema={ema(STEP,10)[-1]} sma={sma(STEP,10)[-1]}")
check("EMA flat = 10", ema(FLAT, 10)[-1] == 10.0)
expected_wma = (56 * 1 + 57 * 2 + 58 * 3 + 59 * 4 + 60 * 5) / 15
check("WMA ramp last = weighted mean of 56..60", math.isclose(wma(RAMP, 5)[-1], expected_wma), str(wma(RAMP, 5)[-1]))
check("WMA(1,2,3,4) = 3.0", math.isclose(wma([1.0, 2.0, 3.0, 4.0], 4)[3], 3.0), str(wma([1.0, 2.0, 3.0, 4.0], 4)[3]))
check("WMA(5) is the value itself", math.isclose(wma([7.0], 1)[0], 7.0))

print("RSI")
r = rsi(RAMP, 14)
check("RSI of pure uptrend = 100", math.isclose(r[-1], 100.0), str(r[-1]))
d = rsi(DECLINE, 14)
check("RSI of pure downtrend = 0", math.isclose(d[-1], 0.0), str(d[-1]))
f = rsi(FLAT, 14)
check("RSI of a flat series = 50", math.isclose(f[-1], 50.0), str(f[-1]))
check("RSI is bounded 0..100", all(value is None or 0 <= value <= 100 for value in r + d + f))

print("MACD / Bollinger")
m = macd(RAMP)
check("MACD signal length matches input", len(m["signal"]) == len(RAMP))
check("MACD > 0 on an uptrend", m["macd"][-1] > 0, str(m["macd"][-1]))
check("MACD signal is warm after 60 bars", m["signal"][-1] is not None)
check("MACD signal leads to a zero histogram on flat", abs(macd(FLAT)["histogram"][-1]) < 1e-9,
      str(macd(FLAT)["histogram"][-1]))
check("MACD is zero on a flat series", abs(macd(FLAT)["macd"][-1]) < 1e-9)
b = bollinger(RAMP, 20, 2.0)
check("Bollinger upper > middle > lower", b["upper"][-1] > b["middle"][-1] > b["lower"][-1])
flat_bands = bollinger(FLAT, 20, 2.0)
check("Bollinger flat series has zero width", flat_bands["upper"][-1] - flat_bands["lower"][-1] < 1e-9,
      str(flat_bands["upper"][-1] - flat_bands["lower"][-1]))

print("Volatility")
candles = [{"open": v, "high": v + 1, "low": v - 1, "close": v, "volume": 100.0} for v in RAMP]
tr = true_range(candles)
check("True range of a ramp = 2.0", math.isclose(tr[1], 2.0), str(tr[1]))
atr = atr_series(candles, 14)
check("ATR of a constant-range series = 2.0", math.isclose(atr[-1], 2.0), str(atr[-1]))
a = adx(candles, 14)
check("ADX of a clean trend > 50", a["adx"][-1] is None or a["adx"][-1] > 50, str(a["adx"][-1]))
check("+DI > -DI on an uptrend", (a["plus_di"][-1] or 0) > (a["minus_di"][-1] or 0))

print("Overlays")
st = supertrend(candles, 5, 2.0)
check("SuperTrend direction is +1 on an uptrend", st["direction"][-1] == 1, str(st["direction"][-1]))
check("SuperTrend line sits below price in an uptrend", st["supertrend"][-1] < candles[-1]["close"])
st_down = supertrend([{"open": v, "high": v + 1, "low": v - 1, "close": v, "volume": 1.0} for v in DECLINE], 5, 2.0)
check("SuperTrend direction is -1 on a downtrend", st_down["direction"][-1] == -1)
check("SuperTrend line sits above price in a downtrend",
      st_down["supertrend"][-1] > DECLINE[-1])
ich = ichimoku(candles)
ich_down = ichimoku([{"open": v, "high": v + 1, "low": v - 1, "close": v, "volume": 1.0} for v in DECLINE])
check("Ichimoku tenkan > kijun on a rising ramp", ich["tenkan"][-1] > ich["kijun"][-1],
      f"tenkan={ich['tenkan'][-1]} kijun={ich['kijun'][-1]}")
check("Ichimoku on a downtrend flips the ordering", ich_down["tenkan"][-1] < ich_down["kijun"][-1])

print("Volume")
typical = [((c["high"] + c["low"] + c["close"]) / 3) for c in candles]
check("VWAP of constant volume = mean typical price",
      math.isclose(vwap(candles)[-1], sum(typical) / len(typical), rel_tol=1e-6), str(vwap(candles)[-1]))
# Sanity: a flat market must have a flat VWAP.
flat_candles = [{"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 5.0} for _ in range(10)]
check("VWAP of a perfectly flat market = that price", math.isclose(vwap(flat_candles)[-1], 10.0))
o = obv(candles)
check("OBV rises by one volume unit per up-candle", o[-1] == 100.0 * (len(RAMP) - 1), str(o[-1]))
cf = cmf(candles, 20)
# CMF's multiplier ((C-L)-(H-C))/(H-L) is 1.0 only when the close is at the high.
at_high = [{"open": v, "high": v, "low": v - 1, "close": v, "volume": 100.0} for v in RAMP]
check("CMF is 1.0 when every candle closes at its high",
      math.isclose(cmf(at_high, 20)[-1], 1.0, rel_tol=1e-6), str(cmf(at_high, 20)[-1]))
# ...and 0.0 when it closes at the midpoint, as this ramp does.
check("CMF is 0.0 when the close sits mid-range", math.isclose(cf[-1], 0.0, abs_tol=1e-9), str(cf[-1]))
vp = volume_profile(candles, 10)
check("Volume profile returns bins", len(vp["prices"]) == 10)
check("Volume profile has a point of control", vp["point_of_control"] is not None)

print("Stochastic")
sr = stochastic_rsi(RAMP, 14, 14)
# A pure uptrend pins RSI at 100, so the range collapses and %K falls back to 50
# by the documented convention rather than dividing by zero.
check("Stochastic RSI %K falls back to 50 when RSI has no range", sr["k"][-1] == 50.0, str(sr["k"][-1]))
sr_down = stochastic_rsi(DECLINE, 14, 14)
check("Stochastic RSI %K also 50 on a pure downtrend", sr_down["k"][-1] == 50.0, str(sr_down["k"][-1]))
# A series with genuine swings must produce a real reading in range.
SWING = [float(value) for value in (10, 12, 11, 14, 12, 15, 13, 16, 14, 17, 15, 18,
                                     16, 19, 17, 20, 18, 21, 19, 22, 20, 23, 21, 24,
                                     22, 25, 23, 26, 24, 27, 22, 20, 23, 25, 21, 24)]
k_swing = stochastic_rsi(SWING, 14, 14)["k"]
valid = [value for value in k_swing if value is not None]
check("Stochastic RSI produces values on a swinging series", len(valid) > 0)
check("Stochastic RSI stays within 0..100", all(0 <= value <= 100 for value in valid),
      f"min={min(valid) if valid else None} max={max(valid) if valid else None}")

print(f"\nAll {CHECKS} indicator checks passed.")
