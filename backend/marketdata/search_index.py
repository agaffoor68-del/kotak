"""Fast local instrument search.

The instrument master is Angel One's scrip master (``OpenAPIScripMaster.json``),
normalised into the ``instruments`` table. Searching it with
``UPPER(symbol) LIKE '%REL%'`` scans every row on every keystroke, and a
leading wildcard can never use an index.

So the cash segments are mirrored once into an in-memory index and served from
there: a lookup is a dictionary hit plus a bisect, it never touches the
database, and it never calls the broker.

Only the NSE/BSE cash segments and the indices are indexed — that is what a
user types a symbol for, and it keeps the index to a few thousand rows. The
remaining segments stay in the table and are reachable through the database
fallback.

The index is a snapshot: readers take the current object and never block, while
a rebuild swaps in a new one.
"""

from __future__ import annotations

import bisect
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

from backend.core.database import app_cursor

log = logging.getLogger("alphatrade.search")

#: The cash segments that make up the in-memory index.
INDEXED_SEGMENTS = ("nse_cm", "bse_cm", "nse_fo", "bse_fo")

INDEX_TYPES = {"INDEX", "IDX", "FUTIDX", "OPTIDX", "AMXIDX"}
INDEX_QUERY_ALIASES = {"NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
BROAD_INDEX_EXCLUSIONS = ("ETF", "ADD", "AXIS", "GSEC")

#: A rebuild is a safety net, not the primary invalidation path: the master is
#: invalidated explicitly whenever it is re-synced.
MAX_AGE_SECONDS = 15 * 60

#: Exchange series suffixes Angel appends to a trading symbol ("RELIANCE-EQ").
SERIES_SUFFIXES = ("-EQ", "-BE", "-BM", "-BO", "-CR", "-IS")

#: The segment order used when a token could live in more than one.
SEGMENT_PREFERENCE = ("nse_cm", "bse_cm", "nse_fo", "bse_fo", "cde_fo", "mcx_fo")


def clean_symbol(value: str | None) -> str:
    """Strip the exchange series suffix: ``"RELIANCE-EQ"`` -> ``"RELIANCE"``."""
    text = (value or "").strip()
    upper = text.upper()
    for suffix in SERIES_SUFFIXES:
        if upper.endswith(suffix):
            return text[: -len(suffix)].strip()
    return text


def compact(value: str | None) -> str:
    """A comparison key with the separators removed: ``"TATA MOTORS"`` -> ``"TATAMOTORS"``.

    Brokers disagree on spacing, so the master can hold ``"TATA MOTORS"`` while
    the operator types ``"TATAMOTORS"``. The compact key makes both find the
    same instrument.
    """
    return "".join(character for character in (value or "").upper() if character.isalnum())


@dataclass(frozen=True)
class Entry:
    """One searchable instrument."""

    token: str
    exchange_segment: str
    symbol: str
    trading_symbol: str
    name: str
    is_index: int
    instrument_type: str | None
    series: str | None
    lot_size: int | None
    tick_size: float | None
    #: Upper-cased search keys, most specific first.
    keys: tuple[str, ...]
    #: Every key squashed into one string, for the substring fallback.
    haystack: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "exchange_segment": self.exchange_segment,
            "symbol": self.symbol,
            "trading_symbol": self.trading_symbol,
            "name": self.name,
            "is_index": self.is_index,
            "instrument_type": self.instrument_type,
            "series": self.series,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
        }


def _entry(row: dict[str, Any]) -> Entry | None:
    token = str(row.get("token") or "").strip()
    if not token:
        return None
    trading_symbol = str(row.get("trading_symbol") or "").strip()
    raw_symbol = str(row.get("symbol") or "").strip()
    name = str(row.get("name") or "").strip()
    # The symbol a user types is the short one: "RELIANCE", not
    # "RELIANCE-EQ" and not "RELIANCE INDUSTRIES LTD".
    symbol = clean_symbol(trading_symbol) or clean_symbol(raw_symbol) or token

    keys: list[str] = []
    for candidate in (symbol, trading_symbol, raw_symbol, name):
        key = candidate.strip().upper()
        if key and key not in keys:
            keys.append(key)
        squeezed = compact(candidate)
        if squeezed and squeezed not in keys:
            keys.append(squeezed)
    if not keys:
        return None

    return Entry(
        token=token,
        exchange_segment=str(row.get("exchange_segment") or "nse_cm"),
        symbol=symbol,
        trading_symbol=trading_symbol or symbol,
        name=name or symbol,
        is_index=int(row.get("is_index") or 0),
        instrument_type=str(row.get("instrument_type") or "").strip().upper() or None,
        series=row.get("series"),
        lot_size=row.get("lot_size"),
        tick_size=row.get("tick_size"),
        keys=tuple(keys),
        haystack=" ".join(keys),
    )


class _Index:
    """An immutable snapshot of the searchable instruments."""

    def __init__(self, entries: list[Entry]) -> None:
        self.entries = entries
        self.by_key: dict[str, list[Entry]] = {}
        for entry in entries:
            for key in entry.keys:
                self.by_key.setdefault(key, []).append(entry)
        self.sorted_keys = sorted(self.by_key)
        self.built_at = time.time()

    def prefix_keys(self, prefix: str) -> list[str]:
        """Every key that starts with `prefix`, in sorted order."""
        if not prefix:
            return []
        start = bisect.bisect_left(self.sorted_keys, prefix)
        end = bisect.bisect_right(self.sorted_keys, prefix + "￿")
        return self.sorted_keys[start:end]


_lock = threading.Lock()
_index: _Index | None = None
_stale = True


def invalidate() -> None:
    """Mark the index stale; the next search rebuilds it.

    Called whenever the instrument master is re-synced.
    """
    global _stale
    with _lock:
        _stale = True


def _build() -> _Index:
    started = time.perf_counter()
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT token, exchange_segment, symbol, trading_symbol, name, series,
                   instrument_type, is_index, lot_size, tick_size
            FROM instruments
            WHERE exchange_segment IN (?, ?)
            """,
            INDEXED_SEGMENTS,
        )
        rows = [dict(row) for row in cursor.fetchall()]
    entries = [entry for entry in (_entry(row) for row in rows) if entry is not None]
    index = _Index(entries)
    log.info(
        "Instrument search index built: %d instruments in %.0f ms",
        len(entries), (time.perf_counter() - started) * 1000,
    )
    return index


def refresh() -> int:
    """Rebuild the index now and return the number of indexed instruments."""
    global _index, _stale
    with _lock:
        index = _build()
        _index = index
        _stale = False
        return len(index.entries)


def _snapshot() -> _Index:
    """The current index, rebuilt when it is missing, stale or too old."""
    global _index, _stale
    index = _index
    if index is not None and not _stale and (time.time() - index.built_at) < MAX_AGE_SECONDS:
        return index
    with _lock:
        index = _index
        if index is not None and not _stale and (time.time() - index.built_at) < MAX_AGE_SECONDS:
            return index
        index = _build()
        _index = index
        _stale = False
        return index


def _rank(index: _Index, needle: str, limit: int) -> list[dict[str, Any]]:
    """Rank the matches: exact symbol, then symbol prefix, then a name match.

    The same company is listed on both NSE and BSE, so the results are grouped
    by symbol and the NSE listing wins; the other listing stays available in
    `listings` instead of taking a slot of its own.
    """
    compact_needle = compact(needle)
    broad_index_query = compact_needle in INDEX_QUERY_ALIASES
    scored: dict[tuple[str, str], tuple[int, Entry]] = {}

    def offer(entry: Entry, tier: int) -> None:
        # Broad index searches must never leak similarly-prefixed equities or
        # ETFs. Stock searches retain the normal EQ behaviour.
        if broad_index_query:
            searchable = f"{entry.symbol} {entry.trading_symbol} {entry.name}".upper()
            if entry.instrument_type not in INDEX_TYPES and not entry.is_index:
                return
            if any(term in searchable for term in BROAD_INDEX_EXCLUSIONS):
                return
        identity = (entry.token, entry.exchange_segment)
        current = scored.get(identity)
        if current is None or tier < current[0]:
            scored[identity] = (tier, entry)

    for key in (needle, compact_needle):
        for entry in index.by_key.get(key, ()):
            offer(entry, 0)
    for prefix in (needle, compact_needle):
        for key in index.prefix_keys(prefix):
            for entry in index.by_key[key]:
                offer(entry, 1)
    if len(scored) < limit:
        # Nothing good enough yet: fall back to a substring match on the
        # symbol and the company name.
        for entry in index.entries:
            if compact_needle and compact_needle in entry.haystack:
                offer(entry, 2)

    ranked = sorted(
        scored.values(),
        key=lambda item: (item[0], len(item[1].keys[0]), item[1].symbol, item[1].token),
    )

    grouped: dict[str, list[Entry]] = {}
    for _tier, entry in ranked:
        group_key = compact(entry.symbol)
        if entry.is_index and group_key in {"NIFTY", "NIFTY50"}:
            group_key = "NIFTY50"
        grouped.setdefault(group_key, []).append(entry)

    results: list[dict[str, Any]] = []
    for entries in grouped.values():
        preferred = min(
            entries,
            key=lambda entry: (
                0 if entry.exchange_segment == "nse_cm" else 1,
                0 if entry.exchange_segment == "bse_cm" else 1,
                0 if entry.token.startswith("999") else 1,
                entry.token,
            ),
        )
        result = preferred.to_dict()
        result["listings"] = [
            {"token": entry.token, "exchange_segment": entry.exchange_segment}
            for entry in entries
        ]
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _database_search(query: str, limit: int, exchange_segment: str | None) -> list[dict[str, Any]]:
    """The slow path, used for the segments the index does not cover."""
    from backend.marketdata import master

    rows = master.search(query, limit=limit, exchange_segment=exchange_segment)
    results: list[dict[str, Any]] = []
    for row in rows:
        entry = _entry(row)
        if entry is not None:
            results.append(entry.to_dict())
    return results


def search(
    query: str,
    *,
    limit: int = 10,
    exchange_segment: str | None = None,
) -> list[dict[str, Any]]:
    """Search the local master by symbol or company name.

    Never calls Angel One: the master is a local file and the index is a local
    structure. Returns the best matches, each with the `token` the rest of the
    platform quotes by.
    """
    needle = query.strip().upper()
    if not needle:
        return []
    limit = max(1, min(int(limit), 50))

    if exchange_segment and exchange_segment not in INDEXED_SEGMENTS:
        return _database_search(query, limit, exchange_segment)

    index = _snapshot()
    if not index.entries:
        return _database_search(query, limit, exchange_segment)
    return _rank(index, needle, limit)


def search_all(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Search every segment, derivatives included.

    This is the slower path: the index only holds the cash segments, so the
    other segments are queried in the database. Used where a caller needs an
    option contract or a future rather than a listed company.
    """
    return _database_search(query, max(1, min(int(limit), 50)), None)


def resolve(
    symbol: str,
    *,
    exchange_segment: str | None = None,
) -> dict[str, Any] | None:
    """Turn a symbol the user typed into the instrument that carries the token.

    This is the conversion the whole platform depends on: "SBIN" becomes the
    Angel One symbol token and the exchange segment it must be quoted with.
    """
    needle = symbol.strip().upper()
    if not needle:
        return None
    matches = search(needle, limit=8, exchange_segment=exchange_segment)
    if not matches:
        return None
    for match in matches:
        if match["symbol"].upper() == needle or match["trading_symbol"].upper() == needle:
            return match
    return matches[0]


def find_instrument(token: str, exchange_segment: str | None = None) -> dict[str, Any] | None:
    """Look an instrument up by token, in the local master."""
    token = str(token).strip()
    if not token:
        return None
    with app_cursor() as cursor:
        if exchange_segment:
            cursor.execute(
                "SELECT * FROM instruments WHERE token = ? AND exchange_segment = ?",
                (token, exchange_segment),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM instruments WHERE token = ?
                ORDER BY CASE exchange_segment
                    WHEN 'nse_cm' THEN 0 WHEN 'bse_cm' THEN 1
                    WHEN 'nse_fo' THEN 2 WHEN 'bse_fo' THEN 3 ELSE 4 END
                LIMIT 1
                """,
                (token,),
            )
        row = cursor.fetchone()
    if not row:
        return None
    record = dict(row)
    entry = _entry(record)
    if entry is not None:
        return {**record, **entry.to_dict()}
    return record


def status() -> dict[str, Any]:
    """Small, secret-free state block for the health endpoint."""
    index = _index
    return {
        "indexed": len(index.entries) if index else 0,
        "keys": len(index.sorted_keys) if index else 0,
        "segments": list(INDEXED_SEGMENTS),
        "built_at": index.built_at if index else None,
        "age_seconds": round(time.time() - index.built_at, 1) if index else None,
        "stale": _stale,
    }
