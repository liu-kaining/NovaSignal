"""Server-side quality gate for agent outputs.

Run **after** the agent returns. Verifies the report and metrics meet hard
quality bars (no language tags, enough citations, JSON validity, etc.).
Failures are returned as structured strings that can be piped back to the
agent as a follow-up correction prompt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

LOGGER = logging.getLogger(__name__)

# Forbidden language tags (the user-facing pet peeve)
_HALF_WIDTH_LABEL_RE = re.compile(r"\((?:English|EN|中文|ZH|中|英|英文)\)", re.IGNORECASE)
_FULL_WIDTH_LABEL_RE = re.compile(r"（(?:中文|英文|EN|ZH|中|英|English)）", re.IGNORECASE)

# URLs (very loose; we only count externally-cited refs in report.md)
_URL_RE = re.compile(r"https?://[^\s\)\]\>\"']+")

# "Information missing" tokens we cap at a hard ceiling
_UNKNOWN_RE = re.compile(
    r"Not available|N/A|Unknown|无法评估|暂无数据|数据缺失|信息缺失|未提供",
    re.IGNORECASE,
)

# A numbered chapter heading: "## 一、xxx (yyy)" — exactly the v3 contract format
_CHAPTER_H2_RE = re.compile(r"^##\s+[一二三四五六七八九十]+、", re.MULTILINE)

# Any H2 (used to count split-by-language violations indirectly)
_ANY_H2_RE = re.compile(r"^##\s+\S", re.MULTILINE)


@dataclass
class QualityGateResult:
    """Outcome of running the quality gate against a sandbox."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, int | float | bool] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        if not self.failures:
            return "pass"
        if len(self.failures) <= 2:
            return "soft_fail"
        return "hard_fail"

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "severity": self.severity,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "metrics": dict(self.metrics),
        }


@dataclass
class GateThresholds:
    """Tunable thresholds for the quality gate. Two presets: draft / final.

    Draft thresholds align with the 6-item required checklist in the drafter
    prompt; final thresholds tighten for the post-reviewer revision.
    """

    min_urls: int = 6
    max_unknown_phrases: int = 8
    required_chapters: int = 8
    max_null_metric_fields: int = 5
    min_research_urls: int = 6
    min_report_chars: int = 3000

    @classmethod
    def draft(cls) -> "GateThresholds":
        return cls()

    @classmethod
    def final(cls) -> "GateThresholds":
        return cls(
            min_urls=7,
            max_unknown_phrases=5,
            required_chapters=8,
            max_null_metric_fields=3,
            min_research_urls=7,
            min_report_chars=3500,
        )


def check_outputs(
    sandbox_dir: str | Path,
    *,
    thresholds: GateThresholds | None = None,
    require_critique: bool = False,
) -> QualityGateResult:
    """Run all hard quality checks against the sandbox outputs.

    Args:
        sandbox_dir: Directory containing report.md, metrics.json, research_notes.md.
        thresholds: Use ``GateThresholds.draft()`` for first pass (lenient), or
            ``GateThresholds.final()`` for last pass (strict).
        require_critique: If True, also require critique.md to exist.
    """
    th = thresholds or GateThresholds.draft()
    failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, int | float | bool] = {}

    sandbox = Path(sandbox_dir)

    # --- 1. report.md ---
    report_path = sandbox / "report.md"
    if not report_path.exists():
        failures.append("report.md is MISSING from sandbox")
        return QualityGateResult(passed=False, failures=failures, metrics=metrics)

    try:
        report = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"report.md unreadable: {exc}")
        return QualityGateResult(passed=False, failures=failures, metrics=metrics)

    metrics["report_chars"] = len(report)
    if len(report) < th.min_report_chars:
        failures.append(
            f"report.md too short: {len(report)} chars, need ≥{th.min_report_chars}. "
            "Expand sections with more data-driven narrative."
        )

    # 1a. Language tag prohibition (the user's #1 pet peeve)
    half_matches = _HALF_WIDTH_LABEL_RE.findall(report)
    full_matches = _FULL_WIDTH_LABEL_RE.findall(report)
    metrics["language_tag_violations"] = len(half_matches) + len(full_matches)
    if half_matches or full_matches:
        samples = (half_matches + full_matches)[:5]
        failures.append(
            f"FORBIDDEN language tags found ({len(half_matches) + len(full_matches)} occurrence(s)) — "
            f"e.g. {samples}. Delete ALL of them; section headers must follow "
            "'## N、中文标题 (English Title)' format with NO additional (English)/(中文) tag."
        )

    # 1b. URL citation count
    urls = _URL_RE.findall(report)
    metrics["report_url_count"] = len(urls)
    if len(urls) < th.min_urls:
        failures.append(
            f"Only {len(urls)} URL citation(s) in report.md; need ≥{th.min_urls}. "
            "Add cited Web findings (EDGAR / IR / financial press)."
        )

    # 1c. Unknown / missing phrase ceiling
    unknown_hits = _UNKNOWN_RE.findall(report)
    metrics["unknown_phrase_count"] = len(unknown_hits)
    if len(unknown_hits) > th.max_unknown_phrases:
        failures.append(
            f"Found {len(unknown_hits)} 'Unknown / Not available / 无法评估' phrases; "
            f"max allowed is {th.max_unknown_phrases}. Go back to Web research and fill the gaps."
        )

    # 1d. Chapter count (numbered H2)
    chapter_count = len(_CHAPTER_H2_RE.findall(report))
    any_h2_count = len(_ANY_H2_RE.findall(report))
    metrics["numbered_chapter_count"] = chapter_count
    metrics["total_h2_count"] = any_h2_count
    if chapter_count < th.required_chapters:
        failures.append(
            f"Only {chapter_count} numbered chapter heading(s) ('## 一、…' .. '## 八、…') found, "
            f"need exactly {th.required_chapters}. Check for split-language section headers."
        )
    # Heuristic: if any_h2 > chapter+3 it likely means agent split chapters by language again
    if any_h2_count > chapter_count + 3:
        warnings.append(
            f"Saw {any_h2_count} H2 headings but only {chapter_count} numbered chapters; "
            "agent may be using extra non-canonical subheaders — verify §3.1 compliance."
        )

    # --- 2. metrics.json ---
    metrics_path = sandbox / "metrics.json"
    if not metrics_path.exists():
        failures.append("metrics.json is MISSING from sandbox")
    else:
        try:
            mjson = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            failures.append(f"metrics.json invalid JSON: {exc}")
            mjson = None

        if isinstance(mjson, dict):
            required_keys = {
                "symbol",
                "analysis_date",
                "subscription_recommendation",
                "confidence",
                "signal",
                "risk_score",
            }
            missing = sorted(required_keys - set(mjson.keys()))
            if missing:
                failures.append(
                    f"metrics.json missing required keys: {missing}. "
                    "All of {symbol, analysis_date, subscription_recommendation, confidence, signal, risk_score} must be present."
                )
            null_count = sum(
                1 for v in mjson.values() if v is None or v == "Unknown" or v == ""
            )
            metrics["metrics_null_count"] = null_count
            if null_count > th.max_null_metric_fields:
                failures.append(
                    f"metrics.json has {null_count} null/Unknown fields; max allowed "
                    f"{th.max_null_metric_fields}. Resolve via Web research or explain "
                    "in report.md why a defensible value cannot be derived."
                )
            rec = mjson.get("subscription_recommendation")
            if rec not in {"strong_buy", "subscribe", "cautious", "avoid", None}:
                failures.append(
                    f"metrics.subscription_recommendation = {rec!r} is not one of "
                    "{strong_buy, subscribe, cautious, avoid}."
                )

    # --- 3. research_notes.md ---
    research_path = sandbox / "research_notes.md"
    if not research_path.exists():
        failures.append(
            "research_notes.md is MISSING. You MUST log every Web research query "
            "(URL or 'no usable result') before drafting the report."
        )
    else:
        try:
            research = research_path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"research_notes.md unreadable: {exc}")
            research = ""
        research_urls = _URL_RE.findall(research)
        metrics["research_url_count"] = len(research_urls)
        metrics["research_chars"] = len(research)
        if len(research_urls) < th.min_research_urls:
            failures.append(
                f"research_notes.md has only {len(research_urls)} URL(s); need ≥{th.min_research_urls}. "
                "Re-run WebSearch on EDGAR / IR / financial press for the symbol."
            )

    # --- 4. critique.md (only after reviewer ran) ---
    if require_critique:
        critique_path = sandbox / "critique.md"
        if not critique_path.exists():
            failures.append(
                "critique.md is MISSING. Reviewer stage must produce structured critique."
            )

    return QualityGateResult(
        passed=len(failures) == 0,
        failures=failures,
        warnings=warnings,
        metrics=metrics,
    )


def verify_urls(
    sandbox_dir: str | Path,
    *,
    sample_size: int = 5,
    timeout: float = 5.0,
    min_ok_ratio: float = 0.5,
) -> QualityGateResult:
    """HEAD-check a small random sample of URLs from report.md.

    This is a *separate* gate from ``check_outputs`` because it makes network
    calls. Run it after ``check_outputs`` passes, before final R2 upload.

    Returns a QualityGateResult — ``passed=False`` if the OK ratio is below
    ``min_ok_ratio``. Failures are returned as a single bundled message.
    """
    import random

    if requests is None:  # pragma: no cover - requests is a hard dep already
        return QualityGateResult(
            passed=True,
            warnings=["requests not installed; skipping URL verification"],
            metrics={},
        )

    report_path = Path(sandbox_dir) / "report.md"
    if not report_path.exists():
        return QualityGateResult(
            passed=False, failures=["report.md missing for URL verification"]
        )

    report = report_path.read_text(encoding="utf-8")
    urls = list(dict.fromkeys(_URL_RE.findall(report)))  # de-dup, preserve order
    if not urls:
        return QualityGateResult(
            passed=False,
            failures=["No URLs found in report.md to verify — failed citation requirement"],
            metrics={"sampled": 0, "ok": 0},
        )

    sample = random.sample(urls, k=min(sample_size, len(urls)))
    ok = 0
    sampled_results: list[tuple[str, str]] = []
    for url in sample:
        try:
            resp = requests.head(
                url,
                allow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (NovaSignal QA Gate)"},
            )
            status = resp.status_code
            if status == 405:  # HEAD not allowed; try GET with stream=True and small read
                resp = requests.get(
                    url,
                    timeout=timeout,
                    stream=True,
                    headers={"User-Agent": "Mozilla/5.0 (NovaSignal QA Gate)"},
                )
                status = resp.status_code
                resp.close()
            sampled_results.append((url, str(status)))
            if 200 <= status < 400:
                ok += 1
        except Exception as exc:  # noqa: BLE001
            sampled_results.append((url, f"ERR: {type(exc).__name__}"))

    ok_ratio = ok / len(sample) if sample else 0.0
    metrics_payload: dict[str, int | float | bool] = {
        "sampled": len(sample),
        "ok": ok,
        "ok_ratio": round(ok_ratio, 3),
    }
    if ok_ratio < min_ok_ratio:
        bad = [u for u, s in sampled_results if not s.startswith(("2", "3"))]
        return QualityGateResult(
            passed=False,
            failures=[
                f"URL verification failed: only {ok}/{len(sample)} URLs returned 2xx/3xx. "
                f"Suspect fabricated links. Examples: {bad[:3]}. "
                "Replace these with real, reachable EDGAR/IR/press URLs you can actually fetch."
            ],
            metrics=metrics_payload,
        )
    return QualityGateResult(passed=True, metrics=metrics_payload)


def format_failures_for_followup(
    result: QualityGateResult,
    *,
    stage_label: str = "draft",
) -> str:
    """Turn QA failures into a follow-up instruction the agent will receive next iteration."""
    if result.passed:
        return ""
    lines: list[str] = []
    lines.append(
        f"[NovaSignal QA Gate — {stage_label} stage rejected your last submission]"
    )
    lines.append("")
    lines.append("Your most recent report.md / metrics.json did not pass the hard quality gate.")
    lines.append("Fix EACH of the following before resubmitting:")
    lines.append("")
    for i, failure in enumerate(result.failures, 1):
        lines.append(f"  {i}. {failure}")
    if result.warnings:
        lines.append("")
        lines.append("Also address these warnings:")
        for i, warn in enumerate(result.warnings, 1):
            lines.append(f"  {i}. {warn}")
    lines.append("")
    lines.append(
        "Do NOT rewrite the entire report from scratch — apply surgical edits to the "
        "existing files in the sandbox. Then re-run your self-check before exiting. "
        "Your sandbox cwd already contains report.md and metrics.json; edit them in place."
    )
    return "\n".join(lines)


def combine_results(*results: Iterable[QualityGateResult]) -> QualityGateResult:
    """Combine multiple gate results into one (any failure fails the whole)."""
    failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, int | float | bool] = {}
    for r in results:
        if not isinstance(r, QualityGateResult):
            continue
        failures.extend(r.failures)
        warnings.extend(r.warnings)
        metrics.update(r.metrics)
    return QualityGateResult(
        passed=not failures,
        failures=failures,
        warnings=warnings,
        metrics=metrics,
    )
