"""Tests for the FII/DII cash market data module."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from backend.marketdata import fii_dii


class TestFiiDiiModule(unittest.TestCase):
    def setUp(self):
        # Reset cache before each test
        fii_dii._cache["data"] = None
        fii_dii._cache["timestamp"] = 0.0

    def test_parse_raw_fiidii(self):
        sample_nse_response = [
            {
                "category": "DII",
                "date": "27-Oct-2026",
                "buyValue": "15,702.74",
                "sellValue": "16,952.77",
                "netValue": "-1,250.03",
            },
            {
                "category": "FII/FPI",
                "date": "27-Oct-2026",
                "buyValue": "24,517.65",
                "sellValue": "20,277.26",
                "netValue": "4,240.39",
            },
        ]
        parsed = fii_dii._parse_raw_fiidii(sample_nse_response, source="test")
        self.assertEqual(parsed["date"], "27-Oct-2026")
        self.assertEqual(parsed["fii_net"], "4240.39")
        self.assertEqual(parsed["dii_net"], "-1250.03")
        self.assertEqual(parsed["fii_buy"], "24517.65")
        self.assertEqual(parsed["fii_sell"], "20277.26")
        self.assertEqual(parsed["dii_buy"], "15702.74")
        self.assertEqual(parsed["dii_sell"], "16952.77")
        self.assertEqual(parsed["source"], "test")

    def test_cache_hit_within_ttl(self):
        mock_data = {
            "date": "27-Oct-2026",
            "fii_net": "500.0",
            "dii_net": "-300.0",
            "fii_buy": "1000.0",
            "fii_sell": "500.0",
            "dii_buy": "700.0",
            "dii_sell": "1000.0",
            "updated_at": 1000000,
            "source": "cached_test",
        }
        fii_dii._cache["data"] = mock_data
        fii_dii._cache["timestamp"] = fii_dii.time.time() - 300  # 5 minutes ago (< 1 hour)

        result = fii_dii.get_fii_dii_data(force_refresh=False)
        self.assertEqual(result, mock_data)

    @patch("backend.marketdata.fii_dii._fetch_from_nse_scrape")
    def test_force_refresh_bypasses_cache(self, mock_scrape):
        old_data = {"date": "old", "fii_net": "1.0", "dii_net": "1.0"}
        fii_dii._cache["data"] = old_data
        fii_dii._cache["timestamp"] = fii_dii.time.time()

        fresh_data = {
            "date": "28-Oct-2026",
            "fii_net": "999.0",
            "dii_net": "888.0",
            "fii_buy": "2000.0",
            "fii_sell": "1001.0",
            "dii_buy": "1888.0",
            "dii_sell": "1000.0",
            "updated_at": 2000000,
            "source": "mock_fresh",
        }
        mock_scrape.return_value = fresh_data

        result = fii_dii.get_fii_dii_data(force_refresh=True)
        self.assertEqual(result["date"], "28-Oct-2026")
        self.assertEqual(result["fii_net"], "999.0")
        self.assertEqual(fii_dii._cache["data"], fresh_data)

    @patch("backend.marketdata.fii_dii._fetch_from_nsepython", return_value=None)
    @patch("backend.marketdata.fii_dii._fetch_from_nse_scrape", return_value=None)
    def test_fallback_on_complete_failure(self, _mock_scrape, _mock_nsepython):
        result = fii_dii.get_fii_dii_data(force_refresh=True)
        self.assertEqual(result["source"], "fallback")
        self.assertIn("fii_net", result)
        self.assertIn("dii_net", result)


if __name__ == "__main__":
    unittest.main()
