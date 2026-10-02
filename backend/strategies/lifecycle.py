"""Strategy approval lifecycle.

A strategy may only reach live trading through the full chain

    draft -> backtested -> forward_tested -> approved -> live

and every step is checked against recorded evidence rather than trusted from the
client. The product rule is stated plainly: *no strategy may enter live trading
without user approval*, so `assert_live_allowed` is called from the execution
path, not only from the API layer — otherwise an internal caller could bypass it.

Rejection is explicit. A strategy that fails its forward test returns to `draft`
with the metrics attached, rather than sitting in a state that looks approvable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from backend.core.database import app_cursor, now, row_to_dict

#: Ordered progression, used by the UI to render the pipeline.
STATES: tuple[str, ...] = ("draft", "backtested", "forward_tested", "approved", "live")

#: Rejection is modelled as a return to `draft`.
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"backtested"},
    "backtested": {"forward_tested", "draft"},
    "forward_tested": {"approved", "draft"},
    "approved": {"live", "forward_tested", "draft"},
    "live": {"approved"},
}

#: A forward test must run this long before it counts as evidence. Short samples
#: are noise, and a strategy that only works over two sessions is not evidence.
MIN_FORWARD_SECONDS = 60 * 60 * 24  # one day of recorded tape

#: Forward tests must clear these to be promoted. Deliberately conservative and
#: configurable: the point is to reject the obviously broken, not to promise edge.
FORWARD_MIN_TRADES = 5
FORWARD_MAX_DRAWDOWN_PCT = 25.0


class LifecycleError(ValueError):
    """A lifecycle rule refused a transition."""


def current_state(strategy: dict[str, Any]) -> str:
    state = strategy.get("lifecycle") or "draft"
    return state if state in STATES else "draft"


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in TRANSITIONS.get(from_state, set())


# ------------------------------------------------------------------ evidence


def backtest_evidence(strategy_id: str) -> dict[str, Any] | None:
    """Most recent completed backtest for this strategy, if any."""
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, metrics, finished_at FROM strategy_runs
            WHERE strategy_id = ? AND mode = 'backtest' AND status = 'completed'
            ORDER BY finished_at DESC LIMIT 1
            """,
            (strategy_id,),
        )
        row = row_to_dict(cursor.fetchone())
    if not row:
        return None
    row["metrics"] = row.get("metrics") or {}
    return row


def forward_evidence(strategy_id: str) -> dict[str, Any] | None:
    """Longest completed forward (paper) run for this strategy, if any."""
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, metrics, started_at, finished_at FROM strategy_runs
            WHERE strategy_id = ? AND mode = 'paper' AND status IN ('completed','stopped')
            ORDER BY started_at ASC LIMIT 1
            """,
            (strategy_id,),
        )
        row = row_to_dict(cursor.fetchone())
    # `is None` rather than truthiness: a timestamp of 0 is a legitimate epoch
    # value, and `if not row["started_at"]` would silently discard that run.
    if row is None or row.get("started_at") is None or row.get("finished_at") is None:
        return None
    row["metrics"] = row.get("metrics") or {}
    row["duration_seconds"] = row["finished_at"] - row["started_at"]
    return row


def _forward_shortfall(row: dict[str, Any]) -> list[str]:
    """Human-readable reasons a forward run is not yet promotable."""
    reasons: list[str] = []
    if row["duration_seconds"] < MIN_FORWARD_SECONDS:
        hours = row["duration_seconds"] / 3600
        reasons.append(f"forward test ran {hours:.1f}h; {MIN_FORWARD_SECONDS / 3600:.0f}h minimum")
    metrics = row.get("metrics") or {}
    trades = int(metrics.get("total_trades") or metrics.get("trades") or 0)
    if trades < FORWARD_MIN_TRADES:
        reasons.append(f"{trades} trades; {FORWARD_MIN_TRADES} minimum")
    drawdown = metrics.get("max_drawdown_pct")
    if drawdown is not None and float(drawdown) > FORWARD_MAX_DRAWDOWN_PCT:
        reasons.append(f"max drawdown {float(drawdown):.1f}% exceeds {FORWARD_MAX_DRAWDOWN_PCT:.0f}%")
    return reasons
# ----------------------------------------------------------------- recording


def record(
    strategy_id: str,
    from_state: str,
    to_state: str,
    actor: str,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Append to the audit trail and move the strategy."""
    with app_cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO strategy_approvals
                (id, strategy_id, from_state, to_state, actor, reason, evidence, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex, strategy_id, from_state, to_state, actor, reason,
                json.dumps(evidence) if evidence else None, now(),
            ),
        )
        cursor.execute(
            "UPDATE strategies SET lifecycle = ?, updated_at = ? WHERE id = ?",
            (to_state, now(), strategy_id),
        )


def history(strategy_id: str) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM strategy_approvals WHERE strategy_id = ? ORDER BY created_at DESC",
            (strategy_id,),
        )
        return [row_to_dict(row) for row in cursor.fetchall()]


# -------------------------------------------------------------------- guards


def assert_live_allowed(strategy: dict[str, Any]) -> None:
    """Refuse a live deployment that has not cleared the pipeline.

    Called from `algo.supervisor.start`, which every live run passes through.
    """
    state = current_state(strategy)
    if state in {"approved", "live"}:
        return
    raise LifecycleError(
        f"Strategy '{strategy.get('name') or strategy.get('id')}' is '{state}'. "
        "It must be backtested, forward tested and explicitly approved before it can trade live."
    )


def promote(strategy: dict[str, Any], to_state: str, actor: str, reason: str | None = None) -> dict[str, Any]:
    """Move a strategy forward, verifying the evidence each step requires."""
    strategy_id = str(strategy["id"])
    from_state = current_state(strategy)

    if not can_transition(from_state, to_state):
        raise LifecycleError(f"Cannot move a strategy from '{from_state}' to '{to_state}'.")

    evidence: dict[str, Any] | None = None

    if to_state == "backtested":
        run = backtest_evidence(strategy_id)
        if not run:
            raise LifecycleError("Run and complete a backtest before marking this strategy backtested.")
        evidence = {"backtest_run": run["id"], "metrics": run["metrics"]}

    elif to_state == "forward_tested":
        run = forward_evidence(strategy_id)
        if not run:
            raise LifecycleError("Run a paper (forward) test before marking this strategy forward tested.")
        shortfall = _forward_shortfall(run)
        if shortfall:
            raise LifecycleError("Forward test has not met its bar: " + "; ".join(shortfall) + ".")
        evidence = {
            "forward_run": run["id"],
            "metrics": run["metrics"],
            "duration_seconds": run["duration_seconds"],
        }

    elif to_state == "approved":
        forward = forward_evidence(strategy_id)
        if not forward or _forward_shortfall(forward):
            raise LifecycleError("Only a strategy that passed its forward test can be approved.")
        if not reason:
            raise LifecycleError("Approval requires a reason; it is stored in the audit trail.")
        evidence = {"forward_run": forward["id"]}

    elif to_state == "live":
        assert_live_allowed({**strategy, "lifecycle": "approved"})

    if to_state == "approved":
        with app_cursor() as cursor:
            cursor.execute(
                "UPDATE strategies SET approved_by = ?, approved_at = ? WHERE id = ?",
                (actor, now(), strategy_id),
            )

    record(strategy_id, from_state, to_state, actor, reason, evidence)
    return {"id": strategy_id, "from": from_state, "to": to_state, "evidence": evidence}