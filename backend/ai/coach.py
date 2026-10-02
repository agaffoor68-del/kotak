"""AI trading coach.

Two halves:

* **Deterministic analysis** — pattern detection over the user's own trade
  history (late entries, oversized losers, revenge trading, overtrading,
  ignoring stops, cutting winners early). This always runs, needs no API key,
  and every finding cites the trades that produced it.
* **Optional LLM narration** — when ``OPENAI_API_KEY`` is set, the findings are
  turned into a readable review. The model has no broker access and cannot place
  orders; it only receives numbers already computed here.

This is coaching on recorded behaviour. It is not investment advice, and nothing
it produces is auto-executed.
"""

from __future__ import annotations

import logging
import statistics
import time
from typing import Any

from backend.analytics import metrics
from backend.core.config import settings
from backend.core.database import app_cursor, now

log = logging.getLogger("alphatrade.coach")

DISCLAIMER = (
    "This review analyses your own recorded trading behaviour. It is educational feedback, "
    "not investment advice, and nothing it produces is executed automatically."
)

#: Thresholds for the deterministic checks.
LATE_ENTRY_RATIO = 0.35      # share of trades entering in the top third of the bar range
OVERSIZED_LOSS_MULTIPLE = 1.5  # loss size vs average loss
REVENGE_WINDOW_SECONDS = 900
OVERTRADE_PER_DAY = 8
STOP_IGNORE_MIN = 5.0         # rupee distance a stop was ignored by


def load_trades(account_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with app_cursor() as cursor:
        cursor.execute(
            """
            SELECT t.*, j.setup, j.emotion, j.followed_plan, j.notes
            FROM trades t LEFT JOIN journal j ON j.trade_id = t.id
            WHERE t.account_id = ?
            ORDER BY t.entry_time DESC LIMIT ?
            """,
            (account_id, max(1, min(limit, 2000))),
        )
        return [dict(row) for row in cursor.fetchall()]


# ------------------------------------------------------------------- patterns


def _late_entries(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entries taken in the top third of the signal bar's range.

    Such entries leave little room before the level breaks, which is the
    signature of chasing.
    """
    findings = []
    late = [trade for trade in trades if (trade.get("entry_zone") or 0) >= LATE_ENTRY_RATIO]
    if len(late) >= 2 and len(late) / max(1, len(trades)) >= 0.2:
        losers = [t for t in late if (t.get("net_pnl") or 0) < 0]
        average = metrics.mean([float(t.get("net_pnl") or 0) for t in losers]) if losers else 0
        findings.append({
            "pattern": "late_entry",
            "title": "Late entry",
            "occurrences": len(late),
            "share_percent": round(len(late) / max(1, len(trades)) * 100, 1),
            "losses": len(losers),
            "average_loss_on_late_entries": round(average, 2),
            "recommendation": (
                "Add trend confirmation and a volume filter before entry, and place a limit order "
                "at the setup level rather than entering at the top of the range."
            ),
            "evidence": [{"id": t["id"], "symbol": t["symbol"], "entry_price": t.get("entry_price"),
                          "net_pnl": t.get("net_pnl"), "entry_zone": t.get("entry_zone")} for t in late[:10]],
        })
    return findings


def _oversized_losses(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Losing trades materially larger than the account's usual loss."""
    losses = [abs(float(t.get("net_pnl") or 0)) for t in trades if (t.get("net_pnl") or 0) < 0]
    if len(losses) < 3:
        return []
    average = statistics.mean(losses)
    outliers = [
        trade for trade in trades
        if (trade.get("net_pnl") or 0) < 0 and abs(float(trade["net_pnl"])) > average * OVERSIZED_LOSS_MULTIPLE
    ]
    if not outliers:
        return []
    return [{
        "pattern": "oversized_loss",
        "title": "Oversized losers",
        "occurrences": len(outliers),
        "average_loss": round(average, 2),
        "threshold": round(average * OVERSIZED_LOSS_MULTIPLE, 2),
        "recommendation": (
            "Cap position size on the risk engine and place a hard stop before entry. "
            "A loss this large relative to the average usually means the stop was moved or ignored."
        ),
        "evidence": [{"id": t["id"], "symbol": t["symbol"], "net_pnl": t.get("net_pnl"),
                      "quantity": t.get("quantity"), "reason": t.get("reason")} for t in outliers[:10]],
    }]


def _revenge_trading(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A new trade opened soon after a loss, at larger size."""
    ordered = sorted(trades, key=lambda t: t.get("entry_time") or 0)
    findings = []
    for previous, current in zip(ordered, ordered[1:]):
        if (previous.get("net_pnl") or 0) >= 0:
            continue
        gap = (current.get("entry_time") or 0) - (previous.get("exit_time") or previous.get("entry_time") or 0)
        if not (0 <= gap <= REVENGE_WINDOW_SECONDS):
            continue
        if float(current.get("quantity") or 0) > float(previous.get("quantity") or 0) * 1.5:
            findings.append({"id": current["id"], "symbol": current["symbol"],
                             "seconds_after_loss": round(gap), "quantity": current.get("quantity"),
                             "previous_quantity": previous.get("quantity")})
    if len(findings) >= 2:
        return [{
            "pattern": "revenge_trading",
            "title": "Size increase right after a loss",
            "occurrences": len(findings),
            "recommendation": (
                "Enforce a cooling-off period after a loss, and cap position size while a strategy "
                "is under review. Increasing size after a loss is the fastest way to turn a drawdown "
                "into a blow-up."
            ),
            "evidence": findings[:10],
        }]
    return []


def _overtrading(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Too many trades in a day, usually a sign of reacting rather than planning."""
    by_day: dict[str, int] = {}
    for trade in trades:
        timestamp = trade.get("entry_time")
        if not timestamp:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(float(timestamp)))
        by_day[day] = by_day.get(day, 0) + 1
    heavy = {day: count for day, count in by_day.items() if count > OVERTRADE_PER_DAY}
    if len(heavy) >= 2:
        return [{
            "pattern": "overtrading",
            "title": "High trade frequency",
            "occurrences": len(heavy),
            "detail": f"Days exceeding {OVERTRADE_PER_DAY} trades: {heavy}",
            "recommendation": (
                "Cap the number of entries per session in the risk engine and require a written setup "
                "for each trade. Most overtrading costs more in charges and bad entries than it earns."
            ),
            "evidence": [{"day": day, "trades": count} for day, count in list(heavy.items())[:10]],
        }]
    return []


def _stop_discipline(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exits whose loss clearly exceeded what a normal stop would allow."""
    losses = [abs(float(t.get("net_pnl") or 0)) for t in trades if (t.get("net_pnl") or 0) < 0]
    if len(losses) < 3:
        return []
    typical = statistics.median(losses)
    ignored = [t for t in trades if (t.get("net_pnl") or 0) < 0 and abs(float(t["net_pnl"])) > typical * 2.5]
    if len(ignored) < 2:
        return []
    return [{
        "pattern": "stop_discipline",
        "title": "Losses larger than the typical stop",
        "occurrences": len(ignored),
        "median_loss": round(typical, 2),
        "recommendation": (
            "Use a hard stop order with the broker rather than a mental one, so the position is "
            "closed even when you are not watching the screen."
        ),
        "evidence": [{"id": t["id"], "symbol": t["symbol"], "net_pnl": t.get("net_pnl"),
                      "reason": t.get("reason")} for t in ignored[:10]],
    }]


def _plan_adherence(journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How often the written plan was actually followed."""
    noted = [entry for entry in journal if entry.get("followed_plan") is not None]
    if len(noted) < 3:
        return []
    followed = [entry for entry in noted if entry.get("followed_plan")]
    rate = len(followed) / len(noted)
    if rate >= 0.7:
        return []
    return [{
        "pattern": "plan_adherence",
        "title": "Plan was not followed",
        "occurrences": len(noted) - len(followed),
        "adherence_percent": round(rate * 100, 1),
        "recommendation": (
            "Log the planned entry, stop and target before the trade and review adherence weekly. "
            "The gap between plan and action is where most discretionary losses originate."
        ),
        "evidence": [{"symbol": e.get("symbol"), "setup": e.get("setup"), "emotion": e.get("emotion")}
                     for e in noted[:10]],
    }]


# ---------------------------------------------------------------------- coach


def analyse(account_id: str, *, limit: int = 500) -> dict[str, Any]:
    """Run every deterministic check over the account's trade history."""
    trades = load_trades(account_id, limit)
    closed = [t for t in trades if t.get("net_pnl") is not None]
    journal = [t for t in closed if t.get("followed_plan") is not None]

    if not closed:
        return {
            "has_data": False,
            "disclaimer": DISCLAIMER,
            "note": (
                "No closed trades are recorded yet. The coach analyses your own history, so it "
                "starts producing findings once trades are on the book — paper or live."
            ),
            "findings": [],
        }

    findings = (
        _late_entries(closed)
        + _oversized_losses(closed)
        + _revenge_trading(closed)
        + _overtrading(closed)
        + _stop_discipline(closed)
        + _plan_adherence(journal)
    )
    # Most severe first: largest share of the sample, then biggest rupee impact.
    findings.sort(key=lambda f: (-f.get("occurrences", 0), abs(f.get("average_loss", 0))))

    equity = [500_000.0]
    for trade in sorted(closed, key=lambda t: t.get("exit_time") or t.get("entry_time") or 0):
        equity.append(equity[-1] + float(trade.get("net_pnl") or 0))

    return {
        "has_data": True,
        "disclaimer": DISCLAIMER,
        "analysed_trades": len(closed),
        "findings": findings,
        "performance": metrics.summarise(equity, closed, period_seconds=86400),
        "generated_at": now(),
    }


def suggest_strategy(account_id: str, *, market_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn the recorded patterns into concrete strategy-building guidance.

    The output is a set of *rule suggestions* in the strategy DSL, so they can be
    loaded into the builder rather than only read as prose.
    """
    analysis = analyse(account_id)
    if not analysis["has_data"]:
        return {
            "has_data": False,
            "disclaimer": DISCLAIMER,
            "note": "Not enough trade history yet to tailor a strategy.",
            "suggestions": [],
        }

    suggestions: list[dict[str, Any]] = []
    patterns = {finding["pattern"] for finding in analysis["findings"]}

    if "late_entry" in patterns:
        suggestions.append({
            "reason": "Your history shows late entries.",
            "change": "Require a trend filter and a volume confirmation before entry.",
            "rule": {"all": [
                {"indicator": "ema", "params": {"period": 20}, "field": "value", "compare": "gt",
                 "against": {"indicator": "ema", "params": {"period": 50}, "field": "value"}},
                {"indicator": "cmf", "params": {"period": 20}, "field": "value", "compare": "gt", "value": 0.0},
            ]},
        })
    if "oversized_loss" in patterns or "stop_discipline" in patterns:
        suggestions.append({
            "reason": "Some losses exceeded a normal stop, or were never protected.",
            "change": "Tighten the stop and size the position from the risk budget, not a fixed lot.",
            "rule": {"all": []},
            "position_sizing": {"mode": "pct_risk", "risk_pct": 0.5},
            "risk": {"stop_loss_pct": 1.0, "target_pct": 2.0},
        })
    if "revenge_trading" in patterns:
        suggestions.append({
            "reason": "Position size increased immediately after a loss.",
            "change": "Set a max-orders-per-minute cap and keep size fixed while a strategy is under review.",
            "risk_config": {"max_orders_per_minute": 4},
        })
    if "overtrading" in patterns:
        suggestions.append({
            "reason": "Trade frequency is high.",
            "change": "Trade a higher timeframe and require a pullback entry rather than reacting to breaks.",
            "rule": {"all": [
                {"indicator": "adx", "params": {"period": 14}, "field": "adx", "compare": "gt", "value": 20},
            ]},
            "timeframe": "15m",
        })

    if not suggestions:
        suggestions.append({
            "reason": "No repeated pattern was detected in your history.",
            "change": "Keep logging setups and outcomes; the coach refines guidance as history grows.",
        })

    return {
        "has_data": True,
        "disclaimer": DISCLAIMER,
        "market_context": market_context or {},
        "suggestions": suggestions,
        "generated_at": now(),
    }


def _llm_narrative(analysis: dict[str, Any], *, mode: str) -> str | None:
    """Optional LLM summary. The model only sees numbers computed above."""
    if not settings.openai_api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        findings = "\n".join(
            f"- {finding['title']}: {finding.get('occurrences', 0)} occurrences. "
            f"Recommendation: {finding['recommendation']}"
            for finding in analysis.get("findings", [])
        ) or "- No repeated patterns detected."
        performance = analysis.get("performance", {}).get("trades", {})

        if mode == "loss_analysis":
            prompt = (
                "You are a trading coach reviewing a trader's own recorded behaviour in the Indian "
                "markets. Give a short, specific, practical review. Do not give financial advice, do "
                "not promise returns, and do not suggest specific trades. Focus on process.\n\n"
                f"Patterns detected:\n{findings}\n\n"
                f"Trade stats: {performance}\n\n"
                "Write: one paragraph on the main problem, then three concrete process changes."
            )
        else:
            prompt = (
                "You are a trading coach. Using the patterns below, suggest process improvements for an "
                "Indian retail trader. Be specific and behavioural. No financial advice, no trade ideas.\n\n"
                f"Patterns detected:\n{findings}\n\nTrade stats: {performance}"
            )

        response = OpenAI().responses.create(model=settings.openai_model, input=prompt)
        return response.output_text
    except Exception as error:  # noqa: BLE001 - the coach must work without the LLM
        log.warning("AI narrative unavailable: %s", error)
        return None


def review(account_id: str, *, mode: str = "loss_analysis") -> dict[str, Any]:
    """Full coach response, with the LLM layer added when configured."""
    if mode == "strategy_suggestions":
        payload = suggest_strategy(account_id)
    else:
        payload = analyse(account_id)

    narrative = _llm_narrative(payload, mode=mode) if payload.get("has_data") else None
    return {
        **payload,
        "mode": mode,
        "narrative": narrative,
        "llm_enabled": bool(settings.openai_api_key),
    }


def performance_report(account_id: str, *, days: int = 90) -> dict[str, Any]:
    """A periodic self-review built from recorded performance."""
    trades = [t for t in load_trades(account_id, 2000) if t.get("net_pnl") is not None]
    if not trades:
        return {"has_data": False, "disclaimer": DISCLAIMER,
                "note": "No closed trades recorded yet."}

    cutoff = time.time() - days * 86400
    recent = [t for t in trades if (t.get("exit_time") or t.get("entry_time") or 0) >= cutoff] or trades
    equity = [500_000.0]
    for trade in sorted(recent, key=lambda t: t.get("exit_time") or t.get("entry_time") or 0):
        equity.append(equity[-1] + float(trade.get("net_pnl") or 0))

    return {
        "has_data": True,
        "disclaimer": DISCLAIMER,
        "window_days": days,
        "performance": metrics.summarise(equity, recent, period_seconds=86400),
        "daily": metrics.daily_pnl(recent, days=days),
        "monthly": metrics.monthly_pnl(recent),
        "coach": review(account_id, mode="loss_analysis"),
        "generated_at": now(),
    }
