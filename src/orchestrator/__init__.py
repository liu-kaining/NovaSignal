"""Pipeline orchestration utilities."""

from __future__ import annotations

from typing import Any

from .async_runner import AgentResult, AgentRunError, invoke_agent
from .sandbox_manager import SandboxContext, SandboxError, SandboxManager

__all__ = [
    "AgentResult",
    "AgentRunError",
    "PipelineError",
    "SandboxContext",
    "SandboxError",
    "SandboxManager",
    "invoke_agent",
    "run_pipeline",
]


def __getattr__(name: str) -> Any:
    """Lazy-import run_pipeline so `python -m src.orchestrator.run_pipeline` avoids runpy warnings."""
    if name == "PipelineError":
        from .run_pipeline import PipelineError

        return PipelineError
    if name == "run_pipeline":
        from .run_pipeline import run_pipeline

        return run_pipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
