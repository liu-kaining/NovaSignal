import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.orchestrator.quality_gate import (
    GateThresholds,
    QualityGateResult,
    check_outputs,
    combine_results,
    format_failures_for_followup,
    verify_urls,
)


GOOD_REPORT = """# 《Vidaroo Corp IPO 打新投资决策报告》

## 一、核心摘要 (Executive Summary)

Vidaroo Corp ("VIDA") priced its NYSE IPO on May 15, 2026 at $4.00 per share with a $15M market cap, drawing from EDGAR Form 424B4 cover page evidence: https://www.sec.gov/Archives/edgar/data/9999/aaa.htm and FMP raw_data confirmation. Our base case is for the stock to trade in a $3.40–$4.80 band over the first 30 sessions.

Vidaroo Corp（VIDA）于 2026 年 5 月 15 日完成纽交所 IPO 定价，按 1,500 万美元市值与 375 万股测算每股 4.00 美元。基准情景下，上市后 30 个交易日股价预计在 3.40–4.80 美元区间。

## 二、IPO 关键信息速览 (IPO Snapshot)

The deal was led by underwriter X per the prospectus filing at https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001 and confirmed by issuer IR at https://ir.vidaroo.com/news/ipo.

承销由 X 公司主导，发行规模与稀释表已在招股书附件中披露。

## 三、公司基本面深度剖析 (Fundamental Analysis)

Industry context per Gartner cloud SMB software report indicates 14% CAGR to 2028: https://www.gartner.com/reports/cloud-smb-2025.

行业研究显示 SMB 云软件赛道复合增速约 14%，公司在该细分市场具备一定卡位。

## 四、财务状况健康度评估 (Financial Health Assessment)

FY2025 revenue $12.3M (+47% YoY) per S-1 Summary Financials: https://www.sec.gov/Archives/edgar/data/9999/financials.htm. Gross margin ~62%, operating loss narrowing.

公司 2025 财年营收 1,230 万美元，同比增长 47%；毛利率 62%；经营亏损收窄。

## 五、股权结构与管理团队 (Ownership & Management)

CEO Smith previously led growth at PriorCo (acquired 2024 for $200M): https://www.crunchbase.com/person/john-smith.

公司管理层兼具技术与商业化经验。

## 六、正向论据 (The Bull Case)

Bull arguments include defensible SMB niche, expanding gross margins, and recent strategic partnership: https://news.example.com/vida-partnership.

正向因素包括细分市场壁垒、毛利率扩张及战略合作公告。

## 七、反向论据 (The Bear Case)

Concentration risk: top customer 31% of revenue per S-1 risk factor 18: https://www.sec.gov/Archives/edgar/data/9999/risk.htm.

反向因素包括客户集中度较高、行业竞争加剧。

## 八、综合评估与策略结语 (Synthesis & Strategy)

Applying EV/Sales 4.5x peer median (peers: AAA, BBB) gives an implied $18–22M EV, modest premium to deal level. We rate it 积极关注 (Subscribe) with 30/50/20 first-day scenario weights.

按可比公司 EV/Sales 中位数 4.5 倍测算，公司公允 EV 区间约 1,800–2,200 万美元，较发行估值略有溢价。建议「积极关注 (Subscribe)」。

## 资料来源 (References)

[1] *S-1 Registration Statement* — SEC EDGAR — https://www.sec.gov/...
[2] *Cloud SMB Market Update* — Gartner — https://www.gartner.com/...

## 免责声明 (Disclaimer)

This analysis is AI-generated and does not constitute investment advice.

本分析由 AI 系统生成，不构成投资建议。
""" + "X" * 2000  # pad to clear min_report_chars


GOOD_METRICS = {
    "symbol": "VIDA",
    "analysis_date": "2026-05-18",
    "market": "US",
    "company_display_name": "Vidaroo Corp",
    "confidence": 0.55,
    "predicted_price": 4.2,
    "signal": "neutral",
    "risk_score": 0.45,
    "sector": "Application Software",
    "catalysts_count": 4,
    "risks_count": 5,
    "data_completeness": 0.65,
    "subscription_recommendation": "subscribe",
}


GOOD_RESEARCH = """# Research Notes — VIDA

## Web Research Log

### Query: "VIDA S-1 site:sec.gov"
- URL: https://www.sec.gov/Archives/edgar/data/9999/aaa.htm
- Extract: business description, risk factors

### Query: "VIDA underwriter"
- URL: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001
- Extract: lead bookrunner X

### Query: "VIDA lockup"
- URL: https://ir.vidaroo.com/news/ipo
- Extract: 180-day lockup

### Query: "Gartner SMB cloud software 2025"
- URL: https://www.gartner.com/reports/cloud-smb-2025
- Extract: 14% CAGR

### Query: "VIDA insider holdings"
- URL: https://www.sec.gov/Archives/edgar/data/9999/risk.htm
- Extract: principal shareholders

### Query: "VIDA news"
- URL: https://news.example.com/vida-partnership
- Extract: strategic partnership

### Query: "Vidaroo CEO Smith background"
- URL: https://www.crunchbase.com/person/john-smith
- Extract: prior role at PriorCo

### Query: "VIDA peer EV/Sales"
- URL: https://www.sec.gov/Archives/edgar/data/9999/financials.htm
- Extract: peer multiples
"""


class CheckOutputsTest(unittest.TestCase):
    def _write_sandbox(self, *, report, metrics, research_notes=None, critique=None):
        sb = Path(tempfile.mkdtemp(prefix="qa_test_"))
        if report is not None:
            (sb / "report.md").write_text(report, encoding="utf-8")
        if metrics is not None:
            (sb / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        if research_notes is not None:
            (sb / "research_notes.md").write_text(research_notes, encoding="utf-8")
        if critique is not None:
            (sb / "critique.md").write_text(critique, encoding="utf-8")
        return sb

    def test_good_report_passes_draft_threshold(self):
        sb = self._write_sandbox(
            report=GOOD_REPORT, metrics=GOOD_METRICS, research_notes=GOOD_RESEARCH
        )
        result = check_outputs(sb, thresholds=GateThresholds.draft())
        self.assertTrue(result.passed, msg=str(result.failures))

    def test_language_tags_detected(self):
        bad_report = GOOD_REPORT.replace(
            "## 一、核心摘要 (Executive Summary)",
            "## Summary (English)\n\n... \n\n## 中文摘要 (中文)",
        )
        sb = self._write_sandbox(
            report=bad_report, metrics=GOOD_METRICS, research_notes=GOOD_RESEARCH
        )
        result = check_outputs(sb)
        self.assertFalse(result.passed)
        self.assertTrue(any("language tag" in f.lower() for f in result.failures))

    def test_full_width_language_tag_detected(self):
        bad_report = GOOD_REPORT.replace(
            "## 五、股权结构与管理团队 (Ownership & Management)",
            "## 五、股权结构与管理团队 (Ownership & Management)（中文）",
        )
        sb = self._write_sandbox(
            report=bad_report, metrics=GOOD_METRICS, research_notes=GOOD_RESEARCH
        )
        result = check_outputs(sb)
        self.assertFalse(result.passed)

    def test_too_few_urls_fails(self):
        bad_report = "# Title\n\n## 一、核心摘要 (Executive Summary)\n\nNo URLs at all here.\n" + "Y" * 4000
        sb = self._write_sandbox(report=bad_report, metrics=GOOD_METRICS, research_notes=GOOD_RESEARCH)
        result = check_outputs(sb)
        self.assertFalse(result.passed)
        self.assertTrue(any("URL citation" in f for f in result.failures))

    def test_excess_unknowns_fails(self):
        bad_report = GOOD_REPORT + "\n\n" + ("Not available. " * 12)
        sb = self._write_sandbox(report=bad_report, metrics=GOOD_METRICS, research_notes=GOOD_RESEARCH)
        result = check_outputs(sb)
        self.assertFalse(result.passed)
        self.assertTrue(any("Unknown" in f or "Not available" in f for f in result.failures))

    def test_too_few_chapters_fails(self):
        # Only 3 chapters
        bad_report = (
            "# Title\n\n"
            "## 一、核心摘要 (Executive Summary)\n\nhttps://a.com\nhttps://b.com\nhttps://c.com\nhttps://d.com\nhttps://e.com\nhttps://f.com\n"
            "## 二、IPO Snapshot\n\n\n"
            "## 三、Fundamentals\n\n"
            + "Z" * 4000
        )
        sb = self._write_sandbox(report=bad_report, metrics=GOOD_METRICS, research_notes=GOOD_RESEARCH)
        result = check_outputs(sb)
        self.assertFalse(result.passed)
        self.assertTrue(any("chapter" in f.lower() for f in result.failures))

    def test_missing_metrics_fails(self):
        sb = self._write_sandbox(
            report=GOOD_REPORT, metrics=None, research_notes=GOOD_RESEARCH
        )
        result = check_outputs(sb)
        self.assertFalse(result.passed)
        self.assertTrue(any("metrics.json" in f for f in result.failures))

    def test_bad_subscription_recommendation_fails(self):
        bad_metrics = dict(GOOD_METRICS)
        bad_metrics["subscription_recommendation"] = "moon_shot"
        sb = self._write_sandbox(
            report=GOOD_REPORT, metrics=bad_metrics, research_notes=GOOD_RESEARCH
        )
        result = check_outputs(sb)
        self.assertFalse(result.passed)
        self.assertTrue(
            any("subscription_recommendation" in f for f in result.failures)
        )

    def test_require_critique_when_set(self):
        sb = self._write_sandbox(
            report=GOOD_REPORT, metrics=GOOD_METRICS, research_notes=GOOD_RESEARCH
        )
        result = check_outputs(sb, require_critique=True)
        self.assertFalse(result.passed)
        self.assertTrue(any("critique.md" in f for f in result.failures))

    def test_final_threshold_stricter(self):
        # GOOD_REPORT has 10 URLs; should pass draft (≥6) and final (≥7)
        sb = self._write_sandbox(
            report=GOOD_REPORT,
            metrics=GOOD_METRICS,
            research_notes=GOOD_RESEARCH,
            critique="# critique\n\n## Overall Verdict\n\n**READY**",
        )
        draft_result = check_outputs(sb, thresholds=GateThresholds.draft())
        final_result = check_outputs(
            sb, thresholds=GateThresholds.final(), require_critique=True
        )
        self.assertTrue(draft_result.passed)
        self.assertTrue(final_result.passed, msg=str(final_result.failures))


class FormatFailuresTest(unittest.TestCase):
    def test_no_failures_returns_empty(self):
        out = format_failures_for_followup(QualityGateResult(passed=True))
        self.assertEqual(out, "")

    def test_failures_become_numbered_list(self):
        result = QualityGateResult(passed=False, failures=["A failed", "B failed"])
        out = format_failures_for_followup(result, stage_label="draft")
        self.assertIn("1. A failed", out)
        self.assertIn("2. B failed", out)
        self.assertIn("draft", out)


class VerifyUrlsTest(unittest.TestCase):
    def test_no_urls_fails(self):
        sb = Path(tempfile.mkdtemp(prefix="qa_url_"))
        (sb / "report.md").write_text("# Empty\n\nno urls\n", encoding="utf-8")
        result = verify_urls(sb)
        self.assertFalse(result.passed)

    @patch("src.orchestrator.quality_gate.requests")
    def test_all_ok_passes(self, mock_requests):
        sb = Path(tempfile.mkdtemp(prefix="qa_url_"))
        (sb / "report.md").write_text(
            "https://a.com https://b.com https://c.com", encoding="utf-8"
        )

        resp = MagicMock()
        resp.status_code = 200
        mock_requests.head.return_value = resp

        result = verify_urls(sb, sample_size=3)
        self.assertTrue(result.passed, msg=str(result.failures))

    @patch("src.orchestrator.quality_gate.requests")
    def test_mostly_broken_fails(self, mock_requests):
        sb = Path(tempfile.mkdtemp(prefix="qa_url_"))
        (sb / "report.md").write_text(
            "https://a.com https://b.com https://c.com https://d.com",
            encoding="utf-8",
        )

        def fake_head(url, **kwargs):
            resp = MagicMock()
            # Make only 1 of 4 succeed
            resp.status_code = 200 if url == "https://a.com" else 500
            return resp

        mock_requests.head.side_effect = fake_head

        result = verify_urls(sb, sample_size=4, min_ok_ratio=0.5)
        self.assertFalse(result.passed)


class CombineResultsTest(unittest.TestCase):
    def test_any_failure_fails_combined(self):
        a = QualityGateResult(passed=True)
        b = QualityGateResult(passed=False, failures=["bad"])
        combined = combine_results(a, b)
        self.assertFalse(combined.passed)
        self.assertEqual(combined.failures, ["bad"])

    def test_all_pass_passes_combined(self):
        a = QualityGateResult(passed=True, metrics={"x": 1})
        b = QualityGateResult(passed=True, metrics={"y": 2})
        combined = combine_results(a, b)
        self.assertTrue(combined.passed)
        self.assertEqual(combined.metrics, {"x": 1, "y": 2})


if __name__ == "__main__":
    unittest.main()
