import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.evolution.corrector import CorrectionError, PromptCorrector
from src.storage.r2_client import R2StorageError


class PromptCorrectorTest(unittest.TestCase):
    def _make_corrector(self, prompt_content="# Template", **kwargs):
        r2 = MagicMock()
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write(prompt_content)
        tmp.flush()
        tmp.close()
        prompt_path = Path(tmp.name)
        corrector = PromptCorrector(
            r2, prompt_path=prompt_path, **kwargs
        )
        return corrector, r2, prompt_path

    def _make_history(self, total=10, failures=6, deviation=0.25):
        """Generate synthetic deviation history."""
        history = []
        for i in range(total):
            within = i >= failures
            history.append({
                "symbol": f"SYM{i}",
                "date": "2026-01-01",
                "deviation": -deviation if not within else 0.05,
                "within_threshold": within,
            })
        return history

    def test_no_history_skips(self):
        corrector, r2, _ = self._make_corrector()
        r2.download_file.side_effect = R2StorageError("not found")

        result = corrector.check_and_correct()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "no_history")

    def test_insufficient_samples_skips(self):
        corrector, r2, _ = self._make_corrector(min_samples=10)
        history = self._make_history(total=3, failures=3)
        r2.download_file.return_value = json.dumps(history).encode()

        result = corrector.check_and_correct()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "insufficient_samples")

    def test_low_failure_rate_skips(self):
        corrector, r2, _ = self._make_corrector(min_samples=5)
        history = self._make_history(total=10, failures=2)
        r2.download_file.return_value = json.dumps(history).encode()

        result = corrector.check_and_correct()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "within_threshold")

    def test_high_failure_rate_triggers_correction(self):
        corrector, r2, prompt_path = self._make_corrector(min_samples=5)
        history = self._make_history(total=10, failures=8, deviation=0.3)
        r2.download_file.return_value = json.dumps(history).encode()

        result = corrector.check_and_correct()

        self.assertEqual(result["action"], "corrected")
        self.assertIn("correction", result)
        self.assertIn("bias_direction", result)

        # Verify prompt was modified
        new_content = prompt_path.read_text()
        self.assertIn("Auto-Correction Applied", new_content)
        self.assertIn("# Template", new_content)

    def test_apply_correction_archives_old_version(self):
        corrector, r2, prompt_path = self._make_corrector()
        correction = {
            "timestamp": "2026-01-15T10:00:00",
            "trigger": "high_failure_rate",
            "bias_direction": "overestimate",
            "failure_rate": 0.7,
            "avg_deviation": 0.25,
            "recommendation": "Adjust downward.",
        }

        corrector.apply_correction(correction)

        r2._put_object.assert_called_once()
        archive_key = r2._put_object.call_args[0][0]
        self.assertTrue(archive_key.startswith("state/evolution/prompt_versions/"))
        self.assertTrue(archive_key.endswith("_ipo_v1_template.md"))

    def test_apply_correction_missing_prompt_raises(self):
        r2 = MagicMock()
        corrector = PromptCorrector(
            r2, prompt_path="/nonexistent/path.md"
        )
        with self.assertRaises(CorrectionError):
            corrector.apply_correction({"timestamp": "x", "trigger": "t",
                                        "bias_direction": "up", "failure_rate": 0.5,
                                        "avg_deviation": 0.1, "recommendation": "r"})

    def test_apply_correction_r2_failure_raises(self):
        corrector, r2, _ = self._make_corrector()
        r2._put_object.side_effect = R2StorageError("upload failed")

        with self.assertRaises(CorrectionError) as ctx:
            corrector.apply_correction({
                "timestamp": "t", "trigger": "t",
                "bias_direction": "up", "failure_rate": 0.5,
                "avg_deviation": 0.1, "recommendation": "r",
            })
        self.assertIn("archive", str(ctx.exception).lower())

    def test_analyze_history_bias_direction(self):
        corrector, r2, _ = self._make_corrector(min_samples=3)

        # Positive deviations = overestimate
        history = [
            {"within_threshold": False, "deviation": 0.3},
            {"within_threshold": False, "deviation": 0.2},
            {"within_threshold": False, "deviation": 0.25},
        ]
        analysis = corrector._analyze_history(history)
        self.assertEqual(analysis["bias_direction"], "overestimate")

        # Negative deviations = underestimate
        history = [
            {"within_threshold": False, "deviation": -0.3},
            {"within_threshold": False, "deviation": -0.2},
            {"within_threshold": False, "deviation": -0.25},
        ]
        analysis = corrector._analyze_history(history)
        self.assertEqual(analysis["bias_direction"], "underestimate")

    def test_correction_content_appended(self):
        corrector, r2, prompt_path = self._make_corrector(
            prompt_content="# Original Prompt\n\nDo analysis."
        )
        correction = {
            "timestamp": "2026-02-01T12:00:00",
            "trigger": "high_failure_rate",
            "bias_direction": "underestimate",
            "failure_rate": 0.8,
            "avg_deviation": 0.2,
            "recommendation": "Adjust upward.",
        }

        corrector.apply_correction(correction)

        content = prompt_path.read_text()
        self.assertIn("# Original Prompt", content)
        self.assertIn("Do analysis.", content)
        self.assertIn("underestimate", content)
        self.assertIn("Adjust upward.", content)

    def test_invalid_json_history_returns_empty(self):
        corrector, r2, _ = self._make_corrector()
        r2.download_file.return_value = b"not json {{{{"

        result = corrector.check_and_correct()
        self.assertEqual(result["action"], "skip")
        self.assertEqual(result["reason"], "no_history")


if __name__ == "__main__":
    unittest.main()
