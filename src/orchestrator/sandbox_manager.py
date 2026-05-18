"""Sandbox lifecycle management for the multi-stage agent pipeline.

The original ``create()`` / ``extract_results()`` / ``cleanup()`` API is
preserved for backward compatibility with single-stage callers and tests.

New helpers (``setup_drafter_sandbox``, ``setup_reviewer_sandbox``,
``inject_critique``, ``extract_final_artifacts``, ``cleanup_paths``) power
the Drafter → Reviewer → Reviser flow.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.orchestrator.data_partitioner import build_index_md, split_raw_data

LOGGER = logging.getLogger(__name__)


class SandboxError(RuntimeError):
    """Raised when sandbox operations fail."""


@dataclass
class SandboxContext:
    """Represents an active sandbox workspace (single-stage legacy)."""

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


@dataclass
class MultiStageContext:
    """Holds both the drafter sandbox (persistent across A/C) and the reviewer sandbox (B only)."""

    symbol: str
    drafter_dir: Path
    reviewer_dir: Path

    @property
    def report_path(self) -> Path:
        return self.drafter_dir / "report.md"

    @property
    def metrics_path(self) -> Path:
        return self.drafter_dir / "metrics.json"

    @property
    def research_notes_path(self) -> Path:
        return self.drafter_dir / "research_notes.md"

    @property
    def critique_path(self) -> Path:
        return self.drafter_dir / "critique.md"

    @property
    def reviewer_critique_path(self) -> Path:
        return self.reviewer_dir / "critique.md"

    @property
    def all_dirs(self) -> tuple[Path, Path]:
        return (self.drafter_dir, self.reviewer_dir)


class SandboxManager:
    """Creates and manages isolated sandbox directories for agent runs.

    Single-stage API (``create`` / ``extract_results`` / ``cleanup``) is
    preserved for older tests and any caller that doesn't need the multi-stage
    workflow. New code should prefer the ``setup_*`` / ``extract_final_*``
    multi-stage helpers.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else None

    # -----------------------------
    # Legacy single-stage API
    # -----------------------------

    def create(
        self,
        symbol: str,
        raw_data: dict[str, Any],
        prompt_content: str,
    ) -> SandboxContext:
        """Create a single sandbox with raw_data.json and prompt.md."""
        if not symbol or not symbol.strip():
            raise SandboxError("symbol must be a non-empty string")

        prefix = f"novasignal_{symbol.upper()}_"
        try:
            sandbox_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=self._base_dir))
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
        """Remove a single-stage sandbox directory."""
        sandbox_dir = context.sandbox_dir
        if sandbox_dir.exists():
            try:
                shutil.rmtree(sandbox_dir)
                LOGGER.info("Cleaned up sandbox at %s", sandbox_dir)
            except OSError as exc:
                LOGGER.warning("Failed to cleanup sandbox %s: %s", sandbox_dir, exc)

    def extract_results(self, context: SandboxContext) -> dict[str, Any]:
        """Extract report.md + metrics.json from a single-stage sandbox."""
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

    # -----------------------------
    # New multi-stage API
    # -----------------------------

    def setup_drafter_sandbox(
        self,
        symbol: str,
        raw_data: dict[str, Any],
        drafter_prompt: str,
    ) -> Path:
        """Build the drafter sandbox with INDEX.md + data/*.json + prompt.md.

        Returns the sandbox directory path. Caller is responsible for cleanup
        via ``cleanup_paths``.
        """
        if not symbol or not symbol.strip():
            raise SandboxError("symbol must be a non-empty string")

        prefix = f"novasignal_drafter_{symbol.upper()}_"
        try:
            sandbox_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=self._base_dir))
        except OSError as exc:
            raise SandboxError(f"Failed to create drafter sandbox: {exc}") from exc

        try:
            partitions = split_raw_data(raw_data)
            data_dir = sandbox_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            for filename, payload in partitions.items():
                (data_dir / filename).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )

            (sandbox_dir / "INDEX.md").write_text(
                build_index_md(raw_data, partitions), encoding="utf-8"
            )
            (sandbox_dir / "prompt.md").write_text(drafter_prompt, encoding="utf-8")

            # Provide an empty research_notes.md so agent can immediately Edit / append.
            (sandbox_dir / "research_notes.md").write_text(
                f"# Research Notes — {symbol.upper()}\n\n_(append your Web research findings here)_\n",
                encoding="utf-8",
            )
        except OSError as exc:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
            raise SandboxError(f"Failed to populate drafter sandbox: {exc}") from exc

        LOGGER.info("Drafter sandbox ready at %s for %s", sandbox_dir, symbol.upper())
        return sandbox_dir

    def setup_reviewer_sandbox(
        self,
        symbol: str,
        drafter_dir: Path,
        reviewer_prompt: str,
    ) -> Path:
        """Build the reviewer sandbox by copying drafter outputs + data catalog.

        The reviewer needs:
          - prompt.md (reviewer prompt)
          - INDEX.md + data/  (to fact-check against source data)
          - report_v1.md      (the draft to critique — copied from drafter)
          - metrics_v1.json   (the draft metrics)
          - research_notes.md (drafter's research log)
        """
        if not drafter_dir.exists():
            raise SandboxError(f"Drafter sandbox does not exist: {drafter_dir}")

        prefix = f"novasignal_reviewer_{symbol.upper()}_"
        try:
            sandbox_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=self._base_dir))
        except OSError as exc:
            raise SandboxError(f"Failed to create reviewer sandbox: {exc}") from exc

        try:
            (sandbox_dir / "prompt.md").write_text(reviewer_prompt, encoding="utf-8")

            # Copy data directory + INDEX.md
            src_data = drafter_dir / "data"
            if src_data.exists():
                shutil.copytree(src_data, sandbox_dir / "data")
            src_index = drafter_dir / "INDEX.md"
            if src_index.exists():
                shutil.copy2(src_index, sandbox_dir / "INDEX.md")

            # Copy drafter outputs (renamed to _v1)
            report = drafter_dir / "report.md"
            if report.exists():
                shutil.copy2(report, sandbox_dir / "report_v1.md")
            else:
                LOGGER.warning(
                    "Drafter produced no report.md for %s — reviewer will critique an empty draft",
                    symbol,
                )
                (sandbox_dir / "report_v1.md").write_text(
                    "_(drafter failed to produce report.md)_\n", encoding="utf-8"
                )

            metrics = drafter_dir / "metrics.json"
            if metrics.exists():
                shutil.copy2(metrics, sandbox_dir / "metrics_v1.json")
            else:
                (sandbox_dir / "metrics_v1.json").write_text("{}\n", encoding="utf-8")

            research = drafter_dir / "research_notes.md"
            if research.exists():
                shutil.copy2(research, sandbox_dir / "research_notes.md")
            else:
                (sandbox_dir / "research_notes.md").write_text(
                    f"# Research Notes — {symbol.upper()}\n", encoding="utf-8"
                )
        except OSError as exc:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
            raise SandboxError(f"Failed to populate reviewer sandbox: {exc}") from exc

        LOGGER.info("Reviewer sandbox ready at %s for %s", sandbox_dir, symbol.upper())
        return sandbox_dir

    def inject_critique(
        self,
        drafter_dir: Path,
        reviewer_dir: Path,
        reviser_prompt: str,
    ) -> None:
        """Copy critique.md from reviewer sandbox into drafter sandbox + swap in reviser prompt.

        After this call, the drafter sandbox is ready for the reviser stage:
          - prompt.md  is the REVISER prompt (replacing the drafter prompt)
          - critique.md is present (from reviewer)
          - report.md / metrics.json / research_notes.md / data/ remain (for in-place editing)
        """
        if not drafter_dir.exists():
            raise SandboxError(f"Drafter sandbox missing: {drafter_dir}")
        if not reviewer_dir.exists():
            raise SandboxError(f"Reviewer sandbox missing: {reviewer_dir}")

        critique = reviewer_dir / "critique.md"
        if critique.exists():
            shutil.copy2(critique, drafter_dir / "critique.md")
        else:
            LOGGER.warning(
                "Reviewer produced no critique.md; injecting placeholder so reviser self-checks anyway"
            )
            (drafter_dir / "critique.md").write_text(
                "# Reviewer Critique\n\n## Overall Verdict\n\n**NEEDS_REVISION**\n\n"
                "Reviewer stage failed to produce critique. Run the §5 self-check rigorously "
                "and fix any gates the pre-commit script flagged.\n",
                encoding="utf-8",
            )

        try:
            (drafter_dir / "prompt.md").write_text(reviser_prompt, encoding="utf-8")
        except OSError as exc:
            raise SandboxError(f"Failed to install reviser prompt: {exc}") from exc

        LOGGER.debug("Injected critique into drafter sandbox %s", drafter_dir)

    def extract_final_artifacts(self, drafter_dir: Path) -> dict[str, Any]:
        """Pull all final artifacts from the drafter sandbox after stage C."""
        results: dict[str, Any] = {
            "report": None,
            "metrics": None,
            "research_notes": None,
            "critique": None,
        }

        for key, fname in (
            ("report", "report.md"),
            ("research_notes", "research_notes.md"),
            ("critique", "critique.md"),
        ):
            path = drafter_dir / fname
            if path.exists():
                try:
                    results[key] = path.read_text(encoding="utf-8")
                except OSError as exc:
                    LOGGER.warning("Failed to read %s: %s", path, exc)

        metrics_path = drafter_dir / "metrics.json"
        if metrics_path.exists():
            try:
                results["metrics"] = json.loads(
                    metrics_path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, OSError) as exc:
                LOGGER.warning("Failed to parse final metrics.json: %s", exc)

        return results

    def cleanup_paths(self, *paths: Path | None) -> None:
        """Remove one or more sandbox directories defensively."""
        for path in paths:
            if path is None:
                continue
            if path.exists():
                try:
                    shutil.rmtree(path)
                    LOGGER.info("Cleaned up sandbox at %s", path)
                except OSError as exc:
                    LOGGER.warning("Failed to cleanup %s: %s", path, exc)

    def cleanup_multi_stage(self, ctx: MultiStageContext | None) -> None:
        """Cleanup helper for MultiStageContext (drafter + reviewer dirs)."""
        if ctx is None:
            return
        self.cleanup_paths(*ctx.all_dirs)
