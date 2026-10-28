import unittest
from backend.strategies.pine import parse_pine_code
from backend.strategies.dsl import validate, evaluate, IndicatorCache


class TestPineParser(unittest.TestCase):
    def test_v6_advanced_script_parsing(self):
        script = """
//@version=6
strategy('Advanced V6 Strategy', overlay=true)

length = input.int(14, 'ATR Length')
mult = input.float(2.0, 'Multiplier')

var float trailingStop = na
var int entryBar = 0

atr = ta.atr(length)
basis = ta.sma(close, 20)
stdev = ta.stdev(close, 20)
upper = basis + mult * stdev
lower = basis - mult * stdev

trailingStop := close - atr * 1.5

longCond = ta.crossover(close, upper) and strategy.position_size == 0

if longCond
    strategy.entry('Long', strategy.long)

strategy.exit('TP/SL', 'Long', limit=close + atr * 2, stop=close - atr)

// UI and Tables to ignore
var table myTable = table.new(position.top_right, 2, 2)
table.cell(myTable, 0, 0, 'Status', bgcolor=color.gray)
table.cell(myTable, 0, 1, 'Active', bgcolor=color.green)
plot(basis, color=color.blue)
plot(upper, color=color.green)
plot(lower, color=color.red)
"""
        result = parse_pine_code(script)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["errors"]), 0)

        defn = result["definition"]
        self.assertIn("entry", defn)
        self.assertIn("exit", defn)
        self.assertIn("risk", defn)

        # Verify dynamic exit parsed without missing indicator errors
        validation = validate(defn)
        self.assertTrue(validation["valid"], f"Validation failed with: {validation.get('errors')}")

        # Verify dynamic risk parameters populated
        self.assertIn("target_dynamic", defn["risk"])
        self.assertIn("stop_loss_dynamic", defn["risk"])

        # Verify UI skipping
        ui_warnings = [w for w in result["warnings"] if "UI/visual directive" in w or "table" in w]
        self.assertGreater(len(ui_warnings), 0)

        # Verify evaluation
        candles = [
            {"open": 100 + i, "high": 105 + i, "low": 98 + i, "close": 102 + i, "volume": 1000}
            for i in range(50)
        ]
        cache = IndicatorCache(candles, position_size=0)
        # Should not raise exception
        entry_eval = evaluate(defn["entry"], cache, 40)
        exit_eval = evaluate(defn["exit"], cache, 40)
        self.assertIsInstance(entry_eval, bool)
        self.assertIsInstance(exit_eval, bool)


if __name__ == "__main__":
    unittest.main()
