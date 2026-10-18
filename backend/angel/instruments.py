"""Angel One instrument master, synchronised from SmartAPI's public JSON.

SmartAPI publishes one large JSON file describing every tradable instrument
across NSE/BSE/MCX/CDS — cash, futures, options and indices. It is the source
of truth for:

* ``token``        — the numeric instrument token SmartAPI expects in quotes,
* ``symbol``       — the exchange trading symbol (``RELIANCE-EQ``, ``NIFTY24OCT…``),
* ``name``         — the underlying name (``RELIANCE``, ``NIFTY``),
* ``expiry``       — ISO date, normalised from Angel's several date formats,
* ``strike``       — in rupees, normalised from Angel's paise-as-string form,
* ``lotsize`` / ``tick_size`` / ``instrumenttype``.

Everything is stored in the shared ``instruments`` table with
``source = 'angel'`` so the rest of the platform reads one shape regardless of
which broker populated it. Nothing here invents an instrument: a segment that
fails to download leaves the previous master in place.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from backend.core.config import settings
from backend.core.database import app_cursor, meta_get, meta_set, now

log = logging.getLogger("alphatrade.angel.instruments")

SOURCE = "angel"

#: Angel prefixes every trading symbol with the segment, e.g. ``NSE:RELIANCE-EQ``.
EXCHANGE_PREFIX = {
    "NSE": "nse_cm",
    "BSE": "bse_cm",
    "NFO": "nse_fo",
    "BFO": "bse_fo",
    "MCX": "mcx_fo",
    "CDS": "cde_fo",
}

#: The reverse map: the platform's segment name back to Angel's exchanger code.
SEGMENT_EXCHANGE = {value: key for key, value in EXCHANGE_PREFIX.items()}

#: Master token -> SmartAPI quote token for indices (Kotak Neo supplies
#: different tokens for index quotes than for the instrument master).
INDEX_QUOTE_TOKEN: dict[str, str] = {
    "265": "99919000",  # SENSEX (BSE)
}

SYNC_INTERVAL_SECONDS = 12 * 60 * 60

_lock = threading.RLock()
_state: dict[str, Any] = {"last_sync": None, "status": "idle", "error": None, "counts": {}}


# --------------------------------------------------------------- normalisation


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def normalise_expiry(raw: Any) -> str | None:
    """Angel writes expiry as ``04JAN2024`` or ``DDMMMYYYY``; return ISO date."""
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text.upper(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _strike(value: Any) -> float | None:
    """Angel encodes strikes in paise (``7600000`` → ``76000``)."""
    number = _as_float(value)
    if number is None:
        return None
    return number / 100.0 if abs(number) >= 1000 and float(number).is_integer() else number


def _option_type(record: dict[str, Any]) -> str | None:
    raw = str(record.get("optiontype") or "").strip().upper()
    if raw in {"CE", "PE"}:
        return raw
    symbol = str(record.get("symbol") or "").upper()
    if symbol.endswith("CE"):
        return "CE"
    if symbol.endswith("PE"):
        return "PE"
    return None


def _trading_symbol(record: dict[str, Any]) -> str:
    symbol = str(record.get("symbol") or "").strip()
    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]
    return symbol


def _segment(record: dict[str, Any]) -> str | None:
    exchange = str(record.get("exch_seg") or "").strip().upper()
    if exchange in EXCHANGE_PREFIX:
        return EXCHANGE_PREFIX[exchange]
    symbol = str(record.get("symbol") or "")
    if ":" in symbol:
        return EXCHANGE_PREFIX.get(symbol.split(":", 1)[0].upper())
    return None


def to_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map one SmartAPI scrip row onto the shared `instruments` shape."""
    token = str(record.get("token") or "").strip()
    segment = _segment(record)
    trading_symbol = _trading_symbol(record)
    if not token or not segment or not trading_symbol:
        return None

    instrument_type = str(record.get("instrumenttype") or "").strip().upper() or None
    is_index = 1 if instrument_type in {"AMXIDX", "INDEX"} else 0
    name = str(record.get("name") or trading_symbol).strip()[:180]
    return {
        "token": token,
        "exchange_segment": segment,
        "symbol": str(record.get("name") or trading_symbol).strip(),
        "trading_symbol": trading_symbol,
        "name": name,
        "series": str(record.get("series") or "").strip() or None,
        "expiry": normalise_expiry(record.get("expiry")),
        "option_type": _option_type(record),
        "strike": _strike(record.get("strike")),
        "lot_size": _as_int(record.get("lotsize")) or 1,
        "tick_size": _as_float(record.get("tick_size")) or 0.05,
        "precision": 2,
        "instrument_type": instrument_type,
        "underlying": name or trading_symbol,
        "multiplier": 1,
        "is_index": is_index,
        "updated_at": now(),
    }


# -------------------------------------------------------------------- sync


#: Known indices that Angel One's scrip master may omit but the platform needs.
KNOWN_INDICES: list[dict[str, Any]] = [
    {"token": "26000", "exchange_segment": "nse_cm", "symbol": "NIFTY", "trading_symbol": "Nifty 50", "name": "Nifty 50", "is_index": 1},
    {"token": "26009", "exchange_segment": "nse_cm", "symbol": "BANKNIFTY", "trading_symbol": "Nifty Bank", "name": "Nifty Bank", "is_index": 1},
    {"token": "26037", "exchange_segment": "nse_cm", "symbol": "FINNIFTY", "trading_symbol": "Nifty Fin Services", "name": "Nifty Fin Services", "is_index": 1},
    {"token": "26074", "exchange_segment": "nse_cm", "symbol": "MIDCPNIFTY", "trading_symbol": "NIFTY MID SELECT", "name": "NIFTY MID SELECT", "is_index": 1},
    {"token": "265", "exchange_segment": "bse_cm", "symbol": "SENSEX", "trading_symbol": "SENSEX", "name": "S&P BSE SENSEX", "is_index": 1},
]


def _download(url: str) -> list[dict[str, Any]]:
    response = requests.get(url, timeout=180)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    raise RuntimeError("SmartAPI instrument file was not a JSON array")


def _upsert(cursor, record: dict[str, Any]) -> None:
    cursor.execute(
        """
        INSERT INTO instruments (token, exchange_segment, symbol, trading_symbol, name, series,
                                 expiry, option_type, strike, lot_size, tick_size, precision,
                                 instrument_type, underlying, multiplier, is_index, source, updated_at)
        VALUES (:token,:exchange_segment,:symbol,:trading_symbol,:name,:series,:expiry,
                :option_type,:strike,:lot_size,:tick_size,:precision,:instrument_type,
                :underlying,:multiplier,:is_index,:source,:updated_at)
        ON CONFLICT(token, exchange_segment) DO UPDATE SET
            symbol=excluded.symbol, trading_symbol=excluded.trading_symbol, name=excluded.name,
            series=excluded.series, expiry=excluded.expiry, option_type=excluded.option_type,
            strike=excluded.strike, lot_size=excluded.lot_size, tick_size=excluded.tick_size,
            precision=excluded.precision, instrument_type=excluded.instrument_type,
            underlying=excluded.underlying, multiplier=excluded.multiplier,
            is_index=excluded.is_index, source=excluded.source, updated_at=excluded.updated_at
        """,
        {**record, "source": SOURCE},
    )


def sync(*, url: str | None = None, segments: Iterable[str] | None = None) -> dict[str, Any]:
    """Download the SmartAPI instrument file and persist it.

    `segments` narrows which exchange segments are stored; the default keeps
    the equity and derivatives segments the platform trades.
    """
    wanted = set(segments) if segments else set(EXCHANGE_PREFIX.values())
    with _lock:
        _state["status"] = "syncing"
        try:
            rows = _download(url or settings.angel_sync_url)
        except Exception as error:  # noqa: BLE001
            _state.update({"status": "failed", "error": str(error)})
            log.warning("Angel instrument sync failed: %s", error)
            return {"status": "failed", "error": str(error), "counts": {}}

        counts: dict[str, int] = {}
        timestamp = now()
        with app_cursor() as cursor:
            for raw in rows:
                record = to_record(raw)
                if record is None or record["exchange_segment"] not in wanted:
                    continue
                _upsert(cursor, record)
                counts[record["exchange_segment"]] = counts.get(record["exchange_segment"], 0) + 1
            meta_set(cursor, "angel_instruments_synced_at", str(timestamp))

            # Seed known indices that Angel One's scrip master may omit.
            # These are inserted with source='angel' so quote lookups find them.
            for idx in KNOWN_INDICES:
                cursor.execute(
                    "SELECT 1 FROM instruments WHERE token = ? AND exchange_segment = ? AND source = ?",
                    (idx["token"], idx["exchange_segment"], SOURCE),
                )
                if cursor.fetchone():
                    continue
                cursor.execute(
                    """
                    INSERT INTO instruments (token, exchange_segment, symbol, trading_symbol, name, series,
                                             expiry, option_type, strike, lot_size, tick_size, precision,
                                             instrument_type, underlying, multiplier, is_index, source, updated_at)
                    VALUES (?,?,?,?,?,?,NULL,NULL,NULL,1,0.05,2,'INDEX',?,1,1,?,?)
                    """,
                    (
                        idx["token"],
                        idx["exchange_segment"],
                        idx["symbol"],
                        idx["trading_symbol"],
                        idx["name"],
                        idx["symbol"],
                        idx["symbol"],
                        SOURCE,
                        timestamp,
                    ),
                )

        _state.update({"status": "ready", "last_sync": timestamp, "error": None, "counts": counts})
        log.info("Angel instrument master synced: %s", counts)
        return {"status": "ready", "synced_at": timestamp, "counts": counts, "total": sum(counts.values())}


def status() -> dict[str, Any]:
    with app_cursor() as cursor:
        synced_at = meta_get(cursor, "angel_instruments_synced_at")
        cursor.execute("SELECT COUNT(*) AS total FROM instruments WHERE source = ?", (SOURCE,))
        total = cursor.fetchone()["total"]
    return {
        "source": SOURCE,
        "status": _state["status"],
        "last_sync": float(synced_at) if synced_at else None,
        "last_error": _state["error"],
        "counts": _state["counts"],
        "instruments": total,
        "stale": bool(synced_at) and (time.time() - float(synced_at) > SYNC_INTERVAL_SECONDS * 2),
    }


def is_due(force: bool = False) -> bool:
    if force or _state["status"] in {"idle", "failed"}:
        return True
    last = _state["last_sync"]
    return last is None or (time.time() - last) > SYNC_INTERVAL_SECONDS


def bootstrap_if_empty() -> dict[str, Any]:
    """Populate the Angel master on first boot so market data is usable immediately."""
    if status()["instruments"]:
        return {"status": "skipped", "reason": "master already populated"}
    return sync()


# ------------------------------------------------------------------ queries


def _broker_symbol(token: str, exchange_segment: str) -> str | None:
    """The ``exch_seg:trading_symbol`` string SmartAPI wants in payloads."""
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT trading_symbol FROM instruments WHERE token = ? AND exchange_segment = ?",
            (str(token), exchange_segment),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    exchange = SEGMENT_EXCHANGE.get(exchange_segment, "NSE")
    return f"{exchange}:{row['trading_symbol']}"


def to_smartapi(keys: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Map (token, segment) pairs to (token, broker_symbol, segment)."""
    resolved: list[tuple[str, str, str]] = []
    for token, segment in keys:
        symbol = _broker_symbol(str(token), segment)
        if symbol:
            resolved.append((str(token), symbol, segment))
    return resolved


def equity_universe(exchange_segment: str = "nse_cm", limit: int | None = None) -> list[dict[str, Any]]:
    """Liquid cash equities for breadth/screeners, index first."""
    limit = limit or settings.breadth_universe_size
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT token, exchange_segment, symbol, trading_symbol, name, is_index
            FROM instruments
            WHERE exchange_segment = ? AND option_type IS NULL AND source = ?
            ORDER BY is_index DESC, symbol
            LIMIT ?
            """,
            (exchange_segment, SOURCE, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def expiry_list(underlying: str, exchange_segment: str = "nse_fo") -> list[str]:
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT expiry FROM instruments
            WHERE UPPER(underlying) = UPPER(?) AND exchange_segment = ?
              AND expiry IS NOT NULL AND source = ?
            ORDER BY expiry
            """,
            (underlying, exchange_segment, SOURCE),
        )
        return [row["expiry"] for row in cursor.fetchall()]
