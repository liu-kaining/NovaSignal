"""Drafter → Reviewer → Reviser multi-stage orchestration with QA-gate feedback loops.

This is the core production path for generating an institutional-grade IPO
report. Each symbol passes through three Claude Code invocations; the QA gate
runs between stages and can trigger up to 1 retry per drafting stage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.orchestrator.async_runner import AgentRunError, invoke_agent
from src.orchestrator.quality_gate import (
    GateThresholds,
    QualityGateResult,
    check_outputs,
    format_failures_for_followup,
    verify_urls,
)
from src.orchestrator.sandbox_manager import (
    MultiStageContext,
    SandboxError,
    SandboxManager,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class StageConfig:
    """Per-stage timeout and retry knobs."""

    drafter_timeout: float = 420
    reviewer_timeout: float = 300
    reviser_timeout: float = 360
    drafter_max_attempts: int = 2  # 1 normal + 1 retry on QA-gate failure
    reviser_max_attempts: int = 2
    url_verify_sample: int = 5
    url_verify_timeout: float = 5.0
    skip_url_verification: bool = False


@dataclass
class StageOutcome:
    """Record of a single agent invocation + gate result."""

    stage: str
    attempt: int
    success: bool
    gate: QualityGateResult | None = None
    error: str | None = None
    duration_seconds: float | None = None


@dataclass
class MultiStageResult:
    """Final result of the multi-stage pipeline for one symbol."""

    symbol: str
    success: bool
    final_gate_passed: bool
    final_gate: QualityGateResult | None = None
    url_verification: QualityGateResult | None = None
    report: str | None = None
    metrics: dict[str, Any] | None = None
    research_notes: str | None = None
    critique: str | None = None
    outcomes: list[StageOutcome] = field(default_factory=list)
    error: str | None = None

    def as_summary(self) -> dict[str, Any]:
        """Compact dict for logging / R2 quality-gate payload."""
        return {
            "symbol": self.symbol,
            "success": self.success,
            "final_gate_passed": self.final_gate_passed,
            "final_gate": self.final_gate.as_dict() if self.final_gate else None,
            "url_verification": (
                self.url_verification.as_dict() if self.url_verification else None
            ),
            "outcomes": [
                {
                    "stage": o.stage,
                    "attempt": o.attempt,
                    "success": o.success,
                    "error": o.error,
                    "duration_seconds": o.duration_seconds,
                    "gate": o.gate.as_dict() if o.gate else None,
                }
                for o in self.outcomes
            ],
            "error": self.error,
        }


# Type alias for the agent invoker so tests can inject a fake.
AgentInvoker = Callable[..., Any]


async def execute_multi_stage(
    *,
    symbol: str,
    raw_data: dict[str, Any],
    drafter_prompt: str,
    reviewer_prompt: str,
    reviser_prompt: str,
    sandbox_mgr: SandboxManager,
    env_vars: dict[str, str] | None = None,
    model: str | None = None,
    stage_config: StageConfig | None = None,
    invoker: AgentInvoker = invoke_agent,
) -> MultiStageResult:
    """Run the Drafter → Reviewer → Reviser pipeline for one symbol.

    Args:
        symbol: Ticker symbol.
        raw_data: The FMP-enriched payload (will be split into themed files).
        drafter_prompt / reviewer_prompt / reviser_prompt: Prompt template contents.
        sandbox_mgr: Manages drafter + reviewer sandbox lifecycle.
        env_vars: Env passed to Claude CLI (ANTHROPIC_API_KEY etc).
        model: Optional model override.
        stage_config: Tunable timeouts + retry knobs.
        invoker: Async function compatible with ``async_runner.invoke_agent``;
            injectable for testing.

    Returns: a ``MultiStageResult`` with final artifacts and full outcome trace.
    """
    cfg = stage_config or StageConfig()
    outcomes: list[StageOutcome] = []
    drafter_dir: Path | None = None
    reviewer_dir: Path | None = None
    result = MultiStageResult(symbol=symbol, success=False, final_gate_passed=False)

    try:
        # =====================
        # Stage A — Drafter
        # =====================
        drafter_dir = sandbox_mgr.setup_drafter_sandbox(
            symbol=symbol, raw_data=raw_data, drafter_prompt=drafter_prompt
        )
        prev_failures: QualityGateResult | None = None
        drafter_gate: QualityGateResult | None = None

        for attempt in range(1, cfg.drafter_max_attempts + 1):
            instruction = _build_drafter_instruction(
                attempt=attempt, prev_failures=prev_failures
            )
            outcome = await _run_agent_safely(
                stage="drafter",
                attempt=attempt,
                invoker=invoker,
                instruction=instruction,
                working_dir=drafter_dir,
                timeout_seconds=cfg.drafter_timeout,
                env_vars=env_vars,
                model=model,
            )
            outcomes.append(outcome)
            if not outcome.success:
                LOGGER.warning(
                    "[%s] drafter attempt %d failed: %s",
                    symbol,
                    attempt,
                    outcome.error,
                )
                break  # invocation itself failed; don't keep retrying

            drafter_gate = check_outputs(
                drafter_dir,
                thresholds=GateThresholds.draft(),
                require_critique=False,
            )
            outcome.gate = drafter_gate
            LOGGER.info(
                "[%s] drafter attempt %d gate: passed=%s failures=%d",
                symbol,
                attempt,
                drafter_gate.passed,
                len(drafter_gate.failures),
            )
            if drafter_gate.passed or attempt == cfg.drafter_max_attempts:
                break
            prev_failures = drafter_gate  # feed back to next attempt

        # If drafter completely collapsed (no report.md), abort.
        if not (drafter_dir / "report.md").exists():
            result.error = "Drafter failed to produce report.md after all attempts"
            result.outcomes = outcomes
            return result

        # =====================
        # Stage B — Reviewer
        # =====================
        reviewer_dir = sandbox_mgr.setup_reviewer_sandbox(
            symbol=symbol,
            drafter_dir=drafter_dir,
            reviewer_prompt=reviewer_prompt,
        )
        reviewer_instruction = _build_reviewer_instruction()
        reviewer_outcome = await _run_agent_safely(
            stage="reviewer",
            attempt=1,
            invoker=invoker,
            instruction=reviewer_instruction,
            working_dir=reviewer_dir,
            timeout_seconds=cfg.reviewer_timeout,
            env_vars=env_vars,
            model=model,
        )
        outcomes.append(reviewer_outcome)

        # =====================
        # Stage C — Reviser
        # =====================
        sandbox_mgr.inject_critique(
            drafter_dir=drafter_dir,
            reviewer_dir=reviewer_dir,
            reviser_prompt=reviser_prompt,
        )

        prev_failures = None
        final_gate: QualityGateResult | None = None

        for attempt in range(1, cfg.reviser_max_attempts + 1):
            instruction = _build_reviser_instruction(
                attempt=attempt, prev_failures=prev_failures
            )
            outcome = await _run_agent_safely(
                stage="reviser",
                attempt=attempt,
                invoker=invoker,
                instruction=instruction,
                working_dir=drafter_dir,
                timeout_seconds=cfg.reviser_timeout,
                env_vars=env_vars,
                model=model,
            )
            outcomes.append(outcome)
            if not outcome.success:
                LOGGER.warning(
                    "[%s] reviser attempt %d failed: %s",
                    symbol,
                    attempt,
                    outcome.error,
                )
                # Don't keep retrying a hard subprocess failure
                break

            final_gate = check_outputs(
                drafter_dir,
                thresholds=GateThresholds.final(),
                require_critique=True,
            )
            outcome.gate = final_gate
            LOGGER.info(
                "[%s] reviser attempt %d gate: passed=%s failures=%d",
                symbol,
                attempt,
                final_gate.passed,
                len(final_gate.failures),
            )
            if final_gate.passed or attempt == cfg.reviser_max_attempts:
                break
            prev_failures = final_gate

        # =====================
        # Final URL verification (network HEAD-check)
        # =====================
        url_check: QualityGateResult | None = None
        if not cfg.skip_url_verification:
            try:
                url_check = verify_urls(
                    drafter_dir,
                    sample_size=cfg.url_verify_sample,
                    timeout=cfg.url_verify_timeout,
                )
                LOGGER.info(
                    "[%s] URL verification: passed=%s metrics=%s",
                    symbol,
                    url_check.passed,
                    url_check.metrics,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[%s] URL verification raised: %s", symbol, exc)
                url_check = QualityGateResult(
                    passed=True,
                    warnings=[f"URL verification raised exception: {exc}"],
                )

        # =====================
        # Collect final artifacts
        # =====================
        artifacts = sandbox_mgr.extract_final_artifacts(drafter_dir)
        result.report = artifacts.get("report")
        result.metrics = artifacts.get("metrics")
        result.research_notes = artifacts.get("research_notes")
        result.critique = artifacts.get("critique")
        result.final_gate = final_gate
        result.url_verification = url_check
        result.final_gate_passed = bool(final_gate and final_gate.passed) and (
            url_check is None or url_check.passed
        )
        result.success = bool(result.report) and bool(result.metrics)
        result.outcomes = outcomes
        return result

    except SandboxError as exc:
        LOGGER.exception("[%s] sandbox error: %s", symbol, exc)
        result.error = f"SandboxError: {exc}"
        result.outcomes = outcomes
        return result
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("[%s] unexpected error in multi-stage runner", symbol)
        result.error = f"{type(exc).__name__}: {exc}"
        result.outcomes = outcomes
        return result
    finally:
        sandbox_mgr.cleanup_paths(drafter_dir, reviewer_dir)


# -------------------------
# Internal helpers
# -------------------------


async def _run_agent_safely(
    *,
    stage: str,
    attempt: int,
    invoker: AgentInvoker,
    instruction: str,
    working_dir: Path,
    timeout_seconds: float,
    env_vars: dict[str, str] | None,
    model: str | None,
) -> StageOutcome:
    """Invoke the agent and translate exceptions into a structured StageOutcome."""
    import time

    start = time.monotonic()
    try:
        await invoker(
            instruction,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
            env_vars=env_vars,
            model=model,
        )
        return StageOutcome(
            stage=stage,
            attempt=attempt,
            success=True,
            duration_seconds=round(time.monotonic() - start, 2),
        )
    except AgentRunError as exc:
        return StageOutcome(
            stage=stage,
            attempt=attempt,
            success=False,
            error=f"AgentRunError: {exc}",
            duration_seconds=round(time.monotonic() - start, 2),
        )
    except Exception as exc:  # noqa: BLE001
        return StageOutcome(
            stage=stage,
            attempt=attempt,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=round(time.monotonic() - start, 2),
        )


def _build_drafter_instruction(
    *, attempt: int, prev_failures: QualityGateResult | None
) -> str:
    """Compose the drafter step instructions, optionally with QA-feedback for retries."""
    base = (
        "[NovaSignal Drafter — Stage A, v4]\n"
        "STEP 1. Read INDEX.md (the data catalog) and prompt.md (your product contract). "
        "Skim INDEX.md to learn which data/<file> backs which chapter and which fields are populated.\n"
        "STEP 2. Plan the 8 chapters. For each chapter, decide which data/<file> you'll read "
        "and which Web research items from prompt.md §4 you need to resolve. Use TodoWrite if helpful.\n"
        "STEP 3. Spawn parallel subagents via the Task tool for deep dives:\n"
        "  - edgar_researcher: pull S-1 business description, top-3 risks, lockup, use of proceeds with real URLs\n"
        "  - peer_analyst: 3 comparables with EV/Sales or P/E multiples\n"
        "  - macro_strategist: 200-word macro/sector framing\n"
        "Wait for all subagents, then weave findings into the report with citations.\n"
        "STEP 4. Append every Web research action to research_notes.md as you go "
        "(query + URL + extract + which chapter uses it). Minimum 8 distinct entries.\n"
        "STEP 5. Read data/<files> on-demand chapter by chapter and draft report.md. "
        "Strict §6 heading rules — ONE H2 per chapter in format '## N、中文 (English)'. "
        "NEVER append (English)/(EN)/(中文)/(ZH) tags. NEVER split a chapter by language.\n"
        "STEP 6. Write metrics.json (strict JSON; ≤5 null fields).\n"
        "STEP 7. Run prompt.md §8 self-check via Bash. Fix any failures in-place. "
        "Only exit when all six grep/python checks pass.\n"
        "Take your time. Use extended reasoning. A 6-minute deep report beats a 2-minute shallow one."
    )
    if attempt > 1 and prev_failures and not prev_failures.passed:
        feedback = format_failures_for_followup(prev_failures, stage_label="draft")
        return (
            f"{base}\n\n"
            f"--- RETRY ATTEMPT {attempt} ---\n"
            f"{feedback}\n\n"
            "Your previous draft is still in the sandbox. Fix only the listed issues."
        )
    return base


def _build_reviewer_instruction() -> str:
    return (
        "[NovaSignal Reviewer — Stage B]\n"
        "Read prompt.md (reviewer contract), then INDEX.md, report_v1.md, research_notes.md, metrics_v1.json. "
        "Selectively Read data/<files> when fact-checking specific claims. "
        "Spot-check 3–5 of the most consequential URL citations using WebFetch. "
        "Spawn Task subagents if deep verification is needed (e.g., peer valuation reasonableness). "
        "Produce critique.md per prompt.md §4 — structured scoring across 10 dimensions, "
        "specific Required Fixes with chapter/quote references, list of any suspected fabrications. "
        "Be tough but constructive."
    )


def _build_reviser_instruction(
    *, attempt: int, prev_failures: QualityGateResult | None
) -> str:
    base = (
        "[NovaSignal Reviser — Stage C]\n"
        "Read prompt.md (reviser contract) and critique.md FIRST. "
        "report.md and metrics.json already exist in cwd — you will edit them IN PLACE. "
        "Apply ONLY surgical edits that address Required Fixes in critique.md. "
        "Use the Edit tool, not Write. Preserve everything the Reviewer didn't flag. "
        "If Required Fixes need new data, run WebSearch/WebFetch and append to research_notes.md "
        "(with 'Stage C addendum:' prefix), then incorporate citations in report.md. "
        "If a URL was flagged as suspected fabrication, REPLACE with a verified URL or remove the claim. "
        "Maintain bilingual paragraph pairs; never add (English)/(中文) tags. "
        "After edits, run prompt.md §5 self-check (stricter: ≥8 URLs, ≤5 Unknown phrases). "
        "Only exit when all checks pass."
    )
    if attempt > 1 and prev_failures and not prev_failures.passed:
        feedback = format_failures_for_followup(prev_failures, stage_label="final")
        return (
            f"{base}\n\n"
            f"--- RETRY ATTEMPT {attempt} ---\n"
            f"{feedback}\n\n"
            "The previous revision did not pass the final quality gate. "
            "Fix EACH listed failure with surgical edits to the existing files."
        )
    return base
