"""Indian market session detection and market-wide breadth.

Sessions are derived from IST trading hours. Outside them the API reports the
market as closed rather than returning stale prices as if they were live.
"""

from __future__ import annotations

import time
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

#: NSE/BSE cash session, with the pre-open and post-close buffers included.
PRE_OPEN = dt_time(9, 0)
OPEN = dt_time(9, 15)
CLOSE = dt_time(15, 30)
POST_CLOSE = dt_time(16, 0)

#: Holidays are maintained here because Kotak's API does not publish a calendar
#: through the trade endpoints. Verify against the exchange circular each season.
MARKET_HOLIDAYS_2026: set[str] = {
    "2026-01-26", "2026-03-03", "2026-03-19", "2026-04-01", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-08-15", "2026-09-14", "2026-10-02",
    "2026-10-21", "2026-11-09", "2026-12-25",
}

WEEKEND = {5, 6}  # Saturday, Sunday


def now_ist() -> datetime:
    return datetime.now(IST)


def session_state(moment: datetime | None = None) -> dict[str, Any]:
    """Describe the current market session."""
    moment = moment or now_ist()
    today = moment.date()
    is_weekend = today.weekday() in WEEKEND
    is_holiday = today.isoformat() in MARKET_HOLIDAYS_2026

    if is_weekend:
        state, reason = "closed", "weekend"
    elif is_holiday:
        state, reason = "closed", "market holiday"
    else:
        clock = moment.time()
        if clock < PRE_OPEN:
            state, reason = "pre_open", "before pre-open"
        elif clock < OPEN:
            state, reason = "pre_open", "pre-open session"
        elif clock <= CLOSE:
            state, reason = "open", "normal trading session"
        elif clock <= POST_CLOSE:
            state, reason = "post_close", "post-close session"
        else:
            state, reason = "closed", "after market hours"

    return {
        "state": state,
        "is_open": state == "open",
        "reason": reason,
        "ist": moment.isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "sessions": {"pre_open": PRE_OPEN.strftime("%H:%M"), "open": OPEN.strftime("%H:%M"),
                     "close": CLOSE.strftime("%H:%M")},
    }


def is_market_open(moment: datetime | None = None) -> bool:
    return session_state(moment)["is_open"]


def seconds_until_open(moment: datetime | None = None) -> int:
    """Seconds until the next open, or 0 when already open."""
    moment = moment or now_ist()
    if session_state(moment)["is_open"]:
        return 0
    for offset in range(0, 8):
        day = moment + timedelta(days=offset)
        candidate = day.replace(hour=OPEN.hour, minute=OPEN.minute, second=0, microsecond=0)
        if session_state(candidate)["is_open"]:
            return max(0, int((candidate - moment).total_seconds()))
    return 0


# ------------------------------------------------------------------- breadth


def breadth(quotes: list[Any]) -> dict[str, Any]:
    """Advance/decline statistics across a quote set.

    `quotes` must be real `Quote` objects from the engine. Instruments with no
    price are excluded from the counts rather than counted as unchanged.
    """
    advances = declines = unchanged = 0
    up_volume = down_volume = 0.0
    limit_up = limit_down = 0
    priced = 0

    for quote in quotes:
        if quote.last is None or quote.change_percent is None:
            continue
        priced += 1
        percent = quote.change_percent
        if percent > 0.05:
            advances += 1
        elif percent < -0.05:
            declines += 1
        else:
            unchanged += 1

        if quote.upper_circuit and quote.last and quote.last >= quote.upper_circuit - 0.001:
            limit_up += 1
        if quote.lower_circuit and quote.last and quote.last <= quote.lower_circuit + 0.001:
            limit_down += 1
        volume = quote.volume or 0
        if percent >= 0:
            up_volume += volume
        else:
            down_volume += volume

    total = advances + declines
    return {
        "priced": priced,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "advance_decline_ratio": round(advances / declines, 2) if declines else (round(advances / 1, 2) if advances else 0.0),
        "up_volume": up_volume,
        "down_volume": down_volume,
        "volume_ratio": round(up_volume / down_volume, 2) if down_volume else None,
        "updated_at": time.time(),
    }


def rank(quotes: list[Any], *, limit: int = 10, reverse: bool = True) -> list[dict[str, Any]]:
    """Top movers by absolute percentage change."""
    priced = [q for q in quotes if q.last is not None and q.change_percent is not None]
    priced.sort(key=lambda q: abs(q.change_percent or 0), reverse=reverse)
    return [q.to_dict() for q in priced[:limit]]


def most_active(quotes: list[Any], limit: int = 10) -> list[dict[str, Any]]:
    active = [q for q in quotes if q.volume is not None and q.last is not None]
    active.sort(key=lambda q: q.volume or 0, reverse=True)
    return [q.to_dict() for q in active[:limit]]
