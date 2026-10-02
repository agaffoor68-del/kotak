"""Strategy runner: evaluates a strategy on live bars and routes orders.

A single supervised thread per run. It pulls live quotes, rebuilds candles from
recorded ticks, evaluates the strategy through the same DSL the backtester uses,
and routes orders to the paper or live executor — with the risk engine in front
of the live one.

Runs are halted automatically when the risk engine says trading must stop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.backtesting.engine import _warmup_bars
from backend.core.config import settings
from backend.core.database import app_cursor, now
from backend.execution import live
from backend.marketdata import master, ticks
from backend.marketdata.market_hours import is_market_open, session_state
from backend.marketdata.quotes import quote_engine
from backend.papertrading import engine as paper
from backend.risk.engine import RiskEngine, RiskViolation
from backend.strategies import dsl

log = logging.getLogger("alphatrade.algo")


@dataclass
class AlgoRun:
    id: str
    strategy_id: str
    account_id: str
    name: str
    mode: str                 # paper or live
    interval: str
    status: str = "pending"   # pending, running, halted, completed, error, stopped
    reason: str | None = None
    started_at: float = field(default_factory=time.time)
    stopped_at: float | None = None
    evaluations: int = 0
    signals: int = 0
    orders_placed: int = 0
    orders_rejected: int = 0
    last_signal: str | None = None
    log: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {message}")
        del self.log[:-200]  # keep the tail bounded

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "strategy_id": self.strategy_id, "account_id": self.account_id,
            "name": self.name, "mode": self.mode, "interval": self.interval,
            "status": self.status, "reason": self.reason,
            "started_at": self.started_at, "stopped_at": self.stopped_at,
            "evaluations": self.evaluations, "signals": self.signals,
            "orders_placed": self.orders_placed, "orders_rejected": self.orders_rejected,
            "last_signal": self.last_signal, "log": self.log[-40:],
        }


class AlgoSupervisor:
    """Owns the set of running strategies."""

    def __init__(self) -> None:
        self._runs: dict[str, AlgoRun] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------
    def start(self, strategy: dict[str, Any], account_id: str, mode: str) -> AlgoRun:
        definition = strategy.get("definition") or {}
        validation = dsl.validate(definition)
        if not validation["valid"]:
            raise ValueError("; ".join(validation["errors"]))
        if mode == "live" and not settings.enable_algo_execution:
            raise ValueError("Algo live execution is disabled. Set ENABLE_ALGO_EXECUTION=1 to enable it.")
        if mode == "live" and not settings.enable_live_trading:
            raise ValueError("Live trading is disabled. Set ENABLE_LIVE_TRADING=1 to enable it.")

        interval = str(definition.get("timeframe") or "5m")
        if interval not in ticks.INTERVAL_SECONDS:
            raise ValueError(f"Unsupported timeframe '{interval}'")

        run = AlgoRun(
            id=uuid.uuid4().hex, strategy_id=strategy["id"], account_id=account_id,
            name=str(strategy.get("name") or definition.get("name") or "Strategy"),
            mode=mode, interval=interval, status="running",
        )
        stop = threading.Event()
        thread = threading.Thread(target=self._loop, args=(run, definition, stop), name=f"algo-{run.id[:8]}", daemon=True)

        with self._lock:
            self._runs[run.id] = run
            self._stops[run.id] = stop
            self._threads[run.id] = thread
        thread.start()

        with app_cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO strategy_runs (id, strategy_id, account_id, mode, status, params, started_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (run.id, run.strategy_id, account_id, mode, "running", json.dumps({"interval": interval}), now()),
            )
        log.info("Algo run %s started: %s (%s, %s)", run.id[:8], run.name, mode, interval)
        return run

    def stop(self, run_id: str, reason: str = "stopped by operator") -> dict[str, Any]:
        with self._lock:
            stop = self._stops.get(run_id)
            run = self._runs.get(run_id)
        if stop is not None:
            stop.set()
        if run is not None:
            run.status = "stopped"
            run.reason = reason
            run.stopped_at = time.time()
        with app_cursor() as cursor:
            cursor.execute(
                "UPDATE strategy_runs SET status = ?, finished_at = ? WHERE id = ?",
                ("stopped", now(), run_id),
            )
        return run.to_dict() if run else {"id": run_id, "status": "stopped"}

    def stop_all_for(self, account_id: str, reason: str) -> int:
        with self._lock:
            run_ids = [rid for rid, run in self._runs.items()
                       if run.account_id == account_id and run.status == "running"]
        for run_id in run_ids:
            self.stop(run_id, reason)
        return len(run_ids)

    def get(self, run_id: str) -> AlgoRun | None:
        return self._runs.get(run_id)

    def list(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            runs = list(self._runs.values())
        if account_id:
            runs = [run for run in runs if run.account_id == account_id]
        return [run.to_dict() for run in sorted(runs, key=lambda r: r.started_at, reverse=True)]

    # -- main loop -------------------------------------------------------
    def _loop(self, run: AlgoRun, definition: dict[str, Any], stop: threading.Event) -> None:
        poll = max(5, int(ticks.INTERVAL_SECONDS.get(run.interval, 300) / 5))
        required = _warmup_bars(definition)
        last_bar = 0.0
        risk = RiskEngine(run.account_id)

        while not stop.is_set():
            try:
                session = session_state()
                if not session["is_open"]:
                    run.status = "halted"
                    run.reason = f"market {session['state']}: {session['reason']}"
                    stop.wait(poll)
                    continue

                risk_status = risk.status()
                if risk_status["trading_halted"]:
                    run.status = "halted"
                    run.reason = risk_status["halt_reason"] or "risk halt"
                    run.note(f"HALTED: {run.reason}")
                    self.stop(run.id, run.reason)
                    break

                run.status = "running"
                instruments = [
                    {"instrument_token": str(item["token"]), "exchange_segment": item.get("exchange_segment", "nse_cm"),
                     "label": item.get("label") or item.get("symbol")}
                    for item in (definition.get("universe") or [])
                ]
                if not instruments:
                    run.status = "error"
                    run.reason = "Strategy has no instruments"
                    break

                # Refresh prices so fills and the indicator window use live data.
                call = _session_call(run.account_id)
                if call is not None:
                    try:
                        quote_engine.fetch_batched(call, instruments, quote_type="all")
                    except Exception as error:  # noqa: BLE001
                        run.note(f"quote refresh failed: {error}")

                first = instruments[0]
                candles = ticks.history(first["instrument_token"], first["exchange_segment"], run.interval, 1000)
                if len(candles) < required:
                    run.note(f"waiting for history: {len(candles)}/{required} bars")
                    stop.wait(poll)
                    continue

                cache = dsl.IndicatorCache(candles)
                index = len(candles) - 1
                bar_time = float(candles[index].get("time") or 0)

                if bar_time == last_bar:
                    stop.wait(poll)
                    continue
                last_bar = bar_time
                run.evaluations += 1

                entry_signal = dsl.evaluate(definition.get("entry"), cache, index)
                exit_signal = dsl.evaluate(definition.get("exit"), cache, index)

                if entry_signal:
                    run.signals += 1
                    run.last_signal = "entry"
                    run.note(f"entry signal on {first.get('label')} @ {candles[index].get('close')}")
                    self._act(run, definition, first, candles[index], exit_signal)
                elif exit_signal:
                    run.last_signal = "exit"
                    self._close(run, definition, first)
                stop.wait(poll)

            except Exception as error:  # noqa: BLE001 - a run must never take the process down
                run.status = "error"
                run.reason = str(error)
                run.note(f"ERROR: {type(error).__name__}: {error}")
                log.exception("Algo run %s failed", run.id)
                with app_cursor() as cursor:
                    cursor.execute(
                        "UPDATE strategy_runs SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
                        (str(error), now(), run.id),
                    )
                break

        if run.status == "running":
            run.status = "stopped"
            run.stopped_at = time.time()
        with app_cursor() as cursor:
            cursor.execute(
                "UPDATE strategy_runs SET status = ?, finished_at = ? WHERE id = ? AND finished_at IS NULL",
                (run.status, now(), run.id),
            )

    def _act(self, run: AlgoRun, definition: dict[str, Any], instrument: dict[str, Any],
             bar: dict[str, Any], exit_signal: bool) -> None:
        """Size and route one order, then place the protective legs if configured."""
        price = float(bar.get("close") or 0)
        if price <= 0:
            return
        risk = definition.get("risk") or {}
        equity = paper.capital(run.account_id) if run.mode == "paper" else None
        quantity = dsl.position_size(definition, equity or 0, price)
        if quantity <= 0:
            run.note("position size resolved to zero; skipped")
            return

        stop, target = dsl.stop_and_target(price, risk, "B")
        order = {
            "exchange_segment": instrument.get("exchange_segment", "nse_cm"),
            "product": (definition.get("universe") or [{}])[0].get("product", "MIS"),
            "trading_symbol": instrument.get("label") or instrument["instrument_token"],
            "instrument_token": instrument["instrument_token"],
            "transaction_type": "B", "order_type": "L" if stop else "MKT",
            "quantity": quantity, "price": stop if stop else price, "validity": "DAY",
        }

        try:
            if run.mode == "paper":
                result = paper.place_paper_order(order, account_id=run.account_id, strategy_run_id=run.id)
            else:
                result = live.place_order(
                    order, account_id=run.account_id, confirmation=live.LIVE_ORDER_PHRASE,
                    equity=equity, is_market_open=True, strategy_run_id=run.id,
                )
            run.orders_placed += 1
            run.note(f"order placed: {quantity} {order['trading_symbol']} ({run.mode})")
            if exit_signal:
                run.note("exit signal also fired on the entry bar; monitor closely")
        except (RuntimeError, live.OrderRejected) as error:
            run.orders_rejected += 1
            run.note(f"order rejected: {error}")

    def _close(self, run: AlgoRun, definition: dict[str, Any], instrument: dict[str, Any]) -> None:
        symbol = instrument.get("label") or instrument["instrument_token"]
        try:
            if run.mode == "paper":
                paper.close_position(account_id=run.account_id, trading_symbol=symbol, reason="algo exit signal")
                run.note(f"closed paper position in {symbol}")
            else:
                run.note("live exit requires an explicit closing order; none sent automatically")
        except Exception as error:  # noqa: BLE001
            run.note(f"close failed: {error}")


def _session_call(account_id: str):
    """Return a callable that invokes a Neo method for this account, or `None`."""
    from backend.broker import accounts
    from backend.broker.neo_auth import session_manager

    try:
        credentials = accounts.get_credentials(account_id)
    except Exception:  # noqa: BLE001
        credentials = None
    if credentials is None:
        return None

    def call(method: str, *args: Any, **kwargs: Any) -> Any:
        return session_manager.call(account_id, credentials, method, *args, **kwargs)

    return call


supervisor = AlgoSupervisor()


def instrument_label(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the strategy universe against the scrip master, for display."""
    resolved = []
    for item in definition.get("universe") or []:
        instrument = master.get_instrument(str(item["token"]), item.get("exchange_segment", "nse_cm"))
        resolved.append({
            "token": str(item["token"]),
            "exchange_segment": item.get("exchange_segment", "nse_cm"),
            "label": item.get("label") or (instrument or {}).get("name") or (instrument or {}).get("symbol") or str(item["token"]),
        })
    return resolved
