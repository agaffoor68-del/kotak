"""FYERS symbol vocabulary ⇄ platform ``(token, exchange_segment)`` keys.

FYERS speaks ``NSE:RELIANCE-EQ``-style tickers while the platform identifies
instruments by the numeric token stored in the ``instruments`` table (the same
identity the UI, the tick store and the candles table use). This module owns
that translation in both directions:

* **The five index** the dashboard watches are mapped **statically**
  (``NSE:NIFTY50-INDEX``, ``NSE:NIFTYBANK-INDEX``, ``NSE:FINNIFTY-INDEX``,
  ``NSE:MIDCPNIFTY-INDEX``, ``BSE:SENSEX-INDEX``) — their platform tokens are
  the well-known exchange tokens, and FYERS' index tickers do not follow the
  equity naming scheme, so guessing would be wrong.
* **Everything else** is built as ``{EXCHANGE}:{trading_symbol}``. Both
  Angel's scrip master and FYERS carry the exchange's own trading symbol, so
  the construction is exact for NSE/BSE cash, F&O, currency and commodity.
* **Cash candidates are validated** against FYERS' public daily master files
  (``public.fyers.in/sym_details`` — NSE/BSE cash only; the F&O master is
  ~88 MB and is deliberately not downloaded). Validation runs in the
  background: before it lands, candidates are accepted as constructed and the
  map simply reports ``validated: false``.

A symbol the map cannot resolve is never guessed: it is counted, surfaced in
``health()``, and the caller gets a clear "not in the FYERS universe" error.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Iterable

import requests

from backend.core.config import settings
from backend.providers.base import InstrumentKey, ProviderSymbolError

log = logging.getLogger("alphatrade.providers.fyers.symbols")

#: Platform segment → exchange prefix FYERS expects in a ticker.
EXCHANGE_BY_SEGMENT: dict[str, str] = {
    "nse_cm": "NSE",
    "bse_cm": "BSE",
    "nse_fo": "NSE",
    "bse_fo": "BSE",
    "cde_fo": "NSE",
    "mcx_fo": "MCX",
}

#: The dashboard's five indices: platform key → (FYERS ticker, display name).
#: Keys match ``backend.marketdata.master.INDEX_SEED``.
INDEX_SYMBOLS: dict[InstrumentKey, tuple[str, str]] = {
    ("26000", "nse_cm"): ("NSE:NIFTY50-INDEX", "NIFTY 50"),
    ("26009", "nse_cm"): ("NSE:NIFTYBANK-INDEX", "NIFTY BANK"),
    ("26037", "nse_cm"): ("NSE:FINNIFTY-INDEX", "FIN NIFTY"),
    ("26074", "nse_cm"): ("NSE:MIDCPNIFTY-INDEX", "MIDCAP NIFTY"),
    ("265", "bse_cm"): ("BSE:SENSEX-INDEX", "SENSEX"),
}
INDEX_BY_SYMBOL: dict[str, InstrumentKey] = {symbol: key for key, (symbol, _name) in INDEX_SYMBOLS.items()}

#: Segments whose FYERS master is small enough to validate candidates against.
#: (NSE_FO's master is ~88 MB; derivatives are accepted as constructed.)
VALIDATOR_MASTERS: dict[str, str] = {
    "nse_cm": "NSE_CM",
    "bse_cm": "BSE_CM",
}

#: Rebuild no more often than this when a lookup misses (master may have synced).
REBUILD_COOLDOWN_SECONDS = 60.0


def candidate_symbol(exchange_segment: str, trading_symbol: str) -> str | None:
    """The FYERS ticker a platform instrument should be, or ``None``."""
    prefix = EXCHANGE_BY_SEGMENT.get(str(exchange_segment or "").lower())
    trading = str(trading_symbol or "").strip()
    if not prefix or not trading:
        return None
    if ":" in trading:  # already prefixed
        return trading if trading.split(":", 1)[0].upper() == prefix else f"{prefix}:{trading.split(':', 1)[1]}"
    return f"{prefix}:{trading}"


def _instrument_rows() -> list[tuple[str, str, str]]:
    """``(token, segment, trading_symbol)`` for every non-index instrument."""
    try:
        from backend.core.database import app_cursor

        with app_cursor() as cursor:
            cursor.execute(
                """
                SELECT token, exchange_segment, trading_symbol FROM instruments
                WHERE is_index = 0 AND trading_symbol IS NOT NULL AND trading_symbol <> ''
                """
            )
            return [(str(row[0]), str(row[1]), str(row[2])) for row in cursor.fetchall()]
    except Exception as error:  # noqa: BLE001 - an empty/absent DB is not fatal
        log.debug("Instrument master unreadable while building the FYERS map: %s", error)
        return []


class FyersSymbolMap:
    """Bidirectional platform-key ⇄ FYERS-ticker mapping with optional checks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._forward: dict[InstrumentKey, str] = {}
        self._reverse: dict[str, InstrumentKey] = {}
        self._validators: dict[str, set[str]] = {}
        self._instrument_count = 0
        self._unvalidated = 0
        self._last_rebuild = 0.0
        self._loading_validators = False
        self._load_error: str | None = None

    # -- construction -----------------------------------------------------

    def reload(self, force: bool = False) -> None:
        """Rebuild the maps from the instrument master (thread-safe, cheap).

        FYERS cash-master validation is fetched in the background the first
        time; the map is usable (unvalidated) before it arrives.
        """
        with self._lock:
            now = time.time()
            if not force and now - self._last_rebuild < REBUILD_COOLDOWN_SECONDS:
                return
            self._last_rebuild = now
            self._rebuild_locked()
        self._schedule_validator_load()

    def _rebuild_locked(self) -> None:
        rows = _instrument_rows()
        forward: dict[InstrumentKey, str] = {}
        reverse: dict[str, InstrumentKey] = {}

        for key, (symbol, _name) in INDEX_SYMBOLS.items():
            forward[key] = symbol
            reverse[symbol] = key

        validated = self._validators
        unvalidated = 0
        skipped = 0
        for token, segment, trading_symbol in rows:
            key: InstrumentKey = (str(token), segment)
            if key in forward:
                continue
            symbol = candidate_symbol(segment, trading_symbol)
            if not symbol:
                skipped += 1
                continue
            # Cash rows are checked against FYERS' own master when available.
            allowed = validated.get(segment)
            if allowed is not None and symbol not in allowed:
                skipped += 1
                continue
            if allowed is None:
                unvalidated += 1
            if symbol in reverse:
                # Two platform tokens mapping to one ticker would make tick
                # routing ambiguous; the first one wins and the clash is counted.
                skipped += 1
                continue
            forward[key] = symbol
            reverse[symbol] = key

        self._forward = forward
        self._reverse = reverse
        self._instrument_count = len(rows)
        self._unvalidated = unvalidated
        log.debug(
            "FYERS symbol map: %d keys (%d instruments, %d unvalidated, %d skipped)",
            len(forward), len(rows), unvalidated, skipped,
        )

    # -- FYERS cash-master validation (background) -------------------------

    def _schedule_validator_load(self) -> None:
        wanted = {
            segment: code
            for segment, code in VALIDATOR_MASTERS.items()
            if segment not in self._validators
        }
        if not wanted:
            return
        with self._lock:
            if self._loading_validators:
                return
            self._loading_validators = True
        threading.Thread(
            target=self._load_validators, args=(wanted,), name="fyers-symbols", daemon=True,
        ).start()

    def _load_validators(self, wanted: dict[str, str]) -> None:
        loaded = 0
        for segment, code in wanted.items():
            tickers = self._fetch_master(code)
            if tickers is None:
                continue
            with self._lock:
                self._validators[segment] = tickers
            loaded += 1
        with self._lock:
            self._loading_validators = False
            if loaded:
                self._load_error = None
                self._rebuild_locked()
                log.info("FYERS symbol validation loaded for %d segment(s)", loaded)

    def _fetch_master(self, code: str) -> set[str] | None:
        """Download (or reuse a fresh copy of) one FYERS master file."""
        url = settings.fyers_symbol_master_url.format(code)
        cache = os.path.join(settings.data_dir, f"fyers_master_{code}.json")
        ttl = max(300, int(settings.fyers_symbol_master_ttl))
        for source in ("cache", "download"):
            try:
                if source == "cache":
                    if not os.path.exists(cache) or time.time() - os.path.getmtime(cache) > ttl:
                        continue
                    with open(cache, "r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                else:
                    response = requests.get(url, timeout=60)
                    response.raise_for_status()
                    payload = response.json()
                    try:
                        os.makedirs(settings.data_dir, exist_ok=True)
                        with open(cache, "w", encoding="utf-8") as handle:
                            json.dump(payload, handle)
                    except OSError:  # a read-only disk must not break the map
                        pass
                tickers = _tickers_of(payload)
                if tickers:
                    return tickers
            except Exception as error:  # noqa: BLE001 - validation is best-effort
                self._load_error = f"FYERS master {code} unavailable: {error}"
                log.warning("FYERS master %s could not be loaded: %s", code, error)
        return None

    # -- lookups -----------------------------------------------------------

    def to_fyers(self, token: str, exchange_segment: str) -> str | None:
        key: InstrumentKey = (str(token), exchange_segment)
        with self._lock:
            symbol = self._forward.get(key)
        if symbol is None:
            self.reload()  # the master may have synced since the last build
            with self._lock:
                symbol = self._forward.get(key)
        return symbol

    def from_fyers(self, symbol: str) -> InstrumentKey | None:
        wanted = str(symbol or "")
        with self._lock:
            hit = self._reverse.get(wanted)
            built = bool(self._forward)
        if hit is not None:
            return hit
        if not built:
            self.reload()
            with self._lock:
                return self._reverse.get(wanted)
        return None

    def resolve(
        self, keys: Iterable[InstrumentKey],
    ) -> tuple[list[tuple[InstrumentKey, str]], list[InstrumentKey]]:
        """Split requested keys into ``(resolved, unmapped)``."""
        resolved: list[tuple[InstrumentKey, str]] = []
        unmapped: list[InstrumentKey] = []
        for token, segment in keys:
            key: InstrumentKey = (str(token), segment)
            symbol = self.to_fyers(key[0], key[1])
            if symbol is None:
                unmapped.append(key)
            else:
                resolved.append((key, symbol))
        if unmapped:
            log.debug("FYERS symbol map could not resolve %d key(s)", len(unmapped))
        return resolved, unmapped

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mapped": len(self._forward),
                "instruments": self._instrument_count,
                "indices": len(INDEX_SYMBOLS),
                "unvalidated": self._unvalidated,
                "validated_segments": sorted(self._validators),
                "last_rebuild": self._last_rebuild or None,
                "validation_error": self._load_error,
            }

    def clear(self) -> None:
        with self._lock:
            self._forward = {}
            self._reverse = {}
            self._last_rebuild = 0.0


def _tickers_of(payload: Any) -> set[str]:
    """Tickers out of a FYERS master file (dict keyed by ticker, or a list)."""
    if isinstance(payload, dict):
        tickers = {str(key) for key in payload.keys() if ":" in str(key)}
        nested = payload.get("data")
        if isinstance(nested, list):
            tickers |= {
                str(row.get("symTicker"))
                for row in nested
                if isinstance(row, dict) and row.get("symTicker")
            }
        return tickers
    if isinstance(payload, list):
        return {
            str(row.get("symTicker"))
            for row in payload
            if isinstance(row, dict) and row.get("symTicker")
        }
    return set()


def require_mapped(resolved, unmapped: list[InstrumentKey]) -> None:
    """Raise one clear, UI-facing error when *nothing* could be mapped."""
    if resolved:
        return
    sample = ", ".join(f"{token}:{segment}" for token, segment in list(unmapped)[:5])
    raise ProviderSymbolError(
        f"None of the {len(unmapped)} requested instruments have a FYERS symbol "
        f"(e.g. {sample}). Sync the instrument master (POST /api/v1/market/sync-master) "
        "and check the symbol-map health for details."
    )


#: One shared map per process.
symbol_map = FyersSymbolMap()


