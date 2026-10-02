"""Live order execution through Kotak Neo.

Every live order must clear, in order:

1. the feature gate (`ENABLE_LIVE_TRADING`),
2. a typed confirmation phrase,
3. the risk engine,
4. the broker itself.

The confirmation is deliberately required server-side: it cannot be satisfied by
a browser that merely replayed a request.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from backend.broker import accounts
from backend.broker.neo_auth import NeoAuthError, extract_error, session_manager
from backend.core.config import settings
from backend.core.database import app_cursor, now
from backend.risk.engine import RiskEngine, RiskViolation

log = logging.getLogger("alphatrade.execution")

#: Exact phrase a client must send to unlock a live order.
LIVE_ORDER_PHRASE = "PLACE LIVE ORDER"

VALID_SEGMENTS = {"nse_cm", "bse_cm", "nse_fo", "bse_fo", "cde_fo", "bcs-fo", "mcx_fo"}
VALID_PRODUCTS = {"CNC", "MIS", "NRML", "CO", "BO", "MTF", "INTRADAY"}
VALID_ORDER_TYPES = {"L", "MKT", "SL", "SL-M"}
VALID_VALIDITY = {"DAY", "IOC", "GTC", "EOS", "GTD"}


class OrderRejected(RuntimeError):
    """The order was refused before or during submission."""

    def __init__(self, reason: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


def _broker_call(account_id: str, method: str, *args: Any, **kwargs: Any) -> Any:
    credentials = accounts.get_credentials(account_id)
    if credentials is None:
        raise OrderRejected("NO_ACCOUNT", f"No Kotak account is configured for '{account_id}'")
    try:
        return session_manager.call(account_id, credentials, method, *args, **kwargs)
    except NeoAuthError as error:
        raise OrderRejected("BROKER_ERROR", str(error)) from error


def _record_order(
    *, account_id: str, order: dict[str, Any], mode: str, status: str,
    response: Any = None, rejection: str | None = None, order_id: str | None = None,
    strategy_run_id: str | None = None,
) -> str:
    local_id = uuid.uuid4().hex
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO orders (id, account_id, strategy_run_id, mode, broker_order_id, exchange_segment,
                                product, trading_symbol, instrument_token, transaction_type, order_type,
                                quantity, price, trigger_price, status, rejection_reason, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                local_id, account_id, strategy_run_id, mode, order_id,
                order["exchange_segment"], order["product"], order["trading_symbol"],
                order.get("instrument_token"), order["transaction_type"], order["order_type"],
                float(order["quantity"]), float(order.get("price") or 0),
                float(order.get("trigger_price") or 0), status, rejection, now(), now(),
            ),
        )
    return local_id


def _validate(order: dict[str, Any]) -> None:
    if order.get("exchange_segment") not in VALID_SEGMENTS:
        raise OrderRejected("VALIDATION", f"Unsupported exchange_segment: {order.get('exchange_segment')}")
    if order.get("product") not in VALID_PRODUCTS:
        raise OrderRejected("VALIDATION", f"Unsupported product: {order.get('product')}")
    if order.get("order_type") not in VALID_ORDER_TYPES:
        raise OrderRejected("VALIDATION", f"Unsupported order type: {order.get('order_type')}")
    if order.get("validity", "DAY") not in VALID_VALIDITY:
        raise OrderRejected("VALIDATION", f"Unsupported validity: {order.get('validity')}")
    if str(order.get("transaction_type")) not in {"B", "S"}:
        raise OrderRejected("VALIDATION", "transaction_type must be B or S")
    try:
        quantity = float(order.get("quantity") or 0)
    except (TypeError, ValueError):
        raise OrderRejected("VALIDATION", "quantity must be a number") from None
    if quantity <= 0:
        raise OrderRejected("VALIDATION", "quantity must be greater than zero")
    if order["order_type"] in {"L", "SL"} and float(order.get("price") or 0) <= 0:
        raise OrderRejected("VALIDATION", "Limit and stop-limit orders require a positive price")
    if order["order_type"] in {"SL", "SL-M"} and float(order.get("trigger_price") or 0) <= 0:
        raise OrderRejected("VALIDATION", "Stop-loss orders require a positive trigger price")
    if not str(order.get("trading_symbol") or "").strip():
        raise OrderRejected("VALIDATION", "trading_symbol is required, for example RELIANCE-EQ")


def build_payload(order: dict[str, Any]) -> dict[str, Any]:
    """Translate a request into the exact kwargs Kotak's `place_order` expects."""
    order_type = order["order_type"]
    return {
        "exchange_segment": order["exchange_segment"],
        "product": order["product"],
        "trading_symbol": str(order["trading_symbol"]).strip().upper(),
        "transaction_type": order["transaction_type"],
        "order_type": order_type,
        "quantity": str(order["quantity"]),
        "price": "0" if order_type in {"MKT", "SL-M"} else str(order.get("price") or 0),
        "trigger_price": "0" if order_type not in {"SL", "SL-M"} else str(order.get("trigger_price") or 0),
        "validity": order.get("validity", "DAY"),
        "disclosed_quantity": str(order.get("disclosed_quantity") or "0"),
        "amo": "YES" if str(order.get("amo", "NO")).upper() == "YES" else "NO",
        "market_protection": str(order.get("market_protection") or "0"),
        "pf": "N",
        "tag": str(order.get("tag") or "alphatrade"),
        "scrip_token": None, "square_off_type": None, "stop_loss_type": None,
        "stop_loss_value": None, "square_off_value": None, "last_traded_price": None,
        "trailing_stop_loss": None, "trailing_sl_value": None,
    }


def place_order(
    order: dict[str, Any],
    *,
    account_id: str,
    confirmation: str = "",
    equity: float | None = None,
    is_market_open: bool = True,
    strategy_run_id: str | None = None,
) -> dict[str, Any]:
    """Submit a real order to Kotak Neo."""
    if not settings.enable_live_trading:
        raise OrderRejected(
            "DISABLED",
            "Live trading is disabled. Set ENABLE_LIVE_TRADING=1 to allow real orders.",
        )
    if confirmation != LIVE_ORDER_PHRASE:
        raise OrderRejected("CONFIRMATION", f'Type "{LIVE_ORDER_PHRASE}" to confirm a real order')

    _validate(order)
    payload = build_payload(order)

    # Risk is checked before anything is sent to the exchange.
    try:
        RiskEngine(account_id).check_order(
            order, equity=equity, price=payload["price"], is_market_open=is_market_open,
        )
    except RiskViolation as violation:
        _record_order(account_id=account_id, order=order, mode="live", status="blocked",
                      rejection=violation.message)
        log.warning("Risk blocked an order for %s: %s", account_id, violation.message)
        raise OrderRejected(violation.rule, violation.message, violation.detail) from violation

    try:
        response = _broker_call(account_id, "place_order", **payload)
    except Exception as error:
        _record_order(account_id=account_id, order=order, mode="live", status="failed",
                      rejection=str(error))
        raise

    failure = extract_error(response)
    if failure:
        _record_order(account_id=account_id, order=order, mode="live", status="rejected", rejection=failure)
        raise OrderRejected("REJECTED", f"Kotak rejected the order: {failure}")

    broker_order_id = _order_id_from(response)
    local_id = _record_order(
        account_id=account_id, order=order, mode="live", status="open",
        response=response, order_id=broker_order_id, strategy_run_id=strategy_run_id,
    )
    log.info("Live order %s submitted for %s (%s)", local_id, account_id, broker_order_id)
    return {
        "id": local_id, "broker_order_id": broker_order_id, "status": "open",
        "request": payload, "response": response, "submitted_at": now(),
    }


def _order_id_from(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("orderId", "order_id", "orderNo", "order_number"):
        value = response.get(key)
        if value:
            return str(value)
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("orderId", "order_id", "orderNo"):
            if data.get(key):
                return str(data[key])
    return None


def modify_order(
    *, account_id: str, order_id: str, price: str, quantity: str,
    order_type: str, validity: str = "DAY", trigger_price: str = "0",
    disclosed_quantity: str = "0", confirmation: str = "",
) -> dict[str, Any]:
    """Amend a working order."""
    if not settings.enable_live_trading:
        raise OrderRejected("DISABLED", "Live trading is disabled.")
    if confirmation != LIVE_ORDER_PHRASE:
        raise OrderRejected("CONFIRMATION", f'Type "{LIVE_ORDER_PHRASE}" to confirm')
    response = _broker_call(
        account_id, "modify_order", order_id=order_id, price=str(price), quantity=str(quantity),
        order_type=order_type, validity=validity, trigger_price=str(trigger_price),
        disclosed_quantity=str(disclosed_quantity),
    )
    failure = extract_error(response)
    if failure:
        raise OrderRejected("REJECTED", f"Kotak rejected the modification: {failure}")
    with app_cursor() as cursor:
        cursor.execute(
            "UPDATE orders SET price = ?, quantity = ?, updated_at = ? WHERE account_id = ? AND broker_order_id = ?",
            (float(price), float(quantity), now(), account_id, order_id),
        )
    return {"order_id": order_id, "status": "modified", "response": response, "modified_at": now()}


def cancel_order(*, account_id: str, order_id: str, confirmation: str = "") -> dict[str, Any]:
    """Cancel a working order. `isVerify=True` makes Kotak refuse if it already traded."""
    if not settings.enable_live_trading:
        raise OrderRejected("DISABLED", "Live trading is disabled.")
    if confirmation != LIVE_ORDER_PHRASE:
        raise OrderRejected("CONFIRMATION", f'Type "{LIVE_ORDER_PHRASE}" to confirm')
    response = _broker_call(account_id, "cancel_order", order_id=order_id, isVerify=True)
    failure = extract_error(response)
    if failure:
        raise OrderRejected("REJECTED", f"Kotak rejected the cancellation: {failure}")
    with app_cursor() as cursor:
        cursor.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE account_id = ? AND broker_order_id = ?",
            (now(), account_id, order_id),
        )
    return {"order_id": order_id, "status": "cancelled", "response": response, "cancelled_at": now()}


# ------------------------------------------------------------- portfolio reads


def portfolio(account_id: str) -> dict[str, Any]:
    """Live holdings, positions, limits and books straight from Kotak."""
    return {
        "as_of": now(),
        "holdings": _broker_call(account_id, "holdings"),
        "positions": _broker_call(account_id, "positions"),
        "limits": _broker_call(account_id, "limits"),
        "orders": _broker_call(account_id, "order_report"),
        "trades": _broker_call(account_id, "trade_report"),
    }


def margin_required(order: dict[str, Any], account_id: str) -> Any:
    """Margin Kotak will require for an order, before it is placed."""
    return _broker_call(
        account_id, "margin_required",
        exchange_segment=order["exchange_segment"], price=str(order.get("price") or 0),
        order_type=order["order_type"], product=order["product"], quantity=str(order["quantity"]),
        instrument_token=str(order.get("instrument_token") or ""),
        transaction_type=order["transaction_type"],
    )


def reconnect(account_id: str) -> dict[str, Any]:
    """Force a fresh Neo login."""
    session_manager.invalidate(account_id)
    return session_manager.health(account_id)
