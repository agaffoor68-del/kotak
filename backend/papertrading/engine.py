"""Paper trading: the same strategy engine, without the exchange.

Fills are simulated against *live* Kotak quotes, so paper results track the real
market rather than a hypothetical one. A market order fills at the current bid
or ask; a limit order fills only when the live price trades through it.

The engine is intentionally not allowed to reach :mod:`backend.execution.live`.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from backend.core.database import app_cursor, now
from backend.execution.charges import compute as compute_charges
from backend.marketdata.quotes import quote_engine
from backend.risk.engine import RiskEngine, RiskViolation

log = logging.getLogger("alphatrade.paper")

DEFAULT_PAPER_CAPITAL = 500_000.0


def capital(account_id: str) -> float:
    """Current paper equity: starting capital plus realised P&L."""
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) AS realised FROM trades WHERE account_id = ? AND mode = 'paper'",
            (account_id,),
        )
        realised = float(cursor.fetchone()["realised"] or 0)
        cursor.execute(
            "SELECT equity FROM equity_curve WHERE account_id = ? AND mode = 'paper' ORDER BY ts DESC LIMIT 1",
            (account_id,),
        )
        row = cursor.fetchone()
    if row is not None:
        return float(row["equity"])
    return DEFAULT_PAPER_CAPITAL + realised


def _live_price(order: dict[str, Any]) -> tuple[float | None, str]:
    """Resolve a fill price from the live quote cache.

    Returns `(price, side_of_book)` where the side tells the simulation which way
    to cross the spread.
    """
    token = order.get("instrument_token")
    segment = order.get("exchange_segment") or "nse_cm"
    if not token:
        return None, "last"
    quote = quote_engine.cached(str(token), segment)
    if quote is None or quote.last is None:
        return None, "last"
    is_buy = str(order.get("transaction_type")) == "B"
    if is_buy and quote.ask is not None:
        return quote.ask, "ask"
    if not is_buy and quote.bid is not None:
        return quote.bid, "bid"
    return quote.last, "last"


def place_paper_order(
    order: dict[str, Any],
    *,
    account_id: str,
    strategy_run_id: str | None = None,
    enforce_risk: bool = True,
) -> dict[str, Any]:
    """Simulate an order against live quotes.

    Fails rather than guessing when no live price is available — a paper trade at
    an invented price would make the whole journal worthless.
    """
    order = dict(order)
    order.setdefault("order_type", "MKT")
    order.setdefault("validity", "DAY")
    order.setdefault("product", "MIS")
    order.setdefault("price", 0)
    order.setdefault("trigger_price", 0)
    is_buy = str(order.get("transaction_type")) == "B"
    order_type = order["order_type"]
    quantity = float(order.get("quantity") or 0)
    if quantity <= 0:
        raise ValueError("quantity must be greater than zero")

    live_price, book_side = _live_price(order)

    if live_price is None:
        raise RuntimeError(
            "No live Kotak quote is available for this instrument, so the paper order "
            "cannot be filled at a real price. Subscribe to the feed and retry."
        )

    if order_type in {"L", "SL"}:
        limit = float(order.get("price") or 0)
        if limit <= 0:
            raise ValueError("Limit orders require a positive price")
        marketable = live_price <= limit if is_buy else live_price >= limit
        if not marketable:
            return _store_pending(order, account_id, strategy_run_id, live_price)
        fill_price = limit
    else:
        fill_price = live_price

    notional = fill_price * quantity
    if enforce_risk:
        try:
            RiskEngine(account_id).check_order(
                order, equity=capital(account_id), price=fill_price, is_market_open=True
            )
        except RiskViolation as violation:
            raise RuntimeError(f"Risk blocked this paper order: {violation.message}") from violation

    charges = compute_charges(
        side=order["transaction_type"], segment=order["exchange_segment"],
        product=order["product"], quantity=quantity, price=fill_price,
        is_option=order.get("exchange_segment") in {"nse_fo", "bse_fo"},
    )

    local_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO orders (id, account_id, strategy_run_id, mode, exchange_segment, product,
                                trading_symbol, instrument_token, transaction_type, order_type,
                                quantity, price, average_price, status, created_at, updated_at)
            VALUES (?,?,?,'paper',?,?,?,?,?,?,?,?,?,'complete',?,?)
            """,
            (local_id, account_id, strategy_run_id, order["exchange_segment"], order["product"],
             str(order["trading_symbol"]).upper(), order.get("instrument_token"),
             order["transaction_type"], order_type, quantity, fill_price, fill_price, now(), now()),
        )
        cursor.execute(
            """
            INSERT INTO trades (id, account_id, strategy_run_id, mode, order_id, symbol, side, quantity,
                                entry_price, entry_time, reason)
            VALUES (?,?,?,'paper',?,?,?,?,?,?,?)
            """,
            (uuid.uuid4().hex, account_id, strategy_run_id, local_id, str(order["trading_symbol"]).upper(),
             order["transaction_type"], quantity, fill_price, time.time(),
             f"paper {order_type} fill at {book_side}"),
        )

    _mark_equity(account_id)
    log.info("Paper order filled: %s %s %s @ %s", order["transaction_type"], quantity, order["trading_symbol"], fill_price)
    return {
        "id": local_id, "status": "complete", "fill_price": fill_price, "quantity": quantity,
        "book_side": book_side, "charges": charges.to_dict(), "filled_at": now(),
    }


def _store_pending(order: dict[str, Any], account_id: str, strategy_run_id: str | None, live_price: float) -> dict[str, Any]:
    """Record a limit order the market has not reached yet."""
    local_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO orders (id, account_id, strategy_run_id, mode, exchange_segment, product,
                                trading_symbol, instrument_token, transaction_type, order_type, quantity,
                                price, status, created_at, updated_at)
            VALUES (?,?,?,'paper',?,?,?,?,?,?,?,?, 'open', ?, ?)
            """,
            (local_id, account_id, strategy_run_id, order["exchange_segment"], order["product"],
             str(order["trading_symbol"]).upper(), order.get("instrument_token"),
             order["transaction_type"], order["order_type"], float(order["quantity"]),
             float(order.get("price") or 0), now(), now()),
        )
    return {
        "id": local_id, "status": "open", "reason": "limit not reached",
        "limit_price": float(order.get("price") or 0), "live_price": live_price,
    }


def close_position(
    *, account_id: str, trading_symbol: str, quantity: float | None = None,
    reason: str = "manual close", price: float | None = None,
) -> dict[str, Any]:
    """Close a paper position, realising P&L at the live price."""
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT * FROM trades
            WHERE account_id = ? AND mode = 'paper' AND symbol = ? AND exit_price IS NULL
            ORDER BY entry_time ASC
            """,
            (account_id, trading_symbol.upper()),
        )
        open_trades = [dict(row) for row in cursor.fetchall()]
    if not open_trades:
        raise RuntimeError(f"No open paper position in {trading_symbol}")

    order = {
        "exchange_segment": open_trades[0].get("exchange_segment") or "nse_cm",
        "product": open_trades[0].get("product") or "MIS",
        "instrument_token": open_trades[0].get("instrument_token"),
        "trading_symbol": trading_symbol,
    }
    exit_price = price
    if exit_price is None:
        live_price, _ = _live_price(order)
        if live_price is None:
            raise RuntimeError("No live quote is available to close this position")
        exit_price = live_price

    closed = []
    remaining = quantity
    for trade in open_trades:
        if remaining is not None and remaining <= 0:
            break
        size = float(trade["quantity"]) if remaining is None else min(remaining, float(trade["quantity"]))
        side = trade["side"]
        direction = 1 if side == "B" else -1
        gross = (exit_price - float(trade["entry_price"])) * size * direction
        exit_side = "S" if side == "B" else "B"
        charges = compute_charges(
            side=exit_side, segment=order["exchange_segment"], product=order["product"],
            quantity=size, price=exit_price,
        )
        net = gross - charges.total

        with app_cursor() as cursor:
            cursor.execute(
                """
                UPDATE trades
                SET exit_price = ?, exit_time = ?, gross_pnl = ?, charges = ?, net_pnl = ?, reason = ?
                WHERE id = ?
                """,
                (exit_price, time.time(), gross, charges.total, net, reason, trade["id"]),
            )
        closed.append({"trade_id": trade["id"], "quantity": size, "exit_price": exit_price, "net_pnl": net})
        remaining = (remaining - size) if remaining is not None else None

    _mark_equity(account_id)
    return {"symbol": trading_symbol, "exit_price": exit_price, "closed": closed, "closed_at": now()}


def _mark_equity(account_id: str) -> float:
    """Snapshot current paper equity: cash plus mark-to-market open positions."""
    total = DEFAULT_PAPER_CAPITAL
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) AS realised FROM trades WHERE account_id = ? AND mode = 'paper'",
            (account_id,),
        )
        realised = float(cursor.fetchone()["realised"] or 0)
        cursor.execute(
            """
            SELECT * FROM trades
            WHERE account_id = ? AND mode = 'paper' AND exit_price IS NULL
            """,
            (account_id,),
        )
        open_trades = [dict(row) for row in cursor.fetchall()]

    unrealised = 0.0
    for trade in open_trades:
        live_price, _ = _live_price({
            "instrument_token": trade.get("instrument_token"),
            "exchange_segment": trade.get("exchange_segment") or "nse_cm",
        })
        if live_price is None:
            continue
        direction = 1 if trade["side"] == "B" else -1
        unrealised += (live_price - float(trade["entry_price"])) * float(trade["quantity"]) * direction

    equity = DEFAULT_PAPER_CAPITAL + realised + unrealised
    with app_cursor() as cursor:
        cursor.execute(
            "INSERT INTO equity_curve (id, account_id, mode, ts, equity, realised, unrealised) VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, account_id, "paper", time.time(), equity, realised, unrealised),
        )
    return equity


def mark_to_market(account_id: str) -> dict[str, Any]:
    """Current paper portfolio, marked against live prices."""
    equity = _mark_equity(account_id)
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM trades WHERE account_id = ? AND mode = 'paper' ORDER BY entry_time DESC LIMIT 200",
            (account_id,),
        )
        trades = [dict(row) for row in cursor.fetchall()]

    positions: dict[str, dict[str, Any]] = {}
    for trade in trades:
        if trade["exit_price"] is not None:
            continue
        symbol = trade["symbol"]
        position = positions.setdefault(symbol, {
            "symbol": symbol, "quantity": 0.0, "avg_entry": 0.0, "instrument_token": trade.get("instrument_token"),
        })
        signed = float(trade["quantity"]) * (1 if trade["side"] == "B" else -1)
        total_cost = position["avg_entry"] * position["quantity"] + float(trade["entry_price"]) * signed
        position["quantity"] += signed
        position["avg_entry"] = total_cost / position["quantity"] if position["quantity"] else 0.0

    for symbol, position in positions.items():
        live_price, _ = _live_price({
            "instrument_token": position["instrument_token"], "exchange_segment": "nse_cm",
        })
        position["last_price"] = live_price
        position["unrealised_pnl"] = (
            round((live_price - position["avg_entry"]) * position["quantity"], 2) if live_price else None
        )

    with app_cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) AS realised FROM trades WHERE account_id = ? AND mode = 'paper'",
            (account_id,),
        )
        realised = float(cursor.fetchone()["realised"] or 0)

    return {
        "equity": round(equity, 2),
        "starting_capital": DEFAULT_PAPER_CAPITAL,
        "realised_pnl": round(realised, 2),
        "unrealised_pnl": round(equity - DEFAULT_PAPER_CAPITAL - realised, 2),
        "open_positions": list(positions.values()),
        "marked_at": now(),
    }


def journal(account_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM trades WHERE account_id = ? AND mode = 'paper' ORDER BY entry_time DESC LIMIT ?",
            (account_id, max(1, min(limit, 1000))),
        )
        return [dict(row) for row in cursor.fetchall()]


def add_journal_note(
    *, account_id: str, trade_id: str | None, symbol: str, side: str, quantity: float,
    entry_price: float, exit_price: float | None = None, setup: str = "", emotion: str = "",
    followed_plan: bool = True, notes: str = "",
) -> str:
    entry_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO journal (id, account_id, trade_id, symbol, side, quantity, entry_price,
                                 exit_price, setup, emotion, followed_plan, notes, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (entry_id, account_id, trade_id, symbol.upper(), side, quantity, entry_price, exit_price,
             setup, emotion, 1 if followed_plan else 0, notes, now()),
        )
    return entry_id


def reset(account_id: str) -> dict[str, Any]:
    """Clear all paper activity for an account."""
    with app_cursor() as cursor:
        cursor.execute("DELETE FROM trades WHERE account_id = ? AND mode = 'paper'", (account_id,))
        cursor.execute("DELETE FROM orders WHERE account_id = ? AND mode = 'paper'", (account_id,))
        cursor.execute("DELETE FROM equity_curve WHERE account_id = ? AND mode = 'paper'", (account_id,))
    return {"reset": True, "account_id": account_id, "at": now()}
