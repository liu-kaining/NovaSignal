import tempfile
import unittest
from pathlib import Path

from config.loader import load_settings, get_fmp_settings, get_r2_settings, get_pipeline_settings, get_evolution_settings


class ConfigLoaderTest(unittest.TestCase):
    def test_load_default_settings(self):
        settings = load_settings()
        self.assertIn("project", settings)
        self.assertEqual(settings["project"]["name"], "NovaSignal")

    def test_fmp_settings(self):
        fmp = get_fmp_settings()
        self.assertEqual(fmp["timeout_seconds"], 30)
        self.assertIn("retry", fmp)
        self.assertEqual(fmp["retry"]["attempts"], 6)

    def test_r2_settings(self):
        r2 = get_r2_settings()
        self.assertEqual(r2["bucket_name"], "novasignal")
        self.assertIn("retry", r2)

    def test_pipeline_settings(self):
        pipeline = get_pipeline_settings()
        # Per-stage default timeout raised to 900s after observed 480s drafter timeouts
        self.assertEqual(pipeline["sandbox_timeout_seconds"], 900)
        # Concurrency reduced to sequential — 3-stage flow is too heavy for parallelism
        self.assertEqual(pipeline["concurrency"], 1)
        # New multi-stage knobs
        self.assertIn("stages", pipeline)
        self.assertEqual(pipeline["stages"]["drafter_timeout_seconds"], 900)
        self.assertEqual(pipeline["stages"]["reviewer_timeout_seconds"], 480)
        self.assertEqual(pipeline["stages"]["reviser_timeout_seconds"], 600)
        self.assertEqual(pipeline["stages"]["drafter_max_attempts"], 2)
        # Default max symbols per run reduced to 3 (quality > quantity, fits 120-min job ceiling)
        self.assertEqual(pipeline["max_symbols_per_run"], 3)

    def test_evolution_settings(self):
        evo = get_evolution_settings()
        self.assertEqual(evo["deviation_threshold"], 0.15)
        self.assertEqual(evo["min_samples_for_correction"], 5)
        self.assertEqual(evo["lookback_days"], 30)

    def test_load_custom_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("custom:\n  key: value\n")
            f.flush()
            settings = load_settings(f.name)
        self.assertEqual(settings["custom"]["key"], "value")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_settings("/nonexistent/settings.yaml")


if __name__ == "__main__":
    unittest.main()
