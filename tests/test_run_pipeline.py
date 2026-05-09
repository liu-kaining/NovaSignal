import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from src.orchestrator.run_pipeline import (
    PipelineError,
    _discover_symbols,
    _load_prompt,
    _build_raw_data,
    run_pipeline,
)


class DiscoverSymbolsTest(unittest.TestCase):
    def test_override_returns_uppercased_dicts(self):
        fmp = MagicMock()
        result = _discover_symbols(fmp, 7, ["aapl", "tsla"])
        self.assertEqual(result, [{"symbol": "AAPL"}, {"symbol": "TSLA"}])
        fmp.get_ipo_calendar.assert_not_called()

    def test_fetches_from_fmp_when_no_override(self):
        fmp = MagicMock()
        fmp.get_ipo_calendar.return_value = [
            {"symbol": "NOVA", "date": "2026-01-01", "company": "Nova Inc"},
            {"symbol": "STAR", "date": "2026-01-02"},
            {"date": "2026-01-03"},  # no symbol - skipped
        ]
        result = _discover_symbols(fmp, 7, None)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["symbol"], "NOVA")
        self.assertEqual(result[0]["company"], "Nova Inc")
        fmp.get_ipo_calendar.assert_called_once()


class FilterAlreadyProcessedTest(unittest.TestCase):
    def test_filters_existing_symbols(self):
        from src.orchestrator.run_pipeline import _filter_already_processed
        r2 = MagicMock()
        r2.list_objects.return_value = [
            "reports/2026-01-01/AAPL_report.md",
            "reports/2026-01-01/TSLA_report.md",
        ]
        discovered = [
            {"symbol": "AAPL"},
            {"symbol": "GOOG"},
            {"symbol": "TSLA"},
        ]
        result = _filter_already_processed(r2, discovered)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "GOOG")

    def test_empty_r2_returns_all(self):
        from src.orchestrator.run_pipeline import _filter_already_processed
        r2 = MagicMock()
        r2.list_objects.return_value = []
        discovered = [{"symbol": "AAPL"}, {"symbol": "GOOG"}]
        result = _filter_already_processed(r2, discovered)
        self.assertEqual(len(result), 2)


class BuildRawDataTest(unittest.TestCase):
    def test_builds_data_with_cik(self):
        fmp = MagicMock()
        fmp.get_fundraising.return_value = [{"round": "series-a"}]
        entry = {"symbol": "TEST", "cik": "0001234567", "exchange": "NASDAQ"}

        result = _build_raw_data(entry, fmp)

        self.assertEqual(result["symbol"], "TEST")
        self.assertIn("timestamp", result)
        self.assertEqual(result["ipo_data"]["exchange"], "NASDAQ")
        self.assertEqual(result["fundraising"], [{"round": "series-a"}])
        fmp.get_fundraising.assert_called_once_with("0001234567")

    def test_builds_data_without_cik(self):
        fmp = MagicMock()
        entry = {"symbol": "NOCIK", "date": "2026-01-01"}

        result = _build_raw_data(entry, fmp)

        self.assertEqual(result["fundraising"], [])
        fmp.get_fundraising.assert_not_called()

    def test_fundraising_failure_non_fatal(self):
        fmp = MagicMock()
        fmp.get_fundraising.side_effect = Exception("API error")
        entry = {"symbol": "ERR", "cik": "000111"}

        result = _build_raw_data(entry, fmp)
        self.assertEqual(result["fundraising"], [])


class LoadPromptTest(unittest.TestCase):
    def test_loads_existing_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Test Prompt")
            f.flush()
            result = _load_prompt(f.name)
        self.assertEqual(result, "# Test Prompt")

    def test_missing_file_raises_pipeline_error(self):
        with self.assertRaises(PipelineError):
            _load_prompt("/nonexistent/path.md")


class RunPipelineTest(unittest.TestCase):
    def test_invalid_mode_raises(self):
        with self.assertRaises(PipelineError):
            run_pipeline(mode="invalid")

    @patch("src.orchestrator.run_pipeline._persist_discovery")
    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    def test_discovery_only_mode(self, mock_fmp_factory, mock_persist):
        fmp = MagicMock()
        fmp.get_ipo_calendar.return_value = [
            {"symbol": "AAPL", "date": "2026-01-01"},
        ]
        mock_fmp_factory.return_value = fmp

        result = run_pipeline(mode="discovery-only")
        self.assertEqual(result["mode"], "discovery-only")
        self.assertEqual(result["symbols"], ["AAPL"])
        self.assertEqual(result["results"], [])
        mock_persist.assert_called_once()

    @patch("src.orchestrator.run_pipeline._create_r2_client")
    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.invoke_agent")
    @patch("src.orchestrator.run_pipeline.SandboxManager")
    def test_production_mode_full_flow(self, mock_sandbox_cls, mock_invoke, mock_fmp_factory, mock_r2_factory):
        # Setup FMP
        fmp = MagicMock()
        fmp.get_fundraising.return_value = []
        mock_fmp_factory.return_value = fmp

        # Setup sandbox
        mock_sandbox = MagicMock()
        mock_sandbox_cls.return_value = mock_sandbox
        mock_context = MagicMock()
        mock_context.sandbox_dir = Path("/tmp/fake")
        mock_sandbox.create.return_value = mock_context
        mock_sandbox.extract_results.return_value = {
            "report": "# Report",
            "metrics": {"confidence": 0.8},
        }

        # Setup agent
        mock_invoke.return_value = MagicMock(success=True)

        # Setup R2
        mock_r2 = MagicMock()
        mock_r2_factory.return_value = mock_r2

        # Create temp prompt
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Analyze this.")
            prompt_path = f.name

        result = run_pipeline(
            mode="production",
            symbols=["AAPL"],
            prompt_path=prompt_path,
            concurrency=1,
            sandbox_timeout=10,
        )

        self.assertEqual(result["mode"], "production")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        mock_r2.upload_report.assert_called_once()
        mock_r2.upload_metrics.assert_called_once()
        mock_sandbox.cleanup.assert_called_once()

    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.invoke_agent")
    @patch("src.orchestrator.run_pipeline.SandboxManager")
    def test_dev_mode_skips_upload(self, mock_sandbox_cls, mock_invoke, mock_fmp_factory):
        fmp = MagicMock()
        fmp.get_fundraising.return_value = []
        mock_fmp_factory.return_value = fmp

        mock_sandbox = MagicMock()
        mock_sandbox_cls.return_value = mock_sandbox
        mock_context = MagicMock()
        mock_context.sandbox_dir = Path("/tmp/fake")
        mock_sandbox.create.return_value = mock_context
        mock_sandbox.extract_results.return_value = {"report": "# Dev Report", "metrics": None}

        mock_invoke.return_value = MagicMock(success=True)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Prompt")
            prompt_path = f.name

        result = run_pipeline(
            mode="dev",
            symbols=["TEST"],
            prompt_path=prompt_path,
        )

        self.assertEqual(result["mode"], "dev")
        self.assertEqual(result["success_count"], 1)

    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.invoke_agent")
    @patch("src.orchestrator.run_pipeline.SandboxManager")
    def test_agent_failure_captured(self, mock_sandbox_cls, mock_invoke, mock_fmp_factory):
        from src.orchestrator.async_runner import AgentRunError

        fmp = MagicMock()
        fmp.get_fundraising.return_value = []
        mock_fmp_factory.return_value = fmp

        mock_sandbox = MagicMock()
        mock_sandbox_cls.return_value = mock_sandbox
        mock_context = MagicMock()
        mock_context.sandbox_dir = Path("/tmp/fake")
        mock_sandbox.create.return_value = mock_context

        mock_invoke.side_effect = AgentRunError("timeout")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("Prompt")
            prompt_path = f.name

        result = run_pipeline(
            mode="dev",
            symbols=["FAIL"],
            prompt_path=prompt_path,
        )

        self.assertEqual(result["failure_count"], 1)
        self.assertFalse(result["results"][0]["success"])
        mock_sandbox.cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
