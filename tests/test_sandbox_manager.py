import json
import tempfile
import unittest
from pathlib import Path

from src.orchestrator.sandbox_manager import SandboxContext, SandboxError, SandboxManager


class SandboxManagerTest(unittest.TestCase):
    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.manager = SandboxManager(base_dir=self.base_dir)

    def test_create_sandbox_creates_directory_and_files(self):
        raw_data = {"symbol": "NOVA", "price": 42.0}
        prompt = "Analyze this IPO."

        ctx = self.manager.create("nova", raw_data, prompt)
        try:
            self.assertTrue(ctx.sandbox_dir.exists())
            self.assertTrue(ctx.raw_data_path.exists())
            self.assertTrue(ctx.prompt_path.exists())
            self.assertEqual(ctx.symbol, "NOVA")

            loaded = json.loads(ctx.raw_data_path.read_text())
            self.assertEqual(loaded["price"], 42.0)
            self.assertEqual(ctx.prompt_path.read_text(), "Analyze this IPO.")
        finally:
            self.manager.cleanup(ctx)

    def test_create_sandbox_symbol_uppercased(self):
        ctx = self.manager.create("aapl", {"data": 1}, "prompt")
        try:
            self.assertEqual(ctx.symbol, "AAPL")
            self.assertIn("AAPL", str(ctx.sandbox_dir))
        finally:
            self.manager.cleanup(ctx)

    def test_create_sandbox_empty_symbol_raises(self):
        with self.assertRaises(SandboxError):
            self.manager.create("", {}, "prompt")

    def test_create_sandbox_whitespace_symbol_raises(self):
        with self.assertRaises(SandboxError):
            self.manager.create("   ", {}, "prompt")

    def test_cleanup_removes_directory(self):
        ctx = self.manager.create("TEST", {"x": 1}, "p")
        sandbox_path = ctx.sandbox_dir
        self.assertTrue(sandbox_path.exists())

        self.manager.cleanup(ctx)
        self.assertFalse(sandbox_path.exists())

    def test_cleanup_nonexistent_directory_is_safe(self):
        ctx = SandboxContext(
            sandbox_dir=Path("/tmp/nonexistent_sandbox_xyz"),
            symbol="X",
        )
        self.manager.cleanup(ctx)

    def test_extract_results_with_both_files(self):
        ctx = self.manager.create("SYM", {"raw": True}, "prompt")
        try:
            ctx.report_path.write_text("# Analysis Report")
            metrics = {"confidence": 0.9, "signal": "bullish"}
            ctx.metrics_path.write_text(json.dumps(metrics))

            results = self.manager.extract_results(ctx)
            self.assertEqual(results["report"], "# Analysis Report")
            self.assertEqual(results["metrics"]["confidence"], 0.9)
        finally:
            self.manager.cleanup(ctx)

    def test_extract_results_missing_report(self):
        ctx = self.manager.create("SYM", {}, "prompt")
        try:
            results = self.manager.extract_results(ctx)
            self.assertIsNone(results["report"])
            self.assertIsNone(results["metrics"])
        finally:
            self.manager.cleanup(ctx)

    def test_extract_results_invalid_metrics_json(self):
        ctx = self.manager.create("SYM", {}, "prompt")
        try:
            ctx.metrics_path.write_text("not valid json {{{")
            results = self.manager.extract_results(ctx)
            self.assertIsNone(results["metrics"])
        finally:
            self.manager.cleanup(ctx)

    def test_sandbox_context_paths(self):
        ctx = SandboxContext(sandbox_dir=Path("/tmp/test"), symbol="X")
        self.assertEqual(ctx.raw_data_path, Path("/tmp/test/raw_data.json"))
        self.assertEqual(ctx.report_path, Path("/tmp/test/report.md"))
        self.assertEqual(ctx.metrics_path, Path("/tmp/test/metrics.json"))
        self.assertEqual(ctx.prompt_path, Path("/tmp/test/prompt.md"))


if __name__ == "__main__":
    unittest.main()
