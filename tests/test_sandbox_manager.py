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


class MultiStageSandboxTest(unittest.TestCase):
    """Tests for the new multi-stage sandbox API."""

    def setUp(self):
        self.base_dir = tempfile.mkdtemp()
        self.manager = SandboxManager(base_dir=self.base_dir)

    def test_setup_drafter_sandbox_creates_index_and_data(self):
        raw = {"symbol": "VIDA", "timestamp": "2026-05-18", "ipo_data": {"exchange": "NYSE"}}
        drafter_dir = self.manager.setup_drafter_sandbox(
            "vida", raw, drafter_prompt="DRAFTER PROMPT"
        )
        try:
            self.assertTrue((drafter_dir / "prompt.md").exists())
            self.assertEqual((drafter_dir / "prompt.md").read_text(encoding="utf-8"), "DRAFTER PROMPT")
            self.assertTrue((drafter_dir / "INDEX.md").exists())
            self.assertTrue((drafter_dir / "data").is_dir())
            self.assertTrue((drafter_dir / "data" / "ipo.json").exists())
            self.assertTrue((drafter_dir / "research_notes.md").exists())
            self.assertIn("VIDA", (drafter_dir / "INDEX.md").read_text(encoding="utf-8"))
            # data/ipo.json should be valid JSON
            json.loads((drafter_dir / "data" / "ipo.json").read_text(encoding="utf-8"))
        finally:
            self.manager.cleanup_paths(drafter_dir)

    def test_setup_drafter_rejects_empty_symbol(self):
        with self.assertRaises(SandboxError):
            self.manager.setup_drafter_sandbox("", {}, drafter_prompt="x")

    def test_setup_reviewer_sandbox_copies_drafter_outputs(self):
        drafter_dir = self.manager.setup_drafter_sandbox(
            "vida", {"symbol": "VIDA"}, drafter_prompt="d"
        )
        try:
            (drafter_dir / "report.md").write_text("# Drafter Report", encoding="utf-8")
            (drafter_dir / "metrics.json").write_text("{}", encoding="utf-8")
            (drafter_dir / "research_notes.md").write_text("# Notes", encoding="utf-8")

            reviewer_dir = self.manager.setup_reviewer_sandbox(
                "vida", drafter_dir, reviewer_prompt="REVIEWER"
            )
            try:
                self.assertTrue((reviewer_dir / "prompt.md").exists())
                self.assertEqual(
                    (reviewer_dir / "prompt.md").read_text(encoding="utf-8"), "REVIEWER"
                )
                self.assertTrue((reviewer_dir / "report_v1.md").exists())
                self.assertEqual(
                    (reviewer_dir / "report_v1.md").read_text(encoding="utf-8"),
                    "# Drafter Report",
                )
                self.assertTrue((reviewer_dir / "metrics_v1.json").exists())
                self.assertTrue((reviewer_dir / "research_notes.md").exists())
                self.assertTrue((reviewer_dir / "data" / "ipo.json").exists())
                self.assertTrue((reviewer_dir / "INDEX.md").exists())
            finally:
                self.manager.cleanup_paths(reviewer_dir)
        finally:
            self.manager.cleanup_paths(drafter_dir)

    def test_inject_critique_copies_and_swaps_prompt(self):
        drafter_dir = self.manager.setup_drafter_sandbox(
            "vida", {"symbol": "VIDA"}, drafter_prompt="d"
        )
        (drafter_dir / "report.md").write_text("# Draft", encoding="utf-8")
        (drafter_dir / "metrics.json").write_text("{}", encoding="utf-8")
        reviewer_dir = self.manager.setup_reviewer_sandbox(
            "vida", drafter_dir, reviewer_prompt="r"
        )
        try:
            (reviewer_dir / "critique.md").write_text(
                "# Critique\n\nFix X.", encoding="utf-8"
            )
            self.manager.inject_critique(
                drafter_dir, reviewer_dir, reviser_prompt="REVISER PROMPT"
            )
            self.assertEqual(
                (drafter_dir / "prompt.md").read_text(encoding="utf-8"),
                "REVISER PROMPT",
            )
            self.assertTrue((drafter_dir / "critique.md").exists())
            self.assertIn("Fix X.", (drafter_dir / "critique.md").read_text(encoding="utf-8"))
        finally:
            self.manager.cleanup_paths(drafter_dir, reviewer_dir)

    def test_inject_critique_writes_placeholder_when_missing(self):
        drafter_dir = self.manager.setup_drafter_sandbox(
            "vida", {"symbol": "VIDA"}, drafter_prompt="d"
        )
        reviewer_dir = self.manager.setup_reviewer_sandbox(
            "vida", drafter_dir, reviewer_prompt="r"
        )
        try:
            self.manager.inject_critique(drafter_dir, reviewer_dir, reviser_prompt="v")
            self.assertTrue((drafter_dir / "critique.md").exists())
            content = (drafter_dir / "critique.md").read_text(encoding="utf-8")
            self.assertIn("NEEDS_REVISION", content)
        finally:
            self.manager.cleanup_paths(drafter_dir, reviewer_dir)

    def test_extract_final_artifacts(self):
        drafter_dir = self.manager.setup_drafter_sandbox(
            "vida", {"symbol": "VIDA"}, drafter_prompt="d"
        )
        try:
            (drafter_dir / "report.md").write_text("# Final", encoding="utf-8")
            (drafter_dir / "metrics.json").write_text(
                '{"confidence": 0.7}', encoding="utf-8"
            )
            (drafter_dir / "research_notes.md").write_text(
                "# Notes", encoding="utf-8"
            )
            (drafter_dir / "critique.md").write_text("# Crit", encoding="utf-8")

            artifacts = self.manager.extract_final_artifacts(drafter_dir)
            self.assertEqual(artifacts["report"], "# Final")
            self.assertEqual(artifacts["metrics"]["confidence"], 0.7)
            self.assertEqual(artifacts["research_notes"], "# Notes")
            self.assertEqual(artifacts["critique"], "# Crit")
        finally:
            self.manager.cleanup_paths(drafter_dir)

    def test_cleanup_paths_handles_missing_safely(self):
        # Should not raise even if path doesn't exist or is None
        self.manager.cleanup_paths(None, Path("/tmp/does_not_exist_xyz"))


if __name__ == "__main__":
    unittest.main()
