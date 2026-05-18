"""Sync R2 reports into the Hugo site; Hugo renders HTML for GitHub Pages."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

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


def _safe_stem_symbol(symbol: str) -> str:
    """Filesystem- and URL-safe token for report basename / slug."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


def _report_basename(date_str: str, symbol: str) -> str:
    return f"{date_str}-{_safe_stem_symbol(symbol)}"


def _write_front_matter_md(path: Path, *, front: dict[str, Any], body: str) -> None:
    header = yaml.safe_dump(
        front,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    path.write_text(f"---\n{header}\n---\n\n{body.lstrip()}", encoding="utf-8")


def _clear_hugo_reports(hugo_root: Path) -> None:
    reports_dir = hugo_root / "content" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    for p in reports_dir.glob("*.md"):
        p.unlink()


def sync_reports_to_hugo(
    r2: R2Client,
    hugo_root: Path,
    *,
    limit: int = 50,
) -> list[tuple[str, str, str]]:
    """Download reports into ``hugo_root/content/reports/``.

    Returns ``(date_str, symbol, url_path)`` sorted newest first. ``url_path``
    matches Hugo ``permalink: /reports/:slug/``.
    """
    _clear_hugo_reports(hugo_root)
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

    reports_dir = hugo_root / "content" / "reports"
    meta: list[tuple[str, str, str]] = []
    for date_str, symbol, key in selected:
        body = r2.download_file(key).decode("utf-8", errors="replace")
        basename = _report_basename(date_str, symbol)
        out = reports_dir / f"{basename}.md"
        front: dict[str, Any] = {
            "title": f"{symbol} · IPO 研报",
            "symbol": symbol,
            "report_date": date_str,
            "date": f"{date_str}T00:00:00Z",
            "slug": basename,
        }
        _write_front_matter_md(out, front=front, body=body)
        url_path = f"/reports/{basename}/"
        meta.append((date_str, symbol, url_path))
        LOGGER.info("Wrote Hugo report %s from %s", out.name, key)

    return meta


def _write_build_data(hugo_root: Path, *, home_report_limit: int) -> None:
    data_dir = hugo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    build_path = data_dir / "novasignal_build.yml"
    build_path.write_text(
        yaml.safe_dump(
            {"home_report_limit": home_report_limit},
            allow_unicode=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )


def prepare_hugo_site(
    hugo_root: str | Path = "hugo",
    *,
    limit: int = 50,
    home_report_limit: int = 20,
) -> None:
    """Pull reports from R2 into the Hugo tree (requires R2 env vars)."""
    root = Path(hugo_root)
    _write_build_data(root, home_report_limit=home_report_limit)
    r2 = R2Client()
    sync_reports_to_hugo(r2, root, limit=limit)


def main() -> None:
    import argparse

    from src.fetchers.fmp_client import configure_logging

    parser = argparse.ArgumentParser(
        description="Sync R2 reports into hugo/content/reports/ (run hugo separately)",
    )
    parser.add_argument(
        "--hugo-dir",
        default="hugo",
        help="Hugo project root (contains hugo.toml)",
    )
    parser.add_argument("--limit", type=int, default=50, help="Max report objects to sync")
    parser.add_argument(
        "--max-links",
        type=int,
        default=20,
        help="Max reports on home page (writes data/novasignal_build.yml)",
    )
    args = parser.parse_args()
    configure_logging()
    prepare_hugo_site(
        args.hugo_dir,
        limit=args.limit,
        home_report_limit=args.max_links,
    )


if __name__ == "__main__":
    main()
