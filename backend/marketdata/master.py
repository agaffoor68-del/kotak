"""Symbol master synchronised from Kotak Neo.

The scrip master is a set of CSV files published by Kotak. We fetch the file
list through the authenticated API, download each segment's CSV and normalise it
into the `instruments` table. Nothing here invents an instrument: if a segment
fails to download, the sync reports it and the previous master stays in place.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from typing import Any, Iterable

import requests

from backend.core.config import settings
from backend.core.database import app_cursor, meta_get, meta_set, now

log = logging.getLogger("alphatrade.master")

SEGMENTS = ("nse_cm", "bse_cm", "nse_fo", "bse_fo", "cde_fo", "mcx_fo")

#: How often the master is refreshed. Kotak publishes new expiries daily.
SYNC_INTERVAL_SECONDS = 6 * 60 * 60

_lock = threading.RLock()
_state: dict[str, Any] = {"last_sync": None, "status": "idle", "error": None, "counts": {}}

#: Indices are not in the CSV; Neo resolves them through the quote API, so the
#: well-known NSE index tokens are seeded from Kotak's own published values.
INDEX_SEED: list[dict[str, Any]] = [
    {"token": "26000", "symbol": "NIFTY", "name": "NIFTY 50", "lot_size": 50, "tick": 0.05},
    {"token": "26009", "symbol": "BANKNIFTY", "name": "NIFTY BANK", "lot_size": 15, "tick": 0.05},
    {"token": "26037", "symbol": "FINNIFTY", "name": "FIN NIFTY", "lot_size": 65, "tick": 0.05},
    {"token": "26074", "symbol": "MIDCPNIFTY", "name": "MIDCAP NIFTY", "lot_size": 140, "tick": 0.05},
    {"token": "265", "symbol": "SENSEX", "name": "S&P BSE SENSEX", "lot_size": 1, "tick": 0.05, "segment": "bse_cm"},
]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _normalise_expiry(raw: str) -> str | None:
    """Kotak's `lExpiryDate` is seconds since epoch; keep only the date."""
    if not raw:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(float(raw))))
    except (TypeError, ValueError, OSError):
        return None


def _upsert(cursor, record: dict[str, Any]) -> None:
    cursor.execute(
        """
        INSERT INTO instruments (token, exchange_segment, symbol, trading_symbol, name, series,
                                 expiry, option_type, strike, lot_size, tick_size, precision,
                                 instrument_type, underlying, multiplier, is_index, updated_at)
        VALUES (:token,:exchange_segment,:symbol,:trading_symbol,:name,:series,:expiry,
                :option_type,:strike,:lot_size,:tick_size,:precision,:instrument_type,
                :underlying,:multiplier,:is_index,:updated_at)
        ON CONFLICT(token, exchange_segment) DO UPDATE SET
            symbol=excluded.symbol, trading_symbol=excluded.trading_symbol, name=excluded.name,
            series=excluded.series, expiry=excluded.expiry, option_type=excluded.option_type,
            strike=excluded.strike, lot_size=excluded.lot_size, tick_size=excluded.tick_size,
            precision=excluded.precision, instrument_type=excluded.instrument_type,
            underlying=excluded.underlying, multiplier=excluded.multiplier,
            is_index=excluded.is_index, updated_at=excluded.updated_at
        """,
        record,
    )


def _parse_segment_csv(text: str, exchange_segment: str) -> Iterable[dict[str, Any]]:
    """Turn one Kotak scrip CSV into instrument records."""
    timestamp = now()
    for row in csv.DictReader(io.StringIO(text)):
        # Kotak headers carry stray spaces (e.g. `dTickSize `).
        cleaned = {(key or "").strip(): (value or "") for key, value in row.items()}
        token = (cleaned.get("pSymbol") or "").strip()
        trading_symbol = (cleaned.get("pTrdSymbol") or "").strip()
        if not token or not trading_symbol:
            continue
        option_type = (cleaned.get("pOptionType") or "").strip().upper() or None
        yield {
            "token": token,
            "exchange_segment": exchange_segment,
            "symbol": (cleaned.get("pSymbolName") or trading_symbol).strip(),
            "trading_symbol": trading_symbol,
            "name": (cleaned.get("pInstName") or cleaned.get("pSymbolName") or trading_symbol).strip()[:180],
            "series": (cleaned.get("pGroup") or "").strip() or None,
            "expiry": _normalise_expiry(cleaned.get("lExpiryDate", "")),
            "option_type": option_type if option_type in {"CE", "PE"} else None,
            "strike": _to_float(cleaned.get("dStrikePrice;")),
            "lot_size": _to_int(cleaned.get("lLotSize")) or 1,
            "tick_size": _to_float(cleaned.get("dTickSize")) or 0.05,
            "precision": _to_int(cleaned.get("lPrecision")) or 2,
            "instrument_type": (cleaned.get("pInstType") or "").strip() or None,
            "underlying": (cleaned.get("pSymbolName") or "").strip() or None,
            "multiplier": _to_float(cleaned.get("lMultiplier")) or 1,
            "is_index": 0,
            "updated_at": timestamp,
        }


def fetch_segment_files(call) -> dict[str, str]:
    """Ask Neo for the scrip-master file list and map segment -> CSV URL."""
    listing = call("scrip_master")
    if isinstance(listing, dict) and "error" in listing:
        raise RuntimeError(f"Kotak rejected the scrip master request: {listing['error']}")

    paths: list[str] = []
    if isinstance(listing, dict):
        data = listing.get("data") or {}
        paths = data.get("filesPaths") or listing.get("filesPaths") or []
    elif isinstance(listing, list):
        paths = listing

    mapping: dict[str, str] = {}
    for path in paths:
        lowered = str(path).lower()
        for segment in SEGMENTS:
            if segment in lowered and segment not in mapping:
                mapping[segment] = str(path)
    return mapping


def sync(session_call, *, segments: Iterable[str] = SEGMENTS) -> dict[str, Any]:
    """Download and persist the scrip master for the requested segments."""
    with _lock:
        _state["status"] = "syncing"
        counts: dict[str, int] = {}
        errors: list[str] = []
        try:
            files = fetch_segment_files(session_call)
        except Exception as error:  # noqa: BLE001
            _state.update({"status": "failed", "error": str(error)})
            return {"status": "failed", "error": str(error), "counts": {}}

        timestamp = now()
        with app_cursor() as cursor:
            for index in INDEX_SEED:
                _upsert(cursor, {
                    "token": index["token"],
                    "exchange_segment": index.get("segment", "nse_cm"),
                    "symbol": index["symbol"],
                    "trading_symbol": index["symbol"],
                    "name": index["name"],
                    "series": "IDX", "expiry": None, "option_type": None, "strike": None,
                    "lot_size": index["lot_size"], "tick_size": index["tick"],
                    "precision": 2, "instrument_type": "IDX", "underlying": index["symbol"],
                    "multiplier": 1, "is_index": 1, "updated_at": timestamp,
                })
            counts["indices"] = len(INDEX_SEED)

            for segment in segments:
                url = files.get(segment)
                if not url:
                    errors.append(f"{segment}: not published by Kotak")
                    continue
                try:
                    response = requests.get(url, timeout=120)
                    response.raise_for_status()
                    total = 0
                    for record in _parse_segment_csv(response.text, segment):
                        _upsert(cursor, record)
                        total += 1
                    counts[segment] = total
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{segment}: {error}")
                    log.warning("Scrip master sync failed for %s: %s", segment, error)

            meta_set(cursor, "scrip_master_synced_at", str(timestamp))

        if errors and not any(key in counts for key in SEGMENTS):
            _state.update({"status": "failed", "error": "; ".join(errors)})
            return {"status": "failed", "error": "; ".join(errors), "counts": counts}

        _state.update({"status": "ready", "last_sync": timestamp, "error": None, "counts": counts})
        return {"status": "ready", "synced_at": timestamp, "counts": counts, "warnings": errors}


def status() -> dict[str, Any]:
    with app_cursor() as cursor:
        synced_at = meta_get(cursor, "scrip_master_synced_at")
        cursor.execute("SELECT COUNT(*) AS total FROM instruments")
        total = cursor.fetchone()["total"]
    return {
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
    last = _state.get("last_sync")
    return last is None or (time.time() - last) > SYNC_INTERVAL_SECONDS


# ------------------------------------------------------------------- queries


def search(query: str, *, limit: int = 25, exchange_segment: str | None = None) -> list[dict[str, Any]]:
    """Search the master by symbol, trading symbol or name."""
    needle = f"%{query.strip().upper()}%"
    sql = """
        SELECT token, exchange_segment, symbol, trading_symbol, name, series, expiry,
               option_type, strike, lot_size, tick_size, precision, instrument_type, underlying, is_index
        FROM instruments
        WHERE (UPPER(symbol) LIKE ? OR UPPER(trading_symbol) LIKE ? OR UPPER(name) LIKE ?)
    """
    params: list[Any] = [needle, needle, needle]
    if exchange_segment:
        sql += " AND exchange_segment = ?"
        params.append(exchange_segment)
    # Prefer cash equities and indices over far-dated option strikes.
    sql += " ORDER BY is_index DESC, (option_type IS NULL) DESC, symbol LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with app_cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def get_instrument(token: str, exchange_segment: str) -> dict[str, Any] | None:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM instruments WHERE token = ? AND exchange_segment = ?", (str(token), exchange_segment)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def list_by_trading_symbol(trading_symbol: str, exchange_segment: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM instruments WHERE UPPER(trading_symbol) = UPPER(?)"
    params: list[Any] = [trading_symbol]
    if exchange_segment:
        sql += " AND exchange_segment = ?"
        params.append(exchange_segment)
    with app_cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def option_contracts(underlying: str, exchange_segment: str = "nse_fo") -> dict[str, Any]:
    """Every CE/PE contract for an underlying, grouped by expiry.

    Returns `{expiries: [...], rows: [...]}` where each row is one strike with
    both legs. Returns an empty result rather than guessing when the master has
    no contracts for the underlying.
    """
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT token, exchange_segment, trading_symbol, symbol, expiry, option_type,
                   strike, lot_size, tick_size, precision
            FROM instruments
            WHERE UPPER(underlying) = UPPER(?) AND exchange_segment = ? AND option_type IN ('CE','PE')
            ORDER BY expiry ASC, strike ASC
            """,
            (underlying, exchange_segment),
        )
        contracts = [dict(row) for row in cursor.fetchall()]

    if not contracts:
        return {"expiries": [], "rows": [], "count": 0}

    expiries = sorted({row["expiry"] for row in contracts if row["expiry"]})
    by_expiry: dict[str, dict[float, dict[str, Any]]] = {}
    for contract in contracts:
        bucket = by_expiry.setdefault(contract["expiry"] or "", {})
        strike = float(contract["strike"] or 0)
        leg = bucket.setdefault(strike, {"strike": strike, "expiry": contract["expiry"],
                                         "lot_size": contract["lot_size"], "tick_size": contract["tick_size"]})
        leg[contract["option_type"].lower()] = {
            "token": contract["token"],
            "trading_symbol": contract["trading_symbol"],
            "exchange_segment": contract["exchange_segment"],
        }
    return {"expiries": expiries, "rows": by_expiry, "count": len(contracts)}


def universe(exchange_segment: str = "nse_cm", limit: int | None = None) -> list[dict[str, Any]]:
    """Cash equities, used for breadth and market-wide scans."""
    limit = limit or settings.breadth_universe_size
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT token, exchange_segment, symbol, trading_symbol, name, is_index
            FROM instruments
            WHERE exchange_segment = ? AND option_type IS NULL
            ORDER BY is_index DESC, symbol
            LIMIT ?
            """,
            (exchange_segment, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def indices(exchange_segment: str = "nse_cm") -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT token, exchange_segment, symbol, trading_symbol, name FROM instruments WHERE is_index = 1 AND exchange_segment = ? ORDER BY symbol",
            (exchange_segment,),
        )
        return [dict(row) for row in cursor.fetchall()]


def underlyings(exchange_segment: str = "nse_fo") -> list[str]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT underlying FROM instruments WHERE exchange_segment = ? AND underlying IS NOT NULL ORDER BY underlying",
            (exchange_segment,),
        )
        return [row["underlying"] for row in cursor.fetchall()]


def futures(exchange_segment: str = "nse_fo") -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT token, exchange_segment, symbol, trading_symbol, underlying, expiry, lot_size, tick_size
            FROM instruments
            WHERE exchange_segment = ? AND option_type IS NULL AND instrument_type LIKE 'FUT%'
            ORDER BY underlying, expiry
            """,
            (exchange_segment,),
        )
        return [dict(row) for row in cursor.fetchall()]
