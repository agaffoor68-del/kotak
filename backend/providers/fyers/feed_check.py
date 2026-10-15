class FyersFeed:
    """Supervises one FYERS Data Socket and fans normalised quotes out."""

    def __init__(self, provider: FyersProvider | None = None) -> None:
        self.state = FeedState()
        self._provider = provider
        self._lock = threading.RLock()
        self._socket_lock = threading.RLock()
        self._subscriptions: dict[str, dict[str, str]] = {}  # token -> {exchange_segment}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket: Any = None
        self._token_used: str | None = None
        self._socket_error: str | None = None
        self._auth_error: str | None = None

    # -- subscription bookkeeping -----------------------------------------

    def subscribe(self, instruments: list[dict[str, Any]]) -> list[dict[str, str]]:
        added: list[dict[str, str]] = []
        with self._lock:
            for instrument in instruments:
                token = str(instrument.get("instrument_token") or instrument.get("token") or "").strip()
                if not token or token in self._subscriptions:
                    continue
                segment = str(instrument.get("exchange_segment") or "nse_cm").strip().lower()
                self._subscriptions[token] = {"exchange_segment": segment}
                added.append({"instrument_token": token, "exchange_segment": segment})
            self.state.subscribed = len(self._subscriptions)
        if added:
            self._push_symbols([self._symbol_of(entry) for entry in added], subscribe=True)
        return added

    def unsubscribe(self, tokens: list[str]) -> list[str]:
        removed: list[dict[str, str]] = []
        with self._lock:
            for token in tokens:
                entry = self._subscriptions.pop(str(token), None)
                if entry:
                    removed.append({"instrument_token": str(token), "exchange_segment": entry["exchange_segment"]})
            self.state.subscribed = len(self._subscriptions)
        if removed:
            self._push_symbols([self._symbol_of(entry) for entry in removed], subscribe=False)
        return [entry["instrument_token"] for entry in removed]

    def subscription_tokens(self) -> list[str]:
        with self._lock:
            return list(self._subscriptions)

    def subscription_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(token, entry["exchange_segment"]) for token, entry in self._subscriptions.items()]

    @staticmethod
    def _symbol_of(entry: dict[str, str]) -> str | None:
        return symbol_map.to_fyers(
            str(entry.get("instrument_token") or ""), entry.get("exchange_segment", "nse_cm"),
        )

    def _subscription_symbols(self) -> list[str]:
        with self._lock:
            keys = [(token, entry["exchange_segment"]) for token, entry in self._subscriptions.items()]
        if not keys:
            return []
        return [symbol for _key, symbol in symbol_map.resolve(keys)[0]]

    def _push_symbols(self, symbols: list[str | None], *, subscribe: bool) -> None:
        """Apply a watchlist change to a live socket (best effort)."""
        wanted = [symbol for symbol in symbols if symbol]
        if not wanted:
            return
        with self._socket_lock:
            sock = self._socket
        if sock is None:
            return
        try:
            if subscribe:
                sock.subscribe(symbols=wanted, data_type="SymbolUpdate")
            else:
                sock.unsubscribe(symbols=wanted, data_type="SymbolUpdate")
        except Exception as error:  # noqa: BLE001 - the next (re)connect re-subscribes
            log.debug("FYERS socket re-subscribe failed (will retry): %s", error)

    # -- fan-out ----------------------------------------------------------

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

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            # The five headline indices are watched from boot so the dashboard
            # shows live prices (or the last close) without waiting for a click.
            self.subscribe([
                {"instrument_token": token, "exchange_segment": segment}
                for token, segment in INDEX_SYMBOLS
            ])
            self._thread = threading.Thread(target=self._run, name="fyers-feed", daemon=True)
            self._thread.start()
            log.info("FYERS market-data feed starting (read-only)")

    def stop(self) -> None:
        self._stop.set()
        self._teardown()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        ticks.flush()

"""FYERS live market-data feed (official Data Socket + REST snapshots).

One shared connection feeds the whole platform: ``/ws/market``, the dashboard
watchlist and the tick recorder all consume the quotes this feed publishes.

Two sources, one normalised stream:

* **Primary — FYERS Data Socket** (``fyers_apiv3.FyersWebsocket.data_ws``):
  SymbolUpdate ticks for every subscribed instrument, with the SDK's own
  auto-reconnect enabled.
* **Fallback/priming — ``GET /data/quotes`` snapshots**: taken on start (so the
  UI shows prices immediately, including *last close* when the market is shut),
  whenever live ticks are late, and once a minute after hours.

The supervisor thread owns the socket's lifetime: it acquires the token (daily
expiry → ``waiting_token`` with the exact reason), opens the socket, services
snapshots, recycles a silent socket, and re-opens after failures with
exponential backoff. Nothing is ever invented: a failed poll leaves the cache
untouched and records why.

Publishing matches the other feeds byte-for-byte (``quote_engine`` cache →
``ticks`` recorder → subscriber queues), so ``/ws/market`` and the UI needed no
protocol change — the "0 ticks" bug was precisely that the old socket published
to a queue nobody registered on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable

from backend.core.config import settings
from backend.marketdata import market_hours, ticks
from backend.marketdata.quotes import quote_engine
from backend.providers.base import ProviderAuthError, ProviderError

from .provider import FyersProvider, build_quote
from .symbols import INDEX_SYMBOLS, symbol_map

log = logging.getLogger("alphatrade.providers.fyers.feed")

#: A WS message older than this while the market is open counts as "silent".
SILENT_AFTER_SECONDS = 15.0


class FeedState:
    """Observable connection state for the dashboard (Kotak/Angel-compatible)."""

    def __init__(self) -> None:
        self.status = "disconnected"
        self.connected_at: float | None = None
        self.last_message_at: float | None = None
        self.frames_received = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.subscribed = 0
        self.unmapped_symbols = 0
        self.snapshots = 0
        self.last_snapshot_at: float | None = None

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "broker": "fyers",
            "status": self.status,
            "connected_at": self.connected_at,
            "last_message_at": self.last_message_at,
            "seconds_since_message": round(now - self.last_message_at, 1) if self.last_message_at else None,
            "frames_received": self.frames_received,
            "reconnects": self.reconnects,
            "subscribed_instruments": self.subscribed,
            "last_error": self.last_error,
            "unmapped_symbols": self.unmapped_symbols,
            "snapshots": self.snapshots,
            "last_snapshot_at": self.last_snapshot_at,
        }


def parse_frames(payload: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalise one socket/REST message into ``(frames, unknown_symbols)``.

    Accepts every shape FYERS uses: a flat tick dict (``symbol`` + ``ltp``), a
    # -- socket callbacks -------------------------------------------------
    def _teardown(self, sock: Any = None) -> None:
        with self._socket_lock:
            target = sock if sock is not None else self._socket
            self._socket = None
            self._token_used = None
        if target is None:
            return
        try:
            ws = getattr(target, "_FyersDataSocket__ws_object", None)
            if ws is not None:
                ws.close()
        except Exception:  # noqa: BLE001
            pass

    # -- supervisor -------------------------------------------------------

    def _run(self) -> None:
        provider = self._ensure_provider()
        backoff: float = 2.0
        while not self._stop.is_set():
            token = provider.auth.get_token()
            self.state.last_error = None
            if not token:
                self.state.status = "waiting_token"
                self.state.last_error = provider.auth.last_error or (
                    "FYERS session missing — open /fyers/login to re-authorise"
                )
                self._stop.wait(10.0)
                continue
            if self._token_used == token and self._socket is not None:
                self._stop.wait(1.0)
                continue
            self._token_used = token
            try:
                self._open_socket(token)
            except ProviderAuthError as error:
                self.state.status = "waiting_token"
                self.state.last_error = str(error)
                log.warning("FYERS login needed: %s", error)
                self._stop.wait(10.0)
                continue
            except Exception as error:  # noqa: BLE001 - transient infra failure
                self.state.status = "reconnecting"
                self.state.last_error = f"FYERS socket failed: {type(error).__name__}: {error}"
                log.warning("FYERS socket failed: %s", error)
                self._stop.wait(min(backoff, 10.0))
                backoff = min(backoff * 2.0, 30.0)
                continue
            provider = self._ensure_provider()
            quotes = provider.get_quotes(self.subscription_keys(), "all")
            for quote in quotes:
                quote_engine.store(quote)
                ticks.record_quote(quote)
                self._publish(quote.to_dict())
            self.state.snapshots += 1
            self.state.last_snapshot_at = time.time()
            self.state.last_message_at = time.time()
            self.state.frames_received += len(quotes)


    def _on_open(self) -> None:
        """SDK ``on_connect``: the socket is up, subscribe the watchlist."""
        symbols = self._subscription_symbols()
        sock = self._socket
        if sock is None or not symbols:
            return
        try:
            sock.subscribe(symbols=symbols, data_type="SymbolUpdate")
            log.info("FYERS socket subscribed to %d symbol(s)", len(symbols))
        except Exception as error:  # noqa: BLE001
            self._socket_error = f"subscribe failed: {error}"
            log.warning("FYERS subscribe failed: %s", error)

    def _on_message(self, message: Any) -> None:
        frames, unknown = parse_frames(message)
        if unknown:
            self.state.unmapped_symbols += len(unknown)
            log.debug("FYERS tick for unmapped symbol(s): %s", ", ".join(unknown[:5]))
        for frame in frames:
            self.state.frames_received += 1
            self.state.last_message_at = time.time()
            quote = quote_engine.apply_tick(frame)
            if quote is not None:
                ticks.record_quote(quote)
                self._publish(quote.to_dict())

    def _on_error(self, error: Any) -> None:
        text = str(error).strip() or error.__class__.__name__
        lowered = text.lower()
        if any(marker in lowered for marker in ("token", "authoriz", "auth", "401", "expired", "invalid")):
            self._auth_error = text
        self._socket_error = text
        if "token" in lowered and "expired" in lowered:
            self.state.last_error = (
                "FYERS session expired (tokens reset daily). Open /fyers/login to re-authorise."
            )
        else:
            self.state.last_error = f"FYERS socket: {text}"
        log.warning("FYERS socket error: %s", text)

    def _on_close(self, message: Any) -> None:
        if self._stop.is_set():
            return
        self.state.status = "reconnecting"
        detail = ""
        if isinstance(message, dict):
            detail = str(message.get("message") or message.get("code") or "")
        elif message:
            detail = str(message)
        self.state.last_error = f"FYERS socket closed{': ' + detail if detail else ''}"
        log.warning("FYERS data socket closed (%s)", detail or "no detail")

    # -- socket open/teardown ---------------------------------------------

    def _open_socket(self, token: str) -> Any:
        from fyers_apiv3.FyersWebsocket import data_ws

        self._socket_error = None
        self._auth_error = None
        sock = data_ws.FyersDataSocket(
            access_token=f"{settings.fyers_app_id}:{token}",
            log_path=settings.data_dir,
            litemode=False,
            write_to_file=False,
            reconnect=True,
            reconnect_retry=10,
            on_connect=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        with self._socket_lock:
            self._teardown()
            self._socket = sock
            self._token_used = token
        sock.background_flag = True  # keep the SDK daemon even without logging
        sock.connect()  # blocks until the handshake completes
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._stop.is_set():
                self._teardown()
                raise ProviderAuthError("FYERS feed cancelled while connecting.")
            if sock.is_connected():
                break
            time.sleep(0.5)
        else:
            self._teardown(sock)
            last_error = self._socket_error or self._auth_error or "unknown handshake failure"
            raise ProviderAuthError(
                f"FYERS data socket did not open: {last_error}. Open /fyers/login to re-authorise."
            )
        return sock

    # -- REST snapshots (priming + fallback) -------------------------------

    def _ensure_provider(self) -> FyersProvider:
        if self._provider is None:
            from backend.providers import get_provider

            self._provider = get_provider()  # type: ignore[assignment]
        return self._provider  # type: ignore[return-value]

    def poll_once(self) -> int:
        """One ``/data/quotes`` snapshot of the watchlist. Returns rows published."""
        keys = self.subscription_keys()
        if not keys:
            return 0
        provider = self._ensure_provider()
        quotes = provider.get_quotes(keys, "all")
        for quote in quotes:
            quote_engine.store(quote)
            ticks.record_quote(quote)
            self._publish(quote.to_dict())
        self.state.snapshots += 1
        self.state.last_snapshot_at = time.time()
        self.state.last_message_at = time.time()
        self.state.frames_received += len(quotes)
        return len(quotes)

    nested ``{"data": {...}}`` wrapper, a ``{"d": [...]}`` quotes envelope, or
    raw JSON text. Control frames (subscribe acks, heartbeats) carry no price
    and are ignored rather than counted as ticks.
    """
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return [], []
    if not isinstance(payload, dict):
        return [], []

    rows: list[dict[str, Any]] = []
    nested = payload.get("d")
    if isinstance(nested, list):
        rows.extend(row for row in nested if isinstance(row, dict))
    data = payload.get("data")
    if isinstance(data, dict):
        rows.append(data)
    elif isinstance(data, list):
        rows.extend(row for row in data if isinstance(row, dict))
    if "symbol" in payload:
        rows.append(payload)

    frames: list[dict[str, Any]] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or row.get("n") or "")
        if not symbol or symbol in seen:
            continue
        if "ltp" not in row and "lp" not in row:
            continue  # control/ack frame without a price
        seen.add(symbol)
        key = symbol_map.from_fyers(symbol)
        if key is None:
            unknown.append(symbol)
            continue
        frames.append(build_quote(key, row).to_dict())
    return frames, unknown
