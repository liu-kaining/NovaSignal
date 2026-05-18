"""Sandbox lifecycle management for agent execution."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """Raised when sandbox operations fail."""


@dataclass
class SandboxContext:
    """Represents an active sandbox workspace."""

    sandbox_dir: Path
    symbol: str
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def raw_data_path(self) -> Path:
        return self.sandbox_dir / "raw_data.json"

    @property
    def report_path(self) -> Path:
        return self.sandbox_dir / "report.md"

    @property
    def metrics_path(self) -> Path:
        return self.sandbox_dir / "metrics.json"

    @property
    def prompt_path(self) -> Path:
        return self.sandbox_dir / "prompt.md"


class SandboxManager:
    """Creates and manages isolated sandbox directories for agent runs."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else None

    def create(
        self,
        symbol: str,
        raw_data: dict[str, Any],
        prompt_content: str,
    ) -> SandboxContext:
        """Create a new sandbox with raw data and prompt files."""
        if not symbol or not symbol.strip():
            raise SandboxError("symbol must be a non-empty string")

        prefix = f"novasignal_{symbol.upper()}_"
        try:
            sandbox_dir = Path(
                tempfile.mkdtemp(prefix=prefix, dir=self._base_dir)
            )
        except OSError as exc:
            raise SandboxError(f"Failed to create sandbox directory: {exc}") from exc

        context = SandboxContext(
            sandbox_dir=sandbox_dir,
            symbol=symbol.upper(),
            raw_data=raw_data,
        )

        try:
            context.raw_data_path.write_text(
                json.dumps(raw_data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            context.prompt_path.write_text(prompt_content, encoding="utf-8")
        except OSError as exc:
            self.cleanup(context)
            raise SandboxError(f"Failed to write sandbox files: {exc}") from exc

        LOGGER.info("Created sandbox at %s for %s", sandbox_dir, symbol.upper())
        return context

    def cleanup(self, context: SandboxContext) -> None:
        """Remove the sandbox directory and all contents."""
        sandbox_dir = context.sandbox_dir
        if sandbox_dir.exists():
            try:
                shutil.rmtree(sandbox_dir)
                LOGGER.info("Cleaned up sandbox at %s", sandbox_dir)
            except OSError as exc:
                LOGGER.warning("Failed to cleanup sandbox %s: %s", sandbox_dir, exc)

    def extract_results(self, context: SandboxContext) -> dict[str, Any]:
        """Extract agent output files from the sandbox."""
        results: dict[str, Any] = {}

        report_path = context.report_path
        if report_path.exists():
            results["report"] = report_path.read_text(encoding="utf-8")
        else:
            LOGGER.warning("No report.md found in sandbox %s", context.sandbox_dir)
            results["report"] = None

        metrics_path = context.metrics_path
        if metrics_path.exists():
            try:
                raw = metrics_path.read_text(encoding="utf-8")
                results["metrics"] = json.loads(raw)
            except (json.JSONDecodeError, OSError) as exc:
                LOGGER.warning("Failed to parse metrics.json: %s", exc)
                results["metrics"] = None
        else:
            LOGGER.warning("No metrics.json found in sandbox %s", context.sandbox_dir)
            results["metrics"] = None

        return results
