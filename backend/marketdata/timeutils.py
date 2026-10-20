"""Time helpers shared by the market-data and backtest layers.

Every timestamp this platform stores is an **epoch second**, and every date a
user types is an **IST** date: "2024-01-04" means the Indian trading session of
the 4th, not the UTC day.

That single rule is what stops the downloader and the backtest query from
disagreeing about the same window. Both sides must turn "2024-01-04 .. 2024-01-10"
into the same epoch bounds, and both must agree that a *naive* timestamp coming
from a market feed is an IST wall-clock reading, not UTC.

Nothing here stores anything; it only converts.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

#: India Standard Time. Used for every date the user types and every bar.
IST = ZoneInfo("Asia/Kolkata")

#: UTC — used only when a source is explicitly UTC.
UTC = timezone.utc

#: IST is UTC+05:30.
IST_OFFSET_SECONDS = 19800

#: The Indian session opens at 09:15 IST and closes at 15:30 IST.
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


def now_ist() -> datetime:
    """The current instant, expressed in IST."""
    return datetime.now(tz=IST)


def to_ist(moment: datetime) -> datetime:
    """Convert an aware datetime to IST. Naive input is *assumed* to be IST.

    The assumption matters: market data for Indian instruments is published on
    the IST wall clock, so a naive ``2024-01-04 09:15:00`` means 09:15 IST.
    Reading it as UTC would shift every bar by five and a half hours.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def to_utc(moment: datetime) -> datetime:
    """Convert a datetime to UTC, treating naive input as IST."""
    return to_ist(moment).astimezone(UTC)


def epoch(moment: datetime) -> float:
    """Epoch seconds for a datetime. Naive input is assumed to be IST.

    The result is an absolute instant, so a value converted from UTC to IST
    keeps the same epoch — only the wall-clock reading changes.
    """
    return to_ist(moment).timestamp()


def parse_ist(value: Any) -> datetime:
    """Parse a user-supplied date/datetime into an IST-aware datetime.

    Accepts ``datetime``, ``date`` and the string shapes the UI and the API
    produce: ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM[:SS]``, ISO-8601 with or
    without an offset.
    """
    if isinstance(value, datetime):
        return to_ist(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=IST)
    if value is None:
        raise ValueError("missing date")

    text = str(value).strip()
    if not text:
        raise ValueError("missing date")

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue

    # Anything else (ISO with an offset, for instance) goes through pandas-free
    # ISO parsing; a trailing "Z" is accepted as UTC.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Unrecognised date '{text}'") from error
    return to_ist(parsed)


def day_bounds(value: Any) -> tuple[datetime, datetime]:
    """The IST start and end of the day `value` falls on.

    A date (or midnight) expands to the whole IST day, so a single-day request
    covers the full session. An explicit time of day is treated as that
    instant.
    """
    start = parse_ist(value)
    if start.time() != time.min:
        return start, start
    end = start.replace(hour=23, minute=59, second=59, microsecond=999_999)
    return start, end


def window(value: Any, end_value: Any) -> tuple[datetime, datetime]:
    """An IST-aware ``(start, end)`` window, ordered and inclusive.

    A date-only `value` expands to the whole IST day; a date-only `end_value`
    expands to 23:59:59.999999 IST of that day.
    """
    start, _ = day_bounds(value)
    _, end = day_bounds(end_value)
    if end < start:
        start, end = end, start
    return start, end


def epoch_bounds(start: datetime, end: datetime) -> tuple[float, float]:
    """Epoch bounds for an IST-aware window."""
    return to_ist(start).timestamp(), to_ist(end).timestamp()


def ist_date(moment: float | datetime) -> date:
    """The IST calendar date an epoch second falls on."""
    if isinstance(moment, datetime):
        return to_ist(moment).date()
    return datetime.fromtimestamp(float(moment), tz=IST).date()


def ist_stamp(moment: float | datetime) -> str:
    """A readable IST timestamp for logs and API payloads."""
    if isinstance(moment, datetime):
        return to_ist(moment).isoformat()
    return datetime.fromtimestamp(float(moment), tz=IST).isoformat()
