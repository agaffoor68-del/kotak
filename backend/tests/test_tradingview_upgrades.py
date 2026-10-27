"""Unit tests for TradingView terminal upgrades: 100+ Indicators engine and Pine Script parser."""

import unittest
from backend.api.routes.indicators import calculate_indicator, CalculateIndicatorRequest
from backend.strategies.pine import parse_pine_code, import_pine, export_pine


class TestTradingViewUpgrades(unittest.TestCase):
    def setUp(self):
        self.user_mock = {"sub": "trader-1", "role": "admin"}

    def test_indicator_supertrend_calculation(self):
        req = CalculateIndicatorRequest(
            symbol="NIFTY 50",
            timeframe="5m",
            indicator_name="supertrend",
            params={"length": 7, "multiplier": 3.0},
        )
        res = calculate_indicator(body=req, user=self.user_mock)
        self.assertEqual(res["symbol"], "NIFTY 50")
        self.assertIn("values", res)
        self.assertGreaterEqual(len(res["values"]), 50)
        # Check supertrend column exists
        has_st = any(col.startswith("SUPERT_") for col in res["columns"])
        self.assertTrue(has_st, f"Expected SUPERT column in {res['columns']}")

    def test_indicator_macd_and_rsi_calculation(self):
        req_macd = CalculateIndicatorRequest(
            symbol="BANKNIFTY",
            timeframe="5m",
            indicator_name="macd",
            params={"fast": 12, "slow": 26, "signal": 9},
        )
        res_macd = calculate_indicator(body=req_macd, user=self.user_mock)
        self.assertEqual(res_macd["symbol"], "BANKNIFTY")
        has_macd = any("MACD" in col for col in res_macd["columns"])
        self.assertTrue(has_macd)

        req_rsi = CalculateIndicatorRequest(
            symbol="SENSEX",
            timeframe="5m",
            indicator_name="rsi",
            params={"length": 14},
        )
        res_rsi = calculate_indicator(body=req_rsi, user=self.user_mock)
        self.assertEqual(res_rsi["symbol"], "SENSEX")
        has_rsi = any("RSI" in col for col in res_rsi["columns"])
        self.assertTrue(has_rsi)

    def test_pine_script_parser_features(self):
        pine_script = """
//@version=5
strategy("Institutional EMA Confluence", overlay=true)

fastLength = input.int(defval=15, title="Fast EMA")
slowLength = input.int(defval=45, title="Slow EMA")
profitMultiplier = input.float(defval=1.025, title="Take Profit Target")

emaFast = ta.ema(close, fastLength)
smaSlow = ta.sma(close, slowLength)
rsi14 = ta.rsi(close, 14)

longCondition = ta.crossover(emaFast, smaSlow)
exitCondition = rsi14 > 70

if (longCondition)
    strategy.entry("LongEntry", strategy.long)

if (exitCondition)
    strategy.close("LongEntry")

// Visuals to be gracefully skipped
plot(emaFast, color=color.blue)
plot(smaSlow, color=color.orange)
bgcolor(color.gray)
"""
        parsed = import_pine(pine_script, name="Institutional EMA Confluence", timeframe="5m")
        self.assertTrue(parsed["valid"])
        defn = parsed["definition"]
        self.assertEqual(defn["name"], "Institutional EMA Confluence")

        # Verify inputs parsed
        inputs = defn.get("inputs", {})
        self.assertIn("fastLength", inputs)
        self.assertEqual(inputs["fastLength"]["defval"], 15)
        self.assertIn("profitMultiplier", inputs)
        self.assertEqual(inputs["profitMultiplier"]["defval"], 1.025)

        # Verify entry condition
        entry_rules = defn["entry"]["all"]
        self.assertGreaterEqual(len(entry_rules), 1)
        self.assertEqual(entry_rules[0]["indicator"], "ema")
        self.assertEqual(entry_rules[0]["compare"], "crosses_above")

        # Verify exit condition
        exit_rules = defn["exit"]["all"]
        self.assertGreaterEqual(len(exit_rules), 1)
        self.assertEqual(exit_rules[0]["indicator"], "rsi")
        self.assertEqual(exit_rules[0]["compare"], "gt")

        # Verify unsupported visuals were gracefully ignored into warnings
        self.assertGreaterEqual(len(parsed["warnings"]), 1)
        has_plot_warning = any("plot" in w for w in parsed["warnings"])
        self.assertTrue(has_plot_warning)

    def test_pine_script_export(self):
        dummy_defn = {
            "name": "Exported Strategy",
            "timeframe": "15",
            "universe": [{"token": "99926000", "exchange_segment": "nse_cm", "label": "NIFTY 50"}],
        }
        res = export_pine(dummy_defn)
        self.assertIn("//@version=5", res["code"])
        self.assertIn("strategy.entry", res["code"])


if __name__ == "__main__":
    unittest.main()
