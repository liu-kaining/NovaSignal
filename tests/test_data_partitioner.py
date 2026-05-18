import json
import unittest

from src.orchestrator.data_partitioner import (
    build_index_md,
    split_raw_data,
)


def _minimal_raw_data() -> dict:
    return {
        "symbol": "VIDA",
        "timestamp": "2026-05-18",
        "data_sources": ["fmp_ipo_calendar_row", "fmp_profile"],
        "ipo_data": {
            "date": "2026-05-15",
            "exchange": "NYSE",
            "shares": 3_750_000,
            "company": "Vidaroo Corp",
        },
        "resolved_cik": None,
        "company_profile": {
            "companyName": "Vidaroo Corp",
            "industry": None,
        },
        "company_notes": [],
        "sector_industry_context": {
            "identifiers": {"sector": None, "industry": None},
            "historical_sector_performance": [],
            "historical_industry_performance": [],
        },
        "financials": {
            "income_statement_annual": [],
            "income_statement_quarter": [],
            "balance_sheet_annual": [],
            "cash_flow_annual": [],
            "key_metrics_annual": [],
            "ratios_annual": [],
            "enterprise_values_annual": [],
            "key_metrics_ttm": None,
            "ratios_ttm": None,
        },
        "fundraising": [],
        "market_context": {
            "benchmark_symbol": "SPY",
            "company_eod_recent": [],
            "benchmark_eod_recent": [],
            "quote": None,
        },
        "news_context": {"stock_news": [], "press_releases": []},
        "comparables": {"stock_peers": []},
        "ownership_governance": {
            "key_executives": [],
            "shares_float": None,
            "insider_trading_statistics": None,
            "insider_trades_search": [],
        },
        "sell_side": {
            "analyst_estimates_annual": [],
            "price_target_summary": None,
            "price_target_consensus": None,
            "ratings_snapshot": None,
        },
        "fundamental_extras": {
            "financial_scores": None,
            "revenue_product_segmentation": [],
            "revenue_geographic_segmentation": [],
        },
        "sec_filings_recent": {
            "window_from": "2026-04-01",
            "window_to": "2026-05-18",
            "filings": [],
        },
        "ipo_regulatory_context": {
            "disclosure_filings_matched": [],
            "prospectus_entries_matched": [],
        },
        "global_market_context": {
            "prefetch_as_of": "2026-05-18",
            "treasury_rates_tail": [{"d": 1}, {"d": 2}],
            "market_risk_premium": {"usa": 5.0},
            "economic_calendar": [],
            "economic_indicators_trimmed": {"GDP": [{"v": 1}]},
            "sector_performance_snapshot": [],
            "industry_performance_snapshot": [],
            "sector_pe_snapshot": [],
            "industry_pe_snapshot": [],
            "etf_sector_weightings_spy": [{"s": "Tech"}],
            "major_index_batch_quotes": [],
        },
        "fetch_errors": [{"step": "stock_peers", "error": "404 not found"}],
    }


class SplitRawDataTest(unittest.TestCase):
    def test_emits_all_partitions(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        expected = {
            "ipo.json",
            "profile.json",
            "financials.json",
            "ownership.json",
            "sell_side.json",
            "peers.json",
            "news.json",
            "filings.json",
            "market.json",
            "sector.json",
            "macro.json",
            "fundraising.json",
            "fetch_errors.json",
        }
        self.assertEqual(set(partitions.keys()), expected)

    def test_meta_is_present_in_each_partition(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        for filename, payload in partitions.items():
            self.assertIn("_meta", payload, f"{filename} missing _meta")
            self.assertEqual(payload["_meta"]["symbol"], "VIDA")
            self.assertEqual(payload["_meta"]["timestamp"], "2026-05-18")

    def test_ipo_partition_carries_expected_keys(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        ipo = partitions["ipo.json"]
        self.assertIn("ipo_data", ipo)
        self.assertIn("ipo_regulatory_context", ipo)
        self.assertIn("resolved_cik", ipo)
        self.assertEqual(ipo["ipo_data"]["exchange"], "NYSE")

    def test_financials_partition_carries_extras(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        fin = partitions["financials.json"]
        self.assertIn("financials", fin)
        self.assertIn("fundamental_extras", fin)

    def test_empty_input_emits_all_with_null_payloads(self):
        partitions = split_raw_data({})
        self.assertEqual(len(partitions), 13)
        for payload in partitions.values():
            self.assertIn("_meta", payload)

    def test_payloads_are_json_serializable(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        for filename, payload in partitions.items():
            json.dumps(payload, default=str)  # must not raise


class BuildIndexMdTest(unittest.TestCase):
    def test_index_contains_symbol_header(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        index = build_index_md(raw, partitions)
        self.assertIn("# Data Catalog — VIDA", index)
        self.assertIn("Prefetch timestamp: 2026-05-18", index)

    def test_index_lists_all_data_files(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        index = build_index_md(raw, partitions)
        for filename in (
            "ipo.json",
            "profile.json",
            "financials.json",
            "ownership.json",
            "macro.json",
            "fundraising.json",
        ):
            self.assertIn(f"data/{filename}", index)

    def test_emoji_marks_availability(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        index = build_index_md(raw, partitions)
        # ipo.json is populated → ✅; financials.json is all empty → ❌
        self.assertIn("✅", index)
        self.assertIn("❌", index)

    def test_research_priority_guidance_present(self):
        raw = _minimal_raw_data()
        partitions = split_raw_data(raw)
        index = build_index_md(raw, partitions)
        self.assertIn("Research priority guidance", index)
        self.assertIn("fetch_errors.json", index)


if __name__ == "__main__":
    unittest.main()
