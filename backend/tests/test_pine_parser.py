"""Unit tests for Pine Script parser upgrades: v6 syntax, math expressions, dynamic TP/SL, and table UI skipping."""

import unittest
from backend.strategies.pine import parse_pine_code, import_pine, export_pine


class TestPineParserUpgrades(unittest.TestCase):
    def test_v6_advanced_script(self):
        sample = """
//@version=6
strategy('V6 Advanced Strategy', overlay=true)

length = input.int(14, 'ATR Length')
mult = input.float(2.0, 'Multiplier')

var float trailingStop = na
var int entryBar = 0

atr = ta.atr(length)
basis = ta.sma(close, 20)
stdev = ta.stdev(close, 20)
upper = basis + mult * stdev
lower = basis - mult * stdev

dev = basis - stdev

longCond = ta.crossover(close, upper)
shortCond = ta.crossunder(close, lower)

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
        res = parse_pine_code(sample)
        self.assertTrue(res["valid"])
        defn = res["definition"]

        # Check inputs
        inputs = defn["inputs"]
        self.assertEqual(inputs["length"]["defval"], 14)
        self.assertEqual(inputs["mult"]["defval"], 2.0)

        # Check entry rules
        entries = defn["entry"]["all"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["indicator"], "close")
        self.assertEqual(entries[0]["compare"], "crosses_above")

        # Check dynamic TP / SL in risk definition
        risk = defn["risk"]
        self.assertIn("target_dynamic", risk)
        self.assertIn("stop_loss_dynamic", risk)
        self.assertEqual(risk["target_dynamic"]["left"], "close")
        self.assertEqual(risk["target_dynamic"]["op"], "+")
        self.assertEqual(risk["stop_loss_dynamic"]["left"], "close")
        self.assertEqual(risk["stop_loss_dynamic"]["op"], "-")

        # Check table and UI directives are skipped without breaking
        warnings = res["warnings"]
        self.assertTrue(any("table.new" in w for w in warnings))
        self.assertTrue(any("table.cell" in w for w in warnings))
        self.assertTrue(any("plot" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
