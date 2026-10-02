"""Technical indicators.

Every function takes plain Python sequences of numbers and returns a list of the
same length with `None` for the warm-up period, so a series can be zipped with
its candles without offset bookkeeping.

Implementations follow the standard definitions (Wilder for RSI/ATR/ADX, standard
Bollinger for the bands) and are written to be read and checked, not imported
from a package — the maths is short and auditable.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

Number = float | None
Series = list[Number]

Candle = dict[str, float | None]
Candles = list[Candle]


def _clean(values: Sequence[float | None]) -> list[float]:
    """Drop `None` so an indicator can be computed, then re-padded by the caller."""
    return [float(v) for v in values if v is not None and not math.isnan(v)]


def _pad(series: Sequence[Number], length: int) -> Series:
    """Left-pad a series with `None` until it is exactly `length` long."""
    out = [None] * max(0, length - len(series))
    out.extend(series)
    return out


def _dense(values: Sequence[Number]) -> tuple[int, list[float]]:
    """Return `(offset, dense_values)` for a series that may contain `None`.

    A leading `None` run is a warm-up period: it is dropped and the offset
    records where the real data starts, so results can be re-aligned to the
    original indices. Interior `None`s are not silently reordered — they end the
    usable window rather than shifting values into the wrong bars.
    """
    offset = 0
    for value in values:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            offset += 1
        else:
            break
    tail = list(values[offset:])
    # Only a contiguous, fully-numeric tail may be smoothed.
    if any(value is None or (isinstance(value, float) and math.isnan(value)) for value in tail):
        return offset, []
    return offset, [float(value) for value in tail]


def _sma_dense(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return []
    total = sum(values[:period])
    out = [total / period]
    for index in range(period, len(values)):
        total += values[index] - values[index - period]
        out.append(total / period)
    return out


def _ema_dense(values: Sequence[float], period: int) -> list[float]:
    """EMA seeded with the first `period` SMA, as is conventional."""
    if len(values) < period or period <= 0:
        return []
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    out = [current]
    for value in values[period:]:
        current = (value - current) * multiplier + current
        out.append(current)
    return out


# ------------------------------------------------------------------ trend


def sma(values: Sequence[Number], period: int) -> Series:
    """Simple moving average, aligned to the input even with a warm-up gap."""
    offset, data = _dense(values)
    if not data:
        return [None] * len(values)
    return _pad(_sma_dense(data, period), len(values))


def ema(values: Sequence[Number], period: int) -> Series:
    """Exponential moving average, aligned to the input even with a warm-up gap."""
    offset, data = _dense(values)
    if not data:
        return [None] * len(values)
    return _pad(_ema_dense(data, period), len(values))


def wma(values: Sequence[Number], period: int) -> Series:
    """Weighted moving average (linearly weighted, most recent heaviest)."""
    if period <= 0:
        raise ValueError("period must be positive")
    offset, data = _dense(values)
    if not data or len(data) < period:
        return [None] * len(values)
    denominator = period * (period + 1) / 2
    out = [
        sum((offset_index + 1) * value for offset_index, value in enumerate(data[index - period + 1: index + 1]))
        / denominator
        for index in range(period - 1, len(data))
    ]
    return _pad(out, len(values))


def vwap(candles: Candles) -> Series:
    """Session VWAP. Falls back to a running mean when volume is unavailable."""
    cumulative_pv = 0.0
    cumulative_volume = 0.0
    out: Series = []
    for candle in candles:
        close = candle.get("close")
        if close is None:
            out.append(out[-1] if out else None)
            continue
        volume = candle.get("volume") or 0
        # A genuine low of 0 is valid data, so `or` must not be used to test for
        # a missing high/low — only an actual `None` falls back to the close.
        high = candle.get("high") if candle.get("high") is not None else close
        low = candle.get("low") if candle.get("low") is not None else close
        typical = (high + low + close) / 3
        cumulative_pv += typical * volume
        cumulative_volume += volume
        out.append(cumulative_pv / cumulative_volume if cumulative_volume else typical)
    return out


def supertrend(candles: Candles, period: int = 10, multiplier: float = 3.0) -> dict[str, Series]:
    """SuperTrend: ATR-banded trend with a flip when the band is crossed."""
    highs = [candle.get("high") for candle in candles]
    lows = [candle.get("low") for candle in candles]
    closes = [candle.get("close") for candle in candles]
    atr = atr_series(candles, period)

    upper: list[Number] = []
    lower: list[Number] = []
    direction: list[Number] = []
    final_upper: float | None = None
    final_lower: float | None = None
    current_trend = 1

    for index in range(len(candles)):
        current_atr = atr[index]
        close = closes[index]
        if current_atr is None or close is None:
            upper.append(None)
            lower.append(None)
            direction.append(None)
            continue
        midpoint = (highs[index] + lows[index]) / 2
        basic_upper = midpoint + multiplier * current_atr
        basic_lower = midpoint - multiplier * current_atr

        previous_close = closes[index - 1] if index > 0 else close
        # Bands only tighten while price stays inside the prior band.
        if final_upper is None or basic_upper < final_upper or (previous_close is not None and previous_close > final_upper):
            final_upper = basic_upper
        if final_lower is None or basic_lower > final_lower or (previous_close is not None and previous_close < final_lower):
            final_lower = basic_lower

        if close > final_upper:
            current_trend = 1
        elif close < final_lower:
            current_trend = -1

        upper.append(final_upper)
        lower.append(final_lower)
        direction.append(current_trend)

    # The SuperTrend line is the active band: below the market in an uptrend,
    # above it in a downtrend. This is what a chart plots.
    line: list[Number] = [
        (lower[i] if direction[i] == 1 else upper[i]) if direction[i] is not None else None
        for i in range(len(candles))
    ]
    return {"supertrend": line, "upper": upper, "lower": lower, "direction": direction}


# ------------------------------------------------------------------ momentum


def rsi(values: Sequence[Number], period: int = 14) -> Series:
    """Wilder's RSI."""
    data = _clean(values)
    if len(data) <= period:
        return [None] * len(values)

    gains = [max(0.0, data[i] - data[i - 1]) for i in range(1, len(data))]
    losses = [max(0.0, data[i - 1] - data[i]) for i in range(1, len(data))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    out: list[Number] = [None] * period  # the first `period` changes have no average

    for index in range(period, len(gains)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period
        if average_loss == 0:
            out.append(100.0 if average_gain > 0 else 50.0)
        else:
            rs = average_gain / average_loss
            out.append(100 - (100 / (1 + rs)))

    return _pad(out, len(values))


def macd(values: Sequence[Number], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, Series]:
    """MACD line, signal line and histogram."""
    fast_series = ema(values, fast)
    slow_series = ema(values, slow)
    line = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_series, slow_series)
    ]
    signal_line = ema(line, signal)
    signal_values: list[Number] = [
        sig if value is not None and sig is not None else None
        for value, sig in zip(line, signal_line)
    ]
    histogram = [
        (value - sig) if value is not None and sig is not None else None
        for value, sig in zip(line, signal_values)
    ]
    return {"macd": line, "signal": signal_values, "histogram": histogram}


def stochastic_rsi(values: Sequence[Number], rsi_period: int = 14, stoch_period: int = 14,
                   k_smooth: int = 3, d_smooth: int = 3) -> dict[str, Series]:
    """Stochastic RSI with the conventional %K / %D smoothing."""
    rsi_values = rsi(values, rsi_period)
    raw_k: list[Number] = [None] * len(values)
    for index in range(len(rsi_values)):
        if index < stoch_period - 1:
            continue
        window = [value for value in rsi_values[index - stoch_period + 1: index + 1] if value is not None]
        if len(window) < stoch_period:
            continue
        lowest, highest = min(window), max(window)
        current = rsi_values[index]
        raw_k[index] = 50.0 if highest == lowest else (current - lowest) / (highest - lowest) * 100

    k = _smooth(raw_k, k_smooth)
    d = _smooth(k, d_smooth)
    return {"k": k, "d": d}


def _smooth(values: Sequence[Number], period: int) -> Series:
    """SMA over a series that contains `None` warm-up values."""
    out: list[Number] = [None] * len(values)
    for index in range(len(values)):
        if index < period - 1:
            continue
        window = values[index - period + 1: index + 1]
        if any(value is None for value in window):
            continue
        out[index] = sum(window) / period
    return out


def stochastic(candles: Candles, period: int = 14, smooth: int = 3) -> dict[str, Series]:
    """Classic stochastic oscillator on the candle range."""
    k: list[Number] = [None] * len(candles)
    for index in range(len(candles)):
        if index < period - 1:
            continue
        window = candles[index - period + 1: index + 1]
        highs = [candle.get("high") for candle in window]
        lows = [candle.get("low") for candle in window]
        closes = [candle.get("close") for candle in window]
        if any(value is None for value in (*highs, *lows, *closes)):
            continue
        highest, lowest = max(highs), min(lows)
        close = closes[-1]
        k[index] = 50.0 if highest == lowest else (close - lowest) / (highest - lowest) * 100
    d = _smooth(k, smooth)
    return {"k": k, "d": d}


# ------------------------------------------------------------------ volatility


def bollinger(values: Sequence[Number], period: int = 20, deviations: float = 2.0) -> dict[str, Series]:
    """Bollinger Bands with a population-standard-deviation basis."""
    middle = sma(values, period)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    data = values
    for index in range(len(values)):
        if index < period - 1:
            continue
        window = [v for v in data[index - period + 1: index + 1] if v is not None]
        if len(window) < period:
            continue
        mean = middle[index]
        if mean is None:
            continue
        variance = sum((value - mean) ** 2 for value in window) / period
        spread = deviations * math.sqrt(variance)
        upper[index] = mean + spread
        lower[index] = mean - spread
    return {"upper": upper, "middle": middle, "lower": lower}


def true_range(candles: Candles) -> list[Number]:
    out: list[Number] = []
    for index, candle in enumerate(candles):
        high, low = candle.get("high"), candle.get("low")
        if high is None or low is None:
            out.append(None)
            continue
        if index == 0 or candles[index - 1].get("close") is None:
            out.append(high - low)
        else:
            previous_close = candles[index - 1]["close"]
            out.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return out


def atr_series(candles: Candles, period: int = 14) -> Series:
    """Average True Range using Wilder smoothing."""
    ranges = true_range(candles)
    if len(ranges) <= period:
        return [None] * len(candles)
    out: list[Number] = [None] * period
    window = [value for value in ranges[:period] if value is not None]
    if len(window) < period:
        return [None] * len(candles)
    current = sum(window) / period
    out.append(current)
    for index in range(period, len(ranges)):
        value = ranges[index]
        if value is None:
            out.append(None)
            continue
        current = (current * (period - 1) + value) / period
        out.append(current)
    return out


def adx(candles: Candles, period: int = 14) -> dict[str, Series]:
    """Average Directional Index with +DI / -DI."""
    length = len(candles)
    if length < period * 2:
        empty: Series = [None] * length
        return {"adx": empty, "plus_di": empty, "minus_di": empty}

    plus_dm: list[Number] = [None] * length
    minus_dm: list[Number] = [None] * length
    for index in range(1, length):
        high, low = candles[index].get("high"), candles[index].get("low")
        previous_high, previous_low = candles[index - 1].get("high"), candles[index - 1].get("low")
        if None in (high, low, previous_high, previous_low):
            continue
        up_move = high - previous_high
        down_move = previous_low - low
        plus_dm[index] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[index] = down_move if (down_move > up_move and down_move > 0) else 0.0

    ranges = true_range(candles)
    smoothed_tr: list[Number] = [None] * length
    smoothed_plus: list[Number] = [None] * length
    smoothed_minus: list[Number] = [None] * length

    tr = sum(value for value in ranges[1:period + 1] if value is not None)
    plus = sum(value for value in plus_dm[1:period + 1] if value is not None)
    minus = sum(value for value in minus_dm[1:period + 1] if value is not None)
    smoothed_tr[period] = tr
    smoothed_plus[period] = plus
    smoothed_minus[period] = minus

    for index in range(period + 1, length):
        previous_tr = smoothed_tr[index - 1] or 0.0
        previous_plus = smoothed_plus[index - 1] or 0.0
        previous_minus = smoothed_minus[index - 1] or 0.0
        current_tr = ranges[index] or 0.0
        current_plus = plus_dm[index] or 0.0
        current_minus = minus_dm[index] or 0.0
        smoothed_tr[index] = previous_tr - previous_tr / period + current_tr
        smoothed_plus[index] = previous_plus - previous_plus / period + current_plus
        smoothed_minus[index] = previous_minus - previous_minus / period + current_minus

    plus_di: list[Number] = [None] * length
    minus_di: list[Number] = [None] * length
    dx: list[Number] = [None] * length
    for index in range(length):
        tr = smoothed_tr[index]
        if not tr:
            continue
        plus_di[index] = 100 * (smoothed_plus[index] or 0) / tr
        minus_di[index] = 100 * (smoothed_minus[index] or 0) / tr
        total = plus_di[index] + minus_di[index]
        dx[index] = 100 * abs(plus_di[index] - minus_di[index]) / total if total else 0.0

    adx_values = _smooth(dx, period)
    return {"adx": adx_values, "plus_di": plus_di, "minus_di": minus_di}


# ------------------------------------------------------------------ trend overlays


def ichimoku(candles: Candles, conversion: int = 9, base: int = 26, span_b: int = 52) -> dict[str, list[Number]]:
    """Ichimoku Kinko Hyo: tenkan, kijun, senkou A/B and chikou."""
    length = len(candles)
    empty: list[Number] = [None] * length

    def midpoint(period: int, offset: int = 0) -> list[Number]:
        out: list[Number] = []
        for index in range(length):
            if index + offset < 0 or index < period - 1:
                out.append(None)
                continue
            window = candles[index - period + 1: index + 1]
            highs = [candle.get("high") for candle in window]
            lows = [candle.get("low") for candle in window]
            if any(value is None for value in (*highs, *lows)):
                out.append(None)
            else:
                out.append((max(highs) + min(lows)) / 2)
        return out

    tenkan = midpoint(conversion)
    kijun = midpoint(base)
    # Senkou spans are plotted `base` bars back.
    senkou_a = [
        (t + k) / 2 if t is not None and k is not None else None
        for t, k in zip(tenkan, kijun)
    ]
    senkou_b = midpoint(span_b)

    return {
        "tenkan": tenkan,
        "kijun": kijun,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "chikou": [candle.get("close") for candle in candles] if length else empty,
    }


# ------------------------------------------------------------------ volume


def obv(candles: Candles) -> Series:
    """On-balance volume."""
    out: list[Number] = []
    total = 0.0
    for index, candle in enumerate(candles):
        close = candle.get("close")
        volume = candle.get("volume") or 0
        if index == 0 or close is None or candles[index - 1].get("close") is None:
            out.append(total)
            continue
        change = close - candles[index - 1]["close"]
        total += volume if change > 0 else (-volume if change < 0 else 0)
        out.append(total)
    return out


def cmf(candles: Candles, period: int = 20) -> Series:
    """Chaikin Money Flow."""
    length = len(candles)
    if length < period:
        return [None] * length
    money_flow: list[Number] = [None] * length
    for index, candle in enumerate(candles):
        high, low, close, volume = candle.get("high"), candle.get("low"), candle.get("close"), candle.get("volume")
        if None in (high, low, close) or high == low:
            continue
        multiplier = ((close - low) - (high - close)) / (high - low)
        money_flow[index] = multiplier * (volume or 0)

    out: list[Number] = [None] * length
    for index in range(period - 1, length):
        window = money_flow[index - period + 1: index + 1]
        volumes = [(candles[i].get("volume") or 0) for i in range(index - period + 1, index + 1)]
        denominator = sum(volumes)
        usable = [value for value in window if value is not None]
        if denominator and len(usable) == period:
            out[index] = sum(usable) / denominator
    return out


def volume_profile(candles: Candles, bins: int = 20) -> dict[str, list[Number]]:
    """Volume distribution across price bins — a proxy for the volume profile.

    With no tick-level trade data Kotak does not publish a true volume-at-price
    ladder, so volume is distributed to bins by where each candle closed.
    """
    priced = [candle for candle in candles if candle.get("close") is not None and (candle.get("volume") or 0) > 0]
    if not priced:
        return {"prices": [], "volumes": [], "point_of_control": None, "value_area": None}
    lows = [candle["low"] if candle.get("low") is not None else candle["close"] for candle in priced]
    highs = [candle["high"] if candle.get("high") is not None else candle["close"] for candle in priced]
    lowest, highest = min(lows), max(highs)
    if highest <= lowest:
        highest = lowest + 1.0
    step = (highest - lowest) / bins

    totals = [0.0] * bins
    for candle in priced:
        close = candle.get("close")
        midpoint = close if close is not None else candle.get("low")
        index = min(bins - 1, max(0, int((midpoint - lowest) / step)))
        totals[index] += candle.get("volume") or 0

    prices = [round(lowest + step * (index + 0.5), 2) for index in range(bins)]
    poc_index = max(range(bins), key=lambda index: totals[index])
    grand_total = sum(totals) or 1.0

    # Grow outward from the point of control until 70% of volume is enclosed.
    low_index = high_index = poc_index
    accumulated = totals[poc_index]
    while accumulated / grand_total < 0.70 and (low_index > 0 or high_index < bins - 1):
        below = totals[low_index - 1] if low_index > 0 else -1
        above = totals[high_index + 1] if high_index < bins - 1 else -1
        if above >= below:
            high_index += 1
            accumulated += totals[high_index]
        else:
            low_index -= 1
            accumulated += totals[low_index]

    return {
        "prices": prices,
        "volumes": totals,
        "point_of_control": prices[poc_index],
        "value_area": [prices[low_index], prices[high_index]],
    }


# ------------------------------------------------------------------ registry

#: name -> callable. The API, strategy DSL and backtester all resolve through this.
INDICATORS: dict[str, Callable] = {
    "sma": sma,
    "ema": ema,
    "wma": wma,
    "vwap": vwap,
    "supertrend": supertrend,
    "rsi": rsi,
    "macd": macd,
    "bollinger": bollinger,
    "atr": atr_series,
    "adx": adx,
    "stochastic_rsi": stochastic_rsi,
    "stochastic": stochastic,
    "ichimoku": ichimoku,
    "obv": obv,
    "cmf": cmf,
    "volume_profile": volume_profile,
}

#: Indicators taking candles rather than a price series.
CANDLE_INDICATORS = {"vwap", "supertrend", "adx", "stochastic", "ichimoku", "obv", "cmf", "volume_profile"}


def available() -> list[dict[str, str]]:
    return [
        {
            "name": name,
            "input": "candles" if name in CANDLE_INDICATORS else "price",
            "outputs": _outputs(name),
        }
        for name in sorted(INDICATORS)
    ]


def _outputs(name: str) -> str:
    return {
        "sma": "value", "ema": "value", "wma": "value", "rsi": "value", "atr": "value", "vwap": "value",
        "macd": "macd,signal,histogram", "bollinger": "upper,middle,lower",
        "supertrend": "supertrend,upper,lower", "adx": "adx,plus_di,minus_di",
        "stochastic_rsi": "k,d", "stochastic": "k,d",
        "ichimoku": "tenkan,kijun,senkou_a,senkou_b,chikou",
        "obv": "value", "cmf": "value", "volume_profile": "prices,volumes,point_of_control,value_area",
    }.get(name, "value")
