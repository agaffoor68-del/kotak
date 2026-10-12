"""Order service: the single entry point for every live order.

This is where the platform's guarantees are enforced, once, for both brokers:

1. the live-trading feature gate,
2. the typed confirmation phrase,
3. validation,
4. the risk engine (per account, before anything is sent),
5. routing to the chosen broker — ``kotak``, ``angel`` or ``both``,
6. recording the outcome in the ``orders`` table, tagged with the broker.

The routes and the algo engine both call :func:`place`, so an order placed by a
strategy and an order placed by hand travel the same path and are subject to
the same controls. Nothing here talks to an exchange directly; it only ever
calls a :class:`~backend.execution.brokers.BrokerAdapter`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from backend.broker import accounts
from backend.core.config import settings
from backend.core.database import app_cursor, now
from backend.risk.engine import RiskEngine, RiskViolation

from . import brokers as broker_registry
from .brokers import BrokerOrderRejected, BrokerUnavailable, NormalisedOrder
from .live import LIVE_ORDER_PHRASE, OrderRejected, _validate

log = logging.getLogger("alphatrade.execution.orders")


def _record_order(
    *,
    account_id: str,
    broker: str,
    order: dict[str, Any],
    mode: str,
    status: str,
    rejection: str | None = None,
    order_id: str | None = None,
    strategy_run_id: str | None = None,
) -> str:
    local_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO orders (id, account_id, broker, strategy_run_id, mode, broker_order_id,
                                exchange_segment, product, trading_symbol, instrument_token,
                                transaction_type, order_type, quantity, price, trigger_price,
                                status, rejection_reason, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                local_id, account_id, broker, strategy_run_id, mode, order_id,
                order["exchange_segment"], order["product"], order["trading_symbol"],
                order.get("instrument_token"), order["transaction_type"], order["order_type"],
                float(order["quantity"]), float(order.get("price") or 0),
                float(order.get("trigger_price") or 0), status, rejection, now(), now(),
            ),
        )
    return local_id


def _update_order_status(broker: str, account_id: str, order_id: str, status: str, **fields: Any) -> None:
    assignments = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now()]
    for column in ("price", "quantity"):
        if column in fields and fields[column] is not None:
            assignments.append(f"{column} = ?")
            params.append(float(fields[column]))
    params.extend([account_id, broker, order_id])
    with app_cursor() as cursor:
        cursor.execute(
            f"UPDATE orders SET {', '.join(assignments)} "
            "WHERE account_id = ? AND broker = ? AND broker_order_id = ?",
            params,
        )


def _guard_gates(confirmation: str) -> None:
    if not settings.enable_live_trading:
        raise OrderRejected(
            "DISABLED",
            "Live trading is disabled. Set ENABLE_LIVE_TRADING=1 to allow real orders.",
        )
    if confirmation != LIVE_ORDER_PHRASE:
        raise OrderRejected("CONFIRMATION", f'Type "{LIVE_ORDER_PHRASE}" to confirm a real order')


def place(
    order: dict[str, Any],
    *,
    account_id: str,
    broker: str | None = None,
    confirmation: str = "",
    equity: float | None = None,
    is_market_open: bool = True,
    strategy_run_id: str | None = None,
) -> dict[str, Any]:
    """Place one real order, routing it to the selected broker(s).

    Returns a single-broker result directly, or ``{"broker": "both", "legs": [...]}``
    when the signal was fanned out. A rejection on one leg does not cancel the
    other: each leg reports its own outcome, and the caller sees every failure.
    """
    _guard_gates(confirmation)
    _validate(order)

    try:
        targets = broker_registry.targets_for(account_id, broker)
    except BrokerUnavailable as error:
        raise OrderRejected("BROKER_ERROR", str(error)) from error

    # A fanned-out signal may be scaled down so a strategy is not silently
    # doubled in the book (DUAL_EXECUTION_SIZE, default 1.0 = full size each).
    size_factor = settings.dual_execution_size if len(targets) > 1 else 1.0

    legs: list[dict[str, Any]] = []
    for name, target_account in targets:
        leg_order = order
        if size_factor != 1.0:
            leg_order = {**order, "quantity": max(1.0, float(order["quantity"]) * size_factor)}
        legs.append(
            _place_one(
                leg_order, broker=name, account_id=target_account,
                equity=equity, is_market_open=is_market_open, strategy_run_id=strategy_run_id,
            )
        )

    if len(legs) == 1:
        return legs[0]
    return {
        "broker": "both",
        "status": "submitted",
        "legs": legs,
        "submitted_at": now(),
        "ok": all(leg["status"] not in {"rejected", "failed", "blocked"} for leg in legs),
    }


def _place_one(
    order: dict[str, Any],
    *,
    broker: str,
    account_id: str,
    equity: float | None,
    is_market_open: bool,
    strategy_run_id: str | None,
) -> dict[str, Any]:
    if accounts.get_credentials(account_id) is None:
        raise OrderRejected("NO_ACCOUNT", f"No {broker} account is configured for '{account_id}'")

    # Risk is checked per account, before anything reaches the exchange.
    try:
        RiskEngine(account_id).check_order(
            order, equity=equity, price=str(order.get("price") or 0), is_market_open=is_market_open,
        )
    except RiskViolation as violation:
        _record_order(account_id=account_id, broker=broker, order=order, mode="live",
                      status="blocked", rejection=violation.message)
        log.warning("Risk blocked a %s order for %s: %s", broker, account_id, violation.message)
        raise OrderRejected(violation.rule, violation.message, violation.detail) from violation

    try:
        adapter = broker_registry.adapter_for(broker, account_id)
    except BrokerUnavailable as error:
        raise OrderRejected("BROKER_ERROR", str(error)) from error

    try:
        result: NormalisedOrder = adapter.place(order)
    except BrokerOrderRejected as error:
        _record_order(account_id=account_id, broker=broker, order=order, mode="live",
                      status="failed", rejection=str(error))
        raise OrderRejected("BROKER_ERROR", str(error)) from error
    except Exception as error:  # noqa: BLE001 - a broker that throws must be recorded too
        _record_order(account_id=account_id, broker=broker, order=order, mode="live",
                      status="failed", rejection=str(error))
        raise

    if result.status == "rejected":
        _record_order(account_id=account_id, broker=broker, order=order, mode="live",
                      status="rejected", rejection=result.error)
        raise OrderRejected("REJECTED", f"{broker} rejected the order: {result.error}")

    local_id = _record_order(
        account_id=account_id, broker=broker, order=order, mode="live", status="open",
        order_id=result.order_id, strategy_run_id=strategy_run_id,
    )
    log.info("Live order %s submitted via %s for %s (%s)", local_id, broker, account_id, result.order_id)
    return {
        "id": local_id,
        "broker": broker,
        "account_id": account_id,
        "broker_order_id": result.order_id,
        "status": "open",
        "request": result.request,
        "response": result.response,
        "submitted_at": now(),
    }


def modify(
    *,
    account_id: str,
    order_id: str,
    broker: str | None = None,
    confirmation: str = "",
    local_order_id: str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Amend a working order on the broker that holds it."""
    if not settings.enable_live_trading:
        raise OrderRejected("DISABLED", "Live trading is disabled.")
    if confirmation != LIVE_ORDER_PHRASE:
        raise OrderRejected("CONFIRMATION", f'Type "{LIVE_ORDER_PHRASE}" to confirm')

    name = _resolve_order_broker(order_id, broker, local_order_id)
    target = _account_for_broker(name, account_id)
    adapter = broker_registry.adapter_for(name, target)
    try:
        result = adapter.modify(order_id=order_id, **changes)
    except BrokerOrderRejected as error:
        raise OrderRejected("BROKER_ERROR", str(error)) from error

    if result.status == "rejected":
        raise OrderRejected("REJECTED", f"{name} rejected the modification: {result.error}")

    _update_order_status(
        name, target, order_id, "modified",
        price=changes.get("price"), quantity=changes.get("quantity"),
    )
    return {
        "order_id": order_id, "broker": name, "status": "modified",
        "response": result.response, "modified_at": now(),
    }


def cancel(
    *,
    account_id: str,
    order_id: str,
    broker: str | None = None,
    confirmation: str = "",
    local_order_id: str | None = None,
) -> dict[str, Any]:
    """Cancel a working order on the broker that holds it."""
    if not settings.enable_live_trading:
        raise OrderRejected("DISABLED", "Live trading is disabled.")
    if confirmation != LIVE_ORDER_PHRASE:
        raise OrderRejected("CONFIRMATION", f'Type "{LIVE_ORDER_PHRASE}" to confirm')

    name = _resolve_order_broker(order_id, broker, local_order_id)
    target = _account_for_broker(name, account_id)
    adapter = broker_registry.adapter_for(name, target)
    try:
        result = adapter.cancel(order_id=order_id)
    except BrokerOrderRejected as error:
        raise OrderRejected("BROKER_ERROR", str(error)) from error

    if result.status == "rejected":
        raise OrderRejected("REJECTED", f"{name} rejected the cancellation: {result.error}")

    _update_order_status(name, target, order_id, "cancelled")
    return {
        "order_id": order_id, "broker": name, "status": "cancelled",
        "response": result.response, "cancelled_at": now(),
    }


def _resolve_order_broker(order_id: str, broker: str | None, local_order_id: str | None) -> str:
    """Work out which broker holds an order.

    An explicit choice wins; otherwise the broker is read back from the order
    this platform recorded, so a modify/cancel from the UI needs no broker
    argument at all.
    """
    if broker:
        return broker_registry.normalise_broker(broker)

    with app_cursor() as cursor:
        if local_order_id:
            cursor.execute("SELECT broker FROM orders WHERE id = ?", (local_order_id,))
        else:
            cursor.execute(
                "SELECT broker FROM orders WHERE broker_order_id = ? ORDER BY created_at DESC LIMIT 1",
                (order_id,),
            )
        row = cursor.fetchone()
    if row is not None and row["broker"]:
        return str(row["broker"]).lower()
    raise OrderRejected(
        "UNKNOWN_BROKER",
        f"Order {order_id} is not in the local book; specify which broker it belongs to.",
    )


def _account_for_broker(broker: str, fallback: str) -> str:
    """The account to act on for a broker, defaulting to the caller's own."""
    if accounts.broker_of(fallback) == broker:
        return fallback
    return accounts.default_account_id(broker) or fallback


def portfolio(account_id: str, broker: str | None = None) -> dict[str, Any]:
    """Live holdings, positions, limits and books from the selected broker."""
    name = broker_registry.normalise_broker(broker or _broker_for_account(account_id))
    target = _account_for_broker(name, account_id)
    adapter = broker_registry.adapter_for(name, target)
    try:
        return {"broker": name, "account_id": target, "as_of": now(), **adapter.portfolio()}
    except BrokerOrderRejected as error:
        raise OrderRejected("BROKER_ERROR", str(error)) from error


def margin_required(order: dict[str, Any], account_id: str, broker: str | None = None) -> Any:
    name = broker_registry.normalise_broker(broker or _broker_for_account(account_id))
    target = _account_for_broker(name, account_id)
    adapter = broker_registry.adapter_for(name, target)
    try:
        return adapter.margin(order)
    except BrokerOrderRejected as error:
        raise OrderRejected("BROKER_ERROR", str(error)) from error


def _broker_for_account(account_id: str) -> str:
    return accounts.broker_of(account_id) or broker_registry.KOTAK


def reconnect(account_id: str, broker: str | None = None) -> dict[str, Any]:
    """Force a fresh login on the selected broker."""
    name = broker_registry.normalise_broker(broker or _broker_for_account(account_id))
    if name == broker_registry.ANGEL:
        from backend.angel.auth import angel_session_manager

        angel_session_manager.invalidate(account_id)
        return angel_session_manager.health(account_id)
    from backend.broker.neo_auth import session_manager

    session_manager.invalidate(account_id)
    return session_manager.health(account_id)
