import json
import unittest
from unittest.mock import MagicMock, patch

from src.evolution.deviation import DeviationCalculator, DeviationError
from src.storage.r2_client import R2StorageError


class DeviationCalculatorTest(unittest.TestCase):
    def _make_calculator(self, threshold=0.15):
        fmp = MagicMock()
        r2 = MagicMock()
        calc = DeviationCalculator(fmp, r2, threshold=threshold)
        return calc, fmp, r2

    def test_evaluate_within_threshold(self):
        calc, fmp, r2 = self._make_calculator(threshold=0.15)

        metrics = {"predicted_price": 100.0, "confidence": 0.8}
        r2.download_file.return_value = json.dumps(metrics).encode()
        fmp.get_stock_price_historical.return_value = [{"close": 95.0}]

        # T+30: metrics_date=2026-01-15, should fetch price for 2026-02-14
        result = calc.evaluate("AAPL", "2026-01-15")

        self.assertTrue(result["within_threshold"])
        self.assertAlmostEqual(result["deviation"], (100 - 95) / 95, places=5)
        self.assertEqual(result["predicted_price"], 100.0)
        self.assertEqual(result["actual_price"], 95.0)
        # Verify T+30 date was used for price fetch
        fmp.get_stock_price_historical.assert_called_with("AAPL", "2026-02-14", "2026-02-19")

    def test_evaluate_exceeds_threshold(self):
        calc, fmp, r2 = self._make_calculator(threshold=0.05)

        metrics = {"predicted_price": 120.0, "confidence": 0.6}
        r2.download_file.return_value = json.dumps(metrics).encode()
        fmp.get_stock_price_historical.return_value = [{"close": 100.0}]

        result = calc.evaluate("TSLA", "2026-02-01")

        self.assertFalse(result["within_threshold"])
        self.assertAlmostEqual(result["deviation"], 0.2, places=5)

    def test_evaluate_no_predicted_price_raises(self):
        calc, fmp, r2 = self._make_calculator()

        metrics = {"confidence": 0.9}  # missing predicted_price
        r2.download_file.return_value = json.dumps(metrics).encode()

        with self.assertRaises(DeviationError) as ctx:
            calc.evaluate("GOOG", "2026-01-01")
        self.assertIn("predicted_price", str(ctx.exception))

    def test_evaluate_metrics_not_found_raises(self):
        calc, fmp, r2 = self._make_calculator()
        r2.download_file.side_effect = R2StorageError("not found")

        with self.assertRaises(DeviationError):
            calc.evaluate("MISS", "2026-01-01")

    def test_evaluate_no_historical_data_raises(self):
        calc, fmp, r2 = self._make_calculator()

        metrics = {"predicted_price": 50.0}
        r2.download_file.return_value = json.dumps(metrics).encode()
        fmp.get_stock_price_historical.return_value = []

        with self.assertRaises(DeviationError) as ctx:
            calc.evaluate("EMPTY", "2026-03-01")
        self.assertIn("No historical price", str(ctx.exception))

    def test_evaluate_batch_collects_results(self):
        calc, fmp, r2 = self._make_calculator()

        metrics_good = {"predicted_price": 100.0, "confidence": 0.8}
        metrics_bad = {"predicted_price": 200.0, "confidence": 0.5}

        def download_side_effect(key):
            if "GOOD" in key:
                return json.dumps(metrics_good).encode()
            if "BAD" in key:
                return json.dumps(metrics_bad).encode()
            raise R2StorageError("not found")

        r2.download_file.side_effect = download_side_effect
        fmp.get_stock_price_historical.return_value = [{"close": 100.0}]

        results = calc.evaluate_batch(["GOOD", "BAD", "MISS"], "2026-01-01")

        self.assertEqual(len(results), 3)
        self.assertTrue(results[0]["within_threshold"])
        self.assertFalse(results[1]["within_threshold"])
        self.assertIn("error", results[2])

    def test_evaluate_uses_adj_close_fallback(self):
        calc, fmp, r2 = self._make_calculator()

        metrics = {"predicted_price": 50.0}
        r2.download_file.return_value = json.dumps(metrics).encode()
        fmp.get_stock_price_historical.return_value = [{"adjClose": 48.0}]

        result = calc.evaluate("ADJ", "2026-01-01")
        self.assertEqual(result["actual_price"], 48.0)

    def test_save_history_creates_new(self):
        calc, fmp, r2 = self._make_calculator()
        r2.download_file.side_effect = R2StorageError("not found")

        results = [{"symbol": "A", "deviation": 0.05}]
        key = calc.save_history(results)

        self.assertEqual(key, "state/evolution/deviation_history.json")
        r2.upload_raw.assert_called_once()
        call_args = r2.upload_raw.call_args
        body = json.loads(call_args[0][1].decode())
        self.assertEqual(len(body), 1)

    def test_save_history_appends_to_existing(self):
        calc, fmp, r2 = self._make_calculator()
        existing = [{"symbol": "OLD", "deviation": 0.02}]
        r2.download_file.return_value = json.dumps(existing).encode()

        new_results = [{"symbol": "NEW", "deviation": 0.1}]
        calc.save_history(new_results)

        call_args = r2.upload_raw.call_args
        body = json.loads(call_args[0][1].decode())
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["symbol"], "OLD")
        self.assertEqual(body[1]["symbol"], "NEW")

    def test_calculate_deviation_zero_actual(self):
        result = DeviationCalculator._calculate_deviation(100, 0)
        self.assertEqual(result, 0.0)

    def test_r2_download_called_with_correct_key(self):
        calc, fmp, r2 = self._make_calculator()
        metrics = {"predicted_price": 10.0}
        r2.download_file.return_value = json.dumps(metrics).encode()
        fmp.get_stock_price_historical.return_value = [{"close": 10.0}]

        calc.evaluate("nvda", "2026-05-01")
        r2.download_file.assert_called_with("metrics/2026-05-01/NVDA_metrics.json")


if __name__ == "__main__":
    unittest.main()
