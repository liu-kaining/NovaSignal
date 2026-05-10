import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.orchestrator.github_pages import sync_reports_for_pages, write_index_html


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
            root = Path(tmp)
            items = sync_reports_for_pages(r2, root, limit=10)

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0][0], "2026-01-10")
        self.assertEqual(items[0][1], "ZZZ")
        self.assertEqual(items[1][0], "2026-01-02")
        self.assertEqual(items[2][0], "2026-01-01")

    def test_write_index_html_contains_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            items = [
                ("2026-05-09", "NOVA", "reports/2026-05-09/NOVA_report.md"),
            ]
            write_index_html(root, items, max_links=5)
            html_text = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("NOVA", html_text)
        self.assertIn("2026-05-09", html_text)
        self.assertIn("reports/2026-05-09/NOVA_report.md", html_text)


if __name__ == "__main__":
    unittest.main()
