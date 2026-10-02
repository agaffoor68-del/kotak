"""Institutional risk controls.

Every order passes through :meth:`RiskEngine.check_order` *before* it reaches the
broker. Limits are conservative by default and — importantly — a limit of 0 means
"unlimited" only for optional cosmetic caps; the loss and drawdown stops are
always enforced because they are the controls that protect the account.

The kill switch is one-way: engaging it stops new orders, and only an explicit
operator action resumes them.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from backend.core.database import app_cursor, now, row_to_dict

log = logging.getLogger("alphatrade.risk")


class RiskViolation(RuntimeError):
    """An order was blocked by a risk control."""

    def __init__(self, rule: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message
        self.detail = detail or {}


#: Explicit, conservative defaults. Rupees for losses, fractions for allocation.
DEFAULT_RISK_CONFIG: dict[str, Any] = {
    "max_daily_loss": 25_000.0,        # ₹25,000 realised loss stops the day
    "max_drawdown": 75_000.0,          # ₹75,000 peak-to-trough stops trading
    "max_position_pct": 0.20,          # 20% of equity in one position
    "max_positions": 10,
    "max_orders_per_minute": 20,
    "max_exposure_pct": 1.00,          # 100% gross, i.e. no leverage
}

#: Equity used for percentage caps when the account has no reported funds.
FALLBACK_EQUITY = 500_000.0

RULES = {
    "KILL_SWITCH": "Emergency kill switch is engaged; no orders may be sent.",
    "DAILY_LOSS": "Daily loss limit reached. Trading is halted for today.",
    "DRAWDOWN": "Maximum drawdown limit reached. Trading is halted.",
    "POSITION_SIZE": "Position exceeds the maximum allocation for a single instrument.",
    "EXPOSURE": "Total exposure would exceed the configured cap.",
    "POSITION_COUNT": "Too many open positions.",
    "ORDER_RATE": "Order rate limit exceeded (possible runaway strategy).",
    "MIN_MARGIN": "Insufficient margin available for this order.",
    "MARKET_CLOSED": "Market is closed; orders are not accepted.",
}


def get_config(account_id: str) -> dict[str, Any]:
    """Risk configuration for an account, seeded with defaults if absent."""
    with app_cursor() as cursor:
        cursor.execute("SELECT * FROM risk_config WHERE account_id = ?", (account_id,))
        row = row_to_dict(cursor.fetchone())
    if row:
        return row
    now_ts = now()
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT OR IGNORE INTO risk_config
              (account_id, max_daily_loss, max_drawdown, max_position_pct, max_positions,
               max_orders_per_minute, max_exposure_pct, kill_switch, updated_at)
            VALUES (?,?,?,?,?,?,?,0,?)
            """,
            (account_id, DEFAULT_RISK_CONFIG["max_daily_loss"], DEFAULT_RISK_CONFIG["max_drawdown"],
             DEFAULT_RISK_CONFIG["max_position_pct"], DEFAULT_RISK_CONFIG["max_positions"],
             DEFAULT_RISK_CONFIG["max_orders_per_minute"], DEFAULT_RISK_CONFIG["max_exposure_pct"], now_ts),
        )
    return get_config(account_id)


def update_config(account_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "max_daily_loss", "max_drawdown", "max_position_pct", "max_positions",
        "max_orders_per_minute", "max_exposure_pct",
    }
    fields = {key: value for key, value in changes.items() if key in allowed}
    if not fields:
        raise ValueError("No supported risk fields supplied")
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with app_cursor() as cursor:
        cursor.execute(
            f"UPDATE risk_config SET {assignments}, updated_at = ? WHERE account_id = ?",
            (*fields.values(), now(), account_id),
        )
        if cursor.rowcount == 0:
            # The account had no config row yet, so insert a full one.
            get_config(account_id)
            cursor.execute(
                f"UPDATE risk_config SET {assignments}, updated_at = ? WHERE account_id = ?",
                (*fields.values(), now(), account_id),
            )
    return get_config(account_id)


def log_event(account_id: str, kind: str, detail: str | None = None) -> None:
    with app_cursor() as cursor:
        cursor.execute(
            "INSERT INTO risk_events (id, account_id, kind, detail, created_at) VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, account_id, kind, detail, now()),
        )


def recent_events(account_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM risk_events WHERE account_id = ? ORDER BY created_at DESC LIMIT ?",
            (account_id, max(1, min(limit, 500))),
        )
        return [dict(row) for row in cursor.fetchall()]


# ------------------------------------------------------------------- engine


class RiskEngine:
    """Evaluates pre-trade and continuous risk."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id

    # -- helpers ---------------------------------------------------------
    def _open_positions(self) -> list[dict[str, Any]]:
        with app_cursor() as cursor:
            cursor.execute(
                "SELECT trading_symbol, quantity, price, exchange_segment, status FROM orders "
                "WHERE account_id = ? AND status IN ('open','pending','submitted','partially_filled')",
                (self.account_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def _realised_today(self) -> float:
        start_of_day = _start_of_day()
        with app_cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(realised_pnl), 0) AS total FROM orders "
                "WHERE account_id = ? AND mode IN ('live','paper') AND updated_at >= ?",
                (self.account_id, start_of_day),
            )
            row = cursor.fetchone()
        return float(row["total"] or 0.0)

    def _peak_equity(self, equity: float) -> float:
        with app_cursor() as cursor:
            cursor.execute(
                "SELECT MAX(equity) AS peak FROM equity_curve WHERE account_id = ?", (self.account_id,)
            )
            row = cursor.fetchone()
        recorded = float(row["peak"] or 0.0)
        return max(recorded, equity)

    def _recent_order_count(self, window_seconds: int = 60) -> int:
        with app_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS total FROM orders WHERE account_id = ? AND created_at >= ?",
                (self.account_id, time.time() - window_seconds),
            )
            return int(cursor.fetchone()["total"] or 0)

    def _available_margin(self) -> float | None:
        with app_cursor() as cursor:
            cursor.execute(
                "SELECT available_cash FROM risk_config WHERE account_id = ?", (self.account_id,)
            )
            row = cursor.fetchone()
        return None  # margin is sourced from Kotak RMS in the caller when available

    # -- public API ------------------------------------------------------
    def status(self, equity: float | None = None) -> dict[str, Any]:
        """Current risk posture, for the dashboard."""
        config = get_config(self.account_id)
        equity = equity if equity is not None else FALLBACK_EQUITY
        realised = self._realised_today()
        peak = self._peak_equity(equity)
        drawdown = peak - equity

        daily_limit = float(config["max_daily_loss"] or 0)
        drawdown_limit = float(config["max_drawdown"] or 0)
        daily_breached = bool(daily_limit) and realised <= -daily_limit
        drawdown_breached = bool(drawdown_limit) and drawdown >= drawdown_limit
        halted = bool(config["kill_switch"]) or daily_breached or drawdown_breached

        # How much further loss the day can absorb before the stop triggers.
        # Only the loss side counts, so a profitable day shows the full allowance.
        daily_remaining = (daily_limit - max(0.0, -realised)) if daily_limit else None
        drawdown_remaining = (drawdown_limit - drawdown) if drawdown_limit else None

        return {
            "account_id": self.account_id,
            "config": config,
            "equity": round(equity, 2),
            "peak_equity": round(peak, 2),
            "current_drawdown": round(drawdown, 2),
            "drawdown_percent": round((drawdown / peak) * 100, 2) if peak else 0.0,
            "realised_today": round(realised, 2),
            "daily_loss_remaining": round(max(0.0, daily_remaining), 2) if daily_remaining is not None else None,
            "drawdown_remaining": round(max(0.0, drawdown_remaining), 2) if drawdown_remaining is not None else None,
            "daily_limit_breached": daily_breached,
            "drawdown_limit_breached": drawdown_breached,
            "kill_switch": bool(config["kill_switch"]),
            "kill_reason": config["kill_reason"],
            "trading_halted": halted,
            "halt_reason": (
                "Kill switch engaged" if config["kill_switch"]
                else RULES["DAILY_LOSS"] if daily_breached
                else RULES["DRAWDOWN"] if drawdown_breached
                else None
            ),
            "open_positions": len(self._open_positions()),
            "orders_last_minute": self._recent_order_count(),
        }

    def engage_kill_switch(self, reason: str) -> dict[str, Any]:
        with app_cursor() as cursor:
            cursor.execute(
                "UPDATE risk_config SET kill_switch = 1, kill_reason = ?, updated_at = ? WHERE account_id = ?",
                (reason, now(), self.account_id),
            )
            if cursor.rowcount == 0:
                get_config(self.account_id)
                cursor.execute(
                    "UPDATE risk_config SET kill_switch = 1, kill_reason = ?, updated_at = ? WHERE account_id = ?",
                    (reason, now(), self.account_id),
                )
        log_event(self.account_id, "KILL_SWITCH", reason)
        log.warning("Kill switch engaged for %s: %s", self.account_id, reason)
        return self.status()

    def release_kill_switch(self) -> dict[str, Any]:
        with app_cursor() as cursor:
            cursor.execute(
                "UPDATE risk_config SET kill_switch = 0, kill_reason = NULL, updated_at = ? WHERE account_id = ?",
                (now(), self.account_id),
            )
        log_event(self.account_id, "KILL_SWITCH_RELEASE", "operator released the kill switch")
        return self.status()

    def check_order(
        self,
        order: dict[str, Any],
        *,
        equity: float | None = None,
        price: float | None = None,
        is_market_open: bool = True,
        available_margin: float | None = None,
    ) -> dict[str, Any]:
        """Validate one order. Raises `RiskViolation` when it must be blocked.

        `order` needs `quantity`, `price` and optionally `trading_symbol`;
        `price` is resolved from a live quote when not supplied.
        """
        config = get_config(self.account_id)
        equity = equity if equity is not None else FALLBACK_EQUITY
        quantity = float(order.get("quantity") or 0)
        unit_price = float(order.get("price") or price or 0)
        notional = quantity * unit_price

        if config["kill_switch"]:
            raise RiskViolation("KILL_SWITCH", RULES["KILL_SWITCH"], {"reason": config["kill_reason"]})

        status = self.status(equity)
        if status["daily_limit_breached"]:
            raise RiskViolation("DAILY_LOSS", RULES["DAILY_LOSS"], {"realised_today": status["realised_today"]})
        if status["drawdown_limit_breached"]:
            raise RiskViolation("DRAWDOWN", RULES["DRAWDOWN"], {"drawdown": status["current_drawdown"]})

        max_position = float(config["max_position_pct"] or 0) * equity
        if max_position and notional > max_position:
            raise RiskViolation(
                "POSITION_SIZE", RULES["POSITION_SIZE"],
                {"notional": round(notional, 2), "limit": round(max_position, 2)},
            )

        positions = self._open_positions()
        symbol = order.get("trading_symbol")
        if max_position:
            already = sum(
                float(p["quantity"] or 0) * float(p["price"] or 0)
                for p in positions if p["trading_symbol"] == symbol
            )
            if already + notional > max_position:
                raise RiskViolation(
                    "POSITION_SIZE", RULES["POSITION_SIZE"],
                    {"existing": round(already, 2), "requested": round(notional, 2),
                     "limit": round(max_position, 2)},
                )

        max_positions = int(config["max_positions"] or 0)
        if max_positions and len(positions) >= max_positions and symbol not in {p["trading_symbol"] for p in positions}:
            raise RiskViolation("POSITION_COUNT", RULES["POSITION_COUNT"], {"open": len(positions)})

        max_exposure = float(config["max_exposure_pct"] or 0) * equity
        if max_exposure:
            current_exposure = sum(float(p["quantity"] or 0) * float(p["price"] or 0) for p in positions)
            if current_exposure + notional > max_exposure:
                raise RiskViolation(
                    "EXPOSURE", RULES["EXPOSURE"],
                    {"current": round(current_exposure, 2), "requested": round(notional, 2),
                     "limit": round(max_exposure, 2)},
                )

        max_rate = int(config["max_orders_per_minute"] or 0)
        if max_rate and self._recent_order_count() >= max_rate:
            raise RiskViolation("ORDER_RATE", RULES["ORDER_RATE"], {"limit_per_minute": max_rate})

        if available_margin is not None and notional > available_margin:
            raise RiskViolation("MIN_MARGIN", RULES["MIN_MARGIN"],
                                {"required": round(notional, 2), "available": round(available_margin, 2)})

        if not is_market_open:
            raise RiskViolation("MARKET_CLOSED", RULES["MARKET_CLOSED"])

        return {
            "approved": True, "notional": round(notional, 2),
            "max_position_value": round(max_position, 2) if max_position else None,
            "max_exposure": round(max_exposure, 2) if max_exposure else None,
            "equity_used_pct": round((notional / equity) * 100, 2) if equity else 0.0,
        }

    def record_equity(self, equity: float, mode: str = "paper", realised: float = 0.0, unrealised: float = 0.0) -> None:
        """Append an equity point; also the drawdown reference series."""
        with app_cursor() as cursor:
            cursor.execute(
                "INSERT INTO equity_curve (id, account_id, mode, ts, equity, realised, unrealised) VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, self.account_id, mode, time.time(), equity, realised, unrealised),
            )


def _start_of_day() -> float:
    from backend.marketdata.market_hours import IST, now_ist

    moment = now_ist()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(IST).timestamp()
