import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.orchestrator.github_pages import prepare_hugo_site, sync_reports_to_hugo


class GitHubPagesTest(unittest.TestCase):
    def test_sync_sorts_newest_dates_first_and_skips_bad_keys(self):
        r2 = MagicMock()
        r2.list_objects.return_value = [
            "reports/2026-01-01/AAA_report.md",
            "reports/2026-01-10/ZZZ_report.md",
            "reports/2026-01-02/BBB_report.md",
            "reports/not-a-report.txt",
        ]
        r2.download_file.side_effect = lambda k: f"body-{k}".encode()

        with tempfile.TemporaryDirectory() as tmp:
            hugo = Path(tmp) / "hugo"
            (hugo / "content" / "reports").mkdir(parents=True)
            items = sync_reports_to_hugo(r2, hugo, limit=10)
            out = hugo / "content" / "reports" / "2026-01-10-ZZZ.md"
            self.assertTrue(out.is_file())
            self.assertIn("symbol: ZZZ", out.read_text(encoding="utf-8"))
            self.assertIn("slug: 2026-01-10-ZZZ", out.read_text(encoding="utf-8"))

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0][0], "2026-01-10")
        self.assertEqual(items[0][1], "ZZZ")
        self.assertEqual(items[0][2], "/reports/2026-01-10-ZZZ/")
        self.assertEqual(items[1][0], "2026-01-02")
        self.assertEqual(items[2][0], "2026-01-01")

    @patch("src.orchestrator.github_pages.R2Client")
    def test_prepare_hugo_site_writes_build_data(self, mock_r2_cls):
        mock_r2 = MagicMock()
        mock_r2_cls.return_value = mock_r2
        mock_r2.list_objects.return_value = ["reports/2026-05-09/NOVA_report.md"]
        mock_r2.download_file.return_value = b"# Hello\n"

        with tempfile.TemporaryDirectory() as tmp:
            hugo = Path(tmp) / "hugo"
            (hugo / "content" / "reports").mkdir(parents=True)
            (hugo / "data").mkdir(parents=True)
            prepare_hugo_site(hugo, limit=10, home_report_limit=15)
            data = (hugo / "data" / "novasignal_build.yml").read_text(encoding="utf-8")

        self.assertIn("home_report_limit: 15", data)


if __name__ == "__main__":
    unittest.main()
