"""Broker adapters: one interface, two implementations.

The rest of the platform (risk engine, algo engine, order routes) deals in a
single high-level order dict. This module turns that into the exact call each
broker expects and normalises the answer back into one shape, so *where* an
order goes is a routing decision rather than a second code path.

::

    order  ->  BrokerAdapter.place(order)  ->  NormalisedOrder

Two adapters exist:

* :class:`KotakAdapter`   — Kotak Neo (``place_order`` / ``modify_order`` /
  ``cancel_order``), symbols passed as ``trading_symbol`` + ``scrip_token``.
* :class:`AngelAdapter`   — Angel One SmartAPI (``placeOrder`` etc.), symbols
  passed as ``tradingsymbol`` + ``symboltoken``, with Angel's own
  ``exchange``/``producttype``/``ordertype`` vocabularies.

Kotak's vocabulary (``nse_cm``, ``MIS``, ``L``) is treated as the canonical
one, since that is what the existing API and UI already use. Angel's adapter
translates between the two at the boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.core.config import settings

log = logging.getLogger("alphatrade.execution.brokers")

KOTAK = "kotak"
ANGEL = "angel"


class BrokerUnavailable(RuntimeError):
    """The named broker is not configured or not enabled in this deployment."""


class BrokerOrderRejected(RuntimeError):
    """The broker refused the order."""


@dataclass
class NormalisedOrder:
    """The one result shape every broker returns to callers."""

    broker: str
    order_id: str | None
    status: str
    request: dict[str, Any] = field(default_factory=dict)
    response: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "broker_order_id": self.order_id,
            "status": self.status,
            "request": self.request,
            "response": self.response,
            "error": self.error,
        }


class BrokerAdapter(Protocol):
    """What the execution layer needs from a broker."""

    name: str

    def place(self, order: dict[str, Any]) -> NormalisedOrder: ...
    def modify(self, *, order_id: str, **changes: Any) -> NormalisedOrder: ...
    def cancel(self, *, order_id: str, **kwargs: Any) -> NormalisedOrder: ...
    def portfolio(self) -> dict[str, Any]: ...
    def margin(self, order: dict[str, Any]) -> Any: ...


# --------------------------------------------------------------------- Kotak


class KotakAdapter:
    """Execution through Kotak Neo, exactly as the platform always did."""

    name = KOTAK

    def __init__(self, account_id: str) -> None:
        from backend.broker import accounts

        self.account_id = account_id
        self._credentials = accounts.get_credentials(account_id)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        from backend.broker.neo_auth import NeoAuthError, session_manager

        try:
            return session_manager.call(self.account_id, self._credentials, method, *args, **kwargs)
        except NeoAuthError as error:
            raise BrokerOrderRejected(str(error)) from error

    def place(self, order: dict[str, Any]) -> NormalisedOrder:
        from backend.broker.neo_auth import extract_error
        from backend.execution.live import build_payload, _order_id_from

        payload = build_payload(order)
        response = self._call("place_order", **payload)
        failure = extract_error(response)
        if failure:
            return NormalisedOrder(self.name, None, "rejected", payload, response, failure)
        return NormalisedOrder(self.name, _order_id_from(response), "open", payload, response)

    def modify(self, *, order_id: str, **changes: Any) -> NormalisedOrder:
        from backend.broker.neo_auth import extract_error

        payload = {
            "order_id": order_id,
            "price": str(changes.get("price", "0")),
            "quantity": str(changes.get("quantity", "0")),
            "order_type": changes.get("order_type", "L"),
            "validity": changes.get("validity", "DAY"),
            "trigger_price": str(changes.get("trigger_price", "0")),
            "disclosed_quantity": str(changes.get("disclosed_quantity", "0")),
        }
        response = self._call("modify_order", **payload)
        failure = extract_error(response)
        if failure:
            return NormalisedOrder(self.name, order_id, "rejected", payload, response, failure)
        return NormalisedOrder(self.name, order_id, "modified", payload, response)

    def cancel(self, *, order_id: str, **kwargs: Any) -> NormalisedOrder:
        from backend.broker.neo_auth import extract_error

        payload = {"order_id": order_id, "isVerify": bool(kwargs.get("is_verify", True))}
        response = self._call("cancel_order", order_id=order_id, isVerify=payload["isVerify"])
        failure = extract_error(response)
        if failure:
            return NormalisedOrder(self.name, order_id, "rejected", payload, response, failure)
        return NormalisedOrder(self.name, order_id, "cancelled", payload, response)

    def portfolio(self) -> dict[str, Any]:
        return {
            "holdings": self._call("holdings"),
            "positions": self._call("positions"),
            "limits": self._call("limits"),
            "orders": self._call("order_report"),
            "trades": self._call("trade_report"),
        }

    def margin(self, order: dict[str, Any]) -> Any:
        return self._call(
            "margin_required",
            exchange_segment=order["exchange_segment"], price=str(order.get("price") or 0),
            order_type=order["order_type"], product=order["product"], quantity=str(order["quantity"]),
            instrument_token=str(order.get("instrument_token") or ""),
            transaction_type=order["transaction_type"],
        )


# --------------------------------------------------------------------- Angel


class AngelAdapter:
    """Execution through Angel One SmartAPI.

    Kotak's vocabulary is the canonical one on this platform, so this adapter
    translates segment/product/order-type names into Angel's and resolves the
    numeric instrument token into Angel's ``EXCHG:TRADINGSYMBOL`` form.
    """

    name = ANGEL

    def __init__(self, account_id: str) -> None:
        from backend.broker import accounts

        self.account_id = account_id
        self._credentials = accounts.get_credentials(account_id)

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        from backend.angel.auth import AngelAuthError, angel_session_manager

        try:
            return angel_session_manager.call(self.account_id, self._credentials, method, path, **kwargs)
        except AngelAuthError as error:
            raise BrokerOrderRejected(str(error)) from error

    def _symbol_for(self, order: dict[str, Any]) -> tuple[str, str]:
        from backend.angel import instruments

        token = str(order.get("instrument_token") or "").strip()
        segment = order["exchange_segment"]
        if token:
            resolved = instruments.to_smartapi([(token, segment)])
            if resolved:
                return resolved[0][0], resolved[0][1].split(":", 1)[1]
        # Fall back to the trading symbol the caller supplied.
        symbol = str(order.get("trading_symbol") or "").strip().upper()
        if ":" in symbol:
            return token, symbol.split(":", 1)[1]
        return token, symbol

    def place(self, order: dict[str, Any]) -> NormalisedOrder:
        from backend.angel import orders as angel_orders

        token, trading_symbol = self._symbol_for(order)
        if not token:
            raise BrokerOrderRejected(
                f"No Angel One instrument token for {order.get('trading_symbol')!r} in "
                f"{order.get('exchange_segment')}. Sync the Angel instrument master."
            )
        result = angel_orders.place(self._call, order, symbol_token=token, trading_symbol=trading_symbol)
        failure = angel_orders.extract_error(result["response"])
        if failure:
            return NormalisedOrder(self.name, None, "rejected", result["request"], result["response"], failure)
        return NormalisedOrder(self.name, result["order_id"], "open", result["request"], result["response"])

    def modify(self, *, order_id: str, **changes: Any) -> NormalisedOrder:
        from backend.angel import orders as angel_orders

        result = angel_orders.modify(
            self._call,
            order_id=order_id,
            variety=changes.get("variety", "NORMAL"),
            order_type=angel_orders.angel_order_type(changes.get("order_type", "L")),
            product_type=angel_orders.angel_product(changes.get("product", "MIS")),
            duration=angel_orders.angel_validity(changes.get("validity", "DAY")),
            price=str(changes.get("price", "0")),
            quantity=str(changes.get("quantity", "0")),
            trigger_price=str(changes.get("trigger_price", "0")),
        )
        failure = angel_orders.extract_error(result["response"])
        if failure:
            return NormalisedOrder(self.name, order_id, "rejected", result["request"], result["response"], failure)
        return NormalisedOrder(self.name, order_id, "modified", result["request"], result["response"])

    def cancel(self, *, order_id: str, **kwargs: Any) -> NormalisedOrder:
        from backend.angel import orders as angel_orders

        result = angel_orders.cancel(self._call, order_id=order_id, variety=kwargs.get("variety", "NORMAL"))
        failure = angel_orders.extract_error(result["response"])
        if failure:
            return NormalisedOrder(self.name, order_id, "rejected", result["request"], result["response"], failure)
        return NormalisedOrder(self.name, order_id, "cancelled", result["request"], result["response"])

    def portfolio(self) -> dict[str, Any]:
        from backend.angel import orders as angel_orders

        return {
            "holdings": angel_orders.holdings(self._call),
            "positions": angel_orders.positions(self._call),
            "limits": angel_orders.limits(self._call),
            "orders": angel_orders.order_book(self._call),
            "trades": angel_orders.trade_book(self._call),
        }

    def margin(self, order: dict[str, Any]) -> Any:
        from backend.angel import orders as angel_orders

        return angel_orders.build_payload(
            order, symbol_token=str(order.get("instrument_token") or ""),
            trading_symbol=str(order.get("trading_symbol") or ""),
        )


# ------------------------------------------------------------------- registry


def enabled_brokers() -> list[str]:
    """Brokers this deployment will route to, in order of preference."""
    configured = [name for name in settings.execution_brokers if name in (KOTAK, ANGEL)]
    return configured or [KOTAK]


def is_enabled(name: str) -> bool:
    return str(name).lower() in enabled_brokers()


def adapter_for(broker: str, account_id: str) -> BrokerAdapter:
    """Build the adapter for `broker`, refusing one that is not enabled."""
    name = str(broker or "").strip().lower()
    if name == KOTAK:
        if not is_enabled(KOTAK):
            raise BrokerUnavailable("Kotak execution is not enabled in EXECUTION_BROKERS")
        return KotakAdapter(account_id)
    if name == ANGEL:
        if not is_enabled(ANGEL):
            raise BrokerUnavailable("Angel One execution is not enabled in EXECUTION_BROKERS")
        return AngelAdapter(account_id)
    raise BrokerUnavailable(f"Unknown execution broker '{broker}'. Expected kotak or angel.")


def targets_for(account_id: str, broker: str | None = None) -> list[tuple[str, str]]:
    """Resolve a routing choice into `(broker, account_id)` pairs.

    ``None`` uses ``DEFAULT_EXECUTION_BROKER``; ``"both"`` fans the order out to
    every enabled broker, each resolved to *its own* account (an Angel order
    cannot go to a Kotak client code or vice versa).
    """
    from backend.broker import accounts

    choice = str(broker or settings.default_execution_broker or KOTAK).strip().lower()
    if choice == "both":
        pairs: list[tuple[str, str]] = []
        for name in enabled_brokers():
            target = accounts.default_account_id(name) or account_id
            pairs.append((name, target))
        return pairs
    name = normalise_broker(choice)
    if name == ANGEL:
        return [(ANGEL, accounts.default_account_id(ANGEL) or account_id)]
    return [(KOTAK, account_id)]


def normalise_broker(broker: str) -> str:
    value = str(broker or "").strip().lower()
    if value not in (KOTAK, ANGEL):
        raise BrokerUnavailable(f"Unknown broker '{broker}'. Expected kotak or angel.")
    return value
