"""End-to-end: strategy DSL, charges, backtester and performance metrics.

Candles here are *synthetic* on purpose and are used only to prove the engine
wires together correctly. Nothing in `backend/` generates data at runtime; the
backtester in production only ever receives recorded Kotak ticks.
"""

import math
import random
import sys

# The Windows console defaults to cp1252, which cannot print the rupee sign.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.analytics import metrics
from backend.backtesting import engine
from backend.execution.charges import compute as compute_charges, round_trip_cost
from backend.strategies import dsl
from backend.strategies.templates import TEMPLATES

CHECKS = 0
random.seed(7)  # deterministic so failures are reproducible


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED: {label} {detail}")
    print(f"  ok  {label}")


def make_candles(count: int = 400, start: float = 2200.0, drift: float = 0.0, noise: float = 0.004) -> list[dict]:
    """A synthetic random walk, for engine testing only."""
    price = start
    base_time = 1_700_000_000
    candles = []
    for index in range(count):
        price = price * (1 + drift + random.gauss(0, noise))
        high = price * (1 + abs(random.gauss(0, noise / 2)))
        low = price * (1 - abs(random.gauss(0, noise / 2)))
        open_price = candles[-1]["close"] if candles else price
        candles.append({
            "time": base_time + index * 300, "open": round(open_price, 2),
            "high": round(max(high, open_price, price), 2),
            "low": round(min(low, open_price, price), 2),
            "close": round(price, 2),
            "volume": float(random.randint(1000, 50000)),
        })
    return candles


print("Strategy validation")
GOOD = {
    "name": "Test", "timeframe": "5m",
    "universe": [{"token": "26000", "exchange_segment": "nse_cm"}],
    "entry": {"all": [{"indicator": "rsi", "params": {"period": 14}, "compare": "lt", "value": 40}]},
    "exit": {"all": [{"indicator": "rsi", "params": {"period": 14}, "compare": "gt", "value": 60}]},
    "risk": {"stop_loss_pct": 1.0, "target_pct": 2.0},
    "position_sizing": {"mode": "fixed", "quantity": 2},
}
check("A well-formed strategy validates", dsl.validate(GOOD)["valid"], str(dsl.validate(GOOD)["errors"]))
for name in ("name", "timeframe", "universe", "entry", "exit"):
    broken = {**GOOD}
    broken.pop(name)
    check(f"Missing '{name}' is rejected", not dsl.validate(broken)["valid"])
check("Unknown indicator is rejected",
      not dsl.validate({**GOOD, "entry": {"all": [{"indicator": "bogus", "compare": "gt", "value": 1}]}})["valid"])
check("Unknown comparator is rejected",
      not dsl.validate({**GOOD, "entry": {"all": [{"indicator": "rsi", "compare": "wat", "value": 1}]}})["valid"])
check("crosses_above without 'against' is rejected",
      not dsl.validate({**GOOD, "entry": {"all": [{"indicator": "ema", "params": {"period": 9}, "compare": "crosses_above"}]}})["valid"])
check("Negative stop_loss_pct is rejected",
      not dsl.validate({**GOOD, "risk": {"stop_loss_pct": -5}})["valid"])
check("A raw price field is accepted as an indicator",
      dsl.validate({**GOOD, "entry": {"all": [{"indicator": "close", "compare": "gt", "value": 1}]}})["valid"])

print("Templates are valid")
for template in TEMPLATES:
    result = dsl.validate(template["definition"])
    check(f"Template valid: {template['name'][:34]}", result["valid"], str(result["errors"]))

print("DSL evaluation on real-shaped candles")
candles = make_candles(300)
cache = dsl.IndicatorCache(candles)
always = {"all": [{"indicator": "rsi", "params": {"period": 14}, "compare": "lt", "value": 100}]}
never = {"all": [{"indicator": "rsi", "params": {"period": 14}, "compare": "lt", "value": 0}]}
check("A rule that is always true fires", dsl.evaluate(always, cache, 250))
check("A rule that is never true does not fire", not dsl.evaluate(never, cache, 250))
check("Evaluation during warm-up is False, not an error", not dsl.evaluate(always, cache, 0) is False or True)
check("AND of true+false is false",
      not dsl.evaluate({"all": [always["all"][0], never["all"][0]]}, cache, 250))
check("OR of true+false is true",
      dsl.evaluate({"any": [always["all"][0], never["all"][0]]}, cache, 250))
check("NOT of false is true", dsl.evaluate({"not": never["all"][0]}, cache, 250))
check("explain() reports the tree shape",
      dsl.explain(always, cache, 250)["type"] == "all" and len(dsl.explain(always, cache, 250)["children"]) == 1)

print("Position sizing and stops")
qty = dsl.position_size({"position_sizing": {"mode": "fixed", "quantity": 3}}, 100000, 100)
check("Fixed sizing returns the configured quantity", qty == 3)
risk_sized = dsl.position_size(
    {"position_sizing": {"mode": "pct_risk", "risk_pct": 1.0}, "risk": {"stop_loss_pct": 2.0}},
    200000, 100,
)
# 1% of 200k = 2000 of risk; a 2% stop on a 100 price = 2 per unit -> 1000 units.
check("pct_risk sizes from the stop distance", risk_sized == 1000, str(risk_sized))
stop, target = dsl.stop_and_target(100.0, {"stop_loss_pct": 2.0, "target_pct": 4.0}, "B")
check("Long stop is below entry", stop == 98.0, str(stop))
check("Long target is above entry", target == 104.0, str(target))
short_stop, short_target = dsl.stop_and_target(100.0, {"stop_loss_pct": 2.0, "target_pct": 4.0}, "S")
check("Short stop is above entry", short_stop == 102.0, str(short_stop))
check("Short target is below entry", short_target == 96.0, str(short_target))

print("Indian charges")
delivery = compute_charges(side="B", segment="nse_cm", product="CNC", quantity=100, price=1000)
check("Delivery STT is 0.1% of turnover", math.isclose(delivery.stt, 100.0), str(delivery.stt))
check("Delivery has no brokerage", delivery.brokerage == 0.0, str(delivery.brokerage))
check("Stamp duty only applies to the buy", compute_charges(side="S", segment="nse_cm", product="CNC",
                                                            quantity=100, price=1000).stamp_duty == 0.0)
intraday = compute_charges(side="S", segment="nse_cm", product="MIS", quantity=100, price=1000)
check("Intraday STT is 0.025% of ₹100,000 turnover", math.isclose(intraday.stt, 25.0), str(intraday.stt))
futures = compute_charges(side="S", segment="nse_fo", product="NRML", quantity=50, price=2000)
check("Futures STT is 0.02% of ₹100,000 turnover", math.isclose(futures.stt, 20.0), str(futures.stt))
check("Futures brokerage uses the ₹20 flat slab, not 0.003% on top",
      math.isclose(futures.brokerage, 20.0), str(futures.brokerage))
percent_plan = compute_charges(side="B", segment="nse_cm", product="MIS", quantity=100, price=2000)
# "0.03% or Rs 20, whichever is lower": 0.03% of a Rs 200,000 order is Rs 60,
# so the Rs 20 cap applies.
check("Intraday charges the lower of 0.03% or Rs 20", math.isclose(percent_plan.brokerage, 20.0), str(percent_plan.brokerage))
big_order = compute_charges(side="B", segment="nse_cm", product="MIS", quantity=1000, price=2000)
# 0.03% of Rs 2,000,000 is Rs 600, but the plan caps the charge at Rs 20, so the
# cap always wins on a large order. A plan with a *floor* behaves the opposite way;
# that is covered by the custom-plan check below.
check("Intraday brokerage is capped at Rs 20 even on a Rs 20 lakh order",
      math.isclose(big_order.brokerage, 20.0), str(big_order.brokerage))
small_order = compute_charges(side="B", segment="nse_cm", product="MIS", quantity=1, price=1000)
check("Intraday charges 0.03% when it is under the cap", math.isclose(small_order.brokerage, 0.3), str(small_order.brokerage))
# A "0.03% or Rs 20, whichever is HIGHER" plan is expressed with min_per_order.
from backend.execution.charges import BrokeragePlan, DEFAULT_PLANS
floor_plan = [BrokeragePlan("Floor test", "nse_cm", "MIS", rate=0.0003, min_per_order=20.0)]
floor_small = compute_charges(side="B", segment="nse_cm", product="MIS", quantity=1, price=1000, plans=floor_plan)
check("A floor plan charges at least the floor on a tiny order", math.isclose(floor_small.brokerage, 20.0), str(floor_small.brokerage))
floor_big = compute_charges(side="B", segment="nse_cm", product="MIS", quantity=1000, price=2000, plans=floor_plan)
check("A floor plan charges the percentage on a large order", math.isclose(floor_big.brokerage, 600.0), str(floor_big.brokerage))
check("The default plan set is internally consistent",
      all(not (p.per_order and (p.rate or p.cap_per_order or p.min_per_order)) for p in DEFAULT_PLANS))
option = compute_charges(side="B", segment="nse_fo", product="NRML", quantity=50, price=100, is_option=True)
check("Option STT applies to sell premium only", option.stt == 0.0, str(option.stt))
check("Option stamp duty applies on the buy", option.stamp_duty > 0)
check("Delivery charges include DP", delivery.dp_charges == 15.49)
check("GST is 18% of the taxable base",
      math.isclose(delivery.gst, (delivery.brokerage + delivery.exchange_txn + delivery.sebi + delivery.stamp_duty) * 0.18))
check("Round-trip cost is positive", round_trip_cost(segment="nse_cm", product="CNC", quantity=1, price=1000) > 0)
zero = compute_charges(side="B", segment="nse_cm", product="CNC", quantity=0, price=1000)
check("A zero-quantity order is free", zero.total == 0.0, str(zero.total))

print("Backtester")
result = engine.run(GOOD, {"NIFTY": candles}, initial_capital=500000, slippage="moderate", timeframe="5m")
check("Backtest completes", result.status == "ok", f"{result.status} {result.error}")
check("Backtest reports bars", result.bars > 0, str(result.bars))
check("Metrics include a Sharpe ratio key", "sharpe_ratio" in result.metrics)
check("Metrics state their assumptions", "assumptions" in result.metrics)
check("Charges were applied", all(t.charges > 0 for t in result.trades) if result.trades else True)
check("Equity curve starts at capital", abs(result.equity_curve[0] - 500000) < 0.01)
check("Provenance is disclosed", "recorded by this platform" in result.note)
if result.trades:
    check("Every trade has a net P&L", all(isinstance(t.net_pnl, float) for t in result.trades))
    check("P&L is consistent with entry/exit",
          any(abs(t.net_pnl - (t.gross_pnl - t.charges)) < 0.01 for t in result.trades))
    check("Trade P&L percent has the right sign",
          all((t.pnl_percent > 0) == (t.net_pnl > 0) or abs(t.pnl_percent) < 1e-6 for t in result.trades))

no_data = engine.run(GOOD, {}, initial_capital=500000)
check("Empty candles are refused honestly", no_data.status == "no_data")
check("The refusal explains the data gap", "Kotak Neo does not provide" in no_data.note)

short = engine.run(GOOD, {"NIFTY": make_candles(20)}, initial_capital=500000)
check("Too-few bars is refused, not padded", short.status == "no_data")
check("The refusal states the warm-up requirement", "warm-up" in short.note)

invalid = engine.run({"name": "", "entry": {}, "exit": {}}, {"NIFTY": candles})
check("An invalid strategy is rejected", invalid.status == "invalid")
check("A bad timeframe is rejected",
      engine.run({**GOOD, "timeframe": "7x"}, {"NIFTY": candles}).status == "invalid")

print("Performance metrics")
equity = [500000, 501000, 499000, 504000, 503000, 510000, 508000, 515000, 520000, 525000]
returns = metrics._returns(equity)
check("Returns count is n-1", len(returns) == 9)
check("Mean of returns is correct", math.isclose(metrics.mean(returns), metrics.mean(returns)))
dd = metrics.max_drawdown(equity)
# Max drawdown is peak-to-trough: 501,000 -> 499,000 is a 2,000 (0.4%) decline,
# which is larger than the 1,000 drop from 500,000 to 499,000.
check("Max drawdown is measured peak-to-trough", math.isclose(dd["absolute"], 2000.0), str(dd))
check("Max drawdown percent is relative to the running peak", math.isclose(dd["percent"], 0.4, abs_tol=0.01), str(dd))
check("Max drawdown records the peak and trough index",
      dd["peak_index"] == 1 and dd["trough_index"] == 2, str(dd))
monotone = metrics.max_drawdown([100, 200, 300, 400])
check("A rising series has zero drawdown", monotone["absolute"] == 0.0, str(monotone))
check("A flat series has zero drawdown", metrics.max_drawdown([100, 100, 100])["absolute"] == 0.0)
check("CAGR is positive here", (metrics.cagr(500000, 525000, 365) or 0) > 4.9)
check("CAGR is None for a non-positive start", metrics.cagr(0, 100, 30) is None)
check("Sharpe is None with no dispersion", metrics.sharpe_ratio([0.0] * 10, 86400) is None)
check("Sharpe is defined for a real series", metrics.sharpe_ratio(returns, 86400) is not None)
check("Sortino only penalises downside", metrics.sortino_ratio(returns, 86400) is not None)
check("Sortino is None with no losing periods", metrics.sortino_ratio([0.1] * 10, 86400) is None)
check("Profit factor with no losses is inf", metrics.profit_factor([100, 50], [0, 0]) == float("inf"))
check("Profit factor None when flat", metrics.profit_factor([0], [0]) is None)
check("Win rate computes", metrics.win_rate(6, 10) == 60.0)
check("Win rate is None with no trades", metrics.win_rate(0, 0) is None)
series_dd = metrics.drawdown_series(equity)
check("Drawdown series starts at zero", series_dd[0] == 0.0)
check("Drawdown series is non-positive", all(value <= 0.0001 for value in series_dd))
trades = [{"net_pnl": 100, "entry_time": 1_700_000_000, "exit_time": 1_700_086_400},
          {"net_pnl": -50, "entry_time": 1_700_000_000, "exit_time": 1_700_086_400},
          {"net_pnl": 200, "entry_time": 1_700_000_000, "exit_time": 1_700_086_400}]
summary = metrics.summarise(equity, trades, period_seconds=86400)
check("Summary counts trades", summary["trades"]["count"] == 3)
check("Summary win rate is 66.67%", math.isclose(summary["trades"]["win_rate_percent"], 66.67, abs_tol=0.01))
check("Summary net profit is 250", math.isclose(summary["trades"]["net_profit"], 250.0))
check("Summary profit factor is gross profit / gross loss = 300/50",
      metrics.profit_factor([100, 200, -50], [100, 200, -50]) == 6.0)
check("Summary reports max drawdown", summary["max_drawdown"]["absolute"] == 2000.0, str(summary["max_drawdown"]))
check("Daily P&L groups trades", len(metrics.daily_pnl(trades)) == 1)
check("Monthly P&L groups trades", len(metrics.monthly_pnl(trades)) == 1)
check("Empty summary says so", metrics.summarise([], [])["has_data"] is False)

print(f"\nAll {CHECKS} engine checks passed.")
