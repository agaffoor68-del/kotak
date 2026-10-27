"""Comprehensive unit tests for the upgraded Multi-Index Self-Learning AI Brain."""
import logging
import os
import sys
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from backend.core import database
from backend.core.database import app_cursor, market_cursor
from backend.ai import live_scanner


class TestAiBrain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialise()

    def setUp(self):
        # Clear trade history before each test to ensure test isolation
        try:
            with app_cursor() as cursor:
                cursor.execute("DELETE FROM AI_Trade_History")
        except Exception:
            pass
        try:
            with market_cursor() as cursor:
                cursor.execute("DELETE FROM AI_Trade_History")
        except Exception:
            pass

    def test_bootstrap_data_task1(self):
        """TASK 1: Verify 50 historical 5m candles are bootstrapped for all indices."""
        counts = live_scanner.bootstrap_all_indices()
        self.assertEqual(len(counts), 3)
        for idx in ("NIFTY 50", "BANKNIFTY", "SENSEX"):
            self.assertIn(idx, counts)
            self.assertGreaterEqual(counts[idx], 20, f"Expected at least 20 candles for {idx}")
            candles = live_scanner.bootstrap_candles(idx, min_candles=50)
            self.assertGreaterEqual(len(candles), 20)
            self.assertIn("close", candles[-1])
            self.assertIn("open", candles[-1])
            self.assertIn("high", candles[-1])
            self.assertIn("low", candles[-1])

    def test_multi_index_analysis_engine_task2(self):
        """TASK 2: Verify multi-index analysis produces valid scoring and JSON array."""
        results = live_scanner.scan_once()
        self.assertEqual(len(results), 3, "Expected analysis for all 3 indices")

        indices = [r["index"] for r in results]
        self.assertIn("NIFTY 50", indices)
        self.assertIn("BANKNIFTY", indices)
        self.assertIn("SENSEX", indices)

        for item in results:
            self.assertIn(item["signal"], ("BUY CE", "BUY PE", "WAIT"))
            self.assertEqual(item["strike"], "ATM")
            self.assertGreater(item["strike_price"], 0)
            self.assertTrue(item["confidence"].endswith("%"))
            self.assertIn(item["trend"], ("Bullish", "Bearish"))
            self.assertNotEqual(item["symbol"], "Unknown")
            self.assertNotEqual(item["trend"], "Unknown")
            self.assertEqual(item["status"], "live")
            # Verify indicators are calculated
            self.assertIn("rsi", item)
            self.assertIn("vwap", item)
            self.assertIn("supertrend", item)
            self.assertIn("ema20", item)
            self.assertIn("ema50", item)
            self.assertGreater(item["price"], 0)

    def test_confluence_scoring_logic(self):
        """TASK 2: Verify confluence logic when candles are distinctly bullish vs bearish."""
        # Create an artificial bullish candle series (consistently rising)
        bullish_candles = [
            {"time": 1000 + i * 300, "open": 20000 + i * 10, "high": 20000 + i * 10 + 8,
             "low": 20000 + i * 10 - 2, "close": 20000 + i * 10 + 6, "volume": 1000}
            for i in range(60)
        ]
        res = live_scanner._analysis_for_index("NIFTY 50", bullish_candles)
        self.assertEqual(res["signal"], "BUY CE")
        self.assertEqual(res["trend"], "Bullish")
        self.assertIn(res["confidence"], ("85%", "90%"))

        # Create an artificial bearish candle series (consistently falling)
        bearish_candles = [
            {"time": 1000 + i * 300, "open": 20000 - i * 10, "high": 20000 - i * 10 + 2,
             "low": 20000 - i * 10 - 8, "close": 20000 - i * 10 - 6, "volume": 1000}
            for i in range(60)
        ]
        res_bear = live_scanner._analysis_for_index("BANKNIFTY", bearish_candles)
        self.assertEqual(res_bear["signal"], "BUY PE")
        self.assertEqual(res_bear["trend"], "Bearish")
        self.assertIn(res_bear["confidence"], ("85%", "90%"))

    def test_learning_from_loss_mechanism_task3(self):
        """TASK 3: Verify AI memory and pattern avoidance when a prior loss is recorded."""
        idx = "SENSEX"
        bullish_candles = [
            {"time": 1000 + i * 300, "open": 70000 + i * 10, "high": 70000 + i * 10 + 8,
             "low": 70000 + i * 10 - 2, "close": 70000 + i * 10 + 6, "volume": 1000}
            for i in range(60)
        ]

        # Prior to recording a loss, this produces BUY CE
        clean_eval = live_scanner._analysis_for_index(idx, bullish_candles)
        self.assertEqual(clean_eval["signal"], "BUY CE")

        current_ind = clean_eval["indicators"]

        # Record a LOSS trade with exact current indicator profile
        with self.assertLogs("alphatrade.ai.live_scanner", level="INFO") as log_capture:
            trade_id = live_scanner.record_ai_trade(
                index=idx,
                signal="BUY CE",
                entry_price=clean_eval["price"],
                exit_price=clean_eval["price"] - 200.0,
                pnl=-5000.0,
                indicators_at_entry=current_ind,
                result="LOSS",
            )
            self.assertGreater(trade_id, 0)
            # Verify the required log message was emitted
            self.assertTrue(
                any(
                    f"AI Learning: Loss recorded for {idx}. Adjusting weights for next trade." in msg
                    for msg in log_capture.output
                ),
                f"Required log message not found in {log_capture.output}",
            )

        # Retrieve trades of the day
        trades = live_scanner.get_recent_ai_trades(idx, limit=3, day_only=True)
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0]["result"], "LOSS")

        # Now re-evaluate with the same bullish candles: it must avoid the loss pattern
        learned_eval = live_scanner._analysis_for_index(idx, bullish_candles)
        self.assertEqual(learned_eval["signal"], "WAIT")
        self.assertTrue(learned_eval["avoiding_loss_pattern"])
        self.assertEqual(learned_eval["reason"], "WAIT (Avoiding previous loss pattern)")
        self.assertEqual(learned_eval["confidence"], "30%")


if __name__ == "__main__":
    unittest.main()
