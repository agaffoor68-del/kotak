"""Kotak Neo WebSocket feed with supervision, reconnect and fan-out.

The socket is a shared, long-lived resource: one connection per broker account,
many subscribers. A supervisor thread owns reconnection, so a dropped socket
recovers without any request needing to know about it.

Incoming frames are normalised, cached, recorded as ticks and published to an
asynchronous subscriber queue. All values originate from Kotak.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Any, Callable

from backend.broker import accounts
from backend.broker.neo_auth import NeoAuthError
from backend.core.config import settings
from backend.marketdata.quotes import quote_engine
from backend.marketdata import ticks

log = logging.getLogger("alphatrade.feed")

SocketFrame = dict[str, Any]


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    text = str(value).replace(",", "").strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


#: Kotak pushes short keys in some modes; map them onto the canonical names.
SOCKET_FIELD_MAP = {
    "lp": "last", "ltp": "last", "last_price": "last",
    "o": "open", "op": "open",
    "h": "high", "hi": "high", "high_price": "high",
    "l": "low", "lo": "low", "low_price": "low",
    "c": "close", "cl": "close", "prev_close": "previous_close", "pc": "previous_close",
    "v": "volume", "vol": "volume", "tot_traded_vol": "volume",
    "oi": "open_interest", "tot_oi": "open_interest",
    "bid": "bid", "ask": "ask", "bidprice": "bid", "askprice": "ask",
    "bidqty": "bid_quantity", "askqty": "ask_quantity",
    "iv": "implied_volatility", "implied_volatility": "implied_volatility",
    "ap": "average_price", "avg_price": "average_price",
    "uc": "upper_circuit", "lc": "lower_circuit",
    "w52h": "week52_high", "w52l": "week52_low",
    "nb": "total_buy_quantity", "ns": "total_sell_quantity",
    "pchange": "change_percent", "change_percent": "change_percent",
    "ltq": "last_traded_quantity",
}


def normalise_frame(frame: SocketFrame) -> SocketFrame | None:
    """Map a Kotak socket row onto the canonical quote field names."""
    token = str(frame.get("tk") or frame.get("token") or frame.get("instrument_token") or "").strip()
    if not token:
        return None
    segment = str(frame.get("exch_seg") or frame.get("exchange_segment") or "nse_cm").strip().lower()

    out: SocketFrame = {"token": token, "exchange_segment": segment}
    for key, value in frame.items():
        canonical = SOCKET_FIELD_MAP.get(key)
        if canonical and out.get(canonical) is None:
            number = _number(value)
            if number is not None:
                out[canonical] = number
    if "last" not in out:
        return None
    if "exchange_segment" in frame:
        out["exchange_segment"] = str(frame["exchange_segment"]).strip().lower()
    return out


class FeedState:
    """Observable connection state for the dashboard."""

    def __init__(self) -> None:
        self.status = "disconnected"
        self.account_id: str | None = None
        self.connected_at: float | None = None
        self.last_message_at: float | None = None
        self.frames_received = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.subscribed: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "account_id": self.account_id,
            "connected_at": self.connected_at,
            "last_message_at": self.last_message_at,
            "seconds_since_message": round(time.time() - self.last_message_at, 1) if self.last_message_at else None,
            "frames_received": self.frames_received,
            "reconnects": self.reconnects,
            "subscribed_instruments": self.subscribed,
            "last_error": self.last_error,
        }


class MarketFeed:
    """Owns the Neo socket and publishes normalised quotes."""

    def __init__(self) -> None:
        self.state = FeedState()
        self._lock = threading.RLock()
        self._client = None
        self._subscriptions: dict[str, dict[str, str]] = {}  # token -> {segment, ...}
        self._is_index: dict[str, bool] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- subscription bookkeeping ---------------------------------------
    def subscribe(self, instruments: list[dict[str, str]]) -> list[dict[str, str]]:
        added: list[dict[str, str]] = []
        with self._lock:
            for instrument in instruments:
                token = str(instrument.get("instrument_token") or instrument.get("token") or "").strip()
                if not token:
                    continue
                segment = str(instrument.get("exchange_segment") or "nse_cm").strip().lower()
                if token in self._subscriptions:
                    continue
                self._subscriptions[token] = {"exchange_segment": segment}
                added.append({"instrument_token": token, "exchange_segment": segment})
        if added and self._client is not None:
            try:
                self._client.subscribe(instrument_tokens=added, isIndex=False, isDepth=False)
                self._push_socket_subscribe(added)
            except Exception as error:  # noqa: BLE001
                log.warning("Neo subscribe failed: %s", error)
        self.state.subscribed = len(self._subscriptions)
        return added

    def unsubscribe(self, tokens: list[str]) -> list[str]:
        removed = []
        with self._lock:
            for token in tokens:
                entry = self._subscriptions.pop(str(token), None)
                if entry:
                    removed.append({"instrument_token": str(token), "exchange_segment": entry["exchange_segment"]})
        if removed and self._client is not None:
            try:
                self._client.un_subscribe(instrument_tokens=removed, isIndex=False, isDepth=False)
            except Exception as error:  # noqa: BLE001
                log.warning("Neo unsubscribe failed: %s", error)
        self.state.subscribed = len(self._subscriptions)
        return removed

    def subscription_tokens(self) -> list[str]:
        with self._lock:
            return list(self._subscriptions)

    # -- frame handling --------------------------------------------------
    def _on_message(self, raw: Any) -> None:
        try:
            payload = raw if isinstance(raw, dict) else json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        self.state.last_message_at = time.time()
        for row in self._iter_rows(payload):
            frame = normalise_frame(row)
            if frame is None:
                continue
            self.state.frames_received += 1
            quote = quote_engine.apply_tick(frame)
            if quote is not None:
                ticks.record_quote(quote)
                self._publish(quote.to_dict())

    @staticmethod
    def _iter_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("data", "feeds", "result", "quotes"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            if "tk" in payload or "token" in payload:
                return [payload]
            nested = [value for value in payload.values() if isinstance(value, dict) and ("tk" in value or "token" in value)]
            if nested:
                return nested
        return []

    # -- fan-out ---------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, channel: asyncio.Queue) -> Callable[[], None]:
        with self._lock:
            self._subscribers.add(channel)

        def unregister() -> None:
            with self._lock:
                self._subscribers.discard(channel)

        return unregister

    def _publish(self, payload: dict[str, Any]) -> None:
        loop, channels = self._loop, list(self._subscribers)
        if loop is None or not channels:
            return
        for channel in channels:
            try:
                loop.call_soon_threadsafe(self._offer, channel, payload)
            except RuntimeError:
                pass  # loop closing

    @staticmethod
    def _offer(channel: asyncio.Queue, payload: dict[str, Any]) -> None:
        try:
            channel.put_nowait(payload)
        except asyncio.QueueFull:
            pass  # a slow client drops ticks rather than stalling the feed

    # -- lifecycle -------------------------------------------------------
    def _connect(self) -> None:
        from backend.broker.neo_auth import session_manager

        account_id = accounts.default_account_id()
        if account_id is None:
            raise NeoAuthError("No Kotak account is configured")
        credentials = accounts.get_credentials(account_id)
        session = session_manager.get(account_id, credentials)

        client = session.client
        client.on_message = self._on_message
        client.on_error = lambda message: self._note_error(f"socket error: {message}")
        client.on_close = lambda message: self._note_error(f"socket closed: {message}")
        client.on_open = lambda message: log.info("Neo socket open: %s", message)

        # Neo's socket is opened by subscribe(); re-subscribe what we already track.
        pending = self._rebuild_subscriptions()
        if pending:
            client.subscribe(instrument_tokens=pending, isIndex=False, isDepth=False)
        # A no-op subscribe opens the connection even with an empty watchlist.
        if not pending:
            client.subscribe(instrument_tokens=[{"instrument_token": "26000", "exchange_segment": "nse_cm"}],
                             isIndex=True, isDepth=False)

        self._client = client
        self.state.status = "connected"
        self.state.account_id = account_id
        self.state.connected_at = time.time()
        self.state.last_error = None

    def _push_socket_subscribe(self, instruments: list[dict[str, str]]) -> None:
        """Also register with the raw socket, which is what actually streams."""
        if self._client is None:
            return
        socket = getattr(self._client, "neo_api", None) or getattr(self._client, "_NeoAPI__neo_api", None)
        if socket is None:
            return
        try:
            socket.subscribe.subscribe_feed(instruments, False, False)
        except Exception as error:  # noqa: BLE001
            log.debug("Raw socket subscribe skipped: %s", error)

    def _rebuild_subscriptions(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"instrument_token": token, "exchange_segment": entry["exchange_segment"]}
                for token, entry in self._subscriptions.items()
            ]

    def _note_error(self, message: str) -> None:
        self.state.last_error = message
        self.state.status = "reconnecting"
        log.warning("Market feed: %s", message)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect()
                backoff = 1.0
                # Hold the connection open; the SDK's socket thread does the work.
                while not self._stop.is_set():
                    time.sleep(1.0)
                    ticks.maybe_flush()
                    if (
                        self.state.last_message_at
                        and time.time() - self.state.last_message_at > 120
                        and self.state.status == "connected"
                    ):
                        self._note_error("no data for 120s; recycling the socket")
                        break
                self._client = None
            except Exception as error:  # noqa: BLE001
                self._note_error(str(error))
            if self._stop.is_set():
                break
            self.state.reconnects += 1
            self.state.status = "reconnecting"
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        self.state.status = "stopped"

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="market-feed", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        ticks.flush()

    def health(self) -> dict[str, Any]:
        return {**self.state.snapshot(), **quote_engine.health()}


market_feed = MarketFeed()
