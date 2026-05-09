"""Prompt auto-correction based on deviation patterns."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.fetchers.fmp_client import configure_logging
from src.storage.r2_client import R2Client, R2StorageError

LOGGER = logging.getLogger(__name__)


class CorrectionError(RuntimeError):
    """Raised when prompt correction fails."""


class PromptCorrector:
    """Monitors deviation history and applies prompt corrections."""

    HISTORY_KEY = "state/evolution/deviation_history.json"
    VERSIONS_PREFIX = "state/evolution/prompt_versions/"

    def __init__(
        self,
        r2: R2Client,
        *,
        prompt_path: str | Path = "prompts/ipo_v1_template.md",
        threshold: float = 0.15,
        min_samples: int = 5,
    ) -> None:
        self._r2 = r2
        self._prompt_path = Path(prompt_path)
        self._threshold = threshold
        self._min_samples = min_samples

    def check_and_correct(self) -> dict[str, Any]:
        """Check deviation history and apply correction if warranted.

        Returns a summary of the correction decision and action taken.
        """
        LOGGER.info("Checking deviation history for correction triggers")

        history = self._load_history()
        if not history:
            LOGGER.info("No deviation history found, skipping correction")
            return {"action": "skip", "reason": "no_history"}

        analysis = self._analyze_history(history)

        if not analysis["needs_correction"]:
            LOGGER.info(
                "No correction needed: %.1f%% failure rate (%d/%d samples)",
                analysis["failure_rate"] * 100,
                analysis["failure_count"],
                analysis["total_samples"],
            )
            return {"action": "skip", "reason": "within_threshold", **analysis}

        LOGGER.info(
            "Correction triggered: %.1f%% failure rate exceeds threshold",
            analysis["failure_rate"] * 100,
        )

        correction = self._generate_correction(analysis)
        self.apply_correction(correction)

        return {"action": "corrected", "correction": correction, **analysis}

    def apply_correction(self, correction: dict[str, Any]) -> str:
        """Apply a correction to the prompt file and archive the old version."""
        if not self._prompt_path.exists():
            raise CorrectionError(f"Prompt file not found: {self._prompt_path}")

        # Archive current version
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        archive_key = f"{self.VERSIONS_PREFIX}{timestamp}_ipo_v1_template.md"
        current_content = self._prompt_path.read_text(encoding="utf-8")

        try:
            self._r2._put_object(
                archive_key,
                current_content.encode("utf-8"),
                content_type="text/markdown",
            )
            LOGGER.info("Archived current prompt to %s", archive_key)
        except R2StorageError as exc:
            raise CorrectionError(f"Failed to archive prompt: {exc}") from exc

        # Write corrected prompt
        new_content = self._apply_correction_to_content(current_content, correction)
        try:
            self._prompt_path.write_text(new_content, encoding="utf-8")
            LOGGER.info("Applied correction to %s", self._prompt_path)
        except OSError as exc:
            raise CorrectionError(f"Failed to write corrected prompt: {exc}") from exc

        return archive_key

    def _load_history(self) -> list[dict[str, Any]]:
        """Load deviation history from R2."""
        try:
            data = self._r2.download_file(self.HISTORY_KEY)
            return json.loads(data.decode("utf-8"))
        except R2StorageError:
            return []
        except json.JSONDecodeError as exc:
            LOGGER.warning("Invalid deviation history JSON: %s", exc)
            return []

    def _analyze_history(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze deviation history to determine if correction is needed."""
        valid_entries = [
            e for e in history
            if e.get("within_threshold") is not None
        ]

        total = len(valid_entries)
        if total < self._min_samples:
            return {
                "needs_correction": False,
                "reason": "insufficient_samples",
                "total_samples": total,
                "min_required": self._min_samples,
                "failure_count": 0,
                "failure_rate": 0.0,
            }

        failures = [e for e in valid_entries if not e["within_threshold"]]
        failure_count = len(failures)
        failure_rate = failure_count / total

        # Calculate average deviation for failures
        avg_deviation = 0.0
        if failures:
            deviations = [abs(e.get("deviation", 0)) for e in failures]
            avg_deviation = sum(deviations) / len(deviations)

        # Determine bias direction
        signed_deviations = [e.get("deviation", 0) for e in failures]
        avg_signed = sum(signed_deviations) / len(signed_deviations) if signed_deviations else 0
        bias_direction = "overestimate" if avg_signed > 0 else "underestimate"

        needs_correction = failure_rate > 0.5 and total >= self._min_samples

        return {
            "needs_correction": needs_correction,
            "total_samples": total,
            "failure_count": failure_count,
            "failure_rate": failure_rate,
            "avg_deviation": avg_deviation,
            "bias_direction": bias_direction,
            "avg_signed_deviation": avg_signed,
        }

    def _generate_correction(self, analysis: dict[str, Any]) -> dict[str, Any]:
        """Generate correction metadata based on analysis."""
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "trigger": "high_failure_rate",
            "failure_rate": analysis["failure_rate"],
            "bias_direction": analysis["bias_direction"],
            "avg_deviation": analysis["avg_deviation"],
            "recommendation": (
                f"Adjust for {analysis['bias_direction']} bias. "
                f"Average deviation: {analysis['avg_deviation']:.2%}. "
                f"Failure rate: {analysis['failure_rate']:.1%} "
                f"({analysis['failure_count']}/{analysis['total_samples']} samples)."
            ),
        }

    def _apply_correction_to_content(
        self, content: str, correction: dict[str, Any]
    ) -> str:
        """Append correction metadata to prompt content."""
        correction_block = (
            f"\n\n---\n"
            f"## Auto-Correction Applied ({correction['timestamp']})\n\n"
            f"**Trigger:** {correction['trigger']}\n"
            f"**Bias Direction:** {correction['bias_direction']}\n"
            f"**Recommendation:** {correction['recommendation']}\n"
            f"\n"
            f"Adjust your confidence scoring and price predictions to account for "
            f"systematic {correction['bias_direction']} bias observed in "
            f"{correction['failure_rate']:.0%} of recent predictions.\n"
        )
        return content + correction_block


def main() -> None:
    """CLI entry point for prompt correction."""
    parser = argparse.ArgumentParser(description="NovaSignal Prompt Corrector")
    parser.add_argument(
        "--prompt",
        default="prompts/ipo_v1_template.md",
        help="Path to prompt template",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.15,
        help="Deviation threshold",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Minimum samples before correction",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check without applying correction",
    )

    args = parser.parse_args()
    configure_logging()

    r2 = R2Client()
    corrector = PromptCorrector(
        r2,
        prompt_path=args.prompt,
        threshold=args.threshold,
        min_samples=args.min_samples,
    )

    if args.dry_run:
        history = corrector._load_history()
        analysis = corrector._analyze_history(history)
        print(json.dumps(analysis, indent=2))
    else:
        result = corrector.check_and_correct()
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
