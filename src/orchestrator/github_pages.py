"""Build a minimal static site from R2 reports for GitHub Pages."""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from urllib.parse import quote

from src.storage.r2_client import R2Client

LOGGER = logging.getLogger(__name__)

_REPORT_KEY = re.compile(
    r"^reports/(?P<date>\d{4}-\d{2}-\d{2})/(?P<symbol>[^/]+)_report\.md$"
)


def _parse_report_key(key: str) -> tuple[str, str] | None:
    m = _REPORT_KEY.match(key)
    if not m:
        return None
    return m.group("date"), m.group("symbol").replace("_report", "").replace(".md", "")


def sync_reports_for_pages(
    r2: R2Client,
    site_root: Path,
    *,
    limit: int = 50,
) -> list[tuple[str, str, str]]:
    """Download recent reports under site_root/reports/. Returns (date, symbol, relative_href) sorted newest first."""
    site_root.mkdir(parents=True, exist_ok=True)
    reports_dir = site_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    keys = r2.list_objects("reports/")
    parsed: list[tuple[str, str, str]] = []
    for key in keys:
        parts = _parse_report_key(key)
        if not parts:
            continue
        date_str, symbol = parts
        parsed.append((date_str, symbol, key))

    parsed.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = parsed[:limit]

    meta: list[tuple[str, str, str]] = []
    for date_str, symbol, key in selected:
        body = r2.download_file(key)
        rel = key.replace("reports/", "")
        local_path = reports_dir / rel
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(body)
        href = "reports/" + quote(rel, safe="/")
        meta.append((date_str, symbol, href))
        LOGGER.info("Synced %s", key)

    return meta


def write_index_html(
    site_root: Path,
    items: list[tuple[str, str, str]],
    *,
    max_links: int = 20,
) -> None:
    """Write site_root/index.html listing the newest reports."""
    shown = items[:max_links]
    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '  <meta charset="utf-8">',
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "  <title>NovaSignal Reports</title>",
        "  <style>",
        "    body { font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; }",
        "    ul { line-height: 1.75; }",
        "  </style>",
        "</head>",
        "<body>",
        "  <h1>NovaSignal Reports</h1>",
        f"  <p>Total reports synced: {len(items)} (showing {len(shown)}).</p>",
        "  <ul>",
    ]
    for date_str, symbol, href in shown:
        label = html.escape(f"{symbol} ({date_str})")
        lines.append(f'    <li><a href="{href}">{label}</a></li>')
    lines.extend(["  </ul>", "</body>", "</html>"])
    (site_root / "index.html").write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Wrote index.html with %d links", len(shown))


def build_site(site_root: str | Path = "site", *, limit: int = 50, max_links: int = 20) -> None:
    """Sync from R2 and write index.html (requires R2 env vars)."""
    root = Path(site_root)
    r2 = R2Client()
    items = sync_reports_for_pages(r2, root, limit=limit)
    write_index_html(root, items, max_links=max_links)


def main() -> None:
    import argparse

    from src.fetchers.fmp_client import configure_logging

    parser = argparse.ArgumentParser(description="Build GitHub Pages static site from R2 reports")
    parser.add_argument("--site-dir", default="site", help="Output directory")
    parser.add_argument("--limit", type=int, default=50, help="Max report objects to download")
    parser.add_argument("--max-links", type=int, default=20, help="Max links on index page")
    args = parser.parse_args()
    configure_logging()
    build_site(args.site_dir, limit=args.limit, max_links=args.max_links)


if __name__ == "__main__":
    main()
