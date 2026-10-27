"""Symbol master synchronised from Kotak Neo and Angel One.

The master has two sources, and both are needed:

* **Kotak Neo** publishes a set of CSV files (one per exchange segment). We
  fetch the file list through the authenticated API, download each segment's
  CSV and normalise it into the `instruments` table.
* **Angel One** publishes one public JSON file, ``OpenAPIScripMaster.json``,
  which is the token namespace the SmartAPI quote endpoint understands. It is
  synced by :func:`sync_angel` and is what the platform quotes by — a symbol
  resolved against a Kotak token alone cannot be priced, which is why the
  option chain and the quote endpoints used to come back empty.

Nothing here invents an instrument: if a source fails to download, the sync
reports it and the previous master stays in place.
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
from backend.marketdata import search_index

log = logging.getLogger("alphatrade.master")

SEGMENTS = ("nse_cm", "bse_cm", "nse_fo", "bse_fo", "cde_fo", "mcx_fo")

#: The source name Angel One's rows carry in the `instruments` table.
ANGEL_SOURCE = "angel"

#: How often the master is refreshed. Kotak publishes new expiries daily.
SYNC_INTERVAL_SECONDS = 6 * 60 * 60

_lock = threading.RLock()
_state: dict[str, Any] = {"last_sync": None, "status": "idle", "error": None, "counts": {}}

#: Indices are not in the CSV; Neo resolves them through the quote API, so the
#: well-known NSE index tokens are seeded from Kotak's own published values.
# Angel One's canonical index namespace.  Do not seed the legacy Kotak
# tokens here: SmartAPI quotes and the live feed use these official tokens.
INDEX_SEED: list[dict[str, Any]] = [
    {"token": "99926000", "symbol": "NIFTY 50", "name": "NIFTY 50", "lot_size": 50, "tick": 0.05},
    {"token": "99926009", "symbol": "BANKNIFTY", "name": "NIFTY BANK", "lot_size": 15, "tick": 0.05},
    {"token": "99926037", "symbol": "FINNIFTY", "name": "FIN NIFTY", "lot_size": 65, "tick": 0.05},
    {"token": "99926074", "symbol": "MIDCPNIFTY", "name": "MIDCAP NIFTY", "lot_size": 140, "tick": 0.05},
    {"token": "99919000", "symbol": "SENSEX", "name": "S&P BSE SENSEX", "lot_size": 1, "tick": 0.05, "segment": "bse_cm"},
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
    """Write one Kotak instrument.

    A Kotak row never overwrites an Angel One row: both masters use the same
    primary key, and Angel's row is the one the quote API understands.
    """
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
        WHERE instruments.source != 'angel'
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


def sync_angel(*, url: str | None = None) -> dict[str, Any]:
    """Sync Angel One's ``OpenAPIScripMaster.json`` into the shared master.

    The file is public, so no broker session is needed. It is the platform's
    primary instrument source: the SmartAPI quote endpoint only understands
    the tokens it publishes, so a master without it resolves symbols to
    tokens no quote request can price.
    """
    from backend.angel import instruments as angel_instruments

    try:
        result = angel_instruments.sync(url=url)
    except Exception as error:  # noqa: BLE001 - a source failure is reported, not raised
        log.warning("Angel One instrument master sync failed: %s", error)
        return {"status": "failed", "error": str(error), "counts": {}, "total": 0}
    if result.get("status") == "ready":
        search_index.invalidate()
    return result


def sync(
    session_call,
    *,
    segments: Iterable[str] = SEGMENTS,
    include_angel: bool = True,
) -> dict[str, Any]:
    """Download and persist the scrip master for the requested segments.

    Both sources are refreshed: the Kotak CSVs (which need an authenticated
    session) and, unless `include_angel` is turned off, Angel One's public
    JSON master.
    """
    with _lock:
        _state["status"] = "syncing"
        counts: dict[str, int] = {}
        errors: list[str] = []
        try:
            files = fetch_segment_files(session_call)
        except Exception as error:  # noqa: BLE001
            files = {}
            errors.append(f"kotak: {error}")
            log.warning("Kotak scrip master listing failed: %s", error)

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

        # Angel One's JSON master is synced outside the Kotak transaction: it
        # opens its own connection and must not be nested in this one.
        if include_angel:
            angel_result = sync_angel()
            if angel_result.get("status") == "ready":
                counts[ANGEL_SOURCE] = int(angel_result.get("total") or 0)
            else:
                errors.append(f"angel: {angel_result.get('error') or 'sync failed'}")

        if errors and not any(key in counts for key in (*SEGMENTS, ANGEL_SOURCE)):
            _state.update({"status": "failed", "error": "; ".join(errors)})
            return {"status": "failed", "error": "; ".join(errors), "counts": counts}

        _state.update({"status": "ready", "last_sync": timestamp, "error": None, "counts": counts})
        # The local search index mirrors this table; drop it so the next
        # search rebuilds from the new master.
        search_index.invalidate()
        return {"status": "ready", "synced_at": timestamp, "counts": counts, "warnings": errors}


def status() -> dict[str, Any]:
    with app_cursor() as cursor:
        synced_at = meta_get(cursor, "scrip_master_synced_at")
        cursor.execute("SELECT COUNT(*) AS total FROM instruments")
        total = cursor.fetchone()["total"]
        cursor.execute("SELECT source, COUNT(*) AS total FROM instruments GROUP BY source")
        sources = {str(row["source"]): int(row["total"]) for row in cursor.fetchall()}
    return {
        "status": _state["status"],
        "last_sync": float(synced_at) if synced_at else None,
        "last_error": _state["error"],
        "counts": _state["counts"],
        "instruments": total,
        "sources": sources,
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


def instrument_display(token: str, exchange_segment: str) -> tuple[str, str | None]:
    """Return the display symbol and optional company name for a token.

    The trading symbol is preferred: it is what the exchange shows and what
    the operator recognises ("RELIANCE", not "RELIANCE INDUSTRIES LTD."). A
    token the master does not know is returned as it is, so the caller can
    decide what to show instead of receiving a decorated token.
    """
    instrument = get_instrument(token, exchange_segment)
    if not instrument:
        return str(token), None
    symbol = (
        search_index.clean_symbol(instrument.get("trading_symbol"))
        or search_index.clean_symbol(instrument.get("symbol"))
        or str(token)
    )
    name = instrument.get("name") or None
    if name and str(name).strip().upper() == symbol.upper():
        name = None
    return symbol, name


def list_by_trading_symbol(trading_symbol: str, exchange_segment: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM instruments WHERE UPPER(trading_symbol) = UPPER(?)"
    params: list[Any] = [trading_symbol]
    if exchange_segment:
        sql += " AND exchange_segment = ?"
        params.append(exchange_segment)
    with app_cursor() as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def find_instrument(query: str, exchange_segment: str | None = None) -> dict[str, Any] | None:
    """Find one instrument by trading symbol, display symbol or company name.

    The desk types what it knows — "NIFTY", "RELIANCE", "NIFTY 50" — while the
    master stores the exchange's trading symbol, the display symbol and the
    company name. An exact match on any of them wins; a partial match is the
    fallback. Nothing is invented: when the master has no such instrument the
    answer is ``None``.
    """
    text = str(query or "").strip()
    if not text:
        return None
    segment_clause = " AND exchange_segment = ?" if exchange_segment else ""

    with app_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT * FROM instruments
            WHERE (UPPER(trading_symbol) = UPPER(?) OR UPPER(symbol) = UPPER(?) OR UPPER(name) = UPPER(?))
              {segment_clause}
            ORDER BY is_index DESC, LENGTH(symbol) ASC
            LIMIT 1
            """,
            (text, text, text, *([exchange_segment] if exchange_segment else [])),
        )
        row = cursor.fetchone()
        if row is not None:
            return dict(row)

        cursor.execute(
            f"""
            SELECT * FROM instruments
            WHERE (UPPER(trading_symbol) LIKE ? OR UPPER(symbol) LIKE ? OR UPPER(name) LIKE ?)
              {segment_clause}
            ORDER BY is_index DESC, LENGTH(symbol) ASC
            LIMIT 1
            """,
            (f"%{text.upper()}%", f"%{text.upper()}%", f"%{text.upper()}%", *([exchange_segment] if exchange_segment else [])),
        )
        row = cursor.fetchone()
    return dict(row) if row is not None else None


def option_contracts(
    underlying: str,
    exchange_segment: str = "nse_fo",
    expiry: str | None = None,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Every CE/PE contract for an underlying, grouped by expiry.

    Returns `{expiries: [...], rows: [...]}` where each row is one strike with
    both legs. Returns an empty result rather than guessing when the master has
    no contracts for the underlying.

    When `expiry` is given (ISO ``YYYY-MM-DD``) only that series is returned, so
    the option chain can show a chosen expiry instead of always the near one.

    `source` narrows the result to one master (``"angel"`` or ``"kotak"``).
    The token namespace matters: a caller that quotes with SmartAPI needs the
    Angel rows, one that orders through Kotak Neo needs the Kotak rows.
    """
    clause = ""
    params: list[Any] = [underlying, exchange_segment]
    if expiry:
        clause += " AND expiry = ?"
        params.append(expiry)
    if source:
        clause += " AND source = ?"
        params.append(source)
    with app_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT token, exchange_segment, trading_symbol, symbol, expiry, option_type,
                   strike, lot_size, tick_size, precision
            FROM instruments
            WHERE UPPER(underlying) = UPPER(?) AND exchange_segment = ? AND option_type IN ('CE','PE'){clause}
            ORDER BY expiry ASC, strike ASC
            """,
            tuple(params),
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


def option_expiries(underlying: str, exchange_segment: str = "nse_fo") -> list[str]:
    """Distinct option expiries for an underlying, ascending (ISO dates).

    This is the `getExpiryDate` surface for the UI: it never invents a date —
    when the master has no contracts for the underlying the list is empty and the
    caller reports that a scrip-master sync is needed.
    """
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT expiry FROM instruments
            WHERE UPPER(underlying) = UPPER(?) AND exchange_segment = ?
              AND option_type IN ('CE','PE') AND expiry IS NOT NULL AND expiry != ''
            ORDER BY expiry ASC
            """,
            (underlying, exchange_segment),
        )
        return [row["expiry"] for row in cursor.fetchall()]


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
