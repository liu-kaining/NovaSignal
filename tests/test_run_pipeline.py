import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.orchestrator.multi_stage_runner import (
    MultiStageResult,
    StageOutcome,
)
from src.orchestrator.run_pipeline import (
    PipelineError,
    _build_raw_data,
    _discover_symbols,
    _load_prompt,
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

    def test_malformed_or_stray_keys_ignored(self):
        from src.orchestrator.run_pipeline import _filter_already_processed
        r2 = MagicMock()
        r2.list_objects.return_value = [
            "reports/2026-01-01/AAPL_report.md",
            "reports/2026-01-01/notes.tmp",
            "reports/2026-01-01/random.md",
            "wrong-prefix/2026-01-01/X_report.md",
        ]
        discovered = [{"symbol": "AAPL"}, {"symbol": "GOOG"}]
        result = _filter_already_processed(r2, discovered)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["symbol"], "GOOG")


def _fmp_mock_for_raw_data() -> MagicMock:
    fmp = MagicMock()
    fmp.get_company_profile.return_value = None
    fmp.get_income_statement.return_value = []
    fmp.get_balance_sheet_statement.return_value = []
    fmp.get_cash_flow_statement.return_value = []
    fmp.get_key_metrics.return_value = []
    fmp.get_ratios.return_value = []
    fmp.get_enterprise_values.return_value = []
    fmp.get_key_metrics_ttm.return_value = None
    fmp.get_ratios_ttm.return_value = None
    fmp.get_stock_price_historical.return_value = []
    fmp.get_stock_news.return_value = []
    fmp.get_press_releases.return_value = []
    fmp.get_ipos_disclosure.return_value = []
    fmp.get_ipos_prospectus.return_value = []
    fmp.get_quote.return_value = None
    fmp.get_stock_peers.return_value = []
    fmp.get_key_executives.return_value = []
    fmp.get_shares_float.return_value = None
    fmp.get_financial_scores.return_value = None
    fmp.get_analyst_estimates.return_value = []
    fmp.get_price_target_summary.return_value = None
    fmp.get_price_target_consensus.return_value = None
    fmp.get_ratings_snapshot.return_value = None
    fmp.get_sec_filings_symbol.return_value = []
    fmp.get_insider_trading_statistics.return_value = None
    fmp.get_insider_trading_search.return_value = []
    fmp.get_revenue_product_segmentation.return_value = []
    fmp.get_revenue_geographic_segmentation.return_value = []
    fmp.get_company_notes.return_value = []
    fmp.get_treasury_rates.return_value = []
    fmp.get_market_risk_premium.return_value = None
    fmp.get_economic_calendar.return_value = []
    fmp.get_economic_indicators.return_value = []
    fmp.get_sector_performance_snapshot.return_value = []
    fmp.get_industry_performance_snapshot.return_value = []
    fmp.get_sector_pe_snapshot.return_value = []
    fmp.get_industry_pe_snapshot.return_value = []
    fmp.get_historical_sector_performance.return_value = []
    fmp.get_historical_industry_performance.return_value = []
    fmp.get_etf_sector_weightings.return_value = []
    fmp.get_batch_quote.return_value = []
    return fmp


class BuildRawDataTest(unittest.TestCase):
    def test_builds_data_with_cik(self):
        fmp = _fmp_mock_for_raw_data()
        fmp.get_company_profile.return_value = {"sector": "Software", "cik": "0001234567"}
        fmp.get_fundraising.return_value = [{"round": "series-a"}]
        entry = {"symbol": "TEST", "cik": "0001234567", "exchange": "NASDAQ"}

        result = _build_raw_data(entry, fmp)

        self.assertEqual(result["symbol"], "TEST")
        self.assertIn("timestamp", result)
        self.assertEqual(result["ipo_data"]["exchange"], "NASDAQ")
        self.assertEqual(result["fundraising"], [{"round": "series-a"}])
        self.assertEqual(result["resolved_cik"], "0001234567")
        fmp.get_fundraising.assert_called_once_with("0001234567")

    def test_resolves_cik_from_profile_when_entry_missing(self):
        fmp = _fmp_mock_for_raw_data()
        fmp.get_company_profile.return_value = {"cik": "320193", "sector": "Tech"}
        fmp.get_fundraising.return_value = [{"s": 1}]
        entry = {"symbol": "AAPL"}

        result = _build_raw_data(entry, fmp)

        self.assertEqual(result["resolved_cik"], "0000320193")
        fmp.get_fundraising.assert_called_once_with("0000320193")

    def test_builds_data_without_cik(self):
        fmp = _fmp_mock_for_raw_data()
        fmp.get_company_profile.return_value = {}
        entry = {"symbol": "NOCIK", "date": "2026-01-01"}

        result = _build_raw_data(entry, fmp)

        self.assertEqual(result["fundraising"], [])
        self.assertIsNone(result["resolved_cik"])
        fmp.get_fundraising.assert_not_called()

    def test_fundraising_failure_non_fatal(self):
        fmp = _fmp_mock_for_raw_data()
        fmp.get_fundraising.side_effect = Exception("API error")
        entry = {"symbol": "ERR", "cik": "000111"}

        result = _build_raw_data(entry, fmp)
        self.assertEqual(result["fundraising"], [])
        self.assertTrue(any(e["step"] == "fundraising" for e in result["fetch_errors"]))

    def test_enrichment_shapes(self):
        fmp = _fmp_mock_for_raw_data()
        fmp.get_company_profile.return_value = {"companyName": "X"}
        fmp.get_income_statement.side_effect = [
            [{"fiscalYear": "2024"}],
            [{"period": "Q1"}],
        ]
        fmp.get_balance_sheet_statement.return_value = [{"totalAssets": 1}]
        fmp.get_stock_price_historical.side_effect = [
            [{"date": "2026-01-10", "close": 10}],
            [{"date": "2026-01-10", "close": 100}],
        ]
        fmp.get_stock_news.return_value = [{"title": "n1"}]
        entry = {"symbol": "ZZ", "date": "2025-06-01"}

        result = _build_raw_data(entry, fmp)

        self.assertEqual(result["company_profile"]["companyName"], "X")
        self.assertEqual(len(result["financials"]["income_statement_annual"]), 1)
        self.assertEqual(result["market_context"]["company_eod_recent"][0]["close"], 10)
        self.assertEqual(result["news_context"]["stock_news"][0]["title"], "n1")
        self.assertIn("fmp_profile", result["data_sources"])

    def test_ipo_regulatory_matches_prefetched_lists(self):
        fmp = _fmp_mock_for_raw_data()
        fmp.get_company_profile.return_value = {}
        lists = {
            "disclosures": [
                {"symbol": "ABC", "form": "S-1"},
                {"symbol": "ZZZ"},
            ],
            "prospectuses": [{"ticker": "ABC", "url": "https://sec.gov/x"}],
        }
        result = _build_raw_data({"symbol": "ABC"}, fmp, ipo_regulatory_lists=lists)
        self.assertEqual(
            result["ipo_regulatory_context"]["disclosure_filings_matched"],
            [{"symbol": "ABC", "form": "S-1"}],
        )
        self.assertEqual(
            result["ipo_regulatory_context"]["prospectus_entries_matched"],
            [{"ticker": "ABC", "url": "https://sec.gov/x"}],
        )

    def test_global_market_prefetch_compacted_into_raw_data(self):
        fmp = _fmp_mock_for_raw_data()
        fmp.get_company_profile.return_value = {}
        cache = {
            "prefetch_as_of": "2026-01-01",
            "treasury_rates": [{"d": i} for i in range(40)],
            "market_risk_premium": {"usa": 5.0},
            "economic_calendar": [{"e": i} for i in range(60)],
            "economic_indicators": {"GDP": [{"v": i} for i in range(20)]},
            "sector_performance_snapshot": [],
            "industry_performance_snapshot": [],
            "sector_pe_snapshot": [],
            "industry_pe_snapshot": [],
            "etf_sector_weightings_spy": [{"s": "Tech"}],
            "major_index_batch_quotes": [],
        }
        result = _build_raw_data({"symbol": "X"}, fmp, global_market_context=cache)
        gmc = result["global_market_context"]
        self.assertIsNotNone(gmc)
        self.assertEqual(len(gmc["treasury_rates_tail"]), 30)
        self.assertEqual(len(gmc["economic_calendar"]), 50)
        self.assertEqual(len(gmc["economic_indicators_trimmed"]["GDP"]), 14)


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


class ParseReportStorageKeyTest(unittest.TestCase):
    def test_parses_canonical_key(self):
        from src.storage.r2_client import parse_report_storage_key

        self.assertEqual(
            parse_report_storage_key("reports/2026-01-01/BRK.B_report.md"),
            ("2026-01-01", "BRK.B"),
        )

    def test_rejects_noncanonical(self):
        from src.storage.r2_client import parse_report_storage_key

        self.assertIsNone(parse_report_storage_key("reports/2026-01-01/foo.md"))
        self.assertIsNone(parse_report_storage_key("metrics/2026-01-01/AAPL_metrics.json"))


def _write_three_prompts() -> tuple[str, str, str]:
    """Write tiny placeholder drafter/reviewer/reviser prompts and return paths."""
    paths = []
    for label in ("drafter", "reviewer", "reviser"):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=f"_{label}.md", delete=False, encoding="utf-8"
        ) as f:
            f.write(f"# {label.title()} Prompt (test)")
            paths.append(f.name)
    return tuple(paths)  # type: ignore[return-value]


def _make_successful_stage_result(symbol: str) -> MultiStageResult:
    return MultiStageResult(
        symbol=symbol,
        success=True,
        final_gate_passed=True,
        report="# Final Report",
        metrics={"confidence": 0.7, "subscription_recommendation": "subscribe"},
        research_notes="# Research Notes",
        critique="# Critique",
        outcomes=[
            StageOutcome(stage="drafter", attempt=1, success=True, duration_seconds=1.0),
            StageOutcome(stage="reviewer", attempt=1, success=True, duration_seconds=0.5),
            StageOutcome(stage="reviser", attempt=1, success=True, duration_seconds=0.5),
        ],
    )


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

        d_path, r_path, v_path = _write_three_prompts()
        result = run_pipeline(
            mode="discovery-only",
            drafter_prompt_path=d_path,
            reviewer_prompt_path=r_path,
            reviser_prompt_path=v_path,
        )
        self.assertEqual(result["mode"], "discovery-only")
        self.assertEqual(result["symbols"], ["AAPL"])
        self.assertEqual(result["results"], [])
        mock_persist.assert_called_once()

    @patch("src.orchestrator.run_pipeline._create_r2_client")
    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.execute_multi_stage")
    def test_production_mode_full_flow(self, mock_exec, mock_fmp_factory, mock_r2_factory):
        fmp = _fmp_mock_for_raw_data()
        mock_fmp_factory.return_value = fmp

        async def _exec(**kwargs):
            return _make_successful_stage_result(kwargs["symbol"])

        mock_exec.side_effect = _exec

        mock_r2 = MagicMock()
        mock_r2_factory.return_value = mock_r2

        d_path, r_path, v_path = _write_three_prompts()
        result = run_pipeline(
            mode="production",
            symbols=["AAPL"],
            drafter_prompt_path=d_path,
            reviewer_prompt_path=r_path,
            reviser_prompt_path=v_path,
            concurrency=1,
            sandbox_timeout=10,
        )

        self.assertEqual(result["mode"], "production")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        mock_r2.upload_fmp_prefetch_bundle.assert_called_once()
        mock_r2.upload_raw_data.assert_called_once()
        mock_r2.upload_report.assert_called_once()
        mock_r2.upload_metrics.assert_called_once()
        mock_r2.upload_research_notes.assert_called_once()
        mock_r2.upload_critique.assert_called_once()
        mock_r2.upload_quality_gate.assert_called_once()

    @patch("src.orchestrator.run_pipeline._create_r2_client")
    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.execute_multi_stage")
    def test_production_skip_upload_does_not_touch_r2(
        self, mock_exec, mock_fmp_factory, mock_r2_factory
    ):
        fmp = _fmp_mock_for_raw_data()
        mock_fmp_factory.return_value = fmp

        async def _exec(**kwargs):
            return _make_successful_stage_result(kwargs["symbol"])

        mock_exec.side_effect = _exec

        d_path, r_path, v_path = _write_three_prompts()
        result = run_pipeline(
            mode="production",
            symbols=["AAPL"],
            drafter_prompt_path=d_path,
            reviewer_prompt_path=r_path,
            reviser_prompt_path=v_path,
            skip_upload=True,
            concurrency=1,
            sandbox_timeout=10,
        )
        self.assertEqual(result["success_count"], 1)
        mock_r2_factory.assert_not_called()

    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.execute_multi_stage")
    def test_dev_mode_skips_upload(self, mock_exec, mock_fmp_factory):
        fmp = _fmp_mock_for_raw_data()
        mock_fmp_factory.return_value = fmp

        async def _exec(**kwargs):
            return _make_successful_stage_result(kwargs["symbol"])

        mock_exec.side_effect = _exec

        d_path, r_path, v_path = _write_three_prompts()
        result = run_pipeline(
            mode="dev",
            symbols=["TEST"],
            drafter_prompt_path=d_path,
            reviewer_prompt_path=r_path,
            reviser_prompt_path=v_path,
        )

        self.assertEqual(result["mode"], "dev")
        self.assertEqual(result["success_count"], 1)

    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.execute_multi_stage")
    def test_agent_failure_captured(self, mock_exec, mock_fmp_factory):
        fmp = _fmp_mock_for_raw_data()
        mock_fmp_factory.return_value = fmp

        async def _exec(**kwargs):
            return MultiStageResult(
                symbol=kwargs["symbol"],
                success=False,
                final_gate_passed=False,
                error="Drafter failed to produce report.md after all attempts",
            )

        mock_exec.side_effect = _exec

        d_path, r_path, v_path = _write_three_prompts()
        result = run_pipeline(
            mode="dev",
            symbols=["FAIL"],
            drafter_prompt_path=d_path,
            reviewer_prompt_path=r_path,
            reviser_prompt_path=v_path,
        )

        self.assertEqual(result["failure_count"], 1)
        self.assertFalse(result["results"][0]["success"])

    @patch("src.orchestrator.run_pipeline._create_fmp_client")
    @patch("src.orchestrator.run_pipeline.execute_multi_stage")
    def test_max_symbols_caps_run(self, mock_exec, mock_fmp_factory):
        fmp = _fmp_mock_for_raw_data()
        mock_fmp_factory.return_value = fmp

        async def _exec(**kwargs):
            return _make_successful_stage_result(kwargs["symbol"])

        mock_exec.side_effect = _exec

        d_path, r_path, v_path = _write_three_prompts()
        result = run_pipeline(
            mode="dev",
            symbols=["A", "B", "C", "D", "E", "F", "G"],
            drafter_prompt_path=d_path,
            reviewer_prompt_path=r_path,
            reviser_prompt_path=v_path,
            max_symbols=3,
        )

        self.assertEqual(len(result["symbols"]), 3)
        self.assertEqual(result["success_count"], 3)


if __name__ == "__main__":
    unittest.main()
