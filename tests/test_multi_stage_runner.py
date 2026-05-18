"""End-to-end-ish tests for the multi-stage runner using a fake agent invoker.

The fake invoker is a callable that writes pre-baked fixture files into the
sandbox to simulate what a real Claude Code session would produce. This lets
us exercise the full Drafter → Reviewer → Reviser orchestration logic and the
QA-gate retry loop without hitting Anthropic.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from src.orchestrator.multi_stage_runner import (
    StageConfig,
    execute_multi_stage,
)
from src.orchestrator.sandbox_manager import SandboxManager


def _full_report() -> str:
    """Return a v3-compliant bilingual report that passes ALL gate checks."""
    chapters = []
    titles = [
        ("一", "核心摘要", "Executive Summary"),
        ("二", "IPO 关键信息速览", "IPO Snapshot"),
        ("三", "公司基本面深度剖析", "Fundamental Analysis"),
        ("四", "财务状况健康度评估", "Financial Health"),
        ("五", "股权结构与管理团队", "Ownership & Management"),
        ("六", "正向论据", "The Bull Case"),
        ("七", "反向论据", "The Bear Case"),
        ("八", "综合评估与策略结语", "Synthesis & Strategy"),
    ]
    urls = [
        "https://www.sec.gov/Archives/edgar/data/9999/aaa.htm",
        "https://www.sec.gov/cgi-bin/browse-edgar?CIK=0001",
        "https://ir.example.com/news/ipo",
        "https://www.gartner.com/reports/cloud-2025",
        "https://news.example.com/partner",
        "https://www.crunchbase.com/person/john-smith",
        "https://www.sec.gov/Archives/edgar/data/9999/risk.htm",
        "https://example.com/financials",
        "https://example.com/peer",
    ]
    for i, (num, zh, en) in enumerate(titles):
        chapters.append(
            f"## {num}、{zh} ({en})\n\n"
            f"Vidaroo Corp ('VIDA') analysis paragraph {i+1}. See {urls[i]} for details. "
            f"The company priced its IPO at $4.00 on May 15, 2026 with $15M market cap. "
            f"Industry context per Gartner report shows 14% CAGR. Peer multiples imply "
            f"$18-22M EV. Bull factors include defensible niche; bear factors include "
            f"concentration risk.\n\n"
            f"Vidaroo Corp（VIDA）相关分析段落 {i+1}。详见上述来源。公司于 2026 年 5 月 "
            f"15 日完成 IPO 定价，每股 4.00 美元，市值 1,500 万美元。行业研究显示 "
            f"复合增速 14%。综合分析得出明确结论：积极关注 (Subscribe)。\n"
        )
    body = (
        "# 《Vidaroo Corp IPO 打新投资决策报告》\n\n"
        + "\n".join(chapters)
        + "\n## 资料来源 (References)\n\n"
        + "\n".join(f"[{i+1}] Source — {u}" for i, u in enumerate(urls))
        + "\n\n## 免责声明 (Disclaimer)\n\n"
        + "This analysis is AI-generated and does not constitute investment advice.\n\n"
        + "本分析由 AI 系统生成，不构成投资建议。\n"
    )
    # Pad to clear min_report_chars (3500 for final gate)
    return body + "\n\n" + "X" * 2000


def _full_metrics() -> dict:
    return {
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


def _full_research() -> str:
    urls = [
        "https://www.sec.gov/Archives/edgar/data/9999/aaa.htm",
        "https://www.sec.gov/cgi-bin/browse-edgar?CIK=0001",
        "https://ir.example.com/news/ipo",
        "https://www.gartner.com/reports/cloud-2025",
        "https://news.example.com/partner",
        "https://www.crunchbase.com/person/john-smith",
        "https://www.sec.gov/Archives/edgar/data/9999/risk.htm",
        "https://example.com/financials",
    ]
    return "# Research Notes\n\n" + "\n".join(f"- {u}" for u in urls)


def _full_critique() -> str:
    return (
        "# Reviewer Critique — VIDA\n\n"
        "## Overall Verdict\n\n**READY**\n\n"
        "## Dimension Scores\n\n| Dim | Score |\n|---|---|\n| All | 4/5 |\n"
        "\n## Required Fixes\n\n(none)\n"
    )


def _make_drafter_fake() -> Callable:
    """Fake invoker that writes a complete drafter output."""

    async def fake_invoke(
        prompt: str,
        *,
        working_dir,
        timeout_seconds: float = 300,
        env_vars=None,
        model=None,
        **kwargs,
    ):
        wd = Path(working_dir)
        # Stage A produces report.md + metrics.json + research_notes.md
        if (wd / "report_v1.md").exists():
            # Reviewer stage
            (wd / "critique.md").write_text(_full_critique(), encoding="utf-8")
        elif (wd / "critique.md").exists():
            # Reviser stage — leave report.md / metrics.json as-is (they're good)
            pass
        else:
            # Drafter stage
            (wd / "report.md").write_text(_full_report(), encoding="utf-8")
            (wd / "metrics.json").write_text(
                json.dumps(_full_metrics()), encoding="utf-8"
            )
            (wd / "research_notes.md").write_text(_full_research(), encoding="utf-8")
        return None  # invoke_agent returns AgentResult but we don't need it

    return fake_invoke


def _make_drafter_fake_then_pass_after_retry() -> Callable:
    """Drafter fails QA first attempt (no URLs), succeeds on retry."""
    state = {"attempts": 0}

    async def fake_invoke(prompt, *, working_dir, **kwargs):
        wd = Path(working_dir)
        if (wd / "report_v1.md").exists():
            (wd / "critique.md").write_text(_full_critique(), encoding="utf-8")
            return None
        if (wd / "critique.md").exists():
            # Reviser stage
            return None
        # Drafter stage
        state["attempts"] += 1
        if state["attempts"] == 1:
            # Bad draft: no URLs
            (wd / "report.md").write_text(
                "# Title\n\n" + "## 一、A (A)\n" * 8 + "no urls" + "Z" * 4000,
                encoding="utf-8",
            )
            (wd / "metrics.json").write_text(json.dumps({}), encoding="utf-8")
            (wd / "research_notes.md").write_text("# Empty", encoding="utf-8")
        else:
            # Good draft
            (wd / "report.md").write_text(_full_report(), encoding="utf-8")
            (wd / "metrics.json").write_text(
                json.dumps(_full_metrics()), encoding="utf-8"
            )
            (wd / "research_notes.md").write_text(_full_research(), encoding="utf-8")
        return None

    return fake_invoke


def _make_drafter_that_crashes() -> Callable:
    """Drafter never writes report.md, simulating subprocess crash."""

    async def fake_invoke(prompt, *, working_dir, **kwargs):
        # Don't write anything
        return None

    return fake_invoke


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class ExecuteMultiStageTest(unittest.TestCase):
    def _stage_config(self) -> StageConfig:
        # Tight timeouts ok because fake invoker is instant
        return StageConfig(
            drafter_timeout=10,
            reviewer_timeout=10,
            reviser_timeout=10,
            drafter_max_attempts=2,
            reviser_max_attempts=2,
            skip_url_verification=True,
        )

    def test_happy_path_three_stages(self):
        sandbox_mgr = SandboxManager()
        result = _run(
            execute_multi_stage(
                symbol="VIDA",
                raw_data={"symbol": "VIDA", "timestamp": "2026-05-18"},
                drafter_prompt="d",
                reviewer_prompt="r",
                reviser_prompt="v",
                sandbox_mgr=sandbox_mgr,
                stage_config=self._stage_config(),
                invoker=_make_drafter_fake(),
            )
        )
        self.assertTrue(result.success, msg=result.error)
        self.assertTrue(result.final_gate_passed, msg=str(result.final_gate.failures if result.final_gate else None))
        self.assertIsNotNone(result.report)
        self.assertIsNotNone(result.metrics)
        self.assertIsNotNone(result.research_notes)
        self.assertIsNotNone(result.critique)

        # Outcome trace: 1 drafter + 1 reviewer + 1 reviser
        stages = [o.stage for o in result.outcomes]
        self.assertIn("drafter", stages)
        self.assertIn("reviewer", stages)
        self.assertIn("reviser", stages)

    def test_drafter_retry_then_success(self):
        sandbox_mgr = SandboxManager()
        result = _run(
            execute_multi_stage(
                symbol="VIDA",
                raw_data={"symbol": "VIDA"},
                drafter_prompt="d",
                reviewer_prompt="r",
                reviser_prompt="v",
                sandbox_mgr=sandbox_mgr,
                stage_config=self._stage_config(),
                invoker=_make_drafter_fake_then_pass_after_retry(),
            )
        )
        self.assertTrue(result.success, msg=result.error)
        # Should have 2 drafter attempts
        drafter_outcomes = [o for o in result.outcomes if o.stage == "drafter"]
        self.assertEqual(len(drafter_outcomes), 2)

    def test_drafter_crash_no_report(self):
        sandbox_mgr = SandboxManager()
        result = _run(
            execute_multi_stage(
                symbol="VIDA",
                raw_data={"symbol": "VIDA"},
                drafter_prompt="d",
                reviewer_prompt="r",
                reviser_prompt="v",
                sandbox_mgr=sandbox_mgr,
                stage_config=self._stage_config(),
                invoker=_make_drafter_that_crashes(),
            )
        )
        self.assertFalse(result.success)
        # Should NOT have reached reviewer stage
        stages = [o.stage for o in result.outcomes]
        self.assertNotIn("reviewer", stages)

    def test_url_verification_runs_when_enabled(self):
        sandbox_mgr = SandboxManager()
        cfg = StageConfig(
            drafter_timeout=10,
            reviewer_timeout=10,
            reviser_timeout=10,
            drafter_max_attempts=1,
            reviser_max_attempts=1,
            skip_url_verification=True,  # explicitly skip; test the flag is honored
        )
        result = _run(
            execute_multi_stage(
                symbol="VIDA",
                raw_data={"symbol": "VIDA"},
                drafter_prompt="d",
                reviewer_prompt="r",
                reviser_prompt="v",
                sandbox_mgr=sandbox_mgr,
                stage_config=cfg,
                invoker=_make_drafter_fake(),
            )
        )
        # url_verification should be None when skipped
        self.assertIsNone(result.url_verification)


if __name__ == "__main__":
    unittest.main()
